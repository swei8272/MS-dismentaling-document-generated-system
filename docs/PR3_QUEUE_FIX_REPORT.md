# PR #3 队列修复补充报告

## 问题及修复

旧 Worker 在任务超时、重新分配后仍可提交终态，导致新 Worker 的结果被拒绝。
现在三个终态接口使用领取时返回的 `attempt_count`，在同一条 SQL UPDATE
中同时校验任务 ID、processing 状态和领取版本。过期报告回滚，不修改图片
状态、错误、时间戳或关联批次统计。

## 修改文件

- `database.py`：终态接口增加必填关键字参数 `attempt_count` 和原子版本校验。
- `tests/test_batches_and_queue.py`：增加三种终态的过期领取回归测试和非法版本测试。
- `tests/test_uploads_and_pages.py`：更新现有调用，传递领取时的版本。
- `docs/INSTRUCTIONS_PHASE_1.md`：记录调用约定及兼容要求。

## 数据和兼容性

复用现有字段，不增加迁移，不修改数据库版本，也不重置已有计数。
旧调用方必须更新为传递原领取结果中的版本；遗漏参数会明确失败，不提供
不安全的兼容默认值。不要在提交结果前重新读取当前版本。未来 OCR 结果和
车辆字段写入也必须与领取版本校验放在同一个事务中，不能先写结果再校验。
本次仅在隔离的临时数据库中验证，未接触真实车辆资料。

## 本次验证

环境：Linux / Python 3.12.13。

- 使用 Python 标准库和实际 `database.py` 独立验证 completed、pending、failed
  三条路径：超时后重领前拒绝旧报告；重领后拒绝旧报告；旧报告不影响 evidence
  和两个关联批次；重复迁移保留领取版本；当前 Worker 可成功结束任务；重复
  结束被拒绝；非法版本及缺少版本被拒绝。全部通过。
- `python -m compileall -q app.py config.py database.py storage.py worker.py tests`：通过。
- `git diff --check`：通过。
- 本环境缺少 Flask、pytest 和 Waitress；安装依赖的联网审批在决定前被取消。
  因此本次未运行完整 pytest、页面测试或 Windows 启动验证。
- 原工作报告的 18 passed 仅适用于原提交，不能作为本次修改后的完整测试结果。

## 本地 Codex 复测

保留本地修改，安全同步本 PR 分支后，在已配置的虚拟环境运行：

```bat
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m compileall -q app.py config.py database.py storage.py worker.py tests
```

新增 9 个参数化测试用例，预期总计 27 个；实际结果须以复测输出为准。
合并前需要完整测试和最小页面验证。当前仍未实现 OCR、GPU 或 Excel 导出。
