#!/usr/bin/env python3
"""创建不可变的 Mask 审阅快照、确认回执和确认后门禁。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image


_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)

from workflow_state import (  # noqa: E402
    RunPaths,
    atomic_create_json,
    atomic_replace_json,
    load_state,
    transition_state,
)

_MASK_MODULE_NAME = "_jewelry_workflow_mask_contract"
_MASK_MODULE_PATH = Path(__file__).resolve().with_name("create_background_edit_mask.py")
_MASK_SPEC = importlib.util.spec_from_file_location(_MASK_MODULE_NAME, _MASK_MODULE_PATH)
if _MASK_SPEC is None or _MASK_SPEC.loader is None:
    raise ImportError(f"无法加载 Mask 技术门禁：{_MASK_MODULE_PATH}")
_MASK_MODULE = sys.modules.get(_MASK_MODULE_NAME)
if _MASK_MODULE is None:
    _MASK_MODULE = importlib.util.module_from_spec(_MASK_SPEC)
    sys.modules[_MASK_MODULE_NAME] = _MASK_MODULE
    _MASK_SPEC.loader.exec_module(_MASK_MODULE)
validate_published_mask_technical_gate = (
    _MASK_MODULE.validate_published_mask_technical_gate
)


IMAGE_ASSET_FIELDS = {
    "source_original_path",
    "geometry_detection_path",
    "local_detection_path",
    "cropped_original_path",
    "cropped_detection_path",
    "cropped_local_detection_path",
    "candidate_alpha_path",
    "mask_path",
    "overlay_path",
}


@dataclass(frozen=True)
class MaskReviewAssets:
    product_context_path: Path
    raw_source_path: Path
    source_original_path: Path
    geometry_detection_path: Path
    local_detection_path: Path
    source_geometry_path: Path
    crop_manifest_path: Path
    cropped_original_path: Path
    cropped_detection_path: Path
    cropped_local_detection_path: Path
    cropped_geometry_path: Path
    candidate_alpha_path: Path
    mask_path: Path
    overlay_path: Path
    draft_assessment_path: Path


@dataclass(frozen=True)
class MaskConfirmationGate:
    automatic_wawapi_edit_allowed: bool
    blockers: tuple[str, ...]
    report_path: Path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _relative_path(run_root: Path, path: Path, *, must_exist: bool = True) -> str:
    root = run_root.resolve()
    resolved = path.resolve(strict=must_exist)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("审阅资产必须位于当前运行目录") from exc


def _resolve_record_path(run_root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("审阅资产路径不合法")
    path = run_root / Path(relative)
    _relative_path(run_root, path)
    return path


def _asset_record(run_root: Path, name: str, path: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": _relative_path(run_root, path),
        "sha256": _sha256(path),
    }
    if name in IMAGE_ASSET_FIELDS:
        with Image.open(path) as image:
            image.load()
            record["size"] = list(image.size)
            record["mode"] = image.mode
    return record


def _asset_records(paths: RunPaths, assets: MaskReviewAssets) -> dict[str, Any]:
    return {
        field.name: _asset_record(paths.root, field.name, getattr(assets, field.name))
        for field in fields(assets)
    }


def _load_json(path: Path, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 必须是 JSON 对象")
    return payload


def _identity_from_context(paths: RunPaths, context: dict[str, Any]) -> dict[str, Any]:
    state = load_state(paths)
    keys = (
        "run_id",
        "product_id",
        "base_token",
        "table_id",
        "record_id",
        "front_field_id",
        "target_field_id",
    )
    identity = {key: state.get(key) for key in keys}
    if any(context.get(key) != value for key, value in identity.items()):
        raise ValueError("product_context 与运行身份不一致")
    front_token = context.get("front_file_token")
    if not isinstance(front_token, str) or not front_token:
        raise ValueError("product_context 缺少 front_file_token")
    identity["front_file_token"] = front_token
    return identity


def _bundle_path(paths: RunPaths) -> Path:
    return paths.root / "manifests" / "mask_review_bundle.json"


def _receipt_path(paths: RunPaths) -> Path:
    return paths.root / "confirmations" / "mask_confirmation.json"


def _gate_path(paths: RunPaths) -> Path:
    return paths.root / "logs" / f"{paths.product_id}_mask-gate.confirmed.json"


def _coerce_paths(paths: RunPaths | Any) -> RunPaths:
    if isinstance(paths, RunPaths):
        return paths
    root = getattr(paths, "root", None)
    if root is None:
        raise TypeError("paths 必须是 RunPaths")
    return RunPaths.from_root(root)


def _validate_asset_snapshot(paths: RunPaths, records: Any) -> tuple[str, ...]:
    if not isinstance(records, dict):
        return ("asset_snapshot_invalid",)
    blockers: list[str] = []
    for name, record in records.items():
        if not isinstance(record, dict):
            blockers.append("asset_snapshot_invalid")
            continue
        try:
            path = _resolve_record_path(paths.root, record.get("path"))
            if _sha256(path) != record.get("sha256"):
                blockers.append("asset_changed")
                continue
            if "size" in record:
                with Image.open(path) as image:
                    image.load()
                    if record.get("size") != list(image.size) or record.get(
                        "mode"
                    ) != image.mode:
                        blockers.append("asset_changed")
        except (OSError, ValueError):
            blockers.append("asset_changed")
    return tuple(dict.fromkeys(blockers))


def _technical_gate_from_records(
    paths: RunPaths,
    records: dict[str, Any],
) -> tuple[str, ...]:
    try:
        resolved = {
            name: _resolve_record_path(paths.root, record["path"])
            for name, record in records.items()
            if isinstance(record, dict)
        }
        gate = validate_published_mask_technical_gate(
            resolved["cropped_original_path"],
            resolved["cropped_detection_path"],
            resolved["cropped_local_detection_path"],
            resolved["candidate_alpha_path"],
            resolved["cropped_geometry_path"],
            resolved["crop_manifest_path"],
            mask_path=resolved["mask_path"],
            overlay_path=resolved["overlay_path"],
            draft_assessment_path=resolved["draft_assessment_path"],
            run_root=paths.root,
        )
        return gate.blockers if not gate.passed else ()
    except (KeyError, OSError, ValueError, TypeError):
        return ("technical_gate_failed",)


def _bind_bundle(payload: dict[str, Any], paths: RunPaths, bundle_path: Path) -> None:
    payload.setdefault("reviews", {})["mask_bundle"] = {
        "path": _relative_path(paths.root, bundle_path),
        "sha256": _sha256(bundle_path),
    }


def create_mask_review_bundle(
    paths: RunPaths,
    assets: MaskReviewAssets,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    paths = _coerce_paths(paths)
    state = load_state(paths)
    if state.get("status") != "mask_ready":
        raise ValueError("只有 mask_ready 状态可以创建 Mask review bundle")
    records = _asset_records(paths, assets)
    context = _load_json(assets.product_context_path, "product_context")
    identity = _identity_from_context(paths, context)
    if context.get("source_identity", {}).get("raw_source_path") != records[
        "raw_source_path"
    ]["path"]:
        raise ValueError("product_context 原始源图路径不一致")
    assessment = _load_json(assets.draft_assessment_path, "draft Mask Assessment")
    blockers = assessment.get("technical_blockers")
    if assessment.get("status") != "review" or blockers != []:
        raise ValueError("Mask 技术门禁未通过，不能创建审阅 bundle")
    reproduced_blockers = _technical_gate_from_records(paths, records)
    if reproduced_blockers:
        raise ValueError("Mask 技术门禁无法从已发布资产重现")
    cropped_geometry = _load_json(assets.cropped_geometry_path, "裁剪几何")
    uncertain = cropped_geometry.get("uncertain_regions", [])
    if not isinstance(uncertain, list):
        raise ValueError("裁剪几何 uncertain_regions 不合法")
    uncertain_ids = [item.get("id") for item in uncertain if isinstance(item, dict)]
    if any(not isinstance(item, str) or not item for item in uncertain_ids):
        raise ValueError("裁剪几何疑点 ID 不合法")
    bundle: dict[str, Any] = {
        "schema_version": "mask-review-bundle-1.0",
        **identity,
        "assets": records,
        "uncertain_region_ids": uncertain_ids,
        "created_at": _format_utc(now()),
    }
    path = _bundle_path(paths)
    if path.exists():
        existing = _load_json(path, "Mask review bundle")
        existing_blockers = _validate_asset_snapshot(paths, existing.get("assets"))
        if existing_blockers or any(
            existing.get(key) != bundle.get(key)
            for key in (
                "schema_version",
                "run_id",
                "product_id",
                "base_token",
                "table_id",
                "record_id",
                "front_field_id",
                "target_field_id",
                "front_file_token",
                "assets",
                "uncertain_region_ids",
            )
        ):
            raise ValueError("已存在的 Mask review bundle 与当前资产冲突")
        bundle = existing
    else:
        atomic_create_json(path, bundle)
    transition_state(
        paths,
        expected_status="mask_ready",
        expected_revision=int(state["state_revision"]),
        next_status="awaiting_mask_confirmation",
        event="mask_review_bundle_created",
        mutate=lambda payload: _bind_bundle(payload, paths, path),
        now=now,
    )
    return bundle


def _load_and_validate_bundle(paths: RunPaths) -> tuple[Path, dict[str, Any], tuple[str, ...]]:
    path = _bundle_path(paths)
    bundle = _load_json(path, "Mask review bundle")
    blockers = list(_validate_asset_snapshot(paths, bundle.get("assets")))
    state = load_state(paths)
    sealed = state.get("reviews", {}).get("mask_bundle")
    expected_seal = {
        "path": _relative_path(paths.root, path),
        "sha256": _sha256(path),
    }
    if sealed != expected_seal:
        blockers.append("bundle_changed")
    for key in (
        "run_id",
        "product_id",
        "base_token",
        "table_id",
        "record_id",
        "front_field_id",
        "target_field_id",
    ):
        if bundle.get(key) != state.get(key):
            blockers.append("bundle_identity_changed")
    return path, bundle, tuple(dict.fromkeys(blockers))


def confirm_mask_review(
    paths: RunPaths,
    bundle: dict[str, Any],
    *,
    reviewer: str,
    resolved_ids: list[str],
    now: Callable[[], datetime] = _utc_now,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> dict[str, Any]:
    paths = _coerce_paths(paths)
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("确认人不能为空")
    bundle_path, current_bundle, blockers = _load_and_validate_bundle(paths)
    if current_bundle != bundle or blockers:
        raise ValueError("Mask review bundle 或绑定资产已变化")
    expected_ids = current_bundle.get("uncertain_region_ids")
    if (
        not isinstance(resolved_ids, list)
        or len(set(resolved_ids)) != len(resolved_ids)
        or set(resolved_ids) != set(expected_ids)
    ):
        raise ValueError("已解析疑点必须恰好覆盖全部疑点 ID")
    confirmation_id = uuid_factory()
    if not isinstance(confirmation_id, uuid.UUID):
        raise TypeError("uuid_factory 必须返回 UUID")
    receipt = {
        "schema_version": "mask-confirmation-1.0",
        "confirmation_id": confirmation_id.hex,
        **{
            key: current_bundle[key]
            for key in (
                "run_id",
                "product_id",
                "base_token",
                "table_id",
                "record_id",
                "front_field_id",
                "target_field_id",
                "front_file_token",
            )
        },
        "decision": "confirmed",
        "confirmed_at": _format_utc(now()),
        "confirmed_by": reviewer.strip(),
        "review_bundle_path": _relative_path(paths.root, bundle_path),
        "review_bundle_sha256": _sha256(bundle_path),
        "resolved_uncertain_region_ids": resolved_ids,
        "assets": current_bundle["assets"],
    }
    receipt_path = _receipt_path(paths)
    atomic_create_json(receipt_path, receipt)
    gate = validate_mask_confirmation(paths, recover_state=False)
    if not gate.automatic_wawapi_edit_allowed:
        raise ValueError("Mask 确认后门禁未通过")
    state = load_state(paths)

    def bind_receipt(payload: dict[str, Any]) -> None:
        payload["receipts"]["mask"] = {
            "path": _relative_path(paths.root, receipt_path),
            "sha256": _sha256(receipt_path),
        }

    if state.get("status") == "awaiting_mask_confirmation":
        transition_state(
            paths,
            expected_status="awaiting_mask_confirmation",
            expected_revision=int(state["state_revision"]),
            next_status="mask_confirmed",
            event="mask_review_confirmed",
            mutate=bind_receipt,
            now=now,
        )
    return receipt


def validate_mask_confirmation(
    paths: RunPaths,
    *,
    recover_state: bool = True,
    now: Callable[[], datetime] = _utc_now,
) -> MaskConfirmationGate:
    paths = _coerce_paths(paths)
    blockers: list[str] = []
    receipt_path = _receipt_path(paths)
    try:
        receipt = _load_json(receipt_path, "Mask confirmation")
        bundle_path, bundle, bundle_blockers = _load_and_validate_bundle(paths)
        blockers.extend(bundle_blockers)
        state = load_state(paths)
        required_receipt_fields = {
            "schema_version",
            "confirmation_id",
            "run_id",
            "product_id",
            "base_token",
            "table_id",
            "record_id",
            "front_field_id",
            "target_field_id",
            "front_file_token",
            "decision",
            "confirmed_at",
            "confirmed_by",
            "review_bundle_path",
            "review_bundle_sha256",
            "resolved_uncertain_region_ids",
            "assets",
        }
        if set(receipt) != required_receipt_fields:
            blockers.append("confirmation_invalid")
        if receipt.get("schema_version") != "mask-confirmation-1.0":
            blockers.append("confirmation_invalid")
        confirmation_id = receipt.get("confirmation_id")
        if (
            not isinstance(confirmation_id, str)
            or len(confirmation_id) != 32
            or any(character not in "0123456789abcdef" for character in confirmation_id)
        ):
            blockers.append("confirmation_invalid")
        for key in (
            "run_id",
            "product_id",
            "base_token",
            "table_id",
            "record_id",
            "front_field_id",
            "target_field_id",
        ):
            if receipt.get(key) != state.get(key) or receipt.get(key) != bundle.get(key):
                blockers.append("confirmation_invalid")
        if receipt.get("front_file_token") != bundle.get("front_file_token"):
            blockers.append("confirmation_invalid")
        if receipt.get("decision") != "confirmed":
            blockers.append("confirmation_invalid")
        if not isinstance(receipt.get("confirmed_by"), str) or not receipt[
            "confirmed_by"
        ].strip():
            blockers.append("confirmation_invalid")
        confirmed_at = receipt.get("confirmed_at")
        try:
            if not isinstance(confirmed_at, str) or not confirmed_at.endswith("Z"):
                raise ValueError
            datetime.fromisoformat(confirmed_at[:-1] + "+00:00")
        except ValueError:
            blockers.append("confirmation_invalid")
        if receipt.get("review_bundle_path") != _relative_path(paths.root, bundle_path):
            blockers.append("confirmation_invalid")
        if receipt.get("review_bundle_sha256") != _sha256(bundle_path):
            blockers.append("bundle_changed")
        if receipt.get("assets") != bundle.get("assets"):
            blockers.append("confirmation_invalid")
        if set(receipt.get("assets", {})) != {
            field.name for field in fields(MaskReviewAssets)
        }:
            blockers.append("confirmation_invalid")
        expected_ids = bundle.get("uncertain_region_ids")
        resolved_ids = receipt.get("resolved_uncertain_region_ids")
        if (
            not isinstance(resolved_ids, list)
            or any(not isinstance(item, str) or not item for item in resolved_ids)
            or len(set(resolved_ids)) != len(resolved_ids)
            or set(resolved_ids) != set(expected_ids)
        ):
            blockers.append("unresolved_uncertain_regions")
        blockers.extend(_validate_asset_snapshot(paths, receipt.get("assets")))
        blockers.extend(_technical_gate_from_records(paths, receipt.get("assets", {})))
        bound_receipt = state.get("receipts", {}).get("mask")
        if state.get("status") == "mask_confirmed" and bound_receipt != {
            "path": _relative_path(paths.root, receipt_path),
            "sha256": _sha256(receipt_path),
        }:
            blockers.append("confirmation_invalid")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        blockers.append("confirmation_invalid")
    blockers = list(dict.fromkeys(blockers))
    allowed = not blockers
    report_path = _gate_path(paths)
    report = {
        "schema_version": "mask-confirmation-gate-1.0",
        "run_id": paths.run_id,
        "product_id": paths.product_id,
        "automatic_wawapi_edit_allowed": allowed,
        "blockers": blockers,
    }
    atomic_replace_json(report_path, report)
    if allowed and recover_state:
        state = load_state(paths)
        if state.get("status") == "awaiting_mask_confirmation":
            def bind_receipt(payload: dict[str, Any]) -> None:
                payload["receipts"]["mask"] = {
                    "path": _relative_path(paths.root, receipt_path),
                    "sha256": _sha256(receipt_path),
                }

            transition_state(
                paths,
                expected_status="awaiting_mask_confirmation",
                expected_revision=int(state["state_revision"]),
                next_status="mask_confirmed",
                event="mask_confirmation_recovered",
                mutate=bind_receipt,
                now=now,
            )
    return MaskConfirmationGate(allowed, tuple(blockers), report_path)


__all__ = [
    "MaskConfirmationGate",
    "MaskReviewAssets",
    "confirm_mask_review",
    "create_mask_review_bundle",
    "validate_mask_confirmation",
]
