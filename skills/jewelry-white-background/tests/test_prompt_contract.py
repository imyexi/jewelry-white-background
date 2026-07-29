from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SKILL_DIR = TEST_DIR.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from build_white_background_prompt import PROMPT_VERSION, build_prompt  # noqa: E402
from reference_plan import load_plan  # noqa: E402


class PromptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = load_plan(TEST_DIR / "fixtures" / "sy1462-reference-plan.json")
        cls.prompt = build_prompt(cls.plan)

    def test_uses_exactly_the_v2_section_order(self) -> None:
        headings = re.findall(r"^【(.+?)】$", self.prompt, flags=re.MULTILINE)

        self.assertEqual(
            [
                "任务目标",
                "参考图职责",
                "产品结构",
                "材质细节",
                "背景、光线与阴影",
                "构图",
                "画面范围",
            ],
            headings,
        )
        self.assertEqual("v2.2", PROMPT_VERSION)

    def test_keeps_sy1462_concrete_component_without_generic_component_catalog(self) -> None:
        self.assertIn("合金牛造型珠", self.prompt)
        for generic_part in ["吊坠", "小金属环", "隔珠", "隔片", "装饰帽", "连接环", "金属垫圈"]:
            self.assertNotIn(generic_part, self.prompt)

    def test_uses_the_front_image_only_for_rendered_structure_facts(self) -> None:
        structure = re.search(r"【产品结构】\n(.*?)(?=\n\n【|\Z)", self.prompt, flags=re.DOTALL)

        self.assertIsNotNone(structure)
        self.assertNotIn("细节图", structure.group(1))
        self.assertNotIn("局部图", structure.group(1))

    def test_keeps_material_source_paths_out_of_the_model_prompt(self) -> None:
        for observation in self.plan["material_observations"]:
            self.assertNotIn(observation["source_image"], self.prompt)

    def test_keeps_parameters_out_of_model_prompt(self) -> None:
        self.assertNotIn("尺寸 12mm", self.prompt)
        self.assertNotIn("水晶材质", self.prompt)

    def test_makes_white_background_composition_independent_from_front_image_framing(self) -> None:
        self.assertIn("正面图不决定白底画面的景别、产品占比或留白", self.prompt)
        self.assertIn("重新安排白底商品图的景别", self.prompt)
        self.assertIn("先建立产品宽度为 45%-55% 的白底构图，再根据正面图重建手串", self.prompt)

    def test_leaves_rendering_parameters_out_and_preserves_observation_wording(self) -> None:
        for excluded in ["3:4", "2K", "闪烁感", "星芒", "亮片", "爆闪", "光晕"]:
            self.assertNotIn(excluded, self.prompt)

        expected = "逐颗保留其底色、通透程度、内含物形态、纹理走向与密度，以及原图可见的明暗关系。"
        self.assertIn(expected, self.prompt)

    def test_reference_template_documents_the_same_v2_contract(self) -> None:
        reference = (SKILL_DIR / "references" / "white-background-prompt.md").read_text(encoding="utf-8")

        for section in ["【任务目标】", "【参考图职责】", "【产品结构】", "【材质细节】", "【背景、光线与阴影】", "【构图】", "【画面范围】"]:
            self.assertIn(section, reference)
        for obsolete_text in ["配件保留是关键", "所有“非普通圆珠”", "3:4", "2K", "闪烁感"]:
            self.assertNotIn(obsolete_text, reference)
