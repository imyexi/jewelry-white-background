from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scripts.run_sy1537_sy1552_white_background as pipeline

from scripts.run_sy1537_sy1552_white_background import (
    Attachment,
    ProductRecord,
    TARGET_PRODUCT_IDS,
    _automatic_audit_decision,
    _draft_reference_plan,
    _derive_upload_decision,
    _upload_file_token,
    build_watermark_command,
    build_parser,
    build_size_override_record,
    is_size_only_failure,
    plan_upload,
    select_reference_attachments,
    watermarked_filename,
    workspace_relative_output,
)


def attachment(token: str, name: str | None = None) -> Attachment:
    return Attachment(token=token, name=name or f"{token}.jpg")


def write_geometry_profile_stub(product_root: Path, product_id: str) -> Path:
    profile_path = product_root / "geometry" / f"{product_id}.json"
    pipeline.write_json(
        profile_path,
        {
            "schema_version": "vision-geometry-mask-2.0",
            "product_id": product_id,
        },
    )
    return profile_path


def write_front_source_context(product_root: Path, product_id: str) -> Path:
    source_path = product_root / "source" / f"{product_id}_front.jpg"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"original-front")
    pipeline.write_json(
        product_root / "product_context.json",
        {
            "product_id": product_id,
            "selected_references": [
                {
                    "role": "front",
                    "local_path": f"source/{product_id}_front.jpg",
                    "preprocessed_path": "preprocessed/01_front.jpg",
                }
            ],
        },
    )
    return source_path


def write_delivery_state(
    product_root: Path,
    product_id: str = "SY1537",
) -> tuple[Path, Path, Path, Path]:
    attempt_root = product_root / "attempt-01"
    prepared = attempt_root / "prepared" / f"{product_id}_front.png"
    mask = attempt_root / "mask" / f"{product_id}_product-protection-mask.png"
    generated = attempt_root / "generated" / f"{product_id}.png"
    report = attempt_root / "logs" / f"{product_id}_vision-mask-report.json"
    for path, content in (
        (prepared, b"prepared"),
        (mask, b"mask"),
        (generated, b"generated"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    identity = {
        "product_id": product_id,
        "source_sha256": "A" * 64,
        "geometry_sha256": "B" * 64,
        "prepared_sha256": pipeline.file_sha256(prepared),
        "mask_sha256": pipeline.file_sha256(mask),
    }
    pipeline.write_json(
        report,
        {
            **identity,
            "status": "ok",
            "automatic_wawapi_edit_allowed": True,
            "reasons": [],
        },
    )
    pipeline.write_json(
        product_root / "manifests" / "generation_state.json",
        {
            **identity,
            "workflow_mode": "background_only_edit",
            "status": "completed",
            "attempt": 1,
            "attempt_root": str(attempt_root),
            "edit_image": str(prepared),
            "mask_image": str(mask),
            "mask_status": "ok",
            "mask_assessment": str(report),
            "automatic_wawapi_edit_allowed": True,
            "generated_image": str(generated),
            "generated_image_sha256": pipeline.file_sha256(generated),
        },
    )
    return prepared, mask, generated, report


def success_upload_receipt(
    *,
    product_id: str,
    record_id: str,
    generated: Path,
    watermarked: Path,
    token: str,
) -> dict[str, object]:
    resolved = watermarked.resolve()
    return {
        "product_id": product_id,
        "record_id": record_id,
        "field_id": pipeline.FIELD_MAIN,
        "status": "uploaded",
        "workflow_mode": "background_only_edit",
        "generated_image_sha256": pipeline.file_sha256(generated),
        "local_file": str(resolved),
        "target_filename": resolved.name,
        "local_file_sha256": pipeline.file_sha256(resolved),
        "uploaded_attachment": {
            "token": token,
            "name": resolved.name,
            "size": 0,
        },
    }


class Sy1537Sy1552PipelineTests(unittest.TestCase):
    def test_target_ids_are_exact_contiguous_range(self) -> None:
        self.assertEqual(
            TARGET_PRODUCT_IDS,
            tuple(f"SY{number}" for number in range(1537, 1553)),
        )

    def test_deliver_cli_requires_explicit_nonblank_authorization_reference(self) -> None:
        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["deliver"])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["deliver", "--authorization-reference", "   "]
            )

        args = parser.parse_args(
            ["deliver", "--authorization-reference", "  本次明确授权  "]
        )
        self.assertEqual(args.authorization_reference, "本次明确授权")

    def test_delivery_entry_points_reject_blank_authorization_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            product_root = run_root / "SY1537"
            with (
                patch.object(
                    pipeline,
                    "run_command",
                    side_effect=AssertionError("空授权时不得加水印"),
                ),
                patch.object(
                    pipeline,
                    "_upload_attachment",
                    side_effect=AssertionError("空授权时不得上传"),
                ),
                self.assertRaisesRegex(ValueError, "授权"),
            ):
                pipeline.watermark_and_upload(
                    product_root,
                    ProductRecord("SY1537", "rec1", (), (), (), ()),
                    "   ",
                )

            with (
                patch.object(
                    pipeline,
                    "_delivery_record_from_context",
                    side_effect=AssertionError("空授权时不得读取交付上下文"),
                ),
                patch.object(
                    pipeline,
                    "watermark_and_upload",
                    side_effect=AssertionError("空授权时不得进入单款交付"),
                ),
                self.assertRaisesRegex(ValueError, "授权"),
            ):
                pipeline.deliver_selected(
                    run_root,
                    ["SY1537"],
                    "\t",
                )

    def test_reference_selection_uses_only_first_valid_front_image(self) -> None:
        record = ProductRecord(
            product_id="SY1537",
            record_id="rec1",
            front_images=(attachment("not-image", "note.txt"), attachment("front"), attachment("front2")),
            product_images=(attachment("p1"), attachment("p2"), attachment("p3")),
            side_images=(attachment("side1"), attachment("side2")),
            main_images=(),
        )

        selected = select_reference_attachments(record)

        self.assertEqual(
            [item.token for item in selected],
            ["front"],
        )

    def test_reference_selection_deduplicates_tokens_without_reordering(self) -> None:
        record = ProductRecord(
            product_id="SY1537",
            record_id="rec1",
            front_images=(attachment("front"),),
            product_images=(attachment("front"), attachment("p1")),
            side_images=(attachment("p1"), attachment("side1")),
            main_images=(),
        )

        selected = select_reference_attachments(record)

        self.assertEqual([item.token for item in selected], ["front"])

    def test_draft_plan_is_v3_background_only_edit_with_front_as_structure_source(self) -> None:
        product_root = Path("run/SY1537")
        front = product_root / "preprocessed" / "01_front.jpg"

        plan = _draft_reference_plan("SY1537", product_root, [front])

        self.assertEqual(plan["schema_version"], "3.0")
        self.assertEqual(plan["workflow_mode"], "background_only_edit")
        self.assertEqual(plan["front_image"], "preprocessed/01_front.jpg")
        self.assertEqual(plan["detail_images"], [])
        self.assertEqual(plan["structure"]["source_image"], plan["front_image"])

    def test_draft_plan_passes_the_real_v3_validator_without_manual_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            front = product_root / "preprocessed" / "01_front.jpg"
            front.parent.mkdir(parents=True)
            front.write_bytes(b"front")
            plan_path = product_root / "reference_plan.json"
            pipeline.write_json(
                plan_path,
                _draft_reference_plan("SY1537", product_root, [front]),
            )

            pipeline.run_command(
                [
                    pipeline.sys.executable,
                    str(pipeline.VALIDATE_PLAN_SCRIPT),
                    "--reference-plan",
                    str(plan_path),
                    "--check-files",
                ]
            )

    def test_prepare_uses_downloaded_source_without_generating_preprocessed_jpeg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            record = ProductRecord(
                product_id="SY1537",
                record_id="rec1",
                front_images=(attachment("front", "front.png"),),
                product_images=(),
                side_images=(),
                main_images=(attachment("main", "main.png"),),
            )

            def fake_download(_record, _attachment, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"source")
                return target

            with (
                patch.object(pipeline, "_download_attachment", side_effect=fake_download),
                patch.object(
                    pipeline,
                    "_run_preprocess",
                    side_effect=AssertionError("不得生成未被 Edit 消费的预处理 JPEG"),
                    create=True,
                ),
            ):
                prepared = pipeline.prepare_product(record, run_root)

            product_root = run_root / "SY1537"
            source_path = product_root / "source" / "01_front_front.png"
            self.assertEqual(prepared.reference_images, (source_path,))
            self.assertFalse((product_root / "preprocessed").exists())
            context = pipeline.read_json(product_root / "product_context.json")
            self.assertNotIn("preprocessed_path", context["selected_references"][0])
            plan = pipeline.read_json(product_root / "reference_plan.draft.json")
            self.assertEqual(plan["front_image"], "source/01_front_front.png")
            self.assertEqual(plan["structure"]["source_image"], plan["front_image"])

    def test_generate_builds_mask_assets_then_edits_without_output_qc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            front = product_root / "preprocessed" / "01_front.jpg"
            front.parent.mkdir(parents=True)
            front.write_bytes(b"front")
            pipeline.write_json(
                product_root / "reference_plan.json",
                {
                    "schema_version": "3.0",
                    "workflow_mode": "background_only_edit",
                    "product_id": "SY1537",
                    "front_image": "preprocessed/01_front.jpg",
                    "detail_images": [],
                    "structure": {"source_image": "preprocessed/01_front.jpg"},
                },
            )
            geometry_profile = write_geometry_profile_stub(product_root, "SY1537")
            source_front = write_front_source_context(product_root, "SY1537")
            events = []

            def fake_run_command(command, **kwargs):
                if str(pipeline.BUILD_PROMPT_SCRIPT) in command:
                    prompt_path = Path(command[command.index("--output") + 1])
                    prompt_path.parent.mkdir(parents=True, exist_ok=True)
                    prompt_path.write_text("fixed edit prompt", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def fake_create_assets(
                input_path,
                image_path,
                mask_path,
                overlay_path,
                report_path,
                profile_path,
                product_id,
            ):
                events.append(
                    (
                        "mask",
                        Path(input_path),
                        Path(image_path),
                        Path(mask_path),
                        Path(profile_path),
                        product_id,
                    )
                )
                for path in (image_path, mask_path, overlay_path):
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    Path(path).write_bytes(b"asset")
                assessment = SimpleNamespace(
                    status="ok",
                    reasons=[],
                    automatic_wawapi_edit_allowed=True,
                    protected_ratio=0.25,
                    border_contact_ratio=0.0,
                    inner_editable_ratio=1.0,
                    background_noise_p90=4.0,
                    geometry_confidence=0.9,
                )
                pipeline.write_json(
                    Path(report_path),
                    {
                        **vars(assessment),
                        "product_id": product_id,
                        "source_sha256": pipeline.file_sha256(Path(input_path)),
                        "geometry_sha256": "B" * 64,
                        "prepared_sha256": pipeline.file_sha256(Path(image_path)),
                        "mask_sha256": pipeline.file_sha256(Path(mask_path)),
                    },
                )
                return assessment

            def fake_edit(prompt, image, mask, output_path, log_path):
                events.append(
                    (
                        "edit",
                        prompt,
                        Path(image),
                        Path(mask),
                        Path(output_path),
                    )
                )
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"generated")
                return SimpleNamespace(
                    provider="wawapi",
                    task_id="task-1",
                    helper_output=Path(output_path),
                )

            with (
                patch.object(pipeline, "run_command", side_effect=fake_run_command),
                patch.object(
                    pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                    create=True,
                ) as geometry_loader,
                patch.object(pipeline, "create_background_edit_assets", side_effect=fake_create_assets, create=True),
                patch.object(pipeline, "edit_background_to_path", side_effect=fake_edit, create=True),
                patch.object(pipeline, "generate_to_path", side_effect=AssertionError("generation must not run"), create=True),
                patch.object(pipeline, "_evaluate", side_effect=AssertionError("output QC must not run")),
                patch.object(pipeline, "_create_contact_sheet", side_effect=AssertionError("contact sheet must not run")),
            ):
                state = pipeline.generate_and_evaluate(product_root)

            self.assertEqual([event[0] for event in events], ["mask", "edit"])
            self.assertEqual(events[0][1], source_front)
            self.assertEqual(
                events[0][2],
                product_root / "attempt-01" / "prepared" / "SY1537_front.png",
            )
            self.assertEqual(
                events[0][3],
                product_root
                / "attempt-01"
                / "mask"
                / "SY1537_product-protection-mask.png",
            )
            self.assertEqual(events[0][4], geometry_profile)
            self.assertEqual(events[0][5], "SY1537")
            geometry_loader.assert_called_once_with(geometry_profile, "SY1537")
            self.assertEqual(events[1][1], "fixed edit prompt")
            self.assertEqual(events[1][2], events[0][2])
            self.assertEqual(events[1][3], events[0][3])
            wawapi_inputs = "\n".join(
                (events[1][1], str(events[1][2]), str(events[1][3]))
            ).lower()
            for forbidden in (
                "geometry",
                "overlay",
                "detection",
                "levels",
                "色阶",
                str(geometry_profile).lower(),
            ):
                self.assertNotIn(forbidden, wawapi_inputs)
            self.assertEqual(state["provider"], "wawapi")
            self.assertEqual(state["task_id"], "task-1")
            self.assertEqual(state["generated_image"], str(events[1][4]))
            self.assertEqual(state["workflow_mode"], "background_only_edit")
            self.assertEqual(state["status"], "completed")
            self.assertIs(state["automatic_wawapi_edit_allowed"], True)
            self.assertEqual(state["source_sha256"], pipeline.file_sha256(source_front))
            self.assertEqual(state["geometry_sha256"], "B" * 64)
            self.assertEqual(state["prepared_sha256"], pipeline.file_sha256(events[0][2]))
            self.assertEqual(state["mask_sha256"], pipeline.file_sha256(events[0][3]))
            self.assertNotIn("pre_watermark_qc", state)
            self.assertEqual(
                pipeline.read_json(product_root / "manifests" / "generation_state.json")["generated_image"],
                state["generated_image"],
            )

    def test_generate_requires_explicit_vision_geometry_profile_before_any_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            front = product_root / "preprocessed" / "01_front.jpg"
            front.parent.mkdir(parents=True)
            front.write_bytes(b"front")
            pipeline.write_json(
                product_root / "reference_plan.json",
                {
                    "schema_version": "3.0",
                    "workflow_mode": "background_only_edit",
                    "product_id": "SY1537",
                    "front_image": "preprocessed/01_front.jpg",
                    "detail_images": [],
                    "structure": {"source_image": "preprocessed/01_front.jpg"},
                },
            )

            with (
                patch.object(
                    pipeline,
                    "run_command",
                    side_effect=AssertionError("缺少 profile 时不得执行命令"),
                ),
                patch.object(
                    pipeline,
                    "create_background_edit_assets",
                    side_effect=AssertionError("缺少 profile 时不得创建资产"),
                ),
                patch.object(
                    pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("缺少 profile 时不得调用 Wawapi"),
                ),
                self.assertRaisesRegex(FileNotFoundError, "视觉几何.*SY1537"),
            ):
                pipeline.generate_and_evaluate(product_root)

    def test_generate_rechecks_prepared_and_mask_hashes_immediately_before_wawapi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            source_front = product_root / "source" / "01_front.png"
            source_front.parent.mkdir(parents=True)
            source_front.write_bytes(b"source")
            pipeline.write_json(
                product_root / "product_context.json",
                {
                    "product_id": "SY1537",
                    "record_id": "rec1",
                    "selected_references": [
                        {"role": "front", "local_path": "source/01_front.png"}
                    ],
                },
            )
            plan = product_root / "reference_plan.json"
            pipeline.write_json(
                plan,
                {
                    "product_id": "SY1537",
                    "front_image": "source/01_front.png",
                    "detail_images": [],
                    "structure": {"source_image": "source/01_front.png"},
                },
            )
            geometry = product_root / "geometry" / "SY1537.json"
            geometry.parent.mkdir(parents=True)
            geometry.write_text("{}", encoding="utf-8")

            def fake_run_command(command, **kwargs):
                if str(pipeline.BUILD_PROMPT_SCRIPT) in command:
                    output = Path(command[command.index("--output") + 1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text("fixed edit prompt", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def fake_assets(input_path, image_path, mask_path, overlay_path, report_path, profile_path, product_id):
                for path in (image_path, mask_path, overlay_path):
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    Path(path).write_bytes(b"asset")
                pipeline.write_json(
                    Path(report_path),
                    {
                        "status": "ok",
                        "automatic_wawapi_edit_allowed": True,
                        "product_id": product_id,
                        "source_sha256": pipeline.file_sha256(Path(input_path)),
                        "geometry_sha256": "B" * 64,
                        "prepared_sha256": pipeline.file_sha256(Path(image_path)),
                        "mask_sha256": pipeline.file_sha256(Path(mask_path)),
                    },
                )
                Path(mask_path).write_bytes(b"replaced-after-report")
                return SimpleNamespace(
                    status="ok", reasons=[], automatic_wawapi_edit_allowed=True
                )

            with (
                patch.object(pipeline, "run_command", side_effect=fake_run_command),
                patch.object(
                    pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(schema_version="vision-geometry-mask-2.0"),
                ),
                patch.object(pipeline, "create_background_edit_assets", side_effect=fake_assets),
                patch.object(
                    pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("identity mismatch must block Wawapi"),
                ),
                self.assertRaisesRegex(RuntimeError, "Mask.*发生变化|身份"),
            ):
                pipeline.generate_and_evaluate(product_root)

    def test_generate_blocks_ok_status_when_automatic_wawapi_flag_is_not_true(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            front = product_root / "preprocessed" / "01_front.jpg"
            front.parent.mkdir(parents=True)
            front.write_bytes(b"front")
            pipeline.write_json(
                product_root / "reference_plan.json",
                {
                    "schema_version": "3.0",
                    "workflow_mode": "background_only_edit",
                    "product_id": "SY1537",
                    "front_image": "preprocessed/01_front.jpg",
                    "detail_images": [],
                    "structure": {"source_image": "preprocessed/01_front.jpg"},
                },
            )
            geometry_profile = write_geometry_profile_stub(product_root, "SY1537")
            write_front_source_context(product_root, "SY1537")

            def fake_run_command(command, **kwargs):
                if str(pipeline.BUILD_PROMPT_SCRIPT) in command:
                    output = Path(command[command.index("--output") + 1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text("fixed edit prompt", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def fake_create_assets(*args):
                report_path = Path(args[4])
                assessment = SimpleNamespace(
                    status="ok",
                    reasons=["automatic_wawapi_edit_not_allowed"],
                    automatic_wawapi_edit_allowed=False,
                )
                pipeline.write_json(report_path, vars(assessment))
                return assessment

            with (
                patch.object(pipeline, "run_command", side_effect=fake_run_command),
                patch.object(
                    pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                    create=True,
                ) as geometry_loader,
                patch.object(
                    pipeline,
                    "create_background_edit_assets",
                    side_effect=fake_create_assets,
                ),
                patch.object(
                    pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("布尔放行位不是 True 时不得调用 Wawapi"),
                ),
                self.assertRaisesRegex(RuntimeError, "automatic_wawapi_edit_allowed"),
            ):
                pipeline.generate_and_evaluate(product_root)

            geometry_loader.assert_called_once_with(geometry_profile, "SY1537")
            state = pipeline.read_json(
                product_root / "manifests" / "generation_state.json"
            )
            self.assertEqual(state["status"], "blocked_by_mask_gate")
            self.assertEqual(state["mask_status"], "ok")
            self.assertIs(state["automatic_wawapi_edit_allowed"], False)

    def test_generate_requires_explicit_original_front_context_without_preprocessed_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            front = product_root / "preprocessed" / "01_front.jpg"
            front.parent.mkdir(parents=True)
            front.write_bytes(b"preprocessed-front")
            pipeline.write_json(
                product_root / "reference_plan.json",
                {
                    "schema_version": "3.0",
                    "workflow_mode": "background_only_edit",
                    "product_id": "SY1537",
                    "front_image": "preprocessed/01_front.jpg",
                    "detail_images": [],
                    "structure": {"source_image": "preprocessed/01_front.jpg"},
                },
            )
            geometry_profile = write_geometry_profile_stub(product_root, "SY1537")

            with (
                patch.object(
                    pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                    create=True,
                ) as geometry_loader,
                patch.object(
                    pipeline,
                    "run_command",
                    side_effect=AssertionError("缺少原图上下文时不得执行命令"),
                ),
                patch.object(
                    pipeline,
                    "create_background_edit_assets",
                    side_effect=AssertionError("不得回退到预处理图创建资产"),
                ),
                patch.object(
                    pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("缺少原图上下文时不得调用 Wawapi"),
                ),
                self.assertRaisesRegex(FileNotFoundError, "原始正面图上下文"),
            ):
                pipeline.generate_and_evaluate(product_root)

            geometry_loader.assert_called_once_with(geometry_profile, "SY1537")

    def test_generate_blocks_review_mask_before_edit_and_keeps_gate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            front = product_root / "preprocessed" / "01_front.jpg"
            front.parent.mkdir(parents=True)
            front.write_bytes(b"front")
            pipeline.write_json(
                product_root / "reference_plan.json",
                {
                    "schema_version": "3.0",
                    "workflow_mode": "background_only_edit",
                    "product_id": "SY1537",
                    "front_image": "preprocessed/01_front.jpg",
                    "detail_images": [],
                    "structure": {"source_image": "preprocessed/01_front.jpg"},
                },
            )
            write_geometry_profile_stub(product_root, "SY1537")
            write_front_source_context(product_root, "SY1537")

            def fake_run_command(command, **kwargs):
                if str(pipeline.BUILD_PROMPT_SCRIPT) in command:
                    output = Path(command[command.index("--output") + 1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text("fixed edit prompt", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def fake_create_assets(*args):
                report_path = args[4]
                assessment = SimpleNamespace(
                    status="review",
                    reasons=["geometry_confidence_low"],
                    automatic_wawapi_edit_allowed=False,
                    protected_ratio=0.25,
                    border_contact_ratio=0.0,
                    inner_editable_ratio=1.0,
                    background_noise_p90=4.0,
                    geometry_confidence=0.4,
                )
                pipeline.write_json(Path(report_path), vars(assessment))
                return assessment

            with (
                patch.object(pipeline, "run_command", side_effect=fake_run_command),
                patch.object(
                    pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                    create=True,
                ),
                patch.object(pipeline, "create_background_edit_assets", side_effect=fake_create_assets, create=True),
                patch.object(pipeline, "edit_background_to_path", side_effect=AssertionError("edit must not run"), create=True),
                self.assertRaisesRegex(RuntimeError, "Mask.*review"),
            ):
                pipeline.generate_and_evaluate(product_root)

            reports = list(
                product_root.glob("attempt-*/logs/SY1537_vision-mask-report.json")
            )
            self.assertEqual(len(reports), 1)
            self.assertEqual(pipeline.read_json(reports[0])["status"], "review")

    def test_failed_retry_invalidates_previous_completed_state_and_never_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            front = product_root / "preprocessed" / "01_front.jpg"
            front.parent.mkdir(parents=True)
            front.write_bytes(b"front")
            pipeline.write_json(
                product_root / "reference_plan.json",
                {
                    "schema_version": "3.0",
                    "workflow_mode": "background_only_edit",
                    "product_id": "SY1537",
                    "front_image": "preprocessed/01_front.jpg",
                    "detail_images": [],
                    "structure": {"source_image": "preprocessed/01_front.jpg"},
                },
            )
            write_geometry_profile_stub(product_root, "SY1537")
            write_front_source_context(product_root, "SY1537")
            previous_generated = product_root / "attempt-01" / "generated" / "SY1537.png"
            previous_generated.parent.mkdir(parents=True)
            previous_generated.write_bytes(b"previous-edit")
            previous_report = product_root / "attempt-01" / "manifests" / "mask_assessment.json"
            pipeline.write_json(previous_report, {"status": "ok"})
            pipeline.write_json(
                product_root / "manifests" / "generation_state.json",
                {
                    "workflow_mode": "background_only_edit",
                    "status": "completed",
                    "product_id": "SY1537",
                    "attempt": 1,
                    "generated_image": str(previous_generated),
                    "generated_image_sha256": pipeline.file_sha256(previous_generated),
                    "mask_status": "ok",
                    "mask_assessment": str(previous_report),
                },
            )

            def fake_run_command(command, **kwargs):
                if str(pipeline.BUILD_PROMPT_SCRIPT) in command:
                    output = Path(command[command.index("--output") + 1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text("fixed edit prompt", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def fake_create_assets(*args):
                report_path = args[4]
                assessment = SimpleNamespace(
                    status="fail",
                    reasons=["protected_region_touches_border"],
                    automatic_wawapi_edit_allowed=False,
                )
                pipeline.write_json(Path(report_path), vars(assessment))
                return assessment

            with (
                patch.object(pipeline, "run_command", side_effect=fake_run_command),
                patch.object(
                    pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                    create=True,
                ),
                patch.object(pipeline, "create_background_edit_assets", side_effect=fake_create_assets),
                patch.object(pipeline, "edit_background_to_path", side_effect=AssertionError("edit must not run")),
                self.assertRaisesRegex(RuntimeError, "Mask.*fail"),
            ):
                pipeline.generate_and_evaluate(product_root, retry=True)

            state = pipeline.read_json(product_root / "manifests" / "generation_state.json")
            self.assertEqual(state["workflow_mode"], "background_only_edit")
            self.assertEqual(state["status"], "blocked_by_mask_gate")
            self.assertEqual(state["attempt"], 2)
            self.assertEqual(state["mask_status"], "fail")
            with (
                patch.object(pipeline, "_upload_attachment", side_effect=AssertionError("upload must not run")),
                self.assertRaisesRegex(RuntimeError, "completed"),
            ):
                pipeline.watermark_and_upload(
                    product_root,
                    ProductRecord("SY1537", "rec1", (), (), (), ()),
                    "用户已授权追加",
                )

    def test_watermark_upload_stops_after_ok_response_without_qc_or_record_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            _prepared, _mask, generated, _mask_report = write_delivery_state(product_root)
            watermarked = product_root / "white-bg" / "SY1537_v2_watermarked.png"
            watermarked.parent.mkdir(parents=True)
            watermarked.write_bytes(b"legacy-watermarked")
            record = ProductRecord("SY1537", "rec1", (), (), (), ())
            upload_response = {"ok": True, "data": {"file_tokens": ["uploaded-token"]}}
            real_file_sha256 = pipeline.file_sha256
            upload_finished = False

            def fake_watermark(command, **kwargs):
                self.assertEqual(command[0], "watermark")
                watermarked.write_bytes(b"fresh-watermarked")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def guarded_file_sha256(path):
                if upload_finished:
                    raise AssertionError("上传响应后不得再次读取或扫描本地图片")
                return real_file_sha256(path)

            def fake_upload(record_id, image_path):
                nonlocal upload_finished
                self.assertEqual((record_id, image_path), ("rec1", watermarked))
                upload_finished = True
                return upload_response

            with (
                patch.object(pipeline, "build_watermark_command", return_value=(["watermark"], watermarked)),
                patch.object(pipeline, "run_command", side_effect=fake_watermark) as watermark_mock,
                patch.object(pipeline, "file_sha256", side_effect=guarded_file_sha256),
                patch.object(pipeline, "_upload_attachment", side_effect=fake_upload) as upload_mock,
                patch.object(pipeline, "_evaluate", side_effect=AssertionError("QC must not run")),
                patch.object(pipeline, "_review_is_approved", side_effect=AssertionError("manual review must not run")),
                patch.object(pipeline.time, "sleep", side_effect=AssertionError("sleep must not run")),
            ):
                receipt = pipeline.watermark_and_upload(
                    product_root,
                    record,
                    "用户已授权追加",
                    allow_size_override=True,
                    append_version="v2",
                )

            upload_mock.assert_called_once_with("rec1", watermarked)
            watermark_mock.assert_called_once_with(["watermark"])
            self.assertEqual(watermarked.read_bytes(), b"fresh-watermarked")
            self.assertEqual(receipt["status"], "uploaded")
            self.assertEqual(receipt["uploaded_attachment"]["token"], "uploaded-token")
            self.assertEqual(receipt["generated_image_sha256"], pipeline.file_sha256(generated))
            self.assertEqual(
                pipeline.read_json(product_root / "manifests" / "upload_receipt.json")["uploaded_attachment"]["token"],
                "uploaded-token",
            )

    def test_matching_local_upload_receipt_blocks_duplicate_watermark_and_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            _prepared, _mask, generated, _report = write_delivery_state(product_root)
            watermarked = product_root / "white-bg" / "SY1537_v2_watermarked.png"
            watermarked.parent.mkdir(parents=True)
            watermarked.write_bytes(b"already-watermarked")
            receipt = success_upload_receipt(
                product_id="SY1537",
                record_id="rec1",
                generated=generated,
                watermarked=watermarked,
                token="already-uploaded-token",
            )
            pipeline.write_json(
                product_root / "manifests" / "upload_receipt.json",
                receipt,
            )

            with (
                patch.object(
                    pipeline,
                    "build_watermark_command",
                    return_value=(["watermark"], watermarked),
                ),
                patch.object(
                    pipeline,
                    "run_command",
                    side_effect=AssertionError("重复交付不得重新加水印"),
                ),
                patch.object(
                    pipeline,
                    "_upload_attachment",
                    side_effect=AssertionError("重复交付不得再次上传"),
                ),
            ):
                replay = pipeline.watermark_and_upload(
                    product_root,
                    ProductRecord("SY1537", "rec1", (), (), (), ()),
                    "本次明确授权",
                    append_version="v2",
                )

            self.assertEqual(replay, receipt)

    def test_receipt_identity_mismatch_never_short_circuits_delivery(self) -> None:
        mutations = {
            "workflow_mode": lambda receipt, _target: receipt.update(
                workflow_mode="other"
            ),
            "product_id": lambda receipt, _target: receipt.update(
                product_id="SY9999"
            ),
            "record_id": lambda receipt, _target: receipt.update(
                record_id="other-record"
            ),
            "field_id": lambda receipt, _target: receipt.update(
                field_id="other-field"
            ),
            "generated_image_sha256": lambda receipt, _target: receipt.update(
                generated_image_sha256="A" * 64
            ),
            "resolved_path": lambda receipt, target: receipt.update(
                local_file=str(target.parent / "alias.png")
            ),
            "target_filename": lambda receipt, _target: receipt.update(
                target_filename="other.png"
            ),
            "uploaded_token": lambda receipt, _target: receipt[
                "uploaded_attachment"
            ].update(token=""),
        }
        for field_name, mutate in mutations.items():
            with self.subTest(field=field_name), tempfile.TemporaryDirectory() as directory:
                product_root = Path(directory) / "SY1537"
                _prepared, _mask, generated, _report = write_delivery_state(
                    product_root
                )
                watermarked = (
                    product_root / "white-bg" / "SY1537_v2_watermarked.png"
                )
                watermarked.parent.mkdir(parents=True)
                watermarked.write_bytes(b"previous-watermark")
                receipt = success_upload_receipt(
                    product_id="SY1537",
                    record_id="rec1",
                    generated=generated,
                    watermarked=watermarked,
                    token="previous-token",
                )
                mutate(receipt, watermarked)
                pipeline.write_json(
                    product_root / "manifests" / "upload_receipt.json",
                    receipt,
                )

                def fake_watermark(_command, **_kwargs):
                    watermarked.write_bytes(b"new-watermark")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                with (
                    patch.object(
                        pipeline,
                        "build_watermark_command",
                        return_value=(["watermark"], watermarked),
                    ),
                    patch.object(
                        pipeline,
                        "run_command",
                        side_effect=fake_watermark,
                    ) as watermark_mock,
                    patch.object(
                        pipeline,
                        "_upload_attachment",
                        return_value={
                            "ok": True,
                            "data": {"file_tokens": ["new-token"]},
                        },
                    ) as upload_mock,
                ):
                    delivered = pipeline.watermark_and_upload(
                        product_root,
                        ProductRecord("SY1537", "rec1", (), (), (), ()),
                        "本次明确授权",
                        append_version="v2",
                    )

                watermark_mock.assert_called_once_with(["watermark"])
                upload_mock.assert_called_once_with("rec1", watermarked)
                self.assertEqual(
                    delivered["uploaded_attachment"]["token"], "new-token"
                )

    def test_append_versions_keep_independent_receipts_and_replay_without_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            write_delivery_state(product_root)
            targets = {
                "v1": product_root / "white-bg" / "SY1537_v1_watermarked.png",
                "v2": product_root / "white-bg" / "SY1537_v2_watermarked.png",
            }

            def build_for_version(**kwargs):
                return ["watermark", kwargs["append_version"]], targets[
                    kwargs["append_version"]
                ]

            def fake_watermark(command, **_kwargs):
                version = command[1]
                targets[version].parent.mkdir(parents=True, exist_ok=True)
                targets[version].write_bytes(f"watermark-{version}".encode("ascii"))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            upload_tokens = iter(("token-v1", "token-v2"))

            def fake_upload(_record_id, _path):
                return {
                    "ok": True,
                    "data": {"file_tokens": [next(upload_tokens)]},
                }

            with (
                patch.object(
                    pipeline,
                    "build_watermark_command",
                    side_effect=build_for_version,
                ),
                patch.object(pipeline, "run_command", side_effect=fake_watermark),
                patch.object(
                    pipeline,
                    "_upload_attachment",
                    side_effect=fake_upload,
                ),
            ):
                first = pipeline.watermark_and_upload(
                    product_root,
                    ProductRecord("SY1537", "rec1", (), (), (), ()),
                    "授权 v1",
                    append_version="v1",
                )
                second = pipeline.watermark_and_upload(
                    product_root,
                    ProductRecord("SY1537", "rec1", (), (), (), ()),
                    "授权 v2",
                    append_version="v2",
                )

            receipt_v1 = (
                product_root
                / "manifests"
                / "upload_receipt.SY1537_v1_watermarked.json"
            )
            receipt_v2 = (
                product_root
                / "manifests"
                / "upload_receipt.SY1537_v2_watermarked.json"
            )
            self.assertTrue(receipt_v1.is_file())
            self.assertTrue(receipt_v2.is_file())
            self.assertEqual(
                pipeline.read_json(receipt_v1)["uploaded_attachment"]["token"],
                "token-v1",
            )
            self.assertEqual(
                pipeline.read_json(receipt_v2)["uploaded_attachment"]["token"],
                "token-v2",
            )
            self.assertEqual(first["target_filename"], targets["v1"].name)
            self.assertEqual(second["target_filename"], targets["v2"].name)

            with (
                patch.object(
                    pipeline,
                    "build_watermark_command",
                    side_effect=build_for_version,
                ),
                patch.object(
                    pipeline,
                    "run_command",
                    side_effect=AssertionError("v1 重放不得重新加水印"),
                ),
                patch.object(
                    pipeline,
                    "_upload_attachment",
                    side_effect=AssertionError("v1 重放不得再次上传"),
                ),
                patch.object(
                    Path,
                    "glob",
                    side_effect=AssertionError("不得扫描回执目录"),
                ),
                patch.object(
                    Path,
                    "iterdir",
                    side_effect=AssertionError("不得枚举回执目录"),
                ),
            ):
                replay = pipeline.watermark_and_upload(
                    product_root,
                    ProductRecord("SY1537", "rec1", (), (), (), ()),
                    "授权 v1",
                    append_version="v1",
                )

            self.assertEqual(replay["uploaded_attachment"]["token"], "token-v1")

    def test_existing_receipt_does_not_block_a_different_append_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            _prepared, _mask, generated, _report = write_delivery_state(product_root)
            previous = product_root / "white-bg" / "SY1537_v1_watermarked.png"
            current = product_root / "white-bg" / "SY1537_v2_watermarked.png"
            previous.parent.mkdir(parents=True)
            previous.write_bytes(b"previous-watermark")
            pipeline.write_json(
                product_root / "manifests" / "upload_receipt.json",
                {
                    "product_id": "SY1537",
                    "record_id": "rec1",
                    "status": "uploaded",
                    "generated_image_sha256": pipeline.file_sha256(generated),
                    "local_file": str(previous),
                    "local_file_sha256": pipeline.file_sha256(previous),
                },
            )

            def fake_watermark(_command, **_kwargs):
                current.write_bytes(b"current-watermark")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(
                    pipeline,
                    "build_watermark_command",
                    return_value=(["watermark"], current),
                ),
                patch.object(
                    pipeline,
                    "run_command",
                    side_effect=fake_watermark,
                ) as watermark_mock,
                patch.object(
                    pipeline,
                    "_upload_attachment",
                    return_value={
                        "ok": True,
                        "data": {"file_tokens": ["v2-token"]},
                    },
                ) as upload_mock,
            ):
                receipt = pipeline.watermark_and_upload(
                    product_root,
                    ProductRecord("SY1537", "rec1", (), (), (), ()),
                    "本次明确授权",
                    append_version="v2",
                )

            watermark_mock.assert_called_once_with(["watermark"])
            upload_mock.assert_called_once_with("rec1", current)
            self.assertEqual(receipt["uploaded_attachment"]["token"], "v2-token")

    def test_watermark_upload_requires_state_and_report_automatic_allow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            _prepared, _mask, _generated, report_path = write_delivery_state(product_root)
            state_path = product_root / "manifests" / "generation_state.json"

            for location in ("state", "report"):
                with self.subTest(location=location):
                    state = pipeline.read_json(state_path)
                    report = pipeline.read_json(report_path)
                    state["automatic_wawapi_edit_allowed"] = True
                    report["automatic_wawapi_edit_allowed"] = True
                    if location == "state":
                        state["automatic_wawapi_edit_allowed"] = False
                    else:
                        report["automatic_wawapi_edit_allowed"] = False
                    pipeline.write_json(state_path, state)
                    pipeline.write_json(report_path, report)
                    with (
                        patch.object(
                            pipeline,
                            "run_command",
                            side_effect=AssertionError("门禁失败时不得加水印"),
                        ),
                        patch.object(
                            pipeline,
                            "_upload_attachment",
                            side_effect=AssertionError("门禁失败时不得上传"),
                        ),
                        self.assertRaisesRegex(
                            RuntimeError,
                            "automatic_wawapi_edit_allowed",
                        ),
                    ):
                        pipeline.watermark_and_upload(
                            product_root,
                            ProductRecord("SY1537", "rec1", (), (), (), ()),
                            "用户已授权追加",
                        )

    def test_watermark_upload_requires_matching_product_source_geometry_and_mask_identities(self) -> None:
        mismatches = {
            "product_id": "SY9999",
            "source_sha256": "C" * 64,
            "geometry_sha256": "D" * 64,
            "prepared_sha256": "E" * 64,
            "mask_sha256": "F" * 64,
        }
        for key, mismatched_value in mismatches.items():
            with self.subTest(identity=key), tempfile.TemporaryDirectory() as directory:
                product_root = Path(directory) / "SY1537"
                _prepared, _mask, _generated, report_path = write_delivery_state(product_root)
                report = pipeline.read_json(report_path)
                report[key] = mismatched_value
                pipeline.write_json(report_path, report)

                with (
                    patch.object(
                        pipeline,
                        "run_command",
                        side_effect=AssertionError("身份错配时不得加水印"),
                    ),
                    patch.object(
                        pipeline,
                        "_upload_attachment",
                        side_effect=AssertionError("身份错配时不得上传"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "身份"),
                ):
                    pipeline.watermark_and_upload(
                        product_root,
                        ProductRecord("SY1537", "rec1", (), (), (), ()),
                        "用户已授权追加",
                    )

    def test_watermark_upload_rehashes_prepared_and_mask_files(self) -> None:
        for asset_name in ("prepared", "mask"):
            with self.subTest(asset=asset_name), tempfile.TemporaryDirectory() as directory:
                product_root = Path(directory) / "SY1537"
                prepared, mask, _generated, _report = write_delivery_state(product_root)
                {"prepared": prepared, "mask": mask}[asset_name].write_bytes(b"tampered")

                with (
                    patch.object(
                        pipeline,
                        "run_command",
                        side_effect=AssertionError("资产被篡改时不得加水印"),
                    ),
                    patch.object(
                        pipeline,
                        "_upload_attachment",
                        side_effect=AssertionError("资产被篡改时不得上传"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "SHA-256"),
                ):
                    pipeline.watermark_and_upload(
                        product_root,
                        ProductRecord("SY1537", "rec1", (), (), (), ()),
                        "用户已授权追加",
                    )

    def test_watermark_upload_rejects_generated_file_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            _prepared, _mask, generated, _report = write_delivery_state(product_root)
            generated.write_bytes(b"legacy-generation")

            with (
                patch.object(pipeline, "run_command", side_effect=AssertionError("watermark must not run")),
                patch.object(pipeline, "_upload_attachment", side_effect=AssertionError("upload must not run")),
                self.assertRaisesRegex(RuntimeError, "SHA-256"),
            ):
                pipeline.watermark_and_upload(
                    product_root,
                    ProductRecord("SY1537", "rec1", (), (), (), ()),
                    "用户已授权追加",
                )

    def test_watermark_upload_rejects_legacy_state_without_ok_mask_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            generated = product_root / "generated" / "legacy.png"
            generated.parent.mkdir(parents=True)
            generated.write_bytes(b"legacy-generation")
            pipeline.write_json(
                product_root / "manifests" / "generation_state.json",
                {
                    "product_id": "SY1537",
                    "generated_image": str(generated),
                },
            )
            record = ProductRecord("SY1537", "rec1", (), (), (), ())

            with (
                patch.object(
                    pipeline,
                    "_upload_attachment",
                    side_effect=AssertionError("legacy output must not upload"),
                ),
                self.assertRaisesRegex(RuntimeError, "Mask"),
            ):
                pipeline.watermark_and_upload(product_root, record, "用户已授权追加")

    def test_verify_is_not_an_active_cli_command(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["verify"])

    def test_remote_verification_helpers_are_removed_from_the_delivery_module(self) -> None:
        self.assertFalse(hasattr(pipeline, "verify_selected"))
        self.assertFalse(hasattr(pipeline, "_record_get"))
        self.assertFalse(hasattr(pipeline, "verify_append"))

    def test_upload_response_must_be_ok_and_contain_a_file_token(self) -> None:
        with self.assertRaises(RuntimeError):
            _upload_file_token(
                {"ok": False, "data": {"file_tokens": ["ignored"]}},
                record_id="rec1",
                field_id=pipeline.FIELD_MAIN,
                target_filename="target.png",
            )
        with self.assertRaises(RuntimeError):
            _upload_file_token(
                {"ok": True, "data": {}},
                record_id="rec1",
                field_id=pipeline.FIELD_MAIN,
                target_filename="target.png",
            )

    def test_upload_token_uses_exact_target_from_real_nested_lark_response(self) -> None:
        response_path = (
            pipeline.PROJECT_ROOT
            / "outputs"
            / "jewelry-white-background"
            / "mask-edit-sy1537-sy1552-20260806-v1"
            / "SY1552"
            / "manifests"
            / "upload_api_response.json"
        )

        token = _upload_file_token(
            json.loads(response_path.read_text(encoding="utf-8-sig")),
            record_id="recvreqSDWhKV8",
            field_id=pipeline.FIELD_MAIN,
            target_filename="SY1552_mask_edit_watermarked.png",
        )

        self.assertEqual(token, "HnMWb9XWxo4lMjxmfYncDCZjnVg")

    def test_nested_upload_token_never_uses_first_or_ambiguous_attachment(self) -> None:
        payload = {
            "ok": True,
            "data": {
                "attachments": {
                    "rec1": {
                        pipeline.FIELD_MAIN: [
                            {"file_token": "wrong-first", "name": "other.png"},
                            {"file_token": "target-a", "name": "target.png"},
                            {"file_token": "target-b", "name": "target.png"},
                        ]
                    }
                }
            },
        }

        with self.assertRaisesRegex(RuntimeError, "唯一"):
            _upload_file_token(
                payload,
                record_id="rec1",
                field_id=pipeline.FIELD_MAIN,
                target_filename="target.png",
            )

    def test_flat_upload_token_response_remains_supported(self) -> None:
        token = _upload_file_token(
            {"ok": True, "data": {"file_tokens": ["flat-token"]}},
            record_id="rec1",
            field_id=pipeline.FIELD_MAIN,
            target_filename="target.png",
        )

        self.assertEqual(token, "flat-token")

    def test_upload_attempt_is_persisted_before_upload_and_updated_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory) / "SY1537"
            _prepared, _mask, generated, _report = write_delivery_state(product_root)
            watermarked = product_root / "white-bg" / "SY1537_v2_watermarked.png"
            attempt_path = (
                product_root
                / "manifests"
                / "upload_attempt.SY1537_v2_watermarked.json"
            )

            def fake_watermark(_command, **_kwargs):
                watermarked.parent.mkdir(parents=True, exist_ok=True)
                watermarked.write_bytes(b"watermarked")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def fake_upload(record_id, image_path):
                attempt = pipeline.read_json(attempt_path)
                self.assertEqual(attempt["status"], "uploading")
                self.assertEqual(attempt["workflow_mode"], "background_only_edit")
                self.assertEqual(attempt["product_id"], "SY1537")
                self.assertEqual(attempt["record_id"], record_id)
                self.assertEqual(attempt["field_id"], pipeline.FIELD_MAIN)
                self.assertEqual(
                    attempt["generated_image_sha256"],
                    pipeline.file_sha256(generated),
                )
                self.assertEqual(attempt["target_filename"], image_path.name)
                self.assertEqual(attempt["local_file"], str(image_path.resolve()))
                self.assertEqual(
                    attempt["local_file_sha256"], pipeline.file_sha256(image_path)
                )
                self.assertEqual(attempt["authorization_reference"], "明确授权")
                return {
                    "ok": True,
                    "data": {
                        "attachments": {
                            record_id: {
                                pipeline.FIELD_MAIN: [
                                    {
                                        "file_token": "wrong-first",
                                        "name": "other.png",
                                    },
                                    {
                                        "file_token": "uploaded-token",
                                        "name": image_path.name,
                                    },
                                ]
                            }
                        }
                    },
                }

            with (
                patch.object(
                    pipeline,
                    "build_watermark_command",
                    return_value=(["watermark"], watermarked),
                ),
                patch.object(pipeline, "run_command", side_effect=fake_watermark),
                patch.object(pipeline, "_upload_attachment", side_effect=fake_upload),
            ):
                receipt = pipeline.watermark_and_upload(
                    product_root,
                    ProductRecord("SY1537", "rec1", (), (), (), ()),
                    "明确授权",
                    append_version="v2",
                )

            completed_attempt = pipeline.read_json(attempt_path)
            self.assertEqual(completed_attempt["status"], "uploaded")
            self.assertEqual(
                completed_attempt["uploaded_attachment"]["token"],
                "uploaded-token",
            )
            self.assertEqual(receipt["status"], "uploaded")

    def test_existing_unresolved_upload_attempt_blocks_automatic_reupload(self) -> None:
        for status in ("uploading", "failed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                product_root = Path(directory) / "SY1537"
                write_delivery_state(product_root)
                watermarked = product_root / "white-bg" / "SY1537_v2_watermarked.png"
                attempt_path = (
                    product_root
                    / "manifests"
                    / "upload_attempt.SY1537_v2_watermarked.json"
                )
                pipeline.write_json(attempt_path, {"status": status})

                with (
                    patch.object(
                        pipeline,
                        "build_watermark_command",
                        return_value=(["watermark"], watermarked),
                    ),
                    patch.object(
                        pipeline,
                        "run_command",
                        side_effect=AssertionError("未决上传不得重新加水印"),
                    ),
                    patch.object(
                        pipeline,
                        "_upload_attachment",
                        side_effect=AssertionError("未决上传不得自动重传"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "人工处置"),
                ):
                    pipeline.watermark_and_upload(
                        product_root,
                        ProductRecord("SY1537", "rec1", (), (), (), ()),
                        "明确授权",
                        append_version="v2",
                    )

    def test_upload_failure_is_persisted_without_automatic_retry(self) -> None:
        cases = {
            "exception": RuntimeError("network uncertain"),
            "non_ok": {"ok": False, "error": "denied"},
            "missing_token": {"ok": True, "data": {}},
        }
        for case, outcome in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                product_root = Path(directory) / "SY1537"
                write_delivery_state(product_root)
                watermarked = product_root / "white-bg" / "SY1537_v2_watermarked.png"
                attempt_path = (
                    product_root
                    / "manifests"
                    / "upload_attempt.SY1537_v2_watermarked.json"
                )

                def fake_watermark(_command, **_kwargs):
                    watermarked.parent.mkdir(parents=True, exist_ok=True)
                    watermarked.write_bytes(b"watermarked")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                upload_effect = (
                    {"return_value": outcome}
                    if isinstance(outcome, dict)
                    else {"side_effect": outcome}
                )
                with (
                    patch.object(
                        pipeline,
                        "build_watermark_command",
                        return_value=(["watermark"], watermarked),
                    ),
                    patch.object(pipeline, "run_command", side_effect=fake_watermark),
                    patch.object(
                        pipeline,
                        "_upload_attachment",
                        **upload_effect,
                    ) as upload_mock,
                    self.assertRaises(RuntimeError),
                ):
                    pipeline.watermark_and_upload(
                        product_root,
                        ProductRecord("SY1537", "rec1", (), (), (), ()),
                        "明确授权",
                        append_version="v2",
                    )

                failed_attempt = pipeline.read_json(attempt_path)
                self.assertEqual(failed_attempt["status"], "failed")
                self.assertIn("error", failed_attempt)
                if isinstance(outcome, dict):
                    self.assertEqual(failed_attempt["upload_response"], outcome)

                with (
                    patch.object(
                        pipeline,
                        "build_watermark_command",
                        return_value=(["watermark"], watermarked),
                    ),
                    patch.object(
                        pipeline,
                        "run_command",
                        side_effect=AssertionError("失败意图不得重新加水印"),
                    ),
                    patch.object(
                        pipeline,
                        "_upload_attachment",
                        side_effect=AssertionError("失败意图不得自动重传"),
                    ) as replay_upload,
                    self.assertRaisesRegex(RuntimeError, "人工处置"),
                ):
                    pipeline.watermark_and_upload(
                        product_root,
                        ProductRecord("SY1537", "rec1", (), (), (), ()),
                        "明确授权",
                        append_version="v2",
                    )
                upload_mock.assert_called_once()
                replay_upload.assert_not_called()

    def test_attachment_upload_forces_exactly_one_lark_attempt(self) -> None:
        image_path = pipeline.PROJECT_ROOT / "outputs" / "SY1537_watermarked.png"
        response = {"ok": True, "data": {"file_tokens": ["token"]}}

        with patch.object(pipeline, "run_lark", return_value=response) as run_lark_mock:
            self.assertIs(pipeline._upload_attachment("rec1", image_path), response)

        self.assertEqual(run_lark_mock.call_count, 1)
        self.assertEqual(run_lark_mock.call_args.kwargs, {"attempts": 1})

    def test_deliver_uses_saved_record_id_without_searching_or_reading_main_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            product_root = run_root / "SY1537"
            pipeline.write_json(
                product_root / "product_context.json",
                {
                    "product_id": "SY1537",
                    "record_id": "rec-local",
                    "existing_main_images": [
                        {"token": "stale-main", "name": "stale.png"}
                    ],
                },
            )
            receipt = {"product_id": "SY1537", "status": "uploaded"}

            with (
                patch.object(
                    pipeline,
                    "find_target_records",
                    side_effect=AssertionError("交付前不得重新搜索或读取主图"),
                ),
                patch.object(
                    pipeline,
                    "watermark_and_upload",
                    return_value=receipt,
                ) as deliver_mock,
            ):
                summary = pipeline.deliver_selected(
                    run_root,
                    ["SY1537"],
                    "用户已授权追加",
                )

            self.assertTrue(summary["ok"])
            target_record = deliver_mock.call_args.args[1]
            self.assertEqual(target_record.product_id, "SY1537")
            self.assertEqual(target_record.record_id, "rec-local")
            self.assertEqual(target_record.main_images, ())

    def test_upload_is_skipped_when_same_filename_already_exists(self) -> None:
        before = (attachment("old", "SY1537_watermarked.png"),)

        decision = plan_upload(before, "SY1537_watermarked.png")

        self.assertEqual(decision.action, "already_present")
        self.assertEqual(decision.existing.token, "old")

    def test_upload_is_planned_when_filename_is_absent(self) -> None:
        before = (attachment("old", "existing.png"),)

        decision = plan_upload(before, "SY1537_watermarked.png")

        self.assertEqual(decision.action, "upload")
        self.assertIsNone(decision.existing)

    def test_versioned_watermark_filename_appends_without_colliding_with_legacy_file(self) -> None:
        before = (attachment("old", "SY1537_watermarked.png"),)

        target_name = watermarked_filename("SY1537", "20260811_v2")
        decision = plan_upload(before, target_name)

        self.assertEqual(target_name, "SY1537_20260811_v2_watermarked.png")
        self.assertEqual(decision.action, "upload")

    def test_versioned_watermark_command_targets_the_versioned_output(self) -> None:
        command, output_path = build_watermark_command(
            generated_path=Path("generated/SY1537.png"),
            output_dir=Path("white-bg"),
            product_id="SY1537",
            append_version="20260811_v2",
        )

        self.assertEqual(
            output_path,
            Path("white-bg/SY1537_20260811_v2_watermarked.png"),
        )
        suffix_index = command.index("--suffix")
        self.assertEqual(command[suffix_index + 1], "_20260811_v2_watermarked")

    def test_versioned_watermark_filename_rejects_unsafe_append_version(self) -> None:
        with self.assertRaises(ValueError):
            watermarked_filename("SY1537", "../outside")

    def test_lark_download_output_is_relative_to_workspace(self) -> None:
        target = Path(__file__).resolve().parents[1] / "outputs" / "target.jpg"

        self.assertEqual(workspace_relative_output(target), "outputs/target.jpg")

    def test_lark_download_output_rejects_path_outside_workspace(self) -> None:
        with self.assertRaises(ValueError):
            workspace_relative_output(Path.home() / "outside.jpg")

    def test_size_only_failure_allows_review_checks_but_no_other_failed_check(self) -> None:
        qc = {
            "decision": "fail",
            "checks": [
                {"id": "composition_width", "status": "fail"},
                {"id": "edge_clearance", "status": "review"},
                {"id": "horizontal_center", "status": "pass"},
            ],
        }

        self.assertTrue(is_size_only_failure(qc))

    def test_size_only_failure_rejects_edge_or_detection_failures(self) -> None:
        edge_failure = {
            "decision": "fail",
            "checks": [
                {"id": "composition_width", "status": "fail"},
                {"id": "edge_clearance", "status": "fail"},
            ],
        }
        malformed_failure = {"decision": "fail", "checks": []}

        self.assertFalse(is_size_only_failure(edge_failure))
        self.assertFalse(is_size_only_failure(malformed_failure))

    def test_upload_decision_requires_explicit_size_override_for_failed_qc(self) -> None:
        size_failure = {
            "decision": "fail",
            "checks": [{"id": "composition_width", "status": "fail"}],
        }

        self.assertEqual(
            _derive_upload_decision(
                size_failure,
                size_failure,
                final_review_approved=True,
                authorized=True,
                allow_size_override=False,
            ),
            "blocked_by_qc",
        )

    def test_upload_decision_records_user_override_only_for_size_failures(self) -> None:
        pre_qc = {
            "decision": "fail",
            "checks": [
                {"id": "composition_width", "status": "fail"},
                {"id": "foreground_confidence", "status": "review"},
            ],
        }
        post_qc = {
            "decision": "fail",
            "checks": [{"id": "composition_width", "status": "fail"}],
        }

        self.assertEqual(
            _derive_upload_decision(
                pre_qc,
                post_qc,
                final_review_approved=True,
                authorized=True,
                allow_size_override=True,
            ),
            "user_override_for_append",
        )

    def test_upload_decision_never_overrides_non_size_failure(self) -> None:
        pre_qc = {
            "decision": "fail",
            "checks": [
                {"id": "composition_width", "status": "fail"},
                {"id": "edge_clearance", "status": "fail"},
            ],
        }
        post_qc = {"decision": "pass", "checks": []}

        self.assertEqual(
            _derive_upload_decision(
                pre_qc,
                post_qc,
                final_review_approved=True,
                authorized=True,
                allow_size_override=True,
            ),
            "blocked_by_qc",
        )

    def test_size_override_record_binds_attempt_hash_and_original_qc(self) -> None:
        state = {
            "attempt": 2,
            "generated_image_sha256": "ABC123",
        }
        pre_qc = {
            "decision": "fail",
            "checks": [{"id": "composition_width", "status": "fail"}],
        }
        post_qc = {
            "decision": "fail",
            "checks": [{"id": "composition_width", "status": "fail"}],
        }

        record = build_size_override_record(
            product_id="SY1537",
            state=state,
            pre_qc=pre_qc,
            post_qc=post_qc,
            authorization_reference="用户明确授权仅覆盖大小门禁",
        )

        self.assertEqual(record["schema_version"], "override-1.0")
        self.assertEqual(record["scope"], "SY1537")
        self.assertEqual(record["attempt"], 2)
        self.assertEqual(record["generated_image_sha256"], "ABC123")
        self.assertEqual(
            record["original_qc_decision"],
            {"pre_watermark": "fail", "post_watermark": "fail"},
        )
        self.assertEqual(record["failed_check_ids"], ["composition_width"])
        self.assertEqual(record["upload_decision"], "user_override_for_append")

    def test_deliver_cli_exposes_explicit_size_override_switch(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "deliver",
                "--authorization-reference",
                "本次明确授权",
                "--allow-size-override",
            ]
        )

        self.assertTrue(args.allow_size_override)
        deliver_parser = next(
            action.choices["deliver"]
            for action in parser._actions
            if getattr(action, "choices", None) and "deliver" in action.choices
        )
        self.assertIn("不执行尺寸 QC", deliver_parser.format_help())

    def test_generate_cli_describes_mask_edit_without_output_qc(self) -> None:
        parser = build_parser()
        generate_parser = next(
            action.choices["generate"]
            for action in parser._actions
            if getattr(action, "choices", None) and "generate" in action.choices
        )
        help_text = generate_parser.format_help()

        self.assertIn("Mask Edit", help_text)
        self.assertNotIn("QC", help_text)

    def test_deliver_cli_accepts_an_append_version(self) -> None:
        args = build_parser().parse_args(
            [
                "deliver",
                "--authorization-reference",
                "本次明确授权",
                "--append-version",
                "20260811_v2",
            ]
        )

        self.assertEqual(args.append_version, "20260811_v2")

    def test_automatic_audit_remains_blocked_until_size_override_is_applied(self) -> None:
        size_failure = {
            "decision": "fail",
            "checks": [{"id": "composition_width", "status": "fail"}],
        }

        self.assertEqual(
            _automatic_audit_decision(
                size_failure,
                size_failure,
                "requires_human_review",
            ),
            "blocked_by_qc",
        )


if __name__ == "__main__":
    unittest.main()
