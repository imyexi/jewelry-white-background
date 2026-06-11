from PIL import Image
from pathlib import Path
out = Path(__file__).resolve().parent
for name in ['prompt1_result_1.png','prompt2_result_1.png']:
    p = out / name
    im = Image.open(p)
    print(name, im.size, p.stat().st_size)
