"""Desktop entry point for creating a Bilibili submission from a video."""
import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from PyQt5.QtCore import Qt, QStandardPaths, QThread, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QFont
from PyQt5.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
                             QMainWindow, QProgressBar, QPushButton, QVBoxLayout, QWidget)

from app_identity import configure_application
from consumer_picture import make_job
from pipeline import Pipeline, PipelineCancelled, find_tool
from quality import inspect_audio, inspect_media, QualityError


def choose_inputs(paths, soundtrack=None, cancel=None):
    if not 1 <= len(paths) <= 2:
        raise ValueError("每次请拖入一个视频。若有单独音轨，可与视频一起拖入。")
    video = info = None
    audio = str(soundtrack or "")
    for path in paths:
        if not Path(path).is_file():
            raise ValueError(f"找不到文件：{Path(path).name}")
        try:
            current = inspect_media(path, ffprobe=find_tool("ffprobe"), cancel_event=cancel)
        except QualityError as original:
            if cancel and cancel.is_set():
                raise PipelineCancelled("已取消") from original
            try:
                inspect_audio(path, ffprobe=find_tool("ffprobe"), cancel_event=cancel)
            except QualityError:
                raise ValueError(f"无法读取《{Path(path).name}》，请重新选择原始媒体文件。") from original
            if audio:
                raise ValueError("已选两条独立音轨。请选择一条，再与视频一起拖入。")
            audio = str(path)
        else:
            if video:
                raise ValueError("已选两个视频。若要使用另一个视频里的声音，先点“添加独立音轨”。")
            video, info = str(path), current
    return video, audio, info


def next_output(folder, video):
    stem = Path(video).stem
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        suffix = "" if index == 1 else f"_{index}"
        path = folder / f"{stem}_B站投稿{suffix}.mp4"
        if not path.exists() and not Path(str(path) + ".quality.json").exists():
            return path
        index += 1


class ExportThread(QThread):
    stage_text = pyqtSignal(str)
    plan_text = pyqtSignal(str)
    progress_value = pyqtSignal(int)
    audio_selected = pyqtSignal(str)
    completed = pyqtSignal(str, object)
    failed = pyqtSignal(str)

    def __init__(self, paths, audio, destination, parent=None):
        super().__init__(parent)
        self.paths, self.audio, self.destination = paths, audio, destination
        self.cancel_event = threading.Event()
        self.pipeline = None

    def cancel(self):
        self.cancel_event.set()
        if self.pipeline:
            self.pipeline.cancel()

    def run(self):
        try:
            video, audio, info = choose_inputs(self.paths, self.audio, self.cancel_event)
            if self.cancel_event.is_set():
                raise PipelineCancelled("已取消")
            if not video:
                self.audio_selected.emit(audio)
                return
            output = next_output(self.destination, video)
            with tempfile.TemporaryDirectory(prefix="bili-export-") as scratch:
                cfg = make_job(info, video, str(output), audio=audio, workspace=scratch)
                plan = cfg["picture_plan"]
                from consumer_audio import select_tracks
                external = inspect_audio(audio, ffprobe=find_tool("ffprobe"), cancel_event=self.cancel_event) if audio else None
                tracks = select_tracks(info["audio_streams"], external["audio_streams"] if external else None)
                if not tracks:
                    sound = "视频没有声音，本次将生成静音投稿文件。"
                elif any(t["plan"].get("joc") for t in tracks):
                    sound = "检测到 Atmos，正在准备立体声和全景声两条音轨。"
                elif os.getenv("DOLBY_DEE_PATH") and os.getenv("DOLBY_DEE_PROFILE"):
                    sound = "正在准备主音轨，并尝试使用本机编码服务制作 Atmos。"
                else:
                    sound = "本次制作主音轨。素材未含可用 Atmos，本机也未配置全景声编码服务。"
                self.plan_text.emit(plan["detail"] + "。\n" + sound)
                self.stage_text.emit(f"{Path(video).name}　·　{plan['source_fps']} → {plan['output_fps']} fps")
                def stage(name, *_):
                    if plan["method"] == "motion_interpolation" and name.startswith("1/"):
                        self.stage_text.emit(f"正在生成中间帧并编码画面　·　目标 {plan['output_fps']} fps")
                    else:
                        self.stage_text.emit(name.split(" ", 1)[-1])
                self.pipeline = Pipeline(cfg, stage=stage, progress=lambda p: self.progress_value.emit(int(p)))
                if self.cancel_event.is_set():
                    self.pipeline.cancel()
                result = Path(self.pipeline.run())
                try:
                    report = json.loads(Path(str(result) + ".quality.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    report = {}
                self.completed.emit(str(result), report)
        except Exception as exc:
            self.failed.emit("已取消制作，可以重新拖入视频。" if self.cancel_event.is_set() else str(exc))


def result_lines(report):
    if not report:
        return "成片已保存。本地格式报告未能读取。"
    media = report.get("media", {})
    audio = report.get("export", {}).get("consumer_audio", {})
    picture = (f"杜比视界 8.4　·　{media.get('width', '?')} × {media.get('height', '?')}　·　"
               f"{float(media.get('fps') or 0):g} fps") if (media.get("dv_profile"), media.get("dv_compatibility")) == (8, 4) else "画面已保存"
    lines = [picture]
    if audio.get("atmos"):
        lines.append("杜比全景声　·　已校验 Atmos 音轨")
    else:
        lines.append("杜比全景声　·　" + (audio.get("atmos_note") or "本片没有可交付的 Atmos 音轨"))
    if audio.get("hires_delivery") or audio.get("hires"):
        origin = {"ai_bandwidth_extension": "AI 重制立体声母版", "atmos_stereo_downmix": "从 Atmos 混制的立体声母版"}.get(audio.get("master_kind"), "保留无损母版")
        lines.append(f"Hi-Res 无损规格　·　{origin}")
    else:
        lines.append("Hi-Res 无损规格　·　" + ("本片使用普通音轨" if audio.get("audio_tracks") else "无音轨"))
    return "\n".join(lines)


class SubmissionWindow(QMainWindow):
    def __init__(self, destination=None):
        super().__init__()
        self.setWindowTitle("B站投稿助手")
        self.setAcceptDrops(True)
        self.resize(1100, 730)
        self.destination = Path(destination or (Path(QStandardPaths.writableLocation(QStandardPaths.MoviesLocation)
                                                    or Path.home() / "Videos") / "B站投稿"))
        self.soundtrack = ""
        self.exported = None
        self.worker = None
        self.closing = False
        self.started_at = 0
        outer = QWidget()
        self.setCentralWidget(outer)
        page = QVBoxLayout(outer)
        page.setContentsMargins(30, 18, 30, 18)
        page.addStretch()
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setMaximumWidth(850)
        panel.setMinimumWidth(480)
        panel.setSizePolicy(7, 5)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(panel, 9)
        row.addStretch(1)
        page.addLayout(row)
        page.addStretch()
        body = QVBoxLayout(panel)
        body.setContentsMargins(38, 28, 38, 28)
        body.setSpacing(12)
        self.heading = QLabel("制作 B站投稿视频")
        self.heading.setObjectName("heading")
        body.addWidget(self.heading)
        self.message = QLabel("将视频拖到这里，即可自动生成投稿 MP4。")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.PlainText)
        body.addWidget(self.message)
        self.detail = QLabel("自动处理画面和帧率，根据素材制作或保留音轨。")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setObjectName("detail")
        body.addWidget(self.detail)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(8)
        body.addWidget(self.bar)
        self.bar.hide()
        action = QHBoxLayout()
        self.primary = QPushButton("选择视频并生成")
        self.primary.setObjectName("primary")
        self.primary.clicked.connect(self.choose_video)
        self.add_sound = QPushButton("添加独立音轨…")
        self.add_sound.clicked.connect(self.choose_sound)
        self.stop = QPushButton("取消制作")
        self.stop.clicked.connect(self.stop_work)
        self.folder = QPushButton("打开所在文件夹")
        self.folder.clicked.connect(self.open_folder)
        for button in (self.primary, self.add_sound, self.stop, self.folder):
            action.addWidget(button)
        action.addStretch()
        body.addLayout(action)
        self.folder.hide()
        self.stop.hide()
        self.save_label = QLabel()
        self.save_label.setTextFormat(Qt.PlainText)
        self.save_label.setWordWrap(True)
        self.save_label.setObjectName("path")
        self.change_folder = QPushButton("更改保存位置…")
        self.change_folder.setFlat(True)
        self.change_folder.clicked.connect(self.choose_folder)
        tail = QHBoxLayout()
        tail.addWidget(self.save_label, 1)
        tail.addWidget(self.change_folder)
        body.addLayout(tail)
        self.refresh_path()
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self.show_elapsed)
        self.setStyleSheet("""
            QMainWindow,QWidget { background:#f2f3f5; color:#20262d; font-size:17px; }
            QFrame#panel { background:white; border:1px solid #cfd6d8; border-radius:10px; }
            QFrame#panel QLabel { background:white; }
            QLabel#heading { font-size:31px; font-weight:600; }
            QLabel#detail { color:#43545a; padding:8px 0; }
            QLabel#path { font-size:15px; color:#546166; }
            QPushButton { background:#f7f8f8; border:1px solid #c7cecf; border-radius:5px; padding:8px 13px; }
            QPushButton:hover { background:#e8edec; }
            QPushButton#primary { color:white; background:#176553; border-color:#176553; }
            QPushButton#primary:hover { background:#105343; }
            QProgressBar { border:0; background:#e9edec; } QProgressBar::chunk {background:#237a63;}
            """)

    def refresh_path(self):
        self.save_label.setText(f"保存位置　{self.destination}")

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择保存文件夹", str(self.destination))
        if path:
            self.destination = Path(path)
            self.refresh_path()

    def open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.exported.parent if self.exported else self.destination)))

    def choose_video(self):
        if self.exported and self.primary.text() == "播放成片":
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.exported)))
            return
        files, _ = QFileDialog.getOpenFileNames(self, "选择视频，可同时选择一条音轨", "", "媒体文件 (*.*)")
        if files:
            self.start_files(files)

    def choose_sound(self):
        if self.exported:
            self.reset_idle()
            self.choose_video()
            return
        if self.soundtrack:
            self.reset_idle()
            return
        f, _ = QFileDialog.getOpenFileName(self, "选择要配入的音轨")
        if f:
            self.waiting_video(f)

    def reset_idle(self):
        self.exported = None
        self.soundtrack = ""
        self.heading.setText("制作 B站投稿视频")
        self.primary.setText("选择视频并生成")
        self.add_sound.setText("添加独立音轨…")
        self.folder.hide()
        self.detail.setText("自动处理画面和帧率，根据素材制作或保留音轨。")
        self.message.setText("将视频拖到这里，即可自动生成投稿 MP4。")

    def dragEnterEvent(self, event):
        if not self.worker and event.mimeData().hasUrls() and all(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if not self.worker and event.mimeData().hasUrls():
            self.start_files([u.toLocalFile() for u in event.mimeData().urls()])
            event.acceptProposedAction()

    def show_elapsed(self):
        if self.worker:
            seconds = int(time.monotonic() - self.started_at)
            self.save_label.setText(f"已用时 {seconds // 60:02d}:{seconds % 60:02d}　·　保存到 {self.destination}")

    def start_files(self, paths):
        if self.worker:
            return
        if self.exported:
            self.reset_idle()
        self.heading.setText("正在制作投稿文件")
        self.message.setText("读取素材…")
        self.detail.setText("可以继续使用电脑，完成后在这里打开成片。")
        self.bar.setRange(0, 0)
        self.bar.show()
        self.started_at = time.monotonic()
        for w in (self.primary, self.add_sound, self.folder, self.change_folder):
            w.hide()
        self.stop.setEnabled(True)
        self.stop.show()
        job = ExportThread(paths, self.soundtrack, self.destination, self)
        self.worker = job
        job.stage_text.connect(self.message.setText)
        job.plan_text.connect(self.detail.setText)
        job.progress_value.connect(self.set_progress)
        job.completed.connect(self.done)
        job.failed.connect(self.error)
        job.audio_selected.connect(self.waiting_video)
        job.finished.connect(self.thread_done)
        self.clock.start()
        job.start()

    def set_progress(self, value):
        if value > 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(max(self.bar.value(), value))

    def waiting_video(self, path):
        self.reset_idle()
        self.soundtrack = path
        self.add_sound.setText("移除独立音轨")
        self.message.setText(f"已选音轨《{Path(path).name}》，请拖入视频。")

    def done(self, path, report):
        self.exported = Path(path)
        self.heading.setText("投稿文件已保存")
        self.message.setText(self.exported.name)
        self.detail.setText(result_lines(report) + "\n\n投稿时按成片音轨勾选相应音质选项；标识以 B站转码后的播放器为准。")
        self.primary.setText("播放成片")
        self.add_sound.setText("再做一个视频")
        self.folder.show()

    def error(self, message):
        if self.soundtrack and Path(self.soundtrack).is_file():
            self.waiting_video(self.soundtrack)
            self.detail.setText(f"已保留音轨《{Path(self.soundtrack).name}》，可重新选择视频。")
        else:
            self.reset_idle()
        self.heading.setText("本次制作已停止")
        self.message.setText(message if len(message) <= 380 else "这个文件处理失败。请确认输入文件可正常播放，并检查保存位置的可用空间。")

    def thread_done(self):
        job, self.worker = self.worker, None
        self.clock.stop()
        self.bar.hide()
        self.stop.hide()
        for w in (self.primary, self.add_sound, self.change_folder):
            w.show()
        self.refresh_path()
        if job:
            job.deleteLater()
        if self.closing:
            self.close()

    def stop_work(self):
        if self.worker:
            self.worker.cancel()
            self.stop.setEnabled(False)
            self.message.setText("正在结束本次制作…")

    def closeEvent(self, event):
        if self.worker:
            self.closing = True
            self.stop_work()
            event.ignore()
        else:
            event.accept()


def main(argv=None):
    opts = argparse.ArgumentParser(description="B站投稿助手")
    opts.add_argument("files", nargs="*")
    opts.add_argument("--output-dir")
    opts.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    args = opts.parse_args(argv)
    import PyQt5
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    plugins = Path(PyQt5.__file__).resolve().parent / "Qt5" / "plugins"
    if plugins.is_dir():
        QApplication.setLibraryPaths([str(plugins)])
    app = QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 12))
    configure_application(app)
    window = SubmissionWindow(args.output_dir)
    window.showMaximized()
    if args.smoke:
        QTimer.singleShot(180, app.quit)
    elif args.files:
        QTimer.singleShot(0, lambda: window.start_files(args.files))
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
