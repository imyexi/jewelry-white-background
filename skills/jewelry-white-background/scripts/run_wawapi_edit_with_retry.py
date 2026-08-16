#!/usr/bin/env python3
"""以持久化 call slot 驱动 Wawapi 背景编辑。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable
from typing import Literal

from PIL import Image


def _load_dependency(name: str, path: Path) -> ModuleType:
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载依赖：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_SCRIPT_DIR = Path(__file__).resolve().parent
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_state = _load_dependency(
    "_jewelry_retry_workflow_state", _SCRIPT_DIR / "workflow_state.py"
)
_adapter = _load_dependency(
    "_jewelry_retry_wawapi_adapter",
    _REPOSITORY_ROOT / "scripts" / "yuan_image_generation_adapter.py",
)


@dataclass(frozen=True)
class AttemptDecision:
    action: Literal["complete", "retry", "failed", "unknown"]
    delay_seconds: int | None = None


def classify_attempt(attempt, slot: int) -> AttemptDecision:
    if attempt.has_valid_result:
        return AttemptDecision("complete")

    has_remote_id = attempt.task_or_job_id is not None
    retryable_429 = attempt.http_status == 429 and not has_remote_id
    retryable_rejected_5xx = (
        attempt.http_status is not None
        and 500 <= attempt.http_status <= 599
        and attempt.rejection_evidence == "request_not_accepted"
        and not has_remote_id
    )
    if retryable_429 or retryable_rejected_5xx:
        if slot < 3:
            return AttemptDecision("retry", (30, 90)[slot - 1])
        return AttemptDecision("failed")

    if has_remote_id:
        return AttemptDecision("unknown")
    if attempt.http_status is not None and 500 <= attempt.http_status <= 599:
        return AttemptDecision("unknown")
    if attempt.request_may_have_been_sent and not attempt.definitive_response:
        return AttemptDecision("unknown")
    return AttemptDecision("failed")


class _SystemClock:
    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def sleep(seconds: int) -> None:
        time.sleep(seconds)


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("时间必须是 UTC RFC 3339")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _default_owner(now: datetime):
    return _state.OwnerIdentity(
        owner_id=uuid.uuid4().hex,
        owner_host_id=socket.gethostname(),
        owner_pid=os.getpid(),
        owner_process_started_at=_format_utc(now),
    )


def _default_process_probe(pid: int, _started_at: str) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return None
    return True


def _state_payload(paths) -> dict:
    return _state.load_state(paths.state_path)


def _native_paths(paths):
    if isinstance(paths, _state.RunPaths):
        return paths
    return _state.RunPaths.from_root(paths.root)


def _transition(paths, next_status: str, event: str) -> dict:
    current = _state_payload(paths)
    if current.get("status") == next_status:
        return current
    return _state.transition_state(
        _native_paths(paths),
        expected_status=current["status"],
        expected_revision=current["state_revision"],
        next_status=next_status,
        event=event,
    )


def _transition_terminal(paths, terminal: str, event: str) -> str:
    current = _state_payload(paths)
    if current.get("status") == terminal:
        return terminal
    if current.get("status") in _state.TERMINAL_STATES:
        return str(current["status"])
    try:
        _transition(paths, terminal, event)
    except _state.StateConflict:
        latest = _state_payload(paths)
        if latest.get("status") == terminal:
            return terminal
        if latest.get("status") in _state.TERMINAL_STATES:
            return str(latest["status"])
        raise
    return terminal


def _redact(value: str) -> str:
    redacted = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;\"']+",
        r"\1<redacted>",
        value,
    )
    return re.sub(
        r'(?i)("?(?:api[_-]?key|token|secret)"?\s*[:=]\s*")[^"]+(\")',
        r"\1<redacted>\2",
        redacted,
    )


def _decision_reason(attempt, decision: AttemptDecision, slot: int) -> str:
    if decision.action == "complete":
        return "valid_result"
    if decision.action == "retry":
        if attempt.http_status == 429:
            return "http_429_not_accepted"
        return "provider_confirmed_request_not_accepted"
    if decision.action == "unknown":
        if attempt.task_or_job_id is not None:
            return "remote_task_or_job_exists"
        if attempt.http_status is not None and 500 <= attempt.http_status <= 599:
            return "server_result_not_provably_unaccepted"
        return "request_outcome_not_determinable"
    if slot == 3 and (
        attempt.http_status == 429
        or attempt.rejection_evidence == "request_not_accepted"
    ):
        return "retry_budget_exhausted"
    return "definitive_non_retryable_failure"


def _result_record(attempt) -> dict | None:
    if not attempt.has_valid_result:
        return None
    return {
        "path": str(attempt.result_path),
        "format": attempt.result_format,
        "size": list(attempt.result_size) if attempt.result_size is not None else None,
        "bytes": attempt.result_bytes,
        "sha256": attempt.result_sha256,
    }


class _Heartbeat:
    def __init__(self, store, owner, now: Callable[[], datetime]):
        self.store = store
        self.owner = owner
        self.now = now
        self.stop_event = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join()

    def _run(self) -> None:
        while not self.stop_event.wait(5):
            try:
                payload = self.store.load()
                if payload.get("status") != "submitting":
                    return
                self.store.heartbeat(
                    expected_revision=payload["record_revision"],
                    owner=self.owner,
                    now=self.now,
                )
            except BaseException as exc:
                self.error = exc
                return


def _record_attempt(
    store,
    attempt,
    decision: AttemptDecision,
    *,
    slot: int,
    started_at: datetime,
    finished_at: datetime,
    reason_override: str | None = None,
) -> dict:
    payload = store.load()

    def record_response(claim: dict) -> None:
        claim.update(
            {
                "status": "response_recorded",
                "started_at": _format_utc(started_at),
                "finished_at": _format_utc(finished_at),
                "command": list(attempt.command),
                "returncode": attempt.returncode,
                "stdout": _redact(attempt.stdout),
                "stderr": _redact(attempt.stderr),
                "response_body": attempt.response_payload,
                "http_status": attempt.http_status,
                "request_may_have_been_sent": attempt.request_may_have_been_sent,
                "task_or_job_id": attempt.task_or_job_id,
                "rejection_evidence": attempt.rejection_evidence,
                "local_error": attempt.local_error,
                "actual_request_identity": attempt.request_identity,
                "actual_request_identity_sha256": attempt.request_identity_sha256,
                "result": _result_record(attempt),
            }
        )

    payload = store.update(
        expected_revision=payload["record_revision"],
        updater=record_response,
        now=lambda: finished_at,
    )

    return _record_decision(
        store,
        payload,
        attempt,
        decision,
        slot=slot,
        finished_at=finished_at,
        reason_override=reason_override,
    )


def _record_decision(
    store,
    payload: dict,
    attempt,
    decision: AttemptDecision,
    *,
    slot: int,
    finished_at: datetime,
    reason_override: str | None = None,
) -> dict:
    def update_decision(claim: dict) -> None:
        claim["result_status"] = decision.action
        claim["retryable"] = decision.action == "retry"
        claim["retry_reason"] = reason_override or _decision_reason(
            attempt, decision, slot
        )
        claim["next_retry_delay_seconds"] = decision.delay_seconds
        claim["retry_not_before"] = (
            _format_utc(finished_at + timedelta(seconds=decision.delay_seconds))
            if decision.delay_seconds is not None
            else None
        )

    return store.update(
        expected_revision=payload["record_revision"],
        updater=update_decision,
        now=lambda: finished_at,
    )


def _attempt_from_claim(claim: dict):
    result = claim.get("result")
    result_size = None
    if isinstance(result, dict) and isinstance(result.get("size"), list):
        result_size = tuple(result["size"])
    return _adapter.BackgroundEditAttempt(
        request_identity=claim.get("actual_request_identity")
        or claim["request_identity"],
        request_identity_sha256=claim.get("actual_request_identity_sha256")
        or claim["request_identity_sha256"],
        command=tuple(claim.get("command") or ()),
        returncode=claim.get("returncode"),
        stdout=claim.get("stdout") or "",
        stderr=claim.get("stderr") or "",
        http_status=claim.get("http_status"),
        request_may_have_been_sent=bool(
            claim.get("request_may_have_been_sent", True)
        ),
        task_or_job_id=claim.get("task_or_job_id"),
        rejection_evidence=claim.get("rejection_evidence"),
        response_payload=claim.get("response_body"),
        local_error=claim.get("local_error"),
        result_path=(
            Path(result["path"])
            if isinstance(result, dict) and isinstance(result.get("path"), str)
            else None
        ),
        result_format=result.get("format") if isinstance(result, dict) else None,
        result_size=result_size,
        result_bytes=result.get("bytes") if isinstance(result, dict) else None,
        result_sha256=result.get("sha256") if isinstance(result, dict) else None,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _identity_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _claim_identity_matches(claim: dict, identity, *, require_actual: bool) -> bool:
    claimed = claim.get("request_identity")
    claimed_sha256 = claim.get("request_identity_sha256")
    if not isinstance(claimed, dict) or not isinstance(claimed_sha256, str):
        return False
    if _identity_sha256(claimed) != claimed_sha256:
        return False
    if claimed != identity.as_dict() or claimed_sha256 != identity.sha256:
        return False
    if not require_actual:
        return True
    actual = claim.get("actual_request_identity")
    actual_sha256 = claim.get("actual_request_identity_sha256")
    return (
        isinstance(actual, dict)
        and isinstance(actual_sha256, str)
        and _identity_sha256(actual) == actual_sha256
        and actual == claimed
        and actual_sha256 == claimed_sha256
    )


def _manifest_payload(paths, claim: dict) -> dict:
    result = claim.get("result")
    if not isinstance(result, dict):
        raise ValueError("call 记录缺少有效 Edit 结果")
    result_path = Path(result["path"])
    if not result_path.is_file() or result_path.stat().st_size == 0:
        raise ValueError("Edit 结果文件不存在或为空")
    actual_sha256 = _sha256(result_path)
    if actual_sha256 != result.get("sha256"):
        raise ValueError("Edit 结果文件摘要与 call 记录不一致")
    actual_bytes = result_path.stat().st_size
    suffix_formats = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
    expected_format = suffix_formats.get(result_path.suffix.lower())
    if expected_format is None:
        raise ValueError("Edit 结果后缀必须是 PNG 或 JPEG")
    try:
        with Image.open(result_path) as opened:
            actual_format = opened.format
            actual_size = opened.size
            opened.verify()
        with Image.open(result_path) as opened:
            opened.load()
    except (OSError, ValueError) as exc:
        raise ValueError("Edit 结果不是可完整解码的图片") from exc
    if (
        actual_format != expected_format
        or actual_format != result.get("format")
        or list(actual_size) != result.get("size")
        or actual_bytes != result.get("bytes")
        or min(actual_size) < 16
        or actual_size[0] * actual_size[1] > 20_000_000
    ):
        raise ValueError("Edit 结果磁盘身份与 call 记录不一致")
    try:
        resolved_result = result_path.resolve(strict=True)
        resolved_root = Path(paths.root).resolve(strict=True)
        resolved_edit = (Path(paths.root) / "edit").resolve(strict=True)
        if resolved_result.parent != resolved_edit:
            raise ValueError("Edit 结果必须是 edit 目录的直接子文件")
        relative_path = resolved_result.relative_to(resolved_root)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError("Edit 结果必须位于当前运行的 edit 目录") from exc
    return {
        "schema_version": "jewelry-edit-result-1.0",
        "run_id": paths.run_id,
        "product_id": paths.product_id,
        "call_slot": claim["call_slot"],
        "request_identity": claim["request_identity"],
        "request_identity_sha256": claim["request_identity_sha256"],
        "result": {
            **result,
            "path": relative_path.as_posix(),
            "format": actual_format,
            "size": list(actual_size),
            "bytes": actual_bytes,
            "sha256": actual_sha256,
        },
    }


def _publish_result_manifest(paths, claim: dict) -> Path:
    manifest_path = paths.manifests_dir / f"{paths.product_id}_edit_result.json"
    payload = _manifest_payload(paths, claim)
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing != payload:
            raise FileExistsError("已有 Edit Result Manifest 与当前结果不一致")
        return manifest_path
    try:
        _state.atomic_create_json(manifest_path, payload)
    except FileExistsError:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing != payload:
            raise
    return manifest_path


def _wait_until(clock, retry_not_before: str) -> None:
    remaining = (_parse_utc(retry_not_before) - clock.now()).total_seconds()
    if remaining > 0:
        clock.sleep(math.ceil(remaining))


def _apply_recorded_decision(paths, store, claim: dict, clock) -> str | None:
    action = claim.get("result_status")
    slot = int(claim["call_slot"])
    attempt_status = f"edit_attempt_{slot}"
    current_status = _state_payload(paths)["status"]

    if action == "complete":
        try:
            _publish_result_manifest(paths, claim)
        except (OSError, ValueError, json.JSONDecodeError):
            return _transition_terminal(
                paths, "edit_unknown", "edit_result_manifest_recovery_failed"
            )
        return _transition_terminal(paths, "edit_completed", "wawapi_edit_completed")
    if action == "failed":
        return _transition_terminal(paths, "edit_failed", "wawapi_edit_failed")
    if action == "unknown":
        return _transition_terminal(paths, "edit_unknown", "wawapi_edit_unknown")
    if action != "retry" or slot >= 3:
        return _transition_terminal(paths, "edit_unknown", "invalid_edit_call_record")

    wait_status = f"edit_retry_wait_{slot}"
    if current_status == attempt_status:
        try:
            _transition(paths, wait_status, "wawapi_edit_retry_scheduled")
        except _state.StateConflict:
            current_status = _state_payload(paths)["status"]
    progress_states = {wait_status}
    if slot == 1:
        progress_states.update(
            {"edit_attempt_2", "edit_retry_wait_2", "edit_attempt_3"}
        )
    elif slot == 2:
        progress_states.add("edit_attempt_3")
    current_status = _state_payload(paths)["status"]
    if current_status == "edit_completed":
        return "edit_completed"
    if current_status in _state.TERMINAL_STATES:
        return current_status
    if current_status not in progress_states:
        return _transition_terminal(paths, "edit_unknown", "edit_retry_state_mismatch")
    retry_not_before = claim.get("retry_not_before")
    if not isinstance(retry_not_before, str):
        return _transition_terminal(paths, "edit_unknown", "missing_retry_deadline")
    _wait_until(clock, retry_not_before)
    return None


def _existing_claim_result(
    paths,
    store,
    *,
    identity,
    clock,
    current_host_id: str,
    process_probe,
) -> str | None:
    claim = store.load()
    if not _claim_identity_matches(claim, identity, require_actual=False):
        return _transition_terminal(paths, "edit_unknown", "edit_request_identity_mismatch")
    if claim.get("status") == "submitting":
        owner_state = store.classify_owner(
            now=clock.now(),
            current_host_id=current_host_id,
            process_probe=process_probe,
        )
        if owner_state in {"active", "unknown"}:
            return "already_in_progress"
        return _transition_terminal(paths, "edit_unknown", "orphaned_edit_call")
    if claim.get("status") != "response_recorded":
        return _transition_terminal(paths, "edit_unknown", "invalid_edit_call_status")
    if not _claim_identity_matches(claim, identity, require_actual=True):
        return _transition_terminal(paths, "edit_unknown", "recorded_request_identity_mismatch")
    if claim.get("result_status") is None:
        try:
            attempt = _attempt_from_claim(claim)
            decision = classify_attempt(attempt, int(claim["call_slot"]))
            claim = _record_decision(
                store,
                claim,
                attempt,
                decision,
                slot=int(claim["call_slot"]),
                finished_at=_parse_utc(claim["finished_at"]),
            )
        except _state.ClaimConflict:
            claim = store.load()
            if claim.get("status") != "response_recorded" or claim.get(
                "result_status"
            ) not in {"complete", "retry", "failed", "unknown"}:
                return _transition_terminal(
                    paths, "edit_unknown", "conflicting_recorded_edit_response"
                )
        except (KeyError, TypeError, ValueError):
            return _transition_terminal(
                paths, "edit_unknown", "invalid_recorded_edit_response"
            )
    return _apply_recorded_decision(paths, store, claim, clock)


def run_edit_until_terminal(
    paths,
    request,
    *,
    clock=None,
    transport=None,
    owner=None,
    current_host_id: str | None = None,
    process_probe=None,
) -> str:
    clock = clock or _SystemClock()
    owner = owner or _default_owner(clock.now())
    current_host_id = current_host_id or owner.owner_host_id
    process_probe = process_probe or _default_process_probe
    transport = transport or __import__("subprocess").run

    logs_dir = Path(paths.root) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    try:
        identity = _adapter.build_background_edit_request_identity(request)
    except (OSError, ValueError):
        existing_claims = sorted(logs_dir.glob("edit-call-*.json"))
        if not existing_claims:
            return _transition_terminal(
                paths, "edit_failed", "edit_request_local_gate_failed"
            )
        latest = _state.ClaimStore(existing_claims[-1])
        claim = latest.load()
        if claim.get("status") == "submitting":
            owner_state = latest.classify_owner(
                now=clock.now(),
                current_host_id=current_host_id,
                process_probe=process_probe,
            )
            if owner_state in {"active", "unknown"}:
                return "already_in_progress"
        return _transition_terminal(
            paths, "edit_unknown", "edit_request_identity_unavailable"
        )

    for slot in range(1, 4):
        store = _state.ClaimStore(logs_dir / f"edit-call-{slot}.json")
        if store.path.exists():
            existing_result = _existing_claim_result(
                paths,
                store,
                identity=identity,
                clock=clock,
                current_host_id=current_host_id,
                process_probe=process_probe,
            )
            if existing_result is not None:
                return existing_result
            continue

        expected_status = "mask_confirmed" if slot == 1 else f"edit_retry_wait_{slot - 1}"
        current = _state_payload(paths)
        if current.get("status") != expected_status:
            return _transition_terminal(paths, "edit_unknown", "missing_edit_call_claim")

        try:
            store.create(
                {
                    "call_slot": slot,
                    "request_identity": identity.as_dict(),
                    "request_identity_sha256": identity.sha256,
                },
                owner,
                status="submitting",
                now=clock.now,
            )
        except FileExistsError:
            return _existing_claim_result(
                paths,
                store,
                identity=identity,
                clock=clock,
                current_host_id=current_host_id,
                process_probe=process_probe,
            ) or "already_in_progress"

        _transition(paths, f"edit_attempt_{slot}", "edit_call_slot_claimed")
        started_at = clock.now()
        heartbeat = _Heartbeat(store, owner, clock.now)
        heartbeat.start()
        try:
            attempt = _adapter.edit_background_single_attempt(
                request, transport=transport
            )
        finally:
            heartbeat.stop()
        finished_at = clock.now()
        identity_matches = (
            attempt.request_identity_sha256 == identity.sha256
            and attempt.request_identity == identity.as_dict()
        )
        decision = (
            classify_attempt(attempt, slot)
            if identity_matches
            else AttemptDecision("unknown")
        )
        claim = _record_attempt(
            store,
            attempt,
            decision,
            slot=slot,
            started_at=started_at,
            finished_at=finished_at,
            reason_override=(
                None
                if identity_matches
                else "request_identity_changed_after_claim"
            ),
        )
        terminal = _apply_recorded_decision(paths, store, claim, clock)
        if terminal is not None:
            return terminal

    return _transition_terminal(paths, "edit_failed", "edit_retry_budget_exhausted")


__all__ = ["AttemptDecision", "classify_attempt", "run_edit_until_terminal"]
