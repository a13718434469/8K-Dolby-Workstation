"""Select a free audio path and verify lossless output against the source samples."""
from __future__ import annotations

import math
import re


class HiresAudioError(ValueError):
    """The selected track cannot be safely exported or validated."""


# FFmpeg's ALAC encoder can silently remap layouts outside this list. Keep
# the original PCM layout in that case and verify both the layout and samples.
_ALAC_LAYOUTS = {"mono", "stereo", "3.0", "4.0", "5.0", "5.1", "6.1(back)", "7.1(wide)"}
_LOSSLESS = {"alac", "flac", "ape", "mlp", "mp4als", "ralf", "shorten", "tak", "wmalossless", "truehd",
             "pcm_bluray", "pcm_dvd", "pcm_lxf", "pcm_s24daud"}
_COPY = {"aac", "ac3", "eac3", "mp3"}


def _positive_int(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return 0


def _precision(stream):
    # s32/s32p is also FFmpeg's *storage* for 16-, 20- and 24-bit samples.
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        value = _positive_int(stream.get(key))
        if value:
            return value
    width = re.fullmatch(r"pcm_[suf](8|16|24|32|64)(?:le|be)?(?:_planar)?", stream["codec_name"])
    return int(width[1]) if width else 0


def plan_hires_audio(stream):
    """Inspect ffprobe's first audio stream. Eligibility describes file specs only."""
    if not isinstance(stream, dict) or not stream.get("codec_name") or stream.get("codec_type", "audio") != "audio":
        raise HiresAudioError("没有可用音轨，请添加音频文件")
    codec = str(stream["codec_name"]).lower()
    rate = _positive_int(stream.get("sample_rate"))
    channels = _positive_int(stream.get("channels"))
    if not rate or not channels:
        raise HiresAudioError("无法读取音轨的采样率或声道数，请检查音频文件")
    bits = _precision(stream)
    layout = stream.get("channel_layout") or {1: "mono", 2: "stereo"}.get(channels, "")
    plan = {"codec": codec, "bits": bits, "sample_rate": rate, "channels": channels, "source_audio_ordinal": 0,
            "channel_layout": layout, "eligible": False, "platform_verified": False,
            "source_history_verified": False}
    if codec in _COPY:
        from quality import _atmos_evidence
        joc = codec == "eac3" and _atmos_evidence(stream)[0] == "confirmed"
        detail = ("保留现成 Dolby Atmos（E-AC-3 JOC）" if joc else
                  f"原样保留 {codec.upper()} 有损音轨，按普通音质导出")
        return dict(plan, action="copy", output_codec=codec, detail=detail, joc=joc)

    # WavPack also has a hybrid lossy mode. Its codec name alone cannot
    # establish lossless provenance. Preserve its decoded samples conservatively.
    linear_pcm = bool(re.fullmatch(r"pcm_[sfu](?:8|16|24|32|64)(?:le|be)?(?:_planar)?", codec))
    dts_ma = codec == "dts" and bool(re.search(r"\bDTS-HD MA\b", str(stream.get("profile", "")), re.I))
    if codec not in _LOSSLESS and codec != "wavpack" and not linear_pcm and not dts_ma:
        return dict(plan, action="aac", output_codec="aac", detail=f"{codec.upper()} 将转为普通 AAC 音轨")

    floating = codec.startswith("pcm_f") or (codec == "wavpack" and stream.get("sample_fmt") in {"flt", "fltp", "dbl", "dblp"})
    if not bits or bits > 64:
        raise HiresAudioError("无法确认无损源的实际位深。请提供带有位深信息的 WAV、FLAC 或 ALAC 母带")
    if floating and bits not in {32, 64}:
        raise HiresAudioError("浮点音轨位深异常，请检查 WAV 母带")
    if not floating and bits > 32:
        raise HiresAudioError("当前无损路线支持最高 32 bit 整数 PCM。请提供可无损表示的母带")

    # Apple Lossless in FFmpeg writes either 16 or 24 bits. For 32-bit
    # integer, float and layouts ALAC cannot represent, use MP4 linear PCM.
    if bits <= 24 and not floating and layout in _ALAC_LAYOUTS:
        action, output = "alac", "alac"
        out_bits = 16 if bits <= 16 else 24
    else:
        action = "pcm"
        out_bits = bits if floating else next(width for width in (16, 24, 32, 64) if width >= bits)
        output = f"pcm_{'f' if floating else 's'}{out_bits}le"
    eligible = not floating and codec != "wavpack" and bits >= 24 and rate >= 48000
    label = "ALAC" if action == "alac" else "PCM"
    spec = f"{rate / 1000:g} kHz / {bits} bit"
    if eligible:
        detail = f"保留 {spec} 母带，导出 {label}，音轨符合 B 站 Hi-Res 本地规格"
    else:
        detail = f"保留 {spec} 音频采样，导出 {label}，按普通无损音轨交付"
    if codec == "truehd":
        detail += "；保留解码后的声道，无法随 PCM 保留 TrueHD 对象元数据"
    if dts_ma and "DTS:X" in str(stream.get("profile", "")).upper():
        detail += "；保留解码后的声道，DTS:X 对象元数据不会随声道导出"
    return dict(plan, action=action, output_codec=output, output_bits=out_bits,
                pcm_codec=output if floating or out_bits > 32 else "pcm_s32le",
                eligible=eligible, detail=detail)


def choose_free_audio_stream(raw_streams):
    """Select the highest-priority usable track, breaking ties by source default."""
    streams = (s for s in raw_streams if isinstance(s, dict) and s.get("codec_type") == "audio")
    candidates = []
    errors = []
    for ordinal, stream in enumerate(streams):
        try:
            plan = plan_hires_audio(stream)
        except HiresAudioError as exc:
            errors.append(str(exc))
            continue
        plan["source_audio_ordinal"] = ordinal
        rank = 4 if plan.get("joc") else 3 if plan["eligible"] else {
            "alac": 2, "pcm": 2, "copy": 1, "aac": 0}[plan["action"]]
        default = (stream.get("disposition") or {}).get("default") in (1, "1", True)
        candidates.append(((rank, default, -ordinal), stream, plan))
    if not candidates:
        raise HiresAudioError(errors[0] if errors else "没有可用音轨，请添加音频文件")
    _, selected, plan = max(candidates, key=lambda item: item[0])
    return selected, plan


def _timeline(plan, duration, audio_offset):
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(audio_offset):
        raise HiresAudioError("无法确定音轨在视频中的起止时间")
    rate = plan["sample_rate"]
    length = round(duration * rate)
    if length < 1:
        raise HiresAudioError("视频时长未达到一个音频采样")
    offset = round(audio_offset * rate)
    filters = ["asetpts=PTS-STARTPTS"]
    if offset > 0:
        filters.append(f"adelay={min(length, offset)}S:all=1")
    elif offset < 0:
        # Pad before trimming so a source ending in the pre-roll yields silence.
        filters.extend(["apad", f"atrim=start_sample={-offset}", "asetpts=PTS-STARTPTS"])
    filters += ["apad", f"atrim=end_sample={length}"]
    return ",".join(filters)


def hires_encode_args(plan, duration, audio_offset=0):
    """Options following the first selected audio stream, preceding the MP4 path."""
    if plan["action"] not in {"alac", "pcm"}:
        raise HiresAudioError("该音轨使用普通音频处理路线")
    args = ["-af", _timeline(plan, duration, audio_offset), "-c:a", plan["output_codec"]]
    if plan["action"] == "alac":
        args += ["-sample_fmt", "s16p" if plan["output_bits"] == 16 else "s32p"]
    return args + ["-movie_timescale", str(plan["sample_rate"])]


def verify_hires_audio(ffmpeg, source, final_path, final_raw_stream, plan, duration, audio_offset, run):
    """Hash decoded source on the submitted timeline and all final PCM samples."""
    if (final_raw_stream.get("codec_name") != plan["output_codec"]
            or _precision(final_raw_stream) != plan["output_bits"]
            or _positive_int(final_raw_stream.get("sample_rate")) != plan["sample_rate"]
            or _positive_int(final_raw_stream.get("channels")) != plan["channels"]):
        raise HiresAudioError("成片无损音轨的编码、位深、采样率或声道数与源计划不一致")
    layout = final_raw_stream.get("channel_layout") or {1: "mono", 2: "stereo"}.get(plan["channels"], "")
    if plan["channel_layout"] and plan["channel_layout"] != layout:
        raise HiresAudioError("成片无损音轨改变了声道布局")
    try:
        start = float(final_raw_stream["start_time"])
    except (TypeError, KeyError, ValueError):
        raise HiresAudioError("无法确认成片无损音轨的起点") from None
    if not math.isfinite(start) or abs(start) > 1 / plan["sample_rate"]:
        raise HiresAudioError("成片无损音轨未从视频起点开始")

    def hash_pcm(path, ordinal, filters):
        command = [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", path, "-map", f"0:a:{ordinal}", "-vn"]
        if filters:
            command += ["-af", filters]
        command += ["-c:a", plan["pcm_codec"], "-f", "hash", "-hash", "sha256", "-"]
        match = re.search(r"^SHA256=([0-9a-f]{64})\s*$", run(command), re.I | re.M)
        if not match:
            raise HiresAudioError("无法读取完整音轨的 PCM 校验值")
        return match[1].lower()

    expected = hash_pcm(source, plan["source_audio_ordinal"], _timeline(plan, duration, audio_offset))
    actual = hash_pcm(final_path, 0, None)
    if actual != expected:
        raise HiresAudioError("最终音轨的逐样本校验失败，采样值或长度与源音轨在视频时间轴上的内容不同")
    return dict(plan, pcm_sha256=actual, pcm_verified=True, verified_samples=round(duration * plan["sample_rate"]),
                offset_samples=round(audio_offset * plan["sample_rate"]))
