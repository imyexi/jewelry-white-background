#!/usr/bin/env python3
"""珠宝白底图工作流的运行身份、原子状态和外部调用 claim。"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping


WORKFLOW_SCHEMA_VERSION = "jewelry-white-background-workflow-1.0"
PRODUCT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
WINDOWS_DEVICE_PATTERN = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE
)
OWNER_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
CLAIM_LEASE_SECONDS = 30
IN_FLIGHT_CLAIM_STATUSES = {"submitting", "watermarking", "uploading"}

TERMINAL_STATES = {
    "source_failed",
    "detection_failed",
    "geometry_failed",
    "crop_failed",
    "mask_failed",
    "mask_rejected",
    "edit_failed",
    "edit_unknown",
    "layout_failed",
    "final_rejected",
    "final_invalid",
    "watermark_failed",
    "upload_failed",
    "upload_unknown",
    "cancelled",
    "completed",
}

ALLOWED_TRANSITIONS = {
    "run_created": {"source_ready", "source_failed", "cancelled"},
    "source_ready": {"detection_image_ready", "detection_failed", "cancelled"},
    "detection_image_ready": {"geometry_ready", "geometry_failed", "cancelled"},
    "geometry_ready": {"crop_ready", "crop_failed", "cancelled"},
    "crop_ready": {"mask_ready", "mask_failed", "cancelled"},
    "mask_ready": {"awaiting_mask_confirmation", "mask_failed", "cancelled"},
    "awaiting_mask_confirmation": {
        "mask_confirmed",
        "mask_rejected",
        "mask_failed",
        "cancelled",
    },
    "mask_confirmed": {"edit_attempt_1", "edit_failed", "edit_unknown", "cancelled"},
    "edit_attempt_1": {
        "edit_completed",
        "edit_retry_wait_1",
        "edit_failed",
        "edit_unknown",
    },
    "edit_retry_wait_1": {"edit_attempt_2", "edit_unknown", "cancelled"},
    "edit_attempt_2": {
        "edit_completed",
        "edit_retry_wait_2",
        "edit_failed",
        "edit_unknown",
    },
    "edit_retry_wait_2": {"edit_attempt_3", "edit_unknown", "cancelled"},
    "edit_attempt_3": {"edit_completed", "edit_failed", "edit_unknown"},
    "edit_completed": {"layout_completed", "layout_failed", "cancelled"},
    "layout_completed": {"awaiting_final_confirmation", "layout_failed", "cancelled"},
    "awaiting_final_confirmation": {
        "final_confirmed",
        "final_rejected",
        "final_invalid",
        "cancelled",
    },
    "final_confirmed": {"watermarking", "final_invalid", "watermark_failed"},
    "watermarking": {"upload_ready", "watermark_failed"},
    "upload_ready": {"uploading", "completed", "watermark_failed", "upload_failed", "upload_unknown"},
    "uploading": {"completed", "watermark_failed", "upload_failed", "upload_unknown"},
}


class StateConflict(RuntimeError):
    """状态或 revision 与调用方预期不一致。"""


class InvalidTransition(RuntimeError):
    """状态机不允许该自动迁移。"""


class TerminalStateError(InvalidTransition):
    """终态不允许自动出边。"""


class ClaimConflict(RuntimeError):
    """claim 身份、owner 或 revision 冲突。"""


@dataclass(frozen=True)
class WorkflowIdentity:
    product_id: str
    base_token: str
    table_id: str
    record_id: str
    front_field_id: str
    target_field_id: str


@dataclass(frozen=True)
class RunPaths:
    output_root: Path
    product_id: str
    run_id: str
    root: Path
    manifests_dir: Path
    state_path: Path
    workflow_lock_path: Path

    @classmethod
    def from_root(cls, root: str | Path) -> "RunPaths":
        root = Path(root)
        return cls(
            output_root=root.parent.parent,
            product_id=root.parent.name,
            run_id=root.name,
            root=root,
            manifests_dir=root / "manifests",
            state_path=root / "manifests" / "workflow_state.json",
            workflow_lock_path=root / "manifests" / "workflow.lock",
        )


@dataclass(frozen=True)
class OwnerIdentity:
    owner_id: str
    owner_host_id: str
    owner_pid: int
    owner_process_started_at: str


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    utc_value = value.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("时间必须是 UTC RFC 3339")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_product_id(product_id: str) -> str:
    if (
        not isinstance(product_id, str)
        or PRODUCT_ID_PATTERN.fullmatch(product_id) is None
        or ".." in product_id
        or product_id.endswith(".")
        or WINDOWS_DEVICE_PATTERN.match(product_id) is not None
    ):
        raise ValueError("product_id 不是安全的 Windows 目录名")
    return product_id


def _validate_identity(identity: WorkflowIdentity) -> None:
    _validate_product_id(identity.product_id)
    for name in (
        "base_token",
        "table_id",
        "record_id",
        "front_field_id",
        "target_field_id",
    ):
        value = getattr(identity, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 不能为空")


def _uuid_hex(factory: Callable[[], uuid.UUID]) -> str:
    value = factory()
    if not isinstance(value, uuid.UUID):
        raise TypeError("uuid_factory 必须返回 UUID")
    return value.hex


def _run_id(now: datetime, run_uuid_hex: str) -> str:
    utc_value = now.astimezone(timezone.utc)
    milliseconds = utc_value.microsecond // 1000
    return f"{utc_value:%Y%m%dT%H%M%S}{milliseconds:03d}Z-{run_uuid_hex}"


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _filesystem_path(path: str | Path) -> Path:
    path = Path(path)
    if os.name != "nt":
        return path
    absolute = os.path.abspath(os.fspath(path))
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def _write_temporary(path: Path, data: bytes) -> Path:
    path = _filesystem_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return temporary


def atomic_create_bytes(path: str | Path, data: bytes) -> None:
    path = _filesystem_path(path)
    temporary = _write_temporary(path, data)
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_create_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    atomic_create_bytes(path, _json_bytes(payload))


def atomic_replace_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    path = _filesystem_path(path)
    temporary = _write_temporary(path, _json_bytes(payload))
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class InterProcessFileLock:
    """使用稳定 lock 文件实现短临界区互斥。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._descriptor: int | None = None

    def __enter__(self) -> "InterProcessFileLock":
        native_path = _filesystem_path(self.path)
        native_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(native_path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _initial_state(identity: WorkflowIdentity, run_id: str, created_at: str) -> dict[str, Any]:
    initial_transition = {"from": None, "event": "run_created", "at": created_at}
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "run_id": run_id,
        "product_id": identity.product_id,
        "base_token": identity.base_token,
        "table_id": identity.table_id,
        "record_id": identity.record_id,
        "front_field_id": identity.front_field_id,
        "target_field_id": identity.target_field_id,
        "status": "run_created",
        "state_revision": 1,
        "created_at": created_at,
        "updated_at": created_at,
        "last_transition": initial_transition,
        "history": [dict(initial_transition)],
        "receipts": {
            "mask": {"path": None, "sha256": None},
            "final": {"path": None, "sha256": None},
        },
        "delivery": {"delivery_id": None, "upload_receipt_path": None},
        "failure": None,
    }


def create_run(
    output_root: str | Path,
    identity: WorkflowIdentity,
    *,
    now: Callable[[], datetime] = _utc_now,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> RunPaths:
    _validate_identity(identity)
    output_root = Path(output_root)
    now_value = now()
    run_id = _run_id(now_value, _uuid_hex(uuid_factory))
    owner_id = _uuid_hex(uuid_factory)
    product_root = output_root / identity.product_id
    final_root = product_root / run_id
    staging_root = product_root / f".{run_id}.creating-{owner_id}"
    paths = RunPaths.from_root(final_root)

    _filesystem_path(product_root).mkdir(parents=True, exist_ok=True)
    _filesystem_path(staging_root).mkdir()
    staging_paths = RunPaths.from_root(staging_root)
    try:
        staging_paths.manifests_dir.mkdir(parents=True)
        created_at = _format_utc(now_value)
        atomic_create_json(
            staging_paths.state_path,
            _initial_state(identity, run_id, created_at),
        )
        atomic_create_bytes(staging_paths.workflow_lock_path, b"\0")
        os.rename(_filesystem_path(staging_root), _filesystem_path(final_root))
    except BaseException:
        native_staging = _filesystem_path(staging_root)
        if native_staging.exists():
            shutil.rmtree(native_staging)
        raise
    return paths


def load_state(paths: RunPaths | str | Path) -> dict[str, Any]:
    state_path = paths.state_path if isinstance(paths, RunPaths) else Path(paths)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("workflow_state.json 必须是 JSON 对象")
    return payload


def transition_state(
    paths: RunPaths,
    *,
    expected_status: str,
    expected_revision: int,
    next_status: str,
    event: str,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    with InterProcessFileLock(paths.workflow_lock_path):
        state = load_state(paths)
        actual = (state.get("status"), state.get("state_revision"))
        expected = (expected_status, expected_revision)
        if actual != expected:
            raise StateConflict(f"状态冲突：期望 {expected}，实际 {actual}")
        if expected_status in TERMINAL_STATES:
            raise TerminalStateError(f"终态 {expected_status} 不允许自动迁移")
        if next_status not in ALLOWED_TRANSITIONS.get(expected_status, set()):
            raise InvalidTransition(f"禁止迁移：{expected_status} -> {next_status}")
        previous = state["status"]
        if mutate is not None:
            mutate(state)
        transition = {
            "from": previous,
            "event": event,
            "at": _format_utc(now()),
        }
        state["status"] = next_status
        state["state_revision"] = expected_revision + 1
        state["updated_at"] = transition["at"]
        state["last_transition"] = transition
        state.setdefault("history", []).append(dict(transition))
        atomic_replace_json(paths.state_path, state)
        return state


def _validate_owner(owner: OwnerIdentity) -> None:
    if OWNER_ID_PATTERN.fullmatch(owner.owner_id) is None:
        raise ValueError("owner_id 必须是 32 位小写 UUID 十六进制")
    if not owner.owner_host_id or owner.owner_pid <= 0:
        raise ValueError("owner 主机和 PID 必须有效")
    _parse_utc(owner.owner_process_started_at)


class ClaimStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = Path(f"{self.path}.lock")

    def load(self) -> dict[str, Any]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("claim 必须是 JSON 对象")
        return payload

    def create(
        self,
        identity: Mapping[str, Any],
        owner: OwnerIdentity,
        *,
        status: str,
        now: Callable[[], datetime] = _utc_now,
    ) -> dict[str, Any]:
        _validate_owner(owner)
        if not status:
            raise ValueError("claim status 不能为空")
        reserved = {
            "status",
            "owner_id",
            "owner_host_id",
            "owner_pid",
            "owner_process_started_at",
            "claimed_at",
            "heartbeat_at",
            "lease_expires_at",
            "record_revision",
            "history",
        }
        if reserved.intersection(identity):
            raise ValueError("claim identity 包含保留字段")
        with InterProcessFileLock(self.lock_path):
            if self.path.exists():
                raise FileExistsError(self.path)
            claimed = now()
            claimed_at = _format_utc(claimed)
            payload = {
                **dict(identity),
                "status": status,
                "owner_id": owner.owner_id,
                "owner_host_id": owner.owner_host_id,
                "owner_pid": owner.owner_pid,
                "owner_process_started_at": owner.owner_process_started_at,
                "claimed_at": claimed_at,
                "heartbeat_at": claimed_at,
                "lease_expires_at": _format_utc(
                    claimed + timedelta(seconds=CLAIM_LEASE_SECONDS)
                ),
                "record_revision": 1,
                "history": [
                    {"status": status, "at": claimed_at, "record_revision": 1}
                ],
            }
            atomic_create_json(self.path, payload)
            return payload

    def update(
        self,
        *,
        expected_revision: int,
        updater: Callable[[dict[str, Any]], None],
        now: Callable[[], datetime] = _utc_now,
    ) -> dict[str, Any]:
        with InterProcessFileLock(self.lock_path):
            payload = self.load()
            if payload.get("record_revision") != expected_revision:
                raise ClaimConflict("claim record_revision 冲突")
            immutable = {
                key: payload[key]
                for key in (
                    "owner_id",
                    "owner_host_id",
                    "owner_pid",
                    "owner_process_started_at",
                    "claimed_at",
                )
            }
            previous_status = payload.get("status")
            updater(payload)
            if any(payload.get(key) != value for key, value in immutable.items()):
                raise ClaimConflict("claim owner 或首次 claim 字段不可修改")
            payload["record_revision"] = expected_revision + 1
            if payload.get("status") != previous_status:
                payload.setdefault("history", []).append(
                    {
                        "status": payload.get("status"),
                        "at": _format_utc(now()),
                        "record_revision": payload["record_revision"],
                    }
                )
            atomic_replace_json(self.path, payload)
            return payload

    def heartbeat(
        self,
        *,
        expected_revision: int,
        owner: OwnerIdentity,
        now: Callable[[], datetime] = _utc_now,
    ) -> dict[str, Any]:
        _validate_owner(owner)
        with InterProcessFileLock(self.lock_path):
            payload = self.load()
            if payload.get("status") not in IN_FLIGHT_CLAIM_STATUSES:
                return payload
            if payload.get("record_revision") != expected_revision:
                raise ClaimConflict("claim heartbeat revision 冲突")
            owner_values = (
                payload.get("owner_id"),
                payload.get("owner_host_id"),
                payload.get("owner_pid"),
                payload.get("owner_process_started_at"),
            )
            if owner_values != (
                owner.owner_id,
                owner.owner_host_id,
                owner.owner_pid,
                owner.owner_process_started_at,
            ):
                raise ClaimConflict("claim heartbeat owner 冲突")
            heartbeat = now()
            payload["heartbeat_at"] = _format_utc(heartbeat)
            payload["lease_expires_at"] = _format_utc(
                heartbeat + timedelta(seconds=CLAIM_LEASE_SECONDS)
            )
            payload["record_revision"] = expected_revision + 1
            atomic_replace_json(self.path, payload)
            return payload

    def classify_owner(
        self,
        *,
        now: datetime,
        current_host_id: str,
        process_probe: Callable[[int, str], bool | None],
    ) -> Literal["active", "terminated", "unknown"]:
        payload = self.load()
        heartbeat_recent = (
            now.astimezone(timezone.utc) - _parse_utc(payload["heartbeat_at"])
        ) <= timedelta(seconds=CLAIM_LEASE_SECONDS)
        if payload.get("owner_host_id") != current_host_id:
            return "active" if heartbeat_recent else "unknown"
        try:
            process_state = process_probe(
                int(payload["owner_pid"]), payload["owner_process_started_at"]
            )
        except (OSError, ValueError):
            process_state = None
        if process_state is True or heartbeat_recent:
            return "active"
        if process_state is False:
            return "terminated"
        return "unknown"


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CLAIM_LEASE_SECONDS",
    "ClaimConflict",
    "ClaimStore",
    "InterProcessFileLock",
    "InvalidTransition",
    "OwnerIdentity",
    "RunPaths",
    "StateConflict",
    "TERMINAL_STATES",
    "TerminalStateError",
    "WorkflowIdentity",
    "atomic_create_bytes",
    "atomic_create_json",
    "atomic_replace_json",
    "create_run",
    "load_state",
    "transition_state",
]
