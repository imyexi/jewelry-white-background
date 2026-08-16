from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image


SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
SOURCE_SCRIPT = SCRIPTS_DIR / "prepare_source_context.py"
STATE_SCRIPT = SCRIPTS_DIR / "workflow_state.py"


def load_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def write_transparent_source(path: Path) -> None:
    image = Image.new("RGBA", (3, 2), (10, 20, 30, 0))
    image.putpixel((1, 0), (80, 90, 100, 255))
    image.save(path, "PNG")


def test_normalize_source_separates_raw_and_canonical_hashes(tmp_path: Path) -> None:
    module = load_path("skill_prepare_source_context", SOURCE_SCRIPT)
    raw = tmp_path / "SY1537_raw.bin"
    canonical = tmp_path / "SY1537_original.png"
    write_transparent_source(raw)

    result = module.normalize_source(raw, canonical)

    assert result.raw_source_sha256 == sha256(raw)
    assert result.canonical_source_sha256 == sha256(canonical)
    assert result.source_sha256 == result.canonical_source_sha256
    assert result.raw_source_sha256 != result.canonical_source_sha256
    assert result.canonical_size == (3, 2)
    with Image.open(canonical) as image:
        image.load()
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert image.getpixel((1, 0)) == (80, 90, 100)


def test_normalize_source_never_overwrites_canonical_target(tmp_path: Path) -> None:
    module = load_path("skill_prepare_source_context", SOURCE_SCRIPT)
    raw = tmp_path / "raw.bin"
    canonical = tmp_path / "original.png"
    write_transparent_source(raw)
    canonical.write_bytes(b"sentinel")

    with pytest.raises(FileExistsError):
        module.normalize_source(raw, canonical)

    assert canonical.read_bytes() == b"sentinel"


def test_build_product_context_binds_run_target_attachment_and_source(
    tmp_path: Path,
) -> None:
    state = load_path("skill_workflow_state_for_source", STATE_SCRIPT)
    module = load_path("skill_prepare_source_context", SOURCE_SCRIPT)
    run_root = tmp_path / "SY1537" / "run-1"
    source_dir = run_root / "source"
    source_dir.mkdir(parents=True)
    raw = source_dir / "SY1537_raw.bin"
    canonical = source_dir / "SY1537_original.png"
    write_transparent_source(raw)
    source = module.normalize_source(raw, canonical)
    paths = state.RunPaths.from_root(run_root)
    target = state.WorkflowIdentity(
        product_id="SY1537",
        base_token="base-token",
        table_id="table-id",
        record_id="record-id",
        front_field_id="front-field-id",
        target_field_id="target-field-id",
    )
    attachment = module.AttachmentIdentity(
        file_token="front-token",
        file_name="front.png",
    )

    context = module.build_product_context(paths, target, attachment, source)

    assert context["schema_version"] == "jewelry-product-context-2.1"
    assert context["workflow_mode"] == "background_only_edit"
    assert context["run_id"] == "run-1"
    assert context["front_file_token"] == "front-token"
    assert context["target_field_id"] == "target-field-id"
    assert context["source_sha256"] == source.canonical_source_sha256
    assert context["source_identity"]["raw_source_path"] == "source/SY1537_raw.bin"
    assert context["source_identity"]["canonical_source_path"] == (
        "source/SY1537_original.png"
    )
    assert (run_root / "product_context.json").is_file()


def test_build_product_context_rejects_source_outside_run(tmp_path: Path) -> None:
    state = load_path("skill_workflow_state_for_escape", STATE_SCRIPT)
    module = load_path("skill_prepare_source_context", SOURCE_SCRIPT)
    run_root = tmp_path / "SY1537" / "run-1"
    run_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    raw = outside / "raw.bin"
    canonical = outside / "original.png"
    write_transparent_source(raw)
    source = module.normalize_source(raw, canonical)
    target = state.WorkflowIdentity(
        product_id="SY1537",
        base_token="base-token",
        table_id="table-id",
        record_id="record-id",
        front_field_id="front-field-id",
        target_field_id="target-field-id",
    )

    with pytest.raises(ValueError, match="运行目录"):
        module.build_product_context(
            state.RunPaths.from_root(run_root),
            target,
            module.AttachmentIdentity("front-token", "front.png"),
            source,
        )
