# 珠宝白底图短路径存储规则实施计划

> **执行要求：** 实施时必须使用 `superpowers:test-driven-development`，逐项完成失败测试、最小实现、回归验证和提交。

**目标：** 在不缩短审计身份、不破坏历史运行恢复的前提下，缩短珠宝白底图流程的临时路径，并在联网前阻止超过 Windows 安全长度预算的运行。

**架构：** 保留完整 `run_id`、`delivery_id`、Manifest 相对路径和交付文件名，只把无业务语义的 Wawapi 临时目录从 `wawapi-edit-<32位UUID>` 改成 `e-<8位UUID>`。运行创建阶段执行保守的路径预算检查，Wawapi 请求身份构建阶段再执行精确检查；任何超长路径都在创建 claim 和发起网络请求之前失败。排版阶段同时补齐 `layout/` 目录的显式创建，避免有效生成结果因目录缺失进入 `layout_failed`。

**技术栈：** Python 3.13、`pathlib`、Pillow、pytest、现有 JSON Manifest 和状态机。

**规格依据：** `docs/superpowers/specs/2026-08-15-jewelry-levels-first-crop-mask-edit-delivery-spec.md`

## 全局约束

- 不修改历史运行目录，不迁移、不重命名已有 `run_id`。
- 不截断 `run_id`、`delivery_id`、商品编号或正式交付文件名。
- Windows 安全路径上限固定为 `240` 个字符，为底层临时文件和工具内部后缀预留空间。
- 超长路径必须在 Wawapi claim 和真实 POST 之前失败，真实调用次数保持 `0`。
- 不在测试中调用真实 Wawapi，不写飞书，不记录任何 API Key。
- 所有新行为先写失败测试，再写最小实现。

---

## 文件结构

- 修改 `skills/jewelry-white-background/scripts/workflow_state.py`：运行根目录路径预算和创建前门禁。
- 修改 `scripts/yuan_image_generation_adapter.py`：短临时目录名和 Wawapi 输出路径精确预算。
- 修改 `skills/jewelry-white-background/scripts/run_white_background_workflow.py`：排版前创建 `layout/`。
- 修改 `skills/jewelry-white-background/SKILL.md`：正式路径命名规则和 Windows 输出根目录要求。
- 修改 `docs/superpowers/specs/2026-08-15-jewelry-levels-first-crop-mask-edit-delivery-spec.md`：补充短临时别名和路径预算合同。
- 修改 `skills/jewelry-white-background/tests/test_workflow_state.py`：运行根目录预算测试。
- 修改 `tests/test_yuan_image_generation_adapter.py`：短临时路径和联网前拒绝测试。
- 修改 `skills/jewelry-white-background/tests/test_run_white_background_workflow.py`：排版目录创建回归测试。
- 修改 `skills/jewelry-white-background/tests/test_skill_contract.py`：Skill 文档合同测试。

---

### 任务 1：运行根目录路径预算

**文件：**
- 修改：`skills/jewelry-white-background/scripts/workflow_state.py:197`
- 修改：`skills/jewelry-white-background/scripts/workflow_state.py:323`
- 测试：`skills/jewelry-white-background/tests/test_workflow_state.py`

**接口：**
- 产出：`WINDOWS_SAFE_PATH_LIMIT = 240`
- 产出：`RUN_DESCENDANT_RESERVE = 64`
- 产出：`IS_WINDOWS = os.name == "nt"`，仅作为可测试的平台判断入口。
- 产出：`_validate_run_path_budget(final_root: Path, *, windows: bool | None = None) -> None`
- 保持：`create_run(...) -> RunPaths` 的公开签名不变。

- [ ] **步骤 1：写运行根目录过长的失败测试**

```python
def test_create_run_rejects_windows_root_without_descendant_budget(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_module()
    monkeypatch.setattr(module, "IS_WINDOWS", True)
    output_root = tmp_path / ("long-root-" + "x" * 180)

    with pytest.raises(ValueError, match="Windows 路径预算"):
        module.create_run(
            output_root,
            identity(module),
            now=lambda: FIXED_NOW,
            uuid_factory=uuid_sequence(RUN_UUID, OWNER_UUID),
        )

    assert not output_root.exists()
```

- [ ] **步骤 2：运行测试并确认因缺少路径预算门禁而失败**

运行：

```powershell
python -m pytest skills/jewelry-white-background/tests/test_workflow_state.py::test_create_run_rejects_windows_root_without_descendant_budget -v
```

预期：测试失败，当前 `create_run` 会继续创建目录或没有抛出指定错误。

- [ ] **步骤 3：实现最小路径预算函数**

```python
WINDOWS_SAFE_PATH_LIMIT = 240
RUN_DESCENDANT_RESERVE = 64
IS_WINDOWS = os.name == "nt"


def _validate_run_path_budget(
    final_root: Path, *, windows: bool | None = None
) -> None:
    is_windows = IS_WINDOWS if windows is None else windows
    if not is_windows:
        return
    resolved_length = len(str(final_root.resolve(strict=False)))
    required_length = resolved_length + RUN_DESCENDANT_RESERVE
    if required_length > WINDOWS_SAFE_PATH_LIMIT:
        raise ValueError(
            "Windows 路径预算不足："
            f"运行根目录 {resolved_length} 字符，"
            f"预留后代路径后为 {required_length}，"
            f"上限为 {WINDOWS_SAFE_PATH_LIMIT}；请使用更短的 output_root"
        )
```

在 `create_run` 计算 `final_root` 后、创建 staging 或最终目录前调用：

```python
_validate_run_path_budget(final_root)
```

- [ ] **步骤 4：补充边界与兼容性测试**

新增测试覆盖：

```python
def test_run_path_budget_accepts_exact_windows_limit(tmp_path: Path) -> None:
    module = load_module("workflow_state_path_budget_boundary")
    final_root = tmp_path / ("x" * (240 - 64 - len(str(tmp_path)) - 1))
    module._validate_run_path_budget(final_root, windows=True)


def test_run_path_budget_is_not_applied_off_windows(tmp_path: Path) -> None:
    module = load_module("workflow_state_path_budget_non_windows")
    module._validate_run_path_budget(tmp_path / ("x" * 300), windows=False)
```

- [ ] **步骤 5：运行任务 1 测试**

```powershell
python -m pytest skills/jewelry-white-background/tests/test_workflow_state.py -v
```

预期：全部通过，原有原子发布、唯一运行和历史状态测试不变。

- [ ] **步骤 6：提交任务 1**

```powershell
git add skills/jewelry-white-background/scripts/workflow_state.py skills/jewelry-white-background/tests/test_workflow_state.py
git commit -m "fix: add workflow path budget gate"
```

---

### 任务 2：缩短 Wawapi 临时目录并在联网前精确校验

**文件：**
- 修改：`scripts/yuan_image_generation_adapter.py:296`
- 修改：`scripts/yuan_image_generation_adapter.py:433`
- 修改：`tests/test_yuan_image_generation_adapter.py:1`，新增 `import uuid` 和 `from unittest import mock`。
- 测试：`tests/test_yuan_image_generation_adapter.py`

**接口：**
- 产出：`_helper_output_path_probe(output_path: Path) -> Path`
- 产出：`_validate_helper_output_path_budget(output_path: Path, *, windows: bool | None = None) -> None`
- 修改：`_temporary_edit_output_dir` 使用 `tmp/e-<8位UUID>`。
- 保持：`BackgroundEditRequest`、请求身份和结果 Manifest schema 不变。

- [ ] **步骤 1：写短临时目录命名失败测试**

```python
def test_background_edit_temp_directory_uses_short_alias(self) -> None:
    module = self.require_adapter()
    output = Path("run/edit/SY1537_generated.png")

    with mock.patch.object(
        module.uuid,
        "uuid4",
        return_value=uuid.UUID("12345678-1234-5678-1234-567812345678"),
    ):
        temporary = module._temporary_edit_output_dir(output)

    self.assertEqual(temporary, Path("run/tmp/e-12345678"))
```

- [ ] **步骤 2：写复现 261 字符问题的失败测试**

```python
def test_background_edit_path_probe_fits_windows_budget_after_shortening(
    self,
) -> None:
    module = self.require_adapter()
    output = Path(
        r"C:\Users\Administrator\Documents\珠宝白底图生成\outputs\"
        r"jewelry-white-background\formal-regenerate-sy1537-20260817-v3\"
        r"runs\SY1537\20260817T152127152Z-55d3c4076050498aa721d76116e74e90\"
        r"edit\SY1537_generated.png"
    )

    probe = module._helper_output_path_probe(output)

    self.assertLessEqual(len(str(probe)), module.WINDOWS_SAFE_PATH_LIMIT)
```

- [ ] **步骤 3：运行两项测试并确认失败**

```powershell
python -m pytest tests/test_yuan_image_generation_adapter.py -k "short_temp_dir or windows_path_probe" -v
```

预期：当前临时目录仍为 `wawapi-edit-<32位UUID>`，261 字符复现路径超过预算。

- [ ] **步骤 4：实现短目录和精确预算**

```python
WINDOWS_SAFE_PATH_LIMIT = 240
WAWAPI_HELPER_FILENAME_PROBE = "image-20991231-235959-ffffffff-9999.jpeg"
IS_WINDOWS = os.name == "nt"


def _temporary_edit_output_dir(output_path: Path) -> Path:
    run_root = (
        output_path.parent.parent
        if output_path.parent.name.lower() == "edit"
        else output_path.parent
    )
    return run_root / "tmp" / f"e-{uuid.uuid4().hex[:8]}"


def _helper_output_path_probe(output_path: Path) -> Path:
    run_root = (
        output_path.parent.parent
        if output_path.parent.name.lower() == "edit"
        else output_path.parent
    )
    return (
        run_root
        / "tmp"
        / "e-ffffffff"
        / WAWAPI_HELPER_FILENAME_PROBE
    ).resolve(strict=False)


def _validate_helper_output_path_budget(
    output_path: Path, *, windows: bool | None = None
) -> None:
    is_windows = IS_WINDOWS if windows is None else windows
    if not is_windows:
        return
    probe = _helper_output_path_probe(output_path)
    if len(str(probe)) > WINDOWS_SAFE_PATH_LIMIT:
        raise ValueError(
            "Wawapi 临时输出路径超过 Windows 安全预算："
            f"{len(str(probe))} > {WINDOWS_SAFE_PATH_LIMIT}；"
            "请使用更短的 output_root"
        )
```

在 `build_background_edit_request_identity` 完成图片和 Mask 校验后加入：

```python
_validate_helper_output_path_budget(Path(request.output_path))
```

该位置位于 call claim 和真实 transport 之前，路径失败会进入 `edit_request_local_gate_failed`，POST 次数为 `0`。

- [ ] **步骤 5：写联网前阻断测试**

```python
def test_overlong_helper_path_fails_before_transport(self) -> None:
    module = self.require_adapter()
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        image_path = root / "front.png"
        mask_path = root / "mask.png"
        Image.new("RGB", (37, 53), "white").save(image_path, "PNG")
        Image.new("RGBA", (37, 53), (255, 255, 255, 0)).save(
            mask_path, "PNG"
        )
        request = module.BackgroundEditRequest(
            prompt="只替换透明背景\n",
            image=image_path,
            mask=mask_path,
            output_path=root / ("x" * 220) / "edit/result.png",
            base_url="https://example.test",
            model="gpt-image-2",
        )
        transport = mock.Mock(
            side_effect=AssertionError("不得调用真实 transport")
        )

        with mock.patch.object(module, "IS_WINDOWS", True):
            with self.assertRaisesRegex(ValueError, "临时输出路径"):
                module.edit_background_single_attempt(
                    request, transport=transport
                )

        transport.assert_not_called()
```

- [ ] **步骤 6：运行适配器与重试测试**

```powershell
python -m pytest tests/test_yuan_image_generation_adapter.py skills/jewelry-white-background/tests/test_run_wawapi_edit_with_retry.py -v
```

预期：全部通过；已有 429、5xx、unknown、call slot 和结果发布合同不变。

- [ ] **步骤 7：提交任务 2**

```powershell
git add scripts/yuan_image_generation_adapter.py tests/test_yuan_image_generation_adapter.py skills/jewelry-white-background/tests/test_run_wawapi_edit_with_retry.py
git commit -m "fix: shorten Wawapi temporary paths"
```

---

### 任务 3：排版目录按合同预创建

**文件：**
- 修改：`skills/jewelry-white-background/scripts/run_white_background_workflow.py:301`
- 修改：`skills/jewelry-white-background/tests/test_run_white_background_workflow.py:1`，新增 `import json`。
- 测试：`skills/jewelry-white-background/tests/test_run_white_background_workflow.py`

**接口：**
- 保持：`DefaultWorkflowDependencies._create_layout(paths) -> None`。
- 新行为：调用排版脚本前保证当前运行的 `layout/` 已存在。

- [ ] **步骤 1：写回归测试复现 `排版运行目录不完整`**

```python
def test_default_layout_stage_creates_layout_directory_before_call(
    tmp_path: Path, monkeypatch
) -> None:
    state, paths = create_paths(tmp_path)
    advance(
        state,
        paths,
        [
            "source_ready",
            "detection_image_ready",
            "geometry_ready",
            "crop_ready",
            "mask_ready",
            "awaiting_mask_confirmation",
            "mask_confirmed",
            "edit_completed",
        ],
    )
    edit_path = paths.root / "edit/SY1537_generated.png"
    edit_path.parent.mkdir(parents=True)
    Image.new("RGB", (32, 32), "white").save(edit_path, "PNG")
    (paths.manifests_dir / "SY1537_edit_result.json").write_text(
        json.dumps({"result": {"path": "edit/SY1537_generated.png"}}),
        encoding="utf-8",
    )
    module = load_path("workflow_layout_directory", WORKFLOW_SCRIPT)
    layout_dir = paths.root / "layout"
    assert not layout_dir.exists()

    def fake_layout(input_path, output_path, manifest_path):
        assert layout_dir.is_dir()
        Image.new("RGB", (1536, 2048), "white").save(output_path, "PNG")
        manifest_path.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(module._layout, "layout_generated_result", fake_layout)
    module.DefaultWorkflowDependencies()._create_layout(paths)

    assert state.load_state(paths.state_path)["status"] == "layout_completed"
```

- [ ] **步骤 2：运行测试并确认当前实现失败**

```powershell
python -m pytest skills/jewelry-white-background/tests/test_run_white_background_workflow.py::test_default_layout_stage_creates_layout_directory_before_call -v
```

- [ ] **步骤 3：在排版调用前创建目录**

```python
def _create_layout(self, paths) -> None:
    root = paths.root
    (root / "layout").mkdir(parents=True, exist_ok=True)
    manifest = _read_json(root / "manifests" / f"{paths.product_id}_edit_result.json")
    generated = root / manifest["result"]["path"]
    _layout.layout_generated_result(
        generated,
        root / "layout" / f"{paths.product_id}_3x4_60pct.png",
        root / "manifests" / f"{paths.product_id}_layout_manifest.json",
    )
    _transition(
        paths,
        "edit_completed",
        "layout_completed",
        "generated_result_laid_out",
    )
```

- [ ] **步骤 4：运行编排器测试**

```powershell
python -m pytest skills/jewelry-white-background/tests/test_run_white_background_workflow.py -v
```

预期：目录回归测试通过，异常到终态、人工 Hook 和单步恢复测试不变。

- [ ] **步骤 5：提交任务 3**

```powershell
git add skills/jewelry-white-background/scripts/run_white_background_workflow.py skills/jewelry-white-background/tests/test_run_white_background_workflow.py
git commit -m "fix: create layout directory before publishing"
```

---

### 任务 4：更新 Skill 与权威规格中的路径合同

**文件：**
- 修改：`skills/jewelry-white-background/SKILL.md:26`
- 修改：`docs/superpowers/specs/2026-08-15-jewelry-levels-first-crop-mask-edit-delivery-spec.md:48`
- 修改：`docs/superpowers/specs/2026-08-15-jewelry-levels-first-crop-mask-edit-delivery-spec.md:80`
- 修改：`docs/superpowers/specs/2026-08-15-jewelry-levels-first-crop-mask-edit-delivery-spec.md:1042`
- 测试：`skills/jewelry-white-background/tests/test_skill_contract.py`

**接口：**
- 保留：`<output_root>/<safe-product-id>/<run-id>/` 正式目录结构。
- 新增：Windows 路径预算 `240`。
- 新增：Wawapi 临时目录 `tmp/e-<8位随机十六进制>`。
- 明确：业务身份不截断，短名称仅用于无业务语义的临时目录。

- [ ] **步骤 1：写 Skill 合同失败测试**

```python
def test_storage_path_contract_is_short_and_fail_fast(self) -> None:
    self.assertIn("Windows 安全路径上限为 `240`", self.skill)
    self.assertIn("`tmp/e-<8位随机十六进制>`", self.skill)
    self.assertIn("不得缩短 `run_id`、`delivery_id`", self.skill)
    self.assertIn("必须在 Wawapi claim 和真实 POST 前终止", self.skill)
```

- [ ] **步骤 2：运行测试并确认文档尚未包含新合同**

```powershell
python -m pytest skills/jewelry-white-background/tests/test_skill_contract.py::TestSkillContract::test_storage_path_contract_is_short_and_fail_fast -v
```

- [ ] **步骤 3：更新 Skill 文档**

在“创建唯一运行”和“Wawapi”章节加入以下规则：

```markdown
- Windows 安全路径上限为 `240` 个字符；创建运行时必须为最深临时输出预留 `64` 个字符。
- 正式身份仍使用完整 `run_id`、`delivery_id` 和交付文件名，不得缩短或复用。
- Wawapi 临时目录固定为 `tmp/e-<8位随机十六进制>`，短随机别名不进入业务身份。
- 运行根目录或精确临时输出超过预算时，必须在 Wawapi claim 和真实 POST 前终止，并提示改用更短的 `output_root`。
```

- [ ] **步骤 4：修正规格中的现有冲突**

将“不得通过截断 `run_id`、`delivery_id` 或文件名规避路径长度问题”保留，并追加：

```markdown
允许缩短无业务语义的临时目录随机段；临时别名不得替代或改变 Manifest、确认回执、call claim 和交付回执中的完整业务身份。
```

在运行目录树中新增：

```text
├── tmp/
│   └── e-<8位随机十六进制>/
```

- [ ] **步骤 5：运行 Skill 合同测试**

```powershell
python -m pytest skills/jewelry-white-background/tests/test_skill_contract.py -v
```

- [ ] **步骤 6：提交任务 4**

```powershell
git add skills/jewelry-white-background/SKILL.md docs/superpowers/specs/2026-08-15-jewelry-levels-first-crop-mask-edit-delivery-spec.md skills/jewelry-white-background/tests/test_skill_contract.py
git commit -m "docs: define short temporary path contract"
```

---

### 任务 5：全量回归与 Windows 路径烟雾验证

**文件：**
- 验证：`skills/jewelry-white-background/tests/`
- 验证：`tests/test_yuan_image_generation_adapter.py`
- 不新增生产代码。

- [ ] **步骤 1：运行路径与编排专项测试**

```powershell
python -m pytest `
  skills/jewelry-white-background/tests/test_workflow_state.py `
  tests/test_yuan_image_generation_adapter.py `
  skills/jewelry-white-background/tests/test_run_wawapi_edit_with_retry.py `
  skills/jewelry-white-background/tests/test_run_white_background_workflow.py `
  skills/jewelry-white-background/tests/test_skill_contract.py `
  -v
```

预期：全部通过，且没有真实网络调用。

- [ ] **步骤 2：运行完整测试套件**

```powershell
python -m pytest -q
```

预期：全部通过；只允许已有、已记录的第三方弃用警告。

- [ ] **步骤 3：执行静态与差异检查**

```powershell
python -m py_compile `
  skills/jewelry-white-background/scripts/workflow_state.py `
  skills/jewelry-white-background/scripts/run_white_background_workflow.py `
  scripts/yuan_image_generation_adapter.py
git diff --check
```

- [ ] **步骤 4：执行无联网 Windows 长路径烟雾测试**

使用假 transport 和与本次事故等长的 `output_root`，验证：

```python
assert old_probe_length == 261
assert new_probe_length <= 240
assert transport_call_count == 1
assert published_result.is_file()
assert edit_manifest["result"]["sha256"] == sha256(published_result)
```

测试只在临时目录写入一个小型 PNG，不调用 Wawapi。

- [ ] **步骤 5：确认工作树范围**

```powershell
git status --short
git log --oneline -4
```

预期：只有本计划列出的脚本、测试、Skill 和规格文件发生变化。

---

## 验收标准

1. 复现事故的旧目标路径长度为 `261`，新临时目标路径不超过 `240`。
2. 任何仍然超长的路径都在 Wawapi claim 和真实 POST 前失败，外部调用次数为 `0`。
3. `run_id`、`delivery_id`、Manifest schema、确认回执和历史运行读取保持兼容。
4. Wawapi 成功后 `layout/` 已存在，排版不会再因“排版运行目录不完整”进入 `layout_failed`。
5. 测试、编译和 `git diff --check` 全部通过。
6. 文档明确推荐短 `output_root`，但不绑定 C 盘或 E 盘等固定盘符。
