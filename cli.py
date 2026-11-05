"""Command-line interface, usable without PyQt5."""
import argparse
import signal
import sys

from pipeline import Pipeline, PipelineCancelled, WORKSPACE, load_checkpoint


def main(argv=None):
    parser = argparse.ArgumentParser(description="8K Dolby Workstation — SDR 输入、AI 超分与 10bit HEVC 输出")
    parser.add_argument("-i", "--input", help="输入视频")
    parser.add_argument("-o", "--output", help="新的 MP4 输出路径；不会覆盖已有文件")
    parser.add_argument("--no-ai", action="store_true", help="禁用 Real-ESRGAN，使用 Lanczos 缩放")
    parser.add_argument("--no-interp", action="store_true", help="保持源帧率，不使用 RIFE")
    parser.add_argument("--fps", default="120", help="插帧目标帧率，支持 60000/1001")
    parser.add_argument("--size", default="7680x4320", help="输出宽x高，默认 7680x4320")
    parser.add_argument("--color", choices=("sdr", "hdr10", "dv81"), default="sdr")
    parser.add_argument("--rpu", default="", help="与最终画面/帧数匹配的 Profile 8.1 RPU.bin")
    parser.add_argument("--audio", default="", help="可选外部音频")
    parser.add_argument("--audio-mode", choices=("copy", "aac", "eac3", "none"), default="copy")
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
            width, height = map(int, args.size.lower().split("x"))
            cfg = {"video": args.input, "output": args.output, "audio": args.audio,
                   "ai": not args.no_ai, "interp": not args.no_interp, "target_fps": args.fps,
                   "width": width, "height": height, "color_mode": args.color, "rpu": args.rpu,
                   "audio_mode": args.audio_mode, "model": args.model, "batch_size": args.batch_size,
                   "preset": args.preset, "crf": args.crf, "auto_clean": not args.keep_temp,
                   "workspace": args.workspace}
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
