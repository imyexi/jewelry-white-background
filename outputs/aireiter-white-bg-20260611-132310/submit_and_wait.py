import base64
import json
import pathlib
import subprocess
import time
import urllib.request
from PIL import Image

OUT_DIR = pathlib.Path(__file__).resolve().parent
REF_PATH = pathlib.Path(r"C:\Users\Administrator\AppData\Local\Temp\codex-clipboard-ff89fce7-a51f-44a6-95a9-5078175a1ddd.png")
CONFIG_PATH = pathlib.Path(r"C:\Users\Administrator\yuan-image\.agents\skills\aireiter-image-generation\references\config.json")
SUBMIT_URL = "https://aireiter.com/api/openapi/submit"
QUERY_URL = "https://aireiter.com/api/openapi/query"

PROMPT = """请将附件参考图作为唯一且准确的产品参考。生成一张真实电商商品摄影风格的同款珠宝手串图片，背景为无缝哑光米白色纸质表面，背景颜色为 #FBFCF9。背景带有非常轻微的细腻纸质纹理，只在近距离观察时可见；纹理应低对比、干净、均匀、自然，类似高级商品摄影纸或艺术哑光纸。画面中只保留手串本体及其自然接触阴影；去除垫子、托盘、布料、手、装饰物、灰尘、黑色背景以及任何额外道具。

必须忠实保留这条具体的珠宝手串：保持与参考图一致的珠子数量、珠子顺序、圆形手串轮廓、透明串线、原始珠子颜色、内部包裹物/纹理、材质质感、半透明效果、整体比例，以及手串上所有可见的产品配件。不要改变手串结构、珠子、配件或它们之间的相对位置。

配件保留是关键。请将参考图中所有“非普通圆珠”的组成部分都视为必须固定保留的产品配件，包括吊坠、小金属环、隔珠/隔片、装饰帽、雕刻造型珠、连接环、扣件、金属垫圈、小珠、刻纹件、异形珠，以及任何不规则的非球形珠宝部件。

对于每一个配件，都必须保留它在手串上的原始位置、与相邻珠子的相对顺序、尺寸、轮廓、厚度、朝向、材质、颜色、表面质感和连接结构。如果某个小配件的细节不清晰，请保持它在参考图中相同的简单柔和形状，不要发明新的标记、花纹或装饰。

产品必须保持为参考图中的同一条真实手串。只允许重建背景、光线、阴影和商品图呈现方式。手串结构、珠子、配件以及所有相对位置必须保持不变。

光线与曝光：左上方 45 度柔和自然光，低反差、中低曝光。产品亮度比常规白底商品图低约 15%–20%，背景仍保持 #FBFCF9。珠子不能发白、发光或过曝；高光要小而柔和，必须保留珠子内部纹理和颜色层次。

阴影要求：手串必须看起来真实放置在纸面上，而不是漂浮。每颗珠子和每个配件下方都要有真实柔和的接触阴影，阴影方向应根据左上方光源，略微向右下方延展。阴影应低透明度、暖灰色、细腻但可见；珠子/配件下方要有明确的接触点，并向外自然渐变。接触阴影应与细腻纸质纹理自然融合，形成真实落在表面的感觉，但不要让背景变脏或发灰。

边缘融合：珠子边缘和配件边缘应通过真实的折射、轻微环境遮蔽和柔和边缘过渡，自然融入 #FBFCF9 哑光纸质表面。透明珠子应表现出轻微的背景折射和真实内部反光，整体不要有硬抠图或贴上去的感觉。

构图：竖版 3:4 画幅，2K 分辨率。手串位于画面居中。整个产品应占据画面宽度中间约 60%，左右各保留约 20% 的干净留白。上下也保留充足留白。不要裁切任何珠子或配件。

风格：自然、高级但真实的摄影棚商品摄影，哑光细腻纸质表面，柔和日光，真实珠宝材质质感。不要文字、不要 logo、不要水印、不要边框。"""

api_key = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["api_key"]

# 压缩参考图，避免 data URL 请求体过大。
ref_jpg = OUT_DIR / "reference_1280_q84.jpg"
with Image.open(REF_PATH) as img:
    img = img.convert("RGB")
    img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    img.save(ref_jpg, "JPEG", quality=84, optimize=True)

data_url = "data:image/jpeg;base64," + base64.b64encode(ref_jpg.read_bytes()).decode("ascii")
task_id = f"jewelry-white-bg-new-{time.strftime('%Y%m%d-%H%M%S')}"
payload = {
    "model": "gpt_image_2",
    "params": {
        "prompt": PROMPT,
        "aspect_ratio": "3:4",
        "resolution": "2K",
        "image_url": [data_url],
    },
    "out_task_id": task_id,
}
(OUT_DIR / "prompt.txt").write_text(PROMPT, encoding="utf-8")
(OUT_DIR / "task_id.txt").write_text(task_id, encoding="utf-8")
payload_file = OUT_DIR / "payload.json"
payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

submit_file = OUT_DIR / "submit.json"
subprocess.run([
    "curl.exe", "-sS", "--request", "POST", "--url", SUBMIT_URL,
    "--header", f"Authorization: Bearer {api_key}",
    "--header", "Content-Type: application/json",
    "--header", "User-Agent: Mozilla/5.0",
    "--data-binary", f"@{payload_file.name}",
    "--output", submit_file.name,
], cwd=OUT_DIR, check=True)
submit = json.loads(submit_file.read_text(encoding="utf-8"))
print(json.dumps(submit, ensure_ascii=False), flush=True)
if int(submit.get("statusCode", 0)) >= 400 or not submit.get("ok"):
    raise SystemExit("AIReiter submit failed")

final = None
for _ in range(180):
    query_payload = OUT_DIR / "query_payload.json"
    query_payload.write_text(json.dumps({"out_task_id": task_id}, ensure_ascii=False), encoding="utf-8")
    final_file = OUT_DIR / "final.json"
    subprocess.run([
        "curl.exe", "-sS", "--request", "POST", "--url", QUERY_URL,
        "--header", f"Authorization: Bearer {api_key}",
        "--header", "Content-Type: application/json",
        "--header", "User-Agent: Mozilla/5.0",
        "--data-binary", f"@{query_payload.name}",
        "--output", final_file.name,
    ], cwd=OUT_DIR, check=True)
    final = json.loads(final_file.read_text(encoding="utf-8"))
    status = ((final.get("data") or {}).get("status") or "")
    print(f"{task_id} status={status}", flush=True)
    if status in {"completed", "failed"}:
        break
    time.sleep(5)

if not final or ((final.get("data") or {}).get("status") != "completed"):
    raise SystemExit("AIReiter task not completed")

outputs = (final.get("data") or {}).get("output") or []
for idx, item in enumerate(outputs, start=1):
    url = item.get("url") if isinstance(item, dict) else None
    if not url:
        continue
    result_file = OUT_DIR / f"result_{idx}.png"
    subprocess.run(["curl.exe", "-L", "-sS", "--url", url, "--output", result_file.name], cwd=OUT_DIR, check=True)
    with Image.open(result_file) as im:
        print(f"downloaded={result_file} size={im.size} bytes={result_file.stat().st_size}", flush=True)

print(json.dumps({"out_dir": str(OUT_DIR), "task_id": task_id}, ensure_ascii=False), flush=True)
