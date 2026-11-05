"""Testable video pipeline. External tools are invoked without a shell."""
from __future__ import annotations

import json
import hashlib
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from fractions import Fraction
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = BASE_DIR / "tools"
WORKSPACE = BASE_DIR / "workspace"
CHECKPOINT_FILE = WORKSPACE / "checkpoint.json"
VERSION = "2.2"


class PipelineError(RuntimeError):
    pass


class PipelineCancelled(PipelineError):
    pass


def find_tool(name):
    stem = name.removesuffix(".exe")
    for path in (TOOLS_DIR / (stem + ".exe"), TOOLS_DIR / stem):
        if path.is_file():
            return str(path)
    found = shutil.which(stem)
    if found:
        return found
    raise FileNotFoundError(f"缺少工具 {stem}；请运行 setup.bat 或加入 PATH")


def find_rife_model():
    for path in [TOOLS_DIR / "rife-v4.6", TOOLS_DIR / "rife-v4", *sorted(TOOLS_DIR.glob("rife-v4*"))]:
        if all((path / ("flownet." + ext)).is_file() for ext in ("bin", "param")):
            return str(path)
    raise FileNotFoundError("缺少 RIFE v4 模型（flownet.bin 和 flownet.param）")


def _json_command(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=60, **_hidden())
        if result.returncode:
            raise PipelineError(result.stderr[-2000:])
        return json.loads(result.stdout)
    except (ValueError, subprocess.TimeoutExpired) as exc:
        raise PipelineError(f"无法读取媒体信息: {exc}") from exc


def _hidden():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def probe_video(path):
    from quality import inspect_media, QualityError
    try:
        return inspect_media(path, ffprobe=find_tool("ffprobe"))
    except QualityError as exc:
        raise PipelineError(str(exc)) from exc


def output_frame_count(source_count, source_fps, target_fps):
    if source_count <= 0 or Fraction(str(source_fps)) <= 0 or Fraction(str(target_fps)) <= 0:
        raise ValueError("帧数与帧率必须大于零")
    return max(1, round(source_count * Fraction(str(target_fps)) / Fraction(str(source_fps))))


def estimate_disk(info, cfg):
    """Upper-bound raw frame payload plus overhead; PNG compression is not assumed."""
    width, height = info["width"], info["height"]
    if cfg.get("route") in ("preserve", "transcode"):
        # Compressed output + staging copy. Native routes do not extract PNGs.
        bitrate = max(float(info.get("video_bit_rate") or info.get("bit_rate") or 0), 30_000_000)
        copies = 3 if cfg.get("color_mode") in ("dv81", "dv84") else 2
        mb = info["duration"] * bitrate / 8 / 1024**2 * copies * 1.5 + 256
        return mb, "原片封装/高精度直编不拆帧；按码率预留输出与校验中间文件，实际体积随内容变化"
    scale = min(1, int(cfg.get("work_width", 1920)) / width, int(cfg.get("work_height", 1080)) / height) if cfg.get("ai", True) else 1
    pixels = math.ceil(width * scale) * math.ceil(height * scale)
    frames = max(info.get("total_frames", 0), math.ceil(info["duration"] * info["fps"]))
    target = cfg.get("target_fps", 120) if cfg.get("interp", True) else info["fps_fraction"]
    out_frames = output_frame_count(frames, info["fps_fraction"], target)
    raw = frames * pixels * 3
    interpolated = out_frames * pixels * 3 if cfg.get("interp", True) else 0
    # ESR output is x4 in each dimension before exact-size scaling during encoding.
    sr = min(out_frames, int(cfg.get("batch_size", 64))) * pixels * 16 * 3 if cfg.get("ai", True) else 0
    if not cfg.get("auto_clean", True):
        sr = out_frames * pixels * 16 * 3 if cfg.get("ai", True) else 0
    # Chunked video + joined HEVC + injected HEVC + final MP4 can coexist.
    encoded = out_frames * int(cfg.get("width", 7680)) * int(cfg.get("height", 4320)) * 1.5 * 4
    mb = (raw + interpolated + sr + encoded) * 1.1 / 1024**2 + 512
    return mb, "按未压缩帧和编码中间文件保守估算；分批超分，每批最多 %s 帧" % cfg.get("batch_size", 64)


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_checkpoint(path=CHECKPOINT_FILE):
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 2 or not data.get("cfg") or not data.get("job_id"):
            raise ValueError("断点版本不兼容")
        return data
    except (ValueError, OSError) as exc:
        raise PipelineError(f"断点损坏，未自动删除: {exc}") from exc


def _fingerprint(path):
    path = Path(path).resolve()
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _sequence(folder):
    files = sorted(folder.glob("*.png"))
    if not files or any(p.name != f"{i:08d}.png" or p.stat().st_size == 0
                        for i, p in enumerate(files, 1)):
        raise PipelineError(f"帧序列不完整: {folder}")
    return files


class Pipeline:
    def __init__(self, cfg, log=lambda msg: None, progress=lambda pct: None,
                 stage=lambda name, cur, total: None):
        self.cfg = dict(cfg)
        self.log, self.progress, self.stage = log, progress, stage
        self._cancel = threading.Event()
        self._paused = threading.Event()
        self._process = None
        self.root = Path(self.cfg.get("workspace", WORKSPACE)).resolve()
        self.job_dir = None
        self.manifest = {}
        self._started = time.monotonic()

    def cancel(self):
        self._cancel.set()

    def set_paused(self, paused):
        self._paused.set() if paused else self._paused.clear()

    def _boundary(self):
        while self._paused.is_set():
            if self._cancel.wait(0.1):
                raise PipelineCancelled("任务已停止，可从已完成阶段恢复")
        if self._cancel.is_set():
            raise PipelineCancelled("任务已停止，可从已完成阶段恢复")

    def _run(self, cmd):
        self._boundary()
        self.log("[命令] " + subprocess.list2cmdline([str(a) for a in cmd]))
        # Keep draining the pipe during pause. Pause takes effect before the next command.
        process = subprocess.Popen([str(a) for a in cmd], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                   errors="replace", **_hidden())
        self._process = process
        lines = queue.Queue(maxsize=256)
        tail = deque(maxlen=80)

        def read():
            try:
                for line in process.stdout:
                    # Bounded log buffer without blocking the subprocess on a slow GUI.
                    try:
                        lines.put_nowait(line.rstrip())
                    except queue.Full:
                        pass
            finally:
                process.stdout.close()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            while process.poll() is None or reader.is_alive() or not lines.empty():
                if self._cancel.is_set() and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                try:
                    line = lines.get(timeout=0.1)
                    if line:
                        tail.append(line)
                        self.log(line)
                except queue.Empty:
                    pass
            code = process.wait()
            if self._cancel.is_set():
                raise PipelineCancelled("任务已停止，可从已完成阶段恢复")
            if code:
                raise PipelineError(f"{Path(cmd[0]).name} 失败 ({code})\n" + "\n".join(tail)[-3000:])
            return "\n".join(tail)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join(timeout=2)
            self._process = None

    def _save(self):
        self.manifest["elapsed"] = self._prior_elapsed + time.monotonic() - self._started
        _atomic_json(self.job_dir / "manifest.json", self.manifest)
        _atomic_json(self.root / "checkpoint.json", self.manifest)

    def _stage_done(self, name, check_cancel=True):
        if check_cancel:
            self._boundary()
        if name not in self.manifest["completed"]:
            self.manifest["completed"].append(name)
        self.manifest["stage"] = name
        self._save()

    def _clean(self, path):
        path = Path(path).resolve()
        # Only delete known artifacts belonging to this specific job.
        if not path.is_relative_to(self.job_dir) or path == self.job_dir:
            raise PipelineError("拒绝清理任务目录以外的路径")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def _prepare(self):
        cfg = self.cfg
        video = Path(cfg.get("video", "")).resolve()
        if not video.is_file():
            raise PipelineError("请选择存在的视频文件")
        for key in ("audio", "rpu"):
            if cfg.get(key) and not Path(cfg[key]).is_file():
                raise PipelineError(f"文件不存在: {cfg[key]}")
            if cfg.get(key):
                cfg[key] = str(Path(cfg[key]).resolve())
        cfg["video"] = str(video)
        cfg.setdefault("route", "enhance")
        cfg.setdefault("delivery", "general")
        cfg.setdefault("require_atmos", False)
        if cfg["route"] not in ("enhance", "transcode", "preserve"):
            raise PipelineError("不支持的处理路线")
        if cfg["delivery"] not in ("general", "bilibili"):
            raise PipelineError("不支持的交付平台")
        self.info = self._inspect(video)
        if cfg["route"] in ("preserve", "transcode"):
            cfg["ai"] = False
            cfg["interp"] = False
            cfg.setdefault("width", self.info.get("display_width", self.info["width"]))
            cfg.setdefault("height", self.info.get("display_height", self.info["height"]))
            if cfg["route"] == "preserve":
                cfg["width"], cfg["height"] = self.info["width"], self.info["height"]
            if cfg.get("color_mode", "source") == "source":
                cfg["color_mode"] = {"smpte2084": "hdr10", "arib-std-b67": "hlg"}.get(self.info["color_transfer"], "sdr")
        cfg.setdefault("color_mode", "sdr")
        cfg.setdefault("audio_mode", "copy")
        cfg.setdefault("ai", True)
        cfg.setdefault("interp", True)
        cfg.setdefault("auto_clean", True)
        cfg.setdefault("width", 7680)
        cfg.setdefault("height", 4320)
        cfg.setdefault("batch_size", 64)
        cfg.setdefault("target_fps", 120)
        cfg.setdefault("model", "realesrgan-x4plus")
        if cfg["color_mode"] not in ("sdr", "hdr10", "dv81", "hlg", "dv84"):
            raise PipelineError("不支持的色彩模式")
        if cfg["audio_mode"] not in ("copy", "aac", "eac3", "none"):
            raise PipelineError("不支持的音频模式")
        if not all(isinstance(cfg[k], int) and 16 <= cfg[k] <= 8192 and cfg[k] % 2 == 0
                   for k in ("width", "height")):
            raise PipelineError("输出宽高必须是 16 到 8192 之间的偶数")
        if cfg["route"] != "preserve" and (cfg["width"] < 64 or cfg["height"] < 48):
            raise PipelineError("为避免编码器在极小画面下崩溃，重新编码的宽至少为 64、高至少为 48；请选择更大输出尺寸，或使用原片保留")
        if not 1 <= int(cfg["batch_size"]) <= 256:
            raise PipelineError("batch_size 必须在 1 到 256 之间")
        default = video.with_name(f"{video.stem}_{cfg['width']}x{cfg['height']}_{cfg['color_mode']}.mp4")
        self.output = Path(cfg.get("output") or default).resolve()
        cfg["output"] = str(self.output)
        if self.output.suffix.lower() != ".mp4":
            raise PipelineError("输出文件扩展名必须为 .mp4")
        if self.output.exists():
            raise PipelineError("输出文件已存在，请选择新文件名（不会覆盖原文件）")
        for key in ("video", "audio", "rpu"):
            if cfg.get(key) and self.output == Path(cfg[key]).resolve():
                raise PipelineError("输出路径不能与输入相同")
        if cfg["route"] == "enhance" and self.info["color_transfer"] in ("smpte2084", "arib-std-b67"):
            raise PipelineError("当前 AI 管线使用 SDR RGB 工作帧；HDR 源请先用受控色调映射转为 SDR，避免丢失原始 HDR 色彩")
        self.source_rate = Fraction(self.info["fps_fraction"])
        self.rate = Fraction(str(cfg["target_fps"])) if cfg["interp"] else self.source_rate
        if cfg["route"] == "transcode" and cfg.get("fps_override"):
            self.rate = Fraction(str(cfg["fps_override"]))
        if cfg["route"] == "preserve":
            self.rate = self.source_rate
        if self.rate <= 0 or self.rate > 240 or (cfg["interp"] and self.rate < self.source_rate):
            raise PipelineError("目标帧率须不低于源帧率，且不超过 240fps")
        find_tool("ffmpeg")
        find_tool("ffprobe")
        if cfg["interp"]:
            find_tool("rife-ncnn-vulkan")
            self.rife_model = find_rife_model()
        if cfg["ai"]:
            find_tool("realesrgan-ncnn-vulkan")
            self.models = TOOLS_DIR / "models"
            if not re.fullmatch(r"[\w-]+", cfg["model"]):
                raise PipelineError("无效模型名称")
            # animevideov3 model filenames include a scale suffix.
            model = cfg["model"]
            if model == "realesr-animevideov3-x4":
                model = "realesr-animevideov3-x4"
                cfg["model"] = "realesr-animevideov3"
            elif model == "realesr-animevideov3":
                model += "-x4"
            if not all((self.models / f"{model}.{ext}").is_file() for ext in ("bin", "param")):
                raise PipelineError(f"缺少超分模型 {model}，请运行 setup.bat")
        if cfg["route"] != "preserve" and cfg["color_mode"] in ("dv81", "dv84"):
            if not cfg.get("rpu"):
                raise PipelineError("杜比视界需要与最终调色、HLG/PQ 基础层及帧数匹配的 RPU.bin；B站路线使用 Profile 8.4 / HLG")
            find_tool("dovi_tool")
            bsfs = self._run([find_tool("ffmpeg"), "-hide_banner", "-bsfs"])
            if not all(re.search(rf"\b{name}\b", bsfs) for name in ("dovi_rpu", "setts")):
                raise PipelineError("杜比视界需要带 dovi_rpu 与 setts 位流过滤器的新版 FFmpeg，请运行 setup.bat -WithDovi")
        if cfg["delivery"] == "bilibili" and cfg["route"] == "preserve":
            from quality import assess_bilibili
            # External audio may replace the source's absent/non-Atmos track.
            source_report = assess_bilibili(self.info, require_atmos=False)
            failed = [c["detail"] for c in source_report["checks"] if c["status"] == "fail"]
            if failed:
                raise PipelineError("原片尚不符合 B站公开杜比指南：\n" + "\n".join(failed))
        if cfg["delivery"] == "bilibili" and cfg["route"] != "preserve" and cfg["color_mode"] != "dv84":
            raise PipelineError("B站杜比制作须选择 Profile 8.4 / HLG；PQ / Profile 8.1 不属于此交付预设")
        self.audio_source = cfg.get("audio") or str(video)
        audio_data = _json_command([find_tool("ffprobe"), "-v", "error", "-show_streams",
                                    "-of", "json", self.audio_source])
        self.audio_stream = next((s for s in audio_data.get("streams", []) if s.get("codec_type") == "audio"), None)
        if cfg.get("audio") and not self.audio_stream and cfg["audio_mode"] != "none":
            raise PipelineError("外部音频文件不包含音轨")
        if self.audio_stream and cfg["audio_mode"] == "copy":
            if self.audio_stream.get("codec_name") not in ("aac", "ac3", "eac3", "mp3", "alac"):
                raise PipelineError("此音轨不能直接复制到 MP4，请选择 AAC 或 E-AC-3 编码")
        if cfg["require_atmos"]:
            if cfg["audio_mode"] != "copy" or self._inspect(self.audio_source, audio_only=True)["atmos"] != "confirmed":
                raise PipelineError("要求保留 Atmos 时，需要已确认的 Atmos/JOC 音轨并选择原码流复制；普通 E-AC-3 编码不会生成 Atmos")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        inputs = {k: _fingerprint(cfg[k]) for k in ("video", "audio", "rpu") if cfg.get(k)}
        if cfg.get("resume"):
            cp = load_checkpoint(self.root / "checkpoint.json")
            if not cp:
                raise PipelineError("没有可恢复的断点")
            job_id = cp["job_id"]
            if not re.fullmatch(r"[0-9a-f]{32}", job_id):
                raise PipelineError("无效任务断点")
            self.job_dir = self.root / "jobs" / job_id
            self.manifest = json.loads((self.job_dir / "manifest.json").read_text(encoding="utf-8"))
            original = {k: v for k, v in self.manifest["cfg"].items() if k != "resume"}
            current = {k: v for k, v in cfg.items() if k != "resume"}
            if original != current or self.manifest["inputs"] != inputs:
                raise PipelineError("输入文件或处理参数已改变，请新建任务")
        else:
            job_id = uuid.uuid4().hex
            self.job_dir = self.root / "jobs" / job_id
            self.job_dir.mkdir(parents=True)
            self.manifest = {"version": 2, "job_id": job_id, "cfg": cfg, "inputs": inputs,
                             "completed": [], "chunks": [], "stage": "准备", "elapsed": 0}
        self._prior_elapsed = self.manifest.get("elapsed", 0)
        self._save()

    def _extract(self):
        self.stage("1/6 提取 SDR 工作帧", 1, 6)
        folder = self.job_dir / "source"
        if "extract" not in self.manifest["completed"]:
            self._clean(folder)
            folder.mkdir()
            # Normalize VFR timestamps to average CFR; never exceed 1080p working size.
            vf = f"fps={self.source_rate},"
            if self.cfg["ai"]:
                vf += (f"scale=w='min({self.cfg.get('work_width', 1920)},iw)':h='min({self.cfg.get('work_height', 1080)},ih)':"
                       "force_original_aspect_ratio=decrease:flags=lanczos,")
            vf += "setsar=1"
            self._run([find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y", "-i", self.cfg["video"],
                       "-map", f"0:{self.info['stream']['index']}", "-vf", vf, "-pix_fmt", "rgb24", "-start_number", "1", folder / "%08d.png"])
            self.manifest["source_frames"] = len(_sequence(folder))
            self._stage_done("extract")
        self.progress(10)
        return folder

    def _interpolate(self, source):
        self.stage("2/6 RIFE 插帧" if self.cfg["interp"] else "2/6 保持源帧率", 2, 6)
        folder = self.job_dir / "interpolated"
        count = self.manifest["source_frames"]
        wanted = output_frame_count(count, self.source_rate, self.rate)
        if self.cfg["color_mode"] in ("dv81", "dv84"):
            self._validate_rpu(wanted)
        self.manifest["output_frames"] = wanted
        self.manifest["fps"] = str(self.rate)
        if "interpolate" not in self.manifest["completed"]:
            if self.cfg["interp"]:
                _sequence(source)
                self._clean(folder)
                folder.mkdir()
                if count == 1:
                    for idx in range(1, wanted + 1):
                        self._boundary()
                        shutil.copy2(source / "00000001.png", folder / f"{idx:08d}.png")
                else:
                    self._run([find_tool("rife-ncnn-vulkan"), "-i", source, "-o", folder,
                               "-m", self.rife_model, "-n", str(wanted), "-f", "%08d.png"])
                if len(_sequence(folder)) != wanted:
                    raise PipelineError("RIFE 输出帧数与目标帧数不一致")
            self._stage_done("interpolate")
        if self.cfg["interp"] and self.cfg["auto_clean"]:
            self._clean(source)
        self.progress(25)
        return folder if self.cfg["interp"] else source

    def _validate_rpu(self, wanted):
        summary = self._run([find_tool("dovi_tool"), "info", "-i", self.cfg["rpu"], "-s"])
        match = re.search(r"Frames:\s*(\d+)", summary)
        if not match or int(match[1]) != wanted or not re.search(r"Profile:\s*8\b", summary):
            raise PipelineError("RPU 必须为 Profile 8，且帧数与最终视频完全一致")
        if self.cfg["color_mode"] == "dv84":
            mapping = self._rpu_first_frame().get("rpu_data_mapping") or {}
            curves = mapping.get("curves", [])
            def identity(curve):
                polynomial = curve
                return (curve.get("mapping_idc") == "Polynomial" and curve.get("pivots") == [0, 1023]
                        and polynomial.get("poly_order_minus1") == [0]
                        and polynomial.get("poly_coef_int") == [[0, 1]]
                        and polynomial.get("poly_coef") == [[0, 0]])
            if len(curves) == 3 and all(identity(c) for c in curves):
                raise PipelineError("该 RPU 使用已知的 PQ 恒等映射，不能直接用于此 HLG / 8.4 制作路线；请选择与 HLG 成片配套的元数据")
        digest = hashlib.sha256()
        with Path(self.cfg["rpu"]).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                self._boundary()
                digest.update(block)
        self.manifest["rpu_validation"] = {"sha256": digest.hexdigest(), "frames": wanted,
            "target": self.cfg["color_mode"], "visual_match_verified": False,
            "note": "帧数及已知不兼容映射检查通过；画面调色与 RPU 的视觉匹配仍需配套母版验收"}

    def _rpu_first_frame(self):
        # A frame's JSON can exceed the normal bounded log queue. Capture it to
        # this job's file, poll cancellation, and cap its size before parsing.
        path = self.job_dir / "rpu-frame-inspection.json"
        process = None
        try:
            with path.open("wb") as output:
                process = subprocess.Popen([find_tool("dovi_tool"), "info", "-i", self.cfg["rpu"], "-f", "0"],
                                           stdout=output, stderr=subprocess.STDOUT, **_hidden())
                started = time.monotonic()
                while process.poll() is None:
                    self._boundary()
                    if time.monotonic() - started > 120 or path.stat().st_size > 2 * 1024 * 1024:
                        raise PipelineError("RPU 单帧检测超出限制")
                    self._cancel.wait(0.1)
            if process.returncode:
                raise PipelineError("RPU 单帧解析失败")
            if path.stat().st_size > 2 * 1024 * 1024:
                raise PipelineError("RPU 单帧数据过大")
            raw = path.read_text(encoding="utf-8", errors="replace")
            try:
                return json.loads(raw[raw.index("{"):])
            except (ValueError, TypeError) as exc:
                raise PipelineError("RPU 单帧检测未返回有效数据") from exc
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            path.unlink(missing_ok=True)

    def _filters(self):
        cfg = self.cfg
        vf = (f"scale={cfg['width']}:{cfg['height']}:force_original_aspect_ratio=decrease:"
              f"force_divisible_by=2:flags=lanczos,pad={cfg['width']}:{cfg['height']}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        # PNG working frames are RGB. Convert actual sample values, not just metadata.
        if cfg["color_mode"] in ("hdr10", "dv81", "hlg", "dv84"):
            transfer = "arib-std-b67" if cfg["color_mode"] in ("hlg", "dv84") else "smpte2084"
            vf += (",format=gbrpf32le,zscale=pin=bt709:tin=bt709:min=gbr:rin=full:"
                   "p=bt709:t=linear:m=gbr:r=full:npl=100,"
                   "zscale=pin=bt709:tin=linear:min=gbr:rin=full:p=bt2020:t=linear:m=gbr:r=full,"
                   "zscale=pin=bt2020:tin=linear:min=gbr:rin=full:p=bt2020:"
                   f"t={transfer}:m=bt2020nc:r=limited:npl=100,format=yuv420p10le")
        else:
            vf += (",zscale=pin=bt709:tin=bt709:min=gbr:rin=full:"
                   "p=bt709:t=bt709:m=bt709:r=limited,format=yuv420p10le")
        return vf

    def _validate_video(self, path, count=None, final=False):
        info = self._inspect(path, count_frames=True)
        data = {"streams": [info["stream"], *info["audio_streams"]], "format": info["format"]}
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video or video.get("width") != self.cfg["width"] or video.get("height") != self.cfg["height"]:
            raise PipelineError("输出视频尺寸校验失败")
        if count is not None and int(video.get("nb_read_frames", -1)) != count:
            raise PipelineError("输出视频帧数校验失败")
        if Fraction(video.get("avg_frame_rate", "0/1")) != self.rate:
            raise PipelineError("输出帧率校验失败")
        if video.get("pix_fmt") != "yuv420p10le":
            raise PipelineError("输出不是 10bit HEVC")
        transfer = "arib-std-b67" if self.cfg["color_mode"] in ("hlg", "dv84") else "smpte2084"
        if self.cfg["color_mode"] != "sdr" and video.get("color_transfer") != transfer:
            raise PipelineError("HDR 传递函数校验失败")
        if final and self.cfg["color_mode"] in ("dv81", "dv84"):
            dv = next((s for s in video.get("side_data_list", []) if s.get("side_data_type") == "DOVI configuration record"), {})
            compat = 4 if self.cfg["color_mode"] == "dv84" else 1
            if dv.get("dv_profile") != 8 or dv.get("dv_bl_signal_compatibility_id") != compat:
                raise PipelineError("输出缺少所选 Dolby Vision Profile 配置；不会发布为杜比视界成功结果")
        return data

    def _encode_batches(self, frames):
        self.stage("3/6 分批超分与 HEVC 编码", 3, 6)
        cfg = self.cfg
        chunks = self.job_dir / "chunks"
        chunks.mkdir(exist_ok=True)
        total = self.manifest["output_frames"]
        batch_size = int(cfg["batch_size"])
        batch_count = math.ceil(total / batch_size)
        for batch in range(batch_count):
            self._boundary()
            name = f"{batch:06d}.mp4"
            count = min(batch_size, total - batch * batch_size)
            output = chunks / name
            if batch < len(self.manifest["chunks"]):
                entry = self.manifest["chunks"][batch]
                if entry["name"] != name or not output.exists() or output.stat().st_size != entry["size"]:
                    raise PipelineError("已完成的视频分片缺失或改变，请新建任务")
                continue
            work = self.job_dir / f"batch_{batch:06d}"
            self._clean(work)
            src, sr = work / "input", work / "sr"
            src.mkdir(parents=True)
            sr.mkdir()
            for idx in range(count):
                original = frames / f"{batch * batch_size + idx + 1:08d}.png"
                if not original.is_file():
                    raise PipelineError(f"缺少输入帧 {original}")
                dest = src / f"{idx + 1:08d}.png"
                try:
                    os.link(original, dest)
                except OSError:
                    shutil.copy2(original, dest)
            if cfg["ai"]:
                self._run([find_tool("realesrgan-ncnn-vulkan"), "-i", src, "-o", sr,
                           "-m", self.models, "-n", cfg["model"], "-s", "4", "-t", "256", "-f", "png"])
                if len(_sequence(sr)) != count:
                    raise PipelineError("超分输出帧数不完整")
            encode_input = sr if cfg["ai"] else src
            partial = output.with_name(output.stem + ".partial.mp4")
            params = "repeat-headers=1:colorprim=bt709:transfer=bt709:colormatrix=bt709"
            if cfg["color_mode"] != "sdr":
                params = ("repeat-headers=1:hdr-opt=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
                          "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=100,100")
            if cfg["color_mode"] in ("hlg", "dv84"):
                params = "repeat-headers=1:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc"
            if cfg["color_mode"] in ("dv81", "dv84") or count < 4:
                # RPU injection uses Annex B. No B-frames allows exact packet
                # timestamp reconstruction after injection without re-encoding.
                params += ":bframes=0"
            # Bound the number of simultaneous full-resolution frame buffers.
            params += ":frame-threads=1"
            self._run([find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y", "-framerate", str(self.rate),
                       "-start_number", "1", "-i", encode_input / "%08d.png", "-frames:v", str(count),
                       "-vf", self._filters(), "-c:v", "libx265", "-preset", cfg.get("preset", "medium"),
                       "-crf", str(cfg.get("crf", 18)), "-pix_fmt", "yuv420p10le", "-x265-params", params,
                       "-tag:v", "hvc1", "-video_track_timescale", str(self.rate.numerator), "-an", partial])
            self._validate_video(partial, count)
            self._boundary()
            os.replace(partial, output)
            self.manifest["chunks"].append({"name": name, "count": count, "size": output.stat().st_size})
            self._save()
            if cfg["auto_clean"]:
                self._clean(work)
            self.progress(25 + 45 * (batch + 1) / batch_count)
        self._stage_done("encode")
        if cfg["auto_clean"]:
            self._clean(frames)
        return chunks

    def _join(self, chunks):
        self.stage("4/6 合并视频与杜比元数据", 4, 6)
        joined = self.job_dir / "joined.mp4"
        if "join" not in self.manifest["completed"]:
            listing = chunks / "concat.txt"
            listing.write_text("\n".join(f"file '{e['name']}'" for e in self.manifest["chunks"]), encoding="utf-8")
            partial = self.job_dir / "joined.partial.mp4"
            self._run([find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y", "-f", "concat", "-safe", "1",
                       "-i", listing, "-c", "copy", partial])
            self._validate_video(partial, self.manifest["output_frames"])
            os.replace(partial, joined)
            self._stage_done("join")
        return self._inject_dv(joined)

    def _inject_dv(self, joined):
        if self.cfg["color_mode"] not in ("dv81", "dv84"):
            self.progress(82)
            return joined
        dv_video = self.job_dir / "dv.hevc"
        if "dv" not in self.manifest["completed"]:
            raw = self.job_dir / "base.hevc"
            self._run([find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y", "-i", joined,
                       "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb", raw])
            partial = self.job_dir / "dv.partial.hevc"
            self._run([find_tool("dovi_tool"), "inject-rpu", "-i", raw, "--rpu-in", self.cfg["rpu"], "-o", partial])
            if not partial.is_file() or not partial.stat().st_size:
                raise PipelineError("杜比元数据注入未生成输出")
            os.replace(partial, dv_video)
            self._stage_done("dv")
        self.progress(82)
        return dv_video

    def _audio(self):
        self.stage("5/6 处理音频", 5, 6)
        if self.cfg["audio_mode"] == "none" or not self.audio_stream:
            self.log("[音频] 无音轨，输出静音视频")
            self._stage_done("audio")
            return None
        output = self.job_dir / "audio.mp4"
        if "audio" not in self.manifest["completed"]:
            partial = self.job_dir / "audio.partial.mp4"
            duration = self.manifest.get("duration") or self.manifest["output_frames"] / float(self.rate)
            cmd = [find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y", "-i", self.audio_source,
                   "-map", "0:a:0", "-vn"]
            mode = self.cfg["audio_mode"]
            if mode == "copy":
                cmd += ["-c:a", "copy"]
                self.log("[音频] 直接复制音轨；已有 E-AC-3/Atmos 数据不重新编码，短音轨按原长保留")
            else:
                cmd += ["-af", "apad", "-ar", "48000", "-c:a", mode, "-b:a", "640k" if mode == "eac3" else "256k"]
                channels = int(self.audio_stream.get("channels", 2))
                if mode == "eac3" and channels > 6:
                    cmd += ["-ac", "6"]
                    self.log("[音频] E-AC-3 编码支持最多 5.1 声道，将下混；此编码不会生成 Atmos 对象元数据")
            cmd += ["-t", f"{duration:.9f}", partial]
            self._run(cmd)
            data = _json_command([find_tool("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(partial)])
            if not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
                raise PipelineError("音轨校验失败")
            os.replace(partial, output)
            self._stage_done("audio")
        if not output.exists() or not output.stat().st_size:
            raise PipelineError("断点音轨缺失")
        self.progress(90)
        return output

    def _mux(self, video, audio):
        self.stage("6/6 封装与验证 MP4", 6, 6)
        # The temporary final output stays on the output volume for atomic publication.
        partial = self.output.with_name(f".{self.output.stem}.{self.manifest['job_id']}.partial.mp4")
        cmd = [find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y"]
        if self.cfg["route"] != "preserve" and self.cfg["color_mode"] in ("dv81", "dv84"):
            cmd += ["-fflags", "+genpts", "-r", str(self.rate)]
        cmd += ["-i", video]
        if audio:
            cmd += ["-i", audio]
        mapping = f"0:{self.info['stream']['index']}" if self.cfg["route"] == "preserve" else "0:v:0"
        cmd += ["-map", mapping]
        if audio:
            cmd += ["-map", "1:a:0"]
        cmd += ["-c", "copy"]
        if self.cfg["route"] != "preserve" or self.info["stream"]["codec_name"] == "hevc":
            cmd += ["-tag:v", "hvc1"]
        elif self.info["stream"]["codec_name"] == "h264":
            cmd += ["-tag:v", "avc1"]
        cmd += ["-movflags", "+faststart", "-strict", "unofficial", partial]
        if self.cfg["delivery"] == "bilibili":
            cmd[-1:-1] = ["-map_metadata", "-1", "-map_chapters", "-1"]
        if self.cfg["route"] != "preserve" and self.cfg["color_mode"] in ("dv81", "dv84"):
            # dovi_rpu writes the decoder configuration record needed by MP4.
            # setts supplies exact CFR timestamps for each non-reordered packet.
            bsf = ("dovi_rpu=compression=0,setts=ts=N:duration=1:time_base="
                   f"{self.rate.denominator}/{self.rate.numerator}")
            cmd[-1:-1] = ["-bsf:v", bsf]
        try:
            self._run(cmd)
            if self.cfg["route"] == "preserve":
                data = self._validate_copy(partial)
            else:
                data = self._validate_video(partial, self.manifest["output_frames"], final=True)
            duration = float(next(s for s in data["streams"] if s["codec_type"] == "video")["duration"])
            expected = self.manifest.get("duration") or self.manifest["output_frames"] / float(self.rate)
            if abs(duration - expected) > max(0.05, 1 / float(self.rate)):
                raise PipelineError("输出时长校验失败")
            if audio and not any(s.get("codec_type") == "audio" for s in data["streams"]):
                raise PipelineError("最终文件缺少音轨")
            # Read the complete video through the decoder. A valid container or
            # successful mux alone does not prove there are no corrupt frames.
            self._run([find_tool("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-i", partial,
                       "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"])
            from quality import assess_bilibili
            media = self._inspect(partial)
            media["path"] = str(self.output)
            media["filename"] = self.output.name
            media["format"]["filename"] = str(self.output)
            readiness = assess_bilibili(media, require_atmos=self.cfg["require_atmos"])
            if self.cfg["require_atmos"] and media["atmos"] != "confirmed":
                raise PipelineError("最终文件未保留已确认的 Atmos 音轨，未发布输出")
            report = readiness if self.cfg["delivery"] == "bilibili" else {
                "status": "pass", "summary": "成片技术质检通过；视频与音频均已完整解码验证",
                "platform_verified": False,
                "checks": [{"id": "decode", "label": "完整解码", "status": "pass",
                            "detail": "尺寸、时长及处理路线约束通过，视频和音频完整解码无报错"}],
                "bilibili_readiness": readiness,
            }
            report["media"] = media
            report["export"] = {"route": self.cfg["route"], "delivery": self.cfg["delivery"],
                                "decode_verified": True, "source": self.cfg["video"]}
            if self.manifest.get("rpu_validation"):
                report["export"]["rpu_validation"] = self.manifest["rpu_validation"]
            if self.cfg["delivery"] == "bilibili" and report["status"] == "fail":
                failures = [c["detail"] for c in report["checks"] if c["status"] == "fail"]
                raise PipelineError("B站交付质检未通过：\n" + "\n".join(failures))
            self._boundary()
            # Same-volume publication is atomic. Windows rename refuses existing
            # destinations; POSIX link provides the same no-overwrite guarantee.
            try:
                if os.name == "nt":
                    os.rename(partial, self.output)
                else:
                    os.link(partial, self.output)
                    partial.unlink()
            except FileExistsError:
                raise PipelineError("输出文件已被创建，请更换文件名")
            except OSError as exc:
                raise PipelineError(f"无法原子保存输出，请选择本地磁盘: {exc}") from exc
            # Cancellation after publication must not turn a completed output into
            # a failed task whose resume would collide with the output file.
            self._stage_done("mux", check_cancel=False)
            from quality import write_quality_report
            try:
                write_quality_report(self.output, report)
            except OSError as exc:
                self.log(f"[提示] 视频已完成，但质检报告保存失败: {exc}")
        finally:
            if partial.exists():
                partial.unlink()
        return self.output

    def _inspect(self, path, count_frames=False, audio_only=False):
        from quality import inspect_media, inspect_audio, QualityError, QualityCancelled
        self._boundary()
        try:
            options = {"ffprobe": find_tool("ffprobe"), "cancel_event": self._cancel}
            if audio_only:
                return inspect_audio(path, **options)
            return inspect_media(path, count_frames=count_frames, **options)
        except QualityCancelled as exc:
            raise PipelineCancelled(str(exc)) from exc
        except QualityError as exc:
            raise PipelineError(str(exc)) from exc

    def _validate_copy(self, path):
        data = _json_command([find_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        source = self.info["stream"]
        if not video or any(video.get(k) != source.get(k) for k in ("codec_name", "width", "height", "pix_fmt")):
            raise PipelineError("原片保留校验失败：编码、尺寸、精度或色彩发生变化")
        for key in ("color_transfer", "color_primaries", "color_space"):
            if source.get(key) not in (None, "unknown", "unspecified") and video.get(key) != source[key]:
                raise PipelineError("原片色彩信息未完整保留")
        for original in source.get("side_data_list", []):
            if original.get("side_data_type") == "DOVI configuration record":
                final = next((d for d in video.get("side_data_list", []) if d.get("side_data_type") == "DOVI configuration record"), {})
                if any(original.get(k) != final.get(k) for k in ("dv_profile", "dv_bl_signal_compatibility_id", "rpu_present_flag")):
                    raise PipelineError("原片杜比配置未完整保留")
        # Hash the encoded video payload, rather than trusting stream tags.
        def digest(file, mapping):
            result = self._run([find_tool("ffmpeg"), "-nostdin", "-v", "error", "-i", file,
                                "-map", mapping, "-c", "copy", "-an", "-f", "hash", "-hash", "sha256", "-"])
            match = re.search(r"SHA256=([0-9a-fA-F]+)", result)
            if not match:
                raise PipelineError("无法读取视频码流校验值")
            return match[1]
        if digest(self.cfg["video"], f"0:{source['index']}") != digest(path, "0:v:0"):
            raise PipelineError("视频码流校验不一致，未发布输出")
        return data

    def _native_filters(self):
        """Keep native precision until the final 10-bit quantization."""
        stream, mode = self.info["stream"], self.cfg["color_mode"]
        transfer = stream.get("color_transfer")
        transfer = transfer if transfer in ("smpte2084", "arib-std-b67", "bt709", "iec61966-2-1", "bt2020-10", "bt2020-12") else "bt709"
        hdr = transfer in ("smpte2084", "arib-std-b67")
        primaries = stream.get("color_primaries")
        primaries = primaries if primaries in ("bt709", "bt2020", "smpte432", "smpte431") else ("bt2020" if hdr else "bt709")
        matrix = stream.get("color_space")
        matrix = matrix if matrix in ("bt709", "bt2020nc", "smpte170m", "bt470bg", "gbr") else ("bt2020nc" if hdr else "bt709")
        if stream.get("pix_fmt", "").startswith(("rgb", "bgr", "gbr")):
            matrix = "gbr"
        value_range = "full" if stream.get("color_range") == "pc" or matrix == "gbr" else "limited"
        out_t = "arib-std-b67" if mode in ("hlg", "dv84") else ("smpte2084" if mode in ("hdr10", "dv81") else "bt709")
        out_p = "bt709" if mode == "sdr" else "bt2020"
        out_m = "bt709" if mode == "sdr" else "bt2020nc"
        cfg = self.cfg
        filters = f"fps={self.rate},"
        visual_width = self.info.get("display_width", stream["width"])
        visual_height = self.info.get("display_height", stream["height"])
        if (cfg['width'], cfg['height']) != (visual_width, visual_height):
            try:
                sar = Fraction(stream.get("sample_aspect_ratio", "1:1").replace(":", "/"))
                if sar <= 0:
                    sar = Fraction(1)
            except (ValueError, ZeroDivisionError):
                sar = Fraction(1)
            if self.info.get("rotation") in (90, 270):
                sar = 1 / sar
            dar = visual_width * sar / visual_height
            fitted_w = max(2, min(cfg['width'], int(cfg['height'] * dar)) // 2 * 2)
            fitted_h = max(2, min(cfg['height'], int(cfg['width'] / dar)) // 2 * 2)
            filters += (f"scale={fitted_w}:{fitted_h}:flags=lanczos,setsar=1,"
                        f"pad={cfg['width']}:{cfg['height']}:(ow-iw)/2:(oh-ih)/2,")
        same_color = transfer == out_t and primaries == out_p and matrix == out_m
        if same_color and value_range == "limited":
            filters += "format=yuv420p10le"
        else:
            filters += (f"zscale=pin={primaries}:tin={transfer}:min={matrix}:rin={value_range}:"
                        f"p={primaries}:t=linear:m=gbr:r=full:npl=100,format=gbrpf32le,")
            if hdr and mode == "sdr":
                filters += "tonemap=tonemap=mobius:param=0.3:desat=0.5,"
            filters += (f"zscale=pin={primaries}:tin=linear:min=gbr:rin=full:"
                        f"p={out_p}:t={out_t}:m={out_m}:r=limited:npl=100:dither=error_diffusion,format=yuv420p10le")
        # Source RPU metadata must never follow regraded/resampled frames into
        # the encoder. Only explicitly supplied matching metadata is injected.
        if not same_color:
            # Regrading invalidates source mastering/light metadata too.
            return filters + ",sidedata=mode=delete"
        if any(s.get("side_data_type") == "DOVI configuration record" for s in stream.get("side_data_list", [])):
            return filters + ",sidedata=mode=delete:type=DOVI_RPU_BUFFER,sidedata=mode=delete:type=DOVI_METADATA"
        return filters

    def _native_video(self):
        self.stage("1/6 原生高精度转码", 1, 6)
        joined = self.job_dir / "native.mp4"
        if "native" not in self.manifest["completed"]:
            partial = self.job_dir / "native.partial.mp4"
            mode = self.cfg["color_mode"]
            transfer = "arib-std-b67" if mode in ("hlg", "dv84") else ("smpte2084" if mode in ("hdr10", "dv81") else "bt709")
            primaries, matrix = ("bt709", "bt709") if mode == "sdr" else ("bt2020", "bt2020nc")
            params = f"repeat-headers=1:frame-threads=1:bframes=0:colorprim={primaries}:transfer={transfer}:colormatrix={matrix}"
            cmd = [find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y", "-i", self.cfg["video"],
                   "-map", f"0:{self.info['stream']['index']}", "-vf", self._native_filters(),
                   "-c:v", "libx265", "-preset", self.cfg.get("preset", "medium"), "-crf", str(self.cfg.get("crf", 18)),
                   "-pix_fmt", "yuv420p10le", "-x265-params", params,
                   "-tag:v", "hvc1", "-video_track_timescale", str(self.rate.numerator), "-an", partial]
            if any(s.get("side_data_type") == "DOVI configuration record" for s in self.info["stream"].get("side_data_list", [])):
                help_text = self._run([find_tool("ffmpeg"), "-hide_banner", "-h", "encoder=libx265"])
                if "-dolbyvision" in help_text:
                    cmd[-1:-1] = ["-dolbyvision", "0"]
            self._run(cmd)
            data = self._validate_video(partial)
            video = next(s for s in data["streams"] if s["codec_type"] == "video")
            self.manifest["output_frames"] = int(video["nb_read_frames"])
            self.manifest["duration"] = self.manifest["output_frames"] / float(self.rate)
            self.manifest["fps"] = str(self.rate)
            if mode in ("dv81", "dv84"):
                self._validate_rpu(self.manifest["output_frames"])
            self._boundary()
            os.replace(partial, joined)
            self._stage_done("native")
        elif not joined.is_file():
            raise PipelineError("高精度转码断点文件缺失，请新建任务")
        self.progress(70)
        return self._inject_dv(joined)

    def run(self):
        self._started = time.monotonic()
        self._boundary()
        lock = None
        try:
            # One writer per workspace, including across separate GUI instances.
            self.root.mkdir(parents=True, exist_ok=True)
            lock = (self.root / ".lock").open("a+b")
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if lock:
                lock.close()
            raise PipelineError("工作目录正被另一个任务使用") from exc
        try:
            self._prepare()
            self.log(f"[任务] {self.manifest['job_id']}；暂停在当前命令完成后生效")
            if self.cfg["route"] == "preserve":
                self.stage("1/6 原片码流保留", 1, 6)
                self.manifest["output_frames"] = self.info["total_frames"]
                self.manifest["duration"] = self.info["duration"]
                self.manifest["fps"] = str(self.rate)
                self._stage_done("preserve")
                video = self.cfg["video"]
                self.progress(75)
            elif self.cfg["route"] == "transcode":
                video = self._native_video()
            else:
                source = self._extract()
                frames = self._interpolate(source)
                chunks = self._encode_batches(frames)
                video = self._join(chunks)
            audio = self._audio()
            output = self._mux(video, audio)
            checkpoint = self.root / "checkpoint.json"
            if checkpoint.exists():
                checkpoint.unlink()
            if self.cfg["auto_clean"]:
                # Keep manifest for traceability; remove only this job's artifacts.
                for child in self.job_dir.iterdir():
                    if child.name != "manifest.json":
                        self._clean(child)
            self.progress(100)
            self.log(f"[完成] 已验证输出: {output}")
            return output
        finally:
            lock.close()
