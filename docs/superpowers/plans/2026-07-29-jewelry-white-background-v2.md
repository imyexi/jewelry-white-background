# 珠宝白底图 V2 实施计划

> **给执行代理：** 逐项执行并保持测试先行；本计划不授权任何飞书写回。

**目标：** 为珠宝白底图技能增加参考计划、V2 提示词构建、双阶段 QC 和可审计的本地金丝雀。当前实现只覆盖本地校验、提示词构建、生成图 QC 与审计门禁；不授权飞书写回。

**架构：** `reference_plan.json` 经过纯 Python 校验后成为提示词构建和 QC 的唯一输入。生成图片先经过水印前 QC，再由既有水印脚本处理，并在忽略水印区域后做第二次 QC；审计记录决定是否有资格请求飞书追加授权。

**技术栈：** Python 3、标准库、Pillow、unittest、Yuan Image Generation v4 图片生成 helper、Yuan Studio 水印脚本。

## 全局约束

- 所有用户可见文档与提示词使用中文。
- `3:4`、`2K` 仅作为 Yuan v4 Wawapi Generation 参数，不写入 V2 提示词。
- 不自动裁切、缩放或重排生成图。
- 任何飞书附件追加都必须在单独的显式授权后执行，并保留原附件。
- SY1462 本轮仅做本地金丝雀，审计值固定为 `not_authorized`。

---

### 任务 1：先定义 V2 契约测试

**文件：**

- 新建：`skills/jewelry-white-background/tests/test_reference_plan.py`
- 新建：`skills/jewelry-white-background/tests/test_prompt_contract.py`
- 新建：`skills/jewelry-white-background/tests/test_image_qc.py`
- 新建：`skills/jewelry-white-background/tests/fixtures/sy1462-reference-plan.json`

**产出接口：** `validate_plan(plan, base_dir=None)`、`build_prompt(plan)`、`evaluate_image(image_path, policy)`。

- [x] 编写失败测试：缺少正面图、越界构图阈值、非实物特殊件和无效材质观察必须被拒绝。
- [x] 运行失败测试并确认缺失校验行为被捕获；新增注入、细节图越权、泛化配件和材质纠偏越权反例。
- [x] 编写失败测试：V2 prompt 必须只出现七个固定标题，且不能包含渲染参数、视觉特效词与旧配件词库。
- [x] 编写失败测试：合成的居中产品图通过；过宽、偏心、触边、低置信度透明产品和浅色触边信号分别产生失败或人工复核。

### 任务 2：实现参考计划与提示词构建

**文件：**

- 新建：`skills/jewelry-white-background/scripts/reference_plan.py`
- 新建：`skills/jewelry-white-background/scripts/validate_reference_plan.py`
- 新建：`skills/jewelry-white-background/scripts/build_white_background_prompt.py`
- 修改：`skills/jewelry-white-background/references/white-background-prompt.md`

**产出接口：** `load_plan(path)`、`validate_plan(plan, base_dir=None)`、`build_prompt(plan)`；两个 CLI 均输出 UTF-8 JSON 或 UTF-8 prompt 文件。

- [x] 用最小实现通过任务 1 的计划和 prompt 测试。
- [x] 为计划字段增加单行/标题/渲染参数拦截，并让材质观察以 `source_image` 追溯到正面图或已声明的细节图。
- [x] 为 SY1462 夹具写入已确认的合金牛造型珠、仅由正面图决定的结构说明和逐颗纹理观察语句。

### 任务 3：实现生成图 QC 与审计结构

**文件：**

- 新建：`skills/jewelry-white-background/scripts/evaluate_white_background.py`
- 修改：`skills/jewelry-white-background/tests/test_image_qc.py`

**产出接口：** CLI 输出包含 `metrics`、`checks`、`decision` 和 `requires_human_review` 的 JSON；允许的决定为 `pass`、`review`、`fail`。

- [x] 用 Pillow 前景阈值实现宽度、中心、触边和置信度检测；水印后检查支持忽略底部区域。
- [x] 对浅色或透明前景增加保守的弱信号延展检测：深色主体正常时，浅色触边仍进入人工复核而非自动通过。
- [x] 将 QC CLI 的 `review` 和 `fail` 分别改为非零退出码，并让审计决策由两份 QC、人工复核和用户授权记录推导。
- [x] 用现有生成图进行只读试跑；已有 SY1462 本地金丝雀均因构图 QC 被阻断，未生成可上传候选。

### 任务 4：修订技能并执行 SY1462 本地金丝雀

**文件：**

- 修改：`skills/jewelry-white-background/SKILL.md`
- 修改：`skills/jewelry-white-background/agents/openai.yaml`
- 修改：`C:\Users\Administrator\.codex\skills\jewelry-white-background\SKILL.md`
- 修改：`C:\Users\Administrator\.codex\skills\jewelry-white-background\agents\openai.yaml`
- 新建：`outputs/sy1462-v2-<timestamp>/reference_plan.json`
- 新建：`outputs/sy1462-v2-<timestamp>/audit.json`

- [x] 将技能全文修订为 V2 流程，移除相互矛盾的旧规则，并明确模型 prompt 只能由构建脚本产生。
- [x] 完成基础元数据和目录结构校验；`quick_validate.py` 依赖当前环境未安装的 PyYAML，因此不安装依赖，改用等价的前置元数据检查。
- [x] 从既有 SY1462 来源图生成三次本地 V2 金丝雀；当时的运行证据保持不变，当前活动流程已统一由 Yuan v4 Wawapi Generation 传递画幅和分辨率，未向飞书写回。
- [x] 对每次本地金丝雀生成 QC 与审计；三次均为 `blocked_by_qc`，未执行水印或飞书命令。
- [x] 已同步最终技能正文与运行文件到安装目录；完整单元测试 `37/37`、基础元数据校验、已安装技能运行时契约和 `git diff --check` 均通过。生成结果仍为三次 SY1462 本地候选均被 QC 阻断，未执行写回。
