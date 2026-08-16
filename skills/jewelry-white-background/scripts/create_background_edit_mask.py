#!/usr/bin/env python3
"""从视觉语义几何创建可审计的珠宝背景 Edit Mask。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat


_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)

from prepare_mask_detection_images import (  # noqa: E402
    percentile as _percentile,
    prepare_mask_detection_images,
)
from create_geometry_crop import rasterize_cropped_geometry  # noqa: E402
from workflow_state import atomic_create_bytes, atomic_create_json  # noqa: E402


MAX_IMAGE_PIXELS = 20_000_000
ASSESSMENT_EXIT_CODES = {"ok": 0, "review": 3, "fail": 4}
_SCHEMA_VERSION = "vision-geometry-mask-2.0"
_COORDINATE_RANGE = (0, 10000)
_PRIMITIVE_TYPES = {"polygon", "polyline", "ellipse", "ellipse_set"}
_SUPPORTED_PRODUCERS = {"codex-cloud-vision", "codex-cloud-vision-poc-migrated"}
_MIN_PROTECTED_RATIO = 0.005
_MAX_PROTECTED_RATIO = 0.70


@dataclass(frozen=True)
class VisionGeometryProfile:
    schema_version: str
    product_id: str
    source_sha256: str
    source_size: tuple[int, int]
    producer: str
    primitives: tuple[dict[str, Any], ...]
    uncertain_regions: tuple[dict[str, Any], ...]
    mask_review: dict[str, Any] | None

    @property
    def primitive_count(self) -> int:
        return len(self.primitives)

    @property
    def touching_primitive_ids(self) -> tuple[str, ...]:
        return tuple(
            primitive["id"]
            for primitive in self.primitives
            if primitive.get("touches_border") is True
        )

    @property
    def uncertain_region_ids(self) -> tuple[str, ...]:
        return tuple(region["id"] for region in self.uncertain_regions)


@dataclass(frozen=True)
class BoundaryVariantReport:
    name: str
    black_point: int
    white_point: int
    clipped_ratio: float
    supported_edge_pixels: int


@dataclass(frozen=True)
class BoundaryRefinementReport:
    status: str
    fallback_reason: str | None
    changed_pixels: int
    band_pixels: int
    consensus_band_pixels: int
    invariant_core_preserved: bool
    invariant_outside_band: bool
    invariant_border_frozen: bool
    variants: tuple[BoundaryVariantReport, ...]
    algorithm_version: str = "boundary-refinement-1.0"


@dataclass(frozen=True)
class MaskAssessment:
    status: str
    reasons: list[str]
    protected_ratio: float
    background_editable_ratio: float
    border_contact_ratio: float
    undeclared_border_contact_ratio: float
    automatic_wawapi_edit_allowed: bool
    mask_review_status: str
    unresolved_uncertain_region_ids: tuple[str, ...]
    declared_border_contact_missing_ids: tuple[str, ...]
    technical_blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftMaskAssessment:
    status: str
    reasons: list[str]
    protected_ratio: float
    background_editable_ratio: float
    border_contact_ratio: float
    undeclared_border_contact_ratio: float
    unresolved_uncertain_region_ids: tuple[str, ...]
    declared_border_contact_missing_ids: tuple[str, ...]
    technical_blockers: tuple[str, ...]


@dataclass(frozen=True)
class PublishedMaskTechnicalGate:
    passed: bool
    blockers: tuple[str, ...]
    assessment: DraftMaskAssessment


@dataclass(frozen=True)
class MaskOutputPaths:
    run_root: Path
    mask_path: Path
    overlay_path: Path
    report_path: Path


def _validate_image_size(size: tuple[int, int]) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸必须为正整数")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("图片不得超过 20MP（20,000,000 像素）")


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是 JSON 对象")
    return value


def _validate_coordinate(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10000:
        raise ValueError("所有坐标和宽度必须是 0..10000 范围内的整数")
    return value


def _validate_points(value: Any, minimum: int) -> None:
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError("图元点集不能为空且点数不足")
    for point in value:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("图元坐标必须是二元数组")
        _validate_coordinate(point[0])
        _validate_coordinate(point[1])


def _polygon_signed_area(points: list[list[int]]) -> int:
    return sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, points[1:] + points[:1])
    )


def _validate_ellipse_params(value: Any) -> None:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("ellipse params 必须包含中心和半径")
    for item in value:
        _validate_coordinate(item)
    if value[2] <= 0 or value[3] <= 0:
        raise ValueError("ellipse 半径必须为正数")


def _validate_primitive(primitive: Any, ids: set[str]) -> dict[str, Any]:
    primitive = _require_mapping(primitive, "图元")
    primitive_id = primitive.get("id")
    if not isinstance(primitive_id, str) or not primitive_id.strip() or primitive_id in ids:
        raise ValueError("所有图元 ID 必须非空且唯一")
    ids.add(primitive_id)
    kind = primitive.get("type")
    if kind not in _PRIMITIVE_TYPES:
        raise ValueError(f"不支持的图元类型：{kind}")
    if not isinstance(primitive.get("semantic"), str) or not primitive["semantic"].strip():
        raise ValueError("图元 semantic 必须非空")
    if kind == "polygon":
        points = primitive.get("points")
        _validate_points(points, 3)
        if len({tuple(point) for point in points}) < 3 or _polygon_signed_area(points) == 0:
            raise ValueError("polygon 不得是退化或零面积图元")
    elif kind == "polyline":
        points = primitive.get("points")
        _validate_points(points, 2)
        if len({tuple(point) for point in points}) < 2:
            raise ValueError("polyline 必须具有非零长度")
        if _validate_coordinate(primitive.get("width")) <= 0:
            raise ValueError("polyline width 必须为正数")
    elif kind == "ellipse":
        _validate_ellipse_params(primitive.get("params"))
    else:
        items = primitive.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("ellipse_set items 不能为空")
        for item in items:
            item = _require_mapping(item, "ellipse_set item")
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id.strip() or item_id in ids:
                raise ValueError("所有图元 ID 必须非空且唯一")
            ids.add(item_id)
            _validate_ellipse_params(item.get("params"))
    if "touches_border" in primitive and not isinstance(primitive["touches_border"], bool):
        raise ValueError("touches_border 必须是布尔值")
    return primitive


def _validate_uncertain_regions(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("uncertain_regions 必须是数组")
    ids: set[str] = set()
    regions: list[dict[str, Any]] = []
    for region in value:
        region = _require_mapping(region, "uncertain_regions 项")
        region_id = region.get("id")
        if not isinstance(region_id, str) or not region_id.strip() or region_id in ids:
            raise ValueError("uncertain_regions 的 id 必须非空且唯一")
        bbox = region.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("uncertain_regions 的 bbox 必须包含四个坐标")
        left, top, right, bottom = (_validate_coordinate(item) for item in bbox)
        if left >= right or top >= bottom:
            raise ValueError("uncertain_regions 的 bbox 必须具有正面积")
        reason = region.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("uncertain_regions 的 reason 必须非空")
        ids.add(region_id)
        regions.append(region)
    return tuple(regions)


def _validate_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9A-F]{64}", value) is None:
        raise ValueError(f"{label} 必须是 64 位大写十六进制 SHA-256")
    return value


def _validate_mask_review(value: Any, source_sha256: str) -> dict[str, Any] | None:
    if value is None:
        return None
    review = _require_mapping(value, "Mask 审核")
    if review.get("status") != "approved":
        raise ValueError("Mask 审核状态必须为 approved")
    reviewer = review.get("reviewer")
    checked = review.get("checked_items")
    if (
        not isinstance(reviewer, str)
        or not reviewer.strip()
        or review.get("source_sha256") != source_sha256
        or not isinstance(checked, list)
        or not checked
        or any(not isinstance(item, str) or not item.strip() for item in checked)
    ):
        raise ValueError("Mask 审核必须绑定源图、审核人和非空检查项")
    _validate_digest(review.get("geometry_sha256"), "Mask 审核 geometry_sha256")
    _validate_digest(review.get("mask_sha256"), "Mask 审核 mask_sha256")
    resolved = review.get("resolved_uncertain_regions", [])
    if not isinstance(resolved, list) or any(
        not isinstance(item, str) or not item.strip() for item in resolved
    ):
        raise ValueError("Mask 审核的 resolved_uncertain_regions 必须是字符串数组")
    if len(set(resolved)) != len(resolved):
        raise ValueError("Mask 审核的 resolved_uncertain_regions 不能重复")
    return review


def load_vision_geometry(path: str | Path, product_id: str) -> VisionGeometryProfile:
    payload = _require_mapping(json.loads(Path(path).read_text(encoding="utf-8")), "几何文件")
    if "products" in payload:
        products = _require_mapping(payload["products"], "products")
        if product_id not in products:
            raise ValueError(f"几何文件中没有商品编号 {product_id}")
        payload = _require_mapping(products[product_id], "商品几何")
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(f"不支持的 schema，必须为 {_SCHEMA_VERSION}")
    if payload.get("product_id") != product_id:
        raise ValueError("几何商品编号与请求商品编号不一致")
    digest = _validate_digest(payload.get("source_sha256"), "source_sha256")
    size = payload.get("source_size")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(not isinstance(item, int) or isinstance(item, bool) for item in size)
    ):
        raise ValueError("source_size 尺寸必须包含两个整数")
    source_size = (size[0], size[1])
    _validate_image_size(source_size)
    if payload.get("coordinate_range") != [0, 10000]:
        raise ValueError("coordinate_range 必须为 0..10000")
    producer = payload.get("producer")
    if producer not in _SUPPORTED_PRODUCERS:
        raise ValueError("producer 必须是受支持的云端视觉来源")
    primitives = payload.get("primitives")
    if not isinstance(primitives, list) or not primitives:
        raise ValueError("primitives 不能为空")
    ids: set[str] = set()
    checked_primitives = tuple(_validate_primitive(item, ids) for item in primitives)
    uncertain = _validate_uncertain_regions(payload.get("uncertain_regions", []))
    review = _validate_mask_review(payload.get("mask_review"), digest)
    if review is not None:
        unknown_resolved = set(review.get("resolved_uncertain_regions", [])) - {
            item["id"] for item in uncertain
        }
        if unknown_resolved:
            raise ValueError("Mask 审核包含不存在的 uncertain_regions 编号")
    return VisionGeometryProfile(
        schema_version=_SCHEMA_VERSION,
        product_id=product_id,
        source_sha256=digest,
        source_size=source_size,
        producer=producer,
        primitives=checked_primitives,
        uncertain_regions=uncertain,
        mask_review=review,
    )


def _scale_point(point: list[int], size: tuple[int, int]) -> tuple[int, int]:
    width, height = size
    return (
        round(point[0] * (width - 1) / 10000),
        round(point[1] * (height - 1) / 10000),
    )


def _ellipse_box(params: list[int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    center = _scale_point(params[:2], size)
    radius_x = max(1, round(params[2] * (size[0] - 1) / 10000))
    radius_y = max(1, round(params[3] * (size[1] - 1) / 10000))
    return center[0] - radius_x, center[1] - radius_y, center[0] + radius_x, center[1] + radius_y


def _draw_primitive(
    draw: ImageDraw.ImageDraw, primitive: dict[str, Any], size: tuple[int, int]
) -> None:
    kind = primitive["type"]
    if kind == "polygon":
        draw.polygon([_scale_point(point, size) for point in primitive["points"]], fill=255)
    elif kind == "polyline":
        line_width = max(1, round(primitive["width"] * min(size) / 10000))
        draw.line(
            [_scale_point(point, size) for point in primitive["points"]],
            fill=255,
            width=line_width,
            joint="curve",
        )
    elif kind == "ellipse":
        draw.ellipse(_ellipse_box(primitive["params"], size), fill=255)
    else:
        for item in primitive["items"]:
            draw.ellipse(_ellipse_box(item["params"], size), fill=255)


def rasterize_vision_geometry(
    profile: VisionGeometryProfile, size: tuple[int, int] | None = None
) -> Image.Image:
    size = profile.source_size if size is None else size
    _validate_image_size(size)
    if size != profile.source_size:
        raise ValueError("栅格化尺寸与几何源图尺寸不一致")
    alpha = Image.new("L", size, 0)
    draw = ImageDraw.Draw(alpha)
    for primitive in profile.primitives:
        _draw_primitive(draw, primitive, size)
    return alpha


def _single_channel_edge(channel: Image.Image) -> Image.Image:
    softened = channel.filter(ImageFilter.GaussianBlur(0.55))
    width, height = softened.size
    left = Image.new("L", softened.size)
    left.paste(softened.crop((0, 0, width - 1, height)), (1, 0))
    left.paste(softened.crop((0, 0, 1, height)), (0, 0))
    up = Image.new("L", softened.size)
    up.paste(softened.crop((0, 0, width, height - 1)), (0, 1))
    up.paste(softened.crop((0, 0, width, 1)), (0, 0))
    return ImageChops.lighter(
        ImageChops.difference(softened, left),
        ImageChops.difference(softened, up),
    )


def _edge_map(image: Image.Image) -> Image.Image:
    """合并亮度与 RGB 色度边缘，避免同亮度彩色细绳被忽略。"""
    if image.mode != "RGB":
        return _single_channel_edge(image.convert("L"))
    red, green, blue = image.split()
    luminance = image.convert("L")
    return ImageChops.lighter(
        _single_channel_edge(luminance),
        ImageChops.lighter(
            _single_channel_edge(red),
            ImageChops.lighter(_single_channel_edge(green), _single_channel_edge(blue)),
        ),
    )


def _count_nonzero(image: Image.Image) -> int:
    return sum(image.histogram()[1:])


def _binary_kernel(radius: int) -> int:
    return max(3, radius * 2 + 1)


def _border_mask(size: tuple[int, int]) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline=255)
    return mask


def refine_candidate_boundary_legacy_read_only(
    image: Image.Image, candidate_alpha: Image.Image
) -> tuple[Image.Image, BoundaryRefinementReport]:
    _validate_image_size(image.size)
    if candidate_alpha.size != image.size:
        raise ValueError("候选 Alpha 尺寸与原图不一致")
    alpha = candidate_alpha.convert("L")
    non_binary = sum(alpha.histogram()[1:255])
    if non_binary:
        raise ValueError("候选 Alpha 必须是二值图")
    detection_variants = prepare_mask_detection_images(image)
    original_gray = detection_variants[0].image
    variants_data = tuple(
        (
            variant.name,
            variant.image,
            variant.black_point,
            variant.white_point,
            variant.clipped_ratio,
        )
        for variant in detection_variants
    )
    # 原图保留 RGB 色度边缘；增强副本只改变亮度，仍在本地检测支路内。
    edges = [_edge_map(image.convert("RGB")), *(_edge_map(item[1]) for item in variants_data[1:])]
    short_edge = min(image.size)
    inner_radius = max(2, min(4, round(short_edge * 0.002)))
    outer_radius = max(3, min(6, round(short_edge * 0.004)))
    core = alpha.filter(ImageFilter.MinFilter(_binary_kernel(inner_radius)))
    expanded = alpha.filter(ImageFilter.MaxFilter(_binary_kernel(outer_radius)))
    band = ImageChops.subtract(expanded, core)
    recovered_core = core.filter(ImageFilter.MaxFilter(_binary_kernel(inner_radius)))
    thin_structure = ImageChops.subtract(alpha, recovered_core)
    band_pixels = _count_nonzero(band)

    # 原图动态范围不足时，增强版本没有独立事实可提供，必须原样回退。
    if ImageStat.Stat(original_gray).extrema[0][1] - ImageStat.Stat(original_gray).extrema[0][0] < 8 or band_pixels == 0:
        supported = [0, 0, 0]
        status = "fallback"
        fallback = "insufficient_cross_variant_edge_evidence"
        refined = alpha.copy()
    else:
        edge_thresholds = [max(3, _percentile(edge, 0.86)) for edge in edges]
        edge_seeds = [edge.point(lambda value, t=t: 255 if value >= t else 0) for edge, t in zip(edges, edge_thresholds)]
        supported = [_count_nonzero(ImageChops.multiply(seed, band)) for seed in edge_seeds]
        consensus = Image.new("L", image.size, 0)
        consensus_pixels = consensus.load()
        seed_pixels = [seed.load() for seed in edge_seeds]
        for y in range(image.height):
            for x in range(image.width):
                if sum(bool(seed[x, y]) for seed in seed_pixels) >= 2:
                    consensus_pixels[x, y] = 255
        proposed = ImageChops.lighter(core, thin_structure)
        proposed = ImageChops.lighter(proposed, ImageChops.multiply(expanded, consensus))
        border = _border_mask(image.size)
        proposed = Image.composite(alpha, proposed, border)
        changed = ImageChops.difference(proposed, alpha)
        consensus_band_pixels = _count_nonzero(ImageChops.multiply(consensus, band))
        if consensus_band_pixels == 0 or _count_nonzero(changed) > max(1, round(band_pixels * 0.95)):
            refined = alpha.copy()
            status = "fallback"
            fallback = "insufficient_cross_variant_edge_evidence"
        else:
            refined = proposed.point(lambda value: 255 if value else 0)
            status = "applied"
            fallback = None

    changed_pixels = _count_nonzero(ImageChops.difference(refined, alpha))
    consensus_band_pixels = _count_nonzero(ImageChops.multiply(consensus, band)) if "consensus" in locals() else 0
    outside_band = ImageOps.invert(band)
    variants = tuple(
        BoundaryVariantReport(name, low, high, clipped, count)
        for (name, _gray, low, high, clipped), count in zip(variants_data, supported)
    )
    return refined, BoundaryRefinementReport(
        status=status,
        fallback_reason=fallback,
        changed_pixels=changed_pixels,
        band_pixels=band_pixels,
        consensus_band_pixels=consensus_band_pixels,
        invariant_core_preserved=ImageChops.difference(ImageChops.multiply(refined, core), core).getbbox() is None,
        invariant_outside_band=ImageChops.multiply(ImageChops.difference(refined, alpha), outside_band).getbbox() is None,
        invariant_border_frozen=ImageChops.multiply(ImageChops.difference(refined, alpha), _border_mask(image.size)).getbbox() is None,
        variants=variants,
    )


def refine_candidate_boundary(
    original_rgb: Image.Image,
    global_robust: Image.Image,
    local_limited: Image.Image,
    candidate_alpha: Image.Image,
) -> tuple[Image.Image, BoundaryRefinementReport]:
    _validate_image_size(original_rgb.size)
    if original_rgb.mode != "RGB":
        raise ValueError("裁剪原图必须为 RGB")
    if global_robust.mode != "L" or local_limited.mode != "L":
        raise ValueError("两张裁剪检测图必须为 L 模式")
    if not (
        global_robust.size
        == local_limited.size
        == candidate_alpha.size
        == original_rgb.size
    ):
        raise ValueError("三路证据与候选 Alpha 尺寸必须一致")
    alpha = candidate_alpha.convert("L")
    if sum(alpha.histogram()[1:255]):
        raise ValueError("候选 Alpha 必须是二值图")

    edges = (
        _edge_map(original_rgb),
        _edge_map(global_robust),
        _edge_map(local_limited),
    )
    thresholds = tuple(max(3, _percentile(edge, 0.86)) for edge in edges)
    seeds = tuple(
        edge.point(lambda value, threshold=threshold: 255 if value >= threshold else 0)
        for edge, threshold in zip(edges, thresholds)
    )
    short_edge = min(original_rgb.size)
    inner_radius = max(2, min(4, round(short_edge * 0.002)))
    outer_radius = max(3, min(6, round(short_edge * 0.004)))
    core = alpha.filter(ImageFilter.MinFilter(_binary_kernel(inner_radius)))
    expanded = alpha.filter(ImageFilter.MaxFilter(_binary_kernel(outer_radius)))
    band = ImageChops.subtract(expanded, core)
    recovered_core = core.filter(ImageFilter.MaxFilter(_binary_kernel(inner_radius)))
    thin_structure = ImageChops.subtract(alpha, recovered_core)
    band_pixels = _count_nonzero(band)
    supported = tuple(
        _count_nonzero(ImageChops.multiply(seed, band)) for seed in seeds
    )
    consensus = Image.new("L", original_rgb.size, 0)
    consensus_pixels = consensus.load()
    seed_pixels = tuple(seed.load() for seed in seeds)
    for y in range(original_rgb.height):
        for x in range(original_rgb.width):
            if sum(bool(seed[x, y]) for seed in seed_pixels) >= 2:
                consensus_pixels[x, y] = 255

    consensus_band_pixels = _count_nonzero(ImageChops.multiply(consensus, band))
    original_extrema = ImageStat.Stat(original_rgb.convert("L")).extrema[0]
    original_dynamic_range = original_extrema[1] - original_extrema[0]
    if (
        original_dynamic_range < 8
        or band_pixels == 0
        or consensus_band_pixels == 0
    ):
        refined = alpha.copy()
        status = "fallback"
        fallback = "insufficient_cross_variant_edge_evidence"
    else:
        proposed = ImageChops.lighter(core, thin_structure)
        proposed = ImageChops.lighter(
            proposed, ImageChops.multiply(expanded, consensus)
        )
        proposed = Image.composite(alpha, proposed, _border_mask(original_rgb.size))
        changed = ImageChops.difference(proposed, alpha)
        if _count_nonzero(changed) > max(1, round(band_pixels * 0.95)):
            refined = alpha.copy()
            status = "fallback"
            fallback = "insufficient_cross_variant_edge_evidence"
        else:
            refined = proposed.point(lambda value: 255 if value else 0)
            status = "applied"
            fallback = None

    outside_band = ImageOps.invert(band)
    changed_pixels = _count_nonzero(ImageChops.difference(refined, alpha))
    names = ("original_rgb", "global_robust", "local_limited")
    variants = tuple(
        BoundaryVariantReport(
            name=name,
            black_point=threshold,
            white_point=255,
            clipped_ratio=round(_count_nonzero(seed) / max(1, seed.width * seed.height), 6),
            supported_edge_pixels=count,
        )
        for name, threshold, seed, count in zip(names, thresholds, seeds, supported)
    )
    return refined, BoundaryRefinementReport(
        status=status,
        fallback_reason=fallback,
        changed_pixels=changed_pixels,
        band_pixels=band_pixels,
        consensus_band_pixels=consensus_band_pixels,
        invariant_core_preserved=(
            ImageChops.difference(ImageChops.multiply(refined, core), core).getbbox()
            is None
        ),
        invariant_outside_band=(
            ImageChops.multiply(
                ImageChops.difference(refined, alpha), outside_band
            ).getbbox()
            is None
        ),
        invariant_border_frozen=(
            ImageChops.multiply(
                ImageChops.difference(refined, alpha),
                _border_mask(original_rgb.size),
            ).getbbox()
            is None
        ),
        variants=variants,
        algorithm_version="boundary-refinement-2.1",
    )


def _load_rgb(path: Path) -> Image.Image:
    with Image.open(path) as source:
        _validate_image_size(source.size)
        source = ImageOps.exif_transpose(source)
        _validate_image_size(source.size)
        if "A" in source.getbands() or "transparency" in source.info:
            rgba = source.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            return Image.alpha_composite(white, rgba).convert("RGB")
        return source.convert("RGB")


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def _validate_asset_paths(input_path: Path, output_paths: tuple[Path, ...]) -> None:
    keys = [_path_key(input_path), *(_path_key(path) for path in output_paths)]
    if len(set(keys)) != len(keys):
        raise ValueError("输入路径与所有输出路径必须互不相同")


def _source_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _canonical_geometry_payload(profile: VisionGeometryProfile) -> dict[str, Any]:
    return {
        "schema_version": profile.schema_version,
        "product_id": profile.product_id,
        "source_sha256": profile.source_sha256,
        "source_size": list(profile.source_size),
        "coordinate_range": list(_COORDINATE_RANGE),
        "producer": profile.producer,
        "primitives": list(profile.primitives),
        "uncertain_regions": list(profile.uncertain_regions),
    }


def _geometry_digest(profile: VisionGeometryProfile) -> str:
    encoded = json.dumps(
        _canonical_geometry_payload(profile),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _bytes_digest(encoded)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _identity_checked_profile(input_path: Path, geometry_path: Path, product_id: str) -> tuple[Image.Image, VisionGeometryProfile, str]:
    image = _load_rgb(input_path)
    digest = _source_digest(input_path)
    profile = load_vision_geometry(geometry_path, product_id)
    if profile.source_sha256 != digest:
        raise ValueError("几何 source SHA-256 与输入源图不一致")
    if profile.source_size != image.size:
        raise ValueError("几何源图尺寸与输入源图尺寸不一致")
    return image, profile, digest


def _mask_metrics(alpha: Image.Image) -> tuple[float, float]:
    histogram = alpha.histogram()
    protected_ratio = histogram[255] / max(1, alpha.width * alpha.height)
    border = _border_mask(alpha.size)
    border_pixels = _count_nonzero(border)
    contact = _count_nonzero(ImageChops.multiply(alpha, border)) / max(1, border_pixels)
    return round(protected_ratio, 6), round(contact, 6)


def _mask_ratio_values(alpha: Image.Image) -> tuple[float, float]:
    protected_pixels = alpha.histogram()[255]
    exact = protected_pixels / max(1, alpha.width * alpha.height)
    return exact, round(exact, 6)


def _declared_border_contact_missing_ids(
    alpha: Image.Image, profile: VisionGeometryProfile
) -> tuple[str, ...]:
    border = _border_mask(alpha.size)
    missing: list[str] = []
    for primitive in profile.primitives:
        if primitive.get("touches_border") is not True:
            continue
        primitive_alpha = Image.new("L", alpha.size, 0)
        _draw_primitive(ImageDraw.Draw(primitive_alpha), primitive, alpha.size)
        if ImageChops.multiply(primitive_alpha, border).getbbox() is None:
            missing.append(primitive["id"])
    return tuple(missing)


def _undeclared_border_contact_ratio(
    alpha: Image.Image, profile: VisionGeometryProfile
) -> float:
    border = _border_mask(alpha.size)
    final_contact = ImageChops.multiply(alpha, border)
    declared_contact = Image.new("L", alpha.size, 0)
    for primitive in profile.primitives:
        if primitive.get("touches_border") is not True:
            continue
        primitive_alpha = Image.new("L", alpha.size, 0)
        _draw_primitive(ImageDraw.Draw(primitive_alpha), primitive, alpha.size)
        declared_contact = ImageChops.lighter(
            declared_contact,
            ImageChops.multiply(primitive_alpha, border),
        )
    undeclared = ImageChops.subtract(final_contact, declared_contact)
    return round(
        _count_nonzero(undeclared) / max(1, _count_nonzero(border)),
        6,
    )


def _unresolved_uncertain_region_ids(
    profile: VisionGeometryProfile, review_identity_matches: bool
) -> tuple[str, ...]:
    if not profile.uncertain_regions:
        return ()
    review = profile.mask_review
    if review is None or not review_identity_matches:
        return profile.uncertain_region_ids
    resolved = set(review.get("resolved_uncertain_regions", []))
    return tuple(region_id for region_id in profile.uncertain_region_ids if region_id not in resolved)


def _assessment(
    alpha: Image.Image,
    profile: VisionGeometryProfile,
    refinement: BoundaryRefinementReport,
    geometry_sha256: str,
    mask_sha256: str,
) -> MaskAssessment:
    protected_ratio, border_contact_ratio = _mask_metrics(alpha)
    background_editable_ratio = round(1 - protected_ratio, 6)
    review = profile.mask_review
    geometry_matches = review is not None and review["geometry_sha256"] == geometry_sha256
    mask_matches = review is not None and review["mask_sha256"] == mask_sha256
    approved = review is not None and geometry_matches and mask_matches
    unresolved_uncertain_region_ids = _unresolved_uncertain_region_ids(profile, approved)
    declared_border_contact_missing_ids = _declared_border_contact_missing_ids(alpha, profile)
    undeclared_border_contact_ratio = _undeclared_border_contact_ratio(alpha, profile)
    reasons: list[str] = []
    if refinement.status == "fallback":
        reasons.append("boundary_refinement_fallback")
    if not (
        refinement.invariant_core_preserved
        and refinement.invariant_outside_band
        and refinement.invariant_border_frozen
    ):
        reasons.append("boundary_refinement_invariant_failed")
    if undeclared_border_contact_ratio > 0:
        reasons.append("undeclared_border_contact")
    if declared_border_contact_missing_ids:
        reasons.append("declared_border_contact_missing")
    if not _MIN_PROTECTED_RATIO <= protected_ratio <= _MAX_PROTECTED_RATIO:
        reasons.append("protected_ratio_out_of_range")
    if unresolved_uncertain_region_ids:
        reasons.append("unresolved_uncertain_regions")
    if review is None:
        reasons.append("mask_review_required")
    else:
        if not geometry_matches:
            reasons.append("mask_review_geometry_mismatch")
        if not mask_matches:
            reasons.append("mask_review_mask_mismatch")
    allowed = approved and not reasons
    return MaskAssessment(
        status="ok" if allowed else "review",
        reasons=reasons,
        protected_ratio=protected_ratio,
        background_editable_ratio=background_editable_ratio,
        border_contact_ratio=border_contact_ratio,
        undeclared_border_contact_ratio=undeclared_border_contact_ratio,
        automatic_wawapi_edit_allowed=allowed,
        mask_review_status=(
            "approved" if approved else "required" if review is None else "stale"
        ),
        unresolved_uncertain_region_ids=unresolved_uncertain_region_ids,
        declared_border_contact_missing_ids=declared_border_contact_missing_ids,
    )


def create_background_edit_assets_legacy_read_only(
    input_path: str | Path,
    image_output_path: str | Path,
    mask_output_path: str | Path,
    overlay_path: str | Path,
    report_path: str | Path,
    geometry_profile_path: str | Path,
    product_id: str,
) -> MaskAssessment:
    input_path = Path(input_path)
    outputs = tuple(Path(path) for path in (image_output_path, mask_output_path, overlay_path, report_path))
    _validate_asset_paths(input_path, outputs)
    image, profile, digest = _identity_checked_profile(input_path, Path(geometry_profile_path), product_id)
    candidate = rasterize_vision_geometry(profile, image.size)
    alpha, refinement = refine_candidate_boundary_legacy_read_only(image, candidate)
    mask = Image.new("RGBA", image.size, (255, 255, 255, 0))
    mask.putalpha(alpha)
    image_bytes = _png_bytes(image)
    mask_bytes = _png_bytes(mask)
    geometry_sha256 = _geometry_digest(profile)
    prepared_sha256 = _bytes_digest(image_bytes)
    mask_sha256 = _bytes_digest(mask_bytes)
    assessment = _assessment(
        alpha,
        profile,
        refinement,
        geometry_sha256,
        mask_sha256,
    )
    editable = alpha.point(lambda value: 255 if value == 0 else 0)
    red_tint = Image.blend(image, Image.new("RGB", image.size, (230, 70, 55)), 0.65)
    overlay = Image.composite(red_tint, image, editable)
    report = {
        **asdict(assessment),
        "schema_version": profile.schema_version,
        "product_id": profile.product_id,
        "source_sha256": digest,
        "source_size": list(image.size),
        "producer": profile.producer,
        "geometry_sha256": geometry_sha256,
        "prepared_sha256": prepared_sha256,
        "mask_sha256": mask_sha256,
        "primitive_count": profile.primitive_count,
        "touching_primitive_ids": list(profile.touching_primitive_ids),
        "uncertain_regions": list(profile.uncertain_regions),
        "mask_review": profile.mask_review,
        "boundary_refinement": asdict(refinement),
    }
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    outputs[0].write_bytes(image_bytes)
    outputs[1].write_bytes(mask_bytes)
    overlay.save(outputs[2], "PNG")
    outputs[3].write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return assessment


def _relative_contract_path(run_root: Path, path: Path, *, must_exist: bool) -> str:
    root = run_root.resolve()
    resolved = path.resolve(strict=must_exist)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("Mask 输入输出必须位于当前运行目录") from exc


def _load_contract_image(path: Path, mode: str, size: tuple[int, int] | None = None) -> Image.Image:
    with Image.open(path) as opened:
        opened.load()
        if opened.format != "PNG" or opened.mode != mode:
            raise ValueError(f"{path.name} 必须是真实 {mode} PNG")
        _validate_image_size(opened.size)
        if size is not None and opened.size != size:
            raise ValueError("裁剪图片与候选 Alpha 尺寸必须一致")
        return opened.copy()


def _contract_record(run_root: Path, path: Path, mode: str, size: tuple[int, int]) -> dict[str, Any]:
    return {
        "path": _relative_contract_path(run_root, path, must_exist=True),
        "sha256": _source_digest(path),
        "size": list(size),
        "mode": mode,
    }


def _validate_cropped_inputs(
    cropped_original_path: Path,
    cropped_detection_path: Path,
    cropped_local_detection_path: Path,
    candidate_alpha_path: Path,
    cropped_geometry_path: Path,
    crop_manifest_path: Path,
    outputs: MaskOutputPaths,
    *,
    require_unpublished_outputs: bool = True,
) -> tuple[Image.Image, Image.Image, Image.Image, Image.Image, dict[str, Any], dict[str, Any]]:
    inputs = (
        cropped_original_path,
        cropped_detection_path,
        cropped_local_detection_path,
        candidate_alpha_path,
        cropped_geometry_path,
        crop_manifest_path,
    )
    published = (outputs.mask_path, outputs.overlay_path, outputs.report_path)
    for path in inputs:
        _relative_contract_path(outputs.run_root, path, must_exist=True)
    for path in published:
        _relative_contract_path(outputs.run_root, path, must_exist=False)
        if require_unpublished_outputs and path.exists():
            raise FileExistsError(path)
    if len({path.resolve() for path in (*inputs, *published)}) != 9:
        raise ValueError("Mask 输入输出路径必须互不相同")

    original = _load_contract_image(cropped_original_path, "RGB")
    detection = _load_contract_image(cropped_detection_path, "L", original.size)
    local_detection = _load_contract_image(
        cropped_local_detection_path, "L", original.size
    )
    candidate = _load_contract_image(candidate_alpha_path, "L", original.size)
    if any(value not in {0, 255} for value in candidate.tobytes()):
        raise ValueError("candidate Alpha 只能包含 0 和 255")

    cropped_geometry = _require_mapping(
        json.loads(cropped_geometry_path.read_text(encoding="utf-8")),
        "裁剪几何",
    )
    audited = rasterize_cropped_geometry(cropped_geometry)
    if audited.size != candidate.size or audited.tobytes() != candidate.tobytes():
        raise ValueError("裁剪几何与 candidate Alpha 不一致")

    manifest = _require_mapping(
        json.loads(crop_manifest_path.read_text(encoding="utf-8")),
        "Crop Manifest",
    )
    if manifest.get("schema_version") != "geometry-crop-manifest-1.0":
        raise ValueError("Crop Manifest schema 不受支持")
    if manifest.get("product_id") != cropped_geometry.get("product_id"):
        raise ValueError("Crop Manifest 商品身份不一致")
    if manifest.get("crop_size") != list(original.size):
        raise ValueError("Crop Manifest 裁剪尺寸不一致")
    if manifest.get("source_size") != cropped_geometry.get("source_size"):
        raise ValueError("Crop Manifest source_size 与裁剪几何不一致")
    if manifest.get("crop_box") != cropped_geometry.get("crop_box"):
        raise ValueError("Crop Manifest crop_box 与裁剪几何不一致")
    if manifest.get("target_max_occupancy") != [77, 100]:
        raise ValueError("Crop Manifest target_max_occupancy 不合法")
    if manifest.get("crop_algorithm") != "geometry-crop-77-over-100-v1":
        raise ValueError("Crop Manifest 裁剪算法不受支持")
    if manifest.get("source_geometry_sha256") != cropped_geometry.get(
        "source_geometry_sha256"
    ):
        raise ValueError("Crop Manifest 源几何语义摘要不一致")
    if manifest.get("detection_image_sha256") != cropped_geometry.get(
        "detection_image_sha256"
    ):
        raise ValueError("Crop Manifest 全局检测图摘要不一致")
    source_size = manifest["source_size"]
    crop_box = manifest["crop_box"]
    content_box = manifest.get("content_box")
    if (
        not isinstance(content_box, list)
        or len(content_box) != 4
        or not all(type(value) is int for value in content_box)
        or not (
            0 <= crop_box[0] <= content_box[0] < content_box[2] <= crop_box[2] <= source_size[0]
            and 0
            <= crop_box[1]
            <= content_box[1]
            < content_box[3]
            <= crop_box[3]
            <= source_size[1]
        )
    ):
        raise ValueError("Crop Manifest content_box 或 crop_box 不合法")
    limited_axes = manifest.get("source_limited_axes")
    if (
        not isinstance(limited_axes, list)
        or len(set(limited_axes)) != len(limited_axes)
        or any(axis not in {"width", "height"} for axis in limited_axes)
    ):
        raise ValueError("Crop Manifest source_limited_axes 不合法")
    expected_border_ids = [
        primitive["id"]
        for primitive in cropped_geometry.get("primitives", [])
        if primitive.get("touches_border") is True
    ]
    if manifest.get("verified_source_border_primitive_ids") != expected_border_ids:
        raise ValueError("Crop Manifest 源图触边验证记录不一致")
    actual_occupancy = manifest.get("actual_occupancy")
    expected_occupancy = {
        "width": (content_box[2] - content_box[0]) / original.width,
        "height": (content_box[3] - content_box[1]) / original.height,
    }
    if actual_occupancy != expected_occupancy:
        raise ValueError("Crop Manifest actual_occupancy 不一致")

    source_geometry_record = manifest.get("source_geometry")
    if not isinstance(source_geometry_record, dict):
        raise ValueError("Crop Manifest 缺少 source_geometry")
    source_geometry_relative = source_geometry_record.get("path")
    if not isinstance(source_geometry_relative, str) or not source_geometry_relative:
        raise ValueError("Crop Manifest source_geometry 路径不合法")
    source_geometry_path = outputs.run_root / Path(source_geometry_relative)
    expected_source_geometry_record = {
        "path": _relative_contract_path(
            outputs.run_root, source_geometry_path, must_exist=True
        ),
        "sha256": _source_digest(source_geometry_path),
        "semantic_sha256": cropped_geometry.get("source_geometry_sha256"),
    }
    if source_geometry_record != expected_source_geometry_record:
        raise ValueError("Crop Manifest 源几何文件身份不一致")
    expected_records = {
        "cropped_original": _contract_record(
            outputs.run_root, cropped_original_path, "RGB", original.size
        ),
        "cropped_detection": _contract_record(
            outputs.run_root, cropped_detection_path, "L", original.size
        ),
        "cropped_local_detection": _contract_record(
            outputs.run_root, cropped_local_detection_path, "L", original.size
        ),
        "candidate_alpha": _contract_record(
            outputs.run_root, candidate_alpha_path, "L", original.size
        ),
    }
    records = manifest.get("outputs")
    if not isinstance(records, dict):
        raise ValueError("Crop Manifest 缺少 outputs")
    for name, expected in expected_records.items():
        if records.get(name) != expected:
            raise ValueError(f"Crop Manifest 的 {name} 记录不一致")
    geometry_record = manifest.get("cropped_geometry")
    expected_geometry_record = {
        "path": _relative_contract_path(
            outputs.run_root, cropped_geometry_path, must_exist=True
        ),
        "sha256": _source_digest(cropped_geometry_path),
        "semantic_sha256": cropped_geometry.get("cropped_geometry_sha256"),
    }
    if geometry_record != expected_geometry_record:
        raise ValueError("Crop Manifest 的 cropped_geometry 记录不一致")
    if manifest.get("source_geometry_sha256") != cropped_geometry.get(
        "source_geometry_sha256"
    ) or manifest.get("source_sha256") != cropped_geometry.get("source_sha256"):
        raise ValueError("Crop Manifest 与裁剪几何源身份不一致")
    return (
        original,
        detection,
        local_detection,
        candidate,
        cropped_geometry,
        manifest,
    )


def _draw_crop_pixel_primitive(
    draw: ImageDraw.ImageDraw,
    primitive: dict[str, Any],
) -> None:
    kind = primitive["type"]
    if kind == "polygon":
        draw.polygon(primitive["points"], fill=255)
    elif kind == "polyline":
        draw.line(
            primitive["points"],
            fill=255,
            width=primitive["width"],
            joint="curve",
        )
    elif kind == "ellipse":
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


def _cropped_border_gate(
    alpha: Image.Image,
    cropped_geometry: dict[str, Any],
) -> tuple[float, tuple[str, ...]]:
    border = _border_mask(alpha.size)
    final_contact = ImageChops.multiply(alpha, border)
    declared_contact = Image.new("L", alpha.size, 0)
    missing: list[str] = []
    for primitive in cropped_geometry["primitives"]:
        if primitive.get("touches_border") is not True:
            continue
        primitive_alpha = Image.new("L", alpha.size, 0)
        _draw_crop_pixel_primitive(ImageDraw.Draw(primitive_alpha), primitive)
        primitive_contact = ImageChops.multiply(primitive_alpha, border)
        if primitive_contact.getbbox() is None:
            missing.append(primitive["id"])
        declared_contact = ImageChops.lighter(declared_contact, primitive_contact)
    undeclared = ImageChops.subtract(final_contact, declared_contact)
    ratio = _count_nonzero(undeclared) / max(1, _count_nonzero(border))
    return ratio, tuple(missing)


def _build_draft_mask(
    original: Image.Image,
    detection: Image.Image,
    local_detection: Image.Image,
    candidate: Image.Image,
    cropped_geometry: dict[str, Any],
) -> tuple[
    Image.Image,
    BoundaryRefinementReport,
    DraftMaskAssessment,
    bytes,
    bytes,
]:
    alpha, refinement = refine_candidate_boundary(
        original,
        detection,
        local_detection,
        candidate,
    )
    mask = Image.new("RGBA", original.size, (255, 255, 255, 0))
    mask.putalpha(alpha)
    mask_bytes = _png_bytes(mask)
    editable = alpha.point(lambda value: 255 if value == 0 else 0)
    red_tint = Image.blend(
        original, Image.new("RGB", original.size, (230, 70, 55)), 0.65
    )
    overlay_bytes = _png_bytes(Image.composite(red_tint, original, editable))
    protected_ratio_exact, protected_ratio = _mask_ratio_values(alpha)
    _reported_ratio, border_contact_ratio = _mask_metrics(alpha)
    undeclared_border_ratio_exact, missing_border_ids = _cropped_border_gate(
        alpha, cropped_geometry
    )
    technical_blockers: list[str] = []
    if refinement.status == "fallback":
        technical_blockers.append("boundary_refinement_fallback")
    if not (
        refinement.invariant_core_preserved
        and refinement.invariant_outside_band
        and refinement.invariant_border_frozen
    ):
        technical_blockers.append("boundary_refinement_invariant_failed")
    if not _MIN_PROTECTED_RATIO <= protected_ratio_exact <= _MAX_PROTECTED_RATIO:
        technical_blockers.append("protected_ratio_out_of_range")
    if undeclared_border_ratio_exact > 0:
        technical_blockers.append("undeclared_border_contact")
    if missing_border_ids:
        technical_blockers.append("declared_border_contact_missing")
    reasons = [*technical_blockers]
    if not technical_blockers:
        reasons.append("mask_review_required")
    assessment = DraftMaskAssessment(
        status="fail" if technical_blockers else "review",
        reasons=reasons,
        protected_ratio=protected_ratio,
        background_editable_ratio=round(1 - protected_ratio_exact, 6),
        border_contact_ratio=border_contact_ratio,
        undeclared_border_contact_ratio=round(undeclared_border_ratio_exact, 6),
        unresolved_uncertain_region_ids=tuple(
            item["id"] for item in cropped_geometry.get("uncertain_regions", [])
        ),
        declared_border_contact_missing_ids=missing_border_ids,
        technical_blockers=tuple(technical_blockers),
    )
    return alpha, refinement, assessment, mask_bytes, overlay_bytes


def create_background_edit_assets(
    cropped_original_path: str | Path,
    cropped_detection_path: str | Path,
    cropped_local_detection_path: str | Path,
    candidate_alpha_path: str | Path,
    cropped_geometry_path: str | Path,
    crop_manifest_path: str | Path,
    outputs: MaskOutputPaths,
) -> DraftMaskAssessment:
    paths = tuple(
        Path(path)
        for path in (
            cropped_original_path,
            cropped_detection_path,
            cropped_local_detection_path,
            candidate_alpha_path,
            cropped_geometry_path,
            crop_manifest_path,
        )
    )
    original, detection, local_detection, candidate, cropped_geometry, manifest = (
        _validate_cropped_inputs(*paths, outputs)
    )
    _alpha, refinement, assessment, mask_bytes, overlay_bytes = _build_draft_mask(
        original,
        detection,
        local_detection,
        candidate,
        cropped_geometry,
    )
    report = {
        **asdict(assessment),
        "schema_version": "mask-assessment-draft-1.0",
        **_draft_audit_fields(
            paths,
            outputs,
            original,
            detection,
            local_detection,
            candidate,
            cropped_geometry,
            manifest,
        ),
        "boundary_refinement": asdict(refinement),
        "mask_sha256": _bytes_digest(mask_bytes),
        "overlay_sha256": _bytes_digest(overlay_bytes),
    }
    atomic_create_bytes(outputs.mask_path, mask_bytes)
    atomic_create_bytes(outputs.overlay_path, overlay_bytes)
    atomic_create_json(outputs.report_path, report)
    return assessment


def _draft_audit_fields(
    paths: tuple[Path, ...],
    outputs: MaskOutputPaths,
    original: Image.Image,
    detection: Image.Image,
    local_detection: Image.Image,
    candidate: Image.Image,
    cropped_geometry: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    return {
        "product_id": cropped_geometry["product_id"],
        "crop_manifest_sha256": _source_digest(paths[5]),
        "cropped_geometry_sha256": cropped_geometry["cropped_geometry_sha256"],
        "inputs": {
            "cropped_original": _contract_record(
                outputs.run_root, paths[0], "RGB", original.size
            ),
            "cropped_detection": _contract_record(
                outputs.run_root, paths[1], "L", detection.size
            ),
            "cropped_local_detection": _contract_record(
                outputs.run_root, paths[2], "L", local_detection.size
            ),
            "candidate_alpha": _contract_record(
                outputs.run_root, paths[3], "L", candidate.size
            ),
        },
        "crop_manifest": manifest,
        "crop_manifest_path": _relative_contract_path(
            outputs.run_root, paths[5], must_exist=True
        ),
        "consensus_required": 2,
        "edge_algorithms": {
            "original_rgb": "rgb-luminance-and-chroma-forward-difference-v1",
            "global_robust": "luminance-forward-difference-v1",
            "local_limited": "luminance-forward-difference-v1",
        },
    }


def validate_published_mask_technical_gate(
    cropped_original_path: str | Path,
    cropped_detection_path: str | Path,
    cropped_local_detection_path: str | Path,
    candidate_alpha_path: str | Path,
    cropped_geometry_path: str | Path,
    crop_manifest_path: str | Path,
    *,
    mask_path: str | Path,
    overlay_path: str | Path,
    draft_assessment_path: str | Path,
    run_root: str | Path,
) -> PublishedMaskTechnicalGate:
    input_paths = tuple(
        Path(path)
        for path in (
            cropped_original_path,
            cropped_detection_path,
            cropped_local_detection_path,
            candidate_alpha_path,
            cropped_geometry_path,
            crop_manifest_path,
        )
    )
    outputs = MaskOutputPaths(
        run_root=Path(run_root),
        mask_path=Path(mask_path),
        overlay_path=Path(overlay_path),
        report_path=Path(draft_assessment_path),
    )
    original, detection, local_detection, candidate, cropped_geometry, manifest = (
        _validate_cropped_inputs(
            *input_paths,
            outputs,
            require_unpublished_outputs=False,
        )
    )
    _alpha, refinement, assessment, mask_bytes, overlay_bytes = _build_draft_mask(
        original,
        detection,
        local_detection,
        candidate,
        cropped_geometry,
    )
    blockers = list(assessment.technical_blockers)
    try:
        if outputs.mask_path.read_bytes() != mask_bytes:
            blockers.append("published_mask_mismatch")
        if outputs.overlay_path.read_bytes() != overlay_bytes:
            blockers.append("published_overlay_mismatch")
        draft = json.loads(outputs.report_path.read_text(encoding="utf-8"))
        if not isinstance(draft, dict):
            raise ValueError("draft assessment 必须是对象")
        normalized_assessment = json.loads(
            json.dumps(asdict(assessment), ensure_ascii=False)
        )
        if any(draft.get(key) != value for key, value in normalized_assessment.items()):
            blockers.append("draft_assessment_mismatch")
        normalized_refinement = json.loads(
            json.dumps(asdict(refinement), ensure_ascii=False)
        )
        if draft.get("boundary_refinement") != normalized_refinement:
            blockers.append("draft_assessment_mismatch")
        if draft.get("mask_sha256") != _bytes_digest(mask_bytes):
            blockers.append("draft_assessment_mismatch")
        if draft.get("overlay_sha256") != _bytes_digest(overlay_bytes):
            blockers.append("draft_assessment_mismatch")
        if draft.get("consensus_required") != 2:
            blockers.append("draft_assessment_mismatch")
        expected_audit = _draft_audit_fields(
            input_paths,
            outputs,
            original,
            detection,
            local_detection,
            candidate,
            cropped_geometry,
            manifest,
        )
        if any(draft.get(key) != value for key, value in expected_audit.items()):
            blockers.append("draft_assessment_mismatch")
    except (OSError, ValueError, json.JSONDecodeError):
        blockers.append("draft_assessment_mismatch")
    blockers = list(dict.fromkeys(blockers))
    return PublishedMaskTechnicalGate(
        passed=not blockers and assessment.status == "review",
        blockers=tuple(blockers),
        assessment=assessment,
    )


def create_mask(
    input_path: str | Path,
    output_path: str | Path,
    geometry_profile_path: str | Path,
    product_id: str,
) -> MaskAssessment:
    input_path = Path(input_path)
    output_path = Path(output_path)
    _validate_asset_paths(input_path, (output_path,))
    image, profile, _digest = _identity_checked_profile(input_path, Path(geometry_profile_path), product_id)
    candidate = rasterize_vision_geometry(profile, image.size)
    alpha, refinement = refine_candidate_boundary_legacy_read_only(image, candidate)
    mask = Image.new("RGBA", image.size, (255, 255, 255, 0))
    mask.putalpha(alpha)
    mask_bytes = _png_bytes(mask)
    geometry_sha256 = _geometry_digest(profile)
    mask_sha256 = _bytes_digest(mask_bytes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(mask_bytes)
    return _assessment(
        alpha,
        profile,
        refinement,
        geometry_sha256,
        mask_sha256,
    )


def _assessment_exit_code(assessment: MaskAssessment) -> int:
    return ASSESSMENT_EXIT_CODES[assessment.status]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从视觉语义几何创建珠宝背景 Edit Mask。")
    parser.add_argument("--input", required=True)
    parser.add_argument("--geometry-profile", required=True)
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--output")
    parser.add_argument("--image-output")
    parser.add_argument("--mask-output")
    parser.add_argument("--overlay")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    try:
        # 先读取图像头并执行像素上限校验，再解析几何，避免超大输入进入后续路径。
        _load_rgb(Path(args.input))
        asset_values = (args.image_output, args.mask_output, args.overlay, args.report)
        if any(asset_values):
            if not all(asset_values):
                parser.error("image-output、mask-output、overlay 和 report 必须同时提供")
            assessment = create_background_edit_assets_legacy_read_only(
                args.input, args.image_output, args.mask_output, args.overlay, args.report,
                args.geometry_profile, args.product_id,
            )
        else:
            if not args.output:
                parser.error("需要 --output 或完整的资产输出参数")
            assessment = create_mask(args.input, args.output, args.geometry_profile, args.product_id)
        return _assessment_exit_code(assessment)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


__all__ = [
    "VisionGeometryProfile",
    "BoundaryVariantReport",
    "BoundaryRefinementReport",
    "DraftMaskAssessment",
    "MaskAssessment",
    "MaskOutputPaths",
    "PublishedMaskTechnicalGate",
    "ASSESSMENT_EXIT_CODES",
    "MAX_IMAGE_PIXELS",
    "load_vision_geometry",
    "rasterize_vision_geometry",
    "refine_candidate_boundary",
    "refine_candidate_boundary_legacy_read_only",
    "validate_published_mask_technical_gate",
    "create_background_edit_assets",
    "create_background_edit_assets_legacy_read_only",
    "create_mask",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
