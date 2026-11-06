"""Install the small RNNoise speech model once; processing is entirely local."""
import argparse
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen


MODEL_PATH = Path(__file__).resolve().parent / "tools" / "rnnoise" / "model.rnnn"
MODEL_SHA256 = "70bb6685eb0c2a1d18e2918dca3fbfbd39317010b1802eb1b6ea73a92f3fdec0"
MODEL_BYTES = 297646
MODEL_URL = ("https://raw.githubusercontent.com/GregorR/rnnoise-models/"
             "3eee541a283fd3b8f81b85b1748e3b9ccbefa04d/somnolent-hogwash-2018-09-01/sh.rnnn")


def model_ready(path=MODEL_PATH):
    path = Path(path)
    try:
        return path.stat().st_size == MODEL_BYTES and hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256
    except OSError:
        return False


def install_model(path=MODEL_PATH):
    path = Path(path)
    if model_ready(path):
        return
    with urlopen(MODEL_URL, timeout=45) as response:
        payload = response.read(MODEL_BYTES + 1)
    if len(payload) != MODEL_BYTES or hashlib.sha256(payload).hexdigest() != MODEL_SHA256:
        raise ValueError("Downloaded RNNoise model failed size or SHA-256 verification.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as pending:
        tmp = Path(pending.name)
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.verify_only:
            install_model()
        if not model_ready():
            raise ValueError("Run install_audio_model.py to install or repair the RNNoise model.")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("RNNoise speech model ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
