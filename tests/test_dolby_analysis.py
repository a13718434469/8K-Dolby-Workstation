"""Numerical contracts and real-RPU tests for the independent CPU analyzer."""
import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
if HAS_NUMPY:
    import numpy as np
    import dolby_analysis as analysis

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg")
DOVI = shutil.which("dovi_tool") or next((str(p) for p in (ROOT / "tools" / "dovi_tool.exe", ROOT / "tools" / "dovi_tool") if p.is_file()), None)


def command(args):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=45)
    if result.returncode:
        raise AssertionError(result.stderr[-3000:] + result.stdout[-1000:])
    return result.stdout


@unittest.skipUnless(HAS_NUMPY, "numpy required")
class AnalysisMathTests(unittest.TestCase):
    def test_quantization_uses_half_up_and_cm4_clamps_are_separate(self):
        self.assertEqual(analysis.quantize_pq(2.5 / 4095), 3)
        self.assertEqual(analysis.quantize_pq(1.5 / 4095), 2)
        self.assertEqual(analysis.quantize_pq(-1), 0)
        self.assertEqual(analysis.quantize_pq(2), 4095)
        self.assertEqual(analysis.clamp_l1(100, 400, 200), {"min_pq": 12, "max_pq": 2081, "avg_pq": 1229})
        self.assertEqual(analysis.clamp_l1(0, 3000, 3000)["avg_pq"], 2999)

    def test_l1_average_is_maxrgb_in_pq_domain_after_2x2_reduction(self):
        rgb = np.zeros((2, 4, 3), dtype=np.float64)
        rgb[:, :2] = [.2, .1, .05]
        rgb[:, 2:] = [.1, .8, .3]
        minimum, maximum, total, count = analysis._reduce_rgb(rgb)
        self.assertAlmostEqual(minimum, .05)
        self.assertAlmostEqual(maximum, .8)
        self.assertAlmostEqual(total / count, .5)
        # Y-weighting and averaging linear light would produce other results.
        self.assertNotAlmostEqual(total / count, (.2627 * .2 + .6780 * .1 + .0593 * .05 + .2627 * .1 + .6780 * .8 + .0593 * .3) / 2)

    def test_2x2_reduction_precedes_maxrgb_not_mean_of_pixel_maxima(self):
        rgb = np.array([[[1., 0., 0.], [0., 1., 0.]], [[0., 0., 1.], [0., 0., 0.]]])
        self.assertEqual(analysis._reduce_rgb(rgb), (.25, .25, .25, 1))

    def test_native420_luma_filter_rounds_and_repeats_edge(self):
        source = np.arange(16, dtype=np.uint16).reshape(4, 4)
        np.testing.assert_array_equal(analysis.downsample_luma_420(source, 0, 2), [[2, 4], [10, 12]])

    def test_post_composer_chroma_interpolation_obeys_siting(self):
        plane = np.array([[0., 2.], [4., 6.]])
        left = analysis._interpolate_chroma(plane, 0, 4, 4, "left")
        top = analysis._interpolate_chroma(plane, 0, 4, 4, "topleft")
        self.assertEqual(left[0, 0], 0.)
        self.assertEqual(left[1, 0], 1.)
        self.assertEqual(top[1, 0], 2.)
        self.assertEqual(left[-1, -1], 6.)


@unittest.skipUnless(HAS_NUMPY and DOVI and FFMPEG, "numpy, ffmpeg and dovi_tool required")
class ActualComposerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="dolby-analysis-")
        cls.work = Path(cls.temp.name).resolve()
        template = cls.work / "template.json"
        template.write_text('{"cm_version":"V40","length":1}', encoding="utf-8")
        rpu = cls.work / "template.bin"
        command([DOVI, "generate", "-j", template, "-p", "8.4", "-o", rpu])
        text = command([DOVI, "info", "-i", rpu, "-f", "0"])
        cls.template = json.loads(text[text.index("{"):])
        cls.composer = analysis.Profile84Composer(cls.template)
        cls.source = cls.work / "alternating-hlg.mp4"
        command([FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:size=64x48:rate=6",
                 "-vf", "format=yuv420p10le,geq=lum='if(lt(N,3),128,900)':cb=512:cr=512", "-frames:v", "6",
                 "-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
                 "lossless=1:bframes=0:frame-threads=1:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
                 "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
                 "-colorspace", "bt2020nc", "-color_range", "tv", cls.source])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_polynomial_uses_global_code_over_1024(self):
        c = self.template["rpu_data_mapping"]["curves"][0]
        h = self.template["header"]
        pivot_codes = np.cumsum(c["pivots"])
        codes = np.array([0, 63, 64, 132, 721, 910, 940, 1023], dtype=np.uint16)
        expected = []
        for code in codes:
            clamped = min(int(pivot_codes[-1]), max(int(pivot_codes[0]), int(code)))
            piece = min(len(pivot_codes) - 2, max(0, sum(clamped >= int(p) for p in pivot_codes) - 1))
            s = clamped / 1024.
            coefficients = [a + b / (2 ** h["coefficient_log2_denom"])
                            for a, b in zip(c["poly_coef_int"][piece], c["poly_coef"][piece])]
            expected.append(min(1., max(0., sum(v * s ** power for power, v in enumerate(coefficients)))))
        np.testing.assert_allclose(self.composer.luma(codes), expected, atol=1e-13)

    def test_actual_dm_roundtrip_uses_dolby_lms_basis_not_generic_ipt(self):
        # P8.4's quantized rgb_to_lms almost equals Dolby's BT.2020 reference;
        # an unrelated IPT basis produces errors around 0.1 instead of 1e-4.
        np.testing.assert_allclose(self.composer.to_bt2020, np.eye(3), atol=2e-4)

    def test_actual_mmr_matches_scalar_fixedpoint_terms(self):
        y, cb, cr = (np.array([64, 400, 721, 940], dtype=np.uint16), np.array([512, 128, 900, 512], dtype=np.uint16), np.array([512, 800, 80, 512], dtype=np.uint16))
        actual = self.composer.chroma(y, cb, cr)
        curves = self.template["rpu_data_mapping"]["curves"]
        denominator = 2 ** self.template["header"]["coefficient_log2_denom"]
        for component in range(2):
            c = curves[component + 1]
            expected = []
            for pixel in range(4):
                values = []
                for sample, curve in zip((y[pixel], cb[pixel], cr[pixel]), curves):
                    pivots = np.cumsum(curve["pivots"])
                    values.append(min(pivots[-1], max(pivots[0], sample)) / 1024.)
                a, b, z = values
                terms = (a, b, z, a * b, a * z, b * z, a * b * z)
                value = c["mmr_constant_int"][0] + c["mmr_constant"][0] / denominator
                for power in range(c["mmr_order_minus1"][0] + 1):
                    value += sum((i + f / denominator) * term ** (power + 1)
                                 for i, f, term in zip(c["mmr_coef_int"][0][power], c["mmr_coef"][0][power], terms))
                expected.append(min(1., max(0., value)))
            np.testing.assert_allclose(actual[component], expected, atol=2e-13)

    def test_available_opencl_device_matches_encoded_l1_on_structured_pixels(self):
        try:
            import pyopencl as cl
        except ImportError:
            self.skipTest("Optional OpenCL binding unavailable")
        try:
            platforms = cl.get_platforms()
        except cl.Error:
            self.skipTest("No OpenCL platform")
        if not any(d.double_fp_config and d.type & cl.device_type.GPU
                   for p in platforms for d in p.get_devices()):
            self.skipTest("No FP64 OpenCL GPU")
        from dolby_opencl import verified_meter
        meter = verified_meter(self.composer, analysis.measure_frame)
        row = np.arange(384, dtype=np.uint16)
        y = (np.arange(218, dtype=np.uint16)[:, None] * 5 + row[None, :]) % 1024
        cb, cr = y[::2, ::2].copy(), np.flip(y[1::2, 1::2], axis=1).copy()
        for location in ("left", "topleft"):
            expected = analysis.measure_frame(y, cb, cr, self.composer, chroma_location=location)
            actual = meter.stats(y, cb, cr, location)
            self.assertEqual({k: analysis.quantize_pq(v) for k, v in actual.items()}, expected["measured_l1"])
            for k in actual:
                self.assertAlmostEqual(actual[k], expected["measured_pq"][k], places=10)

    def test_tile_sizes_do_not_change_native420_measurement(self):
        rng = np.random.default_rng(2026)
        y = rng.integers(64, 941, (48, 64), dtype=np.uint16)
        cb = rng.integers(64, 961, (24, 32), dtype=np.uint16)
        cr = rng.integers(64, 961, (24, 32), dtype=np.uint16)
        a = analysis.measure_frame(y, cb, cr, self.composer, tile_rows=2)
        b = analysis.measure_frame(y, cb, cr, self.composer, tile_rows=64)
        self.assertEqual(a["encoded_l1"], b["encoded_l1"])
        for key in a["measured_pq"]:
            self.assertAlmostEqual(a["measured_pq"][key], b["measured_pq"][key], places=13)

    def test_real_frame_analysis_generates_verified_dynamic_l1(self):
        result = analysis.analyze_video(FFMPEG, self.source, self.work / "normal", 6, dovi_tool=DOVI)
        report = result["report"]
        rows = [json.loads(line) for line in Path(report["measurements"]).read_text().splitlines()]
        self.assertEqual(len(rows), 6)
        self.assertLess(rows[0]["measured_l1"]["max_pq"], rows[3]["measured_l1"]["max_pq"])
        self.assertLess(rows[0]["encoded_l1"]["avg_pq"], rows[3]["encoded_l1"]["avg_pq"])
        self.assertEqual(rows[0]["encoded_l1"]["avg_pq"], 1229)
        self.assertEqual(report["encoded_l1_changes"], 1)
        self.assertTrue(report["written_l1_verified"])
        self.assertEqual(report["l1_frames_verified"], 6)
        self.assertEqual(report["composer_frames_verified"], 1)
        self.assertFalse(report["all_frames_composer_verified"])
        self.assertFalse(report["dolby_algorithm_equivalent"])
        self.assertFalse(report["platform_verified"])
        verified = analysis.verify_rpu(DOVI, result["rpu_path"], report)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["l1_frames_verified"], 6)
        self.assertEqual(verified["composer_frames_verified"], 1)
        self.assertEqual(verified["verification_scope"], "all_frame_l1_and_first_frame_composer")
        self.assertFalse(verified["all_frames_composer_verified"])
        changed = dict(report, encoded_l1_sha256="0" * 64)
        with self.assertRaises(analysis.AnalysisError):
            analysis.verify_rpu(DOVI, result["rpu_path"], changed)
        cfg = json.loads(Path(result["config_path"]).read_text())
        self.assertEqual([s["duration"] for s in cfg["shots"]], [3, 3])
        self.assertEqual(report["shot_starts"], [0, 3])
        self.assertTrue(verified["scene_refresh_flags_verified"])
        with self.assertRaisesRegex(analysis.AnalysisError, "scene refresh"):
            analysis.verify_rpu(DOVI, result["rpu_path"], dict(report, shot_starts=[0, 2, 3]))

    def test_short_flash_not_an_edit_and_interpolated_peaks_stay_stable(self):
        detector = analysis.ShotDetector()
        for v in [128, 128, 800, 128, 128, 128, 700, 700, 700, 700]:
            y = np.full((48, 64), v, dtype=np.uint16)
            chroma = np.full((24, 32), 512, dtype=np.uint16)
            detector.push(detector.sketch(y, chroma, chroma))
        self.assertEqual(detector.finish(), [0, 6])
        with tempfile.TemporaryDirectory(dir=self.work) as folder:
            root = Path(folder)
            raw, cfg, ledger = (root / name for name in ("raw", "config.json", "frames.jsonl"))
            samples = [(0., p, a) for p, a in zip([.89,.70,.88,.70,.89,.70,.63,.60,.63,.59],
                                                   [.35]*6 + [.25]*4)]
            raw.write_text("".join(json.dumps({"frame": i, "measured_pq": dict(zip(
                ("min_pq","max_pq","avg_pq"), values))}) + "\n" for i, values in enumerate(samples)), encoding="utf-8")
            temporal = analysis.write_shot_config(raw, detector.finish(), len(samples), cfg, ledger)
            shots = json.loads(cfg.read_text(encoding="utf-8"))["shots"]
            levels = [shot["metadata_blocks"][0]["Level1"] for shot in shots]
            self.assertEqual([level["max_pq"] for level in levels],
                             [analysis.quantize_pq(.89), analysis.quantize_pq(.63)])
            self.assertEqual(temporal["encoded_l1_changes"], 1)
            records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["frame"] for r in records if r["scene_refresh"]], [0,6])
            self.assertEqual([r["encoded_l1"] for r in records], [levels[0]]*6 + [levels[1]]*4)

    def test_cancel_callback_exception_terminates_and_removes_partial_job(self):
        work = self.work / "cancelled"
        cancelled = [False]
        class UserCancelled(Exception):
            pass
        def boundary():
            if cancelled[0]:
                raise UserCancelled("cancelled")
        def progress(value):
            cancelled[0] = True
        with self.assertRaises(UserCancelled):
            analysis.analyze_video(FFMPEG, self.source, work, 6, dovi_tool=DOVI, cancel_check=boundary, progress=progress)
        self.assertEqual(list(work.iterdir()), [])

    def test_wrong_final_frame_count_never_publishes_analysis(self):
        work = self.work / "wrong-count"
        with self.assertRaisesRegex(analysis.AnalysisError, "frame count mismatch"):
            analysis.analyze_video(FFMPEG, self.source, work, 7, dovi_tool=DOVI)
        self.assertEqual(list(work.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
