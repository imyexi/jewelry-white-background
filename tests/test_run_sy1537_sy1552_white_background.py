from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

import scripts.run_sy1537_sy1552_white_background as legacy


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "run_sy1537_sy1552_white_background.py"


def test_selected_sku_entry_delegates_formal_commands_unchanged() -> None:
    argv = ["deliver", "--run-root", "outputs/SY1537/run-id"]
    with patch.object(legacy, "workflow_main", return_value=23) as delegated:
        assert legacy.main(argv) == 23
    delegated.assert_called_once_with(argv)


@pytest.mark.parametrize(
    "option",
    ["--retry", "--authorization-reference", "--allow-size-override", "--append-version"],
)
def test_selected_sku_entry_rejects_unsafe_legacy_options(option: str) -> None:
    with (
        patch.object(legacy, "workflow_main") as delegated,
        pytest.raises(SystemExit, match="无法安全映射"),
    ):
        legacy.main(["resume", "--run-root", "run", option])
    delegated.assert_not_called()


def test_selected_sku_entry_no_longer_owns_business_state_or_side_effects() -> None:
    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    text = ENTRY.read_text(encoding="utf-8")

    assert functions <= {"_load_workflow_main", "_reject_unsafe_legacy_options", "main"}
    assert "subprocess" not in text
    assert "wawapi" not in text.lower()
    assert "watermark_images" not in text
    assert "record-upload-attachment" not in text
