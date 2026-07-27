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
#   "accelerate",
# ]
# ///
"""Fine-tune CUE-DETR on your own cues (single-class positions).

Continues training from the `disco-eth/cue-detr` checkpoint — which already
knows "a cue point looks like this on a spectrogram" — on the dataset built by
prepare_dataset.py, so it adapts to *your* placement style. Single-class: every
box is category `cue`; the label family is ignored here (that's a later run).

    uv run train_cues.py --data dataset/ --out runs/ft1 [--epochs 8] [--freeze-backbone]
    # quick sanity pass to measure speed + confirm loss drops:
    uv run train_cues.py --data dataset/ --out runs/trial --max-train 300 --epochs 1

Device auto-selects cuda -> mps -> cpu. On MPS, a CPU fallback is enabled for
the few ops MPS lacks. Saves the fine-tuned model to --out for use with
`detect_cues.py -c <out>`.
"""

import argparse
import json
import math
import os
from pathlib import Path

import torch
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from transformers import DetrForObjectDetection, DetrImageProcessor

# a handful of ops (e.g. some grid/interpolate paths) aren't implemented on MPS;
# let them fall back to CPU instead of crashing mid-epoch.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")


def device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class CocoCueDataset(Dataset):
    """Reads the modified-COCO dump from prepare_dataset.py and formats each
    slice for DETR via DetrImageProcessor (COCO detection annotations)."""

    def __init__(self, data_dir: Path, split: str, processor: DetrImageProcessor, limit: int | None = None):
        self.dir = data_dir / split
        self.processor = processor
        coco = json.loads((data_dir / f"{split}.json").read_text())
        self.images = coco["images"]
        if limit:
            self.images = self.images[:limit]
        keep = {im["id"] for im in self.images}
        self.by_image: dict[int, list] = {im["id"]: [] for im in self.images}
        for a in coco["annotations"]:
            if a["image_id"] in keep:
                self.by_image[a["image_id"]].append(a)

    def __len__(self) -> int:
        return len(self.images)

    def __getitem__(self, i: int):
        im = self.images[i]
        img = Image.open(self.dir / im["file_name"]).convert("RGB")
        anns = [{"image_id": im["id"], "category_id": 0, "bbox": a["bbox"],
                 "area": a["area"], "iscrowd": 0} for a in self.by_image[im["id"]]]
        enc = self.processor(images=img, annotations={"image_id": im["id"], "annotations": anns},
                             do_resize=False, return_tensors="pt")
        return {"pixel_values": enc["pixel_values"][0], "labels": enc["labels"][0]}


def collate(batch):
    return (
        torch.stack([b["pixel_values"] for b in batch]),
        [b["labels"] for b in batch],
    )


def to_device(labels, dev):
    """Move each COCO label dict's tensors onto the compute device."""
    out = []
    for lab in labels:
        out.append({k: (v.to(dev) if torch.is_tensor(v) else v) for k, v in lab.items()})
    return out


@torch.no_grad()
def eval_loss(model, loader, dev) -> float:
    model.eval()
    total, n = 0.0, 0
    for pixel_values, labels in loader:
        out = model(pixel_values=pixel_values.to(dev), labels=to_device(labels, dev))
        total += float(out.loss); n += 1
    return total / max(n, 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--checkpoint", default="disco-eth/cue-detr")
    ap.add_argument("--epochs", type=float, default=8)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--freeze-backbone", action="store_true", help="freeze the ResNet backbone (faster, less overfit)")
    ap.add_argument("--max-train", type=int, help="cap train slices (for a quick trial)")
    ap.add_argument("--max-val", type=int, help="cap val slices")
    ap.add_argument("--patience", type=int, default=3, help="stop after N epochs with no val improvement")
    args = ap.parse_args()

    dev = device()
    print(f"device: {dev}  |  continuing from {args.checkpoint}")
    processor = DetrImageProcessor.from_pretrained("facebook/detr-resnet-50")
    model = DetrForObjectDetection.from_pretrained(args.checkpoint).to(dev)
    if args.freeze_backbone:
        for p in model.model.backbone.parameters():
            p.requires_grad = False

    train_ds = CocoCueDataset(args.data, "train", processor, args.max_train)
    val_ds = CocoCueDataset(args.data, "val", processor, args.max_val)
    print(f"train slices {len(train_ds)}  val slices {len(val_ds)}")
    train_loader = DataLoader(train_ds, batch_size=args.batch, shuffle=True, collate_fn=collate, num_workers=2)
    val_loader = DataLoader(val_ds, batch_size=args.batch, shuffle=False, collate_fn=collate, num_workers=2)

    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=args.lr, weight_decay=1e-4)
    total_steps = max(1, int(math.ceil(len(train_loader) * args.epochs)))
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=total_steps)

    args.out.mkdir(parents=True, exist_ok=True)
    best = float("inf")
    since_best = 0
    step = 0
    n_epochs = int(math.ceil(args.epochs))
    for epoch in range(n_epochs):
        model.train()
        for pixel_values, labels in train_loader:
            if step >= total_steps:
                break
            opt.zero_grad()
            out = model(pixel_values=pixel_values.to(dev), labels=to_device(labels, dev))
            out.loss.backward()
            torch.nn.utils.clip_grad_norm_(params, 0.1)
            opt.step(); sched.step(); step += 1
            if step % 50 == 0:
                print(f"  epoch {epoch} step {step}/{total_steps}  loss {float(out.loss):.3f}  lr {sched.get_last_lr()[0]:.2e}", flush=True)
        vloss = eval_loss(model, val_loader, dev)
        print(f"epoch {epoch}: val_loss {vloss:.3f}  (best {best:.3f})", flush=True)
        if vloss < best - 1e-4:
            best = vloss
            since_best = 0
            model.save_pretrained(str(args.out))
            processor.save_pretrained(str(args.out))
            print(f"  saved new best -> {args.out}", flush=True)
        else:
            since_best += 1
            if since_best >= args.patience:
                print(f"  early stop: no improvement for {args.patience} epochs", flush=True)
                break

    print(f"\nbest val_loss {best:.3f}  |  model -> {args.out}")
    print(f"evaluate it with:  uv run detect_cues.py <track> -c {args.out} -o cues_ft/")


if __name__ == "__main__":
    main()
