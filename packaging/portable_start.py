"""The double-click entry point in the Windows portable distribution."""
import ctypes
import os
from pathlib import Path
import runpy
import sys
import traceback


def main():
    app = Path(__file__).resolve().parent
    script = app / "consumer_app.py"
    sys.argv[0] = str(script)
    # A non-console CPython executable otherwise exposes stdout/stderr as None.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    try:
        runpy.run_path(str(script), run_name="__main__")
    except Exception:
        detail = traceback.format_exc()
        try:
            folder = Path(os.environ["LOCALAPPDATA"]) / "BiliVideoStudio"
            folder.mkdir(parents=True, exist_ok=True)
            output = folder / "startup-error.txt"
            output.write_text(detail, encoding="utf-8")
            detail = f"启动记录已保存到 {output}"
        except OSError:
            pass
        ctypes.windll.user32.MessageBoxW(None, "程序启动失败。\n" + detail, "B站投稿助手", 16)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
