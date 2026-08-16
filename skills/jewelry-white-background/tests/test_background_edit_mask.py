from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import struct
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "create_background_edit_mask.py"
REPO_ROOT = Path(__file__).parents[3]
ROOT_SCRIPT_PATH = REPO_ROOT / "scripts" / "create_background_edit_mask.py"


def load_module():
    module_name = "skill_create_background_edit_mask"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def write_source(path: Path, image: Image.Image) -> str:
    image.save(path, "PNG")
    return sha256_file(path)


def geometry_payload(
    source: Path,
    digest: str,
    size: tuple[int, int],
    *,
    product_id: str = "SYTEST",
) -> dict[str, object]:
    return {
        "schema_version": "vision-geometry-mask-2.0",
        "product_id": product_id,
        "source_sha256": digest,
        "source_size": list(size),
        "coordinate_range": [0, 10000],
        "producer": "codex-cloud-vision",
        "source_path": str(source),
        "primitives": [
            {
                "id": "pendant",
                "type": "polygon",
                "semantic": "可见吊坠主体",
                "points": [[3900, 3500], [6100, 3500], [6700, 6800], [3300, 6800]],
            },
            {
                "id": "cord",
                "type": "polyline",
                "semantic": "可见细绳",
                "points": [[5000, 0], [5000, 3600]],
                "width": 180,
                "touches_border": True,
            },
            {
                "id": "single_bead",
                "type": "ellipse",
                "semantic": "可见透明珠",
                "params": [2600, 7600, 650, 480],
            },
            {
                "id": "bead_group",
                "type": "ellipse_set",
                "semantic": "可见珠组",
                "items": [
                    {"id": "bead_left", "params": [4400, 7900, 550, 450]},
                    {"id": "bead_right", "params": [5900, 7900, 550, 450]},
                ],
            },
        ],
        "uncertain_regions": [
            {
                "id": "cord-border",
                "bbox": [4800, 0, 5200, 400],
                "reason": "细绳在画布顶边真实裁切",
            }
        ],
    }


def write_profile(
    path: Path,
    source: Path,
    digest: str,
    size: tuple[int, int],
    *,
    product_id: str = "SYTEST",
    wrapped: bool = False,
) -> dict[str, object]:
    profile = geometry_payload(source, digest, size, product_id=product_id)
    payload: dict[str, object]
    if wrapped:
        payload = {
            "schema_version": "vision-geometry-mask-2.0",
            "products": {product_id: profile},
        }
    else:
        payload = profile
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def add_review_from_draft(
    module,
    tmp_path: Path,
    source: Path,
    profile_path: Path,
    payload: dict[str, object],
    *,
    resolved_uncertain_regions: list[str] | None = None,
) -> dict[str, object]:
    """先生成待审资产，再把精确几何与 Mask 身份写入审核。"""
    profile_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    report_path = tmp_path / "draft-report.json"
    module.create_background_edit_assets_legacy_read_only(
        source,
        tmp_path / "draft-prepared.png",
        tmp_path / "draft-mask.png",
        tmp_path / "draft-overlay.png",
        report_path,
        profile_path,
        str(payload["product_id"]),
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    payload["mask_review"] = {
        "status": "approved",
        "reviewer": "圆圆",
        "source_sha256": payload["source_sha256"],
        "geometry_sha256": report["geometry_sha256"],
        "mask_sha256": report["mask_sha256"],
        "checked_items": ["商品组件完整", "未保护垫板或投影", "真实触边已确认"],
        "resolved_uncertain_regions": resolved_uncertain_regions or [],
    }
    profile_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return report


def rectangle_scene(
    *,
    size: tuple[int, int] = (180, 160),
    background: int = 228,
    foreground: int = 108,
    box: tuple[int, int, int, int] = (52, 42, 128, 118),
) -> Image.Image:
    image = Image.new("RGB", size, (background, background, background))
    ImageDraw.Draw(image).rectangle(box, fill=(foreground, foreground + 3, foreground + 6))
    return image


def binary_rectangle(
    size: tuple[int, int], box: tuple[int, int, int, int]
) -> Image.Image:
    alpha = Image.new("L", size, 0)
    ImageDraw.Draw(alpha).rectangle(box, fill=255)
    return alpha


def changed_pixels(first: Image.Image, second: Image.Image) -> int:
    return sum(value != 0 for value in ImageChops.difference(first, second).getdata())


def write_oversized_bmp_header(path: Path, width: int, height: int) -> None:
    dib = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height,
        1,
        24,
        0,
        width * height * 3,
        2835,
        2835,
        0,
        0,
    )
    path.write_bytes(struct.pack("<2sIHHI", b"BM", 54, 0, 0, 54) + dib)


def test_loads_wrapped_v2_profile_and_rasterizes_non_annular_primitives(
    tmp_path: Path,
) -> None:
    module = load_module()
    size = (200, 300)
    source = tmp_path / "source.png"
    digest = write_source(source, Image.new("RGB", size, (235, 238, 240)))
    profile_path = tmp_path / "profiles.json"
    write_profile(profile_path, source, digest, size, wrapped=True)

    profile = module.load_vision_geometry(profile_path, "SYTEST")
    alpha = module.rasterize_vision_geometry(profile, size)

    assert profile.schema_version == "vision-geometry-mask-2.0"
    assert profile.source_sha256 == digest
    assert profile.source_size == size
    assert profile.primitive_count == 4
    assert profile.touching_primitive_ids == ("cord",)
    assert alpha.mode == "L"
    assert alpha.size == size
    assert set(alpha.getdata()) == {0, 255}
    assert alpha.getpixel((100, 0)) == 255
    assert alpha.getpixel((100, 150)) == 255
    assert alpha.getpixel((52, 228)) == 255
    assert alpha.getpixel((5, 295)) == 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(schema_version="vision-geometry-mask-poc-1.0"), "schema"),
        (lambda payload: payload.update(source_sha256="a" * 64), "SHA-256"),
        (lambda payload: payload.update(source_size=[0, 300]), "尺寸"),
        (lambda payload: payload.update(coordinate_range=[0, 255]), "0..10000"),
        (
            lambda payload: payload["primitives"].append(payload["primitives"][0].copy()),
            "ID",
        ),
        (
            lambda payload: payload["primitives"][0]["points"].__setitem__(0, [10001, 10]),
            "0..10000",
        ),
    ],
)
def test_geometry_schema_rejects_invalid_identity_and_geometry(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    digest = write_source(source, Image.new("RGB", (200, 300), "white"))
    payload = geometry_payload(source, digest, (200, 300))
    mutation(payload)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        module.load_vision_geometry(profile_path, "SYTEST")


@pytest.mark.parametrize("producer", [None, "", "manual-local-contour"])
def test_geometry_requires_supported_cloud_vision_producer(
    tmp_path: Path,
    producer: str | None,
) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    digest = write_source(source, rectangle_scene())
    payload = geometry_payload(source, digest, (180, 160))
    if producer is None:
        payload.pop("producer")
    else:
        payload["producer"] = producer
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="producer"):
        module.load_vision_geometry(profile_path, "SYTEST")


@pytest.mark.parametrize(
    "primitive",
    [
        {
            "id": "collapsed-polygon",
            "type": "polygon",
            "semantic": "退化多边形",
            "points": [[5000, 5000], [5000, 5000], [5000, 5000]],
        },
        {
            "id": "zero-line",
            "type": "polyline",
            "semantic": "零长度细绳",
            "points": [[5000, 5000], [5000, 5000]],
            "width": 180,
        },
    ],
)
def test_geometry_rejects_degenerate_primitives(
    tmp_path: Path,
    primitive: dict[str, object],
) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    digest = write_source(source, rectangle_scene())
    payload = geometry_payload(source, digest, (180, 160))
    payload["primitives"] = [primitive]
    payload["uncertain_regions"] = []
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="退化|长度"):
        module.load_vision_geometry(profile_path, "SYTEST")


def test_rasterizer_rejects_non_binary_candidate_alpha(tmp_path: Path) -> None:
    module = load_module()
    image = rectangle_scene()
    candidate = binary_rectangle(image.size, (50, 40, 130, 120))
    candidate.putpixel((80, 80), 128)

    with pytest.raises(ValueError, match="二值"):
        module.refine_candidate_boundary(image, candidate)


@pytest.mark.parametrize("mismatch", ["sha", "size", "product"])
def test_assets_reject_source_identity_mismatch_before_writing(
    tmp_path: Path,
    mismatch: str,
) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    size = (180, 160)
    digest = write_source(source, rectangle_scene(size=size))
    profile_path = tmp_path / "profile.json"
    product_id = "OTHER" if mismatch == "product" else "SYTEST"
    payload = geometry_payload(source, digest, size, product_id=product_id)
    if mismatch == "sha":
        payload["source_sha256"] = "0" * 64
    elif mismatch == "size":
        payload["source_size"] = [181, 160]
    profile_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match={"sha": "SHA-256", "size": "尺寸", "product": "商品编号"}[mismatch]):
        module.create_background_edit_assets_legacy_read_only(
            source,
            tmp_path / "prepared.png",
            tmp_path / "mask.png",
            tmp_path / "overlay.png",
            tmp_path / "report.json",
            profile_path,
            "SYTEST",
        )

    assert not (tmp_path / "prepared.png").exists()
    assert not (tmp_path / "mask.png").exists()


def test_assets_keep_levels_in_memory_and_edit_image_pixel_identical(
    tmp_path: Path,
) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    source_bytes = source.read_bytes()
    profile_path = tmp_path / "profile.json"
    write_profile(profile_path, source, digest, image.size)
    prepared = tmp_path / "prepared.png"
    mask = tmp_path / "mask.png"
    overlay = tmp_path / "overlay.png"
    report = tmp_path / "report.json"

    assessment = module.create_background_edit_assets_legacy_read_only(
        source,
        prepared,
        mask,
        overlay,
        report,
        profile_path,
        "SYTEST",
    )

    assert source.read_bytes() == source_bytes
    with Image.open(source) as original, Image.open(prepared) as edit:
        expected = ImageOps.exif_transpose(original).convert("RGB")
        assert ImageChops.difference(expected, edit.convert("RGB")).getbbox() is None
    assert {item.name for item in tmp_path.iterdir()} == {
        "source.png",
        "profile.json",
        "prepared.png",
        "mask.png",
        "overlay.png",
        "report.json",
    }
    with Image.open(mask) as generated_mask:
        assert generated_mask.mode == "RGBA"
        assert set(generated_mask.convert("RGB").getdata()) == {(255, 255, 255)}
        assert set(generated_mask.getchannel("A").getdata()) == {0, 255}
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["source_sha256"] == digest
    assert payload["source_size"] == list(image.size)
    assert payload["primitive_count"] == 4
    assert payload["automatic_wawapi_edit_allowed"] is False
    assert payload["boundary_refinement"]["variants"][0]["name"] == "original"
    assert {item["name"] for item in payload["boundary_refinement"]["variants"]} == {
        "original",
        "global_robust",
        "local_limited",
    }
    assert assessment.status == "review"


def test_palette_transparency_is_composited_over_white_for_prepared_image(
    tmp_path: Path,
) -> None:
    module = load_module()
    source = tmp_path / "palette-transparent.png"
    palette = Image.new("P", (180, 160), 0)
    palette.putpalette([0, 0, 0, 90, 100, 110] + [0, 0, 0] * 254)
    palette.info["transparency"] = 0
    ImageDraw.Draw(palette).rectangle((52, 42, 128, 118), fill=1)
    palette.save(source, "PNG", transparency=0)
    digest = sha256_file(source)
    profile_path = tmp_path / "profile.json"
    write_profile(profile_path, source, digest, palette.size)
    prepared = tmp_path / "prepared.png"

    module.create_background_edit_assets_legacy_read_only(
        source,
        prepared,
        tmp_path / "mask.png",
        tmp_path / "overlay.png",
        tmp_path / "report.json",
        profile_path,
        "SYTEST",
    )

    with Image.open(prepared) as image:
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert image.getpixel((80, 80)) == (90, 100, 110)


def test_unreviewed_geometry_cannot_enable_automatic_wawapi_edit(tmp_path: Path) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    profile = tmp_path / "profile.json"
    write_profile(profile, source, digest, image.size)

    assessment = module.create_background_edit_assets_legacy_read_only(
        source,
        tmp_path / "prepared.png",
        tmp_path / "mask.png",
        tmp_path / "overlay.png",
        tmp_path / "report.json",
        profile,
        "SYTEST",
    )

    assert assessment.status == "review"
    assert assessment.automatic_wawapi_edit_allowed is False
    assert "mask_review_required" in assessment.reasons


def test_approved_mask_review_is_bound_to_source_and_auditable(tmp_path: Path) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, image.size)
    draft = add_review_from_draft(
        module,
        tmp_path,
        source,
        profile,
        payload,
        resolved_uncertain_regions=["cord-border"],
    )

    assessment = module.create_background_edit_assets_legacy_read_only(
        source,
        tmp_path / "prepared.png",
        tmp_path / "mask.png",
        tmp_path / "overlay.png",
        tmp_path / "report.json",
        profile,
        "SYTEST",
    )

    assert assessment.status == "ok"
    assert assessment.automatic_wawapi_edit_allowed is True
    assert assessment.mask_review_status == "approved"
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["mask_review"] == {
        "status": "approved",
        "reviewer": "圆圆",
        "source_sha256": digest,
        "geometry_sha256": draft["geometry_sha256"],
        "mask_sha256": draft["mask_sha256"],
        "checked_items": ["商品组件完整", "未保护垫板或投影", "真实触边已确认"],
        "resolved_uncertain_regions": ["cord-border"],
    }
    assert report["geometry_sha256"] == draft["geometry_sha256"]
    assert report["mask_sha256"] == draft["mask_sha256"]
    assert report["prepared_sha256"] == sha256_file(tmp_path / "prepared.png")


def test_approved_review_is_invalidated_when_geometry_changes(tmp_path: Path) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, image.size)
    add_review_from_draft(
        module,
        tmp_path,
        source,
        profile,
        payload,
        resolved_uncertain_regions=["cord-border"],
    )
    payload["primitives"][0]["points"][0] = [3600, 3400]
    profile.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assessment = module.create_background_edit_assets_legacy_read_only(
        source,
        tmp_path / "changed-prepared.png",
        tmp_path / "changed-mask.png",
        tmp_path / "changed-overlay.png",
        tmp_path / "changed-report.json",
        profile,
        "SYTEST",
    )

    assert assessment.status == "review"
    assert assessment.automatic_wawapi_edit_allowed is False
    assert "mask_review_geometry_mismatch" in assessment.reasons


def test_approved_review_is_invalidated_when_uncertainty_definition_changes(
    tmp_path: Path,
) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, image.size)
    add_review_from_draft(
        module,
        tmp_path,
        source,
        profile,
        payload,
        resolved_uncertain_regions=["cord-border"],
    )
    payload["uncertain_regions"][0]["bbox"] = [4700, 0, 5300, 500]
    profile.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assessment = module.create_background_edit_assets_legacy_read_only(
        source,
        tmp_path / "changed-prepared.png",
        tmp_path / "changed-mask.png",
        tmp_path / "changed-overlay.png",
        tmp_path / "changed-report.json",
        profile,
        "SYTEST",
    )

    assert assessment.status == "review"
    assert assessment.automatic_wawapi_edit_allowed is False
    assert "mask_review_geometry_mismatch" in assessment.reasons


@pytest.mark.parametrize(
    "mask_review",
    [
        {
            "status": "approved",
            "reviewer": "",
            "source_sha256": "{digest}",
            "checked_items": ["商品组件完整"],
        },
        {
            "status": "approved",
            "reviewer": "圆圆",
            "source_sha256": "0" * 64,
            "checked_items": ["商品组件完整"],
        },
        {
            "status": "approved",
            "reviewer": "圆圆",
            "source_sha256": "{digest}",
            "checked_items": [],
        },
    ],
)
def test_invalid_approved_mask_review_is_rejected(
    tmp_path: Path,
    mask_review: dict[str, object],
) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, image.size)
    payload["mask_review"] = {
        key: digest if value == "{digest}" else value
        for key, value in mask_review.items()
    }
    profile.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="Mask 审核"):
        module.load_vision_geometry(profile, "SYTEST")


def test_approved_review_must_explicitly_resolve_each_uncertain_region(
    tmp_path: Path,
) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, image.size)
    payload["primitives"] = [payload["primitives"][0]]
    payload["uncertain_regions"] = [
        {
            "id": "pendant-shadow-edge",
            "bbox": [3300, 3500, 6700, 6800],
            "reason": "吊坠边缘与投影接近",
        }
    ]
    add_review_from_draft(module, tmp_path, source, profile, payload)

    assessment = module.create_background_edit_assets_legacy_read_only(
        source,
        tmp_path / "prepared.png",
        tmp_path / "mask.png",
        tmp_path / "overlay.png",
        tmp_path / "report.json",
        profile,
        "SYTEST",
    )

    assert assessment.status == "review"
    assert assessment.automatic_wawapi_edit_allowed is False
    assert "unresolved_uncertain_regions" in assessment.reasons


def test_uncertain_region_requires_identity_bbox_and_reason(tmp_path: Path) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    digest = write_source(source, rectangle_scene())
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, (180, 160))
    payload["uncertain_regions"] = [{"bbox": [100, 100, 200, 200], "reason": "缺少编号"}]
    profile.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="uncertain_regions"):
        module.load_vision_geometry(profile, "SYTEST")


def test_review_rejects_resolution_of_unknown_uncertain_region(tmp_path: Path) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    digest = write_source(source, rectangle_scene())
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, (180, 160))
    payload["uncertain_regions"] = []
    add_review_from_draft(module, tmp_path, source, profile, payload)
    payload["mask_review"]["resolved_uncertain_regions"] = ["not-in-profile"]
    profile.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="不存在"):
        module.load_vision_geometry(profile, "SYTEST")


def test_declared_border_product_must_really_touch_canvas(tmp_path: Path) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, image.size)
    payload["primitives"] = [payload["primitives"][0]]
    payload["primitives"][0]["touches_border"] = True
    payload["uncertain_regions"] = []
    add_review_from_draft(module, tmp_path, source, profile, payload)

    assessment = module.create_background_edit_assets_legacy_read_only(
        source,
        tmp_path / "prepared.png",
        tmp_path / "mask.png",
        tmp_path / "overlay.png",
        tmp_path / "report.json",
        profile,
        "SYTEST",
    )

    assert assessment.status == "review"
    assert assessment.automatic_wawapi_edit_allowed is False
    assert "declared_border_contact_missing" in assessment.reasons


def test_declared_border_primitive_does_not_cover_other_undeclared_contacts(
    tmp_path: Path,
) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, image.size)
    payload["primitives"] = [
        {
            "id": "declared-top-cord",
            "type": "polyline",
            "semantic": "顶边真实细绳",
            "points": [[5000, 0], [5000, 2500]],
            "width": 180,
            "touches_border": True,
        },
        {
            "id": "undeclared-left-shape",
            "type": "polygon",
            "semantic": "不应被顶边声明背书的左侧区域",
            "points": [[0, 3500], [2200, 3500], [2200, 6500], [0, 6500]],
        },
    ]
    payload["uncertain_regions"] = []
    add_review_from_draft(module, tmp_path, source, profile, payload)

    assessment = module.create_background_edit_assets_legacy_read_only(
        source,
        tmp_path / "prepared.png",
        tmp_path / "mask.png",
        tmp_path / "overlay.png",
        tmp_path / "report.json",
        profile,
        "SYTEST",
    )

    assert assessment.status == "review"
    assert assessment.automatic_wawapi_edit_allowed is False
    assert "undeclared_border_contact" in assessment.reasons


def test_refinement_retracts_small_outer_margin_and_preserves_core() -> None:
    module = load_module()
    image = rectangle_scene(box=(52, 42, 128, 118))
    candidate = binary_rectangle(image.size, (50, 40, 130, 120))

    refined, report = module.refine_candidate_boundary(image, candidate)

    assert report.status == "applied"
    assert refined.getpixel((50, 80)) == 0
    assert refined.getpixel((52, 80)) == 255
    assert refined.getpixel((90, 80)) == 255
    assert report.changed_pixels > 0
    assert report.invariant_core_preserved is True
    assert report.invariant_outside_band is True


def test_refinement_expands_small_inner_candidate_to_supported_edge() -> None:
    module = load_module()
    image = rectangle_scene(box=(52, 42, 128, 118))
    candidate = binary_rectangle(image.size, (54, 44, 126, 116))

    refined, report = module.refine_candidate_boundary(image, candidate)

    assert report.status == "applied"
    assert refined.getpixel((52, 80)) == 255
    assert refined.getpixel((90, 42)) == 255
    assert refined.getpixel((49, 80)) == 0


def test_refinement_recalls_pale_transparent_bead_and_thin_cord() -> None:
    module = load_module()
    size = (180, 160)
    image = Image.new("RGB", size, (232, 235, 237))
    draw = ImageDraw.Draw(image)
    draw.ellipse((48, 42, 112, 106), fill=(226, 230, 232), outline=(202, 211, 217), width=2)
    draw.line((112, 74, 169, 23), fill=(211, 217, 221), width=3)
    candidate = Image.new("L", size, 0)
    mask_draw = ImageDraw.Draw(candidate)
    mask_draw.ellipse((50, 44, 110, 104), fill=255)
    mask_draw.line((108, 77, 168, 23), fill=255, width=5)

    refined, report = module.refine_candidate_boundary(image, candidate)

    assert report.status == "applied"
    assert refined.getpixel((48, 74)) == 255
    assert refined.getpixel((112, 74)) == 255
    assert refined.getpixel((150, 40)) == 255
    assert refined.getpixel((80, 74)) == 255


def test_refinement_uses_rgb_chroma_edge_when_luminance_matches_background() -> None:
    module = load_module()
    size = (180, 160)
    image = Image.new("RGB", size, (210, 210, 210))
    draw = ImageDraw.Draw(image)
    # 红色线与背景亮度接近，只有 RGB 色度边缘能稳定把它从背景中分开。
    draw.line((35, 80, 145, 80), fill=(255, 190, 190), width=3)
    candidate = Image.new("L", size, 0)
    ImageDraw.Draw(candidate).line((37, 80, 143, 80), fill=255, width=3)

    rgb_edge = module._edge_map(image)
    gray_edge = module._edge_map(image.convert("L"))
    refined, report = module.refine_candidate_boundary(image, candidate)

    assert rgb_edge.getpixel((35, 80)) > gray_edge.getpixel((35, 80))
    # 单一色度证据不越过跨版本共识门槛，但语义候选的细绳不能被删掉。
    assert refined.getpixel((37, 80)) == 255
    assert report.status == "fallback"


def test_refinement_falls_back_when_consensus_exists_only_outside_candidate_band(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    image = rectangle_scene()
    candidate = binary_rectangle(image.size, (50, 40, 130, 120))
    outside_only = Image.new("L", image.size, 0)
    ImageDraw.Draw(outside_only).rectangle((3, 3, 18, 150), fill=255)
    monkeypatch.setattr(module, "_edge_map", lambda _image: outside_only.copy())

    refined, report = module.refine_candidate_boundary(image, candidate)

    assert refined.tobytes() == candidate.tobytes()
    assert report.status == "fallback"
    assert report.consensus_band_pixels == 0


def test_approved_review_blocks_extreme_product_protection_ratio(tmp_path: Path) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, image.size)
    payload["primitives"] = [
        {
            "id": "oversized-background",
            "type": "polygon",
            "semantic": "测试用大面积候选",
            "points": [[0, 0], [10000, 0], [10000, 9000], [0, 9000]],
        }
    ]
    payload["uncertain_regions"] = []
    add_review_from_draft(module, tmp_path, source, profile, payload)

    assessment = module.create_background_edit_assets_legacy_read_only(
        source,
        tmp_path / "prepared.png",
        tmp_path / "mask.png",
        tmp_path / "overlay.png",
        tmp_path / "report.json",
        profile,
        "SYTEST",
    )

    assert assessment.status == "review"
    assert assessment.automatic_wawapi_edit_allowed is False
    assert assessment.protected_ratio > 0.70
    assert assessment.background_editable_ratio < 0.30
    assert "protected_ratio_out_of_range" in assessment.reasons


def test_approved_review_blocks_near_empty_product_protection_ratio(
    tmp_path: Path,
) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    image = rectangle_scene()
    digest = write_source(source, image)
    profile = tmp_path / "profile.json"
    payload = geometry_payload(source, digest, image.size)
    payload["primitives"] = [
        {
            "id": "implausibly-small-product",
            "type": "ellipse",
            "semantic": "测试用近空保护区",
            "params": [5000, 5000, 80, 80],
        }
    ]
    payload["uncertain_regions"] = []
    add_review_from_draft(module, tmp_path, source, profile, payload)

    assessment = module.create_background_edit_assets_legacy_read_only(
        source,
        tmp_path / "prepared.png",
        tmp_path / "mask.png",
        tmp_path / "overlay.png",
        tmp_path / "report.json",
        profile,
        "SYTEST",
    )

    assert assessment.status == "review"
    assert assessment.automatic_wawapi_edit_allowed is False
    assert assessment.protected_ratio < 0.005
    assert "protected_ratio_out_of_range" in assessment.reasons


def test_refinement_ignores_strong_shadow_outside_narrow_band() -> None:
    module = load_module()
    image = rectangle_scene()
    ImageDraw.Draw(image).rectangle((4, 8, 26, 150), fill=(75, 75, 75))
    candidate = binary_rectangle(image.size, (50, 40, 130, 120))

    refined, report = module.refine_candidate_boundary(image, candidate)

    assert refined.getpixel((10, 80)) == 0
    assert report.invariant_outside_band is True
    assert report.changed_pixels <= report.band_pixels


def test_refinement_freezes_canvas_border_for_declared_touching_product() -> None:
    module = load_module()
    image = Image.new("RGB", (180, 160), (232, 232, 232))
    ImageDraw.Draw(image).line((0, 80, 100, 80), fill=(120, 125, 130), width=5)
    candidate = Image.new("L", image.size, 0)
    ImageDraw.Draw(candidate).line((0, 80, 100, 80), fill=255, width=7)

    refined, report = module.refine_candidate_boundary(image, candidate)

    assert list(refined.crop((0, 0, 1, 160)).getdata()) == list(
        candidate.crop((0, 0, 1, 160)).getdata()
    )
    assert report.invariant_border_frozen is True


def test_low_contrast_refinement_falls_back_byte_for_byte() -> None:
    module = load_module()
    image = Image.new("RGB", (180, 160), (225, 225, 225))
    candidate = binary_rectangle(image.size, (50, 40, 130, 120))

    refined, report = module.refine_candidate_boundary(image, candidate)

    assert refined.tobytes() == candidate.tobytes()
    assert report.status == "fallback"
    assert report.fallback_reason == "insufficient_cross_variant_edge_evidence"
    assert report.changed_pixels == 0


def test_refinement_reuses_fixed_detection_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    image = rectangle_scene()
    candidate = binary_rectangle(image.size, (50, 40, 130, 120))
    original_tool = module.prepare_mask_detection_images
    calls: list[Image.Image] = []

    def tracked_tool(source: Image.Image):
        calls.append(source)
        return original_tool(source)

    monkeypatch.setattr(module, "prepare_mask_detection_images", tracked_tool)
    module.refine_candidate_boundary(image, candidate)

    assert calls == [image]
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "def _stretch_luminance" not in source
    assert "def _local_limited" not in source


def test_refinement_is_deterministic() -> None:
    module = load_module()
    image = rectangle_scene()
    candidate = binary_rectangle(image.size, (50, 40, 130, 120))

    first_alpha, first_report = module.refine_candidate_boundary(image, candidate)
    second_alpha, second_report = module.refine_candidate_boundary(image, candidate)

    assert first_alpha.tobytes() == second_alpha.tobytes()
    assert asdict(first_report) == asdict(second_report)


def test_assets_reject_path_aliases_before_writing(tmp_path: Path) -> None:
    module = load_module()
    source = tmp_path / "source.png"
    digest = write_source(source, rectangle_scene())
    profile = tmp_path / "profile.json"
    write_profile(profile, source, digest, (180, 160))

    with pytest.raises(ValueError, match="路径"):
        module.create_background_edit_assets_legacy_read_only(
            source,
            source,
            tmp_path / "mask.png",
            tmp_path / "overlay.png",
            tmp_path / "report.json",
            profile,
            "SYTEST",
        )

    assert sha256_file(source) == digest
    assert not (tmp_path / "mask.png").exists()


def test_image_pixel_limit_has_an_inclusive_20mp_boundary() -> None:
    module = load_module()

    module._validate_image_size((5000, 4000))
    with pytest.raises(ValueError, match="20"):
        module._validate_image_size((5000, 4001))


def test_cli_rejects_oversized_source_before_writing(tmp_path: Path) -> None:
    source = tmp_path / "oversized.bmp"
    profile = tmp_path / "profile.json"
    output = tmp_path / "mask.png"
    write_oversized_bmp_header(source, 5000, 4001)
    profile.write_text("{}", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT_SCRIPT_PATH),
            "--input",
            str(source),
            "--output",
            str(output),
            "--geometry-profile",
            str(profile),
            "--product-id",
            "SYTEST",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "20" in result.stderr
    assert not output.exists()


def test_formal_module_has_no_ring_inference_or_annulus_fallback() -> None:
    module = load_module()
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert not hasattr(module, "estimate_foreground")
    assert not hasattr(module, "create_annulus_mask")
    assert "_infer_ring_geometry" not in source
    assert "_ring_band" not in source
    assert "outer-rx" not in source
    assert "inner-rx" not in source


def write_cropped_contract(tmp_path: Path, module):
    run_root = tmp_path / "SYTEST" / "run-1"
    cropped = run_root / "cropped"
    geometry_dir = run_root / "geometry"
    manifests = run_root / "manifests"
    cropped.mkdir(parents=True)
    geometry_dir.mkdir()
    manifests.mkdir()
    original = cropped / "SYTEST_original.png"
    detection = cropped / "SYTEST_detection.png"
    local_detection = cropped / "SYTEST_local_detection.png"
    candidate = cropped / "SYTEST_geometry_candidate_alpha.png"
    Image.new("RGB", (20, 16), (230, 232, 234)).save(original, "PNG")
    Image.new("L", (20, 16), 128).save(detection, "PNG")
    Image.new("L", (20, 16), 140).save(local_detection, "PNG")
    alpha = Image.new("L", (20, 16), 0)
    ImageDraw.Draw(alpha).rectangle((5, 4, 14, 11), fill=255)
    alpha.save(candidate, "PNG")
    cropped_geometry_path = geometry_dir / "SYTEST_cropped_geometry.json"
    cropped_geometry = {
        "schema_version": "vision-cropped-geometry-1.0",
        "product_id": "SYTEST",
        "source_geometry_sha256": "A" * 64,
        "source_sha256": "B" * 64,
        "detection_image_sha256": "C" * 64,
        "source_size": [40, 32],
        "crop_box": [10, 8, 30, 24],
        "crop_size": [20, 16],
        "coordinate_space": "crop-pixel",
        "coordinate_bounds": [0, 0, 20, 16],
        "primitives": [
            {
                "id": "product",
                "type": "polygon",
                "semantic": "product",
                "points": [[5, 4], [14, 4], [14, 11], [5, 11]],
                "touches_border": False,
            }
        ],
        "uncertain_regions": [],
    }
    encoded = json.dumps(
        cropped_geometry,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    cropped_geometry["cropped_geometry_sha256"] = hashlib.sha256(encoded).hexdigest().upper()
    cropped_geometry_path.write_text(
        json.dumps(cropped_geometry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    crop_manifest_path = manifests / "SYTEST_crop_manifest.json"

    def record(path: Path, mode: str):
        return {
            "path": path.relative_to(run_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
            "size": [20, 16],
            "mode": mode,
        }

    manifest = {
        "schema_version": "geometry-crop-manifest-1.0",
        "product_id": "SYTEST",
        "source_geometry_sha256": "A" * 64,
        "source_sha256": "B" * 64,
        "detection_image_sha256": "C" * 64,
        "local_detection_image_sha256": "D" * 64,
        "detection_manifest_sha256": "E" * 64,
        "source_size": [40, 32],
        "content_box": [15, 12, 25, 20],
        "crop_box": [10, 8, 30, 24],
        "crop_size": [20, 16],
        "target_max_occupancy": [77, 100],
        "actual_occupancy": {"width": 0.5, "height": 0.5},
        "source_limited_axes": [],
        "verified_source_border_primitive_ids": [],
        "outputs": {
            "cropped_original": record(original, "RGB"),
            "cropped_detection": record(detection, "L"),
            "cropped_local_detection": record(local_detection, "L"),
            "candidate_alpha": record(candidate, "L"),
        },
        "cropped_geometry": {
            "path": cropped_geometry_path.relative_to(run_root).as_posix(),
            "sha256": hashlib.sha256(cropped_geometry_path.read_bytes()).hexdigest().upper(),
            "semantic_sha256": cropped_geometry["cropped_geometry_sha256"],
        },
        "crop_algorithm": "geometry-crop-77-over-100-v1",
    }
    crop_manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    outputs = module.MaskOutputPaths(
        run_root=run_root,
        mask_path=run_root / "mask" / "SYTEST_product-protection-mask.png",
        overlay_path=run_root / "mask" / "SYTEST_editable-overlay.png",
        report_path=run_root / "logs" / "SYTEST_mask-assessment.draft.json",
    )
    return (
        original,
        detection,
        local_detection,
        candidate,
        cropped_geometry_path,
        crop_manifest_path,
        outputs,
    )


def test_new_mask_entry_accepts_only_cropped_contract_inputs() -> None:
    module = load_module()

    assert list(inspect.signature(module.create_background_edit_assets).parameters) == [
        "cropped_original_path",
        "cropped_detection_path",
        "cropped_local_detection_path",
        "candidate_alpha_path",
        "cropped_geometry_path",
        "crop_manifest_path",
        "outputs",
    ]


def test_new_mask_entry_rejects_tampered_crop_manifest_before_writing(
    tmp_path: Path,
) -> None:
    module = load_module()
    inputs = list(write_cropped_contract(tmp_path, module))
    crop_manifest_path = inputs[5]
    manifest = json.loads(crop_manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"]["candidate_alpha"]["sha256"] = "F" * 64
    crop_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Crop Manifest|candidate"):
        module.create_background_edit_assets(*inputs)

    outputs = inputs[-1]
    assert not outputs.mask_path.exists()
    assert not outputs.overlay_path.exists()
    assert not outputs.report_path.exists()
