"""Inspect media and report local Dolby/Bilibili readiness without certification claims.

The cited Bilibili video guidance dates from 2021; its audio help was updated in
2024 and retrieved in 2026. Local conformance does not establish platform approval.
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
    "title": "哔哩哔哩帮助中心：杜比全景声规格与投稿方式（问题 3792、3794）",
    "url": "https://www.bilibili.com/blackboard/help.html#/?qid=300&pid=297",
    "api_url": "https://api.bilibili.com/x/web-goblin/help/detail?questionTypeId=300&ps=20&version=1",
    "updated_date": "2024-09-25",
    "retrieved_date": "2026-11-06",
}, {
    "title": "Dolby Vision for video sharing services: FAQs",
    "url": "https://professionalsupport.dolby.com/s/article/Dolby-Vision-for-video-sharing-services-FAQs?language=en_US",
    "published_date": "2024-03-26",
}]
GUIDANCE_NOTE = ("画面参照 B 站 2021 年杜比视界投稿指引；音频参照 2024-09-25 更新、2026-11-06 仍公开提供的杜比全景声帮助。"
                 "这些资料不是当前平台的完整验收规范。"
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


def _audio_bit_rate(first_audio, streams, fmt):
    """Use a track bitrate; a container total is valid only for one audio stream."""
    bitrate = _integer(first_audio.get("bit_rate"), 0)
    if bitrate > 0:
        return bitrate
    # Never mistake a video/container bitrate (or the sum of several audio
    # streams) for the bitrate of the selected first audio track.
    if len(streams) == 1 and streams[0] is first_audio:
        return max(0, _integer(fmt.get("bit_rate"), 0))
    return 0


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
    duration = _number(video.get("duration"))
    duration_source = "stream"
    if duration <= 0:
        # A container total can include delayed starts or longer audio/subtitle
        # tracks. Prefer this video's own clock before considering that total.
        try:
            duration = float(Fraction(str(video.get("time_base"))) * int(video.get("duration_ts")))
            duration_source = "stream_time_base"
        except (ValueError, TypeError, ZeroDivisionError):
            pass
    duration_exact_from_stream = duration > 0 and math.isfinite(duration)
    if not duration_exact_from_stream:
        duration = _number(fmt.get("duration"))
        duration_source = "container_estimate"
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
        "duration_exact_from_stream": duration_exact_from_stream,
        "duration_exact": duration_exact_from_stream, "duration_source": duration_source,
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
        "audio_bit_rate": _audio_bit_rate(first_audio, streams, fmt),
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
            "audio_bit_rate": _audio_bit_rate(first, streams, fmt),
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
    if not info["duration_exact_from_stream"]:
        _complete_packet_duration(info, ffprobe, path, cancel_event)
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


def _scan_packet_duration(command, *, fallback_frame_duration, timeout=300, cancel_event=None):
    """Scan bounded-memory packet timestamps; all temporary files close on cancel."""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = None
    started = time.monotonic()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise QualityCancelled("媒体检测已停止")
            process = subprocess.Popen([str(x) for x in command], stdout=output, stderr=errors, **options)
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise QualityCancelled("媒体检测已停止")
                if time.monotonic() - started >= timeout:
                    raise QualityError(f"视频时间戳检测超过 {timeout:g} 秒，已终止检测工具")
                if cancel_event is not None:
                    cancel_event.wait(0.1)
                else:
                    time.sleep(0.1)
            if cancel_event is not None and cancel_event.is_set():
                raise QualityCancelled("媒体检测已停止")
            if process.returncode:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 1800))
                raise QualityError("视频时间戳检测失败: " + errors.read().decode("utf-8", "replace").strip())
        except OSError as exc:
            raise QualityError(f"视频时间戳检测工具无法运行: {exc}") from exc
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        output.seek(0)
        minimum = maximum = previous_maximum = None
        maximum_has_duration = False
        known_end = None
        packets = missing_pts = 0
        for line in output:
            if cancel_event is not None and cancel_event.is_set():
                raise QualityCancelled("媒体检测已停止")
            fields = dict(part.split("=", 1) for part in line.decode("utf-8", "replace").strip().split("|") if "=" in part)
            if not fields:
                continue
            packets += 1
            pts = _number(fields.get("pts_time"), None)
            duration = _number(fields.get("duration_time"))
            if pts is None:
                missing_pts += 1
                continue
            minimum = pts if minimum is None else min(minimum, pts)
            if maximum is None or pts > maximum:
                previous_maximum, maximum = maximum, pts
                maximum_has_duration = duration > 0
            elif pts == maximum:
                maximum_has_duration = maximum_has_duration or duration > 0
            elif previous_maximum is None or pts > previous_maximum:
                previous_maximum = pts
            if duration > 0:
                end = pts + duration
                known_end = end if known_end is None else max(known_end, end)
        if minimum is None or maximum is None:
            raise QualityError("视频缺少可用 PTS，无法确定实际画面时长")
        exact = maximum_has_duration and not missing_pts
        end = known_end
        if not maximum_has_duration:
            interval = maximum - previous_maximum if previous_maximum is not None else fallback_frame_duration
            if not math.isfinite(interval) or interval <= 0:
                raise QualityError("视频最后一帧缺少时长，无法确定画面结束时间")
            end = max(known_end or maximum, maximum + interval)
        if end is None or not math.isfinite(end - minimum) or end <= minimum:
            raise QualityError("视频时间戳没有有效的画面时间跨度")
        return {"start": minimum, "end": end, "duration": end - minimum,
                "exact": exact, "packets": packets, "missing_pts": missing_pts,
                "last_duration_estimated": not maximum_has_duration}


def _complete_packet_duration(info, ffprobe, path, cancel_event):
    """Replace a container-length estimate with the selected video's PTS span."""
    index = _integer(info["stream"].get("index"))
    if index is None or index < 0:
        raise QualityError("视频缺少有效轨道索引，无法确认实际画面时长")
    result = _scan_packet_duration(
        [ffprobe, "-v", "error", "-select_streams", str(index), "-show_packets",
         "-show_entries", "packet=pts_time,duration_time", "-of", "compact=p=0:nk=0", str(path)],
        fallback_frame_duration=1 / float(Fraction(info["fps_fraction"])), cancel_event=cancel_event)
    info["duration"] = result["duration"]
    info["duration_exact"] = result["exact"]
    info["duration_source"] = "packet_timestamps" if result["exact"] else "estimated_packet_timestamps"
    info["video_timeline"] = result
    if not info["frame_count_exact"]:
        info["total_frames"] = max(1, round(result["duration"] * Fraction(info["fps_fraction"])))
    detail = f"按视频轨道 {index} 的实际 PTS 范围计算画面时长 {result['duration']:.6f}s，不使用容器总长"
    if result["last_duration_estimated"]:
        detail += "；最后一帧缺少时长，按相邻 PTS 间隔估计，不能视为精确时长"
    if result["missing_pts"]:
        detail += f"；{result['missing_pts']} 个包缺少 PTS，时间跨度仍是估计"
    info["inspection_notes"].append(detail)


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


def _atmos_audio_checks(info, *, require_atmos, strict_sample_rate=False):
    """Keep optional audio warnings separate from video-preservation failures."""
    failure = "fail" if require_atmos else "warning"
    codec = str(info.get("audio_codec", "none")).lower()
    confirmed = info.get("atmos") == "confirmed" and codec == "eac3"
    evidence = info.get("atmos_evidence") or "未能确认对象音频信息；普通 E-AC-3 或多声道不等同于 Atmos"
    checks = [_check("atmos", "Atmos 音轨证据", "pass" if confirmed else failure,
                     "确认第一条音轨包含 E-AC-3 JOC / Atmos 信息；平台双标仍需转码与回放确认"
                     if confirmed else f"需要已确认的 E-AC-3 JOC；当前编码 {codec}。{evidence}")]
    channels = _integer(info.get("audio_channels"), 0)
    checks.append(_check("audio_channels", "Atmos 交付声道", "pass" if channels in (6, 8) else failure,
                         f"第一条音轨 {channels or '未知'} 声道；B 站要求 6（5.1）或 8（7.1）声道"))
    bitrate = _integer(info.get("audio_bit_rate"), 0)
    checks.append(_check("audio_bitrate", "Atmos 音频码率", "pass" if bitrate >= 320_000 else failure,
                         (f"第一条音轨 {bitrate / 1000:g} kbps；按公开要求检查至少 320 kbps"
                          if bitrate > 0 else "无法确认第一条音轨码率；需至少 320 kbps，不能用视频总码率代替")))
    sample_rate = _integer(info.get("audio_sample_rate"), 0)
    checks.append(_check("audio_sample_rate", "Atmos 音频采样率",
                         "pass" if sample_rate == 48_000 else "fail" if strict_sample_rate else "warning",
                         f"第一条音轨 {sample_rate or '未知'} Hz；" +
                         ("本程序生成的 Atmos 音轨须为 48000 Hz" if strict_sample_rate else "B 站推荐 48000 Hz")))
    return checks


def validate_atmos_audio(info, expected_duration=None, *, strict_sample_rate=True):
    """Validate a generated JOC track, returning a report rather than raising.

    Requires parsed Atmos evidence, E-AC-3, 6/8 channels and >=320 kbps. Generated
    audio requires 48 kHz; ``strict_sample_rate=False`` makes this a recommendation
    for an existing JOC stream that is being copied without re-encoding.
    When provided, the intended duration may differ by at most 80 ms, allowing
    E-AC-3 frame padding. This local report does not certify Bilibili acceptance.
    """
    checks = _atmos_audio_checks(info, require_atmos=True, strict_sample_rate=strict_sample_rate)
    duration = _number(info.get("audio_duration"))
    expected = _number(expected_duration) if expected_duration is not None else None
    duration_ok = duration > 0
    detail = f"音轨实际时长 {duration:.6f}s"
    if expected is not None:
        delta = abs(duration - expected)
        duration_ok = duration_ok and expected > 0 and (delta <= 0.08 or math.isclose(delta, 0.08, abs_tol=1e-9))
        detail += f"；预期 {expected:.6f}s；允许误差不超过 0.080s"
    checks.append(_check("audio_duration", "Atmos 音轨时长", "pass" if duration_ok else "fail", detail))
    failed = any(item["status"] == "fail" for item in checks)
    warned = any(item["status"] == "warning" for item in checks)
    status = "fail" if failed else "warning" if warned else "pass"
    summary = ("；".join(item["detail"] for item in checks if item["status"] == "fail") if failed else
               "；".join(item["detail"] for item in checks if item["status"] == "warning") + "；平台识别尚未验收" if warned else
               "Atmos 音轨本地检查通过；平台识别尚未验收")
    return {"status": status, "checks": checks, "summary": summary}


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
    checks.extend(_atmos_audio_checks(info, require_atmos=require_atmos))
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
