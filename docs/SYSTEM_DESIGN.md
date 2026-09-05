# 系统设计

## 部署模型

系统部署在一台配有 RTX 3090 的 Windows 主机上。主机同时运行 Web 服务、SQLite 数据库和独立 OCR Worker。其他工作人员通过厂内局域网访问 Web 页面。

第一版不依赖云服务、Redis、Celery、PostgreSQL 或 Docker。

## 组件

| 组件 | 职责 |
|---|---|
| Flask Web | 批次创建、分块上传、进度查看、人工确认和导出入口 |
| Waitress | Windows 下运行 Web 服务 |
| SQLite | 保存批次、图片证据、车辆、冲突和任务状态 |
| OCR Worker | 独立领取任务并执行 GPU OCR |
| Parser | 从 OCR 文本提取 14 个业务字段 |
| Matcher | 按车牌号和 VIN 合并车辆信息 |
| Excel Exporter | 生成批次表和最新总表 |
| File Storage | 保存原始图片、表格、日志和备份 |

## 数据流

1. Web 创建批次。
2. 浏览器将大量图片拆成小组上传。
3. Web 流式保存图片、计算 SHA-256，并写入等待队列。
4. Web 请求立即结束，用户进入批次进度页面。
5. OCR Worker 原子领取一张等待图片。
6. Worker 进行预处理、OCR 和字段提取。
7. Matcher 根据强标识关联或创建车辆。
8. 不确定图片进入待确认。
9. 批次状态随任务结果更新。
10. 完成后可生成批次表和最新总表。

## 核心数据实体

### vehicles

保存车辆当前最新的已确认信息。车牌号和 VIN 是主要检索字段。

### evidence

保存全局唯一图片、SHA-256、OCR 原文、提取结果、处理状态和错误信息。相同图片不重复 OCR。

### batches

保存一次上传批次的编号、名称、状态和时间。

### batch_images

建立批次和唯一图片之间的多对多关系。跨批次重复图片复用同一 evidence。

### conflicts

保存同一车辆字段的不同非空值，等待人工确认。

### batch_vehicles（后续阶段）

记录每个批次涉及的车辆，用于批次表格生成和追溯。

### schema_migrations

记录已执行的数据库迁移版本。

## 状态机

图片任务状态：

`queued → processing → completed`

异常分支：

- `processing → pending`：OCR完成但无法可靠归档；
- `processing → failed`：处理异常；
- `failed → queued`：人工或自动重试；
- 超时的 `processing → queued`：程序重启或任务中断恢复。

批次状态：

- `created`：批次已建立；
- `uploading`：正在接收图片；
- `queued`：图片已上传并等待处理；
- `processing`：至少一张图片正在处理；
- `needs_review`：存在待确认图片；
- `partial_failed`：存在失败任务；
- `completed`：任务处理完成且没有未解决异常。

## 并发设计

- Web 不执行 OCR。
- Worker 使用原子数据库事务领取任务。
- 第一版只启动一个 GPU Worker。
- SQLite 启用 WAL、foreign keys 和 busy timeout。
- 完成基准测试后才能考虑两个并行 Worker。

## GPU OCR 设计

GPU OCR 在批次和任务基础设施完成后接入。启动时检查 CUDA provider；可用时使用 RTX 3090，不可用时明确显示原因并允许受控 CPU 降级。OCR 模型只在 Worker 中加载一次，Web 进程不得加载模型。

## 文件写入

- 图片按内容哈希保存，避免文件名冲突。
- 上传使用流式读取，避免整张图片进入内存。
- Excel 先生成临时文件，成功后原子替换正式文件。
- 数据库和图片按计划备份；SQLite WAL 相关文件必须与数据库一起正确处理。

## 安全边界

- 服务默认仅监听本机或厂内局域网。
- 不直接暴露公网。
- 上传仅允许批准的图片扩展名并验证实际图像。
- 文件名必须安全化，不能允许路径穿越。
- 页面不显示底层异常堆栈或本地敏感路径。
