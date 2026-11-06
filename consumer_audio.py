"""Select complementary delivery tracks and describe the final MP4 locally."""
from __future__ import annotations

import math
import os
import re

from hires_audio import HiresAudioError, plan_hires_audio, _timeline
from quality import read_audio_probe, validate_atmos_audio


def _candidates(streams, origin):
    choices = []
    errors = []
    for ordinal, stream in enumerate(s for s in streams if s.get("codec_type") == "audio"):
        try:
            plan = plan_hires_audio(stream)
        except HiresAudioError as exc:
            errors.append(str(exc))
            continue
        plan["source_audio_ordinal"] = ordinal
        choices.append({"origin": origin, "stream": stream, "plan": plan})
    if errors and not choices:
        raise HiresAudioError(errors[0])
    return choices


def _rank(track):
    plan, stream = track["plan"], track["stream"]
    special = 4 if _joc_candidate(track) else 3 if plan["eligible"] else {
        "alac": 2, "pcm": 2, "copy": 1, "aac": 0}[plan["action"]]
    return special, bool(stream.get("disposition", {}).get("default"))


def _joc_candidate(track):
    plan = track["plan"]
    if not plan.get("joc") or plan["channels"] not in (6, 8):
        return False
    # Matroska frequently omits track bitrate. Probe again after MP4 muxing.
    try:
        bitrate = int(track["stream"].get("bit_rate") or 0)
    except ValueError:
        bitrate = 0
    return not bitrate or bitrate >= 320000


def _matches_program(track, other):
    streams = (track["stream"], other["stream"])
    languages = [s.get("tags", {}).get("language", "").lower() for s in streams]
    if all(lang not in ("", "und", "zxx") for lang in languages) and languages[0] != languages[1]:
        return False
    # Avoid combining known director's commentary or descriptive audio with
    # the main mix just because the alternate track has better technical specs.
    for key in ("comment", "visual_impaired", "hearing_impaired"):
        if bool(streams[0].get("disposition", {}).get(key)) != bool(streams[1].get("disposition", {}).get(key)):
            return False
    return True


def select_tracks(video_streams, external_streams=None):
    """Use the explicit replacement if supplied; pair only complementary formats."""
    if external_streams is None:
        choices = _candidates(video_streams, "video")
    else:
        choices = _candidates(external_streams, "external")
    if not choices:
        if external_streams is not None:
            raise HiresAudioError("外置声音文件中没有音轨")
        return []
    primary = max(choices, key=_rank)
    if not (_joc_candidate(primary) or primary["plan"]["eligible"]):
        return [primary]
    others = list(choices)
    if external_streams is not None:
        try:
            others.extend(_candidates(video_streams, "video"))
        except HiresAudioError:
            pass  # The explicit replacement is usable on its own.
    def complementary(other):
        return other["plan"]["eligible"] if _joc_candidate(primary) else _joc_candidate(other)
    matches = [item for item in others if complementary(item) and _matches_program(primary, item)]
    if not matches:
        if (_joc_candidate(primary) and primary["plan"]["channel_layout"] in _SURROUNDS
                and primary["plan"]["sample_rate"] == 48000):
            # This is a new, separate stereo mix, not a lossless original.
            plan = dict(primary["plan"], action="stereo_master", output_codec="alac", output_bits=24,
                        detail="制作 48 kHz / 24 bit 立体声降混母版，同时保留原 Dolby Atmos")
            return [dict(primary, plan=plan), primary]
        return [primary]
    alternate = max(matches, key=lambda item: (item["origin"] == primary["origin"], _rank(item)))
    # Preserve a broadly compatible default mix and keep JOC as an alternative.
    return [primary, alternate] if primary["plan"]["eligible"] else [alternate, primary]


_SURROUNDS = {"5.1": ("BL", "BR"), "5.1(side)": ("SL", "SR"),
              "7.1": ("SL+0.70710678*BL", "SR+0.70710678*BR")}


def downmix_filter(plan, duration, offset):
    left, right = _SURROUNDS[plan["channel_layout"]]
    # Lo/Ro: left and right mains, centre shared equally, same-side surrounds.
    # Omit the dedicated LFE channel. '<' normalizes each output row so that
    # coincident full-scale inputs cannot overload the mix.
    return (_timeline(plan, duration, offset) + ",aformat=sample_fmts=fltp,"
            f"pan=stereo|FL<FL+0.70710678*FC+0.70710678*{left}"
            f"|FR<FR+0.70710678*FC+0.70710678*{right},aformat=sample_fmts=s32p")


def verify_downmix(job, ffmpeg, final_path, stream):
    length = job.manifest["duration"]
    if (stream.get("codec_name") != "alac" or str(stream.get("bits_per_raw_sample")) != "24" or
            str(stream.get("sample_rate")) != "48000" or stream.get("channels") != 2):
        raise HiresAudioError("立体声降混母版的编码、精度或声道数校验失败")
    def pcm_hash(path, ordinal, filters=()):
        result = job._run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", path,
                           "-map", f"0:a:{ordinal}", *filters, "-c:a", "pcm_s24le",
                           "-f", "hash", "-hash", "sha256", "-"])
        match = re.search(r"SHA256=([0-9a-f]{64})", result, re.I)
        if not match:
            raise HiresAudioError("无法校验立体声母版的完整 PCM")
        return match[1].lower()
    graph = downmix_filter(job.free_audio, length, job.audio_offset)
    intended = pcm_hash(job.audio_source, job.free_audio["source_audio_ordinal"], ("-af", graph))
    if pcm_hash(final_path, 0) != intended:
        raise HiresAudioError("最终立体声母版与源音轨的参考降混样本不符")
    return intended


def summary(selected, streams, free_audio):
    if len(selected) != len(streams):
        raise HiresAudioError("成片音轨数量与一键投稿计划不符")
    tracks = []
    for ordinal, (item, stream) in enumerate(zip(selected, streams)):
        info = read_audio_probe({"streams": [stream]})
        source = item["plan"]
        joc = bool(source.get("joc") and validate_atmos_audio(info, strict_sample_rate=False)["status"] != "fail")
        hires = bool(ordinal == 0 and free_audio and free_audio.get("eligible") and free_audio.get("pcm_verified"))
        tracks.append({"ordinal": ordinal, "origin": item["origin"], "source_audio_ordinal": source["source_audio_ordinal"],
                       "codec": info["audio_codec"], "channels": info["audio_channels"], "sample_rate": info["audio_sample_rate"],
                       "start_seconds": float(stream.get("start_time") or 0), "duration_seconds": info["audio_duration"],
                       "atmos": joc, "hires": hires})
    atmos = any(t["atmos"] for t in tracks)
    hires = any(t["hires"] for t in tracks)
    detail = ("已封装两条音轨：Hi-Res 规格无损、Dolby Atmos" if atmos and hires else
              "已保留 Dolby Atmos 音轨" if atmos else "已保留 Hi-Res 规格无损音轨" if hires else
              (free_audio or {}).get("detail", "源视频为静音，本次导出画面"))
    return {"atmos": atmos, "hires": hires, "audio_tracks": len(tracks), "tracks": tracks,
            "detail": detail, "platform_verified": False}


def prepare_companion(job, primary, ffmpeg):
    """Attach a second mix only after a codec and payload check on this machine."""
    from atmos_encoding import backend_status, backend_identity, AtmosError
    from pipeline import PipelineError, PipelineCancelled
    if not primary or not job.consumer_tracks or (len(job.consumer_tracks) == 1 and job.free_audio.get("joc")):
        return
    duration = job.manifest["duration"]
    if len(job.consumer_tracks) == 2:
        track = job.consumer_tracks[1]
        source = job.cfg["video"] if track["origin"] == "video" else job.cfg["audio"]
        start = 0.0
        args = [ffmpeg, "-nostdin", "-v", "error", "-y"]
        if track["origin"] == "video":
            video_start = float(job.info["stream"].get("start_time") or 0)
            start = float(track["stream"].get("start_time") or 0) - video_start
            if start < 0:
                seek = video_start - float(job.info["format"].get("start_time") or 0)
                if not math.isfinite(seek) or seek < 0:
                    raise PipelineError("无法定位附加 Atmos 音轨的前滚位置")
                args += ["-ss", f"{seek:.9f}"]
        job.companion_offset = max(start, 0.0)
        remaining = duration - job.companion_offset
        if not math.isfinite(remaining) or remaining <= 0:
            job.consumer_tracks.pop()
            job.consumer_atmos_note = "Atmos 音轨与画面时间轴没有交集"
            return
        companion = job.job_dir / "consumer-source-joc.mp4"
        job._run(args + ["-i", source, "-map", f"0:a:{track['plan']['source_audio_ordinal']}",
                         "-c:a", "copy", "-t", f"{remaining:.9f}", "-map_metadata", "-1", companion])
        candidate = job._inspect(companion, audio_only=True)
        if validate_atmos_audio(candidate, strict_sample_rate=False)["status"] == "fail":
            job.consumer_tracks.pop()
            job.consumer_atmos_note = "附加音轨未通过本地 Atmos 交付检查"
            return
        job.companion_audio = companion
        return

    # Look for an encoder configured by the machine owner. Do not discover
    # cloud credentials or ask the desktop user for an account.
    job.consumer_dee = {"atmos_backend": "dee",
                        "dee_path": job.cfg.get("dee_path") or os.getenv("DOLBY_DEE_PATH", ""),
                        "dee_profile": job.cfg.get("dee_profile") or os.getenv("DOLBY_DEE_PROFILE", "")}
    if not backend_status(job.consumer_dee)["available"]:
        job.consumer_atmos_note = "缺少匹配的 Atmos 音轨或可用的本地授权编码服务"
        return
    try:
        job.atmos_backend_identity = backend_identity(job.consumer_dee, job._boundary)
        offset = 0
        if (job.free_audio["action"] == "copy" and not job.manifest.get("music_sr", {}).get("applied")
                and not job.manifest.get("ai_denoise", {}).get("applied")):
            offset = job.audio_offset + job.manifest.get("audio_trimmed_preroll_seconds", 0)
        job.companion_audio = job._generate_atmos_audio(duration, companion_source=primary, companion_offset=offset)
    except PipelineCancelled:
        raise
    except (PipelineError, AtmosError, OSError, ValueError) as exc:
        job.consumer_atmos_note = "本地 Atmos 编码未通过，已保留可用主音轨"
        job.log("[Atmos] " + job.consumer_atmos_note + "：" + str(exc))
        return
    stream = job._inspect(job.companion_audio, audio_only=True)["audio_streams"][0]
    job.consumer_tracks.append({"origin": "local_dee", "stream": stream,
                                "plan": dict(job.free_audio, joc=True, eligible=False)})


def verify_delivery(job, partial, media, free_audio, ffmpeg):
    result = summary(job.consumer_tracks, media["audio_streams"], free_audio)
    if job.companion_audio:
        peer_hash = job._audio_payload_hash(job.companion_audio)
        if peer_hash != job._audio_payload_hash(partial, 1) or not result["tracks"][1]["atmos"]:
            raise HiresAudioError("第二条 Atmos 音轨的交付参数或码流未通过验证")
        result["tracks"][1]["payload_sha256"] = peer_hash
        start = float(media["audio_streams"][1].get("start_time") or 0)
        if abs(start - job.companion_offset) > 0.034:
            raise HiresAudioError("第二条 Atmos 音轨的起点未对齐")
    result["native_hires"] = result["hires"]
    result["master_kind"] = None
    first = media["audio_streams"][0] if media["audio_streams"] else {}
    if free_audio and free_audio["action"] == "stereo_master":
        result["tracks"][0]["mix_pcm_sha256"] = verify_downmix(job, ffmpeg, partial, first)
        result["master_kind"] = "atmos_stereo_downmix"
        result["detail"] = "已制作 48 kHz / 24 bit 立体声发行母版，来源为有损 Atmos 环绕声降混"
    sr = job.manifest.get("music_sr") or {}
    if (sr.get("applied") and sr.get("delivery_hi_res") and first.get("codec_name") == "alac"
            and str(first.get("bits_per_raw_sample")) == "24" and int(first.get("sample_rate") or 0) >= 48000):
        result["master_kind"] = "ai_bandwidth_extension"
        result["detail"] = "已制作 Hi-Res 规格的 AI 重制音轨，来源为带宽增强后的声音"
    result["mastered"] = result["master_kind"] is not None
    result["hires_delivery"] = result["hires"] = result["native_hires"] or result["mastered"]
    if result["mastered"]:
        result["tracks"][0]["hires"] = True
        if result["atmos"]:
            result["detail"] += "；另有经验证的 Dolby Atmos 音轨"
    result["generated_atmos"] = any(t["origin"] == "local_dee" for t in result["tracks"])
    result["atmos_note"] = job.consumer_atmos_note
    return result
