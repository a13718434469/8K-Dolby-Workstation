"""Local music bandwidth extension. Retain the source below its measured edge."""
from __future__ import annotations

import math
import shutil
import tempfile
from pathlib import Path

import numpy as np

from hires_audio import _timeline


RATE = 48000
FFT_SIZE = 4096
BLOCK = 96000
CONTEXT = 15360
WET = 0.80


class MusicSRError(ValueError):
    pass


def sr_skip_reason(plan):
    if plan.get("joc") or plan.get("retained_atmos"):
        return "保留已有 Atmos 的对象声和声道"
    if plan.get("eligible"):
        return "保留 Hi-Res 无损母带的采样与精度"
    if plan.get("channels") not in (1, 2):
        return "保留多声道混音；音乐超分支持单声道和立体声"
    if not 8000 <= plan.get("sample_rate", 0) <= RATE:
        return "保留原音轨；当前音乐超分支持 8 至 48 kHz 输入"
    # These sources may use the full available precision even when their rate
    # is too low for the Hi-Res preset. Do not reduce their precision silently.
    if plan.get("action") in ("alac", "pcm") and (plan.get("bits") or 0) > 16:
        return "保留高精度无损音轨"
    return ""


def _read_samples(stream, start, count, channels):
    stream.seek(start * channels * 4)
    data = np.frombuffer(stream.read(count * channels * 4), dtype="<f4")
    if data.size != count * channels or not np.isfinite(data).all():
        raise MusicSRError("读到无效的音频采样")
    return data.reshape(count, channels)


def _spectral_profile(stream, length, channels, cancel):
    """Measure equally spaced excerpts, retaining intermittent bright sections."""
    n = FFT_SIZE
    offsets = np.linspace(0, max(0, length - n), min(160, 1 + length // (n // 2)), dtype=np.int64)
    window = np.hanning(n)
    powers = []
    for start in offsets:
        cancel()
        frames = _read_samples(stream, int(start), min(n, length - start), channels)
        if len(frames) < n:
            frames = np.pad(frames, ((0, n - len(frames)), (0, 0)))
        spectrum = np.fft.rfft(frames * window[:, None], axis=0)
        powers.append(np.mean(np.abs(spectrum) ** 2, axis=1))
    matrix = np.array(powers)
    levels = matrix.sum(axis=1)
    if levels.max() < 1e-3:
        return None
    active = matrix[levels >= levels.max() * 1e-4]
    active /= active.sum(axis=1, keepdims=True)
    # Check both the ordinary spectral envelope and occasional wideband events.
    return 0.6 * active.mean(axis=0) + 0.4 * active.max(axis=0)


def _cutoff(stream, length, channels, source_rate, cancel):
    if length < RATE // 6:
        return None
    power = _spectral_profile(stream, length, channels, cancel)
    if power is None:
        return None
    freq = np.fft.rfftfreq(FFT_SIZE, 1 / RATE)
    total = power.sum()
    def band(lo, hi):
        return power[(freq >= lo) & (freq < hi)].mean()

    # A source with only a bass line or a pure tone does not establish a lost
    # high band. Requiring upper-midrange content avoids inventing a hiss.
    if power[(freq >= 2300) & (freq < 8500)].sum() < total * 2e-5:
        return None
    source_edge = source_rate / 2
    candidates = []
    for f in np.arange(4000, min(16500, source_edge + 250) + 1, 250):
        before = band(f - 1400, f - 450)
        after = band(f + 300, f + 1400)
        if before <= total * 1e-7:
            continue
        # Look beyond a local spectral notch before treating it as a cutoff.
        upper = power[freq >= f + 900]
        if (upper.sum() / total > 0.0015
                or np.percentile(upper, 98) > 0.020 * before):
            continue
        drop = 10 * np.log10((before + 1e-18) / (after + 1e-18))
        if drop >= 24:
            candidates.append((f, drop))
    if candidates:
        # Find the leading edge of the steepest sustained transition, then
        # leave a guard above it before adding any model-generated content.
        strongest = max(d for _, d in candidates)
        f = next(f for f, d in candidates if d >= max(24, min(strongest - 6, 45)))
        return int(min(f + 250, source_edge))
    if source_edge <= 12000:
        return int(source_edge)
    return None


def _lowpass(cutoff, taps=385):
    index = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
    kernel = 2 * cutoff / RATE * np.sinc(2 * cutoff / RATE * index) * np.kaiser(taps, 9.)
    return (kernel / kernel.sum()).astype(np.float32)


def _filter(samples, kernel):
    pad = len(kernel) // 2
    return np.convolve(np.pad(samples, pad, mode="reflect"), kernel, mode="valid")


def _rms(samples, radius):
    power = np.square(np.pad(samples, radius, mode="reflect"), dtype=np.float64)
    energy = np.concatenate(([0.0], np.cumsum(power)))
    return np.sqrt(np.maximum(0.0, energy[2 * radius + 1:] - energy[:-2 * radius - 1])
                   / (2 * radius + 1))


def _restore_sample_grid(predicted, step):
    if step == 3:
        return predicted
    # The neural mapping is always 3x. For severely bandlimited music we
    # apply it on a lower spectral scale and interpolate back to 48 kHz.
    # A zero-phase polyphase-equivalent FIR keeps timing and avoids images.
    stretched = np.zeros(len(predicted) * step, dtype=np.float32)
    stretched[::step] = predicted
    index = np.arange(-16 * step, 16 * step + 1)
    kernel = np.sinc(index / step) * np.kaiser(len(index), 9)
    return _filter(stretched, (step * kernel / kernel.sum()).astype(np.float32))[::3]


def _render(dry, wet, length, channels, cutoff, model, cancel):
    from music_sr_backend import upscale
    step = 3 if cutoff >= 6900 else min(6, max(4, math.ceil(24000 / (cutoff + 150))))
    nyquist = RATE / (2 * step)
    antialias = _lowpass(nyquist - 340)
    highpass = -_lowpass(max(.975 * nyquist, cutoff + 450))
    highpass[len(highpass) // 2] += 1
    peak, residual_energy, source_energy = 0.0, 0.0, 0.0
    for start in range(0, length, BLOCK):
        cancel()
        lo = max(0, start - CONTEXT)
        stop = min(length, start + BLOCK)
        hi = min(length, stop + CONTEXT)
        dry_part = _read_samples(dry, lo, hi - lo, channels)
        i, j = start - lo, stop - lo
        result = dry_part[i:j].copy()
        for ch in range(channels):
            cancel()
            base = dry_part[:, ch]
            if not base.any():
                continue
            narrow = np.ascontiguousarray(_filter(base, antialias)[::step], dtype=np.float32)
            predicted = np.asarray(upscale(model, narrow), dtype=np.float32)
            if predicted.shape != (3 * len(narrow),) or not np.isfinite(predicted).all():
                raise MusicSRError("音频超分模型返回的长度或采样值异常")
            prediction = _restore_sample_grid(predicted, step)[:len(base)]
            # Fade out network biases during pauses. Suppress new content
            # locally wherever the source already has a real high-band event.
            activity = _rms(base, 240)
            gate = np.clip((activity - 3e-5) / 4.5e-4, 0, 1)
            high_ratio = _rms(_filter(base, highpass), 480) / np.maximum(activity, 1e-7)
            gate *= np.clip((.022 - high_ratio) / .018, 0, 1)
            added = _filter(prediction, highpass)[i:j] * (gate[i:j] * WET)
            result[:, ch] += added
            residual_energy += np.sum(added ** 2, dtype=np.float64)
            source_energy += np.sum(base[i:j] ** 2, dtype=np.float64)
        peak = max(peak, float(np.max(np.abs(result))))
        wet.write(result.astype("<f4", copy=False).tobytes())
    ratio = residual_energy / max(source_energy, 1e-20)
    return peak, 10 * math.log10(max(ratio, 1e-20))


def enhance_audio(ffmpeg, source, plan, duration, audio_offset, output, workdir, run, cancel_check):
    """Create an aligned MP4 audio track only when extension is warranted."""
    skip = sr_skip_reason(plan)
    if skip:
        return {"applied": False, "detail": skip}
    channels = plan["channels"]
    length = round(duration * RATE)
    if length < RATE // 6:
        return {"applied": False, "detail": "片段太短，保留原音"}
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    required = length * channels * 10 + 16 * 1024**2
    if shutil.disk_usage(workdir).free < required:
        raise MusicSRError(f"音乐超分的临时音频需要约 {math.ceil(required / 1024**2)} MB 可用空间")
    with tempfile.TemporaryDirectory(prefix="music-sr-", dir=workdir) as directory:
        dry_path, wet_path = Path(directory) / "input.raw", Path(directory) / "output.raw"
        chain = _timeline(plan, duration, audio_offset) + f",aresample={RATE},apad,atrim=end_sample={length}"
        run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", source, "-map", f"0:a:{plan['source_audio_ordinal']}",
             "-vn", "-af", chain, "-c:a", "pcm_f32le", "-f", "f32le", str(dry_path)])
        if dry_path.stat().st_size != length * channels * 4:
            raise MusicSRError("音乐超分输入与预期音画时间轴长度不符")
        with dry_path.open("rb") as dry:
            cutoff = _cutoff(dry, length, channels, plan["sample_rate"], cancel_check)
            if cutoff is None:
                return {"applied": False, "detail": "没有测到需要扩展的频带，保留原音"}
            from music_sr_backend import load_model, MODEL_SHA256
            model = load_model()
            with wet_path.open("wb") as wet:
                peak, relative_db = _render(dry, wet, length, channels, cutoff, model, cancel_check)
        if relative_db < -68:
            return {"applied": False, "detail": "模型推测的高频能量很弱，保留原音", "cutoff_hz": cutoff}
        if relative_db > -3:
            return {"applied": False, "detail": "高频估算超出保守范围，保留原音", "cutoff_hz": cutoff}
        gain = min(1.0, 0.995 / max(peak, 1e-12))
        cancel_check()
        run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-y", "-f", "f32le", "-ar", str(RATE),
             "-ac", str(channels), "-i", str(wet_path), "-af", f"volume={gain:.12g}",
             "-c:a", "alac", "-sample_fmt", "s16p", "-movie_timescale", str(RATE), str(output)])
    layout = "立体声" if channels == 2 else "单声道"
    return {"applied": True, "method": "NovaSR + source-band preservation", "model_sha256": MODEL_SHA256,
            "cutoff_hz": cutoff, "sample_rate": RATE, "channels": channels, "output_codec": "alac", "output_bits": 16,
            "verified_samples": length, "generated_highband_relative_db": round(relative_db, 2),
            "headroom_gain_db": round(20 * math.log10(gain), 3),
            "detail": f"AI 音乐超分为约 {cutoff / 1000:g} kHz 带限音轨补充了高频，输出 48 kHz {layout}重制音轨"}


def verify_sr_output(stream, outcome):
    try:
        from fractions import Fraction
        samples = int(stream["duration_ts"]) * Fraction(stream["time_base"]) * RATE
        ok = (stream.get("codec_name") == "alac" and int(stream["bits_per_raw_sample"]) == 16
              and int(stream["sample_rate"]) == RATE and int(stream["channels"]) == outcome["channels"]
              and abs(float(stream["start_time"])) <= 1 / RATE and samples == outcome["verified_samples"])
    except (ValueError, ZeroDivisionError, KeyError, TypeError):
        ok = False
    if not ok:
        raise MusicSRError("音乐超分成片的规格、声道、长度或起点与处理结果不一致")
    return {"verified_samples": int(samples)}
