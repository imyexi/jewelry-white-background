#!/usr/bin/env python3
"""Validate the factual input used by the background-only edit workflow."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping


PROMPT_VERSION = "v3.0"
GENERIC_COMPONENT_BUCKETS = (
    "所有非普通圆珠",
    "全部非普通圆珠",
    "所有可见配件",
    "所有配件",
    "全部配件",
    "所有金属配件",
    "金属配件",
    "非普通圆珠配件",
    "特殊件",
)
STRUCTURE_DETAIL_SOURCE_TERMS = ("细节图", "局部图", "详情图", "detail image", "detail_image")
RENDERING_PARAMETER_PATTERN = re.compile(r"(?:3\s*[:：]\s*4|2\s*[KkＫｋ])")
DEFAULT_COMPOSITION = {
    "width_ratio_min": 0.45,
    "width_ratio_max": 0.55,
    "max_center_offset_ratio": 0.08,
    "require_full_product": True,
}


class PlanValidationError(ValueError):
    """Raised when a reference plan cannot safely become a model prompt."""


def _fail(field: str, message: str) -> None:
    raise PlanValidationError(f"{field}: {message}")


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(field, "必须是非空文本")
    return value.strip()


def _require_prompt_text(value: Any, field: str) -> str:
    """Accept one factual line that cannot create a prompt section or render setting."""

    text = _require_text(value, field)
    if re.search(r"[\r\n\u2028\u2029]", text) or "【" in text or "】" in text:
        _fail(field, "不得包含换行或提示词标题符号")
    if RENDERING_PARAMETER_PATTERN.search(text):
        _fail(field, "画幅和分辨率不得进入背景编辑参考计划")
    return text


def _require_structure_text(value: Any, field: str) -> str:
    text = _require_prompt_text(value, field)
    if any(term in text.lower() for term in STRUCTURE_DETAIL_SOURCE_TERMS):
        _fail(field, "产品结构只能依据正面图，不能引用细节图或局部图")
    return text


def _require_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(field, "必须是数字")
    return float(value)


def _validate_image_path(value: Any, field: str, base_dir: Path | None) -> str:
    path_value = _require_text(value, field)
    if base_dir is not None:
        candidate = Path(path_value)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if not candidate.is_file():
            _fail(field, f"文件不存在: {candidate}")
    return path_value


def _validate_component(component: Any, index: int) -> dict[str, str]:
    field = f"structure.special_components[{index}]"
    if not isinstance(component, Mapping):
        _fail(field, "必须是对象")

    name = _require_structure_text(component.get("name"), f"{field}.name")
    if (
        any(marker in name for marker in GENERIC_COMPONENT_BUCKETS)
        or re.search(r"[,，、/]", name)
        or re.search(r"^(?:所有|全部|整串|全套).{0,16}(?:配件|部件|圆珠|异形珠)$", name)
    ):
        _fail(f"{field}.name", "必须填写单个、可在参考图中核对的特殊件名称")

    return {
        "name": name,
        "location": _require_structure_text(component.get("location"), f"{field}.location"),
        "visual_description": _require_structure_text(component.get("visual_description"), f"{field}.visual_description"),
    }


def load_plan(path: str | Path) -> dict[str, Any]:
    plan_path = Path(path)
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanValidationError(f"reference_plan: 文件不存在: {plan_path}") from exc
    except json.JSONDecodeError as exc:
        raise PlanValidationError(f"reference_plan: JSON 无法解析: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise PlanValidationError("reference_plan: 根节点必须是 JSON 对象")
    return data


def validate_plan(plan: Mapping[str, Any], base_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a normalized plan or raise a field-specific validation error."""

    if not isinstance(plan, Mapping):
        raise PlanValidationError("reference_plan: 根节点必须是 JSON 对象")
    base_path = Path(base_dir) if base_dir is not None else None
    data = copy.deepcopy(dict(plan))

    if data.get("schema_version") != "3.0":
        _fail("schema_version", "必须为 3.0")
    if data.get("workflow_mode") != "background_only_edit":
        _fail("workflow_mode", "必须为 background_only_edit")
    for forbidden_field in ("product_name", "product_parameters"):
        if forbidden_field in data:
            _fail(forbidden_field, "不得进入 reference plan；请保存在旁路 product_context.json")

    product_id = _require_text(data.get("product_id"), "product_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", product_id):
        _fail("product_id", "只能包含字母、数字、下划线或连字符")

    front_image = _validate_image_path(data.get("front_image"), "front_image", base_path)
    detail_images = data.get("detail_images", [])
    if not isinstance(detail_images, list) or len(detail_images) > 4:
        _fail("detail_images", "必须是最多 4 条的数组")
    normalized_details = [
        _validate_image_path(image_path, f"detail_images[{index}]", base_path)
        for index, image_path in enumerate(detail_images)
    ]

    structure = data.get("structure")
    if not isinstance(structure, Mapping):
        _fail("structure", "必须是对象")
    source_image = _validate_image_path(structure.get("source_image"), "structure.source_image", base_path)
    if source_image != front_image:
        _fail("structure.source_image", "必须与 front_image 完全相同")
    normalized_structure: dict[str, Any] = {
        "source_image": source_image,
        "bead_sequence": "",
        "thread": "",
        "special_components": [],
    }
    for field in ("bead_sequence", "thread"):
        if field in structure:
            normalized_structure[field] = _require_structure_text(structure[field], f"structure.{field}")
    if "special_components" in structure:
        components = structure["special_components"]
        if not isinstance(components, list):
            _fail("structure.special_components", "必须是数组")
        normalized_structure["special_components"] = [
            _validate_component(component, index) for index, component in enumerate(components)
        ]

    composition = data.get("composition")
    if composition is None:
        normalized_composition = dict(DEFAULT_COMPOSITION)
    else:
        if not isinstance(composition, Mapping):
            _fail("composition", "必须是对象")
        width_min = _require_number(composition.get("width_ratio_min"), "composition.width_ratio_min")
        width_max = _require_number(composition.get("width_ratio_max"), "composition.width_ratio_max")
        center_offset = _require_number(composition.get("max_center_offset_ratio"), "composition.max_center_offset_ratio")
        if width_min < 0.45 or width_min > 0.55:
            _fail("composition.width_ratio_min", "必须位于 0.45 到 0.55 之间")
        if width_max < 0.45 or width_max > 0.55 or width_min >= width_max:
            _fail("composition.width_ratio_max", "必须大于最小值且位于 0.45 到 0.55 之间")
        if center_offset <= 0 or center_offset > 0.10:
            _fail("composition.max_center_offset_ratio", "必须大于 0 且不超过 0.10")
        if composition.get("require_full_product") is not True:
            _fail("composition.require_full_product", "必须为 true")
        normalized_composition = {
            "width_ratio_min": width_min,
            "width_ratio_max": width_max,
            "max_center_offset_ratio": center_offset,
            "require_full_product": True,
        }

    return {
        "schema_version": "3.0",
        "workflow_mode": "background_only_edit",
        "product_id": product_id,
        "front_image": front_image,
        "detail_images": normalized_details,
        "structure": normalized_structure,
        "composition": normalized_composition,
    }
