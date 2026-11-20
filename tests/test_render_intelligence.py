"""Exercise final rendering, effects validation and subtitle clocks with real media."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from server.media import binary, probe, render, run
from server.planner import validate_timeline


ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / 'work' / 'render-intelligence-tests'
SCRATCH.mkdir(parents=True, exist_ok=True)


class RenderIntelligence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.folder = Path(cls.temp.name)
        (cls.folder / 'media').mkdir()
        cls.video = cls.folder / 'media' / 'video.mp4'
        run([binary('ffmpeg'), '-v', 'error', '-nostdin', '-y',
             '-f', 'lavfi', '-i', 'color=c=0x204080:s=640x360:r=30:d=2',
             '-f', 'lavfi', '-i', 'sine=frequency=330:sample_rate=48000:duration=2',
             '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ac', '2',
             '-threads', '2', '-shortest', cls.video], 40)
        photo = Image.new('RGB', (640, 360), (96, 96, 96))
        ImageDraw.Draw(photo).rectangle((200, 80, 440, 280), fill=(190, 190, 190))
        photo.save(cls.folder / 'media' / 'photo.png')
        cls.media = [
            {'id': 'video', 'name': 'video.mp4', 'kind': 'video', 'file': 'media/video.mp4',
             'duration': 2, 'has_audio': True},
            {'id': 'photo', 'name': 'photo.png', 'kind': 'image', 'file': 'media/photo.png',
             'duration': 0, 'has_audio': False},
        ]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def project(self):
        return {'aspect': '16:9', 'media': copy.deepcopy(self.media), 'music_id': None,
                'music_volume': .1, 'timeline': [
                    {'media_id': 'video', 'start': 0, 'duration': 2, 'volume': .4, 'caption': 'VIDEO'},
                    {'media_id': 'photo', 'start': 0, 'duration': 2, 'volume': 1, 'caption': 'PHOTO',
                     'look': 'warm', 'motion': 'push_in',
                     'transition': {'type': 'dissolve', 'duration': .4}},
                    {'media_id': 'video', 'start': 1, 'duration': 1, 'volume': .4, 'caption': 'END'},
                ]}

    def frame(self, output, seconds):
        data = run([binary('ffmpeg'), '-v', 'error', '-nostdin', '-ss', str(seconds),
                    '-i', output, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'], 40)
        return Image.frombytes('RGB', (640, 360), data)

    def video_frames(self, path):
        result = json.loads(run([binary('ffprobe'), '-v', 'error', '-select_streams', 'v:0',
                                 '-count_frames', '-show_entries', 'stream=nb_read_frames,r_frame_rate',
                                 '-of', 'json', path], 40))['streams'][0]
        self.assertEqual(result['r_frame_rate'], '30/1')
        return int(result['nb_read_frames'])

    def test_twenty_quarter_second_cuts_share_one_global_frame_clock(self):
        from server.effects import join_parts
        for captioned in (True, False):
            with self.subTest(captioned=captioned):
                project = self.project()
                project['timeline'] = [
                    {'media_id': 'video', 'start': 1.75, 'duration': .25,
                     'volume': .4, 'caption': str(i + 1) if captioned else ''} for i in range(20)]
                before = copy.deepcopy(project)
                observed = []

                def inspect_parts(parts, clips, output):
                    observed.extend(self.video_frames(part) for part in parts)
                    return join_parts(parts, clips, output)

                with patch('server.effects.join_parts', side_effect=inspect_parts):
                    relative, info = render(project, self.folder, f'quarter-cuts-{captioned}', lambda *args: None)
                self.assertEqual(project, before)
                self.assertEqual(observed, [8, 7] * 10)
                self.assertEqual(self.video_frames(self.folder / relative), 150)
                self.assertAlmostEqual(info['duration'], 5, delta=1 / 60)
                captions = (self.folder / relative).with_name('captions.ass').read_text(encoding='utf-8-sig')
                events = [line.split(',') for line in captions.splitlines() if line.startswith('Dialogue:')]
                self.assertEqual(len(events), 20 if captioned else 0)
                if captioned:
                    self.assertEqual((events[-1][2], events[-1][-1]), ('0:00:05.00', '20'))

    def test_fractional_overlaps_preserve_global_length_and_subtitle_boundaries(self):
        project = self.project()
        project['timeline'] = [
            {'media_id': 'video', 'start': 0, 'duration': .551, 'volume': .4, 'caption': 'A'},
            {'media_id': 'photo', 'duration': .733, 'look': 'warm', 'motion': 'push_in',
             'caption': 'B', 'transition': {'type': 'dissolve', 'duration': .137}},
            {'media_id': 'video', 'start': 1.4, 'duration': .517, 'volume': .4, 'caption': 'C',
             'transition': {'type': 'fadeblack', 'duration': .173}},
            {'media_id': 'photo', 'duration': .431, 'caption': 'D'},
        ]
        before = copy.deepcopy(project)
        from server.effects import join_parts
        observed = []

        def inspect_parts(parts, clips, output):
            observed.extend(copy.deepcopy(clips))
            for part, clip in zip(parts, clips):
                self.assertEqual(self.video_frames(part), round(clip['duration'] * 30))
            return join_parts(parts, clips, output)

        with patch('server.effects.join_parts', side_effect=inspect_parts):
            relative, info = render(project, self.folder, 'fractional-overlaps', lambda *args: None)
        self.assertEqual(project, before)
        logical_length = .551 + .733 + .517 + .431 - .137 - .173
        self.assertEqual(self.video_frames(self.folder / relative), 58)
        self.assertAlmostEqual(info['duration'], logical_length, delta=1 / 60)
        self.assertEqual(observed[1]['transition'], {'type': 'dissolve', 'duration': 4 / 30})
        self.assertEqual(observed[2]['transition'], {'type': 'fadeblack', 'duration': 5 / 30})
        captions = (self.folder / relative).with_name('captions.ass').read_text(encoding='utf-8-sig')
        events = [line.split(',') for line in captions.splitlines() if line.startswith('Dialogue:')]
        self.assertEqual([event[-1] for event in events], ['A', 'B', 'C', 'D'])
        self.assertTrue(all(events[i][2] == events[i + 1][1] for i in range(3)))
        self.assertEqual(events[-1][2], '0:00:01.93')

    def test_final_mixed_render_has_warm_photo_motion_and_synchronized_captions(self):
        project = self.project()
        before = copy.deepcopy(project)
        progress = []
        relative, info = render(project, self.folder, 'mixed', lambda n, text: progress.append(n))
        self.assertEqual(project, before, 'render must not mutate the editable project')
        self.assertEqual(progress[-1], 100)
        self.assertEqual((info['width'], info['height']), (640, 360))
        self.assertTrue(info['has_audio'])
        self.assertAlmostEqual(info['duration'], 4.6, delta=.12)
        output = self.folder / relative
        self.assertAlmostEqual(probe(output)['duration'], 4.6, delta=.12)

        # Parse the actual ASS artifact, independently checking all clocks and boundaries.
        script = (output.parent / 'captions.ass').read_text(encoding='utf-8-sig')
        captions = [line.split(',') for line in script.splitlines() if line.startswith('Dialogue:')]
        self.assertEqual([(c[1], c[2], c[-1]) for c in captions], [
            ('0:00:00.00', '0:00:01.60', 'VIDEO'),
            ('0:00:01.60', '0:00:03.60', 'PHOTO'),
            ('0:00:03.60', '0:00:04.60', 'END'),
        ])
        self.assertTrue(all(captions[i][2] <= captions[i + 1][1] for i in range(len(captions) - 1)))

        early, late = self.frame(output, 2.2), self.frame(output, 3.4)
        warm = early.getpixel((20, 20))
        self.assertGreater(warm[0] - warm[2], 1, 'warm must affect the rendered pixels')
        early_width = sum(early.getpixel((x, 180))[0] > 150 for x in range(640))
        late_width = sum(late.getpixel((x, 180))[0] > 150 for x in range(640))
        self.assertGreaterEqual(late_width - early_width, 3, 'push_in must enlarge photo detail over time')
        white_caption_pixels = sum(min(early.getpixel((x, y))) > 220
                                   for y in range(290, 360) for x in range(640))
        self.assertGreater(white_caption_pixels, 15, 'the final film must contain burned-in captions')

    def test_only_photos_accept_push_or_pull_motion(self):
        for motion in ('push_in', 'pull_out'):
            with self.subTest(motion=motion):
                project = self.project()
                project['timeline'][1]['motion'] = motion
                self.assertEqual(validate_timeline(project['timeline'], self.media)[1]['motion'], motion)
                project['timeline'][0]['motion'] = motion
                with self.assertRaisesRegex(ValueError, '仅支持照片'):
                    validate_timeline(project['timeline'], self.media)

    def test_malformed_effects_are_rejected_before_media_processing(self):
        for effect in [
            {'look': ['warm']}, {'look': 'warm,drawtext=text=untrusted'},
            {'motion': {'filter': 'zoompan'}}, {'motion': 'push_in;movie=remote'},
            {'transition': 'dissolve'},
            {'transition': {'type': 'dissolve', 'duration': {'value': .4}}},
        ]:
            with self.subTest(effect=effect):
                project = self.project()
                project['timeline'][1].update(effect)
                with patch('server.media.run') as ffmpeg, self.assertRaises(ValueError):
                    render(project, self.folder, 'rejected', lambda *args: None)
                ffmpeg.assert_not_called()

    def test_bad_numeric_parameters_have_validation_errors(self):
        for key, value in [('duration', None), ('start', {}), ('volume', []), ('volume', True)]:
            with self.subTest(key=key, value=value):
                project = self.project()
                project['timeline'][0][key] = value
                with self.assertRaises(ValueError):
                    validate_timeline(project['timeline'], self.media)


if __name__ == '__main__':
    unittest.main(verbosity=2)
