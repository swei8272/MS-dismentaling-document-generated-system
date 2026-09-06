# 铭深文件生成系统

铭深文件生成系统是一套面向拆解厂内部人员的本地车辆资料整理系统。工作人员批量上传车辆外观照片、行驶证和微信沟通截图，系统在后台识别图片中的文字，按车辆整合信息，并生成批次表和最新车辆总表。

## 正式项目位置

项目部署目录：

`D:\Codes\DGM`

程序不得依赖这一绝对路径。代码应根据项目文件位置计算根目录，以便以后整体迁移到其他磁盘或电脑。

## 核心工作流程

1. 工作人员创建一个上传批次。
2. 批量选择多辆车的混合图片。
3. 网页分块保存图片并立即返回批次页面。
4. 后台 Worker 使用 OCR 逐张处理图片。
5. 系统优先根据车牌号、辅助根据 VIN 关联车辆。
6. 无法可靠关联的图片进入“待确认”。
7. 批次处理完成后生成本批次车辆表。
8. 系统同步更新所有批次的车辆总表。
9. 后续批次可以继续补全已有车辆，不重复创建车辆记录。

## 标准内部字段

系统只向内部工作人员显示和导出以下 14 个字段：

1. 自编号
2. 车牌号
3. 车辆类别
4. 车辆品牌型号
5. 车主姓名
6. 车架号/VIN
7. 发动机号
8. 注册日期
9. 整备质量(kg)
10. 业务归属
11. 送车/运输方式
12. 进场时间
13. 是否需要销户
14. 证件处理状态

称重、轮胎、车辆情况、部件费用和结算付款信息不属于当前标准表格。

## 建议目录

```text
D:\Codes\DGM
├── AGENTS.md
├── README.md
├── app.py
├── config.py
├── database.py
├── parser.py
├── ocr_engine.py
├── excel_export.py
├── worker.py
├── requirements.txt
├── install.bat
├── start.bat
├── start_worker.bat
├── templates\
├── static\
├── tests\
├── docs\
├── data\
│   ├── vehicles.db
│   ├── uploads\
│   └── backups\
├── exports\
│   ├── batches\
│   └── master\
└── logs\
```

## 开发状态

开发按 `docs/IMPLEMENTATION_PLAN.md` 分阶段进行。第一阶段批次与任务基础已完成，当前进入第二阶段分组上传与进度；仍不执行真正 OCR。

当前提供：

- SQLite 版本化迁移；
- 批次创建、列表、详情和图片上传；
- SHA-256 全局图片去重；
- 可恢复的持久化任务状态及原子领取接口；
- 不加载 OCR 的 Worker 自检骨架；
- 浏览器默认每组最多 25 张、文件净大小合计最多 64 MiB 的串行上传；
- 单文件 16 MiB 限制、逐文件 JSON 结果、上传进度及最多 2 次自动重试；
- 上传失败与 OCR 失败分开记录，页面刷新后恢复服务端状态；
- 批次图片分页和向已有批次继续补充图片。

## Windows 本机启动

首次运行：

```bat
install.bat
start.bat
```

浏览器访问 `http://127.0.0.1:5000`。`start.bat` 使用 Waitress，并监听局域网地址 `0.0.0.0:5000`；如需从其他电脑访问，应仅在受信任的厂内网络开放 Windows 防火墙端口。

Worker 骨架可以单独运行：

```bat
start_worker.bat
```

第一阶段 Worker 只检查数据库迁移、任务领取接口并恢复超时任务，然后安全退出；不会领取正常任务，也不会加载或执行 OCR。

运行开发测试：

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

默认数据库和图片目录从项目文件位置推导，分别为 `data\vehicles.db` 和 `data\uploads`。程序只会通过版本化迁移升级数据库，不会重建已有数据库。若检测到无法识别的旧 `evidence` 表结构，程序会在写入迁移前停止并报告。

## 开发文档

- `AGENTS.md`：Codex 的最高优先级项目规则。
- `docs/COMMUNICATION_WORKFLOW.md`：我、用户与 Codex 通过 GitHub 协作的规则。
- `docs/BUSINESS_RULES.md`：不可擅自改变的业务规则。
- `docs/SYSTEM_DESIGN.md`：整体系统架构和数据设计。
- `docs/IMPLEMENTATION_PLAN.md`：分阶段开发顺序。
- `docs/INSTRUCTIONS_PHASE_1.md`：已完成的第一阶段实施任务。
- `docs/INSTRUCTIONS_PHASE_2.md`：当前第二阶段的具体实施与验收约定。
- `docs/ACCEPTANCE_CRITERIA.md`：测试和交付标准。
- `docs/PHASE_2_VALIDATION_REPORT.md`：第二阶段自动化与 500 张合成图片实测结果。

## 数据原则

- 原始图片永久保留，并使用 SHA-256 去重。
- 未识别字段在数据库中保存为空，界面和 Excel 中显示“暂无”。
- 不确定的关联必须人工确认。
- 数据库升级必须迁移，不得用新数据库覆盖旧数据库。
- 数据库、图片和导出结果默认不上传互联网。

## 部署方向

正式版本运行在一台配有 RTX 3090 的 Windows 主机上。Web 服务与 OCR Worker 相互独立，避免大量 OCR 任务导致网页无响应。厂内其他电脑通过局域网访问，不直接暴露到公网。

## GitHub 协作

稳定规则保存在 `main` 分支；每项开发工作先建立 GitHub Issue。Codex 从最新 `main` 创建独立分支完成代码和测试，再提交 Pull Request。用户确认并合并后，下一项任务才能开始。具体状态、命名和交付规则见 `docs/COMMUNICATION_WORKFLOW.md`。
