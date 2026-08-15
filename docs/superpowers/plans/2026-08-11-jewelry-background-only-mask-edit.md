# 珠宝白底图语义 Mask Edit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将正式白底图流程改为云端视觉语义几何、本地色阶增强边缘贴合和原图 Wawapi Mask Edit，覆盖手串、吊坠、项链及非圆环珠宝；同时把生成与授权交付拆成可审计、不可预授权、不可重放的两个阶段。

**Architecture:** 云端视觉逐图输出绑定源图身份的归一化几何 JSON；权威 Skill 脚本验证、栅格化并只在候选边界窄带内使用检测增强副本贴边；审核同时绑定源图、规范化几何与最终 Mask；项目入口只把摘要仍匹配的未经色阶处理原图 PNG 与最终 Mask 交给 Wawapi。视觉草稿固定为 review，技术门禁和人工 Mask 审核后才允许联网。生成完成只写 `status=awaiting_authorization`；交付必须匹配具体结果的完整身份，先写版本化 `uploading` 意图回执，单次上传后从内存响应中按目标文件名唯一解析并进入终态。

**Tech Stack:** Python 3.13、Pillow、标准库 `dataclasses/hashlib/json`、pytest、Yuan Image Generation v4、Wawapi、PowerShell、lark-cli。

## Global Constraints

- 所有说明、报告和文档使用中文；schema 标识与机器原因码可保留英文。
- 大量图片逐张或小批处理，不一次提交给云端视觉模型。
- 色阶增强只用于本地 Mask 检测，Wawapi 只接收原图、最终 Mask 和固定 Prompt。
- 白底入口固定 `provider=wawapi`、`operation=edit`、单 image、单 mask，不使用 Generation、其他 provider、fallback 或自动重试。
- Mask 必须同尺寸 RGBA PNG、RGB 全 255、Alpha 仅 `{0,255}`。
- 生成阶段不接受上传授权；授权只能针对已存在的具体生成结果，禁止预授权未来图片。
- 完整交付身份必须绑定 `workflow_mode`、`product_id`、`record_id`、当前首图 `front_file_token` 与 `front_file_name`、五项 Mask 身份摘要（`product_id`、`source_sha256`、`geometry_sha256`、`prepared_sha256`、`mask_sha256`）、`generated_image_sha256`、水印 `local_file` 绝对路径、`local_file_sha256`、`target_filename`、`field_id`；所有来源中的完整交付身份完全相同才可继续。
- 上传只调用一次；调用上传接口前先写版本化 `status=uploading` 意图并绑定完整交付身份，收到任意响应先持久化原始响应，再明确写为 `status=completed`、`status=failed` 或 `status=unknown`；所有非完成或不确定状态均禁止自动重传。
- 通用 `upload_receipt.json` 仅作兼容索引，不得作为防重依据；只有版本化回执的完整交付身份完全相同且状态完成时才能阻止重放。
- `ok: true` 只在 `data.attachments[record_id][field_id]` 中按 `target_filename` 唯一匹配并取得非空 token 后成立，不得取数组首项。
- 上传后只解析内存响应并写回执，立即终止；不回读飞书、不扫描附件、目录或图片。
- 不回滚或覆盖当前工作区已有修改。

---

### Task 1: 固化视觉几何与检测隔离契约

**Files:**
- Modify: `skills/jewelry-white-background/tests/test_background_edit_mask.py`
- Modify: `tests/test_create_background_edit_mask.py`
- Modify: `skills/jewelry-white-background/tests/test_skill_contract.py`

**Interfaces:**
- Produces: `load_vision_geometry(profile_path, product_id) -> VisionGeometryProfile`
- Produces: `create_background_edit_assets(..., geometry_profile_path, product_id) -> MaskAssessment`
- Guarantees: Edit 底图未经色阶处理；检测参数不进入 Wawapi 命令。

- [x] 写入 schema、SHA-256、尺寸、坐标范围、非圆环图元、检测隔离和真实触边声明的失败测试。
- [x] 运行 `python -m pytest skills/jewelry-white-background/tests/test_background_edit_mask.py tests/test_create_background_edit_mask.py skills/jewelry-white-background/tests/test_skill_contract.py -q`，确认测试因新接口和契约尚不存在而失败。
- [x] 将旧合成圆环自动推断测试改成几何 profile 驱动，并保留同尺寸、白色 RGB、二值 Alpha、20MP 与路径安全测试。

### Task 2: 实现确定性视觉几何栅格化

**Files:**
- Modify: `skills/jewelry-white-background/scripts/create_background_edit_mask.py`
- Modify: `scripts/create_background_edit_mask.py`

**Interfaces:**
- Consumes: `vision-geometry-mask-2.0` JSON，图元类型为 `polygon/polyline/ellipse/ellipse_set`。
- Produces: `rasterize_vision_geometry(profile, size) -> Image.Image` 二值 Alpha。
- Produces: 源图身份、图元数量、触边声明和不确定区域的审计数据。

- [x] 实现 profile dataclass、schema/哈希/尺寸/坐标/ID 校验和四类图元的归一化栅格化。
- [x] 删除正式调用链中的 `_infer_ring_geometry`、`_ring_band`、`estimate_foreground` 和环带 CLI 回退；项目 wrapper 只导出新接口。
- [x] 运行 Task 1 定向测试，确认非圆环几何和资产契约通过。

### Task 3: 实现色阶增强边缘贴合

**Files:**
- Modify: `skills/jewelry-white-background/scripts/create_background_edit_mask.py`
- Modify: `skills/jewelry-white-background/tests/test_background_edit_mask.py`

**Interfaces:**
- Produces: `refine_candidate_boundary(image, candidate_alpha) -> tuple[Image.Image, BoundaryRefinementReport]`
- Guarantees: 只修改候选边界窄带；检测图只在内存中存在；失败时逐字节回退候选 Alpha。

- [x] 先增加外扩候选收回、内缩候选补足、透明浅色珠、细绳、窄带外强投影、触边冻结、低对比回退、非二值候选拒绝和确定性测试。
- [x] 实现原图、P1/P99 温和全局亮度拉伸和受限局部对比度三路检测证据；固定 69/204 不进入默认路径。
- [x] 实现按短边缩放的候选边界窄带、跨版本边缘支持、距离约束、安全外扩和不变量校验。
- [x] 报告记录实际黑白点、裁剪比例、支持版本数、窄带与改动像素、回退原因。
- [x] 运行 Mask 定向测试，确认召回、误保护、回退和检测隔离契约通过。

### Task 4: 更新门禁与项目编排

**Files:**
- Modify: `skills/jewelry-white-background/scripts/create_background_edit_mask.py`
- Modify: `scripts/run_sy1537_sy1552_white_background.py`
- Modify: `run_jewelry_base_pipeline.py`
- Modify: `tests/test_run_sy1537_sy1552_white_background.py`
- Modify: `tests/test_project_generation_migration.py`

**Interfaces:**
- Consumes: `MaskAssessment(status, reasons, automatic_wawapi_edit_allowed, ...)`。
- Allows: 有明确 `touches_border=true` 语义声明且连续的真实商品触边。
- Rejects: 未声明触边、退化或近空 Mask、未解决不确定区域、垫板/投影误保护、过期审核和非 `ok` 报告。

- [x] 写入视觉草稿固定 review、审核后才 ok、真实链绳触边可通过、中心被吊坠覆盖不失败的测试。
- [x] 更新 `assess_mask`，移除通用中心可编辑门禁，区分已声明商品触边与未知触边。
- [x] 让两个项目入口显式提供每款几何 profile；只有 `status=ok` 且 `automatic_wawapi_edit_allowed=true` 时允许调用 Wawapi。
- [x] 增加命令边界测试，断言适配层只收到 `prepared` 原图和最终 Mask，且参数不含色阶或检测图。
- [x] 运行项目编排定向测试。
- [x] 按 TDD 增加并修复审核未绑定几何/Mask、混合触边绕过、退化图元、近空保护区、透明调色板 PNG 和本地裁剪比例审计反例。
- [x] 报告写入五项身份摘要；专项与通用入口在联网前重算 prepared/Mask 摘要，拒绝报告后的资产替换。
- [x] Adapter 按响应顺序寻找第一张可完整解码的 PNG/JPEG，所有候选无效才失败。
- [x] 专项交付固定单次上传，只用本地 product context 定位记录，并在上传前复核 state/report/磁盘资产身份。
- [ ] 把生成与交付状态分离：生成入口无论是否收到外部授权参数都只产生 `status=awaiting_authorization`；授权模式只接受已有具体结果，缺少 state 时直接拒绝。
- [ ] 在 context/state 中补齐 `front_file_token`、`front_file_name`，使完整交付身份覆盖 workflow、商品、记录、首图、五项 Mask 身份、生成摘要、水印绝对路径与摘要、目标文件名和字段。
- [ ] 水印入口预先计算唯一绝对输出路径并只验证该路径，删除通过目录 `glob` 或 mtime 选择结果的行为。
- [ ] 实现版本化 `upload_receipt.<target-stem>.json` 防重；通用 `upload_receipt.json` 仅作兼容索引，不得作为防重依据。
- [ ] 实现写前 `uploading` 意图、收到任意响应立即保存原始响应，以及 `completed/failed/unknown` 终态；遗留或冲突状态禁止自动重传。
- [ ] 成功解析只遍历 `data.attachments[record_id][field_id]` 并按 `target_filename` 唯一匹配非空 token，不得取数组首项。
- [ ] 增加专项与通用入口回归测试：无 state 的预授权、无授权复用 `awaiting_authorization`、首图 token/name 错配、完整身份任一字段错配、同内容不同版本回执、`uploading/failed/unknown`、无响应、嵌套响应零/一/多匹配，以及上传后无远端读取/本地扫描。

### Task 5: 全文修订正式 Skill

**Files:**
- Modify: `skills/jewelry-white-background/SKILL.md`
- Modify: `skills/jewelry-white-background/references/white-background-prompt.md`
- Modify: `skills/jewelry-white-background/agents/openai.yaml`
- Modify: `skills/jewelry-white-background/tests/test_skill_contract.py`

**Interfaces:**
- Documents: 云端视觉逐图识别、几何 JSON、本地色阶检测隔离、Mask 审核、Wawapi 与飞书终止契约。

- [x] 先更新 Skill 压力测试，禁止环形推断、纯边缘全图识别、检测副本入模和人工直接放行 review。
- [x] 全文重写 Skill，修正旧的“可见阴影必须保护”“商品不得触边”“中心必须可编辑”等矛盾规则。
- [x] 保持固定 Prompt 的白底 Edit 语义，确认不包含产品参数或色阶参数。
- [x] 全文写清 `uncertain_regions[].id/bbox/reason`、`mask_review.resolved_uncertain_regions`、逐项审核、重跑门禁和 `review/ok` 状态语义。
- [x] 全文写清 producer、非退化几何、`geometry_sha256/mask_sha256` 审核绑定、`0.5%..70%` 保护占比、逐像素触边覆盖和单次上传契约。
- [x] 先新增结果级授权、完整交付身份、真实嵌套响应唯一匹配、写前意图、终态与版本化防重的 Skill 契约测试，并记录旧文档 RED。
- [x] 全文修订 Skill 与设计文档，使 `status=awaiting_authorization`、禁止预授权、`data.attachments[record_id][field_id]`、`target_filename` 唯一匹配和回执状态机语义一致。
- [x] 运行 Skill 契约测试并取得 GREEN；`quick_validate.py` 留到最终验证阶段使用已配置的依赖运行时执行。

### Task 6: 三款样例与全量验证

**Files:**
- Verify: `outputs/jewelry-white-background/vision-geometry-mask-poc-20260812-v1`
- Verify: project and Skill tests

**Interfaces:**
- Samples: `SY1505`、`SY1506`、`SY1549`。

- [x] 使用修复后的正式栅格器逐张重跑 `SY1505`、`SY1506`、`SY1549`，更新报告与叠加图。
- [x] 核对三款叠加图：吊坠、连接件、链绳、透明珠、串线和金属件处于保护区，垫板、桌面、投影和环内背景保持可编辑。
- [x] 确认三款仍因未解决疑点和缺少正式审核保持 `review`，`automatic_wawapi_edit_allowed=false`，未调用在线接口。
- [x] 删除通用入口中无人消费的预处理 JPEG 支路及已删除的全局白底 Skill 路径；源图直接进入身份与 Mask 资产门禁。
- [x] 运行定向测试、`python -m pytest -q` 和 Skill 校验。
- [x] 静态扫描正式入口，确认不存在 Generation、环带正式路径、色阶图 Wawapi 参数、上传后回读或附件扫描。
- [x] 本轮不重复调用 Wawapi、不加水印、不上传飞书；保留此前三次成功实验作为接口效果证据。

### Task 7: 固化授权交付与版本化回执状态机

**Files:**
- Modify: `run_jewelry_base_pipeline.py`
- Modify: `scripts/run_sy1537_sy1552_white_background.py`
- Modify: `tests/test_project_generation_migration.py`
- Modify: `tests/test_run_sy1537_sy1552_white_background.py`
- Modify: `skills/jewelry-white-background/tests/test_skill_contract.py`

**Interfaces:**
- Produces: 无授权生成 state `status=awaiting_authorization`。
- Consumes: 只针对已存在具体结果的授权。
- Produces: `upload_receipt.<target-stem>.json`，状态为 `uploading/completed/failed/unknown`。
- Extracts: `data.attachments[record_id][field_id]` 中按 `target_filename` 唯一匹配的非空 `file_token`。

- [ ] 先写失败测试，覆盖禁止预授权未来图片、`awaiting_authorization` 无授权重跑不再调用 Wawapi、完整身份逐字段错配与确定水印路径。
- [ ] 写失败测试，覆盖同内容不同版本回执、完成回执防重、遗留 `uploading`、明确 `failed`、不确定 `unknown` 和损坏回执一律停止。
- [ ] 写失败测试，覆盖真实嵌套成功响应的零匹配、唯一匹配、多匹配、空 token 和数组首项为其他文件。
- [ ] 实现生成/交付分离与完整身份验证；禁止在授权模式下现场生成未来结果。
- [ ] 实现写前意图与响应先落盘；确保上传命令发生异常或响应解析不确定时留下 `unknown` 证据，且后续运行不自动重传。
- [ ] 实现上传后的硬终止边界：只解析函数已持有的内存响应和写回执，不调用飞书读取，不做本地目录/附件/图片扫描。
- [ ] 运行两个入口的定向测试、Skill 契约测试、全量测试与 `git diff --check`；只有全部通过才勾选本任务。
