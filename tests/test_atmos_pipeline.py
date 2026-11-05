"""Real-media rejection, preservation and resumability around the Atmos adapter.

No test here generates new JOC or proves that a DEE license is available. The
fixture adapter deliberately returns an already encoded Atmos sample; those
tests verify wiring, validation and cache integrity only. The negative adapter
really encodes ordinary E-AC-3 and must be rejected, even though FFmpeg succeeds.
No AWS service is contacted and no audio is uploaded.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline
from quality import inspect_audio, inspect_media


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
ATMOS_FIXTURE = os.environ.get("DOLBY_ATMOS_FIXTURE", "")
HAS_ATMOS_FIXTURE = bool(ATMOS_FIXTURE and Path(ATMOS_FIXTURE).is_file())


def command(args):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60, **options)
    if result.returncode:
        raise AssertionError(result.stderr[-4000:])
    return result.stdout


def payload_hash(path):
    return command([FFMPEG, "-nostdin", "-v", "error", "-i", path,
                    "-map", "0:a:0", "-c:a", "copy", "-f", "hash", "-hash", "sha256", "-"]).strip().split("=")[-1]


class CancelAfterStage(pipeline.Pipeline):
    """Exercise real cancellation after an atomically saved phase boundary."""

    stop_stage = "atmos_encode"

    def _stage_done(self, name, check_cancel=True):
        super()._stage_done(name, check_cancel=check_cancel)
        if name == self.stop_stage:
            self.cancel()


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and ffprobe required")
class AtmosPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_temp = tempfile.TemporaryDirectory(prefix="atmos-pipeline-fixtures-")
        cls.addClassCleanup(cls.fixture_temp.cleanup)
        cls.fixtures = Path(cls.fixture_temp.name).resolve()
        cls.video_fixture = cls.fixtures / "silent-picture.mp4"
        cls.pcm_fixture = cls.fixtures / "ordinary-stereo.wav"
        command([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=6:duration=1",
                 "-c:v", "libx264", "-bf", "0", "-pix_fmt", "yuv420p", "-color_primaries", "bt709",
                 "-color_trc", "bt709", "-colorspace", "bt709", cls.video_fixture])
        command([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                 "aevalsrc=0.12*sin(2*PI*440*t)|0.10*sin(2*PI*660*t):s=48000:d=1",
                 "-c:a", "pcm_s16le", cls.pcm_fixture])
        cls.joc_fixture = cls.fixtures / "preencoded-joc-1s.mp4"
        if HAS_ATMOS_FIXTURE:
            command([FFMPEG, "-v", "error", "-i", ATMOS_FIXTURE, "-map", "0:a:0", "-c:a", "copy",
                     "-t", "1", cls.joc_fixture])
            info = inspect_audio(cls.joc_fixture)
            if info["atmos"] != "confirmed" or info["audio_codec"] != "eac3":
                raise AssertionError("DOLBY_ATMOS_FIXTURE must contain genuine preencoded E-AC-3 JOC")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atmos-pipeline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.video = self.root / "picture.mp4"
        self.pcm = self.root / "ordinary.wav"
        shutil.copyfile(self.video_fixture, self.video)
        shutil.copyfile(self.pcm_fixture, self.pcm)
        self.output = self.root / "result.mp4"
        self.workspace = self.root / "workspace"
        # Only installation/configuration checks read this inert file. The
        # encode_dee adapter is patched in every generation test, so neither
        # these bytes nor guessed commercial-encoder arguments are executed.
        self.executable = self.root / "fixture-adapter-never-executed.exe"
        self.executable.write_bytes(b"Test-only installation identity, not a DEE binary")
        self.profile = self.root / "fixture-profile.json"
        self.profile.write_text(json.dumps({"schema_version": 1, "input_layout": "5.1.4",
                                           "argv": ["{input}", "{output}"], "output_suffix": ".mp4",
                                           "description": "transport fixture only, not DEE command flags"}), encoding="utf-8")

    def config(self, **changes):
        cfg = {"video": str(self.video), "audio": str(self.pcm), "output": str(self.output),
               "workspace": str(self.workspace), "route": "preserve", "delivery": "general",
               "color_mode": "source", "audio_mode": "atmos_auto", "require_atmos": True,
               "atmos_backend": "dee", "dee_path": str(self.executable), "dee_profile": str(self.profile),
               "ai": False, "interp": False, "auto_clean": False}
        cfg.update(changes)
        return cfg

    def report(self):
        return json.loads(Path(str(self.output) + ".quality.json").read_text(encoding="utf-8"))

    def checkpoint(self):
        return json.loads((self.workspace / "checkpoint.json").read_text(encoding="utf-8"))

    def fixture_adapter(self, cfg, master, folder, run):
        """Returns a preencoded fixture, never claims to encode the PCM as JOC."""
        info = inspect_audio(master)
        self.assertEqual((info["audio_codec"], info["audio_channels"], info["audio_sample_rate"]),
                         ("pcm_s24le", 10, 48000))
        self.assertAlmostEqual(info["audio_duration"], 1.0, places=5)
        target = Path(folder) / "fixture-returned-by-adapter.mp4"
        run([FFMPEG, "-nostdin", "-v", "error", "-y", "-i", self.joc_fixture,
             "-map", "0:a:0", "-c:a", "copy", target])
        return target, {"backend": "preencoded_fixture_test_adapter", "new_joc_encoding_tested": False}

    def cancelled_fixture_job(self, stage="atmos_encode"):
        runner = CancelAfterStage(self.config())
        runner.stop_stage = stage
        with patch("atmos_encoding.encode_dee", side_effect=self.fixture_adapter), \
                self.assertRaises(pipeline.PipelineCancelled):
            runner.run()
        self.assertFalse(self.output.exists())
        self.assertIn(stage, self.checkpoint()["completed"])
        return runner

    def resume_config(self):
        return dict(self.checkpoint()["cfg"], resume=True)

    def test_missing_encoder_rejects_ordinary_audio_before_video_processing(self):
        stages = []
        cfg = self.config(route="transcode", dee_path=str(self.root / "absent-encoder.exe"))
        with patch.object(pipeline.Pipeline, "_native_video", side_effect=AssertionError("video must not start")), \
                patch("atmos_encoding.encode_dee", side_effect=AssertionError("encoder must not start")), \
                self.assertRaisesRegex(pipeline.PipelineError, "Atmos|编码器"):
            pipeline.Pipeline(cfg, stage=lambda *entry: stages.append(entry)).run()
        self.assertEqual(stages, [])
        self.assertFalse(self.output.exists())
        self.assertFalse((self.workspace / "checkpoint.json").exists())

    def test_double_dolby_rejects_silent_source_without_output(self):
        with patch("atmos_encoding.encode_dee", side_effect=AssertionError("encoder must not start")), \
                self.assertRaisesRegex(pipeline.PipelineError, "音轨|声音"):
            pipeline.Pipeline(self.config(audio="")).run()
        self.assertFalse(self.output.exists())

    def test_successful_ordinary_eac3_encoder_is_rejected_as_not_atmos(self):
        def ordinary_encoder(cfg, master, folder, run):
            output = Path(folder) / "ordinary-eac3.mp4"
            run([FFMPEG, "-nostdin", "-v", "error", "-y", "-i", master,
                 "-af", "pan=5.1|c0=c0|c1=c1|c2=c2|c3=c3|c4=c4|c5=c5",
                 "-c:a", "eac3", "-b:a", "640k", "-ar", "48000", output])
            info = inspect_audio(output)
            self.assertEqual(info["audio_codec"], "eac3")
            self.assertEqual(info["audio_channels"], 6)
            self.assertNotEqual(info["atmos"], "confirmed")
            return output, {"backend": "ordinary_eac3_negative_fixture"}

        with patch("atmos_encoding.encode_dee", side_effect=ordinary_encoder) as encoder, \
                self.assertRaisesRegex(pipeline.PipelineError, "真实 Atmos 检查"):
            pipeline.Pipeline(self.config()).run()
        encoder.assert_called_once()
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(str(self.output) + ".quality.json").exists())
        self.assertNotIn("atmos_encode", self.checkpoint()["completed"])

    @unittest.skipUnless(HAS_ATMOS_FIXTURE, "set DOLBY_ATMOS_FIXTURE for genuine preencoded JOC")
    def test_existing_joc_auto_mode_needs_no_backend_and_preserves_final_payload(self):
        cfg = self.config(audio=str(self.joc_fixture), dee_path=str(self.root / "missing.exe"), dee_profile="")
        with patch("atmos_encoding.backend_identity", side_effect=AssertionError("copy must not need encoder")), \
                patch("atmos_encoding.encode_dee", side_effect=AssertionError("copy must not encode")):
            pipeline.Pipeline(cfg).run()
        report = self.report()
        audio = report["export"]["atmos_audio"]
        self.assertFalse(audio["generated"])
        self.assertEqual(audio["method"], "source_stream_copy")
        self.assertTrue(audio["payload_preserved"])
        self.assertEqual(audio["final_payload_sha256"], payload_hash(self.output))
        self.assertEqual(payload_hash(self.joc_fixture), payload_hash(self.output))
        self.assertFalse(report["platform_verified"])

    @unittest.skipUnless(HAS_ATMOS_FIXTURE, "set DOLBY_ATMOS_FIXTURE for adapter wiring tests")
    def test_preencoded_fixture_adapter_wires_real_master_and_validation_not_new_joc_encoding(self):
        with patch("atmos_encoding.encode_dee", side_effect=self.fixture_adapter) as encoder:
            pipeline.Pipeline(self.config()).run()
        encoder.assert_called_once()
        report = self.report()
        audio = report["export"]["atmos_audio"]
        self.assertTrue(audio["generated"], "the generation branch should have been exercised")
        self.assertFalse(audio["encoding"]["new_joc_encoding_tested"])
        self.assertEqual(audio["encoding"]["backend"], "preencoded_fixture_test_adapter")
        self.assertTrue(audio["encoding"]["atmos_verified"] and audio["encoding"]["decode_verified"])
        self.assertTrue(audio["payload_preserved"])
        self.assertEqual(audio["final_payload_sha256"], payload_hash(self.output))
        self.assertEqual(inspect_media(self.output)["atmos"], "confirmed")
        self.assertFalse(report["platform_verified"])

    @unittest.skipUnless(HAS_ATMOS_FIXTURE, "set DOLBY_ATMOS_FIXTURE for adapter resume tests")
    def test_preencoded_fixture_cancel_after_verified_encoding_resumes_without_reencoding(self):
        runner = self.cancelled_fixture_job()
        checkpoint = self.checkpoint()
        self.assertNotIn("audio", checkpoint["completed"])
        encoded = runner.job_dir / "atmos" / "encoded-verified.mp4"
        before = hashlib.sha256(encoded.read_bytes()).hexdigest()
        with patch("atmos_encoding.encode_dee", side_effect=AssertionError("verified cache must be reused")), \
                patch("atmos_encoding.prepare_master", side_effect=AssertionError("PCM master must be reused")):
            pipeline.Pipeline(self.resume_config()).run()
        self.assertEqual(before, self.report()["export"]["atmos_audio"]["encoding"]["sha256"])
        self.assertEqual(payload_hash(encoded), payload_hash(self.output))
        self.assertFalse((self.workspace / "checkpoint.json").exists())

    @unittest.skipUnless(HAS_ATMOS_FIXTURE, "set DOLBY_ATMOS_FIXTURE for adapter cache tests")
    def test_preencoded_fixture_caches_reject_content_tampering(self):
        for stage, relative, message in (
            ("atmos_master", "atmos/master.wav", "母版缓存已改变"),
            ("atmos_encode", "atmos/encoded-verified.mp4", "编码缓存已改变"),
            ("audio", "audio.mp4", "Atmos 缓存已改变"),
        ):
            with self.subTest(stage=stage):
                self.workspace = self.root / ("workspace-" + stage)
                runner = self.cancelled_fixture_job(stage)
                damaged = runner.job_dir / relative
                with damaged.open("ab") as handle:
                    handle.write(b"tampered cache")
                with patch("atmos_encoding.encode_dee", side_effect=AssertionError("damaged caches must not be silently rebuilt")), \
                        self.assertRaisesRegex(pipeline.PipelineError, message):
                    pipeline.Pipeline(self.resume_config()).run()
                self.assertFalse(self.output.exists())

    @unittest.skipUnless(HAS_ATMOS_FIXTURE, "set DOLBY_ATMOS_FIXTURE for adapter identity tests")
    def test_preencoded_fixture_cache_rejects_changed_backend_profile_and_executable(self):
        for changed in ("profile", "executable"):
            with self.subTest(changed=changed):
                self.workspace = self.root / ("workspace-" + changed)
                self.cancelled_fixture_job()
                if changed == "profile":
                    data = json.loads(self.profile.read_text(encoding="utf-8"))
                    data["description"] += " changed"
                    self.profile.write_text(json.dumps(data), encoding="utf-8")
                else:
                    with self.executable.open("ab") as handle:
                        handle.write(b" changed backend identity")
                with patch("atmos_encoding.encode_dee", side_effect=AssertionError("identity mismatch must not encode")), \
                        self.assertRaisesRegex(pipeline.PipelineError, "编码器配置已改变"):
                    pipeline.Pipeline(self.resume_config()).run()
                self.assertFalse(self.output.exists())

    @unittest.skipUnless(HAS_ATMOS_FIXTURE, "set DOLBY_ATMOS_FIXTURE for adapter source integrity tests")
    def test_preencoded_fixture_cache_rejects_changed_pcm_even_with_same_size_and_timestamp(self):
        self.cancelled_fixture_job()
        before = self.pcm.stat()
        data = bytearray(self.pcm.read_bytes())
        data[-100] ^= 1
        self.pcm.write_bytes(data)
        os.utime(self.pcm, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.pcm.stat().st_size, before.st_size)
        self.assertEqual(self.pcm.stat().st_mtime_ns, before.st_mtime_ns)
        with patch("atmos_encoding.encode_dee", side_effect=AssertionError("changed PCM must not reuse or encode")), \
                self.assertRaisesRegex(pipeline.PipelineError, "Atmos 输入、混音或编码器配置已改变"):
            pipeline.Pipeline(self.resume_config()).run()
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(HAS_ATMOS_FIXTURE, "set DOLBY_ATMOS_FIXTURE for final JOC mux integrity tests")
    def test_preencoded_fixture_adapter_rejects_final_mux_with_different_valid_joc_payload(self):
        alternate = self.root / "different-valid-joc.mp4"
        command([FFMPEG, "-v", "error", "-ss", "0.32", "-i", ATMOS_FIXTURE, "-map", "0:a:0",
                 "-c:a", "copy", "-t", "1", alternate])
        info = inspect_audio(alternate)
        if info.get("atmos") != "confirmed" or abs(info.get("audio_duration", 0) - 1) > 0.08:
            self.skipTest("the supplied fixture is too short for a different one-second JOC segment")
        if payload_hash(alternate) == payload_hash(self.joc_fixture):
            self.skipTest("the supplied fixture repeats identical payloads at the test offset")

        class WrongMuxAudio(pipeline.Pipeline):
            def _run(self, cmd, **kwargs):
                if "-movflags" in cmd and str(cmd[-1]).endswith(".partial.mp4"):
                    inputs = [i for i, arg in enumerate(cmd) if str(arg) == "-i"]
                    if len(inputs) == 2:
                        cmd = list(cmd)
                        cmd[inputs[1] + 1] = alternate
                return super()._run(cmd, **kwargs)

        with patch("atmos_encoding.encode_dee", side_effect=self.fixture_adapter), \
                self.assertRaisesRegex(pipeline.PipelineError, "改变了 Atmos 音频码流"):
            WrongMuxAudio(self.config()).run()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob("*.partial.mp4")))


if __name__ == "__main__":
    unittest.main()
