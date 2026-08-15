from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
SKILL_DIR = TEST_DIR.parent
PROJECT_ROOT = SKILL_DIR.parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from build_white_background_prompt import build_prompt  # noqa: E402
from reference_plan import load_plan  # noqa: E402


def normalize_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        cls.reference = (
            SKILL_DIR / "references" / "white-background-prompt.md"
        ).read_text(encoding="utf-8")
        cls.agent = (SKILL_DIR / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        cls.design = (
            PROJECT_ROOT
            / "docs"
            / "superpowers"
            / "specs"
            / "2026-08-11-jewelry-background-only-mask-edit-design.md"
        ).read_text(encoding="utf-8")
        cls.implementation_plan = (
            PROJECT_ROOT
            / "docs"
            / "superpowers"
            / "plans"
            / "2026-08-11-jewelry-background-only-mask-edit.md"
        ).read_text(encoding="utf-8")
        cls.delivery_documents = {
            "SKILL.md": cls.skill,
            "设计文档": cls.design,
            "实施计划": cls.implementation_plan,
        }
        cls.combined_docs = "\n".join((cls.skill, cls.reference, cls.agent))
        cls.plan = load_plan(TEST_DIR / "fixtures" / "sy1462-reference-plan.json")
        cls.built_prompt = build_prompt(cls.plan)

    def test_reference_fenced_prompt_exactly_matches_builder_output(self) -> None:
        blocks = re.findall(r"```text\s*\n(.*?)\n```", self.reference, re.DOTALL)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(normalize_text(blocks[0]), normalize_text(self.built_prompt))
        headings = re.findall(r"^【(.+?)】$", blocks[0], flags=re.MULTILINE)
        self.assertEqual(
            headings,
            [
                "任务目标",
                "参考图职责",
                "产品结构",
                "材质细节",
                "背景、光线与阴影",
                "构图",
                "画面范围",
            ],
        )

    def test_skill_declares_one_ordered_background_edit_flow(self) -> None:
        ordered_steps = [
            "飞书最新首张正面图",
            "同尺寸 RGB PNG",
            "RGBA 产品保护 Mask",
            "技术门禁",
            "operation=edit",
            "第一张有效本地结果",
            "Yuan Studio 水印",
            "用户明确授权",
            "ok: true",
            "file_token",
            "本地回执",
            "立即终止",
        ]
        positions = [self.skill.index(step) for step in ordered_steps]

        self.assertEqual(positions, sorted(positions))
        self.assertIn("唯一正式流程", self.skill)

    def test_source_is_only_the_front_image_field_and_main_image_is_only_a_target(self) -> None:
        workflow = self.skill.split("## 输入与运行产物", 1)[1]
        source_workflow, upload_workflow = workflow.split("## 8. 授权后追加主图", 1)

        self.assertIn("正面图字段", source_workflow)
        self.assertNotIn("主图字段", source_workflow)
        self.assertIn("主图字段", upload_workflow)

    def test_skill_requires_auditable_mask_assets_and_exact_semantics(self) -> None:
        for required in [
            "create_background_edit_assets",
            "vision-geometry-mask-2.0",
            "source_sha256",
            "coordinate_range",
            "alpha=0",
            "背景可编辑",
            "alpha=255",
            "产品保护",
            "所有 RGB 通道固定为 255",
            "红色可编辑区叠加图",
            "JSON 技术报告",
        ]:
            self.assertIn(required, self.skill)

    def test_cloud_geometry_and_local_detection_have_separate_roles(self) -> None:
        for required in [
            "每张图片单独",
            "云端视觉",
            "polygon",
            "polyline",
            "ellipse",
            "ellipse_set",
            "自适应色阶",
            "prepare_mask_detection_images.py",
            "无需临时编写或拼装色阶脚本",
            "只用于生成 Mask",
            "prepared",
            "未经色阶处理",
        ]:
            self.assertIn(required, self.skill)
        self.assertTrue(
            (SKILL_DIR / "scripts" / "prepare_mask_detection_images.py").is_file()
        )
        self.assertNotIn("固定 69/204", self.skill)
        for implementation_detail in ("P1/P99", "2.5%", "96..160"):
            self.assertNotIn(implementation_detail, self.skill)
        self.assertIn("不得回退到环形推断、全图纯边缘识别", self.skill)

    def test_detection_copies_never_enter_wawapi_or_prompt(self) -> None:
        for required in [
            "Wawapi 不接收色阶增强图、边缘检测副本或视觉几何 JSON",
            "色阶参数不得进入 Prompt",
        ]:
            self.assertIn(required, self.skill)
        for forbidden in [
            "P1/P99",
            "P3/P97",
            "69/204",
            "CLAHE",
            "black_point",
            "white_point",
        ]:
            self.assertNotIn(forbidden, self.reference)
            self.assertNotIn(forbidden, self.built_prompt)

    def test_only_ok_mask_assessment_may_call_wawapi(self) -> None:
        self.assertIn(
            "status=ok` 且 `automatic_wawapi_edit_allowed=true",
            self.skill,
        )
        self.assertIn("评估报告当前只产生 `ok` 或 `review`", self.skill)
        self.assertIn("输入、schema 或身份校验失败会直接终止", self.skill)
        self.assertIn("真实商品声明", self.skill)
        self.assertIn("允许相应图元覆盖的边框像素触边", self.skill)
        self.assertIn("保护占比", self.skill)
        self.assertIn("中心不再作为通用可编辑门禁", self.skill)
        self.assertNotIn("保护区不得与画布边缘接触", self.skill)
        self.assertNotIn("中心和四角可编辑", self.skill)

    def test_uncertain_regions_require_explicit_review_resolution(self) -> None:
        for required in [
            "uncertain_regions[].id",
            "bbox",
            "reason",
            "mask_review",
            "resolved_uncertain_regions",
            "逐项列出",
            "缺失任一 ID 均保持 `review`",
            "审核、几何、疑点定义或最终 Mask 更新后都必须重跑技术门禁",
            "geometry_sha256",
            "mask_sha256",
            "任一摘要变化都使旧审核变为 `stale`",
        ]:
            self.assertIn(required, self.skill)
        self.assertNotIn("最终 Mask、预览、叠加图", self.skill)

    def test_skill_documents_identity_border_and_degenerate_mask_gates(self) -> None:
        for required in [
            "受支持的云端视觉 `producer`",
            "polygon 必须具有至少三个不同点和非零面积",
            "polyline 必须具有非零长度",
            "逐像素覆盖",
            "0.5%..70%",
            "联网前重算的 prepared/Mask 文件 SHA-256",
        ]:
            self.assertIn(required, self.skill)

    def test_mask_contract_supports_non_ring_products_without_ring_fallback(self) -> None:
        for required in [
            "手串",
            "吊坠",
            "项链",
            "链绳",
            "非圆环",
            "不得回退到环形推断",
            "不存在无视觉几何的正式 fallback",
        ]:
            self.assertIn(required, self.skill)

    def test_mask_excludes_background_structures_and_large_shadows(self) -> None:
        for required in [
            "不保护大面积投影",
            "不保护白色垫板",
            "桌面",
            "道具",
        ]:
            self.assertIn(required, self.skill)
        self.assertNotIn("可见阴影都应落在不透明保护区内", self.skill)

    def test_skill_contains_the_single_image_single_mask_edit_command(self) -> None:
        self.assertIn("yuan-image-generation", self.skill)
        self.assertIn("yuan_image_helper.py", self.skill)
        self.assertIn('generate `', self.skill)
        self.assertIn('--provider "wawapi"', self.skill)
        self.assertIn('--operation "edit"', self.skill)
        self.assertEqual(self.skill.count('--image "'), 1)
        self.assertEqual(self.skill.count('--mask "'), 1)
        self.assertNotIn("--aspect-ratio", self.combined_docs)
        self.assertNotIn("--resolution", self.combined_docs)
        for retired_size in ("3:4", "2K", "1536x2048"):
            self.assertNotIn(retired_size, self.combined_docs)

    def test_prompt_and_docs_exclude_product_semantics_and_other_images(self) -> None:
        for required in [
            "产品名称、产品参数、生肖或材料名称不得进入 Prompt",
            "只使用首张正面图，不读取或提交其他图片",
        ]:
            self.assertIn(required, self.skill)
            self.assertIn(required, self.reference)
        for forbidden in [
            "生肖牛",
            "合金",
            "绿龙晶",
            "尺寸 12mm",
            "产品参数",
            "重新生成",
            "重建",
            "景别",
            "占比",
        ]:
            self.assertNotIn(forbidden, self.built_prompt)

    def test_docs_forbid_content_qc_fallback_and_automatic_reruns(self) -> None:
        for required in [
            "不对 Edit 结果执行内容、构图、占比或尺寸 QC",
            "不自动切换 provider",
            "不自动更换 operation",
            "不修改 Prompt 重跑",
        ]:
            self.assertIn(required, self.skill)
        for forbidden in [
            '--operation "generation"',
            "operation=generation",
            "Wawapi Generation",
            "重建模式",
            "双阶段",
            "双 QC",
            "双QC",
            "detail_images",
            'detail>.jpg',
            "evaluate_white_background.py",
            "pre_watermark_qc",
            "post_watermark_qc",
            "_record_get",
            "+record-get",
            "+record-delete-attachment",
        ]:
            self.assertNotIn(forbidden, self.combined_docs)

    def test_upload_is_append_only_and_terminates_on_ok_response(self) -> None:
        for required in [
            "只追加，不替换",
            "不得删除任何旧 token",
            "只允许一次附件上传调用",
            "仅解析内存中的上传响应",
            "写入版本化回执后立即终止",
            "不得上传后回读",
            "不得扫描附件",
            "本地 `product_context.json`",
        ]:
            self.assertIn(required, self.skill)

    def test_generation_never_consumes_future_result_authorization(self) -> None:
        required = [
            "`status=awaiting_authorization`",
            "已存在的具体生成结果",
            "禁止预授权未来图片",
            "`generated_image_sha256`",
        ]
        for document_name, document in self.delivery_documents.items():
            with self.subTest(document=document_name):
                for token in required:
                    self.assertIn(token, document)

    def test_delivery_identity_is_complete_and_result_specific(self) -> None:
        required = [
            "`workflow_mode`",
            "`product_id`",
            "`record_id`",
            "`front_file_token`",
            "`front_file_name`",
            "五项 Mask 身份摘要",
            "`source_sha256`",
            "`geometry_sha256`",
            "`prepared_sha256`",
            "`mask_sha256`",
            "`generated_image_sha256`",
            "`local_file`",
            "绝对路径",
            "`local_file_sha256`",
            "`target_filename`",
            "`field_id`",
        ]
        for document_name, document in self.delivery_documents.items():
            with self.subTest(document=document_name):
                for token in required:
                    self.assertIn(token, document)

    def test_nested_success_response_uses_unique_filename_match(self) -> None:
        required = [
            "`data.attachments[record_id][field_id]`",
            "按 `target_filename` 唯一匹配",
            "非空 `file_token`",
            "不得取数组首项",
        ]
        for document_name, document in self.delivery_documents.items():
            with self.subTest(document=document_name):
                for token in required:
                    self.assertIn(token, document)

    def test_upload_receipt_state_machine_is_write_ahead_and_non_retrying(self) -> None:
        required = [
            "调用上传接口前",
            "`status=uploading`",
            "完整交付身份",
            "收到任意响应",
            "原始响应",
            "`status=completed`",
            "`status=failed`",
            "`status=unknown`",
            "禁止自动重传",
        ]
        for document_name, document in self.delivery_documents.items():
            with self.subTest(document=document_name):
                for token in required:
                    self.assertIn(token, document)

    def test_versioned_receipts_are_the_only_replay_barrier(self) -> None:
        required = [
            "`upload_receipt.<target-stem>.json`",
            "通用 `upload_receipt.json`",
            "不得作为防重依据",
            "完整交付身份完全相同",
        ]
        for document_name, document in self.delivery_documents.items():
            with self.subTest(document=document_name):
                for token in required:
                    self.assertIn(token, document)

    def test_reference_documents_prompt_source_and_runtime_boundaries(self) -> None:
        for required in [
            "scripts/build_white_background_prompt.py",
            "不得手写、改造或替换模型提示词",
            "Mask 之外的产品区域不得重绘",
            "单 image、单 mask",
            "不输出内容、构图、占比或尺寸 QC",
        ]:
            self.assertIn(required, self.reference)

    def test_agent_metadata_only_describes_background_edit_workflow(self) -> None:
        for required in [
            "飞书最新首张正面图",
            "逐图云端视觉几何",
            "固定检测工具",
            "原图",
            "Mask Edit",
            "Yuan Studio 水印",
            "用户授权",
        ]:
            self.assertIn(required, self.agent)
        for forbidden in ["细节图", "Generation", "双阶段", "双 QC", "双QC"]:
            self.assertNotIn(forbidden, self.agent)

    def test_docs_do_not_name_retired_providers_or_skills(self) -> None:
        forbidden = (
            bytes.fromhex("61697265697465722d696d6167652d67656e65726174696f6e").decode(),
            bytes.fromhex("61697265697465725f696d6167655f68656c7065722e7079").decode(),
            bytes.fromhex("3c41495245495445525f524f4f543e").decode(),
        )
        lowered = self.combined_docs.lower()
        for token in forbidden:
            self.assertNotIn(token.lower(), lowered)


if __name__ == "__main__":
    unittest.main()
