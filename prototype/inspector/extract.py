"""Pull real data out of tracks into one JSON blob for the design mockup."""
import base64, json, os, subprocess, sys, io
from pathlib import Path
import numpy as np
from PIL import Image

BIN = "./target/release/cue-fighter"


def b64json(v):
    try:
        return json.loads(base64.b64decode(v + "=="))
    except Exception:
        return None


def probe(f):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_format", "-show_streams", str(f)],
                       capture_output=True, text=True)
    return json.loads(r.stdout or "{}")


def one(f: Path):
    # absolute but NOT resolved: ~/Music/Tracks is often a symlink into iCloud
    # (~/Library/Mobile Documents/...), and browsers can't read TCC-protected
    # ~/Library at all — resolving the symlink silently kills file:// playback.
    d = {"name": f.name, "path": os.path.abspath(f), "size": f.stat().st_size, "ext": f.suffix.lower()}
    pr = probe(f)
    fmt = pr.get("format", {})
    tags = {k: v for k, v in (fmt.get("tags") or {}).items()}
    astream = next((s for s in pr.get("streams", []) if s.get("codec_type") == "audio"), {})
    vstream = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video"), None)
    for s in pr.get("streams", []):
        if s.get("codec_type") == "audio":
            tags.update({k: v for k, v in (s.get("tags") or {}).items() if k not in tags})
    d["tags"] = tags
    d["audio"] = {k: astream.get(k) for k in
                  ("codec_name", "sample_rate", "channels", "bits_per_raw_sample", "bit_rate")}
    d["dur"] = float(fmt.get("duration") or 0)
    d["container"] = fmt.get("format_long_name")

    # decoded sidecar blobs (Mixed In Key / Serato)
    for key, out in (("CUEPOINTS", "mik"), ("BEATGRID", "beatgrid"), ("ENERGY", "energy"), ("KEY", "mikkey")):
        v = next((tags[k] for k in tags if k.upper() == key), None)
        if v:
            j = b64json(v.replace("\n", ""))
            if j is not None:
                if out == "beatgrid":
                    j = {"tempo": j.get("tempo"), "algorithm": j.get("algorithm"),
                         "n_beats": len(j.get("beats", [])), "beats": j.get("beats", [])}
                d[out] = j
            else:
                d[out + "_err"] = "undecodable"

    # cover art
    if vstream is not None:
        p = Path("/tmp/_cover.jpg")
        subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-i", str(f), "-an", "-frames:v", "1", str(p)],
                       capture_output=True)
        if p.exists() and p.stat().st_size:
            im = Image.open(p).convert("RGB")
            d["cover_dims"] = im.size
            im.thumbnail((320, 320))
            b = io.BytesIO(); im.save(b, "JPEG", quality=76)
            d["cover"] = "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()
            # dominant colour, for accenting the card
            sm = im.copy(); sm.thumbnail((1, 1))
            d["accent"] = "#%02x%02x%02x" % sm.getpixel((0, 0))
            p.unlink()

    # serato cues via our own binary
    r = subprocess.run([BIN, "read", str(f)], capture_output=True, text=True)
    d["cues"] = json.loads(r.stdout or "{}").get("cues", []) if r.returncode == 0 else []
    d["read_err"] = r.stderr.strip()[:200] if r.returncode != 0 else ""

    # waveform: three-band RGB envelope, rendered to an inline PNG
    p = subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(f), "-ac", "1", "-ar", "8000",
                        "-f", "s16le", "-"], capture_output=True)
    y = np.frombuffer(p.stdout, "<i2").astype(np.float32) / 32768
    if len(y):
        d["dur"] = len(y) / 8000
        beats = d.get("beatgrid", {}).get("beats") or []
        d["wave"] = rgb_wave(y, beats)
    return d


SR = 8000
BANDS = [(20, 180), (180, 1200), (1200, 4000)]      # bass / body+vocal / air
# muted palette — colour comes from the *mix*, so these blend rather than glare
HUES = np.array([12, 292, 186], np.float32)          # bass orange-red / mid violet / air cyan
CONTRAST = 4.2                                       # how far a section's mix is pushed
                                                     # from the track's average mix
SAT = 1.9                                            # post-blend saturation
PHRASE_BEATS = 16                                    # hue is flat across 4 bars
FALLBACK_BLOCK = 7.0                                 # seconds, when there's no beatgrid
W_PX, H_PX = 1400, 120
BG = np.array([13, 13, 13], np.float32)      # matches the panel background


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
    """Column indices where the hue is allowed to change — every 4 bars on the
    embedded beatgrid, or fixed-length blocks when a track has no grid."""
    if len(beats) > PHRASE_BEATS:
        t = np.array(beats[::PHRASE_BEATS], np.float32)
    else:
        t = np.arange(0, dur, FALLBACK_BLOCK, dtype=np.float32)
    e = np.unique(np.clip((t / dur * W_PX).astype(int), 0, W_PX))
    return np.unique(np.concatenate([[0], e, [W_PX]]))


def rgb_wave(y: np.ndarray, beats: list[float] | None = None) -> str:
    """Waveform whose colour tracks the spectral mix, quantised to 4-bar phrases:
    bass-led warm, mid/vocal violet, air-led cyan."""
    idx = np.linspace(0, len(y), W_PX + 1).astype(int)
    n = max(int(np.diff(idx).max()), 16)
    n = 1 << (n - 1).bit_length()                     # fft size per column
    freqs = np.fft.rfftfreq(n, 1 / SR)
    masks = [(freqs >= lo) & (freqs < hi) for lo, hi in BANDS]

    amp = np.zeros((W_PX, 3), np.float32)
    peak = np.zeros(W_PX, np.float32)
    for i in range(W_PX):
        seg = y[idx[i]:idx[i + 1]]
        if seg.size == 0:
            continue
        peak[i] = np.abs(seg).max()
        buf = np.zeros(n, np.float32)
        buf[:min(seg.size, n)] = seg[:n]
        mag = np.abs(np.fft.rfft(buf * np.hanning(n)))
        for b, m in enumerate(masks):
            amp[i, b] = np.sqrt((mag[m] ** 2).sum()) / n

    # tilt gain offsets the natural spectral rolloff so a bright vocal section can
    # out-weigh the bass it sits over
    amp *= np.array([1.0, 1.7, 2.4], np.float32)

    # hue = the band *mix*, held flat across each 4-bar phrase so the track reads as
    # discrete coloured segments. Amplitude stays per-column, so detail is unaffected.
    edges = phrase_edges(beats or [], len(y) / SR)
    mix = np.empty((W_PX, 3), np.float32)
    for a, b in zip(edges[:-1], edges[1:]):
        blk = amp[a:b]
        if not blk.size:
            continue
        # weight by loudness: near-silent columns shouldn't steer a phrase's colour
        w = blk.sum(1, keepdims=True)
        m = (blk * w).sum(0) / (w.sum() + 1e-9)
        mix[a:b] = m / (m.sum() + 1e-9)

    # every track has its own average spectrum; what's interesting is how a phrase
    # departs from it, so amplify the deviation. Without this everything blends to
    # one muddy average colour.
    base = mix.mean(0, keepdims=True)
    mix = np.clip(base + (mix - base) * CONTRAST, 0, 1)
    mix /= mix.sum(1, keepdims=True) + 1e-9

    # Colour as hue-angle rather than an RGB blend: each band sits at its own hue on
    # the wheel and the mix is their vector sum. A band-dominant phrase lands far from
    # the centre and comes out neon; a balanced one lands near it and desaturates to
    # near-grey. Blending RGB directly instead pushes balanced phrases to pale pink.
    ang = np.radians(HUES)
    x, y = (mix * np.cos(ang)).sum(1), (mix * np.sin(ang)).sum(1)
    hue = (np.degrees(np.arctan2(y, x)) % 360) / 360
    sat = np.clip(np.hypot(x, y) * SAT, 0.12, 1)

    # loudness drives brightness, not colour. Saturation feeds into value too, so a
    # balanced phrase reads as dim rather than washing out to white.
    pk = np.clip(peak / (np.percentile(peak, 99) or 1.0), 0, 1)
    val = (0.5 + 0.5 * pk ** 0.6) * (0.5 + 0.5 * sat)
    col = hsv_to_rgb(hue, sat, val) * 255

    half = H_PX / 2
    h = (np.clip(pk, 0, 1) ** 0.85 * half * 0.98)[:, None]       # W x 1 half-height
    rows = np.abs(np.arange(H_PX) - half + 0.5)[None, :]         # 1 x H
    cov = np.clip(h - rows + 0.5, 0, 1)                          # W x H, antialiased edge
    # gentle falloff towards the tips keeps it from glaring as a solid block
    shade = 1 - 0.3 * np.clip(rows / np.maximum(h, 1), 0, 1) ** 2
    # composited onto the panel colour rather than kept transparent: a smooth-gradient
    # RGBA PNG is ~7x larger, and a 64-colour palette PNG is indistinguishable here
    img = BG + (col[:, None, :] - BG) * (cov * shade)[:, :, None]
    im = Image.fromarray(np.clip(img, 0, 255).transpose(1, 0, 2).astype(np.uint8), "RGB")
    im = im.quantize(colors=64, method=Image.MEDIANCUT, dither=Image.Dither.NONE)
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


tracks = [Path(a) for a in sys.argv[1:-1]]
docs = [one(t) for t in tracks]
Path(sys.argv[-1]).write_text(json.dumps(docs))
for d in docs:
    print(f'{d["name"][:50]:52} {d["dur"]:7.1f}s  {len(d["cues"]):2} cues  '
          f'{len(d["tags"]):2} tags  mik:{len(d.get("mik", {}).get("cues", []))}  '
          f'grid:{d.get("beatgrid", {}).get("n_beats", "-")}  wave:{len(d.get("wave",""))/1365:.0f}KB')
