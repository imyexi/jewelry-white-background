# 珠宝白底图语义 Mask Edit 设计

## 背景与结论

白底图必须保留飞书最新正面原图中的商品，只允许 Wawapi 重绘背景。旧实现先做 Generation，曾造成珠子、配件和材质整体漂移；随后改成原图 Mask Edit，但正式 Mask 脚本仍通过边框背景拟合、亮度边缘和椭圆环带猜测商品。这套算法默认商品是圆环，遇到吊坠、项链、链绳、非闭合饰品、白色垫板或干扰背景时会保护垫板、漏掉商品，且错误 Mask 仍可能通过只看占比和中心区域的门禁。

正式方案改为“云端视觉语义几何 + 本地确定性栅格化 + 色阶增强边缘贴合”。云端视觉负责识别商品是什么、由哪些可见组件组成以及大致位置；本地只在候选轮廓的窄带内使用原图和检测增强副本寻找更准确的像素边界。色阶增强只服务于本地 Mask 生成，绝不进入 Wawapi。

## 目标

- 唯一商品事实源是飞书正面图字段最新值的第一张图片。
- 支持手串、吊坠、项链、链绳及其他非圆环珠宝，不使用商品形状类别作为硬编码前提。
- 使用云端视觉逐图输出结构化几何，不部署或调用本地大模型。
- 在候选边界窄带内利用自适应色阶和边缘证据贴合商品轮廓，提高浅色、透明和细小组件的召回。
- Wawapi 固定接收未经色阶处理的同尺寸原图 PNG、最终 RGBA Mask 和固定白底 Prompt。
- Mask 与源图的 SHA-256、尺寸、视觉几何、贴边报告和门禁结论均可审计。
- 生成阶段固定无上传授权，水印结果写入 `status=awaiting_authorization` 后，用户只能授权该已存在的具体生成结果。
- 用户授权后只追加飞书主图；通过版本化回执、写前意图和完整身份匹配阻止重放与串号。
- 收到上传响应后只处理内存响应；原始响应先落盘，再按目标文件名唯一提取 token 并立即终止。

## 非目标

- 不修改 `yuan-image-generation` Skill 或 Wawapi 接口实现。
- 不把产品名称、参数、材料、生肖或细节图提交给 Wawapi。
- 不使用其他 provider、Generation、provider fallback、自动重试或自动改写 Prompt。
- 不对 Wawapi 结果执行商品内容、构图、占比或尺寸 QC。
- 不把色阶增强后的图片作为 Edit 底图、参考图或 Prompt 内容。
- 不用纯边缘算法在整张图中重新猜测商品，不保留椭圆环带作为正式回退。
- 不清理历史输出或回滚工作区中的既有修改。

## 唯一数据流

```text
飞书最新首张正面图
-> EXIF 校正后的同尺寸 RGB 原图
-> 云端视觉逐图识别可见商品组件
-> 带源图 SHA-256 和尺寸的归一化几何 JSON
-> 本地栅格化语义候选 Mask
-> 内存中的原图/自适应色阶检测副本
-> 仅在候选边界窄带内做边缘贴合与安全外扩
-> 无审核草稿生成 review 资产、叠加图与报告
-> 人工逐项核对并填写源图、规范化几何与最终 Mask 摘要
-> 重跑同一技术门禁
-> status=ok 且 automatic_wawapi_edit_allowed=true
-> Wawapi：原图 PNG + 最终 RGBA Mask + 固定 Prompt
-> 第一张有效本地图片
-> Yuan Studio 水印
-> 绑定完整结果身份的 status=awaiting_authorization
-> 用户对该具体结果授权
-> 写版本化 status=uploading 意图回执
-> 单次飞书主图追加
-> 原始响应落盘
-> completed / failed / unknown 终态后立即终止
```

检测副本与 Wawapi 输入是两条隔离的数据支路。`prepared/<product-id>_front.png` 必须保存未经色阶处理的原图像素；色阶参数只允许出现在 Mask 审计报告中。调用适配层时只引用该 `prepared` 原图和最终 Mask。

准备阶段把当时当前首张正面图的 `file_token` 和名称保存为 `front_file_token`、`front_file_name`，连同 `workflow_mode`、`product_id`、`record_id` 与目标 `field_id` 写入 `product_context.json`。交付阶段不回读飞书，因此这里的“当前首张正面图”只指该本地 context 固化的附件身份。

## 云端视觉几何契约

每张图片单独识别，禁止一次提交大量原图。视觉模型只输出 JSON，不直接生成或涂抹 Mask。几何文件使用 `vision-geometry-mask-2.0`，至少包含：

```json
{
  "schema_version": "vision-geometry-mask-2.0",
  "product_id": "SY1505",
  "source_sha256": "大写十六进制 SHA-256",
  "source_size": [3024, 4032],
  "coordinate_range": [0, 10000],
  "producer": "codex-cloud-vision",
  "primitives": [
    {
      "id": "main_pendant",
      "type": "polygon",
      "semantic": "吊坠主体",
      "points": [[4680, 4100], [4510, 4150], [4360, 4210]]
    },
    {
      "id": "cord",
      "type": "polyline",
      "semantic": "可见链绳",
      "points": [[0, 260], [300, 400], [600, 570]],
      "width": 145
    }
  ],
  "uncertain_regions": [
    {
      "id": "cord-border",
      "bbox": [4800, 0, 5200, 400],
      "reason": "细绳在顶边真实裁切"
    }
  ]
}
```

支持 `polygon`、`polyline`、`ellipse` 和 `ellipse_set`。坐标和宽度统一使用 `0..10000` 归一化整数；所有 primitive ID（含 `ellipse_set.items[].id`）在图元命名空间内唯一。polygon 至少含三个不同点且面积非零，polyline 至少含两个不同点且长度非零。`producer` 必须是受支持的云端视觉来源；缺失、空值或本地轮廓算法来源均拒绝。`semantic` 只描述正面图可见事实。应纳入珠子、吊坠、链绳、串线、吊环、连接件、金属件和透明商品内部透出的区域；不纳入垫板、桌面、道具及大面积柔和投影。

视觉模型必须优先召回商品，并允许候选几何带有小幅安全余量。无法确认的细绳、透明边缘、遮挡连接处或真实裁切位置写入 `uncertain_regions`，不得靠猜测删除。每个 `uncertain_regions[].id` 必须是非空字符串，并在未确定区域数组内唯一；`bbox` 必须是四个 `0..10000` 整数组成的正面积矩形；`reason` 必须是非空字符串。`bbox` 和 `reason` 仅用于定位与审计，不直接参与 Alpha 栅格化。

首次几何草稿不带 `mask_review`，因此只能产出 `review`。人工核对同一源图的最终 Mask、叠加图和报告后，将审核对象写回同一个 profile：

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

`resolved_uncertain_regions` 必须是无重复字符串数组，且每一项精确引用现有 `uncertain_regions[].id`；未知 ID 直接校验失败，缺省或只覆盖部分 ID 时，其余 ID 继续保持未解决。该字段只记录人工审核结论，不会修改 Mask。`geometry_sha256` 是规范化 JSON 的摘要，覆盖 schema、商品与源图身份、坐标范围、producer、全部图元和完整 `uncertain_regions`，排除 `mask_review` 与非语义路径；`mask_sha256` 是最终 RGBA Mask PNG 的字节摘要。审核同时绑定源图、几何与 Mask，任一变化都会把审核状态降为 `stale`，不得自动放行。

## 本地 Mask 栅格化

栅格器先校验几何文件中的源图 SHA-256、尺寸、坐标范围、图元类型、`uncertain_regions` 和 `mask_review`，再确定性绘制二值候选 Alpha。任何身份、schema 或审核契约不匹配均直接终止，不形成可放行的评估报告。

最终资产包括：

- `prepared/<product-id>_front.png`：同尺寸 RGB PNG，除 EXIF 方向和 Alpha 白底合成外不改变原图像素；
- `mask/<product-id>_product-protection-mask.png`：同尺寸 RGBA PNG，RGB 全为 255，Alpha 只含 `{0,255}`；
- `mask/<product-id>_editable-overlay.png`：红色仅表示可编辑背景；
- `logs/<product-id>_vision-mask-report.json`：身份、几何、边缘贴合和门禁报告。

Mask 语义固定为 `alpha=255` 保护商品、`alpha=0` 编辑背景。不得使用透明黑色隐藏 RGB。报告必须包含 `product_id/source_sha256/geometry_sha256/prepared_sha256/mask_sha256`；prepared 与 Mask 摘要基于实际写入磁盘的 PNG 字节，供联网前和交付前复核。

## 色阶增强与边缘贴合

色阶增强不修改原图文件，只创建内存检测副本。正式默认使用三路边缘证据：

1. `original`：未经增强的原图亮度与 RGB 通道边缘；
2. `global_robust`：只拉伸亮度，黑白点取 `P1/P99`，并将黑点限制在 `0..96`、白点限制在 `160..255`、有效跨度限制为至少 96；
3. `local_limited`：受限局部对比度版本，用小块亮度拉伸或等价的受限 CLAHE，禁止无限制直方图均衡。

固定 `69/204` 只可作为人工诊断对照，不得成为正式默认参数，也不得单独决定 Mask。若温和版本证据不足，可启用 `P3/P97` 强候选版本，但它只能提出待审核边缘。

候选 Alpha 经腐蚀和膨胀构成允许修改的窄带：向内距离为图片短边的约 `0.2%`，向外距离约 `0.4%`，并分别限制为小范围像素值。窄带外 Alpha 必须逐像素保持不变；候选内部的高置信核心不得删除；真实触边组件的画布边缘像素冻结不变。

边缘贴合只接受以下证据：原图与至少一份增强图共同支持的边缘，或两种独立增强机制在小容差内共同支持的边缘。透明珠由近闭合轮廓、重复珠列、折射边界和高光共同确定，并保护完整珠体包络；浅色绳结合色度边缘、连续细长几何和连接拓扑补足。宽、低频、单侧渐变的投影以及长直线、大四边形垫板边缘必须排除。

贴边失败时不得切换到环形推断。低动态范围、增强裁剪过量、跨版本边缘分歧、种子不足、改动越过窄带或改动面积超预算时，保留原始视觉候选 Mask，并将状态保持为 `review`，等待修订几何或完成逐项审核后重跑。

## 联网前门禁

当前 `MaskAssessment` 只产生 `review` 或 `ok`。合法输入在缺少审核或存在任一技术原因时为 `review`；只有合法 approved `mask_review` 存在且 `reasons` 为空时，才同时产生 `status=ok` 与 `automatic_wawapi_edit_allowed=true`。输入、schema 或身份非法时直接终止，不形成 `fail` 报告；`fail` 仅是脚本保留的退出码。视觉几何新草稿固定从 `review` 开始，必须同时满足下列条件并完成 Mask 审核：

- 源图 SHA-256、尺寸、schema 和所有图元通过校验；
- Mask 与原图同尺寸、RGB 全白、Alpha 二值；
- 所有可见商品组件均由跨版本边缘或明确语义几何支持；
- 没有大面积垫板、桌面、投影或道具进入保护区；
- 窄带外不变量、高置信核心和触边冻结不变量均成立；
- 最终保护占比位于 `0.5%..70%`；低于下限按退化或近空 Mask 处理，高于上限按大面积背景误保护处理；
- `mask_review.resolved_uncertain_regions` 逐项覆盖全部 `uncertain_regions[].id`；未覆盖 ID 按原始顺序写入 `unresolved_uncertain_region_ids` 并保持 `review`。

真实商品可以接触画布边缘。只有视觉几何明确声明相应组件被画布裁切、且保护区沿同一组件连续到达边缘时才允许触边；实现将最终边框保护像素减去全部 `touches_border=true` 图元在边框上的栅格像素，剩余任一像素均属于未知触边并保持 `review`。一个合法触边图元不能替其他未声明图元背书。项链内圈、吊坠中心或非环形商品覆盖画面中心均属合法结构，因此不再使用“中心必须可编辑”的通用门禁。

报告记录每个增强版本的实际黑白点、实际被该增强规则裁剪的像素比例、边缘支持版本数、单版本边缘面积、窄带和改动像素、触边声明、未确定区域、审核状态、五项身份摘要、未解决 ID 以及最终原因。审核只能逐项消除相应 uncertainty 原因，不能清除边缘贴合、不变量、触边或保护占比等其他 `reasons`。任何审核、几何、疑点定义或最终 Mask 更新后都必须重跑；不能把未通过技术条件的结果直接放行。

## Wawapi 契约

Wawapi 命令固定为：

```powershell
& "<python>" "<YUAN_IMAGE_ROOT>\scripts\yuan_image_helper.py" generate `
  --provider "wawapi" `
  --operation "edit" `
  --prompt $prompt `
  --output-dir "<output_root>\edit" `
  --image "<output_root>\prepared\<product-id>_front.png" `
  --mask "<output_root>\mask\<product-id>_product-protection-mask.png"
```

仅当 `status=ok` 且 `automatic_wawapi_edit_allowed=true`，并且调用前重算的 prepared 与 Mask SHA-256 仍等于报告摘要时构建请求。请求是单 image、单 mask，二者尺寸相同；不包含色阶参数、检测副本、几何 JSON、细节图、比例或分辨率。Prompt 固定要求浅白连续背景和不重绘 Mask 保护区，不含产品语义。每款只请求一次；失败时不切换 provider、不自动重试、不自动改 Prompt。

响应候选按顺序检查，跳过缺失、空文件、损坏图片或非 PNG/JPEG，接受第一张可完整解码且成功落盘的 PNG/JPEG；所有候选无效则失败。该阶段不打开结果做商品内容、构图、占比或尺寸 QC。

## 生成状态与授权边界

生成与上传是两个严格分离的阶段。生成阶段永远不接收上传授权；Wawapi 结果和水印文件完成后写入 `status=awaiting_authorization`，至少绑定：

- `workflow_mode`、`product_id`、`record_id`；
- 当前首张正面图的 `front_file_token` 和 `front_file_name`；
- 五项 Mask 身份摘要：`product_id`、`source_sha256`、`geometry_sha256`、`prepared_sha256`、`mask_sha256`；
- `generated_image_sha256`；
- 水印文件 `local_file` 绝对路径、`local_file_sha256` 与 `target_filename`；
- 目标 `field_id`。

用户授权必须明确指向这一已存在的具体生成结果。禁止预授权未来图片：缺少有效 `awaiting_authorization` state 时，授权参数不能触发生成并上传；授权也不能跨 `product_id`、`record_id`、首图附件、生成摘要、水印路径或版本复用。授权对象、`product_context.json`、state、Mask 报告与磁盘资产任一身份不一致都直接停止。

## 水印与飞书交付

第一张有效本地结果保留为未加水印原件，水印工具另存 `YUAN STUDIO` 与 `PN <product-id>` 副本。水印输出使用预先确定的唯一绝对路径，不通过目录扫描选择；只有用户对这一具体结果明确授权后才追加到目标主图字段。

上传只追加，不删除、不替换、不清空旧 token。完整交付身份由 `workflow_mode`、`product_id`、`record_id`、`front_file_token`、`front_file_name`、五项 Mask 身份摘要、`generated_image_sha256`、水印 `local_file` 绝对路径、`local_file_sha256`、`target_filename` 与 `field_id` 组成。交付前重新计算 prepared、Mask、generated 与水印文件摘要；所有来源必须完全一致。

每个目标水印文件使用 `upload_receipt.<target-stem>.json`，状态机为：

1. **预检**：若同版本回执已是 `status=completed`、上传 token 非空且完整交付身份完全相同，则本地判定完成并禁止重放；`uploading`、`failed`、`unknown`、损坏或冲突回执全部停止。通用 `upload_receipt.json` 只可作为兼容索引，不得作为防重依据。
2. **写意图**：调用上传接口前，以原子写入方式先保存 `status=uploading`、授权依据和完整交付身份。
3. **单次上传**：只允许一次附件上传调用，不为确认现状而搜索记录或回读主图。
4. **先存响应**：收到任意响应后，先把原始响应写入同一版本化回执，再判断 `ok` 或解析 token；命令异常、无响应或 JSON 不确定时保存错误证据。
5. **唯一匹配**：真实成功响应从 `data.attachments[record_id][field_id]` 取候选，按 `target_filename` 唯一匹配并提取非空 `file_token`，不得取数组首项。零匹配、多匹配或空 token 都不能证明成功。
6. **终态**：`ok: true` 且唯一匹配时写 `status=completed`；明确非成功写 `status=failed`；响应或解析结果无法确定写 `status=unknown`。`failed`、`unknown` 和遗留 `uploading` 均禁止自动重传。
7. **终止**：上传后只解析内存中的响应并写版本化回执；随后立即结束，不回读飞书、不扫描附件、目录或图片、不调用列表接口确认。

## 验收

- 正式入口不存在椭圆环带推断、Generation、其他 provider 或自动 fallback。
- 同一源图和几何 JSON 重跑得到字节级一致的最终 Mask 与报告。
- 合成的手串、吊坠、链绳和透明珠样本商品保护召回不低于 95%，背景误保护率不高于 8%。
- SY1505、SY1506、SY1549 的视觉几何 Mask 能保护吊坠、连接件、全部可见链绳、珠子、串线和金属件，不保护大面积垫板或投影。
- 色阶增强改善边缘贴合时，所有改动局限于候选窄带；低对比或证据分歧时确定性回退并保持 `review`。
- Edit 底图与未经色阶处理的 EXIF 校正原图逐像素一致，Wawapi 命令不包含任何检测副本或色阶参数。
- 参考计划和 Prompt 不包含产品名称、参数、材料或生肖哨兵词。
- 无授权生成只产生 `status=awaiting_authorization`；缺少该 state 时任何授权都不能生成并上传未来图片。
- 回执测试覆盖完整身份错配、不同水印版本、遗留 `uploading`、明确失败、无响应及 `ok: true` 下零/多文件名匹配。
- 上传成功只接受 `data.attachments[record_id][field_id]` 中与 `target_filename` 唯一同名且 token 非空的附件。
- 定向测试、项目全量测试和 Skill 校验全部通过。
