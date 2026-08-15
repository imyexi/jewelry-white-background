from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw


SCRIPT_PATH = (
    Path(__file__).parents[1] / "scripts" / "prepare_mask_detection_images.py"
)


def load_module():
    module_name = "skill_prepare_mask_detection_images"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def sample_image() -> Image.Image:
    image = Image.new("RGB", (96, 72), (224, 226, 228))
    draw = ImageDraw.Draw(image)
    draw.ellipse((18, 12, 76, 64), fill=(92, 118, 142))
    draw.line((12, 36, 84, 36), fill=(210, 42, 80), width=2)
    return image


def test_fixed_tool_returns_three_detection_variants_without_mutating_source() -> None:
    module = load_module()
    source = sample_image()
    before = source.tobytes()

    variants = module.prepare_mask_detection_images(source)

    assert [item.name for item in variants] == [
        "original",
        "global_robust",
        "local_limited",
    ]
    assert all(item.image.mode == "L" and item.image.size == source.size for item in variants)
    assert variants[0].black_point == 0
    assert variants[0].white_point == 255
    assert variants[1].white_point - variants[1].black_point >= 96
    assert (variants[2].black_point, variants[2].white_point) == (96, 160)
    assert source.mode == "RGB"
    assert source.tobytes() == before


def test_fixed_tool_is_deterministic() -> None:
    module = load_module()
    source = sample_image()

    first = module.prepare_mask_detection_images(source)
    second = module.prepare_mask_detection_images(source)

    assert [item.image.tobytes() for item in first] == [
        item.image.tobytes() for item in second
    ]
    assert [
        (item.name, item.black_point, item.white_point, item.clipped_ratio)
        for item in first
    ] == [
        (item.name, item.black_point, item.white_point, item.clipped_ratio)
        for item in second
    ]


def test_local_limited_reports_actual_clipped_pixel_ratio() -> None:
    module = load_module()
    image = Image.new("L", (80, 40), 128)
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            pixels[x, y] = 0 if (x + y) % 2 == 0 else 255

    enhanced, low, high, clipped_ratio = module.local_limited(image)
    gray = image.convert("L")
    radius = max(1, round(min(image.size) * 0.025))
    local_mean = gray.filter(module.ImageFilter.BoxBlur(radius))
    raw_detail = ImageChops.subtract(gray, local_mean, scale=1.0, offset=128)
    limited_detail = raw_detail.point(lambda value: max(96, min(160, value)))
    expected_clipped = sum(
        value != 0
        for value in ImageChops.difference(raw_detail, limited_detail).tobytes()
    )

    assert enhanced.mode == "L"
    assert (low, high) == (96, 160)
    assert clipped_ratio == round(expected_clipped / (image.width * image.height), 6)


def test_cli_writes_detection_only_images_and_traceable_report(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    global_output = tmp_path / "global.png"
    local_output = tmp_path / "local.png"
    report_path = tmp_path / "report.json"
    sample_image().save(source, "PNG")
    source_before = source.read_bytes()

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--input",
            str(source),
            "--global-output",
            str(global_output),
            "--local-output",
            str(local_output),
            "--report",
            str(report_path),
        ],
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert source.read_bytes() == source_before
    for output in (global_output, local_output):
        with Image.open(output) as image:
            assert image.mode == "L"
            assert image.size == (96, 72)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "mask-detection-images-1.0"
    assert report["detection_only"] is True
    assert report["source_sha256"] == sha256_file(source)
    assert report["source_size"] == [96, 72]
    variants = {item["name"]: item for item in report["variants"]}
    assert set(variants) == {"global_robust", "local_limited"}
    assert variants["global_robust"]["output_sha256"] == sha256_file(global_output)
    assert variants["local_limited"]["output_sha256"] == sha256_file(local_output)


def test_cli_rejects_input_output_path_alias_before_writing(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    sample_image().save(source, "PNG")
    source_before = source.read_bytes()

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--input",
            str(source),
            "--global-output",
            str(source),
            "--local-output",
            str(tmp_path / "local.png"),
            "--report",
            str(tmp_path / "report.json"),
        ],
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert source.read_bytes() == source_before
    assert not (tmp_path / "local.png").exists()
    assert not (tmp_path / "report.json").exists()
