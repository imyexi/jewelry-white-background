---
name: jewelry-white-background
description: 在需要根据珠宝手串正面图和细节图生成白底商品图、添加 Yuan Studio 水印，并要求控制结构漂移、材质失真、构图占比或飞书追加前质检时使用。
---

# 珠宝白底图生成

## 核心原则

使用 V2 的“参考计划 -> 提示词 -> 双阶段质检 -> 写回门禁”流程。正面图只决定产品结构；细节图只补局部材质；画幅与分辨率只作为 AIReiter 参数；未经水印前后质检和明确授权的图片不得追加到飞书主图。

将 `<SKILL_ROOT>` 替换为本技能目录，`<AIREITER_ROOT>` 替换为 `aireiter-image-generation` 技能目录，`<WATERMARK_ROOT>` 替换为 `yuanyuan-ruyi-watermark` 技能目录。先读取 `references/white-background-prompt.md`，再执行本技能脚本。

## 输入与产物

- `front_image`：必填，唯一的整体结构参考。
- `detail_images`：可选，最多 4 张，仅补局部材质。
- `product_parameters`：可选，仅供人工整理参考计划；不直接送入模型提示词。
- `reference_plan.json`：V2 必填事实源，包含结构、特殊件、带 `source_image` 的材质观察、构图阈值和人工复核项。
- `output_root`：运行目录，至少包含 `source`、`detail`、`preprocessed`、`generated`、`white-bg`、`logs`、`qc`、`manifests`。

每次运行保留 `reference_plan.json`、最终 `prompt.txt`、AIReiter 响应、两份 QC JSON 和 `audit.json`；重跑必须新建运行目录，不覆盖历史记录。

## 参考计划

先由人工根据实物图填写事实，再校验。特殊件只填写可以在正面图中逐项核对的实物；不要用泛化配件词库替代事实。材质观察必须标明 `source_image`，且该路径必须等于正面图或已声明的一张细节图；它只用于追溯材质依据，不会写入模型提示词。材质观察只描述局部可见的底色、通透程度、内含物、纹理走向、密度和明暗关系，不从材料名称推导视觉特效。

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
        "visual_description": "金色金属造型、雕刻轮廓与中心深色圆形镶嵌以参考图为准。"
      }
    ]
  },
  "material_observations": [
    {
      "subject": "黄塔晶、绿幽灵和闪灵钻珠",
      "source_image": "detail/SY1462_detail_01.jpg",
      "description": "分别以参考图中对应珠子的局部可见纹理为准，逐颗保留其底色、通透程度、内含物形态、纹理走向与密度，以及原图可见的明暗关系。"
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

1. 建立独立运行目录并将正面图、细节图放入相应子目录。使用 `scripts/preprocess_reference_images.py` 预处理所有本地图片，正面图始终排第一。只允许 `manifest.jsonl` 中 `status=ok` 的文件继续。
2. 校验参考计划和本地图片路径。任何校验失败都先修正计划或图片，不提交生图。

```powershell
& "<python>" "<SKILL_ROOT>\scripts\validate_reference_plan.py" `
  --reference-plan "<output_root>\reference_plan.json" `
  --check-files
```

3. 使用 `scripts/build_white_background_prompt.py` 生成模型专用 prompt。不得手写、改造或替换模型提示词；即使为了纠偏，也只能修改通过校验的 `reference_plan.json` 后重新运行构建脚本。构建结果必须且只能包含七个固定标题：`【任务目标】、【参考图职责】、【产品结构】、【材质细节】、【背景、光线与阴影】、【构图】、【画面范围】`。不要把产品参数、选择理由、任务路径、QC 原文或历史审计拼进 prompt。

```powershell
& "<python>" "<SKILL_ROOT>\scripts\build_white_background_prompt.py" `
  --reference-plan "<output_root>\reference_plan.json" `
  --output "<output_root>\logs\<product_id>_prompt.txt"
```

4. 调用 `aireiter-image-generation`。画幅和分辨率仅在这一步传参，prompt 文件中不重复。先传正面图，再按重要性传细节图。

```powershell
$prompt = Get-Content -Raw -Encoding utf8 "<output_root>\logs\<product_id>_prompt.txt"
& "<python>" "<AIREITER_ROOT>\scripts\aireiter_image_helper.py" submit `
  --prompt $prompt `
  --aspect-ratio "3:4" `
  --resolution "2K" `
  --image "<output_root>\preprocessed\<front>.jpg" `
  --image "<output_root>\preprocessed\<detail>.jpg"
```

记录 `out_task_id`、轮询完成、下载原始生成图到 `generated`。AIReiter 失败时遵循 `aireiter-image-generation` 的兜底规则；不要把失败审计原文交给模型。

5. 先检查未加水印图。自动门禁检查宽度占比、居中、裁切风险和前景置信度；结构、逐颗材质和特殊件由人工核验。`fail` 时脚本以退出码 `4` 停止；`review` 时以退出码 `3` 停止，完成可审计的人工复核后才能继续。浅色或透明前景的边缘存在歧义时必须进入 `review`，不能因深色部分正常而自动通过。不要自动裁切、缩放或重排图片。

```powershell
& "<python>" "<SKILL_ROOT>\scripts\evaluate_white_background.py" `
  --image "<output_root>\generated\<product_id>.png" `
  --reference-plan "<output_root>\reference_plan.json" `
  --output "<output_root>\qc\pre_watermark_qc.json"
```

6. 仅在水印前 QC 可继续时，调用 `yuanyuan-ruyi-watermark` 生成副本；该脚本不会覆盖原图。

```powershell
& "<python>" "<WATERMARK_ROOT>\scripts\watermark_images.py" `
  --input "<output_root>\generated\<product_id>.png" `
  --output-dir "<output_root>\white-bg" `
  --product-id "<product_id>"
```

7. 对加水印图再次检查。忽略底部水印区域，但不改变产品像素；同时写入包含上传决策的审计文件。

```powershell
& "<python>" "<SKILL_ROOT>\scripts\evaluate_white_background.py" `
  --image "<output_root>\white-bg\<product_id>_watermarked.png" `
  --reference-plan "<output_root>\reference_plan.json" `
  --stage post-watermark `
  --ignore-bottom-ratio 0.15 `
  --output "<output_root>\qc\post_watermark_qc.json" `
  --pre-watermark-qc "<output_root>\qc\pre_watermark_qc.json" `
  --prompt-version "v2.2" `
  --upload-decision "not_authorized" `
  --audit-output "<output_root>\manifests\audit.json"
```

## 上传门禁

`audit.json` 必须包含 `prompt_version`、`reference_plan_path`、`pre_watermark_qc`、`post_watermark_qc` 和 `upload_decision`。`upload_decision` 由水印前后 QC、人工复核记录和用户授权记录共同推导；传入的决策值与门禁结果不一致时，脚本拒绝生成审计文件。

人工复核记录与用户授权记录必须分别保存为 JSON，字段最小形状如下；它们是申请追加资格的证据，不会自行触发飞书上传。

```json
{
  "status": "approved",
  "reviewer": "质检人员姓名或标识",
  "checked_items": ["结构、材质和水印均已人工复核"]
}
```

```json
{
  "status": "approved",
  "approved_by": "用户",
  "approval_reference": "对应的明确追加授权"
}
```

需要生成“可追加”审计记录时，在水印后 QC 命令中额外传入 `--manual-review-json`、`--append-authorization-json` 和 `--upload-decision "approved_for_append"`；缺少任一证据或任一 QC 未通过时，命令会拒绝该决策。

- 本地生成、校验和水印不授权飞书写回。
- 只有水印前后均未失败、人工复核记录为 `approved`，且用户单独明确要求“追加到主图”并写入授权记录后，审计才可能为 `approved_for_append`。
- 上传使用追加，不替换既有附件；上传后立即回读该记录，确认新文件存在且原附件仍保留。
- 自动 QC 为 `review` 或 `fail` 时，上传决策分别为 `requires_human_review` 或 `blocked_by_qc`，不得绕过；未授权的本地运行也绝不写回飞书。

## 常见判断

| 情况 | 正确处理 |
| --- | --- |
| 细节图与正面图冲突 | 正面图决定结构，细节图只补材质。 |
| 产品占比不在目标区间 | 重生或人工复核，不自动裁图。 |
| 同一来源连续两次构图超界 | 停止单纯追加提示词；保留 QC 证据，并请求是否启用布局引导参考图或人工确定版式。 |
| 浅色透明珠前景置信度低 | 标记人工复核，核对边缘、折射和接触阴影。 |
| 水印前 QC 通过但水印后异常 | 保留原图，修复水印后只重跑水印和交付 QC。 |
| 需要追加到飞书主图 | 先取得单独授权，再串行上传并读取验证。 |
