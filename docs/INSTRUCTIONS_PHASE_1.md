# 第一阶段实施指令

本阶段需要直接修改代码、运行测试并报告结果，不得只给出设计方案。

## 目标

实现批次管理和可恢复的 SQLite 图片任务队列。上传请求只保存图片并排队，不执行 OCR。

## 必须实现

### 数据库迁移

1. 新增 `schema_migrations(version, applied_at)`。
2. 新增 `batches(id, batch_no, name, status, created_at, updated_at, completed_at)`。
3. 新增 `batch_images(batch_id, evidence_id, original_name, upload_order, created_at)`。
4. 为 evidence 增加 `processing_status`、`attempt_count`、`started_at`、`finished_at`、`locked_at`。
5. 为旧 evidence 创建唯一 `LEGACY-IMPORT` 批次并设置为 completed。
6. 所有迁移可重复执行，不得损坏旧数据。

### 批次编号

- 格式为 `YYYYMMDD-001`；
- 同日自动递增；
- 使用事务和唯一约束避免重复；
- 批次名称为空时使用批次编号。

### 上传

实现 `POST /batches/<int:batch_id>/upload`：

- 验证批次和文件格式；
- 分块保存并同时计算 SHA-256；
- 禁止 `item.read()` 读取整个文件；
- 新图片创建 evidence 并设为 queued；
- 重复图片复用 evidence；
- 建立 batch_images 关系；
- 单张失败不影响其他图片；
- 上传完成后立即返回批次页面。

上传请求不得调用 `ocr_engine.recognize()`、`extract_vehicle_fields()` 或 `merge_evidence()`。

### 任务接口

实现：

- `claim_next_queued_evidence()`；
- `mark_evidence_completed()`；
- `mark_evidence_pending()`；
- `mark_evidence_failed()`；
- `requeue_stale_processing_jobs()`。

领取使用原子事务；两个 Worker 不能领取同一任务。超时 processing 任务可以恢复为 queued。

### 页面

实现：

- `GET /batches`；
- `GET /batches/new`；
- `POST /batches/new`；
- `GET /batches/<int:batch_id>`；
- 批次上传路由；
- 顶部“批次管理”导航。

批次列表显示编号、名称、时间、总数、queued、processing、completed、pending、failed 和批次状态。详情页显示图片文件名、顺序、状态和错误。

原 `/upload` 不得删除，可重定向到新建批次。

### Worker 骨架

允许创建 `worker.py` 和 `start_worker.bat`，但本阶段不得加载 OCR。骨架只验证任务领取接口和安全退出。

## 禁止事项

本阶段不得实现 GPU、CUDA、OCR、车辆匹配重写、批次 Excel、最新总表、Redis、Celery、PostgreSQL、Docker、云部署或登录系统。

## 工作顺序

1. 阅读 `AGENTS.md` 和全部必读文档。
2. 检查代码和测试。
3. 给出简短计划。
4. 完成数据库迁移。
5. 完成批次 API 和上传改造。
6. 完成任务接口和页面。
7. 增加测试。
8. 运行全部测试和编译检查。
9. 按 `AGENTS.md` 格式报告。

除非出现会改变业务结果的真实阻塞，否则不要中途要求用户重复确认已确定需求。
