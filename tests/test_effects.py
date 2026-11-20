"""Real FFmpeg transition tests; all generated media stays under work/."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.effects import join_parts, timeline_duration, transition_for
from server.media import binary, probe, run


ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / 'work' / 'effects-tests'
SCRATCH.mkdir(parents=True, exist_ok=True)


class TransitionValidation(unittest.TestCase):
    def test_defaults_and_first_transition_are_cut(self):
        first = {'duration': 2, 'transition': 'ignored for the first shot'}
        second = {'duration': 2}
        self.assertEqual(transition_for(first), {'type': 'cut', 'duration': 0})
        self.assertEqual(timeline_duration([first, second]), 4)

    def test_overlap_is_subtracted_once(self):
        clips = [{'duration': 2}, {'duration': 3, 'transition': {'type': 'dissolve', 'duration': .4}},
                 {'duration': 2, 'transition': {'type': 'cut', 'duration': .4}},
                 {'duration': 1, 'transition': {'type': 'fadeblack', 'duration': .3}}]
        self.assertAlmostEqual(timeline_duration(clips), 7.3)

    def test_rejects_bad_type_and_duration(self):
        for transition in [None, 'dissolve', {'type': 'fade;movie=x'},
                           {'type': 'dissolve', 'duration': .09},
                           {'type': 'dissolve', 'duration': 1.3},
                           {'type': 'dissolve', 'duration': 1.01},
                           {'type': 'dissolve', 'duration': float('nan')},
                           {'type': 'dissolve', 'duration': True},
                           {'type': 'dissolve', 'duration': '.4'},
                           {'type': 'cut', 'duration': -1}]:
            with self.subTest(transition=transition), self.assertRaises(ValueError):
                transition_for({'duration': 2, 'transition': transition}, {'duration': 2})

    def test_rejects_bad_timeline(self):
        for timeline in [[], None, [{'duration': 0}], [{'duration': float('inf')}], ['bad']]:
            with self.subTest(timeline=timeline), self.assertRaises(ValueError):
                timeline_duration(timeline)


class RealTransitionRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.folder = Path(cls.temp.name)
        cls.parts = []
        for i, color in enumerate(['red', 'green', 'blue']):
            output = cls.folder / f'part-{i}.mp4'
            # Silent track represents a silent original after media.render's normalization.
            sound = 'anullsrc=r=48000:cl=stereo' if i == 0 else f'sine=frequency={440 * i}:sample_rate=48000:duration=2'
            run([binary('ffmpeg'), '-v', 'error', '-nostdin', '-y',
                 '-f', 'lavfi', '-i', f'color=c={color}:s=320x180:r=30:d=2',
                 '-f', 'lavfi', '-i', sound, '-t', '2',
                 '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ac', '2',
                 '-ar', '48000', '-threads', '2', output], 40)
            cls.parts.append(output)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def verify(self, output, duration):
        info = probe(output)
        self.assertEqual((info['width'], info['height']), (320, 180))
        self.assertTrue(info['has_audio'])
        self.assertAlmostEqual(info['duration'], duration, delta=.15)

    def test_all_cut_uses_stream_copy(self):
        output = self.folder / 'cuts.mp4'
        from server import media
        with patch('server.media.run', wraps=media.run) as calls:
            duration = join_parts(self.parts[:2], [{'duration': 2}, {'duration': 2}], output)
        self.assertEqual(duration, 4)
        self.assertIn('copy', calls.call_args_list[0].args[0])
        self.verify(output, 4)

    def test_each_allowed_transition_with_silent_track(self):
        for kind in ('dissolve', 'fadeblack', 'wipeleft', 'wiperight', 'slideleft', 'slideright', 'circleopen'):
            with self.subTest(kind=kind):
                output = self.folder / f'{kind}.mp4'
                duration = join_parts(self.parts[:2], [{'duration': 2}, {'duration': 2,
                                    'transition': {'type': kind, 'duration': .4}}], output)
                self.assertEqual(duration, 3.6)
                self.verify(output, 3.6)

    def test_mixed_transitions_and_cuts(self):
        output = self.folder / 'mixed.mp4'
        clips = [{'duration': 2}, {'duration': 2, 'transition': {'type': 'slideleft', 'duration': .4}},
                 {'duration': 2}, {'duration': 2, 'transition': {'type': 'fadeblack', 'duration': .3}}]
        duration = join_parts([self.parts[0], self.parts[1], self.parts[2], self.parts[0]], clips, output)
        self.assertEqual(duration, 7.3)
        self.verify(output, 7.3)

    def test_does_not_overwrite_sources_or_existing_output(self):
        for output in (self.parts[0], self.folder / 'cuts.mp4'):
            if not output.exists():
                output.write_bytes(b'existing file')
            with self.assertRaises(ValueError):
                join_parts(self.parts[:2], [{'duration': 2}, {'duration': 2}], output)

    def test_count_and_missing_file_are_rejected(self):
        with self.assertRaises(ValueError):
            join_parts(self.parts[:1], [{'duration': 2}, {'duration': 2}], self.folder / 'wrong-count.mp4')
        with self.assertRaises(ValueError):
            join_parts([self.folder / 'missing.mp4'], [{'duration': 2}], self.folder / 'missing-output.mp4')
        with self.assertRaises(ValueError):
            join_parts([None], [{'duration': 2}], self.folder / 'bad-input.mp4')


if __name__ == '__main__':
    unittest.main(verbosity=2)
