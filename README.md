# 财税 Harness

项目提供 A1 共享骨架、A2 只读业务查询、A3 发票保存与核验、A4 对话执行器、A5 任务恢复，以及 B 的材料导入、偏好、规则和运行记录工作台。新库初始只包含两家企业、两个测试用户及其权限。业务数据与规则由导入命令加载。

首次从提交包启动请使用 [演示复现说明](demo/README.md)，其中包括新环境安装、版本校验、数据重置和启动命令。项目结构和边界见 [简要设计](DESIGN.md)。

使用 Python 3.12，在项目根目录执行以下 PowerShell 命令。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe -m uvicorn tax_harness.app:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 http://127.0.0.1:8000 使用工作台，访问 /docs 查看接口，GET /health 检查数据库，GET /api/contracts 获取共享 JSON Schema。

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/scope?enterprise_id=C001&month=2026-09' -Headers @{'X-Demo-User'='U001'}
.\.venv\Scripts\python.exe -m pytest -q
```

导入 v1.1.0 材料包，然后使用同一个数据库路径启动。

```powershell
$bundle = Read-Host '请输入赛事 v1.1.0 材料 ZIP 的路径'
.\.venv\Scripts\python.exe -X utf8 -m tax_harness.import_data $bundle --db data/b-v1.1.0.sqlite3
$env:HARNESS_DB_PATH = 'data/b-v1.1.0.sqlite3'
.\.venv\Scripts\python.exe -m uvicorn tax_harness.app:app --host 127.0.0.1 --port 8010 --workers 1
```

导入命令输出四组企业月份的数量与整数分金额，已含明细或规则的目标库会拒绝再次导入。需要重置时指定新的数据库文件。默认数据库位于 data/harness.sqlite3。`.env.example` 是变量说明，应用从进程环境读取配置。新文件首次启动自动初始化，已有版本 1 数据库保持现有数据，未知 schema 版本拒绝启动。

使用已经导入的数据库时，直接设置数据库路径并启动服务。A2 查询可从 `/docs` 调用，也可在 PowerShell 中执行以下命令。

```powershell
$headers = @{'X-Demo-User'='U001'}
Invoke-RestMethod 'http://127.0.0.1:8010/api/invoices?enterprise_id=C001&month=2026-09' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8010/api/bank-transactions?enterprise_id=C001&month=2026-09' -Headers $headers
```

A3 保存验收使用 B 数据库的 SQLite 备份副本，并将 `HARNESS_DB_PATH` 指向副本。通过 `/docs` 调用发票 PATCH、操作 GET 和任务状态 PATCH。保存正文、重试和回查示例见 docs/接口与交接.md。

A4 对话联调同样使用副本。启动前设置 `HARNESS_MODEL_ENDPOINT`、`HARNESS_MODEL_NAME` 和 `HARNESS_MODEL_KEY`，然后在工作台「对话」或 `/docs` 调用 `POST /api/messages`。提案生成后核对发票与修改值，在同一会话回复 `确认保存`。本机 HTTP 替身的验收记录及赛事模型状态见 docs/接口与交接.md。

A5 可在新会话发送 `继续任务` 恢复原企业和月份的待办。若有修改提案，核对重新展示的内容后再确认保存。模型超时可配置 `HARNESS_MODEL_TIMEOUT`，默认 90 秒。发生保存响应中断时，使用原请求编号重试或查询操作。核验流程与独立验收记录见 docs/接口与交接.md。

A6 的 v1.1.0 副本回归命令如下。每组用例从 B 的数据库创建独立 SQLite 副本。B01—B35 实测结果与赛事模型状态见 [A6 验收清单](evaluation/验收清单.md)。

```powershell
$env:HARNESS_ACCEPTANCE_DB = 'data/b-v1.1.0.sqlite3'
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
```

`X-Demo-User` 是本地比赛演示身份选择机制，可以选择 U001 或 U002。它不提供联网部署所需的登录认证，服务器默认只绑定本机回环地址。权限在后端按选定账户执行。

实现入口在 src/tax_harness。共享接口和各阶段实现见 [接口与交接](docs/接口与交接.md)。
