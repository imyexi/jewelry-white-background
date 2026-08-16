from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image


SCRIPT = Path.home() / ".codex/skills/yuanyuan-ruyi-watermark/scripts/watermark_images.py"


def load_module(name: str = "watermark_single_output_test"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def source_image(tmp_path: Path) -> Path:
    source = tmp_path / "source.png"
    Image.new("RGB", (240, 320), (245, 245, 245)).save(source, "PNG")
    return source


def test_single_output_creates_exact_path_and_rejects_existing_target(
    tmp_path: Path,
) -> None:
    module = load_module()
    source = source_image(tmp_path)
    output = tmp_path / "watermarked/SY1537__run__delivery__watermarked.png"
    args = [
        "--input",
        str(source),
        "--output",
        str(output),
        "--product-id",
        "SY1537",
        "--workers",
        "1",
    ]

    assert module.main(args) == 0
    first_bytes = output.read_bytes()
    assert first_bytes
    assert module.main(args) != 0
    assert output.read_bytes() == first_bytes


@pytest.mark.parametrize("kind", ["zero", "multiple", "missing"])
def test_single_output_requires_exactly_one_existing_input(
    tmp_path: Path, kind: str
) -> None:
    module = load_module(f"watermark_single_input_{kind}")
    source = source_image(tmp_path)
    output = tmp_path / "watermarked/result.png"
    inputs = {
        "zero": [],
        "multiple": [str(source), str(source)],
        "missing": [str(tmp_path / "missing.png")],
    }[kind]

    result = module.main(
        ["--input", *inputs, "--output", str(output), "--workers", "1"]
    )

    assert result != 0
    assert not output.exists()


def test_atomic_publish_failure_does_not_leave_target(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_module("watermark_atomic_failure")
    source = source_image(tmp_path)
    output = tmp_path / "watermarked/result.png"

    def fail_link(*_args, **_kwargs):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(module.os, "link", fail_link)

    assert module.main(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--workers",
            "1",
        ]
    ) != 0
    assert not output.exists()
