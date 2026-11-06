# 从提交包复现演示

以下命令使用 Windows PowerShell 7.2+ 和 Python 3.12。在刚解压的提交包根目录执行。需要赛事 v1.1.0 原始材料 ZIP。评委可用原始材料重新导入，源码包内提供导入程序，演示库由本机生成。

## 安装与版本校验

```powershell
python -m venv .venv
$py = '.\.venv\Scripts\python.exe'
& $py -m pip install -r requirements.lock
& $py -m pip install --no-deps --no-build-isolation .
& $py -m pip check
& $py -X utf8 demo/verify_release.py --installed
```

`RELEASE.json` 记录发布标识和提交文件的 SHA-256。校验还会对比已安装的 Python、SQL 和网页文件。录屏开头展示版本校验输出中的发布标识和 manifest SHA-256。若录制后重新打包，用最终 ZIP 解压、安装并重新校验。新增视频时保留用于录像的源码及清单，另外对最终 ZIP 计算哈希。

## 导入或重置演示

每次重置先停止演示服务，再运行以下命令。脚本在 `data/demo/` 下新建独立目录，从原始材料重新导入。旧库、旧操作记录和原始 ZIP 保留，可用于对照。

```powershell
$py = '.\.venv\Scripts\python.exe'
$bundle = Read-Host '赛事 v1.1.0 材料 ZIP 的完整路径'
$raw = & $py -X utf8 demo/reset_data.py $bundle
if ($LASTEXITCODE -ne 0) { throw '演示数据重置失败' }
$demo = $raw | ConvertFrom-Json
$demo | Select-Object material_version,invoices,bank_transactions,rule_versions
$env:HARNESS_DB_PATH = $demo.database
```

导入应显示版本 1.1.0、40 张发票、48 条流水、5 个规则版本。新库的 C001 2026-09 F002 用途和备注为空，任务、保存操作、用户偏好均为空。新库启用前刷新工作台，确认浏览器和 API 指向新启动的端口。

## 配置与启动

`.env.example` 是配置参考。赛事参数由主办方提供。录屏开始前，在当前终端设置环境变量并确认服务启动。

```powershell
$env:HARNESS_MODEL_ENDPOINT = Read-Host '赛事模型的完整 Chat Completions URL'
$env:HARNESS_MODEL_NAME = Read-Host '赛事模型名称'
$env:HARNESS_MODEL_KEY = Read-Host '赛事模型 Key' -MaskInput
& $py -m uvicorn tax_harness.app:app --host 127.0.0.1 --port 8010 --workers 1
```

打开 [工作台](http://127.0.0.1:8010/) 或 [API 文档](http://127.0.0.1:8010/docs)。另开终端执行以下命令检查启动与样例票据。

```powershell
$base = 'http://127.0.0.1:8010'
Invoke-RestMethod "$base/health"
$query = 'enterprise_id=C001&month=2026-09'
$headers = @{'X-Demo-User' = 'U001'}
(Invoke-RestMethod "$base/api/invoices/F002?${query}" -Headers $headers).data
```

出现 503 且提示模型尚未配置时，先核对三个环境变量，再重启服务。已有配置时，检查端点协议、模型名称、网络与凭据。出现端口占用时改用另一个本地端口，并同步修改浏览器和校验命令。U002 对甲企业为只读，对乙企业无权限。

录制分镜、具体输入和验收证据见 [5 分钟录屏脚本](录屏脚本.md)。
