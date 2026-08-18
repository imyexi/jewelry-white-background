from __future__ import annotations

import re
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
SKILL_DIR = TEST_DIR.parent


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        cls.agent = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")

    def test_documents_the_only_formal_order(self) -> None:
        ordered = [
            "检测增强",
            "vision-geometry-mask-2.1",
            "确定性裁剪",
            "三路 Mask",
            "Mask Hook",
            "Wawapi",
            "确定性排版",
            "最终 Hook",
            "水印",
            "飞书追加",
        ]
        positions = [self.skill.index(token) for token in ordered]

        self.assertEqual(positions, sorted(positions))
        self.assertIn("唯一正式流程", self.skill)
        self.assertEqual(self.skill.count("检测增强 →"), 1)

    def test_new_runs_use_only_geometry_21_and_crop_before_mask(self) -> None:
        self.assertIn("新运行只接受 `vision-geometry-mask-2.1`", self.skill)
        self.assertIn("2.0 仅允许历史资产只读检查", self.skill)
        self.assertNotIn("`vision-geometry-mask-2.0` JSON", self.skill)
        self.assertIn("create_geometry_crop.py", self.skill)
        self.assertIn("cropped_original", self.skill)
        self.assertIn("cropped_detection", self.skill)
        self.assertIn("cropped_local_detection", self.skill)
        self.assertIn("candidate_alpha", self.skill)

    def test_detection_images_have_distinct_cloud_and_local_roles(self) -> None:
        self.assertIn(
            "全局色阶图是唯一允许单图提交给云端视觉模型的图片",
            self.skill,
        )
        self.assertIn("局部对比度图只供本地 Mask 边缘贴合", self.skill)
        self.assertNotIn("局部对比度图提交给云端视觉", self.skill)

    def test_two_hooks_are_external_immutable_receipts(self) -> None:
        for required in [
            "mask_review_bundle.json",
            "mask_confirmation.json",
            "final_review_bundle.json",
            "final_confirmation.json",
            "delivery_id",
            "确认前不得继续",
        ]:
            self.assertIn(required, self.skill)
        self.assertNotIn("mask_review.resolved_uncertain_regions", self.skill)
        self.assertNotIn("在同一 profile 中加入合法 `mask_review`", self.skill)

    def test_edit_retry_and_layout_contract_are_explicit(self) -> None:
        for required in [
            "最多 3 个 call slot",
            "30 秒",
            "90 秒",
            "edit_unknown",
            "layout_generated_result.py",
            "只读取 Wawapi 生成结果",
            "不执行生成后结果 QC",
            "响应不含 task/job ID 或可用成功结果",
        ]:
            self.assertIn(required, self.skill)

    def test_final_confirmation_authorizes_both_one_shot_side_effects(self) -> None:
        for required in [
            '"watermark": true',
            '"feishu_append": true',
            "watermark_receipt.<delivery-id>.json",
            "upload_receipt.<delivery-id>.json",
            "--output",
            "+record-upload-attachment",
            "data.attachments[record_id][target_field_id]",
            "不得自动重传",
        ]:
            self.assertIn(required, self.skill)
        for retired in [
            "awaiting_authorization",
            "上传前再次授权",
            "+record-get",
            "+record-list",
            "上传后回读",
        ]:
            self.assertNotIn(retired, self.skill)

    def test_formal_orchestrator_and_legacy_entry_boundary_are_documented(self) -> None:
        for required in [
            "run_white_background_workflow.py",
            "create",
            "confirm-mask",
            "resume",
            "confirm-final",
            "deliver",
            "run_jewelry_base_pipeline.py",
            "run_sy1537_sy1552_white_background.py",
            "只委托正式编排器",
            "--retry",
        ]:
            self.assertIn(required, self.skill)

    def test_storage_path_contract_is_short_and_fail_fast(self) -> None:
        self.assertIn("Windows 安全路径上限为 `240`", self.skill)
        self.assertIn("`tmp/e-<8位随机十六进制>`", self.skill)
        self.assertIn("不得缩短 `run_id`、`delivery_id`", self.skill)
        self.assertIn("必须在 Wawapi claim 和真实 POST 前终止", self.skill)

    def test_prompt_source_and_input_isolation_remain_authoritative(self) -> None:
        for required in [
            "references/white-background-prompt.md",
            "build_white_background_prompt.py",
            "只使用首张正面图",
            "产品名称、产品参数、生肖或材料名称不得进入 Prompt",
            "Wawapi 不接收检测增强图",
        ]:
            self.assertIn(required, self.skill)

    def test_agent_metadata_matches_two_hook_workflow(self) -> None:
        for required in [
            "2.1",
            "裁剪",
            "Mask",
            "两次确认",
            "确定性排版",
            "水印",
            "飞书追加",
        ]:
            self.assertIn(required, self.agent)
        for retired in ["用户授权", "awaiting_authorization", "2.0"]:
            self.assertNotIn(retired, self.agent)

    def test_skill_has_no_retired_active_semantics(self) -> None:
        active = self.skill.split("## 历史兼容边界", 1)[0]
        for forbidden in [
            "automatic_wawapi_edit_allowed=true",
            "status=ok",
            "生成 state",
            "五项 Mask 身份摘要",
            "versioned receipt",
        ]:
            self.assertNotIn(forbidden, active)
        self.assertNotIn("vision-geometry-mask-2.0", active)
        self.assertIsNone(re.search(r'["`]mask_review["`]\s*:', active))
        self.assertIn("不再设置第三个人工 Hook", active)
        self.assertIsNone(
            re.search(r"(?:需要|必须|等待).{0,12}第三.{0,8}(?:授权|确认|Hook)", active)
        )
        layout_section = active.split("## 7. 确定性排版", 1)[1].split(
            "## 8. 最终 Hook", 1
        )[0]
        self.assertEqual(layout_section.count("结果 QC"), 1)
        self.assertIn("不执行生成后结果 QC", layout_section)
        self.assertIn("不再读取远端字段", active)
        self.assertIsNone(re.search(r"record\s+(?:get|list)", active, re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
