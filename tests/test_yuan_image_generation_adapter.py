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
            provider_file = root / "珠宝编辑结果.png"
            Image.new("RGB", (12, 9), (220, 220, 220)).save(provider_file, "PNG")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "provider": "wawapi",
                        "task_id": "edit-1",
                        "status": "completed",
                        "output": [
                            {"path": str(provider_file)},
                            {"path": str(root / "ignored-second.png")},
                        ],
                    },
                    ensure_ascii=False,
                ),
                stderr="",
            )
            runner_calls = []

            def runner(*args, **kwargs):
                runner_calls.append((args, kwargs))
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
            self.assertEqual(result.helper_output, provider_file)
            self.assertEqual(result.delivered_output.read_bytes(), provider_file.read_bytes())
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
            non_image_file = root / "response.json"
            non_image_file.write_text('{"status":"completed"}', encoding="utf-8")
            valid_file = root / "valid.jpg"
            Image.new("RGB", (12, 9), (220, 220, 220)).save(valid_file, "JPEG")
            completed = subprocess.CompletedProcess(
                args=[],
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
                runner=lambda *args, **kwargs: completed,
            )

            self.assertEqual(result.helper_output, valid_file)
            with Image.open(result.delivered_output) as delivered:
                self.assertEqual(delivered.format, "JPEG")
                delivered.verify()

    def test_edit_background_rejects_payload_when_all_outputs_are_invalid(self):
        module = self.require_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path, mask_path = self._write_valid_edit_inputs(root)
            corrupt_file = root / "corrupt.png"
            corrupt_file.write_bytes(b"not-an-image")
            unsupported_file = root / "unsupported.gif"
            Image.new("RGB", (8, 8), (255, 255, 255)).save(unsupported_file, "GIF")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    {
                        "provider": "wawapi",
                        "task_id": "edit-invalid",
                        "status": "completed",
                        "output": [
                            {"path": str(root / "missing.png")},
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
                    runner=lambda *args, **kwargs: completed,
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

    @staticmethod
    def _write_valid_edit_inputs(root: Path):
        image_path = root / "front.png"
        mask_path = root / "mask.png"
        Image.new("RGB", (16, 12), (240, 240, 240)).save(image_path, "PNG")
        Image.new("RGBA", (16, 12), (255, 255, 255, 0)).save(mask_path, "PNG")
        return image_path, mask_path


if __name__ == "__main__":
    unittest.main()
