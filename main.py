# -*- coding: utf-8 -*-
"""Desktop interface for the 8K Dolby Workstation processing engine."""
import sys
import shutil
import threading
import time
import json
from pathlib import Path

import PyQt5
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QVBoxLayout, QWidget, QFrame, QScrollArea,
    QToolButton, QSpinBox,
)
from pipeline import (
    BASE_DIR, CHECKPOINT_FILE, TOOLS_DIR, WORKSPACE, Pipeline, PipelineCancelled, estimate_disk,
    find_rife_model, find_tool, load_checkpoint, probe_video,
)

VERSION = "2.1"


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


class SafeComboBox(QComboBox):
    """Scrolling a long settings page must not accidentally change a preset."""
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


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
        self.cancelled = threading.Event()

    def request_stop(self):
        self.cancelled.set()

    def run(self):
        try:
            try:
                from quality import inspect_media
            except ImportError:
                info = probe_video(self.path)
            else:
                info = inspect_media(self.path, cancel_event=self.cancelled)
            if not self.cancelled.is_set():
                self.result.emit(self.path, info, "")
        except Exception as exc:
            if not self.cancelled.is_set():
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
    PRESETS = {
        "bili_preserve": ("B站杜比 · 保留原片", "保留已有 Dolby Vision 8.4 与原始码流。优先保障画质，有现成 Atmos 就原样保留。"),
        "bili_make": ("B站杜比 · HLG / 8.4 制作", "输出 HLG / Dolby Vision 8.4。需要与最终画面、时序匹配的 8.4 RPU；导出后生成本地质检报告。"),
        "quality": ("高质量常规导出", "原生精度处理，默认保留源分辨率和帧率。适合 SDR、HDR10 与 HLG 内容制作。"),
        "enhance": ("AI 增强 · 插帧与超分", "按需启用 AI。此路径使用 8-bit SDR RGB 工作帧；HDR 母版推荐选择原片保留或高质量导出。"),
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Dolby Workstation · v{VERSION}")
        self.resize(1280, 900)
        self.setMinimumSize(1000, 640)
        self.worker = None
        self.probes = set()
        self.vinfo = None
        self.outcome = None
        self.last_output = None
        self.last_report = None
        self.closing = False
        self.started_at = 0
        self.current_progress = 0
        self.restore_in_progress = False
        self.settings_file = WORKSPACE / "ui_preferences.json"
        self.sr_available, self.interp_available = ai_tools_available()
        self.setStyleSheet(self.stylesheet())
        root = QWidget()
        self.setCentralWidget(root)
        shell = QVBoxLayout(root)
        shell.setContentsMargins(22, 18, 22, 18)
        shell.setSpacing(16)

        heading = QHBoxLayout()
        brand = QVBoxLayout()
        title = QLabel("DOLBY WORKSTATION")
        title.setObjectName("brand")
        brand.addWidget(title)
        subtitle = QLabel("从素材检查到交付质检，让每一步都有依据")
        subtitle.setObjectName("muted")
        brand.addWidget(subtitle)
        heading.addLayout(brand)
        heading.addStretch()
        self.top_status = QLabel("准备就绪")
        self.top_status.setObjectName("statusPill")
        heading.addWidget(self.top_status)
        shell.addLayout(heading)

        body = QHBoxLayout()
        body.setSpacing(18)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(185)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(17, 20, 17, 20)
        label = QLabel("交付工作流")
        label.setObjectName("eyebrow")
        side.addWidget(label)
        self.step_labels = []
        for number, name, detail in [("01", "检查素材", "识别 HDR、杜比与音轨"),
                                      ("02", "选择交付", "原片保留或高质量制作"),
                                      ("03", "处理与质检", "检查码流、时序和元数据"),
                                      ("04", "平台验收", "草稿转码后确认杜比")]:
            step = QLabel(f"<b>{number}　{name}</b><br><span style='color:#8c99ad;font-size:11px'>{detail}</span>")
            step.setWordWrap(True)
            step.setMinimumHeight(63)
            side.addWidget(step)
            self.step_labels.append(step)
        side.addStretch()
        note = QLabel("画质优先\n已有 Atmos 音轨可保留\n\n本地格式合格后，仍需平台草稿验收确认识别结果。")
        note.setWordWrap(True)
        note.setObjectName("muted")
        side.addWidget(note)
        self.tool_label = QLabel("工具环境待检查")
        self.tool_label.setWordWrap(True)
        self.tool_label.setObjectName("muted")
        side.addWidget(self.tool_label)
        self.check_tools = QPushButton("检查工具环境")
        self.check_tools.clicked.connect(self.check_environment)
        side.addWidget(self.check_tools)
        sidebar_area = QScrollArea()
        sidebar_area.setWidgetResizable(True)
        sidebar_area.setFrameShape(QFrame.NoFrame)
        sidebar_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sidebar_area.setFixedWidth(205)
        sidebar_area.setWidget(sidebar)
        body.addWidget(sidebar_area)

        scroll = QScrollArea()
        self.content_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content.setObjectName("content")
        stack = QVBoxLayout(content)
        stack.setContentsMargins(0, 0, 5, 0)
        stack.setSpacing(14)
        scroll.setWidget(content)
        body.addWidget(scroll, 1)
        shell.addLayout(body, 1)

        self.input_group = QGroupBox("01   素材与输出")
        grid = QGridLayout(self.input_group)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(11)
        self.video = FileField("选择或拖入视频文件", "视频 (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;所有文件 (*)")
        self.audio = FileField("可选：已有 Atmos / 其他替换音轨", "音频/视频 (*.flac *.wav *.mp3 *.aac *.ac3 *.eac3 *.m4a *.mp4 *.mkv);;所有文件 (*)")
        self.output = QLineEdit()
        self.output.setPlaceholderText("选择新的 MP4 输出文件")
        output_button = QPushButton("另存为…")
        output_button.clicked.connect(self.choose_output)
        grid.addWidget(QLabel("视频素材"), 0, 0)
        grid.addWidget(self.video, 0, 1, 1, 2)
        grid.addWidget(QLabel("输出文件"), 1, 0)
        grid.addWidget(self.output, 1, 1)
        grid.addWidget(output_button, 1, 2)
        self.info_label = QLabel("导入后自动识别分辨率、帧率、动态范围与音轨。")
        self.info_label.setObjectName("muted")
        self.info_label.setWordWrap(True)
        grid.addWidget(self.info_label, 2, 0, 1, 3)
        metrics = QHBoxLayout()
        self.source_video = self.make_metric("画面规格", "等待素材")
        self.source_dv = self.make_metric("杜比视界", "尚未检测")
        self.source_audio = self.make_metric("源音轨 / Atmos", "尚未检测")
        for card in (self.source_video, self.source_dv, self.source_audio):
            metrics.addWidget(card[0])
        grid.addLayout(metrics, 3, 0, 1, 3)
        stack.addWidget(self.input_group)

        self.param_group = QGroupBox("02   交付方案")
        settings = QVBoxLayout(self.param_group)
        preset_row = QHBoxLayout()
        self.export_preset = SafeComboBox()
        for key, (label, _) in self.PRESETS.items():
            self.export_preset.addItem(label, key)
        preset_row.addWidget(QLabel("使用方案"))
        preset_row.addWidget(self.export_preset, 1)
        self.preflight = QPushButton("检查交付条件")
        self.preflight.clicked.connect(self.run_preflight)
        preset_row.addWidget(self.preflight)
        settings.addLayout(preset_row)
        self.preset_note = QLabel()
        self.preset_note.setObjectName("notice")
        self.preset_note.setWordWrap(True)
        settings.addWidget(self.preset_note)

        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        self.resolution = SafeComboBox()
        for label, value in [("保留源分辨率", (0, 0)), ("1080p · 1920 × 1080", (1920, 1080)),
                             ("4K · 3840 × 2160", (3840, 2160)), ("8K · 7680 × 4320", (7680, 4320)),
                             ("竖屏 4K · 2160 × 3840", (2160, 3840))]:
            self.resolution.addItem(label, value)
        self.fps = SafeComboBox()
        for label, value in [("保留源帧率", 0), ("24 fps", 24), ("30 fps", 30), ("48 fps", 48), ("60 fps", 60), ("120 fps", 120)]:
            self.fps.addItem(label, value)
        self.color = SafeComboBox()
        for label, value in [("SDR · BT.709", "sdr"), ("HDR10 · BT.2020 / PQ", "hdr10"),
                             ("HLG · BT.2020", "hlg"), ("Dolby Vision 8.4 · HLG", "dv84"),
                             ("Dolby Vision 8.1 · PQ", "dv81")]:
            self.color.addItem(label, value)
        self.audio_mode = SafeComboBox()
        for label, value in [("复制原音轨 · 保留已有 Atmos", "copy"), ("AAC · 通用兼容", "aac"),
                             ("E-AC-3 · 普通多声道", "eac3"), ("无音轨", "none")]:
            self.audio_mode.addItem(label, value)
        form.addWidget(QLabel("目标尺寸"), 0, 0)
        form.addWidget(self.resolution, 0, 1)
        form.addWidget(QLabel("目标帧率"), 0, 2)
        form.addWidget(self.fps, 0, 3)
        form.addWidget(QLabel("画面格式"), 1, 0)
        form.addWidget(self.color, 1, 1)
        form.addWidget(QLabel("音频处理"), 1, 2)
        form.addWidget(self.audio_mode, 1, 3)
        self.rpu = FileField("与最终画面及时序配套的 RPU.bin", "RPU 元数据 (*.bin);;所有文件 (*)")
        self.rpu_label = QLabel("杜比元数据")
        form.addWidget(self.rpu_label, 2, 0)
        form.addWidget(self.rpu, 2, 1, 1, 3)
        settings.addLayout(form)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("高级设置")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.RightArrow)
        settings.addWidget(self.advanced_toggle)
        self.advanced = QWidget()
        advanced = QGridLayout(self.advanced)
        advanced.setContentsMargins(0, 3, 0, 0)
        self.ai = QCheckBox("AI 超分")
        self.interp = QCheckBox("AI 插帧")
        self.model = SafeComboBox()
        for label, value in [("通用 / 实拍 · x4plus", "realesrgan-x4plus"), ("动画视频 · animevideov3", "realesr-animevideov3"),
                             ("动画增强 · x4plus-anime", "realesrgan-x4plus-anime")]:
            self.model.addItem(label, value)
        self.encoding_preset = SafeComboBox()
        for label, value in [("标准 · 速度与压缩平衡", "medium"), ("较快 · 更快完成", "fast"),
                             ("精细 · 更高压缩效率", "slow"), ("测试 · 最快编码", "ultrafast")]:
            self.encoding_preset.addItem(label, value)
        self.crf = QSpinBox()
        self.crf.setRange(10, 30)
        self.crf.setValue(18)
        self.crf.setToolTip("较小数值画质更高、文件更大。推荐 16–20。")
        self.batch_size = QSpinBox()
        self.batch_size.setRange(1, 256)
        self.batch_size.setValue(32)
        self.work_size = SafeComboBox()
        self.work_size.addItem("1080p · AI 工作尺寸", (1920, 1080))
        self.work_size.addItem("720p · 节省资源", (1280, 720))
        self.clean = QCheckBox("成功阶段后自动清理临时文件")
        self.clean.setChecked(True)
        advanced.addWidget(self.ai, 0, 0)
        advanced.addWidget(self.model, 0, 1)
        advanced.addWidget(self.interp, 0, 2)
        advanced.addWidget(self.work_size, 0, 3)
        advanced.addWidget(QLabel("编码预设"), 1, 0)
        advanced.addWidget(self.encoding_preset, 1, 1)
        advanced.addWidget(QLabel("画质 CRF"), 1, 2)
        advanced.addWidget(self.crf, 1, 3)
        advanced.addWidget(QLabel("每批帧数"), 2, 0)
        advanced.addWidget(self.batch_size, 2, 1)
        advanced.addWidget(self.clean, 2, 2, 1, 2)
        advanced.addWidget(QLabel("替换音轨"), 3, 0)
        advanced.addWidget(self.audio, 3, 1, 1, 3)
        self.advanced.hide()
        settings.addWidget(self.advanced)
        stack.addWidget(self.param_group)

        self.quality_group = QGroupBox("03   本地质检")
        quality_layout = QVBoxLayout(self.quality_group)
        quality_top = QHBoxLayout()
        self.quality_status = QLabel("等待素材检查")
        self.quality_status.setObjectName("sectionTitle")
        quality_top.addWidget(self.quality_status, 1)
        self.platform_status = QLabel("平台尚未验收")
        self.platform_status.setObjectName("subtlePill")
        quality_top.addWidget(self.platform_status)
        quality_layout.addLayout(quality_top)
        self.output_summary = QLabel()
        self.output_summary.setWordWrap(True)
        self.output_summary.setObjectName("notice")
        self.output_summary.hide()
        quality_layout.addWidget(self.output_summary)
        self.quality_details = QLabel("检查 Dolby Vision Profile、HLG / PQ、位深、分辨率、帧率与音轨。\n技术检查通过后，还需平台草稿转码确认。")
        self.quality_details.setObjectName("muted")
        self.quality_details.setWordWrap(True)
        quality_layout.addWidget(self.quality_details)
        self.space_label = QLabel()
        self.space_label.setObjectName("muted")
        self.space_label.setWordWrap(True)
        quality_layout.addWidget(self.space_label)
        stack.addWidget(self.quality_group)

        self.log_toggle = QToolButton()
        self.log_toggle.setText("处理日志")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.log_toggle.setArrowType(Qt.RightArrow)
        stack.addWidget(self.log_toggle)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        self.log.setMinimumHeight(150)
        self.log.setMaximumHeight(220)
        self.log.hide()
        stack.addWidget(self.log)
        stack.addStretch()

        footer = QFrame()
        footer.setObjectName("footer")
        foot = QVBoxLayout(footer)
        foot.setContentsMargins(16, 12, 16, 12)
        status = QHBoxLayout()
        self.stage_label = QLabel("导入素材，选择交付方案后开始。")
        self.stage_label.setObjectName("sectionTitle")
        self.elapsed = QLabel("")
        self.elapsed.setObjectName("muted")
        status.addWidget(self.stage_label, 1)
        status.addWidget(self.elapsed)
        foot.addLayout(status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFormat("0.0%")
        foot.addWidget(self.progress)
        controls = QHBoxLayout()
        self.start = QPushButton("开始交付")
        self.start.setObjectName("primary")
        self.start.setMinimumWidth(135)
        self.resume = QPushButton("恢复上次任务")
        self.pause = QPushButton("暂停")
        self.stop = QPushButton("停止")
        self.open_output = QPushButton("打开输出目录")
        self.view_report = QPushButton("查看质检报告")
        for button in (self.start, self.resume, self.pause, self.stop):
            controls.addWidget(button)
        controls.addStretch()
        controls.addWidget(self.open_output)
        controls.addWidget(self.view_report)
        self.pause.setEnabled(False)
        self.stop.setEnabled(False)
        self.open_output.setEnabled(False)
        self.view_report.setEnabled(False)
        foot.addLayout(controls)
        shell.addWidget(footer)

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
        self.open_output.clicked.connect(self.open_output_folder)
        self.view_report.clicked.connect(self.show_report)
        self.video.changed.connect(self.video_changed)
        self.export_preset.currentIndexChanged.connect(self.apply_preset)
        self.advanced_toggle.toggled.connect(self.toggle_advanced)
        self.log_toggle.toggled.connect(self.toggle_log)
        for combo in (self.model, self.resolution, self.fps, self.color, self.audio_mode):
            combo.currentIndexChanged.connect(self.update_estimates)
        self.color.currentIndexChanged.connect(self.update_control_states)
        self.ai.toggled.connect(self.update_control_states)
        self.interp.toggled.connect(self.update_control_states)
        self.clean.toggled.connect(self.update_estimates)
        self.batch_size.valueChanged.connect(self.update_estimates)
        self.apply_preset()
        self.load_preferences()
        self.refresh_resume()
        self.check_environment(silent=True)

    @staticmethod
    def stylesheet():
        return """
        QMainWindow, QWidget { background:#111724; color:#e8edf5; font-family:'Microsoft YaHei','Segoe UI'; font-size:12px; }
        QWidget#content { background:#111724; }
        QLabel#brand { color:#f1f7ff; font-size:22px; font-weight:700; letter-spacing:2px; }
        QLabel#muted { color:#8f9db3; }
        QLabel#eyebrow { color:#6de2d3; font-size:12px; font-weight:700; }
        QLabel#sectionTitle { color:#e8edf5; font-size:13px; font-weight:600; }
        QLabel#statusPill { background:#163a36; color:#82e4cc; border:1px solid #23584e; border-radius:12px; padding:7px 14px; }
        QLabel#subtlePill { color:#a7b4c8; background:#263042; border-radius:9px; padding:5px 10px; font-size:11px; }
        QFrame#sidebar, QFrame#footer { background:#182131; border:1px solid #2b374a; border-radius:12px; }
        QFrame#sidebar QLabel, QFrame#footer QLabel { background:transparent; }
        QGroupBox { background:#182131; border:1px solid #2b374a; border-radius:12px; margin-top:9px; padding:24px 15px 14px; }
        QGroupBox::title { subcontrol-origin:margin; left:17px; top:3px; color:#a6b5cc; font-size:12px; font-weight:600; padding:0 5px; }
        QGroupBox QLabel, QGroupBox QWidget { background:transparent; }
        QLabel#notice { background:#142c38; color:#acd4de; border:1px solid #27444f; border-radius:7px; padding:10px 12px; }
        QFrame#metric { background:#1e2a3e; border:1px solid #2b3a51; border-radius:7px; }
        QFrame#metric QLabel { background:transparent; }
        QLabel#metricValue { color:#e8f1ff; font-size:13px; font-weight:600; }
        QLineEdit, QComboBox, QSpinBox { background:#202c40; border:1px solid #34435b; border-radius:6px; padding:7px 9px; min-height:18px; selection-background-color:#167b8a; }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border:1px solid #4ebcac; }
        QComboBox::drop-down { border:0; width:22px; }
        QComboBox QAbstractItemView { background:#202c40; color:#e8edf5; selection-background-color:#2c5360; }
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color:#7d8a9c; background:#1b2433; border-color:#293447; }
        QPushButton { background:#28364c; border:1px solid #3a4960; border-radius:6px; padding:8px 11px; }
        QPushButton:hover { background:#344760; border-color:#58718c; }
        QPushButton:disabled { color:#66758b; background:#202a3b; border-color:#2b384a; }
        QPushButton#primary { background:#1b9a8a; border-color:#30b8a2; color:white; font-weight:700; }
        QPushButton#primary:hover { background:#24ad9b; }
        QToolButton { color:#a5b8d2; border:0; padding:5px 0; text-align:left; }
        QCheckBox { spacing:7px; }
        QCheckBox::indicator { width:16px; height:16px; border:1px solid #53647b; border-radius:4px; background:#243147; }
        QCheckBox::indicator:checked { background:#1b9a8a; border-color:#58cebb; }
        QPlainTextEdit { background:#101826; color:#a8b8cf; border:1px solid #2b3a50; border-radius:7px; font-family:Consolas; font-size:11px; }
        QProgressBar { background:#101927; border:1px solid #304157; border-radius:5px; text-align:center; min-height:17px; font-size:11px; }
        QProgressBar::chunk { background:#1b9a8a; border-radius:4px; }
        QScrollBar:vertical { background:#111724; width:8px; margin:0; }
        QScrollBar::handle:vertical { background:#3c4b63; border-radius:4px; min-height:30px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
        """

    @staticmethod
    def make_metric(title, value):
        box = QFrame()
        box.setObjectName("metric")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(11, 10, 11, 10)
        label = QLabel(title)
        label.setObjectName("muted")
        main_value = QLabel(value)
        main_value.setObjectName("metricValue")
        main_value.setWordWrap(True)
        layout.addWidget(label)
        layout.addWidget(main_value)
        return box, main_value

    @staticmethod
    def select_data(combo, data):
        index = combo.findData(data)
        if index < 0 and isinstance(data, (tuple, list)):
            index = next((i for i in range(combo.count())
                          if isinstance(combo.itemData(i), (tuple, list))
                          and tuple(combo.itemData(i)) == tuple(data)), -1)
        if index >= 0:
            combo.setCurrentIndex(index)
            return True
        return False

    def current_route(self):
        key = self.export_preset.currentData()
        return "preserve" if key == "bili_preserve" else "enhance" if key == "enhance" else "transcode"

    def config(self):
        route = self.current_route()
        width, height = self.resolution.currentData()
        if not width or route == "preserve":
            if self.vinfo:
                width, height = self.vinfo.get("width", 1920), self.vinfo.get("height", 1080)
                if route != "preserve":
                    width = self.vinfo.get("display_width", width)
                    height = self.vinfo.get("display_height", height)
            else:
                width, height = 1920, 1080
        rate = self.fps.currentData()
        source_rate = self.vinfo.get("fps_fraction", "30") if self.vinfo else "30"
        cfg = {
            "video": self.video.filepath, "audio": self.audio.filepath if self.audio_mode.currentData() != "none" else "",
            "output": self.output.text().strip(), "route": route,
            "delivery": "bilibili" if self.export_preset.currentData().startswith("bili_") else "general",
            "width": int(width), "height": int(height), "model": self.model.currentData(),
            "ai": route == "enhance" and self.ai.isChecked(),
            "interp": route == "enhance" and self.interp.isChecked(),
            "target_fps": rate or (120 if self.interp.isChecked() else source_rate),
            "color_mode": self.color.currentData(),
            "rpu": self.rpu.filepath if route != "preserve" and self.color.currentData() in ("dv81", "dv84") else "",
            "audio_mode": self.audio_mode.currentData(), "auto_clean": self.clean.isChecked(),
            "preset": self.encoding_preset.currentData(), "crf": self.crf.value(),
            "batch_size": self.batch_size.value(), "require_atmos": False,
        }
        if route == "transcode" and rate:
            cfg["fps_override"] = rate
        if route == "enhance":
            cfg["work_width"], cfg["work_height"] = self.work_size.currentData()
        return cfg

    def apply_preset(self):
        if self.restore_in_progress:
            return
        key = self.export_preset.currentData()
        self.preset_note.setText(self.PRESETS[key][1])
        self.ai.setChecked(key == "enhance" and self.sr_available)
        self.interp.setChecked(key == "enhance" and self.interp_available)
        self.select_data(self.resolution, (0, 0))
        self.select_data(self.fps, 120 if key == "enhance" else 0)
        self.select_data(self.audio_mode, "copy")
        if key == "bili_make":
            self.select_data(self.color, "dv84")
            if self.vinfo:
                width = self.vinfo.get("display_width", self.vinfo["width"])
                height = self.vinfo.get("display_height", self.vinfo["height"])
                if max(width, height) > 3840 or min(width, height) > 2160:
                    self.select_data(self.resolution, (3840, 2160) if width >= height else (2160, 3840))
                if self.vinfo["fps"] > 60:
                    self.select_data(self.fps, 60)
        elif key == "quality":
            transfer = self.vinfo.get("color_transfer") if self.vinfo else ""
            self.select_data(self.color, {"arib-std-b67": "hlg", "smpte2084": "hdr10"}.get(transfer, "sdr"))
        elif key == "enhance":
            self.select_data(self.color, "sdr")
        elif key == "bili_preserve" and self.vinfo:
            self.select_data(self.resolution, (0, 0))
            rate = self.vinfo.get("fps_fraction", self.vinfo["fps"])
            if not self.select_data(self.fps, rate):
                self.fps.addItem(f"原片 · {self.vinfo['fps']:.3f} fps", rate)
                self.fps.setCurrentIndex(self.fps.count() - 1)
            profile, compat = self.vinfo.get("dv_profile"), self.vinfo.get("dv_compatibility")
            color = "dv84" if profile == 8 and compat == 4 else "dv81" if profile == 8 and compat == 1 else {
                "arib-std-b67": "hlg", "smpte2084": "hdr10"}.get(self.vinfo.get("color_transfer"), "sdr")
            self.select_data(self.color, color)
        self.update_control_states()
        self.update_estimates()
        if self.vinfo:
            self.render_source_quality()

    def update_control_states(self):
        route = self.current_route()
        running = self.worker is not None
        for control in (self.resolution, self.fps, self.color):
            control.setEnabled(route != "preserve" and not running)
        self.ai.setEnabled(route == "enhance" and not running)
        self.interp.setEnabled(route == "enhance" and not running)
        self.model.setEnabled(route == "enhance" and self.ai.isChecked() and not running)
        self.work_size.setEnabled(route == "enhance" and not running)
        for control in (self.encoding_preset, self.crf, self.batch_size):
            control.setEnabled(route != "preserve" and not running)
        needs_rpu = route != "preserve" and self.color.currentData() in ("dv81", "dv84")
        self.rpu.setVisible(needs_rpu)
        self.rpu_label.setVisible(needs_rpu)
        self.rpu.setEnabled(needs_rpu and not running)
        self.update_estimates()

    def update_rpu_state(self):
        self.update_control_states()

    def toggle_advanced(self, enabled):
        self.advanced.setVisible(enabled)
        self.advanced_toggle.setArrowType(Qt.DownArrow if enabled else Qt.RightArrow)

    def toggle_log(self, enabled):
        self.log.setVisible(enabled)
        self.log_toggle.setArrowType(Qt.DownArrow if enabled else Qt.RightArrow)

    def choose_output(self):
        initial = self.output.text() or str(BASE_DIR)
        path, _ = QFileDialog.getSaveFileName(self, "选择新的输出文件", initial, "MP4 视频 (*.mp4)")
        if path:
            self.output.setText(path if path.lower().endswith(".mp4") else path + ".mp4")

    def video_changed(self, path):
        for probe in self.probes:
            probe.request_stop()
        self.vinfo = None
        self.last_report = None
        self.last_output = None
        self.view_report.setEnabled(False)
        self.open_output.setEnabled(False)
        self.output_summary.hide()
        self.info_label.setText("正在读取素材信息…" if path else "导入后自动识别画面与音轨规格。")
        self.source_video[1].setText("正在检查" if path else "等待素材")
        self.source_dv[1].setText("尚未检测")
        self.source_audio[1].setText("尚未检测")
        self.quality_status.setText("正在检查素材" if path else "等待素材检查")
        self.space_label.clear()
        if not self.restore_in_progress and path:
            stem = Path(path).stem
            self.output.setText(str(Path(path).with_name(stem + "_delivery.mp4")))
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
            self.info_label.setText(f"无法读取素材：{error}")
            self.quality_status.setText("素材检查失败")
            self.quality_details.setText("请检查文件是否有效，以及 FFprobe 工具是否已安装。")
            return
        rotation = info.get("rotation", 0)
        rotation_note = f"　·　旋转 {rotation}°" if rotation else ""
        self.info_label.setText(f"{info['filename']}　·　{fmt_time(info['duration'])}　·　约 {info['total_frames']} 帧{rotation_note}")
        width, height = info.get("display_width", info["width"]), info.get("display_height", info["height"])
        self.source_video[1].setText(f"{width} × {height}\n{info['fps']:.3f} fps")
        profile, compat = info.get("dv_profile"), info.get("dv_compatibility")
        transfer = {"arib-std-b67": "HLG", "smpte2084": "PQ / HDR10", "bt709": "SDR"}.get(info.get("color_transfer"), "未声明动态范围")
        profile_text = f"Profile {profile}" + (f".{compat}" if profile == 8 and compat is not None else "")
        self.source_dv[1].setText(f"{profile_text}\n{transfer}" if profile else f"未检测到杜比视界\n{transfer}")
        audio_codec = info.get("audio_codec")
        audio = audio_codec.upper() if audio_codec and audio_codec != "none" else "无音轨"
        atmos = {"confirmed": "Atmos 已确认", "unknown": "Atmos 待确认", "absent": "无 Atmos 标记"}.get(info.get("atmos"), "Atmos 待确认")
        self.source_audio[1].setText(f"{audio}\n{atmos}")
        if not self.restore_in_progress and self.worker is None:
            self.apply_preset()
        self.render_source_quality()
        self.update_estimates()

    def render_source_quality(self):
        if not self.vinfo:
            return
        try:
            from quality import assess_bilibili
            report = assess_bilibili(self.vinfo)
            self.last_report = report
            if self.export_preset.currentData().startswith("bili_"):
                self.render_quality(report, "素材检查")
            else:
                self.quality_status.setText("素材已读取 · 使用原生精度处理")
                self.quality_details.setText("常规交付按所选色彩与画质设置导出。原片保留不重编码；AI 增强请先确认工作精度适合素材。")
        except ImportError:
            self.quality_status.setText("素材已读取")
            self.quality_details.setText("画面与音轨信息已读取；交付质检组件尚未就绪。")

    def render_quality(self, report, prefix):
        status = report.get("status", "warning")
        label = {"pass": "本地技术条件通过", "warning": "有事项需要确认", "fail": "尚不符合目标条件"}.get(status, "待确认")
        self.quality_status.setText(f"{prefix} · {label}")
        colors = {"pass": "#70d4b4", "warning": "#ecc387", "fail": "#ef9d9d"}
        self.quality_status.setStyleSheet(f"color:{colors.get(status, '#adbcd2')};font-weight:600")
        checks = report.get("checks", [])
        problematic = [c for c in checks if c.get("status") != "pass"]
        chosen = (problematic if problematic else checks)[:4]
        details = [f"{c.get('label', '')}：{c.get('detail', '')}" for c in chosen]
        bili = report.get("bilibili_readiness")
        if isinstance(bili, dict):
            readiness = {"pass": "符合公开指引的本地技术条件", "warning": "存在待确认事项", "fail": "尚不符合杜比 8.4 条件"}.get(bili.get("status"), "待检查")
            details.append(f"独立 B站杜比检查：{readiness}；不影响本次常规成片的技术质检结果。")
        self.quality_details.setText("\n".join(details) or report.get("summary", "检查完成"))
        self.platform_status.setText("平台草稿已验收" if report.get("platform_verified") else "平台尚未验收")
        self.platform_status.setToolTip("本地格式检测与平台实际识别是两个独立步骤。")
        media = report.get("media")
        if isinstance(media, dict):
            profile, compatibility = media.get("dv_profile"), media.get("dv_compatibility")
            dv = f"DV {profile}" + (f".{compatibility}" if profile == 8 and compatibility is not None else "") if profile else "无杜比视界标记"
            atmos = {"confirmed": "Atmos 码流已确认", "unknown": "Atmos 待确认", "absent": "未检测到 Atmos"}.get(media.get("atmos"), "Atmos 待确认")
            width, height = media.get("display_width", media.get("width", "?")), media.get("display_height", media.get("height", "?"))
            self.output_summary.setText(f"成片：{width} × {height}　·　{media.get('fps', '?')} fps　·　{dv}　·　{atmos}")
            self.output_summary.show()
        self.view_report.setEnabled(True)

    def run_preflight(self):
        if self.worker is not None:
            return
        self.check_environment(silent=True)
        if self.vinfo:
            self.render_source_quality()
            self.update_estimates()
            self.show_quality_panel()
        else:
            self.start_probe()

    def check_environment(self, silent=False):
        available, missing = [], []
        for name in ("ffmpeg", "ffprobe", "dovi_tool", "realesrgan-ncnn-vulkan", "rife-ncnn-vulkan"):
            try:
                find_tool(name)
                available.append(name)
            except FileNotFoundError:
                missing.append(name)
        self.sr_available, self.interp_available = ai_tools_available()
        self.tool_label.setText("基础工具就绪\nAI 工具" + ("已就绪" if self.sr_available and self.interp_available else "未完整安装"))
        if not silent:
            message = "已找到：\n" + "、".join(available)
            if missing:
                message += "\n\n缺少：\n" + "、".join(missing) + "\n\n运行 setup.bat 安装工具；杜比元数据功能需要 -WithDovi。"
            QMessageBox.information(self, "工具环境", message)

    def update_estimates(self):
        if not self.vinfo or not hasattr(self, "space_label"):
            return
        try:
            cfg = self.config()
            if cfg["route"] == "preserve":
                peak = Path(cfg["video"]).stat().st_size * 2.5 / 1024**2 + 256
                detail = "原片封装，不创建 AI 工作帧；按输入文件大小和封装中间文件估算。"
            else:
                peak, detail = estimate_disk(self.vinfo, cfg)
            free_mb = shutil.disk_usage(BASE_DIR).free / 1024**2
            warning = " · 请先检查磁盘空间" if peak > free_mb else ""
            self.space_label.setText(f"工作空间预估 {fmt_size(peak)}　·　工作盘剩余 {fmt_size(free_mb)}{warning}")
            self.space_label.setToolTip(detail)
        except (ValueError, KeyError, TypeError, OSError):
            self.space_label.setText("空间预估将在素材分析后显示。")

    def refresh_resume(self):
        try:
            checkpoint = load_checkpoint()
        except Exception as exc:
            checkpoint = None
            self.log.appendPlainText(f"处理记录无法读取：{exc}")
        self.resume.setEnabled(checkpoint is not None and self.worker is None)
        self.resume.setToolTip(f"恢复原任务：{Path(checkpoint['cfg'].get('video', '')).name}" if checkpoint else "没有可恢复的未完成任务")

    def restore_config(self, cfg):
        self.restore_in_progress = True
        try:
            route = cfg.get("route", "enhance")
            preset = "bili_preserve" if route == "preserve" else "bili_make" if cfg.get("delivery") == "bilibili" else "enhance" if route == "enhance" else "quality"
            self.select_data(self.export_preset, preset)
            self.preset_note.setText(self.PRESETS[preset][1])
            self.video.set_file(cfg["video"])
            self.audio.set_file(cfg.get("audio", ""))
            self.rpu.set_file(cfg.get("rpu", ""))
            self.output.setText(str(cfg.get("output", "")))
            for combo, key in [(self.model, "model"), (self.color, "color_mode"), (self.audio_mode, "audio_mode"), (self.encoding_preset, "preset")]:
                if key in cfg:
                    self.select_data(combo, cfg[key])
            size = (cfg.get("width", 1920), cfg.get("height", 1080))
            if not self.select_data(self.resolution, size):
                self.resolution.addItem(f"原任务 · {size[0]} × {size[1]}", size)
                self.resolution.setCurrentIndex(self.resolution.count() - 1)
            rate = cfg.get("fps_override", cfg.get("target_fps", 0) if cfg.get("interp") else 0)
            if not self.select_data(self.fps, rate):
                self.fps.addItem(f"原任务 · {rate} fps", rate)
                self.fps.setCurrentIndex(self.fps.count() - 1)
            self.ai.setChecked(cfg.get("ai", False))
            self.interp.setChecked(cfg.get("interp", False))
            self.clean.setChecked(cfg.get("auto_clean", True))
            self.crf.setValue(int(cfg.get("crf", 18)))
            self.batch_size.setValue(int(cfg.get("batch_size", 32)))
        finally:
            self.restore_in_progress = False
        self.update_control_states()

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
            QMessageBox.warning(self, "需要视频", "请先导入存在的视频文件。")
            return
        if not self.vinfo:
            QMessageBox.information(self, "正在分析素材", "请等待素材检查完成后开始；这将确认源分辨率、色彩和音轨，避免误用默认参数。")
            self.start_probe()
            return
        if not cfg["output"]:
            self.choose_output()
            cfg["output"] = self.output.text().strip()
            if not cfg["output"]:
                return
        if Path(cfg["output"]).resolve() == Path(cfg["video"]).resolve() or Path(cfg["output"]).exists():
            QMessageBox.warning(self, "请选择新输出文件", "输出不能覆盖素材或现有文件，请更换文件名。")
            return
        if cfg["route"] != "preserve" and cfg["color_mode"] in ("dv81", "dv84") and not Path(cfg["rpu"]).is_file():
            QMessageBox.warning(self, "需要配套杜比元数据", "请提供与最终画面、调色和帧数匹配的 RPU.bin。已有 Dolby Vision 8.4 原片可改用“保留原片”方案。")
            return
        if cfg["route"] == "preserve" and self.last_report and self.last_report.get("status") == "fail":
            QMessageBox.warning(self, "原片还不符合目标格式", "当前原片未满足 B站杜比 8.4 的本地技术检查。请选择“HLG / 8.4 制作”并提供配套 RPU，或使用常规高质量导出。\n\n" + self.last_report.get("summary", ""))
            return
        self.save_preferences()
        cfg["resume"] = False
        self.begin(cfg)

    def begin(self, cfg):
        self.outcome = None
        self.current_progress = 0
        self.progress.setValue(0)
        self.progress.setFormat("0.0%")
        self.started_at = time.monotonic()
        self.timer.start(1000)
        self.stage_label.setText("正在检查素材和交付参数…")
        self.top_status.setText("正在处理")
        self.input_group.setEnabled(False)
        self.param_group.setEnabled(False)
        self.start.setEnabled(False)
        self.resume.setEnabled(False)
        self.check_tools.setEnabled(False)
        self.open_output.setEnabled(False)
        self.view_report.setEnabled(False)
        self.output_summary.hide()
        self.pause.setEnabled(True)
        self.pause.setText("暂停")
        self.pause.setToolTip("当前外部命令完成后，在安全边界暂停。")
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
        self.current_progress = value
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
        self.check_tools.setEnabled(True)
        self.update_control_states()
        self.start.setEnabled(True)
        self.pause.setEnabled(False)
        self.stop.setEnabled(False)
        self.refresh_resume()
        kind, message = self.outcome or ("error", "工作线程意外结束，请查看处理日志。")
        if self.closing:
            if not self.probes:
                self.close()
            return
        if kind == "ok":
            self.on_progress(100)
            self.stage_label.setText("导出完成 · 成片已通过本地输出校验")
            self.top_status.setText("交付完成")
            self.last_output = Path(message)
            self.open_output.setEnabled(True)
            self.log.appendPlainText(f"输出文件：{message}")
            report_path = Path(str(self.last_output) + ".quality.json")
            if report_path.exists():
                try:
                    self.last_report = json.loads(report_path.read_text(encoding="utf-8"))
                    self.render_quality(self.last_report, "成片质检")
                except (ValueError, OSError) as exc:
                    self.log.appendPlainText(f"质检报告无法读取：{exc}")
            else:
                self.quality_status.setText("导出完成 · 结构校验已通过")
                self.quality_details.setText("成片已保存在输出目录。平台杜比识别需要上传草稿并等待转码确认。")
            self.show_quality_panel()
        elif kind == "cancelled":
            self.stage_label.setText("已停止 · 可恢复已完成的处理阶段")
            self.top_status.setText("已停止")
            self.log.appendPlainText(message)
        else:
            self.stage_label.setText("处理失败 · 请查看原因后重试或恢复")
            self.top_status.setText("需要处理")
            self.log.appendPlainText(f"错误：{message}")
            self.quality_status.setText("本次交付未完成")
            self.quality_details.setText(message[:800])
            self.log_toggle.setChecked(True)
            self.show_failure(message)

    def show_quality_panel(self):
        QTimer.singleShot(0, lambda: self.content_scroll.ensureWidgetVisible(self.quality_group, 0, 12))

    def show_failure(self, message):
        first_line = message.splitlines()[0] if message else "处理过程中出现异常。"
        advice = "请展开详细信息或处理日志，修正后可恢复已完成的任务阶段。"
        lower = message.lower()
        if "缺少" in message or "找不到" in message:
            advice = "请检查素材路径，或运行 setup.bat 补齐工具与模型。"
        elif "rpu" in lower or "元数据" in message:
            advice = "请确认 RPU 的 Profile、画面调色、时序和帧数与最终输出匹配。"
        elif "space" in lower or "空间" in message:
            advice = "请为工作目录和输出目录预留磁盘空间，或降低输出尺寸与批次数量。"
        box = QMessageBox(QMessageBox.Critical, "处理未完成", first_line[:300] + "\n\n" + advice, QMessageBox.Ok, self)
        box.setDetailedText(message)
        box.exec_()

    def toggle_pause(self):
        if self.worker is None:
            return
        paused = not self.worker.paused
        self.worker.request_pause(paused)
        self.pause.setText("继续" if paused else "暂停")
        self.top_status.setText("等待安全暂停" if paused else "正在处理")
        self.log.appendPlainText("暂停已请求，当前外部命令结束后生效。" if paused else "继续处理。")

    def stop_processing(self):
        if self.worker is not None:
            self.worker.request_stop()
            self.stage_label.setText("正在停止并保存可恢复的处理记录…")
            self.top_status.setText("正在停止")
            self.pause.setEnabled(False)
            self.stop.setEnabled(False)

    def update_elapsed(self):
        if self.started_at:
            seconds = time.monotonic() - self.started_at
            text = f"已用 {fmt_time(seconds)}"
            if self.worker is not None and 2 < self.current_progress < 99:
                remain = seconds * (100 / self.current_progress - 1)
                text += f"　·　粗估剩余 {fmt_time(remain)}"
            self.elapsed.setText(text)

    def open_output_folder(self):
        if self.last_output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output.parent)))

    def show_report(self):
        if self.last_output:
            path = Path(str(self.last_output) + ".quality.json")
            if path.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
                return
        if self.last_report:
            details = "\n\n".join(f"{c.get('label', '')}\n{c.get('detail', '')}" for c in self.last_report.get("checks", []))
            bili = self.last_report.get("bilibili_readiness")
            if isinstance(bili, dict):
                details += "\n\n独立 B站杜比准备检查\n" + bili.get("summary", "")
            QMessageBox.information(self, "本地技术检查", self.last_report.get("summary", "") + "\n\n" + details + "\n\n平台识别结果需要草稿转码验收。")

    def save_preferences(self):
        cfg = self.config()
        keys = ("color_mode", "audio_mode", "model", "preset", "crf", "batch_size", "auto_clean")
        data = {key: cfg[key] for key in keys}
        data.update({"version": 1, "export_preset": self.export_preset.currentData(),
                     "resolution": self.resolution.currentData(), "fps": self.fps.currentData()})
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.settings_file)
        except OSError as exc:
            self.log.appendPlainText(f"界面偏好未保存：{exc}")

    def load_preferences(self):
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                return
            self.select_data(self.export_preset, data.get("export_preset", "bili_preserve"))
            for combo, key in [(self.resolution, "resolution"), (self.fps, "fps"), (self.color, "color_mode"),
                               (self.audio_mode, "audio_mode"), (self.model, "model"), (self.encoding_preset, "preset")]:
                if key in data:
                    self.select_data(combo, data[key])
            self.crf.setValue(data.get("crf", 18))
            self.batch_size.setValue(data.get("batch_size", 32))
            self.clean.setChecked(data.get("auto_clean", True))
            self.update_control_states()
        except (OSError, ValueError, TypeError):
            pass

    def closeEvent(self, event):
        self.probe_timer.stop()
        if self.worker is not None or self.probes:
            self.closing = True
            self.stop_processing()
            for probe in self.probes:
                probe.request_stop()
            self.stage_label.setText("正在安全结束任务，完成后关闭…")
            event.ignore()
            return
        self.save_preferences()
        event.accept()


def main():
    # Resolve PyQt's actual Unicode installation directory before QApplication.
    plugins = Path(PyQt5.__file__).resolve().parent / "Qt5" / "plugins"
    if plugins.is_dir():
        QApplication.setLibraryPaths([str(plugins)])
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
