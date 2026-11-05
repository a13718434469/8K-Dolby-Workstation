# -*- coding: utf-8 -*-
"""Desktop interface for the 8K Dolby Workstation processing engine."""
import sys
import shutil
import threading
import time
from pathlib import Path

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)
from pipeline import (
    BASE_DIR, CHECKPOINT_FILE, TOOLS_DIR, Pipeline, PipelineCancelled, estimate_disk,
    find_rife_model, find_tool, load_checkpoint, probe_video,
)

VERSION = "2.0"


def ai_tools_available():
    try:
        find_tool("realesrgan-ncnn-vulkan")
        sr = all((TOOLS_DIR / "models" / f"realesrgan-x4plus.{ext}").is_file()
                 for ext in ("bin", "param"))
    except FileNotFoundError:
        sr = False
    try:
        find_tool("rife-ncnn-vulkan")
        find_rife_model()
        interp = True
    except FileNotFoundError:
        interp = False
    return sr, interp


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{hours}时" if hours else "") + f"{minutes:02d}分{seconds:02d}秒"


def fmt_size(mb):
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


class FileField(QWidget):
    """A local-file field accepting both browsing and drag/drop."""
    changed = pyqtSignal(str)

    def __init__(self, placeholder, file_filter, parent=None):
        super().__init__(parent)
        self.file_filter = file_filter
        self.setAcceptDrops(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.edit.textChanged.connect(self.changed)
        button = QPushButton("浏览…")
        button.clicked.connect(self.browse)
        row.addWidget(self.edit, 1)
        row.addWidget(button)

    @property
    def filepath(self):
        return self.edit.text().strip()

    def set_file(self, path):
        self.edit.setText(str(path))

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", "", self.file_filter)
        if path:
            self.set_file(path)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).is_file():
            self.set_file(urls[0].toLocalFile())
            event.acceptProposedAction()


class ProbeThread(QThread):
    result = pyqtSignal(str, object, str)

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            self.result.emit(self.path, probe_video(self.path), "")
        except Exception as exc:
            self.result.emit(self.path, None, str(exc))


class ProcessThread(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(float)
    stage = pyqtSignal(str, int, int)
    result = pyqtSignal(str, str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = dict(cfg)
        self.pipeline = None
        self.cancelled = threading.Event()
        self.paused = False

    def request_stop(self):
        self.cancelled.set()
        if self.pipeline is not None:
            self.pipeline.cancel()

    def request_pause(self, paused):
        self.paused = paused
        if self.pipeline is not None:
            self.pipeline.set_paused(paused)

    def run(self):
        try:
            self.pipeline = Pipeline(self.cfg, log=self.log.emit,
                                     progress=self.progress.emit, stage=self.stage.emit)
            self.pipeline.set_paused(self.paused)
            if self.cancelled.is_set():
                self.pipeline.cancel()
            output = self.pipeline.run()
            self.result.emit("ok", str(output))
        except PipelineCancelled:
            self.result.emit("cancelled", "任务已停止；可用的处理记录保留在工作目录。")
        except Exception as exc:
            self.result.emit("error", str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"8K Dolby Workstation · v{VERSION}")
        self.setMinimumSize(960, 800)
        self.worker = None
        self.probes = set()
        self.vinfo = None
        self.outcome = None
        self.closing = False
        self.started_at = 0
        self.restore_in_progress = False
        sr_available, interp_available = ai_tools_available()
        self.setStyleSheet("""
            QWidget { background: #171d2c; color: #e4eaf3; font-size: 13px; }
            QGroupBox { border: 1px solid #354057; border-radius: 8px;
                        margin-top: 12px; padding: 18px 12px 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 14px; color: #65d5d4; }
            QLineEdit, QComboBox { background: #222d41; border: 1px solid #3a4860;
                                  border-radius: 4px; padding: 6px; min-height: 20px; }
            QComboBox QAbstractItemView { background: #222d41; }
            QPushButton { background: #2c3a51; border: 1px solid #44526b;
                          border-radius: 5px; padding: 7px 12px; }
            QPushButton:hover { background: #374d68; }
            QPushButton:disabled { background: #232a38; color: #69758a; }
            QPlainTextEdit { background: #101522; border: 1px solid #354057;
                             border-radius: 6px; font-family: Consolas; font-size: 12px; }
            QProgressBar { border: 1px solid #354057; border-radius: 5px;
                           min-height: 23px; text-align: center; }
            QProgressBar::chunk { background: #078e9a; border-radius: 4px; }
        """)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 14, 20, 16)
        title = QLabel("8K Dolby Workstation")
        title.setFont(QFont("Microsoft YaHei", 21, QFont.Bold))
        title.setStyleSheet("color: #65d5d4;")
        layout.addWidget(title)
        subtitle = QLabel("AI 插帧与超分 · HEVC 10-bit · HDR10 / Dolby Vision 元数据封装")
        subtitle.setStyleSheet("color: #9cabbe;")
        layout.addWidget(subtitle)

        self.input_group = QGroupBox("输入与输出")
        grid = QGridLayout(self.input_group)
        self.video = FileField("选择或拖入视频", "视频 (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;所有文件 (*)")
        self.audio = FileField("可选；留空使用视频中的第一条音轨", "音频/视频 (*.flac *.wav *.mp3 *.aac *.ac3 *.eac3 *.m4a *.mp4 *.mkv);;所有文件 (*)")
        self.output = QLineEdit()
        self.output.setPlaceholderText("选择新的输出 MP4 文件")
        output_button = QPushButton("另存为…")
        output_button.clicked.connect(self.choose_output)
        grid.addWidget(QLabel("视频"), 0, 0)
        grid.addWidget(self.video, 0, 1, 1, 2)
        grid.addWidget(QLabel("替换音轨"), 1, 0)
        grid.addWidget(self.audio, 1, 1, 1, 2)
        grid.addWidget(QLabel("输出"), 2, 0)
        grid.addWidget(self.output, 2, 1)
        grid.addWidget(output_button, 2, 2)
        layout.addWidget(self.input_group)
        self.info_label = QLabel("选择视频后显示分辨率、帧率和磁盘空间预估。")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("color: #9cabbe; padding: 3px;")
        layout.addWidget(self.info_label)

        self.param_group = QGroupBox("处理参数")
        grid = QGridLayout(self.param_group)
        self.model = QComboBox()
        self.model.addItem("通用 / 实拍 · realesrgan-x4plus", "realesrgan-x4plus")
        self.model.addItem("动画视频 · realesr-animevideov3", "realesr-animevideov3")
        self.model.addItem("动画增强 · realesrgan-x4plus-anime", "realesrgan-x4plus-anime")
        self.ai = QCheckBox("AI 超分（关闭时使用普通缩放）")
        self.ai.setChecked(sr_available)
        self.resolution = QComboBox()
        for label, size in [("8K · 7680 × 4320", (7680, 4320)), ("4K · 3840 × 2160", (3840, 2160)), ("1080p · 1920 × 1080", (1920, 1080))]:
            self.resolution.addItem(label, size)
        self.interp = QCheckBox("AI 插帧")
        self.interp.setChecked(interp_available)
        self.fps = QComboBox()
        for value in (120, 60, 48, 30, 24):
            self.fps.addItem(f"{value} fps", value)
        fps_row = QHBoxLayout()
        fps_row.addWidget(self.interp)
        fps_row.addWidget(self.fps)
        self.color = QComboBox()
        self.color.addItem("SDR · BT.709", "sdr")
        self.color.addItem("HDR10 · BT.2020 / PQ", "hdr10")
        self.color.addItem("Dolby Vision · Profile 8.1（需配套 RPU）", "dv81")
        self.audio_mode = QComboBox()
        self.audio_mode.addItem("复制原音轨（保留已有 Atmos）", "copy")
        self.audio_mode.addItem("E-AC-3（普通多声道）", "eac3")
        self.audio_mode.addItem("AAC（兼容模式）", "aac")
        self.audio_mode.addItem("无音轨", "none")
        self.rpu = FileField("与最终画面、帧数及时序配套的 RPU.bin", "RPU 元数据 (*.bin);;所有文件 (*)")
        self.clean = QCheckBox("完成阶段后清理可再生成的临时文件")
        self.clean.setChecked(True)
        grid.addWidget(QLabel("超分模型"), 0, 0)
        grid.addWidget(self.model, 0, 1)
        grid.addWidget(QLabel("目标尺寸"), 0, 2)
        grid.addWidget(self.resolution, 0, 3)
        grid.addWidget(self.ai, 1, 0, 1, 4)
        grid.addWidget(QLabel("帧率"), 2, 0)
        grid.addLayout(fps_row, 2, 1)
        grid.addWidget(QLabel("音频"), 2, 2)
        grid.addWidget(self.audio_mode, 2, 3)
        grid.addWidget(QLabel("画面格式"), 3, 0)
        grid.addWidget(self.color, 3, 1, 1, 3)
        grid.addWidget(QLabel("RPU 文件"), 4, 0)
        grid.addWidget(self.rpu, 4, 1, 1, 3)
        grid.addWidget(self.clean, 5, 0, 1, 4)
        notice = QLabel("SDR 转 HDR 为色彩空间转换。杜比视界需自行提供配套元数据；普通 E-AC-3 编码不生成 Atmos 对象音频。")
        notice.setWordWrap(True)
        notice.setStyleSheet("color: #9cabbe; font-size: 12px;")
        grid.addWidget(notice, 6, 0, 1, 4)
        layout.addWidget(self.param_group)
        self.space_label = QLabel("")
        self.space_label.setWordWrap(True)
        self.space_label.setStyleSheet("color: #f0bc73;")
        layout.addWidget(self.space_label)
        status = QHBoxLayout()
        self.stage_label = QLabel("准备就绪")
        self.stage_label.setStyleSheet("color: #65d5d4;")
        self.elapsed = QLabel("")
        status.addWidget(self.stage_label, 1)
        status.addWidget(self.elapsed)
        layout.addLayout(status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFormat("0.0%")
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        layout.addWidget(self.log, 1)
        buttons = QHBoxLayout()
        self.start = QPushButton("开始新任务")
        self.start.setStyleSheet("background: #087f89; font-weight: bold; padding: 10px;")
        self.resume = QPushButton("恢复上次任务")
        self.pause = QPushButton("暂停（命令结束后）")
        self.stop = QPushButton("停止")
        self.pause.setEnabled(False)
        self.stop.setEnabled(False)
        for button in (self.start, self.resume, self.pause, self.stop):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_elapsed)
        self.probe_timer = QTimer(self)
        self.probe_timer.setSingleShot(True)
        self.probe_timer.setInterval(400)
        self.probe_timer.timeout.connect(self.start_probe)
        self.start.clicked.connect(self.start_new)
        self.resume.clicked.connect(self.resume_previous)
        self.pause.clicked.connect(self.toggle_pause)
        self.stop.clicked.connect(self.stop_processing)
        self.video.changed.connect(self.video_changed)
        for combo in (self.model, self.resolution, self.fps, self.color, self.audio_mode):
            combo.currentIndexChanged.connect(self.update_estimates)
        self.color.currentIndexChanged.connect(self.update_rpu_state)
        self.interp.toggled.connect(self.fps.setEnabled)
        self.interp.toggled.connect(self.update_estimates)
        self.ai.toggled.connect(self.model.setEnabled)
        self.ai.toggled.connect(self.update_estimates)
        self.clean.toggled.connect(self.update_estimates)
        self.update_rpu_state()
        self.model.setEnabled(self.ai.isChecked())
        self.fps.setEnabled(self.interp.isChecked())
        self.refresh_resume()
        if not (sr_available and interp_available):
            self.log.appendPlainText("AI 工具未完整安装，已默认关闭对应功能。运行 setup.bat 安装后可启用 AI 超分和插帧。")

    def config(self):
        width, height = self.resolution.currentData()
        return {
            "video": self.video.filepath, "audio": self.audio.filepath,
            "output": self.output.text().strip(), "model": self.model.currentData(),
            "width": width, "height": height, "ai": self.ai.isChecked(),
            "interp": self.interp.isChecked(), "target_fps": self.fps.currentData(),
            "color_mode": self.color.currentData(), "rpu": self.rpu.filepath,
            "audio_mode": self.audio_mode.currentData(), "auto_clean": self.clean.isChecked(),
        }

    def choose_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "输出文件", self.output.text() or str(BASE_DIR), "MP4 视频 (*.mp4)")
        if path:
            self.output.setText(path if path.lower().endswith(".mp4") else path + ".mp4")

    def video_changed(self, path):
        self.vinfo = None
        self.info_label.setText("正在分析视频…" if path else "请选择视频。")
        self.space_label.clear()
        if not self.restore_in_progress and path:
            self.output.setText(str(Path(path).with_name(Path(path).stem + "_processed.mp4")))
        self.probe_timer.start()

    def start_probe(self):
        path = self.video.filepath
        if not path or not Path(path).is_file():
            self.info_label.setText("请选择存在的视频文件。")
            return
        thread = ProbeThread(path)
        self.probes.add(thread)
        thread.result.connect(self.on_probe)
        thread.finished.connect(lambda t=thread: self.probe_finished(t))
        thread.start()

    def probe_finished(self, thread):
        self.probes.discard(thread)
        thread.deleteLater()
        if self.closing and self.worker is None and not self.probes:
            self.close()

    def on_probe(self, path, info, error):
        if path != self.video.filepath:
            return
        self.vinfo = info
        if not info:
            self.info_label.setText(f"无法分析视频：{error}")
            return
        self.info_label.setText(f"{info['width']} × {info['height']} · {info['fps']:.3f} fps · "
                                f"{fmt_time(info['duration'])} · 约 {info['total_frames']} 帧")
        self.update_estimates()

    def update_rpu_state(self):
        self.rpu.setEnabled(self.color.currentData() == "dv81")

    def update_estimates(self):
        if not self.vinfo:
            return
        try:
            peak, detail = estimate_disk(self.vinfo, self.config())
            free_mb = shutil.disk_usage(BASE_DIR).free / 1024**2
            warning = " · 空间可能不足，可关闭插帧或降低尺寸" if peak > free_mb else ""
            self.space_label.setText(f"空间保守预估：{fmt_size(peak)} · 工作盘可用：{fmt_size(free_mb)}{warning}")
            self.space_label.setToolTip(detail)
        except (ValueError, KeyError, TypeError, OSError):
            self.space_label.setText("暂时无法估算空间。")

    def refresh_resume(self):
        try:
            checkpoint = load_checkpoint()
        except Exception as exc:
            checkpoint = None
            self.log.appendPlainText(f"处理记录无法读取：{exc}")
        self.resume.setEnabled(checkpoint is not None and self.worker is None)
        if checkpoint:
            cfg = checkpoint.get("cfg", {})
            self.resume.setToolTip(f"恢复原参数：{Path(cfg.get('video', '')).name}")
        else:
            self.resume.setToolTip(f"没有可恢复任务。记录位置：{CHECKPOINT_FILE}")

    @staticmethod
    def select_data(combo, data):
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)
            return True
        return False

    def restore_config(self, cfg):
        self.restore_in_progress = True
        try:
            self.video.set_file(cfg["video"])
            self.audio.set_file(cfg.get("audio", ""))
            self.rpu.set_file(cfg.get("rpu", ""))
            self.output.setText(str(cfg.get("output", "")))
            for combo, key in [(self.model, "model"), (self.fps, "target_fps"), (self.color, "color_mode"), (self.audio_mode, "audio_mode")]:
                if key in cfg:
                    self.select_data(combo, cfg[key])
            size = (cfg.get("width", 7680), cfg.get("height", 4320))
            if not self.select_data(self.resolution, size):
                self.resolution.addItem(f"自定义 · {size[0]} × {size[1]}", size)
                self.resolution.setCurrentIndex(self.resolution.count() - 1)
            if not self.select_data(self.fps, cfg.get("target_fps", 120)):
                rate = cfg["target_fps"]
                self.fps.addItem(f"{rate} fps", rate)
                self.fps.setCurrentIndex(self.fps.count() - 1)
            self.interp.setChecked(cfg.get("interp", True))
            self.ai.setChecked(cfg.get("ai", True))
            self.clean.setChecked(cfg.get("auto_clean", True))
        finally:
            self.restore_in_progress = False

    def resume_previous(self):
        if self.worker is not None:
            return
        try:
            checkpoint = load_checkpoint()
            if not checkpoint or "cfg" not in checkpoint:
                raise ValueError("没有可恢复的任务。")
            cfg = dict(checkpoint["cfg"])
            self.restore_config(cfg)
            cfg["resume"] = True
            self.begin(cfg)
        except Exception as exc:
            QMessageBox.warning(self, "无法恢复", str(exc))
            self.refresh_resume()

    def start_new(self):
        if self.worker is not None:
            return
        cfg = self.config()
        if not Path(cfg["video"]).is_file():
            QMessageBox.warning(self, "需要视频", "请先选择存在的视频文件。")
            return
        if not cfg["output"]:
            self.choose_output()
            cfg["output"] = self.output.text().strip()
            if not cfg["output"]:
                return
        if Path(cfg["output"]).resolve() == Path(cfg["video"]).resolve():
            QMessageBox.warning(self, "输出路径", "请为输出选择与输入不同的文件名。")
            return
        if cfg["color_mode"] == "dv81" and not Path(cfg["rpu"]).is_file():
            QMessageBox.warning(self, "需要 RPU", "杜比视界模式需要与最终画面配套的 RPU.bin 文件。")
            return
        if Path(cfg["output"]).exists():
            QMessageBox.warning(self, "输出已存在", "请选择新的输出文件名；现有文件不会被覆盖。")
            return
        cfg["resume"] = False
        self.begin(cfg)

    def begin(self, cfg):
        self.outcome = None
        self.progress.setValue(0)
        self.progress.setFormat("0.0%")
        self.started_at = time.monotonic()
        self.timer.start(1000)
        self.stage_label.setText("正在检查处理参数…")
        self.input_group.setEnabled(False)
        self.param_group.setEnabled(False)
        self.start.setEnabled(False)
        self.resume.setEnabled(False)
        self.pause.setEnabled(True)
        self.pause.setText("暂停（命令结束后）")
        self.stop.setEnabled(True)
        self.worker = ProcessThread(cfg)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.progress.connect(self.on_progress)
        self.worker.stage.connect(self.on_stage)
        self.worker.result.connect(self.on_result)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def on_progress(self, value):
        value = max(0, min(100, value))
        self.progress.setValue(round(value * 10))
        self.progress.setFormat(f"{value:.1f}%")

    def on_stage(self, name, current, total):
        prefix = f"{current}/{total}"
        self.stage_label.setText(name if name.startswith(prefix) else f"{prefix} · {name}")

    def on_result(self, kind, message):
        self.outcome = (kind, message)

    def worker_finished(self):
        worker = self.worker
        self.worker = None
        worker.deleteLater()
        self.timer.stop()
        self.update_elapsed()
        self.input_group.setEnabled(True)
        self.param_group.setEnabled(True)
        self.update_rpu_state()
        self.fps.setEnabled(self.interp.isChecked())
        self.model.setEnabled(self.ai.isChecked())
        self.start.setEnabled(True)
        self.pause.setEnabled(False)
        self.stop.setEnabled(False)
        self.refresh_resume()
        kind, message = self.outcome or ("error", "工作线程意外结束，请查看日志。")
        if self.closing:
            if not self.probes:
                self.close()
            return
        if kind == "ok":
            self.on_progress(100)
            self.stage_label.setText("处理完成")
            self.log.appendPlainText(f"输出文件：{message}")
            QMessageBox.information(self, "完成", f"视频已保存：\n{message}")
        elif kind == "cancelled":
            self.stage_label.setText("已停止")
            self.log.appendPlainText(message)
        else:
            self.stage_label.setText("处理失败")
            self.log.appendPlainText(f"错误：{message}")
            QMessageBox.critical(self, "处理失败", message)

    def toggle_pause(self):
        if self.worker is None:
            return
        paused = not self.worker.paused
        self.worker.request_pause(paused)
        self.pause.setText("继续" if paused else "暂停（命令结束后）")
        self.log.appendPlainText("暂停已请求；当前外部命令结束后暂停。" if paused else "继续处理。")

    def stop_processing(self):
        if self.worker is not None:
            self.worker.request_stop()
            self.stage_label.setText("正在停止并保存处理记录…")
            self.pause.setEnabled(False)
            self.stop.setEnabled(False)

    def update_elapsed(self):
        if self.started_at:
            self.elapsed.setText(f"本次已用 {fmt_time(time.monotonic() - self.started_at)}")

    def closeEvent(self, event):
        self.probe_timer.stop()
        if self.worker is not None or self.probes:
            self.closing = True
            self.stop_processing()
            self.stage_label.setText("正在结束任务，完成后自动关闭…")
            event.ignore()
            return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
