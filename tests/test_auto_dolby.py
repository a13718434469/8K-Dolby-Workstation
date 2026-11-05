"""Real-frame contracts for automatic HDR remastering and measured DV metadata.

These checks establish content-dependent metadata and local media integrity.
They do not establish Dolby grading equivalence or Bilibili acceptance. All
fixtures are generated locally; genuine Atmos is an optional private fixture.
"""
import csv
import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from statistics import mean
from unittest.mock import patch

import numpy as np

import pipeline


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
try:
    DOVI = pipeline.find_tool("dovi_tool")
except FileNotFoundError:
    DOVI = None


def command(args, binary=False):
    result = subprocess.run([str(item) for item in args], capture_output=True,
                            timeout=90, **({} if binary else {
                                "text": True, "encoding": "utf-8", "errors": "replace"}))
    if result.returncode:
        raise AssertionError(str(result.stderr[-5000:]))
    return result.stdout


HAS_AUTO_TOOLS = bool(DOVI and FFMPEG and FFPROBE and
                      "dovi_rpu" in command([FFMPEG, "-hide_banner", "-bsfs"]))
ATMOS_FIXTURE = os.environ.get("DOLBY_ATMOS_FIXTURE", "")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@unittest.skipUnless(HAS_AUTO_TOOLS, "dovi_tool and FFmpeg native Dolby support required")
class AutomaticDolbyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dolby-auto-")
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "SDR dark then bright.mkv"
        self.output = self.root / "automatic delivery.mp4"

    def tearDown(self):
        self.temp.cleanup()

    def make_source(self, expression="if(lt(N,6),128,900)", transfer="bt709",
                    source=None, fps="6", frames=12):
        source = Path(source or self.source)
        colors = "bt709" if transfer == "bt709" else "bt2020"
        matrix = "bt709" if transfer == "bt709" else "bt2020nc"
        args = [FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                f"nullsrc=size=192x128:rate={fps}", "-vf",
                f"format=yuv420p10le,geq=lum='{expression}':cb=512:cr=512", "-frames:v", frames]
        if transfer == "bt709":
            args += ["-c:v", "ffv1", "-level", "3"]
        else:
            # Some FFmpeg FFV1/Matroska builds discard transfer/primaries flags.
            # HEVC VUI makes these actual HDR inputs independently verifiable.
            args += ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
                     f"lossless=1:bframes=0:frame-threads=1:colorprim={colors}:transfer={transfer}:colormatrix={matrix}"]
        command(args + ["-pix_fmt", "yuv420p10le", "-color_primaries", colors,
                        "-color_trc", transfer, "-colorspace", matrix, "-color_range", "tv", source])
        if transfer != "bt709":
            decoded = json.loads(command([FFPROBE, "-v", "error", "-select_streams", "v:0",
                                          "-read_intervals", "%+#1", "-show_frames", "-of", "json", source]))
            self.assertTrue(decoded["frames"], "HDR fixture could not be decoded")
            frame = decoded["frames"][0]
            self.assertEqual(frame.get("color_transfer"), transfer, "HDR fixture lost its transfer function")
            self.assertEqual(frame.get("color_primaries"), colors, "HDR fixture lost its primaries")
        return source

    def config(self, **changes):
        cfg = {"video": str(self.source), "output": str(self.output),
               "workspace": str(self.root / "workspace"), "route": "transcode",
               "delivery": "bilibili", "color_mode": "dv84", "hdr_remaster": "auto",
               "hdr_peak_nits": 600, "ai": False, "interp": False,
               "audio_mode": "copy", "auto_clean": False, "preset": "ultrafast", "crf": 14}
        cfg.update(changes)
        return cfg

    def run_export(self, **changes):
        subject = pipeline.Pipeline(self.config(**changes))
        output = Path(subject.run())
        report = json.loads(Path(str(output) + ".quality.json").read_text(encoding="utf-8"))
        return subject, output, report

    def extracted_l1(self, output):
        raw = self.root / (output.stem + ".verification.hevc")
        rpu = self.root / (output.stem + ".verification.rpu.bin")
        values = self.root / (output.stem + ".verification.level1.csv")
        command([FFMPEG, "-nostdin", "-v", "error", "-y", "-i", output,
                 "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", raw])
        command([DOVI, "extract-rpu", "-i", raw, "-o", rpu])
        command([DOVI, "export", "-i", rpu, "--levels", "level1=" + str(values)])
        with values.open(encoding="utf-8-sig", newline="") as handle:
            records = list(csv.DictReader(handle))
        self.assertTrue(records, "final bitstream contains no exported Level 1 metadata")
        rows = [{key: int(row[key]) for key in ("min_pq", "max_pq", "avg_pq")} for row in records]
        for row in rows:
            self.assertTrue(0 <= row["min_pq"] <= row["avg_pq"] <= row["max_pq"] <= 4095,
                            "generated metadata contains invalid PQ bounds")
        digest = hashlib.sha256()
        for row in rows:
            digest.update(struct.pack(">HHH", row["min_pq"], row["max_pq"], row["avg_pq"]))
        return rows, digest.hexdigest()

    def assert_measured_report(self, subject, output, report, frame_count):
        self.assertIs(report["platform_verified"], False)
        self.assertIs(report["export"]["decode_verified"], True)
        self.assertIn("auto_rpu", subject.manifest["completed"])
        analysis = report["export"]["dolby_analysis"]
        self.assertEqual(analysis["frames"], frame_count)
        self.assertIs(analysis["all_frames_measured"], True)
        self.assertIs(analysis["full_resolution"], True)
        self.assertIs(analysis["written_l1_verified"], True)
        self.assertIs(analysis["visual_match_verified"], False)
        self.assertIs(analysis["platform_verified"], False)
        self.assertIs(analysis["dolby_algorithm_equivalent"], False)
        self.assertEqual(analysis["source_sha256"], sha256(subject.job_dir / "native.mp4"),
                         "metadata was not analyzed from the final encoded base layer")
        self.assertEqual(analysis["rpu_sha256"], sha256(analysis["rpu"]))
        measurements = [json.loads(line) for line in Path(analysis["measurements"]).read_text(
            encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(measurements), frame_count)
        self.assertEqual([row["frame"] for row in measurements], list(range(frame_count)))
        rows, digest = self.extracted_l1(output)
        self.assertEqual(len(rows), frame_count, "final output does not have one RPU per final frame")
        self.assertEqual(digest, analysis["encoded_l1_sha256"],
                         "final bitstream L1 differs from the measured/generated metadata")
        final = report["export"]["rpu_final_validation"]
        self.assertIs(final["verified"], True)
        self.assertEqual(final["frame_count"], frame_count)
        self.assertEqual(final["l1_sha256"], digest)
        return analysis, rows

    def decoded_gray_stripes(self, output):
        data = command([FFMPEG, "-nostdin", "-v", "error", "-i", output,
                        "-map", "0:v:0", "-frames:v", "1", "-pix_fmt", "yuv420p10le",
                        "-f", "rawvideo", "-"], binary=True)
        samples = np.frombuffer(data, dtype="<u2")
        self.assertEqual(samples.size, 192 * 128 * 3 // 2)
        luma = samples[:192 * 128].reshape(128, 192)
        cb = samples[192 * 128:192 * 128 + 96 * 64]
        cr = samples[192 * 128 + 96 * 64:]
        means = [float(luma[32:96, x-8:x+8].mean()) for x in (24, 72, 120, 168)]
        self.assertLess(abs(float(cb.mean()) - 512), 8, "neutral gray acquired a blue/yellow cast")
        self.assertLess(abs(float(cr.mean()) - 512), 8, "neutral gray acquired a red/green cast")
        return means

    def test_no_rpu_defaults_to_content_dependent_final_frame_metadata(self):
        measured_orders = []
        for reverse in (False, True):
            self.make_source("if(lt(N,6),900,128)" if reverse else "if(lt(N,6),128,900)")
            subject, output, report = self.run_export(output=str(self.root / f"order-{reverse}.mp4"))
            analysis, rows = self.assert_measured_report(subject, output, report, 12)
            self.assertGreater(analysis["encoded_l1_changes"], 0, "a static metadata template was used")
            first = mean(row["max_pq"] for row in rows[:6])
            second = mean(row["max_pq"] for row in rows[6:])
            self.assertGreater(first - second if reverse else second - first, 50,
                               "metadata brightness did not follow the actual shot order")
            measured_orders.append((first, second))
        self.assertGreater(measured_orders[0][1], measured_orders[0][0])
        self.assertGreater(measured_orders[1][0], measured_orders[1][1])

    def test_fps_change_measures_final_24_frames_and_preserves_shot_timing(self):
        self.make_source()
        subject, output, report = self.run_export(fps_override="12")
        _, rows = self.assert_measured_report(subject, output, report, 24)
        media = report["media"]
        self.assertEqual(Fraction(media["fps_fraction"]), 12)
        self.assertAlmostEqual(media["duration"], 2, delta=0.01)
        self.assertGreater(min(row["max_pq"] for row in rows[12:]) -
                           max(row["max_pq"] for row in rows[:12]), 50,
                           "metadata still follows input frame indices after a rate change")

    def test_sdr_remaster_changes_pixels_and_keeps_gray_order_black_and_neutrality(self):
        self.make_source("if(lt(X,W/4),64,if(lt(X,W/2),356,if(lt(X,3*W/4),648,940)))")
        _, automatic, report = self.run_export(output=str(self.root / "remastered.mp4"))
        _, unchanged, baseline_report = self.run_export(output=str(self.root / "color-converted-only.mp4"),
                                                       hdr_remaster="off")
        self.assertIs(report["export"]["hdr_remaster"]["active"], True)
        self.assertEqual(report["export"]["hdr_remaster"]["source_transfer"], "bt709")
        self.assertEqual(report["export"]["hdr_remaster"]["peak_nits"], 600)
        self.assertIs(baseline_report["export"]["hdr_remaster"]["active"], False)
        actual = self.decoded_gray_stripes(automatic)
        baseline = self.decoded_gray_stripes(unchanged)
        self.assertLess(actual[0], 85, "HDR remaster raised the original black floor")
        self.assertTrue(all(b - a > 10 for a, b in zip(actual, actual[1:])),
                        "HDR remaster crushed or reversed distinct input gray levels")
        self.assertGreater(actual[-1] - baseline[-1], 25,
                           "automatic remaster changed labels without lifting decoded highlights")
        self.assertLessEqual(actual[-1], 950, "HDR remaster exceeded legal 10-bit luma bounds")

    def test_existing_hlg_and_hdr10_are_not_expanded_again_as_sdr(self):
        for transfer, mode, delivery in (("arib-std-b67", "dv84", "bilibili"),
                                          ("smpte2084", "hdr10", "general")):
            with self.subTest(transfer=transfer):
                self.source = self.root / (mode + "-source.mp4")
                self.make_source("if(lt(X,W/4),64,if(lt(X,W/2),356,if(lt(X,3*W/4),648,940)))",
                                 transfer=transfer)
                common = {"color_mode": mode, "delivery": delivery}
                _, automatic, report = self.run_export(output=str(self.root / (mode + "-auto.mp4")), **common)
                _, baseline, _ = self.run_export(output=str(self.root / (mode + "-off.mp4")),
                                                 hdr_remaster="off", **common)
                self.assertEqual(report["media"]["color_transfer"], transfer)
                self.assertEqual(report["media"]["pix_fmt"], "yuv420p10le")
                self.assertIs(report["export"]["hdr_remaster"]["active"], False)
                self.assertEqual(report["export"]["hdr_remaster"]["source_transfer"], transfer)
                np.testing.assert_allclose(self.decoded_gray_stripes(automatic),
                                           self.decoded_gray_stripes(baseline), atol=2, rtol=0,
                                           err_msg="existing HDR was treated as SDR and expanded a second time")

    def test_hdr_matroska_uses_decoded_frame_color_information(self):
        self.make_source(transfer="arib-std-b67")
        # With some FFmpeg builds MKV stream tags omit HLG/BT.2020, although
        # the actual HEVC frame carries both VUI values (checked above).
        _, _, report = self.run_export()
        self.assertIs(report["export"]["hdr_remaster"]["active"], False,
                      "an actual HLG frame in MKV was expanded as if it were SDR")
        self.assertEqual(report["export"]["hdr_remaster"]["source_transfer"], "arib-std-b67")

    def test_cancellation_inside_real_frame_analysis_resumes_without_reencoding_base(self):
        import dolby_analysis
        self.make_source()
        first = pipeline.Pipeline(self.config())
        analyze = dolby_analysis.analyze_video
        measured_progress = []

        def interrupt_after_a_frame(*args, **kwargs):
            original_progress = kwargs.get("progress")
            def progress(percent):
                measured_progress.append(percent)
                if original_progress:
                    original_progress(percent)
                if 0 < percent < 100:
                    first.cancel()
            kwargs["progress"] = progress
            return analyze(*args, **kwargs)

        with patch.object(dolby_analysis, "analyze_video", side_effect=interrupt_after_a_frame):
            with self.assertRaises(pipeline.PipelineCancelled):
                first.run()
        self.assertTrue(any(0 < value < 100 for value in measured_progress),
                        "the test did not reach actual frame analysis")
        self.assertFalse(self.output.exists())
        self.assertIn("native", first.manifest["completed"])
        self.assertNotIn("auto_rpu", first.manifest["completed"])
        base = first.job_dir / "native.mp4"
        base_hash = sha256(base)
        checkpoint = pipeline.load_checkpoint(first.root / "checkpoint.json")
        resumed = pipeline.Pipeline(dict(checkpoint["cfg"], resume=True))
        run = resumed._run
        calls = []
        def record(command, *args, **kwargs):
            calls.append([str(item) for item in command])
            return run(command, *args, **kwargs)
        with patch.object(resumed, "_run", side_effect=record):
            output = Path(resumed.run())
        self.assertEqual(resumed.job_dir, first.job_dir)
        self.assertEqual(sha256(base), base_hash)
        self.assertFalse(any("libx265" in args for args in calls),
                         "resuming metadata analysis unnecessarily reencoded the verified base")
        report = json.loads(Path(str(output) + ".quality.json").read_text(encoding="utf-8"))
        self.assert_measured_report(resumed, output, report, 12)

    def test_corrupt_completed_auto_rpu_is_not_silently_reused_on_resume(self):
        self.make_source()
        first = pipeline.Pipeline(self.config())
        stage_done = first._stage_done
        def interrupt_after_generated_rpu(name, *args, **kwargs):
            result = stage_done(name, *args, **kwargs)
            if name == "auto_rpu":
                first.cancel()
            return result
        with patch.object(first, "_stage_done", side_effect=interrupt_after_generated_rpu):
            with self.assertRaises(pipeline.PipelineCancelled):
                first.run()
        self.assertIn("auto_rpu", first.manifest["completed"])
        self.assertFalse(self.output.exists())
        checkpoint = pipeline.load_checkpoint(first.root / "checkpoint.json")
        rpu = Path(checkpoint["dolby_analysis"]["rpu"])
        payload = bytearray(rpu.read_bytes())
        payload[0] ^= 0xFF
        rpu.write_bytes(payload)
        resumed = pipeline.Pipeline(dict(checkpoint["cfg"], resume=True))
        try:
            output = Path(resumed.run())
        except pipeline.PipelineError:
            self.assertFalse(self.output.exists(), "a damaged RPU was published before rejection")
        else:
            # Reanalysis is a valid recovery policy, as is rejecting this cache.
            report = json.loads(Path(str(output) + ".quality.json").read_text(encoding="utf-8"))
            self.assert_measured_report(resumed, output, report, 12)

    def test_cancel_at_80_percent_keeps_completed_analysis_for_resume(self):
        import dolby_analysis
        self.make_source()
        progress = []
        def cancel_when_analysis_finishes(percent):
            progress.append(percent)
            if percent == 80:
                first.cancel()
        first = pipeline.Pipeline(self.config(), progress=cancel_when_analysis_finishes)
        with patch.object(dolby_analysis, "analyze_video", wraps=dolby_analysis.analyze_video) as analyze:
            with self.assertRaises(pipeline.PipelineCancelled):
                first.run()
        self.assertIn(80, progress, "the test did not reach completed-analysis progress")
        self.assertEqual(analyze.call_count, 1)
        self.assertFalse(self.output.exists(), "cancellation published a final video")
        checkpoint = pipeline.load_checkpoint(first.root / "checkpoint.json")
        self.assertIn("auto_rpu", checkpoint["completed"],
                      "cancellation discarded a fully generated and verified analysis stage")
        analysis = checkpoint["dolby_analysis"]
        analysis_dir = Path(analysis["rpu"]).parent
        analysis_dirs = sorted((first.job_dir / "auto-dolby").glob("analysis-*"))
        self.assertEqual(analysis_dirs, [analysis_dir], "completed analysis was left untracked")
        base_hash = sha256(first.job_dir / "native.mp4")
        stats_hash = sha256(analysis["measurements"])
        resumed = pipeline.Pipeline(dict(checkpoint["cfg"], resume=True))
        run = resumed._run
        calls = []
        def record(command, *args, **kwargs):
            calls.append([str(item) for item in command])
            return run(command, *args, **kwargs)
        with patch.object(dolby_analysis, "analyze_video",
                          side_effect=AssertionError("a completed analysis was run again")) as analyze_again, \
                patch.object(resumed, "_run", side_effect=record):
            output = Path(resumed.run())
        analyze_again.assert_not_called()
        self.assertEqual(resumed.job_dir, first.job_dir)
        self.assertEqual(sorted((first.job_dir / "auto-dolby").glob("analysis-*")), analysis_dirs)
        self.assertEqual(sha256(analysis["measurements"]), stats_hash)
        self.assertEqual(sha256(first.job_dir / "native.mp4"), base_hash)
        self.assertFalse(any("libx265" in args for args in calls),
                         "resuming a completed analysis reencoded the base")
        report = json.loads(Path(str(output) + ".quality.json").read_text(encoding="utf-8"))
        self.assertEqual(report["export"]["dolby_analysis"]["rpu"], analysis["rpu"])
        self.assert_measured_report(resumed, output, report, 12)

    @unittest.skipUnless(ATMOS_FIXTURE and Path(ATMOS_FIXTURE).is_file(),
                         "set DOLBY_ATMOS_FIXTURE to an existing private JOC sample")
    def test_automatic_video_metadata_keeps_existing_atmos_packet_payloads(self):
        self.make_source()
        _, output, report = self.run_export(audio=ATMOS_FIXTURE, require_atmos=True)
        def audio_packets(path):
            data = json.loads(command([FFPROBE, "-v", "error", "-select_streams", "a:0",
                                      "-show_packets", "-show_data_hash", "sha256", "-of", "json", path]))
            return [packet["data_hash"] for packet in data["packets"]]
        original, final = audio_packets(ATMOS_FIXTURE), audio_packets(output)
        self.assertTrue(final)
        self.assertEqual(final, original[:len(final)])
        self.assertEqual(report["media"]["atmos"], "confirmed")
        self.assertEqual(report["media"]["audio_codec"], "eac3")
        self.assertIs(report["platform_verified"], False)

    def test_invalid_policy_or_peak_is_rejected_without_creating_output(self):
        self.make_source()
        for values in ({"rpu_mode": "template"}, {"hdr_remaster": "vivid"},
                       {"hdr_peak_nits": 99}, {"hdr_peak_nits": 1001},
                       {"rpu_mode": "external", "rpu": ""}):
            with self.subTest(values=values):
                with self.assertRaises((ValueError, pipeline.PipelineError)):
                    pipeline.Pipeline(self.config(**values)).run()
                self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
