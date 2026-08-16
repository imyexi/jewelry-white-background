#!/usr/bin/env python3
"""在最终确认后执行一次性水印与飞书附件追加。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable, Literal


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
_state = _load_dependency(
    "_jewelry_delivery_workflow_state", _SCRIPT_DIR / "workflow_state.py"
)
_confirmations = _load_dependency(
    "_jewelry_delivery_confirmations", _SCRIPT_DIR / "workflow_confirmations.py"
)


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal[
        "completed",
        "watermark_failed",
        "upload_failed",
        "upload_unknown",
        "already_in_progress",
        "final_invalid",
    ]
    watermark_call_count: int = 0
    upload_call_count: int = 0


class _Heartbeat:
    def __init__(self, store, owner, now: Callable[[], datetime], expected_status: str):
        self.store = store
        self.owner = owner
        self.now = now
        self.expected_status = expected_status
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join()

    def _run(self) -> None:
        while not self.stop_event.wait(5):
            try:
                claim = self.store.load()
                if claim.get("status") != self.expected_status:
                    return
                self.store.heartbeat(
                    expected_revision=int(claim["record_revision"]),
                    owner=self.owner,
                    now=self.now,
                )
            except BaseException:
                return


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


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


def _native_paths(paths):
    if isinstance(paths, _state.RunPaths):
        return paths
    return _state.RunPaths.from_root(paths.root)


def _load_state(paths) -> dict:
    return _state.load_state(Path(paths.root) / "manifests" / "workflow_state.json")


def _transition(paths, expected_status: str, next_status: str, event: str, mutate=None):
    current = _load_state(paths)
    if current.get("status") == next_status:
        return current
    return _state.transition_state(
        _native_paths(paths),
        expected_status=expected_status,
        expected_revision=int(current["state_revision"]),
        next_status=next_status,
        event=event,
        mutate=mutate,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with _filesystem_path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _filesystem_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    absolute = os.path.abspath(os.fspath(path))
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON 必须是对象")
    return payload


def _final_receipt(paths) -> tuple[Path, dict]:
    path = Path(paths.root) / "confirmations" / "final_confirmation.json"
    return path, _read_json(path)


def _validate_delivery_identity(paths, receipt_path: Path, receipt: dict) -> dict:
    state = _load_state(paths)
    required = ("run_id", "product_id", "base_token", "table_id", "record_id", "target_field_id")
    if any(receipt.get(key) != state.get(key) for key in required):
        raise ValueError("最终确认身份与状态不一致")
    if receipt.get("delivery_id") != state.get("delivery", {}).get("delivery_id"):
        raise ValueError("delivery_id 与状态不一致")
    if receipt.get("authorization") != {"watermark": True, "feishu_append": True}:
        raise ValueError("最终确认未授权交付")
    final_binding = state.get("receipts", {}).get("final")
    expected_binding = {
        "path": receipt_path.resolve().relative_to(Path(paths.root).resolve()).as_posix(),
        "sha256": _sha256(receipt_path),
    }
    if final_binding != expected_binding:
        raise ValueError("最终确认回执未绑定")
    layout = receipt.get("layout")
    if not isinstance(layout, dict):
        raise ValueError("最终确认缺少排版资产")
    layout_path = Path(paths.root) / str(layout.get("path", ""))
    if not layout_path.is_file() or _sha256(layout_path) != layout.get("sha256"):
        raise ValueError("排版资产已变化")
    return state


def _watermark_identity(paths, receipt_path: Path, receipt: dict, output_path: Path) -> dict:
    return {
        "run_id": paths.run_id,
        "product_id": paths.product_id,
        "delivery_id": receipt["delivery_id"],
        "final_confirmation_path": receipt_path.relative_to(paths.root).as_posix(),
        "final_confirmation_sha256": _sha256(receipt_path),
        "layout_path": receipt["layout"]["path"],
        "layout_sha256": receipt["layout"]["sha256"],
        "output_path": output_path.relative_to(paths.root).as_posix(),
        "output_name": output_path.name,
    }


def _upload_identity(paths, receipt_path: Path, receipt: dict, output: dict) -> dict:
    output_path = Path(output["path"])
    return {
        "run_id": paths.run_id,
        "delivery_id": receipt["delivery_id"],
        "final_confirmation_sha256": _sha256(receipt_path),
        "base_token": receipt["base_token"],
        "table_id": receipt["table_id"],
        "record_id": receipt["record_id"],
        "target_field_id": receipt["target_field_id"],
        "watermarked_path": output_path.relative_to(paths.root).as_posix(),
        "watermarked_name": output_path.name,
        "watermarked_bytes": output["bytes"],
        "watermarked_sha256": output["sha256"],
    }


def _claim_identity_matches(claim: dict, identity: dict) -> bool:
    return all(claim.get(key) == value for key, value in identity.items())


def _update_claim(store, status: str, **fields) -> dict:
    current = store.load()

    def update(payload: dict) -> None:
        payload["status"] = status
        payload.update(fields)

    return store.update(
        expected_revision=int(current["record_revision"]), updater=update
    )


def _update_claim_idempotently(store, status: str, **fields) -> dict:
    try:
        return _update_claim(store, status, **fields)
    except _state.ClaimConflict:
        latest = store.load()
        if latest.get("status") == status:
            return latest
        if latest.get("status") in {"completed", "failed", "unknown"}:
            return latest
        raise


def _watermarked_file_record(path: Path) -> dict:
    native = _filesystem_path(path)
    if not native.is_file() or native.stat().st_size <= 0:
        raise ValueError("水印输出不存在或为空")
    return {"path": str(path), "bytes": native.stat().st_size, "sha256": _sha256(path)}


def _watermark_command(layout_path: Path, output_path: Path, product_id: str) -> list[str]:
    script = (
        Path.home()
        / ".codex"
        / "skills"
        / "yuanyuan-ruyi-watermark"
        / "scripts"
        / "watermark_images.py"
    )
    return [
        sys.executable,
        str(script),
        "--input",
        str(layout_path.resolve()),
        "--output",
        str(_filesystem_path(output_path.resolve())),
        "--product-id",
        product_id,
        "--workers",
        "1",
    ]


def _upload_command(receipt: dict, output_path: Path) -> list[str]:
    return [
        "lark-cli",
        "base",
        "+record-upload-attachment",
        "--base-token",
        receipt["base_token"],
        "--table-id",
        receipt["table_id"],
        "--record-id",
        receipt["record_id"],
        "--field-id",
        receipt["target_field_id"],
        "--file",
        str(_filesystem_path(output_path.resolve())),
        "--format",
        "json",
        "--as",
        "user",
    ]


def _parse_upload_response(claim: dict) -> tuple[str, str | None]:
    stdout = claim.get("stdout")
    if not isinstance(stdout, str):
        return "unknown", None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return "unknown", None
    if not isinstance(payload, dict):
        return "unknown", None
    try:
        attachments = payload["data"]["attachments"][claim["record_id"]][
            claim["target_field_id"]
        ]
    except (KeyError, TypeError):
        attachments = None
    if isinstance(attachments, list):
        matches = [
            item
            for item in attachments
            if isinstance(item, dict) and item.get("name") == claim["watermarked_name"]
        ]
        if len(matches) == 1:
            token = matches[0].get("file_token")
            if isinstance(token, str) and token.strip():
                return "completed", token
        return "unknown", None
    explicit_failure = payload.get("success") is False or (
        isinstance(payload.get("code"), int) and payload["code"] != 0
    )
    if explicit_failure:
        return "failed", None
    return "unknown", None


def _record_subprocess_response(store, completed, command: list[str]) -> dict:
    return _update_claim(
        store,
        "response_recorded",
        command=command,
        returncode=getattr(completed, "returncode", None),
        stdout=getattr(completed, "stdout", "") or "",
        stderr=getattr(completed, "stderr", "") or "",
    )


def _recover_watermark(
    paths,
    store,
    identity: dict,
    output_path: Path,
    *,
    now: Callable[[], datetime],
    current_host_id: str,
    process_probe,
) -> str:
    claim = store.load()
    if not _claim_identity_matches(claim, identity):
        return "watermark_failed"
    status = claim.get("status")
    if status == "completed":
        try:
            actual = _watermarked_file_record(output_path)
        except (OSError, ValueError):
            return "watermark_failed"
        return "upload_ready" if claim.get("output") == actual else "watermark_failed"
    if status == "failed":
        return "watermark_failed"
    if status == "response_recorded":
        try:
            output = _watermarked_file_record(output_path)
            if claim.get("returncode") != 0:
                raise ValueError("水印命令失败")
        except (OSError, ValueError):
            _update_claim_idempotently(store, "failed", failure="recorded_watermark_failed")
            return "watermark_failed"
        updated = _update_claim_idempotently(
            store, "completed", output=output, recovery="recorded_response"
        )
        if updated.get("status") != "completed":
            return "watermark_failed"
        return "upload_ready"
    if status != "watermarking":
        return "watermark_failed"
    owner_state = store.classify_owner(
        now=now(), current_host_id=current_host_id, process_probe=process_probe
    )
    if owner_state in {"active", "unknown"}:
        return "already_in_progress"
    try:
        output = _watermarked_file_record(output_path)
    except (OSError, ValueError):
        _update_claim_idempotently(store, "failed", failure="orphaned_watermark_without_output")
        return "watermark_failed"
    updated = _update_claim_idempotently(
        store, "completed", output=output, recovery="terminated_owner"
    )
    if updated.get("status") != "completed":
        return "watermark_failed"
    return "upload_ready"


def _transition_idempotently(paths, expected_status: str, next_status: str, event: str) -> str:
    while True:
        current = _load_state(paths).get("status")
        if current == next_status or current in _state.TERMINAL_STATES:
            return str(current)
        if current != expected_status:
            return str(current)
        try:
            _transition(paths, expected_status, next_status, event)
            return next_status
        except _state.StateConflict:
            continue


def _sync_watermark_state(paths, outcome: str) -> str:
    current = _load_state(paths).get("status")
    if current == "final_confirmed":
        current = _transition_idempotently(
            paths, "final_confirmed", "watermarking", "watermark_claim_recovered"
        )
    if current in _state.TERMINAL_STATES:
        return str(current)
    if outcome == "upload_ready":
        if current == "watermarking":
            current = _transition_idempotently(
                paths, "watermarking", "upload_ready", "watermark_recovered"
            )
        if current in {"upload_ready", "uploading"}:
            return "upload_ready"
        return str(current)
    if outcome == "watermark_failed":
        if current in {"watermarking", "upload_ready", "uploading"}:
            current = _transition_idempotently(
                paths, current, "watermark_failed", "watermark_recovery_failed"
            )
        return str(current)
    return outcome


def _ensure_uploading_state(paths) -> None:
    current = _load_state(paths).get("status")
    if current == "upload_ready":
        _transition_idempotently(
            paths, "upload_ready", "uploading", "upload_claim_recovered"
        )


def _sync_upload_terminal(paths, terminal: str, event: str) -> str:
    _ensure_uploading_state(paths)
    current = _load_state(paths).get("status")
    if current == terminal:
        return terminal
    if current == "uploading":
        current = _transition_idempotently(paths, "uploading", terminal, event)
    return str(current)


def _finish_upload_from_record(paths, store) -> str:
    _ensure_uploading_state(paths)
    claim = store.load()
    decision, token = _parse_upload_response(claim)
    if decision == "completed":
        updated = _update_claim_idempotently(store, "completed", file_token=token)
        if updated.get("status") != "completed":
            return _sync_upload_terminal(paths, "upload_unknown", "conflicting_upload_receipt")
        return _sync_upload_terminal(paths, "completed", "feishu_attachment_appended")
    if decision == "failed":
        updated = _update_claim_idempotently(
            store, "failed", failure="structured_upload_failure"
        )
        if updated.get("status") != "failed":
            return _sync_upload_terminal(paths, "upload_unknown", "conflicting_upload_receipt")
        return _sync_upload_terminal(paths, "upload_failed", "feishu_append_failed")
    _update_claim_idempotently(store, "unknown", failure="upload_outcome_ambiguous")
    return _sync_upload_terminal(paths, "upload_unknown", "feishu_append_unknown")


def _recover_upload(
    paths,
    store,
    identity: dict,
    *,
    now: Callable[[], datetime],
    current_host_id: str,
    process_probe,
) -> str:
    claim = store.load()
    if not _claim_identity_matches(claim, identity):
        return _sync_upload_terminal(paths, "upload_unknown", "upload_claim_identity_mismatch")
    status = claim.get("status")
    if status == "completed":
        return _sync_upload_terminal(paths, "completed", "upload_receipt_recovered")
    if status == "failed":
        return _sync_upload_terminal(paths, "upload_failed", "upload_failure_recovered")
    if status == "unknown":
        return _sync_upload_terminal(paths, "upload_unknown", "upload_unknown_recovered")
    if status == "response_recorded":
        return _finish_upload_from_record(paths, store)
    if status != "uploading":
        return _sync_upload_terminal(paths, "upload_unknown", "invalid_upload_claim_status")
    owner_state = store.classify_owner(
        now=now(), current_host_id=current_host_id, process_probe=process_probe
    )
    if owner_state == "active":
        return "already_in_progress"
    _update_claim_idempotently(store, "unknown", failure="orphaned_upload_claim")
    return _sync_upload_terminal(paths, "upload_unknown", "orphaned_upload_claim")


def deliver_confirmed_result(
    paths,
    *,
    watermark_runner=subprocess.run,
    lark_runner=subprocess.run,
    owner=None,
    clock: Callable[[], datetime] | None = None,
    current_host_id: str | None = None,
    process_probe=None,
) -> DeliveryResult:
    """执行一次水印和一次飞书追加；已有 claim 时只恢复，不重复副作用。"""
    now = clock or _utc_now
    native_paths = _native_paths(paths)
    state = _load_state(paths)
    if state.get("status") == "completed":
        return DeliveryResult("completed")
    if state.get("status") in {"watermark_failed", "upload_failed", "upload_unknown", "final_invalid"}:
        return DeliveryResult(state["status"])

    gate = _confirmations.validate_final_confirmation(native_paths, recover_state=True)
    if not gate.delivery_allowed:
        latest = _load_state(paths)
        if latest.get("status") == "final_confirmed":
            _transition(paths, "final_confirmed", "final_invalid", "final_confirmation_invalidated")
        return DeliveryResult("final_invalid")

    receipt_path, receipt = _final_receipt(paths)
    try:
        _validate_delivery_identity(paths, receipt_path, receipt)
    except (OSError, ValueError, KeyError, TypeError):
        latest = _load_state(paths)
        if latest.get("status") == "final_confirmed":
            _transition(paths, "final_confirmed", "final_invalid", "delivery_identity_invalid")
        return DeliveryResult("final_invalid")

    owner = owner or _default_owner(now())
    current_host_id = current_host_id or owner.owner_host_id
    process_probe = process_probe or _default_process_probe
    delivery_id = receipt["delivery_id"]
    output_path = (
        Path(paths.root)
        / "watermarked"
        / f"{paths.product_id}__{paths.run_id}__{delivery_id}__watermarked.png"
    )
    watermark_store = _state.ClaimStore(
        Path(paths.manifests_dir) / f"watermark_receipt.{delivery_id}.json"
    )
    watermark_identity = _watermark_identity(paths, receipt_path, receipt, output_path)
    watermark_calls = 0

    if watermark_store.path.exists():
        watermark_status = _recover_watermark(
            paths,
            watermark_store,
            watermark_identity,
            output_path,
            now=now,
            current_host_id=current_host_id,
            process_probe=process_probe,
        )
        watermark_status = _sync_watermark_state(paths, watermark_status)
        if watermark_status != "upload_ready":
            return DeliveryResult(watermark_status)
    else:
        watermark_claim_created = True
        try:
            watermark_store.create(watermark_identity, owner, status="watermarking", now=now)
        except FileExistsError:
            watermark_claim_created = False
            watermark_status = _recover_watermark(
                paths,
                watermark_store,
                watermark_identity,
                output_path,
                now=now,
                current_host_id=current_host_id,
                process_probe=process_probe,
            )
            watermark_status = _sync_watermark_state(paths, watermark_status)
            if watermark_status != "upload_ready":
                return DeliveryResult(watermark_status)
        if watermark_claim_created:
            _transition(paths, "final_confirmed", "watermarking", "watermark_claimed")
            command = _watermark_command(
                Path(paths.root) / receipt["layout"]["path"], output_path, paths.product_id
            )
            heartbeat = _Heartbeat(watermark_store, owner, now, "watermarking")
            heartbeat.start()
            try:
                watermark_calls += 1
                completed = watermark_runner(
                    command, capture_output=True, text=True, check=False
                )
            except BaseException as exc:
                completed = subprocess.CompletedProcess(command, 1, "", repr(exc))
            finally:
                heartbeat.stop()
            _record_subprocess_response(watermark_store, completed, command)
            try:
                output = _watermarked_file_record(output_path)
                if completed.returncode != 0:
                    raise ValueError("水印命令失败")
            except (OSError, ValueError):
                updated = _update_claim_idempotently(
                    watermark_store,
                    "failed",
                    failure="watermark_command_failed",
                )
                outcome = (
                    "upload_ready"
                    if updated.get("status") == "completed"
                    else "watermark_failed"
                )
                status = _sync_watermark_state(paths, outcome)
                if status != "upload_ready":
                    return DeliveryResult(status, watermark_calls, 0)
            else:
                updated = _update_claim_idempotently(
                    watermark_store, "completed", output=output
                )
                outcome = (
                    "upload_ready"
                    if updated.get("status") == "completed"
                    else "watermark_failed"
                )
                status = _sync_watermark_state(paths, outcome)
                if status == "completed":
                    return DeliveryResult("completed", watermark_calls, 0)
                if status != "upload_ready":
                    return DeliveryResult(status, watermark_calls, 0)

    completed_watermark = watermark_store.load()
    actual_watermark = _watermarked_file_record(output_path)
    if completed_watermark.get("status") != "completed" or completed_watermark.get("output") != actual_watermark:
        status = _sync_watermark_state(paths, "watermark_failed")
        return DeliveryResult(status, watermark_calls, 0)

    upload_identity = _upload_identity(paths, receipt_path, receipt, actual_watermark)
    upload_store = _state.ClaimStore(
        Path(paths.manifests_dir) / f"upload_receipt.{delivery_id}.json"
    )
    if upload_store.path.exists():
        status = _recover_upload(
            paths,
            upload_store,
            upload_identity,
            now=now,
            current_host_id=current_host_id,
            process_probe=process_probe,
        )
        return DeliveryResult(status, watermark_calls, 0)

    try:
        upload_store.create(upload_identity, owner, status="uploading", now=now)
    except FileExistsError:
        status = _recover_upload(
            paths,
            upload_store,
            upload_identity,
            now=now,
            current_host_id=current_host_id,
            process_probe=process_probe,
        )
        return DeliveryResult(status, watermark_calls, 0)
    _transition(paths, "upload_ready", "uploading", "upload_claimed")
    command = _upload_command(receipt, output_path)
    heartbeat = _Heartbeat(upload_store, owner, now, "uploading")
    heartbeat.start()
    upload_calls = 0
    try:
        upload_calls += 1
        completed = lark_runner(command, capture_output=True, text=True, check=False)
    except BaseException as exc:
        completed = subprocess.CompletedProcess(command, None, "", repr(exc))
    finally:
        heartbeat.stop()
    _record_subprocess_response(upload_store, completed, command)
    status = _finish_upload_from_record(paths, upload_store)
    return DeliveryResult(status, watermark_calls, upload_calls)


__all__ = ["DeliveryResult", "deliver_confirmed_result"]
