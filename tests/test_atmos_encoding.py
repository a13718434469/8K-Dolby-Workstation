"""Real PCM mix tests and offline encoder-configuration contracts.

These tests do not encode JOC, contact AWS, or claim that an encoder license is
available. PCM assertions inspect actual FFmpeg output, not the filter text.
"""
import copy
import json
import shutil
import socket
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import atmos_cloud
import atmos_encoding as encoding


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class EncoderConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atmos-configuration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "installed version profile.json"
        # These are transport-contract test arguments, not claimed DEE flags.
        self.value = {"schema_version": 1, "input_layout": "5.1.4",
                      "argv": ["input={input}", "output={output}", "temp={temp}", "literal with spaces"],
                      "output_suffix": ".ec3"}

    def write_profile(self, value=None):
        self.profile.write_text(json.dumps(self.value if value is None else value), encoding="utf-8")
        return self.profile

    def test_profile_keeps_documented_user_arguments_without_inventing_flags(self):
        self.assertEqual(encoding.load_dee_profile(self.write_profile()), self.value)
        self.value["argv"] = ["{input}", "{output}"]
        result = encoding.load_dee_profile(self.write_profile())
        self.assertEqual(result["argv"], ["{input}", "{output}"])
        self.assertNotIn("--xml", result["argv"])

    def test_input_and_output_placeholders_are_both_required(self):
        for args in (["{input}"], ["{output}"], ["source.wav", "output.ec3"], []):
            with self.subTest(args=args):
                value = {**self.value, "argv": args}
                with self.assertRaises(encoding.AtmosError):
                    encoding.load_dee_profile(self.write_profile(value))

    def test_profile_rejects_unknown_placeholders_and_non_argument_arrays(self):
        invalid = ("--input {input} --output {output}", ["{input}", "{output}", "{key}"],
                   ["{input}", "{output}", "{input"], ["{input}", "{output}", "broken}"],
                   ["{input}", "{output}", "nul\0value"], ["{input}", "{output}", 42])
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(encoding.AtmosError):
                encoding.load_dee_profile(self.write_profile({**self.value, "argv": args}))

    def test_profile_rejects_wrong_input_type_unsupported_output_and_credentials(self):
        for update in ({"input_layout": "ADM"}, {"input_layout": "7.1.4"},
                       {"output_suffix": ".aac"}, {"schema_version": 2},
                       {"aws_secret_access_key": "fixture-not-a-key"}):
            with self.subTest(update=update), self.assertRaises(encoding.AtmosError):
                encoding.load_dee_profile(self.write_profile({**self.value, **update}))

    def test_missing_malformed_and_oversized_profiles_fail_without_running_tools(self):
        with self.assertRaises(encoding.AtmosError):
            encoding.load_dee_profile(self.profile)
        for content in ("not json", "[]", "x" * 65537):
            with self.subTest(content=content[:10]):
                self.profile.write_text(content, encoding="utf-8")
                with self.assertRaises(encoding.AtmosError):
                    encoding.load_dee_profile(self.profile)

    def test_missing_encoder_or_profile_is_unavailable_not_ready_to_generate(self):
        absent = self.root / "not-installed.exe"
        self.assertFalse(encoding.backend_status({"dee_path": str(absent)})["available"])
        placeholder = self.root / "not-an-executed-encoder.exe"
        placeholder.write_bytes(b"not executable; never run")
        cfg = {"dee_path": str(placeholder)}
        self.assertFalse(encoding.backend_status(cfg)["available"])
        cfg["dee_profile"] = str(self.write_profile())
        with patch("subprocess.Popen", side_effect=AssertionError("status must not execute programs")):
            status = encoding.backend_status(cfg)
        self.assertTrue(status["available"])
        self.assertIn("运行时", status["message"])
        self.assertIn("许可证", status["message"])

    def test_argv_dispatch_preserves_paths_and_refuses_success_without_output(self):
        executable = self.root / "not-an-executed-encoder.exe"
        executable.write_bytes(b"transport test only")
        master = self.root / "source with spaces.wav"
        master.write_bytes(b"not media; command transport only")
        cfg = {"dee_path": str(executable), "dee_profile": str(self.write_profile())}
        run = Mock()
        with self.assertRaisesRegex(encoding.AtmosError, "未生成"):
            encoding.encode_dee(cfg, master, self.root, run)
        command = run.call_args.args[0]
        self.assertEqual(command, [str(executable.resolve()), f"input={master.resolve()}",
                                  f"output={self.root.resolve() / 'encoded.ec3'}",
                                  f"temp={self.root.resolve()}", "literal with spaces"])
        self.assertFalse(run.call_args.kwargs["log_command"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_cloud_status_does_not_load_sdk_credentials_or_connect(self):
        config = {"atmos_backend": "aws", "cloud_atmos": {
            "region": "us-west-2", "bucket": "test-atmos-bucket",
            "role_arn": "arn:aws:iam::123456789012:role/MediaConvert", "allow_upload": True}}
        with patch.object(atmos_cloud, "_load_sdk", side_effect=AssertionError("SDK must stay unloaded")) as sdk, \
                patch.object(socket, "create_connection", side_effect=AssertionError("no connections")), \
                patch.object(socket, "socket", side_effect=AssertionError("no sockets")), \
                patch.object(encoding.importlib.util, "find_spec", return_value=object()):
            status = encoding.backend_status(config)
            self.assertTrue(status["available"])
            self.assertIn("计费", status["message"])
            self.assertIn("运行时", status["message"])
            no_consent = copy.deepcopy(config)
            no_consent["cloud_atmos"]["allow_upload"] = False
            self.assertFalse(encoding.backend_status(no_consent)["available"])
            self.assertFalse(encoding.backend_status({"atmos_backend": "aws"})["available"])
            sdk.assert_not_called()
        with patch.object(encoding.importlib.util, "find_spec", return_value=None):
            self.assertFalse(encoding.backend_status(config)["available"])

    def test_unknown_layout_and_invalid_duration_stop_before_ffmpeg(self):
        run = Mock()
        for channels, layout in ((6, ""), (8, ""), (8, "7.1(wide)"), (4, "quad"), (3, "2.1")):
            with self.subTest(channels=channels, layout=layout), self.assertRaises(encoding.AtmosError):
                encoding.prepare_master("ffmpeg", "missing.wav", {"channels": channels, "channel_layout": layout},
                                        1.0, "output.wav", run)
        for duration in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(duration=duration), self.assertRaises(encoding.AtmosError):
                encoding.prepare_master("ffmpeg", "missing.wav", {"channels": 2}, duration, "output.wav", run)
        for offset in (None, "invalid", float("nan"), float("inf"), 1e308):
            with self.subTest(offset=offset), self.assertRaises(encoding.AtmosError):
                encoding.prepare_master("ffmpeg", "missing.wav", {"channels": 2}, 1.0, "output.wav", run,
                                        audio_offset=offset)
        run.assert_not_called()


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and FFprobe required for real PCM tests")
class RealSpatialMixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atmos-real-pcm-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_tool(self, command, **kwargs):
        result = subprocess.run(command, capture_output=True, timeout=30, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result

    def make_source(self, channels, sample_rate=44100, duration=0.25, amplitude=0.2):
        time = np.arange(round(sample_rate * duration)) / sample_rate
        frequencies = np.arange(channels) * 200 + 400
        samples = np.sin(2 * np.pi * time[:, None] * frequencies[None, :]) * amplitude
        path = self.root / f"input-{channels}-{sample_rate}-{amplitude}.wav"
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.writeframes((samples * 32767).astype("<i2").tobytes())
        return path, frequencies

    def mix(self, source, channels, layout, output_channels, duration=0.5, audio_offset=0.0):
        output = self.root / f"output-{channels}-{layout}-{output_channels}.wav"
        manifest = encoding.prepare_master(FFMPEG, source, {"channels": channels, "channel_layout": layout},
                                           duration, output, self.run_tool, output_channels, audio_offset=audio_offset)
        probe = self.run_tool([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(output)])
        stream = json.loads(probe.stdout)["streams"][0]
        self.assertEqual(stream["codec_name"], "pcm_s24le")
        self.assertEqual(int(stream["sample_rate"]), 48000)
        self.assertEqual(stream["bits_per_sample"], 24)
        self.assertEqual(stream["channels"], output_channels)
        raw = self.run_tool([FFMPEG, "-v", "error", "-i", str(output), "-f", "f32le", "-c:a", "pcm_f32le", "-"])
        samples = np.frombuffer(raw.stdout, dtype="<f4").reshape(-1, output_channels)
        self.assertEqual(len(samples), round(duration * 48000))
        self.assertTrue(np.isfinite(samples).all())
        self.assertLess(np.max(np.abs(samples)), 0.99999, "PCM was clipped")
        self.assertEqual(manifest["samples"], len(samples))
        self.assertEqual(manifest["offset_samples"], round(audio_offset * 48000))
        self.assertIn("不是 Atmos", manifest["description"])
        return samples

    def projections(self, samples, frequencies):
        # Independent tone identifiers test routing, including phase-shifted
        # height filters, without matching the implementation's gain formula.
        start, stop = 4800, 9600
        time = np.arange(start, stop) / 48000
        phase = 2 * np.pi * time[:, None] * frequencies[None, :]
        section = samples[start:stop]
        return np.hypot(section.T @ np.sin(phase), section.T @ np.cos(phase)) * 2 / (stop - start)

    def assert_only_tones(self, projection, channel, tones):
        expected = set(tones)
        for source, magnitude in enumerate(projection[channel]):
            if source in expected:
                self.assertGreater(magnitude, 0.001, f"source {source} missing from output {channel}")
            else:
                self.assertLess(magnitude, 0.0001, f"source {source} leaked into output {channel}")

    def assert_silent(self, samples, channels):
        if channels:
            self.assertLessEqual(np.max(np.abs(samples[:, channels])), 2e-7)

    def test_mono_stays_centered_without_fabricated_surround_or_height(self):
        source, frequencies = self.make_source(1)
        for output_channels in (10, 16):
            with self.subTest(output_channels=output_channels):
                samples = self.mix(source, 1, "mono", output_channels)
                self.assert_only_tones(self.projections(samples, frequencies), 2, {0})
                self.assert_silent(samples, [c for c in range(output_channels) if c != 2])
                self.assert_silent(samples[16800:], list(range(output_channels)))

    def test_stereo_front_order_and_quiet_difference_ambience(self):
        source, frequencies = self.make_source(2)
        for count in (10, 16):
            with self.subTest(output_channels=count):
                samples = self.mix(source, 2, "stereo", count)
                projection = self.projections(samples, frequencies)
                self.assert_only_tones(projection, 0, {0})
                self.assert_only_tones(projection, 1, {1})
                heights = [6, 7, 8, 9] if count == 10 else [10, 11, 14, 15]
                for channel in [4, 5] + heights:
                    self.assert_only_tones(projection, channel, {0, 1})
                    self.assertLess(np.max(np.abs(samples[:, channel])), np.max(np.abs(samples[:, :2])) * 0.4)
                self.assert_silent(samples, [2, 3] if count == 10 else [2, 3, 6, 7, 8, 9, 12, 13])
                self.assert_silent(samples[16800:], list(range(count)))

    def test_51_bed_order_and_height_left_right_assignment(self):
        source, frequencies = self.make_source(6)
        for layout in ("5.1", "5.1(side)"):
            for count in (10, 16):
                with self.subTest(layout=layout, output_channels=count):
                    samples = self.mix(source, 6, layout, count)
                    projection = self.projections(samples, frequencies)
                    for channel in range(6):
                        self.assert_only_tones(projection, channel, {channel})
                    left = [6, 8] if count == 10 else [10, 14]
                    right = [7, 9] if count == 10 else [11, 15]
                    for channel in left:
                        self.assert_only_tones(projection, channel, {4})
                    for channel in right:
                        self.assert_only_tones(projection, channel, {5})
                    if count == 16:
                        self.assert_silent(samples, [6, 7, 8, 9, 12, 13])

    def test_71_native_rear_side_order_reaches_correct_cbi_positions(self):
        source, frequencies = self.make_source(8)
        for count in (10, 16):
            with self.subTest(output_channels=count):
                samples = self.mix(source, 8, "7.1", count)
                projection = self.projections(samples, frequencies)
                for channel in range(4):
                    self.assert_only_tones(projection, channel, {channel})
                if count == 10:
                    self.assert_only_tones(projection, 4, {4, 6})
                    self.assert_only_tones(projection, 5, {5, 7})
                else:
                    for channel, source_tone in ((4, 6), (5, 7), (6, 4), (7, 5)):
                        self.assert_only_tones(projection, channel, {source_tone})
                    self.assert_silent(samples, [8, 9, 12, 13])
                for channel in ([6, 8] if count == 10 else [10, 14]):
                    self.assert_only_tones(projection, channel, {4, 6})
                for channel in ([7, 9] if count == 10 else [11, 15]):
                    self.assert_only_tones(projection, channel, {5, 7})

    def test_near_full_scale_sources_keep_headroom_and_exact_short_output(self):
        for channels, layout in ((1, "mono"), (2, "stereo"), (6, "5.1"), (8, "7.1")):
            source, _ = self.make_source(channels, sample_rate=48000, amplitude=0.999)
            for count in (10, 16):
                with self.subTest(channels=channels, output_channels=count):
                    samples = self.mix(source, channels, layout, count, duration=0.12345)
                    self.assertLess(np.max(np.abs(samples)), 0.95)

    def test_delayed_container_audio_keeps_its_position_relative_to_video(self):
        source = self.root / "audio begins half a second after video.mkv"
        self.run_tool([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                       "color=size=64x48:rate=10:duration=2", "-itsoffset", "0.5", "-f", "lavfi", "-i",
                       "sine=frequency=500:sample_rate=48000:duration=1", "-map", "0:v:0", "-map", "1:a:0",
                       "-c:v", "libx264", "-c:a", "pcm_s16le", str(source)])
        probe = json.loads(self.run_tool([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(source)]).stdout)
        video, audio = probe["streams"]
        offset = float(audio["start_time"]) - float(video["start_time"])
        self.assertAlmostEqual(offset, 0.5)
        for count in (10, 16):
            with self.subTest(output_channels=count):
                samples = self.mix(source, 1, "mono", count, duration=2.0, audio_offset=offset)
                self.assert_silent(samples[:24000], list(range(count)))
                self.assertGreater(np.max(np.abs(samples[28800:67200, 2])), 0.05)
                self.assert_silent(samples[72000:], list(range(count)))

    def test_audio_before_video_is_trimmed_by_samples_without_changing_program_timing(self):
        audio = self.root / "early audio.wav"
        time = np.arange(48000) / 48000
        waveforms = np.where(time < 0.2, np.sin(2 * np.pi * 400 * time), np.sin(2 * np.pi * 1000 * time)) * 0.2
        with wave.open(str(audio), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            writer.writeframes((waveforms * 32767).astype("<i2").tobytes())
        source = self.root / "video begins after audio.mkv"
        self.run_tool([FFMPEG, "-v", "error", "-itsoffset", "0.2", "-f", "lavfi", "-i",
                       "color=size=64x48:rate=10:duration=0.5", "-i", str(audio),
                       "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-c:a", "pcm_s16le", str(source)])
        probe = json.loads(self.run_tool([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(source)]).stdout)
        video, audio_track = probe["streams"]
        offset = float(audio_track["start_time"]) - float(video["start_time"])
        self.assertAlmostEqual(offset, -0.2)
        for count in (10, 16):
            with self.subTest(output_channels=count):
                samples = self.mix(source, 1, "mono", count, audio_offset=offset)
                projection = self.projections(samples, np.array([400, 1000]))
                self.assert_only_tones(projection, 2, {1})

    def test_submillisecond_offset_does_not_round_to_milliseconds(self):
        source, _ = self.make_source(1, sample_rate=48000)
        samples = self.mix(source, 1, "mono", 10, audio_offset=13 / 48000)
        self.assert_silent(samples[:14], list(range(10)))  # source sample zero is also silent
        self.assertGreater(abs(samples[14, 2]), 0.001)

    def test_audio_entirely_before_video_becomes_exact_length_silence(self):
        source, _ = self.make_source(1, sample_rate=48000, duration=0.25)
        samples = self.mix(source, 1, "mono", 10, audio_offset=-0.5)
        self.assert_silent(samples, list(range(10)))


if __name__ == "__main__":
    unittest.main()
