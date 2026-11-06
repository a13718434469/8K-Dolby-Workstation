import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

import ai_audio
from hires_audio import choose_free_audio_stream


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def run(args, cwd=None):
    result = subprocess.run([str(v) for v in args], cwd=cwd, capture_output=True, timeout=45)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


def probe(path):
    data = subprocess.check_output([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(path)])
    return json.loads(data)["streams"]


def decode(path, channels):
    raw = subprocess.check_output([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0",
                                   "-c:a", "pcm_f32le", "-f", "f32le", "-"])
    return np.frombuffer(raw, dtype="<f4").reshape(-1, channels)


class AudioAdmissionTests(unittest.TestCase):
    def test_music_masters_and_spatial_audio_remain_on_existing_path(self):
        for plan in ({"action": "alac", "channels": 2}, {"action": "pcm", "channels": 2},
                     {"action": "copy", "channels": 6}, {"action": "copy", "channels": 2, "joc": True}):
            reason = ai_audio.ai_skip_reason(plan)
            self.assertTrue(reason)
            self.assertEqual(ai_audio.analyze_ai_audio(None, None, plan, 1, 0, None, None),
                             {"applied": False, "detail": reason})
        self.assertEqual(ai_audio.ai_skip_reason({"action": "copy", "channels": 2}), "")

    def test_reject_replaced_model_before_invoking_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.rnnn"
            path.write_bytes(b"invalid")
            with self.assertRaisesRegex(ai_audio.AiAudioError, "校验"):
                ai_audio.verify_ai_model(FFMPEG, path, None)


@unittest.skipUnless(FFMPEG and FFPROBE and ai_audio.MODEL_PATH.is_file(), "local RNNoise model and FFmpeg required")
class InferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ai_audio.verify_ai_model(FFMPEG, ai_audio.MODEL_PATH, run)

    def make_sound(self, rate, frames, samples):
        wav = self.root / "input.wav"
        with wave.open(str(wav), "wb") as out:
            out.setnchannels(samples.shape[1])
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes((samples[:frames] * 32767).astype("<i2").tobytes())
        source = self.root / "input.m4a"
        run([FFMPEG, "-v", "error", "-i", wav, "-c:a", "aac", "-b:a", "192k", source])
        return source

    def test_real_model_at_original_rate_preserves_offset_end_and_channel_order(self):
        rate, samples, delay = 44100, 44681, 71
        rng = np.random.default_rng(123)
        noise = rng.uniform(-.03, .03, (samples, 2))
        for channel, start in ((0, 720), (1, samples - 1540)):
            noise[start:start+1000, channel] += rng.uniform(-.35, .35, 1000)
        source = self.make_sound(rate, samples, noise)
        _, plan = choose_free_audio_stream(probe(source))
        evidence = ai_audio.analyze_ai_audio(FFMPEG, source, plan, samples/rate, delay/rate, ai_audio.MODEL_PATH, run)
        self.assertTrue(evidence["applied"])
        target = self.root / "processed.mp4"
        args = ai_audio.ai_encode_args(plan, evidence, samples/rate, delay/rate)
        run([FFMPEG, "-v", "error", "-i", source, "-map", "0:a:0", *args, target], cwd=ai_audio.MODEL_PATH.parent)
        result = ai_audio.verify_ai_audio(FFMPEG, target, probe(target)[0], plan, evidence, samples/rate, run)
        self.assertEqual(result["verified_samples"], samples)
        original, final = decode(source, 2), decode(target, 2)
        self.assertEqual(final.shape, (samples, 2))
        for channel, start in ((0, 720), (1, samples - 1540)):
            # The independent decoded source supplies the timing reference.
            reference = original[start:start+1000, channel]
            begin = start + delay
            where = np.correlate(final[begin-540:begin+1000+540, channel], reference, "valid")
            self.assertLessEqual(abs(int(np.argmax(where)) - 540), 2)
        quiet = slice(rate//2, rate*3//4)
        self.assertLess(float(np.mean(final[quiet] ** 2)), .42 * float(np.mean(original[quiet] ** 2)))
        with self.assertRaisesRegex(ai_audio.AiAudioError, "样本数"):
            ai_audio.verify_ai_audio(FFMPEG, target, probe(target)[0], plan, evidence, .9, run)

    def test_silence_is_measured_and_left_on_original_path(self):
        source = self.make_sound(48000, 4800, np.zeros((4800, 1)))
        _, plan = choose_free_audio_stream(probe(source))
        evidence = ai_audio.analyze_ai_audio(FFMPEG, source, plan, .1, 0, None, run)
        self.assertFalse(evidence["applied"])
        self.assertIsNone(evidence["before"]["peak_dbfs"])
