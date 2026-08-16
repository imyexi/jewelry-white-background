from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image


SKILL_SCRIPTS = Path(__file__).parents[1] / "scripts"
RUNNER_SCRIPT = SKILL_SCRIPTS / "run_wawapi_edit_with_retry.py"
STATE_SCRIPT = SKILL_SCRIPTS / "workflow_state.py"
ADAPTER_SCRIPT = Path(__file__).parents[3] / "scripts" / "yuan_image_generation_adapter.py"


def load_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def attempt(adapter, **overrides):
    values = {
        "request_identity": {"provider": "wawapi"},
        "request_identity_sha256": "A" * 64,
        "command": ("helper",),
        "returncode": 1,
        "stdout": "",
        "stderr": "",
        "http_status": None,
        "request_may_have_been_sent": True,
        "task_or_job_id": None,
        "rejection_evidence": None,
    }
    values.update(overrides)
    return adapter.BackgroundEditAttempt(**values)


@pytest.mark.parametrize(
    ("edit_attempt", "slot", "action", "delay"),
    [
        ({"result_path": Path("result.png")}, 1, "complete", None),
        ({"http_status": 429}, 1, "retry", 30),
        ({"http_status": 429}, 2, "retry", 90),
        ({"http_status": 429}, 3, "failed", None),
        (
            {"http_status": 503, "rejection_evidence": "request_not_accepted"},
            2,
            "retry",
            90,
        ),
        (
            {"http_status": 503, "rejection_evidence": "request_not_accepted"},
            3,
            "failed",
            None,
        ),
        ({"http_status": 503}, 1, "unknown", None),
        ({"http_status": 429, "task_or_job_id": "remote-1"}, 1, "unknown", None),
        ({"http_status": 400}, 1, "failed", None),
        ({"returncode": None}, 1, "unknown", None),
        (
            {"returncode": 0, "response_payload": {"status": "completed"}},
            1,
            "failed",
            None,
        ),
    ],
)
def test_classify_attempt_uses_only_provably_safe_retries(
    edit_attempt: dict,
    slot: int,
    action: str,
    delay: int | None,
) -> None:
    adapter = load_path("adapter_for_retry_classification", ADAPTER_SCRIPT)
    module = load_path("retry_runner_classification", RUNNER_SCRIPT)

    decision = module.classify_attempt(attempt(adapter, **edit_attempt), slot)

    assert decision.action == action
    assert decision.delay_seconds == delay


FIXED_NOW = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.current = FIXED_NOW
        self.sleeps: list[int] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: int) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


class SequencedTransport:
    def __init__(self, responses: list[str]):
        self.responses = iter(responses)
        self.post_count = 0

    def __call__(self, command, **kwargs):
        self.post_count += 1
        response = next(self.responses)
        if response == "success":
            output_dir = Path(command[command.index("--output-dir") + 1])
            candidate = output_dir / f"result-{self.post_count}.png"
            Image.new("RGB", (24, 20), (220, 220, 220)).save(candidate, "PNG")
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "provider": "wawapi",
                        "task_id": f"local-{self.post_count}",
                        "status": "completed",
                        "output": [{"path": str(candidate)}],
                    }
                ),
                stderr="",
            )
        if response == "429":
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr='HTTP 429: {"error":"rate limited"}',
            )
        if response == "rejected-503":
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr='HTTP 503: {"request_not_accepted":true}',
            )
        if response == "500":
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr='HTTP 500: {"error":"server error"}',
            )
        if response == "400":
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr='HTTP 400: {"error":"bad request"}',
            )
        if response == "timeout":
            raise subprocess.TimeoutExpired(command, timeout=30)
        raise AssertionError(response)


def run_fixture(tmp_path: Path):
    state = load_path("state_for_retry_fixture", STATE_SCRIPT)
    adapter = load_path("adapter_for_retry_fixture", ADAPTER_SCRIPT)
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
    paths = state.create_run(
        tmp_path,
        identity,
        now=lambda: FIXED_NOW,
        uuid_factory=lambda: next(uuids),
    )
    payload = state.load_state(paths)
    payload["status"] = "mask_confirmed"
    payload["state_revision"] = 7
    state.atomic_replace_json(paths.state_path, payload)
    image = paths.root / "cropped" / "SY1537_original.png"
    mask = paths.root / "mask" / "SY1537_product-protection-mask.png"
    image.parent.mkdir(parents=True)
    mask.parent.mkdir(parents=True)
    Image.new("RGB", (24, 20), (240, 240, 240)).save(image, "PNG")
    Image.new("RGBA", (24, 20), (255, 255, 255, 0)).save(mask, "PNG")
    request = adapter.BackgroundEditRequest(
        prompt="只替换透明背景\n",
        image=image,
        mask=mask,
        output_path=paths.root / "edit" / "SY1537_edit-result.png",
        base_url="https://example.test",
        model="gpt-image-2",
        python_executable="python",
        helper_path=Path("helper.py"),
    )
    owner = state.OwnerIdentity(
        owner_id="3" * 32,
        owner_host_id="host-1",
        owner_pid=1234,
        owner_process_started_at="2026-08-16T09:00:00.000Z",
    )
    return state, adapter, paths, request, owner


@pytest.mark.parametrize(
    ("responses", "expected_posts", "terminal", "sleeps"),
    [
        (["success"], 1, "edit_completed", []),
        (["429", "success"], 2, "edit_completed", [30]),
        (["429", "rejected-503", "success"], 3, "edit_completed", [30, 90]),
        (["429", "429", "429"], 3, "edit_failed", [30, 90]),
        (["500"], 1, "edit_unknown", []),
        (["timeout"], 1, "edit_unknown", []),
        (["400"], 1, "edit_failed", []),
    ],
)
def test_edit_post_budget_and_terminal_classification(
    tmp_path: Path,
    responses: list[str],
    expected_posts: int,
    terminal: str,
    sleeps: list[int],
) -> None:
    state, _adapter, paths, request, owner = run_fixture(tmp_path)
    module = load_path("retry_runner_integration", RUNNER_SCRIPT)
    clock = FakeClock()
    transport = SequencedTransport(responses)

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=clock,
        transport=transport,
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: True,
    )

    assert result == terminal
    assert transport.post_count == expected_posts
    assert clock.sleeps == sleeps
    assert state.load_state(paths)["status"] == terminal
    claims = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((paths.root / "logs").glob("edit-call-*.json"))
    ]
    assert len(claims) == expected_posts
    assert len({item["request_identity_sha256"] for item in claims}) == 1


def test_active_existing_claim_returns_without_another_post(tmp_path: Path) -> None:
    state, adapter, paths, request, owner = run_fixture(tmp_path)
    module = load_path("retry_runner_active_claim", RUNNER_SCRIPT)
    identity = adapter.build_background_edit_request_identity(request)
    store = state.ClaimStore(paths.root / "logs" / "edit-call-1.json")
    store.create(
        {
            "call_slot": 1,
            "request_identity": identity.as_dict(),
            "request_identity_sha256": identity.sha256,
        },
        owner,
        status="submitting",
        now=lambda: FIXED_NOW,
    )
    transport = SequencedTransport(["success"])

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=FakeClock(),
        transport=transport,
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: True,
    )

    assert result == "already_in_progress"
    assert transport.post_count == 0


def test_expired_terminated_submitting_claim_becomes_unknown_without_new_slot(
    tmp_path: Path,
) -> None:
    state, adapter, paths, request, owner = run_fixture(tmp_path)
    module = load_path("retry_runner_stale_claim", RUNNER_SCRIPT)
    identity = adapter.build_background_edit_request_identity(request)
    store = state.ClaimStore(paths.root / "logs" / "edit-call-1.json")
    store.create(
        {
            "call_slot": 1,
            "request_identity": identity.as_dict(),
            "request_identity_sha256": identity.sha256,
        },
        owner,
        status="submitting",
        now=lambda: FIXED_NOW,
    )
    clock = FakeClock()
    clock.current += timedelta(seconds=31)
    transport = SequencedTransport(["success"])

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=clock,
        transport=transport,
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: False,
    )

    assert result == "edit_unknown"
    assert transport.post_count == 0
    assert not (paths.root / "logs" / "edit-call-2.json").exists()


def create_response_recorded_claim(
    state,
    adapter,
    paths,
    request,
    owner,
    response: str,
) -> Path | None:
    identity = adapter.build_background_edit_request_identity(request)
    store = state.ClaimStore(paths.root / "logs" / "edit-call-1.json")
    claim = store.create(
        {
            "call_slot": 1,
            "request_identity": identity.as_dict(),
            "request_identity_sha256": identity.sha256,
        },
        owner,
        status="submitting",
        now=lambda: FIXED_NOW,
    )
    state.transition_state(
        paths,
        expected_status="mask_confirmed",
        expected_revision=7,
        next_status="edit_attempt_1",
        event="test_claim_created",
        now=lambda: FIXED_NOW,
    )

    result_path = None
    response_payload = None
    http_status = None
    returncode = 1
    stderr = ""
    rejection_evidence = None
    result = None
    if response == "success":
        result_path = request.output_path
        result_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 20), (210, 210, 210)).save(result_path, "PNG")
        data = result_path.read_bytes()
        response_payload = {"status": "completed", "output": [{"path": str(result_path)}]}
        returncode = 0
        result = {
            "path": str(result_path),
            "format": "PNG",
            "size": [24, 20],
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest().upper(),
        }
    elif response == "429":
        http_status = 429
        stderr = 'HTTP 429: {"error":"rate limited"}'
    elif response == "rejected-503":
        http_status = 503
        stderr = 'HTTP 503: {"request_not_accepted":true}'
        rejection_evidence = "request_not_accepted"
    elif response == "400":
        http_status = 400
        stderr = 'HTTP 400: {"error":"bad request"}'
    else:
        raise AssertionError(response)

    def record(payload: dict) -> None:
        payload.update(
            {
                "status": "response_recorded",
                "started_at": "2026-08-16T10:00:00.000Z",
                "finished_at": "2026-08-16T10:00:00.000Z",
                "command": ["helper"],
                "returncode": returncode,
                "stdout": json.dumps(response_payload) if response_payload else "",
                "stderr": stderr,
                "response_body": response_payload,
                "http_status": http_status,
                "request_may_have_been_sent": True,
                "task_or_job_id": None,
                "rejection_evidence": rejection_evidence,
                "local_error": None,
                "actual_request_identity": identity.as_dict(),
                "actual_request_identity_sha256": identity.sha256,
                "result": result,
            }
        )

    store.update(
        expected_revision=claim["record_revision"],
        updater=record,
        now=lambda: FIXED_NOW,
    )
    return result_path


@pytest.mark.parametrize(
    ("recorded", "responses", "terminal", "posts", "sleeps"),
    [
        ("success", [], "edit_completed", 0, []),
        ("429", ["success"], "edit_completed", 1, [30]),
        ("rejected-503", ["success"], "edit_completed", 1, [30]),
        ("400", [], "edit_failed", 0, []),
    ],
)
def test_response_recorded_crash_recovers_without_repeating_old_post(
    tmp_path: Path,
    recorded: str,
    responses: list[str],
    terminal: str,
    posts: int,
    sleeps: list[int],
) -> None:
    state, adapter, paths, request, owner = run_fixture(tmp_path)
    create_response_recorded_claim(
        state, adapter, paths, request, owner, recorded
    )
    module = load_path("retry_runner_response_recovery", RUNNER_SCRIPT)
    clock = FakeClock()
    transport = SequencedTransport(responses)

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=clock,
        transport=transport,
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: False,
    )

    assert result == terminal
    assert transport.post_count == posts
    assert clock.sleeps == sleeps


def test_attempt_identity_drift_becomes_unknown(tmp_path: Path, monkeypatch) -> None:
    state, _adapter, paths, request, owner = run_fixture(tmp_path)
    module = load_path("retry_runner_identity_drift", RUNNER_SCRIPT)

    def drifted_attempt(edit_request, transport):
        result_path = edit_request.output_path
        result_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 20), (200, 200, 200)).save(result_path, "PNG")
        data = result_path.read_bytes()
        return module._adapter.BackgroundEditAttempt(
            request_identity={"provider": "different-request"},
            request_identity_sha256="F" * 64,
            command=("helper",),
            returncode=0,
            stdout='{"status":"completed"}',
            stderr="",
            http_status=None,
            request_may_have_been_sent=True,
            task_or_job_id=None,
            rejection_evidence=None,
            response_payload={"status": "completed"},
            result_path=result_path,
            result_format="PNG",
            result_size=(24, 20),
            result_bytes=len(data),
            result_sha256=hashlib.sha256(data).hexdigest().upper(),
        )

    monkeypatch.setattr(
        module._adapter, "edit_background_single_attempt", drifted_attempt
    )

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=FakeClock(),
        transport=SequencedTransport([]),
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: True,
    )

    assert result == "edit_unknown"
    assert state.load_state(paths)["status"] == "edit_unknown"
    assert not (paths.manifests_dir / "SY1537_edit_result.json").exists()
    claim = json.loads(
        (paths.root / "logs" / "edit-call-1.json").read_text(encoding="utf-8")
    )
    assert claim["actual_request_identity_sha256"] == "F" * 64


@pytest.mark.parametrize("damage", ["size-mismatch", "missing", "fake-png"])
def test_local_request_gate_failure_is_edit_failed_without_claim_or_post(
    tmp_path: Path, damage: str
) -> None:
    state, _adapter, paths, request, owner = run_fixture(tmp_path)
    if damage == "size-mismatch":
        Image.new("RGBA", (23, 20), (255, 255, 255, 0)).save(request.mask, "PNG")
    elif damage == "missing":
        request.mask.unlink()
    elif damage == "fake-png":
        request.mask.write_bytes(b"not-a-png")
    module = load_path(f"retry_runner_local_gate_{damage}", RUNNER_SCRIPT)
    transport = SequencedTransport([])

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=FakeClock(),
        transport=transport,
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: True,
    )

    assert result == "edit_failed"
    assert state.load_state(paths)["status"] == "edit_failed"
    assert transport.post_count == 0
    assert not list((paths.root / "logs").glob("edit-call-*.json"))


def test_recovered_result_manifest_revalidates_disk_metadata(tmp_path: Path) -> None:
    state, adapter, paths, request, owner = run_fixture(tmp_path)
    create_response_recorded_claim(
        state, adapter, paths, request, owner, "success"
    )
    claim_path = paths.root / "logs" / "edit-call-1.json"
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    claim["result"]["size"] = [999, 999]
    state.atomic_replace_json(claim_path, claim)
    module = load_path("retry_runner_manifest_disk_check", RUNNER_SCRIPT)

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=FakeClock(),
        transport=SequencedTransport([]),
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: False,
    )

    assert result == "edit_unknown"
    assert state.load_state(paths)["status"] == "edit_unknown"
    assert not (paths.manifests_dir / "SY1537_edit_result.json").exists()


@pytest.mark.parametrize("tamper", ["claim-object", "actual-identity"])
def test_response_recorded_recovery_rejects_request_identity_tampering(
    tmp_path: Path, tamper: str
) -> None:
    state, adapter, paths, request, owner = run_fixture(tmp_path)
    create_response_recorded_claim(
        state, adapter, paths, request, owner, "success"
    )
    claim_path = paths.root / "logs" / "edit-call-1.json"
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    if tamper == "claim-object":
        claim["request_identity"]["provider"] = "tampered"
    else:
        claim["actual_request_identity"] = {"provider": "different-request"}
        claim["actual_request_identity_sha256"] = "F" * 64
    state.atomic_replace_json(claim_path, claim)
    module = load_path(f"retry_runner_recovery_identity_{tamper}", RUNNER_SCRIPT)

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=FakeClock(),
        transport=SequencedTransport([]),
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: False,
    )

    assert result == "edit_unknown"
    assert state.load_state(paths)["status"] == "edit_unknown"
    assert not (paths.manifests_dir / "SY1537_edit_result.json").exists()


def test_concurrent_response_recovery_reloads_winning_decision(
    tmp_path: Path, monkeypatch
) -> None:
    state, adapter, paths, request, owner = run_fixture(tmp_path)
    create_response_recorded_claim(
        state, adapter, paths, request, owner, "success"
    )
    module = load_path("retry_runner_recovery_cas", RUNNER_SCRIPT)
    original = module._record_decision
    raced = False

    def lose_after_winner(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            original(*args, **kwargs)
            raise module._state.ClaimConflict("simulated concurrent winner")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_record_decision", lose_after_winner)

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=FakeClock(),
        transport=SequencedTransport([]),
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: False,
    )

    assert result == "edit_completed"
    assert state.load_state(paths)["status"] == "edit_completed"
    assert (paths.manifests_dir / "SY1537_edit_result.json").exists()


def test_concurrent_retry_recovery_accepts_winning_wait_transition(
    tmp_path: Path, monkeypatch
) -> None:
    state, adapter, paths, request, owner = run_fixture(tmp_path)
    create_response_recorded_claim(
        state, adapter, paths, request, owner, "429"
    )
    module = load_path("retry_runner_retry_transition_cas", RUNNER_SCRIPT)
    original = module._transition
    raced = False

    def lose_after_wait_winner(run_paths, next_status, event):
        nonlocal raced
        if next_status == "edit_retry_wait_1" and not raced:
            raced = True
            original(run_paths, next_status, event)
            raise module._state.StateConflict("simulated wait transition winner")
        return original(run_paths, next_status, event)

    monkeypatch.setattr(module, "_transition", lose_after_wait_winner)
    transport = SequencedTransport(["success"])
    clock = FakeClock()

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=clock,
        transport=transport,
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: False,
    )

    assert result == "edit_completed"
    assert transport.post_count == 1
    assert clock.sleeps == [30]
    assert state.load_state(paths)["status"] == "edit_completed"


def test_concurrent_retry_recovery_accepts_winner_already_completed(
    tmp_path: Path, monkeypatch
) -> None:
    state, adapter, paths, request, owner = run_fixture(tmp_path)
    create_response_recorded_claim(
        state, adapter, paths, request, owner, "429"
    )
    loser = load_path("retry_runner_retry_completed_loser", RUNNER_SCRIPT)
    winner = load_path("retry_runner_retry_completed_winner", RUNNER_SCRIPT)
    original = loser._transition
    transport = SequencedTransport(["success"])
    winner_clock = FakeClock()
    raced = False

    def lose_after_completed_winner(run_paths, next_status, event):
        nonlocal raced
        if next_status == "edit_retry_wait_1" and not raced:
            raced = True
            original(run_paths, next_status, event)
            assert winner.run_edit_until_terminal(
                paths,
                request,
                clock=winner_clock,
                transport=transport,
                owner=owner,
                current_host_id="host-1",
                process_probe=lambda *_: False,
            ) == "edit_completed"
            raise loser._state.StateConflict("simulated completed winner")
        return original(run_paths, next_status, event)

    monkeypatch.setattr(loser, "_transition", lose_after_completed_winner)

    result = loser.run_edit_until_terminal(
        paths,
        request,
        clock=FakeClock(),
        transport=transport,
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: False,
    )

    assert result == "edit_completed"
    assert transport.post_count == 1
    assert state.load_state(paths)["status"] == "edit_completed"


@pytest.mark.parametrize("source", ["cropped", "mask", "tmp"])
def test_recovered_result_must_be_direct_child_of_edit_directory(
    tmp_path: Path, source: str
) -> None:
    state, adapter, paths, request, owner = run_fixture(tmp_path)
    create_response_recorded_claim(
        state, adapter, paths, request, owner, "success"
    )
    if source == "cropped":
        impostor = request.image
    elif source == "mask":
        impostor = request.mask
    else:
        impostor = paths.root / "tmp" / "impostor.png"
        impostor.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 20), (190, 190, 190)).save(impostor, "PNG")
    data = impostor.read_bytes()
    with Image.open(impostor) as opened:
        actual_format = opened.format
        actual_size = list(opened.size)
    claim_path = paths.root / "logs" / "edit-call-1.json"
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    claim["result"] = {
        "path": str(impostor),
        "format": actual_format,
        "size": actual_size,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest().upper(),
    }
    state.atomic_replace_json(claim_path, claim)
    module = load_path(f"retry_runner_result_location_{source}", RUNNER_SCRIPT)

    result = module.run_edit_until_terminal(
        paths,
        request,
        clock=FakeClock(),
        transport=SequencedTransport([]),
        owner=owner,
        current_host_id="host-1",
        process_probe=lambda *_: False,
    )

    assert result == "edit_unknown"
    assert state.load_state(paths)["status"] == "edit_unknown"
    assert not (paths.manifests_dir / "SY1537_edit_result.json").exists()
