# SY1537-SY1552 白底图与统一生图入口实施计划

> **执行要求：** 必须使用 `superpowers:executing-plans` 在当前任务中逐项实施，并对代码变更使用 `superpowers:test-driven-development`。所有步骤用复选框跟踪。

**目标：** 将白底图项目的活动生图入口统一迁移到 Yuan Image Generation v4 的 Wawapi Generation，并为 SY1537-SY1552 生成、质检、加水印和追加主图。

**架构：** 项目通过两个本地 Skill 目录联接固定运行时入口；`scripts/yuan_image_generation_adapter.py` 封装同步 Wawapi 调用并返回本地图片路径；既有脚本和新的标品库批处理管线只依赖该适配层。批处理管线分为准备、生成与 QC、加水印与上传三个阶段，所有写回均串行并回读验证。

**技术栈：** Python 3、标准库、Pillow、unittest、lark-cli Base shortcut、Yuan Image Generation v4、现有珠宝白底图 V2 校验/QC 脚本、Yuan Studio 水印脚本。

## 全局约束

- 所有思考、输出、代码注释、日志摘要和文档使用中文。
- 活动源码、运行脚本和技能说明统一使用 Yuan Image Generation v4，并显式选择 `wawapi` 与 `generation`。
- 不改写 `outputs/`、`output/` 中的历史运行产物、审计、回执或 Git 历史。
- 不读取、打印、复制或提交真实 API Key；凭据只由 v4 helper 按既有规则使用。
- 每款最多提交 5 张参考图，正面图必须排第 1 张；每次请求只处理一款产品。
- 16 款分 4 批处理，同一批最多并行 2 个生成请求；同一 Base 表附件上传必须串行。
- 自动 QC 不通过时只允许修订参考计划后再生成 1 次，不自动裁切、缩放或重排产品。
- 只有双阶段 QC、人工复核和用户授权均满足时才追加主图；始终保留原附件并回读验证。

---

### 任务 1：建立 v4 运行时注册与统一适配层

**文件：**

- 创建：`.agents/skills/yuan-image-generation`，目录联接到已验证的 v4 注册目录
- 创建：`.agents/skills/jewelry-white-background`，目录联接到 `skills/jewelry-white-background`
- 创建：`scripts/yuan_image_generation_adapter.py`
- 创建：`tests/test_yuan_image_generation_adapter.py`

**接口：**

- `GenerationResult(provider: str, task_id: str, helper_output: Path, delivered_output: Path, payload: dict)`
- `build_generate_command(prompt: str, images: Sequence[Path], output_dir: Path, python_executable: Path | str = sys.executable, helper_path: Path = DEFAULT_HELPER) -> list[str]`
- `parse_generation_payload(raw: str) -> dict`
- `generate_to_path(prompt: str, images: Sequence[Path], output_path: Path, log_path: Path, runner: Callable = subprocess.run) -> GenerationResult`

- [ ] **步骤 1：编写适配层失败测试**

```python
def test_build_generate_command_uses_wawapi_generation_and_preserves_image_order():
    command = build_generate_command(
        "白底商品图",
        [Path("front.jpg"), Path("detail.jpg")],
        Path("generated"),
        python_executable="python",
        helper_path=Path("helper.py"),
    )
    assert command[:4] == ["python", "helper.py", "generate", "--provider"]
    assert command[4:8] == ["wawapi", "--operation", "generation", "--prompt"]
    assert command.count("--image") == 2
    assert command.index("front.jpg") < command.index("detail.jpg")
    assert command[command.index("--aspect-ratio") + 1] == "3:4"
    assert command[command.index("--resolution") + 1] == "2K"
```

```python
def test_generate_to_path_copies_first_local_output_and_writes_log(tmp_path):
    provider_file = tmp_path / "provider.png"
    provider_file.write_bytes(b"png")
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({
            "provider": "wawapi",
            "task_id": "image-1",
            "status": "completed",
            "output": [{"path": str(provider_file)}],
        }),
        stderr="",
    )
    result = generate_to_path(
        "白底图",
        [tmp_path / "front.jpg"],
        tmp_path / "final.png",
        tmp_path / "generate.json",
        runner=lambda *args, **kwargs: completed,
    )
    assert result.delivered_output.read_bytes() == b"png"
    assert json.loads((tmp_path / "generate.json").read_text("utf-8"))["status"] == "completed"
```

- [ ] **步骤 2：运行测试确认 RED**

运行：`python -m unittest discover -s tests -p "test_yuan_image_generation_adapter.py" -v`

预期：因适配层模块不存在而失败。

- [ ] **步骤 3：实现最小适配层**

实现固定的 Wawapi Generation 命令、最多 5 张图片校验、helper JSON 解析、非零退出处理、本地输出存在性检查、日志写入和目标文件复制。日志只保存 provider 结果和错误，不写入命令行凭据。

- [ ] **步骤 4：建立并验证两个目录联接**

```powershell
New-Item -ItemType Directory -Force '.agents\skills' | Out-Null
New-Item -ItemType Junction `
  -Path '.agents\skills\yuan-image-generation' `
  -Target 'C:\Users\Administrator\Documents\珠宝上手图片生成\.agents\skills\yuan-image-generation'
New-Item -ItemType Junction `
  -Path '.agents\skills\jewelry-white-background' `
  -Target 'C:\Users\Administrator\Documents\珠宝白底图生成\skills\jewelry-white-background'
```

验证：两个 `Get-Item` 结果的 `LinkType` 为 `Junction`，目标分别指向上述目录；v4 helper 的 SHA-256 为 `5692CC9546C0436052B0A199CBCE4FBD3258F75122FE64C11202D219BF246509`。

- [ ] **步骤 5：运行测试确认 GREEN 并提交**

运行：`python -m unittest discover -s tests -p "test_yuan_image_generation_adapter.py" -v`

提交：

```powershell
git add scripts/yuan_image_generation_adapter.py tests/test_yuan_image_generation_adapter.py
git commit -m "feat: 添加 Yuan v4 生图适配层"
```

---

### 任务 2：全文迁移白底图 Skill 契约

**文件：**

- 修改：`skills/jewelry-white-background/SKILL.md`
- 修改：`skills/jewelry-white-background/agents/openai.yaml`
- 修改：`skills/jewelry-white-background/tests/test_skill_contract.py`

**接口：** Skill 第 4 步固定调用注册后的 `yuan_image_helper.py generate`，参数为 `--provider wawapi --operation generation --aspect-ratio 3:4 --resolution 2K`，参考图按正面图、细节图顺序重复传入 `--image`。

- [ ] **步骤 1：新增失败契约测试**

```python
def test_skill_uses_project_v4_wawapi_generation_only(self):
    text = SKILL_PATH.read_text(encoding="utf-8")
    forbidden = ("aireiter" + "-image-generation", "aireiter" + "_image_helper.py")
    self.assertIn("yuan-image-generation", text)
    self.assertIn("--provider wawapi", text)
    self.assertIn("--operation generation", text)
    self.assertTrue(all(token not in text for token in forbidden))
```

- [ ] **步骤 2：运行定向测试确认 RED**

运行：`python -m unittest discover -s skills/jewelry-white-background/tests -p "test_skill_contract.py" -v`

预期：当前 Skill 仍描述旧版异步调用，因此失败。

- [ ] **步骤 3：全文修订 Skill 和元数据**

重写生成步骤和故障处理，不在文末追加第二套流程；保留参考计划、七段式 prompt、双阶段 QC、水印和上传门禁。明确 v4 基础 Skill 不做内容 QC，珠宝业务 QC 在外层执行。

- [ ] **步骤 4：运行技能完整测试并提交**

运行：`python -m unittest discover -s skills/jewelry-white-background/tests -v`

提交：

```powershell
git add skills/jewelry-white-background/SKILL.md skills/jewelry-white-background/agents/openai.yaml skills/jewelry-white-background/tests/test_skill_contract.py
git commit -m "docs: 迁移珠宝白底图技能到 Yuan v4"
```

---

### 任务 3：迁移项目活动脚本并增加退化防护

**文件：**

- 修改：`run_jewelry_base_pipeline.py`
- 修改：`scripts/fresh_lark_qy029_qy034_qy008.py`
- 修改：`scripts/rerun_qy006_qy008_color_fix.py`
- 修改：`scripts/rerun_sensitive_eval.py`
- 创建：`tests/test_project_generation_migration.py`

**接口：** 上述脚本统一导入 `generate_to_path`，不再自行提交任务、轮询状态或下载 provider URL；业务清单中的 `task_id` 取自 `GenerationResult.task_id`，生成文件路径取自 `GenerationResult.delivered_output`。

- [ ] **步骤 1：编写活动文件扫描失败测试**

```python
ACTIVE_FILES = (
    ROOT / "run_jewelry_base_pipeline.py",
    ROOT / "scripts/fresh_lark_qy029_qy034_qy008.py",
    ROOT / "scripts/rerun_qy006_qy008_color_fix.py",
    ROOT / "scripts/rerun_sensitive_eval.py",
    ROOT / "skills/jewelry-white-background/SKILL.md",
)

def test_active_files_use_only_the_project_adapter():
    forbidden = ("aireiter" + "-image-generation", "aireiter" + "_image_helper.py")
    for path in ACTIVE_FILES:
        text = path.read_text(encoding="utf-8")
        assert all(token not in text for token in forbidden), path
        if path.suffix == ".py":
            assert "yuan_image_generation_adapter" in text, path
```

- [ ] **步骤 2：运行测试确认 RED**

运行：`python -m unittest discover -s tests -p "test_project_generation_migration.py" -v`

- [ ] **步骤 3：逐文件迁移同步生成逻辑**

删除旧 helper 常量、动态导入、异步提交、轮询和 URL 下载函数；调用：

```python
generation = generate_to_path(
    prompt,
    preprocessed_paths,
    generated_path,
    logs_dir / f"{product_id}_generate.json",
)
```

保持原有产品列表、参考图下载、提示词内容、水印、QC 和上传逻辑不变。旧清单字段若必须兼容，保留字段名但把远程 URL 写为 `None`，并新增 `provider="wawapi"` 和 `helper_output_path`。

- [ ] **步骤 4：运行迁移测试、语法检查并提交**

运行：

```powershell
python -m unittest discover -s tests -p "test_project_generation_migration.py" -v
python -m py_compile run_jewelry_base_pipeline.py
Get-ChildItem scripts\*.py | ForEach-Object { python -m py_compile $_.FullName }
```

提交活动脚本与测试，不添加现有无关 JSON 文件。

---

### 任务 4：实现标品库分阶段批处理管线

**文件：**

- 创建：`scripts/run_sy1537_sy1552_white_background.py`
- 创建：`tests/test_run_sy1537_sy1552_white_background.py`

**接口：**

- `TARGET_PRODUCT_IDS: tuple[str, ...]`，严格为 SY1537-SY1552
- `find_target_records() -> list[ProductRecord]`
- `select_reference_attachments(record: ProductRecord) -> list[Attachment]`
- `prepare_product(record: ProductRecord, run_root: Path) -> PreparedProduct`
- `generate_and_evaluate(product_root: Path) -> ProductResult`
- `watermark_and_upload(product_root: Path, record: ProductRecord) -> UploadReceipt`
- CLI 子命令：`prepare`、`generate`、`deliver`、`verify`
- `generate` 接受可重复的 `--product-id` 和 `--workers 1|2`，只处理显式传入的产品；未传时处理全部 16 款。

- [ ] **步骤 1：编写记录范围与参考图顺序失败测试**

```python
def test_target_ids_are_exact_contiguous_range():
    assert TARGET_PRODUCT_IDS == tuple(f"SY{number}" for number in range(1537, 1553))

def test_reference_selection_places_front_first_and_caps_at_five():
    record = fake_record(front=["front"], side=["side1", "side2"], product_images=["p1", "p2", "p3"])
    selected = select_reference_attachments(record)
    assert [item.token for item in selected] == ["front", "p1", "p2", "p3", "side1"]
```

- [ ] **步骤 2：编写上传幂等与保留原附件失败测试**

```python
def test_upload_is_skipped_when_same_filename_already_exists():
    before = [{"name": "SY1537_watermarked.png", "file_token": "old"}]
    decision = plan_upload(before, "SY1537_watermarked.png")
    assert decision.action == "already_present"

def test_verify_append_requires_original_tokens_to_remain():
    before = [{"file_token": "a"}, {"file_token": "b"}]
    after = [{"file_token": "a"}, {"file_token": "b"}, {"file_token": "new"}]
    assert verify_append(before, after, "new") is True
```

- [ ] **步骤 3：运行测试确认 RED**

运行：`python -m unittest discover -s tests -p "test_run_sy1537_sy1552_white_background.py" -v`

- [ ] **步骤 4：实现准备阶段**

使用 `lark-cli base +record-search` 按产品编号逐条定位记录，最小投影读取产品编号、名称、产品正面图、产品图片、产品侧视图和主图。下载附件后调用现有预处理脚本，生成每款 `product_context.json`、预处理 manifest 和参考图联系表。发现缺少记录、重复记录或缺少正面图时立即停止生成阶段，并在总清单中记录。

- [ ] **步骤 5：实现生成、QC、水印与上传阶段**

生成阶段要求已存在且通过校验的 `reference_plan.json`，由现有构建器生成七段式 prompt，再通过适配层生成。水印前 QC、人工复核、水印后 QC 和用户授权均写入各自 JSON。上传前后读取主图字段，按文件名幂等追加并验证原 token 集合仍为 after token 集合的子集。

- [ ] **步骤 6：运行测试和 dry-run 并提交**

运行：

```powershell
python -m unittest discover -s tests -p "test_run_sy1537_sy1552_white_background.py" -v
python scripts\run_sy1537_sy1552_white_background.py --help
python scripts\run_sy1537_sy1552_white_background.py prepare --dry-run
```

提交管线脚本和测试。

---

### 任务 5：准备 16 款真实参考资料与参考计划

**文件：**

- 生成：`outputs/jewelry-white-background/base-sy1537-sy1552-20260806/`
- 每款生成：`product_context.json`、`reference_plan.json`、参考图、预处理图、联系表和 manifest

- [ ] **步骤 1：运行真实准备阶段并验证 16 条唯一记录**

运行：

```powershell
python scripts\run_sy1537_sy1552_white_background.py prepare
```

验收：准备清单正好包含 16 个目标编号，每个编号唯一，每款至少 1 张正面图，提交候选图不超过 5 张。

- [ ] **步骤 2：按四个批次目视读取参考图**

每次只查看 SY1537-SY1540、SY1541-SY1544、SY1545-SY1548 或 SY1549-SY1552 中的一款联系表；不一次载入 16 款原图。逐款记录可见珠序、串线、特殊件、颜色、通透度、内含物和纹理，不凭材料名称添加不可见效果。

- [ ] **步骤 3：逐款写入并校验参考计划**

运行全部产品目录：

```powershell
$runRoot = 'outputs\jewelry-white-background\base-sy1537-sy1552-20260806'
Get-ChildItem $runRoot -Directory | Where-Object Name -Match '^SY15(3[7-9]|4[0-9]|5[0-2])$' | ForEach-Object {
  python skills\jewelry-white-background\scripts\validate_reference_plan.py `
    --reference-plan (Join-Path $_.FullName 'reference_plan.json') `
    --check-files
  if ($LASTEXITCODE -ne 0) { throw "参考计划校验失败：$($_.Name)" }
}
```

验收：16 份计划全部通过，结构字段只依赖正面图，材质观察的 `source_image` 可追溯到已声明参考图。

---

### 任务 6：分批生成、双阶段 QC 与一次重试

**文件：**

- 更新：本轮输出目录中的 `logs/`、`generated/`、`qc/`、`manifests/`

- [ ] **步骤 1：按 4 批运行生成阶段**

每批命令显式传产品编号，批内并发设为 2：

```powershell
python scripts\run_sy1537_sy1552_white_background.py generate --workers 2 --product-id SY1537 --product-id SY1538 --product-id SY1539 --product-id SY1540
python scripts\run_sy1537_sy1552_white_background.py generate --workers 2 --product-id SY1541 --product-id SY1542 --product-id SY1543 --product-id SY1544
python scripts\run_sy1537_sy1552_white_background.py generate --workers 2 --product-id SY1545 --product-id SY1546 --product-id SY1547 --product-id SY1548
python scripts\run_sy1537_sy1552_white_background.py generate --workers 2 --product-id SY1549 --product-id SY1550 --product-id SY1551 --product-id SY1552
```

确认 helper 日志中的 provider 均为 `wawapi`，且每次只有一款、最多 5 张参考图。

- [ ] **步骤 2：执行水印前自动 QC**

对每款运行现有 `evaluate_white_background.py`，保留 `pass/review/fail` 原始结果，不修改生成图构图。

- [ ] **步骤 3：按四个批次人工复核**

联系表仅包含该款正面图、最多 4 张补充参考图和生成图。逐款检查结构、特殊件、材质和额外内容；写入 `manual_review.json`。

- [ ] **步骤 4：对不合格款最多重试一次**

只修改 `reference_plan.json` 中可观察的结构或材质成功标准，重新运行校验、prompt 构建、v4 生成和水印前 QC。第二次仍不合格则标记 `blocked`，不得进入上传阶段。

- [ ] **步骤 5：对合格款加水印并做水印后 QC**

水印固定为 `YUAN STUDIO` 与 `PN SYxxxx`，生成副本后使用 `--stage post-watermark --ignore-bottom-ratio 0.15` 再次检查。

---

### 任务 7：串行追加主图并回读验证

**文件：**

- 生成：每款 `manifests/append_authorization.json`
- 生成：每款 `manifests/upload_receipt.json`
- 生成：运行根目录 `upload_summary.json`

- [ ] **步骤 1：记录本轮用户授权**

授权引用写为：`用户于 2026-08-06 明确要求将合格水印白底图追加到对应主图并执行。`

- [ ] **步骤 2：串行运行 deliver**

运行：`python scripts\run_sy1537_sy1552_white_background.py deliver`

每款上传前核对编号和原附件，遇到同名文件时标记 `already_present`，不重复上传。

- [ ] **步骤 3：运行远端 verify**

运行：`python scripts\run_sy1537_sy1552_white_background.py verify`

验收：上传文件拥有 `file_token`，产品编号一致，原主图 token 全部保留，新增附件唯一存在。

---

### 任务 8：最终验证与项目级扫描

- [ ] **步骤 1：运行全部本地测试**

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s skills\jewelry-white-background\tests -v
```

- [ ] **步骤 2：运行活动文件旧入口扫描**

扫描根目录脚本、`scripts/`、`skills/` 和本轮文档，排除历史 `outputs/`、`output/` 与 v4 Skill 自身兼容代码。结果必须没有旧版独立 Skill 路径或旧 helper 文件引用。

- [ ] **步骤 3：验证目录联接与 helper 哈希**

确认两个项目 Skill 联接有效，v4 helper SHA-256 与批准版本一致。

- [ ] **步骤 4：核对本轮交付汇总**

最终报告列出 16 款中的首轮通过、重试后通过、失败、待人工复核、已上传和未上传编号；不得把失败或待复核款描述为已完成。

- [ ] **步骤 5：运行 Git 检查并提交剩余源文件**

运行：`git diff --check`。仅提交本轮源代码、测试和文档；不提交图片、私密配置或现有无关 JSON。
