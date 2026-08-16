from __future__ import annotations

import ast
import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

import run_jewelry_base_pipeline as legacy


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "run_jewelry_base_pipeline.py"


def test_general_entry_delegates_formal_commands_unchanged() -> None:
    argv = ["resume", "--run-root", "outputs/SY1537/run-id"]
    with patch.object(legacy, "workflow_main", return_value=17) as delegated:
        assert legacy.main(argv) == 17
    delegated.assert_called_once_with(argv)


@pytest.mark.parametrize(
    "option",
    ["--retry", "--authorization-reference", "--allow-size-override", "--append-version"],
)
def test_general_entry_rejects_unsafe_legacy_options(option: str) -> None:
    with (
        patch.object(legacy, "workflow_main") as delegated,
        pytest.raises(SystemExit, match="无法安全映射"),
    ):
        legacy.main(["resume", "--run-root", "run", option])
    delegated.assert_not_called()


def test_general_entry_contains_only_orchestrator_delegation() -> None:
    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    text = ENTRY.read_text(encoding="utf-8")

    assert "workflow_main" in calls
    assert "subprocess" not in text
    assert "wawapi" not in text.lower()
    assert "watermark_images" not in text
    assert "record-upload-attachment" not in text


def test_repository_callers_no_longer_import_retired_entry_symbols() -> None:
    module = importlib.import_module("scripts.rerun_qy006_qy008_color_fix")

    assert callable(module.run)
