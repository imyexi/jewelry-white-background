#!/usr/bin/env python3
"""校验 2.1 视觉几何，并在完整源图坐标系执行确定性裁剪。"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw


_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)

from workflow_state import atomic_create_bytes, atomic_create_json  # noqa: E402


_DIGEST_PATTERN = re.compile(r"[0-9A-F]{64}\Z")
_GEOMETRY_FIELDS = {
    "schema_version",
    "product_id",
    "source_sha256",
    "source_size",
    "detection_image_sha256",
    "detection_image_size",
    "detection_manifest_sha256",
    "coordinate_transform",
    "coordinate_range",
    "producer",
    "primitives",
    "uncertain_regions",
    "geometry_sha256",
}
_PRIMITIVE_TYPES = {"polygon", "polyline", "ellipse", "ellipse_set"}
_MAX_IMAGE_PIXELS = 20_000_000


@dataclass(frozen=True)
class GeometryExpectedIdentity:
    product_id: str
    source_sha256: str
    source_size: tuple[int, int]
    detection_image_sha256: str
    detection_image_size: tuple[int, int]
    detection_manifest_sha256: str


@dataclass(frozen=True)
class VisionGeometryV21:
    schema_version: str
    product_id: str
    source_sha256: str
    source_size: tuple[int, int]
    detection_image_sha256: str
    detection_image_size: tuple[int, int]
    detection_manifest_sha256: str
    coordinate_transform: str
    coordinate_range: tuple[int, int]
    producer: str
    primitives: Sequence[dict[str, Any]]
    uncertain_regions: Sequence[dict[str, Any]]
    geometry_sha256: str


@dataclass(frozen=True)
class CropOutputPaths:
    run_root: Path
    product_id: str
    cropped_original_path: Path
    cropped_detection_path: Path
    cropped_local_detection_path: Path
    candidate_alpha_path: Path
    cropped_geometry_path: Path
    crop_manifest_path: Path

    def published_paths(self) -> tuple[Path, ...]:
        return (
            self.cropped_original_path,
            self.cropped_detection_path,
            self.cropped_local_detection_path,
            self.candidate_alpha_path,
            self.cropped_geometry_path,
            self.crop_manifest_path,
        )


def _canonical_digest(payload: Mapping[str, Any], digest_field: str) -> str:
    digest_payload = {key: value for key, value in payload.items() if key != digest_field}
    encoded = json.dumps(
        digest_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是 64 位大写 SHA-256")
    return value


def _require_size(value: Any, label: str) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(item) is not int or item <= 0 for item in value)
    ):
        raise ValueError(f"{label} 必须是正整数 [width,height]")
    size = value[0], value[1]
    if size[0] * size[1] > _MAX_IMAGE_PIXELS:
        raise ValueError(f"{label} 不得超过 20MP")
    return size


def _require_coordinate(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 10000:
        raise ValueError(f"{label} 必须是 0..10000 整数")
    return value


def _require_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    return value


def _validate_points(value: Any, minimum: int, label: str) -> list[list[int]]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError(f"{label} 点数量不足")
    points: list[list[int]] = []
    for index, point in enumerate(value):
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError(f"{label}[{index}] 必须是 [x,y]")
        points.append(
            [
                _require_coordinate(point[0], f"{label}[{index}].x"),
                _require_coordinate(point[1], f"{label}[{index}].y"),
            ]
        )
    if len({tuple(point) for point in points}) < minimum:
        raise ValueError(f"{label} 必须包含 {minimum} 个不同点")
    return points


def _polygon_area_twice(points: Sequence[Sequence[int]]) -> int:
    return sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, (*points[1:], points[0]))
    )


def _validate_ellipse_params(value: Any, label: str) -> list[int]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{label} 必须是 [center_x,center_y,radius_x,radius_y]")
    params = [
        _require_coordinate(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    ]
    if params[2] <= 0 or params[3] <= 0:
        raise ValueError(f"{label} 椭圆半径必须为正")
    return params


def _validate_primitives(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("primitives 不得为空")
    identifiers: set[str] = set()
    validated: list[dict[str, Any]] = []

    def claim_identifier(identifier: Any) -> str:
        identifier = _require_identifier(identifier, "primitive id")
        if identifier in identifiers:
            raise ValueError(f"primitive id 重复：{identifier}")
        identifiers.add(identifier)
        return identifier

    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"primitive[{index}] 必须是对象")
        primitive = dict(raw)
        primitive_type = primitive.get("type")
        if primitive_type not in _PRIMITIVE_TYPES:
            raise ValueError(f"primitive[{index}] type 不受支持")
        required = {"id", "type", "semantic"}
        shape_fields = {
            "polygon": {"points"},
            "polyline": {"points", "width"},
            "ellipse": {"params"},
            "ellipse_set": {"items"},
        }[primitive_type]
        allowed = required | shape_fields | {"touches_border"}
        if set(primitive) != required | shape_fields and set(primitive) != allowed:
            raise ValueError(f"primitive[{index}] 字段不合法")
        claim_identifier(primitive["id"])
        _require_identifier(primitive["semantic"], f"primitive[{index}].semantic")
        if "touches_border" in primitive and type(primitive["touches_border"]) is not bool:
            raise ValueError("touches_border 必须是布尔值")

        if primitive_type == "polygon":
            points = _validate_points(primitive["points"], 3, f"primitive[{index}].points")
            if _polygon_area_twice(points) == 0:
                raise ValueError(f"primitive[{index}] polygon 面积不得为零")
        elif primitive_type == "polyline":
            _validate_points(primitive["points"], 2, f"primitive[{index}].points")
            width = _require_coordinate(primitive["width"], f"primitive[{index}].width")
            if width <= 0:
                raise ValueError(f"primitive[{index}] polyline width 必须为正")
        elif primitive_type == "ellipse":
            _validate_ellipse_params(primitive["params"], f"primitive[{index}].params")
        else:
            items = primitive["items"]
            if not isinstance(items, list) or not items:
                raise ValueError(f"primitive[{index}].items 不得为空")
            for item_index, item in enumerate(items):
                if not isinstance(item, dict) or set(item) != {"id", "params"}:
                    raise ValueError(f"primitive[{index}].items[{item_index}] 字段不合法")
                claim_identifier(item["id"])
                _validate_ellipse_params(
                    item["params"], f"primitive[{index}].items[{item_index}].params"
                )
        validated.append(primitive)
    return tuple(validated)


def _validate_uncertain_regions(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("uncertain_regions 必须是数组")
    identifiers: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) != {"id", "bbox", "reason"}:
            raise ValueError(f"uncertain_regions[{index}] 字段不合法")
        identifier = _require_identifier(raw["id"], f"uncertain_regions[{index}].id")
        if identifier in identifiers:
            raise ValueError(f"uncertain_regions id 重复：{identifier}")
        identifiers.add(identifier)
        _require_identifier(raw["reason"], f"uncertain_regions[{index}].reason")
        bbox = raw["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"uncertain_regions[{index}].bbox 必须有四个坐标")
        left, top, right, bottom = (
            _require_coordinate(item, f"uncertain_regions[{index}].bbox")
            for item in bbox
        )
        if right <= left or bottom <= top:
            raise ValueError(f"uncertain_regions[{index}].bbox 必须为正面积")
        validated.append(dict(raw))
    return tuple(validated)


def load_vision_geometry_v21(
    path: str | Path,
    expected_identity: GeometryExpectedIdentity,
) -> VisionGeometryV21:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != _GEOMETRY_FIELDS:
        raise ValueError("vision-geometry-mask-2.1 字段不完整或包含未知字段")
    if payload["schema_version"] != "vision-geometry-mask-2.1":
        raise ValueError("新裁剪流程只接受 vision-geometry-mask-2.1")

    source_size = _require_size(payload["source_size"], "source_size")
    detection_size = _require_size(
        payload["detection_image_size"], "detection_image_size"
    )
    actual_identity = GeometryExpectedIdentity(
        product_id=payload["product_id"],
        source_sha256=_require_digest(payload["source_sha256"], "source_sha256"),
        source_size=source_size,
        detection_image_sha256=_require_digest(
            payload["detection_image_sha256"], "detection_image_sha256"
        ),
        detection_image_size=detection_size,
        detection_manifest_sha256=_require_digest(
            payload["detection_manifest_sha256"], "detection_manifest_sha256"
        ),
    )
    if actual_identity != expected_identity:
        raise ValueError("视觉几何身份与当前运行身份不一致")
    if payload["coordinate_transform"] != "identity":
        raise ValueError("coordinate_transform 必须为 identity")
    if payload["coordinate_range"] != [0, 10000]:
        raise ValueError("coordinate_range 必须为 [0,10000]")
    if payload["producer"] != "codex-cloud-vision":
        raise ValueError("producer 必须为 codex-cloud-vision")
    primitives = _validate_primitives(payload["primitives"])
    uncertain_regions = _validate_uncertain_regions(payload["uncertain_regions"])
    geometry_sha256 = _require_digest(payload["geometry_sha256"], "geometry_sha256")
    if geometry_sha256 != _canonical_digest(payload, "geometry_sha256"):
        raise ValueError("geometry_sha256 与几何内容不一致")

    return VisionGeometryV21(
        schema_version=payload["schema_version"],
        product_id=actual_identity.product_id,
        source_sha256=actual_identity.source_sha256,
        source_size=source_size,
        detection_image_sha256=actual_identity.detection_image_sha256,
        detection_image_size=detection_size,
        detection_manifest_sha256=actual_identity.detection_manifest_sha256,
        coordinate_transform=payload["coordinate_transform"],
        coordinate_range=(0, 10000),
        producer=payload["producer"],
        primitives=primitives,
        uncertain_regions=uncertain_regions,
        geometry_sha256=geometry_sha256,
    )


def _quantize_point(point: Sequence[int], size: tuple[int, int]) -> list[int]:
    width, height = size
    return [
        round(point[0] * (width - 1) / 10000),
        round(point[1] * (height - 1) / 10000),
    ]


def quantize_ellipse(
    params: Sequence[int],
    source_size: tuple[int, int],
    crop_box: tuple[int, int, int, int] | None = None,
) -> list[int]:
    width, height = source_size
    center = _quantize_point(params[:2], source_size)
    quantized = [
        center[0],
        center[1],
        max(1, round(params[2] * (width - 1) / 10000)),
        max(1, round(params[3] * (height - 1) / 10000)),
    ]
    if crop_box is not None:
        quantized[0] -= crop_box[0]
        quantized[1] -= crop_box[1]
    return quantized


def _quantize_polyline_width(width: int, source_size: tuple[int, int]) -> int:
    return max(1, round(width * min(source_size) / 10000))


def _draw_quantized_primitive(
    draw: ImageDraw.ImageDraw,
    primitive: Mapping[str, Any],
    source_size: tuple[int, int],
) -> None:
    primitive_type = primitive["type"]
    if primitive_type == "polygon":
        draw.polygon(
            [_quantize_point(point, source_size) for point in primitive["points"]],
            fill=255,
        )
    elif primitive_type == "polyline":
        draw.line(
            [_quantize_point(point, source_size) for point in primitive["points"]],
            fill=255,
            width=_quantize_polyline_width(primitive["width"], source_size),
            joint="curve",
        )
    elif primitive_type == "ellipse":
        center_x, center_y, radius_x, radius_y = quantize_ellipse(
            primitive["params"], source_size
        )
        draw.ellipse(
            (
                center_x - radius_x,
                center_y - radius_y,
                center_x + radius_x,
                center_y + radius_y,
            ),
            fill=255,
        )
    else:
        for item in primitive["items"]:
            center_x, center_y, radius_x, radius_y = quantize_ellipse(
                item["params"], source_size
            )
            draw.ellipse(
                (
                    center_x - radius_x,
                    center_y - radius_y,
                    center_x + radius_x,
                    center_y + radius_y,
                ),
                fill=255,
            )


def rasterize_source_geometry(geometry: VisionGeometryV21) -> Image.Image:
    alpha = Image.new("L", geometry.source_size, 0)
    draw = ImageDraw.Draw(alpha)
    for primitive in geometry.primitives:
        _draw_quantized_primitive(draw, primitive, geometry.source_size)
    return alpha


def _uncertain_region_box(
    region: Mapping[str, Any],
    source_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    width, height = source_size
    left, top, right, bottom = region["bbox"]
    return (
        math.floor(left * (width - 1) / 10000),
        math.floor(top * (height - 1) / 10000),
        min(width, math.ceil(right * (width - 1) / 10000) + 1),
        min(height, math.ceil(bottom * (height - 1) / 10000) + 1),
    )


def compute_content_box(
    alpha: Image.Image,
    uncertain_regions: Sequence[Mapping[str, Any]],
) -> tuple[int, int, int, int]:
    if alpha.mode != "L":
        raise ValueError("候选 Alpha 必须为 L 模式")
    boxes = [box for box in [alpha.getbbox()] if box is not None]
    boxes.extend(
        _uncertain_region_box(region, alpha.size) for region in uncertain_regions
    )
    if not boxes:
        raise ValueError("候选 Alpha 和疑点区域均为空")
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def compute_crop_box(
    source_size: tuple[int, int],
    content_box: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    source_width, source_height = source_size
    left, top, right, bottom = content_box
    if not (0 <= left < right <= source_width and 0 <= top < bottom <= source_height):
        raise ValueError("content_box 必须位于源图内且具有正面积")
    content_width = right - left
    content_height = bottom - top
    desired_width = (content_width * 100 + 76) // 77
    desired_height = (content_height * 100 + 76) // 77
    crop_width = min(source_width, max(content_width, desired_width))
    crop_height = min(source_height, max(content_height, desired_height))
    ideal_left = math.ceil((left + right - crop_width) / 2)
    ideal_top = math.ceil((top + bottom - crop_height) / 2)
    crop_left = min(max(ideal_left, 0), source_width - crop_width)
    crop_top = min(max(ideal_top, 0), source_height - crop_height)
    return (
        crop_left,
        crop_top,
        crop_left + crop_width,
        crop_top + crop_height,
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _relative_run_path(run_root: Path, path: Path, *, must_exist: bool) -> str:
    root = run_root.resolve()
    resolved = path.resolve(strict=must_exist)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("裁剪输入和输出必须位于当前运行目录") from exc


def _load_exact_image(
    path: Path,
    *,
    expected_mode: str,
    expected_size: tuple[int, int] | None = None,
) -> Image.Image:
    with Image.open(path) as opened:
        opened.load()
        if opened.format != "PNG" or opened.mode != expected_mode:
            raise ValueError(f"{path.name} 必须是真实 {expected_mode} PNG")
        _require_size(list(opened.size), path.name)
        if expected_size is not None and opened.size != expected_size:
            raise ValueError(f"{path.name} 尺寸与源图不一致")
        return opened.copy()


def _validate_detection_manifest(
    manifest_path: Path,
    run_root: Path,
    source_path: Path,
    global_path: Path,
    local_path: Path,
    source_size: tuple[int, int],
) -> dict[str, Any]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Detection Manifest 必须是 JSON 对象")
    if payload.get("schema_version") != "mask-detection-images-2.0":
        raise ValueError("Detection Manifest 必须为 2.0")
    if payload.get("coordinate_transform") != "identity":
        raise ValueError("Detection Manifest 坐标转换必须为 identity")
    if payload.get("source_sha256") != _sha256_file(source_path):
        raise ValueError("Detection Manifest 源图摘要不一致")
    if payload.get("source_size") != list(source_size):
        raise ValueError("Detection Manifest 源图尺寸不一致")
    outputs = payload.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("Detection Manifest 缺少 outputs")
    for name, path in (("global_robust", global_path), ("local_limited", local_path)):
        item = outputs.get(name)
        if not isinstance(item, dict):
            raise ValueError(f"Detection Manifest 缺少 {name} 检测输出")
        expected = {
            "path": _relative_run_path(run_root, path, must_exist=True),
            "sha256": _sha256_file(path),
            "size": list(source_size),
            "mode": "L",
        }
        if any(item.get(key) != value for key, value in expected.items()):
            raise ValueError(f"Detection Manifest 的 {name} 检测资产不一致")
    return payload


def _translate_point(
    point: Sequence[int],
    source_size: tuple[int, int],
    crop_box: tuple[int, int, int, int],
) -> list[int]:
    quantized = _quantize_point(point, source_size)
    return [quantized[0] - crop_box[0], quantized[1] - crop_box[1]]


def _cropped_primitive(
    primitive: Mapping[str, Any],
    source_size: tuple[int, int],
    crop_box: tuple[int, int, int, int],
) -> dict[str, Any]:
    result = {
        key: primitive[key]
        for key in ("id", "type", "semantic", "touches_border")
        if key in primitive
    }
    primitive_type = primitive["type"]
    if primitive_type in {"polygon", "polyline"}:
        result["points"] = [
            _translate_point(point, source_size, crop_box)
            for point in primitive["points"]
        ]
        if primitive_type == "polyline":
            result["width"] = _quantize_polyline_width(
                primitive["width"], source_size
            )
    elif primitive_type == "ellipse":
        result["params"] = quantize_ellipse(
            primitive["params"], source_size, crop_box
        )
    else:
        result["items"] = [
            {
                "id": item["id"],
                "params": quantize_ellipse(item["params"], source_size, crop_box),
            }
            for item in primitive["items"]
        ]
    return result


def _cropped_geometry_payload(
    geometry: VisionGeometryV21,
    crop_box: tuple[int, int, int, int],
) -> dict[str, Any]:
    crop_width = crop_box[2] - crop_box[0]
    crop_height = crop_box[3] - crop_box[1]
    payload: dict[str, Any] = {
        "schema_version": "vision-cropped-geometry-1.0",
        "product_id": geometry.product_id,
        "source_geometry_sha256": geometry.geometry_sha256,
        "source_sha256": geometry.source_sha256,
        "detection_image_sha256": geometry.detection_image_sha256,
        "source_size": list(geometry.source_size),
        "crop_box": list(crop_box),
        "crop_size": [crop_width, crop_height],
        "coordinate_space": "crop-pixel",
        "coordinate_bounds": [0, 0, crop_width, crop_height],
        "primitives": [
            _cropped_primitive(primitive, geometry.source_size, crop_box)
            for primitive in geometry.primitives
        ],
        "uncertain_regions": [
            {
                "id": region["id"],
                "bbox": [
                    _uncertain_region_box(region, geometry.source_size)[0]
                    - crop_box[0],
                    _uncertain_region_box(region, geometry.source_size)[1]
                    - crop_box[1],
                    _uncertain_region_box(region, geometry.source_size)[2]
                    - crop_box[0],
                    _uncertain_region_box(region, geometry.source_size)[3]
                    - crop_box[1],
                ],
                "reason": region["reason"],
            }
            for region in geometry.uncertain_regions
        ],
    }
    payload["cropped_geometry_sha256"] = _canonical_digest(
        payload, "cropped_geometry_sha256"
    )
    return payload


def _draw_cropped_primitive(
    draw: ImageDraw.ImageDraw,
    primitive: Mapping[str, Any],
) -> None:
    primitive_type = primitive["type"]
    if primitive_type == "polygon":
        draw.polygon(primitive["points"], fill=255)
    elif primitive_type == "polyline":
        draw.line(
            primitive["points"],
            fill=255,
            width=primitive["width"],
            joint="curve",
        )
    elif primitive_type == "ellipse":
        center_x, center_y, radius_x, radius_y = primitive["params"]
        draw.ellipse(
            (
                center_x - radius_x,
                center_y - radius_y,
                center_x + radius_x,
                center_y + radius_y,
            ),
            fill=255,
        )
    else:
        for item in primitive["items"]:
            center_x, center_y, radius_x, radius_y = item["params"]
            draw.ellipse(
                (
                    center_x - radius_x,
                    center_y - radius_y,
                    center_x + radius_x,
                    center_y + radius_y,
                ),
                fill=255,
            )


def rasterize_cropped_geometry(payload: Mapping[str, Any]) -> Image.Image:
    if payload.get("schema_version") != "vision-cropped-geometry-1.0":
        raise ValueError("裁剪几何 schema 不受支持")
    if payload.get("cropped_geometry_sha256") != _canonical_digest(
        payload, "cropped_geometry_sha256"
    ):
        raise ValueError("cropped_geometry_sha256 与内容不一致")
    size = _require_size(payload.get("crop_size"), "crop_size")
    if payload.get("coordinate_space") != "crop-pixel" or payload.get(
        "coordinate_bounds"
    ) != [0, 0, size[0], size[1]]:
        raise ValueError("裁剪几何坐标空间不合法")
    alpha = Image.new("L", size, 0)
    draw = ImageDraw.Draw(alpha)
    primitives = payload.get("primitives")
    if not isinstance(primitives, list) or not primitives:
        raise ValueError("裁剪几何 primitives 不得为空")
    for primitive in primitives:
        _draw_cropped_primitive(draw, primitive)
    return alpha


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _output_record(
    run_root: Path,
    path: Path,
    data: bytes,
    size: tuple[int, int],
    mode: str,
) -> dict[str, Any]:
    return {
        "path": _relative_run_path(run_root, path, must_exist=False),
        "sha256": hashlib.sha256(data).hexdigest().upper(),
        "size": list(size),
        "mode": mode,
    }


def _touches_canvas_border(alpha: Image.Image) -> bool:
    width, height = alpha.size
    return any(
        alpha.getpixel(point) != 0
        for point in (
            *((x, 0) for x in range(width)),
            *((x, height - 1) for x in range(width)),
            *((0, y) for y in range(height)),
            *((width - 1, y) for y in range(height)),
        )
    )


def _verified_border_ids(geometry: VisionGeometryV21) -> list[str]:
    verified: list[str] = []
    for primitive in geometry.primitives:
        if not primitive.get("touches_border", False):
            continue
        alpha = Image.new("L", geometry.source_size, 0)
        _draw_quantized_primitive(ImageDraw.Draw(alpha), primitive, geometry.source_size)
        if not _touches_canvas_border(alpha):
            raise ValueError(
                f"touches_border 图元未真实接触源图边界：{primitive['id']}"
            )
        verified.append(primitive["id"])
    return verified


def create_geometry_crop_assets(
    source_path: str | Path,
    global_detection_path: str | Path,
    local_detection_path: str | Path,
    detection_manifest_path: str | Path,
    geometry_path: str | Path,
    outputs: CropOutputPaths,
) -> dict[str, Any]:
    source_path = Path(source_path)
    global_detection_path = Path(global_detection_path)
    local_detection_path = Path(local_detection_path)
    detection_manifest_path = Path(detection_manifest_path)
    geometry_path = Path(geometry_path)
    run_root = outputs.run_root
    input_paths = (
        source_path,
        global_detection_path,
        local_detection_path,
        detection_manifest_path,
        geometry_path,
    )
    for path in input_paths:
        _relative_run_path(run_root, path, must_exist=True)
    for path in outputs.published_paths():
        _relative_run_path(run_root, path, must_exist=False)
        if path.exists():
            raise FileExistsError(path)
    if len({path.resolve() for path in (*input_paths, *outputs.published_paths())}) != 11:
        raise ValueError("裁剪输入输出路径必须互不相同")

    source = _load_exact_image(source_path, expected_mode="RGB")
    global_detection = _load_exact_image(
        global_detection_path, expected_mode="L", expected_size=source.size
    )
    local_detection = _load_exact_image(
        local_detection_path, expected_mode="L", expected_size=source.size
    )
    _validate_detection_manifest(
        detection_manifest_path,
        run_root,
        source_path,
        global_detection_path,
        local_detection_path,
        source.size,
    )
    expected_identity = GeometryExpectedIdentity(
        product_id=outputs.product_id,
        source_sha256=_sha256_file(source_path),
        source_size=source.size,
        detection_image_sha256=_sha256_file(global_detection_path),
        detection_image_size=global_detection.size,
        detection_manifest_sha256=_sha256_file(detection_manifest_path),
    )
    geometry = load_vision_geometry_v21(geometry_path, expected_identity)
    verified_border_ids = _verified_border_ids(geometry)
    full_alpha = rasterize_source_geometry(geometry)
    content_box = compute_content_box(full_alpha, geometry.uncertain_regions)
    crop_box = compute_crop_box(source.size, content_box)
    crop_size = (crop_box[2] - crop_box[0], crop_box[3] - crop_box[1])
    cropped_original = source.crop(crop_box)
    cropped_detection = global_detection.crop(crop_box)
    cropped_local = local_detection.crop(crop_box)
    candidate_alpha = full_alpha.crop(crop_box)
    if candidate_alpha.histogram()[255] != full_alpha.histogram()[255]:
        raise ValueError("裁剪候选 Alpha 丢失了源图内保护像素")

    cropped_geometry = _cropped_geometry_payload(geometry, crop_box)
    audited_alpha = rasterize_cropped_geometry(cropped_geometry)
    if audited_alpha.tobytes() != candidate_alpha.tobytes():
        raise ValueError("裁剪几何栅格与候选 Alpha 不一致")

    original_bytes = _png_bytes(cropped_original)
    detection_bytes = _png_bytes(cropped_detection)
    local_bytes = _png_bytes(cropped_local)
    alpha_bytes = _png_bytes(candidate_alpha)
    cropped_geometry_bytes = _json_bytes(cropped_geometry)
    content_width = content_box[2] - content_box[0]
    content_height = content_box[3] - content_box[1]
    desired_width = (content_width * 100 + 76) // 77
    desired_height = (content_height * 100 + 76) // 77
    manifest: dict[str, Any] = {
        "schema_version": "geometry-crop-manifest-1.0",
        "product_id": geometry.product_id,
        "source_geometry_sha256": geometry.geometry_sha256,
        "source_sha256": geometry.source_sha256,
        "detection_image_sha256": geometry.detection_image_sha256,
        "local_detection_image_sha256": _sha256_file(local_detection_path),
        "detection_manifest_sha256": geometry.detection_manifest_sha256,
        "source_size": list(source.size),
        "content_box": list(content_box),
        "crop_box": list(crop_box),
        "crop_size": list(crop_size),
        "target_max_occupancy": [77, 100],
        "actual_occupancy": {
            "width": content_width / crop_size[0],
            "height": content_height / crop_size[1],
        },
        "source_limited_axes": [
            axis
            for axis, limited in (
                ("width", desired_width > source.width),
                ("height", desired_height > source.height),
            )
            if limited
        ],
        "verified_source_border_primitive_ids": verified_border_ids,
        "source_geometry": {
            "path": _relative_run_path(run_root, geometry_path, must_exist=True),
            "sha256": _sha256_file(geometry_path),
            "semantic_sha256": geometry.geometry_sha256,
        },
        "outputs": {
            "cropped_original": _output_record(
                run_root,
                outputs.cropped_original_path,
                original_bytes,
                crop_size,
                "RGB",
            ),
            "cropped_detection": _output_record(
                run_root,
                outputs.cropped_detection_path,
                detection_bytes,
                crop_size,
                "L",
            ),
            "cropped_local_detection": _output_record(
                run_root,
                outputs.cropped_local_detection_path,
                local_bytes,
                crop_size,
                "L",
            ),
            "candidate_alpha": _output_record(
                run_root,
                outputs.candidate_alpha_path,
                alpha_bytes,
                crop_size,
                "L",
            ),
        },
        "cropped_geometry": {
            "path": _relative_run_path(
                run_root, outputs.cropped_geometry_path, must_exist=False
            ),
            "sha256": hashlib.sha256(cropped_geometry_bytes).hexdigest().upper(),
            "semantic_sha256": cropped_geometry["cropped_geometry_sha256"],
        },
        "crop_algorithm": "geometry-crop-77-over-100-v1",
    }

    atomic_create_bytes(outputs.cropped_original_path, original_bytes)
    atomic_create_bytes(outputs.cropped_detection_path, detection_bytes)
    atomic_create_bytes(outputs.cropped_local_detection_path, local_bytes)
    atomic_create_bytes(outputs.candidate_alpha_path, alpha_bytes)
    atomic_create_bytes(outputs.cropped_geometry_path, cropped_geometry_bytes)
    atomic_create_json(outputs.crop_manifest_path, manifest)
    return manifest


__all__ = [
    "GeometryExpectedIdentity",
    "CropOutputPaths",
    "VisionGeometryV21",
    "compute_content_box",
    "compute_crop_box",
    "create_geometry_crop_assets",
    "load_vision_geometry_v21",
    "quantize_ellipse",
    "rasterize_source_geometry",
    "rasterize_cropped_geometry",
]
