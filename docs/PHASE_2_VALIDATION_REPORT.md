# 第二阶段验证报告

本报告把 2026-09-06 的历史结果与 2026-09-07 审查修复后的结果分开记录。
全部验证均使用合成图片和隔离数据库，未读取、修改、删除或重建真实业务数据。

## 最新状态：图片列表刷新修复（2026-09-07）

本节为最新交付；下方一、二节保留此前 Windows 验证与历史结果。

- 验证代码提交：`26f232093d7e4e90b62f2b6cff1441f9cb725e98`。
- 基线：`1613ec18dca0fee88a51af53ef17919597cfe747`；继续原 PR #5 分支，未合并。
- 环境：Linux / Python 3.12.13 / Node.js v24.19.0。当前环境不能访问用户的
  Windows 正式部署目录，所有测试使用临时数据库、合成图片。

### 修复内容

此前上传结束只刷新统计和失败记录，“批次图片”仍停留在打开页面时的状态。
本次将表格及分页抽成共用 Jinja 片段，通过现有图片接口的 `view=table` 参数
读取。初次页面及局部更新使用同一模板，文件名继续自动转义。

- 上传结束后强制补一次最新读取；状态轮询时更新当前页。
- 默认每页 50 条，最多 100 条；COUNT 与当前页在同一个 SQLite 读快照中读取。
- 分页链接局部切换，上传表单和失败替换选择器不被替换，不清空已有 File 对象。
- 图片列表请求串行，普通轮询合并；迟到的旧页响应不能覆盖用户新选择的页码。
- 更新失败保留旧表格并显示提示；后续轮询或“更新图片列表”按钮可恢复。
- 原图片 JSON 字段保留，新增 `page_count`；越界页码收敛至有效页。
- 无新增迁移，现有版本 1/2/3 和 attempt_count 领取保护保持不变。

关键修改：`app.py`、`database.py`、`static/batch_images.js`、
`static/batch_upload.js`、`templates/_batch_images.html`、
`templates/batch_detail.html`、新增 Python/JS 回归测试及 README/验收标准。

### 本次验证

- `python -m pytest -q -p no:cacheprovider --basetemp <唯一临时目录>`：
  **54 passed in 2.21s**，包含原 52 项及新增 2 项。
- `node --test tests/js/*.test.js`：**11 passed**，包含原 7 项及新增 4 项。
- Python compileall、三个前端 JS 文件的 `node --check`、`pip check`、
  `git diff --check`：通过。
- Waitress 在同一隔离验证进程的临时端口启动：`GET /batches` HTTP 200，
  含“批次管理”；验证服务已关闭。
- 新增覆盖空状态→有图、50→51 张新增分页、第二页记录、页码/每页上限、
  文件名 HTML 转义、旧页迟到响应、轮询合并、上传结束强制读取、失败保留旧数据及恢复。
- 未调用 OCR、车辆合并、Excel 导出或第三阶段功能；未触碰真实数据库及业务目录。

原始摘要：[ui_refresh.json](validation/issue4-ui-refresh/ui_refresh.json)。

### 浏览器与平台限制

本次通过 control-browser 技能成功连接云端 Chrome，但打开隔离本地页面时
先发生 `Transport closed`，随后浏览器明确返回 `net::ERR_BLOCKED_BY_CLIENT`。
没有发生应用内真实交互；未取得应用截图或浏览器内存。按浏览器技能约束未改用
其他浏览器控制通道绕过限制，Node 回归结果不代替真实浏览器验收。

因此第二阶段仍未全部验收。后续需在可访问本地服务的受支持浏览器中验证：

1. 选择 51 张不同合成图片上传，无手动刷新时表格由空变为 50 行并出现第二页。
2. 切到第二页后补传图片，统计、当前页内容及页数正确更新；提前选择的其他文件保留。
3. 上传中切页及延迟响应时，旧页不能覆盖新页；列表接口失败后能恢复。
4. 完成此前要求的分组、服务器确认、有限重试、失败分页/替换、断网补写、刷新恢复、
   响应丢失重传场景，并保留合成截图及浏览器内存测量。
5. 对当前代码完成 Windows / Waitress 启动与必要回归。

此前 Windows 的 500 张、137.401 秒等数据仍只对应 `f0e72da`，本次未重跑，
不得转记为本次代码或浏览器的验证结果。

## 一、本次审查修复验证（2026-09-07）

### 1. 验证版本与环境

- 验证代码提交：f0e72dabcf0f8c8a02bd28ffb1fc2456d1600779。
- 审查基线：152d9474bc672cf169cf1b686c39952aa95fd47c。
- 分支：codex/issue-4-batch-upload-progress。
- 平台：Windows 10 10.0.19045 SP0，64 位。
- Python：3.13.5（Anaconda build，MSC v.1929）。
- Web 服务：Waitress 3.0.2，8 个线程，本机回环地址。
- JavaScript：Node.js v24.19.0。
- 数据：.phase2-validation-fix 下的隔离数据库、上传目录和 500 张合成图。

验证前已核对 PR #5 最新提交和评论：远端仍停留在审查基线，没有后续修复或
未处理评论，因此所有修复都从该基线继续完成。

### 2. 发现的问题与修复

1. 状态接口曾只返回最近 25 条失败记录，轮询会覆盖前端关联状态。现在状态
   轮询只返回未解决数量和轻量修订标识；完整失败记录由默认 25 条、最大 100
   条的分页接口读取，COUNT 和当前页处于同一个 SQLite 读快照。
2. 原实现按文件名和大小猜测失败关联。现在每条失败记录提供独立文件选择器，
   上传携带精确 failure ID 和 batch 内 client_id；名称和大小允许变化，服务端
   拒绝错误 client_id、跨批次和已经由其他内容解决的关联。
3. 断网补写失败曾被直接忽略。现在浏览器 outbox 明确区分 pending 和 saved，
   只在 localStorage 保存 client_id、显示名、大小、失败类型和创建时间，不保存
   图片内容或 File 对象；页面加载、online 事件和手动按钮触发有界重试，每轮
   最多自动重试 2 次并退避。POST/GET/status 都有 15 秒超时，分页恢复另有最高
   30 秒退避。
4. 迟到失败补写可能重开已成功记录。迁移 3 新增 upload_receipts，并给
   upload_failures 增加 resolved_evidence_id；成功回执、evidence、batch_images
   和失败解决在同一事务提交，重复或迟到补写返回 already_confirmed。
5. 两个标签页并发使用同一 client_id 上传不同内容时，失败请求可能留下未引用
   的正式文件。现在图片先在 .incoming 校验和计算 SHA-256，取得 BEGIN
   IMMEDIATE 写锁并二次校验回执后才落到正式内容寻址路径；正常并发冲突或事务
   回滚只清理本请求新建的文件，且在释放写锁前完成。
6. 前端还修复了同步尾声新 outbox 项漏传、分页响应覆盖刚选择的 File、迟到
   saved ACK 覆盖 confirmed、刷新恢复条目与显式替换生成幽灵行，以及传输进度
   提前显示 100% 等竞态。

已有第一阶段 attempt_count 领取版本保护未改动。上传、状态和失败分页路径不
调用 OCR、字段提取、车辆合并或任务领取；未实现 GPU OCR、Excel 导出或第三
阶段功能。

### 3. 自动化与静态验证

在验证提交上执行：

    python -m pytest -p no:cacheprovider --basetemp .pytest-tmp-verified-f0e72da

结果：52 passed in 96.19s。审查基线已有 39 项全部保留，新增 13 项。新增覆盖
26 条和 61 条失败记录分页恢复、替换后名称/大小变化、同名不同图、重复重试、
跨批次拒绝、断网补写幂等、成功与补写交错、两个标签页同 key 不同内容并发、
事务回滚文件清理，以及 v2→v3 兼容迁移。全部重试成功后未解决失败数为 0。

其他检查：

- tests/test_migrations.py 的 6 项迁移测试包含重复 migrate、保留表/列后重跑
  迁移 3、v1/v2 升级和 PRAGMA foreign_key_check，均随完整测试通过。
- Node --test tests/js/batch_upload_state.test.js：7 passed，包含分组边界、仅元
  数据刷新恢复、重复补写/确认幂等、already_confirmed 交错、最多 2 次重试和
  恢复条目复用。
- Node --check static/batch_upload.js 和 static/batch_upload_state.js：通过。
- .venv\Scripts\python.exe -m compileall -q app.py config.py database.py
  storage.py worker.py scripts tests：通过。
- .venv\Scripts\python.exe -m pip check：No broken requirements found。
- git diff --cached --check：通过。
- 静态扫描只在数据库/Worker 自检定义任务领取；Web 上传和状态路径没有 OCR、
  字段提取或车辆合并调用。
- Waitress 最小启动：GET /batches 返回 HTTP 200 且包含“批次管理”；验证后
  5054 端口监听数为 0。

### 4. 500 张 Windows / Waitress 规模验证

命令：

    .venv\Scripts\python.exe scripts\phase2_validation.py generate .phase2-validation-fix\images --count 500 --width 1600 --height 1200
    .venv\Scripts\python.exe scripts\phase2_validation.py serve .phase2-validation-fix\server --port 5054
    .venv\Scripts\python.exe scripts\phase2_validation.py monitor http://127.0.0.1:5054 1 .phase2-validation-fix\stop-500.signal docs\validation\issue4-fix\raw_monitor.json --commit-sha f0e72dabcf0f8c8a02bd28ffb1fc2456d1600779
    .venv\Scripts\python.exe scripts\phase2_validation.py upload http://127.0.0.1:5054 .phase2-validation-fix\images docs\validation\issue4-fix\raw_upload.json --group-size 25 --batch-id 1 --commit-sha f0e72dabcf0f8c8a02bd28ffb1fc2456d1600779

图片与分组：

- 500 张 JPEG，500 个不同 SHA-256，全部 1600×1200。
- 单图 712,306–1,002,547 bytes，平均 876,248 bytes。
- 总净大小 438,123,918 bytes（约 417.83 MiB）。
- 20 组，每组 25 张；每组净大小 21,189,799–22,474,469 bytes。
- multipart 请求大小 21,202,592–22,487,262 bytes，低于 64 MiB。
- 单文件上限配置 16 MiB，组上限 64 MiB；并发请求数 1。

上传窗口为 2026-09-07T01:43:54.132Z 至 01:46:11.532Z，共 137.401 秒。
第 2 组模拟请求中途断开后重传；第 3 组完整发送后丢弃响应并安全重传。可观察
响应为 478 added、0 reused、22 already_in_batch、0 failed。第 3 组重传时，
原请求已确认 22 张，另 3 张由重传请求先确认；最终仍只有 500 份数据。

最终状态：

- evidence：500；不同 SHA-256：500。
- batch_images：500；upload_receipts：500。
- 正式磁盘哈希文件：500；.incoming 残留 part：0。
- queued：500；OCR failed：0；未解决 upload_failures：0。
- 未运行 OCR，因而 queued 是预期状态。

### 5. 性能与内存口径

状态采样窗口为 2026-09-07T01:43:27.231Z 至 01:46:46.361Z，共 199.130 秒；
它比上传早 26.901 秒开始、晚 34.829 秒结束，完整覆盖上传窗口。

- 每秒采样 /batches/1/status，共 199 个样本。
- 状态 HTTP 失败 0；metrics HTTP 失败 0。
- 状态响应 p95 135.532 ms；最大 373.694 ms。
- 服务端采样工作集最大值：54,095,872 bytes（约 51.59 MiB）。
- 服务端进程生命周期峰值工作集：56,045,568 bytes（约 53.45 MiB）。
  来源是服务端进程调用 Windows GetProcessMemoryInfo，并读取
  PROCESS_MEMORY_COUNTERS.PeakWorkingSetSize；报告值是采样窗口内观察到的
  该生命周期高水位最大值。
- 服务端 tracemalloc 峰值：4,962,094 bytes（约 4.73 MiB）。
- Python 上传客户端 tracemalloc 峰值：66,981,147 bytes（约 63.88 MiB）。
  这不是浏览器内存，不能作为浏览器内存证据。
- 浏览器内存：未测量。

原始合成测量摘要：

- [raw_upload.json](validation/issue4-fix/raw_upload.json)
- [raw_monitor.json](validation/issue4-fix/raw_monitor.json)
- [raw_mixed.json](validation/issue4-fix/raw_mixed.json)
- [raw_startup.json](validation/issue4-fix/raw_startup.json)
- [raw_browser_attempt.json](validation/issue4-fix/raw_browser_attempt.json)

### 6. 损坏图片、断线和精确替换

隔离批次 2 混合两张有效图片和一张损坏 JPEG，并先模拟 multipart 中途断开。
重传后两张有效图成功复用，损坏图返回逐文件错误并产生独立 upload_failures；
随后使用该失败 ID、同一 client_id 和一张内容/大小不同的有效图明确替换。
最终批次 3 张、queued 3、OCR failed 0、未解决上传失败 0。

### 7. 真实浏览器验收状态

浏览器验收未完成。

第一次按应用内浏览器技能连接本地页面，在浏览器启动前返回：

    windows sandbox failed: helper_unknown_error: setup refresh had errors

仓库中预先存在的 .pytest_cache 同时拒绝当前用户和提升任务进程读取 ACL；本次
没有修改、删除或替换该缓存。随后按支持的 Chrome 控制技能再次连接同一 URL，
仍在浏览器启动前返回：

    trusted Node process exited unexpectedly; kernel reset, rerun your request

因此没有发生真实浏览器页面交互，也没有可保留的浏览器截图或浏览器内存。
自动分组、串行请求、单文件超限继续后续文件、服务器确认中、最多 2 次重试、
失败组不阻塞后续组、手动仅重试未确认文件、刷新恢复、失败分页和替换文件等，
已由代码审查、JavaScript 回归、接口测试和 Waitress 场景覆盖，但这些不能替代
真实浏览器验收。本报告不宣称第二阶段全部通过；待宿主浏览器沙箱恢复后仍需补
做浏览器交互、截图和浏览器内存记录。

## 二、历史结果（2026-09-06，仅供追溯）

历史代码在 152d947 前完成过 39 项 pytest 和一次 500 张合成图验证：39 passed
in 24.66s；图片总净大小 438,074,092 bytes，上传 64.811 秒，最终 evidence、
batch_images 和磁盘文件均为 500，未解决上传失败为 0；状态接口 101 个样本，
p95 66.521 ms、最大 189.011 ms、HTTP 失败 0。

历史报告把 52,973,568 bytes 称为“服务端生命周期峰值工作集”，该口径不准确。
旧脚本实际读取 WorkingSetSize 并取每秒样本最大值，现更正为“服务端采样工作集
最大值”。旧服务端 tracemalloc 峰值为 4,347,140 bytes；旧 Python 上传客户端
tracemalloc 峰值为 66,957,492 bytes，后者同样不是浏览器内存。

历史运行也没有完成真实浏览器验收，而且未覆盖本次发现的失败记录分页覆盖、
替换后大小变化、断网 outbox 交错和同 client_id 并发落盘问题。因此历史结果
不能作为当前修复版本的验收结论。
