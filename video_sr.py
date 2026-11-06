"""Stream SDR frames through the official general Real-ESRGAN x4plus network."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading

from consumer_picture import cadence_filter


MODEL = "realesrgan-x4plus"
WEIGHTS = {
    "realesrgan-x4plus.bin": "713ee713b0353afaa27976f0563a64a5043bd70b9bd8936c2e26e25ebcdbcddf",
    "realesrgan-x4plus.param": "35330ececcea33b6c397a72548e788d5d53becee4734c50b7fada36e89f10a86",
}
WIN_ENGINE_HASH = "07e49f7cbb4ede01ae4dd4c399d3a7e5846e3d2085c3128eff881e55cb7b1a0c"
PNG = b"\x89PNG\r\n\x1a\n"


class InferenceUnavailable(RuntimeError):
    """The official model or runtime could not produce usable frames."""


def sha256_file(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def installed_engine(tools):
    binary = tools / ("realesrgan-ncnn-vulkan.exe" if os.name == "nt" else "realesrgan-ncnn-vulkan")
    models = tools / "models"
    try:
        if not binary.is_file():
            raise InferenceUnavailable("未安装 Real-ESRGAN 推理程序")
        if os.name == "nt" and sha256_file(binary) != WIN_ENGINE_HASH:
            raise InferenceUnavailable("Real-ESRGAN 运行程序校验失败")
        if any(sha256_file(models / name) != checksum for name, checksum in WEIGHTS.items()):
            raise InferenceUnavailable("Real-ESRGAN 模型校验失败")
    except OSError as exc:
        raise InferenceUnavailable("Real-ESRGAN 模型文件缺失或无法读取") from exc
    return binary, models


def source_filter(info, plan, rate):
    """Convert the selected SDR frames to full-range sRGB for the trained model."""
    stream = info["stream"]
    pri = stream.get("color_primaries")
    pri = pri if pri in ("bt709", "smpte170m", "bt470bg", "smpte432", "smpte431") else "bt709"
    trc = stream.get("color_transfer")
    trc = trc if trc in ("bt709", "iec61966-2-1", "smpte170m") else "bt709"
    matrix = stream.get("color_space")
    matrix = matrix if matrix in ("bt709", "smpte170m", "bt470bg") else "bt709"
    rng = "full" if stream.get("color_range") == "pc" or stream.get("pix_fmt", "").startswith("yuvj") else "limited"
    return ("setpts=PTS-STARTPTS," + cadence_filter(plan, rate, info["duration"]) +
            f"zscale=pin={pri}:tin={trc}:min={matrix}:rin={rng}:p=bt709:t=iec61966-2-1:m=gbr:r=full:npl=100,"
            "format=gbrp,format=rgb24")


def write_png_frame(stream, path, size):
    """Split FFmpeg's image2pipe stream without interpreting or changing its pixels."""
    sign = stream.read(8)
    if not sign:
        return None
    if sign != PNG:
        raise ValueError("解码器输出了无效的 PNG 帧")
    sha = hashlib.sha256(sign)
    limit = size[0] * size[1] * 4 + 65536
    with path.open("wb") as out:
        out.write(sign)
        while True:
            head = stream.read(8)
            if len(head) != 8:
                raise ValueError("视频解码帧提前结束")
            length, kind = struct.unpack(">I4s", head)
            if out.tell() + length + 12 > limit:
                raise ValueError("视频解码帧超出预期范围")
            body = stream.read(length + 4)
            if len(body) != length + 4:
                raise ValueError("视频解码帧不完整")
            if kind == b"IHDR" and struct.unpack(">II", body[:8]) != tuple(size):
                raise ValueError("视频解码后的尺寸与源方向不一致")
            out.write(head)
            out.write(body)
            sha.update(head)
            sha.update(body)
            if kind == b"IEND":
                return sha.digest()


def _check_sr_frame(path, size):
    try:
        with path.open("rb") as stream:
            header = stream.read(24)
        if header[:8] == PNG and header[12:16] == b"IHDR" and struct.unpack(">II", header[16:24]) == size:
            return
    except OSError:
        pass
    raise InferenceUnavailable("模型输出缺失或尺寸与 4 倍推理不符")


def _failure(path, name):
    with path.open("rb") as err:
        err.seek(max(0, err.seek(0, 2) - 1500))
        tail = err.read().decode("utf-8", "replace")
    return RuntimeError(name + "失败：" + tail)


@contextmanager
def _codec(cmd, directory, name, job, reading):
    log = directory / (name + ".log")
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with log.open("wb") as err:
        process = subprocess.Popen(list(map(str, cmd)), cwd=job.job_dir, stderr=err,
                                   stdin=subprocess.DEVNULL if reading else subprocess.PIPE,
                                   stdout=subprocess.PIPE if reading else subprocess.DEVNULL, **hidden)
        # A cancelled job must also break blocking pipe reads/writes.
        finished = threading.Event()
        def watch():
            while not finished.wait(.2):
                if job._cancel.is_set():
                    if process.poll() is None:
                        process.terminate()
                    return
        guard = threading.Thread(target=watch, daemon=True)
        guard.start()
        pipe = process.stdout if reading else process.stdin
        try:
            yield pipe
            pipe.close()
            if process.wait():
                raise _failure(log, name)
        except (BrokenPipeError, OSError):
            job._boundary()
            if process.poll() not in (None, 0):
                raise _failure(log, name)
            raise
        finally:
            finished.set()
            guard.join(timeout=1)
            if process.poll() is None:
                process.kill()
                process.wait()
            try:
                pipe.close()
            except BrokenPipeError:
                pass


def render(job, ffmpeg, tools, encoder_options):
    """Bound disk use to one batch; keep only one continuous HLG encoder timeline."""
    from pipeline import PipelineError, PipelineCancelled
    binary, models = installed_engine(tools)
    plan = job.cfg["picture_plan"]
    size = tuple(plan["source_size"])
    scaled = tuple(4 * dim for dim in size)
    # Limit transient PNGs to ~512 MiB at the incompressible RGB bound.
    worst_frame = 4 * (size[0] * size[1] + scaled[0] * scaled[1]) + 131072
    batch = min(16, max(1, (512 * 1024**2) // worst_frame))
    if shutil.disk_usage(job.job_dir).free < worst_frame * batch + 32 * 1024**2:
        raise InferenceUnavailable("临时空间不足以处理一批 AI 画面")
    decoder = [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", job.cfg["video"],
               "-map", f"0:{job.info['stream']['index']}", "-an", "-sn", "-dn", "-vf",
               source_filter(job.info, plan, job.rate), "-fps_mode", "passthrough", "-c:v", "png",
               "-compression_level", "1", "-threads", "1", "-f", "image2pipe", "pipe:1"]
    encoder = [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-y", "-framerate", str(job.rate),
               "-f", "image2pipe", "-c:v", "png", "-i", "pipe:0", "-map", "0:v:0",
               "-vf", job._native_filters(sr_rgb=True), *encoder_options]
    processed = inferred = 0
    estimated = max(1, round(job.info["duration"] * job.rate))
    with tempfile.TemporaryDirectory(prefix="sr-", dir=job.job_dir) as tmp:
        folder = Path(tmp)
        inputs, outputs = folder / "input", folder / "output"
        inputs.mkdir()
        outputs.mkdir()
        last_key, last_image = None, folder / "last.png"
        with _codec(decoder, folder, "视频解码", job, True) as frames, _codec(encoder, folder, "视频编码", job, False) as sink:
            ended = False
            while not ended:
                files = {}
                order = []
                for i in range(batch):
                    job._boundary()
                    path = inputs / f"{i:04}.png"
                    key = write_png_frame(frames, path, size)
                    if key is None:
                        ended = True
                        break
                    if key in files or key == last_key:
                        path.unlink()
                    else:
                        files[key] = outputs / path.name
                    order.append(key)
                if files:
                    try:
                        job._run([binary, "-i", inputs, "-o", outputs, "-m", models, "-n", MODEL,
                                  "-s", "4", "-t", "256", "-j", "1:1:1", "-f", "png"])
                    except PipelineCancelled:
                        raise
                    except (OSError, PipelineError) as exc:
                        raise InferenceUnavailable("本机 Vulkan 引擎未能完成 4 倍模型推理") from exc
                    for image in files.values():
                        _check_sr_frame(image, scaled)
                for key in order:
                    job._boundary()
                    image = last_image if key == last_key else files[key]
                    with image.open("rb") as src:
                        shutil.copyfileobj(src, sink, 1024 * 1024)
                if order and order[-1] != last_key:
                    shutil.copyfile(files[order[-1]], last_image)
                    last_key = order[-1]
                processed += len(order)
                inferred += len(files)
                job.progress(min(68, 5 + 63 * processed / estimated))
                for path in (*inputs.glob("*.png"), *outputs.glob("*.png")):
                    path.unlink()
    if not processed or not inferred:
        raise InferenceUnavailable("模型没有生成有效画面")
    return {"applied": True, "model": MODEL, "model_scale": 4, "inferred_frames": inferred,
            "encoded_frames": processed, "source_size": list(size), "model_output_size": list(scaled),
            "output_size": plan["output_size"], "weights_sha256": WEIGHTS[MODEL + ".bin"],
            "model_input": "full-range sRGB", "cadence_before_inference": plan["method"],
            "reused_frames": processed - inferred,
            "detail": f"Real-ESRGAN 通用模型实际完成 4 倍超分至 {scaled[0]}×{scaled[1]}，"
                      f"输出画布 {plan['output_size'][0]}×{plan['output_size'][1]}"}
