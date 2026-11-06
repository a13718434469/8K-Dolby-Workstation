"""Check the free audio routes against known samples, including the low 8 bits."""
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from hires_audio import (HiresAudioError, choose_free_audio_stream, hires_encode_args,
                         plan_hires_audio, verify_hires_audio)


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def stream(codec, rate=48000, bits=None, channels=2, **details):
    result = {"codec_type": "audio", "codec_name": codec, "channels": channels, "sample_rate": str(rate)}
    if bits:
        result["bits_per_raw_sample"] = str(bits)
    return dict(result, **details)


class FreeAudioSelectionTests(unittest.TestCase):
    def test_no_upsampling_and_no_promotion_of_low_resolution_or_lossy_audio(self):
        for source, action, eligible in [
                (stream("pcm_s24le", 44100), "alac", False),
                (stream("alac", 48000, 16, sample_fmt="s32p"), "alac", False),
                (stream("flac", 48000, 24), "alac", True),
                (stream("aac", 96000, 24), "copy", False),
                (stream("opus", 48000), "aac", False),
                (stream("vorbis", 48000), "aac", False),
                (stream("dts", 96000, 24, profile="DTS-HD MA"), "alac", True),
                (stream("dts", 48000, 24, profile="DTS"), "aac", False),
                (stream("dts", 96000, 24, profile="DTS-HD HRA"), "aac", False),
                (stream("wavpack", 96000, 24), "alac", False),
                (stream("pcm_f32le", 96000), "pcm", False),
                (stream("pcm_s32le", 96000), "pcm", True),
                (stream("flac", 96000, 24, 6, channel_layout="5.1(side)"), "pcm", True)]:
            with self.subTest(source=source):
                plan = plan_hires_audio(source)
                self.assertEqual((plan["action"], plan["eligible"]), (action, eligible))
                self.assertEqual(plan["sample_rate"], int(source["sample_rate"]))
                self.assertFalse(plan["platform_verified"])
        for source in ({}, stream("alac", sample_fmt="s32p"), stream("pcm_s64le"),
                       stream("mlp"), stream("truehd"), stream("dts", profile="DTS-HD MA")):
            with self.assertRaises(HiresAudioError):
                plan_hires_audio(source)

    def test_atmos_copy_only_trusts_bitstream_profile(self):
        joc = plan_hires_audio(stream("eac3", profile="Dolby Digital Plus + Dolby Atmos"))
        self.assertEqual(joc["action"], "copy")
        self.assertTrue(joc["joc"])
        self.assertFalse(plan_hires_audio(stream("eac3", tags={"title": "Dolby Atmos"}))["joc"])

    def test_multi_track_preference_skips_invalid_audio_and_preserves_audio_index(self):
        items = [{"codec_type": "video", "index": 0}, stream("alac"), stream("aac", disposition={"default": 1}),
                 stream("alac", 48000, 16), stream("pcm_s24le", 48000), stream("flac", 96000, 24),
                 stream("eac3", channels=6, profile="Dolby Digital Plus + Dolby Atmos")]
        self.assertIs(choose_free_audio_stream(items)[0], items[-1])
        selected, plan = choose_free_audio_stream(items[:-1])
        self.assertIs(selected, items[-3])
        self.assertEqual(plan["source_audio_ordinal"], 3)
        items[-2]["disposition"] = {"default": 1}
        self.assertIs(choose_free_audio_stream(items[:-1])[0], items[-2])
        with self.assertRaisesRegex(HiresAudioError, "位深"):
            choose_free_audio_stream(items[:2])


@unittest.skipUnless(FFMPEG and FFPROBE, "requires FFmpeg and ffprobe")
class FreeAudioSignalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="free-hires-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    @staticmethod
    def run_tool(args, binary=False):
        result = subprocess.run([str(arg) for arg in args], capture_output=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout if binary else result.stdout.decode("utf-8")

    def probe(self, path):
        return json.loads(self.run_tool([FFPROBE, "-v", "error", "-select_streams", "a:0",
                                        "-show_streams", "-of", "json", path]))["streams"][0]

    def signal_contract(self, name, rate, raw_fmt, codec, frames, target, offset, eligible):
        raw = self.root / (name + ".raw")
        raw.write_bytes(b"".join(frames))
        source = self.root / (name + (".flac" if codec == "flac" else ".wav"))
        self.run_tool([FFMPEG, "-v", "error", "-f", raw_fmt, "-ar", str(rate), "-ac", "2",
                       "-i", raw, "-c:a", codec, source])
        plan = plan_hires_audio(self.probe(source))
        self.assertEqual(plan["eligible"], eligible)
        out = self.root / (name + ".mp4")
        duration, delta = target / rate, offset / rate
        self.run_tool([FFMPEG, "-v", "error", "-xerror", "-i", source, "-map", "0:a:0", "-vn",
                       *hires_encode_args(plan, duration, delta), out])
        final = self.probe(out)
        evidence = verify_hires_audio(FFMPEG, source, out, final, plan, duration, delta, self.run_tool)
        self.assertTrue(evidence["pcm_verified"])
        self.assertEqual(evidence["verified_samples"], target)
        # Independently construct the intended timeline, without FFmpeg's
        # filters or our hash helper, so conversion and verification cannot
        # agree merely by discarding the same samples or precision.
        zero = bytes(len(frames[0]))
        aligned = ([zero] * offset + frames if offset > 0 else frames[-offset:])
        expected = b"".join((aligned + [zero] * target)[:target])
        decoded = self.run_tool([FFMPEG, "-v", "error", "-i", out, "-map", "0:a:0",
                                 "-c:a", "pcm_" + raw_fmt, "-f", raw_fmt, "-"], binary=True)
        self.assertEqual(decoded, expected)
        return source, out, final, plan, duration, delta

    def test_real_flac24_alignment_padding_and_tamper(self):
        frames = [n.to_bytes(3, "little", signed=True) + (-n).to_bytes(3, "little", signed=True)
                  for n in range(300, 11413)]
        for index, offset in enumerate([-1345, 107, 0]):
            with self.subTest(offset=offset):
                self.signal_contract(f"flac-{index}", 96000, "s24le", "flac", frames, 12309, offset, True)
        source, out, final, plan, duration, delta = self.signal_contract(
            "short", 48000, "s24le", "flac", frames, 9817, 0, True)
        changed = self.root / "changed.mp4"
        self.run_tool([FFMPEG, "-v", "error", "-i", out, "-af", "volume=0.5", "-c:a", "alac",
                       "-sample_fmt", "s32p", changed])
        with self.assertRaisesRegex(HiresAudioError, "逐样本"):
            verify_hires_audio(FFMPEG, source, changed, self.probe(changed), plan, duration, delta, self.run_tool)
        with self.assertRaisesRegex(HiresAudioError, "编码、位深"):
            verify_hires_audio(FFMPEG, source, out, dict(final, bits_per_raw_sample="16"), plan,
                               duration, delta, self.run_tool)

    def test_low_resolution_and_high_precision_roundtrip(self):
        for raw_fmt, packing, frames, rate, eligible in [
                ("s16le", "<hh", [(i, -i) for i in range(5000)], 44100, False),
                ("s32le", "<ii", [(i * 99999 + 13, -i * 33333 - 7) for i in range(5000)], 96000, True),
                ("f32le", "<ff", [(i / 3211, -i / 2511) for i in range(5000)], 48000, False),
                ("f64le", "<dd", [(i / 3211, -i / 2511) for i in range(5000)], 96000, False)]:
            with self.subTest(raw_fmt=raw_fmt):
                self.signal_contract(raw_fmt, rate, raw_fmt, "pcm_" + raw_fmt,
                                     [struct.pack(packing, *v) for v in frames], 6007, 7, eligible)

    def test_multitrack_verifies_selected_audio_even_with_a_different_first_stream(self):
        source = self.root / "two-tracks.mkv"
        self.run_tool([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=80:sample_rate=48000:d=0.1",
                       "-f", "lavfi", "-i", "sine=frequency=807:sample_rate=48000:d=0.1",
                       "-map", "0:a", "-map", "1:a", "-c:a:0", "aac", "-c:a:1", "pcm_s24le", source])
        probe = json.loads(self.run_tool([FFPROBE, "-v", "error", "-show_streams", "-of", "json", source]))
        _, plan = choose_free_audio_stream(probe["streams"])
        ordinal = plan["source_audio_ordinal"]
        self.assertEqual(ordinal, 1)
        out = self.root / "selected.mp4"
        self.run_tool([FFMPEG, "-v", "error", "-i", source, "-map", f"0:a:{ordinal}",
                       *hires_encode_args(plan, 0.1), out])
        result = verify_hires_audio(FFMPEG, source, out, self.probe(out), plan, .1, 0, self.run_tool)
        self.assertTrue(result["pcm_verified"])


if __name__ == "__main__":
    unittest.main()
