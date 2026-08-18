from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image


SCRIPTS = Path(__file__).parents[1] / "scripts"
WORKFLOW_SCRIPT = SCRIPTS / "run_white_background_workflow.py"
STATE_SCRIPT = SCRIPTS / "workflow_state.py"


def load_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fixed_now() -> datetime:
    return datetime(2026, 8, 16, 9, 30, tzinfo=timezone.utc)


def create_paths(tmp_path: Path):
    state = load_path("workflow_state_for_orchestrator", STATE_SCRIPT)
    paths = state.create_run(
        tmp_path,
        state.WorkflowIdentity(
            product_id="SY1537",
            base_token="base-token",
            table_id="table-id",
            record_id="record-id",
            front_field_id="front-field-id",
            target_field_id="target-field-id",
        ),
        now=fixed_now,
        uuid_factory=iter(
            [
                uuid.UUID("11111111-1111-1111-1111-111111111111"),
                uuid.UUID("22222222-2222-2222-2222-222222222222"),
            ]
        ).__next__,
    )
    return state, paths


def advance(state, paths, statuses: list[str]) -> None:
    for next_status in statuses:
        current = state.load_state(paths)
        state.transition_state(
            paths,
            expected_status=current["status"],
            expected_revision=current["state_revision"],
            next_status=next_status,
            event=f"test_{next_status}",
            now=fixed_now,
        )


class FakeDependencies:
    def __init__(self, state):
        self.state = state
        self.calls: list[str] = []

    def run_stage(self, paths, status: str) -> None:
        self.calls.append(status)
        next_status = {
            "run_created": "source_ready",
            "source_ready": "detection_image_ready",
            "detection_image_ready": "geometry_ready",
            "geometry_ready": "crop_ready",
            "crop_ready": "mask_ready",
            "mask_ready": "awaiting_mask_confirmation",
            "mask_confirmed": "edit_completed",
            "edit_completed": "layout_completed",
            "layout_completed": "awaiting_final_confirmation",
            "final_confirmed": "completed",
        }[status]
        current = self.state.load_state(paths.state_path)
        self.state.transition_state(
            self.state.RunPaths.from_root(paths.root),
            expected_status=status,
            expected_revision=current["state_revision"],
            next_status=next_status,
            event=f"fake_{next_status}",
            now=fixed_now,
        )


class FailingDependencies:
    def run_stage(self, _paths, _status: str) -> None:
        raise ValueError("stage failed")


def test_resume_executes_exactly_one_state_owned_stage(tmp_path: Path) -> None:
    state, paths = create_paths(tmp_path)
    module = load_path("workflow_orchestrator_one_step", WORKFLOW_SCRIPT)
    dependencies = FakeDependencies(state)
    workflow = module.WhiteBackgroundWorkflow(dependencies)

    result = workflow.resume(paths.root)

    assert result.status == "source_ready"
    assert dependencies.calls == ["run_created"]


def test_resume_stops_at_both_human_hooks(tmp_path: Path) -> None:
    state, paths = create_paths(tmp_path)
    module = load_path("workflow_orchestrator_hooks", WORKFLOW_SCRIPT)
    dependencies = FakeDependencies(state)
    workflow = module.WhiteBackgroundWorkflow(dependencies)
    advance(
        state,
        paths,
        [
            "source_ready",
            "detection_image_ready",
            "geometry_ready",
            "crop_ready",
            "mask_ready",
            "awaiting_mask_confirmation",
        ],
    )

    first = workflow.resume(paths.root)
    assert first.status == "awaiting_mask_confirmation"
    advance(
        state,
        paths,
        [
            "mask_confirmed",
            "edit_attempt_1",
            "edit_completed",
            "layout_completed",
            "awaiting_final_confirmation",
        ],
    )
    second = workflow.resume(paths.root)

    assert second.status == "awaiting_final_confirmation"
    assert dependencies.calls == []


@pytest.mark.parametrize("terminal", ["edit_failed", "edit_unknown", "upload_unknown", "completed"])
def test_terminal_state_never_calls_later_stages(tmp_path: Path, terminal: str) -> None:
    state, paths = create_paths(tmp_path)
    module = load_path(f"workflow_orchestrator_terminal_{terminal}", WORKFLOW_SCRIPT)
    dependencies = FakeDependencies(state)
    payload = state.load_state(paths)
    payload["status"] = terminal
    state.atomic_replace_json(paths.state_path, payload)

    result = module.WhiteBackgroundWorkflow(dependencies).resume(paths.root)

    assert result.status == terminal
    assert dependencies.calls == []


def test_cli_exposes_only_formal_commands_and_rejects_legacy_retry() -> None:
    module = load_path("workflow_orchestrator_cli", WORKFLOW_SCRIPT)
    parser = module.build_parser()
    subcommands = next(
        action.choices for action in parser._actions if getattr(action, "choices", None)
    )

    assert set(subcommands) == {"create", "confirm-mask", "resume", "confirm-final", "deliver"}
    with pytest.raises(SystemExit):
        parser.parse_args(["resume", "--run-root", "run", "--retry"])
    with pytest.raises(SystemExit):
        parser.parse_args(["create", "--prompt-file", "custom.txt"])


def test_stage_exception_enters_corresponding_terminal_state(tmp_path: Path) -> None:
    state, paths = create_paths(tmp_path)
    module = load_path("workflow_orchestrator_stage_failure", WORKFLOW_SCRIPT)

    result = module.WhiteBackgroundWorkflow(FailingDependencies()).resume(paths.root)

    assert result.status == "source_failed"
    assert state.load_state(paths)["failure"]["stage"] == "run_created"


def test_already_in_progress_is_returned_without_changing_state(tmp_path: Path) -> None:
    state, paths = create_paths(tmp_path)
    module = load_path("workflow_orchestrator_in_progress", WORKFLOW_SCRIPT)

    class BusyDependencies:
        def run_stage(self, _paths, _status: str) -> str:
            return "already_in_progress"

    result = module.WhiteBackgroundWorkflow(BusyDependencies()).resume(paths.root)

    assert result.status == "already_in_progress"
    assert state.load_state(paths)["status"] == "run_created"


def test_default_edit_revalidates_mask_confirmation_before_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, paths = create_paths(tmp_path)
    module = load_path("workflow_orchestrator_mask_gate", WORKFLOW_SCRIPT)
    advance(
        state,
        paths,
        [
            "source_ready",
            "detection_image_ready",
            "geometry_ready",
            "crop_ready",
            "mask_ready",
            "awaiting_mask_confirmation",
            "mask_confirmed",
        ],
    )
    monkeypatch.setattr(
        module._confirmations,
        "validate_mask_confirmation",
        lambda *_args, **_kwargs: type(
            "Gate", (), {"automatic_wawapi_edit_allowed": False}
        )(),
    )
    monkeypatch.setattr(
        module._retry,
        "run_edit_until_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("无效确认不得调用 Wawapi")
        ),
    )

    result = module.WhiteBackgroundWorkflow().resume(paths.root)

    assert result.status == "edit_failed"


def test_default_layout_stage_creates_layout_directory_before_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, paths = create_paths(tmp_path)
    advance(
        state,
        paths,
        [
            "source_ready",
            "detection_image_ready",
            "geometry_ready",
            "crop_ready",
            "mask_ready",
            "awaiting_mask_confirmation",
            "mask_confirmed",
            "edit_attempt_1",
            "edit_completed",
        ],
    )
    edit_path = paths.root / "edit" / "SY1537_generated.png"
    edit_path.parent.mkdir(parents=True)
    Image.new("RGB", (32, 32), "white").save(edit_path, "PNG")
    (paths.manifests_dir / "SY1537_edit_result.json").write_text(
        json.dumps({"result": {"path": "edit/SY1537_generated.png"}}),
        encoding="utf-8",
    )
    module = load_path("workflow_orchestrator_layout_directory", WORKFLOW_SCRIPT)
    module_paths = module._state.RunPaths.from_root(paths.root)
    layout_dir = paths.root / "layout"
    assert not layout_dir.exists()

    def fake_layout(input_path, output_path, manifest_path):
        assert input_path == edit_path
        assert layout_dir.is_dir()
        Image.new("RGB", (1536, 2048), "white").save(output_path, "PNG")
        manifest_path.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(module._layout, "layout_generated_result", fake_layout)

    module.DefaultWorkflowDependencies()._create_layout(module_paths)

    assert module._state.load_state(module_paths.state_path)["status"] == "layout_completed"


def test_formal_asset_paths_match_specification() -> None:
    module = load_path("workflow_orchestrator_paths", WORKFLOW_SCRIPT)

    assert module._raw_source_name("SY1537") == "source/SY1537_raw.bin"
    assert module._detection_manifest_name("SY1537") == "logs/SY1537_detection_manifest.json"
    assert module._vision_geometry_name("SY1537") == "geometry/SY1537_vision_geometry.json"


def test_default_source_stage_prepares_canonical_source(tmp_path: Path) -> None:
    module = load_path("workflow_orchestrator_default_source", WORKFLOW_SCRIPT)
    source = tmp_path / "input.jpg"
    Image.new("RGB", (12, 10), (220, 210, 200)).save(source, "JPEG")
    geometry = tmp_path / "geometry.json"
    geometry.write_text("{}", encoding="utf-8")
    created = module.WhiteBackgroundWorkflow().create(
        module.CreateRequest(
            output_root=tmp_path / "runs",
            product_id="SY1537",
            base_token="base-token",
            table_id="table-id",
            record_id="record-id",
            front_field_id="front-field-id",
            target_field_id="target-field-id",
            front_file_token="front-token",
            front_file_name="input.jpg",
            raw_source_path=source,
            geometry_path=geometry,
            base_url="https://example.invalid",
            model="test-model",
        )
    )

    result = module.WhiteBackgroundWorkflow().resume(created.run_root)

    assert result.status == "source_ready"
    assert (created.run_root / "source" / "SY1537_raw.bin").is_file()
    assert (created.run_root / "source" / "SY1537_original.png").is_file()


def test_invalid_mask_gate_during_edit_recovery_enters_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, paths = create_paths(tmp_path)
    module = load_path("workflow_orchestrator_edit_recovery_gate", WORKFLOW_SCRIPT)
    advance(
        state,
        paths,
        [
            "source_ready",
            "detection_image_ready",
            "geometry_ready",
            "crop_ready",
            "mask_ready",
            "awaiting_mask_confirmation",
            "mask_confirmed",
            "edit_attempt_1",
        ],
    )
    monkeypatch.setattr(
        module._confirmations,
        "validate_mask_confirmation",
        lambda *_args, **_kwargs: type(
            "Gate", (), {"automatic_wawapi_edit_allowed": False}
        )(),
    )

    result = module.WhiteBackgroundWorkflow().resume(paths.root)

    assert result.status == "edit_unknown"


def test_explicit_deliver_returns_already_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, paths = create_paths(tmp_path)
    module = load_path("workflow_orchestrator_explicit_delivery_busy", WORKFLOW_SCRIPT)
    payload = state.load_state(paths)
    payload["status"] = "final_confirmed"
    payload["state_revision"] = 20
    state.atomic_replace_json(paths.state_path, payload)
    monkeypatch.setattr(
        module._delivery,
        "deliver_confirmed_result",
        lambda *_args, **_kwargs: type("Result", (), {"status": "already_in_progress"})(),
    )

    result = module.WhiteBackgroundWorkflow().deliver(paths.root)

    assert result.status == "already_in_progress"
    assert state.load_state(paths)["status"] == "final_confirmed"


def test_user_interrupt_is_not_recorded_as_stage_failure(tmp_path: Path) -> None:
    state, paths = create_paths(tmp_path)
    module = load_path("workflow_orchestrator_interrupt", WORKFLOW_SCRIPT)

    class InterruptedDependencies:
        def run_stage(self, _paths, _status: str) -> None:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        module.WhiteBackgroundWorkflow(InterruptedDependencies()).resume(paths.root)

    assert state.load_state(paths)["status"] == "run_created"
