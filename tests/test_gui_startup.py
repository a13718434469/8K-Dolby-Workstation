"""Exercise the actual GUI entry point in its own Qt process."""
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec("PyQt5"), "PyQt5 is not installed")
class GuiStartupTests(unittest.TestCase):
    def test_main_recovers_missing_plugin_search_path_and_shows_window(self):
        # A separate process permits QApplication even if test_ui has already
        # constructed the one QCoreApplication allowed in the test runner.
        script = r'''
import sys
import tempfile
from pathlib import Path
import main
from PyQt5.QtCore import QTimer, QPoint

# Reproduce Qt's broken automatic prefix without relying on a particular
# username or the CI runner having a non-ASCII installation directory.
main.QApplication.setLibraryPaths(["/nonexistent/dolby-startup-plugin-path"])
main.load_checkpoint = lambda: None
preferences = tempfile.TemporaryDirectory(prefix="dolby-ui-preferences-")
main.WORKSPACE = Path(preferences.name)
OriginalWindow = main.MainWindow

class StartupWindow(OriginalWindow):
    def __init__(self):
        super().__init__()
        QTimer.singleShot(100, self.verify_and_quit)

    def verify_and_quit(self):
        assert self.isVisible(), "main() did not show its window"
        assert self.isMaximized() and not self.isFullScreen(), "main() must open a normal maximized window"
        assert not self.start.isEnabled(), "export should wait until a video has been checked"
        assert self.select_data(self.resolution, (1920, 1080)), "resolution lookup failed"
        assert tuple(self.resolution.currentData()) == (1920, 1080), "wrong restored resolution"
        assert not self.grab().isNull(), "window could not render"
        self.showNormal()
        self.resize(1000, 640)
        main.QApplication.processEvents()
        assert self.height() <= 680, "layout forces the window off a small display"
        assert self.start.mapTo(self, QPoint(0, 0)).y() + self.start.height() <= self.height(), "primary control is clipped"
        # User-facing presets must never silently send HDR through the SDR AI
        # pipeline or re-encode a source selected for bitstream preservation.
        self.vinfo = {
            "filename": "fixture.mp4", "width": 3840, "height": 2160,
            "fps": 60.0, "fps_fraction": "60", "duration": 10,
            "total_frames": 600, "color_transfer": "arib-std-b67",
            "dv_profile": 8, "dv_compatibility": 4,
            "audio_codec": "eac3", "atmos": "confirmed",
        }
        self.select_data(self.export_preset, "bili_preserve")
        self.apply_preset()
        preserved = self.config()
        assert preserved["route"] == "preserve"
        assert preserved["width"] == 3840 and preserved["height"] == 2160
        assert not preserved["ai"] and not preserved["interp"]
        assert not self.color.isEnabled(), "preserve mode exposes color conversion"
        self.select_data(self.export_preset, "quality")
        native = self.config()
        assert native["route"] == "transcode"
        assert native["color_mode"] == "hlg" and not native["ai"]
        self.vinfo.update({"rotation": 90, "display_width": 2160, "display_height": 3840})
        self.apply_preset()
        assert (self.config()["width"], self.config()["height"]) == (2160, 3840), "rotated phone video lost its display aspect"
        self.select_data(self.export_preset, "bili_preserve")
        assert (self.config()["width"], self.config()["height"]) == (3840, 2160), "preserve mode rewrote encoded dimensions"
        self.select_data(self.export_preset, "quality")
        self.render_quality({
            "status": "pass", "checks": [{"label": "完整解码", "detail": "通过", "status": "pass"}],
            "bilibili_readiness": {"status": "fail", "summary": "不是 DV8.4"},
        }, "成片质检")
        assert "本地技术条件通过" in self.quality_status.text()
        assert "不影响本次常规成片" in self.quality_details.text()
        self.select_data(self.export_preset, "bili_make")
        assert self.config()["color_mode"] == "dv84" and self.rpu.isEnabled()
        # Smart import must choose a usable native route for codecs MP4 cannot
        # copy, and make encoder limits visible before a user starts the job.
        self.vinfo = dict(self.vinfo, codec_name="ffv1", width=192, height=128,
                          display_width=192, display_height=128, dv_profile=None,
                          dv_compatibility=None, color_transfer="bt709")
        self._preset_explicit = False
        self.choose_source_preset()
        automatic = self.config()
        assert automatic["route"] == "transcode" and not automatic["ai"] and not automatic["interp"]
        assert (automatic["width"], automatic["height"]) == (192, 128)
        assert "自动转为兼容 MP4" in self.automatic_summary.text()
        self.select_data(self.fps, 60)
        assert "保持源尺寸、帧率" not in self.automatic_summary.text(), "manual changes left a misleading automatic summary"
        self.vinfo.update(width=32, height=24, display_width=32, display_height=24)
        self.apply_preset()
        assert not self.start.isEnabled() and "尺寸过小" in self.automatic_summary.text()
        self.select_data(self.resolution, (1920, 1080))
        assert self.start.isEnabled(), "choosing a supported size did not allow export"
        self.select_data(self.export_preset, "enhance")
        self.choose_source_preset()
        assert self.export_preset.currentData() == "enhance", "automatic import replaced an explicit preset"
        self.select_data(self.export_preset, "quality")
        self.vinfo.update(dv_profile=7)
        self.update_automatic_summary()
        assert "不保留原杜比标签" in self.automatic_summary.text()
        # Resume must pass the original persisted config intact, including
        # exact fractional timing and options not represented by a UI control.
        saved_config = {
            "video": "/not-opened/resume-source.mkv", "output": "/not-opened/resume-output.mp4",
            "route": "transcode", "delivery": "general", "width": 2160, "height": 3840,
            "fps_override": "60000/1001", "ai": False, "interp": False,
            "color_mode": "hlg", "audio_mode": "copy", "crf": 17, "batch_size": 16,
            "future_validation_option": "must survive resume",
        }
        resumed = []
        original_begin = self.begin
        original_checkpoint = main.load_checkpoint
        self.begin = lambda cfg: resumed.append(cfg)
        main.load_checkpoint = lambda: {"cfg": saved_config}
        self.resume_previous()
        self.begin = original_begin
        main.load_checkpoint = original_checkpoint
        assert resumed and resumed[0]["resume"] is True
        assert resumed[0]["future_validation_option"] == "must survive resume"
        assert resumed[0]["fps_override"] == "60000/1001" and not resumed[0]["ai"]
        self.save_preferences()
        saved = self.settings_file.read_text(encoding="utf-8")
        assert '"video"' not in saved and '"output"' not in saved and '"rpu"' not in saved
        print("GUI_STARTUP_OK", flush=True)
        self.close()
        main.QApplication.instance().quit()

main.MainWindow = StartupWindow
result = main.main()
preferences.cleanup()
sys.exit(result)
'''
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1")
        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1], env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=20,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(f"GUI startup did not finish within 20 seconds: {exc}")
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        self.assertIn("GUI_STARTUP_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
