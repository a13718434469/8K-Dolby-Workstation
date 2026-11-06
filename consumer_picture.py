"""Pick a picture route for the desktop and CLI Bilibili export."""
from fractions import Fraction
from math import ceil
from pathlib import Path

from quality import assess_bilibili


def plan_picture(source):
    rate = Fraction(str(source["fps_fraction"]))
    if rate <= 0:
        raise ValueError("源视频缺少有效帧率，请选择可正常播放的原视频")
    width = int(source.get("display_width") or source["width"])
    height = int(source.get("display_height") or source["height"])
    scale = min(1.0, 3840 / max(width, height), 2160 / min(width, height))
    w, h = (max(2, int(v * scale) // 2 * 2) for v in (width, height))
    stream = source.get("stream", {})
    transfer = source.get("color_transfer", "unknown")
    wide = source.get("color_primaries") == "bt2020" or source.get("color_space") in ("bt2020nc", "bt2020c")
    hdr = transfer in ("arib-std-b67", "smpte2084") or bool(source.get("dv_profile")) or wide
    interlaced = stream.get("field_order") in ("tt", "bb", "tb", "bt")
    # Some VFR containers expose an averaged rate very different from the nominal time grid.
    try:
        nominal = Fraction(stream.get("r_frame_rate") or rate)
    except (ValueError, ZeroDivisionError):
        nominal = rate
    stable = float(abs(nominal - rate) / rate) < 0.002
    pix_fmt = source.get("pix_fmt", "")
    motion_input = source.get("bit_depth") == 8 and pix_fmt in (
        "yuv420p", "yuv422p", "yuv444p", "yuvj420p", "yuvj422p", "yuvj444p", "nv12", "nv21")
    target = rate / ceil(rate / 60) if rate > 60 else rate
    suitable = (not interlaced and scale == 1 and rate <= 60
                and assess_bilibili(source)["status"] != "fail")
    route = "preserve" if suitable else "transcode"
    method = "keep"
    detail = "沿用源帧率，在 10-bit 画面上制作杜比视界"
    error = None
    if source.get("dv_profile") == 5:
        error = "此杜比视界原片采用 Profile 5 专用色彩。请提供同一视频的 HLG、HDR10 或 SDR 版本后重新拖入。"
    elif wide and transfer in (None, "", "unknown", "unspecified", "reserved"):
        error = "原视频使用广色域，但缺少区分 HLG 和 HDR10 的色彩信息。请提供保留 HDR 信息的原始视频。"
    elif suitable:
        method = "preserve"
        detail = "原片已有合规的杜比视界 8.4，保留原画面、动态元数据与帧率"
    elif interlaced:
        target = rate * 2 if rate <= 30 else target
        method = "deinterlace_fields" if rate <= 30 else "deinterlace"
        detail = "按原场序去隔行，恢复运动画面的时间顺序"
    elif rate > 60:
        method = "decimate"
        detail = "按原帧率的整数分频选帧，保留运动节奏"
    elif not stable:
        detail = "源视频帧间隔不规则，按原平均节奏制作"
    elif hdr or (source.get("bit_depth") or 0) > 8:
        detail = "沿用原帧率，保留高精度画面后制作杜比视界"
    elif 20 <= rate <= 30 and motion_input and w >= 64 and h >= 48:
        target = rate * 2
        method = "motion_interpolation"
        detail = "按运动轨迹生成中间帧，切镜时保持剪辑边界"
    plan = {"method": method, "source_fps": str(rate), "output_fps": str(target),
            "detail": error or detail}
    if error:
        plan["error"] = error
    cfg = {"route": route, "delivery": "bilibili", "color_mode": "dv84", "rpu_mode": "auto",
           "hdr_remaster": "auto", "width": w, "height": h, "target_fps": str(target),
           "picture_plan": plan, "ai": False, "interp": False}
    if target != rate and route != "preserve":
        cfg["fps_override"] = str(target)
    return cfg


def make_job(source_info, video, output=None, audio="", workspace=None):
    cfg = plan_picture(source_info)
    if cfg["picture_plan"].get("error"):
        raise ValueError(cfg["picture_plan"]["error"])
    if not output:
        source = Path(video).resolve()
        output = source.with_name(source.stem + "_B站投稿.mp4")
        number = 2
        while output.exists():
            output = source.with_name(f"{source.stem}_B站投稿_{number}.mp4")
            number += 1
    cfg.update(video=str(video), output=str(output), audio=str(audio or ""),
               bili_one_click=True, audio_mode="hires", ai_denoise=False,
               music_sr=False, require_atmos=False, preset="fast", crf=18)
    if workspace:
        cfg["workspace"] = str(workspace)
    return cfg


def cadence_filter(plan, rate, duration):
    """Filter after any resize, before high-precision tone/color processing."""
    method = plan.get("method")
    if method == "motion_interpolation":
        # minterpolate needs neighboring frames. Supply its lookahead at EOF
        # and trim to the source timeline rather than losing the final frames.
        count = max(1, round(Fraction(str(duration)) * rate))
        return (f"tpad=stop_mode=clone:stop=3,minterpolate=fps={rate}:mi_mode=mci:"
                f"mc_mode=obmc:me=epzs:search_param=16:scd=fdiff,trim=end_frame={count},")
    if method in ("deinterlace", "deinterlace_fields"):
        mode = "send_field" if method == "deinterlace_fields" else "send_frame"
        return f"bwdif=mode={mode}:parity=auto:deint=all,fps={rate},"
    return f"fps={rate},"
