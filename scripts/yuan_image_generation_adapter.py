from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


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
    first = outputs[0]
    if not isinstance(first, dict) or not first.get("path"):
        raise RuntimeError("图片生成结果缺少本地输出路径")
    return payload


def _write_log(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
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
        helper_output = Path(payload["output"][0]["path"])
        if not helper_output.is_file() or helper_output.stat().st_size == 0:
            raise RuntimeError(f"图片生成输出不存在或为空：{helper_output}")
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
