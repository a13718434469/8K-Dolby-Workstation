"""Inspect media and report local Dolby/Bilibili readiness without certification claims.

The published Bilibili guidance cited below is dated 2021. Local conformance is
useful evidence, but the platform's own transcoding result remains authoritative.
This module has no GUI dependency and does not import the processing pipeline.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path


SOURCES = [{
    "title": "哔哩哔哩创作中心：杜比视界用户投稿功能上线",
    "url": "https://www.bilibili.com/opus/560508706903943321",
    "published_date": "2021-11-22",
}, {
    "title": "Dolby Vision for video sharing services: FAQs",
    "url": "https://professionalsupport.dolby.com/s/article/Dolby-Vision-for-video-sharing-services-FAQs?language=en_US",
    "published_date": "2024-03-26",
}]
GUIDANCE_NOTE = ("依据 B 站已公开的 2021 年投稿指引检查本地格式；该文档不是当前平台的完整验收规范。"
                 "本地检查不会证明平台已识别杜比，仍需创作中心转码结果和支持设备上的回放验收。")


class QualityError(RuntimeError):
    """Media could not be inspected reliably."""


class QualityCancelled(QualityError):
    """The caller requested that an in-progress inspection stop."""


def _tool(name):
    folder = Path(__file__).resolve().parent / "tools"
    for path in (folder / (name + ".exe"), folder / name):
        if path.is_file():
            return str(path)
    return shutil.which(name)


def _run_json(command, timeout=90, cancel_event=None):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = None
    started = time.monotonic()
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise QualityCancelled("媒体检测已停止")
        process = subprocess.Popen([str(x) for x in command], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace", **options)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise QualityCancelled("媒体检测已停止")
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise QualityError(f"媒体检测超过 {timeout:g} 秒，已终止检测工具")
            try:
                stdout, stderr = process.communicate(timeout=0.2 if remaining is None else min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QualityError(f"媒体检测工具无法运行: {exc}") from exc
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
    if process.returncode:
        raise QualityError(f"媒体检测失败: {stderr[-1800:].strip()}")
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise QualityError("媒体检测返回了无效 JSON") from exc
    if not isinstance(data, dict):
        raise QualityError("媒体检测返回了无效结构")
    return data


def _integer(value, default=None):
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _positive_rate(*values):
    for value in values:
        try:
            rate = Fraction(str(value))
            if rate > 0:
                return rate
        except (ValueError, TypeError, ZeroDivisionError):
            continue
    raise QualityError("视频没有有效的帧率")


def _flag(value):
    if value is None:
        return None
    if value in (0, "0", False):
        return False
    if value in (1, "1", True):
        return True
    return None


def _bit_depth(stream):
    # Prefer the pixel format: some codecs report container sample precision in
    # bits_per_sample, which is not the decoded luma precision.
    fmt = str(stream.get("pix_fmt", "")).lower()
    match = re.search(r"(?:p|gray)(9|10|12|14|16)(?:le|be)?$", fmt)
    if not match:
        match = re.fullmatch(r"p0(10|12|16)(?:le|be)", fmt)
    if match:
        return int(match[1])
    if fmt in {"yuv420p", "yuv422p", "yuv444p", "yuvj420p", "yuvj422p", "yuvj444p",
               "rgb24", "bgr24", "rgba", "bgra", "gbrp", "nv12", "nv21", "gray"}:
        return 8
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        bits = _integer(stream.get(key))
        if bits and 1 <= bits <= 32:
            return bits
    return None


def _atmos_evidence(stream):
    if not stream:
        return "absent", "没有音轨"
    codec = str(stream.get("codec_name", "")).lower()
    profile = str(stream.get("profile", ""))
    # Do not use user-editable tags such as a track title containing "Atmos".
    if codec in ("eac3", "truehd") and re.search(r"\batmos\b|\bJOC\b", profile, re.I):
        return "confirmed", f"FFprobe 音频解析信息: {profile}"
    if codec in ("eac3", "truehd"):
        return "unknown", f"{codec.upper()} 容器音轨未提供足以确认 Atmos 的对象音频信息"
    return "absent", "当前第一条音轨不是已确认的 E-AC-3 JOC / TrueHD Atmos"


def read_probe(data, path=""):
    """Normalize a decoded ffprobe ``-show_streams -show_format`` object."""
    if not isinstance(data, dict):
        raise QualityError("无效媒体检测结果")
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        raise QualityError("无效媒体轨道列表")
    video = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise QualityError("文件不包含可用视频轨道")
    fmt = data.get("format") or {}
    rate = _positive_rate(video.get("avg_frame_rate"), video.get("r_frame_rate"))
    duration = _number(video.get("duration")) or _number(fmt.get("duration"))
    if duration <= 0:
        # Some elementary streams expose only duration_ts and time_base.
        try:
            duration = float(Fraction(str(video.get("time_base"))) * int(video.get("duration_ts")))
        except (ValueError, TypeError, ZeroDivisionError):
            pass
    if duration <= 0 or not math.isfinite(duration):
        raise QualityError("视频没有有效时长")
    width, height = _integer(video.get("width"), 0), _integer(video.get("height"), 0)
    if width <= 0 or height <= 0:
        raise QualityError("视频没有有效尺寸")
    matrix_rotation = next((item.get("rotation") for item in video.get("side_data_list", [])
                            if isinstance(item, dict) and "display matrix" in str(item.get("side_data_type", "")).lower()
                            and item.get("rotation") is not None), None)
    angle = _number(matrix_rotation, None)
    if angle is None:
        angle = _number((video.get("tags") or {}).get("rotate"), 0.0)
    angle %= 360
    rotation = next((n % 360 for n in (0, 90, 180, 270, 360) if math.isclose(angle, n, abs_tol=0.1)), angle)
    display_width, display_height = (height, width) if rotation in (90, 270) else (width, height)
    decoded_count = _integer(video.get("nb_read_frames"))
    stored_count = _integer(video.get("nb_frames"))
    count = decoded_count if decoded_count is not None else stored_count
    exact_count = count is not None and count >= 0
    count = count if exact_count else max(1, round(duration * rate))
    dv = next((item for item in video.get("side_data_list", [])
               if isinstance(item, dict) and "dovi configuration record" in str(item.get("side_data_type", "")).lower()), {})
    audio = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    first_audio = audio[0] if audio else {}
    atmos, evidence = _atmos_evidence(first_audio)
    return {
        "path": str(path), "filename": Path(path).name if path else "",
        "width": width, "height": height, "fps": float(rate), "fps_fraction": str(rate),
        "rotation": rotation, "display_width": display_width, "display_height": display_height,
        "duration": duration, "total_frames": count, "frame_count_exact": exact_count,
        "codec_name": video.get("codec_name", "unknown"), "pix_fmt": video.get("pix_fmt", "unknown"),
        "bit_depth": _bit_depth(video), "color_transfer": video.get("color_transfer", "unknown"),
        "color_primaries": video.get("color_primaries", "unknown"), "color_space": video.get("color_space", "unknown"),
        "color_range": video.get("color_range", "unknown"), "container": fmt.get("format_name", "unknown"),
        "bit_rate": _integer(fmt.get("bit_rate"), 0), "video_bit_rate": _integer(video.get("bit_rate"), 0),
        "dv_profile": _integer(dv.get("dv_profile")),
        "dv_compatibility": _integer(dv.get("dv_bl_signal_compatibility_id")),
        "dv_rpu_present": _flag(dv.get("rpu_present_flag")),
        "dv_bl_present": _flag(dv.get("bl_present_flag")),
        "dv_el_present": _flag(dv.get("el_present_flag")),
        "audio_codec": first_audio.get("codec_name", "none"),
        "audio_profile": first_audio.get("profile", "unknown") if audio else "none",
        "audio_channels": _integer(first_audio.get("channels"), 0),
        "audio_sample_rate": _integer(first_audio.get("sample_rate"), 0),
        "audio_duration": _number(first_audio.get("duration")),
        "atmos": atmos, "atmos_evidence": evidence,
        "audio_streams": audio, "stream": video, "format": fmt,
        "inspection_tools": ["ffprobe"], "inspection_notes": [],
    }


def _apply_mediainfo(info, data):
    tracks = (data.get("media") or {}).get("track") or []
    audio = next((track for track in tracks if track.get("@type") == "Audio"), None)
    if not audio:
        return
    # MediaInfo's parsed feature fields are trusted as codec evidence. Freeform
    # Title and Comment fields are deliberately excluded.
    fields = [str(audio.get(key, "")) for key in (
        "Format_AdditionalFeatures", "Format_Profile", "Format_Commercial_IfAny",
        "Format_Commercial", "Format_AdditionalFeatures_String",
    )]
    evidence = " / ".join(value for value in fields if value)
    if info["audio_codec"] in ("eac3", "truehd") and re.search(r"\bJOC\b|\batmos\b", evidence, re.I):
        info["atmos"] = "confirmed"
        info["atmos_evidence"] = "MediaInfo 音频解析信息: " + evidence
    info["inspection_tools"].append("mediainfo")


def read_audio_probe(data, path=""):
    """Normalize audio-only inspection, without weakening video input checks."""
    streams = data.get("streams", []) if isinstance(data, dict) else []
    audio = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    if not audio:
        raise QualityError("文件不包含可用音轨")
    first, fmt = audio[0], data.get("format") or {}
    atmos, evidence = _atmos_evidence(first)
    return {"path": str(path), "filename": Path(path).name if path else "",
            "audio_codec": first.get("codec_name", "unknown"), "audio_profile": first.get("profile", "unknown"),
            "audio_channels": _integer(first.get("channels"), 0), "audio_sample_rate": _integer(first.get("sample_rate"), 0),
            "audio_duration": _number(first.get("duration")) or _number(fmt.get("duration")),
            "duration": _number(first.get("duration")) or _number(fmt.get("duration")),
            "atmos": atmos, "atmos_evidence": evidence, "audio_streams": audio, "format": fmt,
            "container": fmt.get("format_name", "unknown"), "inspection_tools": ["ffprobe"], "inspection_notes": []}


def inspect_audio(path, *, ffprobe=None, mediainfo=None, cancel_event=None):
    """Inspect a selected external audio file, including raw E-AC-3/JOC streams."""
    path = Path(path).resolve()
    if not path.is_file():
        raise QualityError(f"音频文件不存在: {path}")
    ffprobe = ffprobe or _tool("ffprobe")
    if not ffprobe:
        raise QualityError("缺少 ffprobe，请先安装 FFmpeg")
    info = read_audio_probe(_run_json([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                                     cancel_event=cancel_event), path)
    media_tool = mediainfo if mediainfo is not None else _tool("mediainfo")
    if media_tool and info["audio_codec"] in ("eac3", "truehd"):
        try:
            _apply_mediainfo(info, _run_json([media_tool, "--Output=JSON", str(path)], cancel_event=cancel_event))
        except QualityCancelled:
            raise
        except QualityError as exc:
            info["inspection_notes"].append(str(exc))
    return info


def inspect_media(path, *, ffprobe=None, mediainfo=None, count_frames=False, cancel_event=None):
    """Inspect local media; optional MediaInfo enriches Atmos detection if installed.

    ``count_frames`` decodes every video frame and is intended for final QC, not
    the quick file-picker preview. ``cancel_event`` is an optional threading.Event;
    cancellation terminates a long-running probe promptly. No tool is downloaded.
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise QualityError(f"媒体文件不存在: {path}")
    ffprobe = ffprobe or _tool("ffprobe")
    if not ffprobe:
        raise QualityError("缺少 ffprobe，请先安装 FFmpeg")
    command = [ffprobe, "-v", "error"]
    if count_frames:
        command.append("-count_frames")
    command += ["-show_streams", "-show_format", "-of", "json", str(path)]
    info = read_probe(_run_json(command, timeout=None if count_frames else 90, cancel_event=cancel_event), path)
    _complete_frame_colors(info, ffprobe, path, cancel_event)
    media_tool = mediainfo if mediainfo is not None else _tool("mediainfo")
    if media_tool and info["audio_codec"] in ("eac3", "truehd"):
        try:
            _apply_mediainfo(info, _run_json([media_tool, "--Output=JSON", str(path)], cancel_event=cancel_event))
        except QualityCancelled:
            raise
        except QualityError as exc:
            # An optional inspector failure does not invalidate ffprobe results,
            # but it must not silently turn an unknown Atmos stream into a pass.
            info["inspection_notes"].append(str(exc))
    return info


def _complete_frame_colors(info, ffprobe, path, cancel_event):
    """Fill omitted stream colors from this video's first decoded frame only.

    Some demuxer/codec combinations expose HEVC VUI colors on decoded frames
    while omitting them from show_streams. Never replace an explicit stream
    value, inspect an attached picture, or infer HDR from editable track tags.
    """
    fields = ("color_transfer", "color_primaries", "color_space", "color_range")
    def unknown(value):
        return str(value or "").strip().lower() in ("", "unknown", "unspecified", "reserved", "n/a", "none")
    stream = info["stream"]
    missing = [key for key in fields if unknown(stream.get(key))]
    if not missing:
        return
    index = _integer(stream.get("index"))
    if index is None or index < 0:
        info["inspection_notes"].append("视频轨道没有有效索引，未进行首帧色彩补充检测")
        return
    try:
        # A small packet budget permits decoder reordering/B-frames. The
        # absolute stream index is selected, so a cover image cannot take the
        # place of the main video. No full-file frame scan is needed here.
        data = _run_json([ffprobe, "-v", "error", "-select_streams", str(index),
                          "-read_intervals", "%+#32", "-show_frames", "-show_entries",
                          "frame=media_type,stream_index,color_transfer,color_primaries,color_space,color_range,chroma_location",
                          "-of", "json", str(path)], timeout=30, cancel_event=cancel_event)
    except QualityCancelled:
        raise
    except QualityError as exc:
        info["inspection_notes"].append("首帧色彩补充检测失败：" + str(exc))
        return
    frame = next((item for item in data.get("frames", []) if isinstance(item, dict)
                  and item.get("media_type") == "video" and _integer(item.get("stream_index")) == index), None)
    if frame is None:
        info["inspection_notes"].append("限定读取范围内没有可用视频帧，未推测缺失的色彩信息")
        return
    recovered = {}
    for key in missing:
        if not unknown(frame.get(key)):
            stream[key] = info[key] = frame[key]
            recovered[key] = frame[key]
    if unknown(stream.get("chroma_location")) and not unknown(frame.get("chroma_location")):
        stream["chroma_location"] = frame["chroma_location"]
    if recovered:
        info["decoded_frame_color"] = {"stream_index": index, "fields": recovered}
        info["inspection_notes"].append("从实际解码首帧补充视频色彩信息：" + ", ".join(recovered))


def _check(identifier, label, status, detail):
    return {"id": identifier, "label": label, "status": status, "detail": detail}


def assess_bilibili(info, require_atmos=False):
    """Assess the published Dolby Vision upload profile, never platform approval."""
    checks = []
    codec = str(info.get("codec_name", "unknown")).lower()
    checks.append(_check("codec", "HEVC 编码", "pass" if codec == "hevc" else "fail", f"检测到 {codec}"))
    depth = info.get("bit_depth")
    checks.append(_check("bit_depth", "10-bit 精度", "pass" if depth == 10 else "fail", f"检测到 {depth or '未知'} bit"))
    pixel_format = str(info.get("pix_fmt", "unknown")).lower()
    checks.append(_check("pixel_format", "4:2:0 10-bit 画面格式",
                         "pass" if pixel_format in ("yuv420p10le", "yuv420p10be", "p010le", "p010be") else "fail",
                         f"检测到 {pixel_format}；交付使用 4:2:0 10-bit HEVC"))
    transfer = str(info.get("color_transfer", "unknown")).lower()
    checks.append(_check("transfer", "HLG 传递函数", "pass" if transfer == "arib-std-b67" else "fail", f"检测到 {transfer}"))
    primaries, matrix = info.get("color_primaries"), info.get("color_space")
    checks.append(_check("color", "BT.2020 色彩信号", "pass" if primaries == "bt2020" and matrix == "bt2020nc" else "fail",
                         f"色度 {primaries or '未知'}；矩阵 {matrix or '未知'}"))
    profile, compatibility = info.get("dv_profile"), info.get("dv_compatibility")
    checks.append(_check("dolby_profile", "Dolby Vision Profile 8.4", "pass" if profile == 8 and compatibility == 4 else "fail",
                         f"Profile {profile if profile is not None else '未知'}；基础层兼容 ID {compatibility if compatibility is not None else '未知'}"))
    flags_ok = info.get("dv_rpu_present") is True and info.get("dv_bl_present") is True
    checks.append(_check("dolby_payload", "RPU 与基础层声明", "pass" if flags_ok else "fail",
                         "包含 RPU 和基础层声明；内容与时序仍需专门验收" if flags_ok else "缺少或无法确认 RPU / 基础层声明"))
    el_absent = info.get("dv_el_present") is False
    checks.append(_check("dolby_single_layer", "Profile 8.4 单层结构", "pass" if el_absent else "fail",
                         "未声明增强层（EL）" if el_absent else "声明了增强层或无法确认单层结构，不能按 Profile 8.4 交付"))
    container = str(info.get("container", "")).split(",")
    is_mp4 = any(x in container for x in ("mov", "mp4", "m4v"))
    checks.append(_check("container", "投稿封装", "pass" if is_mp4 else "fail", f"检测到 {info.get('container', '未知')}"))
    width, height = info.get("width", 0), info.get("height", 0)
    size_ok = 0 < min(width, height) <= 2160 and max(width, height) <= 3840
    checks.append(_check("resolution_guidance", "公开指引的 4K 范围", "pass" if size_ok else "warning",
                         f"检测到 {width}×{height}；参照旧版 3840×2160 指引按长短边检查，超出时需额外平台实测"))
    fps = _number(info.get("fps"))
    checks.append(_check("fps_guidance", "公开指引的 60fps 范围", "pass" if 0 < fps <= 60.001 else "warning",
                         f"检测到 {info.get('fps_fraction', fps)} fps；旧版官方指引列出最高 60fps"))
    bitrate = _integer(info.get("video_bit_rate"), 0) or _integer(info.get("bit_rate"), 0)
    checks.append(_check("bitrate_guidance", "码率参考", "pass" if bitrate >= 30_000_000 else "warning",
                         (f"检测到约 {bitrate / 1_000_000:.2f} Mbps；官方建议约 30 Mbps。码率不是画质或平台通过的保证"
                          if bitrate else "无法确定视频码率；官方建议约 30 Mbps，需结合画面复杂度和肉眼检查")))
    atmos = info.get("atmos", "unknown")
    codec_audio = info.get("audio_codec", "none")
    if atmos == "confirmed" and codec_audio == "eac3":
        audio_status = "pass"
        audio_detail = "确认第一条音轨包含 E-AC-3 JOC / Atmos 信息；能否获得平台双标仍需转码与回放确认"
    else:
        audio_status = "fail" if require_atmos else "warning"
        if atmos == "confirmed":
            audio_detail = f"确认 Atmos，但音轨为 {codec_audio}；B 站双标交付应保留受支持的 E-AC-3 JOC 码流"
        elif codec_audio == "none":
            audio_detail = "文件没有音轨；无法作为 Atmos 双标素材"
        else:
            audio_detail = info.get("atmos_evidence", "未能确认 Atmos；普通 E-AC-3 或多声道不等同于 Atmos")
    checks.append(_check("atmos", "Atmos 音轨证据", audio_status, audio_detail))
    failed = any(item["status"] == "fail" for item in checks)
    warned = any(item["status"] == "warning" for item in checks)
    status = "fail" if failed else "warning" if warned else "pass"
    if failed:
        summary = "尚不符合本地 B 站杜比视界格式检查，请先处理失败项目"
    elif warned:
        summary = "本地杜比视界关键格式符合公开指引，仍有交付提示；平台识别尚未验收"
    else:
        summary = "本地格式检查通过；平台识别尚未验收"
    return {"schema_version": 1, "status": status, "summary": summary, "checks": checks,
            "platform_verified": False, "require_atmos": bool(require_atmos),
            "sources": [dict(item) for item in SOURCES], "guidance_note": GUIDANCE_NOTE}


def validate_expectations(info, expected):
    """Return invariant checks against the intended export, separately from Bili.

    Frame count is accepted only when inspected rather than estimated. Durations
    permit one output frame plus 5ms rounding; cadence uses exact rational rates.
    """
    checks = []
    for key, label in (("width", "输出宽度"), ("height", "输出高度"), ("total_frames", "输出帧数")):
        if key in expected:
            same = info.get(key) == expected[key]
            if key == "total_frames":
                same = same and bool(info.get("frame_count_exact"))
            checks.append(_check("expected_" + key, label, "pass" if same else "fail",
                                 f"预期 {expected[key]}；实际 {info.get(key, '未知')}"))
    desired_rate = expected.get("fps_fraction", expected.get("fps"))
    if desired_rate is not None:
        try:
            rate_matches = Fraction(str(info.get("fps_fraction"))) == Fraction(str(desired_rate))
        except (ValueError, TypeError, ZeroDivisionError):
            rate_matches = False
        checks.append(_check("expected_fps", "输出帧率", "pass" if rate_matches else "fail",
                             f"预期 {desired_rate}；实际 {info.get('fps_fraction', '未知')}"))
    if "duration" in expected:
        fps = _number(info.get("fps"), 1.0)
        tolerance = 1 / max(fps, 1) + 0.005
        matches = abs(_number(info.get("duration")) - _number(expected["duration"])) <= tolerance
        checks.append(_check("expected_duration", "输出时长", "pass" if matches else "fail",
                             f"预期 {_number(expected['duration']):.6f}s；实际 {_number(info.get('duration')):.6f}s"))
    return checks


def write_quality_report(output, report):
    """Atomically write ``<output>.quality.json`` next to a completed export."""
    target = Path(str(output) + ".quality.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    payload.setdefault("schema_version", 1)
    payload.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
    # Local inspection cannot create evidence of platform acceptance. A caller
    # must attach independently observed platform evidence under a separate key.
    payload["platform_verified"] = False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=target.name + ".", suffix=".tmp",
                                         dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return target
