"""Run the author's published FlashSR ONNX artifact on the local CPU."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.request import urlopen

MODEL_PATH = Path(__file__).resolve().parent / "tools" / "flashsr" / "model.onnx"
MODEL_SHA256 = "e255c76b227f16f7f392cc43677c38bd2c5aa129f042a2ba3eb03fb29e470c7a"
MODEL_BYTES = 498624
MODEL_URL = ("https://huggingface.co/YatharthS/FlashSR/resolve/"
             "3e19cc92e655c2e0661e6268efdade60f42fd0b8/onnx/model.onnx")


def verified_bytes(payload):
    return len(payload) == MODEL_BYTES and hashlib.sha256(payload).hexdigest() == MODEL_SHA256


def model_ready():
    try:
        with MODEL_PATH.open("rb") as stream:
            return verified_bytes(stream.read(MODEL_BYTES + 1))
    except OSError:
        return False


def install():
    if model_ready():
        return
    with urlopen(MODEL_URL, timeout=60) as response:
        data = response.read(MODEL_BYTES + 1)
    if not verified_bytes(data):
        raise ValueError("FlashSR 模型校验失败")
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=MODEL_PATH.parent, delete=False) as f:
        pending = Path(f.name)
    try:
        pending.write_bytes(data)
        os.replace(pending, MODEL_PATH)
    finally:
        pending.unlink(missing_ok=True)


def load_model():
    import onnxruntime as ort
    if not model_ready():
        raise ValueError("音质增强组件不完整，请重新解压完整应用")
    options = ort.SessionOptions()
    options.intra_op_num_threads = min(4, os.cpu_count() or 1)
    return ort.InferenceSession(str(MODEL_PATH), options, providers=["CPUExecutionProvider"])


def upscale(session, mono):
    import numpy as np
    samples = np.ascontiguousarray(mono, dtype=np.float32)
    signal = session.run(["reconstruction"], {"audio_values": samples[None, :]})[0]
    return signal.reshape(-1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    if not parser.parse_args().verify_only:
        install()
    if not model_ready():
        raise SystemExit("FlashSR model missing or damaged")
    print("FlashSR model verified.")
