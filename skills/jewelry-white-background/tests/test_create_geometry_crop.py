from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "create_geometry_crop.py"


def load_module():
    spec = importlib.util.spec_from_file_location("skill_create_geometry_crop", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical_digest(payload: dict[str, object], digest_field: str) -> str:
    digest_payload = {key: value for key, value in payload.items() if key != digest_field}
    encoded = json.dumps(
        digest_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def valid_geometry_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "vision-geometry-mask-2.1",
        "product_id": "SY1537",
        "source_sha256": "A" * 64,
        "source_size": [3024, 4032],
        "detection_image_sha256": "B" * 64,
        "detection_image_size": [3024, 4032],
        "detection_manifest_sha256": "C" * 64,
        "coordinate_transform": "identity",
        "coordinate_range": [0, 10000],
        "producer": "codex-cloud-vision",
        "primitives": [
            {
                "id": "product-envelope",
                "type": "ellipse",
                "semantic": "product",
                "params": [5000, 5000, 2000, 2500],
                "touches_border": False,
            }
        ],
        "uncertain_regions": [],
    }
    payload["geometry_sha256"] = canonical_digest(payload, "geometry_sha256")
    return payload


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def expected_identity(module):
    return module.GeometryExpectedIdentity(
        product_id="SY1537",
        source_sha256="A" * 64,
        source_size=(3024, 4032),
        detection_image_sha256="B" * 64,
        detection_image_size=(3024, 4032),
        detection_manifest_sha256="C" * 64,
    )


def test_loader_accepts_strict_2_1_identity_and_digest(tmp_path: Path) -> None:
    module = load_module()
    geometry_path = tmp_path / "geometry.json"
    payload = valid_geometry_payload()
    write_json(geometry_path, payload)

    geometry = module.load_vision_geometry_v21(
        geometry_path,
        expected_identity(module),
    )

    assert geometry.schema_version == "vision-geometry-mask-2.1"
    assert geometry.product_id == "SY1537"
    assert geometry.source_size == (3024, 4032)
    assert geometry.geometry_sha256 == payload["geometry_sha256"]


def test_loader_rejects_legacy_2_0_even_when_missing_fields_are_added(
    tmp_path: Path,
) -> None:
    module = load_module()
    geometry_path = tmp_path / "geometry.json"
    payload = valid_geometry_payload()
    payload["schema_version"] = "vision-geometry-mask-2.0"
    payload["geometry_sha256"] = canonical_digest(payload, "geometry_sha256")
    write_json(geometry_path, payload)

    with pytest.raises(ValueError, match="2.1"):
        module.load_vision_geometry_v21(geometry_path, expected_identity(module))


def test_loader_rejects_identity_mismatch_and_digest_tampering(tmp_path: Path) -> None:
    module = load_module()
    geometry_path = tmp_path / "geometry.json"
    payload = valid_geometry_payload()
    write_json(geometry_path, payload)

    with pytest.raises(ValueError, match="身份"):
        module.load_vision_geometry_v21(
            geometry_path,
            replace(expected_identity(module), source_sha256="D" * 64),
        )

    payload["primitives"][0]["semantic"] = "tampered"
    write_json(geometry_path, payload)
    with pytest.raises(ValueError, match="geometry_sha256"):
        module.load_vision_geometry_v21(geometry_path, expected_identity(module))


@pytest.mark.parametrize(
    "primitives",
    [
        [
            {
                "id": "flat-polygon",
                "type": "polygon",
                "semantic": "product",
                "points": [[1000, 1000], [2000, 2000], [3000, 3000]],
            }
        ],
        [
            {
                "id": "zero-width-line",
                "type": "polyline",
                "semantic": "cord",
                "points": [[1000, 1000], [2000, 2000]],
                "width": 0,
            }
        ],
        [
            {
                "id": "bad-ellipse",
                "type": "ellipse",
                "semantic": "bead",
                "params": [5000, 5000, 0, 1000],
            }
        ],
        [
            {
                "id": "duplicate",
                "type": "ellipse_set",
                "semantic": "beads",
                "items": [
                    {"id": "duplicate", "params": [3000, 5000, 1000, 1000]}
                ],
            }
        ],
        [
            {
                "id": "bad-touch",
                "type": "ellipse",
                "semantic": "bead",
                "params": [5000, 5000, 1000, 1000],
                "touches_border": "false",
            }
        ],
    ],
)
def test_loader_rejects_degenerate_or_ambiguous_primitives(
    tmp_path: Path,
    primitives: list[dict[str, object]],
) -> None:
    module = load_module()
    geometry_path = tmp_path / "geometry.json"
    payload = valid_geometry_payload()
    payload["primitives"] = primitives
    payload["geometry_sha256"] = canonical_digest(payload, "geometry_sha256")
    write_json(geometry_path, payload)

    with pytest.raises(ValueError, match="primitive|图元|touches_border"):
        module.load_vision_geometry_v21(geometry_path, expected_identity(module))


@pytest.mark.parametrize(
    "regions",
    [
        [{"id": "u1", "bbox": [100, 100, 100, 200], "reason": "zero width"}],
        [{"id": "u1", "bbox": [100, 100, 200, 200], "reason": ""}],
        [
            {"id": "u1", "bbox": [100, 100, 200, 200], "reason": "first"},
            {"id": "u1", "bbox": [300, 300, 400, 400], "reason": "duplicate"},
        ],
    ],
)
def test_loader_rejects_invalid_uncertain_regions(
    tmp_path: Path,
    regions: list[dict[str, object]],
) -> None:
    module = load_module()
    geometry_path = tmp_path / "geometry.json"
    payload = valid_geometry_payload()
    payload["uncertain_regions"] = regions
    payload["geometry_sha256"] = canonical_digest(payload, "geometry_sha256")
    write_json(geometry_path, payload)

    with pytest.raises(ValueError, match="uncertain_regions"):
        module.load_vision_geometry_v21(geometry_path, expected_identity(module))


def test_compute_crop_box_uses_ceil_and_places_odd_extra_pixel_right_bottom() -> None:
    module = load_module()

    crop_box = module.compute_crop_box((1000, 800), (100, 100, 331, 300))

    assert crop_box == (66, 70, 366, 330)


def test_compute_crop_box_clamps_source_limited_axis_without_square_padding() -> None:
    module = load_module()

    crop_box = module.compute_crop_box((100, 300), (5, 100, 95, 150))

    assert crop_box == (0, 93, 100, 158)
    assert crop_box[2] - crop_box[0] != crop_box[3] - crop_box[1]


def test_example_ellipse_quantizes_before_crop_translation() -> None:
    module = load_module()

    result = module.quantize_ellipse(
        [5000, 5000, 2000, 2500],
        (3024, 4032),
        (400, 600, 2500, 3400),
    )

    assert result == [1112, 1416, 605, 1008]


def test_rasterize_source_geometry_and_uncertainty_define_content_box(
    tmp_path: Path,
) -> None:
    module = load_module()
    payload = valid_geometry_payload()
    payload["source_size"] = [101, 81]
    payload["detection_image_size"] = [101, 81]
    payload["primitives"] = [
        {
            "id": "body",
            "type": "polygon",
            "semantic": "product",
            "points": [[2000, 2500], [6000, 2500], [6000, 7500], [2000, 7500]],
        }
    ]
    payload["uncertain_regions"] = [
        {"id": "u1", "bbox": [8000, 1000, 9000, 2000], "reason": "pale edge"}
    ]
    payload["geometry_sha256"] = canonical_digest(payload, "geometry_sha256")
    geometry_path = tmp_path / "geometry.json"
    write_json(geometry_path, payload)
    identity = module.GeometryExpectedIdentity(
        product_id="SY1537",
        source_sha256="A" * 64,
        source_size=(101, 81),
        detection_image_sha256="B" * 64,
        detection_image_size=(101, 81),
        detection_manifest_sha256="C" * 64,
    )
    geometry = module.load_vision_geometry_v21(geometry_path, identity)

    alpha = module.rasterize_source_geometry(geometry)
    content_box = module.compute_content_box(alpha, geometry.uncertain_regions)

    assert alpha.mode == "L"
    assert set(alpha.getdata()) <= {0, 255}
    assert alpha.getpixel((40, 40)) == 255
    assert alpha.getpixel((5, 5)) == 0
    assert content_box == (20, 8, 91, 61)


def write_crop_case(tmp_path: Path):
    module = load_module()
    run_root = tmp_path / "SY1537" / "run-1"
    source_path = run_root / "source" / "SY1537_original.png"
    global_path = run_root / "detection" / "SY1537_geometry_detection.png"
    local_path = run_root / "detection" / "SY1537_local_detection.png"
    detection_manifest_path = run_root / "manifests" / "SY1537_detection.json"
    geometry_path = run_root / "geometry" / "SY1537_geometry.json"
    for path in (source_path, global_path, local_path, detection_manifest_path, geometry_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    source = Image.new("RGB", (101, 81))
    source.putdata(
        [
            ((x * 3) % 256, (y * 5) % 256, (x + y) % 256)
            for y in range(81)
            for x in range(101)
        ]
    )
    source.save(source_path, "PNG")
    global_detection = Image.new("L", source.size)
    global_detection.putdata(
        [(x * 7 + y * 11) % 256 for y in range(81) for x in range(101)]
    )
    global_detection.save(global_path, "PNG")
    local_detection = Image.new("L", source.size)
    local_detection.putdata(
        [(x * 13 + y * 17) % 256 for y in range(81) for x in range(101)]
    )
    local_detection.save(local_path, "PNG")

    detection_manifest: dict[str, object] = {
        "schema_version": "mask-detection-images-2.0",
        "detection_only": True,
        "source_sha256": sha256_file(source_path),
        "source_size": [101, 81],
        "coordinate_transform": "identity",
        "outputs": {
            "global_robust": {
                "path": global_path.relative_to(run_root).as_posix(),
                "sha256": sha256_file(global_path),
                "size": [101, 81],
                "mode": "L",
            },
            "local_limited": {
                "path": local_path.relative_to(run_root).as_posix(),
                "sha256": sha256_file(local_path),
                "size": [101, 81],
                "mode": "L",
            },
        },
        "algorithms": {"fixture": True},
        "authoritative_implementation": (
            "skills/jewelry-white-background/scripts/prepare_mask_detection_images.py"
        ),
    }
    write_json(detection_manifest_path, detection_manifest)

    geometry = valid_geometry_payload()
    geometry["source_sha256"] = sha256_file(source_path)
    geometry["source_size"] = [101, 81]
    geometry["detection_image_sha256"] = sha256_file(global_path)
    geometry["detection_image_size"] = [101, 81]
    geometry["detection_manifest_sha256"] = sha256_file(detection_manifest_path)
    geometry["primitives"] = [
        {
            "id": "body",
            "type": "polygon",
            "semantic": "product",
            "points": [[2000, 2500], [6000, 2500], [6000, 7500], [2000, 7500]],
            "touches_border": False,
        },
        {
            "id": "cord",
            "type": "polyline",
            "semantic": "cord",
            "points": [[6000, 5000], [8500, 1500]],
            "width": 250,
            "touches_border": False,
        },
    ]
    geometry["uncertain_regions"] = [
        {"id": "u1", "bbox": [8000, 1000, 9000, 2000], "reason": "pale edge"}
    ]
    geometry["geometry_sha256"] = canonical_digest(geometry, "geometry_sha256")
    write_json(geometry_path, geometry)

    outputs = module.CropOutputPaths(
        run_root=run_root,
        product_id="SY1537",
        cropped_original_path=run_root / "cropped" / "SY1537_original.png",
        cropped_detection_path=run_root / "cropped" / "SY1537_detection.png",
        cropped_local_detection_path=(
            run_root / "cropped" / "SY1537_local_detection.png"
        ),
        candidate_alpha_path=(
            run_root / "cropped" / "SY1537_geometry_candidate_alpha.png"
        ),
        cropped_geometry_path=(
            run_root / "geometry" / "SY1537_cropped_geometry.json"
        ),
        crop_manifest_path=run_root / "manifests" / "SY1537_crop_manifest.json",
    )
    return (
        module,
        source_path,
        global_path,
        local_path,
        detection_manifest_path,
        geometry_path,
        outputs,
    )


def test_create_geometry_crop_assets_synchronously_crops_four_assets_and_audits(
    tmp_path: Path,
) -> None:
    (
        module,
        source_path,
        global_path,
        local_path,
        detection_manifest_path,
        geometry_path,
        outputs,
    ) = write_crop_case(tmp_path)

    manifest = module.create_geometry_crop_assets(
        source_path,
        global_path,
        local_path,
        detection_manifest_path,
        geometry_path,
        outputs,
    )

    crop_box = tuple(manifest["crop_box"])
    with Image.open(source_path) as source, Image.open(outputs.cropped_original_path) as cropped:
        assert cropped.tobytes() == source.crop(crop_box).tobytes()
    with Image.open(global_path) as source, Image.open(outputs.cropped_detection_path) as cropped:
        assert cropped.tobytes() == source.crop(crop_box).tobytes()
    with Image.open(local_path) as source, Image.open(
        outputs.cropped_local_detection_path
    ) as cropped:
        assert cropped.tobytes() == source.crop(crop_box).tobytes()

    geometry = module.load_vision_geometry_v21(
        geometry_path,
        module.GeometryExpectedIdentity(
            product_id="SY1537",
            source_sha256=sha256_file(source_path),
            source_size=(101, 81),
            detection_image_sha256=sha256_file(global_path),
            detection_image_size=(101, 81),
            detection_manifest_sha256=sha256_file(detection_manifest_path),
        ),
    )
    full_alpha = module.rasterize_source_geometry(geometry)
    with Image.open(outputs.candidate_alpha_path) as candidate:
        candidate.load()
        assert candidate.mode == "L"
        assert candidate.tobytes() == full_alpha.crop(crop_box).tobytes()

    cropped_geometry = json.loads(outputs.cropped_geometry_path.read_text("utf-8"))
    assert cropped_geometry["schema_version"] == "vision-cropped-geometry-1.0"
    assert cropped_geometry["crop_box"] == list(crop_box)
    assert cropped_geometry["cropped_geometry_sha256"] == canonical_digest(
        cropped_geometry, "cropped_geometry_sha256"
    )
    audited_alpha = module.rasterize_cropped_geometry(cropped_geometry)
    with Image.open(outputs.candidate_alpha_path) as candidate:
        assert audited_alpha.tobytes() == candidate.tobytes()

    assert manifest["schema_version"] == "geometry-crop-manifest-1.0"
    assert manifest["target_max_occupancy"] == [77, 100]
    assert manifest["crop_algorithm"] == "geometry-crop-77-over-100-v1"
    assert manifest["outputs"]["cropped_original"]["path"] == (
        "cropped/SY1537_original.png"
    )
    assert manifest["outputs"]["candidate_alpha"]["mode"] == "L"
    assert manifest["cropped_geometry"]["sha256"] == sha256_file(
        outputs.cropped_geometry_path
    )


def test_create_geometry_crop_assets_rejects_manifest_or_existing_output_before_write(
    tmp_path: Path,
) -> None:
    (
        module,
        source_path,
        global_path,
        local_path,
        detection_manifest_path,
        geometry_path,
        outputs,
    ) = write_crop_case(tmp_path)
    outputs.cropped_original_path.parent.mkdir(parents=True)
    outputs.cropped_original_path.write_bytes(b"sentinel")

    with pytest.raises(FileExistsError):
        module.create_geometry_crop_assets(
            source_path,
            global_path,
            local_path,
            detection_manifest_path,
            geometry_path,
            outputs,
        )

    assert outputs.cropped_original_path.read_bytes() == b"sentinel"
    assert not outputs.candidate_alpha_path.exists()

    outputs.cropped_original_path.unlink()
    manifest = json.loads(detection_manifest_path.read_text("utf-8"))
    manifest["outputs"]["local_limited"]["sha256"] = "D" * 64
    write_json(detection_manifest_path, manifest)
    geometry = json.loads(geometry_path.read_text("utf-8"))
    geometry["detection_manifest_sha256"] = sha256_file(detection_manifest_path)
    geometry["geometry_sha256"] = canonical_digest(geometry, "geometry_sha256")
    write_json(geometry_path, geometry)

    with pytest.raises(ValueError, match="local|检测"):
        module.create_geometry_crop_assets(
            source_path,
            global_path,
            local_path,
            detection_manifest_path,
            geometry_path,
            outputs,
        )
    assert not outputs.candidate_alpha_path.exists()


def test_declared_border_primitive_must_really_touch_full_source(
    tmp_path: Path,
) -> None:
    (
        module,
        source_path,
        global_path,
        local_path,
        detection_manifest_path,
        geometry_path,
        outputs,
    ) = write_crop_case(tmp_path)
    geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
    geometry["primitives"][0]["touches_border"] = True
    geometry["geometry_sha256"] = canonical_digest(geometry, "geometry_sha256")
    write_json(geometry_path, geometry)

    with pytest.raises(ValueError, match="touches_border"):
        module.create_geometry_crop_assets(
            source_path,
            global_path,
            local_path,
            detection_manifest_path,
            geometry_path,
            outputs,
        )

    assert not outputs.candidate_alpha_path.exists()
