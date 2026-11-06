"""Exercise genuine alternate-track delivery. No live commercial encoder or cloud."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from consumer_audio import select_tracks
from pipeline import Pipeline


FF = shutil.which("ffmpeg")
FP = shutil.which("ffprobe")
JOC = os.getenv("DOLBY_ATMOS_FIXTURE", "")


class TrackSelection(unittest.TestCase):
    def test_language_and_explicit_replacement(self):
        atmos = {"codec_name": "eac3", "codec_type": "audio", "sample_rate": "48000", "channels": 6,
                 "channel_layout": "5.1(side)", "profile": "Dolby Digital Plus + Dolby Atmos",
                 "bit_rate": "640000", "tags": {"language": "eng"}}
        pcm = {"codec_type": "audio", "codec_name": "pcm_s24le", "sample_rate": "48000", "channels": 2,
               "bits_per_sample": 24, "tags": {"language": "zho"}}
        aac = dict(pcm, codec_name="aac", bits_per_sample=0)
        self.assertEqual(len(select_tracks([atmos], [pcm])), 1)
        self.assertEqual([p["origin"] for p in select_tracks([atmos], [aac])], ["external"])
        matching = dict(pcm, tags={"language": "eng"})
        self.assertEqual([p["origin"] for p in select_tracks([atmos], [matching])], ["external", "video"])
        self.assertEqual([p["plan"]["source_audio_ordinal"] for p in select_tracks([atmos, matching])], [1, 0])
        self.assertFalse(select_tracks([dict(atmos, profile="Dolby Digital Plus", tags={"title":"Atmos"})])[0]["plan"]["joc"])


def run(*args):
    return subprocess.run([str(a) for a in args], check=True, capture_output=True).stdout


@unittest.skipUnless(FF and FP, "requires FFmpeg and ffprobe")
class DeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.video = self.root / "silent.mp4"
        self.master = self.root / "master.wav"
        run(FF, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=12:duration=1",
            "-c:v", "libx264", self.video)
        run(FF, "-v", "error", "-f", "lavfi", "-i",
            "aevalsrc=0.31*sin(2*PI*500*t)|0.23*sin(2*PI*220*t):s=96000:d=1",
            "-c:a", "pcm_s24le", self.master)

    def export(self, source, audio=None):
        dest = self.root / "result.mp4"
        cfg = dict(video=str(source), audio=str(audio or ""), output=str(dest), audio_mode="hires",
                   route="preserve", workspace=str(self.root / "work"), bili_one_click=True)
        Pipeline(cfg).run()
        return json.loads(Path(str(dest)+".quality.json").read_text(encoding="utf-8"))["export"]["consumer_audio"]

    def test_silent_picture_and_unconfigured_private_machine(self):
        r = self.export(self.video)
        self.assertEqual((r["audio_tracks"], r["atmos"], r["hires_delivery"]), (0, False, False))

    def test_hires_without_atmos_remains_deliverable(self):
        with patch("atmos_encoding.backend_status", return_value={"available": False}):
            r = self.export(self.video, self.master)
        self.assertTrue(r["native_hires"])
        self.assertFalse(r["atmos"] or r["generated_atmos"])

    def combined(self):
        target = self.root / "video-joc.mp4"
        run(FF, "-v", "error", "-i", self.video, "-i", JOC, "-map", "0:v", "-map", "1:a:0",
            "-c", "copy", "-t", "1", target)
        return target

    @unittest.skipUnless(JOC and Path(JOC).is_file(), "local genuine JOC fixture")
    def test_joc_and_explicit_lossless_master_stay_individually_intact(self):
        source = self.combined()
        r = self.export(source, self.master)
        self.assertTrue(r["native_hires"] and r["atmos"])
        self.assertFalse(r["generated_atmos"] or r["platform_verified"] or r["mastered"])
        self.assertEqual([t["codec"] for t in r["tracks"]], ["alac", "eac3"])
        def hash_audio(path, ordinal, codec):
            return run(FF, "-v", "error", "-i", path, "-map", f"0:a:{ordinal}", "-c:a", codec,
                       "-f", "hash", "-hash", "sha256", "-")
        dest = self.root / "result.mp4"
        self.assertEqual(hash_audio(source, 0, "copy"), hash_audio(dest, 1, "copy"))
        self.assertEqual(hash_audio(self.master, 0, "pcm_s24le"), hash_audio(dest, 0, "pcm_s24le"))

    @unittest.skipUnless(JOC and Path(JOC).is_file(), "local genuine JOC fixture")
    def test_joc_only_produces_verified_stereo_derivative_separate_from_original(self):
        r = self.export(self.combined())
        self.assertTrue(r["atmos"] and r["hires_delivery"] and r["mastered"])
        self.assertFalse(r["native_hires"] or r["generated_atmos"])
        self.assertEqual(r["master_kind"], "atmos_stereo_downmix")
        self.assertEqual(r["tracks"][0]["channels"], 2)
        self.assertEqual(len(r["tracks"][0]["mix_pcm_sha256"]), 64)

    @unittest.skipUnless(JOC and Path(JOC).is_file(), "local genuine JOC fixture")
    def test_existing_multitrack_keeps_delayed_atmos_and_synchronized_main_track(self):
        target = self.root / "two-tracks.mp4"
        run(FF, "-v", "error", "-i", self.video, "-itsoffset", "0.25", "-i", JOC, "-i", self.master,
            "-map", "0:v", "-map", "1:a:0", "-map", "2:a:0", "-c", "copy", "-c:a:1", "alac",
            "-sample_fmt:a:1", "s32p", "-t", "1", target)
        r = self.export(target)
        self.assertEqual([track["origin"] for track in r["tracks"]], ["video", "video"])
        self.assertEqual([track["source_audio_ordinal"] for track in r["tracks"]], [1, 0])
        self.assertTrue(r["native_hires"] and r["atmos"])
        self.assertAlmostEqual(r["tracks"][0]["start_seconds"], 0, places=4)
        self.assertAlmostEqual(r["tracks"][1]["start_seconds"], 0.25, delta=0.033)

    @unittest.skipUnless(JOC and Path(JOC).is_file(), "local genuine JOC fixture")
    def test_configured_local_encoder_path_with_preencoded_fixture(self):
        # Tests the adapter and checks on a preencoded fixture only.
        with patch("atmos_encoding.backend_status", return_value={"available": True}), \
             patch("atmos_encoding.backend_identity", return_value={"backend": "fixture"}), \
             patch("atmos_encoding.encode_dee", return_value=(JOC, {"backend": "fixture"})), \
             patch("atmos_cloud.encode_atmos_cloud", side_effect=AssertionError("cloud call")):
            r = self.export(self.video, self.master)
        self.assertTrue(r["native_hires"] and r["atmos"] and r["generated_atmos"])

    def test_encoder_failure_falls_back_without_a_false_audio_badge(self):
        from atmos_encoding import AtmosError
        with patch("atmos_encoding.backend_status", return_value={"available": True}), \
             patch("atmos_encoding.backend_identity", return_value={"backend": "fixture"}), \
             patch("atmos_encoding.encode_dee", side_effect=AtmosError("license missing")):
            r = self.export(self.video, self.master)
        self.assertTrue(r["native_hires"])
        self.assertFalse(r["atmos"] or r["generated_atmos"])
        self.assertIn("未通过", r["atmos_note"])
