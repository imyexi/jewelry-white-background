---
name: jewelry-white-background
description: 在需要根据珠宝手串正面图和细节图生成白底商品图、添加 Yuan Studio 水印，并控制结构漂移、材质失真、构图占比或主图追加前质检时使用。
---

# 珠宝白底图生成

## 核心原则

使用“参考计划 -> 固定提示词 -> Yuan Image Generation v4 -> 双阶段质检 -> 写回门禁”流程。

- 正面图是唯一的整体结构依据，只决定珠子数量、顺序、串线、颜色、特殊件和相对位置。
- 细节图只补充局部材质，不得新增、删除或重排产品结构。
- 画幅和分辨率只作为图片生成 helper 参数传递，不写入模型提示词。
- 生成器只负责请求并保存第一张有效图片；珠宝结构、材质、构图和水印由本技能的外层 QC 判断。
- 未经水印前后 QC、人工复核和用户明确授权的图片不得追加到主图。

将 `<SKILL_ROOT>` 替换为本技能目录，`<YUAN_IMAGE_ROOT>` 替换为当前项目注册的 `.agents\skills\yuan-image-generation`，`<WATERMARK_ROOT>` 替换为 `yuanyuan-ruyi-watermark` 技能目录。先读取 `references/white-background-prompt.md`，再执行本技能脚本。

## 输入与产物

- `front_image`：必填，唯一的整体结构参考。
- `detail_images`：可选，最多 4 张，仅补充局部材质或侧面细节。
- `product_parameters`：可选，仅供人工整理参考计划，不直接写入模型提示词。
- `reference_plan.json`：事实源，包含结构、特殊件、带 `source_image` 的材质观察、构图阈值和人工复核项。
- `output_root`：独立运行目录，至少包含 `source`、`detail`、`preprocessed`、`generated`、`white-bg`、`logs`、`qc`、`manifests`。

每次运行保留 `reference_plan.json`、最终 `prompt.txt`、Yuan v4 响应、两份 QC JSON 和 `audit.json`。重跑必须新建尝试目录，不覆盖历史证据。

## 参考计划

先由人工根据实物图填写事实，再执行校验。特殊件只填写能在正面图中逐项核对的实物，不使用泛化配件词库替代事实。

材质观察必须标明 `source_image`，且路径必须等于正面图或已声明的细节图。材质观察只描述局部可见的底色、通透程度、内含物、纹理走向、密度和明暗关系，不从材料名称推导星芒、亮片、光晕或其他不可见效果。

```json
{
  "schema_version": "2.0",
  "product_id": "SY1462",
  "front_image": "source/SY1462_front.jpg",
  "detail_images": ["detail/SY1462_detail_01.jpg"],
  "structure": {
    "bead_sequence": "严格以正面图中可见的珠子数量、颜色顺序和相对位置为准。",
    "thread": "保留正面图可见的透明串线。",
    "special_components": [
      {
        "name": "合金牛造型珠",
        "location": "正面图左侧中部",
        "visual_description": "金色金属造型、雕刻轮廓与中心深色圆形镶嵌以正面图为准。"
      }
    ]
  },
  "material_observations": [
    {
      "subject": "黄塔晶、绿幽灵和闪灵钻珠",
      "source_image": "detail/SY1462_detail_01.jpg",
      "description": "分别以参考图中对应珠子的局部可见纹理为准，逐颗保留底色、通透程度、内含物形态、纹理走向与密度，以及原图可见的明暗关系。"
    }
  ],
  "composition": {
    "width_ratio_min": 0.45,
    "width_ratio_max": 0.55,
    "max_center_offset_ratio": 0.08,
    "require_full_product": true
  },
  "manual_review_items": ["逐颗核对结构与材质", "确认水印不遮挡产品"]
}
```

## 执行流程

### 1. 建立运行目录并预处理参考图

正面图始终排第 1 张，每款最多提交 5 张参考图。使用 `scripts/preprocess_reference_images.py` 预处理本地图片，只有 manifest 中 `status=ok` 的文件可以继续。

### 2. 校验参考计划

```powershell
& "<python>" "<SKILL_ROOT>\scripts\validate_reference_plan.py" `
  --reference-plan "<output_root>\reference_plan.json" `
  --check-files
```

任何校验失败都先修正计划或图片，不提交生图。

### 3. 构建固定提示词

使用 `scripts/build_white_background_prompt.py` 生成模型专用 prompt。不得手写、改造或替换模型提示词；纠偏只能通过修改已校验的 `reference_plan.json` 后重新构建。

构建结果必须且只能包含七个固定标题：`【任务目标】、【参考图职责】、【产品结构】、【材质细节】、【背景、光线与阴影】、【构图】、【画面范围】`。

```powershell
& "<python>" "<SKILL_ROOT>\scripts\build_white_background_prompt.py" `
  --reference-plan "<output_root>\reference_plan.json" `
  --output "<output_root>\logs\<product_id>_prompt.txt"
```

### 4. 使用 Yuan Image Generation v4 生成

画幅和分辨率仅在这一步传参。固定使用 Wawapi Generation；先传正面图，再按重要性传最多 4 张细节图。

```powershell
$prompt = Get-Content -Raw -Encoding utf8 "<output_root>\logs\<product_id>_prompt.txt"
& "<python>" "<YUAN_IMAGE_ROOT>\scripts\yuan_image_helper.py" generate `
  --provider "wawapi" `
  --operation "generation" `
  --prompt $prompt `
  --aspect-ratio "3:4" `
  --resolution "2K" `
  --output-dir "<output_root>\generated" `
  --image "<output_root>\preprocessed\<front>.jpg" `
  --image "<output_root>\preprocessed\<detail>.jpg"
```

读取 helper JSON 中的 `output[0].path`，保存为本款未加水印生成图，并完整保留响应日志。基础生成器不打开图片、不选图、不做内容或尺寸 QC，也不因返回像素与请求目标不同而自动重跑。

### 5. 水印前 QC

自动检查产品宽度占比、水平居中偏差、边缘裁切风险和前景置信度；结构、逐颗材质和特殊件由人工核验。

```powershell
& "<python>" "<SKILL_ROOT>\scripts\evaluate_white_background.py" `
  --image "<output_root>\generated\<product_id>.png" `
  --reference-plan "<output_root>\reference_plan.json" `
  --output "<output_root>\qc\pre_watermark_qc.json"
```

- `fail`：停止交付。修订参考计划后最多重新生成 1 次。
- `review`：停止自动交付，完成人工复核后再决定。
- `pass`：仍需完成结构、材质和特殊件人工复核。

浅色或透明前景边缘存在歧义时必须进入人工复核。不得自动裁切、缩放或重排图片来绕过 QC。

### 6. 添加 Yuan Studio 水印

仅水印前 QC 和人工复核允许继续时，调用水印技能生成副本；不得覆盖未加水印原图。

```powershell
& "<python>" "<WATERMARK_ROOT>\scripts\watermark_images.py" `
  --input "<output_root>\generated\<product_id>.png" `
  --output-dir "<output_root>\white-bg" `
  --product-id "<product_id>"
```

水印只包含 `YUAN STUDIO` 和 `PN <product_id>`。

### 7. 水印后 QC 与审计

```powershell
& "<python>" "<SKILL_ROOT>\scripts\evaluate_white_background.py" `
  --image "<output_root>\white-bg\<product_id>_watermarked.png" `
  --reference-plan "<output_root>\reference_plan.json" `
  --stage post-watermark `
  --ignore-bottom-ratio 0.15 `
  --output "<output_root>\qc\post_watermark_qc.json" `
  --pre-watermark-qc "<output_root>\qc\pre_watermark_qc.json" `
  --prompt-version "v2.3-yuan-v4" `
  --upload-decision "not_authorized" `
  --audit-output "<output_root>\manifests\audit.json"
```

水印后检查忽略底部水印区域对前景分割的干扰，但不改变产品像素。

## 上传门禁

`audit.json` 至少包含 `prompt_version`、`reference_plan_path`、`pre_watermark_qc`、`post_watermark_qc` 和 `upload_decision`。

人工复核记录：

```json
{
  "status": "approved",
  "reviewer": "质检人员姓名或标识",
  "checked_items": ["结构、材质、特殊件和水印均已人工复核"]
}
```

用户授权记录：

```json
{
  "status": "approved",
  "approved_by": "用户",
  "approval_reference": "对应的明确主图追加授权"
}
```

- 本地生成、校验和水印不代表获得写回授权。
- 只有两阶段 QC 可继续、人工复核为 `approved`，且用户明确授权追加主图时，才能上传。
- 上传只追加，不替换已有附件；上传后立即回读，确认新文件存在且原附件全部保留。
- 自动 QC 为 `review` 或 `fail` 时，上传决策分别为 `requires_human_review` 或 `blocked_by_qc`，不得绕过。

## 常见判断

| 情况 | 正确处理 |
| --- | --- |
| 细节图与正面图冲突 | 正面图决定结构，细节图只补材质。 |
| 产品占比不在目标区间 | 修订参考计划后重新生成或人工复核，不自动裁图。 |
| 同一款首次生成未通过 | 保留证据，最多修订计划后再生成 1 次。 |
| 浅色透明珠前景置信度低 | 人工核对边缘、折射和接触阴影。 |
| 水印前通过但水印后异常 | 保留原图，修复水印流程后只重跑水印和交付 QC。 |
| 需要追加到主图 | 先确认授权，再串行上传并回读验证。 |
