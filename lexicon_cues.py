# /// script
# requires-python = ">=3.10,<3.13"
# ///
"""Read the user's cues from Lexicon (the source of truth) and apply the locked
label taxonomy — the replacement for the stale file-tag training source.

Lexicon `main.db` (SQLite) carries every cue across all formats (FLAC/m4a/mp3),
current, with per-track genre/energy/key/bpm. We read `Cuepoint` (type=1) joined
to `Track`, then classify each cue by matching the KEYWORD in its name (NOT the
colour — DJ software reuses colours across types, so colour is unreliable).

    uv run lexicon_cues.py -o cues.csv           # full labeled dump + summary
    uv run lexicon_cues.py --summary-only        # just the distribution

Taxonomy (locked). Two axes:
  STRUCTURE (phrase-aligned)  coarse <- fine leaves
    INTRO      <- INTRO                       (own class; verified by position ~t0)
    MIX IN     <- MIX IN                      (mixable breakdown/entry mid-track)
    CUT        <- CUT, VOCAL CUT, PRE-DROP, DRUM ROLL   (energy collapse before a DROP)
    BUILD      <- BUILD, RISER
    DROP       <- DROP
    BREAK      <- BREAK
    VERSE      <- VERSE
    CHORUS     <- CHORUS
    PRE-CHORUS <- PRE-CHORUS                  (VCV)
    OUTRO      <- OUTRO                        (end + loopable)
  EVENT (bar-aligned; element entry, from stem onsets)
    KICK, BASS, DRUMS, SNARE, RHYTHM, MELODY, VOCAL, HOOK
  EXCLUDE
    MIX OUT (subjective), ENERGY (Mixed-In-Key), generic CUE / unlabeled

Match is keyword-anywhere, most-specific first, first hit wins (so "VOCAL CUT"
beats "CUT"/"VOCAL", "DRUM ROLL" beats "DRUMS", "PRE-DROP" beats "DROP"). The
fine leaf is retained so nothing is lost; train on the coarse class.
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DEFAULT_DB = Path.home() / "Library/Application Support/Lexicon/main.db"

# (regex, leaf, coarse, layer) — ORDER MATTERS: specific first, first match wins.
# layer "exclude:<reason>" drops the cue from the training set.
RULES = [
    (r"VOCAL\s*CUT",            "VOCAL CUT",  "CUT",        "structure"),
    (r"DRUM\s*ROLL|DRUMROLL",   "DRUM ROLL",  "CUT",        "structure"),
    (r"PRE[\s-]*CHORUS",        "PRE-CHORUS", "PRE-CHORUS", "structure"),
    (r"PRE[\s-]*DROP",          "PRE-DROP",   "CUT",        "structure"),
    (r"MIX\s*IN",               "MIX IN",     "MIX IN",     "structure"),
    (r"MIX\s*OUT|MIXOUT",       "MIX OUT",    None,         "exclude:mixout"),
    (r"INTRO",                  "INTRO",      "INTRO",      "structure"),
    (r"OUTRO",                  "OUTRO",      "OUTRO",      "structure"),
    (r"RISER",                  "RISER",      "BUILD",      "structure"),
    (r"BUILD",                  "BUILD",      "BUILD",      "structure"),
    (r"CHORUS",                 "CHORUS",     "CHORUS",     "structure"),
    (r"DROP",                   "DROP",       "DROP",       "structure"),
    (r"VERSE",                  "VERSE",      "VERSE",      "structure"),
    (r"BREAK",                  "BREAK",      "BREAK",      "structure"),
    (r"CUT",                    "CUT",        "CUT",        "structure"),
    (r"KICK",                   "KICK",       "KICK",       "event"),
    (r"\bBASS",                 "BASS",       "BASS",       "event"),
    (r"SNARE",                  "SNARE",      "SNARE",      "event"),
    (r"RHYTHM",                 "RHYTHM",     "RHYTHM",     "event"),
    (r"MELODY",                 "MELODY",     "MELODY",     "event"),
    (r"DRUMS?",                 "DRUMS",      "DRUMS",      "event"),
    (r"HOOK",                   "HOOK",       "HOOK",       "event"),
    (r"VOCALS?",                "VOCAL",      "VOCAL",      "event"),
    (r"ENERGY",                 "ENERGY",     None,         "exclude:energy"),
]
_COMPILED = [(re.compile(p), leaf, coarse, layer) for p, leaf, coarse, layer in RULES]

IBCD = ("house", "techno", "trance", "psy", "goa", "bass", "dance", "edm", "electro",
        "electronic", "electronica", "funk", "indie dance", "synthwave", "hardstyle",
        "garage", "club", "mainstage", "progressive", "melodic", "drum & bass", "dnb")
VCV = ("pop", "hip-hop", "hip hop", "rap", "rock", "r&b", "rnb", "indie", "soul",
       "latin", "country", "folk")


def classify(name: str):
    """-> (leaf, coarse, layer). layer is 'structure', 'event', or 'exclude:<reason>'."""
    u = (name or "").upper()
    for rx, leaf, coarse, layer in _COMPILED:
        if rx.search(u):
            return leaf, coarse, layer
    return None, None, "exclude:unlabeled"


def route(genre: str) -> str:
    g = (genre or "").lower()
    if not g:
        return "?"
    ib = any(x in g for x in IBCD)
    vc = any(x in g for x in VCV)
    if ib and vc:
        return "both"
    return "IBCD" if ib else ("VCV" if vc else "?")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("-o", "--out", type=Path, help="write labeled cue CSV")
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--layer", choices=["structure", "event", "all"], default="all",
                    help="restrict CSV to one axis (default all, excludes always dropped)")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"Lexicon DB not found: {args.db}")
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = con.execute("""
        SELECT t.location, t.genre, t.energy, t.bpm, t.key,
               c.position, c.startTime, c.endTime, c.name
        FROM Cuepoint c JOIN Track t ON t.id = c.trackId
        WHERE c.type = 1
        ORDER BY t.location, c.startTime
    """).fetchall()

    # First pass: classify, and gather each track's coarse types so we can route
    # by CONTENT — a track with any DROP or CUT is IBCD, else traditional (VCV).
    # Content beats genre: it reflects how the track was actually cued (a pop song
    # tagged with drops is performed IBCD-style), and it covers 100% of tracks.
    classified = []
    track_coarse: dict[str, set] = {}
    for loc, genre, energy, bpm, key, pos, start, end, name in rows:
        leaf, coarse, layer = classify(name)
        classified.append((loc, genre, energy, bpm, key, pos, start, end, name, leaf, coarse, layer))
        if not layer.startswith("exclude"):
            track_coarse.setdefault(loc, set()).add(coarse)

    def content_route(loc: str) -> str:
        cs = track_coarse.get(loc, set())
        return "IBCD" if ("DROP" in cs or "CUT" in cs) else "VCV"

    out_rows = []
    coarse_c, event_c, excl_c, route_c = Counter(), Counter(), Counter(), Counter()
    for loc, genre, energy, bpm, key, pos, start, end, name, leaf, coarse, layer in classified:
        if layer.startswith("exclude"):
            excl_c[layer.split(":", 1)[1]] += 1
            continue
        r = content_route(loc)
        (coarse_c if layer == "structure" else event_c)[coarse] += 1
        route_c[r] += 1
        ext = os.path.splitext(loc)[1].lstrip(".").lower()
        out_rows.append({
            "path": loc, "ext": ext, "genre": genre,
            "route": r, "genre_route": route(genre),
            "energy": energy, "bpm": bpm, "key": key, "position": pos,
            "start_ms": int(round(start * 1000)),
            "end_ms": "" if end is None else int(round(end * 1000)),
            "name": name, "leaf": leaf, "coarse": coarse, "layer": layer,
        })

    kept = [r for r in out_rows if args.layer == "all" or r["layer"] == args.layer]
    if args.out and not args.summary_only:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            w.writeheader()
            w.writerows(kept)
        print(f"wrote {args.out}: {len(kept)} cues")

    tot = len(rows)
    struct_n = sum(coarse_c.values())
    event_n = sum(event_c.values())
    print(f"\nLexicon cues: {tot}   kept: {struct_n + event_n}   excluded: {sum(excl_c.values())}")
    print("\nSTRUCTURE (coarse class):")
    for k, n in coarse_c.most_common():
        print(f"  {n:5d}  {k}")
    print("EVENT:")
    for k, n in event_c.most_common():
        print(f"  {n:5d}  {k}")
    print("EXCLUDED:", dict(excl_c))
    print("ROUTE:", dict(route_c))


if __name__ == "__main__":
    main()
