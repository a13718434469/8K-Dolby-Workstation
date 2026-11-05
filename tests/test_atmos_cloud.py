"""Offline SDK contract tests. These tests never submit a paid AWS job."""
import base64
import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

import atmos_cloud as cloud


HAS_SDK = importlib.util.find_spec("boto3") is not None
CONFIG = cloud.CloudAtmosConfig(
    region="us-west-2", bucket="dolby-test-bucket",
    role_arn="arn:aws:iam::123456789012:role/MediaConvert", allow_upload=True)
TOKEN = "1" * 32
PREFIX = f"dolby-workstation/{TOKEN}/"
INPUT = PREFIX + "input/master.wav"
OUTPUT = PREFIX + "output/atmos_joc.mp4"
MANIFEST = PREFIX + "output/atmos.mpd"


def make_wave(path, channels=16):
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(3)
        writer.setframerate(48000)
        writer.writeframes(bytes(channels * 3 * 480))


class CloudConfigTests(unittest.TestCase):
    def test_consent_is_required_before_sdk_or_file_access(self):
        with patch.object(cloud, "_load_sdk") as sdk:
            with self.assertRaisesRegex(cloud.CloudAtmosError, "尚未明确启用"):
                cloud.encode_atmos_cloud(replace(CONFIG, allow_upload=False), Path("missing"),
                                         Path("output"), Event(), lambda n: None, lambda s: None)
            sdk.assert_not_called()

    def test_configuration_rejects_unsafe_scope_and_malformed_names(self):
        for fields in ({"bucket": "s3://other"}, {"bucket": "127.0.0.1"},
                       {"role_arn": "not-a-role"}, {"region": "https://host"},
                       {"prefix": "../all"}, {"prefix": "/root"}, {"prefix": "dir//all"}):
            with self.subTest(fields=fields), self.assertRaises(cloud.CloudAtmosError):
                replace(CONFIG, **fields).validate()

    def test_missing_sdk_has_actionable_safe_error(self):
        with patch.object(cloud.importlib, "import_module", side_effect=ImportError("secret")):
            with self.assertRaisesRegex(cloud.CloudAtmosError, "boto3"):
                cloud.validate_cloud_config(CONFIG)

    def test_request_cannot_target_another_bucket_or_prefix(self):
        with self.assertRaises(cloud.CloudAtmosError):
            cloud.build_job_request(CONFIG, "s3://another/file.wav", "s3://another/out", TOKEN)

    def test_pcm_validation_accepts_valid_riff_and_rejects_stereo_or_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.wav"
            make_wave(path)
            result = cloud.inspect_pcm_master(path)
            self.assertEqual(result["frames"], 480)
            self.assertEqual(result["channel_order"], list(cloud.CHANNEL_ORDER))
            make_wave(path, channels=2)
            with self.assertRaisesRegex(cloud.CloudAtmosError, "16 声道"):
                cloud.inspect_pcm_master(path)
            make_wave(path)
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(cloud.CloudAtmosError, "不完整"):
                cloud.inspect_pcm_master(path)

    def test_rf64_large_file_header_is_parsed_without_loading_media(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.wav"
            data = bytes(48 * 10)
            fmt = struct.pack("<HHIIHH", 1, 16, 48000, 2304000, 48, 24)
            ds64 = struct.pack("<QQQI", len(data) + 72, len(data), 10, 0)
            path.write_bytes(b"RF64" + b"\xff" * 4 + b"WAVEds64" + struct.pack("<I", 28) + ds64
                             + b"fmt " + struct.pack("<I", 16) + fmt
                             + b"data" + b"\xff" * 4 + data)
            self.assertEqual(cloud.inspect_pcm_master(path)["frames"], 10)

    def test_extensible_pcm_header_is_accepted_but_float_subformat_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.wav"
            data = bytes(48 * 10)
            fmt = (struct.pack("<HHIIHHHHI", 0xfffe, 16, 48000, 2304000, 48, 24, 22, 24, 0)
                   + bytes.fromhex("0100000000001000800000aa00389b71"))
            body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
            path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
            self.assertEqual(cloud.inspect_pcm_master(path)["frames"], 10)
            path.write_bytes(path.read_bytes().replace(bytes.fromhex("0100000000001000800000aa00389b71"),
                                                     bytes.fromhex("0300000000001000800000aa00389b71")))
            with self.assertRaises(cloud.CloudAtmosError):
                cloud.inspect_pcm_master(path)


@unittest.skipUnless(HAS_SDK, "optional boto3 required for official AWS schema tests")
class CloudSdkTests(unittest.TestCase):
    def setUp(self):
        import botocore.session
        self.temp = tempfile.TemporaryDirectory(prefix="dolby-cloud-test-")
        self.root = Path(self.temp.name)
        self.master = self.root / "master.wav"
        self.output = self.root / "atmos.mp4"
        make_wave(self.master)
        self.cancel = Event()
        self.logs = []
        self.progress = []
        self.payload = b"synthetic transport fixture - not a real Atmos encode"
        self.model = botocore.session.get_session().get_service_model("mediaconvert")
        self.s3, self.client = MagicMock(), MagicMock()
        self.client.meta.service_model = self.model
        self.client.create_job.return_value = {"Job": {"Id": "job-123"}}
        self.client.get_job.return_value = {"Job": {"Status": "COMPLETE"}}
        self.s3.list_objects_v2.side_effect = self.list_objects
        self.s3.head_object.side_effect = self.head_object
        self.s3.download_file.side_effect = self.download
        self.session = MagicMock()
        self.session.get_credentials.return_value = object()
        self.session.client.side_effect = lambda service, **kwargs: self.s3 if service == "s3" else self.client
        self.session_patch = patch("boto3.Session", return_value=self.session)
        self.session_patch.start()
        self.uuid_patch = patch.object(cloud.uuid, "uuid4", return_value=MagicMock(hex=TOKEN))
        self.uuid_patch.start()
        self.wait_patch = patch.object(cloud, "POLL_SECONDS", 0)
        self.wait_patch.start()

    def tearDown(self):
        self.wait_patch.stop()
        self.uuid_patch.stop()
        self.session_patch.stop()
        self.temp.cleanup()

    def list_objects(self, **kwargs):
        keys = [INPUT, OUTPUT, MANIFEST]
        return {"Contents": [{"Key": key} for key in keys if key.startswith(kwargs["Prefix"])]}

    def head_object(self, **kwargs):
        return {"ContentLength": len(self.payload), "VersionId": "version-for-" + kwargs["Key"],
                "ChecksumType": "FULL_OBJECT",
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(self.payload).digest()).decode("ascii")}

    def download(self, bucket, key, destination, **kwargs):
        Path(destination).write_bytes(self.payload)
        kwargs["Callback"](len(self.payload))

    def encode(self):
        return cloud.encode_atmos_cloud(CONFIG, self.master, self.output, self.cancel,
                                        self.progress.append, self.logs.append)

    def journal_path(self):
        return self.output.with_name(self.output.name + ".cloud-job.json")

    def journal(self):
        return json.loads(self.journal_path().read_text(encoding="utf-8"))

    def test_request_matches_official_schema_and_declares_true_joc_codec(self):
        from botocore.validate import validate_parameters
        request = cloud.build_job_request(CONFIG, f"s3://{CONFIG.bucket}/{INPUT}",
                                          f"s3://{CONFIG.bucket}/{PREFIX}output/atmos", TOKEN)
        validate_parameters(request, self.model.operation_model("CreateJob").input_shape)
        settings = request["Settings"]
        audio = settings["OutputGroups"][0]["Outputs"][0]["AudioDescriptions"][0]
        self.assertEqual(audio["CodecSettings"]["Codec"], "EAC3_ATMOS")
        self.assertEqual(audio["CodecSettings"]["Eac3AtmosSettings"]["Bitrate"], 768000)
        self.assertNotIn("VideoDescription", settings["OutputGroups"][0]["Outputs"][0])
        self.assertEqual(settings["Inputs"][0]["AudioSelectors"]["Audio Selector 1"]["SelectorType"], "ALL_PCM")
        # botocore validates types/ranges but not every enum; check these too.
        self.assertIn("ALL_PCM", self.model.shape_for("AudioSelectorType").enum)
        self.assertIn("CODING_MODE_9_1_6", self.model.shape_for("Eac3AtmosCodingMode").enum)

    def test_success_returns_only_transport_evidence_and_removes_exact_versions(self):
        evidence = self.encode()
        self.assertEqual(self.output.read_bytes(), self.payload)
        self.assertTrue(evidence["verified_transport"])
        self.assertTrue(evidence["remote_sha256_verified"])
        self.assertTrue(evidence["temporary_objects_cleaned"])
        self.assertFalse(evidence["atmos_verified"])
        self.assertTrue(evidence["requires_media_validation"])
        self.assertEqual(evidence["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.client.cancel_job.assert_not_called()
        deleted = {call.kwargs["Key"] for call in self.s3.delete_object.call_args_list}
        self.assertEqual(deleted, {INPUT, OUTPUT, MANIFEST})
        for call in self.s3.delete_object.call_args_list:
            self.assertEqual(call.kwargs["VersionId"], "version-for-" + call.kwargs["Key"])
        self.assertEqual(self.progress[-1], 100)

    def test_missing_credentials_does_not_upload(self):
        self.session.get_credentials.return_value = None
        with self.assertRaisesRegex(cloud.CloudAtmosError, "凭证"):
            self.encode()
        self.s3.upload_file.assert_not_called()
        self.client.create_job.assert_not_called()

    def test_invalid_pcm_and_existing_output_stop_before_sdk_session(self):
        self.output.write_bytes(b"existing")
        with self.assertRaisesRegex(cloud.CloudAtmosError, "已存在"):
            self.encode()
        self.session.client.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"existing")

    def test_precancelled_does_not_upload(self):
        self.cancel.set()
        with self.assertRaises(cloud.CloudAtmosCancelled):
            self.encode()
        self.s3.upload_file.assert_not_called()

    def test_cancel_during_upload_removes_task_objects_without_submitting_job(self):
        def upload(*args, **kwargs):
            self.cancel.set()
            kwargs["Callback"](48)
        self.s3.upload_file.side_effect = upload
        with self.assertRaises(cloud.CloudAtmosCancelled):
            self.encode()
        self.client.create_job.assert_not_called()
        self.assertTrue(self.s3.delete_object.called)
        self.assertFalse(self.output.exists())

    def test_cancel_running_job_calls_cancel_then_cleans_temporary_objects(self):
        def get_job(**kwargs):
            self.cancel.set()
            return {"Job": {"Status": "CANCELED" if self.client.cancel_job.called else "PROGRESSING"}}
        self.client.get_job.side_effect = get_job
        with self.assertRaises(cloud.CloudAtmosCancelled):
            self.encode()
        self.client.cancel_job.assert_called_once_with(Id="job-123")
        self.assertTrue(self.s3.delete_object.called)
        self.assertFalse(self.output.exists())

    def test_remote_error_is_safe_and_cleanup_still_runs(self):
        self.client.get_job.return_value = {"Job": {"Status": "ERROR", "ErrorCode": 1040,
                                                      "ErrorMessage": "secret credential URL"}}
        with self.assertRaisesRegex(cloud.CloudAtmosError, "1040") as raised:
            self.encode()
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("secret", " ".join(self.logs))
        self.assertTrue(self.s3.delete_object.called)
        self.client.cancel_job.assert_not_called()

    def test_poll_exception_cancels_active_job_and_does_not_expose_response(self):
        self.client.get_job.side_effect = [RuntimeError("secret credential URL"), {"Job": {"Status": "CANCELED"}}]
        with self.assertRaisesRegex(cloud.CloudAtmosError, "RuntimeError") as raised:
            self.encode()
        self.assertNotIn("secret", str(raised.exception))
        self.client.cancel_job.assert_called_once()
        self.assertTrue(self.s3.delete_object.called)

    def test_download_corruption_does_not_publish_file(self):
        def corrupt(bucket, key, destination, **kwargs):
            Path(destination).write_bytes(b"x" * len(self.payload))
        self.s3.download_file.side_effect = corrupt
        with self.assertRaisesRegex(cloud.CloudAtmosError, "校验值"):
            self.encode()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob("*.partial")), [])
        self.assertTrue(self.s3.delete_object.called)

    def test_download_cancel_removes_partial_output(self):
        def download(bucket, key, destination, **kwargs):
            Path(destination).write_bytes(b"partial")
            self.cancel.set()
            kwargs["Callback"](7)
        self.s3.download_file.side_effect = download
        with self.assertRaises(cloud.CloudAtmosCancelled):
            self.encode()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_cleanup_does_not_touch_outside_unique_job_prefix(self):
        self.s3.list_objects_v2.side_effect = None
        self.s3.list_objects_v2.return_value = {"Contents": [{"Key": OUTPUT}, {"Key": "unrelated/movie.mp4"}]}
        self.encode()
        self.assertTrue(all(call.kwargs["Key"].startswith(PREFIX)
                            for call in self.s3.delete_object.call_args_list))

    def test_cleanup_failure_is_reported_without_losing_download(self):
        self.s3.delete_object.side_effect = RuntimeError("secret")
        evidence = self.encode()
        self.assertFalse(evidence["temporary_objects_cleaned"])
        self.assertTrue(self.output.exists())
        self.assertTrue(any("清理未获确认" in message for message in self.logs))
        self.assertNotIn("secret", " ".join(self.logs))

    def test_cancel_failure_warns_and_still_attempts_scoped_cleanup(self):
        self.client.get_job.side_effect = RuntimeError("secret")
        self.client.cancel_job.side_effect = RuntimeError("secret")
        with self.assertRaises(cloud.CloudAtmosError):
            self.encode()
        self.assertTrue(any("停止状态尚未确认" in message for message in self.logs))
        self.assertTrue(self.s3.delete_object.called)
        self.assertNotIn("secret", " ".join(self.logs))

    def test_paginated_cleanup_is_confined_to_unique_job(self):
        self.s3.list_objects_v2.side_effect = [
            {"Contents": [{"Key": INPUT}], "IsTruncated": True, "NextContinuationToken": "next-page"},
            {"Contents": [{"Key": OUTPUT}, {"Key": "other-owner/movie.mp4"}]},
        ]
        self.assertTrue(cloud._cleanup(self.s3, CONFIG.bucket, PREFIX, INPUT, self.logs.append))
        self.assertEqual({call.kwargs["Key"] for call in self.s3.delete_object.call_args_list}, {INPUT, OUTPUT})
        self.assertEqual(self.s3.list_objects_v2.call_args_list[1].kwargs["ContinuationToken"], "next-page")

    def test_lost_create_response_reports_unknown_submission_not_success(self):
        self.client.create_job.side_effect = RuntimeError("secret")
        with self.assertRaises(cloud.CloudAtmosError):
            self.encode()
        self.assertTrue(any("提交响应未确认" in message for message in self.logs))
        self.assertFalse(self.output.exists())

    def test_lost_response_journal_blocks_new_paid_submission(self):
        self.client.create_job.side_effect = RuntimeError("lost response")
        with self.assertRaises(cloud.CloudAtmosError):
            self.encode()
        record = self.journal()
        self.assertEqual(record["status"], "pending")
        self.assertFalse(record["remote_terminal"])
        self.assertEqual(record["token"], TOKEN)
        self.assertEqual(record["task_prefix"], PREFIX)
        self.assertEqual(record["master_sha256"], hashlib.sha256(self.master.read_bytes()).hexdigest())
        self.assertNotIn(CONFIG.role_arn, self.journal_path().read_text(encoding="utf-8"))
        with self.assertRaisesRegex(cloud.CloudAtmosError, TOKEN):
            self.encode()
        self.assertEqual(self.client.create_job.call_count, 1)
        self.assertEqual(self.s3.upload_file.call_count, 1)

    def test_crash_during_submission_has_durable_journal_before_sdk_call(self):
        def crash(**kwargs):
            record = self.journal()
            self.assertEqual(record["status"], "submitting")
            self.assertEqual(record["token"], kwargs["ClientRequestToken"])
            raise SystemExit("simulated process interruption before response")
        self.client.create_job.side_effect = crash
        with self.assertRaises(SystemExit):
            self.encode()
        self.assertEqual(self.journal()["status"], "pending")
        self.client.create_job.side_effect = None
        with self.assertRaisesRegex(cloud.CloudAtmosError, "禁止自动重复计费"):
            self.encode()
        self.assertEqual(self.client.create_job.call_count, 1)

    def test_hard_process_exit_keeps_submission_record_and_blocks_restart(self):
        # os._exit bypasses finally/atexit: this models the actual crash window
        # after submission begins, without making any real AWS API calls.
        script = '''
import os, sys
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch
import botocore.session
import atmos_cloud as cloud
config = cloud.CloudAtmosConfig("us-west-2", "dolby-test-bucket",
    "arn:aws:iam::123456789012:role/MediaConvert", allow_upload=True)
s3, client, session = MagicMock(), MagicMock(), MagicMock()
client.meta.service_model = botocore.session.get_session().get_service_model("mediaconvert")
client.create_job.side_effect = lambda **kwargs: os._exit(91)
session.get_credentials.return_value = object()
session.client.side_effect = lambda name, **kwargs: s3 if name == "s3" else client
with patch("boto3.Session", return_value=session):
    cloud.encode_atmos_cloud(config, Path(sys.argv[1]), Path(sys.argv[2]), Event(),
                             lambda value: None, lambda message: None)
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.master), str(self.output)],
                                cwd=Path(cloud.__file__).parent, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 91, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(self.journal()["status"], "submitting")
        self.assertFalse(self.journal()["remote_terminal"])
        with self.assertRaisesRegex(cloud.CloudAtmosError, "禁止自动重复计费"):
            self.encode()
        self.client.create_job.assert_not_called()
        self.s3.upload_file.assert_not_called()

    def test_job_id_is_persisted_immediately_and_uncertain_cancel_remains_pending(self):
        def fail_poll(**kwargs):
            record = self.journal()
            self.assertEqual(record["job_id"], "job-123")
            self.assertEqual(record["status"], "submitted")
            raise RuntimeError("network lost")
        self.client.get_job.side_effect = fail_poll
        self.client.cancel_job.side_effect = RuntimeError("network lost")
        with self.assertRaises(cloud.CloudAtmosError):
            self.encode()
        record = self.journal()
        self.assertEqual(record["status"], "pending")
        self.assertFalse(record["remote_terminal"])
        with self.assertRaisesRegex(cloud.CloudAtmosError, "job_id=job-123"):
            self.encode()
        self.assertEqual(self.client.create_job.call_count, 1)

    def test_completed_download_restores_evidence_without_sdk_or_upload(self):
        original = self.encode()
        record = self.journal()
        self.assertEqual(record["status"], "complete")
        self.assertTrue(record["remote_terminal"])
        self.assertEqual(record["output_sha256"], original["sha256"])
        self.assertEqual(record["evidence"], original)
        self.session.reset_mock()
        self.client.reset_mock()
        self.s3.reset_mock()
        with patch.object(cloud, "_load_sdk", side_effect=AssertionError("cache must be offline")):
            restored = self.encode()
        self.assertTrue(restored["restored_from_journal"])
        self.assertTrue(restored["verified_transport"])
        self.assertFalse(restored["atmos_verified"])
        self.assertEqual(restored["sha256"], original["sha256"])
        self.session.client.assert_not_called()
        self.client.create_job.assert_not_called()
        self.s3.upload_file.assert_not_called()

    def test_tampered_output_cannot_restore_or_trigger_reencoding(self):
        self.encode()
        self.output.write_bytes(b"x" * len(self.payload))
        with self.assertRaisesRegex(cloud.CloudAtmosError, "SHA-256"):
            self.encode()
        self.assertEqual(self.client.create_job.call_count, 1)

    def test_crash_after_publishing_recovers_download_without_claiming_cloud_cleanup(self):
        with patch.object(cloud, "_cleanup", side_effect=SystemExit("crash after publishing")):
            with self.assertRaises(SystemExit):
                self.encode()
        self.assertTrue(self.output.exists())
        restored = self.encode()
        self.assertTrue(restored["restored_from_journal"])
        self.assertFalse(restored["temporary_objects_cleaned"])
        self.assertFalse(restored["atmos_verified"])
        self.assertEqual(self.client.create_job.call_count, 1)
        self.assertTrue(any("临时文件清理尚未确认" in message for message in self.logs))

    def test_changed_master_or_configuration_cannot_reuse_paid_job(self):
        self.encode()
        with self.assertRaisesRegex(cloud.CloudAtmosError, "配置"):
            cloud.encode_atmos_cloud(replace(CONFIG, region="us-east-1"), self.master, self.output,
                                     self.cancel, self.progress.append, self.logs.append)
        data = bytearray(self.master.read_bytes())
        data[-1] ^= 1
        self.master.write_bytes(data)
        with self.assertRaisesRegex(cloud.CloudAtmosError, "母版"):
            self.encode()
        self.assertEqual(self.client.create_job.call_count, 1)

    def test_missing_completed_output_does_not_create_another_job(self):
        self.encode()
        self.output.unlink()
        with self.assertRaisesRegex(cloud.CloudAtmosError, "输出缺失"):
            self.encode()
        self.assertEqual(self.client.create_job.call_count, 1)

    def test_corrupted_journal_fails_closed_before_any_sdk_call(self):
        self.journal_path().write_text("{truncated", encoding="utf-8")
        with self.assertRaisesRegex(cloud.CloudAtmosError, "恢复记录损坏"):
            self.encode()
        self.session.client.assert_not_called()
        self.client.create_job.assert_not_called()

    def test_atomic_initial_claim_cannot_overwrite_existing_task_record(self):
        record = {"schema_version": 1, "status": "pending", "token": "existing"}
        cloud._write_journal(self.journal_path(), record, create=True)
        with self.assertRaises(cloud.CloudAtmosError):
            cloud._write_journal(self.journal_path(), {"token": "replacement"}, create=True)
        self.assertEqual(self.journal(), record)


if __name__ == "__main__":
    unittest.main()
