"""Pick a picture route for the desktop and CLI Bilibili export."""
from fractions import Fraction
from math import ceil
from pathlib import Path

from quality import assess_bilibili


def supports_8k_dolby_level(stream, width, height, rate):
    # FFmpeg's Dolby Vision level table allows 7680-wide images from level 11.
    # The level 11 pixel rate is equivalent to 8K30; level 12 allows 8K60.
    pixel_rate = width * height * rate
    if width <= 3840 and pixel_rate <= 3840 * 2160 * 120:
        return True
    record = next((d for d in stream.get("side_data_list", [])
                   if d.get("side_data_type") == "DOVI configuration record"), {})
    required = 12 if pixel_rate > 7680 * 4320 * 30 else 11
    return required <= (record.get("dv_level") or 0) <= 13


def _delivery_size(source):
    """Return the oriented 8K canvas and the aspect-preserving active picture."""
    width = int(source.get("display_width") or source["width"])
    height = int(source.get("display_height") or source["height"])
    if min(width, height) <= 0:
        raise ValueError("无法读取源画面尺寸")
    try:
        sar = Fraction(str(source.get("stream", {}).get("sample_aspect_ratio") or "1:1").replace(":", "/"))
        if sar <= 0:
            sar = Fraction(1)
    except (ValueError, ZeroDivisionError):
        sar = Fraction(1)
    if source.get("rotation") in (90, 270):
        sar = 1 / sar
    display_w, display_h = width * sar, Fraction(height)
    factor = min(7680 / max(display_w, display_h), 4320 / min(display_w, display_h))
    active = tuple(max(2, 2 * round(side * factor / 2)) for side in (display_w, display_h))
    canvas = (7680, 4320) if display_w >= display_h else (4320, 7680)
    return canvas, active


def plan_picture(source):
    rate = Fraction(str(source["fps_fraction"]))
    if rate <= 0:
        raise ValueError("源视频缺少有效帧率，请选择可正常播放的原视频")
    width = int(source.get("display_width") or source["width"])
    height = int(source.get("display_height") or source["height"])
    (w, h), active = _delivery_size(source)
    padded = active != (w, h)
    resized = (w, h) != (width, height) or padded
    upscale = resized and active[0] * active[1] > width * height
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
    suitable = (not interlaced and not resized and rate <= 60
                and supports_8k_dolby_level(stream, source["width"], source["height"], rate)
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
    elif 20 <= rate <= 30 and motion_input and width >= 64 and height >= 48:
        target = rate * 2
        method = "motion_interpolation"
        detail = "按运动轨迹生成中间帧，切镜时保持剪辑边界"
    if resized:
        detail += f"；源画面 {width}×{height}，等比{'放大' if upscale else '缩放'}至 {active[0]}×{active[1]}"
        if padded:
            detail += f"，居中补黑边至 {w}×{h}"
        if source.get("dv_profile"):
            detail += "，按新画面重新生成杜比元数据"
    neural = (upscale and motion_input and not hdr and min(width, height) >= 32
              and max(width, height) <= 1920 and min(width, height) <= 1080)
    sr = ("尝试使用 Real-ESRGAN 通用模型做 4 倍画面超分，再适配成片尺寸" if neural else
          "沿用高精度缩放，保留源信号精度" if resized else "保留原画面尺寸")
    plan = {"method": method, "source_fps": str(rate), "output_fps": str(target),
            "source_size": [width, height], "output_size": [w, h],
            "active_size": list(active),
            "resize": "upscale" if upscale else "fit" if resized else "native",
            "sr_method": "realesrgan_x4plus" if neural else "native",
            "sr_detail": sr, "detail": error or (detail + "；" + sr)}
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
    """Generate the target cadence before color conversion and UHD upscaling."""
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
