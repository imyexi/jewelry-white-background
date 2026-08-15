#!/usr/bin/env python3
"""Build the fixed-section prompt for a background-only Mask Edit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from reference_plan import PROMPT_VERSION, PlanValidationError, load_plan, validate_plan


def _section(title: str, lines: list[str]) -> str:
    return f"【{title}】\n" + "\n".join(lines)


def build_prompt(plan: Mapping[str, Any]) -> str:
    """Create a product-agnostic prompt after validating the edit plan."""

    validate_plan(plan)

    return "\n\n".join(
        [
            _section(
                "任务目标",
                [
                    "将图片 1 的透明背景区域编辑为连续、干净的浅白商品图背景。",
                ],
            ),
            _section(
                "参考图职责",
                [
                    "- 图片 1 是唯一 Edit 底图，不作为创建新产品的参考。",
                    "- 只编辑 Mask 透明背景区域，不使用其他图片补充或替换内容。",
                ],
            ),
            _section(
                "产品结构",
                [
                    "- Mask 不透明的产品区域不得重绘、移动、增删或替换。",
                    "- 保持所有珠子、串线、特殊件及其原有连接关系不变。",
                ],
            ),
            _section(
                "材质细节",
                [
                    "- 保持产品区域的原始颜色、透明度、纹理、反光和可见细节不变。",
                    "- 不润饰、不扩写、不设计任何产品内容。",
                ],
            ),
            _section(
                "背景、光线与阴影",
                [
                    "- 将透明背景区域填充为均匀的 #FBFCF9 浅白背景。",
                    "- 背景必须连续延伸到画布四边和四角，过渡自然且无接缝。",
                    "- 产品区域的原有光线、反光和阴影保持不变。",
                ],
            ),
            _section(
                "构图",
                [
                    "- 保持原画布、原比例以及产品原有位置、尺寸和视角。",
                    "- 不裁切、不旋转、不缩放、不平移产品，也不改变画布边界。",
                ],
            ),
            _section(
                "画面范围",
                [
                    "- 背景不得出现黑色外围、深色外围、暗角、边框或圆形背景块。",
                    "- 不添加道具、文字、标识或其他元素。",
                ],
            ),
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 V3 reference_plan.json 构建背景编辑 prompt。")
    parser.add_argument("--reference-plan", required=True, help="reference_plan.json 路径")
    parser.add_argument("--output", required=True, help="输出 prompt 文本路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        prompt = build_prompt(load_plan(args.reference_plan))
    except PlanValidationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(prompt + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "prompt_version": PROMPT_VERSION, "output": str(output_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
