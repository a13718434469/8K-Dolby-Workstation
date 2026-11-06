from fractions import Fraction
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from consumer_picture import cadence_filter, make_job, plan_picture


def source(rate="24", **changes):
    data = dict(fps_fraction=rate, fps=float(Fraction(rate)), width=640, height=360,
                duration=1, bit_depth=8, pix_fmt="yuv420p", codec_name="h264",
                color_transfer="bt709", color_space="bt709", color_primaries="bt709",
                stream={"r_frame_rate": rate, "field_order": "progressive"})
    data.update(changes)
    return data


class ConsumerPictureTests(unittest.TestCase):
    def test_plan_fits_hd_into_8k_in_display_orientation(self):
        cases = [
            ({"width": 1280, "height": 720}, [7680, 4320]),
            ({"width": 1920, "height": 1080}, [7680, 4320]),
            ({"width": 720, "height": 1280}, [4320, 7680]),
            ({"width": 1440, "height": 1080}, [5760, 4320]),
            ({"width": 1920, "height": 800}, [7680, 3200]),
            ({"width": 1440, "height": 1080, "display_width": 1080, "display_height": 1440,
              "rotation": 90, "stream": {"sample_aspect_ratio": "4:3"}}, [4320, 7680]),
            ({"width": 720, "height": 480}, [6480, 4320]),
            ({"width": 3840, "height": 2160}, [7680, 4320]),
            ({"width": 7680, "height": 4320}, [7680, 4320]),
        ]
        for changes, active in cases:
            with self.subTest(changes=changes):
                p = plan_picture(source(**changes))
                output = [4320, 7680] if active[0] < active[1] else [7680, 4320]
                self.assertEqual([p["width"], p["height"]], output)
                self.assertEqual(p["picture_plan"]["output_size"], output)
                self.assertEqual(p["picture_plan"]["active_size"], active)
                self.assertEqual(p["picture_plan"]["source_size"],
                                 [changes.get("display_width", changes["width"]),
                                  changes.get("display_height", changes["height"])])

    def test_native_cadence_and_hdr_safety(self):
        for fps, expected in [("24000/1001", "48000/1001"), ("24", "48"), ("25", "50"),
                              ("30000/1001", "60000/1001"), ("30", "60")]:
            with self.subTest(fps=fps):
                plan = plan_picture(source(fps))
                self.assertEqual(plan["picture_plan"]["method"], "motion_interpolation")
                self.assertEqual(plan["fps_override"], expected)
        self.assertEqual(plan_picture(source("100"))["fps_override"], "50")
        self.assertEqual(plan_picture(source("60"))["picture_plan"]["method"], "keep")
        for item in (source(bit_depth=10, pix_fmt="yuv420p10le"), source(color_transfer="smpte2084"),
                     source(stream={"r_frame_rate": "60"})):
            self.assertNotIn("fps_override", plan_picture(item))
        self.assertEqual(plan_picture(source("25", stream={"field_order": "tt"}))["picture_plan"]["method"],
                         "deinterlace_fields")

    def test_compatible_dv_is_preserved_and_ambiguous_sources_are_actionable(self):
        dv = source(width=7680, height=4320, codec_name="hevc", bit_depth=10, pix_fmt="yuv420p10le",
                    color_transfer="arib-std-b67", color_space="bt2020nc", color_primaries="bt2020",
                    container="mov,mp4,m4a,3gp,3g2,mj2", dv_profile=8, dv_compatibility=4,
                    dv_rpu_present=True, dv_bl_present=True, dv_el_present=False,
                    stream={"side_data_list": [{"side_data_type": "DOVI configuration record", "dv_level": 11}]})
        p = plan_picture(dv)
        self.assertEqual(p["route"], "preserve")
        self.assertEqual(p["picture_plan"]["method"], "preserve")
        self.assertEqual(plan_picture(dict(dv, fps_fraction="48", fps=48))["route"], "transcode")
        p = plan_picture(dict(dv, width=1280, height=720))
        self.assertEqual(p["route"], "transcode")
        self.assertEqual(p["picture_plan"]["resize"], "upscale")
        self.assertEqual(p["rpu_mode"], "auto")
        self.assertEqual(p["color_mode"], "dv84")
        self.assertNotIn("fps_override", p)
        with self.assertRaisesRegex(ValueError, "HLG"):
            make_job(source(color_transfer="unknown", color_primaries="bt2020"), "in.mp4", "out.mp4")
        with tempfile.TemporaryDirectory() as temp:
            f = Path(temp) / "clip.mp4"
            (Path(temp) / "clip_B站投稿.mp4").touch()
            self.assertEqual(Path(make_job(source(), f)["output"]).name, "clip_B站投稿_2.mp4")

    @unittest.skipUnless(shutil.which("ffmpeg"), "requires FFmpeg")
    def test_one_click_makes_new_motion_frames_and_keeps_the_final_interval(self):
        # Compare lossless frame hashes before any color conversion or encoding.
        # On motion testsrc2, most between-frame positions must be synthesized.
        rate = Fraction(24000, 1001)
        p = plan_picture(source(str(rate)))["picture_plan"]
        def hashes(vf):
            data = subprocess.check_output([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                f"testsrc2=size=160x96:rate={rate}", "-vf", vf, "-f", "framemd5", "-"
            ], text=True, timeout=25)
            return [line.rsplit(",", 1)[1].strip() for line in data.splitlines() if not line.startswith("#")]
        original = hashes("trim=end_frame=24")
        processed = hashes("trim=end_frame=24,setpts=PTS-STARTPTS," +
                           cadence_filter(p, rate * 2, 24 / rate).rstrip(","))
        self.assertEqual(len(processed), 48)
        self.assertEqual(processed[::2], original)
        novel = [x for i, x in enumerate(processed[1:-1:2]) if x not in (original[i], original[i+1])]
        self.assertGreater(len(novel), 15)

    @unittest.skipUnless(os.environ.get("DOLBY_RUN_8K_SMOKE") == "1", "opt in to short 8K Dolby encode")
    def test_hd_input_actually_exports_8k_10bit_dv_with_rebuilt_rpu(self):
        from pipeline import Pipeline, find_tool
        from quality import inspect_media
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video, output = root / "hd.mkv", root / "uhd.mp4"
            subprocess.run([find_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=1280x720:rate=24", "-frames:v", "2", "-c:v", "ffv1",
                            "-color_primaries", "bt709", "-colorspace", "bt709", "-color_trc", "bt709", str(video)],
                           check=True, timeout=45)
            cfg = make_job(inspect_media(video), video, output, workspace=root / "work")
            cfg["preset"] = "ultrafast"
            Pipeline(cfg).run()
            rendered = inspect_media(output)
            for key, expected in (("width", 7680), ("height", 4320), ("fps_fraction", "48"),
                                  ("pix_fmt", "yuv420p10le"), ("dv_profile", 8), ("dv_compatibility", 4)):
                self.assertEqual(rendered[key], expected, key)
            dv = next(d for d in rendered["stream"]["side_data_list"] if d["side_data_type"] == "DOVI configuration record")
            self.assertGreaterEqual(dv["dv_level"], 12)  # 8K48 pixel rate
            report = json.loads(Path(str(output) + ".quality.json").read_text(encoding="utf-8"))
            self.assertEqual(report["export"]["rpu_final_validation"]["l1_frames_verified"], 4)
            copy = root / "8k-copy.mp4"
            cfg = make_job(rendered, output, copy, workspace=root / "preserve")
            self.assertEqual(cfg["route"], "preserve")
            Pipeline(cfg).run()
            copied = json.loads(Path(str(copy) + ".quality.json").read_text(encoding="utf-8"))
            self.assertEqual(copied["export"]["rpu_final_validation"]["preserved_rpu_frames"], 4)


if __name__ == "__main__":
    unittest.main()
