import csv
import json
import os
import subprocess
import textwrap
import time
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CWD = Path(r"C:\Users\Administrator\Documents\珠宝白底图生成")
sys.path.insert(0, str(CWD / "scripts"))

from yuan_image_generation_adapter import generate_to_path


RUN_ROOT = CWD / "outputs" / "jewelry-white-background" / "20260623-sensitive-rerun-eval-20260623-2"
PREVIOUS_RUN_ROOT = CWD / "outputs" / "jewelry-white-background" / "20260623-weekly-new-20260623-192003"
RECORDS_JSON = CWD / "records-problem-products.json"

BASE_TOKEN = "V1qGbBZlRatF55sNpSyc4hqMnhg"
TABLE_ID = "tblwckIFBxoEbN7C"
LARK_CLI = r"C:\Users\Administrator\AppData\Roaming\npm\lark-cli.cmd"
PYTHON = Path(r"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
PREPROCESS_SCRIPT = Path(r"C:\Users\Administrator\.codex\skills\jewelry-white-background\scripts\preprocess_reference_images.py")
WATERMARK_SCRIPT = Path(r"C:\Users\Administrator\.codex\skills\yuanyuan-ruyi-watermark\scripts\watermark_images.py")
PROMPT_PATH = Path(r"C:\Users\Administrator\.codex\skills\jewelry-white-background\references\white-background-prompt.md")

PRODUCT_IDS = ["SY1220", "SY1360", "SY1361", "SY1362"]
MAX_DETAIL_IMAGES = 3
PREPROCESS_MAX_EDGE = 1800
PREPROCESS_QUALITY = 92

FIELD_PRODUCT_ID = "产品编号"
FIELD_PRODUCT_NAME = "产品名称"
FIELD_FRONT = "产品正面图"
FIELD_DETAIL = "产品细节图"
FIELD_PARAMS = "产品尺寸&材质说明"

PRODUCT_NOTES = {
    "SY1220": [
        "The white freeform crystal and the white snow phantom beads are separate adjacent components. They must remain visually distinct and must not fuse together.",
        "Keep the irregular outline and thickness of the white freeform crystal exactly as in the reference.",
        "Keep the internal green phantom inclusions visible inside the green phantom bead. Do not clean them away.",
    ],
    "SY1360": [
        "Preserve the internal green phantom inclusions inside the transparent green phantom bead.",
        "The inclusions are part of the gemstone body, not noise, dust, cracks, or artifacts.",
    ],
    "SY1361": [
        "Preserve the internal green phantom inclusions inside the transparent green phantom beads.",
        "Keep the pale green transparent beads separated from the off-white background with clear contour, edge contrast, and internal depth.",
        "Do not over-clean the green areas or flatten the translucency.",
    ],
    "SY1362": [
        "Preserve the green hair-like rutile fibers inside the green hair crystal beads.",
        "Preserve the internal green phantom inclusions inside the transparent green phantom beads.",
        "The fibers and inclusions are product features and must remain clearly visible.",
    ],
}


@dataclass
class ProductRecord:
    record_id: str
    product_id: str
    product_name: str
    params: str
    front: list[dict]
    detail: list[dict]


def safe_name(text: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(text or "").strip())
    return out[:80] or "unnamed"


def rel(path: Path) -> str:
    return os.path.relpath(str(path.resolve()), str(CWD))


def run_cmd(args: list[str], timeout: int = 300) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        cwd=str(CWD),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout


def ensure_dirs() -> None:
    for sub in ["source", "detail", "preprocessed", "generated", "white-bg", "eval", "logs"]:
        (RUN_ROOT / sub).mkdir(parents=True, exist_ok=True)


def load_records() -> list[ProductRecord]:
    data = json.loads(RECORDS_JSON.read_text(encoding="utf-8"))["data"]
    fields = data["fields"]
    wanted = []
    for record_id, row in zip(data["record_id_list"], data["data"]):
        mapped = dict(zip(fields, row))
        product_id = (mapped.get(FIELD_PRODUCT_ID) or "").strip()
        if product_id not in PRODUCT_IDS:
            continue
        wanted.append(
            ProductRecord(
                record_id=record_id,
                product_id=product_id,
                product_name=(mapped.get(FIELD_PRODUCT_NAME) or "").strip(),
                params=(mapped.get(FIELD_PARAMS) or "").strip(),
                front=mapped.get(FIELD_FRONT) if isinstance(mapped.get(FIELD_FRONT), list) else [],
                detail=mapped.get(FIELD_DETAIL) if isinstance(mapped.get(FIELD_DETAIL), list) else [],
            )
        )
    wanted.sort(key=lambda item: PRODUCT_IDS.index(item.product_id))
    return wanted


def download_attachment(record_id: str, file_token: str, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    code, raw = run_cmd(
        [
            LARK_CLI,
            "base",
            "+record-download-attachment",
            "--as",
            "user",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            TABLE_ID,
            "--record-id",
            record_id,
            "--file-token",
            file_token,
            "--output",
            rel(output_path),
            "--overwrite",
        ],
        timeout=240,
    )
    if code != 0 or not output_path.exists():
        raise RuntimeError(raw)
    return output_path


def ext_from_name(name: str, fallback: str = ".jpg") -> str:
    suffix = Path(name or "").suffix.lower()
    return suffix if suffix else fallback


def write_preprocess_queue(rows: list[dict], queue_path: Path) -> None:
    with queue_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["image_path", "product_id", "output_path"])
        writer.writeheader()
        writer.writerows(rows)


def run_preprocess(queue_path: Path) -> dict[str, dict]:
    code, raw = run_cmd(
        [
            str(PYTHON),
            str(PREPROCESS_SCRIPT),
            "--queue",
            str(queue_path),
            "--output-dir",
            str(RUN_ROOT / "preprocessed"),
            "--max-edge",
            str(PREPROCESS_MAX_EDGE),
            "--quality",
            str(PREPROCESS_QUALITY),
        ],
        timeout=900,
    )
    (RUN_ROOT / "logs" / "preprocess_stdout.json").write_text(raw, encoding="utf-8")
    if code != 0:
        raise RuntimeError(raw)
    manifest_path = RUN_ROOT / "preprocessed" / "manifest.jsonl"
    records = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {record["image_path"]: record for record in records}


def build_prompt(base_prompt: str, product: ProductRecord) -> str:
    name_line = product.product_name.strip() or "N/A"
    params_line = product.params.strip() or "N/A"
    notes = PRODUCT_NOTES.get(product.product_id, [])
    note_block = "\n".join(f"- {note}" for note in notes)
    extra = f"""

Reference usage rules:
- Image 1 is the main front reference for overall bracelet structure, bead count, order, spacing, pendant positions, metal parts, and thread path.
- Additional reference images are close-up detail references. Use them to preserve inclusions, phantom layers, hair-like fibers, translucency, irregular outlines, and the boundary between adjacent stones.
- Internal inclusions, phantom material, green hair fibers, white snow patterns, and cloudy layers are product features. They must remain visible and must not be removed as noise reduction.
- Transparent pale green beads must stay distinguishable from the #FBFCF9 background with visible contour and internal depth.
- Do not merge adjacent components together. Do not simplify the gemstone texture. Do not smooth away subtle internal structures.

Product ID: {product.product_id}
Product name: {name_line}
Product parameters:
{params_line}
"""
    if note_block:
        extra += "\nSensitive corrections for this product:\n" + note_block + "\n"
    extra += """
Final output requirements:
- Keep exactly one real bracelet matching the references.
- Preserve all inclusions, fibers, phantom layers, and freeform stone silhouettes.
- Do not invent or remove accessories.
- Maintain a clean e-commerce white background result and natural contact shadow.
"""
    return base_prompt + textwrap.dedent(extra)


def run_watermark(generated_path: Path, product_id: str, output_path: Path) -> None:
    queue_path = RUN_ROOT / "logs" / f"watermark_{product_id}.csv"
    with queue_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["image_path", "product_id", "output_path"])
        writer.writeheader()
        writer.writerow(
            {
                "image_path": str(generated_path),
                "product_id": product_id,
                "output_path": str(output_path),
            }
        )
    code, raw = run_cmd(
        [str(PYTHON), str(WATERMARK_SCRIPT), "--queue", str(queue_path), "--output-dir", str(RUN_ROOT / "white-bg")],
        timeout=600,
    )
    (RUN_ROOT / "logs" / f"watermark_{product_id}.log").write_text(raw, encoding="utf-8")
    if code != 0 or not output_path.exists():
        raise RuntimeError(raw)


def image_panel(image_path: Path | None, title: str, box_size: tuple[int, int]) -> Image.Image:
    panel = Image.new("RGB", box_size, "#f6f6f3")
    draw = ImageDraw.Draw(panel)
    font = ImageFont.load_default()
    title_h = 24
    draw.rectangle((0, 0, box_size[0], title_h), fill="#e7e6df")
    draw.text((8, 7), title, fill="#222222", font=font)
    if image_path and image_path.exists() and image_path.is_file():
        with Image.open(image_path) as src:
            img = src.convert("RGB")
            img.thumbnail((box_size[0] - 12, box_size[1] - title_h - 12))
            x = (box_size[0] - img.width) // 2
            y = title_h + 6 + (box_size[1] - title_h - 12 - img.height) // 2
            panel.paste(img, (x, y))
    else:
        draw.text((8, 40), "missing", fill="#aa3333", font=font)
    return panel


def create_contact_sheet(product_id: str, image_paths: dict[str, Path]) -> Path:
    labels = [
        ("front", image_paths["front"]),
        ("detail1", image_paths["detail"].get(0)),
        ("detail2", image_paths["detail"].get(1)),
        ("detail3", image_paths["detail"].get(2)),
        ("old_gen", image_paths["old_generated"]),
        ("new_gen", image_paths["new_generated"]),
        ("watermarked", image_paths["watermarked"]),
    ]
    box = (460, 560)
    cols = 4
    rows = 2
    sheet = Image.new("RGB", (cols * box[0], rows * box[1] + 34), "white")
    draw = ImageDraw.Draw(sheet)
    draw.rectangle((0, 0, sheet.width, 34), fill="#dfe9df")
    draw.text((12, 10), f"{product_id} comparison", fill="#183018", font=ImageFont.load_default())
    for idx, (label, path) in enumerate(labels):
        panel = image_panel(path, label, box)
        x = (idx % cols) * box[0]
        y = 34 + (idx // cols) * box[1]
        sheet.paste(panel, (x, y))
    out = RUN_ROOT / "eval" / f"{product_id}_contact_sheet.png"
    sheet.save(out, format="PNG")
    return out


def create_overall_sheet(items: list[tuple[str, Path]]) -> Path:
    box = (920, 520)
    sheet = Image.new("RGB", (box[0], len(items) * box[1]), "white")
    for idx, (product_id, path) in enumerate(items):
        panel = image_panel(path, product_id, box)
        sheet.paste(panel, (0, idx * box[1]))
    out = RUN_ROOT / "eval" / "all_contact_sheets_overview.png"
    sheet.save(out, format="PNG")
    return out


def old_generated_path(product_id: str) -> Path:
    return PREVIOUS_RUN_ROOT / "generated" / f"{product_id}_generated_a1.png"


def main() -> int:
    ensure_dirs()
    base_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    records = load_records()
    preprocess_rows = []
    for product in records:
        front = product.front[0]
        front_ext = ext_from_name(front.get("name", ""))
        front_path = RUN_ROOT / "source" / f"{product.product_id}_front{front_ext}"
        download_attachment(product.record_id, front["file_token"], front_path)
        detail_paths = []
        for idx, att in enumerate(product.detail[:MAX_DETAIL_IMAGES], start=1):
            detail_ext = ext_from_name(att.get("name", ""))
            detail_path = RUN_ROOT / "detail" / f"{product.product_id}_detail_{idx}{detail_ext}"
            download_attachment(product.record_id, att["file_token"], detail_path)
            detail_paths.append(detail_path)
        preprocess_rows.append(
            {
                "image_path": str(front_path),
                "product_id": f"{product.product_id}_front",
                "output_path": f"{product.product_id}_front.jpg",
            }
        )
        for idx, detail_path in enumerate(detail_paths, start=1):
            preprocess_rows.append(
                {
                    "image_path": str(detail_path),
                    "product_id": f"{product.product_id}_detail_{idx}",
                    "output_path": f"{product.product_id}_detail_{idx}.jpg",
                }
            )
    queue_path = RUN_ROOT / "logs" / "preprocess_queue.csv"
    write_preprocess_queue(preprocess_rows, queue_path)
    preprocess_map = run_preprocess(queue_path)

    results = []
    overall_items = []
    for product in records:
        downloaded_front = RUN_ROOT / "source" / f"{product.product_id}_front{ext_from_name(product.front[0].get('name', ''))}"
        front_pre = Path(preprocess_map[str(downloaded_front)]["preprocessed_path"])
        detail_pre = []
        detail_downloaded = []
        for idx, att in enumerate(product.detail[:MAX_DETAIL_IMAGES], start=1):
            detail_path = RUN_ROOT / "detail" / f"{product.product_id}_detail_{idx}{ext_from_name(att.get('name', ''))}"
            detail_downloaded.append(detail_path)
            detail_pre.append(Path(preprocess_map[str(detail_path)]["preprocessed_path"]))
        prompt = build_prompt(base_prompt, product)
        prompt_path = RUN_ROOT / "logs" / f"{product.product_id}_prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        generated_path = RUN_ROOT / "generated" / f"{product.product_id}_generated.png"
        watermarked_path = RUN_ROOT / "white-bg" / f"{product.product_id}_white_bg_watermarked.png"
        generation_log_path = RUN_ROOT / "logs" / f"{product.product_id}_generate.json"
        preprocessed_paths = [front_pre] + detail_pre

        if generated_path.exists() and watermarked_path.exists() and generation_log_path.exists():
            generation_payload = json.loads(generation_log_path.read_text(encoding="utf-8"))
            task_id = generation_payload.get("task_id") or "reused-existing-output"
            helper_output_path = generation_payload.get("output", [{}])[0].get("path") or str(generated_path)
        else:
            generation = generate_to_path(
                prompt,
                preprocessed_paths,
                generated_path,
                generation_log_path,
            )
            task_id = generation.task_id
            helper_output_path = str(generation.helper_output)
            run_watermark(generated_path, product.product_id, watermarked_path)

        image_paths = {
            "front": downloaded_front,
            "detail": {idx: path for idx, path in enumerate(detail_downloaded)},
            "old_generated": old_generated_path(product.product_id),
            "new_generated": generated_path,
            "watermarked": watermarked_path,
        }
        contact_sheet = create_contact_sheet(product.product_id, image_paths)
        overall_items.append((product.product_id, contact_sheet))
        result_row = {
            "product_id": product.product_id,
            "record_id": product.record_id,
            "product_name": product.product_name,
            "params": product.params,
            "provider": "wawapi",
            "task_id": task_id,
            "helper_output_path": helper_output_path,
            "output_url": None,
            "downloaded_front": str(downloaded_front),
            "downloaded_detail_paths": [str(path) for path in detail_downloaded],
            "preprocessed_paths": [str(path) for path in preprocessed_paths],
            "old_generated_path": str(old_generated_path(product.product_id)),
            "new_generated_path": str(generated_path),
            "watermarked_path": str(watermarked_path),
            "contact_sheet_path": str(contact_sheet),
            "prompt_path": str(prompt_path),
        }
        results.append(result_row)

    overview_path = create_overall_sheet(overall_items)
    report = {
        "run_root": str(RUN_ROOT),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "products": results,
        "overview_path": str(overview_path),
        "notes": "Local evaluation only. Nothing uploaded back to Base.",
    }
    (RUN_ROOT / "eval" / "eval_manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (RUN_ROOT / "eval" / "eval_summary.md").open("w", encoding="utf-8") as f:
        f.write("# Sensitive rerun eval\n\n")
        f.write("This run is local only and does not upload anything back to Base.\n\n")
        for item in results:
            f.write(f"## {item['product_id']}\n")
            f.write(f"- task_id: `{item['task_id']}`\n")
            f.write(f"- prompt: `{item['prompt_path']}`\n")
            f.write(f"- old generated: `{item['old_generated_path']}`\n")
            f.write(f"- new generated: `{item['new_generated_path']}`\n")
            f.write(f"- watermarked: `{item['watermarked_path']}`\n")
            f.write(f"- contact sheet: `{item['contact_sheet_path']}`\n\n")
        f.write(f"Overview: `{overview_path}`\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
