#!/usr/bin/env python3
"""珠宝白底图流程的唯一状态驱动编排器。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence


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
_state = _load_dependency("_jewelry_orchestrator_state", _SCRIPT_DIR / "workflow_state.py")
_source = _load_dependency("_jewelry_orchestrator_source", _SCRIPT_DIR / "prepare_source_context.py")
_detection = _load_dependency("_jewelry_orchestrator_detection", _SCRIPT_DIR / "prepare_mask_detection_images.py")
_crop = _load_dependency("_jewelry_orchestrator_crop", _SCRIPT_DIR / "create_geometry_crop.py")
_mask = _load_dependency("_jewelry_orchestrator_mask", _SCRIPT_DIR / "create_background_edit_mask.py")
_confirmations = _load_dependency("_jewelry_orchestrator_confirmations", _SCRIPT_DIR / "workflow_confirmations.py")
_retry = _load_dependency("_jewelry_orchestrator_retry", _SCRIPT_DIR / "run_wawapi_edit_with_retry.py")
_layout = _load_dependency("_jewelry_orchestrator_layout", _SCRIPT_DIR / "layout_generated_result.py")
_delivery = _load_dependency("_jewelry_orchestrator_delivery", _SCRIPT_DIR / "deliver_confirmed_result.py")
_adapter = _load_dependency("_jewelry_orchestrator_adapter", _REPOSITORY_ROOT / "scripts" / "yuan_image_generation_adapter.py")
_prompt = _load_dependency("_jewelry_orchestrator_prompt", _SCRIPT_DIR / "build_white_background_prompt.py")


BACKGROUND_EDIT_PROMPT_PLAN = {
    "schema_version": "3.0",
    "workflow_mode": "background_only_edit",
    "product_id": "background-edit",
    "front_image": "front.png",
    "detail_images": [],
    "structure": {"source_image": "front.png"},
}


HUMAN_HOOK_STATES = {"awaiting_mask_confirmation", "awaiting_final_confirmation"}
DELIVERY_PROGRESS_STATES = {"final_confirmed", "watermarking", "upload_ready", "uploading"}
EDIT_PROGRESS_STATES = {
    "mask_confirmed",
    "edit_attempt_1",
    "edit_retry_wait_1",
    "edit_attempt_2",
    "edit_retry_wait_2",
    "edit_attempt_3",
}
DIRECT_STAGE_STATES = {
    "run_created",
    "source_ready",
    "detection_image_ready",
    "geometry_ready",
    "crop_ready",
    "mask_ready",
    "edit_completed",
    "layout_completed",
}


@dataclass(frozen=True)
class WorkflowResult:
    run_root: Path
    status: str
    state_revision: int


@dataclass(frozen=True)
class CreateRequest:
    output_root: Path
    product_id: str
    base_token: str
    table_id: str
    record_id: str
    front_field_id: str
    target_field_id: str
    front_file_token: str
    front_file_name: str
    raw_source_path: Path
    geometry_path: Path
    base_url: str
    model: str


def _paths(run_root: str | Path):
    return _state.RunPaths.from_root(Path(run_root))


def _result(paths) -> WorkflowResult:
    payload = _state.load_state(paths.state_path)
    return WorkflowResult(paths.root, str(payload["status"]), int(payload["state_revision"]))


def _transition(paths, expected: str, next_status: str, event: str) -> None:
    payload = _state.load_state(paths.state_path)
    _state.transition_state(
        paths,
        expected_status=expected,
        expected_revision=int(payload["state_revision"]),
        next_status=next_status,
        event=event,
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象")
    return payload


def _raw_source_name(product_id: str) -> str:
    return f"source/{product_id}_raw.bin"


def _detection_manifest_name(product_id: str) -> str:
    return f"logs/{product_id}_detection_manifest.json"


def _vision_geometry_name(product_id: str) -> str:
    return f"geometry/{product_id}_vision_geometry.json"


class DefaultWorkflowDependencies:
    """按固定运行目录约定调用各职责单一的阶段脚本。"""

    def _request(self, paths) -> dict[str, Any]:
        return _read_json(paths.manifests_dir / "workflow_request.json")

    def run_stage(self, paths, status: str) -> str | None:
        if status == "run_created":
            self._prepare_source(paths)
        elif status == "source_ready":
            self._prepare_detection(paths)
        elif status == "detection_image_ready":
            self._publish_geometry(paths)
        elif status == "geometry_ready":
            self._create_crop(paths)
        elif status == "crop_ready":
            self._create_mask(paths)
        elif status == "mask_ready":
            self._create_mask_review(paths)
        elif status in EDIT_PROGRESS_STATES:
            return self._run_edit(paths)
        elif status == "edit_completed":
            self._create_layout(paths)
        elif status == "layout_completed":
            self._create_final_review(paths)
        elif status in DELIVERY_PROGRESS_STATES:
            return _delivery.deliver_confirmed_result(paths).status
        else:
            raise ValueError(f"状态 {status} 没有可执行阶段")

    def _prepare_source(self, paths) -> None:
        request = self._request(paths)
        raw = paths.root / _raw_source_name(paths.product_id)
        _state.atomic_create_bytes(raw, Path(request["raw_source_path"]).read_bytes())
        canonical = paths.root / "source" / f"{paths.product_id}_original.png"
        identity = _source.normalize_source(raw, canonical)
        target = _state.WorkflowIdentity(
            product_id=paths.product_id,
            base_token=request["base_token"],
            table_id=request["table_id"],
            record_id=request["record_id"],
            front_field_id=request["front_field_id"],
            target_field_id=request["target_field_id"],
        )
        attachment = _source.AttachmentIdentity(
            request["front_file_token"], request["front_file_name"]
        )
        _source.build_product_context(paths, target, attachment, identity)
        _transition(paths, "run_created", "source_ready", "source_prepared")

    def _prepare_detection(self, paths) -> None:
        root = paths.root
        _detection.write_detection_images(
            root / "source" / f"{paths.product_id}_original.png",
            root / "detection" / f"{paths.product_id}_geometry_detection.png",
            root / "detection" / f"{paths.product_id}_local_detection.png",
            root / _detection_manifest_name(paths.product_id),
            run_root=root,
        )
        _transition(paths, "source_ready", "detection_image_ready", "detection_images_prepared")

    def _publish_geometry(self, paths) -> None:
        request = self._request(paths)
        target = paths.root / _vision_geometry_name(paths.product_id)
        _state.atomic_create_bytes(target, Path(request["geometry_path"]).read_bytes())
        _transition(paths, "detection_image_ready", "geometry_ready", "vision_geometry_published")

    def _create_crop(self, paths) -> None:
        root = paths.root
        outputs = _crop.CropOutputPaths(
            run_root=root,
            product_id=paths.product_id,
            cropped_original_path=root / "cropped" / f"{paths.product_id}_original.png",
            cropped_detection_path=root / "cropped" / f"{paths.product_id}_detection.png",
            cropped_local_detection_path=root / "cropped" / f"{paths.product_id}_local_detection.png",
            candidate_alpha_path=root / "cropped" / f"{paths.product_id}_geometry_candidate_alpha.png",
            cropped_geometry_path=root / "geometry" / f"{paths.product_id}_cropped_geometry.json",
            crop_manifest_path=root / "manifests" / f"{paths.product_id}_crop_manifest.json",
        )
        _crop.create_geometry_crop_assets(
            root / "source" / f"{paths.product_id}_original.png",
            root / "detection" / f"{paths.product_id}_geometry_detection.png",
            root / "detection" / f"{paths.product_id}_local_detection.png",
            root / _detection_manifest_name(paths.product_id),
            root / _vision_geometry_name(paths.product_id),
            outputs,
        )
        _transition(paths, "geometry_ready", "crop_ready", "geometry_crop_created")

    def _create_mask(self, paths) -> None:
        root = paths.root
        outputs = _mask.MaskOutputPaths(
            run_root=root,
            mask_path=root / "mask" / f"{paths.product_id}_product-protection-mask.png",
            overlay_path=root / "mask" / f"{paths.product_id}_editable-overlay.png",
            report_path=root / "logs" / f"{paths.product_id}_mask-assessment.draft.json",
        )
        _mask.create_background_edit_assets(
            root / "cropped" / f"{paths.product_id}_original.png",
            root / "cropped" / f"{paths.product_id}_detection.png",
            root / "cropped" / f"{paths.product_id}_local_detection.png",
            root / "cropped" / f"{paths.product_id}_geometry_candidate_alpha.png",
            root / "geometry" / f"{paths.product_id}_cropped_geometry.json",
            root / "manifests" / f"{paths.product_id}_crop_manifest.json",
            outputs,
        )
        _transition(paths, "crop_ready", "mask_ready", "mask_assets_created")

    def _create_mask_review(self, paths) -> None:
        root = paths.root
        context = _read_json(root / "product_context.json")
        assets = _confirmations.MaskReviewAssets(
            product_context_path=root / "product_context.json",
            raw_source_path=root / context["source_identity"]["raw_source_path"],
            source_original_path=root / context["source_identity"]["canonical_source_path"],
            geometry_detection_path=root / "detection" / f"{paths.product_id}_geometry_detection.png",
            local_detection_path=root / "detection" / f"{paths.product_id}_local_detection.png",
            source_geometry_path=root / _vision_geometry_name(paths.product_id),
            crop_manifest_path=root / "manifests" / f"{paths.product_id}_crop_manifest.json",
            cropped_original_path=root / "cropped" / f"{paths.product_id}_original.png",
            cropped_detection_path=root / "cropped" / f"{paths.product_id}_detection.png",
            cropped_local_detection_path=root / "cropped" / f"{paths.product_id}_local_detection.png",
            cropped_geometry_path=root / "geometry" / f"{paths.product_id}_cropped_geometry.json",
            candidate_alpha_path=root / "cropped" / f"{paths.product_id}_geometry_candidate_alpha.png",
            mask_path=root / "mask" / f"{paths.product_id}_product-protection-mask.png",
            overlay_path=root / "mask" / f"{paths.product_id}_editable-overlay.png",
            draft_assessment_path=root / "logs" / f"{paths.product_id}_mask-assessment.draft.json",
        )
        _confirmations.create_mask_review_bundle(paths, assets)

    def _run_edit(self, paths) -> str:
        gate = _confirmations.validate_mask_confirmation(paths, recover_state=True)
        if not gate.automatic_wawapi_edit_allowed:
            return self._stop_invalid_edit(paths, "mask_confirmation_invalid_before_edit")
        request = self._request(paths)
        root = paths.root
        prompt_path = root / "logs" / f"{paths.product_id}_prompt.txt"
        prompt = _prompt.build_prompt(BACKGROUND_EDIT_PROMPT_PLAN).rstrip("\n") + "\n"
        prompt_bytes = prompt.encode("utf-8")
        if prompt_path.exists():
            if prompt_path.read_bytes() != prompt_bytes:
                return self._stop_invalid_edit(paths, "prompt_identity_changed")
        else:
            _state.atomic_create_bytes(prompt_path, prompt_bytes)
        edit_request = _adapter.BackgroundEditRequest(
            prompt=prompt_path.read_text(encoding="utf-8"),
            image=root / "cropped" / f"{paths.product_id}_original.png",
            mask=root / "mask" / f"{paths.product_id}_product-protection-mask.png",
            output_path=root / "edit" / f"{paths.product_id}_generated.png",
            base_url=request["base_url"],
            model=request["model"],
        )
        return _retry.run_edit_until_terminal(paths, edit_request)

    @staticmethod
    def _stop_invalid_edit(paths, event: str) -> str:
        current = _state.load_state(paths.state_path)
        status = str(current["status"])
        terminal = "edit_failed" if status == "mask_confirmed" else "edit_unknown"
        _transition(paths, status, terminal, event)
        return terminal

    def _create_layout(self, paths) -> None:
        root = paths.root
        manifest = _read_json(root / "manifests" / f"{paths.product_id}_edit_result.json")
        generated = root / manifest["result"]["path"]
        _layout.layout_generated_result(
            generated,
            root / "layout" / f"{paths.product_id}_3x4_60pct.png",
            root / "manifests" / f"{paths.product_id}_layout_manifest.json",
        )
        _transition(paths, "edit_completed", "layout_completed", "generated_result_laid_out")

    def _create_final_review(self, paths) -> None:
        root = paths.root
        assets = _confirmations.FinalReviewAssets(
            mask_confirmation_path=root / "confirmations" / "mask_confirmation.json",
            mask_gate_path=root / "logs" / f"{paths.product_id}_mask-gate.confirmed.json",
            edit_result_manifest_path=root / "manifests" / f"{paths.product_id}_edit_result.json",
            layout_path=root / "layout" / f"{paths.product_id}_3x4_60pct.png",
            layout_manifest_path=root / "manifests" / f"{paths.product_id}_layout_manifest.json",
        )
        _confirmations.create_final_review_bundle(paths, assets)


class WhiteBackgroundWorkflow:
    def __init__(self, dependencies=None):
        self.dependencies = dependencies or DefaultWorkflowDependencies()

    def create(self, request: CreateRequest) -> WorkflowResult:
        raw_source_path = request.raw_source_path.resolve(strict=True)
        geometry_path = request.geometry_path.resolve(strict=True)
        if not raw_source_path.is_file() or not geometry_path.is_file():
            raise ValueError("源图和几何输入必须是普通文件")
        identity = _state.WorkflowIdentity(
            product_id=request.product_id,
            base_token=request.base_token,
            table_id=request.table_id,
            record_id=request.record_id,
            front_field_id=request.front_field_id,
            target_field_id=request.target_field_id,
        )
        paths = _state.create_run(request.output_root, identity)
        payload = {
            "schema_version": "white-background-workflow-request-1.0",
            **{name: getattr(request, name) for name in (
                "product_id", "base_token", "table_id", "record_id", "front_field_id",
                "target_field_id", "front_file_token", "front_file_name", "base_url", "model"
            )},
            "raw_source_path": str(raw_source_path),
            "geometry_path": str(geometry_path),
        }
        _state.atomic_create_json(paths.manifests_dir / "workflow_request.json", payload)
        return _result(paths)

    def resume(self, run_root: str | Path) -> WorkflowResult:
        paths = _paths(run_root)
        current = _state.load_state(paths.state_path)
        status = str(current["status"])
        if status in HUMAN_HOOK_STATES or status in _state.TERMINAL_STATES:
            return _result(paths)
        if status not in DIRECT_STAGE_STATES | EDIT_PROGRESS_STATES | DELIVERY_PROGRESS_STATES:
            raise ValueError(f"状态 {status} 无法自动恢复")
        try:
            stage_result = self.dependencies.run_stage(paths, status)
        except Exception as exc:
            latest = _state.load_state(paths.state_path)
            failure_status = {
                "run_created": "source_failed",
                "source_ready": "detection_failed",
                "detection_image_ready": "geometry_failed",
                "geometry_ready": "crop_failed",
                "crop_ready": "mask_failed",
                "mask_ready": "mask_failed",
                "edit_completed": "layout_failed",
                "layout_completed": "layout_failed",
            }.get(status)
            if latest.get("status") == status and failure_status is not None:
                def bind_failure(payload: dict[str, Any]) -> None:
                    payload["failure"] = {
                        "stage": status,
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }

                _state.transition_state(
                    paths,
                    expected_status=status,
                    expected_revision=int(latest["state_revision"]),
                    next_status=failure_status,
                    event=f"{status}_failed",
                    mutate=bind_failure,
                )
                return _result(paths)
            raise
        if stage_result == "already_in_progress":
            current = _state.load_state(paths.state_path)
            return WorkflowResult(paths.root, "already_in_progress", int(current["state_revision"]))
        return _result(paths)

    def confirm_mask(self, run_root: str | Path, *, reviewer: str, resolved_ids: list[str]) -> WorkflowResult:
        paths = _paths(run_root)
        bundle = _read_json(paths.manifests_dir / "mask_review_bundle.json")
        _confirmations.confirm_mask_review(paths, bundle, reviewer=reviewer, resolved_ids=resolved_ids)
        return _result(paths)

    def confirm_final(self, run_root: str | Path, *, reviewer: str) -> WorkflowResult:
        paths = _paths(run_root)
        _confirmations.confirm_final_review(paths, reviewer=reviewer)
        return _result(paths)

    def deliver(self, run_root: str | Path) -> WorkflowResult:
        paths = _paths(run_root)
        delivery_result = _delivery.deliver_confirmed_result(paths)
        if delivery_result.status == "already_in_progress":
            current = _state.load_state(paths.state_path)
            return WorkflowResult(
                paths.root,
                "already_in_progress",
                int(current["state_revision"]),
            )
        return _result(paths)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="珠宝白底图唯一正式工作流")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="创建不可复用的新运行")
    for name in ("product-id", "base-token", "table-id", "record-id", "front-field-id", "target-field-id", "front-file-token", "front-file-name", "raw-source", "geometry", "base-url", "model"):
        create.add_argument(f"--{name}", required=True)
    create.add_argument("--output-root", required=True)
    for command in ("resume", "deliver"):
        commands.add_parser(command).add_argument("--run-root", required=True)
    mask = commands.add_parser("confirm-mask")
    mask.add_argument("--run-root", required=True)
    mask.add_argument("--reviewer", required=True)
    mask.add_argument("--resolved-id", action="append", default=[])
    final = commands.add_parser("confirm-final")
    final.add_argument("--run-root", required=True)
    final.add_argument("--reviewer", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workflow = WhiteBackgroundWorkflow()
    if args.command == "create":
        result = workflow.create(CreateRequest(
            output_root=Path(args.output_root), product_id=args.product_id,
            base_token=args.base_token, table_id=args.table_id, record_id=args.record_id,
            front_field_id=args.front_field_id, target_field_id=args.target_field_id,
            front_file_token=args.front_file_token, front_file_name=args.front_file_name,
            raw_source_path=Path(args.raw_source), geometry_path=Path(args.geometry),
            base_url=args.base_url, model=args.model,
        ))
    elif args.command == "resume":
        result = workflow.resume(args.run_root)
    elif args.command == "confirm-mask":
        result = workflow.confirm_mask(args.run_root, reviewer=args.reviewer, resolved_ids=args.resolved_id)
    elif args.command == "confirm-final":
        result = workflow.confirm_final(args.run_root, reviewer=args.reviewer)
    else:
        result = workflow.deliver(args.run_root)
    print(json.dumps({"run_root": str(result.run_root), "status": result.status, "state_revision": result.state_revision}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CreateRequest", "DefaultWorkflowDependencies", "WhiteBackgroundWorkflow", "WorkflowResult", "build_parser", "main"]
