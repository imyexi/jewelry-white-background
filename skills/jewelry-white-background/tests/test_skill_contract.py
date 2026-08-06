from __future__ import annotations

import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    def test_requires_script_built_fixed_section_prompt(self) -> None:
        self.assertIn("不得手写、改造或替换模型提示词", self.skill)
        self.assertIn("【任务目标】、【参考图职责】、【产品结构】、【材质细节】、【背景、光线与阴影】、【构图】、【画面范围】", self.skill)

    def test_keeps_rendering_parameters_only_in_yuan_v4_command(self) -> None:
        self.assertIn("画幅和分辨率仅在这一步传参", self.skill)
        self.assertIn("yuan-image-generation", self.skill)
        self.assertIn("yuan_image_helper.py", self.skill)
        self.assertIn('--provider "wawapi"', self.skill)
        self.assertIn('--operation "generation"', self.skill)
        self.assertIn('--aspect-ratio "3:4"', self.skill)
        self.assertIn('--resolution "2K"', self.skill)

    def test_does_not_reference_the_retired_standalone_generator(self) -> None:
        forbidden = (
            "aireiter" + "-image-generation",
            "aireiter" + "_image_helper.py",
            "<AIREITER" + "_ROOT>",
        )
        for token in forbidden:
            self.assertNotIn(token, self.skill)
