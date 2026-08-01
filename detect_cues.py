# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = [
#   "torch",
#   "transformers==4.42.3",
#   "librosa==0.10.2.post1",
#   "numpy==1.26.4",
#   "scipy",
#   "matplotlib",
#   "pillow",
#   "timm",
# ]
# ///
"""CUE-DETR inference -> cue-fighter JSON.

Adapted from ETH-DISCO/cue-detr (ISMIR'24, CC BY 4.0). Runs on CPU or MPS.

Usage:
    uv run detect_cues.py TRACK [TRACK...] [-s 0.9] [-r 16] [-o outdir]

For each input track, writes <outdir>/<stem>.cues.json:
    {"cues": [{"index": 0, "ms": 32450, "label": "cue"}, ...]}

Pipe straight into the tag writer:
    uv run detect_cues.py track.mp3 -o /tmp/cues
    cue-fighter write track.mp3 --json /tmp/cues/track.cues.json
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import librosa
import numpy as np
import torch
from matplotlib import cm
from PIL import Image
from scipy.signal import find_peaks
from transformers import DetrForObjectDetection, DetrImageProcessor

# CUE-DETR constants (do not change: must match training)
OVERLAP = 0.75
W_WIN = 355
PADDING = 266
SR = 22050


def device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_audio(path: Path) -> np.ndarray:
    """Mono audio at the model's sample rate.

    librosa first (soundfile, then audioread), ffmpeg as the backstop: a real
    library contains FLACs that libsndfile rejects with "unknown error in flac
    decoder" and audioread with MacError -50, which ffmpeg decodes without
    complaint. ~6 of 16 tracks in one sample needed this path.
    """
    try:
        y, _ = librosa.load(path, sr=SR)
        if len(y):
            return y
    except Exception as e:
        print(f"  librosa could not read {path.name} ({e}); falling back to ffmpeg",
              file=sys.stderr)
    p = subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(path), "-ac", "1",
                        "-ar", str(SR), "-f", "f32le", "-"], capture_output=True)
    y = np.frombuffer(p.stdout, "<f4").astype(np.float32)
    if not len(y):
        raise RuntimeError(f"no decodable audio in {path.name}")
    return y


def spectrogram_image(y: np.ndarray) -> np.ndarray:
    M = librosa.feature.melspectrogram(y=y, sr=SR, n_fft=2048)
    M_db = librosa.power_to_db(M, ref=np.max)
    arr = M_db[::-1]
    sm = cm.ScalarMappable(cmap="viridis")
    sm.set_clim(arr.min(), arr.max())
    rgba = np.require(sm.to_rgba(arr, bytes=True), requirements="C")
    im = Image.frombuffer("RGBA", (rgba.shape[1], rgba.shape[0]), rgba, "raw", "RGBA", 0, 1)
    return np.array(im)[:, :, :3]


def sliding_windows(image: np.ndarray):
    image_w = image.shape[1] + PADDING
    n = int(np.floor(image_w / (W_WIN * (1 - OVERLAP))))
    images, borders = [], []
    for i in range(n):
        l = int(np.floor(i * W_WIN * (1 - OVERLAP))) - PADDING
        r = l + W_WIN
        borders.append(l)
        if l < 0:
            seg = np.pad(image[:, :r], ((0, 0), (-l, 0), (0, 0)), mode="linear_ramp")
        elif r > image.shape[1]:
            seg = image[:, l:]
            seg = np.pad(seg, ((0, 0), (0, r - l - seg.shape[1]), (0, 0)), mode="linear_ramp")
        else:
            seg = image[:, l:r]
        images.append(seg)
    return images, borders


def raw_detections(path: Path, model, processor, dev, y: np.ndarray | None = None) -> tuple[list[int], list[float]]:
    """Run the model once. Returns (positions_in_frames, box_scores), unsorted.

    This is the expensive half (DETR forward pass). Sensitivity/radius do not
    affect it, so callers sweeping those can run this once and re-pick cheaply.
    Pass `y` to reuse already-loaded audio instead of decoding the file again.
    """
    if y is None:
        y = load_audio(path)
    image = spectrogram_image(y)
    images, borders = sliding_windows(image)

    encoding = processor.preprocess(images, do_resize=False, return_tensors="pt")
    pixel_values = encoding["pixel_values"].to(dev)
    with torch.no_grad():
        outputs = model(pixel_values)

    predictions = processor.post_process_object_detection(
        outputs, 0, [(128, W_WIN)] * pixel_values.shape[0]
    )
    scores, positions = [], []
    for p, l in zip(predictions, borders):
        scores.extend(p["scores"].tolist())
        pos = (p["boxes"][:, 0] + p["boxes"][:, 2]) // 2 + l
        positions.extend(pos.long().tolist())
    return positions, scores


def peak_pick(positions: list[int], scores: list[float], sensitivity: float, radius: int) -> list[float]:
    """Normalize scores per track, sort by time, peak-pick -> cue times (seconds)."""
    if not positions:
        return []
    s = np.asarray(scores, dtype=float)
    s = (s - s.min()) / (s.max() - s.min()) if s.max() > s.min() else s
    positions, s = zip(*sorted(zip(positions, s)))
    peak_idx, _ = find_peaks(s, height=sensitivity, distance=radius)
    return list(librosa.frames_to_time([positions[i] for i in peak_idx], sr=SR))


def detect(path: Path, model, processor, dev, sensitivity: float, radius: int) -> list[float]:
    positions, scores = raw_detections(path, model, processor, dev)
    return peak_pick(positions, scores, sensitivity, radius)


# Colour per label family, mined from the library's own hand-set cues (each of
# these is used with >=99% consistency there, so auto cues look native in VDJ).
# OUTRO is the exception: the library splits it, so we use blue to keep it
# visually distinct from INTRO's red.
LABEL_COLORS = {
    "INTRO": "CC0000",
    "OUTRO": "0000CC",
    "DROP": "00CC00",
    "BREAK": "CCCC00",
    "BUILD": "00CCCC",
    "CUT": "CC0044",
}

EDGE_INTRO = 0.06   # fraction of track; library INTRO sits at ~0.05
EDGE_OUTRO = 0.94   # library OUTRO sits at ~0.99
WIN_BEATS = 8.0     # two bars of audio compared either side of a cue


def bpm_from_name(name: str) -> float | None:
    """Parse BPM from the library's 'Title (128, C#m)' filename convention."""
    m = re.search(r"\((\d{1,3}),\s*[A-Ga-g][#b]?m?\)", name)
    return (float(m.group(1)) if m else None) or None


def load_downbeats(track: Path, dbdir: Path | None) -> dict | None:
    """Read a madmom sidecar (`<stem>.beats.json` from downbeats.py) if present.

    Returns {"beats", "downbeats", "bpm", "meter"} with times in seconds, or None
    when no sidecar exists for this track — the caller then falls back to librosa.
    madmom's beats align to the reference library's manual cues ~5x tighter than
    librosa's quantized grid (median 4ms vs 22ms), so prefer them when available.
    """
    if dbdir is None:
        return None
    side = dbdir / f"{track.stem}.beats.json"
    if not side.exists():
        return None
    try:
        gj = json.loads(side.read_text())
    except Exception as e:
        print(f"  bad downbeat sidecar for {track.name} ({e}); using librosa", file=sys.stderr)
        return None
    if not gj.get("beats"):
        return None
    return gj


def beat_grid(y: np.ndarray, bpm_hint: float | None = None) -> tuple[float, np.ndarray]:
    """(tempo, detected_beat_times). A filename BPM, when present, seeds the
    tracker. These are librosa's per-beat *estimates* — the interval wobbles."""
    kw = {"start_bpm": bpm_hint} if bpm_hint else {}
    tempo, beats = librosa.beat.beat_track(y=y, sr=SR, units="time", **kw)
    return float(np.atleast_1d(tempo)[0]), np.asarray(beats, dtype=float)


def quantized_grid(y: np.ndarray, bpm: float, dur: float) -> np.ndarray:
    """A rigid constant-tempo grid: `phase + k * (60/bpm)` across the whole track.

    This is the DJ-software model of a beatgrid — every interval is *exactly*
    60/bpm, unlike `beat_grid` whose detected beats drift. The phase (where beat
    one of the grid sits) is fitted to librosa's detected beats via a circular
    mean of their offsets within one beat period, so the rigid grid lines up
    with the actual downbeats instead of starting arbitrarily at t=0.
    """
    period = 60.0 / bpm
    _, beats = librosa.beat.beat_track(y=y, sr=SR, units="time", start_bpm=bpm)
    if len(beats):
        ang = 2 * np.pi * (np.mod(beats, period) / period)
        phase = (np.angle(np.mean(np.exp(1j * ang))) % (2 * np.pi)) / (2 * np.pi) * period
    else:
        phase = 0.0
    k0 = int(np.floor((0.0 - phase) / period))
    k1 = int(np.ceil((dur - phase) / period))
    grid = phase + np.arange(k0, k1 + 1) * period
    return grid[(grid >= -1e-6) & (grid <= dur + 1e-6)]


def bar_starts(y: np.ndarray, beats: np.ndarray, meter: int = 4) -> np.ndarray:
    """Approximate downbeats: of the `meter` possible phases, keep the one whose
    beats carry the most onset energy. librosa has no downbeat tracker, so this
    is a heuristic stand-in for a real one (e.g. Beat This! / madmom)."""
    if len(beats) < meter:
        return beats
    onset = librosa.onset.onset_strength(y=y, sr=SR)
    t_on = librosa.times_like(onset, sr=SR)
    strength = np.interp(beats, t_on, onset)
    phase = max(range(meter), key=lambda p: strength[p::meter].sum())
    return beats[phase::meter]


def snap_times(times: list[float], grid: np.ndarray) -> list[float]:
    """Move each cue onto the nearest grid position (beat or bar)."""
    if grid is None or len(grid) == 0:
        return times
    return [float(grid[int(np.argmin(np.abs(grid - t)))]) for t in times]


def enforce_min_gap(times: list[float], min_gap_s: float) -> list[float]:
    """Drop cues closer than `min_gap_s` to the previously kept one.

    Snapping collapses near-duplicate detections onto the same beat, so this runs
    after it. Keeps the earliest of each cluster (chronological, predictable).
    """
    kept: list[float] = []
    for t in sorted(times):
        if not kept or (t - kept[-1]) >= min_gap_s:
            kept.append(t)
    return kept


def label_cues(y: np.ndarray, times: list[float], bpm: float | None = None) -> list[str]:
    """Assign a structural label to each cue from local energy dynamics.

    CUE-DETR is single-class (it only predicts *where* a cue goes), so labels are
    derived here: position in the track handles INTRO/OUTRO, and the RMS energy
    step across the cue separates DROP (jump up), BREAK (drop out) and BUILD
    (ramping in). Anything else falls back to CUT, the library's generic marker.
    """
    if not times:
        return []
    dur = len(y) / SR
    # compare a musically meaningful span (two bars) rather than a fixed number
    # of seconds, so the same rules behave alike at 82 and 155 BPM
    win = WIN_BEATS * 60.0 / bpm if bpm else 4.0
    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    t_rms = librosa.frames_to_time(np.arange(len(rms)), sr=SR, hop_length=hop)

    def energy(a: float, b: float) -> float:
        m = (t_rms >= max(0.0, a)) & (t_rms < min(dur, b))
        return float(rms[m].mean()) if m.any() else 0.0

    labels = []
    for t in times:
        pos = t / dur if dur else 0.0
        before = energy(t - win, t)
        after = energy(t, t + win)
        earlier = energy(t - 2 * win, t - win)
        ratio = (after + 1e-9) / (before + 1e-9)
        if pos < EDGE_INTRO:
            labels.append("INTRO")
        elif pos > EDGE_OUTRO:
            labels.append("OUTRO")
        elif ratio > 1.30:
            labels.append("DROP")
        elif ratio < 0.75:
            labels.append("BREAK")
        elif before > earlier * 1.15:
            labels.append("BUILD")
        else:
            labels.append("CUT")
    return labels


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tracks", nargs="+", type=Path)
    ap.add_argument("-c", "--checkpoint", default="disco-eth/cue-detr")
    ap.add_argument("-s", "--sensitivity", type=float, default=0.9)
    ap.add_argument("-r", "--radius", type=int, default=16)
    ap.add_argument("-o", "--outdir", type=Path, default=Path("."))
    ap.add_argument("--max-cues", type=int, default=16, help="hot cue slots to fill (VDJ supports 16; Serato DJ shows 8)")
    ap.add_argument("--no-labels", action="store_true", help='emit plain "cue" labels instead of INTRO/DROP/BREAK/...')
    ap.add_argument("--snap", choices=["off", "beat", "bar"], default="beat",
                    help="snap cues onto the beat grid, or bar starts (default: beat). "
                         "bar-snap measured MUCH worse — it drags cues to downbeats, "
                         "but the reference library's cues sit on many beats, not just bar 1")
    ap.add_argument("--grid", choices=["quantized", "detected"], default="quantized",
                    help="quantized = rigid constant-tempo grid (like a DJ beatgrid); "
                         "detected = librosa's per-beat estimates (default: quantized)")
    ap.add_argument("--min-gap-beats", type=float, default=4.0,
                    help="drop cues closer than this many beats (default 4 = 1 bar)")
    ap.add_argument("--downbeats", type=Path, default=None,
                    help="dir of madmom sidecars (<stem>.beats.json from downbeats.py). "
                         "When a track has one, snap to madmom's beat/downbeat grid "
                         "instead of librosa's — aligns ~5x tighter to real beats. "
                         "Falls back to librosa per-track when a sidecar is missing.")
    args = ap.parse_args()

    dev = device()
    print(f"loading {args.checkpoint} on {dev} ...")
    processor = DetrImageProcessor.from_pretrained("facebook/detr-resnet-50")
    model = DetrForObjectDetection.from_pretrained(args.checkpoint).to(dev)

    args.outdir.mkdir(parents=True, exist_ok=True)
    failed = []
    for track in args.tracks:
        try:
            y = load_audio(track)  # loaded once: detection, grid and labelling
        except Exception as e:
            # one unreadable file must never abort a batch — this runs unattended
            print(f"skip {track.name}: {e}", file=sys.stderr)
            failed.append(track.name)
            continue
        bpm_hint = bpm_from_name(track.name)
        db = load_downbeats(track, args.downbeats)
        positions, scores = raw_detections(track, model, processor, dev, y=y)
        # keep every candidate for now — truncate only after snapping/dedupe, so
        # the 16 slots end up holding 16 *distinct* cues
        times = peak_pick(positions, scores, args.sensitivity, args.radius)
        raw_n = len(times)

        # resolve a constant tempo for gap/label windows: filename BPM wins, then
        # madmom's estimate, else librosa's.
        bpm = bpm_hint or (db["bpm"] if db and db.get("bpm") else None) or beat_grid(y)[0]
        grid_src = args.grid
        if args.snap != "off":
            if db is not None:
                # madmom's real beats/downbeats, when a sidecar exists
                grid = np.asarray(db["downbeats"] if args.snap == "bar" else db["beats"], dtype=float)
                grid_src = "madmom"
            elif args.grid == "quantized":
                beats = quantized_grid(y, bpm, len(y) / SR)
                grid = bar_starts(y, beats) if args.snap == "bar" else beats
            else:
                beats = beat_grid(y, bpm)[1]
                grid = bar_starts(y, beats) if args.snap == "bar" else beats
            times = snap_times(times, grid)

        times = enforce_min_gap(times, args.min_gap_beats * 60.0 / bpm)
        dropped = raw_n - len(times)
        times = times[: args.max_cues]
        labels = [] if args.no_labels else label_cues(y, times, bpm)
        cues = []
        for i, t in enumerate(times):
            # clamp to >= 0: window padding can push a detection a hair before t=0,
            # and the Rust writer's ms field is u32 (negative would fail to parse).
            cue = {"index": i, "ms": max(0, int(round(t * 1000)))}
            if labels:
                cue["label"] = labels[i]
                cue["color"] = LABEL_COLORS[labels[i]]
            else:
                cue["label"] = "cue"
            cues.append(cue)
        out = args.outdir / f"{track.stem}.cues.json"
        # `meta` records what produced these cues so reports/regressions are
        # traceable. The Rust reader only looks at "cues" and ignores this.
        meta = {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "checkpoint": args.checkpoint,
            "sensitivity": args.sensitivity,
            "radius": args.radius,
            "snap": args.snap,
            "grid": grid_src,
            "min_gap_beats": args.min_gap_beats,
            "max_cues": args.max_cues,
            "bpm": round(bpm, 2),
            "dropped_too_close": dropped,
            "labelled": not args.no_labels,
        }
        out.write_text(json.dumps({"meta": meta, "cues": cues}, indent=2))
        summary = ", ".join(f"{n}x{l}" for l, n in Counter(labels).most_common()) if labels else "unlabelled"
        print(f"{track.name}: {len(cues)} cues @{bpm:.0f}bpm [{grid_src} {args.snap}-snap, -{dropped} too close] ({summary}) -> {out}")

    if failed:
        print(f"\n{len(failed)} of {len(args.tracks)} track(s) could not be read:",
              file=sys.stderr)
        for name in failed:
            print(f"  {name}", file=sys.stderr)


if __name__ == "__main__":
    main()
