"""Exercise the real submission window and its worker, with encoding held at a gate."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave


ROOT = Path(__file__).resolve().parents[1]


def exercise(case, directory):
    sys.path.insert(0, str(ROOT))
    import consumer_app as ui
    from PyQt5.QtCore import QMimeData, QPoint, QPointF, QTimer, Qt, QUrl
    from PyQt5.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication

    source, audio = directory / "无声视频.mp4", directory / "配乐.wav"
    output = directory / ("output-" + case)
    output.mkdir()
    gate = threading.Event()
    entered = threading.Event()
    calls = []
    failure = []
    saved_rate = 60

    def wait_for(predicate):
        until = time.monotonic() + 12
        while not predicate():
            if time.monotonic() >= until:
                raise AssertionError(f"Timed out in {case}")
            QApplication.processEvents()
            QTest.qWait(5)

    class GatePipeline:
        def __init__(self, cfg, stage, progress):
            self.cfg, self.stage, self.progress = cfg, stage, progress
            self.cancelled = threading.Event()

        def cancel(self):
            self.cancelled.set()
            gate.set()

        def run(self):
            calls.append(self.cfg)
            self.stage("1/6 测试处理", 1, 6)
            self.progress(27)
            entered.set()
            if not gate.wait(12):
                raise AssertionError("Test worker timed out")
            if self.cancelled.is_set():
                raise ui.PipelineCancelled("cancelled")
            result = Path(self.cfg["output"])
            kind = {"drop_pair": "ai_bandwidth_extension", "audio_first": "atmos_stereo_downmix"}.get(case)
            has_sound = bool(self.cfg["audio"])
            inspection = {"atmos": kind == "atmos_stereo_downmix", "hires_delivery": has_sound,
                          "native_hires": bool(has_sound and kind is None), "master_kind": kind,
                          "audio_tracks": 2 if kind == "atmos_stereo_downmix" else int(has_sound)}
            report = {"media": {"dv_profile": 8, "dv_compatibility": 4, "width": 96,
                                "height": 64, "fps": saved_rate}, "export": {"consumer_audio": inspection}}
            result.touch()
            Path(str(result) + ".quality.json").write_text(json.dumps(report), encoding="utf-8")
            return result

    def drop(window, paths):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        enter = QDragEnterEvent(QPoint(20, 20), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(window, enter)
        if not enter.isAccepted():
            return False
        QApplication.sendEvent(window, QDragMoveEvent(QPoint(20, 20), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier))
        release = QDropEvent(QPointF(20, 20), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(window, release)
        return release.isAccepted()

    def scenario(w):
        assert w.isVisible() and w.isMaximized() and not w.isFullScreen()
        assert not w.windowFlags() & Qt.FramelessWindowHint and not w.grab().isNull()
        if case == "picker":
            with patch.object(ui.QFileDialog, "getOpenFileNames", return_value=([str(source)], "")):
                QTest.mouseClick(w.primary, Qt.LeftButton)
        elif case == "drop_pair":
            assert drop(w, [audio, source])
        elif case == "audio_first":
            assert drop(w, [audio])
            wait_for(lambda: w.worker is None and w.soundtrack and Path(w.soundtrack) == audio)
            assert not calls and audio.name in w.message.text()
            assert drop(w, [source])
        else:
            with patch.object(ui.QFileDialog, "getOpenFileName", return_value=(str(audio), "")):
                QTest.mouseClick(w.add_sound, Qt.LeftButton)
            assert Path(w.soundtrack) == audio and not calls
            if case == "native":
                assert "移除" in w.add_sound.text()
                assert drop(w, [source, source])
                wait_for(lambda: w.worker is None)
                assert "两个视频" in w.message.text() and Path(w.soundtrack) == audio and not calls
                QTest.mouseClick(w.add_sound, Qt.LeftButton)
                assert not w.soundtrack and "添加" in w.add_sound.text()
                with patch.object(ui.QFileDialog, "getOpenFileName", return_value=(str(audio), "")):
                    QTest.mouseClick(w.add_sound, Qt.LeftButton)
            assert drop(w, [source])

        wait_for(lambda: entered.is_set() and w.bar.value() == 27)
        assert len(calls) == 1 and calls[0]["bili_one_click"] and Path(calls[0]["video"]) == source
        assert (not calls[0]["audio"]) if case == "picker" else Path(calls[0]["audio"]) == audio
        assert w.stop.isVisible() and not w.primary.isVisible() and not w.change_folder.isVisible()
        assert not drop(w, [source]) and len(calls) == 1, "busy window started a duplicate export"
        if case == "cancel_retry":
            QTest.mouseClick(w.stop, Qt.LeftButton)
            wait_for(lambda: w.worker is None)
            assert w.exported is None and not list(output.glob("*.mp4"))
            assert w.primary.isVisible() and not w.bar.isVisible()
            assert drop(w, [source])
        gate.set()
        wait_for(lambda: w.worker is None and w.exported is not None)
        assert w.exported.parent == output and w.exported.exists()
        assert w.folder.isVisible() and not w.stop.isVisible() and not w.bar.isVisible()
        assert "60 fps" in w.detail.text() and "8.4" in w.detail.text()
        if case == "picker":
            assert "无音轨" in w.detail.text()
        elif case == "audio_first":
            assert "从 Atmos 混制" in w.detail.text() and "已校验 Atmos" in w.detail.text()
        elif case == "drop_pair":
            assert "AI 重制" in w.detail.text() and "已校验 Atmos" not in w.detail.text()
        elif case == "native":
            assert "保留无损母版" in w.detail.text()
        assert "转码" in w.detail.text()

    class Window(ui.SubmissionWindow):
        def __init__(self, *args):
            super().__init__(*args)
            QTimer.singleShot(80, self.check)

        def check(self):
            try:
                scenario(self)
            except BaseException:
                import traceback
                failure.append(traceback.format_exc())
            finally:
                gate.set()
                if self.worker:
                    self.worker.cancel()
                    wait_for(lambda: self.worker is None)
                self.close()

    with patch.object(ui, "SubmissionWindow", Window), patch.object(ui, "Pipeline", GatePipeline):
        result = ui.main(["--output-dir", str(output)])
    if failure:
        raise AssertionError(failure[0])
    return result


@unittest.skipUnless(importlib.util.find_spec("PyQt5") and shutil.which("ffmpeg") and
                     shutil.which("ffprobe"), "requires PyQt5 and FFmpeg")
class ConsumerInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        holder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(holder.cleanup)
        cls.folder = Path(holder.name).resolve()
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=96x64:rate=24:duration=1", "-c:v", "libx264",
                        str(cls.folder / "无声视频.mp4")], check=True, timeout=30)
        with wave.open(str(cls.folder / "配乐.wav"), "wb") as wav:
            wav.setparams((2, 3, 48000, 0, "NONE", "not compressed"))
            wav.writeframes(bytes(48000 * 6))

    def test_startup_import_busy_and_result(self):
        for case in ("picker", "drop_pair", "audio_first", "native", "cancel_retry"):
            with self.subTest(case=case):
                completed = subprocess.run([sys.executable, __file__, case, str(self.folder)], cwd=ROOT,
                                           env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1"),
                                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
                self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])


if __name__ == "__main__" and len(sys.argv) == 3:
    sys.exit(exercise(sys.argv[1], Path(sys.argv[2])))
