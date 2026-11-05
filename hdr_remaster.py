"""Conservative, deterministic SDR highlight expansion in linear BT.2020 RGB.

This is an independent creative up-map, not recovery of clipped source detail.
One continuous curve is used for the entire clip to avoid temporal pumping.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

METHOD = "linear-bt2020-hue-preserving-highlight-v1"


def remap_rgb(red, green, blue, peak_nits=600):
    """Map relative linear RGB (1 = 100 nits), maintaining RGB channel ratios."""
    rgb = tuple(max(0.0, float(v)) for v in (red, green, blue))
    luminance = sum(a * b for a, b in zip(rgb, (0.2627, 0.6780, 0.0593)))
    t = min(1.0, max(0.0, (luminance - 0.25) / 0.75))
    gain = 1.0 + (float(peak_nits) / 100.0 - 1.0) * t * t * (3.0 - 2.0 * t)
    maximum = max(rgb)
    if maximum:
        gain = min(gain, float(peak_nits) / (100.0 * maximum))
    return tuple(value * gain for value in rgb)


def write_lut(path, peak_nits=600, cancel_check=lambda: None):
    """Write a deterministic 65-point tetrahedral LUT, atomically."""
    peak = float(peak_nits)
    if not math.isfinite(peak) or not 100 <= peak <= 1000:
        raise ValueError("HDR 高光目标须在 100 到 1000 nits 之间")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial.cube")
    try:
        with partial.open("w", encoding="ascii", newline="\n") as output:
            output.write(f'TITLE "{METHOD}"\nLUT_3D_SIZE 65\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
            for blue in range(65):
                cancel_check()
                for green in range(65):
                    for red in range(65):
                        values = remap_rgb(red / 64, green / 64, blue / 64, peak)
                        output.write("%.9f %.9f %.9f\n" % values)
            output.flush()
            os.fsync(output.fileno())
        cancel_check()
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return {"active": True, "method": METHOD, "peak_nits": peak,
            "input_reference_white_nits": 100, "linear_knee": 0.25,
            "temporal_curve": "constant_for_entire_clip", "lut_size": 65,
            "lut_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "note": "独立保色相高光扩展；不会恢复原片已经丢失的高光细节"}
