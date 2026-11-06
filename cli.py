"""Command-line interface, usable without PyQt5."""
import argparse
from fractions import Fraction
import signal
import sys

from pipeline import Pipeline, PipelineCancelled, WORKSPACE, load_checkpoint


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dolby Workstation — 原片保留、原生高精度转码与可选 AI 增强")
    parser.add_argument("-i", "--input", help="输入视频")
    parser.add_argument("-o", "--output", help="新的 MP4 输出路径；不会覆盖已有文件")
    parser.add_argument("--route", choices=("preserve", "transcode", "enhance"), default="preserve", help="默认原片保留")
    parser.add_argument("--delivery", choices=("general", "bilibili"), default="general")
    parser.add_argument("--auto-dolby", action="store_true", help="自动制作 B站杜比视界 8.4；合规原片直接保留，其他素材自动分析生成元数据（4K60以内）")
    parser.add_argument("--auto-double-dolby", action="store_true", help="自动制作杜比视界 8.4 + 真正 Atmos；普通音轨需要已配置的 Atmos 编码器，不允许降级为普通音频")
    parser.add_argument("--auto-free", action="store_true", help="免费本地制作杜比视界 8.4；保留现成 Atmos、无损母版或普通原声音质，不使用云编码")
    parser.add_argument("--bili-one-click", action="store_true", help="一键制作 B站投稿 MP4，自动选择帧率、杜比视界及可用音轨")
    parser.add_argument("--ai-denoise", action="store_true", help="免费模式可选：用本地神经网络减轻普通单/双声道对白中的噪声")
    parser.add_argument("--music-sr", action="store_true", help="用本地模型补充带宽不足的音乐和视频原声的高频细节")
    ai = parser.add_mutually_exclusive_group()
    ai.add_argument("--ai", action="store_true", help="增强路线主动启用 Real-ESRGAN")
    ai.add_argument("--no-ai", action="store_true", help="禁用 Real-ESRGAN（默认）")
    interp = parser.add_mutually_exclusive_group()
    interp.add_argument("--interp", action="store_true", help="增强路线主动启用 RIFE")
    interp.add_argument("--no-interp", action="store_true", help="不使用 RIFE（默认）")
    parser.add_argument("--fps", help="可选目标帧率，支持 60000/1001；默认沿用源帧率")
    parser.add_argument("--size", help="可选输出宽x高；默认沿用源尺寸")
    parser.add_argument("--work-size", default="1920x1080", help="AI SDR 工作尺寸上限")
    parser.add_argument("--color", choices=("source", "sdr", "hdr10", "hlg", "dv81", "dv84"), default="source")
    parser.add_argument("--rpu", default="", help="与最终画面、色彩和帧数匹配的 Profile 8.1 / 8.4 RPU.bin")
    parser.add_argument("--rpu-mode", choices=("auto", "external"), help="杜比元数据：auto 自动逐帧分析；external 使用 --rpu，默认按是否提供 RPU 自动选择")
    parser.add_argument("--hdr-remaster", choices=("auto", "off"), help="auto 对普通 SDR 扩展 HDR 亮度；HDR 原片不二次扩展，默认按制作模式选择")
    parser.add_argument("--hdr-peak-nits", type=int, choices=range(100, 1001), metavar="100..1000", help="SDR 自动 HDR 制作的峰值亮度，默认 600 nits")
    parser.add_argument("--source-transfer", choices=("auto", "bt709", "arib-std-b67", "smpte2084"), help="源动态范围解释：自动、SDR BT.709、HLG 或 PQ；缺少 BT.2020 动态范围标记时需明确选择")
    parser.add_argument("--audio", default="", help="可选外部音频")
    parser.add_argument("--audio-mode", choices=("hires", "atmos_auto", "copy", "aac", "eac3", "none"), default="copy")
    parser.add_argument("--require-atmos", action="store_true", help="仅在最终文件确认包含真正 Atmos 时成功")
    parser.add_argument("--atmos-backend", choices=("dee", "aws"), default="dee", help="Atmos 编码后端；默认本地已授权 DEE")
    parser.add_argument("--dee-path", default="", help="完整 DEE 编码程序路径")
    parser.add_argument("--dee-profile", default="", help="与本机已授权 DEE 版本匹配的 JSON 命令配置文件")
    parser.add_argument("--aws-region", default="", help="AWS MediaConvert 区域")
    parser.add_argument("--aws-bucket", default="", help="音频母版及编码输出使用的私有 S3 存储桶")
    parser.add_argument("--aws-role-arn", default="", help="MediaConvert 服务角色 ARN")
    parser.add_argument("--aws-profile", default="", help="本机 AWS 凭证配置名；不接受明文密钥")
    parser.add_argument("--aws-prefix", default="dolby-workstation", help="本任务临时对象目录前缀")
    parser.add_argument("--allow-cloud-upload", action="store_true", help="本次明确允许上传音频母版并产生 AWS 费用；恢复云端任务也需明确传入")
    parser.add_argument("--model", default="realesrgan-x4plus")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--preset", choices=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"), default="medium")
    parser.add_argument("--crf", type=int, choices=range(0, 52), default=18, metavar="0..51")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--workspace", default=str(WORKSPACE))
    parser.add_argument("--resume", action="store_true", help="使用工作目录中原任务的完整参数恢复")
    args = parser.parse_args(argv)
    auto_picture = args.auto_dolby or args.auto_double_dolby or args.auto_free
    supplied = list(argv if argv is not None else sys.argv[1:])
    explicit = {token.split("=", 1)[0] for token in supplied if token.startswith("-")}
    try:
        if args.resume:
            from pathlib import Path
            cp = load_checkpoint(Path(args.workspace) / "checkpoint.json")
            if not cp:
                parser.error("工作目录中没有可恢复的断点")
            # Restoring always uses the original settings; reject ignored options.
            allowed = {"--resume", "--workspace", "--allow-cloud-upload"}
            if any(token.startswith("-") and token.split("=", 1)[0] not in allowed for token in supplied):
                parser.error("--resume 仅可与 --workspace / --allow-cloud-upload 一起使用；恢复原任务参数")
            cfg = dict(cp["cfg"], resume=True)
            if cfg.get("atmos_backend") == "aws" and cfg.get("audio_mode") == "atmos_auto":
                cfg["cloud_atmos"] = dict(cfg.get("cloud_atmos") or {}, allow_upload=args.allow_cloud_upload)
        elif args.bili_one_click:
            if not args.input:
                parser.error("一键制作需要 --input 视频")
            allowed = {"--bili-one-click", "-i", "--input", "-o", "--output", "--audio", "--workspace",
                       "--preset", "--crf", "--keep-temp"}
            if explicit - allowed:
                parser.error("--bili-one-click 会自动选择交付参数；如需手动调节，请使用其他导出方案")
            from consumer_picture import make_job
            from quality import inspect_media
            cfg = make_job(inspect_media(args.input), args.input, args.output, args.audio, args.workspace)
            if "--preset" in explicit:
                cfg["preset"] = args.preset
            cfg.update(crf=args.crf, auto_clean=not args.keep_temp)
        else:
            if not args.input:
                parser.error("新任务需要 --input")
            if sum((args.auto_dolby, args.auto_double_dolby, args.auto_free)) > 1:
                parser.error("--auto-dolby、--auto-double-dolby 与 --auto-free 请选择一个")
            if args.ai_denoise and not (args.auto_free or args.audio_mode == "hires"):
                parser.error("--ai-denoise 需配合 --auto-free 或 --audio-mode hires")
            if args.music_sr and (args.ai_denoise or not (args.auto_free or args.audio_mode == "hires")):
                parser.error("--music-sr 需配合 --auto-free 或 --audio-mode hires；人声降噪应另选")
            if args.auto_free and "--audio-mode" in explicit and args.audio_mode != "hires":
                parser.error("--auto-free 使用本地音频策略；请省略 --audio-mode 或使用 hires")
            if (args.auto_free or args.audio_mode == "hires") and (args.require_atmos or args.allow_cloud_upload):
                parser.error("本地免费模式不使用云编码，也不强制生成 Atmos")
            if auto_picture and explicit & {"--route", "--delivery", "--color"}:
                parser.error("自动制作模式选择路线、B站交付和杜比 8.4；请勿同时指定 --route / --delivery / --color")
            if args.auto_double_dolby and "--audio-mode" in explicit and args.audio_mode != "atmos_auto":
                parser.error("--auto-double-dolby 必须使用 --audio-mode atmos_auto，不能降级为普通音频")
            if args.allow_cloud_upload and args.atmos_backend != "aws":
                parser.error("--allow-cloud-upload 仅用于 --atmos-backend aws")
            if args.rpu and args.rpu_mode == "auto":
                parser.error("--rpu 与 --rpu-mode auto 冲突；手动 RPU 请用 external 或省略 --rpu-mode")
            if args.rpu_mode == "external" and not args.rpu:
                parser.error("--rpu-mode external 需要 --rpu 文件")
            if (args.ai or args.interp) and (auto_picture or args.route != "enhance"):
                parser.error("--ai / --interp 需要 --route enhance")
            if not auto_picture and args.route == "preserve" and (args.size or args.fps or args.rpu or args.color != "source"):
                parser.error("原片保留沿用原视频；改变尺寸、帧率、色彩或 RPU 请使用 --route transcode")
            if not auto_picture and args.route == "preserve" and args.source_transfer not in (None, "auto"):
                parser.error("原片保留不能改写源动态范围解释；--source-transfer 请配合 --route transcode 或 --auto-dolby")
            if args.interp and not args.fps:
                parser.error("RIFE 插帧需要明确 --fps")
            if args.route == "enhance" and args.fps and not args.interp:
                parser.error("增强路线改变帧率须加 --interp；普通重采样可使用 --route transcode")
            def dimensions(text):
                try:
                    pair = tuple(map(int, text.lower().split("x")))
                    if len(pair) != 2 or any(v < 16 or v > 8192 or v % 2 for v in pair):
                        raise ValueError
                    return pair
                except ValueError:
                    parser.error("尺寸应为 WIDTHxHEIGHT，宽高为 16–8192 的偶数")
            from quality import inspect_media, assess_bilibili, inspect_audio
            source = inspect_media(args.input)
            width, height = dimensions(args.size) if args.size else (source["display_width"], source["display_height"])
            work_width, work_height = dimensions(args.work_size)
            route, delivery = args.route, args.delivery
            fps = args.fps
            color = args.color
            audio_mode = "hires" if args.auto_free else "atmos_auto" if args.auto_double_dolby else args.audio_mode
            if auto_picture:
                suitable = assess_bilibili(source).get("status") != "fail"
                unchanged = not (args.size or fps or args.rpu or args.rpu_mode == "external" or args.source_transfer not in (None, "auto"))
                within_limits = max(width, height) <= 3840 and min(width, height) <= 2160 and source["fps"] <= 60
                route = "preserve" if suitable and unchanged and within_limits else "transcode"
                delivery, color = "bilibili", "dv84"
                scale = min(1, 3840 / max(width, height), 2160 / min(width, height))
                if scale < 1:
                    width, height = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
                if Fraction(str(fps or source["fps_fraction"])) > 60:
                    fps = "60"
                if "--audio-mode" not in explicit and not (args.auto_double_dolby or args.auto_free):
                    audio = inspect_audio(args.audio) if args.audio else source
                    if audio.get("audio_codec", "none") not in ("none", "aac", "ac3", "eac3", "mp3", "alac"):
                        audio_mode = "aac"
            if color == "source" and route == "enhance":
                color = "sdr"
            transfer_unknown = str(source.get("color_transfer") or "").lower() in ("", "unknown", "unspecified")
            wide_gamut = source.get("color_primaries") == "bt2020" or source.get("color_space") == "bt2020nc"
            if route != "preserve" and args.source_transfer in (None, "auto") and transfer_unknown and wide_gamut:
                parser.error("源文件缺少动态范围标记且含 BT.2020 信号；请用 --source-transfer bt709 / arib-std-b67 / smpte2084 明确源解释")
            if route != "preserve" and color == "dv81" and not args.rpu:
                parser.error("杜比视界 8.1 需要 --rpu 外部元数据；自动逐帧分析当前仅支持 8.4")
            cfg = {"video": args.input, "output": args.output, "audio": args.audio,
                   "route": route, "delivery": delivery, "require_atmos": args.require_atmos or args.auto_double_dolby or audio_mode == "atmos_auto",
                   "ai": args.ai, "interp": args.interp, "target_fps": fps or source["fps_fraction"],
                   "width": width, "height": height, "color_mode": color, "rpu": args.rpu,
                   "work_width": work_width, "work_height": work_height,
                   "audio_mode": audio_mode, "ai_denoise": args.ai_denoise, "music_sr": args.music_sr,
                   "model": args.model, "batch_size": args.batch_size,
                   "preset": args.preset, "crf": args.crf, "auto_clean": not args.keep_temp,
                   "workspace": args.workspace}
            if audio_mode == "atmos_auto":
                cfg.update({"atmos_backend": args.atmos_backend, "dee_path": args.dee_path,
                            "dee_profile": args.dee_profile,
                            "cloud_atmos": {"region": args.aws_region, "bucket": args.aws_bucket,
                                            "role_arn": args.aws_role_arn, "profile": args.aws_profile,
                                            "prefix": args.aws_prefix, "allow_upload": args.allow_cloud_upload}})
            if args.rpu_mode or args.rpu or (route != "preserve" and color in ("dv81", "dv84")):
                cfg["rpu_mode"] = args.rpu_mode or ("external" if args.rpu else "auto")
            if args.hdr_remaster is not None:
                cfg["hdr_remaster"] = args.hdr_remaster
            if args.hdr_peak_nits is not None:
                cfg["hdr_peak_nits"] = args.hdr_peak_nits
            if args.source_transfer is not None:
                cfg["source_transfer_override"] = args.source_transfer
            if route == "transcode" and fps:
                cfg["fps_override"] = fps
        runner = Pipeline(cfg, log=print, progress=lambda p: print(f"[进度] {p:.1f}%"),
                          stage=lambda name, *_: print(f"[阶段] {name}"))
        signal.signal(signal.SIGINT, lambda *_: runner.cancel())
        print(runner.run())
        return 0
    except PipelineCancelled as exc:
        print(str(exc), file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"处理失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
