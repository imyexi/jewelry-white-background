from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"
DELIVERY_SCRIPT = SCRIPTS / "deliver_confirmed_result.py"
CONFIRMATION_TEST = Path(__file__).with_name("test_workflow_confirmations.py")


def load_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def confirmed_case(tmp_path: Path):
    fixture = load_path("final_delivery_fixture", CONFIRMATION_TEST)
    state, confirmations, paths, assets = fixture.write_final_review_case(tmp_path)
    confirmations.create_final_review_bundle(paths, assets, now=fixture.fixed_now)
    ids = iter(
        [
            uuid.UUID("33333333-3333-3333-3333-333333333333"),
            uuid.UUID("44444444-4444-4444-4444-444444444444"),
        ]
    )
    receipt = confirmations.confirm_final_review(
        paths,
        reviewer="session-1",
        now=fixture.fixed_now,
        uuid_factory=lambda: next(ids),
    )
    return fixture, state, paths, receipt


class WatermarkRunner:
    def __init__(self, success: bool = True):
        self.success = success
        self.calls = []

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        output = Path(command[command.index("--output") + 1])
        if self.success:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"watermarked-result")
            return subprocess.CompletedProcess(command, 0, "ok", "")
        return subprocess.CompletedProcess(command, 1, "", "watermark failed")


class LarkRunner:
    def __init__(self, response, paths=None, delivery_id=None):
        self.response = response
        self.paths = paths
        self.delivery_id = delivery_id
        self.calls = []
        self.observed_statuses = []

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        if self.paths is not None:
            receipt = self.paths.manifests_dir / f"upload_receipt.{self.delivery_id}.json"
            self.observed_statuses.append(json.loads(receipt.read_text(encoding="utf-8"))["status"])
        if isinstance(self.response, str):
            stdout = self.response
        else:
            stdout = json.dumps(self.response)
        return subprocess.CompletedProcess(command, 0, stdout, "")


def success_response(filename: str) -> dict:
    return {
        "data": {
            "attachments": {
                "record-id": {
                    "target-field-id": [
                        {"name": filename, "file_token": "file-token-1"}
                    ]
                }
            }
        }
    }


def test_delivery_writes_claims_before_each_one_shot_call(tmp_path: Path) -> None:
    _fixture, state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_success", DELIVERY_SCRIPT)
    filename = f"SY1537__{paths.run_id}__{receipt['delivery_id']}__watermarked.png"
    watermark = WatermarkRunner()
    lark = LarkRunner(success_response(filename), paths, receipt["delivery_id"])

    result = module.deliver_confirmed_result(
        paths, watermark_runner=watermark, lark_runner=lark
    )

    assert result.status == "completed"
    assert len(watermark.calls) == 1
    assert len(lark.calls) == 1
    assert lark.observed_statuses == ["uploading"]
    command = lark.calls[0]
    assert command[:3] == ["lark-cli", "base", "+record-upload-attachment"]
    assert command.count("--file") == 1
    assert state.load_state(paths)["status"] == "completed"


@pytest.mark.parametrize(
    "response",
    [
        {"data": {"attachments": {"record-id": {"target-field-id": []}}}},
        {"data": {"attachments": {"record-id": {"target-field-id": [{"name": "x", "file_token": "a"}, {"name": "x", "file_token": "b"}]}}}},
        {"data": {"attachments": {"record-id": {"target-field-id": [{"name": "x", "file_token": ""}]}}}},
        "{truncated",
    ],
)
def test_ambiguous_upload_response_becomes_unknown_without_retry(
    tmp_path: Path, response
) -> None:
    _fixture, state, paths, _receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_ambiguous", DELIVERY_SCRIPT)
    watermark = WatermarkRunner()
    lark = LarkRunner(response)

    result = module.deliver_confirmed_result(
        paths, watermark_runner=watermark, lark_runner=lark
    )

    assert result.status == "upload_unknown"
    assert len(lark.calls) == 1
    assert state.load_state(paths)["status"] == "upload_unknown"


def test_watermark_failure_stops_before_upload(tmp_path: Path) -> None:
    _fixture, state, paths, _receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_watermark_failure", DELIVERY_SCRIPT)
    watermark = WatermarkRunner(success=False)
    lark = LarkRunner({})

    result = module.deliver_confirmed_result(
        paths, watermark_runner=watermark, lark_runner=lark
    )

    assert result.status == "watermark_failed"
    assert len(watermark.calls) == 1
    assert len(lark.calls) == 0
    assert state.load_state(paths)["status"] == "watermark_failed"


def test_second_delivery_never_repeats_side_effects(tmp_path: Path) -> None:
    _fixture, _state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_no_repeat", DELIVERY_SCRIPT)
    filename = f"SY1537__{paths.run_id}__{receipt['delivery_id']}__watermarked.png"
    first_watermark = WatermarkRunner()
    first_lark = LarkRunner(success_response(filename))
    assert module.deliver_confirmed_result(
        paths, watermark_runner=first_watermark, lark_runner=first_lark
    ).status == "completed"
    second_watermark = WatermarkRunner()
    second_lark = LarkRunner({})

    result = module.deliver_confirmed_result(
        paths, watermark_runner=second_watermark, lark_runner=second_lark
    )

    assert result.status == "completed"
    assert second_watermark.calls == []
    assert second_lark.calls == []


def seed_completed_watermark(module, state, paths, receipt):
    receipt_path = paths.root / "confirmations" / "final_confirmation.json"
    output = (
        paths.root
        / "watermarked"
        / f"SY1537__{paths.run_id}__{receipt['delivery_id']}__watermarked.png"
    )
    native_output = module._filesystem_path(output)
    native_output.parent.mkdir(parents=True, exist_ok=True)
    native_output.write_bytes(b"ORIGINAL")
    store = module._state.ClaimStore(
        paths.manifests_dir / f"watermark_receipt.{receipt['delivery_id']}.json"
    )
    identity = module._watermark_identity(paths, receipt_path, receipt, output)
    owner = module._default_owner(module._utc_now())
    claim = store.create(identity, owner, status="watermarking")
    store.update(
        expected_revision=claim["record_revision"],
        updater=lambda payload: payload.update(
            status="completed", output=module._watermarked_file_record(output)
        ),
    )
    return output, store, owner


def test_completed_watermark_claim_rejects_changed_bytes_before_upload(
    tmp_path: Path,
) -> None:
    _fixture, state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_tampered_watermark", DELIVERY_SCRIPT)
    output, _store, _owner = seed_completed_watermark(module, state, paths, receipt)
    state.transition_state(
        paths,
        expected_status="final_confirmed",
        expected_revision=state.load_state(paths)["state_revision"],
        next_status="watermarking",
        event="test_watermarking",
    )
    state.transition_state(
        paths,
        expected_status="watermarking",
        expected_revision=state.load_state(paths)["state_revision"],
        next_status="upload_ready",
        event="test_upload_ready",
    )
    module._filesystem_path(output).write_bytes(b"TAMPERED")
    lark = LarkRunner({})

    result = module.deliver_confirmed_result(paths, lark_runner=lark)

    assert result.status == "watermark_failed"
    assert lark.calls == []
    assert state.load_state(paths)["status"] == "watermark_failed"


def test_completed_watermark_claim_before_state_transition_recovers_without_rerun(
    tmp_path: Path,
) -> None:
    _fixture, _state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_watermark_claim_recovery", DELIVERY_SCRIPT)
    output, _store, _owner = seed_completed_watermark(module, _state, paths, receipt)
    lark = LarkRunner(success_response(output.name))
    watermark = WatermarkRunner()

    result = module.deliver_confirmed_result(
        paths, watermark_runner=watermark, lark_runner=lark
    )

    assert result.status == "completed"
    assert watermark.calls == []
    assert len(lark.calls) == 1


def test_response_recorded_watermark_recovers_from_saved_result(tmp_path: Path) -> None:
    _fixture, state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_watermark_response_recovery", DELIVERY_SCRIPT)
    output, store, _owner = seed_completed_watermark(module, state, paths, receipt)
    claim = store.load()
    store.update(
        expected_revision=claim["record_revision"],
        updater=lambda payload: payload.update(
            status="response_recorded", returncode=0, stdout="ok", stderr=""
        ),
    )
    state.transition_state(
        paths,
        expected_status="final_confirmed",
        expected_revision=state.load_state(paths)["state_revision"],
        next_status="watermarking",
        event="test_watermarking",
    )
    lark = LarkRunner(success_response(output.name))

    result = module.deliver_confirmed_result(paths, lark_runner=lark)

    assert result.status == "completed"
    assert len(lark.calls) == 1


def test_completed_upload_receipt_recovers_workflow_terminal_state(
    tmp_path: Path,
) -> None:
    _fixture, state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_upload_receipt_recovery", DELIVERY_SCRIPT)
    output, _watermark_store, owner = seed_completed_watermark(
        module, state, paths, receipt
    )
    for expected, next_status in (
        ("final_confirmed", "watermarking"),
        ("watermarking", "upload_ready"),
    ):
        state.transition_state(
            paths,
            expected_status=expected,
            expected_revision=state.load_state(paths)["state_revision"],
            next_status=next_status,
            event=f"test_{next_status}",
        )
    receipt_path = paths.root / "confirmations" / "final_confirmation.json"
    upload_store = module._state.ClaimStore(
        paths.manifests_dir / f"upload_receipt.{receipt['delivery_id']}.json"
    )
    identity = module._upload_identity(
        paths,
        receipt_path,
        receipt,
        module._watermarked_file_record(output),
    )
    claim = upload_store.create(identity, owner, status="uploading")
    upload_store.update(
        expected_revision=claim["record_revision"],
        updater=lambda payload: payload.update(status="completed", file_token="token"),
    )

    result = module.deliver_confirmed_result(
        paths, watermark_runner=WatermarkRunner(), lark_runner=LarkRunner({})
    )

    assert result.status == "completed"
    assert state.load_state(paths)["status"] == "completed"


def run_two_deliveries(module, paths, **kwargs):
    results = []
    errors = []

    def target() -> None:
        try:
            results.append(module.deliver_confirmed_result(paths, **kwargs).status)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=target) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return results, errors


def test_watermark_claim_creation_race_has_one_side_effect_and_no_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture, _state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_watermark_race", DELIVERY_SCRIPT)
    barrier = threading.Barrier(2)
    original_create = module._state.ClaimStore.create

    def racing_create(store, *args, **kwargs):
        if store.path.name.startswith("watermark_receipt"):
            barrier.wait(timeout=5)
        return original_create(store, *args, **kwargs)

    monkeypatch.setattr(module._state.ClaimStore, "create", racing_create)
    filename = f"SY1537__{paths.run_id}__{receipt['delivery_id']}__watermarked.png"
    watermark = WatermarkRunner()
    lark = LarkRunner(success_response(filename))

    results, errors = run_two_deliveries(
        module, paths, watermark_runner=watermark, lark_runner=lark
    )

    assert errors == []
    assert len(watermark.calls) == 1
    assert len(lark.calls) == 1
    assert "completed" in results


def test_upload_claim_creation_race_has_one_side_effect_and_no_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture, state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_upload_race", DELIVERY_SCRIPT)
    output, _store, _owner = seed_completed_watermark(module, state, paths, receipt)
    for expected, next_status in (
        ("final_confirmed", "watermarking"),
        ("watermarking", "upload_ready"),
    ):
        state.transition_state(
            paths,
            expected_status=expected,
            expected_revision=state.load_state(paths)["state_revision"],
            next_status=next_status,
            event=f"test_{next_status}",
        )
    barrier = threading.Barrier(2)
    original_create = module._state.ClaimStore.create

    def racing_create(store, *args, **kwargs):
        if store.path.name.startswith("upload_receipt"):
            barrier.wait(timeout=5)
        return original_create(store, *args, **kwargs)

    monkeypatch.setattr(module._state.ClaimStore, "create", racing_create)
    lark = LarkRunner(success_response(output.name))

    results, errors = run_two_deliveries(
        module, paths, watermark_runner=WatermarkRunner(), lark_runner=lark
    )

    assert errors == []
    assert len(lark.calls) == 1
    assert "completed" in results


def test_upload_claim_identity_conflict_enters_upload_unknown(tmp_path: Path) -> None:
    _fixture, state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_upload_identity_conflict", DELIVERY_SCRIPT)
    output, _store, owner = seed_completed_watermark(module, state, paths, receipt)
    for expected, next_status in (
        ("final_confirmed", "watermarking"),
        ("watermarking", "upload_ready"),
    ):
        state.transition_state(
            paths,
            expected_status=expected,
            expected_revision=state.load_state(paths)["state_revision"],
            next_status=next_status,
            event=f"test_{next_status}",
        )
    receipt_path = paths.root / "confirmations" / "final_confirmation.json"
    identity = module._upload_identity(
        paths,
        receipt_path,
        receipt,
        module._watermarked_file_record(output),
    )
    identity["target_field_id"] = "wrong-field"
    module._state.ClaimStore(
        paths.manifests_dir / f"upload_receipt.{receipt['delivery_id']}.json"
    ).create(identity, owner, status="uploading")

    result = module.deliver_confirmed_result(paths)

    assert result.status == "upload_unknown"
    assert state.load_state(paths)["status"] == "upload_unknown"


def test_two_completed_watermark_recoverers_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture, _state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_watermark_recovery_race", DELIVERY_SCRIPT)
    output, _store, _owner = seed_completed_watermark(module, _state, paths, receipt)
    barrier = threading.Barrier(2)
    original_recover = module._recover_watermark

    def racing_recover(*args, **kwargs):
        result = original_recover(*args, **kwargs)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(module, "_recover_watermark", racing_recover)
    lark = LarkRunner(success_response(output.name))

    results, errors = run_two_deliveries(module, paths, lark_runner=lark)

    assert errors == []
    assert "completed" in results
    assert len(lark.calls) == 1


def test_two_response_recorded_upload_recoverers_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture, state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_upload_response_race", DELIVERY_SCRIPT)
    output, _store, owner = seed_completed_watermark(module, state, paths, receipt)
    for expected, next_status in (
        ("final_confirmed", "watermarking"),
        ("watermarking", "upload_ready"),
        ("upload_ready", "uploading"),
    ):
        state.transition_state(
            paths,
            expected_status=expected,
            expected_revision=state.load_state(paths)["state_revision"],
            next_status=next_status,
            event=f"test_{next_status}",
        )
    receipt_path = paths.root / "confirmations" / "final_confirmation.json"
    identity = module._upload_identity(
        paths,
        receipt_path,
        receipt,
        module._watermarked_file_record(output),
    )
    store = module._state.ClaimStore(
        paths.manifests_dir / f"upload_receipt.{receipt['delivery_id']}.json"
    )
    claim = store.create(identity, owner, status="uploading")
    store.update(
        expected_revision=claim["record_revision"],
        updater=lambda payload: payload.update(
            status="response_recorded",
            returncode=0,
            stdout=json.dumps(success_response(output.name)),
            stderr="",
        ),
    )
    barrier = threading.Barrier(2)
    original_parse = module._parse_upload_response

    def racing_parse(payload):
        result = original_parse(payload)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(module, "_parse_upload_response", racing_parse)

    results, errors = run_two_deliveries(module, paths)

    assert errors == []
    assert results == ["completed", "completed"]


def test_watermark_owner_accepts_recovery_process_finishing_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture, _state, paths, receipt = confirmed_case(tmp_path)
    module = load_path("deliver_confirmed_watermark_owner_loser", DELIVERY_SCRIPT)
    filename = f"SY1537__{paths.run_id}__{receipt['delivery_id']}__watermarked.png"
    watermark = WatermarkRunner()
    lark = LarkRunner(success_response(filename))
    original_update = module._update_claim_idempotently
    nested_results = []
    triggered = False

    def update_then_recover(store, status, **fields):
        nonlocal triggered
        updated = original_update(store, status, **fields)
        if (
            not triggered
            and store.path.name.startswith("watermark_receipt")
            and status == "completed"
        ):
            triggered = True
            nested_results.append(
                module.deliver_confirmed_result(
                    paths, watermark_runner=WatermarkRunner(), lark_runner=lark
                ).status
            )
        return updated

    monkeypatch.setattr(module, "_update_claim_idempotently", update_then_recover)

    result = module.deliver_confirmed_result(
        paths, watermark_runner=watermark, lark_runner=lark
    )

    assert result.status == "completed"
    assert nested_results == ["completed"]
    assert len(watermark.calls) == 1
    assert len(lark.calls) == 1
