"""Regression tests for media timing, recoverability and truthful output modes.

Run with ``python -m unittest discover -s tests -v``. Real media integration
tests use FFmpeg/ffprobe from PATH (and skip only if those tools are missing).
AI command tests replace inference, not FFmpeg or final-output inspection.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import pipeline


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def command(args):
    completed = subprocess.run(
        [str(a) for a in args], capture_output=True, text=True,
        errors="replace", timeout=60,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr[-5000:])
    return completed.stdout


def inspect_media(path):
    return json.loads(command([
        FFPROBE, "-v", "error", "-count_frames", "-show_streams",
        "-show_format", "-of", "json", path,
    ]))


try:
    DOVI = pipeline.find_tool("dovi_tool")
except FileNotFoundError:
    DOVI = None
HAS_NATIVE_DOVI = bool(
    DOVI and FFMPEG and "dovi_rpu" in command([FFMPEG, "-hide_banner", "-bsfs"])
)


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and ffprobe are required")
class MediaPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dolby-regression-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source with spaces.mp4"
        self.output = self.root / "result with spaces.mp4"
        command([
            FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=48x32:rate=6:duration=1", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", self.source,
        ])

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **changes):
        cfg = {
            "video": str(self.source), "output": str(self.output),
            "workspace": str(self.root / "workspace"),
            "width": 64, "height": 48, "ai": False, "interp": False,
            "color_mode": "sdr", "audio_mode": "copy", "auto_clean": True,
            "batch_size": 2, "preset": "ultrafast", "crf": 28,
        }
        cfg.update(changes)
        return cfg

    def resume(self, first):
        checkpoint = pipeline.load_checkpoint(first.root / "checkpoint.json")
        cfg = dict(checkpoint["cfg"], resume=True)
        return pipeline.Pipeline(cfg)

    def assert_video(self, path, expected_frames=6, expected_fps="6", duration=1):
        info = inspect_media(path)
        videos = [s for s in info["streams"] if s["codec_type"] == "video"]
        self.assertEqual(len(videos), 1)
        video = videos[0]
        self.assertEqual((video["width"], video["height"]), (64, 48))
        self.assertEqual(video["codec_name"], "hevc")
        self.assertEqual(Fraction(video["r_frame_rate"]), Fraction(expected_fps))
        self.assertEqual(int(video["nb_read_frames"]), expected_frames)
        self.assertAlmostEqual(float(video["duration"]), duration, delta=0.04)
        return info, video

    def test_silent_source_preserves_all_frames_and_duration_across_chunks(self):
        result = pipeline.Pipeline(self.config()).run()
        self.assertEqual(Path(result), self.output)
        info, video = self.assert_video(result)
        self.assertEqual([s["codec_type"] for s in info["streams"]], ["video"])
        self.assertEqual(video.get("color_transfer"), "bt709")

    def test_external_short_audio_is_padded_without_shortening_video(self):
        audio = self.root / "short tone.wav"
        command([
            FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.2", audio,
        ])
        result = pipeline.Pipeline(self.config(audio=str(audio), audio_mode="aac")).run()
        info, _ = self.assert_video(result)
        tracks = [s for s in info["streams"] if s["codec_type"] == "audio"]
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["codec_name"], "aac")
        self.assertAlmostEqual(float(tracks[0]["duration"]), 1, delta=0.07)

    def test_fractional_input_rate_and_duration_survive_chunk_muxing(self):
        command([
            FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=48x32:rate=30000/1001", "-frames:v", "30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", self.source,
        ])
        result = pipeline.Pipeline(self.config(batch_size=11)).run()
        self.assert_video(result, expected_frames=30,
                          expected_fps="30000/1001", duration=1.001)

    def test_eac3_encoding_pads_short_audio_and_preserves_channel_count(self):
        audio = self.root / "short stereo.wav"
        command([
            FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.2", "-ac", "2", audio,
        ])
        result = pipeline.Pipeline(self.config(audio=str(audio), audio_mode="eac3")).run()
        info, _ = self.assert_video(result)
        audio_stream = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual(audio_stream["codec_name"], "eac3")
        self.assertEqual(audio_stream["channels"], 2)
        self.assertAlmostEqual(float(audio_stream["duration"]), 1, delta=0.07)

    def test_audio_copy_preserves_encoded_packet_payload(self):
        def payload_hash(path):
            return command([
                FFMPEG, "-v", "error", "-i", path, "-map", "0:a:0",
                "-c:a", "copy", "-f", "hash", "-hash", "sha256", "-",
            ]).strip()

        for codec, suffix in (("aac", ".m4a"), ("eac3", ".eac3")):
            with self.subTest(codec=codec):
                audio = self.root / ("short source audio" + suffix)
                command([
                    FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=0.2",
                    "-c:a", codec, "-b:a", "192k", audio,
                ])
                result = pipeline.Pipeline(self.config(
                    audio=str(audio), audio_mode="copy",
                    output=str(self.root / f"copied {codec}.mp4"),
                )).run()
                self.assert_video(result)
                self.assertEqual(payload_hash(result), payload_hash(audio))

    def test_hdr10_uses_ten_bit_pq_with_bt2020_signaling(self):
        result = pipeline.Pipeline(self.config(color_mode="hdr10")).run()
        _, video = self.assert_video(result)
        self.assertEqual(video["pix_fmt"], "yuv420p10le")
        self.assertEqual(video["color_transfer"], "smpte2084")
        self.assertEqual(video["color_primaries"], "bt2020")
        self.assertEqual(video["color_space"], "bt2020nc")

    def make_rpu(self, frames):
        # Synthetic fixed metadata tests container/bitstream plumbing. It is
        # deliberately not a claim that these trims are professionally graded.
        config = self.root / "synthetic RPU config.json"
        config.write_text(json.dumps({
            "cm_version": "V40", "length": frames,
            "level6": {
                "max_display_mastering_luminance": 1000,
                "min_display_mastering_luminance": 1,
                "max_content_light_level": 100,
                "max_frame_average_light_level": 100,
            },
        }), encoding="utf-8")
        rpu = self.root / "synthetic RPU.bin"
        command([DOVI, "generate", "-j", config, "-p", "8.1", "-o", rpu])
        return rpu

    @unittest.skipUnless(HAS_NATIVE_DOVI, "dovi_tool and FFmpeg dovi_rpu BSF are required")
    def test_native_dolby_vision_has_profile81_and_one_rpu_per_video_frame(self):
        rpu = self.make_rpu(6)
        result = pipeline.Pipeline(self.config(color_mode="dv81", rpu=str(rpu))).run()
        _, video = self.assert_video(result)
        dv = next(s for s in video["side_data_list"]
                  if s["side_data_type"] == "DOVI configuration record")
        self.assertEqual(dv["dv_profile"], 8)
        self.assertEqual(dv["dv_bl_signal_compatibility_id"], 1)
        raw = self.root / "verification.hevc"
        extracted = self.root / "verification RPU.bin"
        command([
            FFMPEG, "-v", "error", "-y", "-i", result, "-map", "0:v:0",
            "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", raw,
        ])
        command([DOVI, "extract-rpu", "-i", raw, "-o", extracted])
        summary = command([DOVI, "info", "-i", extracted, "-s"])
        frames = re.search(r"Frames:\s*(\d+)", summary)
        self.assertIsNotNone(frames)
        self.assertEqual(int(frames[1]), 6)

    @unittest.skipUnless(HAS_NATIVE_DOVI, "dovi_tool and FFmpeg dovi_rpu BSF are required")
    def test_mismatched_native_rpu_does_not_publish_dolby_vision_output(self):
        rpu = self.make_rpu(5)
        subject = pipeline.Pipeline(self.config(color_mode="dv81", rpu=str(rpu)))
        with self.assertRaises(pipeline.PipelineError):
            subject.run()
        self.assertFalse(self.output.exists())
        self.assertNotIn("dv", subject.manifest["completed"])

    def test_overwrite_is_rejected_and_existing_output_is_unchanged(self):
        self.output.write_bytes(b"valuable existing output")
        with self.assertRaises((ValueError, pipeline.PipelineError)):
            pipeline.Pipeline(self.config()).run()
        self.assertEqual(self.output.read_bytes(), b"valuable existing output")

    def test_output_cannot_replace_source(self):
        original = self.source.read_bytes()
        with self.assertRaises((ValueError, pipeline.PipelineError)):
            pipeline.Pipeline(self.config(output=str(self.source))).run()
        self.assertEqual(self.source.read_bytes(), original)

    def test_dolby_vision_without_matching_rpu_fails_without_fallback_output(self):
        with self.assertRaises((ValueError, pipeline.PipelineError)):
            pipeline.Pipeline(self.config(color_mode="dv81", rpu="")).run()
        self.assertFalse(self.output.exists())

    def test_failed_encode_resumes_without_skipping_unfinished_stage(self):
        cfg = self.config()
        first = pipeline.Pipeline(cfg)
        real_run = first._run

        encoded = []

        def fail_encode(cmd):
            if "libx265" in cmd:
                encoded.append(cmd)
                if len(encoded) == 2:
                    raise pipeline.PipelineError("simulated encoder failure")
            return real_run(cmd)

        with patch.object(first, "_run", side_effect=fail_encode):
            with self.assertRaises(pipeline.PipelineError):
                first.run()
        self.assertFalse(self.output.exists())
        self.assertTrue((first.job_dir / "manifest.json").is_file())
        self.assertEqual(len(first.manifest["chunks"]), 1)
        resumed = self.resume(first)
        resumed_run = resumed._run
        commands = []

        def record(cmd):
            commands.append(cmd)
            return resumed_run(cmd)

        with patch.object(resumed, "_run", side_effect=record):
            self.assert_video(resumed.run())
        self.assertEqual(resumed.job_dir, first.job_dir)
        self.assertEqual(sum("libx265" in c for c in commands), 2)
        self.assertFalse(any("-pix_fmt" in c and "rgb24" in c for c in commands))

    def test_cancel_does_not_commit_partial_output_and_job_can_resume(self):
        first = pipeline.Pipeline(self.config())
        real_run = first._run

        def cancel_encode(cmd):
            if "libx265" in cmd:
                raise pipeline.PipelineCancelled("simulated cancellation")
            return real_run(cmd)

        with patch.object(first, "_run", side_effect=cancel_encode):
            with self.assertRaises(pipeline.PipelineCancelled):
                first.run()
        self.assertFalse(self.output.exists())
        resumed = self.resume(first)
        self.assert_video(resumed.run())
        self.assertEqual(resumed.job_dir, first.job_dir)

    def test_resume_rejects_source_changed_since_checkpoint(self):
        first = pipeline.Pipeline(self.config())
        real_run = first._run

        def interrupt(cmd):
            if "libx265" in cmd:
                raise pipeline.PipelineError("simulated encoder failure")
            return real_run(cmd)

        with patch.object(first, "_run", side_effect=interrupt):
            with self.assertRaises(pipeline.PipelineError):
                first.run()
        # An MP4 can still be probed after trailing bytes are appended. The
        # fingerprint must reject it even if its media metadata is unchanged.
        with self.source.open("ab") as handle:
            handle.write(b"source changed after checkpoint")
        with self.assertRaises(pipeline.PipelineError):
            self.resume(first).run()
        self.assertFalse(self.output.exists())

    def test_publication_failure_leaves_no_partial_final_and_can_resume(self):
        first = pipeline.Pipeline(self.config())
        operation = "rename" if os.name == "nt" else "link"
        real_operation = getattr(pipeline.os, operation)

        def fail_publication(src, dest, *args, **kwargs):
            if Path(dest) == self.output:
                raise OSError("simulated target-volume publication failure")
            return real_operation(src, dest, *args, **kwargs)

        with patch.object(pipeline.os, operation, side_effect=fail_publication):
            with self.assertRaises(pipeline.PipelineError):
                first.run()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob("*.partial.mp4")))
        self.assertNotIn("mux", first.manifest["completed"])
        resumed = self.resume(first)
        self.assert_video(resumed.run())
        self.assertEqual(resumed.job_dir, first.job_dir)

    def run_with_fake_ai(self, omit_last_frame=False):
        tools = self.root / "fake tools"
        model = tools / "models"
        model.mkdir(parents=True)
        rife = tools / "rife-v4.6"
        rife.mkdir()
        for extension in ("bin", "param"):
            (model / f"realesrgan-x4plus.{extension}").write_bytes(b"fixture")
            (rife / f"flownet.{extension}").write_bytes(b"fixture")
        subject = pipeline.Pipeline(self.config(ai=True, interp=True, target_fps=12))
        real_run = subject._run
        real_find = pipeline.find_tool
        commands = []

        def tool(name):
            if name in ("rife-ncnn-vulkan", "realesrgan-ncnn-vulkan"):
                return str(tools / (name + ".exe"))
            return real_find(name)

        def fake(cmd):
            name = Path(cmd[0]).name
            if name.startswith(("rife-ncnn-vulkan", "realesrgan-ncnn-vulkan")):
                commands.append(cmd)
                source = Path(cmd[cmd.index("-i") + 1])
                dest = Path(cmd[cmd.index("-o") + 1])
                images = sorted(source.glob("*.png"))
                if name.startswith("rife"):
                    count = int(cmd[cmd.index("-n") + 1])
                    for index in range(count - int(omit_last_frame)):
                        shutil.copy2(images[index * len(images) // count],
                                     dest / f"{index + 1:08d}.png")
                else:
                    for source in images:
                        shutil.copy2(source, dest / source.name)
                return ""
            return real_run(cmd)

        with patch.object(pipeline, "TOOLS_DIR", tools), \
                patch.object(pipeline, "find_tool", side_effect=tool), \
                patch.object(subject, "_run", side_effect=fake):
            if omit_last_frame:
                with self.assertRaises(pipeline.PipelineError):
                    subject.run()
            else:
                self.assert_video(subject.run(), expected_frames=12, expected_fps="12")
        return subject, commands

    def test_rife_receives_target_count_and_every_sr_chunk_preserves_sequence(self):
        subject, commands = self.run_with_fake_ai()
        rife = [c for c in commands if Path(c[0]).name.startswith("rife")]
        sr = [c for c in commands if Path(c[0]).name.startswith("realesrgan")]
        self.assertEqual(len(rife), 1)
        self.assertEqual(rife[0][rife[0].index("-n") + 1], "12")
        self.assertEqual(len(sr), 6)
        self.assertEqual(sum(c["count"] for c in subject.manifest["chunks"]), 12)

    def test_missing_ai_frame_is_an_error_and_never_committed(self):
        subject, commands = self.run_with_fake_ai(omit_last_frame=True)
        self.assertFalse(self.output.exists())
        self.assertNotIn("interpolate", subject.manifest["completed"])
        self.assertFalse(any(Path(c[0]).name.startswith("realesrgan") for c in commands))


class TimingTests(unittest.TestCase):
    def test_target_count_uses_requested_rate_instead_of_fixed_fourfold(self):
        self.assertEqual(pipeline.output_frame_count(24, "24/1", "120"), 120)
        self.assertEqual(pipeline.output_frame_count(30, "30/1", "120"), 120)
        self.assertEqual(pipeline.output_frame_count(60, "60/1", "120"), 120)

    def test_fractional_source_rate_is_not_rounded(self):
        # 1001 frames at 30000/1001 last 33.400033... seconds.
        self.assertEqual(
            pipeline.output_frame_count(1001, "30000/1001", "120"), 4008,
        )

    def test_storage_reserves_full_source_and_interpolation_plus_sr_batch(self):
        info = {"width": 1920, "height": 1080, "duration": 10,
                "total_frames": 300, "fps": 30, "fps_fraction": "30/1"}
        cfg = {"width": 7680, "height": 4320, "interp": True,
               "target_fps": 120, "ai": True, "batch_size": 64,
               "auto_clean": True}
        mb, _ = pipeline.estimate_disk(info, cfg)
        minimum_raw = (300 + 1200) * 1920 * 1080 * 3
        minimum_raw += 64 * 7680 * 4320 * 3
        self.assertGreaterEqual(mb * 1024 ** 2, minimum_raw)
        keep_all, _ = pipeline.estimate_disk(info, dict(cfg, auto_clean=False))
        self.assertGreater(keep_all, mb)


class ProcessControlTests(unittest.TestCase):
    def test_cancel_terminates_child_that_produces_no_log_output(self):
        subject = pipeline.Pipeline({})
        started = threading.Event()
        errors = []
        children = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            started.set()
            return child

        def run():
            try:
                subject._run([sys.executable, "-c", "import time; time.sleep(30)"])
            except Exception as exc:
                errors.append(exc)

        with patch.object(pipeline.subprocess, "Popen", side_effect=launch):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(started.wait(5), "child process did not start")
                subject.cancel()
                worker.join(timeout=6)
                self.assertFalse(worker.is_alive(), "cancel blocked on a silent pipe")
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], pipeline.PipelineCancelled)
                self.assertIsNone(subject._process)
                self.assertIsNotNone(children[0].poll())
            finally:
                subject.cancel()
                for child in children:
                    if child.poll() is None:
                        child.kill()
                worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
