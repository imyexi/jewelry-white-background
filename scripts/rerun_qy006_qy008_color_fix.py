import json
import subprocess
import sys
from pathlib import Path


WORKSPACE = Path(r"C:\Users\Administrator\Documents\珠宝白底图生成")
sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(WORKSPACE / "scripts"))

from yuan_image_generation_adapter import generate_to_path


PYTHON = sys.executable
WATERMARK = str(
    Path.home()
    / ".codex"
    / "skills"
    / "yuanyuan-ruyi-watermark"
    / "scripts"
    / "watermark_images.py"
)


def run(command):
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return completed


SOURCE_ROOT = WORKSPACE / "outputs" / "jewelry-white-background" / "base-021-20260717"
RUN_ROOT = SOURCE_ROOT / "corrections" / "20260712-color-fix"

PRODUCTS = {
    "QY006": {
        "images": [
            "MhhKbQ9sKoBrPnx40IxcrkgTnK5.jpg",
            "E8GYbamELozNPVxoWWWcGJMNnhe.jpg",
            "SfkwbTiDkoDTnwxra73cf7A8nQe.jpg",
            "EnBEbOZCxoXJmvxrIaFcl5w6nHd.jpg",
            "Hjrkbo08roFxsyxUhkNcwpJBn7e.jpg",
        ],
        "correction": (
            "上版把保山冰料樱桃红南红主珠提亮并橙化了。第 1 张正面图是唯一颜色基准；"
            "主珠必须保持正面图中的偏深红、红棕至暗红基调、半透明胶质层次和局部暗部，"
            "不得提高主珠整体明度或把色相推向鲜橙、糖果橙、发光红，也不能做成均匀玻璃珠。"
            "产品外接宽度严格控制为画布宽度的 48%-52%，左右留白各约 24%-26%。"
        ),
    },
    "QY007": {
        "images": [
            "RXNmbySiHotWrdxtAIXcBYeZnNb.jpg",
            "Wn6sbbNHYoFofjxibVOcnizVndh.jpg",
            "Bi5cbtPA4oqCJAxXzxcc2xfynhd.jpg",
            "Zx0KbDgSpoTMmKxa00gc36wGntd.jpg",
        ],
        "correction": (
            "上版把保山冰料樱桃红南红主珠提亮并橙化了。第 1 张正面图是唯一颜色基准；"
            "主珠必须保持正面图中的偏深红、红棕至暗红基调、半透明胶质层次和局部暗部，"
            "不得提高主珠整体明度或把色相推向鲜橙、糖果橙、发光红，也不能做成均匀玻璃珠。"
            "产品外接宽度严格控制为画布宽度的 48%-52%，左右留白各约 24%-26%。"
        ),
    },
    "QY008": {
        "images": [
            "HmLMbOJF4oqfrWxW3wNc2BJqnLd.jpg",
            "GyvEbJzKVoQWukxUyOycuILCnvx.jpg",
            "FXzIbs5K1oraqxxz0X0cboqtnCe.jpg",
        ],
        "correction": (
            "上版把保山冰料柿子红南红主珠提亮成了均匀鲜橙色。第 1 张正面图是唯一颜色基准；"
            "主珠必须保持正面图中的红棕、柿子红至暗红基调、半透明胶质层次和局部暗部，"
            "不得提高主珠整体明度或变成糖果橙、亮橙、发光红，也不能抹平每颗珠子的天然色差。"
            "产品外接宽度严格控制为画布宽度的 48%-52%，左右留白各约 24%-26%。"
        ),
    },
}


def generate_one(product_id, config):
    source_dir = SOURCE_ROOT / product_id
    product_dir = RUN_ROOT / product_id
    logs_dir = product_dir / "logs"
    generated_dir = product_dir / "generated"
    white_dir = product_dir / "white-bg"
    for directory in (logs_dir, generated_dir, white_dir):
        directory.mkdir(parents=True, exist_ok=True)

    original_prompt = (source_dir / "logs" / f"{product_id}_prompt.txt").read_text(encoding="utf-8").rstrip()
    prompt = f"{original_prompt}\n\n【本次定向纠偏】\n{config['correction']}\n"
    prompt_path = logs_dir / f"{product_id}_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    images = [source_dir / "preprocessed" / name for name in config["images"]]
    missing = [str(path) for path in images if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"缺少预处理参考图: {missing}")

    generated_path = generated_dir / f"{product_id}_color_fix.png"
    generation = generate_to_path(
        prompt,
        images,
        generated_path,
        logs_dir / f"{product_id}_generate.json",
    )

    watermarked = run(
        [
            PYTHON,
            WATERMARK,
            "--input",
            str(generated_path),
            "--output-dir",
            str(white_dir),
            "--product-id",
            product_id,
        ]
    )
    candidates = sorted(white_dir.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
    final_path = next(path for path in candidates if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
    return {
        "product_id": product_id,
        "status": "generated_pending_qc",
        "provider": generation.provider,
        "task_id": generation.task_id,
        "reference_images": [str(path) for path in images],
        "correction_prompt": config["correction"],
        "helper_output_path": str(generation.helper_output),
        "generated_url": None,
        "generated_path": str(generated_path),
        "final_path": str(final_path),
        "watermark_stdout": watermarked.stdout.strip(),
        "run_root": str(product_dir),
    }


def main():
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = RUN_ROOT / "run_manifest.jsonl"
    for product_id, config in PRODUCTS.items():
        print(f"开始生成 {product_id}", flush=True)
        try:
            result = generate_one(product_id, config)
        except Exception as exc:
            result = {"product_id": product_id, "status": "failed", "error": str(exc)}
        with manifest_path.open("a", encoding="utf-8") as manifest:
            manifest.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result["status"] == "failed":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
