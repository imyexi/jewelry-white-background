from __future__ import annotations

import argparse
import hashlib
import json
import re
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

from scripts.create_background_edit_mask import (
    create_background_edit_assets,
    load_vision_geometry,
)
from scripts.yuan_image_generation_adapter import edit_background_to_path


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
PROMPT_VERSION = "v3.0-background-only-edit"
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
    return [front]


def plan_upload(
    before: Sequence[Attachment], target_filename: str
) -> UploadPlan:
    matches = [item for item in before if item.name == target_filename]
    if len(matches) > 1:
        return UploadPlan(action="conflict", existing=matches[0])
    if matches:
        return UploadPlan(action="already_present", existing=matches[0])
    return UploadPlan(action="upload")


def _normalized_append_version(append_version: str) -> str:
    version = append_version.strip()
    if not version:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", version):
        raise ValueError("追加版本标识只能包含字母、数字、下划线和连字符")
    return version


def _normalized_authorization_reference(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("交付必须显式提供非空授权引用")
    return value.strip()


def watermarked_filename(product_id: str, append_version: str = "") -> str:
    version = _normalized_append_version(append_version)
    if version:
        return f"{product_id}_{version}_watermarked.png"
    return f"{product_id}_watermarked.png"


def build_watermark_command(
    *,
    generated_path: Path,
    output_dir: Path,
    product_id: str,
    append_version: str = "",
) -> tuple[list[str], Path]:
    version = _normalized_append_version(append_version)
    output_path = output_dir / watermarked_filename(product_id, version)
    command = [
        sys.executable,
        str(WATERMARK_SCRIPT),
        "--input",
        str(generated_path),
        "--output-dir",
        str(output_dir),
        "--product-id",
        product_id,
        "--workers",
        "1",
    ]
    if version:
        command.extend(["--suffix", f"_{version}_watermarked"])
    return command, output_path


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
        "schema_version": "3.0",
        "workflow_mode": "background_only_edit",
        "product_id": product_id,
        "front_image": relative[0],
        "detail_images": [],
        "structure": {
            "source_image": relative[0],
        },
    }


def prepare_product(record: ProductRecord, run_root: Path) -> PreparedProduct:
    selected = select_reference_attachments(record)
    if not selected:
        raise ValueError(f"{record.product_id} 缺少可用的产品正面图")
    product_root = run_root / record.product_id
    for name in (
        "source",
        "detail",
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

    references = tuple(source_paths)

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
    value = plan.get("front_image")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"参考计划正面图路径无效：{plan_path}")
    path = Path(value)
    if not path.is_absolute():
        path = plan_path.parent / path
    return (path,)


def _original_front_path(product_root: Path, product_id: str) -> Path:
    context_path = product_root / "product_context.json"
    if not context_path.is_file():
        raise FileNotFoundError(f"缺少原始正面图上下文：{context_path}")
    context = read_json(context_path)
    if context.get("product_id") != product_id:
        raise ValueError(f"原始正面图上下文与商品编号不一致：{context_path}")
    selected = context.get("selected_references")
    if not isinstance(selected, list) or not selected:
        raise ValueError(f"原始正面图上下文缺少 selected_references：{context_path}")
    front = selected[0]
    if not isinstance(front, dict) or front.get("role") != "front":
        raise ValueError(f"原始正面图上下文首项不是 front：{context_path}")
    value = front.get("local_path")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"原始正面图上下文缺少 local_path：{context_path}")
    path = Path(value)
    if not path.is_absolute():
        path = product_root / path
    try:
        path.resolve().relative_to(product_root.resolve())
    except ValueError as exc:
        raise ValueError(f"原始正面图必须位于商品运行目录内：{path}") from exc
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"原始正面图不存在或为空：{path}")
    return path


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
    geometry_profile_path = product_root / "geometry" / f"{product_id}.json"
    if not geometry_profile_path.is_file():
        raise FileNotFoundError(
            f"缺少视觉几何 profile（{product_id}）：{geometry_profile_path}"
        )
    geometry_profile = load_vision_geometry(geometry_profile_path, product_id)
    original_front_path = _original_front_path(product_root, product_id)
    attempt = _attempt_number(product_root, retry)
    attempt_root = product_root / f"attempt-{attempt:02d}"
    state_path = product_root / "manifests" / "generation_state.json"
    in_progress_state = {
        "workflow_mode": "background_only_edit",
        "status": "in_progress",
        "product_id": product_id,
        "attempt": attempt,
        "attempt_root": str(attempt_root),
        "reference_plan": str(plan_path),
        "reference_plan_sha256": file_sha256(plan_path),
        "vision_geometry_profile": str(geometry_profile_path),
        "vision_geometry_schema": geometry_profile.schema_version,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    write_json(state_path, in_progress_state)
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
    for name in ("prepared", "mask", "logs", "generated", "manifests"):
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
    edit_image_path = attempt_root / "prepared" / f"{product_id}_front.png"
    mask_path = (
        attempt_root / "mask" / f"{product_id}_product-protection-mask.png"
    )
    overlay_path = attempt_root / "mask" / f"{product_id}_editable-overlay.png"
    mask_report_path = attempt_root / "logs" / f"{product_id}_vision-mask-report.json"
    assessment = create_background_edit_assets(
        original_front_path,
        edit_image_path,
        mask_path,
        overlay_path,
        mask_report_path,
        geometry_profile_path,
        product_id,
    )
    automatic_allowed = (
        getattr(assessment, "automatic_wawapi_edit_allowed", None) is True
    )
    reasons = list(getattr(assessment, "reasons", ()) or ())
    if (
        assessment.status == "ok"
        and not automatic_allowed
        and "automatic_wawapi_edit_not_allowed" not in reasons
    ):
        reasons.append("automatic_wawapi_edit_not_allowed")
    mask_state = {
        **in_progress_state,
        "edit_image": str(edit_image_path),
        "mask_image": str(mask_path),
        "mask_overlay": str(overlay_path),
        "mask_assessment": str(mask_report_path),
        "mask_status": assessment.status,
        "mask_reasons": reasons,
        "automatic_wawapi_edit_allowed": automatic_allowed,
    }
    if assessment.status != "ok" or not automatic_allowed:
        blocked_state = {
            **mask_state,
            "status": "blocked_by_mask_gate",
            "blocked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        write_json(attempt_root / "manifests" / "generation_result.json", blocked_state)
        write_json(state_path, blocked_state)
        if assessment.status == "ok":
            raise RuntimeError(
                f"{product_id} Mask 虽为 ok，但 automatic_wawapi_edit_allowed 不是 True，禁止调用 Wawapi Edit"
            )
        raise RuntimeError(
            f"{product_id} Mask 技术门禁为 {assessment.status}，禁止调用 Wawapi Edit"
        )
    mask_report = read_json(mask_report_path)
    if (
        mask_report.get("status") != "ok"
        or mask_report.get("automatic_wawapi_edit_allowed") is not True
    ):
        raise RuntimeError(f"{product_id} Mask 报告未同时通过双门禁，禁止调用 Wawapi Edit")
    identity_keys = (
        "product_id",
        "source_sha256",
        "geometry_sha256",
        "prepared_sha256",
        "mask_sha256",
    )
    identity = {key: mask_report.get(key) for key in identity_keys}
    if identity["product_id"] != product_id or any(
        not isinstance(identity[key], str) or not identity[key]
        for key in identity_keys[1:]
    ):
        raise RuntimeError(f"{product_id} Mask 报告缺少完整身份摘要，禁止调用 Wawapi Edit")
    for path, key, label in (
        (edit_image_path, "prepared_sha256", "prepared 原图"),
        (mask_path, "mask_sha256", "最终 Mask"),
    ):
        if file_sha256(path) != identity[key]:
            raise RuntimeError(f"{product_id} {label}在联网前发生变化，禁止调用 Wawapi Edit")
    mask_state.update(identity)
    write_json(state_path, mask_state)

    generated_path = attempt_root / "generated" / f"{product_id}.png"
    generation = edit_background_to_path(
        prompt_path.read_text(encoding="utf-8"),
        edit_image_path,
        mask_path,
        generated_path,
        attempt_root / "logs" / f"{product_id}_edit.json",
    )
    state = {
        **mask_state,
        "status": "completed",
        "generated_image": str(generated_path),
        "generated_image_sha256": file_sha256(generated_path),
        "provider": generation.provider,
        "task_id": generation.task_id,
        "helper_output": str(generation.helper_output),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    write_json(attempt_root / "manifests" / "generation_result.json", state)
    write_json(state_path, state)
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
                    f"EDITED {product_id}: {result['mask_status']}",
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
        ],
        attempts=1,
    )


def _upload_file_token(
    payload: dict[str, Any],
    *,
    record_id: str,
    field_id: str,
    target_filename: str,
) -> str:
    if payload.get("ok") is not True:
        raise RuntimeError("飞书附件追加未返回 ok: true")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("飞书附件追加响应缺少 data")

    if "attachments" in data:
        attachments = data.get("attachments")
        record_attachments = (
            attachments.get(record_id) if isinstance(attachments, dict) else None
        )
        field_attachments = (
            record_attachments.get(field_id)
            if isinstance(record_attachments, dict)
            else None
        )
        if not isinstance(field_attachments, list):
            raise RuntimeError("飞书附件追加响应缺少指定记录和字段的附件数组")
        matches = [
            item
            for item in field_attachments
            if isinstance(item, dict)
            and target_filename in {item.get("filename"), item.get("name")}
        ]
        if len(matches) != 1:
            raise RuntimeError("飞书附件追加响应未唯一匹配目标文件名")
        token = matches[0].get("file_token") or matches[0].get("token")
        if isinstance(token, str) and token:
            return token
        raise RuntimeError("飞书目标附件缺少 file_token")

    direct = data.get("file_token")
    if isinstance(direct, str) and direct:
        return direct
    tokens = data.get("file_tokens")
    if isinstance(tokens, list):
        valid_tokens = [item for item in tokens if isinstance(item, str) and item]
        if len(valid_tokens) == 1:
            return valid_tokens[0]
        if len(valid_tokens) > 1:
            raise RuntimeError("飞书附件追加响应未返回唯一 file_token")
    raise RuntimeError("飞书附件追加响应缺少 file_token")


def _versioned_upload_receipt_path(
    product_root: Path,
    watermarked_path: Path,
) -> Path:
    return (
        product_root
        / "manifests"
        / f"upload_receipt.{watermarked_path.stem}.json"
    )


def _versioned_upload_attempt_path(
    product_root: Path,
    watermarked_path: Path,
) -> Path:
    return (
        product_root
        / "manifests"
        / f"upload_attempt.{watermarked_path.stem}.json"
    )


def _receipt_matches_delivery(
    receipt: dict[str, Any],
    *,
    state: dict[str, Any],
    record: ProductRecord,
    expected_generated_sha: str,
    watermarked_path: Path,
) -> bool:
    uploaded_attachment = receipt.get("uploaded_attachment")
    if not isinstance(uploaded_attachment, dict):
        return False
    upload_token = uploaded_attachment.get("token")
    if not isinstance(upload_token, str) or not upload_token:
        return False

    local_file = receipt.get("local_file")
    if not isinstance(local_file, str) or not local_file:
        return False
    try:
        local_file_matches = Path(local_file).resolve() == watermarked_path.resolve()
    except (OSError, RuntimeError):
        return False

    local_file_sha = receipt.get("local_file_sha256")
    if (
        receipt.get("status") != "uploaded"
        or state.get("workflow_mode") != "background_only_edit"
        or receipt.get("workflow_mode") != state.get("workflow_mode")
        or receipt.get("product_id") != record.product_id
        or receipt.get("record_id") != record.record_id
        or receipt.get("field_id") != FIELD_MAIN
        or receipt.get("generated_image_sha256") != expected_generated_sha
        or not local_file_matches
        or receipt.get("target_filename") != watermarked_path.name
        or not isinstance(local_file_sha, str)
        or not local_file_sha
        or not watermarked_path.is_file()
        or watermarked_path.stat().st_size == 0
    ):
        return False
    return file_sha256(watermarked_path) == local_file_sha


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


def failed_qc_check_ids(qc: dict[str, Any]) -> tuple[str, ...]:
    checks = qc.get("checks")
    if not isinstance(checks, list):
        return ()
    failed_ids = {
        str(check.get("id"))
        for check in checks
        if isinstance(check, dict)
        and check.get("status") == "fail"
        and check.get("id")
    }
    return tuple(sorted(failed_ids))


def is_size_only_failure(qc: dict[str, Any]) -> bool:
    return (
        qc.get("decision") == "fail"
        and failed_qc_check_ids(qc) == ("composition_width",)
    )


def build_size_override_record(
    *,
    product_id: str,
    state: dict[str, Any],
    pre_qc: dict[str, Any],
    post_qc: dict[str, Any],
    authorization_reference: str,
) -> dict[str, Any]:
    failing_qcs = [qc for qc in (pre_qc, post_qc) if qc.get("decision") == "fail"]
    if not failing_qcs or not all(is_size_only_failure(qc) for qc in failing_qcs):
        raise ValueError(f"{product_id} 存在非大小类 QC 失败，不能写入大小覆盖记录")
    failed_ids = sorted(
        {check_id for qc in failing_qcs for check_id in failed_qc_check_ids(qc)}
    )
    return {
        "schema_version": "override-1.0",
        "status": "approved",
        "override_type": "composition_qc_gate",
        "approved_by": "用户",
        "approved_at": "2026-08-06",
        "scope": product_id,
        "attempt": state.get("attempt"),
        "generated_image_sha256": state.get("generated_image_sha256"),
        "original_qc_decision": {
            "pre_watermark": pre_qc.get("decision"),
            "post_watermark": post_qc.get("decision"),
        },
        "failed_check_ids": failed_ids,
        "reason": "产品宽度占比未落入自动 QC 目标区间",
        "authorization_reference": authorization_reference,
        "effect": "仅覆盖本次产品大小构图门禁，不覆盖结构、裁切、串号或特殊件错误",
        "upload_decision": "user_override_for_append",
    }


def _derive_upload_decision(
    pre_qc: dict[str, Any],
    post_qc: dict[str, Any],
    final_review_approved: bool,
    authorized: bool,
    allow_size_override: bool = False,
) -> str:
    failing_qcs = [qc for qc in (pre_qc, post_qc) if qc.get("decision") == "fail"]
    if failing_qcs and (
        not allow_size_override
        or not all(is_size_only_failure(qc) for qc in failing_qcs)
    ):
        return "blocked_by_qc"
    if not final_review_approved:
        return "requires_human_review"
    if not authorized:
        return "not_authorized"
    if failing_qcs:
        return "user_override_for_append"
    return "approved_for_append"


def _automatic_audit_decision(
    pre_qc: dict[str, Any],
    post_qc: dict[str, Any],
    upload_decision: str,
) -> str:
    if "fail" in {pre_qc.get("decision"), post_qc.get("decision")}:
        return "blocked_by_qc"
    return upload_decision


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
    allow_size_override: bool = False,
    append_version: str = "",
) -> dict[str, Any]:
    authorization_reference = _normalized_authorization_reference(
        authorization_reference
    )
    state_path = product_root / "manifests" / "generation_state.json"
    state = read_json(state_path)
    if state.get("product_id") != record.product_id:
        raise ValueError(f"{record.product_id} 生成状态串号")
    if (
        state.get("workflow_mode") != "background_only_edit"
        or state.get("status") != "completed"
    ):
        raise RuntimeError(
            f"{record.product_id} generation_state 不是 completed 的 background_only_edit Mask Edit 状态，禁止交付"
        )
    mask_report_value = state.get("mask_assessment")
    if (
        state.get("mask_status") != "ok"
        or state.get("automatic_wawapi_edit_allowed") is not True
        or not isinstance(mask_report_value, str)
    ):
        if state.get("automatic_wawapi_edit_allowed") is not True:
            raise RuntimeError(
                f"{record.product_id} generation_state 的 automatic_wawapi_edit_allowed 不是 True，禁止交付"
            )
        raise RuntimeError(f"{record.product_id} 缺少通过的 Mask 技术门禁，禁止交付")
    mask_report = read_json(Path(mask_report_value))
    if mask_report.get("status") != "ok":
        raise RuntimeError(f"{record.product_id} Mask 技术门禁不是 ok，禁止交付")
    if mask_report.get("automatic_wawapi_edit_allowed") is not True:
        raise RuntimeError(
            f"{record.product_id} Mask 报告的 automatic_wawapi_edit_allowed 不是 True，禁止交付"
        )
    identity_keys = (
        "product_id",
        "source_sha256",
        "geometry_sha256",
        "prepared_sha256",
        "mask_sha256",
    )
    state_identity = {key: state.get(key) for key in identity_keys}
    report_identity = {key: mask_report.get(key) for key in identity_keys}
    if state_identity != report_identity or state_identity["product_id"] != record.product_id:
        raise RuntimeError(f"{record.product_id} state 与 Mask 报告身份摘要不一致，禁止交付")
    if any(
        not isinstance(state_identity[key], str) or not state_identity[key]
        for key in identity_keys[1:]
    ):
        raise RuntimeError(f"{record.product_id} 缺少完整身份摘要，禁止交付")
    for path_key, hash_key, label in (
        ("edit_image", "prepared_sha256", "prepared 原图"),
        ("mask_image", "mask_sha256", "最终 Mask"),
    ):
        asset_value = state.get(path_key)
        if not isinstance(asset_value, str) or not asset_value:
            raise RuntimeError(f"{record.product_id} generation_state 缺少{label}路径")
        asset_path = Path(asset_value)
        if not asset_path.is_file() or asset_path.stat().st_size == 0:
            raise FileNotFoundError(f"{label}不存在或为空：{asset_path}")
        if file_sha256(asset_path) != state_identity[hash_key]:
            raise RuntimeError(f"{record.product_id} {label} SHA-256 与身份摘要不一致")
    generated_path = Path(str(state["generated_image"]))
    if not generated_path.is_file() or generated_path.stat().st_size == 0:
        raise FileNotFoundError(f"Edit 结果不存在：{generated_path}")
    expected_generated_sha = state.get("generated_image_sha256")
    if not isinstance(expected_generated_sha, str) or not expected_generated_sha:
        raise RuntimeError(f"{record.product_id} generation_state 缺少生成图 SHA-256")
    if file_sha256(generated_path) != expected_generated_sha:
        raise RuntimeError(f"{record.product_id} 生成图 SHA-256 与 generation_state 不一致")

    white_dir = product_root / "white-bg"
    watermark_command, watermarked_path = build_watermark_command(
        generated_path=generated_path,
        output_dir=white_dir,
        product_id=record.product_id,
        append_version=append_version,
    )
    versioned_receipt_path = _versioned_upload_receipt_path(
        product_root,
        watermarked_path,
    )
    upload_attempt_path = _versioned_upload_attempt_path(
        product_root,
        watermarked_path,
    )
    legacy_receipt_path = product_root / "manifests" / "upload_receipt.json"
    existing_receipt_path = (
        versioned_receipt_path
        if versioned_receipt_path.is_file()
        else legacy_receipt_path
    )
    if existing_receipt_path.is_file():
        existing_receipt = read_json(existing_receipt_path)
        if _receipt_matches_delivery(
            existing_receipt,
            state=state,
            record=record,
            expected_generated_sha=expected_generated_sha,
            watermarked_path=watermarked_path,
        ):
            return existing_receipt

    if upload_attempt_path.is_file():
        existing_attempt = read_json(upload_attempt_path)
        if (
            existing_attempt.get("status") == "uploaded"
            and _receipt_matches_delivery(
                existing_attempt,
                state=state,
                record=record,
                expected_generated_sha=expected_generated_sha,
                watermarked_path=watermarked_path,
            )
        ):
            return existing_attempt
        raise RuntimeError(
            f"{record.product_id} 已存在未决或不完整的上传意图，必须人工处置，禁止自动重传"
        )

    white_dir.mkdir(parents=True, exist_ok=True)
    run_command(watermark_command)
    if not watermarked_path.is_file() or watermarked_path.stat().st_size == 0:
        raise FileNotFoundError(f"水印图未生成：{watermarked_path}")
    resolved_watermarked_path = watermarked_path.resolve()
    watermarked_sha256 = file_sha256(watermarked_path)

    upload_attempt = {
        "status": "uploading",
        "uploading_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "workflow_mode": state["workflow_mode"],
        "product_id": record.product_id,
        "record_id": record.record_id,
        "field_id": FIELD_MAIN,
        "generated_image_sha256": expected_generated_sha,
        "target_filename": watermarked_path.name,
        "local_file": str(resolved_watermarked_path),
        "local_file_sha256": watermarked_sha256,
        "authorization_reference": authorization_reference,
    }
    write_json(upload_attempt_path, upload_attempt)

    upload_response: dict[str, Any] | None = None
    try:
        upload_response = _upload_attachment(record.record_id, watermarked_path)
        file_token = _upload_file_token(
            upload_response,
            record_id=record.record_id,
            field_id=FIELD_MAIN,
            target_filename=watermarked_path.name,
        )
    except Exception as exc:
        failed_attempt = {
            **upload_attempt,
            "status": "failed",
            "failed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        if upload_response is not None:
            failed_attempt["upload_response"] = upload_response
        write_json(upload_attempt_path, failed_attempt)
        raise

    target = Attachment(token=file_token, name=watermarked_path.name)
    receipt = {
        "product_id": record.product_id,
        "record_id": record.record_id,
        "field_id": FIELD_MAIN,
        "local_file": str(resolved_watermarked_path),
        "target_filename": watermarked_path.name,
        "local_file_sha256": watermarked_sha256,
        "status": "uploaded",
        "uploaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "authorization_reference": authorization_reference,
        "workflow_mode": state["workflow_mode"],
        "generation_status": state["status"],
        "generated_image": str(generated_path),
        "generated_image_sha256": expected_generated_sha,
        "uploaded_attachment": asdict(target),
        "upload_response": upload_response,
    }
    write_json(upload_attempt_path, receipt)
    write_json(versioned_receipt_path, receipt)
    write_json(legacy_receipt_path, receipt)
    return receipt


def _delivery_record_from_context(product_root: Path, product_id: str) -> ProductRecord:
    context_path = product_root / "product_context.json"
    if not context_path.is_file():
        raise FileNotFoundError(f"{product_id} 缺少本地 product_context：{context_path}")
    context = read_json(context_path)
    if context.get("product_id") != product_id:
        raise ValueError(f"{product_id} product_context 商品编号不一致")
    record_id = context.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError(f"{product_id} product_context 缺少 record_id")
    return ProductRecord(product_id, record_id.strip(), (), (), (), ())


def deliver_selected(
    run_root: Path,
    product_ids: Sequence[str],
    authorization_reference: str,
    allow_size_override: bool = False,
    append_version: str = "",
) -> dict[str, Any]:
    authorization_reference = _normalized_authorization_reference(
        authorization_reference
    )
    selected = _validate_product_ids(product_ids)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for product_id in selected:
        try:
            product_root = run_root / product_id
            record = _delivery_record_from_context(product_root, product_id)
            result = watermark_and_upload(
                product_root,
                record,
                authorization_reference,
                allow_size_override,
                append_version,
            )
            results.append(result)
            print(f"DELIVER {product_id}: {result['status']}", flush=True)
        except Exception as exc:
            errors.append({"product_id": product_id, "error": str(exc)})
            print(f"DELIVER_FAILED {product_id}: {exc}", file=sys.stderr, flush=True)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SY1537-SY1552 珠宝白底图 Mask Edit、水印与主图追加管线。"
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        default=DEFAULT_RUN_ROOT,
        help=f"运行目录，默认：{DEFAULT_RUN_ROOT}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="定位记录、下载并预处理正面图")
    prepare.add_argument("--dry-run", action="store_true", help="只读取并显示选图计划")

    generate_help = "通过技术门禁后执行 Yuan v4 Wawapi Mask Edit"
    generate = subparsers.add_parser(
        "generate",
        help=generate_help,
        description=generate_help,
    )
    generate.add_argument("--product-id", action="append", default=[])
    generate.add_argument("--workers", type=int, choices=(1, 2), default=2)
    generate.add_argument("--retry", action="store_true", help="修订参考计划后执行唯一一次重试")

    deliver = subparsers.add_parser("deliver", help="加水印并串行追加主图")
    deliver.add_argument("--product-id", action="append", default=[])
    deliver.add_argument(
        "--authorization-reference",
        required=True,
        type=_normalized_authorization_reference,
        help="写入追加授权记录的用户指令引用",
    )
    deliver.add_argument(
        "--allow-size-override",
        action="store_true",
        help="兼容保留；background-only Edit 新流程不执行尺寸 QC，该参数不改变交付判断",
    )
    deliver.add_argument(
        "--append-version",
        default="",
        help="新版本标识；生成独立文件名后追加，不覆盖已有同名主图",
    )

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
                args.allow_size_override,
                args.append_version,
            )
        else:
            raise ValueError(f"未知活动命令：{args.command}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
