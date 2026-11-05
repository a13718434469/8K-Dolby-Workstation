"""Optional high-memory smoke: DOLBY_RUN_8K_SMOKE=1 enables real 8K encoding."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline import Pipeline


@unittest.skipUnless(os.environ.get("DOLBY_RUN_8K_SMOKE") == "1", "set DOLBY_RUN_8K_SMOKE=1 for the 8K smoke")
class EightKSmokeTests(unittest.TestCase):
    def test_single_frame_120fps_hdr10_batch_has_valid_timestamps(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg required")
        with tempfile.TemporaryDirectory(prefix="dolby-8k-") as temp:
            root = Path(temp)
            source = root / "input.mp4"
            subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=120",
                            "-frames:v", "1", "-c:v", "libx264", str(source)], check=True, capture_output=True)
            output = Pipeline({"video": str(source), "output": str(root / "8k.mp4"),
                               "workspace": str(root / "work"), "width": 7680, "height": 4320,
                               "ai": False, "interp": False, "color_mode": "hdr10",
                               "batch_size": 1, "preset": "ultrafast"}).run()
            result = subprocess.run([ffprobe, "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(output)],
                                    check=True, capture_output=True, text=True)
            video = json.loads(result.stdout)["streams"][0]
            self.assertEqual((video["width"], video["height"]), (7680, 4320))
            self.assertEqual(video["avg_frame_rate"], "120/1")
            self.assertEqual(video["nb_read_frames"], "1")
            self.assertEqual(video["pix_fmt"], "yuv420p10le")
            self.assertEqual(video["color_transfer"], "smpte2084")
