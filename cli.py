"""Command-line interface, usable without PyQt5."""
import argparse
import signal
import sys

from pipeline import Pipeline, PipelineCancelled, WORKSPACE, load_checkpoint


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dolby Workstation — 原片保留、原生高精度转码与可选 AI 增强")
    parser.add_argument("-i", "--input", help="输入视频")
    parser.add_argument("-o", "--output", help="新的 MP4 输出路径；不会覆盖已有文件")
    parser.add_argument("--route", choices=("preserve", "transcode", "enhance"), default="preserve", help="默认原片保留")
    parser.add_argument("--delivery", choices=("general", "bilibili"), default="general")
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
    parser.add_argument("--audio", default="", help="可选外部音频")
    parser.add_argument("--audio-mode", choices=("copy", "aac", "eac3", "none"), default="copy")
    parser.add_argument("--require-atmos", action="store_true", help="仅在最终文件确认保留 Atmos 时成功")
    parser.add_argument("--model", default="realesrgan-x4plus")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--preset", choices=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"), default="medium")
    parser.add_argument("--crf", type=int, choices=range(0, 52), default=18, metavar="0..51")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--workspace", default=str(WORKSPACE))
    parser.add_argument("--resume", action="store_true", help="使用工作目录中原任务的完整参数恢复")
    args = parser.parse_args(argv)
    try:
        if args.resume:
            from pathlib import Path
            cp = load_checkpoint(Path(args.workspace) / "checkpoint.json")
            if not cp:
                parser.error("工作目录中没有可恢复的断点")
            # Restoring always uses the original settings; reject ignored options.
            supplied = argv if argv is not None else sys.argv[1:]
            allowed = {"--resume", "--workspace"}
            if any(token.startswith("-") and token.split("=", 1)[0] not in allowed for token in supplied):
                parser.error("--resume 仅可与 --workspace 一起使用；恢复原任务参数")
            cfg = dict(cp["cfg"], resume=True)
        else:
            if not args.input:
                parser.error("新任务需要 --input")
            if (args.ai or args.interp) and args.route != "enhance":
                parser.error("--ai / --interp 需要 --route enhance")
            if args.route == "preserve" and (args.size or args.fps or args.rpu or args.color != "source"):
                parser.error("原片保留沿用原视频；改变尺寸、帧率、色彩或 RPU 请使用 --route transcode")
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
            from quality import inspect_media
            source = inspect_media(args.input)
            width, height = dimensions(args.size) if args.size else (source["display_width"], source["display_height"])
            work_width, work_height = dimensions(args.work_size)
            color = args.color
            if color == "source" and args.route == "enhance":
                color = "sdr"
            cfg = {"video": args.input, "output": args.output, "audio": args.audio,
                   "route": args.route, "delivery": args.delivery, "require_atmos": args.require_atmos,
                   "ai": args.ai, "interp": args.interp, "target_fps": args.fps or source["fps_fraction"],
                   "width": width, "height": height, "color_mode": color, "rpu": args.rpu,
                   "work_width": work_width, "work_height": work_height,
                   "audio_mode": args.audio_mode, "model": args.model, "batch_size": args.batch_size,
                   "preset": args.preset, "crf": args.crf, "auto_clean": not args.keep_temp,
                   "workspace": args.workspace}
            if args.route == "transcode" and args.fps:
                cfg["fps_override"] = args.fps
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
