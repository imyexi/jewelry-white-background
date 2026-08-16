---
name: jewelry-white-background
description: 在需要为手串、吊坠、项链、链绳及其他珠宝制作浅白背景商品图，或继续、确认、恢复和交付既有珠宝白底图运行时使用。
---

# 珠宝白底图生成

## 唯一正式流程

新运行只有这一条顺序：

检测增强 → `vision-geometry-mask-2.1` → 确定性裁剪 → 三路 Mask → Mask Hook → Wawapi → 确定性排版 → 最终 Hook → 水印 → 飞书追加

任何阶段失败、身份变化、人工确认缺失、外部调用结果不确定或 claim 正在被其他进程持有时立即停止。不得跳步，不得从其他 run 或 SKU 扫描输入，不得用旧入口维护第二套状态机。

## 输入边界

- 只使用首张正面图；准备阶段一次性保存 `file_token`、文件名、`record_id`、`target_field_id` 和源文件身份，后续不重新解释远端字段。
- 产品名称、产品参数、生肖或材料名称不得进入 Prompt。
- 每张图片单独处理；不要一次向视觉模型提交大量图片。
- 所有图片和中间大图不得超过 20,000,000 像素。
- Prompt 权威来源固定为 `references/white-background-prompt.md` 和 `scripts/build_white_background_prompt.py`。

## 1. 创建唯一运行

使用 `scripts/run_white_background_workflow.py create` 创建 `<output_root>/<product-id>/<run-id>/`。运行身份写入 `manifests/workflow_state.json`，运行锁为 `manifests/workflow.lock`。目录和业务资产禁止覆盖或复用。

源图规范化为 RGB PNG；`source_sha256` 表示规范化 PNG 摘要，原始附件字节摘要单独记录为 `raw_source_sha256`。

## 2. 检测增强

调用 `scripts/prepare_mask_detection_images.py` 生成全局色阶图和局部对比度图。全局色阶图是唯一允许单图提交给云端视觉模型的图片，用于生成 `vision-geometry-mask-2.1`；局部对比度图只供本地 Mask 边缘贴合。两张图都必须绑定路径、尺寸、模式和 SHA-256，并与规范化原图一起保留为三路 Mask 证据。

Wawapi 不接收检测增强图、边缘副本或几何 JSON，只接收裁剪后的未增强原图、最终保护 Mask 和固定 Prompt。

## 3. 几何 2.1

新运行只接受 `vision-geometry-mask-2.1`。几何必须绑定商品、规范化源图摘要、尺寸、坐标空间、受支持 producer、非退化图元、真实触边声明和 `uncertain_regions`。

允许 `polygon`、`polyline`、`ellipse`、`ellipse_set`。保护所有可见商品结构及透明实体内部，但不保护大面积投影、白色垫板、桌面或道具。

## 4. 确定性裁剪

调用 `scripts/create_geometry_crop.py`。裁剪由几何和固定算法决定，发布：

- `cropped_original`
- `cropped_detection`
- `cropped_local_detection`
- `candidate_alpha`
- 裁剪后几何与 Crop Manifest

裁剪先于 Mask；不得把全尺寸旧资产直接交给新 Mask 流程。

## 5. 三路 Mask 与 Mask Hook

调用 `scripts/create_background_edit_mask.py`，以 `cropped_original`、`cropped_detection`、`cropped_local_detection` 和 `candidate_alpha` 生成 RGBA 产品保护 Mask、可编辑区叠加图和 draft assessment。

Mask 的 RGB 固定为 255；`alpha=255` 表示产品保护，`alpha=0` 表示背景可编辑。技术门禁与人工结论分离：

1. `workflow_confirmations.py` 创建 `manifests/mask_review_bundle.json`，绑定源图、检测、几何、裁剪、Mask、叠加图和报告。
2. 在 Mask Hook 核对商品完整性、边缘、透明区域、触边声明和全部疑点。
3. 使用 `confirm-mask` 创建不可变 `confirmations/mask_confirmation.json`。
4. 确认前不得继续；任一绑定资产变化都使确认失效。

人工结论不得写回几何 profile，也不得靠修改技术报告放行。

## 6. Wawapi

确认有效后，由 `scripts/run_wawapi_edit_with_retry.py` 调用无内部重试的 single-attempt adapter。每个运行最多 3 个 call slot；明确 429 只有在响应不含 task/job ID 或可用成功结果、两者均不存在时才可重试。5xx 只有在不含 task/job ID 且带正式 `request_not_accepted` 证据时才可重试；下一 slot 分别等待 30 秒、90 秒。

普通 5xx、超时、断连、远端 ID、遗留不明 claim 或无法证明请求未被接受的情况进入 `edit_unknown`，禁止自动再请求。

结果只接受第一张完整可解码、格式与后缀一致、至少 16×16 且不超过 20MP 的本地候选，并发布 Edit Result Manifest。

## 7. 确定性排版

调用 `scripts/layout_generated_result.py`。它只读取 Wawapi 生成结果，不读取原图、几何、裁剪或 Mask，不执行生成后结果 QC。

固定算法完成颜色归一化、背景估计、前景框检测、922 目标宽和 1800 高度上限排版，输出 PNG 与 Layout Manifest。输入触边、前景低置信度或中间图超过 20MP 时停止。

## 8. 最终 Hook

`workflow_confirmations.py` 创建 `manifests/final_review_bundle.json`，绑定 Mask 确认、确认门禁、Edit Result Manifest、排版 PNG 及 Layout Manifest。

最终 Hook 通过后，`confirm-final` 创建不可变 `confirmations/final_confirmation.json`，生成唯一 `delivery_id`，并固定授权：

```json
{
  "watermark": true,
  "feishu_append": true
}
```

确认前不得继续。任一身份、摘要、尺寸、bundle 或 manifest 变化都进入 `final_invalid`。这里的一次确认同时授权同一 `delivery_id` 的水印和飞书追加，不再设置第三个人工 Hook。

## 9. 水印

`deliver_confirmed_result.py` 先创建 `manifests/watermark_receipt.<delivery-id>.json` claim，再调用水印技能的正式单文件模式：

```powershell
python watermark_images.py `
  --input "<confirmed-layout>" `
  --output "<exact-watermarked-path>" `
  --product-id "<product-id>" `
  --workers 1
```

固定文件名为 `<product-id>__<run-id>__<delivery-id>__watermarked.png`。水印后只检查精确路径、普通文件、非空和 SHA-256，不用 Pillow 再解码。已有 claim 时只恢复，不重复调用；磁盘文件必须与 completed claim 的路径、字节数和摘要完全一致。

## 10. 飞书追加

先创建 `manifests/upload_receipt.<delivery-id>.json` claim，然后且仅调用一次：

```text
lark-cli base +record-upload-attachment
  --base-token <base-token>
  --table-id <table-id>
  --record-id <record-id>
  --field-id <target-field-id>
  --file <exact-watermarked-path>
  --format json
  --as user
```

响应先完整写成 `response_recorded`，再从 `data.attachments[record_id][target_field_id]` 按完整文件名筛选。恰好一项且 `file_token` 非空才是 `completed`；明确结构化失败为 `upload_failed`；无 JSON、截断、零匹配、多匹配、空 token、超时或不明确的非零退出为 `upload_unknown`。

`failed`、`unknown`、遗留上传 claim 和已消费的 `delivery_id` 均不得自动重传。响应落盘并写入终态后直接结束，不再读取远端字段。

## 编排与恢复

正式 CLI 只有：

- `create`
- `confirm-mask`
- `resume`
- `confirm-final`
- `deliver`

每次 `resume` 只执行当前状态允许的一步。遇到两个 Hook、终态、active claim、`edit_unknown` 或 `upload_unknown` 原样返回。

`run_jewelry_base_pipeline.py` 与 `scripts/run_sy1537_sy1552_white_background.py` 只委托正式编排器。旧 `--retry`、旧授权参数、跳步参数和结果版本参数无法安全映射，必须明确报错。

## 快速门禁

| 阶段 | 继续条件 |
| --- | --- |
| 源图 | 身份固定、规范化 PNG 有效 |
| 几何 | 2.1 schema、图元和触边声明有效 |
| 裁剪 | Crop Manifest 与全部输出摘要一致 |
| Mask | 技术门禁可从发布资产重现 |
| Mask Hook | bundle 和不可变确认有效 |
| Edit | call slot 结果明确且 manifest 有效 |
| 排版 | 只消费生成结果并成功发布 |
| 最终 Hook | bundle、确认和 `delivery_id` 有效 |
| 水印 | claim 与精确文件身份一致 |
| 飞书 | 唯一响应项和非空 token |

## 历史兼容边界

2.0 仅允许历史资产只读检查，不得进入新运行、不得升级后复用确认，也不得绕过裁剪和两个 Hook。
