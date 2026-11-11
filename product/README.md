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

首次启动完成后，数据库应有 2 个租户、3 家企业、2 个账号、3 张发票、4 条银行流水、3 条规则，导入任务和发票审计均为 0。当前 Flyway 应到达 V8。规则验收前的模型预热应输出 `384`，空缓存需下载约 252 MB。测试会修改合成发票、规则和导入任务，恢复演练会生成本机备份。保留数据并停止验收服务时，使用 `docker compose -f product/compose.yaml -f product/compose.https.yaml stop`。

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

LangGraph 每轮最多 6 次模型决策。需求清单首次登记后固定，答案只能引用本轮成功工具证据，金额与规则正文由程序渲染。P6 已将任务、消息、偏好与轨迹迁入 Java 管理的 PostgreSQL；Agent 每轮重新取上下文，恢复过程见下文。

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

## P6 任务、记忆与上下文持久化

任务、用户/助手消息、逐轮轨迹、偏好、偏好失效记录和审计均由 Java 写 PostgreSQL。V8 新增 `agent_task`、`agent_message`、`agent_trace`、`agent_memory`、`agent_memory_invalidation`、`agent_memory_audit`、`agent_operation` 七张表；恢复演练现覆盖 21 张表。Agent 不直连数据库，Redis 只协调短期并发锁。

每账号、每租户、每企业最多一个未结束任务。刷新页面后读取 active 接口，带出原追问或建议卡。跨会话的建议卡须点击“恢复当前任务”，核对内容后再点击“确认保存”。确认请求携带 `X-Chat-Scope` 和稳定的 `Idempotency-Key`；重放返回同一操作编号。提案保存生成时的原值快照，恢复时不会用已变化的当前记录冒充原值。V7 旧会话不迁移，旧对话提案需要重新核验；原有非对话提案流程保留。

同企业切换月份时，只允许新月份查询，原任务的目标、追问、提案、状态和 revision 保留。未知操作必须先核实，不能直接更正目标、取消任务或冲突写入。任务业务状态与单轮轨迹状态分开：一次查询 succeeded 可以与原任务 waiting_user 同时存在。

新接口（`...` 表示 `/api/enterprises/{enterprise}/chat`）：

| 方法及路径 | 参数及约束 |
| --- | --- |
| `GET .../tasks/active?period=YYYY-MM` | 返回 task、matches_period、最近消息、proposal、unfinished_operations、requires_resume |
| `GET .../tasks/{id}?period=YYYY-MM` | 只读本人任务，越权 404 |
| `GET .../tasks/{id}/traces` | 脱敏决策、工具证据、模型输入、裁剪与最终状态 |
| `POST .../tasks/{id}/correct` | expected_revision；可更正 object_id 或 question，旧提案失效 |
| `POST .../tasks/{id}/cancel` | expected_revision；忙碌、未知操作或版本冲突 409 |
| `POST .../tasks/{id}/resume` | session_id、period、expected_revision；显式重绑建议卡 |
| `POST .../tasks/{id}/operations/{operation}/reconcile` | 锁定任务及发票，按审计和提案事实核实，不能凭模型判断 |
| `GET/POST .../memories` | 新建需本人、本企业用户消息 source_message_id；可选 user_wide |
| `POST .../memories/{id}/correct` | content、expected_revision；可提供新的 source_message_id |
| `DELETE .../memories/{id}` | JSON body 中 expected_revision |
| `GET .../memories/{id}/audit` | 包括已删除偏好的版本审计，仅本人可读 |

`POST /internal/agent/context` 和 `POST /internal/agent/trace` 仍需服务凭据与本轮调用令牌。上下文保留范围、本轮输入、任务状态和本轮证据，超预算先裁历史，再裁偏好，逐项记轨迹；强制内容仍超限则 `context_budget_exceeded`。历史最多八条，偏好更正/删除后排除旧来源及同轮助手回声。偏好置于独立数据字段，不能授予工具权限或替代业务证据。

并发使用 30 秒 Redis 所有者锁，每 8 秒续租，同时记录 PostgreSQL active_turn_id/lease_until；后续写入再次核对 turn 和租约。释放使用 Lua 比较所有者后删除。数据库锁顺序为任务→范围→发票，长模型调用在事务外。Java 中断时落 incomplete；Java 进程异常退出的遗留轮次在租约过期后由后续请求核实恢复。

### P6 独立验收

在单独源码副本运行，首次先用 `start.ps1 -Https` 生成该副本凭据、CA 和初始化数据，并按前文准备离线向量模型文件。下面脚本会重建 Agent 配置并在独立实例强制终止 Agent，必须使用以 `tax-product-p6` 开头的独立项目名。基础 P5 验收依赖初始三张发票，先执行它，再执行导入及 P6 用例。

```powershell
$env:COMPOSE_PROJECT_NAME = 'tax-product-p6'
$env:PRODUCT_IMPORTER_IMAGE = 'tax-product-p6-importer'
$env:PRODUCT_HTTP_PORT = '13109'
$env:PRODUCT_HTTPS_PORT = '13448'
powershell -ExecutionPolicy Bypass -File product/start.ps1 -Https
$env:PRODUCT_TEST_BASE = 'https://127.0.0.1:13448'
$env:SSL_CERT_FILE = (Resolve-Path product/tls/local/ca.crt).Path
$files = @('-f','product/compose.yaml','-f','product/compose.https.yaml',
  '-f','product/compose.agent-test.yaml','-f','product/compose.memory-test.yaml')
docker compose @files up -d --build --wait
python product/verify_agent.py
python product/verify_memory.py
python product/verify_https.py
python product/verify_access.py
python product/verify.py
python product/verify.py --p3
python product/verify_import.py
python product/verify_bank.py
python product/verify_agent.py --details
python product/verify_restore.py
```

结果及脱敏轨迹写入副本的 `data/agent-memory-时间/`。模型替身会真实接收请求，脚本将其捕获内容与已持久化轨迹对照。未知写入场景用隔离数据库故障注入构造，核实和拦截走真实 Java API；不作为真实网络故障提交的证明。备份恢复演练会独立启动数据库和对象存储，核对所有表内容与原件 SHA-256。

`verify_memory.py --snapshot` 只读导出六张既有业务表，排除 V8 新增列，用于 V7→V8 前后比较。实际升级前先保存数据库和原件备份并验证可恢复，升级后再比较快照。详细结果、证据目录和限制见 [P6 验收记录](P6验收记录.md)。本轮 P6 使用替身验证程序行为；P6 真实 Qwen3 8B 测试和浏览器实际点击确认另行记录，不将替身结果算作真实模型通过。

## P7 对话工作台与过程面板

默认进入三栏对话工作台：左侧企业、月份、任务与偏好；中间历史消息和输入；右侧执行过程、证据与待确认建议。宽度不超过 900px 时使用“对话 / 过程 / 任务”视图按钮，业务模块统一放在顶部“业务数据”入口。主色沿用 #002FA7，没有新增 npm 运行依赖。

新增 `GET /api/session`，仅返回有效会话的 username 和 tenant_id，响应不缓存；无效会话返回 401。页面刷新后自动恢复登录与未结束任务，退出或过期回到登录页。确认依然携带 X-Chat-Scope、Idempotency-Key，恢复、月份和未核实操作限制保留。保存结果未知会禁用再次确认，并提供核实入口。

SSE 仅增加 `decision`（step、kind）、`requirements`（items）事件，以及 tool_result.duration_ms；原事件字段保持兼容。持久轨迹增加 context_budget，用于过程面板显示真实预算。过程按轮次展示需求、步骤、工具、证据、拦截与上下文，脱敏模型输入默认折叠。证据卡的“已核实”仅在成功答案实际引用本轮有效证据时显示。旧轨迹缺少的测量字段显示“未返回”。

模型状态默认“模型待验证”，首次对话被接收后标“模型已配置”；收到 503 时标“模型未配置”。这是配置可用性判断，不代表模型回答正确。现有 probe 只能证明 Agent 连接，不据此推断模型配置。P6 的 Qwen3 8B 未通过项继续保留。

### P7 浏览器验收

使用独立源码副本，准备离线向量模型文件和 localhost CA。Windows 测试需已安装 Microsoft Edge，Python 3.12；Playwright 使用本机 Edge，无需下载额外浏览器。初始化及启动按独立验收说明，变量如下：

```powershell
$env:COMPOSE_PROJECT_NAME = 'tax-product-p7'
$env:PRODUCT_IMPORTER_IMAGE = 'tax-product-p7-importer'
$env:PRODUCT_HTTP_PORT = '13110'
$env:PRODUCT_HTTPS_PORT = '13449'
powershell -ExecutionPolicy Bypass -File product/start.ps1 -Https
$files = @('-f','product/compose.yaml','-f','product/compose.https.yaml',
 '-f','product/compose.agent-test.yaml','-f','product/compose.memory-test.yaml')
docker compose @files up -d --build --wait
$env:PRODUCT_TEST_BASE = 'https://localhost:13449'
$env:SSL_CERT_FILE = (Resolve-Path product/tls/local/ca.crt).Path
python -m pip install -r product/requirements-verify.txt -r product/requirements-ui.txt
python product/verify_agent.py
python product/verify_ui.py
```

Edge 必须信任该副本用于 localhost 的 CA；脚本不忽略 TLS 错误。复用本机已受信任的开发服务证书时，仅在被 Git 忽略的测试证书目录操作，不能提交证书私钥。UI 脚本会更改合成数据、模拟并发版本冲突、使测试会话过期，并临时切换 Agent 为未配置模型后恢复；不得对主工作台运行。

结果和 1440×900、390×844 截图输出到副本 `data/p7-ui-时间/`。脚本包含真实浏览器点击、真实业务写入、独立回查和同键重放，模型仍是替身。其后执行 verify_https/access、verify.py、verify.py --p3、verify_import/bank、verify_agent.py --details、verify_restore.py。未改动的 verify_memory.py 有 tax-product-p6 项目名保护，应在另一份采用同一 P7 源码的独立 tax-product-p6 实例运行，不要修改该保护来适配 P7。

详细检查与截图见 [P7 验收记录](P7验收记录.md)。完整 B01—B35 真实模型复验、模型动作稳定性修复与视频发布包仍属于后续阶段。

恢复脚本若报独立服务未就绪，先查看具体 Docker 错误。若源容器的 Image ID 已不存在，在独立验收实例的静默窗口用相同 Compose 配置执行 `up -d --no-deps --force-recreate --wait importer worker`，再重跑原恢复脚本；不要修改其内容一致性检查。P7 本轮遇到并修复了这一镜像引用问题。
