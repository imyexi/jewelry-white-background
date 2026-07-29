#!/usr/bin/env python3
"""Build the fixed-section V2 prompt from a validated reference plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from reference_plan import PROMPT_VERSION, PlanValidationError, load_plan, validate_plan


def _section(title: str, lines: list[str]) -> str:
    return f"【{title}】\n" + "\n".join(lines)


def build_prompt(plan: Mapping[str, Any]) -> str:
    """Create a model-only prompt; render parameters and audit data stay outside it."""

    data = validate_plan(plan)
    structure = data["structure"]
    composition = data["composition"]
    side_margin_min = (1 - composition["width_ratio_max"]) / 2
    side_margin_max = (1 - composition["width_ratio_min"]) / 2

    structure_lines = [
        f"- {structure['bead_sequence']}",
        f"- {structure['thread']}",
    ]
    if structure["special_components"]:
        for component in structure["special_components"]:
            structure_lines.append(
                f"- 必须保留：{component['name']}（位置：{component['location']}；{component['visual_description']}）"
            )
    else:
        structure_lines.append("- 以正面图中可见的特殊件为准，不新增或重设计。")
    structure_lines.extend(f"- 本次纠偏：{item}" for item in data["corrections"]["structure"])

    material_lines = [f"- {item['subject']}：{item['description']}" for item in data["material_observations"]]
    material_lines.extend(f"- 本次纠偏：{item}" for item in data["corrections"]["material"])

    return "\n\n".join(
        [
            _section(
                "任务目标",
                [
                    "生成同一条真实珠宝手串的电商白底商品图，忠实呈现参考图中的具体产品。",
                ],
            ),
            _section(
                "参考图职责",
                [
                    "- 第 1 张正面图只决定产品结构：珠子数量、顺序、串线、颜色、特殊件及相对位置。",
                    "- 正面图不决定白底画面的景别、产品占比或留白；白底画面的景别、产品占比和留白以【构图】为准。",
                    "- 后续细节图只补充局部材质，不改变产品结构。",
                ],
            ),
            _section("产品结构", structure_lines),
            _section("材质细节", material_lines),
            _section(
                "背景、光线与阴影",
                [
                    "- 背景为无缝 #FBFCF9 哑光米白纸质表面，纹理低对比、干净、均匀。",
                    "- 使用左上方 45 度柔和自然光和中低曝光，保留原图可见的色彩与内部层次。",
                    "- 产品自然放置在纸面上，每颗珠子和特殊件保留柔和、可见的接触阴影。",
                ],
            ),
            _section(
                "构图",
                [
                    "- 产品居中、完整呈现，不裁切任何珠子或特殊件。",
                    "- 重新安排白底商品图的景别，以完整手串外接宽度和本段占比为准。",
                    f"- 先建立产品宽度为 {composition['width_ratio_min']:.0%}-{composition['width_ratio_max']:.0%} 的白底构图，再根据正面图重建手串。",
                    f"- 左右干净留白分别约为 {side_margin_min:.1%}-{side_margin_max:.1%}。",
                    f"- 产品整体宽度占画面宽度的 {composition['width_ratio_min']:.0%}-{composition['width_ratio_max']:.0%}，保留均衡留白。",
                ],
            ),
            _section(
                "画面范围",
                [
                    "- 画面只保留手串本体及其自然接触阴影。",
                    "- 不出现文字、标识、边框或额外道具。",
                ],
            ),
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 V2 reference_plan.json 构建生图 prompt。")
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
