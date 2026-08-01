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
"""Visual QA report for cue-fighter: waveform + beatgrid + cues, as one HTML file.

Renders each track's waveform to a PNG (with the beat/bar grid baked in), then
overlays cue markers as positioned HTML so the labels stay crisp and hoverable.
Detected cues are shown against the track's *existing* manual cues, so you can
judge placement without involving a DJ app at all.

    uv run report.py TRACK... --json-dir cues/ -o report.html

Cue sources:
  auto   <json-dir>/<stem>.cues.json  (as written by detect_cues.py)
  manual the track's own embedded Serato tag, via `cue-fighter read`

The beat grid is imported from detect_cues, so what's drawn is exactly the grid
the cues were snapped to — not a re-estimate that might disagree.
"""

import argparse
import base64
import html
import json
import re
import subprocess
from datetime import datetime
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import detect_cues as dc
from cuehtml import file_url

DEFAULT_BIN = str(Path(__file__).resolve().parent / "target" / "release" / "cue-fighter")

W, H = 1800, 200          # waveform bitmap size
WAVE = (110, 170, 235)    # waveform colour
BEAT = (255, 255, 255, 18)
BAR = (255, 255, 255, 42)
PHRASE = (255, 210, 120, 70)   # every 4 bars


def waveform_png(y: np.ndarray, beats: np.ndarray, bars: np.ndarray, dur: float) -> str:
    """Waveform with the grid baked in, as a base64 PNG data URI."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def x_of(t: float) -> float:
        return (t / dur) * W if dur else 0

    # grid first, so the waveform draws over it
    if len(beats) and (W / max(len(beats), 1)) > 3:   # skip beats if they'd be a smear
        for t in beats:
            d.line([(x_of(t), 0), (x_of(t), H)], fill=BEAT)
    for i, t in enumerate(bars):
        col = PHRASE if i % 4 == 0 else BAR
        d.line([(x_of(t), 0), (x_of(t), H)], fill=col)

    # min/max envelope per pixel column
    idx = np.linspace(0, len(y), W + 1).astype(int)
    mid, amp = H / 2, (H / 2) * 0.94
    peak = float(np.abs(y).max()) or 1.0
    for i in range(W):
        seg = y[idx[i]:idx[i + 1]]
        if seg.size == 0:
            continue
        lo, hi = float(seg.min()) / peak, float(seg.max()) / peak
        d.line([(i, mid - hi * amp), (i, mid - lo * amp)], fill=WAVE + (255,))

    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# All-In-One (Harmonix) segment label -> band colour. Tuned to read against the
# dark card; `start`/`end` are track edges, not musical sections, so drawn faint.
HARMONIX_COLORS = {
    "intro": "4C9AFF", "verse": "8B949E", "chorus": "3FB950", "inst": "A371F7",
    "break": "D29922", "bridge": "DB61A2", "solo": "F778BA", "outro": "1F6FEB",
    "start": "30363D", "end": "30363D",
}


def load_segments(d: Path | None, stem: str) -> list[dict]:
    """Read All-In-One segments from a <stem>.beats.json sidecar (times in s)."""
    if not d:
        return []
    jf = d / f"{stem}.beats.json"
    if not jf.exists():
        return []
    return json.loads(jf.read_text()).get("segments", [])


def segment_band(segments: list[dict], dur: float) -> str:
    """Coloured, seekable spans — one per segment — with the label centred."""
    if not segments or not dur:
        return '<div class="empty">no segments for this track</div>'
    out = []
    for s in segments:
        st, en = float(s["start"]), float(s["end"])
        left, width = 100 * st / dur, 100 * (en - st) / dur
        lab = html.escape(str(s.get("label", "")))
        col = HARMONIX_COLORS.get(s.get("label", ""), "6E7681")
        out.append(
            f'<div class="seg" style="left:{left:.3f}%;width:{width:.3f}%;background:#{col}" '
            f'data-t="{st:.3f}" title="{lab} — {mmss(st*1000)}–{mmss(en*1000)}">'
            f'<span>{lab}</span></div>'
        )
    return "".join(out)


def read_manual_cues(track: Path, binp: str) -> list[dict]:
    r = subprocess.run([binp, "read", str(track)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return []
    return json.loads(r.stdout or "{}").get("cues", [])


def mmss(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


def markers(cues: list[dict], dur_ms: float, lane: str) -> str:
    """Absolutely-positioned cue markers; labels stagger to limit collisions.
    Each label carries its time in seconds so the player can seek to it."""
    out = []
    for i, c in enumerate(sorted(cues, key=lambda c: c["ms"])):
        pct = 100 * c["ms"] / dur_ms if dur_ms else 0
        col = "#" + (c.get("color") or "888888")
        lab = html.escape(c.get("label") or "cue")
        tier = i % 4
        secs = c["ms"] / 1000
        # clamp chips at the extremes so they don't hang off the container edge
        edge = " edge-l" if pct < 4 else (" edge-r" if pct > 96 else "")
        out.append(
            f'<div class="mk {lane}" style="left:{pct:.3f}%;--c:{col}">'
            f'<span class="tick"></span>'
            f'<span class="lab t{tier}{edge}" data-t="{secs:.3f}" title="{lab} — jump to {mmss(c["ms"])}">'
            f'{html.escape(str(c["index"]))} {lab}<em>{mmss(c["ms"])}</em></span></div>'
        )
    return "".join(out)


CSS = """
*{box-sizing:border-box} body{margin:0;padding:28px;background:#0e1116;color:#e6edf3;
font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
h1{font-size:19px;margin:0 0 4px} .sub{color:#8b949e;font-size:13px;margin-bottom:22px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px 18px;margin-bottom:18px}
.hdr{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.title{font-weight:600;font-size:15px} .meta{color:#8b949e;font-size:12.5px}
.meta b{color:#c9d1d9;font-weight:600}
.lane-label{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.06em;margin:14px 0 2px}
.wrap{position:relative;width:100%} .wrap img{width:100%;display:block;border-radius:4px;background:#0b0e13}
.lane{position:relative;width:100%;height:76px}
.mk{position:absolute;top:0;height:100%;transform:translateX(-0.5px)}
.mk .tick{position:absolute;top:0;width:2px;height:11px;background:var(--c);border-radius:1px}
.mk .lab{position:absolute;white-space:nowrap;font-size:10.5px;padding:1px 5px;border-radius:3px;
background:var(--c);color:#0b0e13;font-weight:700;transform:translateX(-50%);left:1px;z-index:1}
/* time is hidden until hover so chips stay narrow enough not to collide */
.mk .lab em{font-style:normal;opacity:.75;font-weight:600;margin-left:4px;display:none}
.mk .lab:hover{z-index:5}
.mk .lab:hover em{display:inline}
.mk .lab.edge-l{transform:translateX(0)}
.mk .lab.edge-r{transform:translateX(-100%)}
.mk .lab.t0{top:12px}.mk .lab.t1{top:28px}.mk .lab.t2{top:44px}.mk .lab.t3{top:60px}
.overlay{position:absolute;inset:0;pointer-events:none}
/* the waveform overlay shows ticks only — labels live in the lanes below */
.overlay .lab{display:none}
.overlay .mk .tick{height:100%;opacity:.55;width:1.5px}
.legend{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
.legend span{font-size:11px;padding:2px 7px;border-radius:3px;color:#0b0e13;font-weight:700}
.empty{color:#8b949e;font-size:12.5px;padding:6px 0}
/* --- player --- */
.wrap{cursor:crosshair}
.ph{position:absolute;top:0;height:100%;width:2px;background:#fff;box-shadow:0 0 6px #fff;
left:0;pointer-events:none;opacity:0;transition:opacity .15s}
.playing .ph{opacity:.95}
.mk .lab{cursor:pointer;pointer-events:auto;transition:transform .08s,filter .08s}
.mk .lab:hover{filter:brightness(1.25);transform:translateX(-50%) scale(1.08)}
.mk .lab.on{outline:2px solid #fff;outline-offset:1px}
.play{cursor:pointer;background:#238636;border:0;color:#fff;font:600 12px/1 inherit;
padding:7px 13px;border-radius:5px;margin-right:8px}
.play:hover{background:#2ea043}
.time{color:#8b949e;font-variant-numeric:tabular-nums;font-size:12px}
audio{display:none}
.prov{color:#6e7681;font-size:11px;font-variant-numeric:tabular-nums}
.mk.prev .lab,.mk.man .lab{opacity:.82}
/* --- All-In-One structure band --- */
.band{position:relative;width:100%;height:26px;border-radius:4px;overflow:hidden;background:#0b0e13}
.seg{position:absolute;top:0;height:100%;border-right:1px solid #0b0e13;cursor:pointer;
overflow:hidden;display:flex;align-items:center;justify-content:center;transition:filter .08s}
.seg:hover{filter:brightness(1.2)}
.seg span{font-size:10px;font-weight:700;color:#0b0e13;text-transform:uppercase;letter-spacing:.03em;
white-space:nowrap;padding:0 4px;text-overflow:ellipsis;overflow:hidden}
/* segment boundaries drawn over the waveform, so you can read them against cues */
.bnd{position:absolute;top:0;height:100%;width:1.5px;background:rgba(255,255,255,.5);pointer-events:none}
.bnd.first{display:none}
"""

JS = """
document.querySelectorAll('.card').forEach(card => {
  const a = card.querySelector('audio'), wrap = card.querySelector('.wrap'),
        ph = card.querySelector('.ph'), btn = card.querySelector('.play'),
        tm = card.querySelector('.time'), dur = parseFloat(card.dataset.dur);
  if (!a) return;
  const fmt = s => (s<0?0:Math.floor(s/60))+':'+String(Math.floor(s%60)).padStart(2,'0');
  const seek = (t, play=true) => {
    document.querySelectorAll('audio').forEach(o => { if (o!==a) { o.pause();
      o.closest('.card').classList.remove('playing'); } });
    a.currentTime = Math.max(0, Math.min(dur-0.05, t));
    if (play) a.play().catch(()=>{});
  };
  // click the waveform to seek
  wrap.addEventListener('click', e => {
    if (e.target.closest('.lab')) return;               // cue chips handle themselves
    const r = wrap.getBoundingClientRect();
    seek(((e.clientX - r.left) / r.width) * dur);
  });
  // click a cue chip or a structure segment to jump to it
  card.querySelectorAll('.lab[data-t], .seg[data-t]').forEach(el => {
    el.addEventListener('click', e => { e.stopPropagation(); seek(parseFloat(el.dataset.t)); });
  });
  btn.addEventListener('click', () => a.paused ? seek(a.currentTime||0) : a.pause());
  a.addEventListener('play',  () => { card.classList.add('playing'); btn.textContent = '❚❚ pause'; });
  a.addEventListener('pause', () => { card.classList.remove('playing'); btn.textContent = '▶ play'; });
  a.addEventListener('timeupdate', () => {
    const p = a.currentTime / dur;
    ph.style.left = (p*100).toFixed(4) + '%';
    tm.textContent = fmt(a.currentTime) + ' / ' + fmt(dur);
    // highlight the cue we're currently past
    let best = null;
    card.querySelectorAll('.lab[data-t]').forEach(el => {
      el.classList.remove('on');
      if (parseFloat(el.dataset.t) <= a.currentTime + 0.02) best = el;
    });
    if (best) best.classList.add('on');
  });
});
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tracks", nargs="*", type=Path)
    ap.add_argument("--list", help="file of newline-separated track paths")
    ap.add_argument("--json-dir", type=Path, help="detected cues (<stem>.cues.json)")
    ap.add_argument("--compare-dir", type=Path,
                    help="a second cue dir (e.g. a previous run) shown as its own lane")
    ap.add_argument("--compare-name", default="previous run", help="label for --compare-dir")
    ap.add_argument("--segments-dir", type=Path,
                    help="dir of All-In-One sidecars (<stem>.beats.json) — draws the "
                         "labeled segment band + boundary lines over the waveform")
    ap.add_argument("--bin", default=DEFAULT_BIN, help="path to cue-fighter binary")
    ap.add_argument("-o", "--out", type=Path, default=Path("report.html"))
    ap.add_argument("--no-manual", action="store_true", help="skip the manual-cue comparison lane")
    ap.add_argument("--audio-dir", type=Path,
                    help="play audio from <audio-dir>/<track-name> instead of the track's own "
                         "path — use when the library lives in iCloud/~Library (browsers can't "
                         "read it); point at a local copy so file:// audio loads")
    args = ap.parse_args()

    tracks = list(args.tracks)
    if args.list:
        tracks += [Path(l) for l in Path(args.list).read_text().splitlines() if l.strip()]
    if not tracks:
        raise SystemExit("no tracks given (positional paths or --list)")

    cards, skipped = [], []
    for t in tracks:
        try:
            y = dc.load_audio(t)          # ffmpeg fallback lives in detect_cues
        except Exception as e:
            print(f"skip {t.name}: {e}")
            skipped.append(t.name)
            continue
        dur = len(y) / dc.SR
        dur_ms = dur * 1000

        def load_cues(d: Path | None) -> tuple[list[dict], dict]:
            if not d:
                return [], {}
            jf = d / f"{t.stem}.cues.json"
            if not jf.exists():
                return [], {}
            doc = json.loads(jf.read_text())
            return doc.get("cues", []), doc.get("meta", {})

        auto, meta = load_cues(args.json_dir)
        prev, prev_meta = load_cues(args.compare_dir)
        manual = [] if args.no_manual else read_manual_cues(t, args.bin)
        segments = load_segments(args.segments_dir, t.stem)

        # draw the SAME grid the cues were snapped to: prefer the run's own
        # recorded bpm/grid, fall back to the filename BPM + quantized default
        bpm = meta.get("bpm") or dc.bpm_from_name(t.name) or dc.beat_grid(y)[0]
        if meta.get("grid", "quantized") == "detected":
            beats = dc.beat_grid(y, bpm)[1]
        else:
            beats = dc.quantized_grid(y, bpm, dur)
        bars = dc.bar_starts(y, beats)

        key = ""
        m = re.search(r"\(\d{1,3},\s*([A-Ga-g][#b]?m?)\)", t.name)
        if m:
            key = m.group(1)

        img = waveform_png(y, beats, bars, dur)
        prov = ""
        if meta:
            prov = (f' &nbsp;·&nbsp; <span class="prov">-s {meta.get("sensitivity")} '
                    f'-r {meta.get("radius")} · {meta.get("snap")}-snap · '
                    f'min-gap {meta.get("min_gap_beats")}b · '
                    f'-{meta.get("dropped_too_close", 0)} too close · '
                    f'{html.escape(str(meta.get("generated", "")))}</span>')

        lanes = ""
        if auto:
            lanes += (f'<div class="lane-label">detected — {len(auto)} cues{prov}</div>'
                      f'<div class="lane">{markers(auto, dur_ms, "auto")}</div>')
        elif args.json_dir is not None:
            lanes += '<div class="empty">no detected cues for this track</div>'
        if prev:
            pg = html.escape(str(prev_meta.get("generated", "")))
            lanes += (f'<div class="lane-label">{html.escape(args.compare_name)} — '
                      f'{len(prev)} cues &nbsp;·&nbsp; <span class="prov">{pg}</span></div>'
                      f'<div class="lane">{markers(prev, dur_ms, "prev")}</div>')
        if manual:
            lanes += (f'<div class="lane-label">yours (existing tag) — {len(manual)} cues</div>'
                      f'<div class="lane">{markers(manual, dur_ms, "man")}</div>')

        # All-In-One structure band + boundary lines over the waveform
        seg_overlay = band = ""
        if args.segments_dir is not None:
            bounds = sorted({float(s["start"]) for s in segments})
            seg_overlay = "".join(
                f'<div class="bnd{" first" if i == 0 else ""}" '
                f'style="left:{100*bt/dur:.3f}%"></div>'
                for i, bt in enumerate(bounds)
            )
            band = (f'<div class="lane-label">All-In-One structure — {len(segments)} segments</div>'
                    f'<div class="band">{segment_band(segments, dur)}</div>')

        # linked, not embedded: FLACs are ~25 MB each and browsers play file:// audio.
        # never resolve() — a track under ~/Library (iCloud) becomes unreadable to the
        # browser; --audio-dir points playback at a local copy outside ~/Library.
        audio_path = (args.audio_dir / t.name) if args.audio_dir else t
        src = file_url(audio_path)
        cards.append(f"""<div class="card" data-dur="{dur:.4f}">
<div class="hdr"><div class="title">{html.escape(t.stem)}</div>
<div class="meta"><button class="play">▶ play</button><span class="time">0:00 / {mmss(dur_ms)}</span>
&nbsp; <b>{bpm:.0f}</b> bpm &nbsp; <b>{html.escape(key) or "?"}</b> &nbsp;
<b>{mmss(dur_ms)}</b> &nbsp; {len(bars)} bars</div></div>
<audio preload="none" src="{html.escape(src)}"></audio>
<div class="wrap"><img src="{img}" alt="waveform">
<div class="overlay"><div class="lane">{markers(auto, dur_ms, "auto")}</div>{seg_overlay}</div>
<div class="ph"></div></div>
{band}
{lanes}</div>""")

    legend = "".join(
        f'<span style="background:#{c}">{l}</span>' for l, c in dc.LABEL_COLORS.items()
    )
    seg_legend = ""
    if args.segments_dir is not None:
        seg_legend = (
            '<div class="legend" style="margin-top:8px">'
            + "".join(f'<span style="background:#{c}">{l}</span>'
                      for l, c in HARMONIX_COLORS.items() if l not in ("start", "end"))
            + "</div>"
        )
    rev = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip() or "untracked"
    stamp = f'generated {datetime.now().isoformat(timespec="seconds")} · cue-fighter @ {rev}'
    doc = (f"<!doctype html><meta charset=utf-8><title>cue-fighter report</title>"
           f"<style>{CSS}</style><h1>cue-fighter — cue placement report</h1>"
           f'<div class="sub">{len(tracks)} track(s) · <span class="prov">{html.escape(stamp)}</span>'
           f'<br>Grid: faint = beats, brighter = bars, '
           f'amber = every 4 bars. <b>Click the waveform to seek, click a cue chip to jump to it.</b>'
           f'<div class="legend">{legend}</div>{seg_legend}</div>'
           + "".join(cards) + f"<script>{JS}</script>")
    args.out.write_text(doc)
    print(f"wrote {args.out}  ({args.out.stat().st_size/1024:.0f} KB, {len(cards)} tracks)")
    if skipped:
        print(f"{len(skipped)} track(s) skipped as unreadable: " + ", ".join(skipped[:5])
              + (" …" if len(skipped) > 5 else ""))
    print("audio is linked (not embedded) — keep the tracks where they are, and open the")
    print("report from a local browser so file:// audio can load.")


if __name__ == "__main__":
    main()
