---
name: jewelry-white-background
description: 在需要为手串、吊坠、项链、链绳及其他珠宝制作浅白背景商品交付图，或在获得授权后更新飞书主图字段时使用。
---

# 珠宝白底图生成

## 唯一正式流程

唯一正式流程：读取飞书最新首张正面图并保存附件身份，逐图取得云端视觉几何，在本地生成同尺寸 RGB PNG 与 RGBA 产品保护 Mask，通过技术门禁后使用 `operation=edit` 提交，保存第一张有效本地结果，添加 Yuan Studio 水印，并以 `status=awaiting_authorization` 固化结果身份；之后才接受用户明确授权。交付先写版本化 `status=uploading` 意图回执，再调用一次附件上传；响应 `ok: true` 时从内存响应中按目标文件名唯一提取 `file_token`，写入本地回执并立即终止。

这是一条只编辑背景的流水线，不创建新产品。产品像素由 Mask 保护，Wawapi 只填充背景。手串、吊坠、项链、链绳及其他非圆环结构使用同一正式流程；任何步骤不满足门禁时停止，不得回退到环形推断、全图纯边缘识别、Generation 或其他生成路径。不存在无视觉几何的正式 fallback。

将 `<SKILL_ROOT>` 替换为本技能目录，将 `<YUAN_IMAGE_ROOT>` 替换为当前项目注册的 `yuan-image-generation` 技能目录，将 `<WATERMARK_ROOT>` 替换为 `yuanyuan-ruyi-watermark` 技能目录。

## 输入与运行产物

- 输入只取目标记录正面图字段中的最新首张图片。只使用首张正面图，不读取或提交其他图片。
- 每张图片单独调用云端视觉，禁止一次提交大量图片；视觉模型只返回几何 JSON，不生成 Mask。
- 读取阶段把当时当前首张正面图的 `file_token` 与附件名称分别保存为 `front_file_token` 和 `front_file_name`；它们与 `record_id` 一起写入本地 `product_context.json`，后续生成和交付不得改绑。
- 每次运行使用独立的 `<output_root>`，保留源图、视觉几何、Edit 原图、最终 Mask、叠加图、技术报告、Prompt、接口响应、未加水印结果、水印结果、生成 state 与版本化上传回执。
- 不覆盖历史运行目录。产品名称、产品参数、生肖或材料名称不得进入 Prompt。

## 1. 获取唯一源图

从目标飞书记录的正面图字段读取最新值，只下载数组中的第一张图片到 `<output_root>\source`。下载前把 `workflow_mode`、`product_id`、`record_id`、`front_file_token`、`front_file_name` 和目标 `field_id` 写入本地 `product_context.json`。下载完成后不再读取同一字段，也不枚举记录中的其他附件；后续所说的“当前首张正面图”均指该准备阶段保存的附件身份，不允许在交付阶段通过远端回读重新解释。

源文件必须可由 Pillow 解码，宽高均为正整数，且不超过资产脚本允许的像素上限。源文件无效时停止本次运行。

## 2. 逐图生成视觉几何

对唯一源图单独执行云端视觉识别，输出 `vision-geometry-mask-2.0` JSON。几何必须绑定 `product_id`、`source_sha256`、`source_size`、`coordinate_range=[0,10000]` 和受支持的云端视觉 `producer`；缺失、空值或本地轮廓算法来源直接终止。所有 primitive ID（含 `ellipse_set.items[].id`）在图元命名空间内唯一；polygon 必须具有至少三个不同点和非零面积，polyline 必须具有非零长度；每个 `uncertain_regions[].id` 在未确定区域数组内唯一。

允许的图元为 `polygon`、`polyline`、`ellipse`、`ellipse_set`。几何应覆盖正面图中所有可见珠子、吊坠、链绳、串线、吊环、连接件和金属件；透明商品保护完整实体包络，包括透出背景的内部区域。识别优先保证商品召回，可保留小幅安全边界，但不保护大面积投影，不保护白色垫板，也不保护桌面、道具或其他背景结构。

真实商品被画布裁切时，几何中必须做真实商品声明，明确相应组件触边并保持连续。无法确认的浅色绳、透明边缘、遮挡连接处或触边位置写入 `uncertain_regions`，不得靠环形假设补齐。每项必须包含非空字符串 `id`、归一化坐标 `bbox=[left,top,right,bottom]` 和非空 `reason`；四个坐标均为 `0..10000` 整数，且边界具有正面积：

```json
{
  "uncertain_regions": [
    {
      "id": "cord-border",
      "bbox": [4800, 0, 5200, 400],
      "reason": "细绳在顶边真实裁切"
    }
  ]
}
```

`bbox` 和 `reason` 只用于定位与审计，不会直接修改 Alpha。首次几何草稿不要伪造审核；先生成 `review` 资产并核对最终 Mask、叠加图和报告。确认后在同一 profile 中加入合法 `mask_review`，再用同一资产命令重跑：

```json
{
  "mask_review": {
    "status": "approved",
    "reviewer": "圆圆",
    "source_sha256": "与 profile 完全一致的 64 位大写 SHA-256",
    "geometry_sha256": "草稿报告中的规范化几何 SHA-256",
    "mask_sha256": "草稿报告中的最终 Mask PNG SHA-256",
    "checked_items": ["商品组件完整", "未保护垫板或投影"],
    "resolved_uncertain_regions": ["cord-border"]
  }
}
```

`resolved_uncertain_regions` 是对 `uncertain_regions[].id` 的无重复字符串引用数组，只记录审核结论，不修改 Mask。未知 ID 会使 profile 校验失败；遗漏任一已有 ID 会保留联网阻断。`geometry_sha256` 覆盖源图身份、producer、全部图元和完整 `uncertain_regions` 定义，但排除 `mask_review` 与非语义路径；`mask_sha256` 绑定草稿最终 RGBA Mask。任一摘要变化都使旧审核变为 `stale`，必须重新核对并填写新摘要。

## 3. 生成可审计 Edit 资产

使用 `<SKILL_ROOT>\scripts\create_background_edit_mask.py` 的 `create_background_edit_assets` 能力，提供源图、产品 ID 与视觉几何文件，生成相互隔离的 Edit 资产。该脚本内部固定调用 `<SKILL_ROOT>\scripts\prepare_mask_detection_images.py` 生成 Mask 检测版本，无需临时编写或拼装色阶脚本。无审核草稿先生成 `review` 资产；写入逐项审核后必须再次运行同一命令，重新执行全部技术门禁：

```powershell
& "<python>" "<SKILL_ROOT>\scripts\create_background_edit_mask.py" `
  --input "<output_root>\source\<front-file>" `
  --geometry-profile "<output_root>\geometry\<product-id>.json" `
  --product-id "<product-id>" `
  --image-output "<output_root>\prepared\<product-id>_front.png" `
  --mask-output "<output_root>\mask\<product-id>_product-protection-mask.png" `
  --overlay "<output_root>\mask\<product-id>_editable-overlay.png" `
  --report "<output_root>\logs\<product-id>_mask-assessment.json"
```

资产契约：

- 先校验几何 schema、受支持的云端视觉 producer、`source_sha256`、尺寸、坐标范围、非退化图元、`uncertain_regions` 和 `mask_review`；任何不匹配都在联网前直接终止。
- `prepared/<product-id>_front.png` 必须是与 EXIF 校正后源图同宽、同高的 RGB PNG；除 Alpha 白底合成外不改变原图像素，不裁切、不扩画布、不重新排版。
- 固定检测工具生成的自适应色阶和局部对比度版本只用于生成 Mask；正常流程由 Mask 脚本自动调用，不向运行时开放算法参数，也不得覆盖或替换 `prepared` 原图。
- Mask 必须是同尺寸 RGBA PNG；`alpha=0` 表示背景可编辑，`alpha=255` 表示产品保护；所有 RGB 通道固定为 255，包括完全透明像素。
- 红色可编辑区叠加图只用于人工确认透明编辑区，不能作为模型输入。
- JSON 技术报告记录 `product_id/source_sha256/geometry_sha256/prepared_sha256/mask_sha256`、几何图元、色阶检测、边缘贴合、触边声明、保护占比、背景可编辑面积、`mask_review_status`、`unresolved_uncertain_region_ids`、`automatic_wawapi_edit_allowed`、状态和原因。

## 4. 本地边缘贴合与联网前门禁

本地边缘贴合由 `create_background_edit_mask.py` 调用固定检测工具完成，仅在视觉候选边界窄带内使用检测结果。检测版本只用于生成 Mask，不得从整张图重新猜测商品；边缘证据分歧、动态范围不足或贴合越界时保留视觉候选，并保持 `review`。

评估报告当前只产生 `ok` 或 `review`。输入、schema 或身份校验失败会直接终止，不生成可放行报告；`fail` 仅为脚本保留的退出码，不是当前评估分支。

先同时检查最终 Mask、红色可编辑区叠加图和 JSON 技术报告：

- 所有可见商品组件必须获得视觉几何或跨版本边缘证据，透明商品的内部区域也必须完整保护。
- 小幅安全外扩可以保留；大面积柔和投影、白色垫板、桌面和道具必须处于可编辑背景中。
- 视觉几何具有明确且连续的真实商品声明时，允许相应图元覆盖的边框像素触边；最终 Mask 中任何未被 `touches_border=true` 图元逐像素覆盖的边框保护仍须保持 `review`。
- 吊坠、项链内圈或其他非圆环商品可以覆盖画面中心，中心不再作为通用可编辑门禁。最终保护占比必须位于 `0.5%..70%`；低于下限视为退化或近空 Mask，高于上限视为背景大面积误保护，两者都保持 `review`。
- 每个 `uncertain_regions[].id` 都必须由同一源图的合法 approved `mask_review` 在 `resolved_uncertain_regions` 中逐项列出；缺失任一 ID 均保持 `review`。审核只消除相应未确定项，不能覆盖边缘贴合、不变量、触边或保护占比等其他技术原因。
- 审核同时绑定 `source_sha256`、规范化 `geometry_sha256` 和最终 `mask_sha256`；审核、几何、疑点定义或最终 Mask 更新后都必须重跑技术门禁，不得复用旧报告或仅凭肉眼放行。
- 只有 `status=ok` 且 `automatic_wawapi_edit_allowed=true` 时才允许联网。任一条件不满足都不得调用 Wawapi。

正式流程不得回退到环形推断，也不得使用椭圆环带或纯边缘全图识别作为替代。视觉几何缺失、身份不匹配、主要商品漏保护、垫板或投影大面积误保护时停止。

## 5. 构建固定 Prompt

先读取 `references/white-background-prompt.md`。模型 Prompt 的唯一权威实现是 `<SKILL_ROOT>\scripts\build_white_background_prompt.py`；不得手写、改造或替换模型提示词。

```powershell
& "<python>" "<SKILL_ROOT>\scripts\build_white_background_prompt.py" `
  --reference-plan "<output_root>\reference_plan.json" `
  --output "<output_root>\logs\<product-id>_prompt.txt"
```

生成后的 Prompt 必须与参考文档中的唯一 `text` 代码块逐字一致。计划文件只用于脚本校验，不得把商品语义拼入 Prompt，色阶参数不得进入 Prompt。

## 6. 单图单 Mask 编辑

仅在门禁报告同时满足 `status=ok` 且 `automatic_wawapi_edit_allowed=true`，并且联网前重算的 prepared/Mask 文件 SHA-256 仍与报告摘要一致后，使用 `yuan-image-generation` 的 `yuan_image_helper.py` 发起一次 Wawapi Edit。请求必须是单 image、单 mask，且两者尺寸相同：

```powershell
$prompt = Get-Content -Raw -Encoding utf8 "<output_root>\logs\<product-id>_prompt.txt"
& "<python>" "<YUAN_IMAGE_ROOT>\scripts\yuan_image_helper.py" generate `
  --provider "wawapi" `
  --operation "edit" `
  --prompt $prompt `
  --output-dir "<output_root>\edit" `
  --image "<output_root>\prepared\<product-id>_front.png" `
  --mask "<output_root>\mask\<product-id>_product-protection-mask.png"
```

Wawapi 不接收色阶增强图、边缘检测副本或视觉几何 JSON，只接收未经色阶处理的 `prepared` 原图、最终 Mask 和固定 Prompt。请求中不得出现检测参数、色阶参数、其他图片、比例或分辨率参数。

保留完整响应日志，按响应顺序接受第一张能够解码且已实际写入本地的图片；无效候选只跳过，不用于选图。若请求失败、所有候选均无效或结果为空，则停止并报告失败：不自动切换 provider，不自动更换 operation，不修改 Prompt 重跑。

不对 Edit 结果执行内容、构图、占比或尺寸 QC。不得根据模型结果评价、挑选或重新创建产品。

生成阶段不接受或消费任何上传授权。第一张有效结果写盘并完成水印后，生成 state 固定为 `status=awaiting_authorization`，至少绑定 `workflow_mode`、`product_id`、`record_id`、`front_file_token`、`front_file_name`、五项 Mask 身份摘要、`generated_image_sha256`、水印文件绝对路径及其摘要、`target_filename` 与 `field_id`。没有该 state 时只能生成并等待授权，不能把命令行参数、历史对话或批次授权当作未来结果的写回权限。

## 7. 添加水印

将第一张有效结果作为未加水印原件保留，调用水印技能生成新文件。调用前确定唯一目标绝对路径，调用后只校验该路径，不扫描输出目录选择文件：

```powershell
& "<python>" "<WATERMARK_ROOT>\scripts\watermark_images.py" `
  --input "<output_root>\edit\<result-file>" `
  --output-dir "<output_root>\watermarked" `
  --product-id "<product-id>"
```

水印内容只包含 `YUAN STUDIO` 与 `PN <product-id>`。不得覆盖未加水印原件。把解析后的水印绝对路径记录为 `local_file`，文件名记录为 `target_filename`，文件 SHA-256 记录为 `local_file_sha256`；用户看到并授权的是这一已存在的具体结果。

## 8. 授权后追加主图

本地完成图片和水印不等于取得写回权限。授权只能针对已存在的具体生成结果：只有生成 state 已是 `status=awaiting_authorization`，用户看过该结果并明确同意将它追加到目标记录主图字段后，才能进入交付。禁止预授权未来图片；授权不得使缺少 state 的产品现场生成并上传，也不得从一个商品、记录、附件、生成摘要或文件版本扩散到另一个对象。

上传契约：

- 主图字段只追加，不替换；保留字段中全部既有值。
- 不得删除任何旧 token，也不得调用附件删除或字段清空操作。
- 上传前不得为确认现状而再次读取主图字段。
- 交付只使用准备阶段保存的本地 `product_context.json`，并要求授权对象、context、state、Mask 报告、磁盘资产和回执中的完整交付身份完全相同。完整交付身份必须绑定：`workflow_mode`、`product_id`、`record_id`、`front_file_token`、`front_file_name`、五项 Mask 身份摘要（逐项为 `product_id`、`source_sha256`、`geometry_sha256`、`prepared_sha256`、`mask_sha256`）、`generated_image_sha256`、`local_file` 绝对路径、`local_file_sha256`、`target_filename` 与 `field_id`。
- 交付前重新计算 prepared、Mask、generated 与水印文件摘要；路径必须解析为 state 中记录的绝对路径。缺失、变化或身份不一致均停止，不重新生成、不重新加水印、不上传。
- 每个水印目标使用 `upload_receipt.<target-stem>.json`。调用上传接口前，先以原子写入方式创建或更新该版本化回执为 `status=uploading`，记录授权依据和完整交付身份。通用 `upload_receipt.json` 可以作为兼容索引，但不得作为防重依据，也不得覆盖版本化回执。
- 上传前先检查对应版本化回执：只有 `status=completed`、非空上传 token 且完整交付身份完全相同时，才本地返回已完成并阻止重放；`uploading`、`failed`、`unknown`、损坏回执或身份冲突都必须停止，禁止自动重传。
- 只允许一次附件上传调用。收到任意响应后，在做成功判断或 token 解析前，立即把原始响应持久化到同一版本化回执；若命令异常、无响应或 JSON 无法确定，同样写入错误证据并转为 `status=unknown`。
- 真实成功响应固定从 `data.attachments[record_id][field_id]` 取得候选数组，按 `target_filename` 唯一匹配附件并提取非空 `file_token`，不得取数组首项。`ok: true` 但零匹配、多匹配或 token 为空都属于 `status=unknown`，不能视为成功。
- `ok: true`、唯一目标文件匹配且 token 非空时写 `status=completed`；明确的非成功响应写 `status=failed`；响应或结果无法确定时写 `status=unknown`。三种终态都保留原始响应；`failed` 与 `unknown` 均禁止自动重传。
- 上传返回后仅解析内存中的上传响应并写入版本化回执。写入版本化回执后立即终止；不得上传后回读飞书，不得扫描附件、目录或图片，不得通过列表接口确认追加结果。

## 快速检查

| 阶段 | 继续条件 | 不满足时 |
| --- | --- | --- |
| 源图 | 最新值的第一张图片可解码 | 停止 |
| 视觉几何 | 单图识别，身份、schema、图元及触边声明有效 | 修订几何或停止 |
| Edit 资产 | `prepared` 原图未增强，Mask 尺寸、模式和 Alpha 语义正确 | 修正本地资产 |
| 联网门禁 | 商品完整保护，背景结构排除，`status=ok` 且 `automatic_wawapi_edit_allowed=true` | 不联网 |
| 模型请求 | 原图、单 Mask、固定 Prompt，不含检测副本 | 停止 |
| 本地交付 | 第一张结果有效且水印副本成功 | 停止 |
| 飞书追加 | 已有 `awaiting_authorization` state，结果级授权与完整交付身份一致 | 不上传 |
| 上传意图 | 版本化回执已先写 `uploading`，且无完成/不确定回执冲突 | 停止 |
| 上传响应 | 原始响应已先落盘；`ok: true` 且按目标文件名唯一提取 token | 写 `completed` 后终止 |

## 常见错误

| 错误 | 正确处理 |
| --- | --- |
| 一次把大量图片交给视觉模型 | 每张图片单独识别并分别保存几何 |
| 没有视觉几何时调用旧环带或全图边缘算法 | 停止；正式流程不存在无几何 fallback |
| 把色阶增强图保存为 Edit 底图或传给 Wawapi | 保持 `prepared` 原图不变，增强副本只服务本地 Mask |
| 把所有阴影、白垫板或桌面纳入保护区 | 只保护商品及小幅安全边界，修订几何后重跑门禁 |
| 因商品触边或覆盖中心一律失败 | 根据真实商品触边声明和组件连续性判断，不使用中心通用门禁 |
| `review` 后凭肉眼继续请求 | 修订几何，或核对后在 `mask_review.resolved_uncertain_regions` 逐项列出疑点 ID；重跑并重新取得双门禁 |
| 对返回图做商品级评价后再次请求 | 保留失败证据并停止，不触发另一轮模型请求 |
| 用批次参数预授权尚未生成的图片 | 先生成、水印并写 `awaiting_authorization`，用户针对具体结果授权后再交付 |
| 从上传响应数组直接取首项 | 只在 `data.attachments[record_id][field_id]` 中按 `target_filename` 唯一匹配 |
| 上传失败或结果不确定后自动再传 | 写 `failed` 或 `unknown` 并停止，等待人工处置，不自动重传 |
| 上传成功后回读字段确认 | 只解析内存响应，写版本化回执后结束 |
