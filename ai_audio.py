"""Local RNNoise inference for an explicitly selected speech recording."""
from __future__ import annotations

import math
import re
from pathlib import Path

from hires_audio import _timeline
from install_audio_model import MODEL_PATH, MODEL_SHA256, model_ready


WET_MIX = 0.8


class AiAudioError(ValueError):
    """A requested AI audio operation cannot be verified."""


def ai_skip_reason(plan):
    if plan.get("joc") or plan.get("retained_atmos"):
        return "已有 Atmos，保留原音轨与空间信息"
    if plan.get("action") not in {"copy", "aac"}:
        return "无损音轨按原规格保留"
    if plan.get("channels") not in (1, 2):
        return "AI 人声降噪适用于单声道或双声道，当前按原音轨方案导出"
    return ""


def verify_ai_model(ffmpeg, model_path, run):
    """Check both the pinned weights and this FFmpeg build's actual model loader."""
    path = Path(model_path)
    if path.name != MODEL_PATH.name or not path.is_file():
        raise AiAudioError("请先安装 AI 人声降噪模型")
    if not model_ready(path):
        raise AiAudioError("AI 人声降噪模型校验失败，请重新安装")
    # A relative, fixed name avoids interpolating user directory names into a
    # filter expression. The same working directory is used when exporting.
    run([ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-f", "lavfi",
         "-i", "anullsrc=r=48000:cl=mono:d=0.02", "-af", "arnndn=m=model.rnnn",
         "-f", "null", "-"], cwd=path.resolve().parent)
    return MODEL_SHA256


def _measure(ffmpeg, path, ordinal, filters, run):
    stats = "astats=metadata=0:reset=0:measure_perchannel=none:measure_overall=Peak_level+RMS_level+Number_of_samples"
    chain = ",".join(filter(None, (filters, stats)))
    log = run([ffmpeg, "-nostdin", "-nostats", "-hide_banner", "-v", "info", "-xerror",
               "-i", str(path), "-map", f"0:a:{ordinal}", "-vn", "-af", chain, "-f", "null", "-"])
    def decibels(field):
        match = re.search(re.escape(field) + r":\s*(-inf|[-+]?\d+(?:\.\d+)?)\s*$", log, re.M)
        if not match:
            raise AiAudioError("无法读取完整音轨的电平测量")
        return None if match[1] == "-inf" else float(match[1])
    count = re.search(r"Number of samples:\s*(\d+)(?:\.0+)?\s*$", log, re.M)
    if not count or int(count[1]) < 1:
        raise AiAudioError("无法读取完整音轨的样本数")
    return {"samples": int(count[1]), "peak_dbfs": decibels("Peak level dB"),
            "rms_dbfs": decibels("RMS level dB")}


def analyze_ai_audio(ffmpeg, source, plan, duration, audio_offset, model_path, run):
    reason = ai_skip_reason(plan)
    if reason:
        return {"applied": False, "detail": reason}
    before = _measure(ffmpeg, source, plan["source_audio_ordinal"], _timeline(plan, duration, audio_offset), run)
    if before["samples"] != round(duration * plan["sample_rate"]):
        raise AiAudioError("送入 AI 的声音长度与画面不一致")
    if before["peak_dbfs"] is None or before["peak_dbfs"] < -80 or duration < 0.02:
        return {"applied": False, "detail": "声音极短或接近静音，保留原音轨", "before": before}
    if not model_ready(model_path):
        raise AiAudioError("AI 人声降噪模型校验失败，请重新安装")
    return {"applied": True, "detail": "已使用本机 RNNoise 神经网络进行人声降噪，保留部分原声",
            "method": "FFmpeg arnndn / RNNoise", "model_sha256": MODEL_SHA256,
            "mix": WET_MIX, "before": before, "model_rate_hz": 48000}


def ai_encode_args(plan, analysis, duration, audio_offset=0):
    """Call FFmpeg with its working directory set to MODEL_PATH.parent."""
    if not analysis.get("applied") or analysis.get("model_sha256") != MODEL_SHA256:
        raise AiAudioError("未确认 AI 降噪模型或音轨处理计划")
    rate = plan["sample_rate"]
    samples = round(rate * duration)
    # arnndn works in 480-sample frames at 48 kHz. Its output includes one
    # frame of algorithmic delay for both wet and dry. Feed the trailing
    # block, compensate that delay, then restore the source sample grid.
    chain = [_timeline(plan, duration, audio_offset), "aresample=48000", "apad=pad_len=960",
             f"arnndn=m=model.rnnn:mix={WET_MIX}", "atrim=start_sample=480",
             "asetpts=PTS-STARTPTS", f"aresample={rate}",
             "alimiter=limit=0.97:level=0:latency=1", f"atrim=end_sample={samples}"]
    return ["-af", ",".join(chain), "-c:a", "alac", "-sample_fmt", "s16p",
            "-movie_timescale", str(rate)]


def verify_ai_audio(ffmpeg, final_path, final_raw_stream, plan, analysis, duration, run):
    def integer(key):
        try:
            return int(final_raw_stream.get(key, 0))
        except (TypeError, ValueError):
            return 0
    rate = plan["sample_rate"]
    if (final_raw_stream.get("codec_name") != "alac" or integer("bits_per_raw_sample") != 16
            or integer("sample_rate") != rate or integer("channels") != plan["channels"]):
        raise AiAudioError("AI 成片的音轨编码、采样率、位深或声道数不符合处理计划")
    try:
        start = float(final_raw_stream["start_time"])
    except (KeyError, TypeError, ValueError):
        raise AiAudioError("无法确认 AI 成片的音轨起点") from None
    if not math.isfinite(start) or abs(start) > 1 / rate:
        raise AiAudioError("AI 成片的音轨与视频起点不一致")
    after = _measure(ffmpeg, final_path, 0, "", run)
    if after["samples"] != round(duration * rate):
        raise AiAudioError("AI 成片音轨的解码样本数与视频长度不符")
    if after["peak_dbfs"] is None:
        raise AiAudioError("AI 处理后音轨完全静音")
    return dict(analysis, after=after, verified_samples=after["samples"],
                output_codec="alac", output_bits=16)
