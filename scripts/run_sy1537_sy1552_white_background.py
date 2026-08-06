from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.yuan_image_generation_adapter import generate_to_path


TARGET_PRODUCT_IDS = tuple(f"SY{number}" for number in range(1537, 1553))
BASE_TOKEN = "V1qGbBZlRatF55sNpSyc4hqMnhg"
TABLE_ID = "tblwckIFBxoEbN7C"
FIELD_PRODUCT_ID = "fldR6BjwZG"
FIELD_FRONT = "fldvutIHmd"
FIELD_PRODUCT_IMAGES = "fld2RD0sOP"
FIELD_SIDE = "fldLlMicXn"
FIELD_MAIN = "fldEcAYBlX"
FIELD_IDS = (
    FIELD_PRODUCT_ID,
    FIELD_FRONT,
    FIELD_PRODUCT_IMAGES,
    FIELD_SIDE,
    FIELD_MAIN,
)

DEFAULT_RUN_ROOT = (
    PROJECT_ROOT
    / "outputs"
    / "jewelry-white-background"
    / "base-sy1537-sy1552-20260806"
)
SKILL_ROOT = PROJECT_ROOT / "skills" / "jewelry-white-background"
PREPROCESS_SCRIPT = SKILL_ROOT / "scripts" / "preprocess_reference_images.py"
VALIDATE_PLAN_SCRIPT = SKILL_ROOT / "scripts" / "validate_reference_plan.py"
BUILD_PROMPT_SCRIPT = SKILL_ROOT / "scripts" / "build_white_background_prompt.py"
EVALUATE_SCRIPT = SKILL_ROOT / "scripts" / "evaluate_white_background.py"
WATERMARK_SCRIPT = (
    Path.home()
    / ".codex"
    / "skills"
    / "yuanyuan-ruyi-watermark"
    / "scripts"
    / "watermark_images.py"
)
LARK_CLI = Path.home() / "AppData" / "Roaming" / "npm" / "lark-cli.ps1"
PROMPT_VERSION = "v2.3-yuan-v4"
AUTHORIZATION_REFERENCE = (
    "用户于 2026-08-06 明确要求将合格水印白底图追加到对应主图并执行。"
)
IMAGE_SUFFIXES = {
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


@dataclass(frozen=True)
class Attachment:
    token: str
    name: str
    size: int = 0


@dataclass(frozen=True)
class ProductRecord:
    product_id: str
    record_id: str
    front_images: tuple[Attachment, ...]
    product_images: tuple[Attachment, ...]
    side_images: tuple[Attachment, ...]
    main_images: tuple[Attachment, ...]


@dataclass(frozen=True)
class PreparedProduct:
    product_id: str
    product_root: Path
    reference_images: tuple[Path, ...]


@dataclass(frozen=True)
class UploadPlan:
    action: str
    existing: Attachment | None = None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return payload


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _command_text(command: Sequence[str]) -> str:
    return " ".join(str(part) for part in command)


def run_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    allowed_returncodes: Iterable[int] = (0,),
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode not in set(allowed_returncodes):
        raise RuntimeError(
            f"命令执行失败（{completed.returncode}）：{_command_text(command)}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    return completed


def _parse_json_output(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    raw = (completed.stdout or completed.stderr).lstrip("\ufeff").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"命令未返回有效 JSON：{raw[:1000]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("命令返回的 JSON 根节点不是对象")
    return payload


def _lark_command(args: Sequence[str]) -> list[str]:
    if not LARK_CLI.is_file():
        raise FileNotFoundError(f"未找到 lark-cli：{LARK_CLI}")
    return [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(LARK_CLI),
        *[str(item) for item in args],
    ]


def run_lark(args: Sequence[str], attempts: int = 5) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None
    for attempt in range(attempts):
        completed = run_command(
            _lark_command(args),
            cwd=PROJECT_ROOT,
            allowed_returncodes=range(0, 256),
        )
        payload = _parse_json_output(completed)
        last_payload = payload
        if completed.returncode == 0 and payload.get("ok") is True:
            return payload
        error_code = payload.get("error", {}).get("code")
        if error_code not in {800004135, 1254291} or attempt == attempts - 1:
            raise RuntimeError(json.dumps(payload, ensure_ascii=False))
        time.sleep(min(2 ** (attempt + 1), 8))
    raise RuntimeError(json.dumps(last_payload, ensure_ascii=False))


def _attachments(value: Any) -> tuple[Attachment, ...]:
    if not isinstance(value, list):
        return ()
    result: list[Attachment] = []
    for item in value:
        if not isinstance(item, dict) or not item.get("file_token"):
            continue
        result.append(
            Attachment(
                token=str(item["file_token"]),
                name=str(item.get("name") or f"{item['file_token']}.bin"),
                size=int(item.get("size") or 0),
            )
        )
    return tuple(result)


def _field_value(data: dict[str, Any], row: Sequence[Any], field_id: str) -> Any:
    field_ids = data.get("field_id_list")
    if not isinstance(field_ids, list) or field_id not in field_ids:
        return None
    index = field_ids.index(field_id)
    return row[index] if index < len(row) else None


def parse_record_search_payload(
    payload: dict[str, Any], expected_product_id: str
) -> ProductRecord:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("记录搜索结果缺少 data")
    rows = data.get("data") or []
    record_ids = data.get("record_id_list") or []
    matches: list[ProductRecord] = []
    for record_id, row in zip(record_ids, rows):
        if not isinstance(row, list):
            continue
        product_id = str(_field_value(data, row, FIELD_PRODUCT_ID) or "").strip()
        if product_id != expected_product_id:
            continue
        matches.append(
            ProductRecord(
                product_id=product_id,
                record_id=str(record_id),
                front_images=_attachments(_field_value(data, row, FIELD_FRONT)),
                product_images=_attachments(
                    _field_value(data, row, FIELD_PRODUCT_IMAGES)
                ),
                side_images=_attachments(_field_value(data, row, FIELD_SIDE)),
                main_images=_attachments(_field_value(data, row, FIELD_MAIN)),
            )
        )
    if len(matches) != 1:
        raise ValueError(
            f"{expected_product_id} 必须精确匹配 1 条记录，实际为 {len(matches)} 条"
        )
    return matches[0]


def find_target_records(
    product_ids: Sequence[str] = TARGET_PRODUCT_IDS,
) -> list[ProductRecord]:
    records: list[ProductRecord] = []
    for product_id in product_ids:
        payload = run_lark(
            [
                "base",
                "+record-search",
                "--base-token",
                BASE_TOKEN,
                "--table-id",
                TABLE_ID,
                "--keyword",
                product_id,
                "--search-field",
                FIELD_PRODUCT_ID,
                *sum((["--field-id", field_id] for field_id in FIELD_IDS), []),
                "--limit",
                "10",
                "--format",
                "json",
                "--as",
                "user",
            ]
        )
        records.append(parse_record_search_payload(payload, product_id))
    if tuple(record.product_id for record in records) != tuple(product_ids):
        raise ValueError("目标记录顺序或编号范围异常")
    return records


def _is_image_attachment(attachment: Attachment) -> bool:
    return Path(attachment.name).suffix.lower() in IMAGE_SUFFIXES


def select_reference_attachments(record: ProductRecord) -> list[Attachment]:
    front = next(
        (item for item in record.front_images if _is_image_attachment(item)),
        None,
    )
    if front is None:
        return []
    ordered = [front, *record.product_images, *record.side_images]
    selected: list[Attachment] = []
    seen: set[str] = set()
    for item in ordered:
        if item.token in seen or not _is_image_attachment(item):
            continue
        selected.append(item)
        seen.add(item.token)
        if len(selected) == 5:
            break
    return selected


def plan_upload(
    before: Sequence[Attachment], target_filename: str
) -> UploadPlan:
    matches = [item for item in before if item.name == target_filename]
    if len(matches) > 1:
        return UploadPlan(action="conflict", existing=matches[0])
    if matches:
        return UploadPlan(action="already_present", existing=matches[0])
    return UploadPlan(action="upload")


def verify_append(
    before: Sequence[Attachment], after: Sequence[Attachment], new_token: str
) -> bool:
    before_tokens = {item.token for item in before}
    after_tokens = [item.token for item in after]
    return before_tokens.issubset(set(after_tokens)) and after_tokens.count(new_token) == 1


def workspace_relative_output(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"下载目标必须位于项目工作区内：{path}") from exc
    return relative.as_posix()


def _download_attachment(
    record: ProductRecord,
    attachment: Attachment,
    target: Path,
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        return target
    payload = run_lark(
        [
            "base",
            "+record-download-attachment",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            TABLE_ID,
            "--record-id",
            record.record_id,
            "--file-token",
            attachment.token,
            "--output",
            workspace_relative_output(target),
            "--overwrite",
            "--format",
            "json",
            "--as",
            "user",
        ]
    )
    if target.is_file() and target.stat().st_size > 0:
        return target
    candidates = [target.parent / attachment.name]
    data = payload.get("data")
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, str):
                candidates.append(Path(value))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        for key in ("path", "output", "local_path"):
                            if item.get(key):
                                candidates.append(Path(str(item[key])))
    for candidate in candidates:
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        if candidate.is_file() and candidate.stat().st_size > 0:
            if candidate.resolve() != target.resolve():
                shutil.copy2(candidate, target)
            return target
    raise FileNotFoundError(
        f"附件下载后未找到文件：{record.product_id} {attachment.token}"
    )


def _run_preprocess(
    product_id: str,
    product_root: Path,
    source_paths: Sequence[Path],
) -> tuple[Path, ...]:
    queue_path = product_root / "manifests" / "preprocess_queue.jsonl"
    rows: list[dict[str, str]] = []
    for index, source_path in enumerate(source_paths, start=1):
        role = "front" if index == 1 else "detail"
        output_path = product_root / "preprocessed" / f"{index:02d}_{role}.jpg"
        rows.append(
            {
                "image_path": str(source_path),
                "product_id": f"{product_id}_{index:02d}_{role}",
                "output_path": str(output_path),
            }
        )
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    completed = run_command(
        [
            sys.executable,
            str(PREPROCESS_SCRIPT),
            "--queue",
            str(queue_path),
            "--output-dir",
            str(product_root / "preprocessed"),
            "--strict",
        ]
    )
    payload = _parse_json_output(completed)
    records = payload.get("records")
    if not isinstance(records, list) or len(records) != len(source_paths):
        raise RuntimeError(f"{product_id} 预处理结果数量不一致")
    ready = tuple(
        Path(str(item["preprocessed_path"]))
        for item in sorted(records, key=lambda item: int(item.get("index") or 0))
        if item.get("status") == "ok"
    )
    if len(ready) != len(source_paths):
        raise RuntimeError(f"{product_id} 存在无法使用的参考图")
    return ready


def _create_contact_sheet(
    images: Sequence[tuple[Path, str]],
    output_path: Path,
) -> None:
    from PIL import Image, ImageDraw, ImageOps

    cell_width = 720
    cell_height = 620
    columns = 2
    rows = (len(images) + columns - 1) // columns
    canvas = Image.new("RGB", (cell_width * columns, cell_height * rows), "white")
    draw = ImageDraw.Draw(canvas)
    for index, (image_path, label) in enumerate(images):
        with Image.open(image_path) as source:
            source = ImageOps.exif_transpose(source).convert("RGB")
            thumb = ImageOps.contain(source, (cell_width - 40, cell_height - 80))
        x = (index % columns) * cell_width + (cell_width - thumb.width) // 2
        y = (index // columns) * cell_height + 50
        canvas.paste(thumb, (x, y))
        draw.text(((index % columns) * cell_width + 20, (index // columns) * cell_height + 18), label, fill="black")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="JPEG", quality=90, optimize=True)


def _draft_reference_plan(
    product_id: str,
    product_root: Path,
    references: Sequence[Path],
) -> dict[str, Any]:
    relative = [str(path.relative_to(product_root)).replace("\\", "/") for path in references]
    return {
        "schema_version": "2.0",
        "product_id": product_id,
        "front_image": relative[0],
        "detail_images": relative[1:],
        "structure": {
            "bead_sequence": "",
            "thread": "",
            "special_components": [],
        },
        "material_observations": [],
        "composition": {
            "width_ratio_min": 0.45,
            "width_ratio_max": 0.55,
            "max_center_offset_ratio": 0.08,
            "require_full_product": True,
        },
        "manual_review_items": [
            "逐颗核对结构与材质",
            "确认特殊件和串线与正面图一致",
            "确认水印不遮挡产品",
        ],
        "preparation_status": "needs_manual_observation",
    }


def prepare_product(record: ProductRecord, run_root: Path) -> PreparedProduct:
    selected = select_reference_attachments(record)
    if not selected:
        raise ValueError(f"{record.product_id} 缺少可用的产品正面图")
    product_root = run_root / record.product_id
    for name in (
        "source",
        "detail",
        "preprocessed",
        "generated",
        "white-bg",
        "logs",
        "qc",
        "manifests",
    ):
        (product_root / name).mkdir(parents=True, exist_ok=True)

    source_paths: list[Path] = []
    selected_context: list[dict[str, Any]] = []
    for index, attachment in enumerate(selected, start=1):
        role = "front" if index == 1 else "detail"
        suffix = Path(attachment.name).suffix.lower() or ".bin"
        target_dir = product_root / ("source" if index == 1 else "detail")
        target = target_dir / f"{index:02d}_{role}_{attachment.token}{suffix}"
        downloaded = _download_attachment(record, attachment, target)
        source_paths.append(downloaded)
        selected_context.append(
            {
                **asdict(attachment),
                "role": role,
                "local_path": str(downloaded.relative_to(product_root)).replace("\\", "/"),
            }
        )

    references = _run_preprocess(record.product_id, product_root, source_paths)
    for item, reference in zip(selected_context, references):
        item["preprocessed_path"] = str(reference.relative_to(product_root)).replace("\\", "/")

    context = {
        "product_id": record.product_id,
        "record_id": record.record_id,
        "prepared_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "selected_references": selected_context,
        "existing_main_images": [asdict(item) for item in record.main_images],
    }
    write_json(product_root / "product_context.json", context)
    draft_path = product_root / "reference_plan.draft.json"
    if not (product_root / "reference_plan.json").exists():
        write_json(
            draft_path,
            _draft_reference_plan(record.product_id, product_root, references),
        )
    _create_contact_sheet(
        [
            (path, f"{index:02d} {'FRONT' if index == 1 else 'DETAIL'}")
            for index, path in enumerate(references, start=1)
        ],
        product_root / "reference_contact_sheet.jpg",
    )
    return PreparedProduct(record.product_id, product_root, tuple(references))


def prepare_all(run_root: Path, dry_run: bool = False) -> dict[str, Any]:
    records = find_target_records()
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "products": [
                {
                    "product_id": record.product_id,
                    "record_id": record.record_id,
                    "selected": [
                        asdict(item) for item in select_reference_attachments(record)
                    ],
                }
                for record in records
            ],
        }

    run_root.mkdir(parents=True, exist_ok=True)
    products: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for record in records:
        try:
            prepared = prepare_product(record, run_root)
            products.append(
                {
                    "product_id": prepared.product_id,
                    "product_root": str(prepared.product_root),
                    "reference_count": len(prepared.reference_images),
                }
            )
            print(f"PREPARED {record.product_id}", flush=True)
        except Exception as exc:
            errors.append({"product_id": record.product_id, "error": str(exc)})
            print(f"PREPARE_FAILED {record.product_id}: {exc}", file=sys.stderr, flush=True)
    manifest = {
        "ok": not errors and len(products) == len(TARGET_PRODUCT_IDS),
        "prepared_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "expected": len(TARGET_PRODUCT_IDS),
        "prepared": len(products),
        "products": products,
        "errors": errors,
    }
    write_json(run_root / "prepare_manifest.json", manifest)
    if errors:
        raise RuntimeError(f"准备阶段有 {len(errors)} 款失败")
    return manifest


def _validate_product_ids(product_ids: Sequence[str] | None) -> tuple[str, ...]:
    selected = tuple(product_ids or TARGET_PRODUCT_IDS)
    unknown = [item for item in selected if item not in TARGET_PRODUCT_IDS]
    if unknown:
        raise ValueError(f"目标编号超出 SY1537-SY1552：{unknown}")
    if len(set(selected)) != len(selected):
        raise ValueError("目标编号不能重复")
    return selected


def _reference_paths(plan_path: Path) -> tuple[Path, ...]:
    plan = read_json(plan_path)
    values = [plan.get("front_image"), *(plan.get("detail_images") or [])]
    paths: list[Path] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"参考计划图片路径无效：{plan_path}")
        path = Path(value)
        if not path.is_absolute():
            path = plan_path.parent / path
        paths.append(path)
    if not paths or len(paths) > 5:
        raise ValueError("每款必须有 1-5 张参考图")
    return tuple(paths)


def _evaluate(
    *,
    image_path: Path,
    plan_path: Path,
    output_path: Path,
    stage: str,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(EVALUATE_SCRIPT),
        "--image",
        str(image_path),
        "--reference-plan",
        str(plan_path),
        "--stage",
        stage,
        "--output",
        str(output_path),
    ]
    if stage == "post-watermark":
        command.extend(["--ignore-bottom-ratio", "0.15"])
    run_command(command, allowed_returncodes=(0, 3, 4))
    return read_json(output_path)


def _attempt_number(product_root: Path, retry: bool) -> int:
    state_path = product_root / "manifests" / "generation_state.json"
    if not state_path.exists():
        return 1
    previous = read_json(state_path)
    current = int(previous.get("attempt") or 0)
    if not retry:
        raise RuntimeError(
            f"{product_root.name} 已有第 {current} 次生成结果；如已修订参考计划，请使用 --retry"
        )
    if current >= 2:
        raise RuntimeError(f"{product_root.name} 已达到最多 2 次生成限制")
    return current + 1


def generate_and_evaluate(product_root: Path, retry: bool = False) -> dict[str, Any]:
    product_id = product_root.name
    plan_path = product_root / "reference_plan.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"缺少人工填写的参考计划：{plan_path}")
    run_command(
        [
            sys.executable,
            str(VALIDATE_PLAN_SCRIPT),
            "--reference-plan",
            str(plan_path),
            "--check-files",
        ]
    )
    references = _reference_paths(plan_path)
    attempt = _attempt_number(product_root, retry)
    attempt_root = product_root / f"attempt-{attempt:02d}"
    for name in ("logs", "generated", "qc", "manifests"):
        (attempt_root / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan_path, attempt_root / "reference_plan.json")
    prompt_path = attempt_root / "logs" / f"{product_id}_prompt.txt"
    run_command(
        [
            sys.executable,
            str(BUILD_PROMPT_SCRIPT),
            "--reference-plan",
            str(plan_path),
            "--output",
            str(prompt_path),
        ]
    )
    generated_path = attempt_root / "generated" / f"{product_id}.png"
    generation = generate_to_path(
        prompt_path.read_text(encoding="utf-8"),
        references,
        generated_path,
        attempt_root / "logs" / f"{product_id}_generate.json",
    )
    pre_qc_path = attempt_root / "qc" / "pre_watermark_qc.json"
    pre_qc = _evaluate(
        image_path=generated_path,
        plan_path=plan_path,
        output_path=pre_qc_path,
        stage="pre-watermark",
    )
    _create_contact_sheet(
        [
            *[
                (path, f"REF {index:02d}{' FRONT' if index == 1 else ''}")
                for index, path in enumerate(references, start=1)
            ],
            (generated_path, "GENERATED"),
        ],
        attempt_root / "review_contact_sheet.jpg",
    )
    state = {
        "product_id": product_id,
        "attempt": attempt,
        "attempt_root": str(attempt_root),
        "reference_plan": str(plan_path),
        "reference_plan_sha256": file_sha256(plan_path),
        "generated_image": str(generated_path),
        "generated_image_sha256": file_sha256(generated_path),
        "pre_watermark_qc": str(pre_qc_path),
        "pre_watermark_decision": pre_qc.get("decision"),
        "provider": generation.provider,
        "task_id": generation.task_id,
        "helper_output": str(generation.helper_output),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    write_json(attempt_root / "manifests" / "generation_result.json", state)
    write_json(product_root / "manifests" / "generation_state.json", state)
    return state


def generate_selected(
    run_root: Path,
    product_ids: Sequence[str],
    workers: int,
    retry: bool,
) -> dict[str, Any]:
    if workers not in {1, 2}:
        raise ValueError("生成并发只能为 1 或 2")
    selected = _validate_product_ids(product_ids)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(generate_and_evaluate, run_root / product_id, retry): product_id
            for product_id in selected
        }
        for future in as_completed(futures):
            product_id = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(
                    f"GENERATED {product_id}: {result['pre_watermark_decision']}",
                    flush=True,
                )
            except Exception as exc:
                errors.append({"product_id": product_id, "error": str(exc)})
                print(f"GENERATE_FAILED {product_id}: {exc}", file=sys.stderr, flush=True)
    summary = {
        "ok": not errors,
        "products": selected,
        "results": sorted(results, key=lambda item: item["product_id"]),
        "errors": errors,
    }
    write_json(run_root / "generation_summary.json", summary)
    if errors:
        raise RuntimeError(f"生成阶段有 {len(errors)} 款失败")
    return summary


def _record_get(record_id: str) -> ProductRecord:
    payload = run_lark(
        [
            "base",
            "+record-get",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            TABLE_ID,
            "--record-id",
            record_id,
            "--field-id",
            FIELD_PRODUCT_ID,
            "--field-id",
            FIELD_FRONT,
            "--field-id",
            FIELD_PRODUCT_IMAGES,
            "--field-id",
            FIELD_SIDE,
            "--field-id",
            FIELD_MAIN,
            "--format",
            "json",
            "--as",
            "user",
        ]
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("记录回读缺少 data")
    product_id = str(
        _field_value(data, (data.get("data") or [[]])[0], FIELD_PRODUCT_ID) or ""
    )
    return parse_record_search_payload(payload, product_id)


def _upload_attachment(record_id: str, image_path: Path) -> dict[str, Any]:
    return run_lark(
        [
            "base",
            "+record-upload-attachment",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            TABLE_ID,
            "--record-id",
            record_id,
            "--field-id",
            FIELD_MAIN,
            "--file",
            workspace_relative_output(image_path),
            "--format",
            "json",
            "--as",
            "user",
        ]
    )


def _review_is_approved(path: Path, state: dict[str, Any]) -> bool:
    if not path.is_file():
        return False
    review = read_json(path)
    if review.get("status") != "approved":
        return False
    if review.get("attempt") not in {None, state.get("attempt")}:
        return False
    expected_sha = state.get("generated_image_sha256")
    return review.get("generated_image_sha256") in {None, expected_sha}


def _derive_upload_decision(
    pre_qc: dict[str, Any],
    post_qc: dict[str, Any],
    final_review_approved: bool,
    authorized: bool,
) -> str:
    decisions = {pre_qc.get("decision"), post_qc.get("decision")}
    if "fail" in decisions:
        return "blocked_by_qc"
    if not final_review_approved:
        return "requires_human_review"
    if not authorized:
        return "not_authorized"
    return "approved_for_append"


def _write_audit(
    *,
    image_path: Path,
    plan_path: Path,
    pre_qc_path: Path,
    post_qc_path: Path,
    manual_review_path: Path | None,
    authorization_path: Path | None,
    audit_path: Path,
    upload_decision: str,
) -> None:
    command = [
        sys.executable,
        str(EVALUATE_SCRIPT),
        "--image",
        str(image_path),
        "--reference-plan",
        str(plan_path),
        "--stage",
        "post-watermark",
        "--ignore-bottom-ratio",
        "0.15",
        "--output",
        str(post_qc_path),
        "--pre-watermark-qc",
        str(pre_qc_path),
        "--prompt-version",
        PROMPT_VERSION,
        "--upload-decision",
        upload_decision,
        "--audit-output",
        str(audit_path),
    ]
    if manual_review_path is not None:
        command.extend(["--manual-review-json", str(manual_review_path)])
    if authorization_path is not None:
        command.extend(["--append-authorization-json", str(authorization_path)])
    run_command(command, allowed_returncodes=(0, 3, 4))


def watermark_and_upload(
    product_root: Path,
    record: ProductRecord,
    authorization_reference: str,
) -> dict[str, Any]:
    state_path = product_root / "manifests" / "generation_state.json"
    state = read_json(state_path)
    if state.get("product_id") != record.product_id:
        raise ValueError(f"{record.product_id} 生成状态串号")
    generated_path = Path(str(state["generated_image"]))
    pre_qc_path = Path(str(state["pre_watermark_qc"]))
    pre_qc = read_json(pre_qc_path)
    if pre_qc.get("decision") == "fail":
        return {"product_id": record.product_id, "status": "blocked_by_pre_qc"}

    pre_review_path = product_root / "manifests" / "pre_watermark_review.json"
    if not _review_is_approved(pre_review_path, state):
        return {"product_id": record.product_id, "status": "awaiting_pre_watermark_review"}

    white_dir = product_root / "white-bg"
    white_dir.mkdir(parents=True, exist_ok=True)
    watermarked_path = white_dir / f"{record.product_id}_watermarked.png"
    if not watermarked_path.is_file():
        run_command(
            [
                sys.executable,
                str(WATERMARK_SCRIPT),
                "--input",
                str(generated_path),
                "--output-dir",
                str(white_dir),
                "--product-id",
                record.product_id,
                "--workers",
                "1",
            ]
        )
    if not watermarked_path.is_file() or watermarked_path.stat().st_size == 0:
        raise FileNotFoundError(f"水印图未生成：{watermarked_path}")

    attempt_root = Path(str(state["attempt_root"]))
    post_qc_path = attempt_root / "qc" / "post_watermark_qc.json"
    post_qc = _evaluate(
        image_path=watermarked_path,
        plan_path=product_root / "reference_plan.json",
        output_path=post_qc_path,
        stage="post-watermark",
    )
    references = _reference_paths(product_root / "reference_plan.json")
    _create_contact_sheet(
        [
            (references[0], "REFERENCE FRONT"),
            (generated_path, "GENERATED"),
            (watermarked_path, "WATERMARKED"),
        ],
        attempt_root / "delivery_review_contact_sheet.jpg",
    )

    final_review_path = product_root / "manifests" / "manual_review.json"
    final_review_approved = _review_is_approved(final_review_path, state)
    authorization_path = product_root / "manifests" / "append_authorization.json"
    if not authorization_path.exists():
        write_json(
            authorization_path,
            {
                "status": "approved",
                "approved_by": "用户",
                "approved_at": "2026-08-06",
                "approval_reference": authorization_reference,
            },
        )
    authorization = read_json(authorization_path)
    authorized = authorization.get("status") == "approved"
    upload_decision = _derive_upload_decision(
        pre_qc,
        post_qc,
        final_review_approved,
        authorized,
    )
    audit_path = product_root / "manifests" / "audit.json"
    _write_audit(
        image_path=watermarked_path,
        plan_path=product_root / "reference_plan.json",
        pre_qc_path=pre_qc_path,
        post_qc_path=post_qc_path,
        manual_review_path=final_review_path if final_review_approved else None,
        authorization_path=authorization_path if authorized else None,
        audit_path=audit_path,
        upload_decision=upload_decision,
    )
    if upload_decision != "approved_for_append":
        return {
            "product_id": record.product_id,
            "status": upload_decision,
            "watermarked_image": str(watermarked_path),
            "post_watermark_decision": post_qc.get("decision"),
        }

    current = _record_get(record.record_id)
    if current.product_id != record.product_id:
        raise ValueError(f"{record.product_id} 上传前记录串号")
    before = current.main_images
    upload_plan = plan_upload(before, watermarked_path.name)
    if upload_plan.action == "conflict":
        raise RuntimeError(f"{record.product_id} 主图中存在多个同名附件")
    upload_response: dict[str, Any] | None = None
    if upload_plan.action == "upload":
        upload_response = _upload_attachment(record.record_id, watermarked_path)
        time.sleep(1)
    after_record = _record_get(record.record_id)
    if after_record.product_id != record.product_id:
        raise ValueError(f"{record.product_id} 上传后记录串号")
    matches = [
        item for item in after_record.main_images if item.name == watermarked_path.name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{record.product_id} 上传后未找到唯一同名附件")
    target = matches[0]
    if not verify_append(before, after_record.main_images, target.token):
        raise RuntimeError(f"{record.product_id} 原主图附件未完整保留")
    receipt = {
        "product_id": record.product_id,
        "record_id": record.record_id,
        "field_id": FIELD_MAIN,
        "local_file": str(watermarked_path),
        "local_file_sha256": file_sha256(watermarked_path),
        "status": (
            "already_present_and_verified"
            if upload_plan.action == "already_present"
            else "uploaded_and_verified"
        ),
        "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "before_attachments": [asdict(item) for item in before],
        "after_attachments": [asdict(item) for item in after_record.main_images],
        "uploaded_attachment": asdict(target),
        "original_attachments_preserved": True,
        "upload_response": upload_response,
    }
    write_json(product_root / "manifests" / "upload_receipt.json", receipt)
    return receipt


def deliver_selected(
    run_root: Path,
    product_ids: Sequence[str],
    authorization_reference: str,
) -> dict[str, Any]:
    selected = _validate_product_ids(product_ids)
    records = {record.product_id: record for record in find_target_records(selected)}
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for product_id in selected:
        try:
            result = watermark_and_upload(
                run_root / product_id,
                records[product_id],
                authorization_reference,
            )
            results.append(result)
            print(f"DELIVER {product_id}: {result['status']}", flush=True)
        except Exception as exc:
            errors.append({"product_id": product_id, "error": str(exc)})
            print(f"DELIVER_FAILED {product_id}: {exc}", file=sys.stderr, flush=True)
        time.sleep(1)
    summary = {
        "ok": not errors,
        "delivered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "results": results,
        "errors": errors,
    }
    write_json(run_root / "upload_summary.json", summary)
    if errors:
        raise RuntimeError(f"交付阶段有 {len(errors)} 款失败")
    return summary


def verify_selected(run_root: Path, product_ids: Sequence[str]) -> dict[str, Any]:
    selected = _validate_product_ids(product_ids)
    records = {record.product_id: record for record in find_target_records(selected)}
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for product_id in selected:
        try:
            receipt_path = (
                run_root / product_id / "manifests" / "upload_receipt.json"
            )
            receipt = read_json(receipt_path)
            remote = _record_get(records[product_id].record_id)
            before = tuple(
                Attachment(**item) for item in receipt.get("before_attachments", [])
            )
            target_payload = receipt.get("uploaded_attachment") or {}
            target = Attachment(**target_payload)
            filename_matches = [
                item for item in remote.main_images if item.name == target.name
            ]
            if remote.product_id != product_id:
                raise RuntimeError("远端记录产品编号不一致")
            if len(filename_matches) != 1:
                raise RuntimeError("远端主图未找到唯一目标附件")
            if not verify_append(before, remote.main_images, target.token):
                raise RuntimeError("远端主图未保留全部原附件")
            result = {
                "product_id": product_id,
                "status": "verified",
                "record_id": remote.record_id,
                "target": asdict(target),
                "main_image_count": len(remote.main_images),
            }
            results.append(result)
            print(f"VERIFIED {product_id}", flush=True)
        except Exception as exc:
            errors.append({"product_id": product_id, "error": str(exc)})
            print(f"VERIFY_FAILED {product_id}: {exc}", file=sys.stderr, flush=True)
        time.sleep(1)
    summary = {
        "ok": not errors,
        "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "verified": len(results),
        "results": results,
        "errors": errors,
    }
    write_json(run_root / "verify_summary.json", summary)
    if errors:
        raise RuntimeError(f"远端验证有 {len(errors)} 款失败")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SY1537-SY1552 珠宝白底图生成、水印、主图追加与验证管线。"
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        default=DEFAULT_RUN_ROOT,
        help=f"运行目录，默认：{DEFAULT_RUN_ROOT}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="定位记录、下载并预处理参考图")
    prepare.add_argument("--dry-run", action="store_true", help="只读取并显示选图计划")

    generate = subparsers.add_parser("generate", help="使用 Yuan v4 生成并执行水印前 QC")
    generate.add_argument("--product-id", action="append", default=[])
    generate.add_argument("--workers", type=int, choices=(1, 2), default=2)
    generate.add_argument("--retry", action="store_true", help="修订参考计划后执行唯一一次重试")

    deliver = subparsers.add_parser("deliver", help="加水印、执行门禁并串行追加主图")
    deliver.add_argument("--product-id", action="append", default=[])
    deliver.add_argument(
        "--authorization-reference",
        default=AUTHORIZATION_REFERENCE,
        help="写入追加授权记录的用户指令引用",
    )

    verify = subparsers.add_parser("verify", help="回读远端主图并验证原附件保留")
    verify.add_argument("--product-id", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            payload = prepare_all(args.run_root, dry_run=args.dry_run)
        elif args.command == "generate":
            payload = generate_selected(
                args.run_root,
                args.product_id,
                args.workers,
                args.retry,
            )
        elif args.command == "deliver":
            payload = deliver_selected(
                args.run_root,
                args.product_id,
                args.authorization_reference,
            )
        else:
            payload = verify_selected(args.run_root, args.product_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
