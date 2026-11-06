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
VERSION = "3.0"


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
    pcm_mb = (info["duration"] * 48000 * 3 * (16 if cfg.get("atmos_backend") == "aws" else 10)
              / 1024**2 * 1.2) if cfg.get("audio_mode") == "atmos_auto" else 0
    if cfg.get("route") in ("preserve", "transcode"):
        # Compressed output + staging copy. Native routes do not extract PNGs.
        bitrate = max(float(info.get("video_bit_rate") or info.get("bit_rate") or 0), 30_000_000)
        copies = 5 if cfg.get("color_mode") in ("dv81", "dv84") else 2
        analysis_mb = info["duration"] * max(float(info["fps"]), 60) * 1600 / 1024**2 if cfg.get("rpu_mode") == "auto" else 0
        mb = info["duration"] * bitrate / 8 / 1024**2 * copies * 1.5 + 256 + analysis_mb
        return mb + pcm_mb, "原片封装/高精度直编不拆帧；预留输出、空间音频母版与校验文件，实际体积随内容变化"
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
    mb = (raw + interpolated + sr + encoded) * 1.1 / 1024**2 + 512 + pcm_mb
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

    @staticmethod
    def _stop_process(process):
        # Only the process/group started by this Pipeline is eligible.
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10, **_hidden())
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                import signal
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.kill()
            process.wait()

    def _run(self, cmd, cwd=None, log_command=True):
        self._boundary()
        if log_command:
            self.log("[命令] " + subprocess.list2cmdline([str(a) for a in cmd]))
        # Keep draining the pipe during pause. Pause takes effect before the next command.
        process = subprocess.Popen([str(a) for a in cmd], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                   errors="replace", cwd=cwd,
                                   **(_hidden() if os.name == "nt" else {"start_new_session": True}))
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
                    self._stop_process(process)
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
                self._stop_process(process)
            reader.join(timeout=2)
            self._process = None

    def _save(self):
        self.manifest["elapsed"] = self._prior_elapsed + time.monotonic() - self._started
        payload = json.loads(json.dumps(self.manifest))
        if isinstance(payload.get("cfg", {}).get("cloud_atmos"), dict):
            # Upload consent belongs to this invocation, never to a saved job.
            payload["cfg"]["cloud_atmos"]["allow_upload"] = False
        _atomic_json(self.job_dir / "manifest.json", payload)
        _atomic_json(self.root / "checkpoint.json", payload)

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
        if cfg["route"] != "preserve" and self.info.get("dv_profile") == 5:
            raise PipelineError("Profile 5 使用专用 IPT 色彩编码，当前 CPU 制作流程不能直接转换；请先通过支持杜比解码的工具输出 HLG/PQ 母版，或选择原片保留")
        cfg.setdefault("source_transfer_override", "auto")
        if cfg["source_transfer_override"] not in ("auto", "bt709", "arib-std-b67", "smpte2084"):
            raise PipelineError("源动态范围须为自动、SDR、HLG 或 PQ")
        source_transfer = self.info.get("color_transfer")
        if cfg["source_transfer_override"] != "auto":
            if cfg["route"] == "preserve":
                raise PipelineError("原片保留不能更改源动态范围解释，请选择重新制作")
            source_transfer = cfg["source_transfer_override"]
        elif cfg["route"] != "preserve" and source_transfer in (None, "unknown", "unspecified", "reserved"):
            if self.info.get("color_primaries") == "bt2020" or self.info.get("color_space") in ("bt2020nc", "bt2020c"):
                raise PipelineError("源片使用 BT.2020 但缺少可确认的传递函数；请在调整设置中选择源动态范围（SDR、HLG 或 PQ），避免错误扩展")
        if cfg["route"] in ("preserve", "transcode"):
            cfg["ai"] = False
            cfg["interp"] = False
            cfg.setdefault("width", self.info.get("display_width", self.info["width"]))
            cfg.setdefault("height", self.info.get("display_height", self.info["height"]))
            if cfg["route"] == "preserve":
                cfg["width"], cfg["height"] = self.info["width"], self.info["height"]
            if cfg.get("color_mode", "source") == "source":
                cfg["color_mode"] = {"smpte2084": "hdr10", "arib-std-b67": "hlg"}.get(source_transfer, "sdr")
        cfg.setdefault("color_mode", "sdr")
        cfg.setdefault("audio_mode", "copy")
        if cfg.get("bili_one_click") and cfg["audio_mode"] != "hires":
            raise PipelineError("一键投稿使用本地自动音轨方案")
        cfg.setdefault("ai", True)
        cfg.setdefault("interp", True)
        cfg.setdefault("auto_clean", True)
        cfg.setdefault("width", 7680)
        cfg.setdefault("height", 4320)
        cfg.setdefault("batch_size", 64)
        cfg.setdefault("target_fps", 120)
        cfg.setdefault("model", "realesrgan-x4plus")
        self._dolby_defaults(cfg)
        if cfg["rpu_mode"] not in ("auto", "external"):
            raise PipelineError("杜比元数据模式须为 auto 或 external")
        if cfg["hdr_remaster"] not in ("auto", "off"):
            raise PipelineError("HDR 重制模式须为 auto 或 off")
        try:
            peak = float(cfg["hdr_peak_nits"])
        except (TypeError, ValueError):
            peak = float("nan")
        if not math.isfinite(peak) or not 100 <= peak <= 1000:
            raise PipelineError("HDR 高光目标须在 100 到 1000 nits 之间")
        if cfg.get("rpu") and cfg["rpu_mode"] == "auto":
            raise PipelineError("自动元数据与手动 RPU 不能同时使用")
        if cfg["color_mode"] not in ("sdr", "hdr10", "dv81", "hlg", "dv84"):
            raise PipelineError("不支持的色彩模式")
        if cfg["audio_mode"] not in ("copy", "aac", "eac3", "none", "atmos_auto", "hires"):
            raise PipelineError("不支持的音频模式")
        if type(cfg["ai_denoise"]) is not bool or (cfg["ai_denoise"] and cfg["audio_mode"] != "hires"):
            raise PipelineError("AI 人声降噪仅用于免费本地音频路线")
        if type(cfg["music_sr"]) is not bool or (cfg["music_sr"] and (cfg["audio_mode"] != "hires" or cfg["ai_denoise"])):
            raise PipelineError("请在免费方案中选择一项音频处理：AI 音质增强或人声降噪")
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
        if cfg["route"] == "enhance" and source_transfer in ("smpte2084", "arib-std-b67"):
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
            if cfg["rpu_mode"] == "auto" and cfg["color_mode"] != "dv84":
                raise PipelineError("自动逐帧分析当前支持 Profile 8.4；8.1 请选择外部配套 RPU，或改为 8.4")
            if cfg["rpu_mode"] == "external" and not cfg.get("rpu"):
                raise PipelineError("杜比视界需要与最终调色、HLG/PQ 基础层及帧数匹配的 RPU.bin；B站路线使用 Profile 8.4 / HLG")
            if cfg["rpu_mode"] == "auto":
                try:
                    import dolby_analysis
                except ImportError as exc:
                    raise PipelineError("缺少逐帧分析依赖，请运行 setup.bat -CoreOnly 安装") from exc
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
        self.free_audio = None
        self.consumer_tracks = []
        self.companion_audio = None
        self.companion_offset = 0
        self.consumer_atmos_note = ""
        if cfg.get("bili_one_click"):
            from consumer_audio import select_tracks
            external = audio_data["streams"] if cfg.get("audio") and Path(self.audio_source) != video else None
            self.consumer_tracks = select_tracks(self.info.get("audio_streams", ()), external)
            if self.consumer_tracks:
                track = self.consumer_tracks[0]
                self.audio_stream, self.free_audio = track["stream"], track["plan"]
                self.audio_source = str(video) if track["origin"] == "video" else cfg["audio"]
        if cfg["audio_mode"] == "hires":
            from hires_audio import choose_free_audio_stream, HiresAudioError
            if cfg["require_atmos"] or (cfg.get("cloud_atmos") or {}).get("allow_upload"):
                raise PipelineError("免费模式不使用云编码；强制制作 Atmos 请切换到双杜比方案")
            if not self.audio_stream and not cfg.get("bili_one_click"):
                raise PipelineError("文件没有音轨。请拖入音频母版，或选“仅杜比视界”导出静音视频")
        if cfg["audio_mode"] == "hires" and self.audio_stream:
            try:
                if not self.free_audio:
                    self.audio_stream, self.free_audio = choose_free_audio_stream(audio_data["streams"])
            except HiresAudioError as exc:
                raise PipelineError(str(exc)) from exc
            self.log("[免费本地] " + self.free_audio["detail"])
            if cfg["music_sr"]:
                from music_sr import sr_skip_reason
                from music_sr_backend import model_ready
                if not sr_skip_reason(self.free_audio) and not model_ready():
                    raise PipelineError("AI 音质增强模型尚未就绪，请运行项目内的 python music_sr_backend.py 安装")
            if cfg["ai_denoise"]:
                from ai_audio import ai_skip_reason, verify_ai_model
                from install_audio_model import MODEL_PATH
                if not ai_skip_reason(self.free_audio):
                    verify_ai_model(find_tool("ffmpeg"), MODEL_PATH, self._run)
        self.audio_offset = 0.0
        if self.audio_stream and Path(self.audio_source).resolve() == video:
            try:
                self.audio_offset = float(self.audio_stream.get("start_time") or 0) - float(self.info["stream"].get("start_time") or 0)
            except (TypeError, ValueError) as exc:
                raise PipelineError("无法确认源视频音画起点，未继续制作") from exc
            if not math.isfinite(self.audio_offset):
                raise PipelineError("源音画起点无效，未继续制作")
        if cfg.get("audio") and not self.audio_stream and cfg["audio_mode"] != "none":
            raise PipelineError("外部音频文件不包含音轨")
        if self.audio_stream and cfg["audio_mode"] == "copy":
            if self.audio_stream.get("codec_name") not in ("aac", "ac3", "eac3", "mp3", "alac"):
                raise PipelineError("此音轨不能直接复制到 MP4，请选择 AAC 或 E-AC-3 编码")
        self._generate_atmos = False
        if cfg["audio_mode"] == "atmos_auto" or cfg["require_atmos"]:
            if not self.audio_stream:
                raise PipelineError("双杜比需要声音：视频没有音轨，请拖入一个音频文件")
            audio_info = self._inspect(self.audio_source, audio_only=True)
            source_joc = audio_info["atmos"] == "confirmed" and audio_info["audio_codec"] == "eac3"
            if cfg["audio_mode"] == "atmos_auto" and not source_joc:
                from atmos_encoding import backend_identity, master_filter, AtmosError
                try:
                    # Validate layout and installation before expensive video processing.
                    master_filter(self.audio_stream.get("channels", 0), self.audio_stream.get("channel_layout"),
                                  self.info["duration"], 16 if cfg.get("atmos_backend") == "aws" else 10)
                    self.atmos_backend_identity = backend_identity(cfg, self._boundary)
                except AtmosError as exc:
                    raise PipelineError(str(exc)) from exc
                self._generate_atmos = True
            elif not source_joc or cfg["audio_mode"] not in ("copy", "atmos_auto"):
                raise PipelineError("双杜比要求真实 E-AC-3 JOC / Atmos；请选择自动 Atmos 制作或复制已有 JOC 音轨，普通 E-AC-3 / AAC 不会生成 Atmos")
            else:
                from quality import validate_atmos_audio
                readiness = validate_atmos_audio(audio_info, strict_sample_rate=False)
                if readiness["status"] == "fail":
                    raise PipelineError("源 Atmos 音轨不符合交付参数：" + readiness["summary"])
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
            self._dolby_defaults(original)
            current = {k: v for k, v in cfg.items() if k != "resume"}
            for settings in (original, current):
                if isinstance(settings.get("cloud_atmos"), dict):
                    settings["cloud_atmos"] = {k: v for k, v in settings["cloud_atmos"].items() if k != "allow_upload"}
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

    @staticmethod
    def _dolby_defaults(cfg):
        cfg.setdefault("bili_one_click", False)
        cfg.setdefault("ai_denoise", False)
        cfg.setdefault("music_sr", False)
        cfg.setdefault("source_transfer_override", "auto")
        cfg.setdefault("rpu_mode", "external" if cfg.get("rpu") else "auto")
        automatic = cfg.get("route") != "preserve" and cfg.get("color_mode") == "dv84" and cfg["rpu_mode"] == "auto"
        cfg.setdefault("hdr_remaster", "auto" if automatic else "off")
        cfg.setdefault("hdr_peak_nits", 600)
        if cfg.get("audio_mode") == "atmos_auto":
            cfg.setdefault("atmos_backend", "dee")

    def _file_hash(self, path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                self._boundary()
                digest.update(block)
        return digest.hexdigest()

    def _remaster_filter(self, source_transfer):
        """Used only after conversion to linear BT.2020, with 1 = 100 nits."""
        active = self.cfg["hdr_remaster"] == "auto" and source_transfer not in ("smpte2084", "arib-std-b67") and self.cfg["color_mode"] != "sdr"
        report = {"active": False, "source_transfer": source_transfer,
                  "reason": "HDR 源保持原动态范围" if source_transfer in ("smpte2084", "arib-std-b67") else "未启用 SDR 高光扩展"}
        if active:
            from hdr_remaster import write_lut
            report.update(write_lut(self.job_dir / "hdr-remaster.cube", self.cfg["hdr_peak_nits"], self._boundary))
            report["reason"] = "已对 SDR 应用保色相高光扩展"
        self.manifest["hdr_remaster"] = report
        return "lut3d=file=hdr-remaster.cube:interp=tetrahedral," if active else ""

    def _ensure_rpu(self, joined):
        wanted = self.manifest["output_frames"]
        if self.cfg["rpu_mode"] == "external":
            self.rpu_path = Path(self.cfg["rpu"])
            self._validate_rpu(wanted)
            return
        from dolby_analysis import METHOD, analyze_video, verify_rpu
        self.stage("4/6 逐帧分析并生成杜比元数据", 4, 6)
        folder = self.job_dir / "auto-dolby"
        if "auto_rpu" in self.manifest["completed"]:
            report = self.manifest.get("dolby_analysis") or {}
            self.rpu_path = Path(report.get("rpu", folder / "missing.bin"))
            if (report.get("method") != METHOD or not self.rpu_path.is_relative_to(folder) or not self.rpu_path.is_file()
                    or report.get("source_sha256") != self._file_hash(joined)
                    or report.get("rpu_sha256") != self._file_hash(self.rpu_path)):
                raise PipelineError("自动杜比分析缓存已改变，请新建任务，避免使用不匹配元数据")
            verify_rpu(find_tool("dovi_tool"), self.rpu_path, report, cancel_check=self._boundary)
        else:
            result = analyze_video(find_tool("ffmpeg"), joined, folder, wanted,
                                   dovi_tool=find_tool("dovi_tool"), ffprobe=find_tool("ffprobe"),
                                   profile="8.4", cancel_check=self._boundary,
                                   progress=lambda percent: self.progress(70 + 0.1 * percent))
            self.rpu_path = Path(result["rpu_path"])
            self.manifest["dolby_analysis"] = result["report"]
            self._stage_done("auto_rpu", check_cancel=False)
        self._validate_rpu(wanted)

    def _rpu_file(self):
        return str(getattr(self, "rpu_path", None) or self.cfg["rpu"])

    def _verify_final_rpu(self, video):
        """Extract through a pipe: validate every final RPU without a second HEVC copy."""
        from dolby_analysis import verify_rpu
        folder = self.job_dir / "auto-dolby"
        output = folder / "final-extracted.bin"
        log_path = folder / "final-extraction.log"
        output.unlink(missing_ok=True)
        decoder = extractor = None
        try:
            with log_path.open("wb") as log:
                decoder = subprocess.Popen([find_tool("ffmpeg"), "-nostdin", "-v", "error", "-i", str(video),
                    "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
                    stdout=subprocess.PIPE, stderr=log, **_hidden())
                extractor = subprocess.Popen([find_tool("dovi_tool"), "extract-rpu", "-i", "-", "-o", str(output)],
                    stdin=decoder.stdout, stdout=log, stderr=log, **_hidden())
                decoder.stdout.close()
                while decoder.poll() is None or extractor.poll() is None:
                    self._boundary()
                    if log_path.stat().st_size > 8 * 1024 * 1024:
                        raise PipelineError("最终杜比元数据提取日志超出限制")
                    if extractor.poll() is not None and extractor.returncode != 0:
                        break
                    self._cancel.wait(0.05)
                if decoder.poll() != 0 or extractor.poll() != 0:
                    raise PipelineError("最终杜比元数据提取失败，未发布成片")
            self.manifest["rpu_final_validation"] = verify_rpu(find_tool("dovi_tool"), output,
                self.manifest["dolby_analysis"], cancel_check=self._boundary)
        finally:
            for process in (extractor, decoder):
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            log_path.unlink(missing_ok=True)

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
        summary = self._run([find_tool("dovi_tool"), "info", "-i", self._rpu_file(), "-s"])
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
        with Path(self._rpu_file()).open("rb") as source:
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
                process = subprocess.Popen([find_tool("dovi_tool"), "info", "-i", self._rpu_file(), "-f", "0"],
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
                   + self._remaster_filter("bt709") +
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
                       "-tag:v", "hvc1", "-video_track_timescale", str(self.rate.numerator), "-an", partial], cwd=self.job_dir)
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
        self._ensure_rpu(joined)
        dv_video = self.job_dir / "dv.hevc"
        if "dv" not in self.manifest["completed"]:
            raw = self.job_dir / "base.hevc"
            self._run([find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y", "-i", joined,
                       "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb", raw])
            partial = self.job_dir / "dv.partial.hevc"
            self._run([find_tool("dovi_tool"), "inject-rpu", "-i", raw, "--rpu-in", self._rpu_file(), "-o", partial])
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
        duration = self.manifest.get("duration") or self.manifest["output_frames"] / float(self.rate)
        if getattr(self, "_generate_atmos", False):
            return self._generate_atmos_audio(duration)
        output = self.job_dir / "audio.mp4"
        if (self.cfg["music_sr"] or (self.cfg["bili_one_click"] and self.free_audio["action"] != "stereo_master")) and self._music_audio(output, duration):
            return output
        mode = self.free_audio["action"] if self.free_audio else self.cfg["audio_mode"]
        if self.cfg["ai_denoise"]:
            from ai_audio import analyze_ai_audio
            from install_audio_model import MODEL_PATH
            if "audio" not in self.manifest["completed"]:
                self.manifest["ai_denoise"] = analyze_ai_audio(
                    find_tool("ffmpeg"), self.audio_source, self.free_audio, duration, self.audio_offset,
                    MODEL_PATH, self._run)
            decision = self.manifest.get("ai_denoise")
            if not isinstance(decision, dict) or type(decision.get("applied")) is not bool:
                raise PipelineError("缺少降噪处理记录，请新建任务")
            if decision["applied"]:
                mode = "denoise"
            self.log("[AI 人声降噪] " + decision["detail"])
        copy_preroll = max(-self.audio_offset, 0) if mode in ("copy", "atmos_auto") else 0.0
        if (copy_preroll and "audio" in self.manifest["completed"]
                and self.manifest.get("audio_trimmed_preroll_seconds") != copy_preroll):
            # Old checkpoints kept all negative-PTS packets and applied a long
            # edit-list trim later. Re-extract locally; never re-encode JOC.
            self.manifest["completed"].remove("audio")
            self.log("[音频] 更新旧断点的音画起点，重新无损抽取音轨")
        if "audio" not in self.manifest["completed"]:
            partial = self.job_dir / "audio.partial.mp4"
            cmd = [find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y"]
            if copy_preroll:
                # Seek to the neighboring encoded packet before the first video
                # frame. A short edit list retains its exact sample position;
                # retaining every pre-roll packet made some ALAC decoders drop
                # one whole packet while interpreting a long Skip Samples value.
                seek = float(self.info["stream"].get("start_time") or 0) - float(self.info.get("format", {}).get("start_time") or 0)
                if not math.isfinite(seek) or seek < 0:
                    raise PipelineError("无法确认音频前滚在源容器中的起点，未继续复制")
                cmd += ["-ss", f"{seek:.9f}"]
            ordinal = self.free_audio["source_audio_ordinal"] if self.free_audio else 0
            cmd += ["-i", self.audio_source, "-map", f"0:a:{ordinal}", "-vn"]
            if mode in ("copy", "atmos_auto"):
                cmd += ["-c:a", "copy"]
                self.log("[音频] 直接复制音轨；已有 E-AC-3/Atmos 数据不重新编码，短音轨按原长保留")
            elif mode in ("alac", "pcm"):
                from hires_audio import hires_encode_args
                cmd += hires_encode_args(self.free_audio, duration, self.audio_offset)
            elif mode == "stereo_master":
                from consumer_audio import downmix_filter
                cmd += ["-af", downmix_filter(self.free_audio, duration, self.audio_offset),
                        "-c:a", "alac", "-sample_fmt", "s32p", "-movie_timescale", "48000"]
            elif mode == "denoise":
                from ai_audio import ai_encode_args
                cmd += ai_encode_args(self.free_audio, decision, duration, self.audio_offset)
            else:
                offset = round(self.audio_offset * 48000)
                align = "aresample=48000,asetpts=PTS-STARTPTS,"
                if offset > 0:
                    align += f"adelay={offset}S:all=1,"
                elif offset < 0:
                    align += f"apad,atrim=start_sample={-offset},asetpts=PTS-STARTPTS,"
                cmd += ["-af", align + "apad", "-ar", "48000", "-c:a", mode, "-b:a", "640k" if mode == "eac3" else "256k"]
                channels = int(self.audio_stream.get("channels", 2))
                if mode == "eac3" and channels > 6:
                    cmd += ["-ac", "6"]
                    self.log("[音频] E-AC-3 编码支持最多 5.1 声道，将下混；此编码不会生成 Atmos 对象元数据")
            # Copy keeps pre-roll packets for MP4 edit-list alignment. Do not
            # truncate the end before the negative start offset is applied.
            if mode not in ("alac", "pcm", "denoise", "stereo_master"):
                cmd += ["-t", f"{duration:.9f}"]
            cmd += [partial]
            if mode == "denoise":
                self._run(cmd, cwd=MODEL_PATH.parent)
            else:
                self._run(cmd)
            data = _json_command([find_tool("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(partial)])
            if not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
                raise PipelineError("音轨校验失败")
            os.replace(partial, output)
            self.manifest["audio_trimmed_preroll_seconds"] = copy_preroll
            self.manifest["audio_sha256"] = self._file_hash(output)
            if mode in ("copy", "atmos_auto") and not self.free_audio:
                self.manifest["atmos_audio"] = {"generated": False, "method": "source_stream_copy",
                                                "platform_verified": False}
            self._stage_done("audio")
        if not output.exists() or not output.stat().st_size:
            raise PipelineError("断点音轨缺失")
        if self.manifest.get("audio_sha256") and self.manifest["audio_sha256"] != self._file_hash(output):
            raise PipelineError("断点音轨已改变，请新建任务")
        self.progress(90)
        return output

    def _music_audio(self, output, duration):
        from music_sr import enhance_audio, verify_sr_output
        if "audio" in self.manifest["completed"]:
            report = self.manifest.get("music_sr") or {}
            if type(report.get("applied")) is not bool:
                raise PipelineError("断点缺少 AI 音质增强记录，请新建任务")
            if report["applied"] and (not output.is_file() or self._file_hash(output) != self.manifest.get("audio_sha256")):
                raise PipelineError("断点中的 AI 音轨已改变，请新建任务")
            return report["applied"]
        partial = self.job_dir / "music.partial.mp4"
        try:
            report = enhance_audio(find_tool("ffmpeg"), self.audio_source, self.free_audio, duration,
                                   self.audio_offset, partial, self.job_dir, self._run, self._boundary,
                                   official=self.cfg["bili_one_click"])
            self.log("[AI 音质增强] " + report["detail"])
            self.manifest["music_sr"] = report
            if not report["applied"]:
                return False
            verify_sr_output(self._inspect(partial, audio_only=True)["audio_streams"][0], report)
            os.replace(partial, output)
            self.manifest["audio_sha256"] = self._file_hash(output)
            self._stage_done("audio")
            self.progress(90)
            return True
        finally:
            partial.unlink(missing_ok=True)

    def _generate_atmos_audio(self, duration, *, companion_source=None, companion_offset=0):
        from atmos_encoding import prepare_master, encode_dee, AtmosError
        from quality import validate_atmos_audio
        folder = self.job_dir / "atmos"
        folder.mkdir(exist_ok=True)
        stage = "consumer_atmos" if companion_source is not None else "audio"
        output = self.job_dir / (stage + ".mp4")
        hash_field = stage + "_sha256"
        source = self.audio_source if companion_source is None else companion_source
        stream = self.audio_stream if companion_source is None else self._inspect(source, audio_only=True)["audio_streams"][0]
        offset = self.audio_offset if companion_source is None else companion_offset
        backend_cfg = self.cfg if companion_source is None else self.consumer_dee
        identity = {"backend": self.atmos_backend_identity,
                    "source_sha256": self._file_hash(source),
                    "samples": round(duration * 48000), "offset_samples": round(offset * 48000)}
        stored = self.manifest.get("atmos_audio") or {}
        if stored and stored.get("identity") != identity:
            raise PipelineError("Atmos 输入、混音或编码器配置已改变，请新建任务以避免复用错误音轨")
        if stage in self.manifest["completed"]:
            if not output.is_file() or self.manifest.get(hash_field) != self._file_hash(output):
                raise PipelineError("生成的 Atmos 缓存已改变，请新建任务")
            check = validate_atmos_audio(self._inspect(output, audio_only=True), duration)
            if check["status"] != "pass":
                raise PipelineError("Atmos 缓存未通过复核：" + check["summary"])
            return output
        master = folder / "master.wav"
        channels = 16 if backend_cfg.get("atmos_backend") == "aws" else 10
        if "atmos_master" in self.manifest["completed"]:
            if not master.is_file() or stored.get("master_sha256") != self._file_hash(master):
                raise PipelineError("空间音频母版缓存已改变，请新建任务")
        else:
            self.stage("5/6 制作空间音频母版", 5, 6)
            required_bytes = round(duration * 48000 * 3 * channels * 1.2) + 64 * 1024**2
            if shutil.disk_usage(folder).free < required_bytes:
                raise PipelineError("工作盘空间不足以保存空间音频母版，请清理空间或选择其他工作盘")
            partial_master = folder / "master.partial.wav"
            try:
                mix = prepare_master(find_tool("ffmpeg"), source, stream, duration, partial_master,
                                     self._run, channels, audio_offset=offset)
            except AtmosError as exc:
                raise PipelineError(str(exc)) from exc
            info = self._inspect(partial_master, audio_only=True)
            if (info["audio_channels"] != channels or info["audio_sample_rate"] != 48000
                    or info["audio_codec"] != "pcm_s24le" or abs(info["audio_duration"] - duration) > 1 / 48000 + 0.000001):
                raise PipelineError("空间音频母版的声道、精度或时长不符合编码输入要求")
            os.replace(partial_master, master)
            self.manifest["atmos_audio"] = {"generated": True, "identity": identity, "mix": mix,
                "master_sha256": self._file_hash(master), "platform_verified": False}
            self._stage_done("atmos_master")
        self.stage("5/6 编码真正的 Dolby Atmos", 5, 6)
        encoded_cache = folder / "encoded-verified.mp4"
        if "atmos_encode" in self.manifest["completed"]:
            evidence = self.manifest["atmos_audio"].get("encoding", {})
            if not encoded_cache.is_file() or evidence.get("sha256") != self._file_hash(encoded_cache):
                raise PipelineError("Atmos 编码缓存已改变，未继续封装")
        else:
            try:
                if backend_cfg.get("atmos_backend") == "aws":
                    from atmos_cloud import CloudAtmosConfig, encode_atmos_cloud, CloudAtmosCancelled, CloudAtmosError
                    cloud_output = folder / "downloaded.mp4"
                    try:
                        evidence = encode_atmos_cloud(CloudAtmosConfig(**self.cfg["cloud_atmos"]), master,
                            cloud_output, self._cancel, lambda p: self.progress(82 + .06 * p), self.log)
                    except CloudAtmosCancelled as exc:
                        raise PipelineCancelled(str(exc)) from exc
                    except CloudAtmosError as exc:
                        raise PipelineError(str(exc)) from exc
                    encoded = cloud_output
                else:
                    encoded, evidence = encode_dee(backend_cfg, master, folder, self._run)
            except AtmosError as exc:
                raise PipelineError(str(exc)) from exc
            self._run([find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y", "-i", encoded,
                       "-map", "0:a:0", "-c:a", "copy", "-map_metadata", "-1", "-t", f"{duration:.9f}", encoded_cache])
            info = self._inspect(encoded_cache, audio_only=True)
            check = validate_atmos_audio(info, duration)
            if check["status"] != "pass":
                raise PipelineError("编码器输出未通过真实 Atmos 检查（不会降级为普通音频）：" + check["summary"])
            self._run([find_tool("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-i", encoded_cache,
                       "-map", "0:a:0", "-f", "null", "-"])
            evidence.update(sha256=self._file_hash(encoded_cache), atmos_verified=True,
                            requires_media_validation=False, decode_verified=True,
                            verification_scope="local_joc_parameters_and_full_decode", checks=check["checks"])
            self.manifest["atmos_audio"]["encoding"] = evidence
            self._stage_done("atmos_encode", check_cancel=False)
        self._boundary()
        # Keep the validated encoded cache for resume; publish only a local copy.
        shutil.copyfile(encoded_cache, output)
        self.manifest[hash_field] = self._file_hash(output)
        self._stage_done(stage)
        self.progress(90)
        return output

    def _audio_payload_hash(self, path, ordinal=0):
        result = self._run([find_tool("ffmpeg"), "-nostdin", "-v", "error", "-i", path,
                            "-map", f"0:a:{ordinal}", "-c:a", "copy", "-f", "hash", "-hash", "sha256", "-"])
        found = re.search(r"SHA256=([0-9a-f]{64})", result, re.I)
        if not found:
            raise PipelineError("无法核对最终音频码流是否原样封装")
        return found.group(1).lower()

    def _mux(self, video, audio):
        self.stage("6/6 封装与验证 MP4", 6, 6)
        # The temporary final output stays on the output volume for atomic publication.
        partial = self.output.with_name(f".{self.output.stem}.{self.manifest['job_id']}.partial.mp4")
        cmd = [find_tool("ffmpeg"), "-nostdin", "-v", "warning", "-y"]
        denoised = bool(self.manifest.get("ai_denoise", {}).get("applied"))
        music_sr = self.manifest.get("music_sr") or {}
        extended = bool(music_sr.get("applied"))
        if self.cfg["route"] != "preserve" and self.cfg["color_mode"] in ("dv81", "dv84"):
            cmd += ["-fflags", "+genpts", "-r", str(self.rate)]
        if self.cfg["route"] == "preserve":
            # Normalize the selected video's origin, not the container's earliest
            # (possibly audio) stream, before aligning the separately muxed sound.
            video_origin = float(self.info["stream"].get("start_time") or 0)
            container_origin = float(self.info.get("format", {}).get("start_time") or 0)
            offset = container_origin - video_origin
            if offset:
                cmd += ["-itsoffset", f"{offset:.9f}"]
        cmd += ["-i", video]
        if audio:
            audio_offset = self.audio_offset + self.manifest.get("audio_trimmed_preroll_seconds", 0.0)
            copy_audio = self.free_audio["action"] == "copy" if self.free_audio else self.cfg["audio_mode"] in ("copy", "atmos_auto")
            if copy_audio and not (denoised or extended or self._generate_atmos) and audio_offset:
                # Each separate audio input is normalized by FFmpeg on reading;
                # restore its original position on the source video's timeline.
                cmd += ["-itsoffset", f"{audio_offset:.9f}"]
            cmd += ["-i", audio]
        if self.companion_audio:
            if self.companion_offset:
                cmd += ["-itsoffset", f"{self.companion_offset:.9f}"]
            cmd += ["-i", self.companion_audio]
        mapping = f"0:{self.info['stream']['index']}" if self.cfg["route"] == "preserve" else "0:v:0"
        cmd += ["-map", mapping]
        if audio:
            cmd += ["-map", "1:a:0"]
        if self.companion_audio:
            cmd += ["-map", "2:a:0", "-disposition:a:0", "default", "-disposition:a:1", "0"]
        cmd += ["-c", "copy"]
        if self.cfg["route"] != "preserve" or self.info["stream"]["codec_name"] == "hevc":
            cmd += ["-tag:v", "hvc1"]
        elif self.info["stream"]["codec_name"] == "h264":
            cmd += ["-tag:v", "avc1"]
        cmd += ["-movflags", "+faststart", "-strict", "unofficial", partial]
        if self.free_audio and (self.free_audio["action"] in ("alac", "pcm", "stereo_master") or denoised or extended):
            cmd[-1:-1] = ["-movie_timescale", str(music_sr["sample_rate"] if extended else self.free_audio["sample_rate"])]
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
                       "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"])
            if self.manifest.get("dolby_analysis"):
                self._verify_final_rpu(partial)
            from quality import assess_bilibili, validate_atmos_audio
            media = self._inspect(partial)
            media["path"] = str(self.output)
            media["filename"] = self.output.name
            media["format"]["filename"] = str(self.output)
            free_audio = None
            if self.free_audio:
                from hires_audio import verify_hires_audio, HiresAudioError
                free_audio = dict(self.free_audio, retained_atmos=False, platform_verified=False)
                if extended:
                    from music_sr import verify_sr_output
                    verify_sr_output(media["audio_streams"][0], music_sr)
                    free_audio.update(action="music_sr", output_codec=music_sr["output_codec"],
                                      output_bits=music_sr["output_bits"], output_sample_rate=music_sr["sample_rate"],
                                      eligible=False, detail=music_sr["detail"])
                if denoised:
                    from ai_audio import verify_ai_audio
                    decision = self.manifest["ai_denoise"]
                    checked = verify_ai_audio(find_tool("ffmpeg"), partial, media["audio_streams"][0],
                                              self.free_audio, decision, expected, self._run)
                    decision.update(checked)
                    free_audio.update(action="denoise", output_codec="alac", output_bits=16,
                                      eligible=False, detail=decision["detail"])
                if free_audio["action"] in ("alac", "pcm"):
                    try:
                        evidence = verify_hires_audio(find_tool("ffmpeg"), self.audio_source, partial,
                                                      media["audio_streams"][0], self.free_audio, expected,
                                                      self.audio_offset, self._run)
                    except HiresAudioError as exc:
                        raise PipelineError(str(exc)) from exc
                    free_audio.update(evidence)
                else:
                    if media["audio_codec"] != free_audio["output_codec"]:
                        raise PipelineError("免费模式输出音频编码与计划不符")
                    payload = self._audio_payload_hash(audio)
                    if payload != self._audio_payload_hash(partial):
                        raise PipelineError("最终封装改变了音频码流")
                    free_audio.update(mux_payload_sha256=payload, mux_payload_verified=True,
                                      retained_atmos=free_audio["action"] == "copy" and media["atmos"] == "confirmed" and media["audio_codec"] == "eac3")
                    if free_audio["retained_atmos"]:
                        free_audio["detail"] = "已免费保留源 E-AC-3 JOC / Atmos 音轨；未重新编码"
                self.manifest["free_audio"] = free_audio
            readiness = assess_bilibili(media, require_atmos=self.cfg["require_atmos"], free_audio=free_audio)
            if self.cfg["bili_one_click"]:
                from consumer_audio import verify_delivery
                consumer = verify_delivery(self, partial, media, free_audio, find_tool("ffmpeg"))
                self.manifest["consumer_audio"] = consumer
                readiness["summary"] = consumer["detail"] + "。平台标识待 B 站转码确认"
            if self.cfg["require_atmos"] or self.cfg["audio_mode"] == "atmos_auto":
                audio_check = validate_atmos_audio(media, expected if self._generate_atmos else None,
                                                   strict_sample_rate=self._generate_atmos)
                if audio_check["status"] == "fail":
                    raise PipelineError("最终文件未通过 Atmos 音轨检查，未发布输出：" + audio_check["summary"])
                before, after = self._audio_payload_hash(audio), self._audio_payload_hash(partial)
                if before != after:
                    raise PipelineError("最终封装改变了 Atmos 音频码流，未发布输出")
                self.manifest.setdefault("atmos_audio", {}).update(final_payload_sha256=after, payload_preserved=True)
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
            if self.cfg["bili_one_click"] and self.cfg.get("picture_plan"):
                report["export"]["picture_plan"] = self.cfg["picture_plan"]
            for key in ("hdr_remaster", "dolby_analysis", "rpu_final_validation", "atmos_audio", "free_audio", "ai_denoise", "music_sr", "consumer_audio"):
                if key == "atmos_audio" and self.cfg["bili_one_click"] and not self.companion_audio:
                    continue
                if self.manifest.get(key):
                    report["export"][key] = self.manifest[key]
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
        transfer = stream.get("color_transfer") if self.cfg["source_transfer_override"] == "auto" else self.cfg["source_transfer_override"]
        interpretation = ("explicit" if self.cfg["source_transfer_override"] != "auto" else
                          "file" if transfer in ("smpte2084", "arib-std-b67", "bt709", "iec61966-2-1", "bt2020-10", "bt2020-12") else "assumed_bt709")
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
        filters = "setpts=PTS-STARTPTS,"
        cadence = f"fps={self.rate},"
        if cfg.get("bili_one_click"):
            from consumer_picture import cadence_filter
            cadence = cadence_filter(cfg.get("picture_plan", {}), self.rate, self.info["duration"])
        after_resize = cfg.get("bili_one_click") and cfg.get("picture_plan", {}).get("method") == "motion_interpolation"
        if not after_resize:
            filters += cadence
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
        if after_resize:
            filters += cadence
        same_color = transfer == out_t and primaries == out_p and matrix == out_m
        self.manifest["hdr_remaster"] = {"active": False, "source_transfer": transfer,
                                        "reason": "HDR 源保持原动态范围" if hdr else "未启用 SDR 高光扩展"}
        if same_color and value_range == "limited":
            filters += "format=yuv420p10le"
        else:
            filters += (f"zscale=pin={primaries}:tin={transfer}:min={matrix}:rin={value_range}:"
                        f"p={primaries}:t=linear:m=gbr:r=full:npl=100,format=gbrpf32le,")
            if hdr and mode == "sdr":
                filters += "tonemap=tonemap=mobius:param=0.3:desat=0.5,"
            if mode != "sdr" and not hdr and cfg["hdr_remaster"] == "auto":
                filters += (f"zscale=pin={primaries}:tin=linear:min=gbr:rin=full:"
                            "p=bt2020:t=linear:m=gbr:r=full," + self._remaster_filter(transfer))
                primaries = "bt2020"
            filters += (f"zscale=pin={primaries}:tin=linear:min=gbr:rin=full:"
                        f"p={out_p}:t={out_t}:m={out_m}:r=limited:npl=100:dither=error_diffusion,format=yuv420p10le")
        self.manifest["hdr_remaster"]["source_transfer_interpretation"] = interpretation
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
            self._run(cmd, cwd=self.job_dir)
            expected = None
            if self.cfg.get("bili_one_click") and self.cfg.get("picture_plan", {}).get("method") == "motion_interpolation":
                expected = max(1, round(Fraction(str(self.info["duration"])) * self.rate))
            data = self._validate_video(partial, count=expected)
            video = next(s for s in data["streams"] if s["codec_type"] == "video")
            self.manifest["output_frames"] = int(video["nb_read_frames"])
            self.manifest["duration"] = self.manifest["output_frames"] / float(self.rate)
            self.manifest["fps"] = str(self.rate)
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
            if self.cfg["bili_one_click"]:
                from consumer_audio import prepare_companion
                prepare_companion(self, audio, find_tool("ffmpeg"))
            output = self._mux(video, audio)
            checkpoint = self.root / "checkpoint.json"
            if checkpoint.exists():
                checkpoint.unlink()
            if self.cfg["auto_clean"]:
                # Keep manifest for traceability; remove only this job's artifacts.
                for child in self.job_dir.iterdir():
                    if child.name != "manifest.json" and not (child.name == "auto-dolby" and self.manifest.get("dolby_analysis")):
                        self._clean(child)
            self.progress(100)
            self.log(f"[完成] 已验证输出: {output}")
            return output
        finally:
            lock.close()
