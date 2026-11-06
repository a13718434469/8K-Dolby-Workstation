"""Build an isolated Windows x64 distribution from the project's build venv.

Run with CPython 3.12 x64 after installing requirements and onnxruntime 1.23.2.
The chosen FFmpeg folder must contain its matching release README and LICENSE.
No Python, compiler, GPU runtime or internet connection is needed by the user.
"""
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen
import zipfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NAME = "B站投稿助手"
EMBED = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
EMBED_SHA = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
DOVI_LICENSE = "https://raw.githubusercontent.com/quietvoid/dovi_tool/2.3.4/LICENSE"
DOVI_SHA = "8cd1262b4edac2df219448d3450ff8e810c775e8025cdbe5e031d8f5efd55db8"
MODEL_SHA = "e255c76b227f16f7f392cc43677c38bd2c5aa129f042a2ba3eb03fb29e470c7a"
ESRGAN_RELEASE = "https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0"
ESRGAN_FILES = {
    "realesrgan-ncnn-vulkan.exe": {"07e49f7cbb4ede01ae4dd4c399d3a7e5846e3d2085c3128eff881e55cb7b1a0c"},
    "models/realesrgan-x4plus.bin": {"713ee713b0353afaa27976f0563a64a5043bd70b9bd8936c2e26e25ebcdbcddf"},
    "models/realesrgan-x4plus.param": {"35330ececcea33b6c397a72548e788d5d53becee4734c50b7fada36e89f10a86"},
    # Both supported upstream Windows bundles ship the Microsoft OpenMP runtime.
    "vcomp140.dll": {"8f72ef2e483465444b2059fc6744d6cb22cd8d8a27f6fa56befd2a42dcd0f78b",
                     "54fe6b087528b33c2969143d811eb62f1bd49071d37de9db0745fc079764d698"},
}
APP_FILES = (
    "consumer_app.py", "consumer_audio.py", "consumer_picture.py", "pipeline.py", "quality.py", "app_identity.py", "cli.py", "dolby_analysis.py",
    "hdr_remaster.py", "hires_audio.py", "atmos_cloud.py", "atmos_encoding.py", "flashsr_official.py",
    "ai_audio.py", "install_audio_model.py", "music_sr.py", "music_sr_backend.py", "dolby_opencl.py",
    "video_sr.py", "assets/app.svg", "LICENSE", "README.md", "DOLBY_ANALYSIS_NOTICE.md", "NOVASR_NOTICE.txt", "FLASHSR_NOTICE.txt", "REAL_ESRGAN_NOTICE.txt",
    "main.py", "ATMOS_SETUP.md", "requirements.txt", "run.bat", "setup.bat", "setup.ps1",
)
PACKAGES = (
    "numpy", "onnxruntime", "boto3", "botocore", "s3transfer", "python-dateutil", "jmespath", "six", "urllib3",
    "PyQt5", "PyQt5-Qt5", "PyQt5-sip", "coloredlogs", "humanfriendly", "flatbuffers", "packaging",
    "protobuf", "sympy", "mpmath", "pyopencl", "pytools", "platformdirs", "siphash24", "typing_extensions",
)
QT_MODULES = {"__init__.py", "QtCore.pyd", "QtGui.pyd", "QtWidgets.pyd", "QtSvg.pyd", "sip.pyd"}
QT_DLLS = {x + ".dll" for x in (
    "Qt5Core", "Qt5Gui", "Qt5Widgets", "Qt5Svg", "Qt5PrintSupport", "libEGL", "libGLESv2",
    "d3dcompiler_47", "opengl32sw", "msvcp140", "msvcp140_1", "msvcp140_2", "vcruntime140", "vcruntime140_1",
)}


def sha(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def fetch(cache, filename, url, checksum):
    output = cache / filename
    if output.is_file() and sha(output) == checksum:
        return output
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
        staging = Path(stream.name)
    try:
        with urlopen(url, timeout=180) as response, staging.open("wb") as file:
            shutil.copyfileobj(response, file)
        if sha(staging) != checksum:
            raise ValueError(f"Checksum mismatch: {filename}")
        os.replace(staging, output)
    finally:
        staging.unlink(missing_ok=True)
    return output


def include_part(relative):
    parts = relative.parts
    if ".." in parts or relative.is_absolute() or {"__pycache__", "tests", "test"} & set(parts):
        return False
    if relative.name in ("direct_url.json", "RECORD", "INSTALLER", "REQUESTED") or relative.suffix in (".pyc", ".pdb"):
        return False
    if parts[0] == "PyQt5":
        if len(parts) == 2:
            return parts[1] in QT_MODULES or (parts[1].startswith("sip.") and relative.suffix == ".pyd")
        if parts[:3] == ("PyQt5", "Qt5", "bin"):
            return relative.name in QT_DLLS
        if parts[:3] == ("PyQt5", "Qt5", "plugins"):
            return (parts[3] in ("imageformats", "iconengines", "styles")
                    or relative.name in ("qwindows.dll", "qoffscreen.dll", "qminimal.dll"))
        if parts[:3] == ("PyQt5", "Qt5", "translations"):
            return relative.name in ("qt_zh_CN.qm", "qtbase_zh_CN.qm")
        return False
    if parts[0] == "onnxruntime" and len(parts) >= 2:
        return len(parts) == 2 or parts[1] in ("capi", "backend")
    return True


def copy_package(name, target):
    dist = metadata.distribution(name)
    if not dist.files:
        raise RuntimeError(f"Missing installed wheel inventory: {name}")
    for item in dist.files:
        relative = Path(str(item))
        if include_part(relative):
            copy(dist.locate_file(item), target / relative)
    return dist.version


def populate_runtime(out, cache, ffbin):
    runtime = out / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(fetch(cache, "python-3.12.10-embed-amd64.zip", EMBED, EMBED_SHA)) as archive:
        archive.extractall(runtime)
    # _pth disables registry, user site, PYTHONPATH and system Python discovery.
    (runtime / "python312._pth").write_text("python312.zip\n.\nsite-packages\n../app\n", encoding="ascii")
    versions = {name: copy_package(name, runtime / "site-packages") for name in PACKAGES}
    # ORT can be imported before Qt. Keep the MSVC runtime shipped by the Qt
    # wheel in the executable search directory as well, even on a clean PC.
    qt_bin = runtime / "site-packages" / "PyQt5" / "Qt5" / "bin"
    for item in qt_bin.glob("msvcp140*.dll"):
        copy(item, runtime / item.name)
    # The pinned Qt5 wheel carries an older VC++ standard library than ORT
    # requires. Use the compatible runtime distributed in the NumPy wheel.
    bundled_msvcp, = (runtime / "site-packages" / "numpy.libs").glob("msvcp140-*.dll")
    copy(bundled_msvcp, runtime / "msvcp140.dll")
    for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        copy(runtime / name, qt_bin / name)
    for name in ("pyreadline3", "tzdata"):
        try:
            versions[name] = copy_package(name, runtime / "site-packages")
        except metadata.PackageNotFoundError:
            pass
    if versions["onnxruntime"] != "1.23.2":
        raise RuntimeError("Validate the ONNX runtime version before changing the portable build.")
    lic = out / "licenses"
    copy(runtime / "LICENSE.txt", lic / "Python.txt")
    ffroot = ffbin.parent
    copy(ffroot / "LICENSE", lic / "FFmpeg-GPLv3.txt")
    copy(ffroot / "README.txt", lic / "FFmpeg-build-source.txt")
    copy(ffroot / "LICENSE", lic / "PyQt5-GPLv3.txt")
    copy(fetch(cache, "Qt-LGPL-3.0.txt", "https://www.gnu.org/licenses/lgpl-3.0.txt",
               "e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118"), lic / "Qt-LGPLv3.txt")
    copy(fetch(cache, "dovi-LICENSE", DOVI_LICENSE,
               "e9d76c149b1176de4aa0202cfb6a4a266a0c4cb71959f429d670c3a857f95e52"), lic / "dovi_tool-MIT.txt")
    tool_dir = out / "app" / "tools"
    for tool in ("ffmpeg.exe", "ffprobe.exe"):
        copy(ffbin / tool, tool_dir / tool)
    dovi = ROOT / "tools" / "dovi_tool.exe"
    if sha(dovi) != DOVI_SHA:
        raise RuntimeError("Use the verified official dovi_tool 2.3.4 Windows x64 binary for this build.")
    copy(dovi, tool_dir / "dovi_tool.exe")
    sources = {
        "Python": {"version": "3.12.10", "binary": EMBED, "sha256": EMBED_SHA,
                   "source": "https://www.python.org/ftp/python/3.12.10/Python-3.12.10.tar.xz"},
        "ffmpeg": {"source": (ffroot / "README.txt").read_text(encoding="utf-8").split("Source Code: ")[1].splitlines()[0],
                   "build": "https://www.gyan.dev/ffmpeg/builds/", "sha256": sha(tool_dir / "ffmpeg.exe"),
                   "ffprobe_sha256": sha(tool_dir / "ffprobe.exe")},
        "dovi_tool": {"source": "https://github.com/quietvoid/dovi_tool/tree/2.3.4", "sha256": DOVI_SHA},
        "Qt": {"source": "https://download.qt.io/archive/qt/5.15/5.15.2/single/"},
        "Python packages": {n: f"https://pypi.org/project/{n}/{v}/#files" for n, v in versions.items()},
        "FlashSR": {"source": "https://huggingface.co/YatharthS/FlashSR", "sha256": MODEL_SHA},
    }
    (lic / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")


def install_app(out, icon, extra):
    app = out / "app"
    for name in APP_FILES:
        copy(ROOT / name, app / name)
    for name in extra:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or (path.suffix not in (".py", ".md", ".svg", ".txt") and path.name != "LICENSE"):
            raise ValueError(f"Unexpected release input: {name}")
        copy(ROOT / path, app / path)
    model = ROOT / "tools" / "flashsr" / "model.onnx"
    if sha(model) != MODEL_SHA:
        raise RuntimeError("Official FlashSR ONNX weights failed verification")
    copy(model, app / "tools" / "flashsr" / "model.onnx")
    images = {}
    for name, allowed in ESRGAN_FILES.items():
        src = ROOT / "tools" / name
        digest = sha(src)
        if digest not in allowed:
            raise RuntimeError(f"Real-ESRGAN distribution file failed verification: {name}")
        copy(src, app / "tools" / name)
        images[name] = digest
    sources_file = out / "licenses" / "sources.json"
    sources = json.loads(sources_file.read_text(encoding="utf-8"))
    sources["Real-ESRGAN"] = {"release": ESRGAN_RELEASE, "model": "realesrgan-x4plus", "files": images}
    sources_file.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    copy(ROOT / "REAL_ESRGAN_NOTICE.txt", out / "licenses" / "Real-ESRGAN.txt")
    copy(HERE / "portable_start.py", app / "portable_start.py")
    for name in ("Launcher.cs", "portable_start.py", "build_portable.py", "windows-requirements.txt", "BUILD.md"):
        copy(HERE / name, app / "packaging" / name)
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    cmd = [str(csc), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/utf8output",
           "/reference:System.Windows.Forms.dll", "/out:" + str(out / (NAME + ".exe"))]
    if icon:
        cmd.append("/win32icon:" + str(icon.resolve()))
    subprocess.run([*cmd, str(HERE / "Launcher.cs")], check=True)
    (out / "使用说明.txt").write_text(
        "完整解压后双击“B站投稿助手.exe”。拖入视频即开始；完成后点击“打开所在文件夹”，只需投稿成片 MP4。\n"
        "app 和 runtime 是应用的一部分，请与启动程序放在同一文件夹。"
        "程序源码在 app 文件夹，第三方来源和许可证在 licenses 及 runtime/site-packages 中。\n",
        encoding="utf-8-sig")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory for the portable application")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--ffmpeg-bin", type=Path, required=True)
    parser.add_argument("--icon", type=Path)
    parser.add_argument("--extra-source", action="append", default=[])
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--reuse-runtime", action="store_true")
    parser.add_argument("--zip", type=Path)
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
        parser.error("Use the Windows CPython 3.12 x64 build environment.")
    out, cache = args.output.resolve(), args.cache.resolve()
    if out == ROOT or ROOT in out.parents or cache.is_relative_to(out) or (args.zip and args.zip.resolve().is_relative_to(out)):
        parser.error("Runtime, cache, ZIP and project must use separate paths.")
    marker = out / "licenses" / "sources.json"
    if args.reuse_runtime and not marker.is_file():
        parser.error("--reuse-runtime needs a completed build directory from this script")
    if not args.reuse_runtime and out.exists() and any(out.iterdir()):
        parser.error("Use a new empty output directory.")
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    if not args.reuse_runtime:
        populate_runtime(out, cache, args.ffmpeg_bin.resolve())
    if not args.prepare_only:
        install_app(out, args.icon, args.extra_source)
        if args.zip:
            if args.zip.exists():
                parser.error("ZIP already exists")
            with tempfile.NamedTemporaryFile(dir=args.zip.resolve().parent, delete=False) as pending:
                temp_zip = Path(pending.name)
            try:
                with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                    for file in sorted(out.rglob("*")):
                        if file.is_file():
                            archive.write(file, Path(NAME) / file.relative_to(out))
                os.rename(temp_zip, args.zip)
            finally:
                temp_zip.unlink(missing_ok=True)
            print(f"{args.zip}  SHA256 {sha(args.zip)}")
    print(out)


if __name__ == "__main__":
    main()
