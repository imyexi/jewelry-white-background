from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

TEST_DIR = Path(__file__).resolve().parent
SKILL_DIR = TEST_DIR.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from evaluate_white_background import DEFAULT_POLICY, build_audit, evaluate_image  # noqa: E402


BACKGROUND = (251, 252, 249)


def make_image(
    path: Path,
    product_box: tuple[int, int, int, int],
    product_color: tuple[int, int, int],
    *,
    watermark: bool = False,
    background_color: tuple[int, int, int] = BACKGROUND,
    pale_boxes: tuple[tuple[int, int, int, int], ...] = (),
) -> None:
    image = Image.new("RGB", (400, 600), background_color)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(product_box, radius=20, fill=product_color)
    for pale_box in pale_boxes:
        draw.rounded_rectangle(pale_box, radius=12, fill=(248, 250, 247))
    if watermark:
        draw.rounded_rectangle((145, 530, 255, 575), radius=8, fill=(35, 35, 35))
    image.save(path)


def check_status(result: dict, check_id: str) -> str:
    return next(check["status"] for check in result["checks"] if check["id"] == check_id)


class ImageQualityControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def evaluate(self, filename: str, **kwargs: object) -> dict:
        path = self.root / filename
        make_image(path, **kwargs)
        return evaluate_image(path, DEFAULT_POLICY)

    def test_accepts_centered_product_with_target_width(self) -> None:
        result = self.evaluate("pass.png", product_box=(110, 180, 290, 390), product_color=(65, 75, 70))

        self.assertEqual("pass", result["decision"])
        self.assertEqual("pass", check_status(result, "composition_width"))
        self.assertEqual("pass", check_status(result, "horizontal_center"))

    def test_rejects_product_wider_than_composition_limit(self) -> None:
        result = self.evaluate("wide.png", product_box=(60, 180, 340, 390), product_color=(65, 75, 70))

        self.assertEqual("fail", result["decision"])
        self.assertEqual("fail", check_status(result, "composition_width"))

    def test_rejects_large_horizontal_offset(self) -> None:
        result = self.evaluate("off-center.png", product_box=(185, 180, 365, 390), product_color=(65, 75, 70))

        self.assertEqual("fail", result["decision"])
        self.assertEqual("fail", check_status(result, "horizontal_center"))

    def test_rejects_product_touching_canvas_edge(self) -> None:
        result = self.evaluate("cropped.png", product_box=(0, 180, 180, 390), product_color=(65, 75, 70))

        self.assertEqual("fail", result["decision"])
        self.assertEqual("fail", check_status(result, "edge_clearance"))

    def test_routes_low_contrast_transparent_product_to_human_review(self) -> None:
        result = self.evaluate("transparent.png", product_box=(110, 180, 290, 390), product_color=(248, 250, 247))

        self.assertEqual("review", result["decision"])
        self.assertTrue(result["requires_human_review"])
        self.assertEqual("review", check_status(result, "foreground_confidence"))

    def test_uses_strong_product_signal_when_off_white_background_exceeds_weak_threshold(self) -> None:
        result = self.evaluate(
            "off-white-background.png",
            product_box=(110, 180, 290, 390),
            product_color=(65, 75, 70),
            background_color=(246, 247, 245),
        )

        self.assertEqual("pass", result["decision"])
        self.assertEqual("pass", check_status(result, "composition_width"))
        self.assertEqual("pass", check_status(result, "edge_clearance"))

    def test_routes_pale_signal_touching_edge_to_review_instead_of_passing(self) -> None:
        result = self.evaluate(
            "pale-edge.png",
            product_box=(110, 180, 290, 390),
            product_color=(65, 75, 70),
            pale_boxes=((0, 230, 50, 330),),
        )

        self.assertEqual("review", result["decision"])
        self.assertEqual("review", check_status(result, "edge_clearance"))
        self.assertEqual("review", check_status(result, "foreground_confidence"))

    def test_uses_minimum_foreground_confidence_for_mixed_signal(self) -> None:
        path = self.root / "mixed-signal.png"
        make_image(
            path,
            product_box=(110, 180, 290, 390),
            product_color=(65, 75, 70),
            pale_boxes=((80, 210, 105, 350),),
        )

        result = evaluate_image(path, {**DEFAULT_POLICY, "min_foreground_confidence": 0.95})

        self.assertEqual("review", result["decision"])
        self.assertEqual("review", check_status(result, "foreground_confidence"))

    def test_ignores_bottom_watermark_area_for_post_watermark_check(self) -> None:
        path = self.root / "watermarked.png"
        make_image(path, (110, 180, 290, 390), (65, 75, 70), watermark=True)
        policy = {**DEFAULT_POLICY, "ignore_bottom_ratio": 0.15}

        result = evaluate_image(path, policy)

        self.assertEqual("pass", result["decision"])

    def test_builds_audit_with_required_upload_gate_fields(self) -> None:
        audit = build_audit(
            prompt_version="v2.0",
            reference_plan_path="reference_plan.json",
            pre_watermark_qc={"decision": "pass"},
            post_watermark_qc={"decision": "pass"},
            upload_decision="not_authorized",
        )

        self.assertEqual(
            {"prompt_version", "reference_plan_path", "pre_watermark_qc", "post_watermark_qc", "upload_decision"},
            set(audit).intersection(
                {"prompt_version", "reference_plan_path", "pre_watermark_qc", "post_watermark_qc", "upload_decision"}
            ),
        )
        self.assertEqual("not_authorized", audit["upload_decision"])

    def test_rejects_forced_append_when_pre_watermark_qc_failed(self) -> None:
        with self.assertRaisesRegex(ValueError, "上传决策"):
            build_audit(
                prompt_version="v2.0",
                reference_plan_path="reference_plan.json",
                pre_watermark_qc={"decision": "fail"},
                post_watermark_qc={"decision": "pass"},
                upload_decision="approved_for_append",
            )

    def test_rejects_approved_append_without_auditable_manual_review(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires_human_review"):
            build_audit(
                prompt_version="v2.0",
                reference_plan_path="reference_plan.json",
                pre_watermark_qc={"decision": "pass"},
                post_watermark_qc={"decision": "pass"},
                upload_decision="approved_for_append",
                append_authorization={
                    "status": "approved",
                    "approved_by": "用户",
                    "approval_reference": "本次任务中的明确追加授权",
                },
            )

    def test_approves_append_with_auditable_review_and_authorization(self) -> None:
        audit = build_audit(
            prompt_version="v2.0",
            reference_plan_path="reference_plan.json",
            pre_watermark_qc={"decision": "pass"},
            post_watermark_qc={"decision": "pass"},
            upload_decision="approved_for_append",
            manual_review={
                "status": "approved",
                "reviewer": "质检员",
                "checked_items": ["结构、材质和水印均已人工复核。"],
            },
            append_authorization={
                "status": "approved",
                "approved_by": "用户",
                "approval_reference": "本次任务中的明确追加授权",
            },
        )

        self.assertEqual("approved_for_append", audit["upload_decision"])
        self.assertEqual("approved", audit["manual_review"]["status"])
        self.assertEqual("approved", audit["append_authorization"]["status"])

    def test_cli_returns_nonzero_when_qc_fails(self) -> None:
        image_path = self.root / "wide-for-cli.png"
        output_path = self.root / "wide-for-cli-qc.json"
        make_image(image_path, (60, 180, 340, 390), (65, 75, 70))

        completed = subprocess.run(
            [
                sys.executable,
                str(SKILL_DIR / "scripts" / "evaluate_white_background.py"),
                "--image",
                str(image_path),
                "--output",
                str(output_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(4, completed.returncode, completed.stdout + completed.stderr)
        self.assertTrue(output_path.is_file())
