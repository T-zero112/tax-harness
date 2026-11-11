# 财税 Agent 产品版

Windows 上从仓库根目录执行 `powershell -ExecutionPolicy Bypass -File product/start.ps1`。脚本首次生成 `product/.env`，随后用 Docker Compose 构建和启动服务。浏览器入口为 http://127.0.0.1:3100。登录账户为 alpha 或 beta，密码分别在本地 .env 的 PRODUCT_ALPHA_PASSWORD 和 PRODUCT_BETA_PASSWORD。

Next.js 将业务 API 交给 Spring Boot。Java 从 HttpOnly 会话 Cookie 获取 Redis 会话，由会话确定租户，再查询 PostgreSQL。两租户均有 C001，返回各自的企业名称。点击企业旁的连接检查，可接收经 Java 转发的 FastAPI SSE 事件。LangGraph 当前执行连通性探针。

从仓库根目录执行 `python product/verify.py` 检查匿名拒绝、错误密码、租户隔离、SSE 和退出登录。执行 `docker compose -f product/compose.yaml --env-file product/.env ps` 查看服务状态。日常停止用 `docker compose -f product/compose.yaml --env-file product/.env stop`，业务库持久卷会保留。

工作台提供回环地址的 HTTP 开发入口和可选的本地 HTTPS 配置。比赛验收和产品版验收分别记录。

2026-11-09 实测通过一键启动，五个服务均为 healthy。通过 Next.js 入口验证匿名和错误密码拒绝、两个租户的同号企业隔离、SSE status 和 done 事件、退出后会话失效。Next.js 16.4.5 生产构建通过，npm 生产依赖审计返回 0 项漏洞。比赛版 v1.1.0 副本回归为 104 项通过，源库哈希一致。

## P2 发票核验与补全

登录后在「发票核验与补全」选择企业，输入 F001 并查询。甲租户 alpha 可核验并填写用途或备注，生成建议后核对版本、依据和拟修改值，再点击确认保存。工作台会重新读取发票并运行核验，保存审计可展开查看改前、改后、经办账户、时间和规则版本。乙租户 beta 使用只读演示角色。

Java 通过 Flyway V2 在原 P1 数据卷上增加发票、建议和审计表。发票用租户、企业、发票号复合主键定位。LangGraph 执行固定版本的字段完整性检查，并根据经办人提供的值生成建议。Java 校验建议只包含原样提供的用途或备注，保存建议时记录读取的版本号。Agent 通过内部服务凭据接收 Java 提供的快照。

确认请求绑定建议编号和幂等键。Java 在事务内锁定发票，重验会话权限、范围、建议版本和规则版本，写入后回查并追加审计。重复确认返回原审计回执；独立 GET 返回当前状态。浏览器保留幂等键供确认请求重试。审计通过业务接口只追加，核验范围为用途和备注完整性。

从仓库根目录运行 `python product/verify.py` 执行 P1/P2 接口验收。脚本会对 alpha/C001/F001 写入一笔带随机标识的测试补全，并核对其余两张同号发票。可用 `python product/verify.py --snapshot` 读取发票与审计快照，用于比较服务重启前后的状态。详见 [P2 验收记录](P2验收记录.md)。

## P3 规则版本与混合检索

工作台「规则版本与检索」支持按企业查看版本和按会计期间检索。alpha 可以创建草稿、发布和退役。版本内容创建后固定，修改用新版本。生效月份包含当月，失效月份从该月起排除。退役将该版本移出所有实时检索，已保存回执仍保留原引用。发票核验使用发票自己的会计期间。

Java 先在 PostgreSQL 中固定当前租户、企业、会计期间和已发布版本的候选集，再用字面匹配与 pgvector 余弦分数排序。语义向量由 Agent 的 FastEmbed 多语言 MiniLM 模型生成，384 维。版本和文件哈希见 `product/agent/model-artifacts.json`。可在 `product/models` 放置清单中匹配的文件，或由 Agent 下载固定版本到 Compose 的 embedding 缓存卷。

发票规则适用范围内缺少条款、同一规则条款有重叠版本或必填字段要求不一致时，保存人工复核记录并返回编号。补全建议与审计回执保留规则来源、版本和条款。人工确认时重新检查适用规则，遇到旧版或冲突退回重新核验。历史已确认回执按原幂等键仍可重放。

执行 `python product/verify.py --p3` 对 alpha/C002 的演示规则和发票运行版本切换验收。脚本会新增、发布和退役测试规则，写入一笔测试备注。alpha/C001 保留给 P2 验收。

P3 的实测结果与人工复核编号见 [P3 验收记录](P3验收记录.md)。

## P4 发票 Excel 异步导入

执行同一启动脚本升级已有产品数据卷。首次升级会在本地 `.env` 追加导入服务凭据和对象存储凭据。Redis 使用持久卷保存会话和队列，升级后请重新登录。

在「发票 Excel 导入」选择企业并上传单工作表 `.xlsx`。第一行依次填写 `invoice_id`、`accounting_period`、`purpose`、`note`。编号与会计期间使用文本格式，会计期间为 YYYY-MM。用途和备注可留空。每次最多 1000 行、2 MiB，表格中的公式和 Excel 错误值会标为坏行。

Java 验证会话及企业权限，原件按租户、企业和 SHA-256 存入 S3 对象存储。Celery 从 Redis 领取任务并解析原件，Java 将逐行预览和错误存入 PostgreSQL。相同范围、相同文件重复上传返回原任务。等待时间较长时可重新排队，旧尝试的结果不会覆盖当前尝试。

预览无错误后确认导入，Java 在一个事务内保存整批发票及导入回执。同号冲突会回滚整批，重复确认返回同一回执。导入后可跳到首张发票的核验表单，继续 P2 补全和 P3 规则核验。

运行 `python product/verify_import.py` 验收。脚本会短暂停止并启动本机 Worker，并在 alpha/C001 的演示企业创建合成测试记录。

S3 对象存储使用 SeaweedFS，Compose 固定了实测镜像摘要。可用 `python product/verify_import.py --snapshot <任务编号> <发票编号>` 比对重启前后的任务、发票和原件哈希。实测详情见 [P4 Excel 导入验收记录](P4验收记录.md)。[SeaweedFS 官方说明](https://github.com/seaweedfs/seaweedfs#quick-start)

在无导入或确认操作的窗口执行 `python product/verify_restore.py`。脚本将 PostgreSQL 转储、S3 原件与校验清单保存到本机 `data/product-backups/`，在独立容器和数据卷中恢复并生成报告。备份目录受 Git 忽略规则保护。演练覆盖业务数据库与原件，排队任务在恢复后可从原件重新投递。实测见 [P4 备份恢复记录](P4验收记录.md#postgresql-与对象存储备份恢复演练)。

## 账号与企业授权

Flyway V5 将账户、租户成员关系与企业授权保存到 PostgreSQL。首次升级以原有本地启动凭据初始化 alpha（企业管理员）和 beta（查看者），初始密码仅用于创建不存在的种子账号。密码以带独立盐的 PBKDF2-HMAC-SHA256 哈希存储。升级后重新登录。

管理员可在「企业成员与权限」创建账号，为同租户账号授予企业访问权，修改角色或撤销授权。账号可以在不同企业拥有不同角色。查看者可读资料并核验，经办人可提交补全建议、确认保存和导入，管理员还可维护成员和发布规则。当前管理入口授予查看者与经办人角色，初始管理员由启动配置建立。

Redis 会话绑定账号和租户，Java 每次请求从数据库校验企业授权与角色。已登录的成员被降权或撤权后，后续请求按新权限执行。保存审计记录执行确认的用户名。运行 `python product/verify_access.py` 验收，脚本在 alpha/C001 创建测试账号与发票备注，结束时撤销测试账号的企业授权。

## 银行流水只读查询

登录后在「银行流水查询」选择企业和会计期间。流水号可留空，输入时按完整流水号匹配。列表按交易日期和流水号排序，每页最多 100 条。页面按元展示金额，接口以整数字符串返回分值以保留精度。

Java 通过 `GET /api/enterprises/{enterpriseId}/bank-transactions?period=2026-09&transaction_id=T001` 提供查询。租户由会话确定，企业访问权由数据库授权表确认。Flyway V6 按租户、企业、流水号建立复合主键，并以交易日期校验会计期间。迁移载入合成测试记录后，PostgreSQL 触发器阻止新增、修改、删除和清空流水。

在本机产品服务启动后运行 `python product/verify_bank.py` 验收。脚本创建并撤销测试账号的企业授权，使用回滚事务验证数据库写保护。恢复演练脚本也包含银行流水表。

## 本地 HTTPS 部署

安装 Docker Compose 和可从命令行调用的 OpenSSL 后，从仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File product/start.ps1 -Https
```

Nginx 在 `https://localhost:3443/` 提供 HTTPS，HTTP 入口 `http://127.0.0.1:3100/` 以 308 跳转到 HTTPS。Next.js 和 Java 在 Compose 网络内由 Nginx 转发。Java 签发和清除会话 Cookie 时均设置 Secure、HttpOnly 和 SameSite=Strict。代理转发 Excel 上传，并关闭 API 响应缓冲以传送 SSE。

首次启动生成本地 CA 和服务证书，文件位于 `product/tls/local/`。普通浏览器访问前需信任公开的 `ca.crt`，本项目的测试在客户端显式指定该 CA。私钥与本地证书目录受 Git 忽略规则保护。

```powershell
python product/verify_https.py
$env:PRODUCT_TEST_BASE = 'https://localhost:3443'
$env:SSL_CERT_FILE = (Resolve-Path product/tls/local/ca.crt).Path
python product/verify.py
```

重新运行启动脚本并省略 `-Https` 即可返回本机 HTTP 开发模式。两个模式使用同一产品数据卷。

### 独立验收环境

需要 Windows PowerShell、运行 Linux 容器的 Docker、支持 `!reset` 的 Docker Compose、Python 3.12，以及 PATH 上的 OpenSSL。首次构建需要访问镜像和依赖仓库，模型可联网下载或按上文离线准备。从干净检出或解压源码包后，在包含 `product/` 的目录执行以下命令。验收使用单独的 Compose 项目名、镜像标签和端口，首次启动会生成独立的凭据、证书和数据卷。若这些名称或端口已占用，换成未使用的值并同步调整测试入口。

```powershell
$env:COMPOSE_PROJECT_NAME = 'tax-product-acceptance'
$env:PRODUCT_IMPORTER_IMAGE = 'tax-product-acceptance-importer'
$env:PRODUCT_HTTP_PORT = '13100'
$env:PRODUCT_HTTPS_PORT = '13443'
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r product/requirements-verify.txt
powershell -ExecutionPolicy Bypass -File product/start.ps1 -Https
docker compose --project-directory product exec -T agent python -c "from main import embedding_model; print(len(next(embedding_model().embed(['invoice']))))"
.venv/Scripts/python.exe product/verify_https.py
$env:PRODUCT_TEST_BASE = 'https://127.0.0.1:13443'
$env:SSL_CERT_FILE = (Resolve-Path product/tls/local/ca.crt).Path
.venv/Scripts/python.exe product/verify_access.py
.venv/Scripts/python.exe product/verify.py
.venv/Scripts/python.exe product/verify.py --p3
.venv/Scripts/python.exe product/verify_import.py
.venv/Scripts/python.exe product/verify_bank.py
.venv/Scripts/python.exe product/verify_restore.py
```

首次启动完成后，数据库应有 2 个租户、3 家企业、2 个账号、3 张发票、4 条银行流水、3 条规则，导入任务和发票审计均为 0。P5 的 Flyway 应到达 V7。规则验收前的模型预热应输出 `384`，空缓存需下载约 252 MB。测试会修改合成发票、规则和导入任务，恢复演练会生成本机备份。保留数据并停止验收服务时，使用 `docker compose -f product/compose.yaml -f product/compose.https.yaml stop`。

交付源码由 Git 标签导出，复验结果见 [交付归档验收](P4验收记录.md#产品版-v010-交付归档)。源码包的 SHA-256 和对应提交号随包提供。

## P5 对话 Agent

在本机 `product/.env` 配置模型，随后运行 `powershell -ExecutionPolicy Bypass -File product/start.ps1 -Https`。模型接口使用 OpenAI 兼容 Chat Completions，BASE_URL 填写到 `/v1`。本机 Ollama 示例配置如下，API Key 按实际服务要求填写。

```dotenv
PRODUCT_MODEL_BASE_URL=http://host.docker.internal:11434/v1
PRODUCT_MODEL_NAME=qwen3:8b
PRODUCT_MODEL_API_KEY=
PRODUCT_MODEL_STRUCTURED=schema
PRODUCT_MODEL_TIMEOUT=120
PRODUCT_CONTEXT_BUDGET=12000
```

`PRODUCT_MODEL_STRUCTURED` 可为 `schema` 或 `none`，默认 `none`。未提供模型地址或名称时，对话接口返回 503，其他工作台功能可继续使用。登录后在「对话」中选企业和月份，输入查询或补全要求。建议卡展示票号、原值、新值及规则条款，用户点击「确认保存」后由 Java 写入并独立回查。保存请求重试复用原幂等键。

Java 新增 `POST /api/enterprises/{enterprise}/chat` 和 `POST .../chat/scope`。对话经 HTTPS 返回 `status`、`tool_call`、`tool_result`、`proposal`、`answer`、`error`、`done` SSE 事件，长请求每 10 秒发送心跳。内部 `POST /internal/agent/{tool}` 只接受服务凭据与 Java 签发的 15 分钟调用令牌。令牌绑定登录会话、企业、月份和范围版本。工具包括发票与流水查询、规则检索、用途核验、提案生成和操作状态查询。模型工具清单中的所有操作都通过 Java 执行。

V7 补充发票金额、销售方和项目名，并记录对话范围及提案绑定。金额在 PostgreSQL 存 bigint 整数分，接口用十进制整数字符串传递，避免 JavaScript 大整数失真。原四列 Excel 表头仍可用，也可在四列后按顺序追加 `amount_cents`、`seller`、`item_name`。金额为空会保留未知值，汇总时明确拒绝宣称金额完整。

LangGraph 每轮最多 6 次模型决策。需求清单首次登记后固定，答案只能引用本轮成功工具证据，金额与规则正文由程序渲染。当前对话的历史、用户已提供的目标票号和提案指针保存在内存，最多 256 个会话，空闲 1 小时释放。Agent 重启后这些对话信息需要重新提供。正式任务检查点、长期偏好与持久轨迹安排在后续阶段。

### P5 独立验收

验收会写入合成数据。请在独立源码副本中使用以下项目名和端口，并先准备模型文件。首次调用启动脚本可生成该副本的凭据与证书。替身服务仅在验收 Compose 覆盖文件中启动。

```powershell
$env:COMPOSE_PROJECT_NAME = 'tax-product-p5'
$env:PRODUCT_IMPORTER_IMAGE = 'tax-product-p5-importer'
$env:PRODUCT_HTTP_PORT = '13105'
$env:PRODUCT_HTTPS_PORT = '13446'
powershell -ExecutionPolicy Bypass -File product/start.ps1 -Https
docker compose -f product/compose.yaml -f product/compose.https.yaml -f product/compose.agent-test.yaml up -d --build --wait
$env:PRODUCT_TEST_BASE = 'https://127.0.0.1:13446'
$env:SSL_CERT_FILE = (Resolve-Path product/tls/local/ca.crt).Path
python -m pip install -r product/requirements-verify.txt
python product/verify_agent.py
python product/verify_https.py
python product/verify_access.py
python product/verify.py
python product/verify.py --p3
python product/verify_import.py
python product/verify_bank.py
python product/verify_agent.py --details
python product/verify_restore.py
```

基础替身验收需要初始三张发票，扩展导入测试放在其后。验证未配置模型时，先将 agent 替换为无模型配置，测完再恢复替身。

```powershell
docker compose -f product/compose.yaml -f product/compose.https.yaml -f product/compose.agent-test.yaml -f product/compose.agent-unconfigured.yaml up -d --no-deps --force-recreate --wait agent
python product/verify_agent.py --unconfigured
docker compose -f product/compose.yaml -f product/compose.https.yaml -f product/compose.agent-test.yaml up -d --no-deps --force-recreate --wait agent
```

真实模型在另一套初始数据上执行 `python product/verify_agent.py --live`。该副本的 `.env` 必须配置实际模型，Agent 容器还需设置 `PRODUCT_AGENT_EVIDENCE_DIR=/embedding-cache/p5-live-evidence` 以留存脱敏原始响应。脚本核对容器模型名称，逐项输出通过或失败，失败以非零退出码结束。结果和原始响应保存在副本的 `data/agent-live-时间/`。详见 [P5 验收记录](P5验收记录.md)。

独立替身实例需要手工验证前端时，可额外叠加 `compose.agent-ui.yaml`。该覆盖文件为演示 HTTP 测试关闭 Secure Cookie，并仅将前端绑定在回环端口 13108；本轮浏览器点击验收仍标为待验证。产品的正常 HTTPS 启动命令使用基础配置与 HTTPS 配置。
