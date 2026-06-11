import json
import pathlib
import subprocess
import time
from PIL import Image

OUT_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = pathlib.Path(r"C:\Users\Administrator\yuan-image\.agents\skills\aireiter-image-generation\references\config.json")
QUERY_URL = "https://aireiter.com/api/openapi/query"
api_key = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["api_key"]
tasks = [
    {"name": "prompt1", "task_id": (OUT_DIR / "prompt1_task_id.txt").read_text(encoding="utf-8").strip()},
    {"name": "prompt2", "task_id": (OUT_DIR / "prompt2_task_id.txt").read_text(encoding="utf-8").strip()},
]
summary = []

def curl_with_retry(args, retries=5):
    last = None
    for attempt in range(1, retries + 1):
        last = subprocess.run(args, cwd=OUT_DIR)
        if last.returncode == 0:
            return
        print(f"curl retry {attempt}/{retries} rc={last.returncode}", flush=True)
        time.sleep(3 * attempt)
    raise subprocess.CalledProcessError(last.returncode, args)

for task in tasks:
    name = task["name"]
    task_id = task["task_id"]
    final = None
    for _ in range(180):
        query_payload = OUT_DIR / f"{name}_query_payload.json"
        final_file = OUT_DIR / f"{name}_final.json"
        query_payload.write_text(json.dumps({"out_task_id": task_id}, ensure_ascii=False), encoding="utf-8")
        curl_with_retry([
            "curl.exe", "-sS", "--request", "POST", "--url", QUERY_URL,
            "--header", f"Authorization: Bearer {api_key}",
            "--header", "Content-Type: application/json",
            "--header", "User-Agent: Mozilla/5.0",
            "--data-binary", f"@{query_payload.name}",
            "--output", final_file.name,
        ])
        final = json.loads(final_file.read_text(encoding="utf-8"))
        status = ((final.get("data") or {}).get("status") or "")
        print(f"{task_id} status={status}", flush=True)
        if status in {"completed", "failed"}:
            break
        time.sleep(5)
    item = {"name": name, "task_id": task_id, "status": ((final or {}).get("data") or {}).get("status") or "unknown", "downloaded_files": []}
    if item["status"] == "completed":
        for idx, output in enumerate(((final.get("data") or {}).get("output") or []), start=1):
            url = output.get("url") if isinstance(output, dict) else None
            if not url:
                continue
            result_file = OUT_DIR / f"{name}_result_{idx}.png"
            curl_with_retry(["curl.exe", "-L", "-sS", "--url", url, "--output", result_file.name])
            with Image.open(result_file) as im:
                item["downloaded_files"].append(str(result_file))
                item["size"] = im.size
                print(f"downloaded={result_file} size={im.size} bytes={result_file.stat().st_size}", flush=True)
    summary.append(item)

(OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"out_dir": str(OUT_DIR), "summary": summary}, ensure_ascii=False, indent=2), flush=True)
