#!/usr/bin/env python3
"""只根据 Wawapi 生成结果执行确定性商品排版。"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageCms, ImageOps


ALGORITHM_VERSION = "layout-algorithm-1.0"
CANVAS_SIZE = (1536, 2048)
TARGET_PRODUCT_WIDTH = 922
TARGET_CENTER = (768, 900)
PRODUCT_HEIGHT_LIMIT = 1800
MAX_INTERMEDIATE_PIXELS = 20_000_000
STRONG_DISTANCE = 18
WEAK_DISTANCE = 3


class LayoutError(RuntimeError):
    """生成结果无法安全排版。"""


def round_signed(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_create(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_generated_rgb(path: Path) -> tuple[Image.Image, str]:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            raise LayoutError("生成结果不存在或为空")
        with Image.open(path) as opened:
            source_format = opened.format
            if source_format not in {"PNG", "JPEG"}:
                raise LayoutError("生成结果必须是真实 PNG 或 JPEG")
            opened.verify()
        with Image.open(path) as opened:
            icc_profile = opened.info.get("icc_profile")
            has_palette_transparency = "transparency" in opened.info
            oriented = ImageOps.exif_transpose(opened)
            oriented.load()
            if "A" in oriented.getbands() or has_palette_transparency:
                rgba = oriented.convert("RGBA")
                alpha = rgba.getchannel("A")
                if oriented.mode == "LA":
                    color_source = oriented.getchannel("L")
                elif oriented.mode in {"L", "RGB"}:
                    color_source = oriented
                else:
                    color_source = rgba.convert("RGB")
                if icc_profile:
                    source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
                    target_profile = ImageCms.createProfile("sRGB")
                    color = ImageCms.profileToProfile(
                        color_source,
                        source_profile,
                        target_profile,
                        outputMode="RGB",
                    )
                else:
                    color = color_source.convert("RGB")
                normalized = Image.composite(
                    color, Image.new("RGB", color.size, (255, 255, 255)), alpha
                )
            else:
                if icc_profile:
                    source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
                    target_profile = ImageCms.createProfile("sRGB")
                    normalized = ImageCms.profileToProfile(
                        oriented,
                        source_profile,
                        target_profile,
                        outputMode="RGB",
                    )
                else:
                    normalized = oriented.convert("RGB")
            normalized.load()
    except LayoutError:
        raise
    except (OSError, ValueError, ImageCms.PyCMSError) as exc:
        raise LayoutError("生成结果无法完整解码或转换为 sRGB RGB") from exc
    return normalized, source_format


def _median_up(histogram: list[int], count: int) -> int:
    targets = ((count - 1) // 2, count // 2)
    values = []
    cumulative = 0
    target_index = 0
    for value, occurrences in enumerate(histogram):
        cumulative += occurrences
        while target_index < 2 and cumulative > targets[target_index]:
            values.append(value)
            target_index += 1
    return (values[0] + values[1] + 1) // 2


def _corner_background(image: Image.Image) -> tuple[tuple[int, int, int], int, list[list[int]]]:
    width, height = image.size
    edge = max(8, math.floor(min(width, height) * 0.05))
    edge = min(edge, width, height)
    ranges = [
        [0, 0, edge, edge],
        [width - edge, 0, width, edge],
        [0, height - edge, edge, height],
        [width - edge, height - edge, width, height],
    ]
    histograms = [[0] * 256 for _ in range(3)]
    count = 0
    for box in ranges:
        data = image.crop(tuple(box)).tobytes()
        count += len(data) // 3
        for offset in range(0, len(data), 3):
            histograms[0][data[offset]] += 1
            histograms[1][data[offset + 1]] += 1
            histograms[2][data[offset + 2]] += 1
    background = tuple(_median_up(channel, count) for channel in histograms)
    return background, edge, ranges


def _foreground_masks(
    image: Image.Image, background: tuple[int, int, int]
) -> tuple[bytearray, bytearray, int, int]:
    raw = image.tobytes()
    pixel_count = image.width * image.height
    strong = bytearray(pixel_count)
    weak = bytearray(pixel_count)
    strong_count = 0
    weak_count = 0
    strong_squared = STRONG_DISTANCE * STRONG_DISTANCE
    weak_squared = WEAK_DISTANCE * WEAK_DISTANCE
    br, bg, bb = background
    for index in range(pixel_count):
        offset = index * 3
        dr = raw[offset] - br
        dg = raw[offset + 1] - bg
        db = raw[offset + 2] - bb
        distance_squared = dr * dr + dg * dg + db * db
        if distance_squared >= weak_squared:
            weak[index] = 1
            weak_count += 1
        if distance_squared >= strong_squared:
            strong[index] = 1
            strong_count += 1
    return strong, weak, strong_count, weak_count


def _retain_connected_weak(
    strong: bytearray, weak: bytearray, width: int, height: int
) -> bytearray:
    retained = bytearray(width * height)
    visited = bytearray(width * height)
    for start in range(width * height):
        if not weak[start] or visited[start]:
            continue
        queue = deque([start])
        component = []
        visited[start] = 1
        contains_strong = False
        while queue:
            current = queue.popleft()
            component.append(current)
            contains_strong = contains_strong or bool(strong[current])
            x = current % width
            y = current // width
            for neighbor_y in range(max(0, y - 1), min(height, y + 2)):
                row = neighbor_y * width
                for neighbor_x in range(max(0, x - 1), min(width, x + 2)):
                    neighbor = row + neighbor_x
                    if weak[neighbor] and not visited[neighbor]:
                        visited[neighbor] = 1
                        queue.append(neighbor)
        if contains_strong:
            for index in component:
                retained[index] = 1
    return retained


def _product_box(retained: bytearray, width: int, height: int) -> tuple[int, int, int, int]:
    left = width
    top = height
    right = 0
    bottom = 0
    found = False
    for index, value in enumerate(retained):
        if not value:
            continue
        found = True
        x = index % width
        y = index // width
        left = min(left, x)
        top = min(top, y)
        right = max(right, x + 1)
        bottom = max(bottom, y + 1)
    if not found:
        raise LayoutError("保留前景为空")
    if left == 0 or top == 0 or right == width or bottom == height:
        raise LayoutError("商品范围触及生成结果边界")
    return left, top, right, bottom


def _detect_product(image: Image.Image, background: tuple[int, int, int]) -> tuple[tuple[int, int, int, int], dict[str, Any]]:
    width, height = image.size
    pixel_count = width * height
    strong, weak, strong_count, weak_count = _foreground_masks(image, background)
    minimum_strong = math.ceil(pixel_count * 0.001)
    if strong_count < minimum_strong:
        raise LayoutError("强前景像素不足")
    weak_coverage = weak_count / pixel_count
    if weak_coverage >= 0.90:
        retained = bytearray(strong)
        retention_rule = "strong_only_when_weak_coverage_gte_0.90"
    else:
        retained = _retain_connected_weak(strong, weak, width, height)
        retention_rule = "8_connected_weak_components_containing_strong"
    retained_count = retained.count(1)
    if retained_count == 0:
        raise LayoutError("保留前景为空")
    confidence = strong_count / retained_count
    if confidence < 0.60:
        raise LayoutError("前景置信度低于 0.60")
    box = _product_box(retained, width, height)
    return box, {
        "strong_count": strong_count,
        "strong_minimum": minimum_strong,
        "weak_count": weak_count,
        "weak_coverage": weak_coverage,
        "retained_count": retained_count,
        "strong_to_retained_ratio": confidence,
        "connectivity": 8,
        "retention_rule": retention_rule,
    }


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return min(max(value, minimum), maximum)


def _resize_and_place(
    image: Image.Image,
    product_box: tuple[int, int, int, int],
    background: tuple[int, int, int],
) -> tuple[Image.Image, dict[str, Any]]:
    source_width, source_height = image.size
    left, top, right, bottom = product_box
    product_width = right - left
    product_height = bottom - top
    width_scale = TARGET_PRODUCT_WIDTH / product_width
    height_scale = PRODUCT_HEIGHT_LIMIT / product_height
    actual_scale = min(width_scale, height_scale)
    limited = (
        ["product_height_at_target_center"]
        if height_scale < width_scale
        else []
    )
    resized_width = round_signed(source_width * actual_scale)
    resized_height = round_signed(source_height * actual_scale)
    if resized_width < 1 or resized_height < 1:
        raise LayoutError("缩放后中间图任一边小于 1")
    if resized_width * resized_height > MAX_INTERMEDIATE_PIXELS:
        raise LayoutError("缩放后中间图总像素超过 20,000,000")
    scale_x = resized_width / source_width
    scale_y = resized_height / source_height
    scaled_box = (
        math.floor(left * scale_x),
        math.floor(top * scale_y),
        math.ceil(right * scale_x),
        math.ceil(bottom * scale_y),
    )
    scaled_left, scaled_top, scaled_right, scaled_bottom = scaled_box
    product_center_x = (scaled_left + scaled_right) / 2
    product_center_y = (scaled_top + scaled_bottom) / 2
    desired_x = round_signed(TARGET_CENTER[0] - product_center_x)
    desired_y = round_signed(TARGET_CENTER[1] - product_center_y)
    x_min, x_max = -scaled_left, CANVAS_SIZE[0] - scaled_right
    y_min, y_max = -scaled_top, CANVAS_SIZE[1] - scaled_bottom
    if x_min > x_max or y_min > y_max:
        raise LayoutError("商品范围无法完整容纳在最终画布")
    paste_x = _clamp(desired_x, x_min, x_max)
    paste_y = _clamp(desired_y, y_min, y_max)
    resized = image.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", CANVAS_SIZE, background)
    canvas.paste(resized, (paste_x, paste_y))
    actual_width = scaled_right - scaled_left
    actual_center = [
        product_center_x + paste_x,
        product_center_y + paste_y,
    ]
    if not limited:
        if abs(actual_width - TARGET_PRODUCT_WIDTH) > 2:
            raise LayoutError("未受限商品宽度偏离 922 ± 2")
        if any(
            abs(actual_center[index] - TARGET_CENTER[index]) > 1
            for index in (0, 1)
        ):
            raise LayoutError("未受限商品中心误差超过 1 像素")
    return canvas, {
        "target_width_scale": width_scale,
        "actual_scale": actual_scale,
        "scale_limited_by": limited,
        "resized_full_image_size": [resized_width, resized_height],
        "scale_x": scale_x,
        "scale_y": scale_y,
        "scaled_product_box_half_open": list(scaled_box),
        "paste_xy": [paste_x, paste_y],
        "actual_product_width": actual_width,
        "actual_product_width_ratio": actual_width / CANVAS_SIZE[0],
        "actual_product_center": actual_center,
    }


def _paths_and_edit_manifest(
    input_path: Path, output_path: Path, manifest_path: Path
) -> tuple[Path, str, Path, dict[str, Any]]:
    suffix = "_layout_manifest.json"
    if not manifest_path.name.endswith(suffix):
        raise LayoutError("Layout Manifest 文件名不符合合同")
    product_id = manifest_path.name[: -len(suffix)]
    root = manifest_path.parent.parent
    try:
        resolved_root = root.resolve(strict=True)
        resolved_input = input_path.resolve(strict=True)
        resolved_edit = (root / "edit").resolve(strict=True)
        resolved_layout = (root / "layout").resolve(strict=True)
        resolved_manifests = (root / "manifests").resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise LayoutError("排版运行目录不完整") from exc
    if resolved_input.parent != resolved_edit:
        raise LayoutError("生成结果必须是当前运行 edit 目录的直接子文件")
    if output_path.parent.resolve() != resolved_layout:
        raise LayoutError("排版输出必须位于当前运行 layout 目录")
    if manifest_path.parent.resolve() != resolved_manifests:
        raise LayoutError("Layout Manifest 必须位于当前运行 manifests 目录")
    if output_path.name != f"{product_id}_3x4_60pct.png":
        raise LayoutError("排版输出文件名与 product_id 不一致")
    edit_manifest_path = manifest_path.parent / f"{product_id}_edit_result.json"
    try:
        edit_manifest = json.loads(edit_manifest_path.read_text(encoding="utf-8"))
        result = edit_manifest["result"]
        bound_path = (resolved_root / result["path"]).resolve(strict=True)
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LayoutError("Edit Result Manifest 无效") from exc
    request_identity = edit_manifest.get("request_identity")
    required_identity_keys = {
        "provider",
        "operation",
        "endpoint",
        "model",
        "size",
        "n",
        "image_size",
        "mask_size",
        "image_sha256",
        "mask_sha256",
        "prompt_sha256",
    }
    request_identity_sha256 = edit_manifest.get("request_identity_sha256")
    if not isinstance(request_identity, dict):
        raise LayoutError("Edit Result Manifest 请求身份无效")
    encoded_identity = json.dumps(
        request_identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    actual_identity_sha256 = hashlib.sha256(encoded_identity).hexdigest().upper()
    try:
        with Image.open(input_path) as opened:
            actual_format = opened.format
            actual_size = list(opened.size)
            opened.verify()
        with Image.open(input_path) as opened:
            opened.load()
    except (OSError, ValueError) as exc:
        raise LayoutError("Edit Result Manifest 绑定的图片无法完整解码") from exc
    suffix_format = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}.get(
        input_path.suffix.lower()
    )
    call_slot = edit_manifest.get("call_slot")
    if (
        edit_manifest.get("schema_version") != "jewelry-edit-result-1.0"
        or edit_manifest.get("run_id") != root.name
        or edit_manifest.get("product_id") != product_id
        or not isinstance(call_slot, int)
        or isinstance(call_slot, bool)
        or call_slot not in {1, 2, 3}
        or not required_identity_keys.issubset(request_identity)
        or request_identity_sha256 != actual_identity_sha256
        or bound_path != resolved_input
        or result.get("format") != actual_format
        or actual_format != suffix_format
        or result.get("size") != actual_size
        or result.get("bytes") != input_path.stat().st_size
        or result.get("sha256") != _sha256(input_path)
    ):
        raise LayoutError("Edit Result Manifest 未绑定当前生成结果")
    return resolved_root, product_id, edit_manifest_path, edit_manifest


def layout_generated_result(input_path, output_path, manifest_path):
    input_path = Path(input_path)
    output_path = Path(output_path)
    manifest_path = Path(manifest_path)
    if output_path.exists() or manifest_path.exists():
        raise FileExistsError("排版输出或 Manifest 已存在")
    root, product_id, edit_manifest_path, _edit_manifest = _paths_and_edit_manifest(
        input_path, output_path, manifest_path
    )
    input_sha256 = _sha256(input_path)
    image, source_format = _load_generated_rgb(input_path)
    background, corner_edge, corner_ranges = _corner_background(image)
    product_box, foreground = _detect_product(image, background)
    canvas, placement = _resize_and_place(image, product_box, background)

    output_buffer = io.BytesIO()
    canvas.save(output_buffer, "PNG", optimize=False, compress_level=9)
    manifest = {
        "layout_algorithm_version": ALGORITHM_VERSION,
        "run_id": root.name,
        "product_id": product_id,
        "generated_result": {
            "path": input_path.resolve().relative_to(root).as_posix(),
            "sha256": input_sha256,
            "format": source_format,
            "size": list(image.size),
        },
        "edit_result_manifest": {
            "path": edit_manifest_path.resolve().relative_to(root).as_posix(),
            "sha256": _sha256(edit_manifest_path),
        },
        "corner_sampling": {
            "edge_length": corner_edge,
            "ranges_half_open": corner_ranges,
            "background_rgb_median": list(background),
            "even_median_rounding": "ceil",
        },
        "foreground_thresholds": {
            "distance_metric": "rgb_euclidean",
            "strong_distance_gte": STRONG_DISTANCE,
            "weak_distance_gte": WEAK_DISTANCE,
        },
        "foreground": foreground,
        "product_box_half_open": list(product_box),
        "target_canvas_size": list(CANVAS_SIZE),
        "target_product_width": TARGET_PRODUCT_WIDTH,
        "target_center": list(TARGET_CENTER),
        "product_height_limit": PRODUCT_HEIGHT_LIMIT,
        "interpolation": "LANCZOS",
        "rounding_rule": "round_signed: floor(x+0.5) if x>=0 else ceil(x-0.5)",
        **placement,
        "whole_generated_result_resized": True,
        "pre_generation_reference_used": False,
        "pre_generation_mask_used": False,
        "mask_cutout_used": False,
        "sharpening": None,
    }
    _atomic_create(output_path, output_buffer.getvalue())
    _atomic_create(manifest_path, _json_bytes(manifest))
    return manifest


__all__ = ["LayoutError", "layout_generated_result", "round_signed"]
