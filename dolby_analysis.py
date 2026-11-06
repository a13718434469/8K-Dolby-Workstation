"""Independent Profile 8.4 measurements; not Dolby's proprietary analysis.

The actual installed dovi_tool supplies the polynomial/MMR/DM composer. Native
4:2:0 is composed before chroma upsampling. Pixel processing holds one raw frame
and bounded NumPy tiles. dovi_tool's separate metadata operations can use memory
proportional to the clip length. See DOLBY_ANALYSIS_NOTICE.md.
"""
from __future__ import annotations

import csv
from collections import deque
from contextlib import nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid

import numpy as np

# Bump this identity whenever reconstruction/statistics/quantization changes;
# pipeline caches must compare it as well as base-video and generated-RPU hashes.
METHOD = "independent-p84-spec420-pq-maxrgb-shot-v2"
_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AnalysisError(RuntimeError):
    pass


class AnalysisCancelled(AnalysisError):
    pass


def _cancel(check):
    if check is not None and check():
        raise AnalysisCancelled("Dolby Vision analysis cancelled")


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def _command(args, cancel_check=None):
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen([str(x) for x in args], stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, creationflags=_FLAGS)
        try:
            while process.poll() is None:
                _cancel(cancel_check)
                if os.fstat(output.fileno()).st_size > 2 * 1024 * 1024:
                    raise AnalysisError("Analysis helper output exceeds limit")
                time.sleep(.04)
            _cancel(cancel_check)
            if os.fstat(output.fileno()).st_size > 2 * 1024 * 1024:
                raise AnalysisError("Analysis helper output exceeds limit")
            output.seek(0)
            text = output.read().decode("utf-8", errors="replace")
            if process.returncode:
                raise AnalysisError("Analysis helper failed: " + text[-4000:])
            return text
        finally:
            _stop(process)


def _json(text):
    try:
        return json.loads(text[text.index("{"):])
    except (ValueError, TypeError) as exc:
        raise AnalysisError("Invalid analysis helper JSON") from exc


def _atomic_json(path, value):
    temporary = path.with_name(path.name + ".partial")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path, cancel_check=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            _cancel(cancel_check)
            chunk = source.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def quantize_pq(value):
    return max(0, min(4095, math.floor(float(value) * 4095.0 + .5)))


def clamp_l1(min_pq, max_pq, avg_pq):
    maximum = max(2081, min(4095, int(max_pq)))
    return {"min_pq": max(0, min(12, int(min_pq))), "max_pq": maximum,
            "avg_pq": max(1229, min(maximum - 1, int(avg_pq)))}


def _pq_to_linear(value):
    v = np.clip(value, 0., 1.) ** (1. / (2523. / 32.))
    return (np.maximum(v - 3424. / 4096., 0.) /
            np.maximum(2413. / 128. - 2392. / 128. * v, 1e-15)) ** (1. / (2610. / 16384.))


def _linear_to_pq(value):
    v = np.maximum(value, 0.) ** (2610. / 16384.)
    return ((3424. / 4096. + 2413. / 128. * v) / (1. + 2392. / 128. * v)) ** (2523. / 32.)


def _bt2020_rgb_to_lms():
    # Dolby Vision's fixed BT.2020-referred HPE LMS basis is different from
    # generic IPT's HPE+crosstalk basis. These are mathematical decoder matrix
    # constants (also documented by libplacebo's Dolby Vision decode path).
    lms_to_rgb = np.array([[3.06441879, -2.16597676, .10155818],
                           [-.65612108, 1.78554118, -.12943749],
                           [.01736321, -.04725154, 1.03004253]])
    return np.linalg.inv(lms_to_rgb)


class Profile84Composer:
    """Coefficient-driven polynomial luma and single-piece chroma MMR."""
    def __init__(self, rpu):
        try:
            h, mapping, dm = rpu["header"], rpu["rpu_data_mapping"], rpu["vdr_dm_data"]
            depth = int(h["bl_bit_depth_minus8"]) + 8
            if rpu["dovi_profile"] != 8 or depth != 10 or not h["disable_residual_flag"]:
                raise ValueError("requires single-layer 10-bit Profile 8")
            if h["coefficient_data_type"] != 0 or mapping["mapping_color_space"] != 0:
                raise ValueError("unsupported coefficient/color representation")
            self.scale = float(1 << depth)
            denominator = float(1 << int(h["coefficient_log2_denom"]))
            self.curves = mapping["curves"]
            if len(self.curves) != 3 or self.curves[0]["mapping_idc"] != "Polynomial":
                raise ValueError("expected P8.4 polynomial luma")
            self.pivots = [np.cumsum(c["pivots"], dtype=np.float64) / self.scale for c in self.curves]
            if any(len(p) < 2 or np.any(np.diff(p) <= 0) for p in self.pivots):
                raise ValueError("invalid pivots")
            poly = self.curves[0].get("polynomial", self.curves[0])
            self.poly = [np.asarray(a, dtype=np.float64) + np.asarray(b, dtype=np.float64) / denominator
                         for a, b in zip(poly["poly_coef_int"], poly["poly_coef"])]
            if len(self.poly) != len(self.pivots[0]) - 1 or len(poly["poly_order_minus1"]) != len(self.poly):
                raise ValueError("invalid polynomial layout")
            if any(len(p) != int(o) + 2 for p, o in zip(self.poly, poly["poly_order_minus1"])):
                raise ValueError("invalid polynomial order")
            self.mmr = []
            for c in self.curves[1:]:
                m = c.get("mmr", c)
                if c["mapping_idc"] != "MMR" or len(m["mmr_order_minus1"]) != 1 or len(c["pivots"]) != 2:
                    raise ValueError("expected single-piece chroma MMR")
                coef = np.asarray(m["mmr_coef_int"][0], dtype=np.float64) + np.asarray(m["mmr_coef"][0], dtype=np.float64) / denominator
                if coef.shape != (int(m["mmr_order_minus1"][0]) + 1, 7) or not 1 <= len(coef) <= 3:
                    raise ValueError("invalid MMR terms")
                constant = float(m["mmr_constant_int"][0]) + float(m["mmr_constant"][0]) / denominator
                self.mmr.append((constant, coef))
            self.matrix = np.array([dm[f"ycc_to_rgb_coef{i}"] for i in range(9)], dtype=np.float64).reshape(3, 3) / 8192.
            self.offset = np.array([dm[f"ycc_to_rgb_offset{i}"] for i in range(3)], dtype=np.float64) / (1 << 28)
            rgb_lms = np.array([dm[f"rgb_to_lms_coef{i}"] for i in range(9)], dtype=np.float64).reshape(3, 3) / 16384.
            self.to_bt2020 = np.linalg.inv(_bt2020_rgb_to_lms()) @ rgb_lms
            if dm["signal_eotf"] != 65535 or dm["signal_color_space"] != 0:
                raise ValueError("unsupported output signal")
            self.source_range = [dm["source_min_pq"], dm["source_max_pq"]]
            data = {"header": {k: h[k] for k in ("bl_bit_depth_minus8", "bl_video_full_range_flag", "coefficient_log2_denom", "coefficient_data_type")},
                    "mapping": mapping, "matrix": self.matrix.tolist(), "offset": self.offset.tolist(),
                    "rgb_to_lms": rgb_lms.tolist(), "source_range": self.source_range}
            self.fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        except (KeyError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
            raise AnalysisError("Unsupported P8.4 composer: " + str(exc)) from exc

    def luma(self, y):
        p = self.pivots[0]
        s = np.clip(y.astype(np.float64) / self.scale, p[0], p[-1])
        piece = np.searchsorted(p[1:-1], s, side="right")
        result = np.empty(s.shape, dtype=np.float64)
        for i, coefficients in enumerate(self.poly):
            mask = piece == i
            if np.any(mask):
                result[mask] = np.polynomial.polynomial.polyval(s[mask], coefficients)
        return np.clip(result, 0., 1.)

    def chroma(self, y, cb, cr):
        a, b, c = [np.clip(v.astype(np.float64) / self.scale, p[0], p[-1])
                   for v, p in zip((y, cb, cr), self.pivots)]
        terms = (a, b, c, a * b, a * c, b * c, a * b * c)
        result = []
        for constant, coefficients in self.mmr:
            out = np.full(a.shape, constant, dtype=np.float64)
            for order, row in enumerate(coefficients, 1):
                for coefficient, term in zip(row, terms):
                    if coefficient:
                        out += coefficient * term ** order
            result.append(np.clip(out, 0., 1.))
        return result

    def rgb(self, y, cb_composed, cr_composed):
        yuv = np.stack((self.luma(y), cb_composed, cr_composed), axis=-1)
        pq_native = (yuv - self.offset) @ self.matrix.T
        linear = _pq_to_linear(pq_native) @ self.to_bt2020.T
        return np.clip(_linear_to_pq(linear), 0., 1.)


def downsample_luma_420(y, start, stop):
    """ETSI rounded [1,2,1] per row, then rounded average of two rows."""
    x = np.arange(y.shape[1] // 2) * 2
    left, right = np.maximum(x - 1, 0), np.minimum(x + 1, y.shape[1] - 1)
    rows = np.arange(start, stop) * 2
    def horizontal(r):
        return (y[np.ix_(r, left)].astype(np.uint32) + 2 * y[np.ix_(r, x)].astype(np.uint32)
                + y[np.ix_(r, right)].astype(np.uint32) + 2) >> 2
    return ((horizontal(rows) + horizontal(rows + 1) + 1) >> 1).astype(np.uint16)


def _interpolate_chroma(plane, start, stop, width, siting):
    xs = np.arange(width, dtype=np.float64) / 2.
    ys = np.arange(start, stop, dtype=np.float64) / 2. - (.25 if siting == "left" else 0.)
    x0, y0 = np.floor(xs).astype(int), np.floor(ys).astype(int)
    wx, wy = xs - x0, ys - y0
    x1, y1 = np.clip(x0 + 1, 0, plane.shape[1] - 1), np.clip(y0 + 1, 0, plane.shape[0] - 1)
    x0, y0 = np.clip(x0, 0, plane.shape[1] - 1), np.clip(y0, 0, plane.shape[0] - 1)
    top = plane[np.ix_(y0, x0)] * (1. - wx) + plane[np.ix_(y0, x1)] * wx
    bottom = plane[np.ix_(y1, x0)] * (1. - wx) + plane[np.ix_(y1, x1)] * wx
    return top * (1. - wy[:, None]) + bottom * wy[:, None]


def _reduce_rgb(rgb):
    h, w, _ = rgb.shape
    reduced = rgb.reshape(h // 2, 2, w // 2, 2, 3).mean(axis=(1, 3), dtype=np.float64)
    maximum = reduced.max(axis=-1)
    return float(reduced.min()), float(maximum.max()), float(maximum.sum(dtype=np.float64)), maximum.size


def measure_frame(y, cb, cr, composer, *, chroma_location="left", cancel_check=None, tile_rows=64):
    height, width = y.shape
    if width % 2 or height % 2 or cb.shape != (height // 2, width // 2) or cr.shape != cb.shape:
        raise AnalysisError("Expected even-sized native 4:2:0 planes")
    if chroma_location not in ("left", "topleft"):
        raise AnalysisError("Unsupported HLG chroma location")
    tile_rows = max(2, int(tile_rows) // 2 * 2)
    composed = [np.empty(cb.shape, dtype=np.float64), np.empty(cb.shape, dtype=np.float64)]
    for start in range(0, height // 2, tile_rows):
        _cancel(cancel_check)
        stop = min(height // 2, start + tile_rows)
        result = composer.chroma(downsample_luma_420(y, start, stop), cb[start:stop], cr[start:stop])
        composed[0][start:stop], composed[1][start:stop] = result
    minimum, maximum, total, count = 1., 0., 0., 0
    for start in range(0, height, tile_rows):
        _cancel(cancel_check)
        stop = min(height, start + tile_rows)
        rgb = composer.rgb(y[start:stop], _interpolate_chroma(composed[0], start, stop, width, chroma_location),
                           _interpolate_chroma(composed[1], start, stop, width, chroma_location))
        low, high, subtotal, samples = _reduce_rgb(rgb)
        minimum, maximum, total, count = min(minimum, low), max(maximum, high), total + subtotal, count + samples
    raw = {"min_pq": minimum, "max_pq": maximum, "avg_pq": total / count}
    codes = {k: quantize_pq(v) for k, v in raw.items()}
    return {"measured_pq": raw, "measured_l1": codes,
            "encoded_l1": clamp_l1(codes["min_pq"], codes["max_pq"], codes["avg_pq"])}


class _RawReader:
    def __init__(self, stream):
        self.stream, self.queue, self.stop = stream, queue.Queue(maxsize=2), threading.Event()
        self.pending, self.offset = b"", 0
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while not self.stop.is_set():
                chunk = self.stream.read(256 * 1024)
                while not self.stop.is_set():
                    try:
                        self.queue.put(chunk, timeout=.1)
                        break
                    except queue.Full:
                        pass
                if not chunk:
                    return
        except (OSError, ValueError):
            while not self.stop.is_set():
                try:
                    self.queue.put(b"", timeout=.1)
                    break
                except queue.Full:
                    pass

    def frame(self, size, cancel_check):
        data = bytearray(size)
        view, used = memoryview(data), 0
        while used < size:
            _cancel(cancel_check)
            if self.offset == len(self.pending):
                try:
                    self.pending = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                self.offset = 0
                if not self.pending:
                    if used:
                        raise AnalysisError("Incomplete decoded HLG frame")
                    return None
            amount = min(size - used, len(self.pending) - self.offset)
            view[used:used + amount] = self.pending[self.offset:self.offset + amount]
            self.offset += amount
            used += amount
        return data


def _ffprobe(ffmpeg, ffprobe):
    if ffprobe:
        return str(ffprobe)
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if str(ffmpeg).lower().endswith(".exe") else "ffprobe")
    return str(sibling) if sibling.is_file() else shutil.which("ffprobe") or "ffprobe"


class ShotDetector:
    """Detect persistent image cuts, allowing one or two frames of transient flash."""
    def __init__(self):
        self.history = deque(maxlen=6)
        self.starts = [0]
        self.index = -1
        self.next_cut = 1

    @staticmethod
    def sketch(y, cb, cr):
        # Four samples per cell reduce sensitivity to fine motion. Only cut
        # detection is subsampled; L1 still measures every full-resolution frame.
        def grid(plane):
            h, w = plane.shape
            gh, gw = min(48, h), min(80, w)
            result = np.zeros((gh, gw), dtype=np.float32)
            for dy in (.25, .75):
                rows = ((np.arange(gh) + dy) * h / gh).astype(int)
                for dx in (.25, .75):
                    cols = ((np.arange(gw) + dx) * w / gw).astype(int)
                    result += plane[np.ix_(rows, cols)]
            return result.ravel() / 4092.
        return tuple(grid(p) for p in (y, cb, cr))

    @staticmethod
    def distance(a, b):
        return max(float(np.abs(x - y).mean()) * weight
                   for x, y, weight in zip(a, b, (1., .65, .65)))

    def _assess(self, boundary):
        views = dict(self.history)
        old, new = views[boundary - 1], views[boundary]
        gap = self.distance(old, new)
        if gap < .085:
            return
        if boundary - 2 in views:
            baseline = self.distance(views[boundary - 2], old)
            if gap - baseline < .06:
                return
        threshold = min(.040, gap * .30)
        # A flash followed by a return to the prior shot should not cause
        # display metadata to reset both when it starts and when it ends.
        if any(self.distance(old, views[i]) < threshold for i in (boundary + 1, boundary + 2) if i in views):
            return
        if any(self.distance(new, views[i]) < threshold for i in (boundary - 2, boundary - 3) if i in views):
            return
        self.starts.append(boundary)

    def push(self, sketch):
        self.index += 1
        self.history.append((self.index, sketch))
        if self.next_cut <= self.index - 2:
            self._assess(self.next_cut)
            self.next_cut += 1

    def finish(self):
        while self.next_cut <= self.index:
            self._assess(self.next_cut)
            self.next_cut += 1
        return self.starts.copy()


def write_shot_config(raw_stats, starts, frames, config_path, stats_path=None, cancel_check=None):
    """Pool raw PQ measurements within each shot before L1 quantization/clamping."""
    if frames < 1 or not starts or starts[0] != 0 or starts != sorted(set(starts)) or starts[-1] >= frames:
        raise AnalysisError("Invalid shot boundaries")
    shots = []
    with Path(raw_stats).open(encoding="utf-8") as source:
        for start, end in zip(starts, starts[1:] + [frames]):
            minimum, maximum, total = 1., 0., 0.
            for i in range(start, end):
                _cancel(cancel_check)
                line = source.readline()
                if not line:
                    raise AnalysisError("Incomplete shot measurements")
                row = json.loads(line)
                if row["frame"] != i:
                    raise AnalysisError("Misordered shot measurements")
                pq = row["measured_pq"]
                minimum = min(minimum, pq["min_pq"])
                maximum = max(maximum, pq["max_pq"])
                total += pq["avg_pq"]
            duration = end - start
            level = clamp_l1(quantize_pq(minimum), quantize_pq(maximum), quantize_pq(total / duration))
            shots.append({"start": start, "duration": duration, "metadata_blocks": [{"Level1": level}]})
        if source.readline():
            raise AnalysisError("Unexpected extra shot measurements")
    _atomic_json(Path(config_path), {"cm_version": "V40", "profile": "8.4", "length": frames, "shots": shots})
    digest, changes, last = hashlib.sha256(), 0, None
    with Path(raw_stats).open(encoding="utf-8") as source, (
            Path(stats_path).open("w", encoding="utf-8", newline="\n") if stats_path else nullcontext()) as out:
        for shot in shots:
            level = shot["metadata_blocks"][0]["Level1"]
            packed = struct.pack(">HHH", *[level[k] for k in ("min_pq", "max_pq", "avg_pq")])
            changes += int(last is not None and last != packed)
            last = packed
            for i in range(shot["duration"]):
                _cancel(cancel_check)
                digest.update(packed)
                if out is not None:
                    row = json.loads(source.readline())
                    row.update(shot_start=shot["start"], scene_refresh=(i == 0), encoded_l1=level)
                    out.write(json.dumps(row, separators=(",", ":")) + "\n")
    return {"shot_starts": starts, "shot_count": len(shots), "encoded_l1_changes": changes,
            "encoded_l1_sha256": digest.hexdigest(), "temporal_mapping": "one_L1_per_detected_shot"}


def verify_rpu(dovi_tool, rpu_path, report, cancel_check=None):
    """Verify every L1 and frame count, plus only the first composer's fingerprint.

    ``verified`` applies to these explicitly counted checks, not complete
    semantic verification of all metadata in every RPU. Container muxing may
    reorder extension blocks and change CRCs without changing their meaning.
    """
    rpu_path = Path(rpu_path).resolve()
    with tempfile.TemporaryDirectory(prefix="verify-rpu-", dir=rpu_path.parent) as directory:
        exported = Path(directory) / "l1.csv"
        args = [dovi_tool, "export", "-i", rpu_path, "--levels", "level1=" + str(exported)]
        refresh = Path(directory) / "scenes.txt"
        if "shot_starts" in report:
            args += ["--data", "scenes=" + str(refresh)]
        _command(args, cancel_check)
        if "shot_starts" in report:
            indices = [int(line) for line in refresh.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            if indices != report["shot_starts"]:
                raise AnalysisError("Written RPU scene refresh flags differ from detected cuts")
        digest, count = hashlib.sha256(), 0
        with exported.open(encoding="utf-8", newline="") as source:
            for row in csv.DictReader(source):
                _cancel(cancel_check)
                try:
                    digest.update(struct.pack(">HHH", *(int(row[k]) for k in ("min_pq", "max_pq", "avg_pq"))))
                except (KeyError, ValueError, struct.error) as exc:
                    raise AnalysisError("Invalid exported L1 metadata") from exc
                count += 1
        if count != report["frames"] or digest.hexdigest() != report["encoded_l1_sha256"]:
            raise AnalysisError("Written RPU L1 values or frame count differ from measurements")
        first = _json(_command([dovi_tool, "info", "-i", rpu_path, "-f", "0"], cancel_check))
        fingerprint = Profile84Composer(first).fingerprint
        if fingerprint != report["composer_sha256"]:
            raise AnalysisError("Written RPU changed the measured composer")
        return {"verified": True, "frame_count": count, "l1_sha256": digest.hexdigest(),
                "l1_frames_verified": count, "composer_frames_verified": 1,
                "scene_refresh_flags_verified": "shot_starts" in report,
                "verification_scope": "all_frame_l1_and_first_frame_composer",
                "all_frames_composer_verified": False,
                "composer_sha256": fingerprint, "rpu_sha256": _sha256(rpu_path, cancel_check)}


def analyze_video(ffmpeg, video, workdir, expected_frames, *, dovi_tool, profile="8.4", ffprobe=None,
                  cancel_check=None, progress=None):
    """Return config_path, rpu_path, report_path and JSON-serializable report.

    cancel_check() may return True or raise a caller exception; either terminates
    child processes. progress(percent) is optional. Input is never modified.
    """
    if profile not in ("8.4", "dv84") or not isinstance(expected_frames, int) or expected_frames <= 0:
        raise AnalysisError("P8.4 analysis requires a positive exact frame count")
    _cancel(cancel_check)
    video, root = Path(video).resolve(), Path(workdir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    job = root / ("analysis-" + uuid.uuid4().hex)
    job.mkdir()
    success = False
    try:
        info = _json(_command([_ffprobe(ffmpeg, ffprobe), "-v", "error", "-show_streams", "-of", "json", video], cancel_check))
        stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        keys = ("codec_name", "pix_fmt", "color_transfer", "color_primaries", "color_space")
        if tuple(stream.get(k) for k in keys) != ("hevc", "yuv420p10le", "arib-std-b67", "bt2020", "bt2020nc"):
            raise AnalysisError("Analysis requires final HEVC 4:2:0 10-bit BT.2020 HLG base layer")
        if stream.get("color_range") != "tv":
            raise AnalysisError("Profile 8.4 preset analysis requires a declared limited-range HLG base layer")
        width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
        if not width or not height or width % 2 or height % 2:
            raise AnalysisError("Invalid final HLG dimensions")
        location = stream.get("chroma_location", "left")
        if location not in ("left", "topleft"):
            raise AnalysisError("Unsupported HLG chroma location: " + str(location))
        source_hash = _sha256(video, cancel_check)
        version = _command([dovi_tool, "--version"], cancel_check).strip()
        template_cfg, template_rpu = job / "template.json", job / "template.bin"
        _atomic_json(template_cfg, {"cm_version": "V40", "length": 1})
        _command([dovi_tool, "generate", "-j", template_cfg, "-p", "8.4", "-o", template_rpu], cancel_check)
        template = _json(_command([dovi_tool, "info", "-i", template_rpu, "-f", "0"], cancel_check))
        composer = Profile84Composer(template)
        meter = None
        gpu_frames = 0
        if width * height >= 3840 * 2160 and os.environ.get("DOLBY_FORCE_CPU_ANALYSIS") != "1":
            # Driver availability and FP64 numerical agreement are checked per job.
            try:
                from dolby_opencl import verified_meter
                meter = verified_meter(composer, measure_frame)
            except Exception:
                meter = None
        config_path, stats_path = job / "generator.json", job / "measurements.jsonl"
        config_partial, stats_partial = job / "generator.json.partial", job / "measurements.jsonl.partial"
        raw_stats = job / "raw-measurements.jsonl.partial"
        detector, count = ShotDetector(), 0
        argv = [str(ffmpeg), "-nostdin", "-v", "error", "-xerror", "-noautorotate", "-i", str(video), "-map", "0:v:0",
                "-an", "-sn", "-dn", "-fps_mode", "passthrough", "-pix_fmt", "yuv420p10le", "-f", "rawvideo", "-"]
        with (job / "decode.log").open("wb") as errors:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                       creationflags=_FLAGS, bufsize=0)
            reader = _RawReader(process.stdout)
            try:
                with raw_stats.open("w", encoding="utf-8", newline="\n") as stats:
                    while True:
                        _cancel(cancel_check)
                        frame = reader.frame(width * height * 3, cancel_check)
                        if frame is None:
                            break
                        if count >= expected_frames:
                            raise AnalysisError("Decoded more frames than the final video contract")
                        plane, pixels = np.frombuffer(frame, dtype="<u2"), width * height
                        y = plane[:pixels].reshape(height, width)
                        cb = plane[pixels:pixels + pixels // 4].reshape(height // 2, width // 2)
                        cr = plane[pixels + pixels // 4:].reshape(height // 2, width // 2)
                        detector.push(detector.sketch(y, cb, cr))
                        measured = None
                        if meter is not None:
                            try:
                                measured = meter.stats(y, cb, cr, location)
                            except Exception:
                                # Recover even from a device reset without dropping this frame.
                                meter = None
                        if measured is None:
                            result = measure_frame(y, cb, cr, composer, chroma_location=location, cancel_check=cancel_check)
                        else:
                            gpu_frames += 1
                            codes = {k: quantize_pq(v) for k, v in measured.items()}
                            result = {"measured_pq": measured, "measured_l1": codes, "encoded_l1": clamp_l1(**codes)}
                        stats.write(json.dumps({"frame": count, "measured_pq": result["measured_pq"],
                                                "measured_l1": result["measured_l1"]}, separators=(",", ":")) + "\n")
                        count += 1
                        if progress:
                            progress(90. * count / expected_frames)
                process.wait(timeout=10)
                if process.returncode:
                    raise AnalysisError("Final HLG decoding failed: " + (job / "decode.log").read_text(encoding="utf-8", errors="replace")[-2000:])
            finally:
                reader.stop.set()
                _stop(process)
                process.stdout.close()
                reader.thread.join(timeout=2)
        if count != expected_frames:
            raise AnalysisError(f"Final frame count mismatch: expected {expected_frames}, measured {count}")
        temporal = write_shot_config(raw_stats, detector.finish(), count, config_partial, stats_partial, cancel_check)
        raw_stats.unlink()
        _cancel(cancel_check)
        rpu_partial = job / "measured-rpu.bin.partial"
        _command([dovi_tool, "generate", "-j", config_partial, "-p", "8.4", "-o", rpu_partial], cancel_check)
        report = {"schema_version": 2, "method": METHOD, "analysis_kind": "full_frame_measurement_shot_based_metadata",
                  "dolby_algorithm_equivalent": False, "visual_match_verified": False, "platform_verified": False,
                  "source_sha256": source_hash, "source": str(video), "width": width, "height": height,
                  "input_pixel_format": "yuv420p10le", "input_range": "tv",
                  "arithmetic": ("float64 OpenCL" if gpu_frames == count else
                                 "float64 OpenCL and CPU" if gpu_frames else "float64 tiled CPU"),
                  "gpu_measured_frames": gpu_frames, "cpu_measured_frames": count - gpu_frames,
                  "frames": count, "all_frames_measured": True, "full_resolution": True,
                  "composer_sha256": composer.fingerprint, "composer_source": version + " generate -p 8.4",
                  "composer_input_normalization": "code / 2^BL_bit_depth (10-bit /1024)",
                  "chroma_reconstruction": "native420 MMR before bilinear upsampling; ETSI rounded [1,2,1] luma downsample",
                  "chroma_location": location, "spatial_reduction": "2x2 arithmetic mean of PQ RGB components; explicit implementation choice",
                  "minimum_statistic": "minimum minRGB", "average_statistic": "arithmetic mean of PQ maxRGB, after spatial reduction",
                  "quantization": "clip(floor(pq*4095+0.5),0,4095)", "cm_version": "V40",
                  "source_range_pq_codes": composer.source_range, "measurement_source_range_clamp": False,
                  **temporal,
                  "measurements": str(stats_path),
                  "limitations": ["Independent analysis is not Dolby CM4 proprietary analysis or certification.",
                                  "Cuts are automatically estimated; independent shot pooling is not Dolby's proprietary analysis.",
                                  "Bilinear post-composer chroma interpolation is a declared reconstruction choice.",
                                  "2x2 reduction uses PQ component space, a domain not explicitly fixed in the public text.",
                                  "All canvas pixels are included; letterbox bars are not automatically excluded.",
                                  "CM4 L1 limits can flatten dark-scene statistics; measured and encoded values are recorded."]}
        verification = verify_rpu(dovi_tool, rpu_partial, report, cancel_check)
        report.update(written_l1_verified=True, scene_refresh_flags_verified=verification["scene_refresh_flags_verified"],
                      rpu_sha256=verification["rpu_sha256"],
                      l1_frames_verified=verification["l1_frames_verified"],
                      composer_frames_verified=verification["composer_frames_verified"],
                      verification_scope=verification["verification_scope"],
                      all_frames_composer_verified=False)
        _cancel(cancel_check)
        rpu_path = job / "measured-rpu.bin"
        os.replace(config_partial, config_path)
        os.replace(stats_partial, stats_path)
        os.replace(rpu_partial, rpu_path)
        report["rpu"] = str(rpu_path)
        report_path = job / "analysis-report.json"
        _atomic_json(report_path, report)
        success = True
        if progress:
            progress(100.)
        return {"config_path": config_path, "rpu_path": rpu_path, "report_path": report_path, "report": report}
    finally:
        if not success:
            shutil.rmtree(job, ignore_errors=True)
