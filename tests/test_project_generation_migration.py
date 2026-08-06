import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_SCRIPTS = (
    ROOT / "run_jewelry_base_pipeline.py",
    ROOT / "scripts" / "fresh_lark_qy029_qy034_qy008.py",
    ROOT / "scripts" / "rerun_qy006_qy008_color_fix.py",
    ROOT / "scripts" / "rerun_sensitive_eval.py",
)
SKILL_PATH = ROOT / "skills" / "jewelry-white-background" / "SKILL.md"


class ProjectGenerationMigrationTests(unittest.TestCase):
    def test_active_scripts_use_the_project_yuan_adapter(self) -> None:
        for path in ACTIVE_SCRIPTS:
            self.assertTrue(path.is_file(), path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("yuan_image_generation_adapter", text, path)
            self.assertIn("generate_to_path", text, path)

    def test_active_files_do_not_reference_the_retired_generator(self) -> None:
        forbidden = (
            bytes.fromhex("61697265697465722d696d6167652d67656e65726174696f6e").decode(),
            bytes.fromhex("61697265697465725f696d6167655f68656c7065722e7079").decode(),
            bytes.fromhex("4149526569746572").decode(),
            bytes.fromhex("4149524549544552").decode(),
        )
        for path in (*ACTIVE_SCRIPTS, SKILL_PATH):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, path)

    def test_active_scripts_no_longer_run_async_generation_commands(self) -> None:
        for path in ACTIVE_SCRIPTS:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('"submit"', text, path)
            self.assertNotIn('"wait"', text, path)
            self.assertNotIn("wait_for_task", text, path)
            self.assertNotIn("wait_task", text, path)

    def test_project_documents_and_skill_sources_use_only_yuan_v4_wording(self) -> None:
        forbidden = (
            bytes.fromhex("4149526569746572").decode(),
            bytes.fromhex("4149524549544552").decode(),
        )
        roots = (ROOT / "docs", ROOT / "skills" / "jewelry-white-background")
        for root in roots:
            for path in root.rglob("*"):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                if path.suffix.lower() not in {".md", ".py", ".yaml", ".yml", ".json"}:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                for token in forbidden:
                    self.assertNotIn(token, text, path)


if __name__ == "__main__":
    unittest.main()
