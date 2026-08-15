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

    def test_uses_exactly_the_v3_section_order(self) -> None:
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
        self.assertEqual("v3.0", PROMPT_VERSION)

    def test_defines_the_front_image_as_the_only_mask_edit_base(self) -> None:
        self.assertIn("图片 1 是唯一 Edit 底图", self.prompt)
        self.assertIn("只编辑 Mask 透明背景区域", self.prompt)
        self.assertIn("Mask 不透明的产品区域不得重绘", self.prompt)

    def test_preserves_canvas_and_product_geometry(self) -> None:
        for required in ["原画布", "比例", "位置", "尺寸", "视角", "珠子", "串线", "特殊件"]:
            self.assertIn(required, self.prompt)

    def test_requires_a_continuous_light_background_without_artifacts(self) -> None:
        self.assertIn("#FBFCF9", self.prompt)
        self.assertIn("四边和四角", self.prompt)
        for forbidden_artifact in ["黑色外围", "深色外围", "暗角", "边框", "圆形背景块", "道具", "文字"]:
            self.assertIn(forbidden_artifact, self.prompt)

    def test_keeps_product_semantics_and_generation_language_out(self) -> None:
        for forbidden in [
            "生肖牛",
            "合金",
            "绿龙晶",
            "尺寸 12mm",
            "产品参数",
            "重新生成",
            "重建",
            "重新安排",
            "景别",
            "Generation",
            "占比",
        ]:
            self.assertNotIn(forbidden, self.prompt)
