"""Prepare real immersive PCM and invoke an explicitly configured JOC encoder.

This module never labels an upmix, an E-AC-3 encode, or a successful process exit
as Atmos. The caller must inspect and decode the resulting JOC bitstream.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
from pathlib import Path

MIX_VERSION = "front-preserving-ambient-v2"
CBI_10 = ("L", "R", "C", "LFE", "Ls", "Rs", "Ltf", "Rtf", "Ltr", "Rtr")
CBI_16 = ("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs", "Lw", "Rw", "Ltf", "Rtf", "Ltm", "Rtm", "Ltr", "Rtr")


class AtmosError(RuntimeError):
    pass


def file_sha256(path, check=lambda: None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            check()
            digest.update(block)
    return digest.hexdigest()


def load_dee_profile(path):
    """Load argv copied from the installed, licensed DEE version's documentation.

    No shell, environment overrides, credential fields, guessed flags or legacy
    XML generation. A profile describes an installation; it is not a license.
    """
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 65536:
        raise AtmosError("请选择本机 DEE 的编码配置 JSON（最大 64KB）")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise AtmosError("无法读取 DEE 编码配置 JSON") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise AtmosError("DEE 编码配置需要 schema_version: 1")
    if set(data) - {"schema_version", "argv", "output_suffix", "input_layout", "description"}:
        raise AtmosError("DEE 配置包含不支持的字段；不要在配置中保存账号或密钥")
    if data.get("input_layout") != "5.1.4":
        raise AtmosError("本地 DEE 配置须接受 5.1.4 交错 PCM WAV，不能把普通 WAV 当成 ADM")
    args = data.get("argv")
    if not isinstance(args, list) or not args or len(args) > 128:
        raise AtmosError("DEE argv 须为参数数组，请依据已安装版本的官方命令配置")
    joined = []
    for arg in args:
        if not isinstance(arg, str) or not arg or len(arg) > 4096 or "\0" in arg:
            raise AtmosError("DEE argv 包含无效参数")
        tokens = re.findall(r"\{([^{}]*)\}", arg)
        if any(token not in ("input", "output", "temp") for token in tokens):
            raise AtmosError("DEE 配置仅支持 {input}、{output}、{temp} 占位符")
        if "{" in re.sub(r"\{(?:input|output|temp)\}", "", arg) or "}" in re.sub(r"\{(?:input|output|temp)\}", "", arg):
            raise AtmosError("DEE 配置的占位符不完整")
        joined.append(arg)
    if not all(any("{" + key + "}" in arg for arg in joined) for key in ("input", "output")):
        raise AtmosError("DEE argv 必须包含 {input} 和 {output}")
    if data.get("output_suffix", ".ec3") not in (".ec3", ".eac3", ".mp4"):
        raise AtmosError("DEE 配置须输出 .ec3 / .eac3 / .mp4")
    return data


def _dee_executable(cfg):
    supplied = str(cfg.get("dee_path") or "").strip()
    if supplied:
        path = Path(supplied).expanduser().resolve()
    else:
        folder = Path(__file__).resolve().parent / "tools"
        path = next((item for item in (folder / "dee_ddpjoc_encoder.exe", folder / "dee_ddpjoc_encoder") if item.is_file()), None)
        if path is None:
            found = shutil.which("dee_ddpjoc_encoder")
            path = Path(found).resolve() if found else None
    if path is None or not path.is_file():
        raise AtmosError("未配置真实 Atmos 编码器。请在「Atmos 编码设置」选择已授权的 DEE，或配置 AWS MediaConvert；FFmpeg 不能代替 JOC 编码器")
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise AtmosError("本地编码器请选择 DEE 的 .exe，不能使用脚本或命令字符串")
    return path


def backend_status(cfg):
    """Cheap local-only UI check: no subprocess, network or credential discovery."""
    try:
        backend = cfg.get("atmos_backend", "dee")
        if backend == "dee":
            _dee_executable(cfg)
            if not cfg.get("dee_profile"):
                raise AtmosError("已找到本地编码器，还需导入对应 DEE 版本的编码配置；存在程序文件不代表许可证有效")
            load_dee_profile(cfg["dee_profile"])
            message = "本地 DEE 路径与编码配置就绪；运行时验证许可证和真实 JOC 输出"
        elif backend == "aws":
            from atmos_cloud import CloudAtmosConfig
            cloud = CloudAtmosConfig(**cfg.get("cloud_atmos", {}))
            cloud.validate()
            if importlib.util.find_spec("boto3") is None:
                raise AtmosError("尚未安装云编码组件，请重新运行 setup.bat")
            message = "云编码配置就绪；开始任务将上传 PCM 母版并按 AWS 计费，运行时验证账号权限"
        else:
            raise AtmosError("请选择 DEE 或 AWS MediaConvert Atmos 编码器")
        return {"available": True, "message": message}
    except (RuntimeError, ValueError, TypeError, OSError, ImportError) as exc:
        return {"available": False, "message": str(exc)}


def backend_identity(cfg, check=lambda: None):
    status = backend_status(cfg)
    if not status["available"]:
        raise AtmosError(status["message"])
    backend = cfg.get("atmos_backend", "dee")
    result = {"backend": backend, "mix_version": MIX_VERSION}
    if backend == "dee":
        result.update(executable_sha256=file_sha256(_dee_executable(cfg), check),
                      profile_sha256=file_sha256(cfg["dee_profile"], check))
    else:
        # Bind a resumed job to the same account selection and destination,
        # without serializing credentials, tokens or authorization state.
        config = {k: v for k, v in cfg.get("cloud_atmos", {}).items() if k != "allow_upload"}
        result["config_sha256"] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return result


def _offset_samples(audio_offset):
    try:
        seconds = float(audio_offset)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AtmosError("音频相对画面的起始偏移必须为有限秒数") from exc
    if not math.isfinite(seconds) or not math.isfinite(seconds * 48000):
        raise AtmosError("音频相对画面的起始偏移必须为有限秒数")
    return round(seconds * 48000)


def master_filter(channels, layout, duration, output_channels=10, audio_offset=0.0):
    """A conservative creative upmix, not recovery of original sound objects.

    Front channels stay in place. Stereo difference / existing surround energy
    supplies quiet, band-limited ambience. Mono produces no invented height
    movement. Known multichannel layouts retain their bed; unknown ordering is
    rejected rather than silently assigning sounds to the wrong speakers.
    """
    if not math.isfinite(duration) or duration <= 0:
        raise AtmosError("空间混音需要有效的视频时长")
    if output_channels not in (10, 16):
        raise AtmosError("空间母版须为 5.1.4 或 9.1.6")
    offset = _offset_samples(audio_offset)
    layout = str(layout or "").lower()
    channels = int(channels)
    surround_left = surround_right = "0*c0"
    rear_left = rear_right = "0*c0"
    if channels == 1 and layout in ("", "mono", "1 channels"):
        bed = ["0*c0", "0*c0", "0.9*c0", "0*c0", "0*c0", "0*c0"]
    elif channels == 2 and layout in ("", "stereo", "2 channels"):
        bed = ["0.9*c0", "0.9*c1", "0*c0", "0*c0", "0.12*c0-0.12*c1", "0.12*c1-0.12*c0"]
        surround_left, surround_right = "0.07*c0-0.07*c1", "0.07*c1-0.07*c0"
    elif channels == 6 and layout in ("5.1", "5.1(side)"):
        bed = [f"0.9*c{i}" for i in range(6)]
        surround_left, surround_right = "0.12*c4", "0.12*c5"
    elif channels == 8 and layout == "7.1":
        # FFmpeg native 7.1 order: FL FR FC LFE BL BR SL SR.
        bed = [f"0.9*c{i}" for i in range(4)] + ["0.9*c6", "0.9*c7"]
        surround_left, surround_right = "0.06*c4+0.06*c6", "0.06*c5+0.06*c7"
        if output_channels == 16:
            rear_left, rear_right = "0.9*c4", "0.9*c5"
        else:
            # Documented downmix with bounded sum, rather than discarding rears.
            bed[4:6] = ["0.53*c6+0.37*c4", "0.53*c7+0.37*c5"]
    else:
        raise AtmosError(f"尚不支持自动空间混音的声道顺序：{channels} / {layout or '未知'}；请先导出 mono、stereo、5.1 或 7.1 标准 PCM")
    frames = round(duration * 48000)
    timing = "aresample=48000,asetpts=PTS-STARTPTS,"
    if offset > 0:
        # Uppercase S is FFmpeg's sample unit; milliseconds would round away
        # precise source offsets. Work after resampling so this means 48 kHz.
        timing += f"adelay={offset}S:all=1,"
    elif offset < 0:
        # Padding before trimming also produces silence when the entire input
        # precedes the video. Padding only after an empty trim can yield no PCM.
        timing += f"apad,atrim=start_sample={-offset},asetpts=PTS-STARTPTS,"
    head = f"[0:a:0]{timing}apad,atrim=end_sample={frames},asplit=3[bed][frontheight][rearheight]"
    zero = "0*c0"
    if output_channels == 10:
        bed_channels = bed
    else:
        bed_channels = bed + [rear_left, rear_right, zero, zero]
    pan_bed = "pan=" + str(len(bed_channels)) + "c|" + "|".join(f"c{i}={v}" for i, v in enumerate(bed_channels))
    stereo = f"pan=stereo|c0={surround_left}|c1={surround_right},highpass=f=350,lowpass=f=7000"
    sections = [head, f"[bed]{pan_bed}[b]",
                f"[frontheight]{stereo},adelay=11:all=1,atrim=end_sample={frames}[hf]",
                f"[rearheight]{stereo},volume=0.65,adelay=23:all=1,atrim=end_sample={frames}[hr]",
                "[b][hf][hr]amerge=inputs=3[merged]"]
    if output_channels == 16:
        # 10 bed + 2 front heights + 2 rear heights -> insert silent middle heights.
        order = list(range(12)) + [None, None, 12, 13]
        sections.append("[merged]pan=16c|" + "|".join(f"c{i}=" + (f"c{v}" if v is not None else zero) for i, v in enumerate(order)) + "[out]")
    else:
        sections.append("[merged]anull[out]")
    return ";".join(sections)


def prepare_master(ffmpeg, source, stream, duration, output, run, output_channels=10, audio_offset=0.0):
    graph = master_filter(stream.get("channels", 0), stream.get("channel_layout"), duration, output_channels, audio_offset)
    run([ffmpeg, "-nostdin", "-v", "warning", "-y", "-i", str(source),
         "-filter_complex", graph, "-map", "[out]", "-ar", "48000", "-c:a", "pcm_s24le",
         "-rf64", "auto", "-map_metadata", "-1", str(output)])
    return {"method": MIX_VERSION, "input_channels": stream.get("channels"),
            "input_layout": stream.get("channel_layout", "unknown"), "channels": output_channels,
            "channel_order": list(CBI_10 if output_channels == 10 else CBI_16),
            "sample_rate": 48000, "bits_per_sample": 24,
            "offset_samples": _offset_samples(audio_offset),
            "samples": round(duration * 48000),
            "description": "保留前方声像；从立体声差分或已有环绕声生成低增益环境声。单声道保留中置，不虚构空间移动。此 PCM 母版本身不是 Atmos。"}


def encode_dee(cfg, master, directory, run):
    executable = _dee_executable(cfg)
    profile = load_dee_profile(cfg["dee_profile"])
    directory = Path(directory).resolve()
    output = directory / ("encoded" + profile.get("output_suffix", ".ec3"))
    output.unlink(missing_ok=True)
    values = {"input": str(Path(master).resolve()), "output": str(output), "temp": str(directory)}
    args = [str(executable)] + [arg.format_map(values) for arg in profile["argv"]]
    # Do not log the full local command; encoder profiles can reference a license.
    run(args, cwd=directory, log_command=False)
    if not output.is_file() or not output.stat().st_size:
        raise AtmosError("DEE 未生成音频；请检查许可证及该版本的官方编码配置")
    return output, {"backend": "dee", "generated": True, "license_checked_by_encoder": True,
                    "encoder_exit_success": True, "platform_verified": False}
