import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from consumer_picture import make_job, plan_picture
from pipeline import Pipeline, TOOLS_DIR, find_tool
from quality import inspect_media
from video_sr import installed_engine, InferenceUnavailable


class SuperResolutionTests(unittest.TestCase):
    def test_eligibility_preserves_hdr_precision_and_video_geometry(self):
        base = dict(width=1280, height=720, fps=24, fps_fraction="24", bit_depth=8,
                    pix_fmt="yuv420p", duration=1, color_transfer="bt709")
        self.assertEqual(plan_picture(base)["picture_plan"]["sr_method"], "realesrgan_x4plus")
        for changes in (dict(bit_depth=10), dict(color_transfer="smpte2084"),
                        dict(color_transfer="arib-std-b67"), dict(width=3840, height=2160)):
            with self.subTest(changes=changes):
                self.assertEqual(plan_picture(dict(base, **changes))["picture_plan"]["sr_method"], "native")

    def test_missing_package_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(InferenceUnavailable):
            installed_engine(Path(tmp))

    @unittest.skipUnless(shutil.which("ffmpeg") and (TOOLS_DIR / "models/realesrgan-x4plus.bin").is_file(),
                         "requires installed Real-ESRGAN engine and weights")
    def test_real_model_static_cadence_and_full_colour_export(self):
        try:
            installed_engine(TOOLS_DIR)
        except InferenceUnavailable as exc:
            self.skipTest(str(exc))
        ff = find_tool("ffmpeg")
        def command(*args):
            return subprocess.run([ff, "-nostdin", "-v", "error", *map(str, args)],
                                  capture_output=True, check=True, timeout=60).stdout
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, final = root / "bars.mkv", root / "result.mp4"
            command("-f", "lavfi", "-i", "smptebars=size=160x96:rate=24", "-frames:v", 6,
                    "-c:v", "ffv1", "-color_trc", "bt709", "-colorspace", "bt709",
                    "-color_primaries", "bt709", source)
            cfg = make_job(inspect_media(source), source, final, workspace=root / "scratch")
            # Smaller output for the colour/temporal integration test. The real
            # 8K consumer picture smoke separately asserts the delivery raster.
            cfg.update(width=640, height=384, preset="ultrafast", crf=4)
            cfg["picture_plan"].update(active_size=[640, 384], output_size=[640, 384])
            Pipeline(cfg).run()
            report = json.loads(Path(str(final) + ".quality.json").read_text(encoding="utf-8"))
            sr = report["export"]["video_super_resolution"]
            self.assertTrue(sr["applied"], sr["detail"])
            self.assertEqual(sr["model_output_size"], [640, 384])
            self.assertEqual(sr["inferred_frames"], 1, "repeat identical pictures using the exact same model output")
            self.assertEqual(sr["encoded_frames"], 12)
            self.assertEqual(report["export"]["rpu_final_validation"]["l1_frames_verified"], 12)
            self.assertEqual(report["media"]["fps_fraction"], "48")
            self.assertEqual(report["media"]["color_transfer"], "arib-std-b67")
