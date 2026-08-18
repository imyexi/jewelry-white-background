from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "workflow_state.py"


def load_module():
    module_name = "skill_workflow_state"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


FIXED_NOW = datetime(2026, 8, 16, 8, 9, 10, 123000, tzinfo=timezone.utc)
RUN_UUID = uuid.UUID("01234567-89ab-cdef-0123-456789abcdef")
OWNER_UUID = uuid.UUID("fedcba98-7654-3210-fedc-ba9876543210")


def identity(module):
    return module.WorkflowIdentity(
        product_id="SY1537",
        base_token="base-token",
        table_id="table-id",
        record_id="record-id",
        front_field_id="front-field-id",
        target_field_id="target-field-id",
    )


def uuid_sequence(*values: uuid.UUID):
    iterator = iter(values)
    return lambda: next(iterator)


def create_run(module, tmp_path: Path):
    return module.create_run(
        tmp_path,
        identity(module),
        now=lambda: FIXED_NOW,
        uuid_factory=uuid_sequence(RUN_UUID, OWNER_UUID),
    )


def test_create_run_atomically_publishes_only_manifests_state_and_lock(
    tmp_path: Path,
) -> None:
    module = load_module()

    run = create_run(module, tmp_path)

    assert run.run_id == (
        "20260816T080910123Z-0123456789abcdef0123456789abcdef"
    )
    assert run.root == tmp_path / "SY1537" / run.run_id
    assert run.state_path == run.root / "manifests" / "workflow_state.json"
    assert run.workflow_lock_path == run.root / "manifests" / "workflow.lock"
    assert run.state_path.is_file()
    assert run.workflow_lock_path.is_file()
    assert not (run.root / "workflow_state.json").exists()
    assert not (run.root / "workflow.lock").exists()
    assert list((tmp_path / "SY1537").glob(".*.creating-*")) == []

    state = json.loads(run.state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == "jewelry-white-background-workflow-1.0"
    assert state["run_id"] == run.run_id
    assert state["product_id"] == "SY1537"
    assert state["status"] == "run_created"
    assert state["state_revision"] == 1
    assert state["base_token"] == "base-token"
    assert state["target_field_id"] == "target-field-id"
    assert state["receipts"] == {
        "mask": {"path": None, "sha256": None},
        "final": {"path": None, "sha256": None},
    }
    assert state["delivery"] == {
        "delivery_id": None,
        "upload_receipt_path": None,
    }
    assert state["failure"] is None


@pytest.mark.parametrize(
    "product_id",
    ["", "SKU.", "A..B", "CON", "CON.txt", "AUX", "COM1", "LPT9.png", "A/B"],
)
def test_create_run_rejects_unsafe_product_ids_before_creating_directories(
    tmp_path: Path, product_id: str
) -> None:
    module = load_module()
    invalid = module.WorkflowIdentity(
        product_id=product_id,
        base_token="base-token",
        table_id="table-id",
        record_id="record-id",
        front_field_id="front-field-id",
        target_field_id="target-field-id",
    )

    with pytest.raises(ValueError):
        module.create_run(
            tmp_path,
            invalid,
            now=lambda: FIXED_NOW,
            uuid_factory=uuid_sequence(RUN_UUID, OWNER_UUID),
        )

    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_create_run_rejects_windows_root_without_descendant_budget(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_module()
    monkeypatch.setattr(module, "IS_WINDOWS", True, raising=False)
    output_root = tmp_path / ("long-root-" + "x" * 180)

    with pytest.raises(ValueError, match="Windows 路径预算"):
        module.create_run(
            output_root,
            identity(module),
            now=lambda: FIXED_NOW,
            uuid_factory=uuid_sequence(RUN_UUID, OWNER_UUID),
        )

    assert not output_root.exists()


def test_run_path_budget_accepts_exact_windows_limit() -> None:
    module = load_module()
    final_root = Path("C:/") / ("x" * 173)

    assert len(str(final_root)) + module.RUN_DESCENDANT_RESERVE == 240
    module._validate_run_path_budget(final_root, windows=True)


def test_run_path_budget_is_not_applied_off_windows() -> None:
    module = load_module()

    module._validate_run_path_budget(Path("C:/") / ("x" * 300), windows=False)


def test_create_run_never_overwrites_an_existing_run(tmp_path: Path) -> None:
    module = load_module()
    first = create_run(module, tmp_path)
    original = first.state_path.read_bytes()

    with pytest.raises(FileExistsError):
        create_run(module, tmp_path)

    assert first.state_path.read_bytes() == original


def test_transition_requires_expected_status_and_revision(tmp_path: Path) -> None:
    module = load_module()
    run = create_run(module, tmp_path)

    with pytest.raises(module.StateConflict):
        module.transition_state(
            run,
            expected_status="source_ready",
            expected_revision=1,
            next_status="detection_image_ready",
            event="detection_ok",
            now=lambda: FIXED_NOW + timedelta(seconds=1),
        )

    state = module.load_state(run)
    assert state["status"] == "run_created"
    assert state["state_revision"] == 1


def test_transition_increments_revision_and_preserves_history(tmp_path: Path) -> None:
    module = load_module()
    run = create_run(module, tmp_path)

    state = module.transition_state(
        run,
        expected_status="run_created",
        expected_revision=1,
        next_status="source_ready",
        event="source_persisted",
        now=lambda: FIXED_NOW + timedelta(seconds=1),
    )

    assert state["status"] == "source_ready"
    assert state["state_revision"] == 2
    assert state["last_transition"] == {
        "from": "run_created",
        "event": "source_persisted",
        "at": "2026-08-16T08:09:11.123Z",
    }
    assert state["history"][-1] == state["last_transition"]


def test_terminal_state_has_no_automatic_transition(tmp_path: Path) -> None:
    module = load_module()
    run = create_run(module, tmp_path)
    state = module.load_state(run)
    state["status"] = "edit_unknown"
    state["state_revision"] = 4
    module.atomic_replace_json(run.state_path, state)

    with pytest.raises(module.TerminalStateError):
        module.transition_state(
            run,
            expected_status="edit_unknown",
            expected_revision=4,
            next_status="edit_attempt_2",
            event="retry",
            now=lambda: FIXED_NOW + timedelta(seconds=2),
        )


def owner(module):
    return module.OwnerIdentity(
        owner_id=OWNER_UUID.hex,
        owner_host_id="host-1",
        owner_pid=1234,
        owner_process_started_at="2026-08-16T08:00:00.000Z",
    )


def test_claim_uses_independent_sidecar_lock_and_revision_cas(tmp_path: Path) -> None:
    module = load_module()
    store = module.ClaimStore(tmp_path / "edit-call-1.json")

    claim = store.create(
        {"request_identity": "A" * 64},
        owner(module),
        status="submitting",
        now=lambda: FIXED_NOW,
    )

    assert store.lock_path == tmp_path / "edit-call-1.json.lock"
    assert claim["record_revision"] == 1
    assert claim["owner_id"] == OWNER_UUID.hex
    assert claim["lease_expires_at"] == "2026-08-16T08:09:40.123Z"
    with pytest.raises(FileExistsError):
        store.create(
            {"request_identity": "A" * 64},
            owner(module),
            status="submitting",
            now=lambda: FIXED_NOW,
        )

    recorded = store.update(
        expected_revision=1,
        updater=lambda payload: payload.update(status="response_recorded"),
    )
    assert recorded["record_revision"] == 2
    assert recorded["status"] == "response_recorded"
    assert store.heartbeat(
        expected_revision=1,
        owner=owner(module),
        now=lambda: FIXED_NOW + timedelta(seconds=5),
    ) == recorded


def test_claim_owner_classification_requires_process_and_heartbeat_evidence(
    tmp_path: Path,
) -> None:
    module = load_module()
    store = module.ClaimStore(tmp_path / "upload.json")
    store.create({}, owner(module), status="uploading", now=lambda: FIXED_NOW)

    assert (
        store.classify_owner(
            now=FIXED_NOW + timedelta(seconds=20),
            current_host_id="host-1",
            process_probe=lambda *_: False,
        )
        == "active"
    )
    assert (
        store.classify_owner(
            now=FIXED_NOW + timedelta(seconds=31),
            current_host_id="host-1",
            process_probe=lambda *_: False,
        )
        == "terminated"
    )
    assert (
        store.classify_owner(
            now=FIXED_NOW + timedelta(seconds=31),
            current_host_id="host-2",
            process_probe=lambda *_: False,
        )
        == "unknown"
    )
