# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "numpy>=1.26",
#   "pillow>=10",
# ]
# ///
"""Tag inspector — what is *actually* embedded in a set of audio files, as one
self-contained HTML page.

    uv run inspector.py TRACK|DIR... -o inspect.html
    uv run inspector.py ~/Music/Tracks --sidecar     # one <track>.<ext>.html each

Shows, per file: hot cues (from the file's own Serato tag, via `cue-fighter
read`), star rating, #hashtags, cover art, bpm/key, Mixed In Key's beatgrid /
cuepoints / energy, and the complete raw tag dump. Filter by rating, hashtag or
text; sort by name, score, rating, bpm or cue count.

Distinct from report.py, which judges *detection quality* against manual cues and
therefore imports detect_cues (and with it torch). This tool never loads a model:
numpy, pillow and ffmpeg are the whole dependency set.

Named inspector.py, not inspect.py, because a script directory goes on sys.path
and `inspect` is a stdlib module several dependencies import.
"""

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cuehtml import (DEFAULT_BIN, PLAY_BUTTON, PLAYER_JS, esc, expand_tracks,  # noqa: E402
                     file_url, mmss, read_cues, rel_url)

# ---------------------------------------------------------------- waveform ---
SR = 8000                                            # decode rate for analysis only
BANDS = [(20, 180), (180, 1200), (1200, 4000)]       # bass / body+vocal / air
HUES = np.array([12, 292, 186], np.float32)          # orange-red / violet / cyan
TILT = np.array([1.0, 1.7, 2.4], np.float32)         # offsets natural spectral rolloff
CONTRAST = 4.2                                       # push from the track's own average
SAT = 1.9
PHRASE_BEATS = 16                                    # hue held flat across 4 bars
FALLBACK_BLOCK = 7.0                                 # seconds, when there's no beatgrid
W_PX, H_PX = 1400, 120
BG = np.array([13, 13, 13], np.float32)              # must match --panel in CSS


def hsv_to_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Vectorised HSV -> RGB; h/s/v are 0..1 arrays of length W, result is W x 3."""
    i = np.floor(h * 6).astype(int)
    f = h * 6 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    i %= 6
    return np.stack([np.choose(i, [v, q, p, p, t, v]),
                     np.choose(i, [t, v, v, q, p, p]),
                     np.choose(i, [p, p, t, v, v, q])], 1)


def phrase_edges(beats: list[float], dur: float) -> np.ndarray:
    """Columns where the hue may change — every 4 bars on the embedded beatgrid,
    or fixed blocks for tracks that have none."""
    if len(beats) > PHRASE_BEATS:
        t = np.array(beats[::PHRASE_BEATS], np.float32)
    else:
        t = np.arange(0, max(dur, 1), FALLBACK_BLOCK, dtype=np.float32)
    e = np.unique(np.clip((t / max(dur, 1e-9) * W_PX).astype(int), 0, W_PX))
    return np.unique(np.concatenate([[0], e, [W_PX]]))


def rgb_wave(y: np.ndarray, beats: list[float]) -> str:
    """Waveform whose colour tracks the spectral mix, quantised to 4-bar phrases:
    bass-led warm, mid/vocal violet, air-led cyan. Returned as a data: PNG."""
    idx = np.linspace(0, len(y), W_PX + 1).astype(int)
    n = 1 << (max(int(np.diff(idx).max()), 16) - 1).bit_length()
    freqs = np.fft.rfftfreq(n, 1 / SR)
    masks = [(freqs >= lo) & (freqs < hi) for lo, hi in BANDS]
    win = np.hanning(n)

    amp = np.zeros((W_PX, 3), np.float32)
    peak = np.zeros(W_PX, np.float32)
    for i in range(W_PX):
        seg = y[idx[i]:idx[i + 1]]
        if seg.size == 0:
            continue
        peak[i] = np.abs(seg).max()
        buf = np.zeros(n, np.float32)
        buf[:min(seg.size, n)] = seg[:n]
        mag = np.abs(np.fft.rfft(buf * win))
        for b, m in enumerate(masks):
            amp[i, b] = np.sqrt((mag[m] ** 2).sum()) / n
    amp *= TILT

    # hue from the band mix, held flat per phrase; amplitude stays per-column
    mix = np.empty((W_PX, 3), np.float32)
    for a, b in zip(*(lambda e: (e[:-1], e[1:]))(phrase_edges(beats, len(y) / SR))):
        blk = amp[a:b]
        if not blk.size:
            continue
        w = blk.sum(1, keepdims=True)          # loud columns steer the phrase colour
        m = (blk * w).sum(0) / (w.sum() + 1e-9)
        mix[a:b] = m / (m.sum() + 1e-9)

    # measure each phrase against the track's own average spectrum: raw proportions
    # barely vary within a track and come out uniformly muddy
    base = mix.mean(0, keepdims=True)
    mix = np.clip(base + (mix - base) * CONTRAST, 0, 1)
    mix /= mix.sum(1, keepdims=True) + 1e-9

    # colour as a hue *angle*, not an RGB blend: a band-dominant phrase lands far
    # from the centre and reads neon, a balanced one desaturates to near-grey.
    # Blending RGB directly pushes balanced phrases to pale pink instead.
    ang = np.radians(HUES)
    x, y2 = (mix * np.cos(ang)).sum(1), (mix * np.sin(ang)).sum(1)
    hue = (np.degrees(np.arctan2(y2, x)) % 360) / 360
    sat = np.clip(np.hypot(x, y2) * SAT, 0.12, 1)
    pk = np.clip(peak / (np.percentile(peak, 99) or 1.0), 0, 1)
    val = (0.5 + 0.5 * pk ** 0.6) * (0.5 + 0.5 * sat)   # sat feeds value, so flat
    col = hsv_to_rgb(hue, sat, val) * 255               # phrases dim instead of whiten

    half = H_PX / 2
    h = (pk ** 0.85 * half * 0.98)[:, None]
    rows = np.abs(np.arange(H_PX) - half + 0.5)[None, :]
    cov = np.clip(h - rows + 0.5, 0, 1)                 # antialiased edge
    shade = 1 - 0.3 * np.clip(rows / np.maximum(h, 1), 0, 1) ** 2
    # composited on the panel colour and palette-quantised: a smooth-gradient RGBA
    # PNG is ~7x larger (≈107 KB vs ≈25 KB), which matters across a whole library
    img = BG + (col[:, None, :] - BG) * (cov * shade)[:, :, None]
    im = Image.fromarray(np.clip(img, 0, 255).transpose(1, 0, 2).astype(np.uint8), "RGB")
    im = im.quantize(colors=64, method=Image.MEDIANCUT, dither=Image.Dither.NONE)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# -------------------------------------------------------------- extraction ---
def ffprobe(f: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_format", "-show_streams", str(f)],
                       capture_output=True, text=True)
    return json.loads(r.stdout or "{}")


def b64json(v: str):
    try:
        return json.loads(base64.b64decode(v.replace("\n", "") + "=="))
    except Exception:
        return None


def cover_art(f: Path, idx: int) -> tuple[str, tuple] | tuple[None, None]:
    tmp = Path(f"/tmp/_cuefighter_cover_{os.getpid()}_{idx}.jpg")
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-i", str(f), "-an", "-frames:v", "1",
                    str(tmp)], capture_output=True)
    if not (tmp.exists() and tmp.stat().st_size):
        return None, None
    try:
        im = Image.open(tmp).convert("RGB")
        dims = im.size
        im.thumbnail((320, 320))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=76)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), dims
    finally:
        tmp.unlink(missing_ok=True)


def inspect_track(f: Path, binp: str, idx: int = 0, no_wave: bool = False) -> dict:
    # absolute but NOT resolved — see cuehtml.file_url for why symlinks matter
    d = {"name": f.name, "path": os.path.abspath(f), "size": f.stat().st_size,
         "ext": f.suffix.lower()}
    pr = ffprobe(f)
    fmt = pr.get("format", {})
    tags = dict(fmt.get("tags") or {})
    audio = next((s for s in pr.get("streams", []) if s.get("codec_type") == "audio"), {})
    for s in pr.get("streams", []):                    # FLAC keeps tags on the stream
        if s.get("codec_type") == "audio":
            tags.update({k: v for k, v in (s.get("tags") or {}).items() if k not in tags})
    d["tags"] = tags
    d["audio"] = {k: audio.get(k) for k in
                  ("codec_name", "sample_rate", "channels", "bits_per_raw_sample", "bit_rate")}
    d["dur"] = float(fmt.get("duration") or 0)
    d["container"] = fmt.get("format_long_name") or ""

    for key, out in (("CUEPOINTS", "mik"), ("BEATGRID", "beatgrid"),
                     ("ENERGY", "energy"), ("KEY", "mikkey")):
        v = next((tags[k] for k in tags if k.upper() == key), None)
        if not v:
            continue
        j = b64json(v)
        if j is None:
            d[out + "_err"] = "undecodable"
        elif out == "beatgrid":
            d[out] = {"tempo": j.get("tempo"), "algorithm": j.get("algorithm"),
                      "n_beats": len(j.get("beats", [])), "beats": j.get("beats", [])}
        else:
            d[out] = j

    if any(s.get("codec_type") == "video" for s in pr.get("streams", [])):
        art, dims = cover_art(f, idx)
        if art:
            d["cover"], d["cover_dims"] = art, dims

    d["cues"], d["read_err"] = read_cues(f, binp)

    if not no_wave:
        p = subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(f), "-ac", "1",
                            "-ar", str(SR), "-f", "s16le", "-"], capture_output=True)
        y = np.frombuffer(p.stdout, "<i2").astype(np.float32) / 32768
        if len(y):
            d["dur"] = len(y) / SR
            d["wave"] = rgb_wave(y, d.get("beatgrid", {}).get("beats") or [])
    # the grid is only needed for phrase edges; drop it before it hits the JSON
    if "beatgrid" in d:
        d["beatgrid"].pop("beats", None)
    return d


# -------------------------------------------------------------------- page ---
CSS = r"""
*{box-sizing:border-box}
body{margin:0;background:#0a0a0a;color:#e8e8e8;
 font:13.5px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
h1{font-size:16px;margin:0;font-weight:700;letter-spacing:-.01em}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.tnum{font-variant-numeric:tabular-nums}
.muted{color:#7a7a7a}
.top{position:sticky;top:0;z-index:50;background:#0a0a0aee;backdrop-filter:blur(8px);
 border-bottom:1px solid #232323;padding:11px 18px;display:flex;align-items:center;gap:14px}
.top .note{color:#7a7a7a;font-size:12px}
.top .right{margin-left:auto;display:flex;gap:8px;align-items:center}
.top input{background:#141414;border:1px solid #2a2a2a;color:#e8e8e8;border-radius:6px;
 padding:6px 10px;font:12px inherit;width:230px}
.top input:focus{outline:0;border-color:#4a4a4a}
.page{padding:14px 18px 60px}

.chips{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.chip{font-size:11px;padding:2px 7px;border-radius:4px;background:#181818;border:1px solid #292929;
 color:#a8a8a8;white-space:nowrap}
.chip b{color:#e8e8e8;font-weight:700}
.chip.pads{display:inline-flex;align-items:center;gap:5px;padding:2px 7px 2px 5px;
 font-variant-numeric:tabular-nums;font-weight:700}
.padicon{width:13px;height:13px;flex:none;opacity:.95}
.chip.warn{border-color:#6b3d0d;color:#ffa53d;background:#1e1305}
.chip.ok{border-color:#186a3a;color:#2fe884;background:#08190f}

/* filters */
.filters{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:9px 18px;
 border-bottom:1px solid #1c1c1c;background:#0c0c0c}
.fgrp{display:flex;gap:4px;align-items:center}
.flabel{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#5e5e5e;margin-right:2px}
.rbtn,.tagchip{background:#161616;border:1px solid #2a2a2a;color:#9a9a9a;font:600 11px/1 inherit;
 padding:5px 8px;border-radius:5px;cursor:pointer}
.rbtn:hover,.tagchip:hover{border-color:#4a4a4a;color:#d8d8d8}
.rbtn.on{background:#3a2d05;border-color:#ffd24d;color:#ffd24d}
.tagchip.on{background:#062a20;border-color:#2fe884;color:#2fe884}
.tagchip em{font-style:normal;opacity:.55;margin-left:4px}
.fcount{margin-left:auto;font-size:11.5px;color:#7a7a7a}
.fclear{background:none;border:0;color:#7a7a7a;font:600 11px/1 inherit;cursor:pointer;
 text-decoration:underline}
.fclear:hover{color:#e8e8e8}
select.sort{background:#161616;border:1px solid #2a2a2a;color:#c8c8c8;border-radius:5px;
 padding:5px 7px;font:600 11px/1 inherit;cursor:pointer}

/* score */
.score{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;
 font:700 12px/1 inherit;position:relative;cursor:help;z-index:5;
 background:conic-gradient(var(--sc) calc(var(--p)*1%),#1e1e1e 0);
 /* box-shadow, not a filter: a filter applies to the tooltip child too and makes
    this a stacking context that traps the tooltip behind later rows */
 box-shadow:0 0 7px color-mix(in srgb,var(--sc) 40%,transparent)}
.score:hover{z-index:60}
/* direct child only — a bare `.score i` also matches the bar fills in the tooltip */
.score > i{position:absolute;inset:3px;border-radius:50%;background:#0f0f0f;display:grid;
 place-items:center;font-style:normal;color:var(--sc)}
.score .tip{position:absolute;left:44px;top:-8px;z-index:80;display:none;width:252px;
 background:#171717;border:1px solid #343434;border-radius:8px;padding:10px 12px;
 font:11.5px/1.6 inherit;color:#c8c8c8;box-shadow:0 10px 34px #000e;text-align:left}
.score.up .tip{top:auto;bottom:-8px}
.score:hover .tip{display:block}
.score .tip b{color:#fff}
.bar{position:relative;height:4px;border-radius:2px;background:#242424;margin:3px 0 7px;
 overflow:hidden}
.bar > i{position:static;display:block;height:100%;border-radius:2px;background:var(--sc)}

/* waveform */
.wave{position:relative;width:100%;background:#0d0d0d;border-radius:4px;overflow:hidden;
 cursor:pointer}
.wave img{display:block;width:100%;height:100%}
.wave .grid{position:absolute;inset:0;pointer-events:none;
 background-image:repeating-linear-gradient(90deg,#ffffff12 0 1px,transparent 1px var(--bar))}
.ph{position:absolute;top:0;bottom:0;width:1.5px;background:#fff;opacity:0;pointer-events:none;
 box-shadow:0 0 7px #fff9}
.playing .ph{opacity:.95}
.prog{position:absolute;left:0;top:0;bottom:0;width:0;pointer-events:none;
 background:#ffffff2e;border-right:1px solid #ffffff33}
.cues{position:absolute;inset:0}
.cue{position:absolute;top:0;bottom:0;width:13px;margin-left:-6px;cursor:pointer;z-index:2}
/* hovering must outrank *sibling cues*, not just this cue's own children */
.cue:hover{z-index:40}
.cue .tk{position:absolute;left:6px;top:0;bottom:0;width:2px;background:var(--c);
 box-shadow:0 0 0 1px #000000cc}
.cue .fl{position:absolute;left:6px;top:0;width:7px;height:7px;background:var(--c);
 clip-path:polygon(0 0,100% 0,0 100%)}
.cue:hover .tk{width:3px;box-shadow:0 0 8px var(--c)}
.cue .nm{position:absolute;left:9px;bottom:4px;display:none;white-space:nowrap;
 background:var(--c);color:#0a0a0a;font:700 10px/1.5 inherit;padding:1px 5px;border-radius:3px;
 box-shadow:0 2px 10px #000c}
.cue.r .nm{left:auto;right:9px}
.cue:hover .nm{display:block}

/* rows */
.sheet{border:1px solid #232323;border-radius:9px;background:#101010}
.sheet > .srow:first-child{border-radius:9px 9px 0 0}
.sheet > .srow:last-child{border-radius:0 0 9px 9px;border-bottom:0}
.srow{display:grid;grid-template-columns:68px minmax(180px,232px) 42px 1fr 82px 52px;gap:12px;
 align-items:center;padding:7px 12px;border-bottom:1px solid #1b1b1b;cursor:pointer}
.srow:hover,.srow.open{background:#151515}
.cover{border-radius:4px;display:block;background:#1a1a1a;object-fit:cover}
.nm2{min-width:0}
.nm2 .t{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nm2 .a{color:#8a8a8a;font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stars{letter-spacing:1px;font-size:11.5px;line-height:1;color:#3a3a3a;white-space:nowrap}
.stars b{color:#ffd24d;font-weight:400;text-shadow:0 0 6px #ffd24d66}
.htags{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}
.htag{font-size:10px;padding:1px 5px;border-radius:3px;background:#14201c;border:1px solid #24413a;
 color:#5fd3a6;white-space:nowrap;cursor:pointer}
.htag:hover{border-color:#2fe884;color:#2fe884}
.htag.more{background:#181818;border-color:#2a2a2a;color:#7a7a7a;cursor:default}
.pb{width:44px;height:44px;border-radius:50%;border:1px solid #333;background:#1a1a1a;
 color:#e8e8e8;display:grid;place-items:center;cursor:pointer;justify-self:end;
 transition:background .13s,border-color .13s,transform .13s,box-shadow .13s}
.pb svg{width:15px;height:15px;fill:currentColor;margin-left:2px}
.pb:hover{background:#2b2b2b;border-color:#585858;transform:scale(1.06)}
.pb:active{transform:scale(.96)}
.playing .pb{background:#e8e8e8;color:#0a0a0a;border-color:#e8e8e8;box-shadow:0 0 0 4px #ffffff14}
.playing .pb svg{margin-left:0}
.right{text-align:right;font-size:11px;color:#8a8a8a;line-height:1.45}
.right .tm{color:#c8c8c8;font-size:11.5px}
.dead .tm{color:#ff4d6a}
.dead .pb{opacity:.45}
.empty{padding:26px;text-align:center;color:#6a6a6a;font-size:12.5px}

/* expanded detail */
.expand{padding:2px 12px 18px 92px;border-bottom:1px solid #232323;background:#0d0d0d}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:14px}
@media(max-width:980px){.cols{grid-template-columns:1fr}}
.grp{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#6a6a6a;
 margin:14px 0 6px;padding-bottom:3px;border-bottom:1px solid #232323}
table.ct{border-collapse:collapse;width:100%;font-size:12px}
table.ct th{text-align:left;color:#6a6a6a;font-weight:600;font-size:10px;text-transform:uppercase;
 letter-spacing:.05em;padding:4px 8px;border-bottom:1px solid #232323}
table.ct td{padding:3px 8px;border-bottom:1px solid #1a1a1a}
table.ct tr[data-t]{cursor:pointer}
table.ct tr:hover td{background:#181818}
.sw{display:inline-block;width:9px;height:9px;border-radius:2px;vertical-align:-1px;margin-right:5px}
.md{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;font-size:12px;align-items:baseline}
.md dt{color:#6a6a6a;font-family:ui-monospace,Menlo,monospace;font-size:11px}
.md dd{margin:0;overflow-wrap:anywhere}
.md dd.b64{color:#5c5c5c;cursor:pointer}
.md dd.b64:hover{color:#a8a8a8}
.err{color:#ff4d6a;font-size:11.5px}
audio{display:none}

/* sidecar: single track, always expanded */
.sidecar .srow{cursor:default}
.sidecar .expand{padding-left:12px}
"""

APP_JS = r"""
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const mmss = s => Math.floor(Math.abs(s)/60) + ':' + String(Math.floor(Math.abs(s)%60)).padStart(2,'0');
const mmssms = ms => mmss(ms/1000) + '.' + String(Math.abs(Math.round(ms))%1000).padStart(3,'0');
const kb = n => n > 1048576 ? (n/1048576).toFixed(1)+' MB' : (n/1024).toFixed(0)+' KB';
const tag = (t,k) => { const kk = Object.keys(t.tags).find(x => x.toUpperCase() === k); return kk ? t.tags[kk] : ''; };
const title = t => tag(t,'TITLE') || t.name.replace(/\.[^.]+$/,'');
const artist = t => tag(t,'ARTIST') || tag(t,'ALBUMARTIST') || '—';
const bpm = t => tag(t,'BPM') || (t.beatgrid ? t.beatgrid.tempo.toFixed(0) : '') || (t.name.match(/\((\d{2,3}),/)||[])[1] || '?';
const keyOf = t => tag(t,'INITIALKEY') || (t.mikkey && t.mikkey.key) || (t.name.match(/,\s*([A-G][#b]?m?)\)/)||[])[1] || '?';
// library convention is 0/20/40/60/80/100; POPM-style 0-255 also normalises
const stars = t => { const v = parseFloat(tag(t,'RATING')); if (!v) return 0;
  return Math.min(5, Math.round(v > 100 ? v/51 : v/20)); };
const hashes = t => (tag(t,'GROUPING').match(/#[^\s#]+/g) || []);

const PAD_ICON = `<svg class="padicon" viewBox="0 0 16 16" aria-hidden="true">${
  [2.4,8,13.6].flatMap(y => [2.4,8,13.6].map(x =>
    `<rect x="${x-2}" y="${y-2}" width="4" height="4" rx="1" fill="currentColor"/>`)).join('')}</svg>`;

/* ----------------------------------------------------------------- score */
const VOCAB = ['INTRO','OUTRO','DROP','BREAK','BUILD','CUT','MIX IN','MIX OUT','VOCAL'];
const CORE = ['TITLE','ARTIST','ALBUM','DATE','GENRE','BPM','INITIALKEY','LABEL','ENERGYLEVEL'];
function score(t) {
  const n = t.cues.length;
  const cues = Math.min(n,16)/16*40;
  const named = t.cues.filter(c => c.label && VOCAB.some(v => c.label.toUpperCase().startsWith(v)));
  const labels = n ? named.length/n*25 : 0;
  const have = CORE.filter(k => tag(t,k)).length;
  const tags = (have/CORE.length*20) + (t.cover ? 5 : 0);
  const an = (t.beatgrid?5:0) + (tag(t,'SERATO_BEATGRID')?3:0) + (tag(t,'SERATO_AUTOGAIN')?2:0);
  const total = Math.round(cues+labels+tags+an);
  const col = total>=85?'#2fe884':total>=65?'#ffe14d':total>=45?'#ff9f2e':'#ff4d6a';
  return {total, col, parts:[['cue coverage',cues,40,`${n}/16 pads used`],
    ['label discipline',labels,25,n?`${named.length}/${n} match ${VOCAB.slice(0,5).join('/')}…`:'no cues'],
    ['tagging',tags,25,`${have}/${CORE.length} core fields${t.cover?' + art':', no art'}`],
    ['analysis data',an,10,[t.beatgrid?'grid':null,tag(t,'SERATO_BEATGRID')?'serato grid':null,
      tag(t,'SERATO_AUTOGAIN')?'gain':null].filter(Boolean).join(', ')||'none']]};
}
function scoreEl(t, up) {
  const s = score(t);
  return `<div class="score ${up?'up':''}" style="--sc:${s.col};--p:${s.total}"><i>${s.total}</i>
    <div class="tip"><b>${s.total}/100</b> — ${esc(title(t))}
      ${s.parts.map(([k,v,max,note]) => `<div style="margin-top:6px">${k}
        <span style="float:right">${v.toFixed(0)}/${max}</span>
        <div class="bar" style="--sc:${s.col}"><i style="width:${v/max*100}%"></i></div>
        <span class="muted" style="font-size:10.5px">${esc(note)}</span></div>`).join('')}
    </div></div>`;
}

/* -------------------------------------------------------------- waveform */
function gridOverlay(t) {
  if (!t.beatgrid || !t.dur) return '';
  const bar4 = 16 * 60 / t.beatgrid.tempo;
  return `<div class="grid" style="--bar:${(bar4/t.dur*100).toFixed(4)}%"></div>`;
}
function cueMarks(t) {
  return t.cues.slice().sort((a,b)=>a.ms-b.ms).map(c => {
    const pct = 100*c.ms/1000/t.dur;
    return `<div class="cue ${pct>82?'r':''}" data-t="${(c.ms/1000).toFixed(3)}"
      style="left:${pct.toFixed(3)}%;--c:#${c.color||'888888'}">
      <span class="tk"></span><span class="fl"></span>
      <span class="nm">${c.index+1} ${esc(c.label||'cue')} · ${mmss(c.ms/1000)}</span></div>`;
  }).join('');
}
function waveBlock(t, h) {
  return `<div class="wave" style="height:${h}px">${
    t.wave ? `<img src="${t.wave}" alt="">` : ''}${gridOverlay(t)}<div class="prog"></div>
    <div class="cues">${cueMarks(t)}</div><div class="ph"></div></div>`;
}

/* ---------------------------------------------------------------- detail */
const GROUPS = [
  ['identity', ['TITLE','ARTIST','ALBUMARTIST','ALBUM','SUBTITLE','VERSION','DATE','GENRE','LABEL',
                'PUBLISHER','ISRC','CATALOGNUMBER','TRACKNUMBER','TOTALTRACKS','COMPOSER','GROUPING','COMMENT','ENCODER']],
  ['analysis', ['BPM','KEY','INITIALKEY','ENERGY','ENERGYLEVEL','RATING','COLOR','LENGTH','BEATGRID','CUEPOINTS']],
  ['serato',   ['SERATO_MARKERS_V2','SERATO_MARKERS2','SERATO_BEATGRID','SERATO_AUTOGAIN','SERATO_OVERVIEW','SERATO_ANALYSIS']],
];
function metaBlocks(t) {
  const keys = Object.keys(t.tags), used = new Set();
  const rows = ks => ks.map(k => {
    const real = keys.find(x => x.toUpperCase() === k); if (!real) return '';
    used.add(real); const v = t.tags[real] || '';
    const big = v.length > 90;
    return `<dt>${esc(real.toLowerCase())}</dt><dd class="${big?'b64 mono':''}" ${
      big?`data-full="${esc(v)}"`:''}>${big ? `⟨base64 · ${v.length} chars⟩`
      : esc(v) || '<span class="muted">empty</span>'}</dd>`;
  }).join('');
  let html = '';
  for (const [name, ks] of GROUPS) { const r = rows(ks);
    if (r) html += `<div class="grp">${name}</div><dl class="md">${r}</dl>`; }
  const rest = keys.filter(k => !used.has(k));
  if (rest.length) html += `<div class="grp">other (${rest.length})</div><dl class="md">${
    rows(rest.map(k=>k.toUpperCase()))}</dl>`;
  html += `<div class="grp">decoded</div><dl class="md">
    <dt>rating</dt><dd>${stars(t) ? `<span class="stars">${'<b>★</b>'.repeat(stars(t))}${
      '★'.repeat(5-stars(t))}</span> &nbsp;<span class="muted">(${esc(tag(t,'RATING'))})</span>`
      : '<span class="muted">unrated</span>'}</dd>
    <dt>hashtags</dt><dd>${hashes(t).length ? `<span class="htags" style="display:inline-flex">${
      hashes(t).map(h => `<span class="htag" data-tag="${esc(h)}">${esc(h)}</span>`).join('')}</span>`
      : '<span class="muted">none</span>'}</dd>
    <dt>beatgrid</dt><dd>${t.beatgrid ? `MIK algo ${t.beatgrid.algorithm} · ${
      t.beatgrid.tempo.toFixed(3)} bpm · ${t.beatgrid.n_beats} beats`
      : '<span class="muted">none</span>'}</dd>
    <dt>mik cues</dt><dd>${t.mik && t.mik.cues && t.mik.cues.length
      ? t.mik.cues.map(c=>`${esc(c.name||'?')}@${mmss(c.time/1000)}`).join(', ')
      : '<span class="muted">none</span>'}</dd>
    <dt>energy</dt><dd>${t.energy ? `level ${t.energy.energyLevel} (algo ${t.energy.algorithm})`
      : '<span class="muted">none</span>'}</dd>
    <dt>audio</dt><dd>${esc(t.audio.codec_name)} · ${t.audio.sample_rate} Hz · ${
      t.audio.channels} ch${t.audio.bits_per_raw_sample?' · '+t.audio.bits_per_raw_sample+' bit':''} · ${kb(t.size)}</dd>
    <dt>path</dt><dd class="mono" style="font-size:11px">${esc(t.path)}</dd>
    ${t.read_err ? `<dt>tag error</dt><dd class="err">${esc(t.read_err)}</dd>` : ''}</dl>`;
  return html;
}
function cueTable(t) {
  const beat = 60/(t.beatgrid ? t.beatgrid.tempo : (parseFloat(bpm(t))||120));
  const rows = t.cues.slice().sort((a,b)=>a.index-b.index).map(c => `<tr data-t="${(c.ms/1000).toFixed(3)}">
      <td class="tnum">${c.index+1}</td><td class="tnum mono">${mmssms(c.ms)}</td>
      <td class="tnum muted">${(c.ms/1000/beat/4+1).toFixed(2)}</td>
      <td><span class="sw" style="background:#${c.color||'888'}"></span><span class="mono muted">${esc(c.color||'—')}</span></td>
      <td>${esc(c.label||'')}</td></tr>`).join('');
  // Mixed In Key stores cue times in MILLISECONDS (its 54890.37 lines up with the
  // Serato drop at 54.881s), unlike the seconds it uses in BEATGRID
  const mik = (t.mik && t.mik.cues ? t.mik.cues : []).map(c => `<tr data-t="${(c.time/1000).toFixed(3)}">
      <td class="muted">·</td><td class="tnum mono muted">${mmssms(c.time)}</td><td></td>
      <td class="muted">MIK</td><td class="muted">${esc(c.name||'')}</td></tr>`).join('');
  return `<table class="ct"><tr><th>pad</th><th>time</th><th>bar</th><th>colour</th><th>label</th></tr>
    ${rows||'<tr><td colspan=5 class="muted">no serato cues</td></tr>'}${mik}</table>`;
}

/* --------------------------------------------------------------- filters */
const F = {q:'', minStars:0, tags:new Set(), sort:'name'};
const TAGS = (() => {
  const n = new Map();
  DATA.forEach(t => hashes(t).forEach(h => n.set(h, (n.get(h)||0)+1)));
  return [...n].sort((a,b) => b[1]-a[1] || a[0].localeCompare(b[0]));
})();
const SORTS = {name:'name', score:'score', rating:'rating', bpm:'bpm', cues:'cue count'};
function matches(t) {
  if (stars(t) < F.minStars) return false;
  const h = hashes(t);                  // multiple tags narrow (AND)
  if (![...F.tags].every(x => h.includes(x))) return false;
  const q = F.q.toLowerCase();
  return !q || (t.name+' '+artist(t)+' '+tag(t,'GROUPING')).toLowerCase().includes(q);
}
function ordered(rows) {
  const key = {name: r => title(r.t).toLowerCase(), score: r => -score(r.t).total,
               rating: r => -stars(r.t), bpm: r => parseFloat(bpm(r.t))||0,
               cues: r => -r.t.cues.length}[F.sort];
  return rows.slice().sort((a,b) => { const x = key(a), y = key(b);
    return typeof x === 'string' ? x.localeCompare(y) : x - y; });
}
function drawFilters(shown) {
  const el = document.getElementById('filters');
  if (!el) return;
  el.innerHTML = `
    <div class="fgrp"><span class="flabel">rating</span>
      ${[0,3,4,5].map(n => `<button class="rbtn ${F.minStars===n?'on':''}" data-stars="${n}">${
        n ? '★'.repeat(n) + (n<5?'+':'') : 'any'}</button>`).join('')}</div>
    ${TAGS.length ? `<div class="fgrp"><span class="flabel">tags</span>
      ${TAGS.map(([h,n]) => `<button class="tagchip ${F.tags.has(h)?'on':''}" data-tag="${esc(h)}">${
        esc(h)}<em>${n}</em></button>`).join('')}</div>` : ''}
    <div class="fgrp"><span class="flabel">sort</span>
      <select class="sort" id="sortsel">${Object.entries(SORTS).map(([k,v]) =>
        `<option value="${k}" ${F.sort===k?'selected':''}>${v}</option>`).join('')}</select></div>
    <span class="fcount">${shown} of ${DATA.length}${
      (F.minStars||F.tags.size||F.q) ? ' · <button class="fclear" id="fclear">clear</button>' : ''}</span>`;
}

/* ------------------------------------------------------------------ view */
const open = new Set(MODE.sidecar ? DATA.map((_,i) => i) : []);
function render() {
  const rows = ordered(DATA.map((t,i) => ({t,i})).filter(r => matches(r.t)));
  const sub = document.getElementById('sub');
  if (sub && !MODE.sidecar) {
    const med = [...DATA].map(score).map(s=>s.total).sort((a,b)=>a-b)[DATA.length>>1];
    sub.textContent = `${DATA.length} file${DATA.length>1?'s':''} · median score ${med}`;
  }
  drawFilters(rows.length);
  document.getElementById('page').innerHTML = (rows.length ? rows.map(({t,i}) => `
    <div class="srow ${open.has(i)?'open':''}" data-row="${i}" data-player data-dur="${t.dur}">
      ${t.cover?`<img class="cover" src="${t.cover}" width="68" height="68" alt="">`
               :'<div class="cover" style="width:68px;height:68px"></div>'}
      <div class="nm2"><div class="t">${esc(title(t))}</div><div class="a">${esc(artist(t))}</div>
        <div class="chips" style="margin-top:4px">
          <span class="stars" title="${stars(t) ? stars(t)+' / 5' : 'unrated'}">${
            '<b>★</b>'.repeat(stars(t))}${'★'.repeat(5-stars(t))}</span>
          <span class="chip">${bpm(t)} bpm</span><span class="chip">${esc(keyOf(t))}</span>
          <span class="chip pads ${t.cues.length>=8?'ok':'warn'}"
            title="${t.cues.length} of 16 hot cue pads used">${PAD_ICON}${t.cues.length}</span></div>
        ${hashes(t).length ? `<div class="htags">${hashes(t).slice(0,3).map(h =>
          `<span class="htag" data-tag="${esc(h)}">${esc(h)}</span>`).join('')}${
          hashes(t).length>3?`<span class="htag more">+${hashes(t).length-3}</span>`:''}</div>` : ''}</div>
      ${scoreEl(t, !MODE.sidecar && i >= DATA.length - 2)}
      ${waveBlock(t, 62)}
      <div class="right"><span class="tm tnum">${mmss(t.dur)}</span><br>
        ${Object.keys(t.tags).length} tags${t.mik && t.mik.cues && t.mik.cues.length
          ? '<br>'+t.mik.cues.length+' MIK' : ''}</div>
      ${PLAY_BUTTON}
      <audio preload="none" src="${t.src}"></audio>
    </div>
    ${open.has(i)?`<div class="expand">
      <div class="cols"><div><div class="grp">cue points</div>${cueTable(t)}</div>
      <div>${metaBlocks(t)}</div></div></div>`:''}`).join('')
    : '<div class="empty">no files match these filters</div>');
  wire();
}
function wire() {
  initPlayers(document);
  document.querySelectorAll('.srow').forEach(row => {
    const i = +row.dataset.row, exp = row.nextElementSibling;
    if (exp && exp.classList.contains('expand'))
      exp.querySelectorAll('[data-t]').forEach(el => el.onclick = () => row.seekTo(+el.dataset.t));
    if (MODE.sidecar) return;
    row.onclick = e => { if (e.target.closest('.wave,.pb,.score,.htag')) return;
      open.has(i) ? open.delete(i) : open.add(i); render(); };
  });
  document.querySelectorAll('dd.b64').forEach(d => d.onclick = e => { e.stopPropagation();
    d.textContent = d.dataset.full; d.style.maxHeight='140px'; d.style.overflow='auto';
    d.classList.remove('b64'); });
  const toggleTag = h => { F.tags.has(h) ? F.tags.delete(h) : F.tags.add(h); render(); };
  document.querySelectorAll('.htag[data-tag]').forEach(el => el.onclick = e => {
    e.stopPropagation(); if (!MODE.sidecar) toggleTag(el.dataset.tag); });
  document.querySelectorAll('.tagchip').forEach(b => b.onclick = () => toggleTag(b.dataset.tag));
  document.querySelectorAll('.rbtn').forEach(b => b.onclick = () => {
    F.minStars = +b.dataset.stars; render(); });
  const sel = document.getElementById('sortsel');
  if (sel) sel.onchange = () => { F.sort = sel.value; render(); };
  const clr = document.getElementById('fclear');
  if (clr) clr.onclick = () => { F.q=''; F.minStars=0; F.tags.clear();
    const fb = document.getElementById('fb'); if (fb) fb.value=''; render(); };
  const fb = document.getElementById('fb');
  // bind once — rebinding on every render would drop focus mid-keystroke
  if (fb && !fb.dataset.bound) { fb.dataset.bound = '1';
    fb.oninput = () => { F.q = fb.value; render(); fb.focus(); }; }
}
render();
"""


def page(docs: list[dict], sidecar: bool, stamp: str) -> str:
    head = ('<div class="top"><h1>cue-fighter · tag inspector</h1>'
            f'<span class="note" id="sub">{esc(docs[0]["name"]) if sidecar else ""}</span>'
            + ('' if sidecar else '<div class="right"><input id="fb" placeholder="filter…"></div>')
            + '</div>' + ('' if sidecar else '<div class="filters" id="filters"></div>'))
    body = f'<div class="page"><div class="sheet{" sidecar" if sidecar else ""}" id="page"></div>'
    body += f'<div class="muted" style="font-size:11px;margin-top:10px">{esc(stamp)}</div></div>'
    return ("<!doctype html><meta charset=utf-8>"
            f"<title>{esc(docs[0]['name'] if sidecar else 'cue-fighter tag inspector')}</title>"
            f"<style>{CSS}</style>{head}{body}<script>"
            f"const DATA={json.dumps(docs)};"
            f"const MODE={json.dumps({'sidecar': sidecar})};"
            f"const PLAY_BUTTON={json.dumps(PLAY_BUTTON)};"
            f"{PLAYER_JS}{APP_JS}</script>")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tracks", nargs="*", type=Path, help="audio files or directories")
    ap.add_argument("--list", help="file of newline-separated track paths")
    ap.add_argument("-o", "--out", type=Path, default=Path("inspect.html"))
    ap.add_argument("--sidecar", action="store_true",
                    help="write <track>.<ext>.html beside each file instead of one page")
    ap.add_argument("--bin", default=DEFAULT_BIN, help="path to the cue-fighter binary")
    ap.add_argument("--no-wave", action="store_true", help="skip waveforms (much faster)")
    ap.add_argument("-j", "--jobs", type=int, default=min(8, (os.cpu_count() or 4)))
    ap.add_argument("-q", "--quiet", action="store_true", help="no per-file progress")
    args = ap.parse_args()

    tracks = expand_tracks(args.tracks, args.list)
    if not tracks:
        raise SystemExit("no tracks given (paths, a directory, or --list)")

    n = len(tracks)
    width = len(str(n))
    started = time.monotonic()
    lock = threading.Lock()
    state = {"done": 0, "cues": 0, "nocues": 0}
    if not args.quiet:
        print(f"inspecting {n} file{'s' if n > 1 else ''} "
              f"({args.jobs} workers{', no waveforms' if args.no_wave else ''})", file=sys.stderr)

    def note(t: Path, d: dict | None, err: str = "") -> None:
        # printed from the worker so progress appears as files finish, not in
        # submission order — a slow first file would otherwise stall the display
        with lock:
            state["done"] += 1
            if args.quiet:
                return
            head = f"[{state['done']:>{width}}/{n}]"
            if d is None:
                print(f"{head} {t.name[:52]:<52} FAILED  {err}", file=sys.stderr)
                return
            state["cues"] += len(d["cues"])
            state["nocues"] += 0 if d["cues"] else 1
            flags = []
            if not d["cues"]:
                flags.append("no cues")
            if not d.get("beatgrid"):
                flags.append("no grid")
            if d.get("read_err"):
                flags.append("tag unreadable")
            print(f"{head} {d['name'][:52]:<52} {mmss(d['dur']):>6}  "
                  f"{len(d['cues']):>2} cues  {len(d['tags']):>2} tags"
                  f"{'  · ' + ', '.join(flags) if flags else ''}", file=sys.stderr)

    def work(pair):
        i, t = pair
        try:
            d = inspect_track(t, args.bin, i, args.no_wave)
        except Exception as e:                      # one bad file must not kill a batch
            note(t, None, str(e))
            return None
        note(t, d)
        return d

    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        docs = [d for d in ex.map(work, enumerate(tracks)) if d]
    if not docs:
        raise SystemExit("nothing could be read")
    if not args.quiet:
        print(f"\nread {len(docs)}/{n} in {time.monotonic()-started:.1f}s · "
              f"{state['cues']} cues total · {state['nocues']} file(s) with none",
              file=sys.stderr)

    rev = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent),
                          "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip() or "untracked"
    stamp = f'generated {datetime.now().isoformat(timespec="seconds")} · cue-fighter @ {rev}'

    if args.sidecar:
        total = 0
        for d in docs:
            # relative src, so a track and its sidecar survive being moved together
            d["src"] = rel_url(Path(d["path"]))
            out = Path(d["path"] + ".html")
            out.write_text(page([d], True, stamp))
            total += out.stat().st_size
            print(f"{out.name}  ({out.stat().st_size/1024:.0f} KB)")
        print(f"\n{len(docs)} sidecar(s), {total/1024/1024:.1f} MB total", file=sys.stderr)
    else:
        for d in docs:
            d["src"] = file_url(Path(d["path"]))
        args.out.write_text(page(docs, False, stamp))
        print(f"wrote {args.out}  ({args.out.stat().st_size/1024:.0f} KB, {len(docs)} tracks)")
        print("audio is linked, not embedded — open the page from a real browser so "
              "file:// audio can load.", file=sys.stderr)


if __name__ == "__main__":
    main()
