import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import music_sr as sr


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def spectral_signal(length, cutoff=None, seed=752):
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.normal(0, 0.055, length))
    hz = np.fft.rfftfreq(length, 1 / sr.RATE)
    spectrum *= (1 + hz / 700) ** -.6
    if cutoff:
        spectrum *= np.clip((cutoff - hz) / 400, 0, 1)
    return np.fft.irfft(spectrum, n=length).astype("<f4")


class BandwidthTests(unittest.TestCase):
    def test_estimated_edge_and_full_band_protection(self):
        samples = sr.RATE * 2
        def detect(signal):
            return sr._cutoff(io.BytesIO(signal.tobytes()), len(signal), 1, sr.RATE, lambda: None)
        for cutoff in (5500, 8000, 12500, 15700):
            measured = detect(spectral_signal(samples, cutoff))
            self.assertIsNotNone(measured)
            self.assertLess(abs(measured - cutoff), 800)
        self.assertIsNone(detect(spectral_signal(samples)))
        self.assertIsNone(detect(np.zeros(samples, dtype="<f4")))
        t = np.arange(samples) / sr.RATE
        self.assertIsNone(detect(np.sin(t * 2 * np.pi * 200).astype("<f4")))

    def test_surface_protects_hires_and_atmos_and_accepts_low_rate_pcm(self):
        plan = dict(action="alac", channels=2, sample_rate=16000, bits=16, eligible=False)
        self.assertFalse(sr.sr_skip_reason(plan))
        for update in ({"eligible": True}, {"joc": True}, {"channels": 6}, {"bits": 24}):
            self.assertTrue(sr.sr_skip_reason(dict(plan, **update)))

    def test_cancellation_in_spectral_analysis(self):
        def stop():
            raise InterruptedError
        with self.assertRaises(InterruptedError):
            sr._cutoff(io.BytesIO(b""), 10000, 1, sr.RATE, stop)

    def test_publication_rejects_replacement_track_with_wrong_sample_count(self):
        outcome = {"channels": 2, "verified_samples": 5000}
        good = {"codec_name": "alac", "bits_per_raw_sample": 16, "channels": 2,
                "sample_rate": 48000, "start_time": "0", "duration_ts": "5000",
                "time_base": "1/48000"}
        sr.verify_sr_output(good, outcome)
        with self.assertRaises(sr.MusicSRError):
            sr.verify_sr_output(dict(good, duration_ts="4999"), outcome)

    def test_narrow_source_regains_adjacent_band_and_a_pause_stays_silent(self):
        import music_sr_backend
        if not music_sr_backend.model_ready():
            self.skipTest("install the official music model to run the inference contract")
        n = sr.RATE
        source = spectral_signal(n, 4500)
        source[n//3:2*n//3] = 0
        wet = io.BytesIO()
        sr._render(io.BytesIO(source.tobytes()), wet, n, 1, 4500,
                   music_sr_backend.load_model(), lambda: None)
        out = np.frombuffer(wet.getvalue(), "<f4")
        self.assertTrue(np.all(out[n//3+1000:2*n//3-1000] == 0))
        sl = slice(0,n//3)
        window = np.hanning(n//3)
        hz = np.fft.rfftfreq(len(window), 1 / sr.RATE)
        before = np.abs(np.fft.rfft(source[sl] * window))
        after = np.abs(np.fft.rfft(out[sl] * window))
        band = (hz > 5400) & (hz < 7200)
        self.assertGreater(np.linalg.norm(after[band]), 12 * np.linalg.norm(before[band]))

    @unittest.skipUnless(FFMPEG and FFPROBE, "requires FFmpeg and FFprobe")
    def test_real_network_extends_selected_stereo_and_retains_low_band_timing(self):
        import music_sr_backend
        if not music_sr_backend.model_ready():
            self.skipTest("install the official music model to run the inference contract")
        samples = sr.RATE * 2 + 137
        t = np.arange(samples) / sr.RATE
        # Asymmetric events on both sides of the processing cut catch swaps,
        # frame drift and discontinuities. Use different tones per channel.
        rng = np.random.default_rng(126)
        mono = spectral_signal(samples, 7600)
        channels = []
        for k in (0, 1):
            carrier = np.sin(t * 2 * np.pi * (219 + 48 * k))
            harmonics = sum(np.sin(t * 2 * np.pi * (219 + 48 * k) * n) / n for n in range(2, 22))
            envelope = (.12 + .88 * np.exp(-5 * np.mod(t + .19 * k, .37)))
            channels.append((.09 * carrier + .08 * harmonics + mono * rng.uniform(.6, 1)) * envelope)
        # Bandlimit the entire mix after adding the note attacks. A sharp
        # envelope applied after filtering would reintroduce wideband clicks.
        mix = np.stack(channels, axis=1)
        freq = np.fft.rfftfreq(samples, 1 / sr.RATE)
        source = np.fft.irfft(np.fft.rfft(mix, axis=0) *
                              np.clip((7600 - freq) / 380, 0, 1)[:, None],
                              n=samples, axis=0).astype("<f4")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw, wav, out = root / "dry.raw", root / "input.wav", root / "result.mp4"
            raw.write_bytes(source.tobytes())
            run([FFMPEG, "-v", "error", "-f", "f32le", "-ar", str(sr.RATE), "-ac", "2", "-i", raw,
                 "-c:a", "pcm_s16le", wav])
            plan = dict(sample_rate=sr.RATE, channels=2, source_audio_ordinal=0,
                        action="alac", bits=16, eligible=False)
            r = sr.enhance_audio(FFMPEG, wav, plan, samples / sr.RATE, 0, out, root, run, lambda: None)
            self.assertTrue(r["applied"], r)
            info = json.loads(run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", out]).stdout)["streams"][0]
            sr.verify_sr_output(info, r)
            audio = np.frombuffer(run([FFMPEG, "-v", "error", "-i", out, "-f", "f32le", "-"]).stdout,
                                  dtype="<f4").reshape(-1, 2)
            self.assertEqual(audio.shape, source.shape)
            def parts(x):
                return np.fft.rfft(x * np.hanning(samples)[:, None], axis=0)
            hz = np.fft.rfftfreq(samples, 1 / sr.RATE)
            src, result = parts(source), parts(audio)
            for k in (0, 1):
                delta = np.linalg.norm((result - src)[hz < 6600, k]) / np.linalg.norm(src[hz < 6600, k])
                self.assertLess(delta, .003)
                self.assertGreater(np.linalg.norm(result[hz > 8500, k]), 5 * np.linalg.norm(src[hz > 8500, k]))
            self.assertFalse(list(root.glob("music-sr-*")))


if __name__ == "__main__":
    unittest.main()
