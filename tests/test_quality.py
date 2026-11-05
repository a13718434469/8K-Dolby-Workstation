"""Independent media-inspection and conservative platform-readiness regressions."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import quality


def fixture():
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2.002", "bit_rate": "32000000"},
        "streams": [{
            "index": 0, "codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
            "avg_frame_rate": "30000/1001", "r_frame_rate": "30000/1001", "duration": "2.002",
            "nb_frames": "60", "pix_fmt": "yuv420p10le", "color_transfer": "arib-std-b67",
            "color_primaries": "bt2020", "color_space": "bt2020nc", "color_range": "tv",
            "bit_rate": "31000000", "side_data_list": [{
                "side_data_type": "DOVI configuration record", "dv_profile": 8,
                "dv_bl_signal_compatibility_id": 4, "rpu_present_flag": 1,
                "bl_present_flag": 1, "el_present_flag": 0,
            }],
        }, {
            "index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6,
            "profile": "Dolby Digital Plus + Dolby Atmos", "sample_rate": "48000", "duration": "2.002",
            "bit_rate": "768000",
        }],
    }


class ProbeNormalizationTests(unittest.TestCase):
    def test_video_clock_duration_precedes_longer_container_duration(self):
        data = fixture()
        video = data["streams"][0]
        video.pop("duration")
        video.update(duration_ts=1001, time_base="1/1000")
        data["format"]["duration"] = "40"
        info = quality.read_probe(data)
        self.assertAlmostEqual(info["duration"], 1.001)
        self.assertTrue(info["duration_exact_from_stream"])
        self.assertTrue(info["duration_exact"])
        self.assertEqual(info["duration_source"], "stream_time_base")

    def test_container_duration_fallback_is_identified_as_an_estimate(self):
        data = fixture()
        data["streams"][0].pop("duration")
        info = quality.read_probe(data)
        self.assertFalse(info["duration_exact_from_stream"])
        self.assertFalse(info["duration_exact"])
        self.assertEqual(info["duration_source"], "container_estimate")

    def test_preserves_exact_fractional_rate_and_dolby_flags(self):
        info = quality.read_probe(fixture(), "movie with spaces.mp4")
        self.assertEqual(Fraction(info["fps_fraction"]), Fraction(30000, 1001))
        self.assertEqual(info["total_frames"], 60)
        self.assertTrue(info["frame_count_exact"])
        self.assertEqual(info["bit_depth"], 10)
        self.assertEqual(info["dv_profile"], 8)
        self.assertEqual(info["dv_compatibility"], 4)
        self.assertIs(info["dv_rpu_present"], True)
        self.assertIs(info["dv_bl_present"], True)
        self.assertEqual(info["audio_channels"], 6)
        self.assertEqual(info["atmos"], "confirmed")

    def test_missing_sample_count_is_an_estimate_not_verified_frames(self):
        data = fixture()
        data["streams"][0]["nb_frames"] = "N/A"
        data["streams"][0]["nb_read_frames"] = "N/A"
        info = quality.read_probe(data)
        self.assertEqual(info["total_frames"], 60)
        self.assertFalse(info["frame_count_exact"])
        checks = quality.validate_expectations(info, {"total_frames": 60})
        self.assertEqual(checks[0]["status"], "fail")

    def test_decode_count_overrides_container_frame_count(self):
        data = fixture()
        data["streams"][0]["nb_read_frames"] = "59"
        info = quality.read_probe(data)
        self.assertEqual(info["total_frames"], 59)
        self.assertEqual(quality.validate_expectations(info, {"total_frames": 60})[0]["status"], "fail")

    def test_attached_cover_art_is_not_selected_as_main_video(self):
        data = fixture()
        cover = {"index": 0, "codec_type": "video", "codec_name": "mjpeg",
                 "width": 400, "height": 400, "disposition": {"attached_pic": 1}}
        data["streams"].insert(0, cover)
        self.assertEqual(quality.read_probe(data)["codec_name"], "hevc")

    def test_invalid_average_rate_falls_back_to_rational_rate(self):
        data = fixture()
        data["streams"][0]["avg_frame_rate"] = "0/0"
        self.assertEqual(quality.read_probe(data)["fps_fraction"], "30000/1001")

    def test_phone_display_rotation_keeps_coded_dimensions_and_reports_portrait(self):
        data = fixture()
        data["streams"][0]["side_data_list"].append({"side_data_type": "Display Matrix", "rotation": -90})
        info = quality.read_probe(data)
        self.assertEqual((info["width"], info["height"]), (3840, 2160))
        self.assertEqual(info["rotation"], 270)
        self.assertEqual((info["display_width"], info["display_height"]), (2160, 3840))

    def test_display_matrix_rotation_takes_precedence_over_legacy_rotate_tag(self):
        data = fixture()
        data["streams"][0]["tags"] = {"rotate": "180"}
        info = quality.read_probe(data)
        self.assertEqual(info["rotation"], 180)
        self.assertEqual((info["display_width"], info["display_height"]), (3840, 2160))
        data["streams"][0]["side_data_list"].append({"side_data_type": "Display Matrix", "rotation": 90})
        info = quality.read_probe(data)
        self.assertEqual(info["rotation"], 90)
        self.assertEqual((info["display_width"], info["display_height"]), (2160, 3840))

    def test_invalid_video_metadata_fails_instead_of_inventing_defaults(self):
        for key, value in (("width", 0), ("duration", "NaN"), ("avg_frame_rate", "0/0")):
            with self.subTest(key=key):
                data = fixture()
                data["streams"][0][key] = value
                if key == "duration":
                    data["format"]["duration"] = "NaN"
                if key == "avg_frame_rate":
                    data["streams"][0]["r_frame_rate"] = "0/0"
                with self.assertRaises(quality.QualityError):
                    quality.read_probe(data)

    def test_eac3_alone_and_editable_atmos_title_do_not_confirm_atmos(self):
        data = fixture()
        data["streams"][1].pop("profile")
        data["streams"][1]["tags"] = {"title": "Dolby Atmos JOC", "comment": "Certified Atmos"}
        info = quality.read_probe(data)
        self.assertEqual(info["atmos"], "unknown")
        required = quality.assess_bilibili(info, require_atmos=True)
        self.assertEqual(required["status"], "fail")
        self.assertEqual(next(c for c in required["checks"] if c["id"] == "atmos")["status"], "fail")

    def test_mediainfo_only_uses_parsed_codec_feature_fields(self):
        data = fixture()
        data["streams"][1].pop("profile")
        info = quality.read_probe(data)
        quality._apply_mediainfo(info, {"media": {"track": [{"@type": "Audio", "Title": "Dolby Atmos"}]}})
        self.assertEqual(info["atmos"], "unknown")
        quality._apply_mediainfo(info, {"media": {"track": [{"@type": "Audio", "Format_AdditionalFeatures": "JOC"}]}})
        self.assertEqual(info["atmos"], "confirmed")
        self.assertIn("MediaInfo", info["atmos_evidence"])

    def test_silent_video_is_valid_media_but_not_double_label_material(self):
        data = fixture()
        data["streams"] = data["streams"][:1]
        info = quality.read_probe(data)
        self.assertEqual(info["atmos"], "absent")
        self.assertEqual(info["audio_codec"], "none")
        self.assertEqual(quality.assess_bilibili(info)["status"], "warning")
        self.assertEqual(quality.assess_bilibili(info, require_atmos=True)["status"], "fail")

    def test_audio_only_probe_does_not_make_the_video_reader_accept_audio(self):
        data = fixture()
        data["streams"] = data["streams"][1:]
        info = quality.read_audio_probe(data, "source.eac3")
        self.assertEqual(info["audio_codec"], "eac3")
        self.assertEqual(info["atmos"], "confirmed")
        with self.assertRaises(quality.QualityError):
            quality.read_probe(data)


class ReadinessTests(unittest.TestCase):
    def test_compliant_format_never_implies_platform_approval(self):
        report = quality.assess_bilibili(quality.read_probe(fixture()), require_atmos=True)
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["platform_verified"])
        self.assertIn("平台识别尚未验收", report["summary"])
        self.assertTrue(any("2021" in item["published_date"] for item in report["sources"]))

    def test_pq_profile81_is_not_reported_as_bili_profile84(self):
        data = fixture()
        data["streams"][0]["color_transfer"] = "smpte2084"
        data["streams"][0]["side_data_list"][0]["dv_bl_signal_compatibility_id"] = 1
        report = quality.assess_bilibili(quality.read_probe(data))
        failed = {c["id"] for c in report["checks"] if c["status"] == "fail"}
        self.assertTrue({"transfer", "dolby_profile"}.issubset(failed))

    def test_profile_flag_without_rpu_or_base_layer_declaration_fails(self):
        for key in ("rpu_present_flag", "bl_present_flag"):
            with self.subTest(key=key):
                data = fixture()
                data["streams"][0]["side_data_list"][0][key] = 0
                report = quality.assess_bilibili(quality.read_probe(data))
                self.assertEqual(report["status"], "fail")

    def test_profile84_with_enhancement_layer_or_422_output_fails(self):
        data = fixture()
        data["streams"][0]["pix_fmt"] = "yuv422p10le"
        report = quality.assess_bilibili(quality.read_probe(data))
        self.assertEqual(next(c for c in report["checks"] if c["id"] == "pixel_format")["status"], "fail")
        data = fixture()
        data["streams"][0]["side_data_list"][0]["el_present_flag"] = 1
        report = quality.assess_bilibili(quality.read_probe(data))
        self.assertEqual(next(c for c in report["checks"] if c["id"] == "dolby_single_layer")["status"], "fail")

    def test_old_resolution_fps_and_bitrate_recommendations_are_warnings(self):
        data = fixture()
        data["streams"][0].update(width=7680, height=4320, avg_frame_rate="120/1", bit_rate="10000000")
        report = quality.assess_bilibili(quality.read_probe(data))
        self.assertEqual(report["status"], "warning")
        warning_ids = {c["id"] for c in report["checks"] if c["status"] == "warning"}
        self.assertTrue({"resolution_guidance", "fps_guidance", "bitrate_guidance"}.issubset(warning_ids))

    def test_portrait_4k_is_within_long_edge_short_edge_guidance(self):
        data = fixture()
        data["streams"][0].update(width=2160, height=3840)
        report = quality.assess_bilibili(quality.read_probe(data))
        self.assertEqual(next(c for c in report["checks"] if c["id"] == "resolution_guidance")["status"], "pass")

    def test_truehd_atmos_is_identified_but_requires_audio_handoff_for_bili(self):
        data = fixture()
        data["streams"][1].update(codec_name="truehd", profile="Dolby TrueHD + Dolby Atmos")
        info = quality.read_probe(data)
        self.assertEqual(info["atmos"], "confirmed")
        self.assertEqual(quality.assess_bilibili(info, require_atmos=True)["status"], "fail")

    def test_expected_cadence_uses_exact_fraction_not_rounded_float(self):
        info = quality.read_probe(fixture())
        checks = quality.validate_expectations(info, {"fps_fraction": "30000/1001", "duration": 2.002})
        self.assertTrue(all(c["status"] == "pass" for c in checks))
        mismatch = quality.validate_expectations(info, {"fps": 29.97})
        self.assertEqual(mismatch[0]["status"], "fail")

    def test_report_is_atomic_and_refuses_a_false_platform_verified_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp).resolve() / "result with spaces.mp4"
            target = quality.write_quality_report(output, {"platform_verified": True, "status": "pass"})
            self.assertEqual(target.name, "result with spaces.mp4.quality.json")
            original = target.read_bytes()
            self.assertFalse(json.loads(original)["platform_verified"])
            with self.assertRaises(ValueError):
                quality.write_quality_report(output, {"invalid": float("nan")})
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])


class AtmosDeliveryTests(unittest.TestCase):
    def audio_info(self, **overrides):
        data = fixture()
        data["streams"] = data["streams"][1:]
        data["streams"][0].update(overrides)
        return quality.read_audio_probe(data)

    def test_bitrate_is_from_first_audio_track_not_video_or_later_tracks(self):
        data = fixture()
        data["streams"].append({"codec_type": "audio", "codec_name": "eac3", "bit_rate": "1024000"})
        self.assertEqual(quality.read_probe(data)["audio_bit_rate"], 768000)
        del data["streams"][1]["bit_rate"]
        self.assertEqual(quality.read_probe(data)["audio_bit_rate"], 0)
        self.assertEqual(quality.read_audio_probe(data)["audio_bit_rate"], 0)
        report = quality.assess_bilibili(quality.read_probe(data), require_atmos=True)
        self.assertEqual(next(c for c in report["checks"] if c["id"] == "audio_bitrate")["status"], "fail")

    def test_container_bitrate_fallback_requires_one_audio_stream_and_no_video(self):
        data = fixture()
        data["streams"] = data["streams"][1:]
        data["streams"][0].pop("bit_rate")
        data["format"]["bit_rate"] = "384000"
        self.assertEqual(quality.read_audio_probe(data)["audio_bit_rate"], 384000)
        first = copy.deepcopy(data["streams"][0])
        for other in (first, {"codec_type": "video", "disposition": {"attached_pic": 1}}, {}):
            with self.subTest(other=other):
                mixed = copy.deepcopy(data)
                mixed["streams"].append(other)
                self.assertEqual(quality.read_audio_probe(mixed)["audio_bit_rate"], 0)

    def test_bili_atmos_requirements_are_hard_only_when_required(self):
        for changes, failed_id in (({"channels": 2}, "audio_channels"),
                                   ({"channels": 10}, "audio_channels"),
                                   ({"bit_rate": "319999"}, "audio_bitrate"),
                                   ({"bit_rate": "N/A"}, "audio_bitrate"),
                                   ({"profile": "E-AC-3"}, "atmos"),
                                   ({"codec_name": "truehd"}, "atmos")):
            with self.subTest(changes=changes):
                data = fixture()
                data["streams"][1].update(changes)
                info = quality.read_probe(data)
                required = quality.assess_bilibili(info, require_atmos=True)
                self.assertEqual(next(c for c in required["checks"] if c["id"] == failed_id)["status"], "fail")
                optional = quality.assess_bilibili(info, require_atmos=False)
                self.assertEqual(optional["status"], "warning")
                self.assertFalse(any(c["status"] == "fail" for c in optional["checks"]),
                                 "optional audio must not disable valid Dolby Vision preservation")

    def test_48khz_is_recommended_for_upload_but_required_for_generated_audio(self):
        data = fixture()
        data["streams"][1]["sample_rate"] = "44100"
        info = quality.read_probe(data)
        upload = quality.assess_bilibili(info, require_atmos=True)
        self.assertEqual(upload["status"], "warning")
        self.assertEqual(quality.validate_atmos_audio(info)["status"], "fail")

    def test_generated_audio_accepts_six_or_eight_channels_and_320kbps_boundary(self):
        for channels in (6, 8):
            with self.subTest(channels=channels):
                report = quality.validate_atmos_audio(self.audio_info(channels=channels, bit_rate="320000"), 2.002)
                self.assertEqual(report["status"], "pass")
                self.assertTrue(all(c["status"] == "pass" for c in report["checks"]))
                self.assertIn("平台识别尚未验收", report["summary"])

    def test_existing_44100_joc_can_be_preserved_with_sample_rate_warning(self):
        report = quality.validate_atmos_audio(self.audio_info(sample_rate="44100"), strict_sample_rate=False)
        self.assertEqual(report["status"], "warning")
        self.assertEqual({c["id"] for c in report["checks"] if c["status"] != "pass"}, {"audio_sample_rate"})

    def test_preservation_sample_rate_exception_does_not_waive_other_requirements(self):
        for change in ({"audio_codec": "aac"}, {"atmos": "unknown"}, {"audio_channels": 2},
                       {"audio_bit_rate": 319999}, {"audio_duration": 0.5}):
            with self.subTest(change=change):
                info = self.audio_info(sample_rate="44100")
                info.update(change)
                report = quality.validate_atmos_audio(info, 2.002, strict_sample_rate=False)
                self.assertEqual(report["status"], "fail")

    def test_generated_audio_rejects_fake_titles_plain_eac3_and_non_joc_codecs(self):
        for changes in ({"profile": "E-AC-3", "tags": {"title": "Dolby Atmos JOC"}},
                        {"profile": "unknown"}, {"codec_name": "truehd"},
                        {"codec_name": "aac"}):
            with self.subTest(changes=changes):
                report = quality.validate_atmos_audio(self.audio_info(**changes), 2.002)
                self.assertEqual(next(c for c in report["checks"] if c["id"] == "atmos")["status"], "fail")

    def test_generated_audio_rejects_wrong_channels_bitrate_and_sample_rate(self):
        for key, values in (("audio_channels", (0, 2, 10)),
                            ("audio_bit_rate", (0, 319999, None, "N/A")),
                            ("audio_sample_rate", (0, 44100, 96000))):
            for value in values:
                with self.subTest(key=key, value=value):
                    info = self.audio_info()
                    info[key] = value
                    self.assertEqual(quality.validate_atmos_audio(info, 2.002)["status"], "fail")

    def test_generated_audio_duration_allows_codec_padding_but_not_sync_errors(self):
        for duration, passed in ((1.936, True), (2.064, True), (2.08, True),
                                 (1.92, True), (2.08001, False), (1.91999, False),
                                 (0, False), (float("nan"), False), (float("inf"), False)):
            with self.subTest(duration=duration):
                info = self.audio_info()
                info["audio_duration"] = duration
                report = quality.validate_atmos_audio(info, expected_duration=2.0)
                self.assertEqual(report["status"], "pass" if passed else "fail")
        for expected in (0, -1, float("nan"), float("inf")):
            with self.subTest(expected=expected):
                self.assertEqual(quality.validate_atmos_audio(self.audio_info(), expected)["status"], "fail")
        info = self.audio_info()
        del info["audio_duration"]
        info["duration"] = 2.002
        self.assertEqual(quality.validate_atmos_audio(info)["status"], "fail", "video duration cannot prove audio duration")

    def test_audio_guidance_records_update_and_retrieval_dates_separately(self):
        audio_source = next(item for item in quality.SOURCES if "3792" in item["title"])
        self.assertEqual(audio_source["updated_date"], "2024-09-25")
        self.assertEqual(audio_source["retrieved_date"], "2026-11-06")
        self.assertIn("2021", quality.GUIDANCE_NOTE)
        self.assertIn("2024-09-25", quality.GUIDANCE_NOTE)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class ActualMediaTests(unittest.TestCase):
    def test_packet_duration_excludes_video_start_offset_and_longer_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            for audio_duration in (0.7, 2.0):
                with self.subTest(audio_duration=audio_duration):
                    path = Path(temp) / f"nonzero-video-start-{audio_duration}.mkv"
                    command = [shutil.which("ffmpeg"), "-v", "error", "-itsoffset", "0.2", "-f", "lavfi", "-i",
                               "color=size=64x48:rate=10:duration=0.5", "-f", "lavfi", "-i",
                               f"sine=sample_rate=48000:duration={audio_duration}", "-map", "0:v:0", "-map", "1:a:0",
                               "-c:v", "libx264", "-bf", "2", "-c:a", "pcm_s16le", str(path)]
                    encoded = subprocess.run(command, capture_output=True, text=True, timeout=30)
                    self.assertEqual(encoded.returncode, 0, encoded.stderr)
                    info = quality.inspect_media(path, mediainfo="", count_frames=True)
                    self.assertAlmostEqual(float(info["stream"]["start_time"]), 0.2)
                    self.assertGreater(float(info["format"]["duration"]), 0.5)
                    self.assertAlmostEqual(info["duration"], 0.5, places=6)
                    self.assertFalse(info["duration_exact_from_stream"])
                    self.assertTrue(info["duration_exact"])
                    self.assertEqual(info["duration_source"], "packet_timestamps")
                    self.assertEqual(info["total_frames"], 5)
                    self.assertTrue(info["frame_count_exact"])

    def test_vfr_packet_span_is_independent_of_frame_count_and_nominal_fps(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "VFR with B frames and longer audio.mkv"
            command = [shutil.which("ffmpeg"), "-v", "error", "-itsoffset", "0.4", "-f", "lavfi", "-i",
                       "testsrc2=size=64x48:rate=10:duration=1.6", "-f", "lavfi", "-i",
                       "sine=sample_rate=48000:duration=3", "-map", "0:v:0", "-map", "1:a:0",
                       "-vf", r"select=eq(n\,0)+eq(n\,1)+eq(n\,4)+eq(n\,10)+eq(n\,15)",
                       "-fps_mode", "vfr", "-c:v", "libx264", "-bf", "2", "-c:a", "pcm_s16le", str(path)]
            encoded = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(encoded.returncode, 0, encoded.stderr)
            info = quality.inspect_media(path, mediainfo="", count_frames=True)
            self.assertAlmostEqual(info["duration"], 1.6, places=6)
            self.assertAlmostEqual(info["video_timeline"]["start"], 0.4, places=6)
            self.assertAlmostEqual(info["video_timeline"]["end"], 2.0, places=6)
            self.assertEqual(info["total_frames"], 5)
            self.assertTrue(info["frame_count_exact"])
            self.assertTrue(info["duration_exact"])
            self.assertNotAlmostEqual(info["duration"], info["total_frames"] / info["fps"])

    def test_real_hlg_matroska_with_b_frames_reports_decoded_vui_colors(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "HLG with delayed frames.mkv"
            result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                                     "testsrc2=size=192x128:rate=12", "-frames:v", "12", "-c:v", "libx265",
                                     "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
                                     "-x265-params", "lossless=1:frame-threads=1:bframes=3:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
                                     "-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
                                     "-colorspace", "bt2020nc", "-color_range", "tv", str(path)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            info = quality.inspect_media(path, mediainfo="")
            self.assertEqual(info["color_transfer"], "arib-std-b67")
            self.assertEqual(info["color_primaries"], "bt2020")
            self.assertEqual(info["stream"]["color_transfer"], "arib-std-b67")
            self.assertEqual(info["stream"]["color_primaries"], "bt2020")
            self.assertEqual(info["width"], 192)
            self.assertEqual(info["height"], 128)

    def test_actual_external_eac3_audio_is_inspected_without_claiming_atmos(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "external audio.eac3"
            result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                                     "sine=frequency=440:sample_rate=48000:duration=0.2", "-c:a", "eac3",
                                     str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            info = quality.inspect_audio(path)
            self.assertEqual(info["audio_codec"], "eac3")
            self.assertEqual(info["audio_sample_rate"], 48000)
            self.assertEqual(info["atmos"], "unknown")

    def test_real_sdr_file_inspection_reports_actual_media_and_bili_mismatches(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "real sample.mp4"
            result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                                     "testsrc2=size=48x32:rate=30000/1001", "-frames:v", "3",
                                     "-c:v", "libx264", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            info = quality.inspect_media(path, count_frames=True)
            self.assertEqual(info["codec_name"], "h264")
            self.assertEqual(info["total_frames"], 3)
            self.assertTrue(info["frame_count_exact"])
            self.assertEqual(info["fps_fraction"], "30000/1001")
            self.assertEqual(info["bit_depth"], 8)
            self.assertEqual(info["atmos"], "absent")
            report = quality.assess_bilibili(info)
            self.assertEqual(report["status"], "fail")
            self.assertFalse(report["platform_verified"])

    def test_optional_mediainfo_failure_keeps_ffprobe_results_and_records_note(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "sample.mp4"
            path.write_bytes(b"synthetic test placeholder")
            with patch.object(quality, "_run_json", side_effect=[fixture(), quality.QualityError("optional tool failed")]):
                info = quality.inspect_media(path, ffprobe="ffprobe", mediainfo="mediainfo")
            self.assertEqual(info["codec_name"], "hevc")
            self.assertEqual(info["inspection_notes"], ["optional tool failed"])


class InspectionControlTests(unittest.TestCase):
    def packet_scan(self, content):
        return quality._scan_packet_duration(
            [sys.executable, "-c", "import sys; sys.stdout.write(" + repr(content) + ")"],
            fallback_frame_duration=0.04)

    def test_packet_duration_handles_reordered_pts_and_exposes_estimated_last_duration(self):
        exact = self.packet_scan("pts_time=1.0|duration_time=0.1\npts_time=1.4|duration_time=0.2\npts_time=1.1|duration_time=0.3\n")
        self.assertAlmostEqual(exact["duration"], 0.6)
        self.assertTrue(exact["exact"])
        estimated = self.packet_scan("pts_time=1.0|duration_time=0.1\npts_time=1.4|duration_time=N/A\npts_time=1.1|duration_time=0.3\n")
        self.assertAlmostEqual(estimated["duration"], 0.7)
        self.assertFalse(estimated["exact"])
        self.assertTrue(estimated["last_duration_estimated"])

    def test_packet_duration_missing_pts_cannot_claim_exact_timing(self):
        result = self.packet_scan("pts_time=0.0|duration_time=0.1\npts_time=N/A|duration_time=0.1\n")
        self.assertFalse(result["exact"])
        self.assertEqual(result["missing_pts"], 1)
        with self.assertRaisesRegex(quality.QualityError, "PTS"):
            self.packet_scan("pts_time=N/A|duration_time=0.1\n")

    def test_packet_duration_updates_only_estimated_frame_count_and_targets_video_index(self):
        data = fixture()
        video = data["streams"][0]
        video.pop("duration")
        video.pop("nb_frames")
        video["index"] = 3
        info = quality.read_probe(data)
        result = {"duration": 0.5, "start": 0.2, "end": 0.7, "exact": True,
                  "packets": 5, "missing_pts": 0, "last_duration_estimated": False}
        with patch.object(quality, "_scan_packet_duration", return_value=result) as scan:
            quality._complete_packet_duration(info, "ffprobe", "placeholder.mkv", None)
        self.assertEqual(info["total_frames"], round(0.5 * Fraction(info["fps_fraction"])))
        self.assertFalse(info["frame_count_exact"])
        command = scan.call_args.args[0]
        self.assertEqual(command[command.index("-select_streams") + 1], "3")
        info.update(total_frames=5, frame_count_exact=True)
        with patch.object(quality, "_scan_packet_duration", return_value=result):
            quality._complete_packet_duration(info, "ffprobe", "placeholder.mkv", None)
        self.assertEqual(info["total_frames"], 5)

    def test_packet_probe_cancel_and_timeout_close_files_and_reap_process(self):
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                children, handles = [], []
                cancel = threading.Event()
                real_popen, real_tempfile = subprocess.Popen, tempfile.TemporaryFile

                def launch(*args, **kwargs):
                    child = real_popen(*args, **kwargs)
                    children.append(child)
                    if cancelled:
                        cancel.set()
                    return child

                def temporary(*args, **kwargs):
                    handle = real_tempfile(*args, **kwargs)
                    handles.append(handle)
                    return handle

                with patch.object(quality.subprocess, "Popen", side_effect=launch), \
                        patch.object(quality.tempfile, "TemporaryFile", side_effect=temporary):
                    with self.assertRaises(quality.QualityCancelled if cancelled else quality.QualityError):
                        quality._scan_packet_duration([sys.executable, "-c", "import time; time.sleep(30)"],
                                                      fallback_frame_duration=0.04, timeout=0.1, cancel_event=cancel)
                self.assertIsNotNone(children[0].poll())
                self.assertEqual(len(handles), 2)
                self.assertTrue(all(handle.closed for handle in handles))

    def test_frame_color_fallback_targets_main_video_and_keeps_explicit_values(self):
        data = fixture()
        video = data["streams"][0]
        video.update(index=3, color_primaries="unknown", color_space="unspecified")
        cover = {"index": 0, "codec_type": "video", "codec_name": "mjpeg",
                 "disposition": {"attached_pic": 1}, "width": 400, "height": 400}
        data["streams"].insert(0, cover)
        frames = {"frames": [
            {"media_type": "video", "stream_index": 0, "color_primaries": "bt709"},
            {"media_type": "audio", "stream_index": 1, "color_primaries": "bt709"},
            {"media_type": "video", "stream_index": 3, "color_primaries": "bt2020",
             "color_space": "bt2020nc", "color_transfer": "smpte2084", "color_range": "pc"},
        ]}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "placeholder.mp4"
            path.write_bytes(b"placeholder")
            with patch.object(quality, "_run_json", side_effect=[data, frames]) as probe:
                info = quality.inspect_media(path, ffprobe="ffprobe", mediainfo="")
        self.assertEqual(info["stream"]["index"], 3)
        self.assertEqual(info["color_primaries"], "bt2020")
        self.assertEqual(info["stream"]["color_space"], "bt2020nc")
        self.assertEqual(info["color_transfer"], "arib-std-b67", "explicit stream transfer was overwritten")
        self.assertEqual(info["color_range"], "tv", "explicit stream range was overwritten")
        command = probe.call_args_list[1].args[0]
        self.assertEqual(command[command.index("-select_streams") + 1], "3")
        self.assertIn("-show_frames", command)

    def test_complete_stream_colors_skip_extra_frame_decoding(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "placeholder.mp4"
            path.write_bytes(b"placeholder")
            with patch.object(quality, "_run_json", return_value=fixture()) as probe:
                quality.inspect_media(path, ffprobe="ffprobe", mediainfo="")
            self.assertEqual(probe.call_count, 1)

    def test_cancellation_during_frame_color_fallback_is_not_swallowed(self):
        data = fixture()
        del data["streams"][0]["color_transfer"]
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "placeholder.mp4"
            path.write_bytes(b"placeholder")
            with patch.object(quality, "_run_json", side_effect=[data, quality.QualityCancelled("stopped")]) as probe:
                with self.assertRaises(quality.QualityCancelled):
                    quality.inspect_media(path, ffprobe="ffprobe", mediainfo="", cancel_event=cancel)
            self.assertIs(probe.call_args_list[1].kwargs["cancel_event"], cancel)

    def test_failed_frame_color_fallback_keeps_unknown_instead_of_guessing_hdr(self):
        data = fixture()
        del data["streams"][0]["color_transfer"]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "placeholder.mp4"
            path.write_bytes(b"placeholder")
            with patch.object(quality, "_run_json", side_effect=[data, quality.QualityError("decode failed")]):
                info = quality.inspect_media(path, ffprobe="ffprobe", mediainfo="")
        self.assertEqual(info["color_transfer"], "unknown")
        self.assertNotIn("color_transfer", info["stream"])
        self.assertTrue(any("decode failed" in note for note in info["inspection_notes"]))

    def test_cancel_terminates_a_silent_long_running_probe(self):
        cancel = threading.Event()
        children = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            cancel.set()
            return child

        started = time.monotonic()
        with patch.object(quality.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(quality.QualityCancelled):
                quality._run_json([sys.executable, "-c", "import time; time.sleep(30)"], cancel_event=cancel)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll(), "inspection left an orphan child")

    def test_probe_timeout_terminates_its_child_and_preserves_error(self):
        children = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        with patch.object(quality.subprocess, "Popen", side_effect=launch):
            with self.assertRaisesRegex(quality.QualityError, "超过"):
                quality._run_json([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
        self.assertIsNotNone(children[0].poll())


if __name__ == "__main__":
    unittest.main()
