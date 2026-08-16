#!/usr/bin/env python3
"""生成仅供 Mask 边缘检测使用的固定图像版本。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageOps


_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)

from workflow_state import atomic_create_bytes  # noqa: E402


MAX_IMAGE_PIXELS = 20_000_000
GLOBAL_LOW_FRACTION = 0.01
GLOBAL_HIGH_FRACTION = 0.99
MIN_GLOBAL_RANGE = 96
LOCAL_DETAIL_LOW = 96
LOCAL_DETAIL_HIGH = 160
LOCAL_BLUR_RATIO = 0.025


@dataclass(frozen=True)
class DetectionVariant:
    name: str
    image: Image.Image
    black_point: int
    white_point: int
    clipped_ratio: float


def _validate_image_size(size: tuple[int, int]) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸必须为正整数")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("图片不得超过 20MP（20,000,000 像素）")


def percentile(image: Image.Image, fraction: float) -> int:
    if not 0 <= fraction <= 1:
        raise ValueError("百分位必须位于 0..1")
    histogram = image.histogram()
    target = round((sum(histogram) - 1) * fraction)
    seen = 0
    for value, count in enumerate(histogram):
        seen += count
        if seen > target:
            return value
    return 255


def stretch_luminance(
    image: Image.Image,
    low_fraction: float = GLOBAL_LOW_FRACTION,
    high_fraction: float = GLOBAL_HIGH_FRACTION,
) -> tuple[Image.Image, int, int, float]:
    if not 0 <= low_fraction < high_fraction <= 1:
        raise ValueError("全局色阶百分位必须满足 0 <= low < high <= 1")
    _validate_image_size(image.size)
    luminance = image.convert("L")
    low = min(LOCAL_DETAIL_LOW, percentile(luminance, low_fraction))
    high = max(LOCAL_DETAIL_HIGH, percentile(luminance, high_fraction))
    if high - low < MIN_GLOBAL_RANGE:
        center = (high + low) // 2
        low = max(0, center - MIN_GLOBAL_RANGE // 2)
        high = min(255, low + MIN_GLOBAL_RANGE)
        low = max(0, high - MIN_GLOBAL_RANGE)
    table = [
        max(0, min(255, round((value - low) * 255 / max(1, high - low))))
        for value in range(256)
    ]
    enhanced = luminance.point(table)
    histogram = luminance.histogram()
    clipped = sum(histogram[: low + 1]) + sum(histogram[high:])
    clipped_ratio = round(clipped / max(1, image.width * image.height), 6)
    return enhanced, low, high, clipped_ratio


def local_limited(image: Image.Image) -> tuple[Image.Image, int, int, float]:
    _validate_image_size(image.size)
    gray = image.convert("L")
    radius = max(1, round(min(image.size) * LOCAL_BLUR_RATIO))
    local_mean = gray.filter(ImageFilter.BoxBlur(radius))
    raw_detail = ImageChops.subtract(gray, local_mean, scale=1.0, offset=128)
    detail = raw_detail.point(
        lambda value: max(LOCAL_DETAIL_LOW, min(LOCAL_DETAIL_HIGH, value))
    )
    result = ImageChops.add(gray, detail, scale=1.0, offset=-128)
    clipped = sum(ImageChops.difference(raw_detail, detail).histogram()[1:])
    clipped_ratio = round(clipped / max(1, image.width * image.height), 6)
    return result, LOCAL_DETAIL_LOW, LOCAL_DETAIL_HIGH, clipped_ratio


def prepare_mask_detection_images(
    image: Image.Image,
) -> tuple[DetectionVariant, DetectionVariant, DetectionVariant]:
    """固定生成原始亮度、全局色阶和受限局部对比度三个检测版本。"""
    _validate_image_size(image.size)
    original = image.convert("RGB").convert("L")
    global_image, global_low, global_high, global_clipped = stretch_luminance(image)
    local_image, local_low, local_high, local_clipped = local_limited(image)
    return (
        DetectionVariant("original", original, 0, 255, 0.0),
        DetectionVariant(
            "global_robust", global_image, global_low, global_high, global_clipped
        ),
        DetectionVariant(
            "local_limited", local_image, local_low, local_high, local_clipped
        ),
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


def _validate_paths(input_path: Path, output_paths: tuple[Path, ...]) -> None:
    keys = [_path_key(input_path), *(_path_key(path) for path in output_paths)]
    if len(set(keys)) != len(keys):
        raise ValueError("输入路径与所有输出路径必须互不相同")


def _relative_run_path(run_root: Path, path: Path) -> str:
    root = run_root.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("检测输入和输出必须位于当前运行目录") from exc


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def write_detection_images(
    input_path: str | Path,
    global_output_path: str | Path,
    local_output_path: str | Path,
    report_path: str | Path,
    *,
    run_root: str | Path,
) -> dict[str, object]:
    input_path = Path(input_path)
    global_output_path = Path(global_output_path)
    local_output_path = Path(local_output_path)
    report_path = Path(report_path)
    run_root = Path(run_root)
    outputs = (global_output_path, local_output_path, report_path)
    _validate_paths(input_path, outputs)
    _relative_run_path(run_root, input_path)
    for path in outputs:
        _relative_run_path(run_root, path)
        if path.exists():
            raise FileExistsError(path)

    source_bytes = input_path.read_bytes()
    image = _load_rgb(input_path)
    variants = prepare_mask_detection_images(image)
    by_name = {variant.name: variant for variant in variants}
    global_bytes = _png_bytes(by_name["global_robust"].image)
    local_bytes = _png_bytes(by_name["local_limited"].image)
    output_data: dict[str, tuple[Path, bytes]] = {
        "global_robust": (global_output_path, global_bytes),
        "local_limited": (local_output_path, local_bytes),
    }
    global_variant = by_name["global_robust"]
    local_variant = by_name["local_limited"]
    local_radius = max(1, round(min(image.size) * LOCAL_BLUR_RATIO))
    report: dict[str, object] = {
        "schema_version": "mask-detection-images-2.0",
        "detection_only": True,
        "source_sha256": _sha256_bytes(source_bytes),
        "source_size": list(image.size),
        "coordinate_transform": "identity",
        "outputs": {
            name: {
                "path": _relative_run_path(run_root, path),
                "sha256": _sha256_bytes(data),
                "size": list(image.size),
                "mode": "L",
            }
            for name, (path, data) in output_data.items()
        },
        "algorithms": {
            "global_robust": {
                "low_fraction": GLOBAL_LOW_FRACTION,
                "high_fraction": GLOBAL_HIGH_FRACTION,
                "black_point": global_variant.black_point,
                "white_point": global_variant.white_point,
                "minimum_range": MIN_GLOBAL_RANGE,
                "clipped_ratio": global_variant.clipped_ratio,
            },
            "local_limited": {
                "blur_ratio": LOCAL_BLUR_RATIO,
                "blur_radius": local_radius,
                "detail_range": [LOCAL_DETAIL_LOW, LOCAL_DETAIL_HIGH],
                "clipped_ratio": local_variant.clipped_ratio,
            },
        },
        "authoritative_implementation": (
            "skills/jewelry-white-background/scripts/prepare_mask_detection_images.py"
        ),
    }
    report_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )

    atomic_create_bytes(global_output_path, global_bytes)
    atomic_create_bytes(local_output_path, local_bytes)
    atomic_create_bytes(report_path, report_bytes)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成仅供 Mask 检测使用的固定色阶和局部对比度图片。"
    )
    parser.add_argument("--input", required=True, help="唯一源图")
    parser.add_argument("--global-output", required=True, help="全局色阶检测图 PNG")
    parser.add_argument("--local-output", required=True, help="局部对比度检测图 PNG")
    parser.add_argument("--report", required=True, help="检测参数与摘要 JSON")
    parser.add_argument("--run-root", required=True, help="当前运行根目录")
    args = parser.parse_args(argv)
    try:
        write_detection_images(
            args.input,
            args.global_output,
            args.local_output,
            args.report,
            run_root=args.run_root,
        )
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


__all__ = [
    "DetectionVariant",
    "MAX_IMAGE_PIXELS",
    "percentile",
    "stretch_luminance",
    "local_limited",
    "prepare_mask_detection_images",
    "write_detection_images",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
