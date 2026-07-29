#!/usr/bin/env python3
"""CLI for validating a jewelry white-background reference plan before generation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reference_plan import PROMPT_VERSION, PlanValidationError, load_plan, validate_plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验珠宝白底图 V2 reference_plan.json。")
    parser.add_argument("--reference-plan", required=True, help="reference_plan.json 路径")
    parser.add_argument("--check-files", action="store_true", help="同时检查正面图和细节图路径是否存在")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan_path = Path(args.reference_plan)
    try:
        plan = load_plan(plan_path)
        normalized = validate_plan(plan, plan_path.parent if args.check_files else None)
    except PlanValidationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "prompt_version": PROMPT_VERSION,
                "product_id": normalized["product_id"],
                "detail_image_count": len(normalized["detail_images"]),
                "special_component_count": len(normalized["structure"]["special_components"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
