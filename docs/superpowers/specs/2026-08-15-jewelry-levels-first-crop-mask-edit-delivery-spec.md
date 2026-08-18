# 珠宝白底图“色阶先行—裁剪—Mask Edit—自动交付”开发规格

## 文档信息

- 日期：2026-08-15
- 规格版本：`2.1`
- 目标 Skill：`skills/jewelry-white-background`
- 文档状态：已评审修订，待实施
- 适用范围：手串、吊坠、项链、链绳及其他需要生成浅白背景商品图的珠宝
- 核心工具：云端视觉几何识别、Wawapi Edits、Yuan Studio 水印、飞书多维表格附件追加
- 兼容边界：历史 `vision-geometry-mask-2.0` 资产只读；本规格只适用于新建的 `2.1` 运行

## 背景

当前正式 Skill 已具备云端视觉几何、产品保护 Mask、Wawapi Edits、水印和飞书追加能力，但现有入口仍执行旧流程，运行目录、人工确认、裁剪坐标、重试和上传幂等合同也不足以支持安全恢复和并发执行。

新流程要求先从规范化原图生成固定检测资产，再把全局色阶图单独交给云端模型识别几何；依据几何裁剪未经调色的原图，并在同一裁剪坐标系中生成 Mask。Wawapi 只能接收未经色阶处理的裁剪原图、用户确认且重新校验通过的 Mask、固定 Prompt。

Wawapi 返回结果后，排版只能读取该生成结果。用户确认最终排版图后，该次确认同时授权固定水印和飞书附件追加；自动流程不再设置第三个人工 Hook，也不在上传后回读飞书。

## 目标

- 把每次处理固化为有唯一 `run_id`、不可覆盖资产和可恢复状态的独立运行。
- 区分飞书附件原始字节与规范化 RGB PNG，消除 `source_sha256` 的歧义。
- 用固定的全局色阶图提高云端几何识别可见度，同时保留原图、全局图、局部图三路 Mask 边缘证据。
- 使用完整、可校验的 `vision-geometry-mask-2.1`，确定性计算非方形裁剪框和裁剪坐标资产。
- 用不可变确认回执把用户确认绑定到当时实际查看的文件、摘要、尺寸、记录和字段。
- 在确认后只允许固定 Wawapi Edit 请求；真实 POST 最多 3 次，未知远端结果不得自动重提。
- 只使用 Wawapi 生成结果执行确定性 `1536 × 2048` 排版。
- 最终确认后自动执行一次水印和最多一次飞书追加，不重复询问上传权限。
- 通过原子状态、调用 claim 和上传回执，保证并发进程及崩溃恢复不会重复产生外部副作用。

## 非目标

- 不让任何色阶或局部对比度资产成为 Wawapi、排版、水印或飞书上传的像素来源。
- 不让云端模型直接返回不可审计的 Mask 位图。
- 不一次向云端模型提交多张商品图。
- 不使用 Generation、其他 provider、其他 operation 或自动改写 Prompt 作为 fallback。
- 不在生成结果排版阶段使用原图、裁剪图、几何或 Mask 二次抠图、拼接或修复。
- 不在最终确认后再次询问是否允许上传。
- 不在水印成功后解码、查看或执行任何图片内容检查。
- 不对 Wawapi 未知结果或飞书上传执行自动重试。
- 不原地升级历史运行，不覆盖历史资产、确认回执或调用 claim。
- 不在单元测试中调用真实 Wawapi 或写入飞书。

## 核心不变量

1. 每次运行都有唯一、不可变的 `run_id`，目录固定为 `<output_root>/<safe-product-id>/<run-id>/`。
2. `raw_source` 是飞书附件原始下载字节；`source_original` 是规范化 RGB PNG。二者摘要必须分开记录。
3. 全流程中的 `source_sha256` 固定指向 `source_original` 的实际文件摘要，不得指向原始下载字节。
4. `geometry_detection` 和 `local_detection` 与 `source_original` 尺寸及方向完全一致，只用于识别和 Mask 边缘证据。
5. `cropped_original` 必须逐像素等于 `source_original.crop(crop_box)`，不得应用色阶、锐化或其他调色。
6. 同一 `crop_box` 必须同步作用于原图、全局检测图和局部检测图。
7. Mask 的商品保护候选必须先在完整源图坐标系栅格化，再使用同一 `crop_box` 裁剪。
8. Wawapi 只接收确认回执绑定的 `cropped_original + product_protection_mask + fixed_prompt`。
9. `automatic_wawapi_edit_allowed` 只能由“Mask 技术门禁通过且确认回执仍有效”即时派生，不作为第二个可独立修改的真相源。
10. 每个 Wawapi single-attempt adapter 内部重试次数为 0，每个 call slot 最多执行一次 Edit POST。
11. 排版接口只能接收 Wawapi 生成结果、固定参数和输出路径。
12. 排版缩放和移动的是完整生成结果，不得创建产品 Alpha 或读取生成前资产。
13. 最终确认只授权其绑定的当前排版图、飞书记录、目标字段和 `delivery_id`。
14. 水印目标文件必须由本次调用以唯一文件名原子创建；退出成功后只允许做非图像文件检查。
15. 飞书正式写入只允许调用一次附件追加命令；上传前后均不得由自动流程回读目标字段。
16. 所有失败状态和未知状态都是当前运行的自动终态；除“人工确认旧上传已成功后只补记 `completed`”外，重新生成、修订、重新排版或再次上传必须新建 `run_id`。
17. Windows 安全路径上限为 `240` 个字符；创建运行时为最深临时输出预留 `64` 个字符，Wawapi 精确临时输出必须在 claim 和真实 POST 前再次校验。

## 运行身份、资产身份与原子性合同

### `run_id` 与目录

- 在读取飞书字段和写入任何运行资产前，生成唯一 `run_id`。
- 格式固定为 `<UTC时间戳>-<32位小写UUID十六进制>`，例如 `20260815T103015123Z-0123456789abcdef0123456789abcdef`。
- `product_id` 必须匹配 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`，不得包含 `..`，不得以点结尾。
- 把 `product_id` 转为大写后，若匹配 `^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)`，必须拒绝，以避免 Windows 设备名和带扩展名设备名。
- 通过上述检查后 `safe-product-id = product_id`；本版本不做可能产生碰撞的静默替换。
- 先以独占创建方式建立同级 staging 目录 `.<run-id>.creating-<owner-id>`，在其中创建 `manifests/`，写入并刷新完整初始 `manifests/workflow_state.json` 和 `manifests/workflow.lock`。
- 初始文件就绪后，使用同卷、禁止覆盖的目录原子重命名，把 staging 目录发布为最终 `<run-id>/`。
- 最终目录已存在时不得复用、清空或覆盖，必须重新生成 `run_id`。
- 崩溃遗留的 staging 目录不属于正式运行，不得恢复为最终目录；只有证明其 owner 已终止后才可由维护流程清理。
- 同一 SKU 的再次执行、用户否定后的修订、人工重试、重新排版或重新上传都必须建立新运行。
- 同一运行内 Wawapi 的 30/90 秒允许重试仍使用同一个 `run_id`。
- Windows 实现必须支持扩展长度路径；不得通过截断 `run_id`、`delivery_id` 或文件名规避路径长度问题。
- 允许缩短无业务语义的临时目录随机段；Wawapi 临时目录固定为 `tmp/e-<8位随机十六进制>/`，该别名不得替代或改变 Manifest、确认回执、call claim 和交付回执中的完整业务身份。
- 运行根目录或精确临时输出超过路径预算时，必须在 Wawapi claim 和真实 POST 前终止，真实调用次数为 `0`，并提示调用方改用更短的 `output_root`。

### 源图和摘要语义

全流程区分：

- `raw_source`：从飞书附件接口取得的原始下载字节，尚未执行 EXIF 校正、Alpha 合成或格式转换；摘要为 `raw_source_sha256`。
- `canonical_source`：对原始字节依次执行 EXIF 方向校正、透明通道在纯白背景上合成、转换为 RGB、以 PNG 落盘后的文件；流程别名为 `source_original`，摘要为 `canonical_source_sha256`。
- 所有 `vision-geometry-mask-2.1`、裁剪、Mask 和确认合同中的 `source_sha256` 必须等于 `canonical_source_sha256`。
- 所有文件 SHA-256 都基于实际落盘字节计算，表示为 64 个大写十六进制字符。
- JSON 语义摘要使用 UTF-8、键名升序、`separators=(",", ":")`、无 BOM 的规范化 JSON 计算；路径和人工确认数据只有在对应合同明确列出时才进入摘要。
- 图片尺寸统一表示为 `[width, height]`；所有合同路径都使用当前运行目录的相对路径。

### 资产发布与不可变性

- 普通资产必须先写入同目录临时文件，完整关闭后再原子发布到最终路径。
- 最终路径已存在时发布失败，不得覆盖。
- 资产一旦进入 review bundle、确认回执或后续阶段，就不得在当前运行内修改。
- `manifests/workflow_state.json`、Wawapi call 记录、水印回执和上传回执允许原子更新状态，但必须保留不可变首次 claim、原始响应和完整历史，不得删除或改写既有证据。
- 临时文件不属于正式资产；恢复流程只可删除能够证明属于当前运行且从未发布的临时文件。
- 对无外部副作用的本地阶段，若完整输出已发布但状态仍停留在前一阶段，恢复流程必须重新校验全部预期文件、manifest 和摘要：全部一致时只推进状态；存在部分发布或冲突时进入对应失败终态。不得覆盖后重算。

### `manifests/workflow_state.json`

初始状态固定为 `run_created`。必填字段：

- `schema_version`
- `run_id`
- `product_id`
- `base_token`
- `table_id`
- `record_id`
- `front_field_id`
- `target_field_id`
- `status`
- 从 `1` 开始、每次迁移递增的 `state_revision`
- `created_at`、`updated_at`，均为 UTC RFC 3339
- `last_transition.from`、`last_transition.event`、`last_transition.at`
- `receipts.mask.path`、`receipts.mask.sha256`，未确认时为 `null`
- `receipts.final.path`、`receipts.final.sha256`，未确认时为 `null`
- `delivery.delivery_id`、`delivery.upload_receipt_path`，未进入交付时为 `null`
- `failure`，无失败时为 `null`

状态迁移必须：

1. 取得当前运行的进程间独占锁。
2. 校验 `expected_status + expected_state_revision`。
3. 在同一锁内完成对应的确认回执或副作用 claim 创建。
4. 把新状态写到 `manifests/` 内的临时文件，刷新文件内容后原子替换 `manifests/workflow_state.json`。
5. 释放锁后才执行已经取得 claim 的外部调用。

校验失败的进程不得执行外部副作用。锁只负责短临界区；外部网络等待不得长期持有锁。

### 外部调用 owner 与存活判定

Wawapi call、水印调用和飞书上传的持久化 claim 都必须包含：

- `owner_id`：32 位小写 UUID 十六进制，只用于安全文件名和本次 claim 标识。
- `owner_host_id`、`owner_pid`、`owner_process_started_at`。
- `claimed_at`、`heartbeat_at`、`lease_expires_at`。
- 从 1 开始递增的 `record_revision`。

`owner_id + owner_host_id + owner_pid + owner_process_started_at` 共同标识持有进程，创建后不可修改。每个 claim 使用与 claim JSON 同目录、文件名固定为 `<claim-file>.lock` 的独立进程间更新锁，不同 claim 不得共用；所有心跳、响应和终态更新都必须校验预期 `record_revision`，合并现有字段后原子替换，禁止用旧快照整文件覆盖。

持有者在外部调用期间每 5 秒更新 `heartbeat_at`，并把 `lease_expires_at` 更新为本次心跳后 30 秒。心跳只在 owner 匹配且状态仍为 in-flight 时生效；发现 `response_recorded` 或终态时必须无操作退出。

主线程写入响应或终态前，必须先通知心跳线程停止并等待其退出，再取得 claim 更新锁，基于最新 revision 合并响应、history 和新状态。该顺序适用于 Wawapi call、水印回执和上传回执。

其他进程遇到 in-flight claim 时：

1. owner 进程仍存活，或 `heartbeat_at` 距当前时间小于等于 30 秒：claim 为 active，当前进程不得改状态、接管 claim 或执行外部调用，只返回 `already_in_progress`。
2. 只有在同一主机上通过 PID 与进程启动时间证明 owner 已终止，且心跳已超过 30 秒时，claim 才可进入恢复判定。
3. 无法可靠判断 owner 是否存活时，保持当前状态并要求人工恢复；不得因租约时间到达而自动接管。
4. 恢复判定只能解析现有文件和响应，永远不得重新执行旧 claim 对应的 Wawapi POST、水印命令或飞书上传命令。

claim 文件是“副作用名额已被占用”的权威证据。若进程在创建 claim 后、更新 `manifests/workflow_state.json` 前崩溃，恢复流程必须按已有 claim 判定 active、unknown 或本地结果，禁止重建同一 claim。

### 确认有效期

- Mask 和最终确认均不按自然时间自动过期。
- 任一绑定资产、摘要、尺寸、`run_id`、`record_id`、`target_field_id`、review bundle 或 manifest 变化时，确认立即失效。
- 已进入终态的确认不得在新运行中复用。

## 唯一正式数据流

```text
创建唯一 run
→ 单次读取飞书首张正面图
→ 固化原始下载字节和规范化原图
→ 生成同尺寸全局色阶图与局部对比度图
→ 单图提交全局色阶图给云端视觉
→ 本地包装并校验 vision-geometry-mask-2.1
→ 完整源图坐标栅格化候选 Alpha
→ 确定性计算 crop_box
→ 同步裁剪原图、全局图、局部图和候选 Alpha
→ 生成并审计裁剪坐标几何
→ 三路证据窄带贴合，生成产品保护 Mask
→ 固化 Mask review bundle
→ Mask 检查 Hook
→ 写入不可变 Mask 确认回执
→ 重新校验全部绑定资产和技术门禁
→ Wawapi Edit：裁剪原图 + 确认 Mask + 固定 Prompt
→ 仅在明确 429，或有机器证据证明请求未被接受的 5xx 时按 30/90 秒重试，总 POST 不超过 3 次
→ 原子保存第一张有效生成结果
→ 只使用生成结果确定性排版
→ 固化最终 review bundle
→ 最终效果 Hook
→ 写入不可变最终确认回执和 delivery_id
→ 固定单文件水印
→ 对唯一目标做非图像文件检查
→ 原子创建上传 claim
→ 一次飞书附件追加
→ 保存响应与终态
→ 结束，自动流程不回读飞书
```

## 资产分支隔离

```text
source_original
├─→ geometry_detection ─→ 云端视觉几何 ─→ 完整候选 Alpha ─┐
├─→ local_detection ────────────────────────────────────────┤
└─→ cropped_original ───────────────────────────────────────┤
                                                            ├─→ confirmed_mask
cropped_detection + cropped_local_detection ────────────────┘
                                                                   │
cropped_original + confirmed_mask + fixed_prompt ──────────────────┘
                                                                   ↓
                                                           generated_result
                                                                   ↓
                                                    整张结果缩放和排版
                                                                   ↓
                                                       用户确认最终效果
                                                                   ↓
                                                         水印 → 飞书
```

禁止从 `geometry_detection`、`local_detection`、几何检查图、Mask 叠加图或任何生成前图片连接到 `generated_result` 之后的节点。

## 阶段 0：创建运行并固化唯一源图

### 输入

- 飞书 Base、数据表、目标记录、正面图字段和目标主图字段身份。
- 正面图字段在运行开始时读取到的第一张附件。

### 处理

- 原子创建运行目录和初始 `manifests/workflow_state.json`。
- 每个 SKU 只读取正面图字段一次，只选择当次字段值中的第一张附件。
- 下载字节原样写入 `source/<product-id>_raw.bin`，不得重新编码或覆盖。
- 从该文件生成唯一规范化像素事实源：EXIF 校正 → Alpha 在 `(255,255,255)` 上合成 → RGB → PNG。
- 除上述规范化外，不得执行色阶、锐化、缩放、额外旋转或裁剪。
- 把运行身份、飞书附件身份、源字段、目标字段和源图身份写入 `product_context.json`。
- 下载完成后不再读取正面图字段，也不枚举其他附件。

`product_context.json` 至少包含：

```json
{
  "schema_version": "jewelry-product-context-2.1",
  "workflow_mode": "background_only_edit",
  "run_id": "<run-id>",
  "product_id": "SY1537",
  "base_token": "<base-token>",
  "table_id": "<table-id>",
  "record_id": "<record-id>",
  "front_field_id": "<front-field-id>",
  "target_field_id": "<target-field-id>",
  "front_file_token": "<file-token>",
  "front_file_name": "<original-name>",
  "source_identity": {
    "raw_source_path": "source/SY1537_raw.bin",
    "raw_source_sha256": "<raw-bytes-sha256>",
    "raw_source_size_bytes": 123456,
    "canonical_source_path": "source/SY1537_original.png",
    "canonical_source_sha256": "<canonical-png-sha256>",
    "canonical_source_size": [3024, 4032],
    "canonical_source_mode": "RGB"
  },
  "source_sha256": "<must-equal-canonical-source-sha256>",
  "source_size": [3024, 4032]
}
```

### 输出

- `source/<product-id>_raw.bin`
- `source/<product-id>_original.png`
- `product_context.json`

### 门禁

- 原始附件和规范化 PNG 均必须非空。
- 规范化 PNG 必须完整解码，模式为 RGB，宽高为正，且不超过 `20,000,000` 像素。
- `source_sha256` 必须等于规范化 PNG 的实际文件摘要。
- 任一身份、摘要或尺寸不一致时进入 `source_failed`，不创建后续资产。

## 阶段 1：生成固定检测资产

### 唯一权威实现

`skills/jewelry-white-background/scripts/prepare_mask_detection_images.py` 是检测算法的唯一权威实现。编排器、裁剪脚本和 Mask 脚本只能调用该模块，不得复制算法、另写 LUT 或接受单 SKU 参数覆盖。

固定使用三路证据：

1. `original_rgb`：规范化 RGB 原图，仅供本地亮度和 RGB 色度边缘计算。
2. `global_robust`：全局受限色阶图；唯一允许提交给云端视觉模型的图片。
3. `local_limited`：受限局部对比度图；只供本地 Mask 边缘贴合。

`global_robust` 固定算法：

- 灰度输入为 `canonical_source.convert("L")`。
- 百分位位置使用 Python 3 `round((pixel_count - 1) * fraction)`。
- `low_fraction=0.01`，`high_fraction=0.99`。
- `black_point=min(96, P01)`，`white_point=max(160, P99)`。
- 动态范围小于 `96` 时，以当前区间中心扩展到至少 `96`，并限制在 `0..255`。
- LUT 为 `clamp(round((value-black_point)*255/(white_point-black_point)), 0, 255)`。
- 输出为与原图同宽高的单通道 L PNG。

`local_limited` 固定算法：

- 灰度输入为 `canonical_source.convert("L")`。
- `radius=max(1, round(min(width,height)*0.025))`。
- 使用 `BoxBlur(radius)` 取得局部均值。
- `raw_detail=gray-BoxBlur(gray,radius)+128`。
- `detail=clamp(raw_detail,96,160)`。
- `local_limited=clamp(gray+detail-128,0,255)`。
- 输出为与原图同宽高的单通道 L PNG。

### 输出

- `detection/<product-id>_geometry_detection.png`，对应 `global_robust`
- `detection/<product-id>_local_detection.png`，对应 `local_limited`
- `logs/<product-id>_detection_manifest.json`

### Detection Manifest 必填字段

- `schema_version="mask-detection-images-2.0"`
- `source_sha256`、`source_size`
- `coordinate_transform="identity"`
- 两个输出的相对路径、SHA-256、尺寸和模式
- 算法名称、全部固定参数和实际计算出的黑白点、模糊半径、裁切比例
- `authoritative_implementation="skills/jewelry-white-background/scripts/prepare_mask_detection_images.py"`

### 门禁

- 两个检测图尺寸必须等于 `source_size`。
- 不得缩放、旋转、裁剪或改写规范化原图。
- Manifest 中的输入、输出摘要必须与磁盘文件一致。
- 失败时进入 `detection_failed`，不调用云端视觉。

## 阶段 2：云端模型识别产品几何

### 输入

- 单张 `geometry_detection.png`。

### 模型职责

- 每次请求只提交一张图片。
- 模型响应必须是只包含 `primitives` 和 `uncertain_regions` 的 JSON 对象；出现其他顶层字段时拒绝，不做静默兼容。
- 模型不得生成图片、Mask 位图、摘要、尺寸、producer、确认状态或本地路径。
- 应识别珠子、吊坠、链绳、串线、吊环、连接件、金属件、透明商品包络和真实源图触边部分。
- 不把垫板、桌面、道具或大面积柔和投影识别为商品。

### `vision-geometry-mask-2.1` 完整合同

模型响应通过“只含两个顶层字段”的严格校验后，本地编排器再从 `product_context.json` 和 Detection Manifest 填写身份外壳；模型不参与任何身份字段。

```json
{
  "schema_version": "vision-geometry-mask-2.1",
  "product_id": "SY1537",
  "source_sha256": "<canonical-source-sha256>",
  "source_size": [3024, 4032],
  "detection_image_sha256": "<geometry-detection-sha256>",
  "detection_image_size": [3024, 4032],
  "detection_manifest_sha256": "<detection-manifest-file-sha256>",
  "coordinate_transform": "identity",
  "coordinate_range": [0, 10000],
  "producer": "codex-cloud-vision",
  "primitives": [
    {
      "id": "product-envelope",
      "type": "ellipse",
      "semantic": "product",
      "params": [5000, 5000, 2000, 2500],
      "touches_border": false
    }
  ],
  "uncertain_regions": [],
  "geometry_sha256": "<digest-excluding-this-field>"
}
```

图元合同：

- 所有坐标和宽度均为 `0..10000` 整数。
- `polygon`：`points` 至少包含三个不同点，面积非零。
- `polyline`：`points` 至少包含两个不同点、长度非零，且 `width>0`。
- `ellipse.params`：固定为 `[center_x,center_y,radius_x,radius_y]`，两个半径均为正。
- `ellipse_set.items`：非空；每项包含唯一非空 `id` 和合法 `params`。
- 全部 primitive 和 `ellipse_set.items` 的 ID 在同一图元命名空间内唯一。
- 每个 primitive 必须包含非空 `semantic`。
- 可选 `touches_border` 必须为布尔值；`true` 只表示商品在规范化源图边界真实被裁切。
- 每个 `uncertain_regions` 项包含唯一非空 `id`、正面积 `bbox=[left,top,right,bottom]` 和非空 `reason`。
- `primitives` 不得为空。
- 几何文件不得内嵌 `mask_review`、确认人或其他人工确认数据。

`geometry_sha256` 对上述完整身份外壳、producer、图元和疑点区域计算规范化 JSON 摘要，排除 `geometry_sha256` 自身、文件路径与人工确认文件。

### `2.0` 兼容与迁移

- 新编排器和新裁剪流程只接受 `vision-geometry-mask-2.1`。
- 现有 `2.0` loader 可暂时保留为旧运行只读兼容代码，但新编排器不得调用。
- 禁止给 `2.0` JSON 补写检测图摘要来伪造 `2.1`。
- 合法迁移方式只有：新建 `run_id`，重新执行检测，并把本次 `geometry_detection.png` 单图提交云端模型。
- `2.0` 中已有的 `mask_review` 不得迁移；新运行必须重新生成 Mask 并重新确认。

### 门禁

- 本地身份外壳必须与本次 context、Detection Manifest 和磁盘资产逐项一致。
- 图元必须合法、非退化，`producer` 必须为 `codex-cloud-vision`。
- `2.0`、未知 schema、缺字段、身份冲突或本地轮廓 producer 均进入 `geometry_failed`。
- 失败时不得回退到纯本地边缘、椭圆环带或 Generation。

## 阶段 3：根据几何确定性裁剪

### 坐标和栅格语义

- 图片矩形统一使用 `[left,top,right,bottom)` 半开区间。
- 归一化点到源图像素中心的映射固定为：

```text
pixel(n, dimension) = Python3-round(n * (dimension - 1) / 10000)
```

- 椭圆半径分别按对应轴映射并至少为 1 像素。
- polyline 宽度固定为 `max(1, Python3-round(width * min(source_size) / 10000))`。
- 使用与 Mask 相同的权威栅格器，在完整 `source_size` 上生成二值候选 Alpha。
- `uncertain_regions` 的 bbox 使用保守外扩：

```text
left_px   = floor(left   * (width  - 1) / 10000)
top_px    = floor(top    * (height - 1) / 10000)
right_px  = min(width,  ceil(right  * (width  - 1) / 10000) + 1)
bottom_px = min(height, ceil(bottom * (height - 1) / 10000) + 1)
```

- `content_box` 是候选 Alpha 的 `getbbox()` 与全部疑点区域像素框的联合框；为空时失败。

### 裁剪公式

令 `content_width=right-left`、`content_height=bottom-top`。目标最大占比固定为精确有理数 `77/100`：

```text
desired_width  = ceil(content_width  * 100 / 77)
desired_height = ceil(content_height * 100 / 77)

crop_width  = min(source_width,  max(content_width,  desired_width))
crop_height = min(source_height, max(content_height, desired_height))

ideal_left = ceil((content_left + content_right - crop_width) / 2)
ideal_top  = ceil((content_top + content_bottom - crop_height) / 2)

crop_left = clamp(ideal_left, 0, source_width - crop_width)
crop_top  = clamp(ideal_top,  0, source_height - crop_height)
crop_box  = [crop_left, crop_top, crop_left + crop_width, crop_top + crop_height]
```

- 两个轴独立计算，不强制正方形或固定宽高比。
- 未发生边界夹紧时，奇数像素无法均分的多余 1 像素固定留在右侧或下侧。
- 若目标尺寸超过源图，对应轴使用完整可用范围，并记录 `source_limited`；该轴实际占比允许高于 77%。
- 不得扩展画布或伪造源图外内容。
- 同一个 `crop_box` 同步裁剪原图、全局检测图、局部检测图和完整候选 Alpha。

### 输出

- `cropped/<product-id>_original.png`
- `cropped/<product-id>_detection.png`
- `cropped/<product-id>_local_detection.png`
- `cropped/<product-id>_geometry_candidate_alpha.png`
- `geometry/<product-id>_cropped_geometry.json`
- `manifests/<product-id>_crop_manifest.json`

`geometry_candidate_alpha.png` 必须为单通道 L PNG，像素只含 `{0,255}`，并逐像素等于完整候选 Alpha 的 `crop(crop_box)`。

### `cropped_geometry` 合同

```json
{
  "schema_version": "vision-cropped-geometry-1.0",
  "product_id": "SY1537",
  "source_geometry_sha256": "<geometry-sha256>",
  "source_sha256": "<canonical-source-sha256>",
  "detection_image_sha256": "<full-global-detection-sha256>",
  "source_size": [3024, 4032],
  "crop_box": [400, 600, 2500, 3400],
  "crop_size": [2100, 2800],
  "coordinate_space": "crop-pixel",
  "coordinate_bounds": [0, 0, 2100, 2800],
  "primitives": [
    {
      "id": "product-envelope",
      "type": "ellipse",
      "semantic": "product",
      "params": [1112, 1416, 605, 1008],
      "touches_border": false
    }
  ],
  "uncertain_regions": [],
  "cropped_geometry_sha256": "<digest-excluding-this-field>"
}
```

转换规则：

- 先按 `2.1` 栅格规则把点、椭圆半径和线宽量化为完整源图像素参数，再从点和中心坐标减去 `crop_left/crop_top`。
- polygon/polyline 的点改为裁剪局部像素中心坐标。
- polyline 的 width 改为正整数像素。
- ellipse 和 ellipse_set 参数改为 `[center_x_px,center_y_px,radius_x_px,radius_y_px]`。
- 疑点区域 bbox 改为裁剪局部半开像素矩形。
- ID、semantic、reason 和真实 `touches_border` 声明原样保留。
- `cropped_geometry_sha256` 使用规范化 JSON 计算，排除 `cropped_geometry_sha256` 自身。
- 裁剪几何栅格器必须满足：

```text
rasterize(cropped_geometry)
==
rasterize(source_geometry).crop(crop_box)
```

两侧二值 Alpha 必须逐像素完全一致。阶段 4 以已保存的 `geometry_candidate_alpha.png` 为权威候选；`cropped_geometry` 用于审计，避免因再次量化产生差异。

### Crop Manifest 必填字段

- 源几何、规范化原图、完整全局检测图和完整局部检测图摘要
- `content_box`、`crop_box`、`crop_size`
- `target_max_occupancy=[77,100]`
- 两个轴的实际占比和 `source_limited_axes`
- 四个裁剪输出的路径、SHA-256、尺寸和模式
- `cropped_geometry` 路径、SHA-256
- 固定裁剪算法版本

### 门禁

- 三张裁剪图片尺寸一致，并分别与对应完整图片的 `crop(crop_box)` 逐像素一致。
- 裁剪候选 Alpha 不得丢失完整候选 Alpha 中位于源图内的保护像素。
- 非真实源图触边不得因坐标转换产生新的商品触边声明。
- Crop Manifest、`cropped_geometry` 和全部磁盘摘要一致。
- 失败时进入 `crop_failed`，不生成 Mask。

## 阶段 4：使用三路证据生成裁剪坐标系 Mask

### 输入

- `cropped_original`
- `cropped_detection`
- `cropped_local_detection`
- `geometry_candidate_alpha`
- `vision-cropped-geometry-1.0`
- Crop Manifest

### 三路边缘证据

三路证据固定为：

1. `original_rgb`：从 `cropped_original` 计算亮度边缘和 RGB 三通道色度边缘。
2. `global_robust`：直接读取 `cropped_detection`，不得重新生成。
3. `local_limited`：直接读取 `cropped_local_detection`，不得修改参数或重新生成。

`create_background_edit_mask.py` 是 `boundary-refinement-2.1` 的唯一权威实现。固定规则：

- 只允许在候选 Alpha 的边缘窄带内修改。
- 内侧半径为 `max(2, min(4, round(short_edge*0.002)))`。
- 外侧半径为 `max(3, min(6, round(short_edge*0.004)))`。
- 每路边缘阈值为 `max(3, P86)`。
- 同一像素至少获得三路中的两路支持才属于共识边缘。
- 候选核心、窄带外区域和图片边框必须冻结。
- 原图灰度动态范围小于 8、窄带为空、共识为空，或 `changed_pixels > max(1, Python3-round(band_pixels*0.95))` 时，使用原候选并记录 `boundary_refinement_fallback`。
- `boundary_refinement_fallback` 和任一冻结不变量失败都是技术阻断原因，用户确认不能覆盖。

### Mask 语义和输出

- Mask 为 RGBA PNG。
- RGB 三通道全部为 255。
- Alpha 只含 `{0,255}`。
- `alpha=255` 表示保护商品，`alpha=0` 表示允许编辑背景。

输出：

- `mask/<product-id>_product-protection-mask.png`
- `mask/<product-id>_editable-overlay.png`
- `logs/<product-id>_mask-assessment.draft.json`

Mask Assessment 至少记录：

- 三张裁剪输入、候选 Alpha、源几何、裁剪几何和 Crop Manifest 的路径与摘要
- 每路边缘算法、阈值和支持像素数
- `consensus_required=2`
- 全部边缘贴合不变量
- 商品保护占比、背景可编辑占比
- 已声明和未声明的边框接触
- 全部疑点区域 ID
- `technical_blockers`

### 技术门禁

- Mask 尺寸必须等于 `cropped_original`。
- 商品保护占比必须位于闭区间 `[0.005, 0.70]`。
- 未声明边框接触比例必须为 0。
- 每个 `touches_border=true` 的图元必须已在阶段 3 的完整源图候选 Alpha 上验证为真实接触源图边界，并把结果写入 Crop Manifest。
- `technical_blockers` 必须为空，才允许进入 Mask Hook。
- 疑点区域属于人工检查项，不因存在而使阶段 4 失败；但确认回执必须逐项解析。
- 技术门禁失败时进入 `mask_failed`。允许保留报告供排查，但不得创建确认回执或调用 Wawapi。

## 阶段 5：Mask 检查 Hook 与不可变确认

### Review bundle

展示前创建 `manifests/mask_review_bundle.json`，至少绑定：

- `run_id`、`product_id`、`base_token`、`table_id`、`record_id`
- `front_field_id`、`target_field_id`、`front_file_token`
- `product_context.json` 和 `raw_source`
- `source_original`、完整全局检测图、完整局部检测图
- 源几何、Crop Manifest
- `cropped_original`、两张裁剪检测图、裁剪几何、候选 Alpha
- 产品保护 Mask、叠加图、draft Mask Assessment
- 每项相对路径和 SHA-256；图片额外记录宽高
- `created_at`

Review bundle 原子发布后，上述资产在当前运行内全部冻结，状态进入 `awaiting_mask_confirmation`。

### 展示内容

- 未调色裁剪原图。
- 产品保护 Mask。
- 可编辑背景叠加图。
- 必要时展示全局或局部检测图作为识别辅助，但明确标注其不是 Wawapi 输入。

### 用户检查标准

- 商品是否完整位于裁剪范围内。
- 所有珠子、吊坠、链绳、串线、连接件和透明边缘是否被保护。
- 垫板、桌面、道具和大面积阴影是否保持可编辑。
- 是否存在产品漏保护或背景大面积误保护。
- 对每个 `uncertain_regions.id` 明确确认已检查并接受当前 Mask 处理。

### Mask 确认回执

用户肯定确认时：

1. 重新计算 review bundle 及全部绑定资产的摘要和尺寸。
2. 要求 `resolved_uncertain_region_ids` 恰好覆盖源几何中的全部疑点 ID，不得缺少或包含未知 ID。
3. 以“文件不存在才创建”的语义原子写入 `confirmations/mask_confirmation.json`。
4. 回执已存在时不得覆盖；重复或冲突确认失败。

回执至少包含：

- `schema_version`、`confirmation_id`；`confirmation_id` 为 32 位小写 UUID 十六进制
- `run_id`、`product_id`、`base_token`、`table_id`、`record_id`、`front_field_id`、`target_field_id`、`front_file_token`
- `decision="confirmed"`
- `confirmed_at`、`confirmed_by`；无平台用户 ID 时使用当前交互会话 ID
- `review_bundle_path`、`review_bundle_sha256`
- `resolved_uncertain_region_ids`
- review bundle 中全部资产的路径、SHA-256 和图片尺寸

回执不保存可独立修改的 `automatic_wawapi_edit_allowed`。

### 确认后重新门禁

- 重新执行 Mask 技术门禁并写入 `logs/<product-id>_mask-gate.confirmed.json`，不得覆盖 draft 报告。
- 只有“技术门禁通过 + 确认回执有效 + 全部疑点已解析”同时成立时，门禁报告才派生 `automatic_wawapi_edit_allowed=true`。
- `manifests/workflow_state.json` 必须引用确认回执路径和文件 SHA-256，随后状态才可推进为 `mask_confirmed`。
- 若进程在写回执后、更新状态前崩溃，恢复流程只能重新验证已有回执并推进状态，不得再次询问或覆盖回执。
- 任一资产变化、回执冲突或门禁无法重现时进入 `mask_failed`，Wawapi 调用次数为 0。
- 用户否定时进入终态 `mask_rejected`；修订几何、裁剪或 Mask 必须新建运行。

Mask 确认只授权进入 Wawapi 阶段，不等于最终效果确认或飞书上传授权。

## 阶段 6：Wawapi Edits、真实调用计数与安全重试

### 固定 Prompt

- 唯一构建入口为 `skills/jewelry-white-background/scripts/build_white_background_prompt.py`。
- Prompt 内容继续由 `references/white-background-prompt.md` 锁定，不得加入 SKU、材料、生肖或其他动态商品描述。
- `logs/<product-id>_prompt.txt` 使用 UTF-8、无 BOM、LF 换行和一个结尾 LF；发送给 API 的字符串必须是该文件全部内容。
- `prompt_sha256` 基于实际发送的 UTF-8 字节计算。

### 请求前门禁

- 当前状态必须为 `mask_confirmed`，且 `state_revision` 与调用方预期一致。
- `manifests/workflow_state.json` 引用的 Mask 确认回执必须存在且摘要正确。
- 回执的 `run_id`、`product_id`、`record_id` 必须与当前运行一致。
- 必须重新校验回执中的全部资产摘要、图片尺寸和确认后 Mask 门禁报告。
- `automatic_wawapi_edit_allowed` 必须由本次校验即时派生为 `true`。
- image 固定为回执绑定的 `cropped_original`。
- mask 固定为回执绑定的 `product_protection_mask`。
- 命令不得引用裁剪检测图、未裁剪原图、叠加图或其他参考图。
- 所有本地门禁在占用 call slot 前完成；本地失败时真实 POST 次数为 0。

### 固定请求身份

联网前生成并固化规范对象：

```json
{
  "provider": "wawapi",
  "operation": "edit",
  "endpoint": "<normalized-base-url>/v1/images/edits",
  "model": "<resolved-model>",
  "size": "<resolved-size>",
  "n": 1,
  "image_size": [2100, 2800],
  "mask_size": [2100, 2800],
  "image_sha256": "<cropped-original-sha256>",
  "mask_sha256": "<confirmed-mask-sha256>",
  "prompt_sha256": "<exact-prompt-bytes-sha256>"
}
```

示例中的 `2100 × 2800` 只是说明字段类型；运行时必须写入本次 image 和 mask 从磁盘重新读取的实际整数尺寸，且两者必须相等。禁止把示例尺寸、配置默认值或上一次运行尺寸写入请求身份。

规范对象的 SHA-256 记为 `request_identity`。

- `attempt_number`、call slot、输出目录、日志路径和 helper 本地 `task_id` 不得进入 `request_identity`。
- 本地 `task_id` 不是远端幂等键；流程不得假定 Wawapi 支持幂等提交。
- 所有自动重试必须使用完全相同的 `request_identity`。

### single-attempt 合同

- Wawapi 一次真实调用定义为开始执行一次 `POST /v1/images/edits`。
- single-attempt adapter 内部重试次数为 0，每次最多执行一次 POST。
- HTTP 客户端、helper 和外层 adapter 均不得启用隐藏重试或 provider fallback。
- 每次准备开始 POST 前，在运行锁内原子创建 `logs/edit-call-<n>.json`，初始状态为 `submitting`，并记录 call slot、`request_identity` 和 owner/心跳字段。
- call slot 一旦创建，保守地视为请求可能已发出；不得删除或回收。
- 每次运行最多创建 3 个 call slot。
- 恢复时发现 `submitting` 且 owner 仍 active，只返回 `already_in_progress`。只有证明 owner 已终止且心跳过期后，才进入 `edit_unknown`，不得创建下一 slot。

### 允许自动重试的结果

只有已经收到明确 HTTP 响应，且能证明服务端未接受本次 Edit 的以下结果允许再次 POST：

- HTTP 429，且响应不含 task/job ID 或可用成功结果。
- HTTP 5xx 只有在 provider 的正式响应合同提供机器可判定的“请求未被接受”标志、适配器验证该标志为真、且响应不含任何 task/job ID 时才可重试。

普通 5xx、缺少上述拒绝证据的 5xx，或携带 task/job ID 的 5xx 都进入 `edit_unknown`。仅凭状态码、空 body 或“看起来是服务端错误”不得推断请求未执行。

第 1 个可重试结果后持久化 `retry_not_before=响应时间+30秒`；第 2 个后持久化 `retry_not_before=响应时间+90秒`。恢复流程只在该时间到达后创建下一 slot。第 3 次调用后无论结果如何都停止。

### 不允许自动重试的结果

- POST 开始后超时、断连、连接重置、helper 异常退出或响应无法判定：`edit_unknown`。
- call slot 已创建但进程崩溃，无法证明请求未发出：`edit_unknown`。
- HTTP 400、401、403 等明确参数、认证或权限错误：`edit_failed`。
- HTTP 2xx 但响应为空、结构错误、下载失败或没有有效本地图片：`edit_failed`。
- 本地状态、身份、摘要或尺寸不符：不创建 call slot，`edit_failed`。
- 用户在创建 call slot 前取消：`cancelled`；创建后无法取得确定结果时为 `edit_unknown`。

`edit_failed` 和 `edit_unknown` 均不得自动重新提交，也不得进入排版。

### 有效生成结果

按响应顺序选择第一张同时满足以下条件的候选：

- 候选字节非空。
- 真实格式为 PNG 或 JPEG，后缀与真实格式一致。
- 宽高均至少为 16 像素，且总像素数不超过 `20,000,000`。
- Pillow `verify()` 成功，重新打开后完整 `load()` 成功。
- 先写入当前运行目录临时路径，再原子发布到 `edit/`。
- 发布后记录真实格式、尺寸、文件大小和 SHA-256。

只有生成结果和 `manifests/<product-id>_edit_result.json` 都已原子发布后，才能进入 `edit_completed`。取得 2xx 但无法形成有效文件时不得通过新 POST 获取另一个结果。

### Call 记录

每个 `edit-call-<n>.json` 至少包含：

- `call_slot`、`request_identity`
- `started_at`、`finished_at`
- `request_may_have_been_sent`
- `http_status`、`returncode`
- 经密钥脱敏的 `stdout`、`stderr`、`response_body`
- `result_status`、`retryable`、`retry_reason`、`next_retry_delay_seconds`
- 有效输出的路径、真实格式、尺寸和 SHA-256
- 不可删除的状态历史

最终状态只能为 `edit_completed`、`edit_failed` 或 `edit_unknown`。

## 阶段 7：只使用生成结果进行确定性排版

### 输入隔离

排版脚本公开接口只能接收：

- 已完成 Edit 的生成结果路径。
- 固定排版参数。
- 输出路径。

不得接收或读取：

- 生成前原图或裁剪原图。
- 任一检测图。
- Mask、叠加图。
- 源几何、裁剪几何或 Crop Manifest。

### `layout-algorithm-1.0`

1. 只对生成结果执行 EXIF 方向校正并转换为 sRGB RGB；若生成结果自身包含 Alpha，只允许把该结果合成到纯白底。
2. 在四角分别取边长 `max(8, floor(min(width,height)*0.05))` 的正方形区域。
3. 合并四个区域的像素，各 RGB 通道分别取中位数；`.5` 使用向上取整，得到画布背景色。
4. 计算每个像素与背景色的 RGB 欧氏距离。`distance>=18` 为强前景，`distance>=3` 为弱前景。
5. 强前景像素少于 `ceil(width*height*0.001)` 时进入 `layout_failed`。
6. 若弱前景覆盖率大于等于 `0.90`，使用强前景；否则对弱前景执行 8 邻域连通分析，只保留至少包含一个强前景像素的连通分量。
7. 保留后的前景为空，或 `strong_count/retained_count<0.60` 时进入 `layout_failed`。
8. 商品范围为保留前景的最小半开矩形。商品范围触及生成结果边界时进入 `layout_failed`，不得使用整图框兜底。
9. 最终画布固定为 `1536 × 2048`，目标商品宽度固定为 `round(1536*0.60)=922` 像素，目标中心固定为 `(768,900)`。
10. 宽度目标缩放比例为 `922/product_width`；以目标中心完整容纳商品的高度上限固定为 `2*min(900,2048-900)=1800` 像素。
11. 实际缩放比例为宽度目标比例与 `1800/product_height` 的较小值。因高度限制而缩小时记录 `scale_limited_by=["product_height_at_target_center"]`。
12. 固定有符号取整函数为 `round_signed(x)=floor(x+0.5)`（`x>=0`），否则为 `ceil(x-0.5)`。
13. 使用固定 LANCZOS 插值缩放完整生成结果；缩放后整图宽高分别为 `max(1,round_signed(source_dimension*scale))`。
14. 缩放后整图任一边小于 1 或总像素数超过 `20,000,000` 时进入 `layout_failed`，不得分配超限中间图。
15. 使用实际整数缩放尺寸推导 `scale_x=resized_width/source_width` 和 `scale_y=resized_height/source_height`。商品半开矩形转换为 `[floor(left*scale_x), floor(top*scale_y), ceil(right*scale_x), ceil(bottom*scale_y))`。
16. 半开矩形中心固定为 `((left+right)/2,(top+bottom)/2)`。期望粘贴坐标为 `desired_x=round_signed(768-product_center_x)`、`desired_y=round_signed(900-product_center_y)`。
17. 保证商品不被裁切的合法区间固定为 `x∈[-left,1536-right]`、`y∈[-top,2048-bottom]`；任一区间为空时进入 `layout_failed`。
18. 最终粘贴坐标分别为 `clamp(desired_x,x_min,x_max)` 和 `clamp(desired_y,y_min,y_max)`。x、y 独立计算，不存在方向优先级或平局选择。
19. 粘贴坐标允许为负数。允许画布裁掉完整生成结果矩形中位于画布外的背景区域，但不得裁掉商品范围。
20. 画布空白区域使用第 3 步背景色填充。
21. 本版本禁止锐化，manifest 固定记录 `sharpening=null`。

未受高度限制时，最终商品宽度必须为 `922 ± 2` 像素；商品中心相对 `(768,900)` 的单轴误差不得超过 1 像素。受限时商品宽度可以更小，但必须记录限制原因、实际比例和实际中心。

### 输出

- `layout/<product-id>_3x4_60pct.png`
- `manifests/<product-id>_layout_manifest.json`

### Layout Manifest 必填字段

- `layout_algorithm_version`
- 生成结果相对路径、SHA-256、格式和尺寸
- Edit Result Manifest 相对路径和 SHA-256
- 四角取样边长、取样范围和背景 RGB 中位数
- 强/弱阈值、像素数、覆盖率、连通规则和前景置信度
- 半开区间商品范围
- 目标缩放比例、实际缩放比例、`scale_limited_by`
- 插值、取整规则、缩放后整图尺寸和粘贴坐标
- 实际商品宽度、宽度占比和中心
- `whole_generated_result_resized=true`
- `pre_generation_reference_used=false`
- `pre_generation_mask_used=false`
- `mask_cutout_used=false`
- `sharpening=null`

上述布尔字段只用于审计，不能替代输入隔离测试。

## 阶段 8：最终效果 Hook 与不可变确认

### Review bundle

排版成功后创建 `manifests/final_review_bundle.json`，至少绑定：

- `run_id`、`product_id`、`base_token`、`table_id`、`record_id`、`target_field_id`
- Mask 确认回执、确认后 Mask 门禁报告和 Edit Result Manifest 的路径与 SHA-256
- 未加水印排版图的路径、SHA-256、尺寸
- Layout Manifest 路径和 SHA-256
- `created_at`

Review bundle 发布后，排版图和 Layout Manifest 在当前运行内冻结，状态进入 `awaiting_final_confirmation`。

### 展示内容

- 未加水印的最终排版图。

### 最终确认回执

用户肯定确认时：

1. 重新计算 review bundle、排版图和 Layout Manifest 摘要。
2. 生成唯一 `delivery_id`，格式为 32 位小写 UUID 十六进制。
3. 原子独占创建 `confirmations/final_confirmation.json`，不得覆盖。

回执至少包含：

- `schema_version`、`confirmation_id`、`delivery_id`；`confirmation_id` 为 32 位小写 UUID 十六进制
- `run_id`、`product_id`、`base_token`、`table_id`、`record_id`、`target_field_id`
- `decision="confirmed"`、`confirmed_at`、`confirmed_by`
- `review_bundle_path`、`review_bundle_sha256`
- 排版图相对路径、SHA-256、宽高
- Layout Manifest 相对路径和 SHA-256
- `authorization.watermark=true`
- `authorization.feishu_append=true`

回执只授权其绑定的对象。状态文件引用回执路径与摘要后，状态进入 `final_confirmed`。

若进程在写回执后、更新状态前崩溃，恢复流程只能校验已有回执并推进状态，不得创建第二个 `delivery_id`。已有回执无效或与状态冲突时进入 `final_invalid`。用户否定时进入 `final_rejected`；重新生成或重新排版必须新建运行。

最终肯定确认同时表示接受当前排版、允许固定水印、允许向当前记录的当前目标字段追加附件。确认后不得再次询问上传权限。

## 阶段 9：水印和飞书一次性追加

### 交付前门禁

- 当前状态必须为 `final_confirmed`。
- 最终确认回执、review bundle、排版图和 Layout Manifest 的身份与摘要必须重新校验。
- `delivery_id`、`base_token`、`table_id`、`record_id`、`target_field_id` 必须与状态文件一致。
- 任一变化进入 `final_invalid`，不得水印或上传。

### 水印脚本合同

正式流程只允许单输入、显式目标文件模式：

```text
python watermark_images.py
  --input <confirmed-layout-absolute-path>
  --output <exact-watermarked-absolute-path>
  --product-id <product-id>
  --workers 1
```

目标文件名固定为：

```text
<safe-product-id>__<run-id>__<delivery-id>__watermarked.png
```

合同要求：

- 必须恰好解析出 1 个输入和 1 个目标文件。
- `--output` 必须位于当前运行的 `watermarked/`，且身份与最终确认回执一致。
- 目标文件调用前必须不存在；存在时返回非零，禁止覆盖。
- 不得通过 glob、目录扫描或“最新文件”寻找结果。
- 脚本先写同目录临时文件，完整保存并关闭后原子发布到 `--output`。
- 零输入、多输入、输入不存在、目标已存在、保存失败或原子发布失败均返回非零。
- 退出码 0 只表示本次调用原子发布了恰好一个目标；批处理模式当前“零 job 返回 0”的行为不适用于正式流程。
- 外层流程不得重新打开或解码水印图。
- 外层流程必须执行且只执行以下非图像检查：调用前目标不存在、退出码为 0、精确目标路径是非空普通文件、读取字节成功并计算 SHA-256。
- 目标缺失、为空或摘要读取失败时进入 `watermark_failed`，不得上传。
- 恢复时发现状态为 `watermarking` 且 owner 仍 active，只返回 `already_in_progress`。只有证明 owner 已终止且心跳过期后，才可检查唯一目标；目标已原子发布且通过上述非图像检查时推进 `upload_ready`，否则进入 `watermark_failed`，不得重新运行脚本。

### 唯一飞书写接口

正式流程唯一允许的写命令为：

```text
lark-cli base +record-upload-attachment
  --base-token <base-token>
  --table-id <table-id>
  --record-id <record-id>
  --field-id <target-field-id>
  --file <absolute-watermarked-path>
  --format json
  --as user
```

- 每次交付只有一个 `--file`。
- 该命令的服务端语义必须是“上传并追加附件”。
- 禁止使用 record update、先读取附件数组再覆盖写回，或组合其他接口模拟追加。
- 自动流程在上传前后都不得调用 record list、record get、附件列表或字段读取接口。
- CLI 内部的大文件分片不视为新的应用层上传授权。

### 上传 claim 和回执

- 网络调用前，在运行锁内以“文件不存在才创建”的语义原子创建 `manifests/upload_receipt.<delivery-id>.json`，初始状态为 `uploading`，并记录 owner/心跳字段。
- 首次 claim 至少绑定 `run_id`、`delivery_id`、最终确认回执 SHA-256、`base_token`、`table_id`、`record_id`、`target_field_id`、目标文件路径、文件名、文件 SHA-256 和 claim 时间。
- 回执已存在时，无论状态为何，当前进程都不得再次调用上传命令；该文件同时充当跨进程 claim。
- 获得 CLI 输出后，先把原始响应和进程结果原子写入回执，并把回执状态推进为 `response_recorded`，再解析。
- 命令中的 `--field-id`、最终确认回执的 `target_field_id` 和响应查找键必须是同一值。
- 成功只接受 `data.attachments[record_id][target_field_id]` 中按完整目标文件名唯一匹配出的非空 `file_token`。
- 禁止兼容读取 `data.file_token`、`data.file_tokens`、数组首项或其他响应形状。
- 唯一匹配成功写 `completed`。
- 取得结构化、明确的失败响应写 `failed`。
- CLI 启动后超时、非零退出但无明确失败 JSON、无 JSON、响应截断、零匹配、多匹配或空 token 均写 `unknown`。
- `failed`、`unknown` 和经存活判定确认 owner 已终止的遗留 `uploading` 均禁止自动重传。
- 恢复时发现 `response_recorded`，只允许基于已保存响应继续本地解析，不得再次调用 CLI。
- 工作流状态分别进入 `completed`、`upload_failed` 或 `upload_unknown`。
- 写入终态后结束，自动流程不回读飞书。

### 人工恢复

- 只允许在用户明确要求后执行。
- 人工恢复可以读取本地回执、调用日志，并按需人工查看飞书目标字段；这是自动流程“不回读”的唯一例外。
- 已确认附件存在时，可人工把原回执和工作流解析为 `completed`，但必须追加操作者、时间、证据和 `file_token`，不得删除原 claim、原响应或历史状态。
- 无法确认是否上传成功时保持 `upload_unknown`。
- 已确认未上传或明确失败后，如用户要求再次上传，必须新建 `run_id`、重新完成最终确认并生成新 `delivery_id`；不得复用旧文件名、回执或授权。

## 状态机

所有迁移都必须执行 `expected_status + expected_state_revision` 校验。除表后明确列出的用户取消规则外，下表未列出的跳转全部禁止。

| 当前状态 | 事件与门禁 | 下一状态 |
|---|---|---|
| `run_created` | 源图固化成功 | `source_ready` |
| `run_created` | 源图固化失败 | `source_failed` |
| `source_ready` | 检测资产成功 | `detection_image_ready` |
| `source_ready` | 检测资产失败 | `detection_failed` |
| `detection_image_ready` | `2.1` 几何成功 | `geometry_ready` |
| `detection_image_ready` | 几何失败 | `geometry_failed` |
| `geometry_ready` | 裁剪和裁剪几何成功 | `crop_ready` |
| `geometry_ready` | 裁剪失败 | `crop_failed` |
| `crop_ready` | Mask 技术门禁通过 | `mask_ready` |
| `crop_ready` | Mask 技术门禁失败 | `mask_failed` |
| `mask_ready` | review bundle 已发布并展示 | `awaiting_mask_confirmation` |
| `awaiting_mask_confirmation` | 确认回执持久化且重新门禁通过 | `mask_confirmed` |
| `awaiting_mask_confirmation` | 用户否定 | `mask_rejected` |
| `awaiting_mask_confirmation` | 确认冲突、资产变化或门禁无法重现 | `mask_failed` |
| `mask_confirmed` | 请求前门禁失败且未创建 call slot | `edit_failed` |
| `mask_confirmed` 或任一 `edit_retry_wait_*` | 下一 call slot claim 已存在且 owner active | 保持当前状态；当前进程无操作退出 |
| `mask_confirmed` 或任一 `edit_retry_wait_*` | 下一 call slot claim 已存在，owner 已终止且心跳过期 | `edit_unknown` |
| `mask_confirmed` | 原子创建 call slot 1 | `edit_attempt_1` |
| `edit_attempt_1` | 有效结果已原子发布 | `edit_completed` |
| `edit_attempt_1` | 明确 429，或有机器证据证明未接受的 5xx | `edit_retry_wait_1` |
| `edit_retry_wait_1` | 30 秒到达并创建 call slot 2 | `edit_attempt_2` |
| `edit_attempt_2` | 有效结果已原子发布 | `edit_completed` |
| `edit_attempt_2` | 明确 429，或有机器证据证明未接受的 5xx | `edit_retry_wait_2` |
| `edit_retry_wait_2` | 90 秒到达并创建 call slot 3 | `edit_attempt_3` |
| `edit_attempt_3` | 有效结果已原子发布 | `edit_completed` |
| 任一 `edit_attempt_*` | 可判定且不可重试，或第 3 次明确失败 | `edit_failed` |
| 任一 `edit_attempt_*` | submitting claim 的 owner 仍 active | 保持当前状态；当前进程无操作退出 |
| 任一 `edit_attempt_*` | 请求结果无法判定，或 owner 已终止且 submitting claim 心跳过期 | `edit_unknown` |
| `edit_completed` | 排版成功 | `layout_completed` |
| `edit_completed` | 排版失败 | `layout_failed` |
| `layout_completed` | final review bundle 已发布并展示 | `awaiting_final_confirmation` |
| `awaiting_final_confirmation` | 最终确认回执持久化且校验通过 | `final_confirmed` |
| `awaiting_final_confirmation` | 用户否定 | `final_rejected` |
| `awaiting_final_confirmation` | 已有最终确认回执无效或冲突 | `final_invalid` |
| `final_confirmed` | 交付前身份变化 | `final_invalid` |
| `final_confirmed` | 已存在 active 水印 claim | 保持 `final_confirmed`；当前进程无操作退出 |
| `final_confirmed` | 水印 claim 的 owner 已终止且心跳过期 | 只检查唯一目标并进入 `upload_ready` 或 `watermark_failed` |
| `final_confirmed` | 开始水印 | `watermarking` |
| `watermarking` | 水印 owner 仍 active | 保持 `watermarking`；当前进程无操作退出 |
| `watermarking` | 唯一目标成功发布并完成非图像检查 | `upload_ready` |
| `watermarking` | 水印失败，或 owner 已终止且恢复时无有效唯一目标 | `watermark_failed` |
| `upload_ready` | 原子创建上传回执成功 | `uploading` |
| `upload_ready` | 已存在 completed 回执 | `completed` |
| `upload_ready` | 已存在 failed 回执 | `upload_failed` |
| `upload_ready` | 已存在 response_recorded 回执 | `uploading`，只恢复本地解析 |
| `upload_ready` | 已存在 active uploading 回执 | 保持 `upload_ready`；当前进程无操作退出 |
| `upload_ready` | 已存在 unknown，或 uploading owner 已终止且心跳过期 | `upload_unknown` |
| `uploading` | 上传 owner 仍 active | 保持 `uploading`；当前进程无操作退出 |
| `uploading` | 唯一 token 匹配成功 | `completed` |
| `uploading` | 接口明确失败 | `upload_failed` |
| `uploading` | 响应无法确定，或 owner 已终止且心跳过期且无完整响应 | `upload_unknown` |

`source_failed`、`detection_failed`、`geometry_failed`、`crop_failed`、`mask_failed`、`mask_rejected`、`edit_failed`、`edit_unknown`、`layout_failed`、`final_rejected`、`final_invalid`、`watermark_failed`、`upload_failed`、`upload_unknown`、`cancelled` 和 `completed` 均为自动终态，没有自动出边。

用户取消规则：

- Wawapi call claim 和上传 claim 创建前的非终态可以进入 `cancelled`。
- Wawapi call claim 创建后无法取得确定结果时只能进入 `edit_unknown`。
- 上传 claim 创建后发生取消或进程中断时只能进入 `upload_unknown`。

人工恢复是唯一例外：

| 当前自动终态 | 人工事件与证据 | 下一状态 |
|---|---|---|
| `upload_failed` 或 `upload_unknown` | 用户明确要求核验，且人工确认当前 `delivery_id` 的唯一目标文件已存在于授权字段 | `completed` |

该迁移必须在上传回执中追加 `manual_resolution`，记录操作者、时间、证据、完整目标文件名和 `file_token`；不得执行新上传或删除原终态历史。

## 运行目录与文件契约

```text
<output_root>/<safe-product-id>/<run-id>/
├── product_context.json
├── source/
│   ├── <product-id>_raw.bin
│   └── <product-id>_original.png
├── detection/
│   ├── <product-id>_geometry_detection.png
│   └── <product-id>_local_detection.png
├── geometry/
│   ├── <product-id>_vision_geometry.json
│   └── <product-id>_cropped_geometry.json
├── cropped/
│   ├── <product-id>_original.png
│   ├── <product-id>_detection.png
│   ├── <product-id>_local_detection.png
│   └── <product-id>_geometry_candidate_alpha.png
├── mask/
│   ├── <product-id>_product-protection-mask.png
│   └── <product-id>_editable-overlay.png
├── edit/
│   └── <first-valid-result>.<png|jpg|jpeg>
├── tmp/
│   └── e-<8位随机十六进制>/
├── layout/
│   └── <product-id>_3x4_60pct.png
├── watermarked/
│   └── <safe-product-id>__<run-id>__<delivery-id>__watermarked.png
├── confirmations/
│   ├── mask_confirmation.json
│   └── final_confirmation.json
├── logs/
│   ├── <product-id>_detection_manifest.json
│   ├── <product-id>_mask-assessment.draft.json
│   ├── <product-id>_mask-gate.confirmed.json
│   ├── <product-id>_prompt.txt
│   ├── edit-call-<n>.json
│   └── edit-call-<n>.json.lock
└── manifests/
    ├── <product-id>_crop_manifest.json
    ├── <product-id>_edit_result.json
    ├── <product-id>_layout_manifest.json
    ├── mask_review_bundle.json
    ├── final_review_bundle.json
    ├── watermark_receipt.<delivery-id>.json
    ├── watermark_receipt.<delivery-id>.json.lock
    ├── upload_receipt.<delivery-id>.json
    ├── upload_receipt.<delivery-id>.json.lock
    ├── workflow.lock
    └── workflow_state.json
```

- `product_context.json`、全部 manifest、确认回执、call 记录和上传回执都必须记录与目录一致的 `run_id`。
- 确认回执不得覆盖或改写。
- 进入 `watermarking` 时创建水印回执；该回执记录 claim、命令、退出码、目标路径、文件字节数和 SHA-256，不得包含图片解码结果。
- `manifests/workflow.lock` 是唯一运行级状态迁移锁，不得用于 claim 心跳或回执 CAS；各 `<claim-file>.lock` 只保护对应 claim。所有锁文件都不属于业务资产，不进入确认摘要；运行根目录不得出现同名 `workflow.lock` 或 `workflow_state.json`。
- 上传回执更新时保留不可变 `claim`、原始响应和 `history`。
- 不得扫描其他 run 或 SKU 目录寻找输入、回退文件或“最新结果”。

## 唯一正式编排器与拟修改文件

### 新增 `skills/jewelry-white-background/scripts/run_white_background_workflow.py`

- 作为唯一正式编排器，负责运行目录、状态、恢复、两次确认、Wawapi call slot、水印和上传 claim。
- 所有阶段恢复和状态跳转都通过该编排器。
- 各阶段脚本不得自行跳过确认或直接进入后续阶段。
- 编排器只调用无内部重试的 single-attempt Wawapi adapter。

### `skills/jewelry-white-background/SKILL.md`

- 更新为本规格的唯一顺序、两次 Hook、确认回执、输入隔离和自动交付语义。
- 明确历史 `2.0` 只读，新运行只使用 `2.1`。
- 明确水印成功后无图片复检，自动流程上传后不回读。

### `skills/jewelry-white-background/scripts/prepare_mask_detection_images.py`

- 将 report schema 升级为 `mask-detection-images-2.0`。
- 固化全局和局部检测输出身份、算法参数、路径、尺寸与模式。
- 使用临时文件和原子发布，拒绝覆盖已有输出。

### `skills/jewelry-white-background/scripts/create_background_edit_mask.py`

- 为新流程增加严格 `vision-geometry-mask-2.1` 和 `vision-cropped-geometry-1.0` 支持。
- 移除新流程对几何内嵌 `mask_review` 的依赖。
- 接收已裁剪三路证据和候选 Alpha，不在裁剪图上重新解释完整源图几何。
- 输出 draft assessment 和确认后派生门禁报告。
- 旧 `2.0` loader 只能由明确的 legacy 只读路径调用。

### 新增 `skills/jewelry-white-background/scripts/create_geometry_crop.py`

- 在完整源图坐标系栅格化候选 Alpha。
- 按 `77/100` 公式计算非方形裁剪框。
- 同步裁剪四路资产，生成 `cropped_geometry` 和 Crop Manifest。
- 执行像素一致性和栅格等价门禁。

### `scripts/yuan_image_generation_adapter.py`

- 增加明确的 single-attempt Wawapi Edit 接口。
- 内部重试次数为 0，最多执行一次 POST。
- 返回可判定 HTTP 状态、请求是否可能已发出和有效输出身份。
- 输出文件后缀必须与真实格式一致。

### 新增 `skills/jewelry-white-background/scripts/run_wawapi_edit_with_retry.py`

- 只负责编排最多 3 个持久化 call slot 和 30/90 秒等待。
- 只对明确 429，或有机器证据证明请求未被接受的 5xx 创建下一 slot。
- `edit_unknown`、本地失败和不可重试结果立即终止。

### 新增 `skills/jewelry-white-background/scripts/layout_generated_result.py`

- 公开接口只接收生成结果、固定参数和输出路径。
- 实现 `layout-algorithm-1.0`。
- 禁止访问运行目录中的生成前图片和 Mask。
- 输出排版图和完整 Layout Manifest。

### 新增 `skills/jewelry-white-background/scripts/deliver_confirmed_result.py`

- 只接受有效 `final_confirmed`。
- 调用单文件水印模式，执行允许的非图像检查。
- 创建唯一上传 claim，并只调用一次飞书附件追加命令。
- 不在水印后解码图片，不在上传前后回读飞书。
- 统一处理 Windows 扩展长度路径。

### `C:\Users\Administrator\.codex\skills\yuanyuan-ruyi-watermark\scripts\watermark_images.py`

- 增加单文件 `--output` 模式。
- 单文件模式要求恰好一个 job，零 job 不得成功退出。
- 使用临时文件和原子发布，拒绝覆盖已有目标。
- 保留原批处理接口供其他调用方使用；正式珠宝流程不得从批处理目录推断输出。

### `run_jewelry_base_pipeline.py`

- 保留必要的旧 CLI 参数兼容，但业务执行必须委托唯一正式编排器。
- 删除或禁用直接调用旧 Edit、水印和飞书上传的业务路径。
- 无法安全映射的旧参数明确报错，不得静默执行旧流程。

### `scripts/run_sy1537_sy1552_white_background.py`

- 改为兼容入口并委托唯一正式编排器。
- 不再维护独立生成、重试、确认或交付状态机。
- 旧 `--retry` 不得绕过 `edit_unknown`、人工确认或 3 个 call slot 上限。

### 测试和触发描述

- 扩展现有 Skill 合同、检测、Mask、Prompt、adapter 和旧入口测试。
- 新增裁剪、编排器、排版输入隔离、水印单文件、并发和崩溃恢复测试。
- 更新 `agents/openai.yaml` 前先检查触发描述是否与新流程冲突；只有不一致时才重新生成。

## 测试规格

### 运行身份、源图与检测资产

- 强制两个进程使用同一 `run_id` 时，只有一个目录创建成功，第一个目录内容不变。
- 在 staging 的 `manifests/` 写入初始状态后、原子重命名前模拟崩溃，最终 `<run-id>/` 不得出现；下一次运行生成新 `run_id`，不得复用孤儿 staging 目录。
- 发布后只允许存在 `manifests/workflow_state.json` 和 `manifests/workflow.lock`；运行根目录同名文件必须不存在。
- `SKU.`、包含 `..` 的 ID、`CON`、`CON.txt`、`AUX`、`COM1` 和 `LPT9.png` 均在创建目录前被拒绝。
- 同一 SKU 连续运行产生不同目录，全部 manifest 和回执中的 `run_id` 与目录一致。
- 原始附件摘要与规范化 PNG 摘要分别校验，`source_sha256` 只能等于后者。
- 固定小图对 `global_robust`、`local_limited` 执行像素级 golden test。
- 检测图与原图尺寸一致，云端模型只收到 `geometry_detection`。
- 任一检测资产被删除或修改时，后续门禁失败。

### 几何与裁剪

- 新编排器明确拒绝 `vision-geometry-mask-2.0`，补字段伪迁移也失败。
- 各 primitive 的合法、退化、重复 ID、非法触边和疑点 bbox 均有边界测试。
- 精确验证 `ceil(content*100/77)`、奇数像素分配、边界夹紧和 `source_limited`。
- 非方形商品不会被强制裁为方形。
- 三张图片和候选 Alpha 使用同一 `crop_box`。
- `cropped_original` 与规范化原图对应区域逐像素一致。
- `rasterize(cropped_geometry)` 与完整几何栅格裁剪逐像素一致。
- 裁剪 manifest 和磁盘摘要任一冲突时不生成 Mask。

### Mask 与确认回执

- 三路边缘输入、阈值、2/3 共识和冻结不变量使用 golden test 固定。
- `boundary_refinement_fallback`、比例越界、未声明触边或声明触边缺失时不会进入 Hook。
- Mask 与 `cropped_original` 同尺寸，RGBA/RGB/Alpha 语义严格符合合同。
- 未确认、确认文件缺失、回执摘要错误或疑点未全部解析时，Wawapi POST 次数为 0。
- 修改 review bundle 中任一绑定文件后，旧确认立即失效。
- 两个进程并发确认时只有一个原子创建成功。
- 回执创建后、状态更新前模拟崩溃，恢复流程复用原回执并只推进一次状态。
- 用户否定后当前运行终止；不得在旧目录内修改 Mask 后再次展示。

### 状态机与终态

- 每次迁移都拒绝错误的 `expected_status` 或 `state_revision`。
- `edit_failed`、`edit_unknown` 不进入排版。
- `watermark_failed` 不进入上传。
- 所有终态均无自动出边。
- 状态文件在任意写入点崩溃后仍是旧版或完整新版 JSON，不得出现截断 JSON。
- 取消发生在 Wawapi/上传 claim 前后时分别进入合同规定的不同状态。
- 第一进程持有 active Wawapi、水印或上传 claim 时启动第二进程，第二进程只返回 `already_in_progress`，状态和外部调用次数都不变。
- claim 创建后、state 更新前分别模拟 Wawapi、水印和上传崩溃；恢复流程识别已有 claim，禁止重建或重复外部调用。
- 让心跳线程与响应/终态写入并发，验证 `record_revision` CAS 使响应、终态和 history 不会被迟到心跳覆盖。
- 验证状态迁移只使用 `manifests/workflow.lock`，每个 claim 更新只使用自身 `<claim-file>.lock`；并发更新不同 claim 时不得共享锁文件。
- 已有无效最终确认回执时，`awaiting_final_confirmation` 必须进入 `final_invalid`。
- `upload_failed/upload_unknown → completed` 只允许在显式人工核验事件下发生，并保留原终态历史。

### Wawapi 真实调用与未知结果

- single-attempt adapter 的 transport POST 次数最多为 1，内部重试次数为 0。
- 第 1 次成功时真实 POST 次数为 1。
- 明确 429，或带“请求未被接受”证据的 5xx 后成功时，真实 POST 次数分别为 2 或 3。
- 普通 5xx、缺少拒绝证据的 5xx 或携带 task/job ID 的 5xx 进入 `edit_unknown`，不存在下一次 POST。
- 连续 3 个明确可重试响应后不存在第 4 次 POST。
- POST 后超时、断连、helper 崩溃、响应不可判定或遗留 submitting claim 时进入 `edit_unknown`，transport 不再被调用。
- HTTP 2xx 但无有效候选时进入 `edit_failed`，不得新建 Edit 请求。
- 三个 call slot 的 `request_identity` 完全相同，但日志路径和 slot 不同。
- `request_identity.image_size` 和 `mask_size` 必须等于本次磁盘文件的实际尺寸；不同裁剪尺寸的 SKU 产生不同请求身份。
- 测试直接统计假 transport 的 POST 次数，不能只统计 helper 启动次数。
- 使用虚拟时钟或可注入 sleep，不真实等待 30/90 秒。
- 空文件、截断图片、格式与后缀不符、只能 verify 不能完整 load 的候选均无效。
- 任一边小于 16 像素或总像素超过 20MP 的候选无效，不得进入逐像素排版分析。
- 第一张无效、第二张有效时只原子发布第二张。

### 生成结果排版

- 排版函数签名没有原图、裁剪图、几何或 Mask 参数。
- 隔离临时目录只提供生成结果；对业务资产文件读取设置 allowlist，除该生成结果外，读取任何图片、几何 JSON、Crop Manifest 或其他运行资产都立即失败。
- 在常见旧路径放置内容不同的诱饵原图、Mask、源几何、裁剪几何和 Crop Manifest，输出及 Layout Manifest 保持不变。
- 相同输入连续运行两次，商品范围、缩放比例、粘贴坐标和输出像素完全一致。
- 无强前景、置信度不足或商品范围触边时进入 `layout_failed`。
- 计算出的完整缩放中间图超过 20MP 时进入 `layout_failed`，不得尝试分配超限图像。
- 输出严格为 `1536 × 2048`。
- 未受高度限制时商品宽度为 `922 ± 2`，中心单轴误差不超过 1 像素。
- 受限时必须记录 `scale_limited_by`、实际宽度和实际中心。
- 验证实际缩放的是完整生成结果，未创建产品 Alpha，`sharpening=null`。

### 最终确认、水印和飞书

- 最终确认前水印和上传调用次数均为 0。
- 修改排版图、Layout Manifest、Edit Result Manifest、Mask 确认回执、记录或字段后，旧最终确认失效，水印和上传调用次数为 0。
- 并发最终确认只生成一个 `delivery_id`。
- 水印正式模式零输入、多输入、目标已存在、保存失败或零 job 时均返回非零。
- 水印退出码 0 时目标必须由本次调用原子发布，不能复用旧同名文件。
- 水印回执更新必须保留不可变 claim 和完整 history；不得通过重写回执删除首次 owner 身份。
- 水印后外层流程不调用 Pillow、尺寸检查或视觉检查；只执行路径、普通文件、非空和 SHA-256 检查。
- 恢复 `watermarking` 时不会再次运行水印脚本。
- 两个进程并发处理同一 `delivery_id` 时，只有一个进程创建上传 claim，飞书命令总调用次数严格为 1。
- 已存在 `failed`、`unknown` 或 `uploading` 回执时，重启流程的上传调用次数为 0。
- 飞书命令严格为一次 `base +record-upload-attachment` 且只有一个 `--file`。
- 断言命令 `--field-id`、最终确认回执 `target_field_id` 和响应查找键三者完全相同。
- 禁止调用 record update、record list、record get 或上传后回读。
- 零 token、多 token、空 token、非标准响应、CLI 超时或响应截断进入 `upload_unknown`。
- 人工恢复不得复用原 `delivery_id` 再次上传。

### 旧入口迁移

- 两个旧入口都委托唯一正式编排器。
- 使用 AST 或调用 spy 证明旧入口不再直接调用 Wawapi helper、水印脚本或飞书上传命令。
- 任一旧入口都无法执行“Mask 后直接 Edit”或“生成后直接水印上传”的旧顺序。
- 旧 `--retry` 无法绕过 `edit_unknown`、确认回执或 call slot 上限。

### 大图和批次约束

- 规范化图或检测图超过 20MP 时在云端调用前失败。
- 多 SKU 批次中，云端视觉每次请求只包含一张图片。
- 禁止把整批原图、检测图、Mask 或最终图一次提交给模型。
- 每个 SKU 使用独立运行目录、状态、确认回执和上传 claim。

## 验收标准

### A 级：离线合同验收

必须完成，且不得调用真实 Wawapi 或写入飞书：

- 全部单元测试、合同测试、并发测试、崩溃恢复测试和旧入口迁移测试通过。
- 假 transport 证明 Wawapi 真实 POST 上限、未知状态和无内部重试。
- 假 lark-cli 证明唯一追加命令、单次调用和禁止回读。
- 水印单文件合同、排版确定性和输入隔离测试通过。
- Skill 校验通过，`SKILL.md` 与 `agents/openai.yaml` 的触发描述一致。

A 级通过只能表述为“离线合同验收通过”，不能表述为“联网或端到端验收通过”。

### B 级：Wawapi 联网验证

仅在用户针对独立测试 SKU 单独授权后执行：

- 使用正式 Wawapi endpoint 完成一次 Mask 确认后的真实 Edit。
- 保存真实 call 记录和有效输出身份。
- 完成确定性排版并由用户检查最终效果。
- 本级不写入飞书。

B 级通过只能表述为“Wawapi 联网验收通过，飞书写入未验收”。

### C 级：端到端交付验收

仅在用户再次明确授权测试 SKU 写入飞书后执行：

- 测试前由用户在目标字段保留一个可识别的哨兵附件。
- 从最终确认状态自动执行一次水印和一次附件追加。
- 自动流程按规定不回读飞书。
- 流程结束后由用户在飞书客户端人工确认哨兵附件仍存在，且唯一目标文件名的新附件已经追加；人工检查不触发程序自动重传。
- 保存最终本地回执和本次授权身份。

只有 A、B、C 三级均通过，才能表述为“端到端上线验收通过”。验收报告必须写明实际达到的最高级别和未执行级别。

### 功能验收不变量

- 正式顺序严格为：检测增强 → `2.1` 几何 → 确定性裁剪 → 三路 Mask → Mask Hook → Wawapi → 生成结果排版 → 最终 Hook → 水印 → 飞书。
- 云端几何输入是全局色阶图，Wawapi 图片输入是未经色阶处理的裁剪原图。
- Mask 未确认或确认失效时真实 Wawapi POST 次数为 0。
- Wawapi 总 POST 不超过 3 次，且只对明确 429 或可证明未被接受的 5xx 自动重试。
- 排版无法访问生成前原图、检测图、几何或 Mask。
- 最终确认后不再询问上传权限。
- 水印成功后不解码或查看水印图，只执行规定的非图像检查。
- 飞书上传最多调用一次，只追加、不替换、不删除，自动流程上传后不回读。
- 历史运行和 `2.0` 资产保持只读。

## 实施顺序

1. 先新增失败测试，覆盖运行目录、摘要语义、原子状态、并发 claim 和旧入口迁移。
2. 升级检测 manifest 和 `vision-geometry-mask-2.1` loader，明确 `2.0` 只读边界。
3. 实现完整几何栅格、确定性裁剪、裁剪几何和像素等价门禁。
4. 修改 Mask 脚本接收三路裁剪证据和候选 Alpha，拆分技术门禁与人工确认。
5. 实现 review bundle、两类不可变确认回执和状态恢复。
6. 增加 single-attempt adapter，再实现最多 3 个 call slot 和 30/90 秒等待。
7. 实现只读生成结果的确定性排版。
8. 增加水印单文件模式、非图像检查和一次性飞书上传 claim。
9. 建立唯一正式编排器，并让两个旧入口只做参数兼容和委托。
10. 更新 Skill 文档和必要的触发描述。
11. 完成 A 级离线验收；B、C 级必须分别取得用户单独授权后执行。

实施过程中不得用真实 Wawapi 或飞书写入代替单元测试。任何真实联网或写入验证都必须使用独立测试 SKU、明确授权和独立运行目录。
