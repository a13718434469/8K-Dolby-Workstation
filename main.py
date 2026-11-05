# -*- coding: utf-8 -*-
"""Desktop interface for the 8K Dolby Workstation processing engine."""
import sys
import shutil
import threading
import time
import json
from pathlib import Path

import PyQt5
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal, QUrl, QEvent
from PyQt5.QtGui import QDesktopServices, QKeySequence, QFont
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QVBoxLayout, QWidget, QFrame, QScrollArea,
    QToolButton, QSpinBox, QShortcut, QStyle,
)
from pipeline import (
    BASE_DIR, CHECKPOINT_FILE, TOOLS_DIR, WORKSPACE, Pipeline, PipelineCancelled, estimate_disk,
    find_rife_model, find_tool, load_checkpoint, probe_video,
)

VERSION = "2.2"


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
    files_selected = pyqtSignal(object)

    def __init__(self, placeholder, file_filter, parent=None):
        super().__init__(parent)
        self.file_filter = file_filter
        self.import_target = None
        self.setAcceptDrops(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setAcceptDrops(False)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.edit.textChanged.connect(self.changed)
        self.browse_button = QPushButton("选择…")
        self.browse_button.clicked.connect(self.browse)
        row.addWidget(self.edit, 1)
        row.addWidget(self.browse_button)

    @property
    def filepath(self):
        return self.edit.text().strip()

    def set_file(self, path):
        self.edit.setText(str(path))
        self.edit.setToolTip(str(path))

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", "", self.file_filter)
        if path:
            if self.import_target:
                self.files_selected.emit([path])
            else:
                self.set_file(path)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if self.isEnabled() and urls and all(url.isLocalFile() for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not self.isEnabled():
            event.ignore()
            return
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
        if self.import_target and paths:
            self.files_selected.emit(paths)
            event.acceptProposedAction()
        elif len(paths) == 1 and Path(paths[0]).is_file():
            if "*.bin" in self.file_filter and Path(paths[0]).suffix.lower() != ".bin":
                self.files_selected.emit(paths)
            else:
                self.set_file(paths[0])
            event.acceptProposedAction()


class MediaDropArea(QFrame):
    files_dropped = pyqtSignal(object)
    browse_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setObjectName("dropArea")

    def dragEnterEvent(self, event):
        if self.isEnabled() and event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if not self.isEnabled():
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()

    def mouseDoubleClickEvent(self, event):
        if self.isEnabled() and event.button() == Qt.LeftButton:
            self.browse_requested.emit()


class IntakeThread(QThread):
    result = pyqtSignal(object, str)

    def __init__(self, paths, target):
        super().__init__()
        self.paths = list(paths)
        self.target = target
        self.cancelled = threading.Event()

    def request_stop(self):
        self.cancelled.set()

    def run(self):
        collected = []
        try:
            from quality import inspect_media, inspect_audio
            for raw in self.paths:
                if self.cancelled.is_set():
                    return
                path = Path(raw).resolve()
                if not path.is_file():
                    raise ValueError(f"文件不存在或不是普通文件：{path.name}")
                try:
                    info = inspect_media(path, cancel_event=self.cancelled)
                    kind = "video"
                except Exception as video_error:
                    if self.cancelled.is_set():
                        return
                    try:
                        info = inspect_audio(path, cancel_event=self.cancelled)
                        kind = "audio"
                    except Exception:
                        raise ValueError(f"无法读取媒体文件：{path.name}。当前素材保持不变。") from video_error
                collected.append({"path": str(path), "kind": kind, "info": info})
            if not self.cancelled.is_set():
                self.result.emit(collected, "")
        except Exception as exc:
            if not self.cancelled.is_set():
                self.result.emit([], str(exc))


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
        "preserve": ("保持原画质（推荐）", "不重编码视频，保持原始分辨率、帧率和色彩。"),
        "bili_preserve": ("B站杜比 · 保留原片", "保留已有 Dolby Vision 8.4 与原始码流。优先保障画质，有现成 Atmos 就原样保留。"),
        "bili_make": ("B站杜比 · HLG / 8.4 制作", "输出 HLG / Dolby Vision 8.4。需要与最终画面、时序匹配的 8.4 RPU；导出后生成本地质检报告。"),
        "quality": ("高质量常规导出", "原生精度处理，默认保留源分辨率和帧率。适合 SDR、HDR10 与 HLG 内容制作。"),
        "enhance": ("AI 增强 · 插帧与超分", "按需启用 AI。此路径使用 8-bit SDR RGB 工作帧；HDR 母版推荐选择原片保留或高质量导出。"),
    }

    intake_finished = pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"杜比工作站 · v{VERSION}")
        self.resize(1180, 780)
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)
        try:
            from app_identity import app_icon
            self.setWindowIcon(app_icon())
        except ImportError:
            pass
        self.worker = None
        self.probes = set()
        self.intakes = set()
        self.intake_busy = False
        self.ainfo = None
        self.vinfo = None
        self.outcome = None
        self.last_output = None
        self.last_report = None
        self.closing = False
        self.started_at = 0
        self.current_progress = 0
        self.restore_in_progress = False
        self._intake_committing = False
        self._preset_explicit = False
        self._programmatic_output = False
        self._output_user_modified = False
        self._audio_mode_explicit = False
        self._setting_audio_mode = False
        self._preserve_restored_settings = False
        self._smart_native_conversion = False
        self.settings_file = WORKSPACE / "ui_preferences.json"
        self.sr_available, self.interp_available = ai_tools_available()
        self.setStyleSheet(self.stylesheet())
        root = QWidget()
        self.setCentralWidget(root)
        shell = QVBoxLayout(root)
        shell.setContentsMargins(20, 14, 20, 14)
        shell.setSpacing(12)

        toolbar = QHBoxLayout()
        name = QLabel("杜比工作站")
        name.setObjectName("appName")
        try:
            from app_identity import app_icon
            icon_label = QLabel()
            icon_label.setPixmap(app_icon().pixmap(24, 24))
            toolbar.addWidget(icon_label)
        except ImportError:
            pass
        toolbar.addWidget(name)
        self.import_button = QPushButton("添加文件…")
        self.import_button.setToolTip("选择视频，或同时选择视频和音频（Ctrl+O）")
        self.import_button.clicked.connect(self.choose_media)
        toolbar.addWidget(self.import_button)
        self.check_tools = QPushButton("工具检查")
        self.check_tools.clicked.connect(self.check_environment)
        toolbar.addStretch()
        self.top_status = QLabel("等待添加视频")
        self.top_status.setObjectName("muted")
        toolbar.addWidget(self.top_status)
        self.window_toggle = QPushButton("还原窗口")
        self.window_toggle.clicked.connect(self.toggle_window_state)
        self.window_toggle.setToolTip("在普通窗口和最大化窗口之间切换")
        toolbar.addWidget(self.window_toggle)
        shell.addLayout(toolbar)

        self.notification = QLabel("视频可自带音轨，也可以把视频和音频一起拖入。")
        self.notification.setObjectName("notification")
        self.notification.setWordWrap(True)
        shell.addWidget(self.notification)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        stack = QVBoxLayout(content)
        stack.setContentsMargins(0, 0, 3, 0)
        stack.setSpacing(10)
        self.content_scroll.setWidget(content)
        shell.addWidget(self.content_scroll, 1)
        workspace_row = QHBoxLayout()
        workspace_row.setSpacing(16)

        self.input_group = MediaDropArea()
        self.input_group.files_dropped.connect(lambda paths: self.import_files(paths, "auto"))
        self.input_group.browse_requested.connect(self.choose_media)
        sources = QVBoxLayout(self.input_group)
        sources.setContentsMargins(24, 24, 24, 20)
        sources.setSpacing(12)
        sources.addStretch(1)
        self.drop_title = QLabel("将视频拖到这里")
        self.drop_title.setAlignment(Qt.AlignCenter)
        self.drop_title.setObjectName("dropTitle")
        self.drop_title.setWordWrap(True)
        sources.addWidget(self.drop_title)
        self.drop_hint = QLabel("或点击上方“添加文件”\n支持整片导入、音频先导入、视频与音频同时导入")
        self.drop_hint.setAlignment(Qt.AlignCenter)
        self.drop_hint.setObjectName("muted")
        self.drop_hint.setWordWrap(True)
        sources.addWidget(self.drop_hint)
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setObjectName("muted")
        sources.addWidget(self.info_label)
        self.play_source = QPushButton("查看原片")
        self.play_source.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_source.clicked.connect(self.open_source)
        self.play_source.hide()
        sources.addWidget(self.play_source, 0, Qt.AlignHCenter)
        sources.addStretch(1)
        fields = QGridLayout()
        fields.setHorizontalSpacing(10)
        fields.setVerticalSpacing(10)
        self.video = FileField("尚未选择视频", "视频 (*.mp4 *.mkv *.mov *.m4v *.avi *.webm *.ts *.m2ts *.mxf);;所有文件 (*)")
        self.audio = FileField("使用视频自带音轨", "音频或含音轨的视频 (*.wav *.flac *.mp3 *.aac *.m4a *.eac3 *.ac3 *.mp4 *.mkv *.mov *.mka *.opus *.ogg);;所有文件 (*)")
        self.video.import_target = "video"
        self.audio.import_target = "audio"
        self.video.edit.setReadOnly(True)
        self.audio.edit.setReadOnly(True)
        self.video.edit.setClearButtonEnabled(False)
        self.audio.edit.setClearButtonEnabled(False)
        self.video.files_selected.connect(lambda paths: self.import_files(paths, "video"))
        self.audio.files_selected.connect(lambda paths: self.import_files(paths, "audio"))
        self.audio.browse_button.setText("选择音频…")
        self.audio_clear = QPushButton("使用原音")
        self.original_audio = self.audio_clear
        self.audio_clear.setToolTip("移除替换音频，恢复视频自带的音轨")
        self.audio_clear.clicked.connect(self.restore_original_audio)
        self.audio_clear.setEnabled(False)
        fields.addWidget(QLabel("视频"), 0, 0)
        fields.addWidget(self.video, 0, 1, 1, 2)
        fields.addWidget(QLabel("音轨"), 1, 0)
        fields.addWidget(self.audio, 1, 1)
        fields.addWidget(self.audio_clear, 1, 2)
        sources.addLayout(fields)
        self.audio_hint = QLabel("未替换音频时，直接使用视频原音。")
        self.audio_hint.setObjectName("muted")
        self.audio_hint.setWordWrap(True)
        sources.addWidget(self.audio_hint)
        workspace_row.addWidget(self.input_group, 3)

        self.param_group = QGroupBox("自动处理")
        self.param_group.setMinimumWidth(320)
        self.param_group.setMaximumWidth(440)
        export = QVBoxLayout(self.param_group)
        export.setContentsMargins(16, 22, 16, 16)
        export.setSpacing(10)
        self.export_preset = SafeComboBox()
        for key, (label, _) in self.PRESETS.items():
            self.export_preset.addItem(label, key)
        self.automatic_summary = QLabel("添加视频后自动检查格式。\n通常无需调整设置，直接开始处理。")
        self.smart_summary = self.automatic_summary
        self.automatic_summary.setObjectName("automaticSummary")
        self.automatic_summary.setWordWrap(True)
        export.addWidget(self.automatic_summary)
        self.preset_note = QLabel()
        self.preset_note.setObjectName("muted")
        self.preset_note.setWordWrap(True)
        self.resolution = SafeComboBox()
        for label, value in [("保持源分辨率", (0, 0)), ("1080p · 1920 × 1080", (1920, 1080)),
                             ("4K · 3840 × 2160", (3840, 2160)), ("8K · 7680 × 4320", (7680, 4320)),
                             ("竖屏 4K · 2160 × 3840", (2160, 3840))]:
            self.resolution.addItem(label, value)
        self.fps = SafeComboBox()
        for label, value in [("保持源帧率", 0), ("24 fps", 24), ("30 fps", 30), ("48 fps", 48), ("60 fps", 60), ("120 fps", 120)]:
            self.fps.addItem(label, value)
        compact = QGridLayout()
        compact.addWidget(QLabel("尺寸"), 0, 0)
        compact.addWidget(self.resolution, 0, 1)
        compact.addWidget(QLabel("帧率"), 1, 0)
        compact.addWidget(self.fps, 1, 1)
        export.addStretch()
        export.addWidget(QLabel("保存到"))
        self.output = QLineEdit()
        self.output.setAcceptDrops(False)
        self.output.setPlaceholderText("导入视频后自动生成文件名")
        self.output.textEdited.connect(self.output_edited)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output, 1)
        output_button = QPushButton("更改…")
        output_button.clicked.connect(self.choose_output)
        output_row.addWidget(output_button)
        export.addLayout(output_row)
        self.preflight = QPushButton("检查格式")
        self.preflight.clicked.connect(self.run_preflight)
        workspace_row.addWidget(self.param_group, 2)
        stack.addLayout(workspace_row, 1)

        detail_links = QHBoxLayout()
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("调整设置")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setArrowType(Qt.RightArrow)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        detail_links.addWidget(self.advanced_toggle)
        self.quality_toggle = QToolButton()
        self.quality_toggle.setText("格式检查详情")
        self.quality_toggle.setCheckable(True)
        self.quality_toggle.setArrowType(Qt.RightArrow)
        self.quality_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        detail_links.addWidget(self.quality_toggle)
        self.log_toggle = QToolButton()
        self.log_toggle.setText("日志")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setArrowType(Qt.RightArrow)
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        detail_links.addWidget(self.log_toggle)
        detail_links.addStretch()
        self.tool_label = QLabel()
        self.tool_label.setObjectName("muted")
        stack.addLayout(detail_links)

        self.advanced = QGroupBox("调整设置")
        adjustment_layout = QVBoxLayout(self.advanced)
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("处理方式"))
        preset_row.addWidget(self.export_preset, 1)
        adjustment_layout.addLayout(preset_row)
        adjustment_layout.addWidget(self.preset_note)
        adjustment_layout.addLayout(compact)
        advanced = QGridLayout()
        adjustment_layout.addLayout(advanced)
        advanced.setHorizontalSpacing(12)
        advanced.setVerticalSpacing(9)
        self.color = SafeComboBox()
        for label, value in [("SDR · BT.709", "sdr"), ("HDR10 · PQ", "hdr10"), ("HLG", "hlg"),
                             ("杜比视界 8.4 · HLG", "dv84"), ("杜比视界 8.1 · PQ", "dv81")]:
            self.color.addItem(label, value)
        self.audio_mode = SafeComboBox()
        for label, value in [("复制音轨（保留已有 Atmos）", "copy"), ("AAC", "aac"), ("E-AC-3（普通多声道）", "eac3"), ("不保留音轨", "none")]:
            self.audio_mode.addItem(label, value)
        self.ai = QCheckBox("启用超分")
        self.interp = QCheckBox("启用插帧")
        self.model = SafeComboBox()
        for label, value in [("通用 / 实拍", "realesrgan-x4plus"), ("动画视频", "realesr-animevideov3"), ("动画增强", "realesrgan-x4plus-anime")]:
            self.model.addItem(label, value)
        self.encoding_preset = SafeComboBox()
        for label, value in [("标准", "medium"), ("较快", "fast"), ("精细", "slow"), ("最快（测试）", "ultrafast")]:
            self.encoding_preset.addItem(label, value)
        self.crf = QSpinBox()
        self.crf.setRange(10, 30)
        self.crf.setValue(18)
        self.crf.setToolTip("数值越低，画质越高、文件越大。推荐 16–20。")
        self.batch_size = QSpinBox()
        self.batch_size.setRange(1, 256)
        self.batch_size.setValue(32)
        self.work_size = SafeComboBox()
        self.work_size.addItem("1080p 工作尺寸", (1920, 1080))
        self.work_size.addItem("720p 工作尺寸", (1280, 720))
        self.rpu = FileField("选择与成片匹配的 RPU.bin", "RPU 元数据 (*.bin);;所有文件 (*)")
        self.rpu.files_selected.connect(lambda paths: self.import_files(paths, "auto"))
        self.rpu_label = QLabel("杜比元数据")
        self.clean = QCheckBox("自动清理临时文件")
        self.clean.setChecked(True)
        advanced.addWidget(QLabel("画面格式"), 0, 0)
        advanced.addWidget(self.color, 0, 1)
        advanced.addWidget(QLabel("音频编码"), 0, 2)
        advanced.addWidget(self.audio_mode, 0, 3)
        advanced.addWidget(QLabel("编码速度"), 1, 0)
        advanced.addWidget(self.encoding_preset, 1, 1)
        advanced.addWidget(QLabel("画质 CRF"), 1, 2)
        advanced.addWidget(self.crf, 1, 3)
        advanced.addWidget(self.ai, 2, 0)
        advanced.addWidget(self.model, 2, 1)
        advanced.addWidget(self.interp, 2, 2)
        advanced.addWidget(self.work_size, 2, 3)
        advanced.addWidget(QLabel("每批帧数"), 3, 0)
        advanced.addWidget(self.batch_size, 3, 1)
        advanced.addWidget(self.clean, 3, 2, 1, 2)
        advanced.addWidget(self.rpu_label, 4, 0)
        advanced.addWidget(self.rpu, 4, 1, 1, 3)
        tool_row = QHBoxLayout()
        tool_row.addWidget(self.tool_label)
        tool_row.addStretch()
        tool_row.addWidget(self.check_tools)
        adjustment_layout.addLayout(tool_row)
        self.advanced.hide()
        stack.addWidget(self.advanced)

        self.quality_group = QGroupBox("格式检查")
        q = QVBoxLayout(self.quality_group)
        qrow = QHBoxLayout()
        self.quality_status = QLabel("尚未检查")
        self.platform_status = QLabel("平台尚未验收")
        self.platform_status.setObjectName("muted")
        qrow.addWidget(self.quality_status, 1)
        qrow.addWidget(self.platform_status)
        qrow.addWidget(self.preflight)
        q.addLayout(qrow)
        self.output_summary = QLabel()
        self.output_summary.setWordWrap(True)
        self.output_summary.hide()
        q.addWidget(self.output_summary)
        self.quality_details = QLabel("导入后自动检查素材。平台识别结果仍需上传草稿并等待转码确认。")
        self.quality_details.setObjectName("muted")
        self.quality_details.setWordWrap(True)
        q.addWidget(self.quality_details)
        self.space_label = QLabel()
        self.space_label.setObjectName("muted")
        self.space_label.setWordWrap(True)
        q.addWidget(self.space_label)
        self.quality_group.hide()
        stack.addWidget(self.quality_group)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        self.log.setMaximumHeight(220)
        self.log.setMinimumHeight(130)
        self.log.hide()
        stack.addWidget(self.log)
        # These labels retain the source-summary API without presenting cards.
        self.source_video = (None, QLabel(self))
        self.source_dv = (None, QLabel(self))
        self.source_audio = (None, QLabel(self))
        for _, label in (self.source_video, self.source_dv, self.source_audio):
            label.hide()

        footer = QFrame()
        footer.setObjectName("footer")
        foot = QVBoxLayout(footer)
        foot.setContentsMargins(0, 10, 0, 0)
        foot.setSpacing(8)
        status_row = QHBoxLayout()
        self.stage_label = QLabel("添加视频后，点击“开始处理”。")
        self.elapsed = QLabel()
        self.elapsed.setObjectName("muted")
        status_row.addWidget(self.stage_label, 1)
        status_row.addWidget(self.elapsed)
        foot.addLayout(status_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFormat("0.0%")
        foot.addWidget(self.progress)
        controls = QHBoxLayout()
        self.start = QPushButton("开始处理")
        self.start.setObjectName("primary")
        self.start.setMinimumWidth(140)
        self.start.setEnabled(False)
        self.resume = QPushButton("恢复上次任务")
        self.pause = QPushButton("暂停")
        self.stop = QPushButton("停止")
        self.open_output = QPushButton("打开输出目录")
        self.view_report = QPushButton("查看报告")
        for button in (self.start, self.resume, self.pause, self.stop):
            controls.addWidget(button)
        controls.addStretch()
        controls.addWidget(self.open_output)
        controls.addWidget(self.view_report)
        for button in (self.pause, self.stop, self.open_output, self.view_report):
            button.setEnabled(False)
        foot.addLayout(controls)
        shell.addWidget(footer)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_elapsed)
        self.probe_timer = QTimer(self)
        self.probe_timer.setSingleShot(True)
        self.probe_timer.setInterval(350)
        self.probe_timer.timeout.connect(self.start_probe)
        self.start.clicked.connect(self.start_new)
        self.resume.clicked.connect(self.resume_previous)
        self.pause.clicked.connect(self.toggle_pause)
        self.stop.clicked.connect(self.stop_processing)
        self.open_output.clicked.connect(self.open_output_folder)
        self.view_report.clicked.connect(self.show_report)
        self.video.changed.connect(self.video_changed)
        self.audio.changed.connect(self.audio_changed)
        self.export_preset.currentIndexChanged.connect(self.preset_selected)
        self.advanced_toggle.toggled.connect(self.toggle_advanced)
        self.quality_toggle.toggled.connect(self.toggle_quality)
        self.log_toggle.toggled.connect(self.toggle_log)
        for combo in (self.model, self.resolution, self.fps, self.color, self.audio_mode):
            combo.currentIndexChanged.connect(self.update_estimates)
            combo.currentIndexChanged.connect(self.update_automatic_summary)
        self.resolution.currentIndexChanged.connect(self.refresh_start_state)
        self.color.currentIndexChanged.connect(self.update_control_states)
        self.audio_mode.currentIndexChanged.connect(self.audio_mode_selected)
        self.ai.toggled.connect(self.update_control_states)
        self.interp.toggled.connect(self.update_control_states)
        self.clean.toggled.connect(self.update_estimates)
        self.batch_size.valueChanged.connect(self.update_estimates)
        self.shortcut_open = QShortcut(QKeySequence.Open, self)
        self.shortcut_open.activated.connect(self.choose_media)
        self.apply_preset()
        self.load_preferences()
        # Import defaults are determined from each source, not a prior project.
        self._choosing_automatic = True
        self.select_data(self.export_preset, "preserve")
        self._choosing_automatic = False
        self.apply_preset()
        self.advanced_toggle.setChecked(False)
        self._preset_explicit = False
        self._audio_mode_explicit = False
        self.set_compatible_audio()
        self.refresh_resume()
        self.check_environment(silent=True)

    @staticmethod
    def stylesheet():
        return """
        QMainWindow, QWidget { background:#f3f4f6; color:#242a32; font-family:'Microsoft YaHei UI','Microsoft YaHei','Segoe UI'; font-size:16px; }
        QLabel#appName { font-size:20px; font-weight:600; padding-right:10px; }
        QLabel#muted { color:#697382; font-size:14px; }
        QLabel#automaticSummary { color:#263548; font-size:18px; }
        QLabel#notification { color:#5b6573; padding:3px 0; }
        QLabel#dropTitle { color:#333d4b; font-size:24px; font-weight:500; }
        QFrame#dropArea { background:white; border:1px dashed #aab3bf; border-radius:7px; }
        QFrame#dropArea QLabel, QFrame#dropArea QWidget { background:transparent; }
        QGroupBox { background:white; border:1px solid #d4d9e0; border-radius:6px; margin-top:8px; padding:18px 12px 12px; }
        QGroupBox::title { subcontrol-origin:margin; left:12px; top:1px; padding:0 4px; color:#3e4856; font-weight:600; }
        QGroupBox QLabel, QGroupBox QWidget { background:transparent; }
        QLineEdit, QComboBox, QSpinBox { background:white; color:#242a32; border:1px solid #bdc5cf; border-radius:4px; padding:9px 10px; min-height:20px; selection-background-color:#d9e8ff; selection-color:#162d4b; }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border:1px solid #3676c8; }
        QLineEdit:read-only { color:#4d5867; background:#f8f9fb; }
        QComboBox::drop-down { border:0; width:20px; }
        QComboBox QAbstractItemView { background:white; color:#242a32; selection-background-color:#e3edfb; }
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color:#8b94a1; background:#f0f2f5; border-color:#dce0e5; }
        QPushButton { background:#fff; border:1px solid #bdc5cf; border-radius:4px; padding:8px 14px; min-height:22px; font-size:16px; }
        QPushButton:hover { background:#edf3fc; border-color:#7f9fc7; }
        QPushButton:pressed { background:#e0eaf8; }
        QPushButton:disabled { color:#a2a9b3; background:#f4f5f7; border-color:#dde1e6; }
        QPushButton#primary { color:white; background:#2563d9; border-color:#2563d9; font-weight:600; }
        QPushButton#primary:hover { background:#1d4ed8; }
        QPushButton#primary:disabled { background:#a9bfdb; border-color:#a9bfdb; color:#f8fafc; }
        QToolButton { background:transparent; color:#53677f; border:0; padding:5px 3px; }
        QCheckBox { spacing:7px; }
        QPlainTextEdit { background:#fafbfc; color:#364152; border:1px solid #d5dbe3; border-radius:4px; font-family:Consolas; font-size:14px; }
        QProgressBar { background:#e2e6ed; color:#263548; border:0; border-radius:3px; text-align:center; min-height:20px; font-size:14px; }
        QProgressBar::chunk { background:#6b9bd5; border-radius:3px; }
        QScrollBar:vertical { background:#f3f4f6; width:9px; margin:0; }
        QScrollBar::handle:vertical { background:#bdc5cf; border-radius:4px; min-height:26px; }
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
        return "preserve" if key in ("preserve", "bili_preserve") else "enhance" if key == "enhance" else "transcode"

    def choose_media(self):
        if self.worker is not None or self.intake_busy:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "添加视频或音频", "", "媒体文件 (*.mp4 *.mkv *.mov *.m4v *.avi *.webm *.ts *.m2ts *.mxf *.wav *.flac *.mp3 *.aac *.m4a *.eac3 *.ac3 *.mka *.ogg *.opus);;所有文件 (*)")
        if paths:
            self.import_files(paths, "auto")

    def notify(self, message, error=False):
        self.notification.setText(message)
        self.notification.setStyleSheet("color:#a3342b" if error else "color:#5b6573")

    def dragEnterEvent(self, event):
        if self.worker is None and not self.intake_busy and event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if self.import_files(paths, "auto"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def import_files(self, paths, target="auto"):
        if self.worker is not None:
            message = "正在导出，素材不能更改。请先停止任务。"
            self.notify(message, True)
            self.intake_finished.emit(False, message)
            return False
        if self.intake_busy:
            message = "正在读取文件，请稍候再添加。"
            self.notify(message)
            self.intake_finished.emit(False, message)
            return False
        if target not in ("auto", "video", "audio") or not paths:
            message = "没有可导入的媒体文件。"
            self.notify(message, True)
            self.intake_finished.emit(False, message)
            return False
        normalized = []
        for path in paths:
            text = str(path)
            if text not in normalized:
                normalized.append(text)
        self.intake_busy = True
        self.import_button.setEnabled(False)
        self.audio_clear.setEnabled(False)
        self.start.setEnabled(False)
        self.resume.setEnabled(False)
        self.param_group.setEnabled(False)
        self.advanced.setEnabled(False)
        self.notify("正在检查文件…")
        thread = IntakeThread(normalized, target)
        self.intakes.add(thread)
        thread.result.connect(lambda entries, error, t=thread: self.finish_intake(t, entries, error))
        thread.finished.connect(lambda t=thread: self.intake_thread_finished(t))
        thread.start()
        return True

    def intake_thread_finished(self, thread):
        self.intakes.discard(thread)
        if not getattr(thread, "_delivered", False):
            self.intake_busy = False
            self.intake_finished.emit(False, "导入已取消。")
        thread.deleteLater()
        if self.closing and self.worker is None and not self.probes and not self.intakes:
            self.close()

    def finish_intake(self, thread, entries, error):
        thread._delivered = True
        self.intake_busy = False
        self.import_button.setEnabled(self.worker is None)
        self.param_group.setEnabled(self.worker is None)
        self.advanced.setEnabled(self.worker is None)
        if self.closing:
            self.intake_finished.emit(False, "窗口正在关闭。")
            return
        if self.worker is not None:
            error = "正在导出，未更改素材。"
        videos = [item for item in entries if item["kind"] == "video"]
        audios = [item for item in entries if item["kind"] == "audio"]
        video, audio = None, None
        if not error and thread.target == "audio":
            if len(entries) != 1:
                error = "音轨区域一次只接受一个音频文件或一个含音轨的视频，当前素材保持不变。"
            elif not entries[0]["info"].get("audio_streams"):
                error = "这个文件没有音轨，当前音轨保持不变。"
            else:
                audio = entries[0]
        elif not error:
            if len(videos) > 1:
                error = "一次只能处理一个视频。请分别添加，当前素材保持不变。"
            elif len(audios) > 1:
                error = "一次只能添加一个替换音频，当前素材保持不变。"
            elif thread.target == "video" and (len(videos) != 1 or audios):
                error = "请在视频区域添加一个视频；替换音频请拖到音轨区域。"
            else:
                video = videos[0] if videos else None
                audio = audios[0] if audios else None
        if error:
            self.notify(error, True)
            self.update_audio_hint()
            self.refresh_start_state()
            self.refresh_resume()
            self.intake_finished.emit(False, error)
            return
        old_video = self.video.filepath
        self._intake_committing = True
        try:
            if video:
                different = not old_video or Path(old_video).resolve() != Path(video["path"]).resolve()
                if different:
                    self._preset_explicit = False
                    self._preserve_restored_settings = False
                    self._audio_mode_explicit = False
                if old_video and different and audio is None:
                    self.ainfo = None
                    self.audio.set_file("")
                self.video.set_file(video["path"])
                self.probe_timer.stop()
            if audio:
                self.ainfo = audio["info"]
                self._audio_mode_explicit = False
                self.audio.set_file(audio["path"])
            if video:
                self.on_probe(video["path"], video["info"], "")
            self.set_compatible_audio()
            self.update_audio_hint()
        finally:
            self._intake_committing = False
        if video and audio:
            message = "视频和替换音频已添加。确认设置后点击“开始处理”。"
        elif video:
            message = "视频已添加。确认设置后点击“开始处理”。"
        elif self.video.filepath:
            message = "替换音频已添加，可随时点击“使用原音”恢复。"
        else:
            message = "音频已添加，请继续拖入视频。"
        if self.output_size_needs_attention():
            message = "这个视频的尺寸太小，兼容转码需至少 64 × 48。请在“调整设置”中选择输出尺寸，当前不会开始编码。"
        self.notify(message)
        self.refresh_start_state()
        self.refresh_resume()
        self.intake_finished.emit(True, message)

    def set_compatible_audio(self):
        if self._audio_mode_explicit:
            self.update_audio_hint()
            return
        info = self.ainfo if self.audio.filepath and self.ainfo else self.vinfo or {}
        codec = info.get("audio_codec", "none")
        mode = "copy" if codec in ("none", "aac", "ac3", "eac3", "mp3", "alac") else "aac"
        self._setting_audio_mode = True
        try:
            self.select_data(self.audio_mode, mode)
        finally:
            self._setting_audio_mode = False

    def audio_mode_selected(self):
        if not self._setting_audio_mode and not self.restore_in_progress:
            self._audio_mode_explicit = True
        self.update_audio_hint()

    def audio_changed(self, path):
        if not path:
            self.ainfo = None
        self.update_audio_hint()

    def update_audio_hint(self):
        if not hasattr(self, "audio_hint"):
            return
        self.audio_clear.setEnabled(bool(self.audio.filepath) and self.worker is None and not self.intake_busy)
        if self.audio.filepath:
            codec = (self.ainfo or {}).get("audio_codec", "")
            atmos = (self.ainfo or {}).get("atmos")
            text = "将使用替换音频"
            if codec:
                text += f" · {codec.upper()}"
            text += self.audio_processing_hint(codec, atmos)
            if not self.video.filepath:
                text += "；请继续添加视频。"
            self.audio_hint.setText(text)
        elif self.vinfo:
            codec = self.vinfo.get("audio_codec", "none")
            if codec == "none":
                self.audio_hint.setText("视频无音轨；可拖入音频，也可以导出静音视频。")
            else:
                suffix = self.audio_processing_hint(codec, self.vinfo.get("atmos"))
                self.audio_hint.setText(f"使用视频原音 · {codec.upper()}{suffix}")
        else:
            self.audio_hint.setText("未替换音频时，直接使用视频原音。")
        self.update_automatic_summary()

    def update_automatic_summary(self):
        if not hasattr(self, "automatic_summary"):
            return
        if not self.vinfo:
            self.automatic_summary.setText("音频已添加，继续添加视频即可。" if self.audio.filepath else "添加视频后自动检查格式。\n通常无需调整设置，直接开始处理。")
            return
        route = self.current_route()
        picture = "画面保持原样" if route == "preserve" else "画面按所选设置处理"
        if self._smart_native_conversion and route == "transcode":
            source_color = {"arib-std-b67": "hlg", "smpte2084": "hdr10"}.get(self.vinfo.get("color_transfer"), "sdr")
            unchanged = tuple(self.resolution.currentData()) == (0, 0) and not self.fps.currentData() and self.color.currentData() == source_color
            picture = "自动转为兼容 MP4\n" + ("保持源尺寸、帧率和色彩设置" if unchanged else "画面按所选设置处理")
        if self.output_size_needs_attention():
            picture += "\n原片尺寸过小，请在“调整设置”中选择更大尺寸"
        source_dv = self.vinfo.get("dv_profile")
        if source_dv and route != "preserve":
            if self.color.currentData() in ("dv81", "dv84"):
                picture += "\n仅使用原片基础层；原杜比元数据不保留，将按配套 RPU 重新制作"
            else:
                picture += "\n仅处理基础层，不保留原杜比标签"
        mode = self.audio_mode.currentData()
        if mode == "none":
            sound = "不保留音轨"
        elif self.audio.filepath:
            sound = "使用替换音频"
        elif self.vinfo.get("audio_codec", "none") == "none":
            sound = "原片无音轨，可直接处理或另加音频"
        else:
            sound = "使用原音"
        prefix = "保存到" if self._output_user_modified else "自动保存到"
        destination = self.output.text().strip()
        self.automatic_summary.setText(f"{picture}\n\n{sound}\n\n{prefix}：\n{Path(destination).name if destination else '视频所在文件夹'}")

    def audio_processing_hint(self, codec, atmos):
        mode = self.audio_mode.currentData()
        if mode == "none":
            return " · 当前设置不导出音轨（不保留 Atmos）" if atmos == "confirmed" else " · 当前设置不导出音轨"
        if mode == "copy" and codec in ("aac", "ac3", "eac3", "mp3", "alac"):
            return " · Atmos 已确认，将保留原码流" if atmos == "confirmed" else " · 保留原音轨"
        if mode in ("aac", "eac3"):
            return f" · 转为 {mode.upper()}，不保留 Atmos 对象元数据" if atmos == "confirmed" else f" · 转为 {mode.upper()}"
        if codec == "truehd":
            return " · TrueHD 不能直接封装为 MP4，请选择 AAC 或 E-AC-3"
        return ""

    def restore_original_audio(self):
        if self.worker is not None or self.intake_busy:
            return
        self.ainfo = None
        self._audio_mode_explicit = False
        self.audio.set_file("")
        self.set_compatible_audio()
        self.update_audio_hint()
        self.notify("已恢复视频原音。" if self.vinfo else "已移除待配对音频。")

    def output_edited(self, text):
        if not self._programmatic_output:
            self._output_user_modified = bool(text.strip())
        self.update_automatic_summary()

    def default_output(self, path):
        source = Path(path)
        candidate = source.with_name(source.stem + "_导出.mp4")
        counter = 2
        while candidate.exists() or candidate.resolve() == source.resolve():
            candidate = source.with_name(f"{source.stem}_导出_{counter}.mp4")
            counter += 1
        return str(candidate)

    def preset_selected(self):
        if not self.restore_in_progress and not getattr(self, "_choosing_automatic", False):
            self._preset_explicit = True
            self._preserve_restored_settings = False
            self._smart_native_conversion = False
        self.apply_preset()

    def open_source(self):
        if self.video.filepath and Path(self.video.filepath).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.video.filepath))

    def choose_source_preset(self):
        if self._preset_explicit or not self.vinfo:
            return
        info = self.vinfo
        suitable = info.get("dv_profile") == 8 and info.get("dv_compatibility") == 4 and info.get("color_transfer") == "arib-std-b67"
        if suitable:
            from quality import assess_bilibili
            suitable = assess_bilibili(info).get("status") != "fail"
        codec = str(info.get("codec_name") or info.get("stream", {}).get("codec_name") or "unknown").lower()
        containers = {name.strip().lower() for name in str(info.get("container") or info.get("format", {}).get("format_name") or "").split(",")}
        # Annex B to MP4 changes the bitstream representation; use the native
        # route rather than a copy that cannot pass strict payload validation.
        annex_b = bool(containers & {"mpegts", "h264", "hevc"})
        mp4_copyable = codec in {"h264", "hevc", "av1", "vp9", "mpeg4"} and not annex_b
        self._smart_native_conversion = not mp4_copyable
        key = "bili_preserve" if suitable and mp4_copyable else "preserve" if mp4_copyable else "quality"
        self._choosing_automatic = True
        self.select_data(self.export_preset, key)
        self._choosing_automatic = False
        self.apply_preset()

    def output_size_needs_attention(self):
        if not self.vinfo or self.current_route() == "preserve":
            return False
        cfg = self.config()
        return cfg["width"] < 64 or cfg["height"] < 48

    def refresh_start_state(self):
        if not hasattr(self, "start"):
            return
        ready = self.vinfo is not None and self.worker is None and not self.intake_busy
        self.start.setEnabled(ready and not self.output_size_needs_attention())
        self.update_automatic_summary()

    def toggle_window_state(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self.update_window_button()

    def update_window_button(self):
        if hasattr(self, "window_toggle"):
            self.window_toggle.setText("还原窗口" if self.isMaximized() else "最大化")

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            self.update_window_button()

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
        elif key in ("preserve", "bili_preserve") and self.vinfo:
            self.select_data(self.resolution, (0, 0))
            rate = self.vinfo.get("fps_fraction", self.vinfo["fps"])
            if not self.select_data(self.fps, rate):
                self.fps.addItem(f"原片 · {self.vinfo['fps']:.3f} fps", rate)
                self.fps.setCurrentIndex(self.fps.count() - 1)
            profile, compat = self.vinfo.get("dv_profile"), self.vinfo.get("dv_compatibility")
            color = "dv84" if profile == 8 and compat == 4 else "dv81" if profile == 8 and compat == 1 else {
                "arib-std-b67": "hlg", "smpte2084": "hdr10"}.get(self.vinfo.get("color_transfer"), "sdr")
            self.select_data(self.color, color)
        self.set_compatible_audio()
        self.update_control_states()
        self.update_estimates()
        self.update_automatic_summary()
        self.refresh_start_state()
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
        if needs_rpu:
            self.advanced_toggle.setChecked(True)
        self.audio_clear.setEnabled(bool(self.audio.filepath) and not running and not self.intake_busy)
        self.update_estimates()

    def update_rpu_state(self):
        self.update_control_states()

    def toggle_advanced(self, enabled):
        self.advanced.setVisible(enabled)
        self.advanced_toggle.setArrowType(Qt.DownArrow if enabled else Qt.RightArrow)

    def toggle_log(self, enabled):
        self.log.setVisible(enabled)
        self.log_toggle.setArrowType(Qt.DownArrow if enabled else Qt.RightArrow)

    def toggle_quality(self, enabled):
        self.quality_group.setVisible(enabled)
        self.quality_toggle.setArrowType(Qt.DownArrow if enabled else Qt.RightArrow)

    def choose_output(self):
        initial = self.output.text() or str(BASE_DIR)
        path, _ = QFileDialog.getSaveFileName(self, "选择新的输出文件", initial, "MP4 视频 (*.mp4)")
        if path:
            self.output.setText(path if path.lower().endswith(".mp4") else path + ".mp4")
            self._output_user_modified = True
            self.update_automatic_summary()

    def video_changed(self, path):
        for probe in self.probes:
            probe.request_stop()
        self.vinfo = None
        self.play_source.hide()
        if not self.restore_in_progress:
            self._preserve_restored_settings = False
            self.outcome = None
            self._smart_native_conversion = False
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
        self.start.setEnabled(False)
        self.drop_title.setText(Path(path).name if path else "将视频拖到这里")
        self.drop_hint.setText("正在检查视频…" if path else "或点击上方“添加文件”\n可以同时选择视频和音频")
        self.space_label.clear()
        if not self.restore_in_progress and path and not self._output_user_modified:
            self.output.setText(self.default_output(path))
        if not self._intake_committing:
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
            self.start.setEnabled(False)
            return
        rotation = info.get("rotation", 0)
        rotation_note = f"　·　旋转 {rotation}°" if rotation else ""
        self.info_label.setText(f"{info.get('display_width', info['width'])} × {info.get('display_height', info['height'])}　·　{info['fps']:.3f} fps　·　{fmt_time(info['duration'])}{rotation_note}")
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
        self.drop_title.setText(info.get("filename") or Path(path).name)
        self.drop_hint.setText("可直接开始处理，也可以继续拖入音频替换原音。")
        if not self.restore_in_progress and self.worker is None and not self._preserve_restored_settings:
            self.choose_source_preset()
            if self._preset_explicit:
                self.apply_preset()
        self.set_compatible_audio()
        self.update_audio_hint()
        self.play_source.show()
        self.refresh_start_state()
        result_visible = self.last_output is not None or (self._preserve_restored_settings and self.outcome is not None)
        if self.worker is None and not result_visible:
            attention = self.output_size_needs_attention()
            self.top_status.setText("需要调整尺寸" if attention else "可以处理")
            self.stage_label.setText("请在“调整设置”中选择更大输出尺寸。" if attention else "准备就绪，点击“开始处理”。")
        if not result_visible:
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
                self.quality_status.setText("素材已读取")
                self.quality_status.setStyleSheet("color:#374151;font-weight:600")
                self.quality_details.setText("已按源文件选择处理方式。保持原画质时不重编码视频；B站杜比格式需单独检查和平台转码验收。")
                self.view_report.setEnabled(True)
        except ImportError:
            self.quality_status.setText("素材已读取")
            self.quality_details.setText("画面与音轨信息已读取；交付质检组件尚未就绪。")

    def render_quality(self, report, prefix):
        status = report.get("status", "warning")
        label = {"pass": "本地技术条件通过", "warning": "有事项需要确认", "fail": "尚不符合目标条件"}.get(status, "待确认")
        self.quality_status.setText(f"{prefix} · {label}")
        colors = {"pass": "#167347", "warning": "#926200", "fail": "#b3261e"}
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
        base_ready = "ffmpeg" in available and "ffprobe" in available
        self.tool_label.setText(("基础工具可用" if base_ready else "缺少基础工具") + (" · 插帧/超分可用" if self.sr_available and self.interp_available else " · 可选增强工具未齐"))
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
        self.resume.setEnabled(checkpoint is not None and self.worker is None and not self.intake_busy)
        self.resume.setToolTip(f"恢复原任务：{Path(checkpoint['cfg'].get('video', '')).name}" if checkpoint else "没有可恢复的未完成任务")

    def restore_config(self, cfg):
        self.restore_in_progress = True
        self._preserve_restored_settings = True
        self._smart_native_conversion = False
        try:
            route = cfg.get("route", "enhance")
            preset = ("bili_preserve" if cfg.get("delivery") == "bilibili" else "preserve") if route == "preserve" else "bili_make" if cfg.get("delivery") == "bilibili" else "enhance" if route == "enhance" else "quality"
            self.select_data(self.export_preset, preset)
            self.preset_note.setText(self.PRESETS[preset][1])
            self.video.set_file(cfg["video"])
            self.audio.set_file(cfg.get("audio", ""))
            self.rpu.set_file(cfg.get("rpu", ""))
            self.output.setText(str(cfg.get("output", "")))
            self._output_user_modified = bool(cfg.get("output"))
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
            self._audio_mode_explicit = True
        finally:
            self.restore_in_progress = False
        self.update_control_states()

    def resume_previous(self):
        if self.worker is not None or self.intake_busy:
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
        if self.worker is not None or self.intake_busy:
            return
        cfg = self.config()
        if not Path(cfg["video"]).is_file():
            QMessageBox.warning(self, "需要视频", "请先导入存在的视频文件。")
            return
        if not self.vinfo:
            QMessageBox.information(self, "正在分析素材", "请等待素材检查完成后开始；这将确认源分辨率、色彩和音轨，避免误用默认参数。")
            self.start_probe()
            return
        if self.output_size_needs_attention():
            self.notify("兼容转码的尺寸需至少 64 × 48。请在“调整设置”中选择输出尺寸。", True)
            self.advanced_toggle.setChecked(True)
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
        if cfg["route"] == "preserve" and cfg["delivery"] == "bilibili" and self.last_report and self.last_report.get("status") == "fail":
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
        self.import_button.setEnabled(False)
        self.advanced.setEnabled(False)
        self.audio_clear.setEnabled(False)
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
        self.import_button.setEnabled(True)
        self.advanced.setEnabled(True)
        self.update_control_states()
        self.refresh_start_state()
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
            self.top_status.setText("导出完成")
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
        self.quality_toggle.setChecked(True)
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
        if self.worker is not None or self.probes or self.intakes:
            self.closing = True
            self.stop_processing()
            for probe in self.probes:
                probe.request_stop()
            for intake in self.intakes:
                intake.request_stop()
            self.stage_label.setText("正在安全结束任务，完成后关闭…")
            event.ignore()
            return
        self.save_preferences()
        event.accept()


def main():
    # Resolve PyQt's actual Unicode installation directory before QApplication.
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    plugins = Path(PyQt5.__file__).resolve().parent / "Qt5" / "plugins"
    if plugins.is_dir():
        QApplication.setLibraryPaths([str(plugins)])
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 12))
    from app_identity import configure_application
    configure_application(app)
    window = MainWindow()
    window.showMaximized()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
