"""Shared pieces for the HTML-emitting tools (report.py, inspector.py).

Deliberately dependency-free — importing this must never pull in torch or
librosa, so `inspector.py` can run without the ML stack.
"""

import html
import json
import subprocess
from pathlib import Path
from urllib.parse import quote

DEFAULT_BIN = str(Path(__file__).resolve().parent / "target" / "release" / "cue-fighter")

AUDIO_EXTS = {".flac", ".mp3", ".aif", ".aiff", ".m4a", ".mp4"}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def mmss(seconds: float) -> str:
    s = int(abs(seconds))
    return f"{s // 60}:{s % 60:02d}"


def mmssms(ms: float) -> str:
    return f"{mmss(ms / 1000)}.{abs(round(ms)) % 1000:03d}"


def read_cues(track: Path, binp: str = DEFAULT_BIN) -> tuple[list[dict], str]:
    """Hot cues from the file's own Serato tag, via our CLI. (cues, error)."""
    try:
        r = subprocess.run([binp, "read", str(track)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"cue-fighter: {e}"
    if r.returncode != 0:
        return [], (r.stderr or "read failed").strip()[:200]
    try:
        return json.loads(r.stdout or "{}").get("cues", []), ""
    except json.JSONDecodeError as e:
        return [], f"bad JSON from cue-fighter: {e}"


def file_url(path: Path) -> str:
    """A file:// URL a browser will actually open.

    Two traps, both of which silently break playback rather than erroring:
      * `~/Music/Tracks` is often a symlink into iCloud
        (`~/Library/Mobile Documents/...`), and browsers cannot read
        TCC-protected `~/Library` at all — so never resolve() the path.
      * '#' is a fragment delimiter, and a large share of a DJ library has a
        sharp in the key: "(126, F#m).flac" truncates without quoting.
    """
    import os
    return "file://" + quote(os.path.abspath(path))


def rel_url(path: Path) -> str:
    """Just the filename, quoted — for sidecars that sit beside their track."""
    return quote(Path(path).name)


def expand_tracks(paths: list[Path], listfile: str | None = None) -> list[Path]:
    """Flatten files, directories and an optional --list into a track list."""
    out: list[Path] = []
    if listfile:
        paths = list(paths) + [Path(l) for l in Path(listfile).read_text().splitlines() if l.strip()]
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() in AUDIO_EXTS)
        elif p.suffix.lower() in AUDIO_EXTS:
            out.append(p)
        elif p.exists():
            out.append(p)                      # explicit path, unknown extension: try it
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


# Playback shared by both tools. Generic over markup: any element carrying
# data-player and data-dur, containing <audio> plus .wave/.ph/.prog/.pb/.tm.
# Anything with data-t inside (cue flag, table row) seeks to that time.
PLAYER_JS = r"""
function initPlayers(root) {
  (root || document).querySelectorAll('[data-player]').forEach(scope => {
    if (scope.dataset.wired) return;
    scope.dataset.wired = '1';
    const a = scope.querySelector('audio'), wv = scope.querySelector('.wave'),
          ph = scope.querySelector('.ph'), pr = scope.querySelector('.prog'),
          pb = scope.querySelector('.pb'), tm = scope.querySelector('.tm'),
          dur = parseFloat(scope.dataset.dur) || 0;
    if (!a) return;
    const fmt = s => Math.floor(Math.abs(s)/60) + ':' + String(Math.floor(Math.abs(s)%60)).padStart(2,'0');
    const fail = why => { if (tm) { tm.textContent = 'no audio'; tm.title = why; }
                          scope.classList.add('dead'); };
    const seek = s => {
      document.querySelectorAll('audio').forEach(o => { if (o !== a) o.pause(); });
      a.currentTime = Math.max(0, Math.min(dur - 0.05, s));
      a.play().catch(e => fail(e.message || 'playback refused'));
    };
    scope.seekTo = seek;
    if (wv) wv.onclick = e => {
      e.stopPropagation();
      const c = e.target.closest('[data-t]');
      if (c) return seek(parseFloat(c.dataset.t));
      const r = wv.getBoundingClientRect();
      seek((e.clientX - r.left) / r.width * dur);
    };
    scope.querySelectorAll('[data-t]').forEach(el => el.onclick = e => {
      e.stopPropagation(); seek(parseFloat(el.dataset.t));
    });
    if (pb) pb.onclick = e => { e.stopPropagation(); a.paused ? seek(a.currentTime || 0) : a.pause(); };
    const ICON = {play:'M3 1.5l11 6.5-11 6.5z', pause:'M3 1.5h4v13H3zm6 0h4v13H9z'};
    const icon = k => { const p = pb && pb.querySelector('path'); if (p) p.setAttribute('d', ICON[k]); };
    a.onplay  = () => { scope.classList.add('playing'); icon('pause'); };
    a.onpause = () => { scope.classList.remove('playing'); icon('play'); };
    a.onerror = () => fail(a.error && a.error.message ? a.error.message : 'load failed');
    a.ontimeupdate = () => {
      const p = (a.currentTime / dur * 100).toFixed(3) + '%';
      if (ph) ph.style.left = p;
      if (pr) pr.style.width = p;
      if (tm) tm.textContent = fmt(a.currentTime);
    };
  });
}
"""

PLAY_BUTTON = ('<button class="pb" aria-label="play">'
               '<svg viewBox="0 0 16 16"><path d="M3 1.5l11 6.5-11 6.5z"/></svg></button>')
