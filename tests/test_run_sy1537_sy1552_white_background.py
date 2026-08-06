from __future__ import annotations

import unittest
from pathlib import Path

from scripts.run_sy1537_sy1552_white_background import (
    Attachment,
    ProductRecord,
    TARGET_PRODUCT_IDS,
    plan_upload,
    select_reference_attachments,
    verify_append,
    workspace_relative_output,
)


def attachment(token: str, name: str | None = None) -> Attachment:
    return Attachment(token=token, name=name or f"{token}.jpg")


class Sy1537Sy1552PipelineTests(unittest.TestCase):
    def test_target_ids_are_exact_contiguous_range(self) -> None:
        self.assertEqual(
            TARGET_PRODUCT_IDS,
            tuple(f"SY{number}" for number in range(1537, 1553)),
        )

    def test_reference_selection_places_front_first_and_caps_at_five(self) -> None:
        record = ProductRecord(
            product_id="SY1537",
            record_id="rec1",
            front_images=(attachment("front"),),
            product_images=(attachment("p1"), attachment("p2"), attachment("p3")),
            side_images=(attachment("side1"), attachment("side2")),
            main_images=(),
        )

        selected = select_reference_attachments(record)

        self.assertEqual(
            [item.token for item in selected],
            ["front", "p1", "p2", "p3", "side1"],
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

        self.assertEqual([item.token for item in selected], ["front", "p1", "side1"])

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

    def test_verify_append_requires_original_tokens_to_remain(self) -> None:
        before = (attachment("a"), attachment("b"))
        after = (attachment("a"), attachment("b"), attachment("new", "target.png"))

        self.assertTrue(verify_append(before, after, "new"))

    def test_verify_append_rejects_missing_original_or_duplicate_new_token(self) -> None:
        before = (attachment("a"), attachment("b"))
        missing_original = (attachment("a"), attachment("new", "target.png"))
        duplicate_new = (
            attachment("a"),
            attachment("b"),
            attachment("new", "target.png"),
            attachment("new", "target-copy.png"),
        )

        self.assertFalse(verify_append(before, missing_original, "new"))
        self.assertFalse(verify_append(before, duplicate_new, "new"))

    def test_lark_download_output_is_relative_to_workspace(self) -> None:
        target = Path(__file__).resolve().parents[1] / "outputs" / "target.jpg"

        self.assertEqual(workspace_relative_output(target), "outputs/target.jpg")

    def test_lark_download_output_rejects_path_outside_workspace(self) -> None:
        with self.assertRaises(ValueError):
            workspace_relative_output(Path.home() / "outside.jpg")


if __name__ == "__main__":
    unittest.main()
