"""Real decoded-signal contracts for embedded and replacement audio timing."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

import pipeline


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
RATE = 48000
ATMOS_FIXTURE = os.environ.get("DOLBY_ATMOS_FIXTURE", "")
HAS_ATMOS_FIXTURE = bool(ATMOS_FIXTURE and Path(ATMOS_FIXTURE).is_file())


def command(arguments, binary=False):
    result = subprocess.run([str(part) for part in arguments], capture_output=True, timeout=90)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace")[-6000:])
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")


def probe(path):
    return json.loads(command([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path]))


def packet_hashes(path, selector="v:0"):
    data = json.loads(command([FFPROBE, "-v", "error", "-select_streams", selector,
                               "-show_packets", "-show_data_hash", "sha256", "-of", "json", path]))
    return [packet["data_hash"] for packet in data["packets"]]


def decoded_frame_hashes(path):
    data = command([FFMPEG, "-nostdin", "-v", "error", "-xerror", "-i", path,
                    "-map", "0:v:0", "-an", "-f", "framemd5", "-"])
    return [line.rsplit(",", 1)[-1].strip() for line in data.splitlines()
            if line and not line.startswith("#")]


def decode_audio_on_timeline(path):
    # first_pts=0 expands a delayed first packet into actual leading silence.
    # Thus these checks inspect playback samples, not just stream metadata.
    data = command([FFMPEG, "-nostdin", "-v", "error", "-xerror", "-i", path,
                    "-map", "0:a:0", "-vn", "-af", "aresample=48000:first_pts=0",
                    "-ac", "1", "-c:a", "pcm_f32le", "-f", "f32le", "-"], binary=True)
    return np.frombuffer(data, dtype="<f4")


def rms(samples, start, end):
    window = samples[round(start * RATE):round(end * RATE)].astype(np.float64)
    if len(window) != round((end - start) * RATE):
        raise AssertionError("decoded audio does not cover the requested playback interval")
    return float(np.sqrt(np.mean(window * window)))


def peak_frequency(samples, start, end):
    window = samples[round(start * RATE):round(end * RATE)].astype(np.float64)
    return float(np.argmax(np.abs(np.fft.rfft(window))) * RATE / len(window))


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and ffprobe required")
class AudioTimingTests(unittest.TestCase):
    def setUp(self):
        # In local Windows verification TEMP/TMP point to work/runtime-temp;
        # CI and ordinary installations use their normal temporary directory.
        self.temp = tempfile.TemporaryDirectory(prefix="dolby-audio-timing-")
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "delayed-audio.mp4"
        self.base_video = self.root / "video.mp4"
        self.base_audio = self.root / "tone.m4a"

    def tearDown(self):
        self.temp.cleanup()

    def make_source(self):
        command([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=96x64:rate=12:duration=2", "-an", "-c:v", "libx264",
                 "-preset", "ultrafast", "-tune", "zerolatency", "-x264-params", "threads=1:bframes=0",
                 "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
                 "-colorspace", "bt709", self.base_video])
        # ALAC avoids codec priming in the fixture, so the imposed 0.5 s offset
        # is exact. AAC/E-AC-3 delay compensation is exercised by real encodes.
        command([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=997:sample_rate=48000:duration=1.5", "-c:a", "alac", self.base_audio])
        command([FFMPEG, "-nostdin", "-v", "error", "-i", self.base_video,
                 "-itsoffset", "0.5", "-i", self.base_audio, "-map", "0:v:0", "-map", "1:a:0",
                 "-c", "copy", self.source])
        streams = probe(self.source)["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        self.assertAlmostEqual(float(video["start_time"]), 0, delta=1 / RATE)
        self.assertAlmostEqual(float(audio["start_time"]), .5, delta=1 / RATE)
        self.assertEqual(len(decoded_frame_hashes(self.source)), 24)

    def config(self, mode, **changes):
        result = {"video": str(self.source), "output": str(self.root / f"result-{mode}.mp4"),
                  "workspace": str(self.root / f"workspace-{mode}"), "route": "preserve",
                  "delivery": "general", "color_mode": "source", "ai": False, "interp": False,
                  "audio_mode": mode, "auto_clean": True}
        result.update(changes)
        return result

    def assert_video_unchanged(self, output, original_packets, original_frames):
        self.assertEqual(packet_hashes(output), original_packets, "video packet payloads must be preserved")
        self.assertEqual(decoded_frame_hashes(output), original_frames,
                         "all 24 decoded video frames must remain unchanged")

    def test_embedded_half_second_delay_survives_copy_aac_and_eac3(self):
        self.make_source()
        source_audio = decode_audio_on_timeline(self.source)
        self.assertLess(rms(source_audio, 0, .3), 1e-6)
        self.assertGreater(rms(source_audio, .7, 1.0), .04)
        original_packets = packet_hashes(self.source)
        original_frames = decoded_frame_hashes(self.source)
        for mode in ("copy", "aac", "eac3"):
            with self.subTest(mode=mode):
                runner = pipeline.Pipeline(self.config(mode))
                output = runner.run()
                samples = decode_audio_on_timeline(output)
                self.assertAlmostEqual(runner.audio_offset, .5, delta=1 / RATE)
                self.assertLess(rms(samples, 0, .3), 1e-5,
                                "the first 0.3 seconds must remain silent on playback")
                self.assertGreater(rms(samples, .7, 1.0), .04,
                                   "the delayed tone must be audible at 0.7 seconds")
                self.assert_video_unchanged(output, original_packets, original_frames)
                if mode == "copy":
                    self.assertEqual(packet_hashes(output, "a:0"), packet_hashes(self.source, "a:0"))

    def test_external_audio_aligns_its_own_start_to_video_zero(self):
        self.make_source()
        external = self.root / "external-offset.m4a"
        command([FFMPEG, "-nostdin", "-v", "error", "-itsoffset", "0.5", "-i", self.base_audio,
                 "-map", "0:a:0", "-c:a", "copy", external])
        audio = next(stream for stream in probe(external)["streams"] if stream["codec_type"] == "audio")
        self.assertAlmostEqual(float(audio["start_time"]), .5, delta=1 / RATE)
        original_packets = packet_hashes(self.source)
        original_frames = decoded_frame_hashes(self.source)
        for mode in ("copy", "aac", "eac3"):
            with self.subTest(mode=mode):
                runner = pipeline.Pipeline(self.config(mode, audio=str(external)))
                output = runner.run()
                samples = decode_audio_on_timeline(output)
                self.assertEqual(runner.audio_offset, 0)
                self.assertGreater(rms(samples, .05, .3), .04,
                                   "replacement audio must start at zero despite its original container offset")
                self.assertGreater(rms(samples, .7, 1.0), .04)
                self.assert_video_unchanged(output, original_packets, original_frames)
                if mode == "copy":
                    self.assertEqual(packet_hashes(output, "a:0"), packet_hashes(external, "a:0"))

    def test_audio_before_first_video_frame_is_trimmed_without_losing_the_tail(self):
        self.make_source()
        leading_audio = self.root / "audio-leading-half-second.m4a"
        # A distinct 330 Hz pre-roll proves that the discarded half-second is
        # the correct source interval, rather than merely changing timestamps.
        command([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=330:sample_rate=48000:duration=0.5", "-f", "lavfi", "-i",
                 "sine=frequency=997:sample_rate=48000:duration=2", "-filter_complex",
                 "[0:a][1:a]concat=n=2:v=0:a=1", "-c:a", "alac", leading_audio])
        self.source = self.root / "audio-leads-video.mp4"
        command([FFMPEG, "-nostdin", "-v", "error", "-itsoffset", "0.5", "-i", self.base_video,
                 "-i", leading_audio, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", self.source])
        source_streams = probe(self.source)["streams"]
        source_video = next(stream for stream in source_streams if stream["codec_type"] == "video")
        source_audio = next(stream for stream in source_streams if stream["codec_type"] == "audio")
        self.assertAlmostEqual(float(source_video["start_time"]), .5, delta=1 / RATE)
        self.assertAlmostEqual(float(source_audio["start_time"]), 0, delta=1 / RATE)
        self.assertAlmostEqual(float(source_video["duration"]), 2, delta=1 / RATE)
        original_packets = packet_hashes(self.source)
        original_frames = decoded_frame_hashes(self.source)
        for mode in ("copy", "aac", "eac3"):
            with self.subTest(mode=mode):
                runner = pipeline.Pipeline(self.config(mode))
                output = runner.run()
                final = probe(output)
                video = next(stream for stream in final["streams"] if stream["codec_type"] == "video")
                self.assertAlmostEqual(runner.audio_offset, -.5, delta=1 / RATE)
                self.assertAlmostEqual(float(video["start_time"]), 0, delta=1 / RATE)
                self.assertAlmostEqual(float(video["duration"]), 2, delta=1 / RATE)
                self.assertAlmostEqual(float(final["format"]["duration"]), 2, delta=.04)
                samples = decode_audio_on_timeline(output)
                self.assertGreater(rms(samples, .05, .3), .04)
                self.assertAlmostEqual(peak_frequency(samples, .05, .3), 997, delta=5,
                                       msg="the 330 Hz pre-roll must not leak into the video timeline")
                self.assertGreater(rms(samples, 1.7, 1.98), .04,
                                   "the end of the original program audio must not be cut off")
                self.assert_video_unchanged(output, original_packets, original_frames)
                if mode == "copy":
                    original_audio_packets = packet_hashes(self.source, "a:0")
                    copied_packets = packet_hashes(output, "a:0")
                    first = original_audio_packets.index(copied_packets[0])
                    self.assertEqual(copied_packets, original_audio_packets[first:],
                                     "only packets entirely before the visible program may be removed")
                    reference = decode_audio_on_timeline(leading_audio)[round(.5 * RATE):]
                    self.assertEqual(len(samples), 2 * RATE)
                    np.testing.assert_array_equal(samples, reference,
                                                  "ALAC copy must retain every audible sample exactly")

    @unittest.skipUnless(HAS_ATMOS_FIXTURE, "external genuine JOC fixture required; never synthesize Atmos metadata")
    def test_real_joc_negative_offset_preserves_selected_encoded_packets(self):
        self.make_source()
        fixture_audio = self.root / "genuine-joc.mp4"
        command([FFMPEG, "-nostdin", "-v", "error", "-i", ATMOS_FIXTURE,
                 "-map", "0:a:0", "-c:a", "copy", "-t", "2.5", fixture_audio])
        track = next(stream for stream in probe(fixture_audio)["streams"] if stream["codec_type"] == "audio")
        self.assertEqual(track["codec_name"], "eac3")
        self.assertIn("Atmos", track.get("profile", ""), "fixture must contain real JOC bitstream evidence")
        self.source = self.root / "joc-leads-video.mp4"
        command([FFMPEG, "-nostdin", "-v", "error", "-itsoffset", "0.5", "-i", self.base_video,
                 "-i", fixture_audio, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", self.source])
        runner = pipeline.Pipeline(self.config("copy", require_atmos=True, auto_clean=False))
        output = runner.run()
        final = probe(output)
        video = next(stream for stream in final["streams"] if stream["codec_type"] == "video")
        audio = next(stream for stream in final["streams"] if stream["codec_type"] == "audio")
        self.assertAlmostEqual(float(video["start_time"]), 0, delta=1 / RATE)
        self.assertAlmostEqual(float(video["duration"]), 2, delta=1 / RATE)
        self.assertAlmostEqual(float(audio["duration"]), 2, delta=.032)
        self.assertIn("Atmos", audio.get("profile", ""))
        self.assert_video_unchanged(output, packet_hashes(self.source), decoded_frame_hashes(self.source))
        selected = packet_hashes(runner.job_dir / "audio.mp4", "a:0")
        self.assertEqual(packet_hashes(output, "a:0"), selected,
                         "final MP4 must retain every selected JOC packet byte-for-byte")
        original = packet_hashes(self.source, "a:0")
        self.assertTrue(any(original[index:index + len(selected)] == selected for index in range(len(original))),
                        "selected JOC packets must be a contiguous subset of the original bitstream")
        self.assertTrue(runner.manifest["atmos_audio"]["payload_preserved"])


if __name__ == "__main__":
    unittest.main()
