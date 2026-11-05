"""Opt-in AWS MediaConvert Dolby Digital Plus JOC transport.

The caller supplies 16-channel, 48 kHz, 24-bit PCM in CHANNEL_ORDER, then
independently verifies the downloaded bitstream before accepting the result.
This module never labels an SDK/schema check as a successful Atmos encode.

AWS references (checked against botocore's public MediaConvert model):
https://docs.aws.amazon.com/mediaconvert/latest/ug/using-dolby-atmos-encoding.html
https://docs.aws.amazon.com/mediaconvert/latest/ug/audio-only-output.html
https://docs.aws.amazon.com/boto3/latest/reference/services/mediaconvert/client/create_job.html

An audio-only DASH SINGLE_FILE output is deliberate: the audio-only support
table explicitly lists JOC for DASH. The resulting fragmented MP4 is intended
for stream-copy into the locally produced video, not for public cloud hosting.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import re
import struct
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event
from typing import Callable


CHANNEL_ORDER = ("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs",
                 "Lw", "Rw", "Ltf", "Rtf", "Ltm", "Rtm", "Ltr", "Rtr")
ATMOS_BITRATE = 768000
POLL_SECONDS = 3.0
CANCEL_TIMEOUT_SECONDS = 30.0
JOB_TIMEOUT_SECONDS = 24 * 60 * 60
_TERMINAL = {"COMPLETE", "ERROR", "CANCELED"}
_PCM_GUID = bytes.fromhex("0100000000001000800000aa00389b71")


class CloudAtmosError(RuntimeError):
    """Safe-to-display error: does not include AWS response bodies or secrets."""


class CloudAtmosCancelled(CloudAtmosError):
    pass


@dataclass(frozen=True)
class CloudAtmosConfig:
    region: str
    bucket: str
    role_arn: str
    profile: str = ""
    prefix: str = "dolby-workstation"
    allow_upload: bool = False

    def validate(self) -> None:
        if self.allow_upload is not True:
            raise CloudAtmosError("尚未明确启用云端 Atmos：此功能会上传音频并产生 AWS 费用。")
        if not isinstance(self.region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", self.region):
            raise CloudAtmosError("AWS 区域格式无效。")
        if (not isinstance(self.bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket)
                or ".." in self.bucket or ".-" in self.bucket or "-." in self.bucket
                or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", self.bucket)):
            raise CloudAtmosError("请填写有效的 S3 存储桶名称，不要填写 URL 或目录。")
        if not isinstance(self.role_arn, str) or not re.fullmatch(
                r"arn:aws(?:-cn|-us-gov)?:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+", self.role_arn):
            raise CloudAtmosError("请填写 MediaConvert 服务角色 ARN。")
        if not isinstance(self.profile, str) or "\n" in self.profile or "\r" in self.profile:
            raise CloudAtmosError("AWS 配置名称无效。")
        if (not isinstance(self.prefix, str) or not self.prefix or len(self.prefix) > 512
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", self.prefix)
                or any(part in {"", ".", ".."} for part in self.prefix.split("/"))):
            raise CloudAtmosError("S3 临时目录仅支持字母、数字、短横线、下划线和正常子目录。")


def _cancelled(cancel: Event) -> None:
    if cancel.is_set():
        raise CloudAtmosCancelled("已取消云端 Atmos 处理。")


def inspect_pcm_master(master: Path) -> dict:
    """Validate RIFF/RF64 PCM without loading long recordings into memory."""
    master = Path(master)
    try:
        size = master.stat().st_size
        with master.open("rb") as source:
            header = source.read(12)
            if len(header) != 12 or header[:4] not in {b"RIFF", b"RF64"} or header[8:] != b"WAVE":
                raise CloudAtmosError("Atmos 母版必须为 PCM WAV/RF64 文件。")
            fmt = None
            rf64_data_size = None
            while source.tell() < min(size, 16 * 1024 * 1024):
                chunk = source.read(8)
                if len(chunk) != 8:
                    break
                tag, length = struct.unpack("<4sI", chunk)
                if tag == b"ds64":
                    if length < 28 or length > 1024 * 1024:
                        break
                    fields = source.read(28)
                    if len(fields) != 28:
                        break
                    rf64_data_size = struct.unpack("<QQQI", fields)[1]
                    source.seek(length - 28 + (length & 1), 1)
                elif tag == b"fmt ":
                    if length < 16 or length > 4096:
                        break
                    fmt = source.read(length)
                    if len(fmt) != length:
                        break
                    source.seek(length & 1, 1)
                elif tag == b"data":
                    if fmt is None:
                        break
                    format_id, channels, rate, byte_rate, block, bits = struct.unpack("<HHIIHH", fmt[:16])
                    valid_pcm = format_id == 1 or (
                        format_id == 0xfffe and len(fmt) >= 40
                        and struct.unpack("<H", fmt[16:18])[0] >= 22
                        and struct.unpack("<H", fmt[18:20])[0] in {0, 24}
                        and fmt[24:40] == _PCM_GUID)
                    data_size = rf64_data_size if length == 0xffffffff else length
                    if not valid_pcm or (channels, rate, bits, block, byte_rate) != (16, 48000, 24, 48, 2304000):
                        raise CloudAtmosError("Atmos 母版必须为 16 声道、48 kHz、24 位整数 PCM，声道顺序须符合 9.1.6 约定。")
                    if not data_size or data_size % block or source.tell() + data_size > size:
                        raise CloudAtmosError("Atmos 母版音频数据为空或不完整。")
                    return {"channels": channels, "sample_rate": rate, "bits_per_sample": bits,
                            "frames": data_size // block, "duration": data_size / byte_rate,
                            "channel_order": list(CHANNEL_ORDER)}
                else:
                    if length == 0xffffffff or source.tell() + length > size:
                        break
                    source.seek(length + (length & 1), 1)
    except OSError as exc:
        raise CloudAtmosError("无法读取 Atmos PCM 母版。") from exc
    raise CloudAtmosError("Atmos 母版缺少有效 WAV 格式或音频数据。")


def build_job_request(config: CloudAtmosConfig, input_uri: str,
                      destination_uri: str, token: str) -> dict:
    """Build a reviewable request without SDK calls, credentials, or networking."""
    config.validate()
    expected = f"s3://{config.bucket}/{config.prefix}/"
    if not input_uri.startswith(expected) or not destination_uri.startswith(expected):
        raise CloudAtmosError("云端输入输出必须位于已配置的临时目录内。")
    return {
        "Role": config.role_arn,
        "ClientRequestToken": token,
        "StatusUpdateInterval": "SECONDS_10",
        "Settings": {
            "TimecodeConfig": {"Source": "ZEROBASED"},
            "Inputs": [{
                "FileInput": input_uri,
                "TimecodeSource": "ZEROBASED",
                # One interleaved WAV contains 16 PCM channels, not 16 tracks.
                "AudioSelectors": {"Audio Selector 1": {
                    "SelectorType": "ALL_PCM", "DefaultSelection": "DEFAULT"}},
            }],
            "OutputGroups": [{
                "Name": "Atmos audio",
                "OutputGroupSettings": {
                    "Type": "DASH_ISO_GROUP_SETTINGS",
                    "DashIsoGroupSettings": {
                        "Destination": destination_uri,
                        "SegmentControl": "SINGLE_FILE",
                        "SegmentLength": 30, "FragmentLength": 2,
                        "MpdProfile": "ON_DEMAND_PROFILE",
                        "AudioChannelConfigSchemeIdUri": "DOLBY_CHANNEL_CONFIGURATION",
                    },
                },
                "Outputs": [{
                    "NameModifier": "_joc",
                    "ContainerSettings": {"Container": "MPD"},
                    "AudioDescriptions": [{
                        "AudioSourceName": "Audio Selector 1",
                        "CodecSettings": {
                            "Codec": "EAC3_ATMOS",
                            "Eac3AtmosSettings": {
                                "Bitrate": ATMOS_BITRATE, "SampleRate": 48000,
                                "CodingMode": "CODING_MODE_9_1_6",
                                "BitstreamMode": "COMPLETE_MAIN",
                                "MeteringMode": "ITU_BS_1770_4",
                            },
                        },
                    }],
                }],
            }],
        },
    }


def _load_sdk():
    try:
        boto3 = importlib.import_module("boto3")
        botocore_config = importlib.import_module("botocore.config")
        transfer = importlib.import_module("boto3.s3.transfer")
    except ImportError as exc:
        raise CloudAtmosError("云端 Atmos 需要可选依赖 boto3；请先安装 AWS SDK。") from exc
    return boto3, botocore_config.Config, transfer.TransferConfig


def validate_cloud_config(config: CloudAtmosConfig) -> None:
    """Offline preflight, safe to call before expensive local video work."""
    config.validate()
    _load_sdk()


def _safe_error_code(exc: Exception) -> str:
    # Never interpolate exception messages: SDK errors can contain URLs/tokens.
    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else type(exc).__name__


def _file_sha256(path: Path, cancel: Event) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                _cancelled(cancel)
                digest.update(block)
    except OSError as exc:
        raise CloudAtmosError("无法读取文件以检查云端任务恢复记录。") from exc
    _cancelled(cancel)
    return digest.hexdigest()


def _config_digest(config: CloudAtmosConfig) -> str:
    # Only a digest of non-secret configuration is written to the journal.
    data = {"config": asdict(config), "bitrate": ATMOS_BITRATE,
            "coding_mode": "CODING_MODE_9_1_6", "request_version": 1}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _write_journal(path: Path, record: dict, *, create: bool = False) -> None:
    """Publish a complete, flushed record; initial creation cannot overwrite a rival."""
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as target:
            json.dump(record, target, ensure_ascii=False, sort_keys=True, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        if create:
            # Windows rename refuses an existing destination; POSIX link has
            # the same no-replace property. Both publish the fully written
            # record atomically. Never overwrite another process's claim.
            if os.name == "nt":
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        else:
            os.replace(temporary, path)
    except FileExistsError as exc:
        raise CloudAtmosError("已有云端任务恢复记录或另一个处理进程；本次未创建新的收费任务。") from exc
    except OSError as exc:
        raise CloudAtmosError("无法持久保存云端任务恢复记录，已停止继续提交；请保留现有记录并检查 AWS。") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _journal_reference(record: dict) -> str:
    fields = []
    for name in ("job_id", "token"):
        value = record.get(name)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            fields.append(f"{name}={value}")
    return "；".join(fields) or "标识缺失或损坏"


def _restore_journal(path: Path, master_hash: str, config_hash: str,
                     output: Path, cancel: Event) -> dict | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("oversized journal")
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            raise ValueError("invalid journal")
    except (OSError, ValueError, UnicodeError) as exc:
        raise CloudAtmosError("云端任务恢复记录损坏；为防止重复计费已禁止重新提交，请先在 AWS 确认原任务。") from exc
    reference = _journal_reference(record)
    if record.get("status") != "complete" or record.get("remote_terminal") is not True:
        raise CloudAtmosError("此输出已有云端任务记录，禁止自动重复计费。请先在 AWS 确认原任务：" + reference)
    if record.get("master_sha256") != master_hash or record.get("config_sha256") != config_hash:
        raise CloudAtmosError("母版或云端配置与已完成任务不一致，未重新提交收费任务：" + reference)
    evidence = record.get("evidence")
    checksum = record.get("output_sha256")
    if (not isinstance(evidence, dict) or not isinstance(checksum, str)
            or not re.fullmatch(r"[a-f0-9]{64}", checksum)
            or evidence.get("sha256") != checksum or evidence.get("verified_transport") is not True
            or evidence.get("verification_scope") != "transport_only"):
        raise CloudAtmosError("云端任务已结束，但本地完整下载尚未确认；禁止重复计费，请先检查原任务：" + reference)
    if not output.is_file() or output.stat().st_size != evidence.get("download_size"):
        raise CloudAtmosError("已完成云端任务的本地输出缺失或长度不符；禁止重复计费，请先检查原任务：" + reference)
    if _file_sha256(output, cancel) != checksum:
        raise CloudAtmosError("本地云端输出 SHA-256 与恢复记录不一致；禁止重新提交收费任务：" + reference)
    restored = dict(evidence)
    restored.setdefault("temporary_objects_cleaned", record.get("temporary_objects_cleaned") is True)
    restored.update({"atmos_verified": False, "requires_media_validation": True,
                     "verification_scope": "transport_only", "restored_from_journal": True})
    return restored


def _keys(s3, bucket: str, prefix: str) -> list[str]:
    result = []
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    seen_tokens = set()
    while True:
        response = s3.list_objects_v2(**kwargs)
        for item in response.get("Contents", []):
            key = item.get("Key", "")
            if isinstance(key, str) and key.startswith(prefix):
                result.append(key)
        if not response.get("IsTruncated"):
            return result
        token = response.get("NextContinuationToken")
        if not token or token in seen_tokens:
            raise CloudAtmosError("S3 临时文件列表不完整，无法确认输出。")
        seen_tokens.add(token)
        kwargs["ContinuationToken"] = token


def _cleanup(s3, bucket: str, prefix: str, input_key: str, log: Callable[[str], None]) -> bool:
    """Delete exact keys in this random job prefix, including current versions."""
    success = True
    try:
        keys = set(_keys(s3, bucket, prefix)) | {input_key}
    except Exception:
        keys = {input_key}
        success = False
    for key in sorted(keys):
        try:
            request = {"Bucket": bucket, "Key": key}
            # Explicit VersionId removes the uploaded media in versioned buckets,
            # rather than merely hiding it behind a delete marker.
            try:
                version = s3.head_object(Bucket=bucket, Key=key).get("VersionId")
                if version:
                    request["VersionId"] = version
            except Exception as exc:
                if _safe_error_code(exc) not in {"404", "NoSuchKey", "NotFound"}:
                    success = False
            s3.delete_object(**request)
        except Exception:
            success = False
    if not success:
        log("部分本次云端临时文件清理未获确认，请检查配置存储桶中的任务目录：" + prefix)
    return success


def _stop_job(client, job_id: str, log: Callable[[str], None]) -> str | None:
    try:
        client.cancel_job(Id=job_id)
        until = time.monotonic() + CANCEL_TIMEOUT_SECONDS
        while time.monotonic() < until:
            status = client.get_job(Id=job_id)["Job"]["Status"]
            if status in _TERMINAL:
                return status
            time.sleep(min(1.0, max(0.0, until - time.monotonic())))
    except Exception:
        pass
    log("云端任务停止状态尚未确认，请在 AWS 检查任务：" + job_id)
    return None


def encode_atmos_cloud(config: CloudAtmosConfig, master: Path, output: Path,
                       cancel: Event, progress: Callable[[float], None],
                       log: Callable[[str], None]) -> dict:
    """Run opt-in encoding; return transport evidence, never codec certification.

    Progress ranges from 0 to 100. Credentials use boto3's normal provider chain;
    this function neither accepts nor persists access keys. No AWS calls occur
    until explicit upload consent, local PCM validation and SDK checks pass.
    The caller must inspect/decode the output and confirm E-AC-3 JOC separately.
    """
    config.validate()
    _cancelled(cancel)
    master, output = Path(master), Path(output)
    master_info = inspect_pcm_master(master)
    journal_path = output.with_name(output.name + ".cloud-job.json")
    master_hash = _file_sha256(master, cancel)
    config_hash = _config_digest(config)
    restored = _restore_journal(journal_path, master_hash, config_hash, output, cancel)
    if restored is not None:
        log("已校验并恢复之前下载的云端音频，未上传音频或重新创建收费任务。")
        if not restored.get("temporary_objects_cleaned"):
            log("原任务的云端临时文件清理尚未确认，请在 AWS 检查任务：" + str(restored.get("job_id", "unknown")))
        progress(100.0)
        return restored
    if output.exists():
        raise CloudAtmosError("云端 Atmos 输出文件已存在，请使用新的临时输出路径。")
    if not output.parent.is_dir():
        raise CloudAtmosError("云端 Atmos 输出目录不存在。")
    boto3, SDKConfig, TransferConfig = _load_sdk()
    token = uuid.uuid4().hex
    job_prefix = f"{config.prefix}/{token}/"
    input_key = job_prefix + "input/master.wav"
    output_prefix = job_prefix + "output/"
    request = build_job_request(config, f"s3://{config.bucket}/{input_key}",
                                f"s3://{config.bucket}/{output_prefix}atmos", token)
    partial = output.with_name(output.name + f".{token}.partial")
    sdk_config = SDKConfig(connect_timeout=10, read_timeout=30,
                           retries={"mode": "standard", "max_attempts": 2})
    s3 = client = None
    touched_remote = False
    job_id = None
    terminal = False
    created_attempted = False
    evidence = None
    journal = None
    try:
        session = boto3.Session(region_name=config.region, profile_name=config.profile or None)
        if session.get_credentials() is None:
            raise CloudAtmosError("未找到 AWS 凭证；请先配置系统凭证链或指定 AWS 配置名称。")
        s3 = session.client("s3", config=sdk_config)
        client = session.client("mediaconvert", config=sdk_config)
        # Local schema validation occurs before any S3 media upload.
        from botocore.validate import validate_parameters
        validate_parameters(request, client.meta.service_model.operation_model("CreateJob").input_shape)
        _cancelled(cancel)
        new_journal = {
            "schema_version": 1, "status": "pending", "remote_terminal": False,
            "token": token, "task_prefix": job_prefix, "job_id": None,
            "master_sha256": master_hash, "config_sha256": config_hash,
            "created_at": time.time(),
        }
        _write_journal(journal_path, new_journal, create=True)
        journal = new_journal
        size = master.stat().st_size
        uploaded = 0

        def uploaded_bytes(count):
            nonlocal uploaded
            _cancelled(cancel)
            uploaded += count
            progress(min(20.0, uploaded * 20.0 / size))

        log("正在上传音频母版到已配置的 AWS 存储桶。")
        touched_remote = True
        s3.upload_file(str(master), config.bucket, input_key, Callback=uploaded_bytes,
                       Config=TransferConfig(use_threads=False))
        _cancelled(cancel)
        progress(20.0)
        log("正在提交 Dolby Digital Plus JOC 云端编码任务。")
        journal["status"] = "submitting"
        _write_journal(journal_path, journal)
        created_attempted = True
        response = client.create_job(**request)
        job_id = response["Job"]["Id"]
        journal.update({"status": "submitted", "job_id": job_id})
        _write_journal(journal_path, journal)
        started = time.monotonic()
        while True:
            _cancelled(cancel)
            job = client.get_job(Id=job_id)["Job"]
            status = job.get("Status")
            if status in _TERMINAL:
                terminal = True
                journal.update({"status": status.lower(), "remote_terminal": True})
                _write_journal(journal_path, journal)
                if status != "COMPLETE":
                    if status == "CANCELED":
                        raise CloudAtmosCancelled("云端 Atmos 任务已取消。")
                    error_code = job.get("ErrorCode", "unknown")
                    safe_code = str(error_code) if isinstance(error_code, int) else "unknown"
                    raise CloudAtmosError(f"云端 Atmos 编码失败（服务错误码 {safe_code}）。")
                break
            if time.monotonic() - started > JOB_TIMEOUT_SECONDS:
                raise CloudAtmosError("云端 Atmos 任务等待超时，正在取消。")
            percent = job.get("JobPercentComplete", 0)
            if isinstance(percent, (int, float)):
                progress(20.0 + max(0.0, min(100.0, percent)) * .6)
            cancel.wait(POLL_SECONDS)
        _cancelled(cancel)
        candidates = [key for key in _keys(s3, config.bucket, output_prefix) if key.lower().endswith(".mp4")]
        if len(candidates) != 1:
            raise CloudAtmosError("云端未返回唯一的单文件 Atmos 音频输出。")
        key = candidates[0]
        head = s3.head_object(Bucket=config.bucket, Key=key, ChecksumMode="ENABLED")
        expected_size = head.get("ContentLength", 0)
        if not isinstance(expected_size, int) or expected_size <= 0:
            raise CloudAtmosError("云端 Atmos 输出为空。")
        downloaded = 0

        def downloaded_bytes(count):
            nonlocal downloaded
            _cancelled(cancel)
            downloaded += count
            progress(80.0 + min(19.0, downloaded * 19.0 / expected_size))

        log("正在下载云端编码结果，随后仍需本地核验 Atmos 位流。")
        extra = {"VersionId": head["VersionId"]} if head.get("VersionId") else {}
        s3.download_file(config.bucket, key, str(partial), ExtraArgs=extra,
                         Callback=downloaded_bytes, Config=TransferConfig(use_threads=False))
        _cancelled(cancel)
        if not partial.is_file() or partial.stat().st_size != expected_size:
            raise CloudAtmosError("下载的 Atmos 文件大小与云端不一致。")
        digest = hashlib.sha256()
        with partial.open("rb") as result:
            while block := result.read(1024 * 1024):
                _cancelled(cancel)
                digest.update(block)
        checksum_verified = False
        if head.get("ChecksumSHA256") and head.get("ChecksumType") == "FULL_OBJECT":
            if base64.b64encode(digest.digest()).decode("ascii") != head["ChecksumSHA256"]:
                raise CloudAtmosError("下载的 Atmos 文件校验值与云端不一致。")
            checksum_verified = True
        _cancelled(cancel)
        evidence = {
            "backend": "aws_mediaconvert", "job_id": job_id,
            "requested_codec": "EAC3_ATMOS", "requested_bitrate": ATMOS_BITRATE,
            "requested_coding_mode": "CODING_MODE_9_1_6", "master": master_info,
            "verified_transport": True, "verification_scope": "transport_only",
            "atmos_verified": False, "requires_media_validation": True,
            "download_size": expected_size, "sha256": digest.hexdigest(),
            "remote_sha256_verified": checksum_verified,
        }
        # Save the verified download hash before publishing the output. If the
        # process crashes just after the rename, the next call can reuse it.
        # A crash before the rename leaves an incomplete local delivery and is
        # blocked, rather than silently starting another paid encode.
        journal.update({"status": "complete", "remote_terminal": True,
                        "output_sha256": digest.hexdigest(), "evidence": evidence})
        _write_journal(journal_path, journal)
        partial.replace(output)
    except CloudAtmosError:
        raise
    except Exception as exc:
        if cancel.is_set():
            raise CloudAtmosCancelled("已取消云端 Atmos 处理。") from None
        raise CloudAtmosError("AWS Atmos 操作失败（" + _safe_error_code(exc) + "）。") from None
    finally:
        if job_id and not terminal:
            stopped_status = _stop_job(client, job_id, log)
            if journal is not None:
                journal.update({"status": stopped_status.lower() if stopped_status else "pending",
                                "remote_terminal": stopped_status is not None})
        elif created_attempted and not job_id:
            if journal is not None:
                journal.update({"status": "pending", "remote_terminal": False})
            log("提交响应未确认；请在 AWS 检查本次请求，避免重复提交。请求标识：" + token)
        elif not created_attempted and journal is not None:
            journal.update({"status": "canceled", "remote_terminal": True})
        if touched_remote and s3 is not None:
            cleaned = _cleanup(s3, config.bucket, job_prefix, input_key, log)
            if evidence is not None:
                evidence["temporary_objects_cleaned"] = cleaned
            if journal is not None:
                journal["temporary_objects_cleaned"] = cleaned
        if journal is not None:
            try:
                _write_journal(journal_path, journal)
            except CloudAtmosError:
                # The previous durable record remains and blocks resubmission.
                log("云端任务恢复记录更新失败，请保留现有记录并先在 AWS 确认任务：" + _journal_reference(journal))
        if partial.exists():
            partial.unlink()
    progress(100.0)
    return evidence
