import json
import hashlib
import argparse
import os
import re
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from scripts.create_background_edit_mask import (
    create_background_edit_assets,
    load_vision_geometry,
)
from scripts.yuan_image_generation_adapter import edit_background_to_path

SKILL_SCRIPTS = Path(__file__).resolve().parent / "skills" / "jewelry-white-background" / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

_build_authoritative_prompt = import_module("build_white_background_prompt").build_prompt

PYTHON = r"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
BASE_TOKEN = "D4Vjbv19WaVVTwsGKdJcsnt5neg"
TABLE_ID = "tblEtBnKFwkgTp22"
FIELD_PRODUCT_ID = "fldPxwdPYF"
FIELD_FRONT = "fldqCNEJYB"
FIELD_WHITE = "fldaCsBeg0"
WATERMARK = r"C:\Users\Administrator\.codex\skills\yuanyuan-ruyi-watermark\scripts\watermark_images.py"
ROOT = Path(r"C:\Users\Administrator\Documents\珠宝白底图生成\outputs\jewelry-white-background\base-021-20260717")

FIELD_IDS = [FIELD_PRODUCT_ID, FIELD_FRONT, FIELD_WHITE]
FIELD_NAMES = ["产品编号", "正面图", "白底图"]

BACKGROUND_EDIT_PROMPT_PLAN = {
    "schema_version": "3.0",
    "workflow_mode": "background_only_edit",
    "product_id": "background-edit",
    "front_image": "front.png",
    "detail_images": [],
    "structure": {"source_image": "front.png"},
}

os.environ["PYTHONIOENCODING"] = "utf-8"


def run(cmd, check=True, cwd=None):
    p = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True, cwd=cwd)
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed {p.returncode}: {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return p


def safe_name(s):
    return re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", str(s or "")).strip("_")[:80] or "item"


def attachments(v):
    return v if isinstance(v, list) else []


def load_records():
    cmd = [r"C:\\Users\\Administrator\\AppData\\Roaming\\npm\\lark-cli.cmd", "base", "+record-list", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
           "--limit", "200", "--format", "json", "--as", "user"]
    for f in FIELD_IDS:
        cmd += ["--field-id", f]
    data = json.loads(run(cmd).stdout)
    out = []
    for rid, row in zip(data["data"]["record_id_list"], data["data"]["data"]):
        item = dict(zip(FIELD_NAMES, row))
        item["record_id"] = rid
        out.append(item)
    return out


def download_attachment(record_id, token, name, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    ext = Path(name or "").suffix or ".bin"
    target = outdir / f"{token}{ext}"
    if target.exists() and target.stat().st_size > 0:
        return target
    cmd = [r"C:\\Users\\Administrator\\AppData\\Roaming\\npm\\lark-cli.cmd", "base", "+record-download-attachment", "--base-token", BASE_TOKEN,
           "--table-id", TABLE_ID, "--record-id", record_id, "--file-token", token,
           "--output", target.name, "--overwrite", "--format", "json", "--as", "user"]
    run(cmd, cwd=str(outdir))
    if not target.exists():
        # lark-cli may choose original filename when output is treated as a directory.
        candidates = list(outdir.glob(f"*{token}*")) + list(outdir.glob(name or "__none__"))
        if candidates:
            return candidates[0]
        raise FileNotFoundError(f"downloaded file not found for {record_id} {token}")
    return target


def build_prompt(item):
    del item
    return _build_authoritative_prompt(BACKGROUND_EDIT_PROMPT_PLAN)


def watermark(image_path, pid, product_dir):
    outdir = product_dir / "white-bg"
    outdir.mkdir(parents=True, exist_ok=True)
    image_path = Path(image_path)
    output_path = outdir / f"{image_path.stem}_watermarked{image_path.suffix}"
    run([PYTHON, WATERMARK, "--input", str(image_path), "--output-dir", str(outdir), "--product-id", pid])
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"watermark output not found for {pid}: {output_path}")
    return output_path


def upload(record_id, final_path):
    final_path = Path(final_path)
    cmd = [r"C:\\Users\\Administrator\\AppData\\Roaming\\npm\\lark-cli.cmd", "base", "+record-upload-attachment", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
           "--record-id", record_id, "--field-id", FIELD_WHITE, "--file", final_path.name, "--format", "json", "--as", "user"]
    return json.loads(run(cmd, cwd=str(final_path.parent)).stdout)


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verified_mask_report_identity(report_path, product_id, edit_image, mask_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise RuntimeError(f"{product_id} Mask 报告不是 JSON 对象，禁止调用 Wawapi Edit")
    if (
        report.get("status") != "ok"
        or report.get("automatic_wawapi_edit_allowed") is not True
    ):
        raise RuntimeError(f"{product_id} Mask 报告未同时通过双门禁，禁止调用 Wawapi Edit")

    identity_keys = (
        "product_id",
        "source_sha256",
        "geometry_sha256",
        "prepared_sha256",
        "mask_sha256",
    )
    identity = {key: report.get(key) for key in identity_keys}
    if identity["product_id"] != product_id or any(
        not isinstance(identity[key], str)
        or re.fullmatch(r"[0-9A-F]{64}", identity[key]) is None
        for key in identity_keys[1:]
    ):
        raise RuntimeError(f"{product_id} Mask 报告缺少完整身份摘要，禁止调用 Wawapi Edit")

    for path, key, label in (
        (edit_image, "prepared_sha256", "prepared 原图"),
        (mask_path, "mask_sha256", "最终 Mask"),
    ):
        if file_sha256(path) != identity[key]:
            raise RuntimeError(f"{product_id} {label}在联网前发生变化，禁止调用 Wawapi Edit")
    return identity


def first_valid_front(value):
    image_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
    return next(
        (
            item
            for item in attachments(value)
            if isinstance(item, dict)
            and item.get("file_token")
            and Path(str(item.get("name") or "")).suffix.lower() in image_suffixes
        ),
        None,
    )


def upload_file_token(payload, record_id, field_id, target_filename):
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
            raise RuntimeError("飞书附件追加响应未匹配指定记录和字段")
        matches = [
            item.get("file_token")
            for item in field_attachments
            if isinstance(item, dict)
            and item.get("name") == target_filename
            and isinstance(item.get("file_token"), str)
            and item["file_token"]
        ]
        if len(matches) != 1:
            raise RuntimeError("飞书附件追加响应未唯一匹配目标文件名")
        return matches[0]

    direct = data.get("file_token")
    if isinstance(direct, str) and direct:
        return direct
    tokens = data.get("file_tokens")
    if isinstance(tokens, list):
        valid_tokens = [item for item in tokens if isinstance(item, str) and item]
        if len(valid_tokens) == 1:
            return valid_tokens[0]
        if valid_tokens:
            raise RuntimeError("飞书附件追加响应包含歧义 file_tokens，无法唯一匹配")
    raise RuntimeError("飞书附件追加响应缺少 file_token")


def normalize_authorization_reference(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("授权依据必须是非空字符串")
    return value.strip()


def reusable_generated_path(
    state_path,
    product_dir,
    product_id,
    record_id,
    front_file_token,
):
    state_path = Path(state_path)
    if not state_path.is_file():
        raise RuntimeError(
            f"{product_id} 不存在 awaiting_authorization 本地状态，禁止授权交付"
        )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{product_id} 本地生成状态无法用于复用：{exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError(f"{product_id} 本地生成状态不是 JSON 对象，禁止复用")
    if state.get("status") != "awaiting_authorization":
        raise RuntimeError(
            f"{product_id} 本地状态不是 awaiting_authorization，禁止授权交付"
        )
    if (
        state.get("workflow_mode") != "background_only_edit"
        or state.get("product_id") != product_id
        or state.get("record_id") != record_id
    ):
        raise RuntimeError(f"{product_id} 本地生成状态身份不匹配，禁止复用")
    if state.get("front_file_token") != front_file_token:
        raise RuntimeError(f"{product_id} 当前正面图 file_token 与本地状态不一致")
    if (
        state.get("mask_status") != "ok"
        or state.get("automatic_wawapi_edit_allowed") is not True
    ):
        raise RuntimeError(f"{product_id} 本地生成状态未通过双门禁，禁止复用")

    prepared = product_dir / "prepared" / f"{product_id}_front.png"
    mask = product_dir / "mask" / f"{product_id}_product-protection-mask.png"
    generated = product_dir / "generated" / f"{product_id}_generated.png"
    expected_report = product_dir / "logs" / f"{product_id}_vision-mask-report.json"
    if Path(str(state.get("generated_image") or "")) != generated:
        raise RuntimeError(f"{product_id} 本地生成图路径不匹配，禁止复用")
    if Path(str(state.get("mask_assessment") or "")) != expected_report:
        raise RuntimeError(f"{product_id} Mask 报告路径不匹配，禁止复用")

    try:
        identity = verified_mask_report_identity(
            expected_report,
            product_id,
            prepared,
            mask,
        )
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{product_id} 本地 Mask 资产无法复用：{exc}") from exc
    if any(state.get(key) != value for key, value in identity.items()):
        raise RuntimeError(f"{product_id} 本地状态与 Mask 报告五项摘要不一致，禁止复用")
    if not generated.is_file():
        raise RuntimeError(f"{product_id} 本地生成图缺失，禁止复用")
    if file_sha256(generated) != state.get("generated_image_sha256"):
        raise RuntimeError(f"{product_id} 本地生成图在授权前发生变化，禁止复用")
    return generated, state


def verified_upload_receipt(
    receipt_path,
    state_path,
    product_dir,
    product_id,
    record_id,
    front_file_token,
):
    receipt_path = Path(receipt_path)
    if not receipt_path.is_file():
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{product_id} 成功上传回执无法验证：{exc}") from exc
    if not isinstance(receipt, dict) or not isinstance(state, dict):
        raise RuntimeError(f"{product_id} 成功上传回执或生成状态格式错误")
    if (
        receipt.get("workflow_mode") != "background_only_edit"
        or receipt.get("status") != "uploaded"
        or receipt.get("product_id") != product_id
        or receipt.get("record_id") != record_id
        or receipt.get("field_id") != FIELD_WHITE
    ):
        raise RuntimeError(f"{product_id} 成功上传回执身份不匹配，禁止重复交付")
    if (
        state.get("workflow_mode") != "background_only_edit"
        or state.get("status") not in {"awaiting_authorization", "completed"}
        or state.get("product_id") != product_id
        or state.get("record_id") != record_id
        or state.get("front_file_token") != front_file_token
    ):
        raise RuntimeError(f"{product_id} 成功上传回执与当前生成状态不匹配")

    generated = product_dir / "generated" / f"{product_id}_generated.png"
    target = (
        product_dir
        / "white-bg"
        / f"{generated.stem}_watermarked{generated.suffix}"
    )
    generated_sha = receipt.get("generated_image_sha256")
    local_sha = receipt.get("local_file_sha256")
    uploaded_token = receipt.get("uploaded_file_token")
    if (
        Path(str(receipt.get("generated_image") or "")) != generated
        or Path(str(state.get("generated_image") or "")) != generated
        or Path(str(receipt.get("local_file") or "")) != target
        or not isinstance(generated_sha, str)
        or generated_sha != state.get("generated_image_sha256")
        or not isinstance(local_sha, str)
        or not isinstance(uploaded_token, str)
        or not uploaded_token
    ):
        raise RuntimeError(f"{product_id} 成功上传回执缺少完整本地身份，禁止重复交付")
    if not generated.is_file() or file_sha256(generated) != generated_sha:
        raise RuntimeError(f"{product_id} 成功上传回执对应生成图已变化")
    if (
        not target.is_file()
        or target.stat().st_size <= 0
        or file_sha256(target) != local_sha
    ):
        raise RuntimeError(f"{product_id} 成功上传回执对应水印图已变化")
    return receipt, state, generated, target


def deliver_generated(result, generated_path, authorization_reference, product_dir):
    pid = result["product_id"]
    record_id = result["record_id"]
    generated_sha = file_sha256(generated_path)
    final_path = watermark(generated_path, pid, product_dir)
    final_sha = file_sha256(final_path)
    result["final_path"] = str(final_path)
    state_path = product_dir / "manifests" / "generation_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    attempt = int(state.get("delivery_attempt") or 0) + 1
    write_json(
        state_path,
        {
            **state,
            "status": "uploading",
            "delivery_attempt": attempt,
            "authorization_reference": authorization_reference,
            "delivery_local_file": str(final_path),
            "delivery_local_file_sha256": final_sha,
        },
    )
    try:
        upload_json = upload(record_id, final_path)
        result["upload_result"] = upload_json
        uploaded_file_token = upload_file_token(
            upload_json,
            record_id,
            FIELD_WHITE,
            final_path.name,
        )
    except Exception as exc:
        write_json(
            state_path,
            {
                **state,
                "status": "delivery_failed",
                "delivery_attempt": attempt,
                "authorization_reference": authorization_reference,
                "delivery_local_file": str(final_path),
                "delivery_local_file_sha256": final_sha,
                "delivery_error": str(exc),
            },
        )
        raise
    result["uploaded_file_token"] = uploaded_file_token
    receipt_path = product_dir / "manifests" / "upload_receipt.json"
    write_json(
        receipt_path,
        {
            "workflow_mode": "background_only_edit",
            "status": "uploaded",
            "product_id": pid,
            "record_id": record_id,
            "field_id": FIELD_WHITE,
            "local_file": str(final_path),
            "local_file_sha256": final_sha,
            "generated_image": str(generated_path),
            "generated_image_sha256": generated_sha,
            "authorization_reference": authorization_reference,
            "uploaded_file_token": uploaded_file_token,
            "upload_response": upload_json,
        },
    )
    write_json(
        state_path,
        {
            **state,
            "status": "completed",
            "authorization_reference": authorization_reference,
            "upload_receipt": str(receipt_path),
            "uploaded_file_token": uploaded_file_token,
        },
    )
    result["upload_receipt"] = str(receipt_path)
    result["authorization_reference"] = authorization_reference
    result["status"] = "completed"
    return result


def process_one(item, authorization_reference=None):
    authorization_reference = normalize_authorization_reference(
        authorization_reference
    )
    pid = item["产品编号"]
    product_dir = ROOT / safe_name(pid)
    for sub in ["source", "logs"]:
        (product_dir / sub).mkdir(parents=True, exist_ok=True)
    result = {
        "workflow_mode": "background_only_edit",
        "product_id": pid,
        "record_id": item["record_id"],
        "run_root": str(product_dir),
    }
    front = first_valid_front(item.get("正面图"))
    if front is None:
        result.update(status="skipped", reason="无正面图")
        return result
    white = attachments(item.get("白底图"))
    if white:
        result.update(status="skipped", reason="白底图字段已有附件", existing_white=white)
        return result
    state_path = product_dir / "manifests" / "generation_state.json"
    if authorization_reference is not None:
        receipt_path = product_dir / "manifests" / "upload_receipt.json"
        completed = verified_upload_receipt(
            receipt_path,
            state_path,
            product_dir,
            pid,
            item["record_id"],
            front["file_token"],
        )
        if completed is not None:
            receipt, state, generated_path, final_path = completed
            result.update(
                provider=state.get("provider"),
                task_id=state.get("task_id"),
                helper_output_path=state.get("helper_output"),
                generated_path=str(generated_path),
                generated_url=None,
                final_path=str(final_path),
                upload_receipt=str(receipt_path),
                uploaded_file_token=receipt["uploaded_file_token"],
                authorization_reference=receipt.get("authorization_reference"),
                status="completed",
                reused_upload_receipt=True,
            )
            return result
        reusable = reusable_generated_path(
            state_path,
            product_dir,
            pid,
            item["record_id"],
            front["file_token"],
        )
        generated_path, state = reusable
        result.update(
            provider=state.get("provider"),
            task_id=state.get("task_id"),
            helper_output_path=state.get("helper_output"),
            generated_path=str(generated_path),
            generated_url=None,
            mask_status=state["mask_status"],
            mask_report=state["mask_assessment"],
            automatic_wawapi_edit_allowed=True,
            reused_local_generation=True,
        )
        return deliver_generated(
            result,
            generated_path,
            authorization_reference,
            product_dir,
        )

    state_base = {
        "workflow_mode": "background_only_edit",
        "product_id": pid,
        "record_id": item["record_id"],
        "front_file_token": front["file_token"],
    }
    write_json(state_path, {**state_base, "status": "in_progress"})

    geometry_profile_path = product_dir / "geometry" / f"{pid}.json"
    try:
        if not geometry_profile_path.is_file():
            raise FileNotFoundError(
                f"缺少视觉几何 profile（{pid}）：{geometry_profile_path}"
            )
        geometry_profile = load_vision_geometry(geometry_profile_path, pid)
        source = download_attachment(
            item["record_id"],
            front["file_token"],
            front.get("name", ""),
            product_dir / "source",
        )
        prompt = build_prompt(item)
        prompt_path = product_dir / "logs" / f"{pid}_prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        edit_image = product_dir / "prepared" / f"{pid}_front.png"
        mask_path = product_dir / "mask" / f"{pid}_product-protection-mask.png"
        overlay_path = product_dir / "mask" / f"{pid}_editable-overlay.png"
        mask_report_path = product_dir / "logs" / f"{pid}_vision-mask-report.json"
        assessment = create_background_edit_assets(
            source,
            edit_image,
            mask_path,
            overlay_path,
            mask_report_path,
            geometry_profile_path,
            pid,
        )
    except Exception as exc:
        write_json(
            state_path,
            {
                **state_base,
                "status": "failed",
                "error": str(exc),
            },
        )
        raise
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
    result["mask_status"] = assessment.status
    result["mask_report"] = str(mask_report_path)
    result["automatic_wawapi_edit_allowed"] = automatic_allowed
    if assessment.status != "ok" or not automatic_allowed:
        result.update(status="blocked_by_mask_gate", reasons=reasons)
        write_json(
            state_path,
            {
                "workflow_mode": "background_only_edit",
                "status": "blocked_by_mask_gate",
                "product_id": pid,
                "vision_geometry_profile": str(geometry_profile_path),
                "vision_geometry_schema": geometry_profile.schema_version,
                "mask_assessment": str(mask_report_path),
                "mask_status": assessment.status,
                "mask_reasons": reasons,
                "automatic_wawapi_edit_allowed": automatic_allowed,
            },
        )
        return result

    try:
        identity = verified_mask_report_identity(
            mask_report_path,
            pid,
            edit_image,
            mask_path,
        )
    except Exception as exc:
        write_json(
            state_path,
            {
                **state_base,
                "status": "blocked_by_mask_gate",
                "vision_geometry_profile": str(geometry_profile_path),
                "vision_geometry_schema": geometry_profile.schema_version,
                "mask_assessment": str(mask_report_path),
                "mask_status": assessment.status,
                "automatic_wawapi_edit_allowed": automatic_allowed,
                "error": str(exc),
            },
        )
        raise

    generated_path = product_dir / "generated" / f"{pid}_generated.png"
    try:
        generation = edit_background_to_path(
            prompt,
            edit_image,
            mask_path,
            generated_path,
            product_dir / "logs" / f"{pid}_edit.json",
        )
    except Exception as exc:
        write_json(
            state_path,
            {
                **state_base,
                "status": "failed",
                "vision_geometry_profile": str(geometry_profile_path),
                "vision_geometry_schema": geometry_profile.schema_version,
                "mask_assessment": str(mask_report_path),
                "mask_status": assessment.status,
                "automatic_wawapi_edit_allowed": automatic_allowed,
                **identity,
                "error": str(exc),
            },
        )
        raise
    result["provider"] = generation.provider
    result["task_id"] = generation.task_id
    result["helper_output_path"] = str(generation.helper_output)
    result["generated_path"] = str(generated_path)
    result["generated_url"] = None
    generated_sha = file_sha256(generated_path)
    write_json(
        state_path,
        {
            "workflow_mode": "background_only_edit",
            "status": "awaiting_authorization",
            "product_id": pid,
            "record_id": item["record_id"],
            "front_file_token": front["file_token"],
            "vision_geometry_profile": str(geometry_profile_path),
            "vision_geometry_schema": geometry_profile.schema_version,
            "generated_image": str(generated_path),
            "generated_image_sha256": generated_sha,
            "provider": generation.provider,
            "task_id": generation.task_id,
            "helper_output": str(generation.helper_output),
            "mask_assessment": str(mask_report_path),
            "mask_status": assessment.status,
            "automatic_wawapi_edit_allowed": automatic_allowed,
            **identity,
        },
    )
    result.update(
        status="awaiting_authorization",
        reason="等待用户针对本次结果明确授权",
    )
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization-reference")
    args = parser.parse_args(argv)
    if args.authorization_reference is not None:
        try:
            args.authorization_reference = normalize_authorization_reference(
                args.authorization_reference
            )
        except ValueError as exc:
            parser.error(str(exc))
    return args


def main(argv=None):
    args = parse_args(argv)
    ROOT.mkdir(parents=True, exist_ok=True)
    records = load_records()
    manifest_path = ROOT / "run_manifest.jsonl"
    latest_by_product = {}
    if manifest_path.exists():
        for line in manifest_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    rec = json.loads(line)
                    product_id = rec.get("product_id")
                    if isinstance(product_id, str) and product_id:
                        latest_by_product[product_id] = rec
                except Exception:
                    pass
    done = {
        product_id
        for product_id, rec in latest_by_product.items()
        if rec.get("workflow_mode") == "background_only_edit"
        and rec.get("mask_status") == "ok"
        and rec.get("automatic_wawapi_edit_allowed") is True
        and rec.get("status") in {"completed", "awaiting_authorization"}
    }
    todo = [
        item
        for item in records
        if args.authorization_reference is not None
        or item.get(FIELD_NAMES[0]) not in done
    ]
    summary = {"completed": 0, "skipped": 0, "failed": 0, "total_records": len(records), "todo": len(todo)}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    with manifest_path.open("a", encoding="utf-8") as mf:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {
                ex.submit(
                    process_one,
                    item,
                    args.authorization_reference,
                ): item
                for item in todo
            }
            for fut in as_completed(futures):
                item = futures[fut]
                pid = item[FIELD_NAMES[0]]
                try:
                    rec = fut.result()
                except Exception as e:
                    rec = {"product_id": pid, "record_id": item["record_id"], "status": "failed", "error": str(e)}
                    print(f"FAILED {pid}: {e}", file=sys.stderr, flush=True)
                else:
                    print(f"{rec['status'].upper()} {pid}", flush=True)
                summary[rec.get("status", "failed")] = summary.get(rec.get("status", "failed"), 0) + 1
                mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                mf.flush()
    (ROOT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
