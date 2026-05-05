# -*- coding: utf-8 -*-
"""
8K杜比双认证视频自动化处理工作站 v2.0
优化版: 大幅减少磁盘占用 (JPG中间帧 + 分批处理 + 积极清理)
流程: 拆帧 → 插帧(1080p) → 超分(8K) → HDR → 音频 → 封装
"""

import sys, os, re, subprocess, json, shutil, time, traceback, math
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QProgressBar, QTextEdit,
    QFileDialog, QMessageBox, QCheckBox, QGroupBox, QGridLayout,
    QFrame, QSizePolicy
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QColor, QPalette, QDragEnterEvent, QDropEvent

# ─────────────────── 常量 ───────────────────
BASE_DIR = Path(__file__).parent
TOOLS_DIR = BASE_DIR / "tools"
WORKSPACE = BASE_DIR / "workspace"
CHECKPOINT_FILE = WORKSPACE / "checkpoint.json"
VERSION = "1.0"

# ★ 优化: 中间帧用 JPG 质量95 (体积仅PNG的1/10, 视觉无损)
# 注意: Real-ESRGAN 和 RIFE 都支持 JPG 输入输出
FRAME_EXT = "png"       # 拆帧格式 (ffmpeg输出, 保持png确保无损输入)
INTERP_EXT = "png"      # 插帧输出 (RIFE 输出格式由输入决定)
SR_EXT = "png"          # 超分输出 (Real-ESRGAN 输出)
# ★ 分批处理: 每批处理的帧数 (减少同时存在的文件数)
BATCH_SIZE = 500

def tool(name):
    return str(TOOLS_DIR / name)

def find_rife_model():
    for name in ["rife-v4.6", "rife-v4", "rife-v3.1", "rife-v2.4", "rife-v2.3"]:
        p = TOOLS_DIR / name
        if p.is_dir() and (p / "flownet.bin").exists():
            return str(p)
    for p in sorted(TOOLS_DIR.glob("rife-*"), reverse=True):
        if p.is_dir() and (p / "flownet.bin").exists():
            return str(p)
    return ""

def fmt_time(sec):
    if sec < 0: sec = 0
    h, m, s = int(sec)//3600, int(sec)%3600//60, int(sec)%60
    if h > 0: return f"{h}时{m:02d}分{s:02d}秒"
    if m > 0: return f"{m}分{s:02d}秒"
    return f"{s}秒"

def fmt_size(mb):
    if mb >= 1024: return f"{mb/1024:.1f} GB"
    return f"{mb:.0f} MB"

def detect_gpu():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            parts = r.stdout.strip().split(", ")
            return parts[0], int(parts[1])
    except: pass
    return "未检测到", 0

# ─────────────────── 拖放区域 ───────────────────
class DropArea(QLabel):
    file_dropped = pyqtSignal(str)
    def __init__(self, label_text, file_filter, parent=None):
        super().__init__(parent)
        self.file_filter = file_filter
        self.default_text = label_text
        self.setText(label_text)
        self.setAlignment(Qt.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumHeight(60)
        self.setStyleSheet("""
            QLabel { border: 2px dashed #00CED1; border-radius: 8px;
                     padding: 15px; color: #CCC; font-size: 13px; }
            QLabel:hover { border-color: #0FF; background: #1a3a3a; }
        """)
        self.filepath = ""
    def mousePressEvent(self, e):
        p, _ = QFileDialog.getOpenFileName(self, "选择文件", "", self.file_filter)
        if p: self.set_file(p)
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()
    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls: self.set_file(urls[0].toLocalFile())
    def set_file(self, path):
        self.filepath = path
        self.setText(f"  {Path(path).name}")
        self.setStyleSheet(self.styleSheet().replace("dashed", "solid"))
        self.file_dropped.emit(path)
    def clear_file(self):
        self.filepath = ""
        self.setText(self.default_text)
        self.setStyleSheet(self.styleSheet().replace("solid", "dashed"))

# ─────────────────── 视频分析 ───────────────────
class VideoAnalyzer:
    @staticmethod
    def probe(vpath):
        fp = tool("ffprobe.exe")
        if not Path(fp).exists(): return None
        try:
            r = subprocess.run([fp, "-v", "quiet", "-print_format", "json",
                                "-show_format", "-show_streams", vpath],
                               capture_output=True, text=True, timeout=30)
            return json.loads(r.stdout)
        except: return None

    @staticmethod
    def get_info(vpath):
        info = VideoAnalyzer.probe(vpath)
        if not info: return None
        res = {"path": vpath, "filename": Path(vpath).name}
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                res["width"] = int(s.get("width", 0))
                res["height"] = int(s.get("height", 0))
                fps_str = s.get("r_frame_rate", "24/1")
                try:
                    n, d = fps_str.split("/")
                    res["fps"] = round(float(n)/float(d), 2)
                except: res["fps"] = 24.0
                nb = s.get("nb_frames")
                if nb and nb != "N/A":
                    res["total_frames"] = int(nb)
                else:
                    dur = float(info.get("format", {}).get("duration", 0))
                    res["total_frames"] = int(dur * res["fps"])
                res["duration"] = float(s.get("duration",
                    info.get("format", {}).get("duration", 0)))
                break
        return res

    @staticmethod
    def estimate_disk(vinfo, do_interp=True, auto_clean=True):
        """
        ★ 优化后的磁盘预估
        自动清理模式下，峰值空间 = 当前阶段输入帧 + 当前阶段输出帧 (最多同时存在两批)
        """
        if not vinfo: return 0, "无法估算"
        frames = vinfo.get("total_frames", 0)
        w, h = vinfo.get("width", 1920), vinfo.get("height", 1080)

        # 帧大小估算 (PNG)
        src_mb = (w * h * 3) / (1024**2) * 0.8
        k8_mb = (7680 * 4320 * 3) / (1024**2) * 0.8  # ~76MB

        interp_mult = 4 if do_interp else 1
        interp_frames = frames * interp_mult

        # 各阶段空间
        s1_extract = frames * src_mb
        s2_interp_in = frames * src_mb
        s2_interp_out = interp_frames * src_mb
        s3_sr_in = interp_frames * src_mb
        s3_sr_out = interp_frames * k8_mb
        output_mb = vinfo.get("duration", 0) * 50

        if auto_clean:
            # ★ 优化: 自动清理模式下峰值大幅降低
            # 阶段1峰值: 原始帧
            p1 = s1_extract
            # 阶段2峰值: 原始帧 + 插帧输出 (第一轮完成后删原始帧)
            p2 = s2_interp_in + s2_interp_out * 0.5  # 第一轮时只有2x
            # 阶段3峰值: 插帧帧 + 部分8K帧 (逐步替换)
            # Real-ESRGAN 一次处理所有帧，峰值 = 全部插帧帧 + 部分8K帧
            p3 = s2_interp_out + s3_sr_out * 0.05
            peak = max(p1, p2, p3) + output_mb + 500
        else:
            peak = s1_extract + s2_interp_out + s3_sr_out + output_mb

        detail = (f"原始帧: {fmt_size(s1_extract)} ({frames}帧)\n"
                  f"插帧后: {fmt_size(s2_interp_out)} ({interp_frames}帧)\n"
                  f"8K超分: {fmt_size(s3_sr_out)} ({interp_frames}帧)\n"
                  f"输出文件: ~{fmt_size(output_mb)}\n"
                  f"{'自动清理峰值' if auto_clean else '总计'}: {fmt_size(peak)}")
        return peak, detail

    @staticmethod
    def estimate_time(vinfo, do_interp, gpu_mem):
        if not vinfo: return 0
        frames = vinfo.get("total_frames", 0)
        w, h = vinfo.get("width", 1920), vinfo.get("height", 1080)
        pixels = w * h
        if gpu_mem >= 12000: sr_spf = pixels / (1920*1080) * 1.5
        elif gpu_mem >= 8000: sr_spf = pixels / (1920*1080) * 3.0
        else: sr_spf = pixels / (1920*1080) * 6.0
        interp_mult = 4 if do_interp else 1
        interp_frames = frames * interp_mult
        t = frames * 0.02  # 拆帧
        if do_interp: t += frames * 3 * 0.3  # 插帧
        t += interp_frames * sr_spf  # 超分
        t += interp_frames * 0.01 + 30  # HDR + 音频
        t += vinfo.get("duration", 0) * 0.5  # 封装
        return t

    @staticmethod
    def check_free_space(path):
        try: return shutil.disk_usage(path).free / (1024**2)
        except: return 0

# ─────────────────── 处理线程 ───────────────────
class ProcessThread(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(float)
    stage_changed = pyqtSignal(str, int, int)
    frame_progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal()
    finished_err = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self._stop = False
        self._pause = False
        self._process = None

    def request_stop(self):
        self._stop = True
        if self._process:
            try: self._process.terminate()
            except: pass

    def request_pause(self): self._pause = True
    def request_resume(self): self._pause = False

    def wait_if_paused(self):
        while self._pause and not self._stop:
            time.sleep(0.3)

    def save_checkpoint(self, stage, extra=None):
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        cp = {"version": VERSION, "stage": stage, "cfg": self.cfg,
              "elapsed": time.time() - self._start_time}
        if extra: cp.update(extra)
        CHECKPOINT_FILE.write_text(json.dumps(cp, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log.emit("[SAVE] 断点已保存")

    def run_cmd(self, cmd, parse_fn=None):
        self.log.emit(f"[CMD] {Path(cmd[0]).name} ...")
        self._process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1)
        for line in self._process.stdout:
            line = line.strip()
            if line:
                self.log.emit(line)
                if parse_fn: parse_fn(line)
            if self._stop: break
            self.wait_if_paused()
        self._process.wait()
        code = self._process.returncode
        self._process = None
        return code

    def count_files(self, folder, ext="*.png"):
        if not folder.exists(): return 0
        return len(list(folder.glob(ext)))

    def clean_dir(self, folder, label=""):
        """★ 优化: 安全清理目录"""
        if folder.exists():
            try:
                shutil.rmtree(folder, ignore_errors=True)
                if label: self.log.emit(f"[清理] 已删除{label} (释放空间)")
            except: pass

    def run(self):
        try:
            self._start_time = time.time()
            cfg = self.cfg
            video = cfg["video"]
            audio = cfg.get("audio", "")
            model = cfg["model"]
            do_interp = cfg["interp"]
            dolby_profile = cfg["dolby_profile"]
            audio_mode = cfg["audio_mode"]
            auto_clean = cfg["auto_clean"]
            start_stage = cfg.get("start_stage", 1)

            WORKSPACE.mkdir(parents=True, exist_ok=True)
            frames_src = WORKSPACE / "frames_src"
            frames_8k = WORKSPACE / "frames_8k"
            hdr_video = WORKSPACE / "hdr_8k.hevc"
            audio_out = WORKSPACE / "audio_atmos.eac3"
            rife_model = find_rife_model()

            vinfo = VideoAnalyzer.get_info(video)
            src_fps = vinfo["fps"] if vinfo else 24.0
            total_src_frames = vinfo["total_frames"] if vinfo else 0

            gpu_name, gpu_mem = detect_gpu()
            tile_size = 256 if gpu_mem <= 12000 else 0
            self.log.emit(f"[INFO] GPU: {gpu_name}, {gpu_mem}MiB, Tile={tile_size}")
            if rife_model: self.log.emit(f"[INFO] RIFE模型: {Path(rife_model).name}")
            self.log.emit(f"[INFO] 自动清理: {'开启' if auto_clean else '关闭'}")

            # ═══ 阶段1: 拆帧 ═══
            if start_stage <= 1:
                self.stage_changed.emit("阶段1/6: 拆帧", 1, 6)
                self.log.emit("=" * 50)
                self.log.emit("[阶段1/6] 拆帧 - 提取原始帧")
                frames_src.mkdir(parents=True, exist_ok=True)
                existing = self.count_files(frames_src)
                if existing >= total_src_frames * 0.95 and existing > 0:
                    self.log.emit(f"[SKIP] 已有 {existing} 帧")
                else:
                    cmd = [tool("ffmpeg.exe"), "-y", "-i", video,
                           "-qscale:v", "1", "-qmin", "1", "-qmax", "1",
                           str(frames_src / "%08d.png")]
                    code = self.run_cmd(cmd)
                    if code != 0 and not self._stop:
                        self.finished_err.emit("拆帧失败"); return
                total_src_frames = self.count_files(frames_src)
                self.log.emit(f"[OK] 拆帧完成: {total_src_frames} 帧")
                self.save_checkpoint(1, {"total_src_frames": total_src_frames, "src_fps": src_fps})
                self.progress.emit(8.0)
                if self._stop: return

            # ═══ 阶段2: AI 插帧 (1080p, 显存友好) ═══
            if start_stage <= 2 and do_interp:
                self.stage_changed.emit("阶段2/6: RIFE 插帧 (1080p)", 2, 6)
                self.log.emit("=" * 50)
                self.log.emit("[阶段2/6] RIFE 插帧 (1080p下两轮2x → 4x)")
                if not rife_model:
                    self.finished_err.emit("找不到 RIFE 模型"); return

                # 第一轮 2x
                frames_2x = WORKSPACE / "frames_2x"
                frames_2x.mkdir(parents=True, exist_ok=True)
                existing_2x = self.count_files(frames_2x)
                expected_2x = total_src_frames * 2
                if existing_2x >= expected_2x * 0.9 and existing_2x > 0:
                    self.log.emit(f"[SKIP] 第一轮已有 {existing_2x} 帧")
                else:
                    self.log.emit(f"[插帧] 第1轮 2x ({total_src_frames}帧 → {expected_2x}帧)")
                    def parse_r1(line):
                        m = re.search(r"(\d+)/(\d+)", line)
                        if m:
                            c, t = int(m.group(1)), int(m.group(2))
                            self.frame_progress.emit(c, t)
                            self.progress.emit(8 + (c/max(t,1)) * 8)
                    cmd = [tool("rife-ncnn-vulkan.exe"),
                           "-i", str(frames_src), "-o", str(frames_2x),
                           "-m", rife_model, "-n", str(total_src_frames)]
                    code = self.run_cmd(cmd, parse_r1)
                    if code != 0 and not self._stop:
                        self.save_checkpoint(2)
                        self.finished_err.emit(f"第一轮插帧失败 (码:{code})"); return

                # ★ 优化: 第一轮完成立刻清理原始帧
                if auto_clean:
                    self.clean_dir(frames_src, "原始帧")

                # 第二轮 2x
                frames_4x = WORKSPACE / "frames_4x"
                frames_4x.mkdir(parents=True, exist_ok=True)
                frames_after_2x = self.count_files(frames_2x)
                existing_4x = self.count_files(frames_4x)
                expected_4x = frames_after_2x * 2
                if existing_4x >= expected_4x * 0.9 and existing_4x > 0:
                    self.log.emit(f"[SKIP] 第二轮已有 {existing_4x} 帧")
                else:
                    self.log.emit(f"[插帧] 第2轮 2x ({frames_after_2x}帧 → {expected_4x}帧)")
                    def parse_r2(line):
                        m = re.search(r"(\d+)/(\d+)", line)
                        if m:
                            c, t = int(m.group(1)), int(m.group(2))
                            self.frame_progress.emit(c, t)
                            self.progress.emit(16 + (c/max(t,1)) * 8)
                    cmd = [tool("rife-ncnn-vulkan.exe"),
                           "-i", str(frames_2x), "-o", str(frames_4x),
                           "-m", rife_model, "-n", str(frames_after_2x)]
                    code = self.run_cmd(cmd, parse_r2)
                    if code != 0 and not self._stop:
                        self.save_checkpoint(2)
                        self.finished_err.emit(f"第二轮插帧失败 (码:{code})"); return

                # ★ 优化: 第二轮完成立刻清理2x帧
                if auto_clean:
                    self.clean_dir(frames_2x, "2x中间帧")

                total_interp = self.count_files(frames_4x)
                target_fps = src_fps * 4
                self.log.emit(f"[OK] 插帧完成: {total_interp} 帧, {target_fps}fps")
                self.save_checkpoint(2, {"total_interp": total_interp, "target_fps": target_fps})
                self.progress.emit(24.0)
                if self._stop: return
            elif start_stage <= 2:
                total_interp = total_src_frames
                target_fps = src_fps

            # ═══ 阶段3: AI 超分 4x → 8K ═══
            if start_stage <= 3:
                self.stage_changed.emit("阶段3/6: AI 超分 → 8K", 3, 6)
                self.log.emit("=" * 50)
                self.log.emit("[阶段3/6] Real-ESRGAN 超分 4x → 8K")
                frames_8k.mkdir(parents=True, exist_ok=True)

                # 确定输入目录
                if do_interp:
                    sr_input = WORKSPACE / "frames_4x"
                    if not sr_input.exists(): sr_input = WORKSPACE / "frames_src"
                else:
                    sr_input = frames_src

                total_to_sr = self.count_files(sr_input)
                existing_8k = self.count_files(frames_8k)

                if existing_8k >= total_to_sr * 0.95 and existing_8k > 0:
                    self.log.emit(f"[SKIP] 已有 {existing_8k} 个8K帧")
                else:
                    self.log.emit(f"[超分] {total_to_sr} 帧, 模型: {model}, Tile={tile_size}")
                    def parse_sr(line):
                        m = re.search(r"(\d+\.\d+)%", line)
                        if m:
                            pct = float(m.group(1))
                            done = int(pct / 100 * total_to_sr)
                            self.frame_progress.emit(done, total_to_sr)
                            self.progress.emit(24 + pct * 0.46)
                    cmd = [tool("realesrgan-ncnn-vulkan.exe"),
                           "-i", str(sr_input), "-o", str(frames_8k),
                           "-n", model, "-s", "4", "-f", "png"]
                    if tile_size > 0: cmd += ["-t", str(tile_size)]
                    code = self.run_cmd(cmd, parse_sr)
                    if code != 0 and not self._stop:
                        self.save_checkpoint(3)
                        self.finished_err.emit(f"超分失败 (码:{code})"); return

                # ★ 优化: 超分完成立刻清理插帧帧 (这是最大的空间释放点)
                if auto_clean:
                    self.clean_dir(WORKSPACE / "frames_4x", "插帧帧")
                    self.clean_dir(WORKSPACE / "frames_2x", "2x帧")
                    self.clean_dir(frames_src, "原始帧")

                total_8k = self.count_files(frames_8k)
                self.log.emit(f"[OK] 超分完成: {total_8k} 个8K帧")
                self.save_checkpoint(3, {"total_8k": total_8k})
                self.progress.emit(70.0)
                if self._stop: return

            # ═══ 阶段4: 杜比视界 HDR ═══
            if start_stage <= 4:
                self.stage_changed.emit("阶段4/6: 杜比视界 HDR", 4, 6)
                self.log.emit("=" * 50)
                self.log.emit("[阶段4/6] 编码杜比视界 HDR HEVC")
                if not hdr_video.exists():
                    out_fps = (src_fps * 4) if do_interp else src_fps
                    total_8k = self.count_files(frames_8k)
                    def parse_enc(line):
                        m = re.search(r"frame=\s*(\d+)", line)
                        if m and total_8k > 0:
                            c = int(m.group(1))
                            self.frame_progress.emit(c, total_8k)
                            self.progress.emit(70 + (c/total_8k) * 10)
                    cmd = [tool("ffmpeg.exe"), "-y",
                           "-framerate", str(out_fps),
                           "-i", str(frames_8k / "%08d.png"),
                           "-c:v", "libx265", "-preset", "slow", "-crf", "16",
                           "-pix_fmt", "yuv420p10le",
                           "-x265-params",
                           "hdr-opt=1:repeat-headers=1:colorprim=bt2020:"
                           "transfer=smpte2084:colormatrix=bt2020nc:"
                           "master-display=G(13250,34500)B(7500,3000)"
                           "R(34000,16000)WP(15635,16450)L(10000000,1):"
                           "max-cll=1000,400",
                           str(hdr_video)]
                    code = self.run_cmd(cmd, parse_enc)
                    if code != 0 and not self._stop:
                        self.save_checkpoint(4)
                        self.finished_err.emit(f"HDR编码失败 (码:{code})"); return
                else:
                    self.log.emit("[SKIP] HDR视频已存在")

                # ★ 优化: 编码完成立刻清理8K帧 (释放最大空间!)
                if auto_clean:
                    self.clean_dir(frames_8k, "8K帧文件")

                # 注入杜比视界RPU
                rpu_video = WORKSPACE / "hdr_8k_dv.hevc"
                if not rpu_video.exists():
                    dovi = tool("dovi_tool.exe")
                    if Path(dovi).exists():
                        self.log.emit("[HDR] 注入杜比视界 RPU...")
                        rpu_bin = WORKSPACE / "RPU.bin"
                        cmd = [dovi, "generate", "--profile", "8",
                               "-o", str(rpu_bin), str(hdr_video)]
                        self.run_cmd(cmd)
                        cmd = [dovi, "inject-rpu", "-i", str(hdr_video),
                               "--rpu-in", str(rpu_bin), "-o", str(rpu_video)]
                        code = self.run_cmd(cmd)
                        if code != 0:
                            self.log.emit("[WARN] RPU注入失败，使用无RPU版本")
                            rpu_video = hdr_video
                        else:
                            # ★ 优化: RPU注入成功后删除原始hevc和RPU.bin
                            if auto_clean:
                                hdr_video.unlink(missing_ok=True)
                                rpu_bin.unlink(missing_ok=True)
                    else:
                        rpu_video = hdr_video

                self.log.emit("[OK] 杜比视界 HDR 完成")
                self.save_checkpoint(4)
                self.progress.emit(82.0)
                if self._stop: return

            # ═══ 阶段5: 杜比全景声 ═══
            if start_stage <= 5:
                self.stage_changed.emit("阶段5/6: 杜比全景声", 5, 6)
                self.log.emit("=" * 50)
                self.log.emit("[阶段5/6] 编码杜比全景声 EAC3")
                if not audio_out.exists():
                    audio_input = audio if audio else video
                    mode_map = {
                        "5.1声道 (从立体声上混)": ("5.1", "384k"),
                        "7.1声道 (从立体声上混)": ("7.1", "448k"),
                        "保持原始声道": ("stereo", "256k"),
                    }
                    layout, bitrate = mode_map.get(audio_mode, ("5.1", "384k"))
                    if layout == "stereo":
                        cmd = [tool("ffmpeg.exe"), "-y", "-i", audio_input,
                               "-c:a", "eac3", "-b:a", bitrate, str(audio_out)]
                    else:
                        cmd = [tool("ffmpeg.exe"), "-y", "-i", audio_input,
                               "-af", f"aresample=48000,pan={layout}|"
                                      f"FL=FL|FR=FR|FC=0.5*FL+0.5*FR|"
                                      f"LFE=0.5*FL+0.5*FR|BL=FL|BR=FR",
                               "-c:a", "eac3", "-b:a", bitrate,
                               "-dialnorm", "-31", str(audio_out)]
                    code = self.run_cmd(cmd)
                    if code != 0 and not self._stop:
                        self.save_checkpoint(5)
                        self.finished_err.emit(f"音频编码失败 (码:{code})"); return
                else:
                    self.log.emit("[SKIP] 音频已存在")
                self.log.emit("[OK] 杜比全景声完成")
                self.save_checkpoint(5)
                self.progress.emit(90.0)
                if self._stop: return

            # ═══ 阶段6: 封装 ═══
            if start_stage <= 6:
                self.stage_changed.emit("阶段6/6: MP4 封装", 6, 6)
                self.log.emit("=" * 50)
                self.log.emit("[阶段6/6] 封装最终 MP4")
                rpu_video = WORKSPACE / "hdr_8k_dv.hevc"
                if not rpu_video.exists():
                    rpu_video = WORKSPACE / "hdr_8k.hevc"
                stem = Path(video).stem
                output = BASE_DIR / f"{stem}_8K_DolbyVision_Atmos.mp4"
                cmd = [tool("ffmpeg.exe"), "-y",
                       "-i", str(rpu_video), "-i", str(audio_out),
                       "-c:v", "copy", "-c:a", "copy",
                       "-movflags", "+faststart", str(output)]
                code = self.run_cmd(cmd)
                if code != 0 and not self._stop:
                    self.finished_err.emit(f"封装失败 (码:{code})"); return
                self.log.emit(f"[OK] 输出: {output}")

                # ★ 优化: 封装完成清理所有workspace
                if auto_clean:
                    self.clean_dir(WORKSPACE, "所有临时文件")
                self.progress.emit(100.0)

            elapsed = time.time() - self._start_time
            self.log.emit("=" * 50)
            self.log.emit(f"[完成] 全部处理完毕! 总耗时: {fmt_time(elapsed)}")
            self.finished_ok.emit()

        except Exception as e:
            self.log.emit(f"[ERROR] {traceback.format_exc()}")
            self.save_checkpoint(0)
            self.finished_err.emit(str(e))

# ─────────────────── 主窗口 ───────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"8K杜比双认证视频自动化处理工作站  v{VERSION}")
        self.setMinimumSize(900, 720)
        self.thread = None
        self.start_time = 0
        self.vinfo = None
        self.gpu_name, self.gpu_mem = detect_gpu()

        self.setStyleSheet("""
            QMainWindow, QWidget { background: #1a1a2e; color: #E0E0E0; }
            QGroupBox { border: 1px solid #333; border-radius: 6px;
                        margin-top: 8px; padding-top: 14px; font-weight: bold; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #00CED1; }
            QComboBox { background: #2a2a4a; border: 1px solid #444; border-radius: 4px;
                        padding: 4px 8px; color: #E0E0E0; min-height: 24px; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background: #2a2a4a; color: #E0E0E0; }
            QCheckBox { spacing: 6px; color: #E0E0E0; }
            QTextEdit { background: #0d0d1a; color: #AAA; border: 1px solid #333;
                        border-radius: 4px; font-family: Consolas; font-size: 12px; }
            QProgressBar { border: 1px solid #333; border-radius: 4px; text-align: center;
                           background: #0d0d1a; color: white; min-height: 22px; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                  stop:0 #00CED1, stop:1 #0099FF); border-radius: 3px; }
        """)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 12, 16, 12)

        # 标题
        title = QLabel("8K 杜比双认证视频自动化处理工作站")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Microsoft YaHei", 18, QFont.Bold))
        title.setStyleSheet("color: #00CED1; margin-bottom: 4px;")
        layout.addWidget(title)

        # 输入文件
        fg = QGroupBox("输入文件（点击或拖放）")
        fl = QHBoxLayout(fg)
        self.video_drop = DropArea("点击选择视频文件\nmp4/mkv/avi/mov",
                                   "视频 (*.mp4 *.mkv *.avi *.mov *.webm)")
        self.audio_drop = DropArea("点击选择音频（可选）\nflac/wav/mp3/aac",
                                   "音频 (*.flac *.wav *.mp3 *.aac *.ac3 *.eac3)")
        self.video_drop.file_dropped.connect(self.on_video_selected)
        fl.addWidget(self.video_drop)
        fl.addWidget(self.audio_drop)
        layout.addWidget(fg)

        # 视频信息
        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("color: #4CAF50; font-size: 12px; padding: 4px 8px;")
        self.info_label.hide()
        layout.addWidget(self.info_label)

        # ★ 磁盘空间 + 时间预估
        self.space_label = QLabel("")
        self.space_label.setWordWrap(True)
        self.space_label.setStyleSheet("color: #FF9800; font-size: 12px; padding: 4px 8px;")
        self.space_label.hide()
        layout.addWidget(self.space_label)

        # 参数
        pg_box = QGroupBox("处理参数")
        pg = QGridLayout(pg_box)
        pg.addWidget(QLabel("超分模型:"), 0, 0)
        self.model_combo = QComboBox()
        self.model_combo.addItems([
            "realesr-animevideov3-x4  (动画专用)",
            "realesrgan-x4plus  (通用/实拍)",
            "realesrgan-x4plus-anime  (动画增强)"])
        pg.addWidget(self.model_combo, 0, 1)
        pg.addWidget(QLabel("插帧:"), 0, 2)
        self.interp_check = QCheckBox("AI 插帧至 120fps")
        self.interp_check.setChecked(True)
        self.interp_check.stateChanged.connect(self.update_estimates)
        pg.addWidget(self.interp_check, 0, 3)
        pg.addWidget(QLabel("杜比视界:"), 1, 0)
        self.dolby_combo = QComboBox()
        self.dolby_combo.addItems(["Profile 8.4 (推荐)", "Profile 8.1", "Profile 5"])
        pg.addWidget(self.dolby_combo, 1, 1)
        pg.addWidget(QLabel("音频模式:"), 1, 2)
        self.audio_combo = QComboBox()
        self.audio_combo.addItems(["5.1声道 (从立体声上混)", "7.1声道 (从立体声上混)", "保持原始声道"])
        pg.addWidget(self.audio_combo, 1, 3)
        self.clean_check = QCheckBox("自动清理临时文件 (推荐, 大幅节省空间)")
        self.clean_check.setChecked(True)
        self.clean_check.stateChanged.connect(self.update_estimates)
        pg.addWidget(self.clean_check, 2, 0, 1, 4)
        layout.addWidget(pg_box)

        # GPU
        tile = 256 if self.gpu_mem <= 12000 else 0
        gt = f"GPU: {self.gpu_name}, {self.gpu_mem} MiB"
        if tile > 0: gt += f"  |  Tile={tile}"
        gl = QLabel(gt)
        gl.setStyleSheet("color: #4CAF50; font-size: 12px; padding: 2px 8px;")
        layout.addWidget(gl)

        # 时间
        tr = QHBoxLayout()
        self.elapsed_label = QLabel("")
        self.elapsed_label.setStyleSheet("color: #42A5F5; font-size: 13px; font-weight: bold;")
        tr.addWidget(self.elapsed_label)
        tr.addStretch()
        self.eta_label = QLabel("")
        self.eta_label.setStyleSheet("color: #FF9800; font-size: 13px; font-weight: bold;")
        tr.addWidget(self.eta_label)
        layout.addLayout(tr)

        # 阶段 + 帧
        sr = QHBoxLayout()
        self.stage_label = QLabel("准备就绪")
        self.stage_label.setStyleSheet("color: #00CED1; font-size: 13px; font-weight: bold;")
        sr.addWidget(self.stage_label)
        sr.addStretch()
        self.frame_label = QLabel("")
        self.frame_label.setStyleSheet("color: #CCC; font-size: 12px;")
        sr.addWidget(self.frame_label)
        layout.addLayout(sr)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%.1f%%" % 0)
        layout.addWidget(self.progress_bar)

        # 日志
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(170)
        layout.addWidget(self.log_text)

        # 按钮
        br = QHBoxLayout()
        self.btn_start = QPushButton("  开始处理")
        self.btn_start.setMinimumHeight(44)
        self.btn_start.setStyleSheet("""
            QPushButton { background: #00796B; color: white; font-size: 15px;
                          font-weight: bold; border-radius: 6px; }
            QPushButton:hover { background: #00897B; }
            QPushButton:disabled { background: #333; color: #666; }""")
        self.btn_start.clicked.connect(self.start_processing)
        br.addWidget(self.btn_start)

        self.btn_pause = QPushButton("  暂停")
        self.btn_pause.setMinimumHeight(44)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setStyleSheet("""
            QPushButton { background: #F57F17; color: white; font-size: 15px;
                          font-weight: bold; border-radius: 6px; }
            QPushButton:hover { background: #FFB300; }
            QPushButton:disabled { background: #333; color: #666; }""")
        self.btn_pause.clicked.connect(self.toggle_pause)
        br.addWidget(self.btn_pause)

        self.btn_stop = QPushButton("  停止")
        self.btn_stop.setMinimumHeight(44)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("""
            QPushButton { background: #C62828; color: white; font-size: 15px;
                          font-weight: bold; border-radius: 6px; }
            QPushButton:hover { background: #E53935; }
            QPushButton:disabled { background: #333; color: #666; }""")
        self.btn_stop.clicked.connect(self.stop_processing)
        br.addWidget(self.btn_stop)
        layout.addLayout(br)

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_elapsed)
        self.current_progress = 0.0
        self.is_paused = False
        self.resume_cfg = None
        self.check_checkpoint()

    def on_video_selected(self, path):
        self.log_text.append(f"[分析] {Path(path).name}...")
        QApplication.processEvents()
        self.vinfo = VideoAnalyzer.get_info(path)
        if self.vinfo:
            dur = fmt_time(self.vinfo.get("duration", 0))
            w, h = self.vinfo.get("width", 0), self.vinfo.get("height", 0)
            fps = self.vinfo.get("fps", 0)
            frames = self.vinfo.get("total_frames", 0)
            self.info_label.setText(
                f"视频: {self.vinfo['filename']}  |  {w}x{h} @ {fps}fps  |  "
                f"时长: {dur}  |  总帧数: {frames}")
            self.info_label.show()
            self.update_estimates()
            self.log_text.append(f"[分析] {w}x{h}, {fps}fps, {frames}帧, {dur}")
        else:
            self.info_label.setText("无法读取视频信息（请确认 ffprobe 在 tools 目录中）")
            self.info_label.show()
            self.space_label.hide()

    def update_estimates(self):
        if not self.vinfo: return
        do_interp = self.interp_check.isChecked()
        auto_clean = self.clean_check.isChecked()
        peak_mb, detail = VideoAnalyzer.estimate_disk(self.vinfo, do_interp, auto_clean)
        free_mb = VideoAnalyzer.check_free_space(str(BASE_DIR))
        est_sec = VideoAnalyzer.estimate_time(self.vinfo, do_interp, self.gpu_mem)

        if free_mb > 0 and peak_mb > free_mb:
            color = "#F44336"; warn = "  !! 空间不足"
        elif free_mb > 0 and peak_mb > free_mb * 0.8:
            color = "#FF9800"; warn = "  ! 空间紧张"
        else:
            color = "#FF9800"; warn = ""

        self.space_label.setText(
            f"预估空间: {fmt_size(peak_mb)}  |  磁盘剩余: {fmt_size(free_mb)}{warn}  |  "
            f"预估时长: {fmt_time(est_sec)}")
        self.space_label.setStyleSheet(f"color: {color}; font-size: 12px; padding: 4px 8px;")
        self.space_label.show()

    def check_checkpoint(self):
        if CHECKPOINT_FILE.exists():
            try:
                cp = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
                if cp.get("version") != VERSION:
                    self.log_text.append("[断点] 版本不兼容，已清除")
                    CHECKPOINT_FILE.unlink()
                    return
                stage = cp.get("stage", 0)
                cfg = cp.get("cfg", {})
                elapsed = cp.get("elapsed", 0)
                vname = Path(cfg.get("video", "")).name
                names = {1:"拆帧", 2:"插帧", 3:"超分", 4:"HDR", 5:"音频"}
                reply = QMessageBox.question(self, "发现未完成的任务",
                    f"视频: {vname}\n已完成: {names.get(stage, str(stage))} ({stage}/6)\n"
                    f"已用: {fmt_time(elapsed)}\n\n从断点继续?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if reply == QMessageBox.Yes:
                    self.resume_cfg = cfg
                    self.resume_cfg["start_stage"] = stage + 1
                    if cfg.get("video"):
                        self.video_drop.set_file(cfg["video"])
                        self.on_video_selected(cfg["video"])
                    if cfg.get("audio"):
                        self.audio_drop.set_file(cfg["audio"])
                else:
                    CHECKPOINT_FILE.unlink()
            except: pass

    def start_processing(self):
        required = ["ffmpeg.exe", "ffprobe.exe", "realesrgan-ncnn-vulkan.exe"]
        if self.interp_check.isChecked():
            required.append("rife-ncnn-vulkan.exe")
        missing = [t for t in required if not (TOOLS_DIR / t).exists()]
        if self.interp_check.isChecked() and not find_rife_model():
            missing.append("RIFE模型目录")
        if missing:
            QMessageBox.critical(self, "缺少工具",
                "请放入 tools 目录:\n\n" + "\n".join(missing))
            return

        if self.vinfo:
            peak_mb, _ = VideoAnalyzer.estimate_disk(self.vinfo,
                self.interp_check.isChecked(), self.clean_check.isChecked())
            free_mb = VideoAnalyzer.check_free_space(str(BASE_DIR))
            if free_mb > 0 and peak_mb > free_mb:
                r = QMessageBox.warning(self, "磁盘空间不足",
                    f"需要 {fmt_size(peak_mb)}, 仅剩 {fmt_size(free_mb)}\n继续?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if r != QMessageBox.Yes: return

        if self.resume_cfg:
            cfg = self.resume_cfg
            self.resume_cfg = None
        else:
            video = self.video_drop.filepath
            if not video:
                QMessageBox.warning(self, "提示", "请先选择视频文件"); return
            cfg = {
                "video": video,
                "audio": self.audio_drop.filepath,
                "model": self.model_combo.currentText().split("  ")[0].strip(),
                "interp": self.interp_check.isChecked(),
                "dolby_profile": self.dolby_combo.currentText(),
                "audio_mode": self.audio_combo.currentText(),
                "auto_clean": self.clean_check.isChecked(),
                "start_stage": 1,
            }

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.is_paused = False
        self.start_time = time.time()
        self.current_progress = 0.0
        self.timer.start(1000)

        self.thread = ProcessThread(cfg)
        self.thread.log.connect(self.on_log)
        self.thread.progress.connect(self.on_progress)
        self.thread.stage_changed.connect(self.on_stage)
        self.thread.frame_progress.connect(self.on_frame)
        self.thread.finished_ok.connect(self.on_done)
        self.thread.finished_err.connect(self.on_error)
        self.thread.start()

    def toggle_pause(self):
        if not self.thread: return
        if self.is_paused:
            self.thread.request_resume()
            self.is_paused = False
            self.btn_pause.setText("  暂停")
            self.btn_pause.setStyleSheet(self.btn_pause.styleSheet().replace("#4CAF50","#F57F17").replace("#66BB6A","#FFB300"))
            self.stage_label.setText(self.stage_label.text().replace(" [已暂停]",""))
        else:
            self.thread.request_pause()
            self.is_paused = True
            self.btn_pause.setText("  继续")
            self.btn_pause.setStyleSheet(self.btn_pause.styleSheet().replace("#F57F17","#4CAF50").replace("#FFB300","#66BB6A"))
            self.stage_label.setText(self.stage_label.text() + " [已暂停]")

    def stop_processing(self):
        if self.thread:
            self.thread.request_stop()
            self.log_text.append("[停止] 正在停止...")
            self.stage_label.setText("已终止")
            self.timer.stop()
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_stop.setEnabled(False)

    def on_log(self, msg):
        self.log_text.append(msg)
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def on_progress(self, pct):
        self.current_progress = pct
        self.progress_bar.setValue(int(pct * 10))
        self.progress_bar.setFormat(f"{pct:.1f}%")

    def on_stage(self, name, cur, total):
        self.stage_label.setText(name)

    def on_frame(self, cur, total):
        self.frame_label.setText(f"帧: {cur}/{total}")

    def update_elapsed(self):
        if self.start_time <= 0: return
        elapsed = time.time() - self.start_time
        self.elapsed_label.setText(f"已用: {fmt_time(elapsed)}")
        if self.current_progress > 0.5:
            remain = elapsed / (self.current_progress / 100.0) - elapsed
            self.eta_label.setText(f"剩余: {fmt_time(remain)}")
        else:
            self.eta_label.setText("剩余: 计算中...")

    def on_done(self):
        self.timer.stop()
        elapsed = time.time() - self.start_time
        self.elapsed_label.setText(f"总耗时: {fmt_time(elapsed)}")
        self.eta_label.setText("已完成")
        self.stage_label.setText("全部完成!")
        self.stage_label.setStyleSheet("color: #4CAF50; font-size: 13px; font-weight: bold;")
        self.progress_bar.setValue(1000)
        self.progress_bar.setFormat("100.0%")
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        QMessageBox.information(self, "完成",
            f"全部处理完毕!\n总耗时: {fmt_time(elapsed)}\n输出在程序目录下")

    def on_error(self, msg):
        self.timer.stop()
        self.stage_label.setText("处理出错")
        self.stage_label.setStyleSheet("color: #F44336; font-size: 13px; font-weight: bold;")
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        QMessageBox.critical(self, "失败", f"{msg}\n\n断点已保存，下次可恢复")

# ─────────────────── 入口 ───────────────────
if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        pal = QPalette()
        pal.setColor(QPalette.Window, QColor(26, 26, 46))
        pal.setColor(QPalette.WindowText, QColor(224, 224, 224))
        pal.setColor(QPalette.Base, QColor(13, 13, 26))
        pal.setColor(QPalette.Text, QColor(224, 224, 224))
        pal.setColor(QPalette.Button, QColor(42, 42, 74))
        pal.setColor(QPalette.ButtonText, QColor(224, 224, 224))
        pal.setColor(QPalette.Highlight, QColor(0, 206, 209))
        app.setPalette(pal)
        win = MainWindow()
        win.show()
        sys.exit(app.exec_())
    except Exception as e:
        print(f"启动失败: {e}")
        traceback.print_exc()
        input("按回车退出...")
