# 第二阶段证据校正与补拍（2026-09-08）

程序 SHA：`7b71407bed03eda7a65e4e7803267fba956cd213`。
运行 checkout / 审查报告基线：`49a85633ffc454b7b2ed4bf1cc60c3c98799342f`。
本次仅文档及证据变更，没有应用、测试、迁移或领取保护改动。运行前本地/远端
PR #5 均为该基线，无新评论。全部数据合成；旧证据保留，不覆盖旧 PNG/JSON。

## 环境与隔离数据

Windows 10 10.0.19045、Python 3.13.5、Waitress 3.0.2 / 8 线程、
Chrome 152.0.7977.82（受支持扩展文件选择器及页面按钮）、Node.js v24.19.0。
所有时间 UTC，当地 Brisbane +10。没有通过接口上传替代浏览器。

合成根：`%LOCALAPPDATA%\Temp\dgm-phase2-browser-9def9aedf733486e9cc633a1d4a95335`。
以下用 `$phase2BrowserRun` 代表该已解析根，不代表生产部署目录。

```powershell
.\.venv\Scripts\python.exe scripts\phase2_browser_harness.py "$phase2BrowserRun\server-evidence-correction-20260908" --port 5057
.\.venv\Scripts\python.exe scripts\phase2_browser_harness.py "$phase2BrowserRun\server-500-final-20260908" --port 5058
```

两个根均有 `SYNTHETIC_BROWSER_VALIDATION` 标记，SQLite 为各自 `data/validation.db`，
图片为各自 `uploads`；5057 是本次新隔离根，5058 是保留的原 500 张批次。
没有启动生产入口、修改真实数据库/图片、系统 ACL 或 sandbox 设置。
合成 JPEG 直接复用根下 images 中的 1600×1200 图片；损坏图复用仓库已保留的
`.phase2-validation-browser-fixtures-20260908/damaged-00.jpg` 至 `damaged-25.jpg`。

## 截图与同一时刻 DOM

每张 PNG 都有同名（36 去掉 `-full`）`.dom.json`：保存截图前后 DOM、UTC 时间、
代码 SHA、隔离批次及真实操作步骤。先等待预期状态，再定位标题/画面后捕获；
六张文件均使用 `view_image` 重新打开，逐张核对像素。
[visual_review.json](visual_review.json) 记录检查结论、文件 SHA-256 和大小；
其中时间是复核记录写入时间，不伪称历史截图时间。

| 新截图 | 捕获窗口（2026-09-08 UTC） | 批次/实际结果 | 替代关系 |
|---|---|---|---|
| [31](31-failures-page2.png) / [DOM](31-failures-page2.dom.json) | 01:59:32.660–01:59:33.990 | 5057 批次 1：第 2/2 页，仅 1 条 ID 1，总失败 26 | 替代旧 12 的第二页证明；11/12 都不合格 |
| [32](32-outbox-saved-unresolved.png) / [DOM](32-outbox-saved-unresolved.dom.json) | 02:02:04.651–02:02:05.599 | 5057 批次 2：元数据已保存，ID 27 未解决，图片 0 | 旧 17 是待同步；18 才是此类已保存/未解决状态 |
| [33](33-outbox-replaced-final.png) / [DOM](33-outbox-replaced-final.dom.json) | 02:07:04.921–02:07:07.759 | 5057 批次 2：明确替换后保存 1/未解决 0/图片 1 行 | 补齐旧 18 未证明的替换终态 |
| [34](34-mixed-saved2-failed1.png) / [DOM](34-mixed-saved2-failed1.dom.json) | 02:08:17.628–02:08:20.739 | 5057 批次 3：保存 2/未解决 1/OCR failed 0，ID 28 | 对照旧 25 部分成功状态；旧 24 是上传前 |
| [35](35-mixed-replaced-final.png) / [DOM](35-mixed-replaced-final.dom.json) | 02:09:14.181–02:09:18.358 | 5057 批次 3：保存 3/未解决 0/OCR failed 0/图片 3 行 | 补齐旧 25 未证明的替换终态 |
| [36](36-retained-500-page2-full.png) / [DOM](36-retained-500-page2.dom.json) | 02:03:20.150–02:03:24.932 | 5058 保留批次 1：第 2/10 页、50 行、序号 51–100，保存 500 | 替代旧 28 的第二页证明；旧 28 实为第 1/10 页 |

操作：

1. 新批次 1 选择 26 张损坏图，按 25+1 上传，失败总数达到 26 后刷新，点击失败
   下一页；确认仅 ID 1 后拍 31。第一次 fullPage 捕获返回 CDP
   `Page.captureScreenshot` 5000ms 超时且未生成文件；重新定位标题后视口捕获成功。
2. 新批次 2 开启隔离控制页 outbox 503 故障，选 synthetic-0080 上传，观察待同步并
   刷新。恢复控制页 reset，等待有限自动补写，拍 32；不是本轮手动点击再次同步。
   在 ID 27 的独立选择器选名称/大小不同的 synthetic-0081，点击该行上传，等待 1/0 拍 33。
3. 新批次 3 同组选 synthetic-0084、damaged-00、synthetic-0085，上传后等待 2/1 拍 34。
   在 ID 28 行选择 synthetic-0086，仅上传此替换文件，等待 3/0/OCR failed 0 拍 35。
4. 重开原 500 张隔离批次，点击图片下一页，读取所有序号为 51–100 且页码 2/10，
   稳定后拍整页 36。未重新上传、未重跑性能、未新测内存。

33/35 的未解决失败为 0 时应用会隐藏整个失败面板。图上可见本轮“失败/未确认 0”，
服务端未解决 0 由同一捕获窗口前后 DOM + 后续只读 SQLite 一致确认；没有改写 DOM。
31 的 26 条失败有意保留供分页复核。本次没有再做这 26 条全部替换；原全部解决证据
仍为旧 29 + `final_26_dom.json`、原只读数据库摘要及本次回归测试。

旧 `final_browser_dom.json` 时间为 **2026-09-07T23:21:07.667Z**，checkout e54f7bb /
应用 0acd823；新 36 是应用 7b71407 在保留批次的另一次观察，不能拼作同一时刻证据。
500 张性能继续引用旧目录 `verified_*` 的 7b71407 历史独立运行；补拍使用的是旧
`final_*` 对应的保留数据库，两者没有混用。

## 新执行的完整测试日志

历史 `verified_checks.json` 只有摘要，没有找回当时完整 stdout/stderr。因此这里是
**新运行**，不是历史日志。`run_checks.py` 用 subprocess 直接重定向并拒绝覆盖，
`test_run.json` 保存命令、时间、退出码及进程墙钟。

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp .pytest-tmp-evidence-20260908T015527Z
node --test tests/js/batch_upload_state.test.js tests/js/batch_images.test.js
```

| 检查 | 新运行 UTC 窗口 | 实际结果 | 全量输出 |
|---|---|---|---|
| pytest | 01:55:27.870822–01:57:49.364734 | 60 passed in 134.17s；进程墙钟 141.495s；exit 0 | [stdout](pytest.stdout.txt)、[stderr](pytest.stderr.txt) |
| JS | 01:57:49.364832–01:57:49.737984 | 17 passed，0 failed；Node 181.7648ms、进程墙钟 0.373s；exit 0 | [stdout](javascript.stdout.txt)、[stderr](javascript.stderr.txt) |

两个 stderr 均 0 bytes，stdout 没有截断；原 39 项保留，6 项迁移测试包含在 60 项内。
原历史 59.65s 不改写为本次 134.17s。程序未改变，无新增迁移，也未用自动化代替浏览器。

`check_delivery.py` 另核对 PNG 哈希、前后 DOM 预期、JSON/JSONL 和文档链接，
并执行 Python compileall、三个前端 `node --check`、`pip check`、`git diff --check`
与 7b71407 的程序差异检查；全部退出码 0。命令、实际新时间及输出保存在
[delivery_checks.json](delivery_checks.json)，CRLF 提示是 Git 警告，不是检查失败。

## 独立核对与边界

`collect_evidence.py` 只接受带标记的两个合成子根，SQLite 用 `mode=ro`；
[isolated_state.json](isolated_state.json) 与 [correction_events.jsonl](correction_events.jsonl)
分别保留事后只读计数和本次隔离服务真实事件，不伪称截图同一时刻查询。
新库：图片/证据/回执/磁盘各 4，failure 27/28 已解决，批次 2/3 未解决 0；
分页批次 1 仍保留 26 条失败。保留 500 库：各项 500，queued 500，未解决/OCR failed 0，
仍只有原来 20 次上传，2026-09-08 UTC 新上传 0；两库最大 attempt_count 0、外键无错误。

两次 Waitress 原会话均已 Ctrl+C 停止，5057/5058 监听 0。没有删除隔离数据或原证据。
四个本次截图缺口已补齐；历史完整测试日志不可恢复，改由明确新运行补充。
浏览器单标签 renderer 内存及 TCP 层静默丢包仍未单独验证：旧全 Chrome 聚合内存、
提交后 503 夹具不能代替它们。未进入第三阶段，未合并 PR。
