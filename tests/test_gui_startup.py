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
import main
from PyQt5.QtCore import QTimer

# Reproduce Qt's broken automatic prefix without relying on a particular
# username or the CI runner having a non-ASCII installation directory.
main.QApplication.setLibraryPaths(["/nonexistent/dolby-startup-plugin-path"])
main.load_checkpoint = lambda: None
OriginalWindow = main.MainWindow

class StartupWindow(OriginalWindow):
    def __init__(self):
        super().__init__()
        QTimer.singleShot(100, self.verify_and_quit)

    def verify_and_quit(self):
        assert self.isVisible(), "main() did not show its window"
        assert self.start.isEnabled(), "new-task control is not ready"
        assert self.select_data(self.resolution, (1920, 1080)), "resolution lookup failed"
        assert tuple(self.resolution.currentData()) == (1920, 1080), "wrong restored resolution"
        assert not self.grab().isNull(), "window could not render"
        print("GUI_STARTUP_OK", flush=True)
        self.close()
        main.QApplication.instance().quit()

main.MainWindow = StartupWindow
sys.exit(main.main())
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
