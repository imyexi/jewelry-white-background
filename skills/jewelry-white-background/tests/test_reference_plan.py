from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SKILL_DIR = TEST_DIR.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from reference_plan import PlanValidationError, load_plan, validate_plan  # noqa: E402


class ReferencePlanValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_path = TEST_DIR / "fixtures" / "sy1462-reference-plan.json"
        cls.valid_plan = load_plan(cls.fixture_path)

    def test_accepts_background_only_v3_plan_and_preserves_source_image(self) -> None:
        validated = validate_plan(self.valid_plan)

        self.assertEqual("3.0", validated["schema_version"])
        self.assertEqual("background_only_edit", validated["workflow_mode"])
        self.assertEqual(validated["front_image"], validated["structure"]["source_image"])

    def test_accepts_the_minimal_v3_structure_contract(self) -> None:
        plan = copy.deepcopy(self.valid_plan)
        plan["structure"] = {"source_image": plan["front_image"]}

        validated = validate_plan(plan)

        self.assertEqual([], validated["detail_images"])
        self.assertEqual(
            {
                "source_image": plan["front_image"],
                "bead_sequence": "",
                "thread": "",
                "special_components": [],
            },
            validated["structure"],
        )
        self.assertEqual(
            {
                "width_ratio_min": 0.45,
                "width_ratio_max": 0.55,
                "max_center_offset_ratio": 0.08,
                "require_full_product": True,
            },
            validated["composition"],
        )

    def test_normalizes_declared_detail_images_for_legacy_consumers(self) -> None:
        plan = copy.deepcopy(self.valid_plan)
        plan["detail_images"] = ["detail/SY1462_detail_01.jpg"]

        validated = validate_plan(plan)

        self.assertEqual(["detail/SY1462_detail_01.jpg"], validated["detail_images"])

    def test_validates_declared_composition_for_legacy_consumers(self) -> None:
        plan = copy.deepcopy(self.valid_plan)
        plan["composition"] = {
            "width_ratio_min": 0.30,
            "width_ratio_max": 0.55,
            "max_center_offset_ratio": 0.08,
            "require_full_product": True,
        }

        with self.assertRaisesRegex(PlanValidationError, "composition.width_ratio_min"):
            validate_plan(plan)

    def test_rejects_non_v3_schema(self) -> None:
        plan = copy.deepcopy(self.valid_plan)
        plan["schema_version"] = "2.0"

        with self.assertRaisesRegex(PlanValidationError, "schema_version"):
            validate_plan(plan)

    def test_rejects_non_background_only_workflow(self) -> None:
        plan = copy.deepcopy(self.valid_plan)
        plan["workflow_mode"] = "generation"

        with self.assertRaisesRegex(PlanValidationError, "workflow_mode"):
            validate_plan(plan)

    def test_rejects_missing_front_image(self) -> None:
        plan = copy.deepcopy(self.valid_plan)
        plan["front_image"] = ""

        with self.assertRaisesRegex(PlanValidationError, "front_image"):
            validate_plan(plan)

    def test_rejects_structure_source_that_is_not_the_front_image(self) -> None:
        plan = copy.deepcopy(self.valid_plan)
        plan["structure"]["source_image"] = "detail/SY1462_detail_01.jpg"

        with self.assertRaisesRegex(PlanValidationError, "structure.source_image"):
            validate_plan(plan)

    def test_rejects_business_product_fields(self) -> None:
        for field, value in (
            ("product_name", "生肖猴"),
            ("product_parameters", "尺寸 12mm；材料：绿龙晶"),
        ):
            with self.subTest(field=field):
                plan = copy.deepcopy(self.valid_plan)
                plan[field] = value

                with self.assertRaisesRegex(PlanValidationError, field):
                    validate_plan(plan)

    def test_load_plan_rejects_non_object_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            invalid_path = Path(temporary_directory) / "invalid-plan.json"
            invalid_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

            with self.assertRaisesRegex(PlanValidationError, "JSON 对象"):
                load_plan(invalid_path)
