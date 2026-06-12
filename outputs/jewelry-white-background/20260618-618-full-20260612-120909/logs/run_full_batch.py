import csv
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None
    ImageOps = None

CWD = Path(r"C:\Users\Administrator\Documents\珠宝白底图生成")
BASE_TOKEN = "D4Vjbv19WaVVTwsGKdJcsnt5neg"
TABLE_ID = "tblwSMbqUjjJ3Eiy"
PRODUCT_IMAGE_FIELD = "fldWCFdZbJ"
PYTHON = Path(r"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
HELPER_PATH = Path(r"C:\Users\Administrator\yuan-image\.agents\skills\aireiter-image-generation\scripts\aireiter_image_helper.py")
WATERMARK_SCRIPT = Path(r"C:\Users\Administrator\.codex\skills\yuanyuan-ruyi-watermark\scripts\watermark_images.py")
LARK_CLI = r"C:\Users\Administrator\AppData\Roaming\npm\lark-cli.cmd"
PROMPT_PATH = Path(r"C:\Users\Administrator\.codex\skills\jewelry-white-background\references\white-background-prompt.md")
RUN_ROOT = Path(sys.argv[1]).resolve()
BATCH_SIZE = int(sys.argv[2]) if len(sys.argv) > 2 else 12
POLL_INTERVAL = 8
BATCH_TIMEOUT = 1800

spec = importlib.util.spec_from_file_location("aireiter_helper", str(HELPER_PATH))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
API_KEY = helper.env_api_key()
PROMPT = PROMPT_PATH.read_text(encoding="utf-8")

for sub in ["source", "prepared", "generated", "white-bg", "logs"]:
    (RUN_ROOT / sub).mkdir(parents=True, exist_ok=True)

STATE_PATH = RUN_ROOT / "logs" / "full_state.json"
EVENTS_PATH = RUN_ROOT / "logs" / "full_events.jsonl"
REGISTRY_PATH = CWD / "outputs" / "jewelry-white-background" / "generated_products_registry.csv"
QC_PATH = RUN_ROOT / "logs" / "full_basic_qc.jsonl"
UPLOAD_MANIFEST = RUN_ROOT / "logs" / "full_upload_manifest.jsonl"

REGISTRY_FIELDS = [
    "product_id", "record_id", "settlement", "source_file_token", "task_id",
    "source_path", "generated_path", "final_path", "upload_status",
    "uploaded_file_token", "uploaded_at", "run_root",
]


def safe_name(text):
    text = str(text or "").strip()
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text)
    return text or "unnamed"


def rel(path):
    return os.path.relpath(str(Path(path).resolve()), str(CWD))


def run_cmd(args, timeout=300):
    proc = subprocess.run(args, cwd=str(CWD), text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return proc.returncode, proc.stdout


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def event(kind, row):
    payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **row}
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"[{payload['ts']}] {kind}: {row.get('product_id','')} {row.get('status','')}", flush=True)


def ensure_registry():
    if not REGISTRY_PATH.exists():
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with REGISTRY_PATH.open("w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=REGISTRY_FIELDS).writeheader()


def registry_uploaded_products():
    ensure_registry()
    done = set()
    with REGISTRY_PATH.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("upload_status") == "uploaded" and row.get("product_id"):
                done.add(row["product_id"])
    return done


def append_registry(row):
    ensure_registry()
    with REGISTRY_PATH.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REGISTRY_FIELDS)
        writer.writerow({k: row.get(k, "") for k in REGISTRY_FIELDS})


def source_ext(name):
    suffix = Path(name or "").suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    return ".jpg"


def download_source(item):
    pn = safe_name(item["product_id"])
    out = RUN_ROOT / "source" / f"{pn}_front{source_ext(item.get('front_name'))}"
    if out.exists() and out.stat().st_size > 0:
        return out
    code, raw = run_cmd([
        LARK_CLI, "base", "+record-download-attachment", "--as", "user",
        "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
        "--record-id", item["record_id"], "--file-token", item["front_file_token"],
        "--output", rel(out), "--overwrite",
    ], timeout=180)
    if code != 0 or not out.exists():
        raise RuntimeError(raw)
    return out


def prepare_image(path, pn):
    prepared = RUN_ROOT / "prepared" / f"{safe_name(pn)}_front_1280_q84.jpg"
    if prepared.exists() and prepared.stat().st_size > 0:
        return prepared
    if Image is None:
        return path
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                if "A" in im.getbands():
                    bg.paste(im, mask=im.getchannel("A"))
                    im = bg
                else:
                    im = im.convert("RGB")
            else:
                im = im.convert("RGB")
            im.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            im.save(prepared, "JPEG", quality=84, optimize=True)
        return prepared
    except Exception as exc:
        event("prepare_image_fallback_original", {"product_id": pn, "status": "original", "error": str(exc), "source_path": str(path)})
        return path


def submit_task(item, image_path):
    pn = safe_name(item["product_id"])
    task_id = f"jewelry-white-bg-{pn}-{time.strftime('%Y%m%d%H%M%S')}"
    image_value = helper.normalize_image_input(str(image_path))
    payload = {
        "model": "gpt_image_2",
        "params": {
            "prompt": PROMPT,
            "aspect_ratio": "3:4",
            "resolution": "2K",
            "image_url": image_value,
        },
        "out_task_id": task_id,
    }
    result = helper.post_json(helper.SUBMIT_URL, payload, API_KEY)
    (RUN_ROOT / "logs" / f"{pn}_submit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not helper.is_submit_accepted(result):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return task_id


def query_task(task_id):
    return helper.post_json(helper.QUERY_URL, {"out_task_id": task_id}, API_KEY)


def download_generated(pn, url):
    out = RUN_ROOT / "generated" / f"{safe_name(pn)}_generated.png"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        out.write_bytes(resp.read())
    return out


def run_watermark(generated_items):
    queue = RUN_ROOT / "logs" / f"watermark_queue_{int(time.time())}.csv"
    with queue.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["image_path", "product_id", "output_path"])
        writer.writeheader()
        for item in generated_items:
            pn = item["product_id"]
            writer.writerow({
                "image_path": item["generated_path"],
                "product_id": pn,
                "output_path": str(RUN_ROOT / "white-bg" / f"{safe_name(pn)}_white_bg_watermarked.png"),
            })
    code, raw = run_cmd([str(PYTHON), str(WATERMARK_SCRIPT), "--queue", str(queue), "--output-dir", str(RUN_ROOT / "white-bg")], timeout=600)
    (RUN_ROOT / "logs" / f"watermark_{int(time.time())}.log").write_text(raw, encoding="utf-8")
    if code != 0:
        raise RuntimeError(raw)


def basic_qc(path):
    if Image is None:
        return {"ok": Path(path).stat().st_size > 100000, "size": Path(path).stat().st_size}
    with Image.open(path) as im:
        w, h = im.size
    ratio = w / h
    ok = abs(ratio - 0.75) < 0.02 and Path(path).stat().st_size > 100000
    return {"ok": ok, "width": w, "height": h, "ratio": round(ratio, 4), "size": Path(path).stat().st_size}


def parse_json_from_raw(raw):
    idx = raw.find("{")
    if idx < 0:
        return None
    return json.loads(raw[idx:])


def upload_final(item, final_path, task_id, generated_path, source_path):
    code, raw = run_cmd([
        LARK_CLI, "base", "+record-upload-attachment", "--as", "user",
        "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
        "--record-id", item["record_id"], "--field-id", PRODUCT_IMAGE_FIELD,
        "--file", rel(final_path),
    ], timeout=300)
    upload_token = ""
    if code == 0:
        try:
            obj = parse_json_from_raw(raw)
            attachments = obj["data"]["attachments"][item["record_id"]][PRODUCT_IMAGE_FIELD]
            expected = Path(final_path).name
            for att in attachments:
                if att.get("name") == expected:
                    upload_token = att.get("file_token", "")
        except Exception:
            pass
    upload_row = {
        "product_id": item["product_id"],
        "record_id": item["record_id"],
        "settlement": item.get("settlement", ""),
        "source_file_token": item.get("front_file_token", ""),
        "task_id": task_id,
        "final_path": str(final_path),
        "upload_status": "uploaded" if code == 0 else "failed_upload",
        "uploaded_file_token": upload_token,
        "raw": raw,
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with UPLOAD_MANIFEST.open("a", encoding="utf-8") as f:
        f.write(json.dumps(upload_row, ensure_ascii=False, separators=(",", ":")) + "\n")
    if code != 0:
        raise RuntimeError(raw)
    append_registry({
        "product_id": item["product_id"],
        "record_id": item["record_id"],
        "settlement": item.get("settlement", ""),
        "source_file_token": item.get("front_file_token", ""),
        "task_id": task_id,
        "source_path": str(source_path),
        "generated_path": str(generated_path),
        "final_path": str(final_path),
        "upload_status": "uploaded",
        "uploaded_file_token": upload_token,
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_root": str(RUN_ROOT),
    })
    return upload_token


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i+size]


def main():
    pending_path = RUN_ROOT / "logs" / "pending_records.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8-sig"))
    state = load_state()
    done_registry = registry_uploaded_products()
    work = []
    for item in pending:
        pn = item.get("product_id")
        st = state.get(pn, {}).get("status")
        if pn in done_registry or st == "uploaded":
            continue
        work.append(item)
    event("start", {"product_id": "", "status": f"work={len(work)} batch_size={BATCH_SIZE}"})

    for batch_index, batch in enumerate(chunks(work, BATCH_SIZE), start=1):
        submitted = []
        event("batch_start", {"product_id": "", "status": f"batch={batch_index} count={len(batch)}"})
        state = load_state()
        for item in batch:
            pn = item["product_id"]
            try:
                source = download_source(item)
                prepared = prepare_image(source, pn)
                task_id = submit_task(item, prepared)
                row = {"status": "submitted", "task_id": task_id, "source_path": str(source), "prepared_path": str(prepared), "record_id": item["record_id"]}
                state[pn] = row
                save_state(state)
                submitted.append({"item": item, **row})
                event("submitted", {"product_id": pn, "status": "submitted", "task_id": task_id})
                time.sleep(1)
            except Exception as exc:
                state[pn] = {"status": "failed_submit", "error": str(exc), "record_id": item.get("record_id", "")}
                save_state(state)
                event("failed_submit", {"product_id": pn, "status": "failed_submit", "error": str(exc)[:500]})

        remaining = {x["task_id"]: x for x in submitted}
        deadline = time.time() + BATCH_TIMEOUT
        generated = []
        while remaining and time.time() < deadline:
            for task_id, meta in list(remaining.items()):
                pn = meta["item"]["product_id"]
                try:
                    result = query_task(task_id)
                    status = helper.extract_status(result)
                    if status == "completed":
                        (RUN_ROOT / "logs" / f"{safe_name(pn)}_wait.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                        url = result.get("data", {}).get("output", [{}])[0].get("url")
                        if not url:
                            raise RuntimeError("completed without output url")
                        gen_path = download_generated(pn, url)
                        state[pn].update({"status": "generated", "generated_path": str(gen_path), "generated_url": url, "credits_used": result.get("credits_used", "")})
                        save_state(state)
                        generated.append({"product_id": pn, "item": meta["item"], "task_id": task_id, "source_path": meta["source_path"], "generated_path": str(gen_path)})
                        del remaining[task_id]
                        event("generated", {"product_id": pn, "status": "generated", "task_id": task_id})
                    elif status == "failed":
                        state[pn].update({"status": "failed_generation", "error": json.dumps(result, ensure_ascii=False)})
                        save_state(state)
                        del remaining[task_id]
                        event("failed_generation", {"product_id": pn, "status": "failed_generation", "task_id": task_id})
                except Exception as exc:
                    event("query_warning", {"product_id": pn, "status": "query_warning", "task_id": task_id, "error": str(exc)[:300]})
            if remaining:
                time.sleep(POLL_INTERVAL)
        for task_id, meta in list(remaining.items()):
            pn = meta["item"]["product_id"]
            state[pn].update({"status": "failed_timeout", "task_id": task_id})
            save_state(state)
            event("failed_timeout", {"product_id": pn, "status": "failed_timeout", "task_id": task_id})

        if generated:
            try:
                run_watermark(generated)
            except Exception as exc:
                for g in generated:
                    pn = g["product_id"]
                    state[pn].update({"status": "failed_watermark", "error": str(exc)})
                save_state(state)
                event("failed_watermark_batch", {"product_id": "", "status": "failed_watermark", "error": str(exc)[:500]})
                continue

        for g in generated:
            item = g["item"]
            pn = g["product_id"]
            final = RUN_ROOT / "white-bg" / f"{safe_name(pn)}_white_bg_watermarked.png"
            try:
                qc = basic_qc(final)
                with QC_PATH.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"product_id": pn, "file": str(final), **qc}, ensure_ascii=False, separators=(",", ":")) + "\n")
                if not qc.get("ok"):
                    raise RuntimeError(f"basic qc failed: {qc}")
                token = upload_final(item, final, g["task_id"], g["generated_path"], g["source_path"])
                state[pn].update({"status": "uploaded", "final_path": str(final), "uploaded_file_token": token, "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                save_state(state)
                event("uploaded", {"product_id": pn, "status": "uploaded", "uploaded_file_token": token})
            except Exception as exc:
                state[pn].update({"status": "failed_upload_or_qc", "error": str(exc)[:1000], "final_path": str(final)})
                save_state(state)
                event("failed_upload_or_qc", {"product_id": pn, "status": "failed_upload_or_qc", "error": str(exc)[:500]})

    state = load_state()
    counts = {}
    for row in state.values():
        counts[row.get("status", "unknown")] = counts.get(row.get("status", "unknown"), 0) + 1
    event("finished", {"product_id": "", "status": json.dumps(counts, ensure_ascii=False)})

if __name__ == "__main__":
    main()
