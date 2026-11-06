from fractions import Fraction
from pathlib import Path
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
        p = plan_picture(source(codec_name="hevc", bit_depth=10, pix_fmt="yuv420p10le",
                                color_transfer="arib-std-b67", color_space="bt2020nc", color_primaries="bt2020",
                                container="mov,mp4,m4a,3gp,3g2,mj2", dv_profile=8, dv_compatibility=4,
                                dv_rpu_present=True, dv_bl_present=True, dv_el_present=False))
        self.assertEqual(p["route"], "preserve")
        self.assertEqual(p["picture_plan"]["method"], "preserve")
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


if __name__ == "__main__":
    unittest.main()
