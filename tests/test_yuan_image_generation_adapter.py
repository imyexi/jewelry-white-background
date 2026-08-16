import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

try:
    import yuan_image_generation_adapter as adapter
except ModuleNotFoundError:
    adapter = None


class YuanImageGenerationAdapterTests(unittest.TestCase):
    def require_adapter(self):
        self.assertIsNotNone(adapter, "统一生图适配层尚未实现")
        return adapter

    def test_build_generate_command_uses_wawapi_generation_and_preserves_image_order(self):
        module = self.require_adapter()
        command = module.build_generate_command(
            "生成白底商品图",
            [Path("front.jpg"), Path("detail.jpg")],
            Path("generated"),
            python_executable="python",
            helper_path=Path("helper.py"),
        )

        self.assertEqual(command[:5], ["python", "helper.py", "generate", "--provider", "wawapi"])
        self.assertEqual(command[5:8], ["--operation", "generation", "--prompt"])
        self.assertEqual(command[command.index("--aspect-ratio") + 1], "3:4")
        self.assertEqual(command[command.index("--resolution") + 1], "2K")
        self.assertEqual(command.count("--image"), 2)
        self.assertLess(command.index("front.jpg"), command.index("detail.jpg"))

    def test_build_generate_command_rejects_more_than_five_images(self):
        module = self.require_adapter()
        with self.assertRaisesRegex(ValueError, "最多 5 张"):
            module.build_generate_command(
                "生成白底商品图",
                [Path(f"{index}.jpg") for index in range(6)],
                Path("generated"),
            )

    def test_parse_generation_payload_requires_completed_local_output(self):
        module = self.require_adapter()
        with self.assertRaisesRegex(RuntimeError, "completed"):
            module.parse_generation_payload(
                json.dumps({"provider": "wawapi", "status": "failed", "output": []})
            )

    def test_generate_to_path_copies_first_local_output_and_writes_log(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider_file = root / "provider.png"
            Image.new("RGB", (12, 9), (230, 230, 230)).save(provider_file, "PNG")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "provider": "wawapi",
                        "task_id": "image-1",
                        "status": "completed",
                        "output": [{"path": str(provider_file)}],
                    }
                ),
                stderr="",
            )

            result = module.generate_to_path(
                "生成白底商品图",
                [root / "front.jpg"],
                root / "final.png",
                root / "generate.json",
                runner=lambda *args, **kwargs: completed,
            )

            self.assertEqual(result.provider, "wawapi")
            self.assertEqual(result.task_id, "image-1")
            self.assertEqual(result.delivered_output.read_bytes(), provider_file.read_bytes())
            log = json.loads((root / "generate.json").read_text(encoding="utf-8"))
            self.assertEqual(log["status"], "completed")
            self.assertNotIn("prompt", log)

    def test_generate_to_path_forces_utf8_for_helper_stdout_paths(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            provider_file = root / "珠宝输出.png"
            Image.new("RGB", (12, 9), (230, 230, 230)).save(provider_file, "PNG")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "provider": "wawapi",
                        "task_id": "image-utf8",
                        "status": "completed",
                        "output": [{"path": str(provider_file)}],
                    },
                    ensure_ascii=False,
                ),
                stderr="",
            )
            runner_kwargs = {}

            def runner(*args, **kwargs):
                runner_kwargs.update(kwargs)
                return completed

            result = module.generate_to_path(
                "生成白底商品图",
                [root / "front.jpg"],
                root / "final.png",
                root / "generate.json",
                runner=runner,
            )

            self.assertEqual(result.delivered_output.read_bytes(), provider_file.read_bytes())
            self.assertEqual(runner_kwargs["env"]["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(runner_kwargs["env"]["PYTHONUTF8"], "1")

    def test_generate_to_path_skips_corrupt_output_and_uses_next_decodable_png(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            corrupt_file = root / "corrupt.png"
            corrupt_file.write_bytes(b"not-an-image")
            valid_file = root / "valid.png"
            Image.new("RGB", (12, 9), (230, 230, 230)).save(valid_file, "PNG")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "provider": "wawapi",
                        "task_id": "image-fallback",
                        "status": "completed",
                        "output": [
                            {"path": str(corrupt_file)},
                            {"path": str(valid_file)},
                        ],
                    }
                ),
                stderr="",
            )

            result = module.generate_to_path(
                "生成白底商品图",
                [root / "front.jpg"],
                root / "final.png",
                root / "generate.json",
                runner=lambda *args, **kwargs: completed,
            )

            self.assertEqual(result.helper_output, valid_file)
            with Image.open(result.delivered_output) as delivered:
                self.assertEqual(delivered.format, "PNG")
                delivered.verify()

    def test_generate_to_path_reports_helper_failure_without_copying_output(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            completed = subprocess.CompletedProcess(
                args=[], returncode=2, stdout="", stderr="provider unavailable"
            )

            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                module.generate_to_path(
                    "生成白底商品图",
                    [root / "front.jpg"],
                    root / "final.png",
                    root / "generate.json",
                    runner=lambda *args, **kwargs: completed,
                )

            self.assertFalse((root / "final.png").exists())
            log = json.loads((root / "generate.json").read_text(encoding="utf-8"))
            self.assertEqual(log["status"], "failed")

    def test_build_background_edit_command_is_wawapi_edit_only(self):
        module = self.require_adapter()

        command = module.build_background_edit_command(
            "只替换透明背景",
            Path("front.png"),
            Path("mask.png"),
            Path("edit-output"),
            python_executable="python",
            helper_path=Path("helper.py"),
        )

        self.assertEqual(
            command,
            [
                "python",
                "helper.py",
                "generate",
                "--provider",
                "wawapi",
                "--operation",
                "edit",
                "--prompt",
                "只替换透明背景",
                "--output-dir",
                "edit-output",
                "--image",
                "front.png",
                "--mask",
                "mask.png",
            ],
        )
        self.assertEqual(command.count("--image"), 1)
        self.assertNotIn("--aspect-ratio", command)
        self.assertNotIn("--resolution", command)
        self.assertNotIn("generation", command)

    def test_edit_background_copies_first_output_and_reuses_utf8_environment(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            runner_calls = []
            expected_bytes = None

            def runner(command, **kwargs):
                nonlocal expected_bytes
                provider_file = (
                    Path(command[command.index("--output-dir") + 1])
                    / "珠宝编辑结果.png"
                )
                Image.new("RGB", (20, 20), (220, 220, 220)).save(
                    provider_file, "PNG"
                )
                expected_bytes = provider_file.read_bytes()
                completed = subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "provider": "wawapi",
                            "task_id": "edit-1",
                            "status": "completed",
                            "output": [{"path": str(provider_file)}],
                        },
                        ensure_ascii=False,
                    ),
                    stderr="",
                )
                runner_calls.append(((command,), kwargs))
                return completed

            result = module.edit_background_to_path(
                "只替换透明背景",
                image_path,
                mask_path,
                root / "delivered.png",
                root / "edit.json",
                runner=runner,
            )

            self.assertIsInstance(result, module.ImageEditResult)
            self.assertEqual(result.provider, "wawapi")
            self.assertEqual(result.task_id, "edit-1")
            self.assertEqual(result.helper_output, result.delivered_output)
            self.assertEqual(result.delivered_output.read_bytes(), expected_bytes)
            self.assertEqual(len(runner_calls), 1)
            command = runner_calls[0][0][0]
            self.assertEqual(command.count("--image"), 1)
            self.assertEqual(command[command.index("--image") + 1], str(image_path))
            self.assertEqual(command[command.index("--mask") + 1], str(mask_path))
            runner_kwargs = runner_calls[0][1]
            self.assertEqual(runner_kwargs["env"]["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(runner_kwargs["env"]["PYTHONUTF8"], "1")
            log = json.loads((root / "edit.json").read_text(encoding="utf-8"))
            self.assertEqual(log["status"], "completed")

    def test_edit_background_skips_non_image_output_and_uses_next_decodable_jpeg(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)

            def runner(command, **kwargs):
                helper_dir = Path(command[command.index("--output-dir") + 1])
                non_image_file = helper_dir / "response.json"
                non_image_file.write_text('{"status":"completed"}', encoding="utf-8")
                valid_file = helper_dir / "valid.jpg"
                Image.new("RGB", (20, 20), (220, 220, 220)).save(
                    valid_file, "JPEG"
                )
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "provider": "wawapi",
                            "task_id": "edit-fallback",
                            "status": "completed",
                            "output": [
                                {"path": str(non_image_file)},
                                {"path": str(valid_file)},
                            ],
                        }
                    ),
                    stderr="",
                )

            result = module.edit_background_to_path(
                "只替换透明背景",
                image_path,
                mask_path,
                root / "delivered.jpg",
                root / "edit.json",
                runner=runner,
            )

            self.assertEqual(result.helper_output, result.delivered_output)
            with Image.open(result.delivered_output) as delivered:
                self.assertEqual(delivered.format, "JPEG")
                delivered.verify()

    def test_edit_background_rejects_payload_when_all_outputs_are_invalid(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)

            def runner(command, **kwargs):
                helper_dir = Path(command[command.index("--output-dir") + 1])
                corrupt_file = helper_dir / "corrupt.png"
                corrupt_file.write_bytes(b"not-an-image")
                unsupported_file = helper_dir / "unsupported.gif"
                Image.new("RGB", (8, 8), (255, 255, 255)).save(
                    unsupported_file, "GIF"
                )
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "provider": "wawapi",
                            "task_id": "edit-invalid",
                            "status": "completed",
                            "output": [
                                {"path": str(helper_dir / "missing.png")},
                                {"path": str(corrupt_file)},
                                {"path": str(unsupported_file)},
                            ],
                        }
                    ),
                    stderr="",
                )

            with self.assertRaisesRegex(RuntimeError, "可解码的 PNG/JPEG"):
                module.edit_background_to_path(
                    "只替换透明背景",
                    image_path,
                    mask_path,
                    root / "delivered.png",
                    root / "edit.json",
                    runner=runner,
                )

            self.assertFalse((root / "delivered.png").exists())
            log = json.loads((root / "edit.json").read_text(encoding="utf-8"))
            self.assertEqual(log["status"], "failed")
            self.assertIn("可解码的 PNG/JPEG", log["error"])

    def test_edit_background_rejects_png_content_with_non_png_suffix_before_runner(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "front.jpg"
            Image.new("RGB", (16, 12), (240, 240, 240)).save(image_path, "PNG")
            mask_path = root / "mask.png"
            Image.new("RGBA", (16, 12), (255, 255, 255, 0)).save(mask_path, "PNG")
            runner_calls = []

            with self.assertRaisesRegex(ValueError, "PNG"):
                module.edit_background_to_path(
                    "只替换透明背景",
                    image_path,
                    mask_path,
                    root / "delivered.png",
                    root / "edit.json",
                    runner=lambda *args, **kwargs: runner_calls.append(args),
                )

            self.assertEqual(runner_calls, [])

    def test_edit_background_rejects_fake_png_content_before_runner(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "front.png"
            Image.new("RGB", (16, 12), (240, 240, 240)).save(image_path, "JPEG")
            mask_path = root / "mask.png"
            Image.new("RGBA", (16, 12), (255, 255, 255, 0)).save(mask_path, "PNG")
            runner_calls = []

            with self.assertRaisesRegex(ValueError, "PNG"):
                module.edit_background_to_path(
                    "只替换透明背景",
                    image_path,
                    mask_path,
                    root / "delivered.png",
                    root / "edit.json",
                    runner=lambda *args, **kwargs: runner_calls.append(args),
                )

            self.assertEqual(runner_calls, [])

    def test_edit_background_rejects_mask_without_alpha_before_runner(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "front.png"
            mask_path = root / "mask.png"
            Image.new("RGB", (16, 12), (240, 240, 240)).save(image_path, "PNG")
            Image.new("RGB", (16, 12), (255, 255, 255)).save(mask_path, "PNG")
            runner_calls = []

            with self.assertRaisesRegex(ValueError, "alpha"):
                module.edit_background_to_path(
                    "只替换透明背景",
                    image_path,
                    mask_path,
                    root / "delivered.png",
                    root / "edit.json",
                    runner=lambda *args, **kwargs: runner_calls.append(args),
                )

            self.assertEqual(runner_calls, [])

    def test_edit_background_rejects_size_mismatch_before_runner(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "front.png"
            mask_path = root / "mask.png"
            Image.new("RGB", (16, 12), (240, 240, 240)).save(image_path, "PNG")
            Image.new("RGBA", (15, 12), (255, 255, 255, 0)).save(mask_path, "PNG")
            runner_calls = []

            with self.assertRaisesRegex(ValueError, "尺寸"):
                module.edit_background_to_path(
                    "只替换透明背景",
                    image_path,
                    mask_path,
                    root / "delivered.png",
                    root / "edit.json",
                    runner=lambda *args, **kwargs: runner_calls.append(args),
                )

            self.assertEqual(runner_calls, [])

    def test_edit_background_logs_single_helper_failure_without_retry(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            completed = subprocess.CompletedProcess(
                args=[], returncode=2, stdout="", stderr="provider unavailable"
            )
            runner_calls = []

            def runner(*args, **kwargs):
                runner_calls.append((args, kwargs))
                return completed

            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                module.edit_background_to_path(
                    "只替换透明背景",
                    image_path,
                    mask_path,
                    root / "delivered.png",
                    root / "edit.json",
                    runner=runner,
                )

            self.assertEqual(len(runner_calls), 1)
            self.assertFalse((root / "delivered.png").exists())
            log = json.loads((root / "edit.json").read_text(encoding="utf-8"))
            self.assertEqual(log["status"], "failed")
            self.assertEqual(log["returncode"], 2)

    def test_edit_background_logs_invalid_json_payload(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="not-json", stderr=""
            )

            with self.assertRaisesRegex(RuntimeError, "有效 JSON"):
                module.edit_background_to_path(
                    "只替换透明背景",
                    image_path,
                    mask_path,
                    root / "delivered.png",
                    root / "edit.json",
                    runner=lambda *args, **kwargs: completed,
                )

            log = json.loads((root / "edit.json").read_text(encoding="utf-8"))
            self.assertEqual(log["status"], "failed")
            self.assertIn("有效 JSON", log["error"])

    def test_single_attempt_uses_actual_dimensions_and_one_transport_call(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "front.png"
            mask_path = root / "mask.png"
            Image.new("RGB", (37, 53), (240, 240, 240)).save(image_path, "PNG")
            Image.new("RGBA", (37, 53), (255, 255, 255, 0)).save(mask_path, "PNG")
            calls = []

            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=root / "edit" / "result.png",
                base_url="https://example.test/",
                model="gpt-image-2",
                python_executable="python",
                helper_path=Path("helper.py"),
            )
            def transport(command, **kwargs):
                calls.append(((command,), kwargs))
                provider_file = (
                    Path(command[command.index("--output-dir") + 1]) / "provider.png"
                )
                Image.new("RGB", (37, 53), (220, 220, 220)).save(
                    provider_file, "PNG"
                )
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "provider": "wawapi",
                            "task_id": "edit-single-1",
                            "status": "completed",
                            "output": [{"path": str(provider_file)}],
                        }
                    ),
                    stderr="",
                )

            attempt = module.edit_background_single_attempt(request, transport=transport)

            self.assertEqual(attempt.request_identity["image_size"], [37, 53])
            self.assertEqual(attempt.request_identity["mask_size"], [37, 53])
            self.assertEqual(attempt.request_identity["size"], "37x53")
            self.assertEqual(
                attempt.request_identity["endpoint"],
                "https://example.test/v1/images/edits",
            )
            self.assertEqual(len(attempt.request_identity_sha256), 64)
            self.assertEqual(len(calls), 1)
            self.assertTrue(attempt.has_valid_result)
            self.assertIsNone(attempt.task_or_job_id)
            self.assertEqual(attempt.result_path, request.output_path)
            self.assertTrue(request.output_path.is_file())

    def test_single_attempt_returns_http_failure_evidence_without_retry(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            calls = []
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr='HTTP 429: {"request_not_accepted":true,"error":"rate limited"}',
            )
            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=root / "edit" / "result.png",
                base_url="https://example.test",
                model="gpt-image-2",
            )

            attempt = module.edit_background_single_attempt(
                request,
                transport=lambda *args, **kwargs: calls.append((args, kwargs))
                or completed,
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(attempt.http_status, 429)
            self.assertTrue(attempt.request_may_have_been_sent)
            self.assertEqual(attempt.rejection_evidence, "request_not_accepted")
            self.assertIsNone(attempt.task_or_job_id)
            self.assertFalse(attempt.has_valid_result)

    def test_single_attempt_skips_invalid_candidates_and_atomically_publishes_first_valid(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            valid_bytes = None
            output = root / "edit" / "result.jpg"
            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=output,
                base_url="https://example.test",
                model="gpt-image-2",
            )

            def transport(command, **kwargs):
                nonlocal valid_bytes
                helper_dir = Path(command[command.index("--output-dir") + 1])
                corrupt = helper_dir / "corrupt.png"
                corrupt.write_bytes(b"not-an-image")
                wrong_suffix = helper_dir / "wrong.png"
                Image.new("RGB", (20, 20), (200, 200, 200)).save(
                    wrong_suffix, "JPEG"
                )
                tiny = helper_dir / "tiny.jpg"
                Image.new("RGB", (15, 20), (200, 200, 200)).save(tiny, "JPEG")
                oversized = helper_dir / "oversized.png"
                Image.new("L", (5_000, 4_001), 220).save(oversized, "PNG")
                valid = helper_dir / "valid.jpg"
                Image.new("RGB", (32, 24), (210, 210, 210)).save(valid, "JPEG")
                valid_bytes = valid.read_bytes()
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "provider": "wawapi",
                            "task_id": "edit-candidates",
                            "status": "completed",
                            "output": [
                                {"path": str(corrupt)},
                                {"path": str(wrong_suffix)},
                                {"path": str(tiny)},
                                {"path": str(oversized)},
                                {"path": str(valid)},
                            ],
                        }
                    ),
                    stderr="",
                )

            attempt = module.edit_background_single_attempt(request, transport=transport)

            self.assertEqual(output.read_bytes(), valid_bytes)
            self.assertEqual(attempt.result_format, "JPEG")
            self.assertEqual(attempt.result_size, (32, 24))
            self.assertEqual(attempt.result_bytes, output.stat().st_size)
            self.assertEqual(len(attempt.result_sha256 or ""), 64)

    def test_single_attempt_rejects_existing_output_before_transport(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            output = root / "edit" / "result.png"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"existing")
            calls = []
            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=output,
                base_url="https://example.test",
                model="gpt-image-2",
            )

            with self.assertRaises(FileExistsError):
                module.edit_background_single_attempt(
                    request,
                    transport=lambda *args, **kwargs: calls.append((args, kwargs)),
                )

            self.assertEqual(calls, [])
            self.assertEqual(output.read_bytes(), b"existing")

    def test_single_attempt_reports_transport_exception_as_indeterminate(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            calls = []
            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=root / "edit" / "result.png",
                base_url="https://example.test",
                model="gpt-image-2",
            )

            def transport(*args, **kwargs):
                calls.append((args, kwargs))
                raise subprocess.TimeoutExpired(args[0], timeout=30)

            attempt = module.edit_background_single_attempt(
                request, transport=transport
            )

            self.assertEqual(len(calls), 1)
            self.assertIsNone(attempt.returncode)
            self.assertTrue(attempt.request_may_have_been_sent)
            self.assertFalse(attempt.definitive_response)
            self.assertFalse(attempt.has_valid_result)

    def test_single_attempt_rejects_oversized_input_before_transport(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "front.png"
            Image.new("L", (5_000, 4_001), 220).save(image_path, "PNG")
            mask_path = root / "mask.png"
            Image.new("RGBA", (16, 16), (255, 255, 255, 0)).save(mask_path, "PNG")
            calls = []
            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=root / "edit" / "result.png",
                base_url="https://example.test",
                model="gpt-image-2",
            )

            with self.assertRaisesRegex(ValueError, "20,000,000"):
                module.edit_background_single_attempt(
                    request,
                    transport=lambda *args, **kwargs: calls.append((args, kwargs)),
                )

            self.assertEqual(calls, [])

    def test_request_identity_excludes_local_attempt_paths_and_helper_details(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            first = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=root / "edit-1" / "result.png",
                base_url="https://example.test/",
                model="gpt-image-2",
                python_executable="python-a",
                helper_path=Path("helper-a.py"),
            )
            second = module.BackgroundEditRequest(
                prompt=first.prompt,
                image=image_path,
                mask=mask_path,
                output_path=root / "edit-2" / "other.png",
                base_url="https://example.test",
                model="gpt-image-2",
                python_executable="python-b",
                helper_path=Path("helper-b.py"),
            )

            first_identity = module.build_background_edit_request_identity(first)
            second_identity = module.build_background_edit_request_identity(second)

            self.assertIsInstance(
                first_identity, module.BackgroundEditRequestIdentity
            )
            self.assertEqual(first_identity, second_identity)
            self.assertEqual(first_identity.sha256, second_identity.sha256)

    def test_single_attempt_uses_run_temporary_directory_for_helper_outputs(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            output = root / "edit" / "result.png"
            observed_output_dir = None

            def transport(command, **kwargs):
                nonlocal observed_output_dir
                observed_output_dir = Path(command[command.index("--output-dir") + 1])
                candidate = observed_output_dir / "helper-result.png"
                candidate.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (20, 20), (220, 220, 220)).save(candidate, "PNG")
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "provider": "wawapi",
                            "task_id": "local-helper-id",
                            "status": "completed",
                            "output": [{"path": str(candidate)}],
                        }
                    ),
                    stderr="",
                )

            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=output,
                base_url="https://example.test",
                model="gpt-image-2",
            )
            attempt = module.edit_background_single_attempt(
                request, transport=transport
            )

            self.assertTrue(attempt.has_valid_result)
            self.assertIsNotNone(observed_output_dir)
            self.assertNotEqual(observed_output_dir, output.parent)
            self.assertEqual(observed_output_dir.parent, root / "tmp")
            self.assertFalse(observed_output_dir.exists())
            self.assertTrue(output.is_file())

    def test_single_attempt_uses_first_valid_candidate_real_suffix_for_final_path(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            first_valid_bytes = None
            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=root / "edit" / "result.png",
                base_url="https://example.test",
                model="gpt-image-2",
            )

            def transport(command, **kwargs):
                nonlocal first_valid_bytes
                helper_dir = Path(command[command.index("--output-dir") + 1])
                first_valid = helper_dir / "first-valid.jpg"
                Image.new("RGB", (24, 20), (210, 210, 210)).save(
                    first_valid, "JPEG"
                )
                first_valid_bytes = first_valid.read_bytes()
                later_png = helper_dir / "later.png"
                Image.new("RGB", (24, 20), (220, 220, 220)).save(
                    later_png, "PNG"
                )
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "provider": "wawapi",
                            "task_id": "local-helper-id",
                            "status": "completed",
                            "output": [
                                {"path": str(first_valid)},
                                {"path": str(later_png)},
                            ],
                        }
                    ),
                    stderr="",
                )

            attempt = module.edit_background_single_attempt(request, transport=transport)

            expected = request.output_path.with_suffix(".jpg")
            self.assertEqual(attempt.result_path, expected)
            self.assertEqual(expected.read_bytes(), first_valid_bytes)
            self.assertFalse(request.output_path.exists())

    def test_single_attempt_preserves_response_when_atomic_publish_loses_race(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            output = root / "edit" / "result.png"

            def transport(command, **kwargs):
                helper_dir = Path(command[command.index("--output-dir") + 1])
                candidate = helper_dir / "candidate.png"
                Image.new("RGB", (20, 20), (220, 220, 220)).save(
                    candidate, "PNG"
                )
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"concurrent-winner")
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "provider": "wawapi",
                            "task_id": "local-helper-id",
                            "status": "completed",
                            "output": [{"path": str(candidate)}],
                        }
                    ),
                    stderr="",
                )

            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=output,
                base_url="https://example.test",
                model="gpt-image-2",
            )

            attempt = module.edit_background_single_attempt(
                request, transport=transport
            )

            self.assertEqual(attempt.returncode, 0)
            self.assertTrue(attempt.definitive_response)
            self.assertFalse(attempt.has_valid_result)
            self.assertIn("已存在", attempt.local_error or "")
            self.assertEqual(output.read_bytes(), b"concurrent-winner")

    def test_single_attempt_zero_exit_with_invalid_stdout_is_not_definitive(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=root / "edit" / "result.png",
                base_url="https://example.test",
                model="gpt-image-2",
            )

            attempt = module.edit_background_single_attempt(
                request,
                transport=lambda *args, **kwargs: subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="not-json", stderr=""
                ),
            )

            self.assertFalse(attempt.definitive_response)
            self.assertFalse(attempt.has_valid_result)

    def test_legacy_edit_wrapper_rejects_existing_output_before_runner(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            output = root / "result.png"
            output.write_bytes(b"existing")
            calls = []

            with self.assertRaises(FileExistsError):
                module.edit_background_to_path(
                    "只替换透明背景",
                    image_path,
                    mask_path,
                    output,
                    root / "edit.json",
                    runner=lambda *args, **kwargs: calls.append((args, kwargs)),
                )

            self.assertEqual(calls, [])
            self.assertEqual(output.read_bytes(), b"existing")

    def test_single_attempt_rejects_candidate_outside_this_helper_directory(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "front.png"
            mask_path = root / "mask.png"
            Image.new("RGB", (20, 20), (240, 240, 240)).save(image_path, "PNG")
            Image.new("RGBA", (20, 20), (255, 255, 255, 0)).save(mask_path, "PNG")
            request = module.BackgroundEditRequest(
                prompt="只替换透明背景\n",
                image=image_path,
                mask=mask_path,
                output_path=root / "edit" / "result.png",
                base_url="https://example.test",
                model="gpt-image-2",
            )

            attempt = module.edit_background_single_attempt(
                request,
                transport=lambda *args, **kwargs: subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "provider": "wawapi",
                            "task_id": "local-helper-id",
                            "status": "completed",
                            "output": [{"path": str(image_path)}],
                        }
                    ),
                    stderr="",
                ),
            )

            self.assertFalse(attempt.has_valid_result)
            self.assertFalse(request.output_path.exists())

    def test_legacy_wrapper_returns_existing_published_path_after_temp_cleanup(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)

            def runner(command, **kwargs):
                helper_dir = Path(command[command.index("--output-dir") + 1])
                candidate = helper_dir / "provider.png"
                candidate.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (20, 20), (220, 220, 220)).save(candidate, "PNG")
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "provider": "wawapi",
                            "task_id": "local-helper-id",
                            "status": "completed",
                            "output": [{"path": str(candidate)}],
                        }
                    ),
                    stderr="",
                )

            result = module.edit_background_to_path(
                "只替换透明背景",
                image_path,
                mask_path,
                root / "edit" / "result.png",
                root / "edit.json",
                runner=runner,
            )

            self.assertEqual(result.helper_output, result.delivered_output)
            self.assertTrue(result.helper_output.is_file())

    @staticmethod
    def _write_valid_edit_inputs(root: Path):
        image_path = root / "front.png"
        mask_path = root / "mask.png"
        Image.new("RGB", (16, 12), (240, 240, 240)).save(image_path, "PNG")
        Image.new("RGBA", (16, 12), (255, 255, 255, 0)).save(mask_path, "PNG")
        return image_path, mask_path


if __name__ == "__main__":
    unittest.main()
