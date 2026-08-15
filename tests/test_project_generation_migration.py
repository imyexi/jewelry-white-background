import unittest
import ast
import hashlib
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_pythonioencoding = os.environ.get("PYTHONIOENCODING")
import run_jewelry_base_pipeline as general_pipeline
from scripts.yuan_image_generation_adapter import build_background_edit_command
if _pythonioencoding is None:
    os.environ.pop("PYTHONIOENCODING", None)
else:
    os.environ["PYTHONIOENCODING"] = _pythonioencoding


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_SCRIPTS = (
    ROOT / "run_jewelry_base_pipeline.py",
    ROOT / "scripts" / "run_sy1537_sy1552_white_background.py",
)
SKILL_PATH = ROOT / "skills" / "jewelry-white-background" / "SKILL.md"


def write_geometry_profile_stub(product_root: Path, product_id: str) -> Path:
    profile_path = product_root / "geometry" / f"{product_id}.json"
    general_pipeline.write_json(
        profile_path,
        {
            "schema_version": "vision-geometry-mask-2.0",
            "product_id": product_id,
        },
    )
    return profile_path


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def write_reusable_generation_stub(
    product_root: Path,
    product_id: str,
    record_id: str,
    status: str,
    front_file_token: str = "front-1",
) -> dict:
    prepared = product_root / "prepared" / f"{product_id}_front.png"
    mask = product_root / "mask" / f"{product_id}_product-protection-mask.png"
    generated = product_root / "generated" / f"{product_id}_generated.png"
    report = product_root / "logs" / f"{product_id}_vision-mask-report.json"
    for path, payload in (
        (prepared, b"prepared-asset"),
        (mask, b"mask-asset"),
        (generated, b"generated-asset"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    identity = {
        "product_id": product_id,
        "source_sha256": "A" * 64,
        "geometry_sha256": "B" * 64,
        "prepared_sha256": sha256_bytes(b"prepared-asset"),
        "mask_sha256": sha256_bytes(b"mask-asset"),
    }
    general_pipeline.write_json(
        report,
        {
            "status": "ok",
            "automatic_wawapi_edit_allowed": True,
            **identity,
        },
    )
    state = {
        "workflow_mode": "background_only_edit",
        "status": status,
        "product_id": product_id,
        "record_id": record_id,
        "front_file_token": front_file_token,
        "generated_image": str(generated),
        "generated_image_sha256": sha256_bytes(b"generated-asset"),
        "mask_assessment": str(report),
        "mask_status": "ok",
        "automatic_wawapi_edit_allowed": True,
        **identity,
    }
    general_pipeline.write_json(
        product_root / "manifests" / "generation_state.json",
        state,
    )
    return {
        "prepared": prepared,
        "mask": mask,
        "generated": generated,
        "report": report,
        "state": state,
    }


class ProjectGenerationMigrationTests(unittest.TestCase):
    def test_upload_file_token_matches_real_nested_attachment_identity(self) -> None:
        payload = {
            "ok": True,
            "identity": "user",
            "data": {
                "attachments": {
                    "recvqFiJV24hM3": {
                        "fldEcAYBlX": [
                            {
                                "file_token": "wrong-file-token",
                                "name": "other_watermarked.png",
                                "size": 100,
                            },
                            {
                                "file_token": "Xw6objYNUoXPACxdxQ7csbLynYd",
                                "name": "SY1528_watermarked.png",
                                "size": 2947708,
                            },
                        ]
                    }
                }
            },
        }

        token = general_pipeline.upload_file_token(
            payload,
            "recvqFiJV24hM3",
            "fldEcAYBlX",
            "SY1528_watermarked.png",
        )

        self.assertEqual(token, "Xw6objYNUoXPACxdxQ7csbLynYd")

    def test_upload_file_token_rejects_ambiguous_or_wrong_nested_attachment(self) -> None:
        for case, attachments in (
            (
                "duplicate-name",
                [
                    {"file_token": "token-1", "name": "target.png"},
                    {"file_token": "token-2", "name": "target.png"},
                ],
            ),
            (
                "wrong-name",
                [{"file_token": "token-1", "name": "other.png"}],
            ),
        ):
            with self.subTest(case=case), self.assertRaisesRegex(
                RuntimeError, "唯一|匹配"
            ):
                general_pipeline.upload_file_token(
                    {
                        "ok": True,
                        "data": {
                            "attachments": {
                                "rec1": {general_pipeline.FIELD_WHITE: attachments}
                            }
                        },
                    },
                    "rec1",
                    general_pipeline.FIELD_WHITE,
                    "target.png",
                )

    def test_upload_file_token_keeps_unambiguous_flat_compatibility(self) -> None:
        self.assertEqual(
            general_pipeline.upload_file_token(
                {"ok": True, "data": {"file_token": "direct-token"}},
                "rec1",
                general_pipeline.FIELD_WHITE,
                "target.png",
            ),
            "direct-token",
        )
        self.assertEqual(
            general_pipeline.upload_file_token(
                {"ok": True, "data": {"file_tokens": ["only-token"]}},
                "rec1",
                general_pipeline.FIELD_WHITE,
                "target.png",
            ),
            "only-token",
        )
        with self.assertRaisesRegex(RuntimeError, "唯一|歧义"):
            general_pipeline.upload_file_token(
                {"ok": True, "data": {"file_tokens": ["one", "two"]}},
                "rec1",
                general_pipeline.FIELD_WHITE,
                "target.png",
            )

    def test_active_scripts_use_background_edit_and_mask_assets_only(self) -> None:
        for path in ACTIVE_SCRIPTS:
            self.assertTrue(path.is_file(), path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("yuan_image_generation_adapter", text, path)
            self.assertIn("edit_background_to_path", text, path)
            self.assertIn("create_background_edit_assets", text, path)
            self.assertIn("load_vision_geometry", text, path)
            self.assertNotIn("generate_to_path", text, path)
            self.assertNotIn("operation=generation", text.replace(" ", "").lower(), path)

    def test_standard_active_functions_do_not_reach_output_qc_or_upload_readback(self) -> None:
        path = ROOT / "scripts" / "run_sy1537_sy1552_white_background.py"
        text = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(text)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        def called_names(function):
            return {
                call.func.id
                for call in ast.walk(function)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }

        generation_calls = called_names(functions["generate_and_evaluate"])
        delivery_calls = called_names(functions["watermark_and_upload"])
        main_calls = called_names(functions["main"])
        for forbidden in ("generate_to_path", "_evaluate", "_create_contact_sheet"):
            self.assertNotIn(forbidden, generation_calls)
        for forbidden in (
            "_record_get",
            "verify_append",
            "plan_upload",
            "_evaluate",
            "_create_contact_sheet",
            "_review_is_approved",
        ):
            self.assertNotIn(forbidden, delivery_calls)
        self.assertNotIn("verify_selected", main_calls)
        delivery_source = ast.get_source_segment(text, functions["watermark_and_upload"]) or ""
        self.assertNotIn("time.sleep(", delivery_source)

    def test_general_pipeline_has_no_detail_or_existing_generated_bypass(self) -> None:
        text = (ROOT / "run_jewelry_base_pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn('item.get("细节图")', text)
        self.assertNotIn("all_front[1:", text)
        self.assertNotIn("reused_existing_final", text)
        self.assertNotIn("reused-existing-output", text)
        self.assertNotIn("【产品名称】", text)
        self.assertNotIn("【产品参数】", text)

    def test_general_pipeline_has_no_preprocessed_image_branch(self) -> None:
        path = ROOT / "run_jewelry_base_pipeline.py"
        text = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(text)
        function_names = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        called_names = {
            call.func.id
            for call in ast.walk(tree)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }

        self.assertNotIn("PREPROCESS", text)
        self.assertNotIn("preprocess_reference_images.py", text)
        self.assertNotIn("preprocess", function_names)
        self.assertNotIn("preprocess", called_names)

    def test_general_prompt_is_fixed_at_seven_product_agnostic_sections(self) -> None:
        prompt = general_pipeline.build_prompt(
            {
                "产品编号": "SENTINEL-ID",
                "描述": "SENTINEL-DESCRIPTION",
                "主水晶类型": "SENTINEL-MATERIAL",
            }
        )

        self.assertEqual(prompt.count("【"), 7)
        self.assertNotIn("SENTINEL", prompt)
        self.assertIn("#FBFCF9", prompt)

    def test_general_prompt_delegates_to_the_skill_authoritative_builder(self) -> None:
        with patch.object(
            general_pipeline,
            "_build_authoritative_prompt",
            return_value="AUTHORITATIVE-PROMPT",
            create=True,
        ) as builder:
            prompt = general_pipeline.build_prompt({"产品编号": "IGNORED"})

        self.assertEqual(prompt, "AUTHORITATIVE-PROMPT")
        builder.assert_called_once()

    def test_general_pipeline_blocks_non_ok_mask_before_edit_or_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "front.jpg"
            source.write_bytes(b"front")
            downloaded_tokens = []

            def fake_download(record_id, token, name, outdir):
                downloaded_tokens.append(token)
                return source

            assessment = SimpleNamespace(
                status="review",
                reasons=["geometry_confidence_low"],
                automatic_wawapi_edit_allowed=False,
            )
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [
                    {"file_token": "not-image", "name": "note.txt"},
                    {"file_token": "front-1", "name": "front.jpg"},
                    {"file_token": "front-2", "name": "front-2.jpg"},
                ],
                "白底图": [],
            }
            geometry_profile = write_geometry_profile_stub(root / "SY1537", "SY1537")
            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(general_pipeline, "download_attachment", side_effect=fake_download),
                patch.object(
                    general_pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                    create=True,
                ) as geometry_loader,
                patch.object(
                    general_pipeline,
                    "create_background_edit_assets",
                    return_value=assessment,
                ),
                patch.object(
                    general_pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("edit must not run"),
                ),
                patch.object(
                    general_pipeline,
                    "watermark",
                    side_effect=AssertionError("watermark must not run"),
                ),
                patch.object(
                    general_pipeline,
                    "upload",
                    side_effect=AssertionError("upload must not run"),
                ),
            ):
                result = general_pipeline.process_one(item)

            self.assertEqual(downloaded_tokens, ["front-1"])
            self.assertEqual(result["status"], "blocked_by_mask_gate")
            geometry_loader.assert_called_once_with(geometry_profile, "SY1537")

    def test_general_pipeline_blocks_fail_mask_before_edit_or_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "front.jpg"
            source.write_bytes(b"front")
            assessment = SimpleNamespace(
                status="fail",
                reasons=["protected_region_touches_border"],
                automatic_wawapi_edit_allowed=False,
            )
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }
            write_geometry_profile_stub(root / "SY1537", "SY1537")
            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(general_pipeline, "download_attachment", return_value=source),
                patch.object(
                    general_pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                    create=True,
                ),
                patch.object(
                    general_pipeline,
                    "create_background_edit_assets",
                    return_value=assessment,
                ),
                patch.object(
                    general_pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("edit must not run"),
                ),
                patch.object(
                    general_pipeline,
                    "upload",
                    side_effect=AssertionError("upload must not run"),
                ),
            ):
                result = general_pipeline.process_one(item)

            self.assertEqual(result["status"], "blocked_by_mask_gate")
            self.assertEqual(result["mask_status"], "fail")

    def test_general_pipeline_generates_awaiting_state_without_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "front.jpg"
            source.write_bytes(b"front")
            assessment = SimpleNamespace(
                status="ok",
                reasons=[],
                automatic_wawapi_edit_allowed=True,
            )
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }
            geometry_profile = write_geometry_profile_stub(root / "SY1537", "SY1537")
            wawapi_call = {}

            def fake_create_assets(
                input_path,
                image_path,
                mask_path,
                overlay_path,
                report_path,
                profile_path,
                product_id,
            ):
                self.assertEqual(Path(input_path), source)
                self.assertEqual(
                    Path(image_path), root / "SY1537" / "prepared" / "SY1537_front.png"
                )
                self.assertEqual(
                    Path(mask_path),
                    root
                    / "SY1537"
                    / "mask"
                    / "SY1537_product-protection-mask.png",
                )
                self.assertEqual(Path(profile_path), geometry_profile)
                self.assertEqual(product_id, "SY1537")
                assets = {
                    Path(image_path): b"prepared-asset",
                    Path(mask_path): b"mask-asset",
                    Path(overlay_path): b"overlay-asset",
                }
                for path, payload in assets.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
                general_pipeline.write_json(
                    Path(report_path),
                    {
                        **vars(assessment),
                        "product_id": "SY1537",
                        "source_sha256": sha256_bytes(b"front"),
                        "geometry_sha256": "B" * 64,
                        "prepared_sha256": sha256_bytes(b"prepared-asset"),
                        "mask_sha256": sha256_bytes(b"mask-asset"),
                    },
                )
                return assessment

            def fake_edit(prompt, image, mask, output_path, log_path):
                command = build_background_edit_command(
                    prompt,
                    Path(image),
                    Path(mask),
                    Path(output_path).parent,
                )
                wawapi_call.update(
                    prompt=prompt,
                    image=Path(image),
                    mask=Path(mask),
                    command=command,
                )
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"edited")
                return SimpleNamespace(
                    provider="wawapi",
                    task_id="task-1",
                    helper_output=Path(output_path),
                )

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(general_pipeline, "download_attachment", return_value=source),
                patch.object(
                    general_pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                    create=True,
                ) as geometry_loader,
                patch.object(
                    general_pipeline,
                    "create_background_edit_assets",
                    side_effect=fake_create_assets,
                ),
                patch.object(
                    general_pipeline,
                    "edit_background_to_path",
                    side_effect=fake_edit,
                ),
                patch.object(
                    general_pipeline,
                    "watermark",
                    side_effect=AssertionError("首次生成不得加水印"),
                ),
                patch.object(
                    general_pipeline,
                    "upload",
                    side_effect=AssertionError("首次生成不得上传"),
                ),
            ):
                result = general_pipeline.process_one(item)

            state = general_pipeline.json.loads(
                (root / "SY1537" / "manifests" / "generation_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["workflow_mode"], "background_only_edit")
            self.assertEqual(result["status"], "awaiting_authorization")
            self.assertEqual(state["workflow_mode"], "background_only_edit")
            self.assertEqual(state["status"], "awaiting_authorization")
            self.assertIs(state["automatic_wawapi_edit_allowed"], True)
            self.assertEqual(state["product_id"], "SY1537")
            self.assertEqual(state["front_file_token"], "front-1")
            self.assertEqual(state["source_sha256"], sha256_bytes(b"front"))
            self.assertEqual(state["geometry_sha256"], "B" * 64)
            self.assertEqual(
                state["prepared_sha256"], sha256_bytes(b"prepared-asset")
            )
            self.assertEqual(state["mask_sha256"], sha256_bytes(b"mask-asset"))
            self.assertFalse(
                (root / "SY1537" / "manifests" / "upload_receipt.json").exists()
            )
            geometry_loader.assert_called_once_with(geometry_profile, "SY1537")
            self.assertEqual(
                wawapi_call["image"],
                root / "SY1537" / "prepared" / "SY1537_front.png",
            )
            self.assertEqual(
                wawapi_call["mask"],
                root
                / "SY1537"
                / "mask"
                / "SY1537_product-protection-mask.png",
            )
            command = wawapi_call["command"]
            self.assertEqual(command.count("--image"), 1)
            self.assertEqual(command.count("--mask"), 1)
            self.assertEqual(command[command.index("--operation") + 1], "edit")
            for forbidden in (
                "--geometry-profile",
                "--levels",
                "--detection-image",
                "--aspect-ratio",
                "--resolution",
            ):
                self.assertNotIn(forbidden, command)
            request_text = "\n".join(command).lower()
            for forbidden in ("geometry", "overlay", "detection", "levels", "色阶"):
                self.assertNotIn(forbidden, request_text)

    def test_general_pipeline_waits_for_current_authorization_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "front.jpg"
            source.write_bytes(b"front")
            write_geometry_profile_stub(root / "SY1537", "SY1537")
            assessment = SimpleNamespace(
                status="ok",
                reasons=[],
                automatic_wawapi_edit_allowed=True,
            )
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }

            def fake_edit(prompt, image, mask, output_path, log_path):
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"edited")
                return SimpleNamespace(
                    provider="wawapi",
                    task_id="task-1",
                    helper_output=Path(output_path),
                )

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline, "download_attachment", return_value=source
                ),
                patch.object(
                    general_pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                ),
                patch.object(
                    general_pipeline,
                    "create_background_edit_assets",
                    return_value=assessment,
                ),
                patch.object(
                    general_pipeline,
                    "verified_mask_report_identity",
                    return_value={
                        "product_id": "SY1537",
                        "source_sha256": "A" * 64,
                        "geometry_sha256": "B" * 64,
                        "prepared_sha256": "C" * 64,
                        "mask_sha256": "D" * 64,
                    },
                ),
                patch.object(
                    general_pipeline,
                    "edit_background_to_path",
                    side_effect=fake_edit,
                ),
                patch.object(
                    general_pipeline,
                    "watermark",
                    side_effect=AssertionError("未授权时不得加水印"),
                ),
                patch.object(
                    general_pipeline,
                    "upload",
                    side_effect=AssertionError("未授权时不得上传"),
                ),
            ):
                result = general_pipeline.process_one(item)

            state_path = (
                root / "SY1537" / "manifests" / "generation_state.json"
            )
            receipt_path = root / "SY1537" / "manifests" / "upload_receipt.json"
            state = general_pipeline.json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(result["status"], "awaiting_authorization")
            self.assertEqual(state["status"], "awaiting_authorization")
            self.assertFalse(receipt_path.exists())

    def test_general_pipeline_invalidates_stale_completed_state_on_mask_identity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product_root = root / "SY1537"
            source = root / "front.jpg"
            source.write_bytes(b"front")
            write_geometry_profile_stub(product_root, "SY1537")
            state_path = product_root / "manifests" / "generation_state.json"
            general_pipeline.write_json(
                state_path,
                {
                    "workflow_mode": "background_only_edit",
                    "status": "completed",
                    "product_id": "SY1537",
                    "generated_image_sha256": "F" * 64,
                },
            )
            states_seen_during_asset_creation = []
            assessment = SimpleNamespace(
                status="ok",
                reasons=[],
                automatic_wawapi_edit_allowed=True,
            )
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }

            def fake_create_assets(
                input_path,
                image_path,
                mask_path,
                overlay_path,
                report_path,
                profile_path,
                product_id,
            ):
                current_state = general_pipeline.json.loads(
                    state_path.read_text(encoding="utf-8")
                )
                states_seen_during_asset_creation.append(current_state["status"])
                for path, payload in (
                    (Path(image_path), b"prepared-asset"),
                    (Path(mask_path), b"mask-asset"),
                    (Path(overlay_path), b"overlay-asset"),
                ):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
                general_pipeline.write_json(
                    Path(report_path),
                    {
                        "status": "ok",
                        "automatic_wawapi_edit_allowed": True,
                        "product_id": "SY9999",
                        "source_sha256": sha256_bytes(b"front"),
                        "geometry_sha256": "B" * 64,
                        "prepared_sha256": sha256_bytes(b"prepared-asset"),
                        "mask_sha256": sha256_bytes(b"mask-asset"),
                    },
                )
                return assessment

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline, "download_attachment", return_value=source
                ),
                patch.object(
                    general_pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                ),
                patch.object(
                    general_pipeline,
                    "create_background_edit_assets",
                    side_effect=fake_create_assets,
                ),
                patch.object(
                    general_pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("身份失败时不得调用 Wawapi"),
                ),
                self.assertRaisesRegex(RuntimeError, "Mask 报告|身份摘要"),
            ):
                general_pipeline.process_one(item)

            state = general_pipeline.json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(states_seen_during_asset_creation, ["in_progress"])
            self.assertIn(state["status"], {"blocked_by_mask_gate", "failed"})

    def test_general_pipeline_delivers_reusable_local_generation_without_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product_root = root / "SY1537"
            local = write_reusable_generation_stub(
                product_root,
                "SY1537",
                "rec1",
                "awaiting_authorization",
            )
            watermarked = (
                product_root / "white-bg" / "SY1537_generated_watermarked.png"
            )
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }

            def fake_watermark(image_path, pid, product_dir):
                self.assertEqual(Path(image_path), local["generated"])
                watermarked.parent.mkdir(parents=True, exist_ok=True)
                watermarked.write_bytes(b"watermarked")
                return watermarked

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "download_attachment",
                    side_effect=AssertionError("补授权不得重新下载"),
                ),
                patch.object(
                    general_pipeline,
                    "create_background_edit_assets",
                    side_effect=AssertionError("补授权不得重建 Mask"),
                ),
                patch.object(
                    general_pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("补授权不得再次调用 Wawapi"),
                ),
                patch.object(
                    general_pipeline,
                    "watermark",
                    side_effect=fake_watermark,
                ),
                    patch.object(
                        general_pipeline,
                        "upload",
                        return_value={
                            "ok": True,
                            "data": {
                                "attachments": {
                                    "rec1": {
                                        general_pipeline.FIELD_WHITE: [
                                            {
                                                "file_token": "uploaded-token",
                                                "name": watermarked.name,
                                            }
                                        ]
                                    }
                                }
                            },
                        },
                    ),
            ):
                result = general_pipeline.process_one(
                    item,
                    authorization_reference="  本次补授权  ",
                )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["authorization_reference"], "本次补授权")
            self.assertEqual(result["generated_path"], str(local["generated"]))
            receipt = general_pipeline.json.loads(
                (
                    product_root / "manifests" / "upload_receipt.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["authorization_reference"], "本次补授权")

    def test_general_pipeline_records_watermark_hash_computed_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product_root = root / "SY1537"
            write_reusable_generation_stub(
                product_root,
                "SY1537",
                "rec1",
                "awaiting_authorization",
            )
            watermarked = (
                product_root / "white-bg" / "SY1537_generated_watermarked.png"
            )
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }

            def fake_watermark(image_path, pid, product_dir):
                watermarked.parent.mkdir(parents=True, exist_ok=True)
                watermarked.write_bytes(b"before-upload")
                return watermarked

            def fake_upload(record_id, final_path):
                Path(final_path).write_bytes(b"changed-after-upload-started")
                return {
                    "ok": True,
                    "data": {"file_token": "uploaded-token"},
                }

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "watermark",
                    side_effect=fake_watermark,
                ),
                patch.object(general_pipeline, "upload", side_effect=fake_upload),
            ):
                general_pipeline.process_one(
                    item,
                    authorization_reference="本次补授权",
                )

            receipt = general_pipeline.json.loads(
                (
                    product_root / "manifests" / "upload_receipt.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                receipt["local_file_sha256"],
                sha256_bytes(b"before-upload"),
            )

    def test_general_pipeline_reuse_binds_current_first_front_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product_root = root / "SY1537"
            write_reusable_generation_stub(
                product_root,
                "SY1537",
                "rec1",
                "awaiting_authorization",
                front_file_token="old-front-token",
            )
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [
                    {"file_token": "new-front-token", "name": "front.jpg"}
                ],
                "白底图": [],
            }
            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "download_attachment",
                    side_effect=AssertionError("正面图变化不得下载"),
                ),
                patch.object(
                    general_pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("正面图变化不得生成"),
                ),
                patch.object(
                    general_pipeline,
                    "watermark",
                    side_effect=AssertionError("正面图变化不得加水印"),
                ),
                patch.object(
                    general_pipeline,
                    "upload",
                    side_effect=AssertionError("正面图变化不得上传"),
                ),
                self.assertRaisesRegex(RuntimeError, "正面图|file_token"),
            ):
                general_pipeline.process_one(
                    item,
                    authorization_reference="本次补授权",
                )

    def test_general_pipeline_success_receipt_prevents_duplicate_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product_root = root / "SY1537"
            local = write_reusable_generation_stub(
                product_root,
                "SY1537",
                "rec1",
                "completed",
            )
            watermarked = product_root / "white-bg" / "SY1537_generated_watermarked.png"
            watermarked.parent.mkdir(parents=True, exist_ok=True)
            watermarked.write_bytes(b"watermarked")
            receipt_path = product_root / "manifests" / "upload_receipt.json"
            general_pipeline.write_json(
                receipt_path,
                {
                    "workflow_mode": "background_only_edit",
                    "status": "uploaded",
                    "product_id": "SY1537",
                    "record_id": "rec1",
                    "field_id": general_pipeline.FIELD_WHITE,
                    "local_file": str(watermarked),
                    "local_file_sha256": sha256_bytes(b"watermarked"),
                    "generated_image": str(local["generated"]),
                    "generated_image_sha256": sha256_bytes(b"generated-asset"),
                    "uploaded_file_token": "uploaded-token",
                },
            )
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }
            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "watermark",
                    side_effect=AssertionError("成功回执不得重复水印"),
                ),
                patch.object(
                    general_pipeline,
                    "upload",
                    side_effect=AssertionError("成功回执不得重复上传"),
                ),
            ):
                result = general_pipeline.process_one(
                    item,
                    authorization_reference="重复授权",
                )

            self.assertEqual(result["status"], "completed")
            self.assertIs(result["reused_upload_receipt"], True)
            self.assertEqual(result["uploaded_file_token"], "uploaded-token")

    def test_general_pipeline_marks_upload_attempt_and_failure_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product_root = root / "SY1537"
            write_reusable_generation_stub(
                product_root,
                "SY1537",
                "rec1",
                "awaiting_authorization",
            )
            watermarked = product_root / "white-bg" / "SY1537_generated_watermarked.png"
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }

            def fake_watermark(image_path, pid, product_dir):
                watermarked.parent.mkdir(parents=True, exist_ok=True)
                watermarked.write_bytes(b"watermarked")
                return watermarked

            def fake_upload(record_id, final_path):
                state = general_pipeline.json.loads(
                    (
                        product_root / "manifests" / "generation_state.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(state["status"], "uploading")
                self.assertIsInstance(state["delivery_attempt"], int)
                self.assertGreater(state["delivery_attempt"], 0)
                raise RuntimeError("模拟上传连接中断")

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "watermark",
                    side_effect=fake_watermark,
                ),
                patch.object(general_pipeline, "upload", side_effect=fake_upload),
                self.assertRaisesRegex(RuntimeError, "连接中断"),
            ):
                general_pipeline.process_one(
                    item,
                    authorization_reference="本次补授权",
                )

            state = general_pipeline.json.loads(
                (
                    product_root / "manifests" / "generation_state.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "delivery_failed")
            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "upload",
                    side_effect=AssertionError("失败尝试不得自动重试"),
                ),
                self.assertRaisesRegex(RuntimeError, "awaiting_authorization"),
            ):
                general_pipeline.process_one(
                    item,
                    authorization_reference="再次授权",
                )

    def test_general_pipeline_authorization_requires_existing_awaiting_state(self) -> None:
        for state_status in (None, "completed", "in_progress", "failed", "blocked_by_mask_gate"):
            with self.subTest(state_status=state_status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                product_root = root / "SY1537"
                if state_status is not None:
                    write_reusable_generation_stub(
                        product_root,
                        "SY1537",
                        "rec1",
                        state_status,
                    )
                item = {
                    "产品编号": "SY1537",
                    "record_id": "rec1",
                    "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                    "白底图": [],
                }
                with (
                    patch.object(general_pipeline, "ROOT", root),
                    patch.object(
                        general_pipeline,
                        "download_attachment",
                        side_effect=AssertionError("授权模式不得下载"),
                    ),
                    patch.object(
                        general_pipeline,
                        "edit_background_to_path",
                        side_effect=AssertionError("授权模式不得生成"),
                    ),
                    patch.object(
                        general_pipeline,
                        "watermark",
                        side_effect=AssertionError("无等待态不得加水印"),
                    ),
                    patch.object(
                        general_pipeline,
                        "upload",
                        side_effect=AssertionError("无等待态不得上传"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "awaiting_authorization|等待授权"),
                ):
                    general_pipeline.process_one(
                        item,
                        authorization_reference="本次明确授权",
                    )

    def test_general_pipeline_blocks_tampered_reusable_generation_without_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product_root = root / "SY1537"
            local = write_reusable_generation_stub(
                product_root,
                "SY1537",
                "rec1",
                "awaiting_authorization",
            )
            local["generated"].write_bytes(b"tampered-generated-asset")
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("复用校验失败不得回退到 Wawapi"),
                ),
                patch.object(
                    general_pipeline,
                    "watermark",
                    side_effect=AssertionError("复用校验失败不得加水印"),
                ),
                patch.object(
                    general_pipeline,
                    "upload",
                    side_effect=AssertionError("复用校验失败不得上传"),
                ),
                self.assertRaisesRegex(RuntimeError, "生成图.*发生变化|复用"),
            ):
                general_pipeline.process_one(
                    item,
                    authorization_reference="本次补授权",
                )

    def test_general_main_parses_and_forwards_optional_authorization(self) -> None:
        for argv, expected in (
            ([], None),
            (["--authorization-reference", "  本次明确授权  "], "本次明确授权"),
        ):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                forwarded = []

                def fake_process(item, authorization_reference=None):
                    forwarded.append(authorization_reference)
                    return {
                        "product_id": item["产品编号"],
                        "record_id": item["record_id"],
                        "workflow_mode": "background_only_edit",
                        "status": "awaiting_authorization",
                    }

                with (
                    patch.object(general_pipeline, "ROOT", root),
                    patch.object(
                        general_pipeline,
                        "load_records",
                        return_value=[
                            {"产品编号": "SY1537", "record_id": "rec1"}
                        ],
                    ),
                    patch.object(
                        general_pipeline,
                        "process_one",
                        side_effect=fake_process,
                    ),
                ):
                    general_pipeline.main(argv)

                self.assertEqual(forwarded, [expected])

    def test_general_main_rejects_explicit_blank_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "load_records",
                    side_effect=AssertionError("空白授权应在读取数据前拒绝"),
                ),
                self.assertRaises(SystemExit),
            ):
                general_pipeline.main(
                    ["--authorization-reference", "   "]
                )

    def test_general_main_with_authorization_revisits_completed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run_manifest.jsonl").write_text(
                '{"product_id":"SY1537","workflow_mode":"background_only_edit",'
                '"mask_status":"ok","automatic_wawapi_edit_allowed":true,'
                '"status":"completed"}\n',
                encoding="utf-8",
            )
            forwarded = []

            def fake_process(item, authorization_reference=None):
                forwarded.append((item["产品编号"], authorization_reference))
                return {
                    "product_id": item["产品编号"],
                    "record_id": item["record_id"],
                    "workflow_mode": "background_only_edit",
                    "status": "completed",
                }

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "load_records",
                    return_value=[{"产品编号": "SY1537", "record_id": "rec1"}],
                ),
                patch.object(
                    general_pipeline,
                    "process_one",
                    side_effect=fake_process,
                ),
            ):
                general_pipeline.main(
                    ["--authorization-reference", "本次补授权"]
                )

            self.assertEqual(forwarded, [("SY1537", "本次补授权")])

    def test_general_main_without_authorization_skips_awaiting_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run_manifest.jsonl").write_text(
                '{"product_id":"SY1537","workflow_mode":"background_only_edit",'
                '"mask_status":"ok","automatic_wawapi_edit_allowed":true,'
                '"status":"awaiting_authorization"}\n',
                encoding="utf-8",
            )
            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "load_records",
                    return_value=[{"产品编号": "SY1537", "record_id": "rec1"}],
                ),
                patch.object(
                    general_pipeline,
                    "process_one",
                    side_effect=AssertionError("等待授权记录不得重复生成"),
                ),
            ):
                general_pipeline.main([])

            summary = general_pipeline.json.loads(
                (root / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["todo"], 0)

    def test_general_watermark_returns_only_deterministic_tool_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            product_root = Path(directory)
            generated = product_root / "generated" / "SY1537_generated.png"
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_bytes(b"generated")
            outdir = product_root / "white-bg"
            outdir.mkdir(parents=True, exist_ok=True)
            historical = outdir / "newer_historical.png"
            historical.write_bytes(b"historical")
            expected = outdir / "SY1537_generated_watermarked.png"

            def fake_run(command, check=True, cwd=None):
                self.assertIn(str(generated), command)
                expected.write_bytes(b"expected-watermark")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(general_pipeline, "run", side_effect=fake_run),
                patch.object(
                    Path,
                    "glob",
                    side_effect=AssertionError("不得扫描水印目录"),
                ),
            ):
                result = general_pipeline.watermark(
                    generated,
                    "SY1537",
                    product_root,
                )

            self.assertEqual(result, expected)
            self.assertEqual(result.read_bytes(), b"expected-watermark")

    def test_general_pipeline_requires_complete_allowed_mask_report_before_edit(self) -> None:
        report_mutations = {
            "status": {"status": "review"},
            "automatic_wawapi_edit_allowed": {
                "automatic_wawapi_edit_allowed": False
            },
            "product_id": {"product_id": "SY9999"},
            "source_sha256": {"source_sha256": None},
            "geometry_sha256": {"geometry_sha256": None},
            "prepared_sha256": {"prepared_sha256": None},
            "mask_sha256": {"mask_sha256": None},
        }
        for case, mutation in report_mutations.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "front.jpg"
                source.write_bytes(b"front")
                write_geometry_profile_stub(root / "SY1537", "SY1537")
                assessment = SimpleNamespace(
                    status="ok",
                    reasons=[],
                    automatic_wawapi_edit_allowed=True,
                )
                item = {
                    "产品编号": "SY1537",
                    "record_id": "rec1",
                    "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                    "白底图": [],
                }

                def fake_create_assets(
                    input_path,
                    image_path,
                    mask_path,
                    overlay_path,
                    report_path,
                    profile_path,
                    product_id,
                ):
                    Path(image_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(mask_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(overlay_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(image_path).write_bytes(b"prepared-asset")
                    Path(mask_path).write_bytes(b"mask-asset")
                    Path(overlay_path).write_bytes(b"overlay-asset")
                    report = {
                        "status": "ok",
                        "automatic_wawapi_edit_allowed": True,
                        "product_id": product_id,
                        "source_sha256": sha256_bytes(b"front"),
                        "geometry_sha256": "B" * 64,
                        "prepared_sha256": sha256_bytes(b"prepared-asset"),
                        "mask_sha256": sha256_bytes(b"mask-asset"),
                    }
                    report.update(mutation)
                    general_pipeline.write_json(Path(report_path), report)
                    return assessment

                with (
                    patch.object(general_pipeline, "ROOT", root),
                    patch.object(
                        general_pipeline, "download_attachment", return_value=source
                    ),
                    patch.object(
                        general_pipeline,
                        "load_vision_geometry",
                        return_value=SimpleNamespace(
                            schema_version="vision-geometry-mask-2.0"
                        ),
                    ),
                    patch.object(
                        general_pipeline,
                        "create_background_edit_assets",
                        side_effect=fake_create_assets,
                    ),
                    patch.object(
                        general_pipeline,
                        "edit_background_to_path",
                        side_effect=AssertionError("报告门禁失败时不得调用 Wawapi"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "Mask 报告|身份摘要"),
                ):
                    general_pipeline.process_one(item)

    def test_general_pipeline_rehashes_prepared_and_mask_before_edit(self) -> None:
        for changed_asset in ("prepared", "mask"):
            with (
                self.subTest(changed_asset=changed_asset),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = root / "front.jpg"
                source.write_bytes(b"front")
                write_geometry_profile_stub(root / "SY1537", "SY1537")
                assessment = SimpleNamespace(
                    status="ok",
                    reasons=[],
                    automatic_wawapi_edit_allowed=True,
                )
                item = {
                    "产品编号": "SY1537",
                    "record_id": "rec1",
                    "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                    "白底图": [],
                }

                def fake_create_assets(
                    input_path,
                    image_path,
                    mask_path,
                    overlay_path,
                    report_path,
                    profile_path,
                    product_id,
                ):
                    prepared_path = Path(image_path)
                    final_mask_path = Path(mask_path)
                    overlay = Path(overlay_path)
                    for path in (prepared_path, final_mask_path, overlay):
                        path.parent.mkdir(parents=True, exist_ok=True)
                    prepared_path.write_bytes(b"prepared-asset")
                    final_mask_path.write_bytes(b"mask-asset")
                    overlay.write_bytes(b"overlay-asset")
                    general_pipeline.write_json(
                        Path(report_path),
                        {
                            "status": "ok",
                            "automatic_wawapi_edit_allowed": True,
                            "product_id": product_id,
                            "source_sha256": sha256_bytes(b"front"),
                            "geometry_sha256": "B" * 64,
                            "prepared_sha256": sha256_bytes(b"prepared-asset"),
                            "mask_sha256": sha256_bytes(b"mask-asset"),
                        },
                    )
                    changed_path = (
                        prepared_path if changed_asset == "prepared" else final_mask_path
                    )
                    changed_path.write_bytes(b"changed-after-report")
                    return assessment

                with (
                    patch.object(general_pipeline, "ROOT", root),
                    patch.object(
                        general_pipeline, "download_attachment", return_value=source
                    ),
                    patch.object(
                        general_pipeline,
                        "load_vision_geometry",
                        return_value=SimpleNamespace(
                            schema_version="vision-geometry-mask-2.0"
                        ),
                    ),
                    patch.object(
                        general_pipeline,
                        "create_background_edit_assets",
                        side_effect=fake_create_assets,
                    ),
                    patch.object(
                        general_pipeline,
                        "edit_background_to_path",
                        side_effect=AssertionError("资产变化时不得调用 Wawapi"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "发生变化|身份"),
                ):
                    general_pipeline.process_one(item)

    def test_general_pipeline_requires_geometry_profile_before_download_or_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "download_attachment",
                    side_effect=AssertionError("缺少 profile 时不得下载"),
                ),
                patch.object(
                    general_pipeline,
                    "create_background_edit_assets",
                    side_effect=AssertionError("缺少 profile 时不得创建资产"),
                ),
                patch.object(
                    general_pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("缺少 profile 时不得调用 Wawapi"),
                ),
                self.assertRaisesRegex(FileNotFoundError, "视觉几何.*SY1537"),
            ):
                general_pipeline.process_one(item)

    def test_general_pipeline_blocks_ok_status_without_automatic_wawapi_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "front.jpg"
            source.write_bytes(b"front")
            geometry_profile = write_geometry_profile_stub(root / "SY1537", "SY1537")
            assessment = SimpleNamespace(
                status="ok",
                reasons=["automatic_wawapi_edit_not_allowed"],
                automatic_wawapi_edit_allowed=False,
            )
            item = {
                "产品编号": "SY1537",
                "record_id": "rec1",
                "正面图": [{"file_token": "front-1", "name": "front.jpg"}],
                "白底图": [],
            }

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline, "download_attachment", return_value=source
                ),
                patch.object(
                    general_pipeline,
                    "load_vision_geometry",
                    return_value=SimpleNamespace(
                        schema_version="vision-geometry-mask-2.0"
                    ),
                    create=True,
                ) as geometry_loader,
                patch.object(
                    general_pipeline,
                    "create_background_edit_assets",
                    return_value=assessment,
                ),
                patch.object(
                    general_pipeline,
                    "edit_background_to_path",
                    side_effect=AssertionError("布尔放行位不是 True 时不得调用 Wawapi"),
                ),
                patch.object(
                    general_pipeline,
                    "watermark",
                    side_effect=AssertionError("门禁未通过时不得加水印"),
                ),
                patch.object(
                    general_pipeline,
                    "upload",
                    side_effect=AssertionError("门禁未通过时不得上传"),
                ),
            ):
                result = general_pipeline.process_one(item)

            geometry_loader.assert_called_once_with(geometry_profile, "SY1537")
            self.assertEqual(result["status"], "blocked_by_mask_gate")
            self.assertEqual(result["mask_status"], "ok")
            self.assertIs(result["automatic_wawapi_edit_allowed"], False)

    def test_general_manifest_only_skips_completed_background_edit_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "run_manifest.jsonl"
            records = [
                {"产品编号": "LEGACY-COMPLETED", "record_id": "rec1"},
                {"产品编号": "LEGACY-SKIPPED", "record_id": "rec2"},
                {"产品编号": "EDIT-COMPLETED", "record_id": "rec3"},
            ]
            manifest.write_text(
                "\n".join(
                    (
                        '{"product_id":"LEGACY-COMPLETED","status":"completed"}',
                        '{"product_id":"LEGACY-SKIPPED","status":"skipped"}',
                        '{"product_id":"EDIT-COMPLETED","workflow_mode":"background_only_edit","mask_status":"ok","automatic_wawapi_edit_allowed":true,"status":"completed"}',
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            processed = []

            def fake_process(item, authorization_reference=None):
                self.assertIsNone(authorization_reference)
                processed.append(item["产品编号"])
                return {
                    "product_id": item["产品编号"],
                    "workflow_mode": "background_only_edit",
                    "mask_status": "fail",
                    "status": "blocked_by_mask_gate",
                }

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(general_pipeline, "load_records", return_value=records),
                patch.object(general_pipeline, "process_one", side_effect=fake_process),
            ):
                general_pipeline.main([])

            self.assertCountEqual(processed, ["LEGACY-COMPLETED", "LEGACY-SKIPPED"])

    def test_general_manifest_uses_latest_status_for_each_product(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run_manifest.jsonl").write_text(
                "\n".join(
                    (
                        '{"product_id":"SY1537","workflow_mode":"background_only_edit","mask_status":"ok","status":"completed"}',
                        '{"product_id":"SY1537","workflow_mode":"background_only_edit","mask_status":"fail","status":"blocked_by_mask_gate"}',
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            processed = []

            def fake_process(item, authorization_reference=None):
                self.assertIsNone(authorization_reference)
                processed.append(item["产品编号"])
                return {
                    "product_id": item["产品编号"],
                    "workflow_mode": "background_only_edit",
                    "mask_status": "fail",
                    "status": "blocked_by_mask_gate",
                }

            with (
                patch.object(general_pipeline, "ROOT", root),
                patch.object(
                    general_pipeline,
                    "load_records",
                    return_value=[{"产品编号": "SY1537", "record_id": "rec1"}],
                ),
                patch.object(general_pipeline, "process_one", side_effect=fake_process),
            ):
                general_pipeline.main([])

            self.assertEqual(processed, ["SY1537"])

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
