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
        cls.reference = (SKILL_DIR / "references" / "white-background-prompt.md").read_text(encoding="utf-8")

    def test_uses_exactly_the_v3_section_order(self) -> None:
        headings = re.findall(r"^【(.+?)】$", self.prompt, flags=re.MULTILINE)
        self.assertEqual(
            ["任务目标", "参考图职责", "产品结构", "材质细节", "背景、光线与阴影", "构图", "画面范围"],
            headings,
        )
        self.assertEqual("v3.0", PROMPT_VERSION)

    def test_reference_contains_the_exact_authoritative_prompt(self) -> None:
        blocks = re.findall(r"```text\s*\n(.*?)\n```", self.reference, re.DOTALL)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].strip(), self.prompt.strip())

    def test_defines_the_front_image_as_the_only_mask_edit_base(self) -> None:
        self.assertIn("图片 1 是唯一 Edit 底图", self.prompt)
        self.assertIn("只编辑 Mask 透明背景区域", self.prompt)
        self.assertIn("Mask 不透明的产品区域不得重绘", self.prompt)

    def test_prompt_contains_no_workflow_control_or_product_semantics(self) -> None:
        for forbidden in [
            "vision-geometry-mask",
            "call slot",
            "delivery_id",
            "水印",
            "飞书",
            "确认",
            "生肖牛",
            "合金",
            "绿龙晶",
            "尺寸 12mm",
            "产品参数",
            "Generation",
            "占比",
        ]:
            self.assertNotIn(forbidden, self.prompt)


if __name__ == "__main__":
    unittest.main()
