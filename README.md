# AI-RPA 订单自动化平台

把电商平台上的订单，用 **AI 分析 + RPA 自动录入 ERP**。

订单从 Excel/CSV 导入后，AI 读买家留言、判断异常并给出处理建议，硬规则引擎与 AI 结果合并成最终判定；
无异常的单子生成 RPA 任务推入 Redis 队列，RPA Worker 领到任务后用 Playwright **像人一样打开 ERP 的网页**，
登录、点导航、填表单、点确认框，读回 ERP 生成的单号再回传主平台。失败自动重试，超限转人工。

> 这是一个**个人作品集项目**，目标是一个真的能跑起来、端到端闭环的原型。
> v1 使用**模拟**电商平台与**模拟** ERP，**不接入**任何真实账号、真实 ERP、真实客户数据（《需求规格》§2.2）。
> 它不是生产系统，重点在于把「一条链路真正跑通」，并把每个设计决定讲清楚。

---

## 实现状态

8 个开发阶段（《需求规格》§11）的实际完成度 —— 诚实地列出来，比假装没有更有说服力：

| Phase | 内容 | 状态 | 说明 |
| --- | --- | --- | --- |
| 1 | 工程骨架 + FastAPI + MySQL 接入 | ✅ 已完成 | 分层结构、统一响应外壳、全局异常处理、健康检查 |
| 2 | 订单模块 | ✅ 已完成 | Excel/CSV 导入（四道校验关卡）、订单列表/详情、导入批次 |
| 3 | Redis 任务队列 | ✅ 已完成 | 任务生成、优先级出队、状态机、人工审核/重试/取消 |
| 4 | RPA Worker | ✅ 已完成 | Playwright 驱动模拟 ERP，①→⑨ 全流程跑通；含幂等预检、失败截图、心跳、僵尸回收 |
| 5 | AI 分析 | ✅ 已完成 | `ai/` 是可安装包；导入后自动入队 → AI Worker 出队调 DeepSeek → 硬规则合并 → 落 `ai_analyses` → 订单 `ANALYZED` → 建任务 `TASK_CREATED`；含 `GET/POST /ai-analyses` 两个运营端点、`POST /orders/{id}/reanalyze` 重分析端点与 prompt 评测脚本 |
| 6 | 前端 | ✅ 已完成 | Vue3 + Vite 管理后台（登录鉴权、看板、订单/任务/AI 分析/导入批次/通知、导入与复核操作）；**手写 CSS 无组件库**，dev 期走 Vite 代理免 CORS；同时补齐后端 `dashboard`/`notifications` 三个端点 |
| 7 | Docker + 云服务器部署 | ✅ 已完成 | 8 服务 compose 一键起全套（MySQL/Redis/backend/workers/seed/mock-erp/rpa-worker/frontend），完整跑通「导入→AI→RPA→ERP」；**已部署腾讯云服务器**（Ubuntu 24.04 / x86_64，全链路真跑通），公网地址与口令不写入仓库 |
| 8 | 压力测试 + 完善 | ✅ 已完成 | 压测脚本 `scripts/loadtest_import.py`（自管假 LLM + 打桩 Worker，不花钱）：实测 100 单/分钟下导入 300/300、导入 p95 < 60ms；突发 300 单导入仅 **0.07 s**、AI 分析异步排空 99.3 s —— 见 [`docs/压力测试.md`](docs/压力测试.md) |

**当前可以完整演示的闭环**：导入订单 → AI 分析（调 LLM + 硬规则合并） → 生成任务 → 入队 → RPA Worker 领取 → 驱动模拟 ERP 录单 → 回传结果 → 管理员接口查询。
`scripts/seed_demo_task.py` 仍在，用来**跳过 AI** 直接造一笔 `QUEUED` 任务（本地不想花 LLM 费用时用）。

---

## 关键设计决策

摘自《需求规格》§12「关键设计决策（面试必答）」。这些是这套系统里最该讲清楚的地方：

| 决策 | 理由 |
| --- | --- |
| 为什么用 FastAPI？ | 异步支持好、Pydantic 类型校验、自动生成 API 文档，适合 IO 密集的调度类服务 |
| 为什么用 Redis 做队列？ | 订单处理与 RPA 执行解耦；RPA 慢且不可控，不能让 FastAPI 同步等；支持优先级 |
| 为什么 RPA 不直连数据库？ | 职责隔离。RPA 只负责操作 UI，数据一致性由 FastAPI 单点保证 |
| 为什么 RPA 也不直连 Redis？ | 出队/鉴权/状态变更集中在 FastAPI 一处，逻辑单点；RPA 端不用引入客户端、不懂队列协议 |
| Redis 里为什么不存订单详情？ | 避免多份拷贝不一致；队列只做「有活干」的信号；换队列中间件时 RPA 代码不用改 |
| Worker 领了任务后崩溃怎么办？ | 心跳 + 定时扫描回收僵尸任务（`heartbeat_at` 超 5 分钟没有更新就判定为僵尸） |
| 为什么给 RPA 单独的角色和接口组？ | Worker 跑在另一台机器，权限最小化，它的 JWT 只能访问 `/rpa/*` |
| 为什么 RPA 不调 ERP 的 API？ | 真实老 ERP 常无 API；且直接调 API 就不是 RPA 了 |
| AI 和硬规则怎么配合？ | 硬规则处理确定性判断，AI 处理自然语言；冲突时硬规则优先并记录日志 |
| 为什么 AI 分析要异步？ | 100 单/分钟同步调 LLM 会阻塞导入接口；异步后导入与分析解耦 |
| RPA 失败怎么重试？ | 每次执行独立记录，最多 3 次，超限转 FAILED 交人工，保留错误截图便于排查 |
| 任务状态怎么流转？ | 单表 + 状态字段，而不是拆表（状态机见《数据库设计》§4.5） |
| MySQL 存什么？ | 订单、AI 结果、任务、执行记录、修正记录 —— 长期可追溯数据 |
| 为什么要 Docker？ | 环境一致性；一键拉起后端/MySQL/Redis；便于部署到云服务器 |

**两条贯穿全项目的硬边界**（实现时反复守住的）：

1. **`/api/v1/rpa/*` 的接口契约已冻结。** 例如「库存不足」这类永久性失败，按规格仍会照常重试 3 次，
   而不是给契约加一个 `retryable` 字段 —— 代价作为已知局限（见下）记录下来，不靠改接口解决。
2. **RPA Worker 只与主平台说 HTTP，不连 MySQL、不连 Redis。**
   `rpa/requirements.txt` 刻意不含任何数据库/队列驱动 —— 那份清单本身就是这条约束的**可执行证据**：
   想让 Worker 去连库，得先往依赖里加一行，一眼就能看出越界。

---

## 业务流

《需求规格》§5 定义的 15 步闭环，**①→⑮ 全程已跑通**：

```
① 模拟平台生成订单 Excel/CSV
② 管理员在后台导入
③ FastAPI 校验并写入 MySQL(orders)
④ 订单进入「待分析」队列（AI_QUEUE_KEY）
⑤ AI Worker 分析订单文本 → 写 ai_analyses
⑥ 硬规则引擎 + AI 结果合并 → 得出最终判定
⑦ 生成 RPA 任务(tasks)：无异常 → QUEUED；有异常 → WAITING_REVIEW
⑧ 任务 ID + 优先级推入 Redis 队列
⑨ RPA Worker 按优先级取任务
⑩ 调 FastAPI 拉订单详情
⑪ Playwright 操作模拟 ERP（登录→订单管理→新建→填写→保存→提交审核）
⑫ 回传执行结果（成功/失败/截图/错误信息）
⑬ FastAPI 更新任务状态、写执行日志
⑭ 失败则重试（≤3 次），超限转 FAILED → 人工处理
⑮ 管理后台展示全部数据
```

> 第 ⑤⑥ 两步要真的调 LLM（花钱）。本地只想看 RPA 这一段时，可以用
> `scripts/seed_demo_task.py` 跳过 AI、直接生成一个 `QUEUED` 任务
> —— 它与 AI Worker 建任务时调用的是**同一个** `TaskService.generate()`。

---

## 快速开始（本机）

前置：本机已装 MySQL（9.x）与 Redis，Python 3.12，且解释器里装好了依赖。
详细步骤与排障见 [`docs/部署说明.md`](docs/部署说明.md)。

```bash
cp .env.example .env          # 按注释填好 MySQL/Redis/账号/JWT 等
./scripts/init_db.sh          # 建库建表（主库 ai_rpa + 模拟 ERP 库 mock_erp），只需一次
python scripts/seed_admin.py  # 创建管理员账号（口令读自 .env）
python scripts/seed_worker.py # 创建 RPA Worker 账号（最小权限）
python scripts/seed_blacklist.py # 种一条示例黑名单手机号（幂等，表非空则跳过）
pip install -e ai/            # 把 ai/ 装成可编辑包（backend 以规范包方式 import ai），只需一次

./scripts/start.sh            # ① 主平台 backend  → logs/backend.log
./scripts/start_workers.sh    # ② 作业进程（僵尸回收 + AI 补偿 + AI 分析） → logs/workers.log
./scripts/start_mock_erp.sh   # ③ 模拟 ERP  → logs/mock_erp.log
./scripts/run_rpa_worker.sh --foreground --headed   # ④ RPA Worker（--headed 看着它点）

# 造一笔演示订单 + 任务，然后看 Worker 把它录进 ERP
python scripts/seed_demo_task.py

./scripts/stop.sh             # 一次停掉上面四个进程
```

接口文档：<http://127.0.0.1:8000/docs>　健康检查：`GET /api/v1/health`

### 前端管理后台

```bash
cd frontend
npm install     # 首次
npm run dev     # 起 Vite dev server → http://localhost:5173
```

dev server 通过 Vite 代理把 `/api` 转发到 `http://127.0.0.1:8000`，
**后端刻意不开 CORS** —— 开发期的跨域交给代理，生产由 nginx 收口（Phase 7）。
用 `.env` 里的管理员账号登录。生产构建 `npm run build`（`dist/` 与 `node_modules/` 已 gitignore）。

### Docker 一键起全套（Phase 7）

装了 Docker Desktop 后不必一个个起进程 —— compose 把 8 个服务一次拉齐：

```bash
cp .env.example .env      # 填好 MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD / JWT_SECRET / ADMIN_PASSWORD / WORKER_PASSWORD 等
./scripts/deploy.sh up    # = docker compose up -d --build
open http://localhost:8080  # 唯一对外端口；用 .env 里的管理员账号登录
```

`seed` 是一次性服务：首次起容器时自动建管理员/Worker 账号、种黑名单、并导入
`database/seed/04_orders_sample.csv` 的 7 笔样例订单，跑完即退。MySQL 数据卷非空时
不再重灌（想重来跑 `./scripts/deploy.sh reset`，它 `down -v` 清卷）。

容器内一律用服务名寻址（`mysql` / `redis` / `backend` / `mock-erp`）；backend:8000、
mock-erp:8001、MySQL、Redis 都**不**映射到宿主机 —— 只有前端 8080 对外。
`./scripts/deploy.sh logs` 看日志、`down` 停（保留数据）。详见 [`docs/部署说明.md`](docs/部署说明.md)。

### 造一批订单来导入

```bash
python mock/platform/generate_orders.py --count 50      # 生成 CSV 到 mock/platform/out/
# 然后按脚本打印的 curl 示例，带管理员 JWT 调 POST /api/v1/orders/import 导入
```

生成器只负责造文件，不导入、不建任务 —— 导入走接口；导入成功后**自动入队等 AI 分析**，
建任务由 AI Worker 完成。以下几个脚本名容易混，各司其职：

| 脚本 | 输入 | 产出 |
| --- | --- | --- |
| `mock/platform/generate_orders.py` | 无（凭空造） | 订单 **CSV 文件** |
| `POST /api/v1/orders/import` | CSV/Excel 文件 | 库里的**订单** + 分析队列里的一条待办 |
| `scripts/seed_demo_task.py` | 凭空造一笔订单 | 一笔订单 + 一个 **QUEUED** 任务（跳过 AI，仅本地演示） |
| `scripts/gen_tasks.py` | 库里**已有订单** | 库里的**任务** + 入队（绕过 AI 的手工路径） |
| `scripts/loadtest_import.py` | 凭空造（复用生成器） | **导入压测**：按速率打导入 + 采样 AI 队列 + JSON 报告（自管假 LLM 与打桩 Worker，不花钱） |

---

## 目录结构

```
ai/                 AI 分析（Phase 5）—— 独立发行包 ai-rpa-analysis，import 名仍是 ai；
                    LLM 客户端（httpx 异步）/ 输出解析 / 枚举白名单校验 / 兜底 / prompt（当前 v2，v1 保留）；
                    **不许 import backend.app.***（依赖单向：backend → ai）
backend/            主平台 FastAPI 服务
  app/
    api/              路由层（见下方接口清单）
    core/             配置、安全(JWT/bcrypt)、异常、枚举
    models/           SQLAlchemy 模型
    repositories/     数据访问层
    schemas/          Pydantic 请求/响应 + 统一响应外壳
    services/         业务逻辑（导入、任务、队列、审核、RPA 结果处理、
                      硬规则引擎、单笔分析、AI 队列与建任务补偿…）
    workers/          作业进程（僵尸回收 + AI 分析 worker）
  var/                运行时产物（失败截图、评测报告，不入库）
config/             各进程配置样例（空壳，当前配置走 .env）
database/
  schema/             建表 SQL（001~010）
  seed/               种子数据
  migrations/         增量迁移（010 cancel_reason、011 ERP 单号唯一、012 客户黑名单）
docs/               设计文档（需求规格 / 数据库设计 / API 接口设计 / 模拟ERP设计 / …）
frontend/           前端 Vue3 + Vite 管理后台（Phase 6；手写 CSS，dev 走代理 /api → 后端）
mock/
  erp/                模拟 ERP —— 独立 FastAPI + 独立库 mock_erp（扮演「别人家的老系统」）
  platform/           模拟电商平台 —— 订单生成器
rpa/                RPA Worker（Playwright，只与主平台说 HTTP）
  common/             配置、日志、异常分类、HTTP 客户端、浏览器会话
  erp/                选择器契约 + Page Object
  order_sync/         ①→⑨ 录单主流程
  inventory_sync/     库存只读流程
  tests/              单元测试 + 默认跳过的 e2e
scripts/            初始化 / 起停 / 造数据脚本
tests/              主平台测试（backend）+ AI 包单测与评测集（tests/ai）
```

---

## 接口清单

当前**真正挂载**的模块（`backend/app/api/router.py`），统一前缀 `/api/v1`，共 26 个端点。
响应统一外壳 `{code, message, data}`（`code=0` 为成功）；分页数据形如 `{items, total, page, page_size}`。

| 模块 | 方法 | 路径 | 鉴权 |
| --- | --- | --- | --- |
| health | GET | `/health` | 公开 |
| auth | POST | `/auth/login` | 公开 |
| auth | GET | `/auth/me` | 登录用户 |
| auth | POST | `/auth/logout` | 登录用户 |
| orders | GET | `/orders` | 管理员 |
| orders | POST | `/orders/import` | 管理员 |
| orders | GET | `/orders/{order_id}` | 管理员 |
| orders | POST | `/orders/{order_id}/reanalyze` | 管理员 |
| import-batches | GET | `/import-batches` | 管理员 |
| import-batches | GET | `/import-batches/{batch_id}` | 管理员 |
| ai-analyses | GET | `/ai-analyses` | 管理员 |
| ai-analyses | POST | `/ai-analyses/{analysis_id}/review` | 管理员 |
| tasks | GET | `/tasks` | 管理员 |
| tasks | GET | `/tasks/{task_id}` | 管理员 |
| tasks | GET | `/tasks/{task_id}/executions` | 管理员 |
| tasks | POST | `/tasks/{task_id}/review` | 管理员 |
| tasks | POST | `/tasks/{task_id}/retry` | 管理员 |
| tasks | POST | `/tasks/{task_id}/cancel` | 管理员 |
| tasks | GET | `/tasks/{task_id}/executions/{execution_id}/screenshot` | 管理员 |
| rpa | POST | `/rpa/tasks/claim` | Worker |
| rpa | POST | `/rpa/tasks/{task_id}/heartbeat` | Worker |
| rpa | POST | `/rpa/tasks/{task_id}/result` | Worker |
| rpa | POST | `/rpa/tasks/{task_id}/screenshot` | Worker |
| dashboard | GET | `/dashboard/summary` | 管理员 |
| dashboard | GET | `/dashboard/trends` | 管理员 |
| notifications | GET | `/notifications` | 管理员 |

> Phase 6 补齐的 `dashboard` / `notifications` 见《API接口设计》§11 / §12；
> 至此《API接口设计》里的端点已全部挂载，无空壳路由。

---

## 配置

所有配置集中在根目录 `.env`（样例见 `.env.example`）。按用途分组：

| 分组 | 键 | 说明 |
| --- | --- | --- |
| 应用 | `APP_NAME` `APP_ENV` `DEBUG` `API_V1_PREFIX` `APP_HOST` `APP_PORT` `PYTHON_BIN` | `PYTHON_BIN` 供各起停脚本读取，脚本里不写死任何机器的路径 |
| MySQL | `MYSQL_HOST` `MYSQL_PORT` `MYSQL_USER` `MYSQL_PASSWORD` `MYSQL_DB` | 主库 |
| Redis | `REDIS_HOST` `REDIS_PORT` `REDIS_DB` `REDIS_PASSWORD` | 任务队列 |
| JWT | `JWT_SECRET` `JWT_ALGORITHM` `JWT_EXPIRE_MINUTES` | |
| 账号种子 | `ADMIN_USERNAME/PASSWORD`、`WORKER_USERNAME/PASSWORD` | 由 `seed_admin.py` / `seed_worker.py` 读取 |
| 队列 / 僵尸回收 | `TASK_QUEUE_KEY` `ZOMBIE_TIMEOUT_SECONDS` `ZOMBIE_SCAN_INTERVAL_SECONDS` | 超过时限没心跳的任务被回收重排 |
| AI 分析队列 | `AI_QUEUE_KEY` `AI_ENQUEUE_GRACE_SECONDS` `AI_ANALYZING_TIMEOUT_SECONDS` | **刻意与任务队列分开的第二个 key**：任务队列按优先级出队（HIGH 能插队），分析队列只能 FIFO（分析前不知道紧急度，那正是分析的产出）。后两项是补偿扫描的两条时间线 |
| AI 分析 | `AI_BASE_URL` `AI_API_KEY` `AI_MODEL` `AI_TIMEOUT_SECONDS` `AI_WORKER_CONCURRENCY` `AI_WORKER_POLL_INTERVAL_SECONDS` | `AI_API_KEY` 为空时 `ai_worker` 空转（不崩作业循环）；`AI_WORKER_CONCURRENCY` 控每轮并发分析数，`AI_WORKER_POLL_INTERVAL_SECONDS` 是队空时的轮询间隔 |
| RPA Worker | `RPA_API_BASE_URL` `RPA_WORKER_NAME` `RPA_HEADLESS` `RPA_NAV_TIMEOUT_MS` `RPA_ACTION_TIMEOUT_MS` `RPA_POLL_WAIT_SECONDS` `RPA_HEARTBEAT_SECONDS` | |
| 模拟 ERP | `MOCK_ERP_BASE_URL` `MOCK_ERP_USERNAME` `MOCK_ERP_PASSWORD` `MOCK_ERP_HOST` `MOCK_ERP_PORT` `MOCK_ERP_DB` `MOCK_ERP_SESSION_SECRET` `MOCK_ERP_SESSION_MINUTES` `MOCK_ERP_PAGE_DELAY_MS` `MOCK_ERP_FAIL_RATE` `MOCK_ERP_ENABLE_REVIEW_PAGE` | `MOCK_ERP_PAGE_DELAY_MS` 刻意让每次跳转变慢，逼 RPA 用「等待」而不是「睡觉」；`MOCK_ERP_FAIL_RATE` 用于故障注入 |
| 其他 | `SCREENSHOT_MAX_BYTES` `SCREENSHOT_DIR` | |

---

## 测试

三套测试**各自独立**跑（每个包有自己的 `pytest.ini`，`pythonpath` 语义不同，不能合成一次）：

```bash
PY=/opt/anaconda3/envs/ai-rpa/bin/python   # 或 .env 里 PYTHON_BIN 指向的解释器

$PY -m pytest                    # 主平台 backend + AI 包单测 —— 246 个（见下）
$PY -m pytest mock/erp           # 模拟 ERP                  —— 93 个
$PY -m pytest rpa/tests          # RPA Worker 单元           —— 63 个（默认跳过 e2e）
$PY -m pytest rpa/tests -m e2e   # RPA 端到端                 —— 2 个（见下）

./scripts/eval_prompt.sh         # LLM prompt 评测集 —— 30 条，真调 API、花钱，默认不跑

$PY scripts/loadtest_import.py --total 300 --rate 100   # 导入压测（100 单/分钟，自管假 LLM，不花钱）
```

`tests/ai/` 的评测集打了 `eval` marker，`pytest.ini` 里 `-m "not eval"` 默认把它排除，
**它真的调 DeepSeek API（花钱）**，只在需要评估 prompt 质量时手动跑。
它需要 `.env` 里有非空 `AI_API_KEY`，否则跳过。

**e2e 的前置**（不满足会**跳过而非失败**，避免「忘起服务」看起来像代码坏了）：
主平台与模拟 ERP 都在跑、本机装了 Chromium，**且队列里除了刚 seed 的那一笔没有别的待办**
（Worker 是 FIFO 领任务，队列里堆着别的任务会先干那些，测试最终会拿到一个含义不明的超时）。
`-m e2e` 必须显式加：`rpa/pytest.ini` 的 `addopts` 里有 `-m "not e2e"`，命令行上的 `-m` 排在它后面会覆盖它。

---

## 已知局限（v1）

《需求规格》§14 明确要求写进 README —— 主动说局限比假装没有更有说服力：

- **重试无退避延迟**：3 次快速失败的实际意义有限。
- **永久性失败也照规格重试**（如「库存不足」本身不会因为重试而成功）：为保持 `/rpa/*` 契约冻结，不加 `retryable` 字段。
- **单 RPA Worker，无并发调度**：v1 明确不做多 Worker。
- **JWT 无法主动失效**：没有黑名单，登出只是客户端丢弃 token。
- **订单导入要求整批成功，不做部分成功**：任何一行不合法，整批零插入。
- **无多角色权限体系**：v1 只有管理员（外加一个权限最小的 Worker 角色）。
- **通知渠道未真实接入**：通知只落库，不发企业微信/钉钉/短信。
- **任务队列只有手动对账**：Redis 任务队列丢了或变脏时，只有手动跑
  `python scripts/gen_tasks.py --reconcile` 重建（它是「先清空再全量重写」，
  属**停机维护操作**，不能有 Worker 在跑）。
  AI 分析队列这一侧已经有周期性自动补偿（`services/ai_reconciler.py`，扫库补 L1/L2/L3），
  但**任务队列这一侧没有** —— 这是刻意留的差异，两边补起来要动的面不一样大。
- **`CustomerBlacklist` 只有只读种子，没有管理 API**：加/删黑名单得直接改库
  （`scripts/seed_blacklist.py` 只在表为空时种一条示例）。

---

## 更多文档

- [`docs/需求规格.md`](docs/需求规格.md) —— 范围、架构、状态机、设计决策、开发阶段
- [`docs/数据库设计.md`](docs/数据库设计.md) —— 表结构、状态机、索引
- [`docs/API接口设计.md`](docs/API接口设计.md) —— 接口契约与错误码
- [`docs/模拟ERP设计.md`](docs/模拟ERP设计.md) —— 模拟 ERP 的页面、选择器契约与「刻意留的坑」
- [`docs/部署说明.md`](docs/部署说明.md) —— 本机部署与排障
- [`docs/压力测试.md`](docs/压力测试.md) —— 压测方法与实测数字（100 单/分钟解耦验证）
