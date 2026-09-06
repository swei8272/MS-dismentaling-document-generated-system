# 第二阶段验证报告

验证日期：2026-09-06

平台：Windows / Python 3.13.5

数据范围：全部为程序生成的合成图片和隔离临时数据库，未使用真实车辆资料。

## 自动化与静态检查

- `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp-issue4`：
  `39 passed in 24.66s`。
- `.venv\Scripts\python.exe -m compileall -q app.py config.py database.py storage.py worker.py scripts tests`：通过。
- `node.exe --check static\batch_upload.js`：通过。
- `.venv\Scripts\python.exe -m pip check`：`No broken requirements found.`。
- `git diff --check`：通过，仅有 Windows 工作区 LF→CRLF 提示。
- 静态扫描：业务 Python 未出现硬编码项目盘符、无参数整文件 `read()`、上传路径
  OCR/字段提取/车辆合并调用；领取接口只存在于数据库和 Worker 自检中。

新增自动化覆盖逐文件 JSON、请求内唯一标识、同名不同图、同图不同名、跨批次
复用、损坏图混合上传、响应丢失重传、单文件/组字节/组数量限制、普通表单
跨请求失败标识、上传失败与
OCR failed 分离、手动重试解决失败记录、刷新持久化和图片分页。第一阶段
`attempt_count` 领取版本栅栏的 9 个参数化回归用例仍通过。

## 500 张 Windows / Waitress 验证

服务使用 `scripts/phase2_validation.py serve` 启动在本机回环地址，数据库和图片
目录均位于被 Git 忽略的 `.phase2-validation`。图片由同一工具生成并检查：

- 数量及内容：500 张，500 个不同 SHA-256；
- 分辨率：全部 1600×1200；
- 格式：JPEG，带独立噪声、色彩、编号和线条；
- 单图大小：712,184–1,002,456 bytes，平均 876,148 bytes；
- 总净大小：438,074,092 bytes（约 417.78 MiB）；
- 分组：20 组，每组 25 张、单请求约 20.2–21.4 MiB、仅一个请求在途；
- 总耗时：64.811 秒，包含第 2 组中途断开后的重传和第 3 组响应丢失后的等待/重传。

第 3 组在完整发送后主动丢弃响应，随后安全重传；重传逐文件返回 25 个
`already_in_batch`。可观察响应合计为 475 `added`、25
`already_in_batch`、0 `failed`。最终数据库为 500 evidence、500 个不同
SHA-256、批次 500 个关联，磁盘 500 个哈希文件，全部为 `queued`；OCR
failed 为 0，未解决上传失败为 0。

上传期间独立进程每秒访问 `/batches/1/status`：

- 101 个样本；
- HTTP 失败 0；
- 响应时间 p95 66.521 ms；
- 最大响应时间 189.011 ms；
- 服务端 Python 子进程生命周期峰值工作集 52,973,568 bytes（约 50.52 MiB）；
- 服务端 `tracemalloc` 峰值 4,347,140 bytes（约 4.15 MiB）；
- 上传客户端 `tracemalloc` 峰值 66,957,492 bytes（约 63.86 MiB）。

最终代码另行启动全新隔离 Waitress：`GET /batches` 返回 HTTP 200 并包含
“批次管理”，进程内存接口返回工作集 46,022,656 bytes。验证后所有本任务
启动的 Waitress 进程均已停止。

## 损坏图片和网络中断

独立批次先在 multipart 请求中途断开，然后重传同组的两张有效合成图片和一张
损坏 JPEG。两张有效图片继续成功复用，损坏图片返回可理解的逐文件错误并形成
独立 `upload_failures` 记录，OCR failed 保持 0。随后以失败 ID 和有效图片执行
手动重传，失败记录变为 resolved；最终批次 3 张、queued 3、未解决上传失败 0。

## 浏览器验证限制

本次未能完成真实浏览器交互、浏览器内存测量或截图。应用内浏览器在初始化时
持续返回 `windows sandbox failed: helper_unknown_error: setup refresh had errors`。
仓库内旧 `.pytest_cache` 同时存在 Windows ACL 拒绝访问；已确认它被 Git 忽略，
但当前用户和提升后的任务进程均无法读取、重置 ACL、取得所有权或删除。按照
浏览器技能约束，未改用独立 Playwright/其他浏览器控制面伪装验证结果。

因此，自动分组、传输进度、“服务器确认中”、最多 2 次自动重试、后续组继续、
手动重试和刷新提示已由代码、JavaScript 语法检查、接口测试及 Waitress 场景覆盖，
但不能替代真实浏览器行为验收。PR 不附浏览器截图，浏览器内存明确记为未测量；
在 Codex 工作区沙箱恢复后仍需补做这一项。
