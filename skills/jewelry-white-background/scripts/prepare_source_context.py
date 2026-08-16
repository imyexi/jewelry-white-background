#!/usr/bin/env python3
"""固化飞书原始附件，并生成唯一规范化 RGB PNG 与产品上下文。"""

from __future__ import annotations

import hashlib
import io
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from workflow_state import (  # noqa: E402
    RunPaths,
    WorkflowIdentity,
    _filesystem_path,
    atomic_create_bytes,
    atomic_create_json,
)


MAX_IMAGE_PIXELS = 20_000_000


@dataclass(frozen=True)
class AttachmentIdentity:
    file_token: str
    file_name: str


@dataclass(frozen=True)
class SourceIdentity:
    raw_path: Path
    raw_source_sha256: str
    raw_size_bytes: int
    canonical_path: Path
    canonical_source_sha256: str
    canonical_size: tuple[int, int]

    @property
    def source_sha256(self) -> str:
        return self.canonical_source_sha256


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _validate_size(size: tuple[int, int]) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸必须为正整数")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("图片不得超过 20MP（20,000,000 像素）")


def _canonical_rgb(raw_path: Path) -> Image.Image:
    with Image.open(_filesystem_path(raw_path)) as opened:
        _validate_size(opened.size)
        opened.load()
        transposed = ImageOps.exif_transpose(opened)
        _validate_size(transposed.size)
        if "A" in transposed.getbands() or "transparency" in transposed.info:
            rgba = transposed.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            return Image.alpha_composite(white, rgba).convert("RGB")
        return transposed.convert("RGB")


def normalize_source(
    raw_path: str | Path,
    canonical_path: str | Path,
) -> SourceIdentity:
    raw_path = Path(raw_path)
    canonical_path = Path(canonical_path)
    raw_bytes = _filesystem_path(raw_path).read_bytes()
    if not raw_bytes:
        raise ValueError("飞书原始附件不能为空")
    canonical = _canonical_rgb(raw_path)
    buffer = io.BytesIO()
    canonical.save(buffer, "PNG")
    canonical_bytes = buffer.getvalue()
    atomic_create_bytes(canonical_path, canonical_bytes)
    return SourceIdentity(
        raw_path=raw_path,
        raw_source_sha256=_sha256_bytes(raw_bytes),
        raw_size_bytes=len(raw_bytes),
        canonical_path=canonical_path,
        canonical_source_sha256=_sha256_bytes(canonical_bytes),
        canonical_size=canonical.size,
    )


def _relative_run_path(run_root: Path, asset_path: Path) -> str:
    resolved_root = run_root.resolve()
    resolved_asset = asset_path.resolve(strict=True)
    try:
        relative = resolved_asset.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("源图资产必须位于当前运行目录") from exc
    return relative.as_posix()


def _validate_attachment(attachment: AttachmentIdentity) -> None:
    if not attachment.file_token.strip() or not attachment.file_name.strip():
        raise ValueError("飞书附件 token 和文件名不能为空")


def build_product_context(
    paths: RunPaths,
    target: WorkflowIdentity,
    attachment: AttachmentIdentity,
    source: SourceIdentity,
) -> dict[str, object]:
    _validate_attachment(attachment)
    if target.product_id != paths.product_id:
        raise ValueError("飞书目标商品编号与运行目录不一致")
    raw_relative = _relative_run_path(paths.root, source.raw_path)
    canonical_relative = _relative_run_path(paths.root, source.canonical_path)
    raw_bytes = _filesystem_path(source.raw_path).read_bytes()
    canonical_bytes = _filesystem_path(source.canonical_path).read_bytes()
    if _sha256_bytes(raw_bytes) != source.raw_source_sha256:
        raise ValueError("原始附件 SHA-256 已变化")
    if _sha256_bytes(canonical_bytes) != source.canonical_source_sha256:
        raise ValueError("规范化 PNG SHA-256 已变化")
    with Image.open(_filesystem_path(source.canonical_path)) as canonical:
        canonical.load()
        if canonical.format != "PNG" or canonical.mode != "RGB":
            raise ValueError("规范化源图必须是真实 RGB PNG")
        if canonical.size != source.canonical_size:
            raise ValueError("规范化源图尺寸已变化")

    context: dict[str, object] = {
        "schema_version": "jewelry-product-context-2.1",
        "workflow_mode": "background_only_edit",
        "run_id": paths.run_id,
        "product_id": target.product_id,
        "base_token": target.base_token,
        "table_id": target.table_id,
        "record_id": target.record_id,
        "front_field_id": target.front_field_id,
        "target_field_id": target.target_field_id,
        "front_file_token": attachment.file_token,
        "front_file_name": attachment.file_name,
        "source_identity": {
            "raw_source_path": raw_relative,
            "raw_source_sha256": source.raw_source_sha256,
            "raw_source_size_bytes": source.raw_size_bytes,
            "canonical_source_path": canonical_relative,
            "canonical_source_sha256": source.canonical_source_sha256,
            "canonical_source_size": list(source.canonical_size),
            "canonical_source_mode": "RGB",
        },
        "source_sha256": source.canonical_source_sha256,
        "source_size": list(source.canonical_size),
    }
    atomic_create_json(paths.root / "product_context.json", context)
    return context


__all__ = [
    "AttachmentIdentity",
    "MAX_IMAGE_PIXELS",
    "SourceIdentity",
    "build_product_context",
    "normalize_source",
]
