# 第二阶段 Windows 本机浏览器验收

状态：部分执行（隔离环境准备完成，真实浏览器 UI 受运行环境阻断）。代码基线为
`0acd823e7e67684ae4fa9d50fa63aed5fd7f8629`；验收指令提交为
`6473329bab725d387bed5501a96e387875bd0adf`，未改变程序代码。不得把 API/Node
测试结果填写为真实浏览器通过。

2026-09-07 已在系统临时目录生成 500 张新合成图片，并用隔离数据库在
`127.0.0.1:5057` 启动 Waitress；应用入口返回 HTTP 200。随后按
computer-use 技能初始化受支持的本机浏览器能力并按规定重试一次，技能后端和
统一 CUA 备用后端都在窗口枚举前因 Windows sandbox setup refresh 失败，未打开
浏览器、未执行页面操作、未上传文件。只读诊断发现工作区既有 `.pytest_cache`
目录可枚举但 ACL 无法读取；本轮没有删除、移动或修改该目录及其权限。验证服务
已按精确 PID 停止，临时合成数据保留，真实业务数据未访问。

本轮所有浏览器场景仍为“未验证”，第二阶段尚未全部通过。原始事实记录见
[本机 computer-use 尝试](validation/issue4-browser-0acd823/raw_local_computer_use_attempt.json)，
详细结论见[第二阶段验证报告](PHASE_2_VALIDATION_REPORT.md)。以下清单保留，待
恢复工作区读取权限后继续执行。

## 1. 准备独立环境

在已有项目目录 `D:\Codes\DGM` 打开 PowerShell。先检查本地工作区与 PR #5
版本，保留已有修改；不要切换到 main、清理工作区或覆盖数据库。

```powershell
git status --short
git log -1 --oneline
```

确认本地包含 PR #5 当前程序后，逐行执行以下单行命令。变量仅在当前
PowerShell 窗口有效。所有新数据位于系统临时目录；不要使用 `start.bat`
进行此次验收，以免连接正式数据库。

```powershell
$phase2BrowserRun = Join-Path ([System.IO.Path]::GetTempPath()) ('dgm-phase2-browser-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $phase2BrowserRun | Out-Null
.\.venv\Scripts\python.exe scripts\phase2_validation.py generate (Join-Path $phase2BrowserRun 'images') --count 500 --width 1600 --height 1200
.\.venv\Scripts\python.exe scripts\phase2_validation.py inspect (Join-Path $phase2BrowserRun 'images')
[System.IO.File]::WriteAllText((Join-Path $phase2BrowserRun 'damaged.jpg'), 'deliberately invalid synthetic image')
Write-Output $phase2BrowserRun
.\.venv\Scripts\python.exe scripts\phase2_validation.py serve (Join-Path $phase2BrowserRun 'server') --port 5057
```

最后一条命令保持运行。在这台 Windows 电脑的 Chrome 或 Edge 中打开
`http://127.0.0.1:5057/batches`。若端口被占用，选择空闲端口并同步修改命令
和浏览器地址；不要停止不属于本次验收的进程。

## 2. 先验证图片列表与文件选择

1. 在页面新建“浏览器 51 张验证”批次，确认初始列表为空。
2. 用页面文件选择器选中刚生成的 `synthetic-0000.jpg` 至
   `synthetic-0050.jpg`，点击“开始上传”。不得使用 Python 上传代替这一步。
3. 不按 F5：确认已保存总数变为 51、第一页 50 条、出现第二页，点击第二页
   后为 1 条。截图须同时能对应批次编号与实际列表/统计。
4. 再选择 `synthetic-0051.jpg`，先不上传。在“批次图片”中往返切页，确认
   文件选择及本地等待条目仍保留，再点击上传。第二页应变为 2 条，总数 52。
5. 上传过程中切页并观察自动刷新，当前页不能被旧响应强行切回；如测试了延迟
   响应，需要记录所用延迟方式和实际观察，不能仅凭正常网络结果认定通过。

## 3. 完成剩余浏览器场景

| 场景 | 操作与应记录结果 |
|---|---|
| 500 张分组 | 新建另一批次，用原生选择器选择全部 500 张。观察最多 25 张/64 MiB、串行上传、最终批次总数 500。此前图片会复用，不能要求 500 个 added。 |
| 服务器确认 | 观察上传进度 100% 后等待服务端确认，确认前不得计为已保存；如过快无法观察，标记未验证，使用可控慢速场景补测。 |
| 损坏图与替换 | 上传 damaged.jpg 和两张有效合成图；有效图正常保存，损坏图有独立错误。使用该失败记录的替换选择器选一张有效图，记录失败解决、OCR failed 仍为 0。 |
| 文件选择与刷新 | 整页刷新后恢复服务端统计及失败记录；页面提示重新选择本地文件，不得声称 File 对象跨刷新保留。 |
| 断网补写 | 在浏览器开发者工具中对测试页面模拟 Offline，执行上传失败流程并刷新；恢复 Online 后观察待同步元数据补写和明确替换重传。保留实际操作时序。 |
| 有限重试与后续组 | 在隔离环境制造可恢复的网络/服务错误，观察初次请求加最多 2 次自动重试，后续组仍继续，并能只重试失败/未确认项。 |
| 列表读取失败 | 在隔离环境让图片列表请求失败，确认旧表格保留且提示可见；恢复后“更新图片列表”按钮能恢复。 |
| 响应丢失重传 | 用受支持工具或隔离验证服务制造“已保存、响应未收到”，在页面重传后检查无重复关联、已完成任务不重排队。未能制造该场景时保留为未验证。 |

网络错误注入只针对隔离测试页面和服务，不修改正式服务或系统安全设置。
未测试的场景必须明确留空或写“未验证”，不能用已有接口测试代填。

## 4. 截图、内存与结果记录

- 截图只包含合成数据，记录批次编号、操作前后计数、分页、失败/重试及恢复状态。
- 记录浏览器版本、Windows 版本、实际代码 SHA、测试开始/结束时间及图片清单。
- 浏览器内存可通过浏览器任务管理器观察：明确记录对应测试标签页/进程、测量
  工具、观察频率和开始/上传期间最大观察值/结束值。人工观察最大值只能称
  “观察到的最大值”，不得冒称进程生命周期峰值；不得使用 Python 客户端内存替代。
- 记录每个场景：已执行操作、预期、实际结果、通过/失败/未验证、证据位置。
- 如需重新采样服务端性能，另存于本轮临时目录；不要覆盖已有 Windows 原始证据。

## 5. 结束与交付

在启动 Waitress 的 PowerShell 窗口按 Ctrl+C，仅停止本次验证服务。保留临时
结果供核对，勿删除用户文件。

把实际结果和纯合成截图补充到 PR #5 的报告。确认浏览器验收全部完成后再给出
第二阶段验收结论；当前文档本身不是通过证据，也不授权自动合并或进入第三阶段。
