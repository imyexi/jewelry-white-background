from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
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
MAX_IMAGE_PIXELS = 20_000_000
DEFAULT_WAWAPI_BASE_URL = "https://wawapii.com"
DEFAULT_WAWAPI_MODEL = "gpt-image-2"


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


@dataclass(frozen=True)
class BackgroundEditRequest:
    prompt: str
    image: Path
    mask: Path
    output_path: Path
    base_url: str
    model: str
    python_executable: Path | str = sys.executable
    helper_path: Path = DEFAULT_HELPER


@dataclass(frozen=True)
class BackgroundEditRequestIdentity:
    provider: str
    operation: str
    endpoint: str
    model: str
    size: str
    n: int
    image_size: tuple[int, int]
    mask_size: tuple[int, int]
    image_sha256: str
    mask_sha256: str
    prompt_sha256: str

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "operation": self.operation,
            "endpoint": self.endpoint,
            "model": self.model,
            "size": self.size,
            "n": self.n,
            "image_size": list(self.image_size),
            "mask_size": list(self.mask_size),
            "image_sha256": self.image_sha256,
            "mask_sha256": self.mask_sha256,
            "prompt_sha256": self.prompt_sha256,
        }

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest().upper()


@dataclass(frozen=True)
class BackgroundEditAttempt:
    request_identity: dict
    request_identity_sha256: str
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    http_status: int | None
    request_may_have_been_sent: bool
    task_or_job_id: str | None
    rejection_evidence: str | None
    response_payload: dict | None = None
    helper_output: Path | None = None
    local_error: str | None = None
    result_path: Path | None = None
    result_format: str | None = None
    result_size: tuple[int, int] | None = None
    result_bytes: int | None = None
    result_sha256: str | None = None

    @property
    def has_valid_result(self) -> bool:
        return self.result_path is not None

    @property
    def definitive_response(self) -> bool:
        return self.http_status is not None or self.response_payload is not None


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
    base_url: str = "",
    model: str = "",
) -> list[str]:
    if not prompt.strip():
        raise ValueError("背景编辑提示词不能为空")
    command = [
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
    if base_url:
        command.extend(["--base-url", base_url])
    if model:
        command.extend(["--model", model])
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
            if size[0] * size[1] > MAX_IMAGE_PIXELS:
                raise ValueError(f"{label} 总像素不得超过 20,000,000")
            opened.verify()
    except ValueError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(f"{label} 不是可验证的 PNG 文件：{path}") from exc
    if require_alpha and "A" not in bands:
        raise ValueError(f"{label} 必须包含 alpha 通道")
    return size


def _validate_background_edit_inputs(
    image: Path, mask: Path
) -> tuple[tuple[int, int], tuple[int, int]]:
    image_size = _inspect_png(Path(image), "Edit 底图")
    mask_size = _inspect_png(Path(mask), "Mask", require_alpha=True)
    if image_size != mask_size:
        raise ValueError(
            f"Edit 底图与 Mask 尺寸必须相同：{image_size} != {mask_size}"
        )
    return image_size, mask_size


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def build_background_edit_request_identity(
    request: BackgroundEditRequest,
) -> BackgroundEditRequestIdentity:
    if not request.prompt.strip():
        raise ValueError("背景编辑提示词不能为空")
    image_size, mask_size = _validate_background_edit_inputs(
        Path(request.image), Path(request.mask)
    )
    base_url = request.base_url.strip().rstrip("/")
    if not base_url:
        raise ValueError("Wawapi base_url 不能为空")
    if not request.model.strip():
        raise ValueError("Wawapi model 不能为空")
    return BackgroundEditRequestIdentity(
        provider="wawapi",
        operation="edit",
        endpoint=f"{base_url}/v1/images/edits",
        model=request.model.strip(),
        size=f"{image_size[0]}x{image_size[1]}",
        n=1,
        image_size=image_size,
        mask_size=mask_size,
        image_sha256=_sha256(Path(request.image)),
        mask_sha256=_sha256(Path(request.mask)),
        prompt_sha256=hashlib.sha256(request.prompt.encode("utf-8"))
        .hexdigest()
        .upper(),
    )


def _nested_value(payload: object, names: set[str]) -> object | None:
    if isinstance(payload, dict):
        for name in names:
            value = payload.get(name)
            if value not in (None, ""):
                return value
        for value in payload.values():
            found = _nested_value(value, names)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _nested_value(value, names)
            if found not in (None, ""):
                return found
    return None


def _response_evidence(stdout: str, stderr: str) -> tuple[dict | None, int | None, str | None, str | None]:
    payload: dict | None = None
    try:
        decoded = json.loads(stdout)
        if isinstance(decoded, dict):
            payload = decoded
    except json.JSONDecodeError:
        pass
    status_match = re.search(r"\bHTTP\s+(\d{3})\b", stderr)
    http_status = int(status_match.group(1)) if status_match else None
    error_payload: dict | None = None
    if status_match and ":" in stderr[status_match.end() :]:
        raw_body = stderr[status_match.end() :].split(":", 1)[1].strip()
        try:
            decoded_body = json.loads(raw_body)
            if isinstance(decoded_body, dict):
                error_payload = decoded_body
        except json.JSONDecodeError:
            pass
    evidence_payload = error_payload or payload or {}
    task_or_job_id = (
        _nested_value(error_payload, {"task_id", "job_id"})
        if error_payload is not None
        else _nested_value(payload, {"remote_task_id", "remote_job_id", "job_id"})
    )
    rejection = None
    if _nested_value(evidence_payload, {"request_not_accepted"}) is True:
        rejection = "request_not_accepted"
    return (
        payload,
        http_status,
        str(task_or_job_id) if task_or_job_id not in (None, "") else None,
        rejection,
    )


def _generated_candidate(
    path: Path,
) -> tuple[bytes, str, tuple[int, int], str] | None:
    suffix_formats = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
    expected_format = suffix_formats.get(path.suffix.lower())
    if expected_format is None:
        return None
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return None
        data = path.read_bytes()
        with Image.open(path) as opened:
            actual_format = opened.format
            size = opened.size
            if actual_format != expected_format:
                return None
            if min(size) < 16 or size[0] * size[1] > MAX_IMAGE_PIXELS:
                return None
            opened.verify()
        with Image.open(path) as opened:
            opened.load()
    except (OSError, ValueError):
        return None
    return data, actual_format, size, path.suffix.lower()


def _is_helper_output(path: Path, helper_output_dir: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(helper_output_dir.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        return False
    return True


def _atomic_create_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _candidate_output_paths(output_path: Path) -> tuple[Path, ...]:
    return tuple(
        dict.fromkeys(output_path.with_suffix(suffix) for suffix in (".png", ".jpg", ".jpeg"))
    )


def _temporary_edit_output_dir(output_path: Path) -> Path:
    run_root = (
        output_path.parent.parent
        if output_path.parent.name.lower() == "edit"
        else output_path.parent
    )
    return run_root / "tmp" / f"wawapi-edit-{uuid.uuid4().hex}"


def _cleanup_temporary_output_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def edit_background_single_attempt(
    request: BackgroundEditRequest,
    transport: Callable = subprocess.run,
) -> BackgroundEditAttempt:
    image = Path(request.image)
    mask = Path(request.mask)
    output_path = Path(request.output_path)
    for possible_output in _candidate_output_paths(output_path):
        if possible_output.exists():
            raise FileExistsError(possible_output)
    identity_record = build_background_edit_request_identity(request)
    identity = identity_record.as_dict()
    identity_sha256 = identity_record.sha256
    helper_output_dir = _temporary_edit_output_dir(output_path)
    helper_output_dir.mkdir(parents=True, exist_ok=False)
    command = build_background_edit_command(
        request.prompt,
        image,
        mask,
        helper_output_dir,
        python_executable=request.python_executable,
        helper_path=request.helper_path,
        base_url=request.base_url.strip().rstrip("/"),
        model=request.model.strip(),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = transport(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            env=_helper_environment(),
        )
    except Exception as exc:
        _cleanup_temporary_output_dir(helper_output_dir)
        return BackgroundEditAttempt(
            request_identity=identity,
            request_identity_sha256=identity_sha256,
            command=tuple(command),
            returncode=None,
            stdout="",
            stderr=str(exc),
            http_status=None,
            request_may_have_been_sent=True,
            task_or_job_id=None,
            rejection_evidence=None,
            local_error=str(exc),
        )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    payload, http_status, task_or_job_id, rejection = _response_evidence(
        stdout, stderr
    )
    result_path = None
    result_format = None
    result_size = None
    result_bytes = None
    result_sha256 = None
    helper_output = None
    local_error = None
    if (
        completed.returncode == 0
        and isinstance(payload, dict)
        and payload.get("status") == "completed"
        and isinstance(payload.get("output"), list)
    ):
        for output in payload["output"]:
            if not isinstance(output, dict):
                continue
            raw_path = output.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            candidate_path = Path(raw_path)
            if not _is_helper_output(candidate_path, helper_output_dir):
                continue
            candidate = _generated_candidate(candidate_path)
            if candidate is None:
                continue
            data, result_format, result_size, result_suffix = candidate
            helper_output = candidate_path
            final_output_path = output_path.with_suffix(result_suffix)
            try:
                _atomic_create_bytes(final_output_path, data)
            except OSError as exc:
                local_error = f"结果原子发布失败，目标已存在或不可写：{exc}"
                break
            result_path = final_output_path
            helper_output = final_output_path
            result_bytes = len(data)
            result_sha256 = hashlib.sha256(data).hexdigest().upper()
            break
        if result_path is None and local_error is None:
            local_error = "图片生成结果中没有已落盘且可解码的 PNG/JPEG 本地图片"
    elif completed.returncode == 0 and payload is None:
        local_error = "图片编辑 helper 未返回有效 JSON"
    _cleanup_temporary_output_dir(helper_output_dir)
    return BackgroundEditAttempt(
        request_identity=identity,
        request_identity_sha256=identity_sha256,
        command=tuple(command),
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        http_status=http_status,
        request_may_have_been_sent=True,
        task_or_job_id=task_or_job_id,
        rejection_evidence=rejection,
        response_payload=payload,
        helper_output=helper_output,
        local_error=local_error,
        result_path=result_path,
        result_format=result_format,
        result_size=result_size,
        result_bytes=result_bytes,
        result_sha256=result_sha256,
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
    base_url: str = DEFAULT_WAWAPI_BASE_URL,
    model: str = DEFAULT_WAWAPI_MODEL,
) -> ImageEditResult:
    output_path = Path(output_path)
    log_path = Path(log_path)
    request = BackgroundEditRequest(
        prompt=prompt,
        image=Path(image),
        mask=Path(mask),
        output_path=output_path,
        base_url=base_url,
        model=model,
        python_executable=python_executable,
        helper_path=helper_path,
    )
    attempt = edit_background_single_attempt(request, transport=runner)
    if attempt.returncode != 0:
        error = (attempt.stderr or attempt.stdout or "图片编辑 helper 调用失败").strip()
        _write_log(
            log_path,
            {
                "status": "failed",
                "returncode": attempt.returncode,
                "error": error,
            },
        )
        raise RuntimeError(error)
    if attempt.response_payload is None:
        error = attempt.local_error or "图片编辑 helper 未返回有效 JSON"
        _write_log(log_path, {"status": "failed", "error": error})
        raise RuntimeError(error)
    if not attempt.has_valid_result:
        error = (
            attempt.local_error
            or "图片生成结果中没有已落盘且可解码的 PNG/JPEG 本地图片"
        )
        _write_log(log_path, {"status": "failed", "error": error})
        raise RuntimeError(error)
    payload = attempt.response_payload
    _write_log(log_path, payload)

    return ImageEditResult(
        provider=str(payload.get("provider") or ""),
        task_id=str(payload.get("task_id") or ""),
        helper_output=attempt.helper_output or attempt.result_path,
        delivered_output=attempt.result_path,
        payload=payload,
    )
