"""Local CV integration tests: real FFmpeg media, no account/model/network calls."""
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from server.media import binary, run
from server.understanding import analyze_media, sample_times, MAX_VIDEO_FRAMES


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT.parents[1] / 'work' / 'cv-verification'
SCRATCH.mkdir(parents=True, exist_ok=True)


class Understanding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.folder = Path(cls.temp.name)
        (cls.folder / 'media').mkdir()
        cls.video = cls.folder / 'media' / 'two-scenes.mp4'
        cls.photo = cls.folder / 'media' / 'photo.png'
        cls.long = cls.folder / 'media' / 'long.mp4'
        ff = binary('ffmpeg')
        run([ff, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=red:s=160x90:r=10:d=2',
             '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10:d=2',
             '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]',
             '-c:v', 'libx264', '-pix_fmt', 'yuv420p', cls.video])
        Image.new('RGB', (200, 120), (20, 100, 180)).save(cls.photo)
        run([ff, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=green:s=64x36:r=2:d=121',
             '-c:v', 'libx264', '-pix_fmt', 'yuv420p', cls.long])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def video_item(self):
        return {'id': 'two-scenes', 'kind': 'video', 'file': 'media/two-scenes.mp4', 'duration': 4}

    def test_01_scene_change_and_measurable_frames(self):
        result = analyze_media(self.video_item(), self.folder)
        self.assertTrue(result['coverage']['scan_complete'])
        self.assertGreaterEqual(len(result['scenes']), 2)
        self.assertAlmostEqual(result['scenes'][0]['end'], 2, delta=.5)
        self.assertEqual(result['scenes'][0]['start'], 0)
        self.assertEqual(result['scenes'][-1]['end'], 4)
        self.assertGreaterEqual(len(result['samples']), 4)
        self.assertLessEqual(len(result['samples']), MAX_VIDEO_FRAMES)
        self.assertEqual(sorted(s['time'] for s in result['samples']), [s['time'] for s in result['samples']])
        self.assertTrue(any(s['mean_rgb'][0] > s['mean_rgb'][2] for s in result['samples']))
        self.assertTrue(any(s['mean_rgb'][2] > s['mean_rgb'][0] for s in result['samples']))
        for sample in result['samples']:
            self.assertTrue((self.folder / sample['frame']).is_file())
            self.assertTrue(sample['frame'].startswith('media/'))
            self.assertGreaterEqual(sample['brightness'], 0)
            self.assertLessEqual(sample['brightness'], 1)
            self.assertGreaterEqual(sample['sharpness'], 0)
        self.assertFalse(result['coverage']['semantic_understanding'])

    def test_02_cache_unchanged_and_invalidated_when_source_changes(self):
        item = self.video_item()
        first = analyze_media(item, self.folder)
        second = analyze_media(item, self.folder)
        self.assertTrue(second['cache_hit'])
        self.assertEqual(first['frames'], second['frames'])
        saved = self.video.read_bytes()
        try:
            # A valid media file with changed bytes, rather than a fake metadata-only edit.
            run([binary('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                 'color=c=yellow:s=160x90:r=10:d=4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', self.video])
            updated = analyze_media(item, self.folder)
            self.assertFalse(updated['cache_hit'])
            self.assertNotEqual(first['frames'], updated['frames'])
        finally:
            self.video.write_bytes(saved)

    def test_03_photo_is_one_frame_and_audio_is_skipped(self):
        photo = analyze_media({'id': 'photo', 'kind': 'image', 'file': 'media/photo.png'}, self.folder)
        self.assertEqual(len(photo['frames']), 1)
        self.assertEqual(photo['samples'][0]['time'], 0)
        self.assertEqual(photo['scenes'], [])
        self.assertEqual(photo['coverage']['sample_count'], 1)
        audio = analyze_media({'kind': 'audio', 'duration': 12}, self.folder)
        self.assertEqual(audio['status'], 'skipped')
        self.assertEqual(audio['frames'], [])

    def test_04_long_video_uses_at_most_24_frames_across_whole_timeline(self):
        result = analyze_media({'id': 'long', 'kind': 'video', 'file': 'media/long.mp4', 'duration': 121}, self.folder)
        self.assertEqual(len(result['frames']), 24)
        self.assertLessEqual(result['coverage']['scan_frame_count'], 600)
        self.assertEqual(result['samples'][0]['time'], 0)
        self.assertGreater(result['samples'][-1]['time'], 120)
        self.assertLess(result['coverage']['max_gap'], 12)

    def test_05_bounds_and_paths(self):
        for duration in (0, -1, float('nan'), 3601):
            with self.assertRaises(ValueError):
                sample_times(duration, [])
        with self.assertRaises(ValueError):
            analyze_media({'id': 'bad', 'kind': 'image', 'file': '../outside.png'}, self.folder)
        with self.assertRaises(ValueError):
            analyze_media({'kind': 'unknown'}, self.folder)

    def test_06_many_scene_candidates_respect_frame_budget(self):
        scenes = [{'start': i * 10, 'end': (i + 1) * 10} for i in range(100)]
        times = sample_times(1000, scenes)
        self.assertLessEqual(len(times), 24)
        self.assertEqual(times[0], 0)
        self.assertGreater(times[-1], 999)
        self.assertTrue(all(0 <= t < 1000 for t in times))


if __name__ == '__main__':
    unittest.main(verbosity=2)
