#!/usr/bin/env python3
"""Validate the factual input used to build a V2 jewelry white-background prompt."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping


PROMPT_VERSION = "v2.2"
DISALLOWED_VISUAL_EFFECT_TERMS = ("闪烁感", "星芒", "亮片", "爆闪", "光晕")
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
MATERIAL_CORRECTION_STRUCTURE_TERMS = ("删除", "新增", "重排", "移动", "替换", "顺序", "位置", "数量", "特殊件", "配件", "串线")
RENDERING_PARAMETER_PATTERN = re.compile(r"(?:3\s*[:：]\s*4|2\s*[KkＫｋ])")


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
        _fail(field, "画幅和分辨率只能作为 AIReiter 参数传递")
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


def _validate_observation(observation: Any, index: int, allowed_sources: set[str]) -> dict[str, str]:
    field = f"material_observations[{index}]"
    if not isinstance(observation, Mapping):
        _fail(field, "必须是对象")

    source_image = _require_text(observation.get("source_image"), f"{field}.source_image")
    if source_image not in allowed_sources:
        _fail(f"{field}.source_image", "必须指向正面图或已声明的细节图")
    description = _require_prompt_text(observation.get("description"), f"{field}.description")
    if any(term in description for term in DISALLOWED_VISUAL_EFFECT_TERMS):
        _fail(f"{field}.description", "不得包含未从参考图核实的视觉特效词")

    return {
        "subject": _require_prompt_text(observation.get("subject"), f"{field}.subject"),
        "source_image": source_image,
        "description": description,
    }


def _validate_corrections(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {"structure": [], "material": []}
    if not isinstance(value, Mapping):
        _fail("corrections", "必须是对象")

    result: dict[str, list[str]] = {}
    for key in ("structure", "material"):
        entries = value.get(key, [])
        if not isinstance(entries, list) or len(entries) > 3:
            _fail(f"corrections.{key}", "必须是最多 3 条的文本数组")
        result[key] = []
        for index, entry in enumerate(entries):
            text = _require_prompt_text(entry, f"corrections.{key}[{index}]")
            if any(term in text for term in DISALLOWED_VISUAL_EFFECT_TERMS):
                _fail(f"corrections.{key}[{index}]", "不得包含未从参考图核实的视觉特效词")
            if key == "material" and any(term in text for term in MATERIAL_CORRECTION_STRUCTURE_TERMS):
                _fail(f"corrections.{key}[{index}]", "材质纠偏不得新增、删除或重排产品结构")
            result[key].append(text)
    return result


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

    if data.get("schema_version") != "2.0":
        _fail("schema_version", "必须为 2.0")

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
    components = structure.get("special_components", [])
    if not isinstance(components, list):
        _fail("structure.special_components", "必须是数组")
    normalized_structure = {
        "bead_sequence": _require_structure_text(structure.get("bead_sequence"), "structure.bead_sequence"),
        "thread": _require_structure_text(structure.get("thread"), "structure.thread"),
        "special_components": [_validate_component(component, index) for index, component in enumerate(components)],
    }

    observations = data.get("material_observations")
    if not isinstance(observations, list) or not observations:
        _fail("material_observations", "必须至少包含一条基于参考图的材质观察")
    allowed_observation_sources = {front_image, *normalized_details}
    normalized_observations = [
        _validate_observation(observation, index, allowed_observation_sources)
        for index, observation in enumerate(observations)
    ]

    composition = data.get("composition")
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

    review_items = data.get("manual_review_items")
    if not isinstance(review_items, list) or not review_items:
        _fail("manual_review_items", "必须至少包含一条人工复核项")
    normalized_review_items = [_require_text(item, f"manual_review_items[{index}]") for index, item in enumerate(review_items)]

    return {
        "schema_version": "2.0",
        "product_id": product_id,
        "front_image": front_image,
        "detail_images": normalized_details,
        "product_name": str(data.get("product_name") or "").strip(),
        "product_parameters": str(data.get("product_parameters") or "").strip(),
        "structure": normalized_structure,
        "material_observations": normalized_observations,
        "composition": {
            "width_ratio_min": width_min,
            "width_ratio_max": width_max,
            "max_center_offset_ratio": center_offset,
            "require_full_product": True,
        },
        "manual_review_items": normalized_review_items,
        "corrections": _validate_corrections(data.get("corrections")),
    }
