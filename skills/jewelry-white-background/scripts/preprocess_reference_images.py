#!/usr/bin/env python3
"""Preprocess jewelry reference images for Yuan v4 reference-guided generation.

Valid local reference images are normalized to JPEG. Unsupported or unreadable
files are reported as skipped and must not be submitted to downstream generation.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except Exception as exc:  # pragma: no cover - import failure is environment-specific
    print(json.dumps({"ok": False, "error": f"Pillow is unavailable: {exc}"}, ensure_ascii=False))
    sys.exit(1)

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    PILLOW_HEIF_AVAILABLE = True
except Exception:
    PILLOW_HEIF_AVAILABLE = False

HEIF_CONVERTER = shutil.which("heif-convert")
HEIF_AVAILABLE = PILLOW_HEIF_AVAILABLE or bool(HEIF_CONVERTER)

SUPPORTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}
HEIF_EXTENSIONS = {".heic", ".heif"}


@dataclass
class InputItem:
    image_path: str
    product_id: str = ""
    output_path: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize reference images and skip unsupported files.")
    parser.add_argument("--input", action="append", default=[], help="Local image file, directory, or glob pattern. Repeatable.")
    parser.add_argument("--queue", help="CSV or JSONL queue with image_path, optional product_id and output_path.")
    parser.add_argument("--output-dir", required=True, help="Directory for preprocessed JPEG files and manifest.jsonl.")
    parser.add_argument("--max-edge", type=int, default=1280, help="Resize longest edge to this size if larger. Default: 1280.")
    parser.add_argument("--quality", type=int, default=84, help="JPEG quality. Default: 84.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any item is skipped.")
    return parser.parse_args()


def iter_queue(path: Path) -> Iterable[InputItem]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                image_path = (row.get("image_path") or "").strip()
                if image_path:
                    yield InputItem(image_path, (row.get("product_id") or "").strip(), (row.get("output_path") or "").strip())
    elif suffix in {".jsonl", ".ndjson"}:
        with path.open("r", encoding="utf-8-sig") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                image_path = str(row.get("image_path") or "").strip()
                if image_path:
                    yield InputItem(image_path, str(row.get("product_id") or "").strip(), str(row.get("output_path") or "").strip())
    else:
        raise ValueError(f"Unsupported queue format: {path.suffix}")


def iter_inputs(raw_inputs: list[str], queue: str | None) -> list[InputItem]:
    items: list[InputItem] = []
    if queue:
        items.extend(iter_queue(Path(queue)))

    for raw in raw_inputs:
        path = Path(raw)
        if any(ch in raw for ch in "*?["):
            matches = sorted(Path().glob(raw))
            items.extend(InputItem(str(match)) for match in matches if match.is_file())
        elif path.is_dir():
            for match in sorted(path.rglob("*")):
                if match.is_file():
                    items.append(InputItem(str(match)))
        else:
            items.append(InputItem(str(path)))
    return items


def safe_stem(path: Path, index: int, product_id: str) -> str:
    base = product_id.strip() or path.stem or f"image-{index:04d}"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in base)
    return safe[:80] or f"image-{index:04d}"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(2, 10000):
        candidate = path.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to create unique output path for {path}")


def skip_record(item: InputItem, reason: str, index: int) -> dict:
    return {
        "status": "skipped",
        "reason": reason,
        "image_path": item.image_path,
        "product_id": item.product_id,
        "index": index,
    }


def preprocess_one(item: InputItem, output_dir: Path, index: int, max_edge: int, quality: int) -> dict:
    src = Path(item.image_path)
    if not src.exists() or not src.is_file():
        return skip_record(item, "file not found", index)

    ext = src.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return skip_record(item, f"unsupported extension: {ext or '<none>'}", index)

    if ext in HEIF_EXTENSIONS and not HEIF_AVAILABLE:
        return skip_record(item, "HEIC/HEIF decoder unavailable", index)

    temporary_dir: tempfile.TemporaryDirectory[str] | None = None
    image_source = src
    heif_decoder: str | None = None
    if ext in HEIF_EXTENSIONS:
        if PILLOW_HEIF_AVAILABLE:
            heif_decoder = "pillow-heif"
        else:
            temporary_dir = tempfile.TemporaryDirectory(prefix="jewelry-heif-")
            image_source = Path(temporary_dir.name) / "converted.jpg"
            completed = subprocess.run(
                [str(HEIF_CONVERTER), "--quiet", str(src), str(image_source)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode != 0 or not image_source.is_file():
                temporary_dir.cleanup()
                return skip_record(
                    item,
                    f"HEIC/HEIF conversion failed: {(completed.stderr or completed.stdout).strip()}",
                    index,
                )
            heif_decoder = "heif-convert"

    try:
        with Image.open(image_source) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                img = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                img = img.convert("RGB")

            if max_edge > 0:
                longest = max(img.size)
                if longest > max_edge:
                    scale = max_edge / longest
                    new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)

            explicit_output = Path(item.output_path) if item.output_path else None
            if explicit_output:
                out_path = explicit_output if explicit_output.suffix else explicit_output.with_suffix(".jpg")
                if not out_path.is_absolute():
                    out_path = output_dir / out_path
            else:
                out_path = output_dir / f"{safe_stem(src, index, item.product_id)}.jpg"
            out_path = unique_path(out_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path, format="JPEG", quality=quality, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return skip_record(item, f"cannot decode image: {exc}", index)
    finally:
        if temporary_dir is not None:
            temporary_dir.cleanup()

    return {
        "status": "ok",
        "image_path": str(src),
        "preprocessed_path": str(out_path),
        "product_id": item.product_id,
        "index": index,
        "source_extension": ext,
        "heif_decoder": heif_decoder,
    }


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        items = iter_inputs(args.input, args.queue)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    if not items:
        print(json.dumps({"ok": False, "error": "no input images"}, ensure_ascii=False))
        return 1

    records = [preprocess_one(item, output_dir, i + 1, args.max_edge, args.quality) for i, item in enumerate(items)]
    manifest = output_dir / "manifest.jsonl"
    with manifest.open("w", encoding="utf-8", newline="") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    ok_count = sum(1 for record in records if record["status"] == "ok")
    skipped_count = len(records) - ok_count
    summary = {
        "ok": skipped_count == 0,
        "total": len(records),
        "ready": ok_count,
        "skipped": skipped_count,
        "manifest": str(manifest),
        "records": records,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.strict and skipped_count:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
