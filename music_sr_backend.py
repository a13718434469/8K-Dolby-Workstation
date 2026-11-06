"""CPU inference for the official NovaSR v1 weights; see NOVASR_NOTICE.txt."""
import argparse
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen
import zipfile

MODEL_PATH = Path(__file__).resolve().parent / "tools" / "novasr" / "pytorch_model_v1.bin"
MODEL_REVISION = "27c2a0f8836d80d3e10a3729f690b578a0c49e17"
MODEL_SHA256 = "7ca4d3a650a8e3c80fe0b2e022975c8f57f41b9f1358fc856bcce67fd18d3124"
MODEL_BYTES = 53299
MODEL_URL = f"https://huggingface.co/YatharthS/NovaSR/resolve/{MODEL_REVISION}/pytorch_model_v1.bin"
_SHAPES = ((32,), (32, 1, 7), (32,), (32, 32, 11), (32,), (32, 32, 11),
           (32,), (32,), (12,), (12,), (32,), (32,), (12,), (12,), (1, 32, 7))


def _valid(payload):
    return len(payload) == MODEL_BYTES and hashlib.sha256(payload).hexdigest() == MODEL_SHA256


def model_ready(path=MODEL_PATH):
    try:
        with Path(path).open("rb") as stream:
            return _valid(stream.read(MODEL_BYTES + 1))
    except OSError:
        return False


def install_model(path=MODEL_PATH):
    path = Path(path)
    if model_ready(path):
        return
    with urlopen(MODEL_URL, timeout=60) as response:
        payload = response.read(MODEL_BYTES + 1)
    if not _valid(payload):
        raise ValueError("NovaSR 模型下载校验失败，请重试")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as pending:
        temporary = Path(pending.name)
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_model(path=MODEL_PATH):
    """Read the 15 known numeric storages. Never evaluate data.pkl."""
    import numpy as np
    try:
        with Path(path).open("rb") as stream:
            payload = stream.read(MODEL_BYTES + 1)
    except OSError as exc:
        raise ValueError("缺少音乐超分模型，请运行 music_sr_backend.py 安装") from exc
    if not _valid(payload):
        raise ValueError("音乐超分模型校验失败，请运行 music_sr_backend.py 修复")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return tuple(np.frombuffer(archive.read(f"pytorch_model_v1/data/{i}"), dtype="<f2")
                     .astype(np.float32).reshape(shape) for i, shape in enumerate(_SHAPES))


def _conv(samples, weights, bias=None):
    """Padded 1D cross-correlation with output in (time, channels) order."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    kernel = weights.shape[2]
    padded = np.pad(samples, ((kernel // 2, kernel // 2), (0, 0)))
    windows = sliding_window_view(padded, kernel, axis=0)
    weights = np.ascontiguousarray(weights.reshape(weights.shape[0], -1).T)
    result = np.empty((samples.shape[0], weights.shape[1]), np.float32)
    for start in range(0, len(samples), 2048):
        window = windows[start:start + 2048]
        result[start:start + len(window)] = np.ascontiguousarray(window).reshape(len(window), -1) @ weights
    if bias is not None:
        result += bias
    return result


def _activation(samples, alpha, beta, up, down):
    # Official v1 polyphase 2x upsampler, SnakeBeta activation, 2x decimation.
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    padded = np.pad(samples, ((2, 3), (0, 0)))
    windows = sliding_window_view(padded, 6, axis=0)
    expanded = np.empty((len(samples) * 2, samples.shape[1]), np.float32)
    for phase in (0, 1):
        expanded[phase::2] = np.einsum("tck,k->tc", windows, 2 * up[phase::2])
    expanded = expanded[2:-2]
    expanded += (1 - np.cos(2 * np.exp(alpha) * expanded)) / (2 * np.exp(beta) + 1e-9)
    padded = np.pad(expanded, ((5, 5), (0, 0)))
    windows = sliding_window_view(padded, 12, axis=0)[::2]
    return np.einsum("tck,k->tc", windows, down)


def _forward(model, source):
    import numpy as np
    x = _conv(source[:, None], model[1], model[0])
    # F.interpolate(..., mode="linear", align_corners=False) at exactly 3x.
    y = np.repeat(x, 3, axis=0)
    between = x[1:] - x[:-1]
    y[2:-1:3] += between / 3
    y[3::3] -= between / 3
    z = _activation(y, *model[6:10])
    z = _conv(z, model[3], model[2])
    z = _activation(z, *model[10:14])
    z = _conv(z, model[5], model[4])
    return np.tanh(_conv(z + y[:len(z)], model[14])[:, 0])


def upscale(model, mono_16k_float32):
    """Upscale one mono block to 48 kHz. 64 input samples of context suffice."""
    import numpy as np
    audio = np.asarray(mono_16k_float32, dtype=np.float32)
    if audio.ndim != 1 or not np.isfinite(audio).all():
        raise ValueError("NovaSR 输入须为有限值的单声道 PCM")
    if not len(audio):
        return np.empty(0, dtype=np.float32)
    # Native v1 drops four samples across its two activations. Extra input
    # context lets callers keep the original duration, including the tail.
    padded = np.pad(audio, (64, 64), mode="edge")
    generated = _forward(model, padded)
    return generated[192:192 + 3 * len(audio)].copy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install or verify the official NovaSR v1 music bandwidth-extension model.")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.verify_only:
            install_model()
        if not model_ready():
            raise ValueError("音乐超分模型未安装或校验失败，请运行 music_sr_backend.py 修复")
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("NovaSR v1 ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
