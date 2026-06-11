import importlib.util
import json
import mimetypes
import pathlib
import sys
import time
import urllib.request
from PIL import Image

HELPER_PATH = pathlib.Path(r"C:\Users\Administrator\yuan-image\.agents\skills\aireiter-image-generation\scripts\aireiter_image_helper.py")
REF_PATH = pathlib.Path(r"C:\Users\Administrator\AppData\Local\Temp\codex-clipboard-438d8bd1-7dae-4236-b7b1-6b8779d6abce.png")
OUT_DIR = pathlib.Path(__file__).resolve().parent

PROMPT_1 = """请将附件参考图作为唯一且准确的产品参考。生成一张真实电商商品摄影风格的同款珠宝手串图片，背景为无缝哑光米白色纸质表面，背景颜色为 #FBFCF9。背景带有非常轻微的细腻纸质纹理，只在近距离观察时可见；纹理应低对比、干净、均匀、自然，类似高级商品摄影纸或艺术哑光纸。画面中只保留手串本体及其自然接触阴影；去除垫子、托盘、布料、手、装饰物、灰尘、黑色背景以及任何额外道具。

必须忠实保留这条具体的珠宝手串：保持与参考图一致的珠子数量、珠子顺序、圆形手串轮廓、透明串线、原始珠子颜色、内部包裹物/纹理、材质质感、半透明效果、整体比例，以及手串上所有可见的产品配件。不要改变手串结构、珠子、配件或它们之间的相对位置。

配件保留是关键。请将参考图中所有“非普通圆珠”的组成部分都视为必须固定保留的产品配件，包括吊坠、小金属环、隔珠/隔片、装饰帽、雕刻造型珠、连接环、扣件、金属垫圈、小珠、刻纹件、异形珠，以及任何不规则的非球形珠宝部件。

对于每一个配件，都必须保留它在手串上的原始位置、与相邻珠子的相对顺序、尺寸、轮廓、厚度、朝向、材质、颜色、表面质感和连接结构。如果某个小配件的细节不清晰，请保持它在参考图中相同的简单柔和形状，不要发明新的标记、花纹或装饰。

产品必须保持为参考图中的同一条真实手串。只允许重建背景、光线、阴影和商品图呈现方式。手串结构、珠子、配件以及所有相对位置必须保持不变。

光线与曝光：左上方 45 度柔和自然光，低反差、中低曝光。产品亮度比常规白底商品图低约 15%–20%，背景仍保持 #FBFCF9。珠子不能发白、发光或过曝；高光要小而柔和，必须保留珠子内部纹理和颜色层次。

阴影要求：手串必须看起来真实放置在纸面上，而不是漂浮。每颗珠子和每个配件下方都要有真实柔和的接触阴影，阴影方向应根据左上方光源，略微向右下方延展。阴影应低透明度、暖灰色、细腻但可见；珠子/配件下方要有明确的接触点，并向外自然渐变。接触阴影应与细腻纸质纹理自然融合，形成真实落在表面的感觉，但不要让背景变脏或发灰。

边缘融合：珠子边缘和配件边缘应通过真实的折射、轻微环境遮蔽和柔和边缘过渡，自然融入 #FBFCF9 哑光纸质表面。透明珠子应表现出轻微的背景折射和真实内部反光，整体不要有硬抠图或贴上去的感觉。

构图：竖版 3:4 画幅，2K 分辨率。手串位于画面居中。整个产品应占据画面宽度中间约 60%，左右各保留约 20% 的干净留白。上下也保留充足留白。不要裁切任何珠子或配件。

风格：自然、高级但真实的摄影棚商品摄影，哑光细腻纸质表面，柔和日光，真实珠宝材质质感。不要文字、不要 logo、不要水印、不要边框。"""

PROMPT_2 = """参考图为唯一产品依据。仅保留手串和自然接触阴影，去除托盘、布料、手、装饰物和黑底。保持珠子数量、顺序、颜色、材质、纹理、串线及所有配件形状位置不变。背景为 #FBFCF9 哑光白色细纸纹。左上45度柔光，中低曝光，珠子不过曝不发白。手串居中，3:4，2K，左右各留20%，无文字、水印、logo。"""

spec = importlib.util.spec_from_file_location("aireiter_image_helper", HELPER_PATH)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
api_key = h.env_api_key()
if not api_key:
    raise SystemExit("未找到 AIReiter API key")

# 压缩参考图，避免 base64 请求体过大，同时保留足够产品细节。
ref_jpg = OUT_DIR / "reference_1280_q84.jpg"
with Image.open(REF_PATH) as img:
    img = img.convert("RGB")
    img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    img.save(ref_jpg, "JPEG", quality=84, optimize=True)

image_data_url = h.file_to_data_url(str(ref_jpg))
run_stamp = time.strftime("%Y%m%d-%H%M%S")
tasks = [
    {"name": "prompt1", "task_id": f"jewelry-white-bg-p1-{run_stamp}", "prompt": PROMPT_1},
    {"name": "prompt2", "task_id": f"jewelry-white-bg-p2-{run_stamp}", "prompt": PROMPT_2},
]

summary = []
for item in tasks:
    (OUT_DIR / f"{item['name']}.txt").write_text(item["prompt"], encoding="utf-8")
    payload = {
        "model": "gpt_image_2",
        "params": {
            "prompt": item["prompt"],
            "aspect_ratio": "3:4",
            "resolution": "2K",
            "image_url": [image_data_url],
        },
        "out_task_id": item["task_id"],
    }
    submit = h.post_json(h.SUBMIT_URL, payload, api_key)
    (OUT_DIR / f"{item['name']}_submit.json").write_text(json.dumps(submit, ensure_ascii=False, indent=2), encoding="utf-8")
    if not h.is_submit_accepted(submit):
        item["submit"] = submit
        item["status"] = "submit_failed"
        summary.append(item)
        continue

    deadline = time.time() + 900
    last = None
    while True:
        last = h.post_json(h.QUERY_URL, {"out_task_id": item["task_id"]}, api_key)
        status = h.extract_status(last)
        print(f"{item['task_id']} status={status}", flush=True)
        if status in {"completed", "failed"}:
            break
        if time.time() > deadline:
            status = "timeout"
            break
        time.sleep(5)

    (OUT_DIR / f"{item['name']}_final.json").write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
    item["final"] = last
    item["status"] = h.extract_status(last) if last else status
    item["downloaded_files"] = []

    outputs = (((last or {}).get("data") or {}).get("output") or [])
    for idx, output in enumerate(outputs, start=1):
        url = output.get("url") if isinstance(output, dict) else None
        if not url:
            continue
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            content = resp.read()
            ctype = resp.headers.get("Content-Type", "")
        ext = ".png"
        if "jpeg" in ctype or "jpg" in ctype:
            ext = ".jpg"
        elif "webp" in ctype:
            ext = ".webp"
        out_file = OUT_DIR / f"{item['name']}_result_{idx}{ext}"
        out_file.write_bytes(content)
        item["downloaded_files"].append(str(out_file))
    summary.append(item)

(OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"out_dir": str(OUT_DIR), "summary": summary}, ensure_ascii=False, indent=2))
