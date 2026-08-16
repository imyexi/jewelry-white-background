from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw


SCRIPT = Path(__file__).parents[1] / "scripts" / "layout_generated_result.py"


def load_module(name: str = "layout_generated_result_test"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def generated_image(
    size: tuple[int, int] = (600, 600),
    box: tuple[int, int, int, int] = (150, 100, 450, 500),
    *,
    background: tuple[int, int, int] = (240, 240, 240),
    product: tuple[int, int, int] = (30, 30, 30),
) -> Image.Image:
    image = Image.new("RGB", size, background)
    ImageDraw.Draw(image).rectangle(
        (box[0], box[1], box[2] - 1, box[3] - 1), fill=product
    )
    return image


def layout_fixture(tmp_path: Path, image: Image.Image, product_id: str = "SY1537"):
    root = tmp_path / product_id / "run-1"
    edit_dir = root / "edit"
    layout_dir = root / "layout"
    manifests_dir = root / "manifests"
    edit_dir.mkdir(parents=True)
    layout_dir.mkdir()
    manifests_dir.mkdir()
    input_path = edit_dir / f"{product_id}_edit-result.png"
    image.save(input_path, "PNG")
    request_identity = {
        "provider": "wawapi",
        "operation": "edit",
        "endpoint": "https://example.test/v1/images/edits",
        "model": "gpt-image-2",
        "size": "600x600",
        "n": 1,
        "image_size": [600, 600],
        "mask_size": [600, 600],
        "image_sha256": "A" * 64,
        "mask_sha256": "B" * 64,
        "prompt_sha256": "C" * 64,
    }
    edit_manifest_path = manifests_dir / f"{product_id}_edit_result.json"
    edit_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "jewelry-edit-result-1.0",
                "run_id": "run-1",
                "product_id": product_id,
                "call_slot": 1,
                "request_identity": request_identity,
                "request_identity_sha256": canonical_sha256(request_identity),
                "result": {
                    "path": input_path.relative_to(root).as_posix(),
                    "format": "PNG",
                    "size": list(image.size),
                    "bytes": input_path.stat().st_size,
                    "sha256": sha256(input_path),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return (
        root,
        input_path,
        layout_dir / f"{product_id}_3x4_60pct.png",
        manifests_dir / f"{product_id}_layout_manifest.json",
    )


def test_public_signature_accepts_only_generated_result_and_outputs() -> None:
    module = load_module()

    assert list(inspect.signature(module.layout_generated_result).parameters) == [
        "input_path",
        "output_path",
        "manifest_path",
    ]


def test_layout_is_deterministic_and_records_required_audit_fields(
    tmp_path: Path,
) -> None:
    module = load_module("layout_determinism")
    first = layout_fixture(tmp_path / "first", generated_image())
    second = layout_fixture(tmp_path / "second", generated_image())

    first_manifest = module.layout_generated_result(*first[1:])
    second_manifest = module.layout_generated_result(*second[1:])

    assert first[2].read_bytes() == second[2].read_bytes()
    assert first_manifest == second_manifest
    with Image.open(first[2]) as opened:
        assert opened.size == (1536, 2048)
    assert first_manifest["layout_algorithm_version"] == "layout-algorithm-1.0"
    assert first_manifest["run_id"] == "run-1"
    assert first_manifest["product_id"] == "SY1537"
    assert first_manifest["actual_product_width"] == pytest.approx(922, abs=2)
    assert first_manifest["actual_product_center"][0] == pytest.approx(768, abs=1)
    assert first_manifest["actual_product_center"][1] == pytest.approx(900, abs=1)
    assert first_manifest["paste_xy"][0] < 0
    assert first_manifest["whole_generated_result_resized"] is True
    assert first_manifest["pre_generation_reference_used"] is False
    assert first_manifest["pre_generation_mask_used"] is False
    assert first_manifest["mask_cutout_used"] is False
    assert first_manifest["sharpening"] is None
    assert first_manifest["edit_result_manifest"]["path"] == (
        "manifests/SY1537_edit_result.json"
    )


def test_decoy_pre_generation_assets_do_not_change_output(tmp_path: Path) -> None:
    module = load_module("layout_decoy_isolation")
    results = []
    for name, color in (("red", (255, 0, 0)), ("blue", (0, 0, 255))):
        root, input_path, output_path, manifest_path = layout_fixture(
            tmp_path / name, generated_image()
        )
        for relative in (
            "cropped/SY1537_original.png",
            "mask/SY1537_product-protection-mask.png",
            "detection/SY1537_global.png",
        ):
            decoy = root / relative
            decoy.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (32, 32), color).save(decoy, "PNG")
        (root / "manifests/SY1537_crop_manifest.json").write_text(
            json.dumps({"decoy": color}), encoding="utf-8"
        )
        manifest = module.layout_generated_result(
            input_path, output_path, manifest_path
        )
        results.append((output_path.read_bytes(), manifest))

    assert results[0] == results[1]


@pytest.mark.parametrize(
    "image",
    [
        generated_image(box=(0, 100, 300, 500)),
        Image.new("RGB", (600, 600), (240, 240, 240)),
    ],
    ids=["product-touches-edge", "not-enough-strong-foreground"],
)
def test_layout_rejects_edge_touch_and_missing_foreground(
    tmp_path: Path, image: Image.Image
) -> None:
    module = load_module("layout_failure_gate")
    fixture = layout_fixture(tmp_path, image)

    with pytest.raises(module.LayoutError):
        module.layout_generated_result(*fixture[1:])

    assert not fixture[2].exists()
    assert not fixture[3].exists()


def test_layout_rejects_low_foreground_confidence(tmp_path: Path) -> None:
    module = load_module("layout_low_confidence")
    image = generated_image(product=(236, 240, 240))
    ImageDraw.Draw(image).rectangle((250, 250, 349, 349), fill=(30, 30, 30))
    fixture = layout_fixture(tmp_path, image)

    with pytest.raises(module.LayoutError):
        module.layout_generated_result(*fixture[1:])


def test_height_limited_layout_records_smaller_width(tmp_path: Path) -> None:
    module = load_module("layout_height_limited")
    fixture = layout_fixture(
        tmp_path,
        generated_image(
            size=(600, 1000),
            box=(250, 50, 350, 950),
        ),
    )

    manifest = module.layout_generated_result(*fixture[1:])

    assert manifest["scale_limited_by"] == ["product_height_at_target_center"]
    assert manifest["actual_product_width"] < 922
    assert manifest["actual_product_center"][0] == pytest.approx(768, abs=1)
    assert manifest["actual_product_center"][1] == pytest.approx(900, abs=1)


def test_intermediate_image_over_20mp_fails_before_resize(tmp_path: Path) -> None:
    module = load_module("layout_intermediate_limit")
    fixture = layout_fixture(
        tmp_path,
        generated_image(
            size=(1000, 1000),
            box=(480, 480, 520, 520),
        ),
    )

    with pytest.raises(module.LayoutError, match="20,000,000"):
        module.layout_generated_result(*fixture[1:])

    assert not fixture[2].exists()


def test_edit_result_manifest_must_bind_exact_input(tmp_path: Path) -> None:
    module = load_module("layout_edit_manifest_binding")
    fixture = layout_fixture(tmp_path, generated_image())
    edit_manifest = fixture[0] / "manifests/SY1537_edit_result.json"
    payload = json.loads(edit_manifest.read_text(encoding="utf-8"))
    payload["result"]["sha256"] = "F" * 64
    edit_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(module.LayoutError, match="Edit Result Manifest"):
        module.layout_generated_result(*fixture[1:])


@pytest.mark.parametrize("field", ["run_id", "product_id"])
def test_edit_result_manifest_identity_must_match_run_directory(
    tmp_path: Path, field: str
) -> None:
    module = load_module(f"layout_edit_identity_{field}")
    fixture = layout_fixture(tmp_path, generated_image())
    edit_manifest = fixture[0] / "manifests/SY1537_edit_result.json"
    payload = json.loads(edit_manifest.read_text(encoding="utf-8"))
    payload[field] = "different"
    edit_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(module.LayoutError, match="Edit Result Manifest"):
        module.layout_generated_result(*fixture[1:])


def test_even_corner_median_rounds_up(tmp_path: Path) -> None:
    module = load_module("layout_even_median")
    image = Image.new("RGB", (600, 600), (101, 101, 101))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 29, 29), fill=(100, 100, 100))
    draw.rectangle((0, 570, 29, 599), fill=(100, 100, 100))
    draw.rectangle((150, 100, 449, 499), fill=(30, 30, 30))
    fixture = layout_fixture(tmp_path, image)

    manifest = module.layout_generated_result(*fixture[1:])

    assert manifest["corner_sampling"]["background_rgb_median"] == [101, 101, 101]


def test_alpha_is_composited_on_white(tmp_path: Path) -> None:
    module = load_module("layout_alpha_white")
    image = Image.new("RGBA", (600, 600), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle(
        (150, 100, 449, 499), fill=(30, 30, 30, 255)
    )
    fixture = layout_fixture(tmp_path, image)

    manifest = module.layout_generated_result(*fixture[1:])

    assert manifest["corner_sampling"]["background_rgb_median"] == [255, 255, 255]


def test_palette_transparency_is_composited_on_white(tmp_path: Path) -> None:
    module = load_module("layout_palette_transparency")
    image = Image.new("P", (600, 600), 0)
    palette = [0, 0, 0, 30, 30, 30] + [0] * (768 - 6)
    image.putpalette(palette)
    image.info["transparency"] = 0
    ImageDraw.Draw(image).rectangle((150, 100, 449, 499), fill=1)
    fixture = layout_fixture(tmp_path, image)

    manifest = module.layout_generated_result(*fixture[1:])

    assert manifest["corner_sampling"]["background_rgb_median"] == [255, 255, 255]


def test_rounded_intermediate_edge_below_one_is_rejected(tmp_path: Path) -> None:
    module = load_module("layout_zero_sized_intermediate")
    fixture = layout_fixture(
        tmp_path,
        generated_image(
            size=(32000, 16),
            box=(500, 4, 31500, 12),
        ),
    )

    with pytest.raises(module.LayoutError, match="小于 1"):
        module.layout_generated_result(*fixture[1:])


@pytest.mark.parametrize(
    ("field", "nested"),
    [
        ("schema_version", None),
        ("call_slot", None),
        ("request_identity_sha256", None),
        ("bytes", "result"),
        ("format", "result"),
        ("size", "result"),
    ],
)
def test_incomplete_edit_result_manifest_is_rejected(
    tmp_path: Path, field: str, nested: str | None
) -> None:
    module = load_module(f"layout_incomplete_edit_manifest_{field}")
    fixture = layout_fixture(tmp_path, generated_image())
    edit_manifest = fixture[0] / "manifests/SY1537_edit_result.json"
    payload = json.loads(edit_manifest.read_text(encoding="utf-8"))
    target = payload if nested is None else payload[nested]
    target.pop(field)
    edit_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(module.LayoutError, match="Edit Result Manifest"):
        module.layout_generated_result(*fixture[1:])


def test_cmyk_icc_conversion_receives_original_color_mode(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_module("layout_cmyk_icc_mode")
    path = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (32, 32), (0, 20, 30, 10)).save(
        path, "JPEG", icc_profile=b"fake-cmyk-profile"
    )
    monkeypatch.setattr(module.ImageCms, "ImageCmsProfile", lambda _: object())
    monkeypatch.setattr(module.ImageCms, "createProfile", lambda _: object())

    def convert(image, _source, _target, *, outputMode):
        assert image.mode == "CMYK"
        assert outputMode == "RGB"
        return image.convert("RGB")

    monkeypatch.setattr(module.ImageCms, "profileToProfile", convert)

    normalized, source_format = module._load_generated_rgb(path)

    assert source_format == "JPEG"
    assert normalized.mode == "RGB"


def test_icc_conversion_failure_is_reported_as_layout_error(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_module("layout_icc_failure")
    path = tmp_path / "profiled.jpg"
    Image.new("RGB", (32, 32), (240, 240, 240)).save(
        path, "JPEG", icc_profile=b"fake-profile"
    )
    monkeypatch.setattr(module.ImageCms, "ImageCmsProfile", lambda _: object())
    monkeypatch.setattr(module.ImageCms, "createProfile", lambda _: object())

    def fail(*_args, **_kwargs):
        raise module.ImageCms.PyCMSError("cannot build transform")

    monkeypatch.setattr(module.ImageCms, "profileToProfile", fail)

    with pytest.raises(module.LayoutError, match="sRGB RGB"):
        module._load_generated_rgb(path)


def test_grayscale_alpha_icc_conversion_preserves_l_color_mode(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_module("layout_la_icc_mode")
    path = tmp_path / "grayscale-alpha.png"
    Image.new("LA", (32, 32), (180, 128)).save(
        path, "PNG", icc_profile=b"fake-grayscale-profile"
    )
    monkeypatch.setattr(module.ImageCms, "ImageCmsProfile", lambda _: object())
    monkeypatch.setattr(module.ImageCms, "createProfile", lambda _: object())

    def convert(image, _source, _target, *, outputMode):
        assert image.mode == "L"
        assert outputMode == "RGB"
        return Image.merge("RGB", (image, image, image))

    monkeypatch.setattr(module.ImageCms, "profileToProfile", convert)

    normalized, source_format = module._load_generated_rgb(path)

    assert source_format == "PNG"
    assert normalized.mode == "RGB"
