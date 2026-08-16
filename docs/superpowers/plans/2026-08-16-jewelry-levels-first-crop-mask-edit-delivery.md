# 珠宝白底图“色阶先行—裁剪—Mask Edit—自动交付”实施计划

> **供执行者使用：** 必须逐任务使用 `superpowers:test-driven-development`；整份计划在单会话内执行时使用 `superpowers:executing-plans`。所有步骤使用复选框跟踪。

**目标：** 按已评审规格实现唯一、可恢复、可并发审计的珠宝白底图流程，并把两个旧入口收敛到同一个正式编排器。

**架构：** 新增一个运行状态基础模块和一个唯一正式编排器；检测、裁剪、Mask、Wawapi、排版、交付继续由职责单一的脚本完成。所有外部副作用先创建持久化 claim，所有本地资产先写临时文件再禁止覆盖地原子发布，状态迁移统一由 `manifests/workflow_state.json` 驱动。

**技术栈：** Python 3.13、Pillow、标准库 `json/hashlib/pathlib/subprocess/threading/uuid`、pytest/unittest、PowerShell、`lark-cli`、Wawapi helper。

**规格：** `docs/superpowers/specs/2026-08-15-jewelry-levels-first-crop-mask-edit-delivery-spec.md`

## 全局约束

- 新运行只接受 `vision-geometry-mask-2.1`；历史 `2.0` 仅保留只读兼容入口。
- `source_sha256` 固定表示规范化 RGB PNG 的实际文件摘要；原始下载字节使用 `raw_source_sha256`。
- 正式运行目录固定为 `<output_root>/<safe-product-id>/<run-id>/`，禁止覆盖或复用。
- 规范状态路径固定为 `manifests/workflow_state.json`，运行级状态锁固定为 `manifests/workflow.lock`。
- 每个 Wawapi、水印或上传 claim 使用同目录 `<claim-file>.lock`，不得复用运行级状态锁。
- 所有 SHA-256 使用 64 位大写十六进制；所有时间使用 UTC RFC 3339。
- 所有图片像素上限固定为 `20,000,000`；超过上限时必须在分配后续大图或联网前失败。
- Wawapi single-attempt 内部重试为 0；每个运行最多 3 个 POST。
- 排版只能读取 Wawapi 生成结果；水印后只执行路径、普通文件、非空和 SHA-256 检查。
- 飞书写入只允许一次 `base +record-upload-attachment`，自动流程上传前后不得回读目标字段。
- 每项生产代码变更必须先有能够因缺少该行为而失败的测试。

---

### 任务 1：运行身份、原子文件和状态迁移基础

**文件：**
- 新建：`skills/jewelry-white-background/scripts/workflow_state.py`
- 新建：`skills/jewelry-white-background/tests/test_workflow_state.py`

**接口：**
- 产生：`RunPaths`、`create_run()`、`load_state()`、`transition_state()`、`atomic_create_bytes()`、`atomic_replace_json()`、`ClaimStore`。
- 后续任务通过 `RunPaths` 获取唯一目录，通过 `transition_state` 执行 `expected_status + expected_state_revision` CAS。

- [ ] **步骤 1：写运行 ID、目录发布和 Windows 安全名称失败测试**

```python
def test_create_run_atomically_publishes_only_manifests_state_and_lock(tmp_path):
    run = module.create_run(tmp_path, "SY1537", fixed_now, fixed_uuid)
    assert run.root == tmp_path / "SY1537" / run.run_id
    assert run.state_path == run.root / "manifests" / "workflow_state.json"
    assert run.workflow_lock_path == run.root / "manifests" / "workflow.lock"
    assert not (run.root / "workflow_state.json").exists()
    assert not (run.root / "workflow.lock").exists()

@pytest.mark.parametrize("product_id", ["SKU.", "A..B", "CON", "CON.txt", "AUX", "COM1", "LPT9.png"])
def test_create_run_rejects_unsafe_product_ids_before_creating_directories(tmp_path, product_id):
    with pytest.raises(ValueError):
        module.create_run(tmp_path, product_id, fixed_now, fixed_uuid)
    assert list(tmp_path.iterdir()) == []
```

- [ ] **步骤 2：运行失败测试并确认因模块或接口不存在而失败**

运行：`python -m pytest skills/jewelry-white-background/tests/test_workflow_state.py -q`

预期：FAIL，原因是 `workflow_state.py` 或 `create_run` 尚不存在。

- [ ] **步骤 3：实现最小运行目录和原子 I/O**

```python
@dataclass(frozen=True)
class RunPaths:
    root: Path
    run_id: str
    state_path: Path
    workflow_lock_path: Path

def atomic_create_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def atomic_replace_json(path: Path, payload: Mapping[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
```

`create_run()` 使用上述原语：先在同级 `.<run-id>.creating-<owner-id>` 写完整 `manifests/`，刷新文件后用 `os.rename(staging_root, final_root)` 禁止覆盖地发布；失败时不得产生最终目录。

- [ ] **步骤 4：写状态 CAS、终态和崩溃原子性失败测试**

```python
def test_transition_requires_expected_status_and_revision(run_paths):
    with pytest.raises(StateConflict):
        module.transition_state(run_paths, "source_ready", 99, "detection_image_ready", "detection_ok")

def test_terminal_state_has_no_automatic_transition(run_paths):
    write_state(run_paths, status="edit_unknown", revision=4)
    with pytest.raises(TerminalStateError):
        module.transition_state(run_paths, "edit_unknown", 4, "edit_attempt_2", "retry")
```

- [ ] **步骤 5：实现 `transition_state` 和状态历史**

```python
def transition_state(
    paths: RunPaths,
    expected_status: str,
    expected_revision: int,
    next_status: str,
    event: str,
    *,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    with interprocess_lock(paths.workflow_lock_path):
        state = load_state(paths)
        if (state["status"], state["state_revision"]) != (expected_status, expected_revision):
            raise StateConflict()
        if expected_status in TERMINAL_STATES:
            raise TerminalStateError(expected_status)
        previous = state["status"]
        if mutate is not None:
            mutate(state)
        state["status"] = next_status
        state["state_revision"] += 1
        state["last_transition"] = {"from": previous, "event": event, "at": utc_now()}
        state.setdefault("history", []).append(dict(state["last_transition"]))
        atomic_replace_json(paths.state_path, state)
        return state
```

迁移必须持有 `manifests/workflow.lock`，递增 `state_revision`，保留完整 `history`，再原子替换状态文件。

- [ ] **步骤 6：写 claim owner、心跳和 revision CAS 失败测试**

```python
def test_claim_heartbeat_cannot_overwrite_recorded_response(tmp_path):
    store = module.ClaimStore(tmp_path / "edit-call-1.json")
    claim = store.create(identity, owner, "submitting")
    recorded = store.update(claim["record_revision"], lambda p: p.update(status="response_recorded"))
    with pytest.raises(ClaimConflict):
        store.heartbeat(claim["record_revision"], owner, now_plus_5)
    assert store.load()["status"] == "response_recorded"
    assert store.load()["record_revision"] == recorded["record_revision"]
```

- [ ] **步骤 7：实现 `ClaimStore` 的独立 sidecar 锁、5 秒心跳和 30 秒 active 判定**

```python
store = ClaimStore(run_root / "logs" / "edit-call-1.json")
assert store.lock_path == run_root / "logs" / "edit-call-1.json.lock"
claim = store.create(identity, owner, "submitting")
assert claim["record_revision"] == 1
assert claim["owner_id"] == owner.owner_id
assert parse_utc(claim["lease_expires_at"]) - parse_utc(claim["heartbeat_at"]) == timedelta(seconds=30)
```

- [ ] **步骤 8：运行任务 1 测试并提交**

运行：`python -m pytest skills/jewelry-white-background/tests/test_workflow_state.py -q`

提交：`git add skills/jewelry-white-background/scripts/workflow_state.py skills/jewelry-white-background/tests/test_workflow_state.py && git commit -m "feat: add atomic workflow state primitives"`

---

### 任务 2：唯一源图、规范化身份和检测资产 2.0

**文件：**
- 新建：`skills/jewelry-white-background/scripts/prepare_source_context.py`
- 新建：`skills/jewelry-white-background/tests/test_prepare_source_context.py`
- 修改：`skills/jewelry-white-background/scripts/prepare_mask_detection_images.py`
- 修改：`skills/jewelry-white-background/tests/test_prepare_mask_detection_images.py`

**接口：**
- 产生：`normalize_source(raw_path, canonical_path)`、`build_product_context(paths, target, attachment)`。
- 修改：`write_detection_images(input_path, global_output_path, local_output_path, report_path, run_root=run_root)` 返回 `mask-detection-images-2.0`，记录完整路径、摘要、尺寸、模式、固定算法参数。

- [ ] **步骤 1：写原始字节与规范化 PNG 双摘要失败测试**

```python
def test_normalize_source_separates_raw_and_canonical_hashes(tmp_path):
    raw = tmp_path / "input.png"
    write_transparent_png_with_exif(raw)
    result = module.normalize_source(raw, tmp_path / "source" / "SY1537_original.png")
    assert result.raw_source_sha256 == sha256(raw)
    assert result.canonical_source_sha256 == sha256(result.canonical_path)
    assert result.source_sha256 == result.canonical_source_sha256
    with Image.open(result.canonical_path) as image:
        assert image.mode == "RGB"
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest skills/jewelry-white-background/tests/test_prepare_source_context.py -q`

- [ ] **步骤 3：实现源图规范化和 `product_context.json` 原子发布**

```python
@dataclass(frozen=True)
class SourceIdentity:
    raw_path: Path
    raw_source_sha256: str
    canonical_path: Path
    canonical_source_sha256: str
    canonical_size: tuple[int, int]

identity = normalize_source(raw_path, canonical_path)
context = build_product_context(paths, target, attachment)
assert context["source_sha256"] == identity.canonical_source_sha256
assert context["source_identity"]["raw_source_sha256"] == identity.raw_source_sha256
assert context["source_identity"]["canonical_source_mode"] == "RGB"
```

- [ ] **步骤 4：把现有检测报告期望升级为 2.0，并先观察失败**

测试必须断言：`schema_version="mask-detection-images-2.0"`、`coordinate_transform="identity"`、两张输出包含相对路径/SHA/尺寸/模式、报告包含权威实现和全部固定参数。

运行：`python -m pytest skills/jewelry-white-background/tests/test_prepare_mask_detection_images.py -q`

- [ ] **步骤 5：实现检测资产禁止覆盖和原子发布**

```python
report = write_detection_images(
    input_path,
    global_output_path,
    local_output_path,
    report_path,
    run_root=run_root,
)
assert report["schema_version"] == "mask-detection-images-2.0"
assert report["coordinate_transform"] == "identity"
assert report["authoritative_implementation"] == "skills/jewelry-white-background/scripts/prepare_mask_detection_images.py"
```

删除直接 `write_bytes` 发布方式；目标存在时立即失败，三个目标全部先写各自同目录临时文件，关闭后逐个禁止覆盖发布。

- [ ] **步骤 6：运行任务 2 测试并提交**

运行：`python -m pytest skills/jewelry-white-background/tests/test_prepare_source_context.py skills/jewelry-white-background/tests/test_prepare_mask_detection_images.py -q`

提交：`git add skills/jewelry-white-background/scripts/prepare_source_context.py skills/jewelry-white-background/scripts/prepare_mask_detection_images.py skills/jewelry-white-background/tests/test_prepare_source_context.py skills/jewelry-white-background/tests/test_prepare_mask_detection_images.py && git commit -m "feat: add canonical source and detection manifests"`

---

### 任务 3：`vision-geometry-mask-2.1` 与确定性裁剪

**文件：**
- 新建：`skills/jewelry-white-background/scripts/create_geometry_crop.py`
- 新建：`skills/jewelry-white-background/tests/test_create_geometry_crop.py`
- 修改：`skills/jewelry-white-background/scripts/create_background_edit_mask.py`
- 修改：`skills/jewelry-white-background/tests/test_background_edit_mask.py`

**接口：**
- 产生：`VisionGeometryV21`、`load_vision_geometry_v21()`、`rasterize_source_geometry()`、`compute_crop_box()`、`create_geometry_crop_assets()`。
- `create_background_edit_mask.py` 只接收裁剪后的四路资产和 `vision-cropped-geometry-1.0`。

- [ ] **步骤 1：写严格 2.1 schema 和拒绝 2.0 的失败测试**

```python
def test_loader_rejects_legacy_2_0_even_when_missing_fields_are_added(tmp_path):
    payload = valid_geometry_21()
    payload["schema_version"] = "vision-geometry-mask-2.0"
    write_json(tmp_path / "geometry.json", payload)
    with pytest.raises(ValueError, match="2.1"):
        module.load_vision_geometry_v21(tmp_path / "geometry.json", expected_identity)
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest skills/jewelry-white-background/tests/test_create_geometry_crop.py -q`

- [ ] **步骤 3：实现严格身份外壳、图元和疑点校验**

```python
@dataclass(frozen=True)
class VisionGeometryV21:
    product_id: str
    source_sha256: str
    source_size: tuple[int, int]
    detection_image_sha256: str
    detection_manifest_sha256: str
    primitives: Sequence[dict[str, Any]]
    uncertain_regions: Sequence[dict[str, Any]]
    geometry_sha256: str
```

- [ ] **步骤 4：写栅格量化与 `77/100` 裁剪公式失败测试**

```python
def test_compute_crop_box_uses_ceil_and_places_odd_extra_pixel_right_bottom():
    assert module.compute_crop_box((1000, 800), (100, 100, 331, 300)) == expected_box

def test_example_ellipse_quantizes_before_crop_translation():
    result = module.quantize_ellipse([5000, 5000, 2000, 2500], (3024, 4032), (400, 600, 2500, 3400))
    assert result == [1112, 1416, 605, 1008]
```

- [ ] **步骤 5：实现完整源图栅格、裁剪和裁剪几何**

```python
def compute_crop_box(source_size: tuple[int, int], content_box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    source_width, source_height = source_size
    left, top, right, bottom = content_box
    content_width, content_height = right - left, bottom - top
    crop_width = min(source_width, max(content_width, math.ceil(content_width * 100 / 77)))
    crop_height = min(source_height, max(content_height, math.ceil(content_height * 100 / 77)))
    ideal_left = math.ceil((left + right - crop_width) / 2)
    ideal_top = math.ceil((top + bottom - crop_height) / 2)
    crop_left = min(max(ideal_left, 0), source_width - crop_width)
    crop_top = min(max(ideal_top, 0), source_height - crop_height)
    return crop_left, crop_top, crop_left + crop_width, crop_top + crop_height

def create_geometry_crop_assets(
    source_path: Path,
    global_detection_path: Path,
    local_detection_path: Path,
    geometry_path: Path,
    output_paths: CropOutputPaths,
) -> dict[str, Any]:
    geometry = load_vision_geometry_v21(geometry_path, expected_identity)
    full_alpha = rasterize_source_geometry(geometry)
    crop_box = compute_crop_box(geometry.source_size, content_box(full_alpha, geometry.uncertain_regions))
    return publish_crop_assets(source_path, global_detection_path, local_detection_path, full_alpha, geometry, crop_box, output_paths)
```

必须验证三张图片与候选 Alpha 使用同一个 `crop_box`，并验证 `rasterize(cropped_geometry)` 与完整 Alpha 裁剪逐像素一致。

- [ ] **步骤 6：修改 Mask 模块的 2.0 入口边界**

新流程公开函数改为：

```python
def create_background_edit_assets(
    cropped_original_path: Path,
    cropped_detection_path: Path,
    cropped_local_detection_path: Path,
    candidate_alpha_path: Path,
    cropped_geometry_path: Path,
    crop_manifest_path: Path,
    outputs: MaskOutputPaths,
) -> MaskAssessment:
    inputs = validate_cropped_inputs(
        cropped_original_path,
        cropped_detection_path,
        cropped_local_detection_path,
        candidate_alpha_path,
        cropped_geometry_path,
        crop_manifest_path,
    )
    return build_and_publish_mask_assets(inputs, outputs)
```

保留旧 loader 时必须放在名称含 `legacy_read_only` 的私有路径，新正式入口不得调用。

- [ ] **步骤 7：运行任务 3 测试并提交**

运行：`python -m pytest skills/jewelry-white-background/tests/test_create_geometry_crop.py skills/jewelry-white-background/tests/test_background_edit_mask.py tests/test_create_background_edit_mask.py -q`

提交：`git add skills/jewelry-white-background/scripts/create_geometry_crop.py skills/jewelry-white-background/scripts/create_background_edit_mask.py skills/jewelry-white-background/tests/test_create_geometry_crop.py skills/jewelry-white-background/tests/test_background_edit_mask.py tests/test_create_background_edit_mask.py && git commit -m "feat: add geometry-first deterministic crop"`

---

### 任务 4：三路 Mask、review bundle 与不可变确认

**文件：**
- 修改：`skills/jewelry-white-background/scripts/create_background_edit_mask.py`
- 新建：`skills/jewelry-white-background/scripts/workflow_confirmations.py`
- 新建：`skills/jewelry-white-background/tests/test_workflow_confirmations.py`
- 修改：`skills/jewelry-white-background/tests/test_background_edit_mask.py`

**接口：**
- 产生：`create_mask_review_bundle()`、`confirm_mask_review()`、`validate_mask_confirmation()`。
- `automatic_wawapi_edit_allowed` 只由 `validate_mask_confirmation` 返回，不写入确认回执。

- [ ] **步骤 1：把边缘证据测试改为读取三张已裁剪输入**

测试 spy 必须证明 `create_background_edit_mask.py` 不再调用 `prepare_mask_detection_images` 重新生成检测图，并验证三路 2/3 共识、fallback、冻结核心/窄带外/边框。

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest skills/jewelry-white-background/tests/test_background_edit_mask.py -q`

- [ ] **步骤 3：实现 `boundary-refinement-2.1` 和 draft assessment**

```python
def refine_candidate_boundary(
    original_rgb: Image.Image,
    global_robust: Image.Image,
    local_limited: Image.Image,
    candidate_alpha: Image.Image,
) -> tuple[Image.Image, BoundaryRefinementReport]:
    edges = (_edge_map(original_rgb), _edge_map(global_robust), _edge_map(local_limited))
    thresholds = tuple(max(3, percentile(edge, 0.86)) for edge in edges)
    consensus = two_of_three_consensus(edges, thresholds)
    return apply_consensus_inside_frozen_band(candidate_alpha, consensus)
```

fallback 必须写入 `technical_blockers`，任何 blocker 都阻止进入 Hook。

- [ ] **步骤 4：写 review bundle 摘要冻结和确认原子创建失败测试**

```python
def test_mask_confirmation_is_create_only_and_binds_every_review_asset(run_paths):
    bundle = module.create_mask_review_bundle(run_paths, assets)
    receipt = module.confirm_mask_review(run_paths, bundle, reviewer="session-1", resolved_ids=["cord"])
    with pytest.raises(FileExistsError):
        module.confirm_mask_review(run_paths, bundle, reviewer="session-2", resolved_ids=["cord"])
    assert "automatic_wawapi_edit_allowed" not in receipt

def test_changed_bundle_asset_invalidates_confirmation(run_paths):
    receipt = confirmed_receipt(run_paths)
    mutate_bound_asset(receipt)
    assert module.validate_mask_confirmation(run_paths) is False
```

- [ ] **步骤 5：实现 Mask bundle、确认回执和重新门禁**

```python
bundle = create_mask_review_bundle(paths, assets)
receipt = confirm_mask_review(paths, bundle, reviewer="session-1", resolved_ids=all_uncertain_ids)
gate = validate_mask_confirmation(paths)
assert receipt["review_bundle_sha256"] == sha256_file(paths.root / bundle["path"])
assert gate.automatic_wawapi_edit_allowed is True
```

- [ ] **步骤 6：运行任务 4 测试并提交**

运行：`python -m pytest skills/jewelry-white-background/tests/test_background_edit_mask.py skills/jewelry-white-background/tests/test_workflow_confirmations.py -q`

提交：`git add skills/jewelry-white-background/scripts/create_background_edit_mask.py skills/jewelry-white-background/scripts/workflow_confirmations.py skills/jewelry-white-background/tests/test_background_edit_mask.py skills/jewelry-white-background/tests/test_workflow_confirmations.py && git commit -m "feat: add immutable mask review confirmation"`

---

### 任务 5：Wawapi single-attempt 适配器

**文件：**
- 修改：`scripts/yuan_image_generation_adapter.py`
- 修改：`tests/test_yuan_image_generation_adapter.py`

**接口：**
- 产生：`BackgroundEditRequestIdentity`、`BackgroundEditAttempt`、`edit_background_single_attempt()`。
- 旧 `edit_background_to_path()` 仅保留兼容包装，不得包含重试。

- [ ] **步骤 1：写请求身份、真实尺寸和一次 POST 失败测试**

```python
def test_single_attempt_uses_actual_image_dimensions_and_one_transport_call(tmp_path):
    image, mask = write_edit_inputs(tmp_path, size=(37, 53))
    calls = []
    result = module.edit_background_single_attempt(request(image, mask), transport=lambda command, **kw: calls.append(command) or completed_response)
    assert result.request_identity["image_size"] == [37, 53]
    assert result.request_identity["mask_size"] == [37, 53]
    assert len(calls) == 1
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/test_yuan_image_generation_adapter.py -q`

- [ ] **步骤 3：实现 single-attempt 返回类型和禁止隐藏重试**

```python
@dataclass(frozen=True)
class BackgroundEditAttempt:
    command: Sequence[str]
    returncode: int | None
    stdout: str
    stderr: str
    http_status: int | None
    request_may_have_been_sent: bool
    task_or_job_id: str | None
    rejection_evidence: str | None

def edit_background_single_attempt(request: BackgroundEditRequest, transport: Callable = subprocess.run) -> BackgroundEditAttempt:
    validate_background_edit_inputs(request.image, request.mask)
    command = build_background_edit_command(request)
    completed = transport(command, capture_output=True, text=True, check=False, env=_helper_environment())
    return parse_background_edit_attempt(command, completed)
```

适配器只负责一次 helper/transport 调用和响应证据，不自行 sleep、不创建下一次请求。

- [ ] **步骤 4：写有效候选真实格式、完整 load 和后缀一致性失败测试**

候选必须逐个验证，第一张有效候选才可原子发布；截断图、格式/后缀不符、少于 16 像素或超过 20MP 都必须无效。

- [ ] **步骤 5：运行任务 5 测试并提交**

运行：`python -m pytest tests/test_yuan_image_generation_adapter.py -q`

提交：`git add scripts/yuan_image_generation_adapter.py tests/test_yuan_image_generation_adapter.py && git commit -m "feat: add single-attempt Wawapi edit adapter"`

---

### 任务 6：最多三个 call slot、安全重试和未知终态

**文件：**
- 新建：`skills/jewelry-white-background/scripts/run_wawapi_edit_with_retry.py`
- 新建：`skills/jewelry-white-background/tests/test_run_wawapi_edit_with_retry.py`

**接口：**
- 消费：任务 1 的 `ClaimStore/transition_state`，任务 5 的 `edit_background_single_attempt`。
- 产生：`run_edit_until_terminal()`，返回 `edit_completed/edit_failed/edit_unknown/already_in_progress`。

- [ ] **步骤 1：写 POST 次数和重试分类参数化失败测试**

```python
@pytest.mark.parametrize(
    ("responses", "expected_posts", "terminal"),
    [
        ([success()], 1, "edit_completed"),
        ([retryable_429(), success()], 2, "edit_completed"),
        ([retryable_429(), retryable_rejected_5xx(), success()], 3, "edit_completed"),
        ([ordinary_500()], 1, "edit_unknown"),
        ([timeout_after_send()], 1, "edit_unknown"),
        ([bad_request_400()], 1, "edit_failed"),
    ],
)
def test_edit_post_budget_and_terminal_classification(tmp_path, responses, expected_posts, terminal):
    transport = SequencedTransport(responses)
    result = module.run_edit_until_terminal(
        run_fixture(tmp_path), request_fixture(tmp_path), clock=FakeClock(), transport=transport
    )
    assert result == terminal
    assert transport.post_count == expected_posts
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest skills/jewelry-white-background/tests/test_run_wawapi_edit_with_retry.py -q`

- [ ] **步骤 3：实现 call slot claim、30/90 秒等待和分类器**

```python
def classify_attempt(attempt: BackgroundEditAttempt, slot: int) -> AttemptDecision:
    if attempt.has_valid_result:
        return AttemptDecision("complete")
    if attempt.http_status == 429 and attempt.task_or_job_id is None and slot < 3:
        return AttemptDecision("retry", delay_seconds=(30, 90)[slot - 1])
    rejected_5xx = (
        attempt.http_status is not None
        and 500 <= attempt.http_status <= 599
        and attempt.rejection_evidence is not None
        and attempt.task_or_job_id is None
    )
    if rejected_5xx and slot < 3:
        return AttemptDecision("retry", delay_seconds=(30, 90)[slot - 1])
    if attempt.request_may_have_been_sent and not attempt.definitive_response:
        return AttemptDecision("unknown")
    return AttemptDecision("failed")

result = run_edit_until_terminal(paths, request, clock=clock, transport=transport)
assert result in {"edit_completed", "edit_failed", "edit_unknown", "already_in_progress"}
```

只有无 task/job ID 的明确 429，或带正式“未接受”机器证据的 5xx 才返回 retry；普通 5xx、超时、断连和遗留 submitting claim 返回 unknown。

- [ ] **步骤 4：写并发 active claim、owner 终止和迟到心跳失败测试**

使用两个进程或可控进程探针；第二执行者遇到 active claim 必须返回 `already_in_progress`，POST 计数不变。

- [ ] **步骤 5：运行任务 6 测试并提交**

运行：`python -m pytest skills/jewelry-white-background/tests/test_run_wawapi_edit_with_retry.py -q`

提交：`git add skills/jewelry-white-background/scripts/run_wawapi_edit_with_retry.py skills/jewelry-white-background/tests/test_run_wawapi_edit_with_retry.py && git commit -m "feat: add persistent Wawapi retry slots"`

---

### 任务 7：只读生成结果的确定性排版

**文件：**
- 新建：`skills/jewelry-white-background/scripts/layout_generated_result.py`
- 新建：`skills/jewelry-white-background/tests/test_layout_generated_result.py`

**接口：**
- 产生：`layout_generated_result(input_path, output_path, manifest_path)`。
- 公开接口不得包含原图、Mask、几何或 Crop Manifest 参数。

- [ ] **步骤 1：写公开签名和输入隔离失败测试**

```python
def test_public_signature_accepts_only_generated_result_and_outputs():
    assert list(inspect.signature(module.layout_generated_result).parameters) == [
        "input_path", "output_path", "manifest_path"
    ]

def test_decoy_pre_generation_assets_do_not_change_output(tmp_path):
    first = run_layout_with_decoys(tmp_path, color=(255, 0, 0))
    second = run_layout_with_decoys(tmp_path, color=(0, 0, 255))
    assert first.output_bytes == second.output_bytes
    assert first.manifest_core == second.manifest_core
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest skills/jewelry-white-background/tests/test_layout_generated_result.py -q`

- [ ] **步骤 3：实现 `layout-algorithm-1.0`**

```python
def round_signed(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)

def layout_generated_result(input_path: Path, output_path: Path, manifest_path: Path) -> dict[str, Any]:
    normalized = load_generated_rgb(input_path)
    background = corner_median_rgb(normalized)
    product_box = detect_product_box(normalized, background)
    composed, metrics = resize_and_place_whole_result(normalized, product_box, background)
    return publish_layout(composed, input_path, output_path, manifest_path, metrics)
```

实现四角背景中位数、强弱前景、8 邻域连通、半开商品框、目标宽 922、高度上限 1800、负粘贴坐标、20MP 中间图门禁和 `sharpening=null`。

- [ ] **步骤 4：写像素确定性、触边失败和高度受限测试**

重复运行必须输出相同像素；商品触边、低置信度和 20MP 中间图必须失败；未受限商品宽度必须为 `922 ± 2`。

- [ ] **步骤 5：运行任务 7 测试并提交**

运行：`python -m pytest skills/jewelry-white-background/tests/test_layout_generated_result.py -q`

提交：`git add skills/jewelry-white-background/scripts/layout_generated_result.py skills/jewelry-white-background/tests/test_layout_generated_result.py && git commit -m "feat: add deterministic generated-result layout"`

---

### 任务 8：最终确认、水印单文件模式和一次性飞书追加

**文件：**
- 修改：`skills/jewelry-white-background/scripts/workflow_confirmations.py`
- 新建：`skills/jewelry-white-background/scripts/deliver_confirmed_result.py`
- 新建：`skills/jewelry-white-background/tests/test_deliver_confirmed_result.py`
- 修改：`C:/Users/Administrator/.codex/skills/yuanyuan-ruyi-watermark/scripts/watermark_images.py`
- 新建：`skills/jewelry-white-background/tests/test_watermark_single_output_contract.py`

**接口：**
- 产生：`create_final_review_bundle()`、`confirm_final_review()`、`deliver_confirmed_result()`。
- 水印脚本新增 `--output`，与 `--input` 一起形成正式单文件模式。

- [ ] **步骤 1：写最终确认绑定和唯一 `delivery_id` 失败测试**

```python
def test_final_confirmation_binds_layout_and_authorizes_both_side_effects(run_paths):
    receipt = module.confirm_final_review(run_paths, reviewer="session-1", uuid_factory=fixed_uuid)
    assert receipt["authorization"] == {"watermark": True, "feishu_append": True}
    assert receipt["delivery_id"] == FIXED_DELIVERY_ID
    with pytest.raises(FileExistsError):
        module.confirm_final_review(run_paths, reviewer="session-2", uuid_factory=fixed_uuid)
```

- [ ] **步骤 2：写水印单输入、显式输出和禁止覆盖失败测试**

```python
def test_watermark_single_output_creates_exact_path_and_rejects_existing_target(tmp_path):
    output = tmp_path / "watermarked" / "SY1537__run__delivery__watermarked.png"
    assert watermark.main(["--input", str(source), "--output", str(output), "--product-id", "SY1537", "--workers", "1"]) == 0
    assert output.is_file()
    assert watermark.main(["--input", str(source), "--output", str(output), "--product-id", "SY1537", "--workers", "1"]) != 0
```

- [ ] **步骤 3：运行失败测试**

运行：`python -m pytest skills/jewelry-white-background/tests/test_watermark_single_output_contract.py -q`

- [ ] **步骤 4：实现水印单文件原子发布**

单文件模式必须恰好产生一个 job；目标存在、零 job、保存失败或原子发布失败时返回非零。保留现有批处理接口，不修改其他调用方语义。

- [ ] **步骤 5：写上传 write-ahead claim、响应落盘优先和唯一 token 失败测试**

```python
def test_upload_claim_is_written_before_only_lark_call(run_paths):
    observed = []
    def runner(command):
        observed.append(load_upload_receipt(run_paths)["status"])
        return completed_nested_response()
    result = module.deliver_confirmed_result(run_paths, runner=runner)
    assert observed == ["uploading"]
    assert result.status == "completed"
    assert result.call_count == 1

@pytest.mark.parametrize("response", [zero_match(), two_matches(), empty_token(), truncated_json()])
def test_ambiguous_upload_response_becomes_unknown_without_retry(run_paths, response):
    runner = CountingRunner(response)
    result = module.deliver_confirmed_result(
        run_paths, watermark_runner=successful_watermark, lark_runner=runner
    )
    assert result.status == "upload_unknown"
    assert runner.call_count == 1
```

- [ ] **步骤 6：实现交付门禁和唯一飞书命令**

```python
result = deliver_confirmed_result(
    paths,
    watermark_runner=watermark_runner,
    lark_runner=lark_runner,
)
assert result.status in {"completed", "watermark_failed", "upload_failed", "upload_unknown", "already_in_progress"}
```

命令固定为 `lark-cli base +record-upload-attachment --base-token <base-token> --table-id <table-id> --record-id <record-id> --field-id <target-field-id> --file <exact-path> --format json --as user`；先写 `response_recorded` 再解析 `data.attachments[record_id][target_field_id]`。

- [ ] **步骤 7：运行任务 8 测试并提交仓库内文件**

运行：`python -m pytest skills/jewelry-white-background/tests/test_workflow_confirmations.py skills/jewelry-white-background/tests/test_watermark_single_output_contract.py skills/jewelry-white-background/tests/test_deliver_confirmed_result.py -q`

提交：`git add skills/jewelry-white-background/scripts/workflow_confirmations.py skills/jewelry-white-background/scripts/deliver_confirmed_result.py skills/jewelry-white-background/tests/test_workflow_confirmations.py skills/jewelry-white-background/tests/test_watermark_single_output_contract.py skills/jewelry-white-background/tests/test_deliver_confirmed_result.py && git commit -m "feat: add confirmed one-shot delivery"`

说明：全局水印 skill 文件不属于本仓库提交范围，但必须保留测试输出和最终文件摘要作为验收证据。

---

### 任务 9：唯一正式编排器和旧入口迁移

**文件：**
- 新建：`skills/jewelry-white-background/scripts/run_white_background_workflow.py`
- 新建：`skills/jewelry-white-background/tests/test_run_white_background_workflow.py`
- 修改：`run_jewelry_base_pipeline.py`
- 修改：`scripts/run_sy1537_sy1552_white_background.py`
- 修改：`tests/test_project_generation_migration.py`
- 修改：`tests/test_run_sy1537_sy1552_white_background.py`

**接口：**
- 产生：`WhiteBackgroundWorkflow.resume(run_root)` 和 CLI `create/confirm-mask/resume/confirm-final/deliver`。
- 两个旧入口只负责参数兼容和调用正式编排器。

- [ ] **步骤 1：写阶段顺序、恢复和终态失败测试**

```python
def test_workflow_stops_at_each_human_hook(run_fixture):
    workflow = module.WhiteBackgroundWorkflow(dependencies=fakes)
    assert workflow.resume(run_fixture).status == "awaiting_mask_confirmation"
    confirm_mask(run_fixture)
    assert workflow.resume(run_fixture).status == "awaiting_final_confirmation"
    confirm_final(run_fixture)
    assert workflow.resume(run_fixture).status == "completed"

def test_terminal_edit_unknown_never_calls_layout_or_delivery(run_fixture):
    write_state(run_fixture, "edit_unknown")
    workflow.resume(run_fixture)
    assert fakes.layout.calls == 0
    assert fakes.delivery.calls == 0
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest skills/jewelry-white-background/tests/test_run_white_background_workflow.py -q`

- [ ] **步骤 3：实现唯一状态驱动编排器**

```python
workflow = WhiteBackgroundWorkflow(dependencies)
created = workflow.create(create_request)
mask_wait = workflow.resume(created.run_root)
assert mask_wait.status == "awaiting_mask_confirmation"
mask_confirmed = workflow.confirm_mask(created.run_root, mask_confirmation)
final_wait = workflow.resume(mask_confirmed.run_root)
assert final_wait.status == "awaiting_final_confirmation"
final_confirmed = workflow.confirm_final(created.run_root, final_confirmation)
completed = workflow.resume(final_confirmed.run_root)
assert completed.status in TERMINAL_STATES | {"already_in_progress"}
```

每次 `resume` 只执行当前状态允许的一步；遇到 Hook、active claim、失败或未知终态立即返回。

- [ ] **步骤 4：把旧入口迁移测试改为委托断言并观察失败**

测试使用 AST 和 spy 断言旧入口不再直接调用 Wawapi helper、水印脚本、飞书上传或旧独立状态机。

运行：`python -m pytest tests/test_project_generation_migration.py tests/test_run_sy1537_sy1552_white_background.py -q`

- [ ] **步骤 5：最小化修改两个旧入口**

旧入口保留能够安全映射的参数；无法映射的 `--retry`、旧授权和跳步参数明确报错。业务函数只构造正式编排器请求并返回其退出码。

- [ ] **步骤 6：运行任务 9 测试并提交**

运行：`python -m pytest skills/jewelry-white-background/tests/test_run_white_background_workflow.py tests/test_project_generation_migration.py tests/test_run_sy1537_sy1552_white_background.py -q`

提交：`git add skills/jewelry-white-background/scripts/run_white_background_workflow.py skills/jewelry-white-background/tests/test_run_white_background_workflow.py run_jewelry_base_pipeline.py scripts/run_sy1537_sy1552_white_background.py tests/test_project_generation_migration.py tests/test_run_sy1537_sy1552_white_background.py && git commit -m "feat: route legacy entries through workflow orchestrator"`

---

### 任务 10：Skill 合同、离线 A 级验收和最终回归

**文件：**
- 修改：`skills/jewelry-white-background/SKILL.md`
- 按需修改：`skills/jewelry-white-background/agents/openai.yaml`
- 修改：`skills/jewelry-white-background/tests/test_skill_contract.py`
- 修改：`skills/jewelry-white-background/tests/test_prompt_contract.py`

**接口：**
- 文档只描述新顺序、两次 Hook、2.1 几何、裁剪、最多三个 call slot、确定性排版和一次性交付。

- [ ] **步骤 1：先更新 Skill 合同测试并观察旧文档失败**

测试必须断言唯一顺序为：检测增强 → 2.1 几何 → 裁剪 → 三路 Mask → Mask Hook → Wawapi → 排版 → 最终 Hook → 水印 → 飞书；同时禁止旧 `mask_review` 内嵌确认、上传前授权、结果 QC 和上传回读。

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest skills/jewelry-white-background/tests/test_skill_contract.py skills/jewelry-white-background/tests/test_prompt_contract.py -q`

- [ ] **步骤 3：按规格重写 Skill 正式流程段落**

保持 Prompt 权威来源不变；只有 `agents/openai.yaml` 与新触发语义冲突时才修改元数据。

- [ ] **步骤 4：运行仓库全部受控测试**

运行：

```powershell
python -m pytest tests skills/jewelry-white-background/tests -q
```

预期：仓库内测试全部通过；不得把 `outputs/` 下的历史临时测试纳入 A 级结果。

- [ ] **步骤 5：运行静态与文档验证**

```powershell
python -m compileall run_jewelry_base_pipeline.py scripts skills/jewelry-white-background/scripts
git diff --check
rg -n "vision-geometry-mask-2\.0|mask_review|record get|record list|automatic_wawapi_edit_allowed\s*=" skills/jewelry-white-background run_jewelry_base_pipeline.py scripts
```

逐条确认命中仅存在于明确的 legacy 只读说明或测试反例中。

- [ ] **步骤 6：保存 A 级验收报告并提交**

在最终回复中记录：测试数量、失败数量、spec/计划路径、未执行的 B/C 级验收。不得调用真实 Wawapi 或写入飞书。

提交：`git add skills/jewelry-white-background/SKILL.md skills/jewelry-white-background/agents/openai.yaml skills/jewelry-white-background/tests/test_skill_contract.py skills/jewelry-white-background/tests/test_prompt_contract.py && git commit -m "docs: align jewelry workflow contract"`

---

## 计划自检

- 规格覆盖：运行身份、双摘要、检测、2.1 几何、裁剪、三路 Mask、两次确认、Wawapi 重试、排版、水印、飞书和旧入口均有独立任务。
- 占位扫描：未发现空函数体、模糊步骤或未决定的行为。
- 类型一致：所有后续任务统一消费 `RunPaths`、`ClaimStore`、`transition_state`；确认模块和编排器使用同一运行目录与状态真相源。
- 验收边界：本计划只完成 A 级离线合同验收；B 级真实 Wawapi 与 C 级飞书写入必须再次取得用户针对测试 SKU 的单独授权。
