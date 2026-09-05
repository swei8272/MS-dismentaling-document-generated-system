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

开发按 `docs/IMPLEMENTATION_PLAN.md` 分阶段进行。第一阶段只建立批次、数据库迁移和后台任务基础，不执行真正 OCR。完成第一阶段验收后，再接入 RTX 3090 GPU OCR。

## 开发文档

- `AGENTS.md`：Codex 的最高优先级项目规则。
- `docs/COMMUNICATION_WORKFLOW.md`：我、用户与 Codex 通过 GitHub 协作的规则。
- `docs/BUSINESS_RULES.md`：不可擅自改变的业务规则。
- `docs/SYSTEM_DESIGN.md`：整体系统架构和数据设计。
- `docs/IMPLEMENTATION_PLAN.md`：分阶段开发顺序。
- `docs/INSTRUCTIONS_PHASE_1.md`：当前第一阶段的具体实施任务。
- `docs/ACCEPTANCE_CRITERIA.md`：测试和交付标准。

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
