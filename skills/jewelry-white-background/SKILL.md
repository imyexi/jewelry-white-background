---
name: jewelry-white-background
description: 生成珠宝手串白底电商商品图并添加 Yuan Studio 水印。用于用户提供产品参考图和可选货号，要求预处理 JPG/PNG/WEBP/TIFF/BMP/HEIC/HEIF 参考图、跳过不可处理图片、把同款珠宝手串生成在 #FBFCF9 米白纸质背景上，再添加水印并输出白底图的场景。
---

# 珠宝白底图生成

## 概览

根据产品参考图生成同一条珠宝手串的竖版 3:4、2K、#FBFCF9 哑光米白纸质白底商品图，然后为结果图添加 Yuan Studio 水印。输入是产品参考图和可选货号；输出是本地白底成品图，若提供货号则水印第二行显示 `PN {货号}`。

## 依赖技能

必须按顺序使用以下技能，不要重新实现它们的核心逻辑：

1. `aireiter-image-generation`：用 AIReiter `gpt_image_2` 图生图生成白底图。
2. `yuanyuan-ruyi-watermark`：对生成后的白底图添加底部居中的 Yuan Studio 水印。

本机依赖技能路径：

- `<AIReiter skill root>\SKILL.md`
- `<CODEX_HOME>\skills\yuanyuan-ruyi-watermark\SKILL.md`

## 输入

- `reference_image`：必填，产品参考图。本地路径优先使用绝对路径；支持 `.jpg`、`.jpeg`、`.png`、`.webp`、`.bmp`、`.tif`、`.tiff`、`.heic`、`.heif`。远程图片 URL 可直接交给 AIReiter，但本地文件必须先预处理。
- `product_id`：可选，货号。存在时传给水印脚本并渲染为 `PN {product_id}`；不要把货号写进生图画面。
- `output_root`：可选，默认使用当前工作目录下的 `outputs/jewelry-white-background/<run-id>`。

## 工作流

1. 对本地参考图先运行预处理脚本；若是远程图片 URL，记录为远程输入并跳过本地预处理。

```powershell
& "<python>" "skills\jewelry-white-background\scripts\preprocess_reference_images.py" `
  --input "<reference_image>" `
  --output-dir "outputs/jewelry-white-background/<run-id>/preprocessed"
```

2. 只允许 `manifest.jsonl` 中 `status` 为 `ok` 的 `preprocessed_path` 进入后续生图。`status` 为 `skipped` 的图片必须直接跳过，并记录 `reason`；不要把不可解码、未知扩展、损坏文件或缺少 HEIC/HEIF 解码能力的图片交给 AIReiter。
3. 读取 `references/white-background-prompt.md`，完整使用其中的提示词，不要删减关于配件、阴影、曝光、构图和禁止项的约束。
4. 调用 `aireiter-image-generation` 的 helper 脚本提交图生图任务，`--image` 必须使用预处理后的 JPEG 路径：

```powershell
& "<python>" "<AIReiter skill root>\scripts\aireiter_image_helper.py" submit `
  --prompt "<white-background-prompt.md 的完整提示词>" `
  --aspect-ratio "3:4" `
  --resolution "2K" `
  --image "<preprocessed_path>"
```

5. 记录返回的 `out_task_id`，然后轮询直到完成：

```powershell
& "<python>" "<AIReiter skill root>\scripts\aireiter_image_helper.py" wait `
  --task-id "<out_task_id>"
```

6. 从完成结果的 `data.output[].url` 取得图片 URL，下载到本地未加水印目录，例如：

```text
outputs/jewelry-white-background/<run-id>/generated/<product_id-or-task-id>.png
```

7. 调用 `yuanyuan-ruyi-watermark` 的水印脚本输出最终白底图：

```powershell
& "<python>" "<CODEX_HOME>\skills\yuanyuan-ruyi-watermark\scripts\watermark_images.py" `
  --input "<generated-image-path>" `
  --output-dir "outputs/jewelry-white-background/<run-id>/white-bg"
```

若存在货号，追加：

```powershell
--product-id "<product_id>"
```

8. 返回最终 `white-bg` 目录中的加水印图片路径；同时保留 `preprocessed` 和 `generated` 目录，便于追踪、质检或重跑。

## 预处理脚本行为

- 使用 `scripts/preprocess_reference_images.py` 把可处理的本地参考图统一转成 JPEG，默认最长边 1280px、quality 84，以降低 AIReiter 上传失败率。
- HEIC/HEIF 通过 `pillow-heif` 解码；若环境缺少解码器或图片无法打开，记录为 `skipped`。
- 不支持扩展名、文件不存在、损坏图、解码失败的记录全部写入 `manifest.jsonl`，并带有 `reason`。
- 后续生图循环必须只遍历 `status=ok` 的记录；任何 `skipped` 记录都不允许继续交给 AIReiter、imagegen 或水印技能。

## 质检规则

生成后至少检查以下项目：

- 手串必须是参考图中的同一条真实手串，珠子数量、顺序、圆形轮廓、配件位置和透明串线不能变化。
- 所有非普通圆珠配件必须保留，包括吊坠、小金属环、隔珠/隔片、装饰帽、雕刻珠、连接环、扣件、金属垫圈、小珠、刻纹件和异形珠。
- 背景必须是干净的 #FBFCF9 哑光米白纸质表面，只保留手串本体和自然接触阴影，不得出现托盘、布料、手、黑底、文字、logo、边框或额外道具。
- 光线应为左上 45 度柔和自然光，中低曝光；珠子不能发白、发光或过曝，内部纹理和颜色层次要保留。
- 构图为竖版 3:4，产品居中，占画面宽度约 50%，不得裁切珠子或配件。

如果任一关键项失败，优先用同一参考图和同一提示词重跑一次，并在提示词末尾追加一条简短纠偏说明，例如“上一版改变了配件位置，本次必须完全保持参考图中所有配件的原始位置和顺序”。

## 批量处理

批量输入时，先把全部本地参考图交给 `scripts/preprocess_reference_images.py`；可用 `--queue` 读取包含 `image_path`、`product_id`、`output_path` 的 CSV/JSONL。只对 `manifest.jsonl` 中 `status=ok` 的记录生成独立 `out_task_id` 和本地文件名；`status=skipped` 的记录直接写入跳过清单，不进入 AIReiter。每张图完成后再进入水印步骤；如果每张图有不同货号，优先生成水印 CSV/JSONL 队列，交给 `yuanyuan-ruyi-watermark` 批量处理。

## 失败处理

- 预处理失败或格式不可处理时，标记为 `skipped` 并跳过；不要尝试把原图直接丢给 AIReiter 或其他生图技能。
- HEIC/HEIF 文件必须先通过预处理转成 JPEG；如果当前 Python 环境缺少 `pillow-heif` 或文件无法解码，也标记为 `skipped`。
- AIReiter 提交或轮询失败时，按 `aireiter-image-generation` 技能的失败兜底规则处理。
- 下载结果失败时，不要直接宣称完成；先重试下载或返回图片 URL 与下载错误。
- 水印失败时，返回未加水印图路径、失败命令和错误信息，并提示修复后可只重跑水印步骤。
