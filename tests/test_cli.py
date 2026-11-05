"""User-facing CLI regressions using real child processes and encoded media."""
import json
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from fractions import Fraction
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
try:
    from pipeline import find_tool
    DOVI = find_tool("dovi_tool")
except FileNotFoundError:
    DOVI = None


def run_command(args, timeout=60):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise AssertionError(result.stderr[-4000:])
    return result.stdout


def inspect(path):
    return json.loads(run_command([FFPROBE, "-v", "error", "-count_frames", "-show_streams", "-of", "json", path]))


def video_hash(path):
    return run_command([FFMPEG, "-v", "error", "-i", path, "-map", "0:v:0", "-c", "copy",
                        "-an", "-f", "hash", "-hash", "sha256", "-"]).strip()


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and ffprobe required")
class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dolby-cli-")
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source with spaces.mp4"
        self.output = self.root / "output with spaces.mp4"
        self.workspace = self.root / "workspace"
        run_command([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=30000/1001",
                     "-frames:v", "6", "-c:v", "libx264", "-bf", "0", "-color_primaries", "bt709",
                     "-color_trc", "bt709", "-colorspace", "bt709", self.source])

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *options, output=None):
        command = [sys.executable, "cli.py", "-i", str(self.source), "-o", str(output or self.output),
                   "--workspace", str(self.workspace), *options]
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(command, cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace", **group)
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            # Killing just the CLI can leave FFmpeg holding files on Windows.
            # Only this test-owned process group/tree is eligible for cleanup.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=10)
            self.fail("CLI timed out after 60s\n" + stderr[-2000:] + stdout[-2000:])
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def test_default_export_keeps_encoded_video_and_native_cadence(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-2000:])
        self.assertEqual(video_hash(self.source), video_hash(self.output))
        video = inspect(self.output)["streams"][0]
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual((video["width"], video["height"]), (96, 64))
        self.assertEqual(Fraction(video["avg_frame_rate"]), Fraction(30000, 1001))
        self.assertEqual(video["nb_read_frames"], "6")
        report = json.loads(Path(str(self.output) + ".quality.json").read_text(encoding="utf-8"))
        self.assertFalse(report["platform_verified"])
        self.assertEqual(report["export"]["route"], "preserve")
        self.assertTrue(report["export"]["decode_verified"])
        self.assertEqual(report["status"], "pass", "general delivery should not inherit Bili-only failures")

    def test_native_transcode_uses_source_size_and_rational_fps_by_default(self):
        result = self.cli("--route", "transcode", "--color", "source", "--preset", "ultrafast")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-2000:])
        video = inspect(self.output)["streams"][0]
        self.assertEqual(video["codec_name"], "hevc")
        self.assertEqual((video["width"], video["height"]), (96, 64))
        self.assertEqual(Fraction(video["avg_frame_rate"]), Fraction(30000, 1001))
        self.assertEqual(video["nb_read_frames"], "6")
        self.assertEqual(video["pix_fmt"], "yuv420p10le")
        self.assertEqual(video["color_transfer"], "bt709")

    def test_sub_block_output_is_rejected_before_an_unstable_encoder_launch(self):
        for size in ("48x32", "48x64", "64x32"):
            with self.subTest(size=size):
                result = self.cli("--route", "transcode", "--size", size)
                self.assertEqual(result.returncode, 1)
                self.assertIn("编码器", result.stderr)
                self.assertFalse(self.output.exists())

    def test_phone_rotation_uses_display_size_for_native_transcode(self):
        # Stay above tiny-block encoder edge cases; exercise portrait orientation
        # rather than a vendor x265 build's handling of a 32-pixel-wide picture.
        unrotated = self.root / "phone original.mp4"
        run_command([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=192x128:rate=30000/1001",
                     "-frames:v", "6", "-c:v", "libx264", "-bf", "0", "-color_primaries", "bt709",
                     "-color_trc", "bt709", "-colorspace", "bt709", unrotated])
        rotated = self.root / "rotated source.mp4"
        run_command([FFMPEG, "-v", "error", "-display_rotation", "90", "-i", unrotated, "-c", "copy", rotated])
        from quality import inspect_media
        phone = inspect_media(rotated)
        self.assertEqual((phone["display_width"], phone["display_height"]), (128, 192), "invalid rotated test fixture")
        os.replace(rotated, self.source)
        result = self.cli("--route", "transcode", "--color", "source", "--preset", "ultrafast")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-2000:])
        video = inspect(self.output)["streams"][0]
        self.assertEqual((video["width"], video["height"]), (128, 192), "native portrait must not be padded back to landscape")

    def test_preserve_rejects_changes_instead_of_silently_ignoring_them(self):
        cases = [("--size", "1920x1080"), ("--fps", "60"), ("--color", "hdr10"),
                 ("--rpu", "unmatched.bin"), ("--ai",), ("--interp", "--fps", "60")]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.cli("--route", "preserve", *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.output.exists())

    def test_invalid_route_options_and_dimensions_fail_without_output(self):
        cases = [
            ("--route", "transcode", "--interp", "--fps", "60"),
            ("--route", "enhance", "--interp"),
            ("--route", "enhance", "--fps", "60", "--no-interp"),
            ("--route", "transcode", "--size", "33x32"),
            ("--route", "transcode", "--size", "32x0"),
            ("--route", "transcode", "--size", "fullhd"),
            ("--route", "enhance", "--ai", "--no-ai"),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.output.exists())

    def test_strict_atmos_rejects_a_silent_source_before_publication(self):
        result = self.cli("--require-atmos")
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, "音轨|Atmos")
        self.assertFalse(self.output.exists())

    def test_bili_delivery_rejects_ordinary_sdr_without_relabelling_it(self):
        original = video_hash(self.source)
        result = self.cli("--delivery", "bilibili")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(video_hash(self.source), original)

    @unittest.skipUnless(DOVI, "dovi_tool required for automatic Dolby export")
    def test_auto_dolby_creates_real_dv84_then_preserves_a_compliant_result(self):
        result = self.cli("--auto-dolby", "--preset", "ultrafast")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-3000:])
        from quality import inspect_media
        generated = inspect_media(self.output, count_frames=True)
        self.assertEqual((generated["dv_profile"], generated["dv_compatibility"]), (8, 4))
        self.assertTrue(generated["dv_rpu_present"] and generated["dv_bl_present"])
        self.assertEqual(generated["color_transfer"], "arib-std-b67")
        self.assertEqual(generated["bit_depth"], 10)
        self.assertEqual(Fraction(generated["fps_fraction"]), Fraction(30000, 1001))
        self.assertEqual(generated["total_frames"], 6)
        self.assertEqual((generated["width"], generated["height"]), (96, 64))
        report = json.loads(Path(str(self.output) + ".quality.json").read_text(encoding="utf-8"))
        self.assertFalse(report["platform_verified"])
        self.assertEqual(report["export"]["route"], "transcode")
        self.source, self.output = self.output, self.root / "preserved Dolby.mp4"
        # A separate workspace models the next job rather than resuming the
        # newly generated one. The shortcut must preserve compliant payloads.
        self.workspace = self.root / "second workspace"
        result = self.cli("--auto-dolby")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-2000:])
        self.assertEqual(video_hash(self.source), video_hash(self.output))
        self.source, self.output = self.output, self.root / "explicit HLG remake.mp4"
        self.workspace = self.root / "source interpretation workspace"
        result = self.cli("--auto-dolby", "--source-transfer", "arib-std-b67", "--preset", "ultrafast")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-2000:])
        report = json.loads(Path(str(self.output) + ".quality.json").read_text(encoding="utf-8"))
        self.assertEqual(report["export"]["route"], "transcode", "explicit source interpretation was sent to incompatible preservation")
        remade = inspect_media(self.output)
        self.assertEqual((remade["dv_profile"], remade["dv_compatibility"]), (8, 4))

    def test_automatic_dolby_options_reject_explicit_conflicts_before_output(self):
        cases = [("--auto-dolby", "--route", "preserve"),
                 ("--auto-dolby", "--color", "hlg"),
                 ("--auto-dolby", "--delivery", "general"),
                 ("--route", "transcode", "--color", "dv84", "--rpu", "manual.bin", "--rpu-mode", "auto"),
                 ("--route", "transcode", "--color", "dv81"),
                 ("--rpu-mode", "external"),
                 ("--hdr-peak-nits", "1001")]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.environ.get("DOLBY_ATMOS_FIXTURE"), "set DOLBY_ATMOS_FIXTURE for native JOC audio")
    def test_external_raw_atmos_audio_is_supported_without_a_video_track(self):
        audio = Path(os.environ["DOLBY_ATMOS_FIXTURE"]).resolve()
        self.assertTrue(audio.is_file(), "DOLBY_ATMOS_FIXTURE does not exist")
        result = self.cli("--audio", str(audio), "--audio-mode", "copy", "--require-atmos")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-2000:])
        report = json.loads(Path(str(self.output) + ".quality.json").read_text(encoding="utf-8"))
        self.assertEqual(report["media"]["atmos"], "confirmed")
        self.assertEqual(report["media"]["audio_codec"], "eac3")


class CliConfigurationTests(unittest.TestCase):
    """Inspect parser-to-engine contracts without encoding an 8K fixture."""

    def invoke(self, options, source=None, checkpoint=None):
        import cli
        cfgs = []
        class Runner:
            def __init__(self, cfg, **_):
                cfgs.append(cfg)
            def run(self):
                return "configuration inspected"
        info = {"width": 1920, "height": 1080, "display_width": 1920, "display_height": 1080,
                "fps": 30, "fps_fraction": "30", "audio_codec": "aac"} | (source or {})
        with patch("cli.Pipeline", Runner), patch("cli.signal.signal"), patch("cli.load_checkpoint", return_value=checkpoint), \
             patch("quality.inspect_media", return_value=info), patch("quality.assess_bilibili", return_value={"status": "fail"}), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(options), 0)
        return cfgs[0]

    def test_general_defaults_do_not_inject_new_dolby_options(self):
        cfg = self.invoke(["-i", "source.mp4"])
        self.assertEqual(cfg["route"], "preserve")
        for key in ("rpu_mode", "hdr_remaster", "hdr_peak_nits", "source_transfer_override"):
            self.assertNotIn(key, cfg)

    def test_auto_dolby_caps_native_size_and_fps_without_ai(self):
        cfg = self.invoke(["-i", "source.mp4", "--auto-dolby"],
                          {"width": 7680, "height": 4320, "display_width": 7680, "display_height": 4320,
                           "fps": 120, "fps_fraction": "120"})
        self.assertEqual((cfg["width"], cfg["height"]), (3840, 2160))
        self.assertEqual(cfg["fps_override"], "60")
        self.assertEqual((cfg["route"], cfg["delivery"], cfg["color_mode"], cfg["rpu_mode"]),
                         ("transcode", "bilibili", "dv84", "auto"))
        self.assertFalse(cfg["ai"] or cfg["interp"])

    def test_manual_rpu_and_explicit_hdr_controls_reach_engine(self):
        cfg = self.invoke(["-i", "source.mp4", "--route", "transcode", "--color", "dv84",
                           "--rpu", "matching.bin", "--hdr-remaster", "off", "--hdr-peak-nits", "800"])
        self.assertEqual(cfg["rpu_mode"], "external")
        self.assertEqual(cfg["rpu"], "matching.bin")
        self.assertEqual((cfg["hdr_remaster"], cfg["hdr_peak_nits"]), ("off", 800))

    def test_resume_uses_original_automatic_metadata_and_unknown_options(self):
        original = {"video": "source.mp4", "route": "transcode", "rpu_mode": "auto", "hdr_remaster": "off",
                    "hdr_peak_nits": 800, "source_transfer_override": "arib-std-b67", "future_option": "must remain"}
        cfg = self.invoke(["--resume", "--workspace", "checkpoint-dir"], checkpoint={"cfg": original})
        self.assertEqual(cfg, dict(original, resume=True))

    def test_unresolved_bt2020_requires_source_transfer_and_override_reaches_engine(self):
        source = {"color_transfer": "unknown", "color_primaries": "bt2020", "color_space": "bt2020nc"}
        with self.assertRaises(SystemExit) as error:
            self.invoke(["-i", "source.mp4", "--auto-dolby"], source)
        self.assertEqual(error.exception.code, 2)
        cfg = self.invoke(["-i", "source.mp4", "--auto-dolby", "--source-transfer", "arib-std-b67"], source)
        self.assertEqual(cfg["source_transfer_override"], "arib-std-b67")
        # Original bitstream preservation does not interpret or alter pixels.
        self.assertEqual(self.invoke(["-i", "source.mp4"], source)["route"], "preserve")
        with self.assertRaises(SystemExit) as error:
            self.invoke(["-i", "source.mp4", "--source-transfer", "smpte2084"], source)
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
