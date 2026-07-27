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
"""Phase 1 detection-quality tools for cue-fighter.

Subcommands:

  score    Score a finished cue dir (as written by detect_cues.py) against each
           track's own manual cues — needs NO model. Reports position
           recall/precision, phase offset (in beats; flags systematic
           downbeat error), and per-family label agreement.

           uv run detect_cues.py --list tracks.txt -o cues/
           uv run evaluate.py score --list tracks.txt --json-dir cues/


  sweep    Run CUE-DETR once per track, then peak-pick across a grid of
           -s (sensitivity) and -r (radius) values. Prints a cue-count table
           per track so you can find per-genre sweet spots without reloading
           the model for every setting.

           uv run evaluate.py sweep TRACK... -s 0.3,0.5,0.7,0.9 -r 8,16

  compare  Score predicted cues against manually-set (ground-truth) cues.
           Reports recall (truth cues covered), precision (predictions that
           land on a truth cue), and offset stats. "Covered" = within one beat,
           derived from BPM (--bpm, or parsed from a "Title (128, C#m)" name).

           # produce the two inputs first:
           uv run detect_cues.py "Track (128, C#m).mp3" -o pred/
           cue-fighter read "Track (128, C#m).mp3" > truth.json
           uv run evaluate.py compare --pred pred/Track*.cues.json \\
               --truth truth.json --name "Track (128, C#m).mp3"

Both consume/emit the shared JSON schema: {"cues":[{"index","ms","label"?}]}.
"""

import argparse
import json
import re
import statistics
import subprocess
from pathlib import Path

DEFAULT_BIN = str(Path(__file__).resolve().parent / "target" / "release" / "cue-fighter")


# ---------- shared ----------

def load_cue_ms(path: Path) -> list[int]:
    """Read a {"cues":[{"ms":...}]} file -> sorted list of cue times in ms."""
    doc = json.loads(Path(path).read_text())
    return sorted(int(c["ms"]) for c in doc.get("cues", []))


def read_truth_ms(track: Path, binp: str) -> list[int]:
    """Read a track's embedded manual cues via `cue-fighter read` -> sorted ms."""
    r = subprocess.run([binp, "read", str(track)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return []
    doc = json.loads(r.stdout or "{}")
    return sorted(int(c["ms"]) for c in doc.get("cues", []))


def bpm_from_name(name: str) -> float | None:
    """Parse BPM from the library's filename convention 'Title (128, C#m)'.

    Requires the '<bpm>, <key>' shape so stray parenthesized numbers like
    '(001)' or tags like '(FREESTYLE)' don't get mistaken for the tempo.
    A parsed BPM of 0 (unknown, e.g. '(0, Bm)') is treated as missing.
    """
    m = re.search(r"\((\d{1,3}),\s*[A-Ga-g][#b]?m?\)", name)
    bpm = float(m.group(1)) if m else None
    return bpm or None


def norm_label(s: str | None) -> str:
    """Collapse a cue label to its family: uppercase, drop a trailing number."""
    if not s:
        return "?"
    return re.sub(r"\s*\d+\s*$", "", s.strip().upper()) or "?"


def read_labeled(path: Path) -> list[tuple[int, str]]:
    """(ms, family) from a {"cues":[...]} JSON file."""
    doc = json.loads(Path(path).read_text())
    return sorted((int(c["ms"]), norm_label(c.get("label"))) for c in doc.get("cues", []))


def read_truth_labeled(track: Path, binp: str) -> list[tuple[int, str]]:
    """(ms, family) from a track's embedded manual cues."""
    r = subprocess.run([binp, "read", str(track)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return []
    doc = json.loads(r.stdout or "{}")
    return sorted((int(c["ms"]), norm_label(c.get("label"))) for c in doc.get("cues", []))


# ---------- sweep ----------

def cmd_sweep(args) -> None:
    import detect_cues as dc
    from transformers import DetrForObjectDetection, DetrImageProcessor

    sens = [float(x) for x in args.sensitivity.split(",")]
    rads = [int(x) for x in args.radius.split(",")]

    dev = dc.device()
    print(f"loading {args.checkpoint} on {dev} ...")
    processor = DetrImageProcessor.from_pretrained("facebook/detr-resnet-50")
    model = DetrForObjectDetection.from_pretrained(args.checkpoint).to(dev)

    for track in args.tracks:
        positions, scores = dc.raw_detections(track, model, processor, dev)
        print(f"\n{track.name}  ({len(positions)} raw detections)")
        header = "  r\\s |" + "".join(f"{s:>6}" for s in sens)
        print(header)
        print("  " + "-" * (len(header) - 2))
        for r in rads:
            counts = []
            for s in sens:
                n = len(dc.peak_pick(positions, scores, s, r)[: args.max_cues])
                counts.append(n)
            print(f"  {r:>3} |" + "".join(f"{n:>6}" for n in counts))
    print("\n(cell = cue count after capping at --max-cues; aim for your target count)")


# ---------- compare ----------

def match(pred_ms: list[int], truth_ms: list[int], tol_ms: float) -> dict:
    """Greedy nearest-neighbor match within tolerance; each truth used once."""
    remaining = list(truth_ms)
    offsets = []
    matched_pred = 0
    for p in pred_ms:
        if not remaining:
            break
        j = min(range(len(remaining)), key=lambda k: abs(remaining[k] - p))
        if abs(remaining[j] - p) <= tol_ms:
            offsets.append(abs(remaining[j] - p))
            matched_pred += 1
            remaining.pop(j)
    return {
        "matched": matched_pred,
        "covered_truth": len(truth_ms) - len(remaining),
        "offsets": offsets,
    }


def cmd_compare(args) -> None:
    bpm = args.bpm or (bpm_from_name(args.name) if args.name else None)
    if not bpm:
        raise SystemExit("need --bpm N (or --name with '(BPM, key)') to set the 1-beat tolerance")
    tol_ms = 60_000.0 / bpm

    pred = load_cue_ms(args.pred)
    truth = load_cue_ms(args.truth)
    if not truth:
        raise SystemExit(f"{args.truth}: no ground-truth cues to compare against")

    r = match(pred, truth, tol_ms)
    recall = r["covered_truth"] / len(truth)
    precision = r["matched"] / len(pred) if pred else 0.0
    offs = r["offsets"]

    print(f"bpm {bpm:g}  |  1-beat tolerance = {tol_ms:.0f} ms")
    print(f"pred cues : {len(pred)}")
    print(f"truth cues: {len(truth)}")
    print(f"recall    : {recall:.0%}  ({r['covered_truth']}/{len(truth)} manual cues covered)")
    print(f"precision : {precision:.0%}  ({r['matched']}/{len(pred)} predictions on target)")
    if offs:
        print(f"offset    : mean {statistics.mean(offs):.0f} ms, median {statistics.median(offs):.0f} ms, max {max(offs):.0f} ms")


def match_labeled(pred: list[tuple[int, str]], truth: list[tuple[int, str]], tol_ms: float):
    """Greedy nearest match within tolerance. Each truth used once.
    Returns (n_matched, n_truth_covered, pairs) where each pair is
    (pred_family, truth_family, signed_offset_ms = pred - truth)."""
    remaining = list(truth)
    pairs = []
    for pms, plab in pred:
        if not remaining:
            break
        j = min(range(len(remaining)), key=lambda k: abs(remaining[k][0] - pms))
        if abs(remaining[j][0] - pms) <= tol_ms:
            tms, tlab = remaining.pop(j)
            pairs.append((plab, tlab, pms - tms))
    return len(pairs), len(truth) - len(remaining), pairs


def cmd_score(args) -> None:
    """Score a finished cue dir against each track's manual cues: position,
    label agreement, and phase offset. Needs no model — reads the JSON the
    pipeline already wrote and the manual cues from the tags."""
    from collections import Counter

    tracks = list(args.tracks)
    if args.list:
        tracks += [Path(ln) for ln in Path(args.list).read_text().splitlines() if ln.strip()]

    tot_pred = tot_truth = tot_matched = tot_cov = 0
    all_pairs: list[tuple[str, str, float]] = []
    per_track = []
    for t in tracks:
        bpm = bpm_from_name(t.name)
        jf = args.json_dir / f"{t.stem}.cues.json"
        if not bpm or not jf.exists():
            continue
        pred = read_labeled(jf)
        truth = read_truth_labeled(t, args.bin)
        if not truth:
            continue
        beat = 60_000.0 / bpm
        n_match, cov, pairs = match_labeled(pred, truth, args.beats * beat)
        # per-track offsets in beats for the phase read
        pairs_b = [(pl, tl, off / beat) for pl, tl, off in pairs]
        all_pairs += pairs_b
        tot_pred += len(pred); tot_truth += len(truth); tot_matched += n_match; tot_cov += cov
        rec = cov / len(truth); prec = n_match / len(pred) if pred else 0.0
        per_track.append((bpm, t.name, rec, prec, len(pred), len(truth)))

    if not per_track:
        raise SystemExit("no scorable tracks (need filename BPM + a matching <stem>.cues.json + manual cues)")

    recall = tot_cov / tot_truth
    precision = tot_matched / tot_pred
    f1 = 2 * recall * precision / (recall + precision) if (recall + precision) else 0.0
    print(f"\nscored {len(per_track)} tracks  |  tolerance {args.beats:g} beat(s)")
    print(f"  detected {tot_pred}  manual {tot_truth}  matched {tot_matched}\n")

    print("POSITION")
    print(f"  recall     {recall:6.0%}   (your cues the detector found)")
    print(f"  precision  {precision:6.0%}   (detected cues that land on one of yours)")
    print(f"  F1         {f1:6.0%}")

    offs = [off for _, _, off in all_pairs]
    if offs:
        signed = statistics.median(offs)
        absmed = statistics.median(abs(o) for o in offs)
        far = sum(1 for o in offs if abs(o) > 0.5) / len(offs)
        print("\nPHASE (matched pairs, in beats)")
        print(f"  median signed offset  {signed:+.2f}   (far from 0 => systematic downbeat/phase error)")
        print(f"  median |offset|       {absmed:.2f}")
        print(f"  >0.5 beat off         {far:.0%}")

    agree = sum(1 for pl, tl, _ in all_pairs if pl == tl) / len(all_pairs) if all_pairs else 0.0
    print(f"\nLABELS (of {len(all_pairs)} matched pairs)")
    print(f"  family agreement  {agree:.0%}")
    by_truth = Counter(tl for _, tl, _ in all_pairs)
    print(f"  {'your label':<12}{'n':>4}{'we agree':>10}   we predicted")
    for tl, n in by_truth.most_common(10):
        rows = [(pl, c) for pl, c in Counter(pl for pl, t, _ in all_pairs if t == tl).most_common()]
        correct = next((c for pl, c in rows if pl == tl), 0)
        got = ", ".join(f"{pl} {100*c//n}%" for pl, c in rows[:3])
        print(f"  {tl:<12}{n:>4}{correct/n:>9.0%}   {got}")

    if args.per_track:
        print("\nper-track (sorted by bpm):")
        print(f"{'bpm':>4} {'recall':>7} {'prec':>6} {'#det':>5} {'#man':>5}  track")
        for bpm, name, rec, prec, npred, ntruth in sorted(per_track):
            print(f"{bpm:>4.0f} {rec:>6.0%} {prec:>5.0%} {npred:>5} {ntruth:>5}  {name[:46]}")


def cmd_tune(args) -> None:
    """Sweep -s/-r and score each cell against each track's own manual cues."""
    import detect_cues as dc
    from transformers import DetrForObjectDetection, DetrImageProcessor

    sens = [float(x) for x in args.sensitivity.split(",")]
    rads = [int(x) for x in args.radius.split(",")]

    tracks = list(args.tracks)
    if args.list:
        tracks += [Path(ln) for ln in Path(args.list).read_text().splitlines() if ln.strip()]

    dev = dc.device()
    print(f"loading {args.checkpoint} on {dev} ...")
    processor = DetrImageProcessor.from_pretrained("facebook/detr-resnet-50")
    model = DetrForObjectDetection.from_pretrained(args.checkpoint).to(dev)

    grid = {(s, r): [] for s in sens for r in rads}  # -> list of (recall, precision, count)
    per_track = []  # (bpm, name, recall, precision, npred, ntruth) at the first setting
    primary = (sens[0], rads[0])
    used = 0
    for track in tracks:
        bpm = bpm_from_name(track.name)
        truth = read_truth_ms(track, args.bin)
        if not bpm or not truth:
            print(f"  skip (no bpm/truth): {track.name}")
            continue
        tol = args.beats * 60_000.0 / bpm
        positions, scores = dc.raw_detections(track, model, processor, dev)
        used += 1
        for s in sens:
            for r in rads:
                pred = [max(0, int(round(t * 1000))) for t in dc.peak_pick(positions, scores, s, r)[: args.max_cues]]
                m = match(pred, truth, tol)
                recall = m["covered_truth"] / len(truth)
                precision = m["matched"] / len(pred) if pred else 0.0
                grid[(s, r)].append((recall, precision, len(pred)))
                if (s, r) == primary:
                    per_track.append((bpm, track.name, recall, precision, len(pred), len(truth)))

    print(f"\nscored {used} track(s) with manual cues\n")
    print(f"{'sens':>5} {'rad':>4} | {'recall':>7} {'prec':>6} {'F1':>6} | {'avg#':>5}")
    print("  " + "-" * 44)
    best = None
    for (s, r), vals in sorted(grid.items()):
        if not vals:
            continue
        rc = statistics.mean(v[0] for v in vals)
        pr = statistics.mean(v[1] for v in vals)
        ct = statistics.mean(v[2] for v in vals)
        f1 = 2 * rc * pr / (rc + pr) if (rc + pr) else 0.0
        print(f"{s:>5} {r:>4} | {rc:>6.0%} {pr:>5.0%} {f1:>5.0%} | {ct:>5.1f}")
        if best is None or f1 > best[0]:
            best = (f1, s, r, rc, pr)
    if best:
        print(f"\nbest F1: -s {best[1]} -r {best[2]}  (recall {best[3]:.0%}, precision {best[4]:.0%})")

    if len(sens) == 1 and len(rads) == 1:
        print(f"\nper-track @ -s {primary[0]} -r {primary[1]} (sorted by bpm):")
        print(f"{'bpm':>4} {'recall':>7} {'prec':>6} {'#pred':>6} {'#man':>5}  track")
        for bpm, name, rc, pr, npred, ntruth in sorted(per_track):
            print(f"{bpm:>4.0f} {rc:>6.0%} {pr:>5.0%} {npred:>6} {ntruth:>5}  {name[:48]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sw = sub.add_parser("sweep", help="cue-count table across a -s/-r grid")
    sw.add_argument("tracks", nargs="+", type=Path)
    sw.add_argument("-c", "--checkpoint", default="disco-eth/cue-detr")
    sw.add_argument("-s", "--sensitivity", default="0.3,0.5,0.7,0.9")
    sw.add_argument("-r", "--radius", default="8,16")
    sw.add_argument("--max-cues", type=int, default=16)
    sw.set_defaults(func=cmd_sweep)

    cp = sub.add_parser("compare", help="score predicted cues vs manual cues")
    cp.add_argument("--pred", type=Path, required=True)
    cp.add_argument("--truth", type=Path, required=True)
    cp.add_argument("--bpm", type=float)
    cp.add_argument("--name", help="filename to parse BPM from, e.g. 'Title (128, C#m)'")
    cp.set_defaults(func=cmd_compare)

    tn = sub.add_parser("tune", help="sweep -s/-r and score each cell vs each track's manual cues")
    tn.add_argument("tracks", nargs="*", type=Path)
    tn.add_argument("--list", help="file of newline-separated track paths")
    tn.add_argument("-c", "--checkpoint", default="disco-eth/cue-detr")
    tn.add_argument("-s", "--sensitivity", default="0.3,0.5,0.7,0.9")
    tn.add_argument("-r", "--radius", default="8,16")
    tn.add_argument("--max-cues", type=int, default=16)
    tn.add_argument("--beats", type=float, default=1.0, help="match tolerance in beats (default 1)")
    tn.add_argument("--bin", default=DEFAULT_BIN, help="path to cue-fighter binary")
    tn.set_defaults(func=cmd_tune)

    sc = sub.add_parser("score", help="score a finished cue dir vs manual cues (position/label/phase; no model)")
    sc.add_argument("tracks", nargs="*", type=Path)
    sc.add_argument("--list", help="file of newline-separated track paths")
    sc.add_argument("--json-dir", type=Path, required=True, help="cues written by detect_cues.py")
    sc.add_argument("--beats", type=float, default=1.0, help="match tolerance in beats (default 1)")
    sc.add_argument("--per-track", action="store_true", help="also print a per-track table")
    sc.add_argument("--bin", default=DEFAULT_BIN, help="path to cue-fighter binary")
    sc.set_defaults(func=cmd_score)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
