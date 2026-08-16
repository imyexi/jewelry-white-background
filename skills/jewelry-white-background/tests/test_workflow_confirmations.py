from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

import pytest
from PIL import Image, ImageDraw


SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
CONFIRMATIONS_SCRIPT = SCRIPTS_DIR / "workflow_confirmations.py"
STATE_SCRIPT = SCRIPTS_DIR / "workflow_state.py"
MASK_SCRIPT = SCRIPTS_DIR / "create_background_edit_mask.py"
ROOT_MASK_WRAPPER = Path(__file__).parents[3] / "scripts" / "create_background_edit_mask.py"


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


def canonical_digest(payload: dict[str, object], digest_field: str) -> str:
    encoded = json.dumps(
        {key: value for key, value in payload.items() if key != digest_field},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def write_final_review_case(tmp_path: Path):
    state = load_path("workflow_state_for_final_confirmation", STATE_SCRIPT)
    module = load_path("workflow_confirmations_for_final_confirmation", CONFIRMATIONS_SCRIPT)
    identity = state.WorkflowIdentity(
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
    paths = state.create_run(tmp_path, identity, now=fixed_now, uuid_factory=lambda: next(uuids))
    payload = state.load_state(paths)
    payload["status"] = "layout_completed"
    payload["state_revision"] = 20
    state.atomic_replace_json(paths.state_path, payload)

    def artifact(relative: str, content: bytes) -> Path:
        path = paths.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    layout = paths.root / "layout/SY1537_3x4_60pct.png"
    layout.parent.mkdir(parents=True)
    Image.new("RGB", (1536, 2048), (240, 240, 240)).save(layout, "PNG")
    assets = module.FinalReviewAssets(
        mask_confirmation_path=artifact("confirmations/mask_confirmation.json", b"{}\n"),
        mask_gate_path=artifact("logs/SY1537_mask-gate.confirmed.json", b"{}\n"),
        edit_result_manifest_path=artifact("manifests/SY1537_edit_result.json", b"{}\n"),
        layout_path=layout,
        layout_manifest_path=artifact("manifests/SY1537_layout_manifest.json", b"{}\n"),
    )
    return state, module, paths, assets


def test_final_confirmation_binds_layout_and_authorizes_side_effects(tmp_path: Path) -> None:
    state, module, paths, assets = write_final_review_case(tmp_path)
    module.create_final_review_bundle(paths, assets, now=fixed_now)
    ids = iter(
        [
            uuid.UUID("33333333-3333-3333-3333-333333333333"),
            uuid.UUID("44444444-4444-4444-4444-444444444444"),
        ]
    )

    receipt = module.confirm_final_review(
        paths, reviewer="session-1", now=fixed_now, uuid_factory=lambda: next(ids)
    )

    assert receipt["authorization"] == {"watermark": True, "feishu_append": True}
    assert receipt["delivery_id"] == "44444444444444444444444444444444"
    assert receipt["layout"]["size"] == [1536, 2048]
    current = state.load_state(paths)
    assert current["status"] == "final_confirmed"
    assert current["delivery"]["delivery_id"] == receipt["delivery_id"]
    with pytest.raises(FileExistsError):
        module.confirm_final_review(
            paths,
            reviewer="session-2",
            now=fixed_now,
            uuid_factory=lambda: uuid.uuid4(),
        )


def test_final_confirmation_invalidates_when_layout_changes(tmp_path: Path) -> None:
    _state, module, paths, assets = write_final_review_case(tmp_path)
    module.create_final_review_bundle(paths, assets, now=fixed_now)
    ids = iter([uuid.uuid4(), uuid.uuid4()])
    module.confirm_final_review(
        paths, reviewer="session-1", now=fixed_now, uuid_factory=lambda: next(ids)
    )
    assets.layout_path.write_bytes(b"changed")

    gate = module.validate_final_confirmation(paths)

    assert gate.delivery_allowed is False
    assert "asset_changed" in gate.blockers


def test_final_confirmation_receipt_recovers_without_new_delivery_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, module, paths, assets = write_final_review_case(tmp_path)
    module.create_final_review_bundle(paths, assets, now=fixed_now)
    ids = iter(
        [
            uuid.UUID("33333333-3333-3333-3333-333333333333"),
            uuid.UUID("44444444-4444-4444-4444-444444444444"),
        ]
    )
    original = module.transition_state
    monkeypatch.setattr(
        module,
        "transition_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        module.confirm_final_review(
            paths, reviewer="session-1", now=fixed_now, uuid_factory=lambda: next(ids)
        )
    monkeypatch.setattr(module, "transition_state", original)

    gate = module.validate_final_confirmation(paths, recover_state=True, now=fixed_now)

    assert gate.delivery_allowed is True
    assert state.load_state(paths)["delivery"]["delivery_id"] == (
        "44444444444444444444444444444444"
    )


def test_final_review_bundle_recovers_after_publish_before_state_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, module, paths, assets = write_final_review_case(tmp_path)
    original = module.transition_state
    monkeypatch.setattr(
        module,
        "transition_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        module.create_final_review_bundle(paths, assets, now=fixed_now)
    monkeypatch.setattr(module, "transition_state", original)

    recovered = module.create_final_review_bundle(paths, assets, now=fixed_now)

    assert recovered["schema_version"] == "final-review-bundle-1.0"
    assert state.load_state(paths)["status"] == "awaiting_final_confirmation"


def test_concurrent_final_bundle_recovery_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, module, paths, assets = write_final_review_case(tmp_path)
    original = module.transition_state
    monkeypatch.setattr(
        module,
        "transition_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        module.create_final_review_bundle(paths, assets, now=fixed_now)

    barrier = Barrier(2)

    def synchronized_transition(*args, **kwargs):
        if kwargs.get("next_status") == "awaiting_final_confirmation":
            barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "transition_state", synchronized_transition)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: module.create_final_review_bundle(
                    paths, assets, now=fixed_now
                ),
                range(2),
            )
        )

    assert [item["schema_version"] for item in results] == [
        "final-review-bundle-1.0",
        "final-review-bundle-1.0",
    ]
    assert state.load_state(paths)["status"] == "awaiting_final_confirmation"


def test_concurrent_valid_final_confirmation_recovery_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, module, paths, assets = write_final_review_case(tmp_path)
    module.create_final_review_bundle(paths, assets, now=fixed_now)
    ids = iter(
        [
            uuid.UUID("33333333-3333-3333-3333-333333333333"),
            uuid.UUID("44444444-4444-4444-4444-444444444444"),
        ]
    )
    original = module.transition_state
    monkeypatch.setattr(
        module,
        "transition_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        module.confirm_final_review(
            paths, reviewer="session-1", now=fixed_now, uuid_factory=lambda: next(ids)
        )

    barrier = Barrier(2)

    def synchronized_transition(*args, **kwargs):
        if kwargs.get("next_status") == "final_confirmed":
            barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "transition_state", synchronized_transition)
    with ThreadPoolExecutor(max_workers=2) as executor:
        gates = list(
            executor.map(
                lambda _index: module.validate_final_confirmation(
                    paths, recover_state=True, now=fixed_now
                ),
                range(2),
            )
        )

    assert [gate.delivery_allowed for gate in gates] == [True, True]
    assert state.load_state(paths)["status"] == "final_confirmed"


def test_concurrent_invalid_final_confirmation_recovery_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, module, paths, assets = write_final_review_case(tmp_path)
    module.create_final_review_bundle(paths, assets, now=fixed_now)
    receipt_path = paths.root / "confirmations" / "final_confirmation.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text('{"schema_version":"broken"}', encoding="utf-8")
    original = module.transition_state
    barrier = Barrier(2)

    def synchronized_transition(*args, **kwargs):
        if kwargs.get("next_status") == "final_invalid":
            barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "transition_state", synchronized_transition)
    with ThreadPoolExecutor(max_workers=2) as executor:
        gates = list(
            executor.map(
                lambda _index: module.validate_final_confirmation(
                    paths, recover_state=True, now=fixed_now
                ),
                range(2),
            )
        )

    assert [gate.delivery_allowed for gate in gates] == [False, False]
    assert state.load_state(paths)["status"] == "final_invalid"


def test_invalid_existing_final_receipt_enters_final_invalid(tmp_path: Path) -> None:
    state, module, paths, assets = write_final_review_case(tmp_path)
    module.create_final_review_bundle(paths, assets, now=fixed_now)
    receipt_path = paths.root / "confirmations" / "final_confirmation.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text('{"schema_version":"broken"}', encoding="utf-8")

    gate = module.validate_final_confirmation(paths, recover_state=True, now=fixed_now)

    assert gate.delivery_allowed is False
    assert state.load_state(paths)["status"] == "final_invalid"


def test_confirmation_module_ignores_preloaded_root_mask_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = load_path("root_mask_wrapper_collision_fixture", ROOT_MASK_WRAPPER)
    monkeypatch.setitem(sys.modules, "create_background_edit_mask", wrapper)

    module = load_path("workflow_confirmations_import_collision", CONFIRMATIONS_SCRIPT)

    assert module.validate_published_mask_technical_gate.__module__ == (
        "_jewelry_workflow_mask_contract"
    )


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
    with Image.open(cropped_original) as opened:
        prepared_original = opened.copy()
    ImageDraw.Draw(prepared_original).rectangle((5, 4, 14, 11), fill=(90, 90, 90))
    prepared_original.save(cropped_original, "PNG")
    for path, value in ((cropped_detection, 50), (cropped_local, 60)):
        with Image.open(path) as opened:
            prepared_detection = opened.copy()
        ImageDraw.Draw(prepared_detection).rectangle((5, 4, 14, 11), fill=value)
        prepared_detection.save(path, "PNG")
    candidate = Image.new("L", (20, 16), 0)
    ImageDraw.Draw(candidate).rectangle((5, 4, 14, 11), fill=255)
    candidate.save(candidate_alpha, "PNG")
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
    cropped_geometry_payload: dict[str, object] = {
        "schema_version": "vision-cropped-geometry-1.0",
        "product_id": "SY1537",
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
        "uncertain_regions": [
            {"id": "cord", "bbox": [5, 4, 15, 12], "reason": "pale"}
        ],
    }
    cropped_geometry_payload["cropped_geometry_sha256"] = canonical_digest(
        cropped_geometry_payload, "cropped_geometry_sha256"
    )
    cropped_geometry = binary(
        "geometry/SY1537_cropped_geometry.json",
        (json.dumps(cropped_geometry_payload, ensure_ascii=False, indent=2) + "\n").encode(),
    )

    def record(path: Path, mode: str) -> dict[str, object]:
        return {
            "path": path.relative_to(paths.root).as_posix(),
            "sha256": sha256(path),
            "size": [20, 16],
            "mode": mode,
        }

    crop_manifest_payload = {
        "schema_version": "geometry-crop-manifest-1.0",
        "product_id": "SY1537",
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
        "source_geometry": {
            "path": source_geometry.relative_to(paths.root).as_posix(),
            "sha256": sha256(source_geometry),
            "semantic_sha256": "A" * 64,
        },
        "outputs": {
            "cropped_original": record(cropped_original, "RGB"),
            "cropped_detection": record(cropped_detection, "L"),
            "cropped_local_detection": record(cropped_local, "L"),
            "candidate_alpha": record(candidate_alpha, "L"),
        },
        "cropped_geometry": {
            "path": cropped_geometry.relative_to(paths.root).as_posix(),
            "sha256": sha256(cropped_geometry),
            "semantic_sha256": cropped_geometry_payload[
                "cropped_geometry_sha256"
            ],
        },
        "crop_algorithm": "geometry-crop-77-over-100-v1",
    }
    crop_manifest = binary(
        "manifests/SY1537_crop_manifest.json",
        (json.dumps(crop_manifest_payload, ensure_ascii=False, indent=2) + "\n").encode(),
    )
    mask_module = load_path("mask_for_confirmation_fixture", MASK_SCRIPT)
    mask = paths.root / "mask" / "SY1537_product-protection-mask.png"
    overlay = paths.root / "mask" / "SY1537_editable-overlay.png"
    draft_assessment = paths.root / "logs" / "SY1537_mask-assessment.draft.json"
    mask_outputs = mask_module.MaskOutputPaths(
        run_root=paths.root,
        mask_path=mask,
        overlay_path=overlay,
        report_path=draft_assessment,
    )
    assessment = mask_module.create_background_edit_assets(
        cropped_original,
        cropped_detection,
        cropped_local,
        candidate_alpha,
        cropped_geometry,
        crop_manifest,
        mask_outputs,
    )
    assert assessment.status == "review"
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


def test_forged_passing_draft_cannot_override_invalid_actual_mask(
    tmp_path: Path,
) -> None:
    _state, module, paths, assets = write_review_case(tmp_path)
    Image.new("RGBA", (20, 16), (255, 255, 255, 255)).save(assets.mask_path, "PNG")
    draft = json.loads(assets.draft_assessment_path.read_text(encoding="utf-8"))
    draft["status"] = "review"
    draft["technical_blockers"] = []
    assets.draft_assessment_path.write_text(json.dumps(draft), encoding="utf-8")

    with pytest.raises(ValueError, match="技术门禁"):
        module.create_mask_review_bundle(paths, assets, now=fixed_now)


def test_changed_review_bundle_file_invalidates_old_review(tmp_path: Path) -> None:
    _state, module, paths, assets = write_review_case(tmp_path)
    module.create_mask_review_bundle(paths, assets, now=fixed_now)
    bundle_path = paths.root / "manifests" / "mask_review_bundle.json"
    changed = json.loads(bundle_path.read_text(encoding="utf-8"))
    changed["created_at"] = "2026-08-16T10:00:00.000Z"
    bundle_path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ValueError, match="bundle|审阅"):
        module.confirm_mask_review(
            paths,
            changed,
            reviewer="session-1",
            resolved_ids=["cord"],
            now=fixed_now,
            uuid_factory=uuid.uuid4,
        )


def test_tampered_draft_audit_fields_prevent_review_bundle(tmp_path: Path) -> None:
    _state, module, paths, assets = write_review_case(tmp_path)
    draft = json.loads(assets.draft_assessment_path.read_text(encoding="utf-8"))
    draft["edge_algorithms"]["original_rgb"] = "forged-algorithm"
    assets.draft_assessment_path.write_text(json.dumps(draft), encoding="utf-8")

    with pytest.raises(ValueError, match="技术门禁"):
        module.create_mask_review_bundle(paths, assets, now=fixed_now)


def test_normal_confirmation_records_review_event_with_requested_time(
    tmp_path: Path,
) -> None:
    state, module, paths, assets = write_review_case(tmp_path)
    bundle = module.create_mask_review_bundle(paths, assets, now=fixed_now)

    module.confirm_mask_review(
        paths,
        bundle,
        reviewer="session-1",
        resolved_ids=["cord"],
        now=fixed_now,
        uuid_factory=lambda: uuid.UUID("33333333-3333-3333-3333-333333333333"),
    )

    confirmed = state.load_state(paths)
    assert confirmed["status"] == "mask_confirmed"
    assert confirmed["history"][-1]["event"] == "mask_review_confirmed"
    assert confirmed["history"][-1]["at"] == "2026-08-16T09:30:00.000Z"


def test_tampered_receipt_identity_or_duplicate_resolution_fails_closed(
    tmp_path: Path,
) -> None:
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
    receipt_path = paths.root / "confirmations" / "mask_confirmation.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["run_id"] = "wrong-run"
    receipt["resolved_uncertain_region_ids"] = ["cord", "cord"]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    gate = module.validate_mask_confirmation(paths)

    assert gate.automatic_wawapi_edit_allowed is False
    assert "confirmation_invalid" in gate.blockers


def test_existing_receipt_can_resume_state_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, module, paths, assets = write_review_case(tmp_path)
    bundle = module.create_mask_review_bundle(paths, assets, now=fixed_now)
    real_transition = module.transition_state

    def crash_before_state(*args, **kwargs):
        if kwargs.get("next_status") == "mask_confirmed":
            raise RuntimeError("simulated crash")
        return real_transition(*args, **kwargs)

    monkeypatch.setattr(module, "transition_state", crash_before_state)
    with pytest.raises(RuntimeError, match="simulated crash"):
        module.confirm_mask_review(
            paths,
            bundle,
            reviewer="session-1",
            resolved_ids=["cord"],
            now=fixed_now,
            uuid_factory=lambda: uuid.UUID(
                "33333333-3333-3333-3333-333333333333"
            ),
        )
    assert state.load_state(paths)["status"] == "awaiting_mask_confirmation"
    monkeypatch.setattr(module, "transition_state", real_transition)

    gate = module.validate_mask_confirmation(paths)

    assert gate.automatic_wawapi_edit_allowed is True
    resumed = state.load_state(paths)
    assert resumed["status"] == "mask_confirmed"
    assert resumed["receipts"]["mask"]["sha256"] == sha256(
        paths.root / "confirmations" / "mask_confirmation.json"
    )


def test_existing_bundle_can_resume_state_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, module, paths, assets = write_review_case(tmp_path)
    real_transition = module.transition_state
    monkeypatch.setattr(
        module,
        "transition_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        module.create_mask_review_bundle(paths, assets, now=fixed_now)
    assert state.load_state(paths)["status"] == "mask_ready"
    monkeypatch.setattr(module, "transition_state", real_transition)

    bundle = module.create_mask_review_bundle(paths, assets, now=fixed_now)

    assert bundle["schema_version"] == "mask-review-bundle-1.0"
    assert state.load_state(paths)["status"] == "awaiting_mask_confirmation"
