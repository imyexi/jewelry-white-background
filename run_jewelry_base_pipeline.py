import json
import os
import re
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from scripts.yuan_image_generation_adapter import generate_to_path

PYTHON = r"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
BASE_TOKEN = "D4Vjbv19WaVVTwsGKdJcsnt5neg"
TABLE_ID = "tblEtBnKFwkgTp22"
FIELD_PRODUCT_ID = "fldPxwdPYF"
FIELD_FRONT = "fldqCNEJYB"
FIELD_DETAIL = "fldEDO6Hy8"
FIELD_DESC = "fldpBx1fZw"
FIELD_CRYSTAL = "fldkmjIeJU"
FIELD_TYPE = "fld4K5vs1k"
FIELD_WHITE = "fldaCsBeg0"
PREPROCESS = r"C:\Users\Administrator\.codex\skills\jewelry-white-background\scripts\preprocess_reference_images.py"
WATERMARK = r"C:\Users\Administrator\.codex\skills\yuanyuan-ruyi-watermark\scripts\watermark_images.py"
PROMPT_FILE = Path(r"C:\Users\Administrator\.codex\skills\jewelry-white-background\references\white-background-prompt.md")
ROOT = Path(r"C:\Users\Administrator\Documents\珠宝白底图生成\outputs\jewelry-white-background\base-021-20260717")

FIELD_IDS = [FIELD_PRODUCT_ID, FIELD_FRONT, FIELD_DETAIL, FIELD_DESC, FIELD_CRYSTAL, FIELD_TYPE, FIELD_WHITE]
FIELD_NAMES = ["产品编号", "正面图", "细节图", "描述", "主水晶类型", "产品类型", "白底图"]

os.environ["PYTHONIOENCODING"] = "utf-8"


def run(cmd, check=True, cwd=None):
    p = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True, cwd=cwd)
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed {p.returncode}: {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return p


def safe_name(s):
    return re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", str(s or "")).strip("_")[:80] or "item"


def attachments(v):
    return v if isinstance(v, list) else []


def values_to_text(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return "、".join(map(str, v))
    return str(v)


def load_records():
    cmd = [r"C:\\Users\\Administrator\\AppData\\Roaming\\npm\\lark-cli.cmd", "base", "+record-list", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
           "--limit", "200", "--format", "json", "--as", "user"]
    for f in FIELD_IDS:
        cmd += ["--field-id", f]
    data = json.loads(run(cmd).stdout)
    out = []
    for rid, row in zip(data["data"]["record_id_list"], data["data"]["data"]):
        item = dict(zip(FIELD_NAMES, row))
        item["record_id"] = rid
        out.append(item)
    return out


def download_attachment(record_id, token, name, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    ext = Path(name or "").suffix or ".bin"
    target = outdir / f"{token}{ext}"
    if target.exists() and target.stat().st_size > 0:
        return target
    cmd = [r"C:\\Users\\Administrator\\AppData\\Roaming\\npm\\lark-cli.cmd", "base", "+record-download-attachment", "--base-token", BASE_TOKEN,
           "--table-id", TABLE_ID, "--record-id", record_id, "--file-token", token,
           "--output", target.name, "--overwrite", "--format", "json", "--as", "user"]
    run(cmd, cwd=str(outdir))
    if not target.exists():
        # lark-cli may choose original filename when output is treated as a directory.
        candidates = list(outdir.glob(f"*{token}*")) + list(outdir.glob(name or "__none__"))
        if candidates:
            return candidates[0]
        raise FileNotFoundError(f"downloaded file not found for {record_id} {token}")
    return target


def base_prompt():
    text = PROMPT_FILE.read_text(encoding="utf-8")
    marker = "## 追加块：参考优先级"
    if marker in text:
        text = text.split(marker, 1)[0].strip()
    text = re.sub(r"^# .*?\n", "", text, count=1).strip()
    text = text.replace("## 基础正文", "").strip()
    return text

BASE_PROMPT = base_prompt()


def sensitive_constraints(product_text):
    t = product_text
    rules = []
    if any(k in t for k in ["幽灵", "雪花", "包裹物"]):
        rules.append("- 珠内云雾状、苔状、雪花状或分层状包裹物必须清晰可见，不能被做成干净透明玻璃珠。")
    if any(k in t for k in ["发晶", "绿发", "黑发", "钛晶", "兔毛", "红铜发"]):
        rules.append("- 发晶类珠子内部平行或交织的丝状发丝必须清晰可见，不能变成均匀色块。")
    if any(k in t for k in ["随型", "随行", "异形", "方糖", "原矿", "切面", "不规则", "树丁", "无事牌", "药片", "直切", "算盘"]):
        rules.append("- 随型、方糖、药片、树丁、无事牌等非圆形部件必须保留独立边界、体积和轮廓，不能与相邻浅色珠粘连或融合。")
    if any(k in t for k in ["白水晶", "月光", "冰料", "透体", "透明", "海蓝宝"]):
        rules.append("- 浅色透明珠必须依靠边缘折射、接触阴影和内部层次与 #FBFCF9 背景分开，不能发虚或融进背景。")
    if any(k in t for k in ["配件", "吊坠", "隔片", "小环", "扣", "金属", "蜜蜡", "琥珀", "青金石", "和田", "翡翠", "绿松石", "海纹石", "南红"]):
        rules.append("- 所有配件的原始朝向、尺寸、连接关系和相对顺序必须固定，不能被优化设计或替换。")
    return "\n".join(rules)


def build_prompt(item):
    pid = item["产品编号"]
    desc = values_to_text(item.get("描述"))
    crystal = values_to_text(item.get("主水晶类型"))
    ptype = values_to_text(item.get("产品类型"))
    params = "；".join([x for x in [desc, crystal, ptype] if x])
    pieces = [BASE_PROMPT]
    pieces.append("【参考优先级】\n- 第 1 张参考图是正面图，只决定整件产品结构、珠子顺序、配件位置、串线和整体轮廓。\n- 后续参考图都是同一件产品的局部放大或补充视角，只用来恢复局部材质细节，不能新增珠子、不能改顺序、不能改配件、不能把局部形状扩展成整件新结构。")
    if desc:
        pieces.append(f"【产品名称】\n{desc}")
    if params:
        pieces.append(f"【产品参数】\n产品编号：{pid}；{params}")
    constraints = sensitive_constraints(params)
    if constraints:
        pieces.append(f"【材质敏感约束】\n{constraints}")
    return "\n\n".join(pieces)


def preprocess(paths, product_dir):
    outdir = product_dir / "preprocessed"
    cmd = [PYTHON, PREPROCESS, "--output-dir", str(outdir)]
    for p in paths:
        cmd += ["--input", str(p)]
    result = json.loads(run(cmd).stdout)
    ok = [Path(r["preprocessed_path"]) for r in result.get("records", []) if r.get("status") == "ok"]
    skipped = [r for r in result.get("records", []) if r.get("status") != "ok"]
    return ok, skipped, result


def watermark(image_path, pid, product_dir):
    outdir = product_dir / "white-bg"
    outdir.mkdir(parents=True, exist_ok=True)
    run([PYTHON, WATERMARK, "--input", str(image_path), "--output-dir", str(outdir), "--product-id", pid])
    candidates = sorted(outdir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for c in candidates:
        if c.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            return c
    raise RuntimeError(f"watermark output not found for {pid}")


def upload(record_id, final_path):
    final_path = Path(final_path)
    cmd = [r"C:\\Users\\Administrator\\AppData\\Roaming\\npm\\lark-cli.cmd", "base", "+record-upload-attachment", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
           "--record-id", record_id, "--field-id", FIELD_WHITE, "--file", final_path.name, "--format", "json", "--as", "user"]
    return json.loads(run(cmd, cwd=str(final_path.parent)).stdout)


def process_one(item):
    pid = item["产品编号"]
    product_dir = ROOT / safe_name(pid)
    for sub in ["source", "logs"]:
        (product_dir / sub).mkdir(parents=True, exist_ok=True)
    result = {"product_id": pid, "record_id": item["record_id"], "run_root": str(product_dir)}
    front = attachments(item.get("正面图"))
    if not front:
        result.update(status="skipped", reason="无正面图")
        return result
    white = attachments(item.get("白底图"))
    if white:
        result.update(status="skipped", reason="白底图字段已有附件", existing_white=white)
        return result
    existing = sorted((product_dir / "white-bg").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) if (product_dir / "white-bg").exists() else []
    existing = [p for p in existing if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    if existing:
        final_path = existing[0]
        upload_json = upload(item["record_id"], final_path)
        result["final_path"] = str(final_path)
        result["upload_result"] = upload_json
        result["uploaded_file_token"] = (((upload_json.get("data") or {}).get("file_tokens") or [None])[0])
        result["status"] = "completed"
        result["reused_existing_final"] = True
        return result
    refs = []
    all_front = front[:]
    # 第 1 张正面图作结构主参考；其余正面图和细节图作补充，最多总计 5 张。
    for att in all_front[:1] + all_front[1:3] + attachments(item.get("细节图"))[:4]:
        if len(refs) >= 5:
            break
        refs.append(download_attachment(item["record_id"], att["file_token"], att.get("name", ""), product_dir / "source"))
    ok_images, skipped, pp_summary = preprocess(refs, product_dir)
    result["preprocess"] = pp_summary
    if not ok_images:
        result.update(status="skipped", reason="preprocess failed", skipped=skipped)
        return result
    prompt = build_prompt(item)
    prompt_path = product_dir / "logs" / f"{pid}_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    generated_path = product_dir / "generated" / f"{pid}_generated.png"
    generation_log = product_dir / "logs" / f"{pid}_generate.json"
    if generated_path.is_file() and generated_path.stat().st_size > 0:
        task_id = "reused-existing-output"
        helper_output_path = str(generated_path)
    else:
        generation = generate_to_path(
            prompt,
            ok_images,
            generated_path,
            generation_log,
        )
        task_id = generation.task_id
        helper_output_path = str(generation.helper_output)
    result["provider"] = "wawapi"
    result["task_id"] = task_id
    result["helper_output_path"] = helper_output_path
    result["generated_path"] = str(generated_path)
    result["generated_url"] = None
    final_path = watermark(generated_path, pid, product_dir)
    result["final_path"] = str(final_path)
    upload_json = upload(item["record_id"], final_path)
    result["upload_result"] = upload_json
    result["uploaded_file_token"] = (((upload_json.get("data") or {}).get("file_tokens") or [None])[0])
    result["status"] = "completed"
    return result


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    records = load_records()
    manifest_path = ROOT / "run_manifest.jsonl"
    done = set()
    if manifest_path.exists():
        for line in manifest_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    rec = json.loads(line)
                    if rec.get("status") in {"completed", "skipped"}:
                        done.add(rec.get("product_id"))
                except Exception:
                    pass
    todo = [item for item in records if item.get(FIELD_NAMES[0]) not in done]
    summary = {"completed": 0, "skipped": 0, "failed": 0, "total_records": len(records), "todo": len(todo)}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    with manifest_path.open("a", encoding="utf-8") as mf:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(process_one, item): item for item in todo}
            for fut in as_completed(futures):
                item = futures[fut]
                pid = item[FIELD_NAMES[0]]
                try:
                    rec = fut.result()
                except Exception as e:
                    rec = {"product_id": pid, "record_id": item["record_id"], "status": "failed", "error": str(e)}
                    print(f"FAILED {pid}: {e}", file=sys.stderr, flush=True)
                else:
                    print(f"{rec['status'].upper()} {pid}", flush=True)
                summary[rec.get("status", "failed")] = summary.get(rec.get("status", "failed"), 0) + 1
                mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                mf.flush()
    (ROOT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
