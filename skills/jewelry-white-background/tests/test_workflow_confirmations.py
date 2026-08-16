from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image


SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
CONFIRMATIONS_SCRIPT = SCRIPTS_DIR / "workflow_confirmations.py"
STATE_SCRIPT = SCRIPTS_DIR / "workflow_state.py"


def load_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fixed_now() -> datetime:
    return datetime(2026, 8, 16, 9, 30, tzinfo=timezone.utc)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def advance_to_mask_ready(state, paths) -> None:
    status = "run_created"
    revision = 1
    for next_status in (
        "source_ready",
        "detection_image_ready",
        "geometry_ready",
        "crop_ready",
        "mask_ready",
    ):
        state.transition_state(
            paths,
            expected_status=status,
            expected_revision=revision,
            next_status=next_status,
            event=next_status,
            now=fixed_now,
        )
        status = next_status
        revision += 1


def write_review_case(tmp_path: Path):
    state = load_path("workflow_state_for_confirmations", STATE_SCRIPT)
    module = load_path("workflow_confirmations_under_test", CONFIRMATIONS_SCRIPT)
    identities = state.WorkflowIdentity(
        product_id="SY1537",
        base_token="base-token",
        table_id="table-id",
        record_id="record-id",
        front_field_id="front-field-id",
        target_field_id="target-field-id",
    )
    uuids = iter(
        [
            uuid.UUID("11111111-1111-1111-1111-111111111111"),
            uuid.UUID("22222222-2222-2222-2222-222222222222"),
        ]
    )
    paths = state.create_run(
        tmp_path,
        identities,
        now=fixed_now,
        uuid_factory=lambda: next(uuids),
    )
    advance_to_mask_ready(state, paths)

    def binary(name: str, data: bytes) -> Path:
        path = paths.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def image(name: str, mode: str) -> Path:
        path = paths.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new(mode, (20, 16), 255 if mode == "L" else (230, 232, 234)).save(
            path, "PNG"
        )
        return path

    raw_source = binary("source/SY1537_raw.bin", b"raw-source")
    source_original = image("source/SY1537_original.png", "RGB")
    geometry_detection = image("detection/SY1537_geometry_detection.png", "L")
    local_detection = image("detection/SY1537_local_detection.png", "L")
    cropped_original = image("cropped/SY1537_original.png", "RGB")
    cropped_detection = image("cropped/SY1537_detection.png", "L")
    cropped_local = image("cropped/SY1537_local_detection.png", "L")
    candidate_alpha = image("cropped/SY1537_geometry_candidate_alpha.png", "L")
    mask = image("mask/SY1537_product-protection-mask.png", "L")
    overlay = image("mask/SY1537_editable-overlay.png", "RGB")
    source_geometry = binary(
        "geometry/SY1537_geometry.json",
        json.dumps(
            {
                "schema_version": "vision-geometry-mask-2.1",
                "uncertain_regions": [
                    {"id": "cord", "bbox": [1, 1, 2, 2], "reason": "pale"}
                ],
            }
        ).encode(),
    )
    cropped_geometry = binary(
        "geometry/SY1537_cropped_geometry.json",
        json.dumps(
            {
                "schema_version": "vision-cropped-geometry-1.0",
                "uncertain_regions": [
                    {"id": "cord", "bbox": [1, 1, 2, 2], "reason": "pale"}
                ],
            }
        ).encode(),
    )
    crop_manifest = binary("manifests/SY1537_crop_manifest.json", b'{"ok":true}')
    draft_assessment = binary(
        "logs/SY1537_mask-assessment.draft.json",
        json.dumps(
            {
                "schema_version": "mask-assessment-draft-1.0",
                "status": "review",
                "technical_blockers": [],
                "unresolved_uncertain_region_ids": ["cord"],
            }
        ).encode(),
    )
    product_context = paths.root / "product_context.json"
    product_context.write_text(
        json.dumps(
            {
                "run_id": paths.run_id,
                "product_id": "SY1537",
                "base_token": "base-token",
                "table_id": "table-id",
                "record_id": "record-id",
                "front_field_id": "front-field-id",
                "target_field_id": "target-field-id",
                "front_file_token": "front-token",
                "source_identity": {
                    "raw_source_path": raw_source.relative_to(paths.root).as_posix()
                },
            }
        ),
        encoding="utf-8",
    )
    assets = module.MaskReviewAssets(
        product_context_path=product_context,
        raw_source_path=raw_source,
        source_original_path=source_original,
        geometry_detection_path=geometry_detection,
        local_detection_path=local_detection,
        source_geometry_path=source_geometry,
        crop_manifest_path=crop_manifest,
        cropped_original_path=cropped_original,
        cropped_detection_path=cropped_detection,
        cropped_local_detection_path=cropped_local,
        cropped_geometry_path=cropped_geometry,
        candidate_alpha_path=candidate_alpha,
        mask_path=mask,
        overlay_path=overlay,
        draft_assessment_path=draft_assessment,
    )
    return state, module, paths, assets


def test_mask_review_bundle_binds_every_asset_and_advances_to_waiting(
    tmp_path: Path,
) -> None:
    state, module, paths, assets = write_review_case(tmp_path)

    bundle = module.create_mask_review_bundle(paths, assets, now=fixed_now)

    assert bundle["schema_version"] == "mask-review-bundle-1.0"
    assert set(bundle["assets"]) == set(module.MaskReviewAssets.__dataclass_fields__)
    assert bundle["assets"]["cropped_original_path"]["size"] == [20, 16]
    assert bundle["uncertain_region_ids"] == ["cord"]
    assert state.load_state(paths)["status"] == "awaiting_mask_confirmation"


def test_mask_confirmation_is_create_only_and_binds_every_review_asset(
    tmp_path: Path,
) -> None:
    state, module, paths, assets = write_review_case(tmp_path)
    bundle = module.create_mask_review_bundle(paths, assets, now=fixed_now)

    receipt = module.confirm_mask_review(
        paths,
        bundle,
        reviewer="session-1",
        resolved_ids=["cord"],
        now=fixed_now,
        uuid_factory=lambda: uuid.UUID("33333333-3333-3333-3333-333333333333"),
    )

    with pytest.raises(FileExistsError):
        module.confirm_mask_review(
            paths,
            bundle,
            reviewer="session-2",
            resolved_ids=["cord"],
            now=fixed_now,
            uuid_factory=lambda: uuid.UUID("44444444-4444-4444-4444-444444444444"),
        )
    assert receipt["confirmation_id"] == "33333333333333333333333333333333"
    assert receipt["decision"] == "confirmed"
    assert receipt["resolved_uncertain_region_ids"] == ["cord"]
    assert receipt["assets"] == bundle["assets"]
    assert "automatic_wawapi_edit_allowed" not in receipt
    assert state.load_state(paths)["status"] == "mask_confirmed"


def test_changed_bundle_asset_invalidates_confirmation(tmp_path: Path) -> None:
    _state, module, paths, assets = write_review_case(tmp_path)
    bundle = module.create_mask_review_bundle(paths, assets, now=fixed_now)
    module.confirm_mask_review(
        paths,
        bundle,
        reviewer="session-1",
        resolved_ids=["cord"],
        now=fixed_now,
        uuid_factory=lambda: uuid.UUID("33333333-3333-3333-3333-333333333333"),
    )
    assets.mask_path.write_bytes(b"changed")

    gate = module.validate_mask_confirmation(paths)

    assert gate.automatic_wawapi_edit_allowed is False
    assert "asset_changed" in gate.blockers


def test_confirmation_requires_exact_uncertain_region_resolution(tmp_path: Path) -> None:
    _state, module, paths, assets = write_review_case(tmp_path)
    bundle = module.create_mask_review_bundle(paths, assets, now=fixed_now)

    with pytest.raises(ValueError, match="疑点"):
        module.confirm_mask_review(
            paths,
            bundle,
            reviewer="session-1",
            resolved_ids=[],
            now=fixed_now,
            uuid_factory=uuid.uuid4,
        )


def test_technical_blocker_prevents_review_bundle(tmp_path: Path) -> None:
    _state, module, paths, assets = write_review_case(tmp_path)
    assets.draft_assessment_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "technical_blockers": ["boundary_refinement_fallback"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="技术门禁"):
        module.create_mask_review_bundle(paths, assets, now=fixed_now)

    assert not (paths.root / "manifests" / "mask_review_bundle.json").exists()
