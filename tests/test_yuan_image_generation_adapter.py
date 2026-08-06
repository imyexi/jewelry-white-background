import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
            provider_file.write_bytes(b"png-data")
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
            self.assertEqual(result.delivered_output.read_bytes(), b"png-data")
            log = json.loads((root / "generate.json").read_text(encoding="utf-8"))
            self.assertEqual(log["status"], "completed")
            self.assertNotIn("prompt", log)

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


if __name__ == "__main__":
    unittest.main()
