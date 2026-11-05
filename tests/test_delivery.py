"""Real-media contracts for source-preserving and Bilibili delivery routes.

All generated Dolby metadata is a synthetic test fixture. Passing these tests
does not establish visual grading quality or acceptance by Bilibili's service.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import pipeline

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
try:
    DOVI = pipeline.find_tool("dovi_tool")
except FileNotFoundError:
    DOVI = None


def command(args):
    result = subprocess.run([str(x) for x in args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=90)
    if result.returncode:
        raise AssertionError(result.stderr[-5000:])
    return result.stdout


def probe(path):
    return json.loads(command([FFPROBE, "-v", "error", "-count_frames", "-show_streams",
                               "-show_format", "-of", "json", path]))


def video(path):
    return next(s for s in probe(path)["streams"] if s["codec_type"] == "video")


def packets(path, selector="v:0"):
    data = json.loads(command([FFPROBE, "-v", "error", "-select_streams", selector,
                               "-show_packets", "-show_data_hash", "sha256", "-of", "json", path]))
    return data["packets"]


HAS_DOVI = bool(DOVI and FFMPEG and "dovi_rpu" in command([FFMPEG, "-hide_banner", "-bsfs"]))
ATMOS_FIXTURE = os.environ.get("DOLBY_ATMOS_FIXTURE", "")
HAS_ATMOS_FIXTURE = bool(ATMOS_FIXTURE and Path(ATMOS_FIXTURE).is_file())


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and ffprobe required")
class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dolby-delivery-")
        # Windows TEMP can expose an 8.3 alias of the same directory.
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.mp4"
        self.output = self.root / "delivery.mp4"

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **changes):
        cfg = {"video": str(self.source), "output": str(self.output),
               "workspace": str(self.root / "workspace"), "route": "transcode",
               "delivery": "general", "color_mode": "source", "ai": False,
               "interp": False, "audio_mode": "copy", "auto_clean": True,
               "preset": "ultrafast", "crf": 18}
        cfg.update(changes)
        return cfg

    def make_source(self, transfer="arib-std-b67", size="64x48", fps="6", count=6,
                    codec="libx265", audio=False, vfr=False):
        if vfr:
            self.source = self.root / "variable-frame-rate.mkv"
        args = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}"]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1"]
        args += ["-map", "0:v:0", "-frames:v", str(count)]
        if vfr:
            args += ["-vf", "setpts='if(lt(N,4),N,2*N-4)/(30*TB)'", "-fps_mode", "vfr"]
        args += ["-c:v", codec, "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020",
                 "-color_trc", transfer, "-colorspace", "bt2020nc", "-color_range", "tv"]
        if codec == "libx265":
            args += ["-preset", "ultrafast", "-x265-params",
                     "bframes=0:frame-threads=1:colorprim=bt2020:colormatrix=bt2020nc:transfer=" + transfer]
        if audio:
            args += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "128k", "-shortest"]
        args += [self.source]
        command(args)

    def assert_sidecar(self, output):
        path = Path(str(output) + ".quality.json")
        self.assertTrue(path.is_file(), "each final output needs an inspectable quality report")
        report = json.loads(path.read_text(encoding="utf-8"))
        # The exact nested schema may grow; none of its assessment levels may
        # claim platform verification without the platform's own acceptance.
        def markers(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "platform_verified":
                        yield item
                    yield from markers(item)
            elif isinstance(value, list):
                for item in value:
                    yield from markers(item)
        found = list(markers(report))
        self.assertTrue(found, "report must distinguish local inspection from platform acceptance")
        self.assertTrue(all(item is False for item in found))
        return report

    def make_dolby_source(self, profile):
        """Build a valid synthetic P8.1/P8.4 fixture independently of Pipeline."""
        transfer = "arib-std-b67" if profile == "8.4" else "smpte2084"
        self.make_source(transfer=transfer, codec="libx265")
        base = self.root / "fixture-base.hevc"
        command([FFMPEG, "-v", "error", "-i", self.source, "-map", "0:v:0", "-c", "copy",
                 "-bsf:v", "hevc_mp4toannexb", base])
        cfg = self.root / "fixture-rpu.json"
        cfg.write_text(json.dumps({"cm_version": "V40", "length": 6}), encoding="utf-8")
        rpu = self.root / "fixture.bin"
        command([DOVI, "generate", "-j", cfg, "-p", profile, "-o", rpu])
        injected = self.root / "fixture-dv.hevc"
        command([DOVI, "inject-rpu", "-i", base, "--rpu-in", rpu, "-o", injected])
        self.source = self.root / "dolby-source.mp4"
        command([FFMPEG, "-v", "error", "-fflags", "+genpts", "-r", "6", "-i", injected,
                 "-c", "copy", "-tag:v", "hvc1", "-strict", "unofficial", "-bsf:v",
                 "dovi_rpu=compression=0,setts=ts=N:duration=1:time_base=1/6", self.source])
        conf = next(item for item in video(self.source).get("side_data_list", [])
                    if item.get("side_data_type") == "DOVI configuration record")
        self.assertEqual(conf["dv_bl_signal_compatibility_id"], 4 if profile == "8.4" else 1)

    def test_preserve_remux_keeps_video_and_audio_packet_payloads(self):
        self.make_source(codec="libx265", audio=True)
        original_video = [p["data_hash"] for p in packets(self.source)]
        original_audio = [p["data_hash"] for p in packets(self.source, "a:0")]
        result = pipeline.Pipeline(self.config(route="preserve")).run()
        self.assertEqual(original_video, [p["data_hash"] for p in packets(result)])
        self.assertEqual(original_audio, [p["data_hash"] for p in packets(result, "a:0")])
        self.assertEqual(video(result)["color_transfer"], "arib-std-b67")
        self.assert_sidecar(result)

    def test_preserve_keeps_variable_frame_timing(self):
        self.make_source(codec="libx265", fps="30", count=8, vfr=True)
        original = packets(self.source)
        result = pipeline.Pipeline(self.config(route="preserve")).run()
        final = packets(result)
        self.assertEqual([p["data_hash"] for p in original], [p["data_hash"] for p in final])
        self.assertEqual(len(original), len(final))
        src_pts = [Fraction(p["pts_time"]) for p in original]
        dst_pts = [Fraction(p["pts_time"]) for p in final]
        self.assertGreater(len(set(b - a for a, b in zip(src_pts, src_pts[1:]))), 1)
        for src, dst in zip(src_pts, dst_pts):
            self.assertAlmostEqual(float(src - src_pts[0]), float(dst - dst_pts[0]), delta=0.00001)

    def test_transcode_retains_native_hdr_precision_size_and_avoids_rgb_png_workflow(self):
        self.make_source(size="1922x1082", count=2)
        runner = pipeline.Pipeline(self.config())
        calls = []
        original = runner._run

        def record(cmd, *args, **kwargs):
            calls.append([str(x) for x in cmd])
            return original(cmd, *args, **kwargs)

        with patch.object(runner, "_run", side_effect=record):
            result = runner.run()
        stream = video(result)
        self.assertEqual((stream["width"], stream["height"]), (1922, 1082))
        self.assertEqual(stream["pix_fmt"], "yuv420p10le")
        self.assertEqual(stream["color_transfer"], "arib-std-b67")
        self.assertEqual(int(stream["nb_read_frames"]), 2)
        flat = [part for cmd in calls for part in cmd]
        self.assertNotIn("rgb24", flat)
        self.assertFalse(any("%08d.png" in part for part in flat))
        self.assert_sidecar(result)

    def test_explicit_fps_override_reduces_rate_without_speeding_up_playback(self):
        self.make_source(fps="120", count=12)
        result = pipeline.Pipeline(self.config(fps_override="60")).run()
        stream = video(result)
        self.assertEqual(Fraction(stream["avg_frame_rate"]), Fraction(60))
        self.assertEqual(int(stream["nb_read_frames"]), 6)
        self.assertAlmostEqual(float(stream["duration"]), 0.1, delta=0.005)

    def test_native_transcode_does_not_squeeze_anamorphic_source_display_aspect(self):
        command([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=720x576:rate=6",
                 "-frames:v", "2", "-vf", "setsar=16/15", "-c:v", "libx265", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p10le", "-x265-params", "bframes=0:frame-threads=1", self.source])
        original = video(self.source)
        result = pipeline.Pipeline(self.config()).run()
        final = video(result)
        self.assertEqual(Fraction(original["display_aspect_ratio"].replace(":", "/")), Fraction(4, 3))
        self.assertEqual(Fraction(final["display_aspect_ratio"].replace(":", "/")), Fraction(4, 3))

    def test_native_hdr10_keeps_mastering_display_and_content_light_metadata(self):
        hdr_params = ("bframes=0:frame-threads=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
                      "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):"
                      "max-cll=1000,400")
        command([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=6",
                 "-frames:v", "2", "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
                 "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
                 "-x265-params", hdr_params, self.source])

        def hdr_metadata(path):
            frames = json.loads(command([FFPROBE, "-v", "error", "-select_streams", "v:0",
                                          "-read_intervals", "%+#1", "-show_frames", "-of", "json", path]))["frames"]
            return {item["side_data_type"]: item for frame in frames
                    for item in frame.get("side_data_list", [])
                    if item["side_data_type"] in ("Mastering display metadata", "Content light level metadata")}

        original = hdr_metadata(self.source)
        self.assertEqual(set(original), {"Mastering display metadata", "Content light level metadata"})
        result = pipeline.Pipeline(self.config()).run()
        self.assertEqual(hdr_metadata(result), original)

    def test_native_source_size_respects_phone_rotation_without_letterboxing_to_landscape(self):
        self.make_source(size="192x128", count=2)
        rotated = self.root / "phone-rotated.mp4"
        command([FFMPEG, "-v", "error", "-display_rotation:v:0", "90", "-i", self.source,
                 "-c", "copy", rotated])
        self.source = rotated
        original = video(rotated)
        matrix = next(item for item in original.get("side_data_list", [])
                      if item.get("side_data_type") == "Display Matrix")
        self.assertEqual(abs(matrix["rotation"]), 90)
        result = pipeline.Pipeline(self.config()).run()
        final = video(result)
        self.assertEqual((final["width"], final["height"]), (128, 192))

    def test_bilibili_delivery_rejects_non_dolby_source_without_publishing(self):
        self.make_source(transfer="smpte2084", codec="libx265")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.Pipeline(self.config(route="preserve", delivery="bilibili")).run()
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(HAS_DOVI, "dovi_tool and FFmpeg dovi_rpu required")
    def test_bilibili_rejects_actual_pq_profile81_source(self):
        self.make_dolby_source("8.1")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.Pipeline(self.config(route="preserve", delivery="bilibili")).run()
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(HAS_DOVI, "dovi_tool and FFmpeg dovi_rpu required")
    def test_preserve_profile84_keeps_dolby_payloads_and_configuration(self):
        self.make_dolby_source("8.4")
        original = [p["data_hash"] for p in packets(self.source)]
        result = pipeline.Pipeline(self.config(route="preserve", delivery="bilibili")).run()
        self.assertEqual(original, [p["data_hash"] for p in packets(result)])
        stream = video(result)
        conf = next(item for item in stream.get("side_data_list", [])
                    if item.get("side_data_type") == "DOVI configuration record")
        self.assertEqual(conf["dv_profile"], 8)
        self.assertEqual(conf["dv_bl_signal_compatibility_id"], 4)
        self.assertEqual(int(stream["nb_read_frames"]), 6)
        self.assert_sidecar(result)

    @unittest.skipUnless(HAS_DOVI, "dovi_tool and FFmpeg dovi_rpu required")
    def test_native_profile84_delivery_has_hlg_config_and_exact_rpu_count(self):
        self.make_source()
        config = self.root / "rpu.json"
        config.write_text(json.dumps({"cm_version": "V40", "length": 6,
                                      "level6": {"max_display_mastering_luminance": 1000,
                                                 "min_display_mastering_luminance": 1,
                                                 "max_content_light_level": 1000,
                                                 "max_frame_average_light_level": 400}}), encoding="utf-8")
        rpu = self.root / "profile84.bin"
        command([DOVI, "generate", "-j", config, "-p", "8.4", "-o", rpu])
        result = pipeline.Pipeline(self.config(color_mode="dv84", rpu=str(rpu), delivery="bilibili")).run()
        stream = video(result)
        self.assertEqual(stream["color_transfer"], "arib-std-b67")
        self.assertEqual(stream["color_primaries"], "bt2020")
        self.assertEqual(stream["color_space"], "bt2020nc")
        self.assertEqual(stream["pix_fmt"], "yuv420p10le")
        self.assertEqual(int(stream["nb_read_frames"]), 6)
        conf = next(item for item in stream.get("side_data_list", [])
                    if item.get("side_data_type") == "DOVI configuration record")
        self.assertEqual(conf["dv_profile"], 8)
        self.assertEqual(conf["dv_bl_signal_compatibility_id"], 4)
        raw = self.root / "output.hevc"
        command([FFMPEG, "-v", "error", "-i", result, "-map", "0:v:0", "-c", "copy",
                 "-bsf:v", "hevc_mp4toannexb", raw])
        extracted = self.root / "roundtrip.bin"
        command([DOVI, "extract-rpu", "-i", raw, "-o", extracted])
        summary = command([DOVI, "info", "-i", extracted, "-s"])
        self.assertRegex(summary, r"Frames:\s*6\b")
        self.assert_sidecar(result)

    @unittest.skipUnless(HAS_DOVI, "dovi_tool and FFmpeg dovi_rpu required")
    def test_profile84_mismatched_rpu_count_never_publishes(self):
        self.make_source()
        config = self.root / "short-rpu.json"
        config.write_text(json.dumps({"cm_version": "V40", "length": 5}), encoding="utf-8")
        rpu = self.root / "short.bin"
        command([DOVI, "generate", "-j", config, "-p", "8.4", "-o", rpu])
        with self.assertRaises(pipeline.PipelineError):
            pipeline.Pipeline(self.config(color_mode="dv84", rpu=str(rpu), delivery="bilibili")).run()
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(HAS_ATMOS_FIXTURE, "set DOLBY_ATMOS_FIXTURE to an external JOC sample")
    def test_actual_atmos_source_copy_preserves_every_encoded_audio_packet(self):
        self.source = Path(ATMOS_FIXTURE).resolve()
        source_audio = next(s for s in probe(self.source)["streams"] if s["codec_type"] == "audio")
        self.assertEqual(source_audio["codec_name"], "eac3")
        self.assertIn("Atmos", source_audio.get("profile", ""))
        source_hashes = [p["data_hash"] for p in packets(self.source, "a:0")]
        result = pipeline.Pipeline(self.config(route="preserve", require_atmos=True)).run()
        target_audio = next(s for s in probe(result)["streams"] if s["codec_type"] == "audio")
        self.assertIn("Atmos", target_audio.get("profile", ""))
        self.assertEqual(source_hashes, [p["data_hash"] for p in packets(result, "a:0")])
        self.assert_sidecar(result)

    @unittest.skipUnless(HAS_ATMOS_FIXTURE and HAS_DOVI, "external JOC sample and dovi_tool required")
    def test_bilibili_preserve_uses_effective_external_atmos_track_for_double_label_checks(self):
        self.make_dolby_source("8.4")
        # The DV video deliberately has no audio. A valid separately supplied
        # Atmos track must be checked instead of rejecting the silent container.
        result = pipeline.Pipeline(self.config(route="preserve", delivery="bilibili",
                                               require_atmos=True, audio=ATMOS_FIXTURE)).run()
        target_audio = next(s for s in probe(result)["streams"] if s["codec_type"] == "audio")
        self.assertIn("Atmos", target_audio.get("profile", ""))
        original = [p["data_hash"] for p in packets(ATMOS_FIXTURE, "a:0")]
        final = [p["data_hash"] for p in packets(result, "a:0")]
        # The longer external track is trimmed to the video, without re-encoding.
        self.assertGreater(len(final), 0)
        self.assertEqual(final, original[:len(final)])
        self.assert_sidecar(result)


if __name__ == "__main__":
    unittest.main()
