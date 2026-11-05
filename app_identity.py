"""Application identity and a crisp, locally rendered video/audio icon."""
from pathlib import Path
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer


ICON_PATH = Path(__file__).resolve().parent / "assets" / "app.svg"
_icon = None


def app_icon():
    """Supply raster variants for native Windows surfaces, backed by one SVG."""
    global _icon
    if _icon is None:
        renderer = QSvgRenderer(str(ICON_PATH))
        icon = QIcon()
        if renderer.isValid():
            for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
                pixmap = QPixmap(size, size)
                pixmap.fill(Qt.transparent)
                painter = QPainter(pixmap)
                renderer.render(painter)
                painter.end()
                icon.addPixmap(pixmap)
        _icon = icon
    return QIcon(_icon)


def configure_application(app):
    app.setApplicationName("Dolby Workstation")
    app.setOrganizationName("DolbyWorkstationCommunity")
    if sys.platform == "win32":
        # Give this application its own taskbar identity instead of Python's.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DolbyWorkstation.Desktop")
        except (AttributeError, OSError):
            pass
    app.setWindowIcon(app_icon())
