from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HELPER = (
    PROJECT_ROOT
    / ".agents"
    / "skills"
    / "yuan-image-generation"
    / "scripts"
    / "yuan_image_helper.py"
)


@dataclass(frozen=True)
class GenerationResult:
    provider: str
    task_id: str
    helper_output: Path
    delivered_output: Path
    payload: dict


@dataclass(frozen=True)
class ImageEditResult:
    provider: str
    task_id: str
    helper_output: Path
    delivered_output: Path
    payload: dict


def build_generate_command(
    prompt: str,
    images: Sequence[Path],
    output_dir: Path,
    python_executable: Path | str = sys.executable,
    helper_path: Path = DEFAULT_HELPER,
) -> list[str]:
    image_paths = [Path(path) for path in images]
    if not prompt.strip():
        raise ValueError("生图提示词不能为空")
    if not image_paths:
        raise ValueError("至少需要 1 张正面参考图")
    if len(image_paths) > 5:
        raise ValueError("每款最多 5 张参考图")

    command = [
        str(python_executable),
        str(helper_path),
        "generate",
        "--provider",
        "wawapi",
        "--operation",
        "generation",
        "--prompt",
        prompt,
        "--aspect-ratio",
        "3:4",
        "--resolution",
        "2K",
        "--output-dir",
        str(output_dir),
    ]
    for image_path in image_paths:
        command.extend(["--image", str(image_path)])
    return command


def build_background_edit_command(
    prompt: str,
    image: Path,
    mask: Path,
    output_dir: Path,
    python_executable: Path | str = sys.executable,
    helper_path: Path = DEFAULT_HELPER,
) -> list[str]:
    if not prompt.strip():
        raise ValueError("背景编辑提示词不能为空")
    return [
        str(python_executable),
        str(helper_path),
        "generate",
        "--provider",
        "wawapi",
        "--operation",
        "edit",
        "--prompt",
        prompt,
        "--output-dir",
        str(output_dir),
        "--image",
        str(image),
        "--mask",
        str(mask),
    ]


def parse_generation_payload(raw: str) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("图片生成 helper 未返回有效 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("图片生成 helper 返回值必须是 JSON 对象")
    if payload.get("status") != "completed":
        raise RuntimeError("图片生成状态不是 completed")
    outputs = payload.get("output")
    if not isinstance(outputs, list) or not outputs:
        raise RuntimeError("图片生成结果缺少本地输出")
    return payload


def _write_log(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _helper_environment() -> dict[str, str]:
    helper_env = os.environ.copy()
    helper_env["PYTHONIOENCODING"] = "utf-8"
    helper_env["PYTHONUTF8"] = "1"
    return helper_env


def _first_decodable_local_image(payload: dict) -> Path:
    for output in payload["output"]:
        if not isinstance(output, dict):
            continue
        raw_path = output.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        candidate = Path(raw_path)
        try:
            if not candidate.is_file() or candidate.stat().st_size == 0:
                continue
            with Image.open(candidate) as opened:
                if opened.format not in {"PNG", "JPEG"}:
                    continue
                opened.verify()
            with Image.open(candidate) as opened:
                opened.load()
        except (OSError, ValueError):
            continue
        return candidate
    raise RuntimeError("图片生成结果中没有已落盘且可解码的 PNG/JPEG 本地图片")


def _inspect_png(path: Path, label: str, require_alpha: bool = False) -> tuple[int, int]:
    path = Path(path)
    if path.suffix.lower() != ".png":
        raise ValueError(f"{label} 必须使用 .PNG 后缀")
    try:
        with Image.open(path) as opened:
            if opened.format != "PNG":
                raise ValueError(f"{label} 必须是真实 PNG 文件")
            size = opened.size
            bands = opened.getbands()
            opened.verify()
    except ValueError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(f"{label} 不是可验证的 PNG 文件：{path}") from exc
    if require_alpha and "A" not in bands:
        raise ValueError(f"{label} 必须包含 alpha 通道")
    return size


def _validate_background_edit_inputs(image: Path, mask: Path) -> None:
    image_size = _inspect_png(Path(image), "Edit 底图")
    mask_size = _inspect_png(Path(mask), "Mask", require_alpha=True)
    if image_size != mask_size:
        raise ValueError(
            f"Edit 底图与 Mask 尺寸必须相同：{image_size} != {mask_size}"
        )


def generate_to_path(
    prompt: str,
    images: Sequence[Path],
    output_path: Path,
    log_path: Path,
    runner: Callable = subprocess.run,
) -> GenerationResult:
    output_path = Path(output_path)
    log_path = Path(log_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_generate_command(prompt, images, output_path.parent)
    completed = runner(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=_helper_environment(),
    )
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "图片生成 helper 调用失败").strip()
        _write_log(
            log_path,
            {
                "status": "failed",
                "returncode": completed.returncode,
                "error": error,
            },
        )
        raise RuntimeError(error)

    try:
        payload = parse_generation_payload(completed.stdout)
        helper_output = _first_decodable_local_image(payload)
        if helper_output.resolve() != output_path.resolve():
            shutil.copy2(helper_output, output_path)
        _write_log(log_path, payload)
    except Exception as exc:
        _write_log(log_path, {"status": "failed", "error": str(exc)})
        raise

    return GenerationResult(
        provider=str(payload.get("provider") or ""),
        task_id=str(payload.get("task_id") or ""),
        helper_output=helper_output,
        delivered_output=output_path,
        payload=payload,
    )


def edit_background_to_path(
    prompt: str,
    image: Path,
    mask: Path,
    output_path: Path,
    log_path: Path,
    runner: Callable = subprocess.run,
    python_executable: Path | str = sys.executable,
    helper_path: Path = DEFAULT_HELPER,
) -> ImageEditResult:
    image = Path(image)
    mask = Path(mask)
    output_path = Path(output_path)
    log_path = Path(log_path)
    _validate_background_edit_inputs(image, mask)
    command = build_background_edit_command(
        prompt,
        image,
        mask,
        output_path.parent,
        python_executable=python_executable,
        helper_path=helper_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    completed = runner(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=_helper_environment(),
    )
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "图片编辑 helper 调用失败").strip()
        _write_log(
            log_path,
            {
                "status": "failed",
                "returncode": completed.returncode,
                "error": error,
            },
        )
        raise RuntimeError(error)

    try:
        payload = parse_generation_payload(completed.stdout)
        helper_output = _first_decodable_local_image(payload)
        if helper_output.resolve() != output_path.resolve():
            shutil.copy2(helper_output, output_path)
        _write_log(log_path, payload)
    except Exception as exc:
        _write_log(log_path, {"status": "failed", "error": str(exc)})
        raise

    return ImageEditResult(
        provider=str(payload.get("provider") or ""),
        task_id=str(payload.get("task_id") or ""),
        helper_output=helper_output,
        delivered_output=output_path,
        payload=payload,
    )
