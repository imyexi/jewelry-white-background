import json
import re
import subprocess
import sys
from pathlib import Path


WORKSPACE = Path(r"C:\Users\Administrator\Documents\珠宝白底图生成")
sys.path.insert(0, str(WORKSPACE / "scripts"))

from yuan_image_generation_adapter import generate_to_path


PYTHON = r"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
LARK = r"C:\Users\Administrator\AppData\Roaming\npm\lark-cli.cmd"
PREPROCESS = r"C:\Users\Administrator\.codex\skills\jewelry-white-background\scripts\preprocess_reference_images.py"
WATERMARK = r"C:\Users\Administrator\.codex\skills\yuanyuan-ruyi-watermark\scripts\watermark_images.py"
PROMPT_PATH = Path(r"C:\Users\Administrator\.codex\skills\jewelry-white-background\references\white-background-prompt.md")
RUN_ROOT = Path(r"C:\Users\Administrator\jwb-fresh-20260717-qy029-qy034-qy008")
BASE_TOKEN = "D4Vjbv19WaVVTwsGKdJcsnt5neg"
TABLE_ID = "tblEtBnKFwkgTp22"


PRODUCTS = {
    "QY029": {
        "record_id": "recvoKTrZTcUsm",
        "name": "沉香+南红三圈，可当项链佩戴",
        "params": "尺寸：8mm+；沉香八分沉；产品类型：手链",
        "front": ("Ic5lbB25Sox5iLxViCGc1rcRnad", "cb27b2d9be0e1d35432834328bc67096.jpg"),
        "detail": [("ZVuLbQbxGoatZSxGo1LckELXn9b", "4a692a5e0ef64c136779a2c9f9031eb5.heic")],
        "side": [],
        "correction": "必须保留正面图里的三圈缠绕结构、每一圈的珠子数量和相对位置，不能简化成单圈或双圈。沉香珠保持深棕色木质哑光与天然纹理，南红保持真实红棕色，不得塑料化、过曝或泛橙。",
    },
    "QY034": {
        "record_id": "recvoKTENUkBDX",
        "name": "沉香随型无事牌项链+保山肉柿子红",
        "params": "尺寸：9mm+；沉香八分沉；纯银配件；产品类型：项链",
        "front": ("AjWSbalazogVYmxehxfcig6onmb", "7770c3dad313e5fa84d6dade37e38ba3.jpg"),
        "detail": [
            ("APPpbLM1sobE4TxA7vbchnSkn7g", "c6dac3d90938199292080d83e2105963.heic"),
            ("Pr5Sbb9CMo91pIxn1MAcTvUonzd", "ecb135add9b0bb55b0f88781d499b7aa.heic"),
        ],
        "side": [("Vt2XbkDb3oodoextFmccUNcbnBh", "6e2a1caa390b5c0f4c51b35a03e458a3.heic")],
        "correction": "沉香随型无事牌是必须保留的独立异形主件，保持原有轮廓、厚度、朝向、连接关系和木质纹理，不能替换为圆珠或规则吊坠。保山肉柿子红珠保持正面图里的红棕基调和暗部，不能提亮为鲜橙色。",
    },
    "QY008": {
        "record_id": "recvoKSBPTNrbD",
        "name": "保山冰料柿子红+蜜蜡+海纹石+青金石",
        "params": "尺寸：9mm+；南红；产品类型：设计款",
        "front": ("HmLMbOJF4oqfrWxW3wNc2BJqnLd", "915bfc3771ba9a4874aaa15d303b6a54.heic"),
        "detail": [
            ("GyvEbJzKVoQWukxUyOycuILCnvx", "1ece23bc5dc6772c7716f5d605f9cd2d.heic"),
            ("FXzIbs5K1oraqxxz0X0cboqtnCe", "1f335b906dc95f92242662da350b07a7.heic"),
        ],
        "side": [("PPKabNFm1ohWCvxllsKcSutxnq0", "ef5311a67dc39f57f3f3a675843b7d76.heic")],
        "correction": "南红主珠必须以第 1 张正面图为唯一颜色基准，保留红棕、柿子红至暗红的真实基调、半透明胶质层次和局部暗部；禁止鲜橙、糖果橙、发光红和均匀玻璃珠。蜜蜡、海纹石、青金石与金属配件的颜色、尺寸、位置和顺序必须固定。",
    },
}


def run(command, cwd=None, check=True):
    result = subprocess.run(command, cwd=cwd, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"命令失败({result.returncode}): {' '.join(command)}\n{result.stdout}\n{result.stderr}")
    return result


def download_attachment(product, attachment, kind, index):
    token, original_name = attachment
    directory = RUN_ROOT / product / kind
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(original_name).suffix or ".bin"
    filename = f"{index:02d}_{token}{suffix}"
    run([
        LARK, "base", "+record-download-attachment", "--base-token", BASE_TOKEN,
        "--table-id", TABLE_ID, "--record-id", PRODUCTS[product]["record_id"],
        "--file-token", token, "--output", filename, "--overwrite", "--as", "user", "--format", "json",
    ], cwd=str(directory))
    path = directory / filename
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"飞书附件下载失败: {product} {token}")
    return path


def base_prompt():
    content = PROMPT_PATH.read_text(encoding="utf-8")
    content = content.split("## 追加块：参考优先级", 1)[0]
    content = re.sub(r"^# .*?\n", "", content, count=1)
    return content.replace("## 基础正文", "").strip()


def build_prompt(product_id, data):
    return "\n\n".join([
        base_prompt(),
        "【参考优先级】\n- 第 1 张参考图是最新正面图，只决定整体结构、珠子数量和顺序、配件位置、串线及产品轮廓。\n- 后续参考图均来自同一件产品的最新细节图或侧视图，只恢复局部材质与侧面细节，不能新增珠子、改变顺序或重设计产品。",
        f"【产品名称】\n{data['name']}",
        f"【产品参数】\n产品编号：{product_id}；{data['params']}",
        f"【本次定向纠偏】\n{data['correction']}",
    ])


def preprocess(paths, product_dir):
    output_dir = product_dir / "preprocessed"
    command = [PYTHON, PREPROCESS, "--output-dir", str(output_dir)]
    for path in paths:
        command += ["--input", str(path)]
    payload = json.loads(run(command).stdout)
    ok = [Path(row["preprocessed_path"]) for row in payload.get("records", []) if row.get("status") == "ok"]
    skipped = [row for row in payload.get("records", []) if row.get("status") != "ok"]
    if len(ok) != len(paths):
        raise RuntimeError(f"预处理未通过: {skipped}")
    return ok, payload


def process(product_id, data):
    product_dir = RUN_ROOT / product_id
    for name in ("source", "detail", "side", "logs", "generated", "white-bg"):
        (product_dir / name).mkdir(parents=True, exist_ok=True)
    front_path = download_attachment(product_id, data["front"], "source", 1)
    detail_paths = [download_attachment(product_id, item, "detail", index) for index, item in enumerate(data["detail"], 1)]
    side_paths = [download_attachment(product_id, item, "side", index) for index, item in enumerate(data["side"], 1)]
    reference_paths = [front_path] + detail_paths + side_paths
    preprocessed_paths, preprocess_manifest = preprocess(reference_paths, product_dir)
    prompt = build_prompt(product_id, data)
    prompt_path = product_dir / "logs" / f"{product_id}_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    generated_path = product_dir / "generated" / f"{product_id}_fresh_generated.png"
    generation = generate_to_path(
        prompt,
        preprocessed_paths,
        generated_path,
        product_dir / "logs" / f"{product_id}_generate.json",
    )
    watermark = run([PYTHON, WATERMARK, "--input", str(generated_path), "--output-dir", str(product_dir / "white-bg"), "--product-id", product_id])
    final_path = product_dir / "white-bg" / f"{product_id}_fresh_generated_watermarked.png"
    if not final_path.is_file():
        raise FileNotFoundError(f"未找到水印图: {final_path}")
    return {
        "product_id": product_id,
        "record_id": data["record_id"],
        "status": "generated_pending_qc",
        "front_image": str(front_path),
        "detail_images": [str(path) for path in detail_paths + side_paths],
        "preprocess": preprocess_manifest,
        "provider": generation.provider,
        "task_id": generation.task_id,
        "helper_output_path": str(generation.helper_output),
        "generated_url": None,
        "generated_path": str(generated_path),
        "final_path": str(final_path),
        "upload_status": "pending_qc",
        "run_root": str(product_dir),
        "watermark": watermark.stdout.strip(),
    }


def main():
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    manifest = RUN_ROOT / "run_manifest.jsonl"
    for product_id, data in PRODUCTS.items():
        print(f"开始：{product_id}", flush=True)
        try:
            result = process(product_id, data)
        except Exception as error:
            result = {"product_id": product_id, "record_id": data["record_id"], "status": "failed", "error": str(error)}
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result["status"] == "failed":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
