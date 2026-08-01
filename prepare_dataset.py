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
"""Build a fine-tuning dataset for CUE-DETR from your own manual cues.

For each track we render the mel-spectrogram (exactly as detect_cues does, so the
image distribution matches what the model was trained on), read its embedded
manual cues, and cut a 355px-wide slice centred on each cue. Every cue that falls
inside a slice becomes a full-height box — the modified-COCO target CUE-DETR
expects. Slices are the positive training examples; DETR's set-prediction learns
"no cue here" from the unmatched object queries within each slice.

    uv run prepare_dataset.py --list tracks.txt -o dataset/ [--preview 8]

Output:
    dataset/train/*.png, dataset/val/*.png
    dataset/train.json, dataset/val.json    (COCO: images + annotations[bbox,category_id,family])
    dataset/preview/*.png                    (slices with boxes drawn, for eyeballing)

Split is BY TRACK (--val-frac) so no slice from a val track leaks into train.
Each annotation keeps a `family` field (INTRO/DROP/...) so a later multi-class
run needs no re-prep; single-class training just ignores it.
"""

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import detect_cues as dc

W_SLICE = dc.W_WIN          # 355 — must match the model's training/inference width
H = 128
HOP = 512                   # librosa melspectrogram default (n_fft//4) — sets the time->pixel map
DEFAULT_BIN = str(Path(__file__).resolve().parent / "target" / "release" / "cue-fighter")


def norm_family(label: str | None) -> str:
    import re
    if not label:
        return "CUE"
    return re.sub(r"\s*\d+\s*$", "", label.strip().upper()) or "CUE"


# Canonical structural vocabulary. `canon_family` collapses a messy label to one
# of these by matching the first rule whose pattern appears in the label — so
# "DROP 1", "DROP (LOW)", "3 DROP" -> DROP and "CHORUS ohhh lyrics" -> CHORUS.
# Anything with no structural keyword (ENERGY, freeform lyrics, unlabelled) -> None
# and is dropped. Multi-word families are listed before their substrings so
# "MIX IN" beats "MIX" and "PRE-DROP" beats "DROP".
CANON_RULES = [
    ("MIX IN", ("MIX IN", "MIXIN")),
    ("MIX OUT", ("MIX OUT", "MIXOUT")),
    ("PRE-DROP", ("PRE-DROP", "PRE DROP", "PREDROP")),
    ("PRE-CHORUS", ("PRE-CHORUS", "PRE CHORUS")),
    ("POST-CHORUS", ("POST-CHORUS", "POST CHORUS")),
    ("DRUM ROLL", ("DRUM ROLL", "DRUMROLL")),
    ("BREAKDOWN", ("BREAKDOWN",)),
    ("DROP", ("DROP",)),
    ("CUT", ("CUT",)),
    ("INTRO", ("INTRO",)),
    ("OUTRO", ("OUTRO",)),
    ("BUILD", ("BUILD",)),
    ("BREAK", ("BREAK",)),
    ("CHORUS", ("CHORUS",)),
    ("VERSE", ("VERSE",)),
    ("VOCAL", ("VOCAL",)),
    ("BRIDGE", ("BRIDGE",)),
    ("HOOK", ("HOOK",)),
    ("MIX", ("MIX",)),
    ("PRE", ("PRE",)),
    ("DRUM", ("DRUM", "DRUMS")),
    ("RISER", ("RISER",)),
    ("MELODY", ("MELODY",)),
    ("PHRASE", ("PHRASE",)),
    ("BASS", ("BASS",)),
    ("SNARE", ("SNARE",)),
    ("KICK", ("KICK",)),
    ("SYNTH", ("SYNTH",)),
    ("CYMBAL", ("CYMBAL",)),
    ("BEAT", ("BEAT",)),
    ("RHYTHM", ("RHYTHM",)),
]


def canon_family(family: str) -> str | None:
    """Collapse a label to its canonical structural family, or None if it uses no
    structural vocabulary (dropped under --keep-standard)."""
    u = family.upper()
    for canon, pats in CANON_RULES:
        if any(p in u for p in pats):
            return canon
    return None


def read_manual(track: Path, binp: str) -> list[tuple[int, str]]:
    """(ms, family) for a track's embedded cues, sorted by time."""
    r = subprocess.run([binp, "read", str(track)], capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        return []
    cues = json.loads(r.stdout or "{}").get("cues", [])
    return sorted((int(c["ms"]), norm_family(c.get("label"))) for c in cues)


def cue_x_pixels(cues_ms: list[int]) -> np.ndarray:
    """Cue time (ms) -> x pixel (spectrogram frame index). Inverse of the
    frames_to_time detect_cues uses for output, so positions round-trip."""
    return np.asarray([int(round(ms / 1000 * dc.SR / HOP)) for ms in cues_ms])


def extract_slice(img: np.ndarray, left: int) -> np.ndarray:
    """A 355-wide slice starting at `left`. The valid overlap with the image is
    copied onto a canvas; any part hanging off either end is edge-replicated.
    Robust for any `left`, including slices entirely past the track end."""
    h, w, _ = img.shape
    out = np.zeros((h, W_SLICE, 3), dtype=img.dtype)
    src_l, src_r = max(0, left), min(w, left + W_SLICE)
    if src_r <= src_l:                       # no overlap: replicate nearest edge column
        col = img[:, :1] if left < 0 else img[:, -1:]
        return np.repeat(col, W_SLICE, axis=1)
    dst_l = src_l - left
    dst_r = dst_l + (src_r - src_l)
    out[:, dst_l:dst_r] = img[:, src_l:src_r]
    if dst_l > 0:
        out[:, :dst_l] = out[:, dst_l:dst_l + 1]   # left pad = first real column
    if dst_r < W_SLICE:
        out[:, dst_r:] = out[:, dst_r - 1:dst_r]   # right pad = last real column
    return out


def box_for(pos_local: int, w_box: int) -> list[float]:
    """COCO [x,y,w,h] in absolute slice pixels: a full-height box centred at pos."""
    x = max(0.0, min(float(W_SLICE - w_box), pos_local - w_box / 2))
    return [x, 0.0, float(w_box), float(H)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tracks", nargs="*", type=Path)
    ap.add_argument("--list", help="file of newline-separated track paths")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--bin", default=DEFAULT_BIN)
    ap.add_argument("--w-box", type=int, default=20, help="target box width in pixels")
    ap.add_argument("--jitter", type=float, default=0.30, help="max slice-centre jitter as fraction of width")
    ap.add_argument("--exclude-labels", default="",
                    help="comma-separated cue families to drop entirely (e.g. ENERGY = Mixed In Key's "
                         "auto markers, which aren't your placements). Dropped cues seed no slice and "
                         "appear in no box.")
    ap.add_argument("--keep-standard", action="store_true",
                    help="keep ONLY cues whose label uses the structural vocabulary (DROP/CUT/INTRO/"
                         "BUILD/BREAK/MIX/CHORUS/VERSE/VOCAL/...). Drops ENERGY, unlabelled cues, and "
                         "freeform lyric cues in one pass.")
    ap.add_argument("--val-frac", type=float, default=0.10, help="fraction of TRACKS held out for validation")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--preview", type=int, default=8, help="how many annotated slices to render for eyeballing")
    ap.add_argument("--min-cues", type=int, default=4, help="skip tracks with fewer manual cues")
    args = ap.parse_args()

    tracks = list(args.tracks)
    if args.list:
        tracks += [Path(l) for l in Path(args.list).read_text().splitlines() if l.strip()]

    rng = np.random.default_rng(args.seed)
    # deterministic per-track val split
    order = sorted(tracks, key=lambda p: p.name)
    val_set = set(rng.choice(len(order), size=max(1, int(len(order) * args.val_frac)), replace=False).tolist())

    for split in ("train", "val", "preview"):
        (args.out / split).mkdir(parents=True, exist_ok=True)

    coco = {s: {"images": [], "annotations": [],
                "categories": [{"id": 0, "name": "cue"}]} for s in ("train", "val")}
    img_id = ann_id = 0
    n_prev = 0
    stats = {"tracks": 0, "skipped": 0, "slices": 0, "boxes": 0}

    exclude = {x.strip().upper() for x in args.exclude_labels.split(",") if x.strip()}
    dropped_fams: Counter = Counter()
    for ti, track in enumerate(order):
        cues = read_manual(track, args.bin)
        if exclude:
            cues = [(ms, fam) for ms, fam in cues if fam not in exclude]
        if args.keep_standard:
            kept = []
            for ms, fam in cues:
                canon = canon_family(fam)
                if canon is None:
                    dropped_fams[fam] += 1
                else:
                    kept.append((ms, canon))   # store the collapsed family
            cues = kept
        if len(cues) < args.min_cues:
            stats["skipped"] += 1
            continue
        try:
            y = dc.load_audio(track)                     # a few library files are
            img = dc.spectrogram_image(y)                # corrupt / absurdly long
        except Exception as e:
            print(f"  skip (load failed): {track.name}: {type(e).__name__}: {e}")
            stats["skipped"] += 1
            continue
        xs = cue_x_pixels([ms for ms, _ in cues])
        fams = [f for _, f in cues]
        split = "val" if ti in val_set else "train"
        stats["tracks"] += 1

        for ci, cx in enumerate(xs):
            try:
                jit = int(rng.uniform(-args.jitter, args.jitter) * W_SLICE)
                left = int(cx) - W_SLICE // 2 + jit
                seg = extract_slice(img, left)
                # every cue landing inside this slice becomes a box
                boxes = []
                for xj, fam in zip(xs, fams):
                    p = int(xj) - left
                    if 0 <= p < W_SLICE:
                        boxes.append((p, fam))
                if not boxes:
                    continue                              # (shouldn't happen: the centre cue is in-slice)
                fn = f"{track.stem[:60]}_{ci}.png".replace("/", "_")
                Image.fromarray(seg).save(args.out / split / fn)
                coco[split]["images"].append({"id": img_id, "file_name": fn, "width": W_SLICE, "height": H})
                for p, fam in boxes:
                    bb = box_for(p, args.w_box)
                    coco[split]["annotations"].append({
                        "id": ann_id, "image_id": img_id, "category_id": 0,
                        "bbox": bb, "area": bb[2] * bb[3], "iscrowd": 0, "family": fam,
                    })
                    ann_id += 1
                    stats["boxes"] += 1
                img_id += 1
                stats["slices"] += 1

                # a few annotated previews so box placement can be eyeballed
                if n_prev < args.preview and split == "train":
                    prev = Image.fromarray(seg.copy())
                    d = ImageDraw.Draw(prev)
                    for p, fam in boxes:
                        bb = box_for(p, args.w_box)
                        d.rectangle([bb[0], bb[1], bb[0] + bb[2], bb[1] + bb[3] - 1], outline=(255, 0, 0), width=2)
                        d.text((bb[0] + 2, 2), fam[:4], fill=(255, 255, 255))
                    prev.save(args.out / "preview" / f"{n_prev:02d}_{fn}")
                    n_prev += 1
            except Exception as e:
                print(f"  skip slice {track.name}#{ci}: {type(e).__name__}: {e}")
                continue

        if (ti + 1) % 25 == 0:
            print(f"  {ti + 1}/{len(order)} tracks  ({stats['slices']} slices, {stats['boxes']} boxes)")

    for s in ("train", "val"):
        (args.out / f"{s}.json").write_text(json.dumps(coco[s]))
    print(f"\ntracks used {stats['tracks']}  skipped {stats['skipped']}")
    print(f"train: {len(coco['train']['images'])} slices / {len(coco['train']['annotations'])} boxes")
    print(f"val:   {len(coco['val']['images'])} slices / {len(coco['val']['annotations'])} boxes")
    if args.keep_standard:
        kept_fams = Counter(a["family"] for s in ("train", "val") for a in coco[s]["annotations"])
        print(f"\nkept families (canonical): {dict(kept_fams.most_common())}")
        n_drop = sum(dropped_fams.values())
        print(f"dropped {n_drop} non-standard cues; top: "
              + ", ".join(f"{f}({n})" for f, n in dropped_fams.most_common(10)))
    print(f"previews -> {args.out / 'preview'}")


if __name__ == "__main__":
    main()
