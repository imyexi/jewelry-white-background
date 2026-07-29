#!/usr/bin/env python3
"""Run deterministic layout checks for a generated jewelry white-background image."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageOps

from reference_plan import PlanValidationError, load_plan, validate_plan


DEFAULT_POLICY: dict[str, float | str] = {
    "background_color": "#FBFCF9",
    "width_ratio_min": 0.45,
    "width_ratio_max": 0.55,
    "max_center_offset_ratio": 0.08,
    "edge_margin_ratio": 0.01,
    "strong_distance_threshold": 18.0,
    "weak_distance_threshold": 3.0,
    "min_strong_pixel_ratio": 0.001,
    "min_foreground_confidence": 0.60,
    "weak_background_coverage_ratio": 0.90,
    "ignore_bottom_ratio": 0.0,
}
ALLOWED_UPLOAD_DECISIONS = {
    "not_authorized",
    "blocked_by_qc",
    "requires_human_review",
    "approved_for_append",
    "appended_and_verified",
}


def _parse_color(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not value.startswith("#") or len(value) != 7:
        raise ValueError("background_color 必须是 #RRGGBB")
    try:
        return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))
    except ValueError as exc:
        raise ValueError("background_color 必须是 #RRGGBB") from exc


def _normalize_policy(policy: Mapping[str, Any] | None) -> dict[str, float | str]:
    result = dict(DEFAULT_POLICY)
    if policy:
        result.update(policy)

    _parse_color(str(result["background_color"]))
    for key in (
        "width_ratio_min",
        "width_ratio_max",
        "max_center_offset_ratio",
        "edge_margin_ratio",
        "strong_distance_threshold",
        "weak_distance_threshold",
        "min_strong_pixel_ratio",
        "min_foreground_confidence",
        "weak_background_coverage_ratio",
        "ignore_bottom_ratio",
    ):
        if isinstance(result[key], bool) or not isinstance(result[key], (int, float)):
            raise ValueError(f"{key} 必须是数字")
        result[key] = float(result[key])

    if not 0 < float(result["width_ratio_min"]) < float(result["width_ratio_max"]) <= 1:
        raise ValueError("产品宽度阈值无效")
    if not 0 < float(result["max_center_offset_ratio"]) <= 0.5:
        raise ValueError("中心偏移阈值无效")
    if not 0 <= float(result["ignore_bottom_ratio"]) < 0.5:
        raise ValueError("ignore_bottom_ratio 必须位于 0 到 0.5 之间")
    if not 0 < float(result["weak_distance_threshold"]) <= float(result["strong_distance_threshold"]):
        raise ValueError("前景阈值无效")
    if not 0 < float(result["min_strong_pixel_ratio"]) <= 1:
        raise ValueError("min_strong_pixel_ratio 必须位于 0 到 1 之间")
    if not 0 < float(result["min_foreground_confidence"]) <= 1:
        raise ValueError("min_foreground_confidence 必须位于 0 到 1 之间")
    if not 0 < float(result["weak_background_coverage_ratio"]) <= 1:
        raise ValueError("weak_background_coverage_ratio 必须位于 0 到 1 之间")
    return result


def _append_check(checks: list[dict[str, str]], check_id: str, status: str, message: str) -> None:
    checks.append({"id": check_id, "status": status, "message": message})


def evaluate_image(image_path: str | Path, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return pass/review/fail without altering the generated image."""

    normalized = _normalize_policy(policy)
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"图片不存在: {path}")

    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")

    width, height = image.size
    background = _parse_color(str(normalized["background_color"]))
    ignored_rows = math.floor(height * float(normalized["ignore_bottom_ratio"]))
    scanned_height = height - ignored_rows
    weak_threshold_sq = float(normalized["weak_distance_threshold"]) ** 2
    strong_threshold_sq = float(normalized["strong_distance_threshold"]) ** 2

    weak_count = 0
    strong_count = 0
    weak_left = weak_top = None
    weak_right = weak_bottom = None
    strong_left = strong_top = None
    strong_right = strong_bottom = None
    pixels = image.load()
    for y in range(scanned_height):
        for x in range(width):
            red, green, blue = pixels[x, y]
            distance_sq = (red - background[0]) ** 2 + (green - background[1]) ** 2 + (blue - background[2]) ** 2
            if distance_sq < weak_threshold_sq:
                continue
            weak_count += 1
            weak_left = x if weak_left is None else min(weak_left, x)
            weak_right = x if weak_right is None else max(weak_right, x)
            weak_top = y if weak_top is None else min(weak_top, y)
            weak_bottom = y if weak_bottom is None else max(weak_bottom, y)
            if distance_sq >= strong_threshold_sq:
                strong_count += 1
                strong_left = x if strong_left is None else min(strong_left, x)
                strong_right = x if strong_right is None else max(strong_right, x)
                strong_top = y if strong_top is None else min(strong_top, y)
                strong_bottom = y if strong_bottom is None else max(strong_bottom, y)

    checks: list[dict[str, str]] = []
    min_strong_pixels = max(1, math.ceil(width * scanned_height * float(normalized["min_strong_pixel_ratio"])))
    has_strong_foreground = strong_count >= min_strong_pixels
    scanned_pixels = width * scanned_height
    weak_coverage_ratio = weak_count / scanned_pixels
    weak_background_drift = has_strong_foreground and weak_coverage_ratio >= float(normalized["weak_background_coverage_ratio"])
    if has_strong_foreground:
        left, top, right, bottom = strong_left, strong_top, strong_right, strong_bottom
        detection_method = "strong_signal_background_drift" if weak_background_drift else "strong_signal"
    else:
        left, top, right, bottom = weak_left, weak_top, weak_right, weak_bottom
        detection_method = "weak_signal"

    if not weak_count or left is None or right is None or top is None or bottom is None:
        _append_check(checks, "foreground_detected", "fail", "未检测到足够的产品前景。")
        _append_check(checks, "composition_width", "review", "无法自动估算产品宽度。")
        _append_check(checks, "horizontal_center", "review", "无法自动估算水平中心。")
        _append_check(checks, "edge_clearance", "review", "无法自动估算边缘裁切风险。")
        _append_check(checks, "foreground_confidence", "review", "前景分割置信度不足，需要人工复核。")
        return {
            "image_path": str(path),
            "image_size": {"width": width, "height": height},
            "metrics": {
                "weak_pixel_count": weak_count,
                "strong_pixel_count": strong_count,
                "scanned_height": scanned_height,
                "foreground_detection_method": detection_method,
            },
            "checks": checks,
            "decision": "fail",
            "requires_human_review": True,
        }

    foreground_width = right - left + 1
    foreground_height = bottom - top + 1
    width_ratio = foreground_width / width
    product_center_x = (left + right) / 2
    canvas_center_x = (width - 1) / 2
    center_offset_ratio = abs(product_center_x - canvas_center_x) / width
    strong_pixel_ratio = strong_count / scanned_pixels
    confidence = 1.0 if weak_background_drift else strong_count / weak_count
    margin = max(1, math.ceil(width * float(normalized["edge_margin_ratio"])))
    strong_touches_edge = left <= margin or right >= width - 1 - margin or top <= margin or bottom >= scanned_height - 1 - margin
    weak_extends_beyond_strong = has_strong_foreground and not weak_background_drift and (
        weak_left < left or weak_right > right or weak_top < top or weak_bottom > bottom
    )
    weak_touches_edge = weak_left <= margin or weak_right >= width - 1 - margin or weak_top <= margin or weak_bottom >= scanned_height - 1 - margin
    weak_edge_ambiguity = weak_extends_beyond_strong and weak_touches_edge

    _append_check(checks, "foreground_detected", "pass", "已检测到产品前景。")
    width_status = "pass" if float(normalized["width_ratio_min"]) <= width_ratio <= float(normalized["width_ratio_max"]) else "fail"
    _append_check(
        checks,
        "composition_width",
        width_status,
        f"产品宽度占比为 {width_ratio:.3f}，目标范围为 {float(normalized['width_ratio_min']):.2f}-{float(normalized['width_ratio_max']):.2f}。",
    )
    center_status = "pass" if center_offset_ratio <= float(normalized["max_center_offset_ratio"]) else "fail"
    _append_check(
        checks,
        "horizontal_center",
        center_status,
        f"水平中心偏移为 {center_offset_ratio:.3f}，上限为 {float(normalized['max_center_offset_ratio']):.2f}。",
    )
    if strong_touches_edge:
        edge_status = "fail"
        edge_message = "强前景触及画面边缘，存在裁切风险。"
    elif weak_edge_ambiguity:
        edge_status = "review"
        edge_message = "浅色或透明前景可能触及画面边缘，需要人工复核裁切风险。"
    else:
        edge_status = "pass"
        edge_message = "产品与画面边缘保持安全距离。"
    _append_check(checks, "edge_clearance", edge_status, edge_message)
    confidence_status = (
        "pass"
        if has_strong_foreground
        and confidence >= float(normalized["min_foreground_confidence"])
        and not weak_extends_beyond_strong
        else "review"
    )
    _append_check(
        checks,
        "foreground_confidence",
        confidence_status,
        "已使用强前景信号估算产品边界。"
        if confidence_status == "pass"
        else "低置信度或存在浅色前景延展，需要人工复核。",
    )

    statuses = {check["status"] for check in checks}
    decision = "fail" if "fail" in statuses else "review" if "review" in statuses else "pass"
    return {
        "image_path": str(path),
        "image_size": {"width": width, "height": height},
        "foreground_bbox": {"left": left, "top": top, "right": right, "bottom": bottom},
        "metrics": {
            "foreground_width_ratio": round(width_ratio, 6),
            "foreground_height_ratio": round(foreground_height / height, 6),
            "horizontal_center_offset_ratio": round(center_offset_ratio, 6),
            "foreground_confidence": round(confidence, 6),
            "strong_pixel_ratio": round(strong_pixel_ratio, 6),
            "weak_coverage_ratio": round(weak_coverage_ratio, 6),
            "weak_background_drift": weak_background_drift,
            "weak_extends_beyond_strong": weak_extends_beyond_strong,
            "weak_pixel_count": weak_count,
            "strong_pixel_count": strong_count,
            "scanned_height": scanned_height,
            "foreground_detection_method": detection_method,
        },
        "checks": checks,
        "decision": decision,
        "requires_human_review": decision == "review",
    }


def policy_from_plan(plan: Mapping[str, Any]) -> dict[str, float | str]:
    data = validate_plan(plan)
    return {
        **DEFAULT_POLICY,
        "width_ratio_min": data["composition"]["width_ratio_min"],
        "width_ratio_max": data["composition"]["width_ratio_max"],
        "max_center_offset_ratio": data["composition"]["max_center_offset_ratio"],
    }


def _require_audit_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空文本")
    return value.strip()


def _qc_decision(qc: Mapping[str, Any], field: str) -> str:
    if not isinstance(qc, Mapping):
        raise ValueError(f"{field} 必须是对象")
    decision = qc.get("decision")
    if decision not in {"pass", "review", "fail"}:
        raise ValueError(f"{field}.decision 必须为 pass、review 或 fail")
    return str(decision)


def _normalize_manual_review(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("manual_review 必须是对象")
    status = value.get("status")
    if status not in {"approved", "rejected"}:
        raise ValueError("manual_review.status 必须为 approved 或 rejected")
    reviewer = _require_audit_text(value.get("reviewer"), "manual_review.reviewer")
    checked_items = value.get("checked_items")
    if not isinstance(checked_items, list) or not checked_items:
        raise ValueError("manual_review.checked_items 必须是非空数组")
    return {
        "status": status,
        "reviewer": reviewer,
        "checked_items": [_require_audit_text(item, "manual_review.checked_items[]") for item in checked_items],
    }


def _normalize_append_authorization(value: Mapping[str, Any] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("append_authorization 必须是对象")
    status = value.get("status")
    if status not in {"approved", "not_authorized"}:
        raise ValueError("append_authorization.status 必须为 approved 或 not_authorized")
    if status == "not_authorized":
        return {"status": status}
    return {
        "status": status,
        "approved_by": _require_audit_text(value.get("approved_by"), "append_authorization.approved_by"),
        "approval_reference": _require_audit_text(value.get("approval_reference"), "append_authorization.approval_reference"),
    }


def derive_upload_decision(
    *,
    pre_watermark_qc: Mapping[str, Any],
    post_watermark_qc: Mapping[str, Any],
    manual_review: Mapping[str, Any] | None = None,
    append_authorization: Mapping[str, Any] | None = None,
) -> str:
    """Derive the only safe upload state from QC, review, and authorization evidence."""

    pre_decision = _qc_decision(pre_watermark_qc, "pre_watermark_qc")
    post_decision = _qc_decision(post_watermark_qc, "post_watermark_qc")
    normalized_review = _normalize_manual_review(manual_review)
    normalized_authorization = _normalize_append_authorization(append_authorization)

    if "fail" in {pre_decision, post_decision}:
        return "blocked_by_qc"
    if "review" in {pre_decision, post_decision} and (not normalized_review or normalized_review["status"] != "approved"):
        return "requires_human_review"
    if not normalized_authorization or normalized_authorization["status"] != "approved":
        return "not_authorized"
    if not normalized_review or normalized_review["status"] != "approved":
        return "requires_human_review"
    return "approved_for_append"


def build_audit(
    *,
    prompt_version: str,
    reference_plan_path: str,
    pre_watermark_qc: Mapping[str, Any],
    post_watermark_qc: Mapping[str, Any],
    upload_decision: str,
    manual_review: Mapping[str, Any] | None = None,
    append_authorization: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the minimum auditable gate record; uploading remains an external action."""

    if not prompt_version.strip():
        raise ValueError("prompt_version 不能为空")
    if not reference_plan_path.strip():
        raise ValueError("reference_plan_path 不能为空")
    if upload_decision not in ALLOWED_UPLOAD_DECISIONS:
        raise ValueError("upload_decision 无效")
    derived_decision = derive_upload_decision(
        pre_watermark_qc=pre_watermark_qc,
        post_watermark_qc=post_watermark_qc,
        manual_review=manual_review,
        append_authorization=append_authorization,
    )
    if upload_decision != derived_decision:
        raise ValueError(f"上传决策与门禁结果不一致：应为 {derived_decision}")

    audit: dict[str, Any] = {
        "prompt_version": prompt_version,
        "reference_plan_path": reference_plan_path,
        "pre_watermark_qc": dict(pre_watermark_qc),
        "post_watermark_qc": dict(post_watermark_qc),
        "upload_decision": derived_decision,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if manual_review is not None:
        audit["manual_review"] = _normalize_manual_review(manual_review)
    if append_authorization is not None:
        audit["append_authorization"] = _normalize_append_authorization(append_authorization)
    return audit


def _load_json(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON 对象无效: {path}")
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查珠宝白底图的构图门禁，并可生成水印前后审计。")
    parser.add_argument("--image", required=True, help="待检查图片路径")
    parser.add_argument("--output", required=True, help="QC JSON 输出路径")
    parser.add_argument("--reference-plan", help="可选的 V2 reference_plan.json，用于读取构图阈值")
    parser.add_argument("--stage", choices=("pre-watermark", "post-watermark"), default="pre-watermark")
    parser.add_argument("--ignore-bottom-ratio", type=float, help="覆盖默认的底部忽略比例")
    parser.add_argument("--pre-watermark-qc", help="生成 audit 时读取的水印前 QC JSON")
    parser.add_argument("--prompt-version", help="生成 audit 时记录的提示词版本")
    parser.add_argument("--upload-decision", choices=sorted(ALLOWED_UPLOAD_DECISIONS), help="生成 audit 时记录的上传决策")
    parser.add_argument("--manual-review-json", help="人工复核记录 JSON；申请追加前必须为 approved")
    parser.add_argument("--append-authorization-json", help="用户追加授权记录 JSON；申请追加前必须为 approved")
    parser.add_argument("--audit-output", help="可选的 audit.json 输出路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        policy: dict[str, float | str] = dict(DEFAULT_POLICY)
        if args.reference_plan:
            policy = policy_from_plan(load_plan(args.reference_plan))
        if args.ignore_bottom_ratio is not None:
            policy["ignore_bottom_ratio"] = args.ignore_bottom_ratio
        qc = evaluate_image(args.image, policy)
        qc["stage"] = args.stage
        if args.reference_plan:
            qc["reference_plan_path"] = str(args.reference_plan)

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(qc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        if args.audit_output:
            if not (args.pre_watermark_qc and args.prompt_version and args.upload_decision and args.reference_plan):
                raise ValueError("生成 audit 需要 --pre-watermark-qc、--prompt-version、--upload-decision 和 --reference-plan")
            audit = build_audit(
                prompt_version=args.prompt_version,
                reference_plan_path=str(args.reference_plan),
                pre_watermark_qc=_load_json(args.pre_watermark_qc),
                post_watermark_qc=qc,
                upload_decision=args.upload_decision,
                manual_review=_load_json(args.manual_review_json) if args.manual_review_json else None,
                append_authorization=_load_json(args.append_authorization_json) if args.append_authorization_json else None,
            )
            audit_path = Path(args.audit_output)
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            qc["audit_path"] = str(audit_path)

        exit_code = {"pass": 0, "review": 3, "fail": 4}[qc["decision"]]
        print(
            json.dumps(
                {
                    "ok": qc["decision"] == "pass",
                    "decision": qc["decision"],
                    "output": str(output_path),
                    "audit_path": qc.get("audit_path"),
                },
                ensure_ascii=False,
            )
        )
        return exit_code
    except (FileNotFoundError, OSError, ValueError, PlanValidationError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
