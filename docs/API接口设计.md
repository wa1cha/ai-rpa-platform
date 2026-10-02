# AI-RPA 电商订单智能自动化平台 — API 接口设计 v1

> 配套文档：`docs/需求规格.md`、`docs/数据库设计.md`
> 协议：HTTP / REST / JSON　Base URL：`/api/v1`

---

## 1. 通用约定

| 项 | 约定 |
| --- | --- |
| Base URL | `/api/v1` |
| 数据格式 | `application/json; charset=utf-8` |
| 文件上传 | `multipart/form-data` |
| 认证 | `Authorization: Bearer <jwt>` |
| 时间格式 | `YYYY-MM-DD HH:mm:ss`（本地时间，`+08:00`） |
| 分页参数 | `page`（从 1 开始）、`page_size`（默认 20，最大 100） |
| 排序 | 列表默认按 `created_at DESC` |

### 1.1 两类调用方

| 调用方 | 接口前缀 | 认证 |
| --- | --- | --- |
| 管理后台（Vue） | `/api/v1/*` | 管理员 JWT |
| RPA Worker | `/api/v1/rpa/*` | Worker JWT（独立账号，只能访问 `/rpa/*`） |

**为什么给 RPA 单独的账号和接口组**：Worker 跑在另一台机器（Windows），权限必须最小化。它只需「领任务 / 回传 / 上传截图」三件事，不应该能访问订单列表、用户管理等接口。JWT 里带 `role`，服务端按角色鉴权。

---

## 2. 统一响应结构

### 2.1 成功

```json
{
  "code": 0,
  "message": "ok",
  "data": { }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | int | `0` 表示成功，非 0 为业务错误码 |
| `message` | string | 提示信息，成功时固定 `ok` |
| `data` | any | 业务数据，可为 `null` |

### 2.2 分页

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [],
    "total": 128,
    "page": 1,
    "page_size": 20
  }
}
```

### 2.3 失败

```json
{
  "code": 4004,
  "message": "任务不存在",
  "data": null
}
```

> **为什么不直接裸返回数组**：统一外层结构让前端只需写一次响应拦截器（拿 `code` 判断成败、拿 `data` 取业务数据）。代价是多一层嵌套，v1 接受。

---

## 3. 错误码

HTTP 状态码表达「传输/鉴权层」结果，`code` 表达「业务层」结果，两者配合。

| HTTP | code | 含义 | 典型场景 |
| --- | --- | --- | --- |
| 200 | 0 | 成功 | |
| 400 | 4000 | 参数校验失败 | 缺必填字段、格式错误 |
| 401 | 4001 | 未认证 / token 失效 | 未带 token、token 过期 |
| 401 | 4002 | 用户名或密码错误 | 登录失败 |
| 403 | 4003 | 无权限 | RPA 账号访问管理接口 |
| 404 | 4004 | 资源不存在 | 订单/任务 ID 不存在 |
| 409 | 4009 | **当前状态不允许该操作** | 对 `RUNNING` 任务调审核 |
| 409 | 4010 | 冲突（唯一键） | 订单号重复导入 |
| 422 | 4020 | Excel 文件解析失败 | 表头不对、文件损坏 |
| 500 | 5000 | 服务器内部错误 | |
| 502 | 5001 | AI 服务调用失败 | LLM 超时/报错 |
| 503 | 5002 | 队列不可用 | Redis 连不上 |

> `4009` 是本项目最常用也最该讲清楚的错误码：**状态机是业务的核心约束**，任何违反状态流转的请求都该被明确拒绝，而不是静默改数据。例如对 `SUCCESS` 的任务再调 `/retry`，必须返回 4009。

---

## 4. 接口总览

| 分组 | 数量 | 说明 |
| --- | --- | --- |
| [认证](#5-认证-auth) | 3 | 登录、当前用户、登出 |
| [订单](#6-订单-orders) | 4 | 列表、详情、导入、重新分析 |
| [导入批次](#7-导入批次-import-batches) | 2 | 批次列表、批次详情 |
| [AI 分析](#8-ai-分析-ai-analyses) | 2 | 列表、人工修正 |
| [任务](#9-任务-tasks) | 7 | 列表、详情、审核、重试、取消、执行记录、下载截图 |
| [RPA（Worker 专用）](#10-rpa-worker-专用) | 4 | 领取、心跳、回传结果、上传截图 |
| [看板](#11-看板-dashboard) | 2 | 汇总、趋势 |
| [通知](#12-通知-notifications) | 1 | 列表（v1 只读） |
| [系统](#13-系统-system) | 1 | 健康检查 |
| **合计** | **26** | |

---

## 5. 认证 Auth

### 5.1 登录

```
POST /api/v1/auth/login
```

**权限**：公开

**请求体**

```json
{
  "username": "admin",
  "password": "******"
}
```

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIs...",
    "token_type": "Bearer",
    "expires_in": 43200,
    "user": {
      "id": 1,
      "username": "admin",
      "role": "ADMIN"
    }
  }
}
```

**说明**
- `expires_in` 单位秒，默认 12 小时
- 密码用 bcrypt 校验，失败返回 `code=4002`
- 成功时更新 `users.last_login_at`

### 5.2 当前用户

```
GET /api/v1/auth/me
```

**权限**：已登录

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "id": 1,
    "username": "admin",
    "role": "ADMIN",
    "last_login_at": "2026-09-30 10:12:33"
  }
}
```

### 5.3 登出

```
POST /api/v1/auth/logout
```

**权限**：已登录

**说明**：JWT 无状态，服务端不维护黑名单，此接口**只写审计日志**，前端负责丢弃 token。返回 `data: null`。

> 这是 JWT 的固有取舍：换来无状态和易扩展，代价是**无法主动踢人下线**。v1 单管理员场景可接受；如果要支持「强制下线」，需要引入 Redis 存 token 黑名单——那条路会把 JWT 的收益吃掉大半。

---

## 6. 订单 Orders

### 6.1 订单列表

```
GET /api/v1/orders
```

**权限**：管理员

**Query 参数**

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `status` | string | 否 | 按订单状态筛选，可多值逗号分隔 |
| `platform` | string | 否 | 来源平台 |
| `order_no` | string | 否 | 订单号，精确匹配 |
| `customer_name` | string | 否 | 客户名，前缀匹配 |
| `has_risk` | bool | 否 | 只显示有风险的订单 |
| `start_date` | date | 否 | 下单时间起 |
| `end_date` | date | 否 | 下单时间止 |
| `page` / `page_size` | int | 否 | 分页 |

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": 1001,
        "order_no": "MOCK20260930001",
        "platform": "mock",
        "ordered_at": "2026-09-30 09:15:00",
        "customer_name": "张三",
        "phone": "138****8888",
        "product_name": "儿童积木套装",
        "sku": "SKU-001",
        "quantity": 1,
        "amount": "299.00",
        "status": "TASK_CREATED",
        "risk_level": "LOW",
        "priority": "HIGH",
        "task_id": 2001,
        "task_status": "QUEUED"
      }
    ],
    "total": 128,
    "page": 1,
    "page_size": 20
  }
}
```

**说明**
- `risk_level` / `priority` 来自**该订单最新一次** AI 分析（`ai_analyses` 按 `created_at DESC` 取第一条）
- `task_id` / `task_status` 来自关联任务，列表页直接拼出「订单→任务」的视图，前端不用二次请求
- `phone` 在列表中**脱敏**，详情页才返回完整值

### 6.2 订单详情

```
GET /api/v1/orders/{id}
```

**权限**：管理员

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "id": 1001,
    "order_no": "MOCK20260930001",
    "platform": "mock",
    "ordered_at": "2026-09-30 09:15:00",
    "customer_name": "张三",
    "phone": "13800008888",
    "address": "浙江省杭州市西湖区文三路 100 号",
    "product_name": "儿童积木套装",
    "sku": "SKU-001",
    "quantity": 1,
    "amount": "299.00",
    "buyer_message": "孩子明天生日，希望今天能发货",
    "seller_note": null,
    "status": "TASK_CREATED",
    "imported_at": "2026-09-30 09:16:02",
    "latest_analysis": {
      "id": 3001,
      "priority": "HIGH",
      "deadline": "TODAY",
      "need_contact": false,
      "risk_level": "LOW",
      "risk_reason": "客户要求当天发货",
      "action": "优先安排出库",
      "model_name": "deepseek-chat",
      "created_at": "2026-09-30 09:16:10"
    },
    "task": {
      "id": 2001,
      "status": "QUEUED",
      "priority": "HIGH",
      "retry_count": 0,
      "max_retry": 3,
      "created_at": "2026-09-30 09:16:12"
    },
    "review_logs": [
      {
        "field_name": "priority",
        "original_value": "MEDIUM",
        "new_value": "HIGH",
        "reviewer": "admin",
        "reviewed_at": "2026-09-30 09:20:00"
      }
    ]
  }
}
```

**说明**：详情页一次返回订单 + 最新 AI 结果 + 任务摘要 + 人工修正记录，避免前端连打 4 个接口。

### 6.3 导入订单

```
POST /api/v1/orders/import
Content-Type: multipart/form-data
```

**权限**：管理员

**请求**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | file | 是 | `.xlsx` / `.csv`，最大 10 MB |
| `dry_run` | bool | 否 | `true` 时只校验不入库，返回预计结果 |

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "batch_id": 501,
    "filename": "orders_20260930.xlsx",
    "total_rows": 120,
    "success_rows": 118,
    "failed_rows": 2,
    "status": "COMPLETED",
    "errors": [
      { "row": 3, "order_no": "MOCK20260930003", "reason": "手机号格式错误" },
      { "row": 47, "order_no": "MOCK20260930001", "reason": "订单号已存在" }
    ]
  }
}
```

**校验规则**

| 字段 | 规则 |
| --- | --- |
| `order_no` | 必填，≤64 字符，**全局唯一**（对库内已有数据查重） |
| `ordered_at` | 必填，可被解析为日期时间 |
| `customer_name` | 必填，≤64 字符 |
| `phone` | 必填，11 位数字（`^1[3-9]\d{9}$`） |
| `address` | 必填，≤512 字符 |
| `product_name` / `sku` | 必填 |
| `quantity` | 必填，整数 ≥ 1 |
| `amount` | 必填，decimal ≥ 0 |
| `buyer_message` / `seller_note` | 选填，≤1000 字符 |

**部分失败策略（重要）**

采用**逐行校验、整批提交**：

1. 先全部解析校验，收集所有错误行
2. 有错误行 → **整批不入库**，`import_batches.status='FAILED'`，错误明细写入 `error_detail`
3. 全部通过 → 开事务一次性写入所有订单，`status='COMPLETED'`

**为什么不做「部分成功」**：导入一半会让运维难以判断数据状态，且重试时更难处理（哪些已入？）。整批原子性让「要么全进，要么全不进」语义清晰，重试就是重传一次。代价是 1 行错就要全部重来——但 Excel 通常由固定模板生成，错误是成片的（如模板列错位），整体重传反而更快。

**导入成功后**：所有新订单置 `status='IMPORTED'`，并推入「待分析」队列，由 AI Worker 异步消费。

### 6.4 重新触发 AI 分析

```
POST /api/v1/orders/{id}/reanalyze
```

**权限**：管理员

**请求体**

```json
{
  "reason": "AI 判断与实际不符"
}
```

**响应**：`data: { "order_id": 1001, "status": "ANALYZING" }`

**说明**
- 允许状态：`ANALYZED` / `TASK_CREATED`（即已分析过的订单）
- 若关联任务已进入 `RUNNING` 或终态，返回 `4009`
- 重新分析会在 `ai_analyses` **新增一条记录**，不覆盖旧记录，便于对比模型效果

---

## 7. 导入批次 Import Batches

### 7.1 批次列表

```
GET /api/v1/import-batches?page=1&page_size=20&status=FAILED
```

**权限**：管理员

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": 501,
        "filename": "orders_20260930.xlsx",
        "uploaded_by": "admin",
        "total_rows": 120,
        "success_rows": 118,
        "failed_rows": 2,
        "status": "COMPLETED",
        "created_at": "2026-09-30 09:16:00",
        "finished_at": "2026-09-30 09:16:02"
      }
    ],
    "total": 12,
    "page": 1,
    "page_size": 20
  }
}
```

### 7.2 批次详情

```
GET /api/v1/import-batches/{id}
```

**响应**：在列表字段基础上追加

```json
{
  "errors": [
    { "row": 3, "order_no": "MOCK20260930003", "reason": "手机号格式错误" }
  ],
  "order_ids": [1001, 1002, 1003]
}
```

> `order_ids` 让后台可以「点批次 → 跳到该批次导入的订单列表」。

---

## 8. AI 分析 AI Analyses

### 8.1 分析结果列表

```
GET /api/v1/ai-analyses?risk_level=MEDIUM&priority=HIGH&page=1&page_size=20
```

**权限**：管理员

**Query 参数**

| 参数 | 说明 |
| --- | --- |
| `risk_level` | `LOW` / `MEDIUM` / `HIGH` |
| `priority` | `LOW` / `MEDIUM` / `HIGH` |
| `need_contact` | bool |
| `status` | `SUCCESS` / `FAILED`（查 AI 调用失败的分析） |
| `order_no` | 按订单号筛 |

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": 3001,
        "order_id": 1001,
        "order_no": "MOCK20260930001",
        "buyer_message": "孩子明天生日，希望今天能发货",
        "priority": "HIGH",
        "deadline": "TODAY",
        "need_contact": false,
        "risk_level": "LOW",
        "risk_reason": "客户要求当天发货",
        "action": "优先安排出库",
        "model_name": "deepseek-chat",
        "status": "SUCCESS",
        "created_at": "2026-09-30 09:16:10"
      }
    ],
    "total": 45,
    "page": 1,
    "page_size": 20
  }
}
```

### 8.2 人工修正 AI 结果

```
POST /api/v1/ai-analyses/{id}/review
```

**权限**：管理员

**请求体**

```json
{
  "changes": [
    { "field_name": "priority", "new_value": "MEDIUM" },
    { "field_name": "need_contact", "new_value": "false" }
  ],
  "reason": "客户是常客，留言只是随口一提"
}
```

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "analysis_id": 3001,
    "applied": [
      { "field_name": "priority", "original_value": "HIGH", "new_value": "MEDIUM" },
      { "field_name": "need_contact", "original_value": "true", "new_value": "false" }
    ]
  }
}
```

**约束**

| 字段 | 允许值 |
| --- | --- |
| `priority` | `LOW` / `MEDIUM` / `HIGH` |
| `deadline` | `TODAY` / `TOMORROW` / `NONE` / `YYYY-MM-DD` |
| `need_contact` | `true` / `false` |
| `risk_level` | `LOW` / `MEDIUM` / `HIGH` |
| `risk_reason` / `action` | 自由文本，≤500 字符 |

**行为**
1. 按 `changes` 更新 `ai_analyses` 的业务字段
2. 每个字段写一条 `ai_review_logs`（原值 + 新值 + 修正人）
3. 若该订单的任务**仍在 `WAITING_REVIEW`**，同步更新 `tasks.priority`
4. 若任务已 `QUEUED` / `RUNNING`，只记录修正，**不改变正在执行的任务**（返回结果中额外给 `task_updated: false`）

> 第 4 点是刻意的：**不让后台改数据去影响正在执行的任务**。否则 RPA 拿着旧数据执行、后端已改，状态会不一致。要改就得先取消任务再重建。

---

## 9. 任务 Tasks

### 9.1 任务列表

```
GET /api/v1/tasks?status=QUEUED&priority=HIGH&page=1&page_size=20
```

**权限**：管理员

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": 2001,
        "order_id": 1001,
        "order_no": "MOCK20260930001",
        "customer_name": "张三",
        "priority": "HIGH",
        "status": "QUEUED",
        "need_review": false,
        "retry_count": 0,
        "max_retry": 3,
        "last_error": null,
        "claimed_by": null,
        "created_at": "2026-09-30 09:16:12"
      }
    ],
    "total": 60,
    "page": 1,
    "page_size": 20
  }
}
```

### 9.2 任务详情

```
GET /api/v1/tasks/{id}
```

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "id": 2001,
    "order": { "...完整订单..." },
    "analysis": { "...生成该任务的 AI 分析..." },
    "priority": "HIGH",
    "status": "QUEUED",
    "need_review": false,
    "retry_count": 0,
    "max_retry": 3,
    "claimed_by": null,
    "review_result": null,
    "review_reason": null,
    "cancel_reason": null,
    "queued_at": "2026-09-30 09:16:12",
    "executions": [
      {
        "id": 4001,
        "attempt": 1,
        "status": "FAILED",
        "worker_name": "rpa-worker-01",
        "started_at": "2026-09-30 09:20:00",
        "finished_at": "2026-09-30 09:20:35",
        "duration_ms": 35000,
        "error_code": "ERP_LOGIN_FAILED",
        "error_message": "登录超时",
        "screenshot_path": "4001.png"
      }
    ]
  }
}
```

> `review_reason` 与 `cancel_reason` 是**两件事**：前者是审核意见，后者是取消 / 人工处置说明。
> Phase 4 之前 `cancel` / `retry` 把说明借用到了 `review_reason` 上，会产生
> 「`CANCELLED` 但 `review_result` 为 NULL、`review_reason` 有值」这种要靠猜的行，
> 现已拆成独立列。见《数据库设计》§4.5。

> `screenshot_path` 里是**文件名**，不是可访问 URL，也不是绝对路径。取图走 §9.7 的接口；
> 存文件名而非路径是为了换部署目录（本机 / 容器挂载点不同）时库里不会留下一堆失效路径。

### 9.3 人工审核任务

```
POST /api/v1/tasks/{id}/review
```

**权限**：管理员

**请求体**

```json
{
  "result": "APPROVED",
  "reason": "已确认可发货"
}
```

**响应**：`data: { "task_id": 2001, "status": "QUEUED" }`

**状态约束**

| 当前状态 | 结果 | 新状态 |
| --- | --- | --- |
| `WAITING_REVIEW` | `APPROVED` | `QUEUED`（推入 Redis） |
| `WAITING_REVIEW` | `REJECTED` | `CANCELLED` |
| 其他状态 | — | 返回 `4009` |

### 9.4 手动重试任务

```
POST /api/v1/tasks/{id}/retry
```

**权限**：管理员

**请求体**

```json
{
  "reset_retry_count": true,
  "reason": "ERP 已恢复"
}
```

**响应**：`data: { "task_id": 2001, "status": "QUEUED", "retry_count": 0 }`

**状态约束**：仅允许对 `FAILED` 操作，否则 `4009`。

`reset_retry_count=true` 时把 `retry_count` 归零——人工介入的场景往往已经解决了根因，不该再消耗自动重试次数。

### 9.5 取消任务

```
POST /api/v1/tasks/{id}/cancel
```

**权限**：管理员

**请求体**

```json
{
  "reason": "客户已取消订单"
}
```

**状态约束**：`PENDING` / `WAITING_REVIEW` / `QUEUED` 可取消 → `CANCELLED`。
`RUNNING` **不可取消**（RPA 正在操作 ERP，强行中断可能留下脏数据），返回 `4009`，提示「请等待当前执行结束」。

> 这是个真实取舍：允许中断 `RUNNING` 任务会让「主库状态」和「ERP 实际状态」可能不一致。v1 选择让执行自然结束，人工在结束后再处理。

### 9.6 任务执行记录

```
GET /api/v1/tasks/{id}/executions
```

**响应**：执行记录数组（含每次重试），结构同 9.2 的 `executions`。

### 9.7 下载失败截图

```
GET /api/v1/tasks/{id}/executions/{execution_id}/screenshot
```

**权限**：管理员

**响应**：图片二进制（`Content-Type: image/png` 或 `image/jpeg`）。

> **不套 `{code, message, data}` 外壳** —— 那一层结构装不下图片字节。失败时仍走统一错误响应。

**为什么截图不放在静态目录、要单开一个鉴权接口**：截图拍的是 ERP 页面，
图里有客户姓名、电话、地址**明文**，而本项目的订单列表是特意给手机号脱敏的 ——
让这些图能被无鉴权地 GET 到，等于一边遮一边漏。所以文件落在 webroot 之外
（`backend/var/screenshots/`），只能带管理员 JWT 从这个接口取。

**为什么 Worker 传得上去、却读不回来**：上传走 §10.4（Worker 角色），读取是
管理员角色 —— Worker 没有回头翻看截图的场景，不需要给它这个权限。

**路径穿越**：库里的 `screenshot_path` 是我们自己生成的文件名，但读取时仍取
`Path(...).name` 剥掉任何目录成分 —— 万一以后有人从别处把值灌进来，
这一行就挡住了 `../../etc/passwd` 这类穿越。

---

## 10. RPA（Worker 专用）

> 前缀 `/api/v1/rpa`，需 Worker 角色 JWT。管理后台账号访问返回 `4003`。

### 10.1 领取任务

```
POST /api/v1/rpa/tasks/claim
```

**请求体**

```json
{
  "worker_name": "rpa-worker-01",
  "wait_seconds": 20
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `worker_name` | string | 是 | Worker 标识，写入 `tasks.claimed_by` |
| `wait_seconds` | int | 否 | 长轮询等待秒数，`0~30`，默认 `0` |

**响应（有任务）**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "task": {
      "id": 2001,
      "priority": "HIGH",
      "attempt": 1,
      "need_review": false
    },
    "order": {
      "order_no": "MOCK20260930001",
      "customer_name": "张三",
      "phone": "13800008888",
      "address": "浙江省杭州市西湖区文三路 100 号",
      "product_name": "儿童积木套装",
      "sku": "SKU-001",
      "quantity": 1,
      "amount": "299.00",
      "buyer_message": "孩子明天生日，希望今天能发货"
    },
    "instruction": {
      "erp_url": "http://mock-erp:8001",
      "action": "CREATE_ORDER",
      "submit_for_review": true
    }
  }
}
```

**响应（无任务）**

```json
{ "code": 0, "message": "ok", "data": null }
```

**服务端行为（按序）**

1. 从 Redis 出队：取 `priority` 最高、同级 `created_at` 最早的任务
2. 原子更新 MySQL：`status='RUNNING'`、`claimed_by=worker_name`、`heartbeat_at=NOW()`、`retry_count` 不变
3. 写一条 `task_executions`（`attempt = retry_count + 1`，`status='RUNNING'`）
4. 返回完整任务 + 订单数据

**关于长轮询**：`wait_seconds > 0` 时，若当前无任务则挂起等待（最长 30 秒），有新任务立即返回。**避免 Worker 空转轮询**——每 100ms 问一次会把 MySQL 和 FastAPI 打满。

**关于「无任务返回 200 + null」**：不用 `204 No Content`，保持响应结构统一，Worker 端解析逻辑只有一条路径。

### 10.2 心跳

```
POST /api/v1/rpa/tasks/{id}/heartbeat
```

**请求体**

```json
{ "worker_name": "rpa-worker-01" }
```

**响应**：`data: { "task_id": 2001, "status": "RUNNING", "cancel": false }`

> 任务不存在时**也回 200**，`status: "MISSING"`、`cancel: true`。心跳是高频、
> 无人值守的调用，抛 404 会让 Worker 端的自然反应变成「重试」，而这里要的是「立刻停手」。

**说明**
- Worker 每 **30 秒**调用一次
- 服务端更新 `tasks.heartbeat_at`
- 若任务已不是 `RUNNING`（被回收/取消），响应 `data.cancel = true`，Worker 应**立即停止当前执行**

> `cancel` 机制是配合僵尸回收用的：回收后任务状态变了，但 Worker 可能还活着。它下一次心跳时会得知「这活已经不归你了」，主动退出，避免两个 Worker 同时操作同一订单。

### 10.3 回传执行结果

```
POST /api/v1/rpa/tasks/{id}/result
```

**请求体（成功）**

```json
{
  "worker_name": "rpa-worker-01",
  "success": true,
  "erp_order_no": "ERP20260930001",
  "duration_ms": 18400
}
```

**请求体（失败）**

```json
{
  "worker_name": "rpa-worker-01",
  "success": false,
  "error_code": "ERP_SUBMIT_FAILED",
  "error_message": "提交审核时提示：收货地址超出配送范围",
  "duration_ms": 25000
}
```

**响应（成功）**

```json
{
  "code": 0,
  "message": "ok",
  "data": { "task_id": 2001, "status": "SUCCESS", "retry_scheduled": false }
}
```

**响应（失败且将重试）**

```json
{
  "code": 0,
  "message": "ok",
  "data": { "task_id": 2001, "status": "QUEUED", "retry_scheduled": true, "retry_count": 1 }
}
```

**响应（失败且重试超限）**

```json
{
  "code": 0,
  "message": "ok",
  "data": { "task_id": 2001, "status": "FAILED", "retry_scheduled": false }
}
```

**服务端行为**

| 结果 | 处理 |
| --- | --- |
| 成功 | `tasks.status='SUCCESS'`、`finished_at` 更新；`orders.status='COMPLETED'`；当前 `task_executions` 置 `SUCCESS` |
| 失败，`retry_count < max_retry` | `retry_count + 1`；`status='QUEUED'` 重新入队；`task_executions` 置 `FAILED` |
| 失败，`retry_count >= max_retry` | `status='FAILED'`；`orders.status='FAILED'`；写一条 `notifications`（`type='TASK_FAILED'`） |

> **重试为什么不带延迟**：v1 的失败大多是「ERP 页面结构变了 / 元素没加载出来」这类，立即重试有意义。**注意**：这是 v1 的简化，真实系统需要退避（如 1 分钟 / 5 分钟 / 15 分钟），否则快速失败 3 次毫无意义。这一点要写进 README 的「已知局限」。

### 10.4 上传失败截图

```
POST /api/v1/rpa/tasks/{id}/screenshot
Content-Type: multipart/form-data
```

**请求**：`file`（PNG/JPG，最大 5 MB）、`attempt`（int，对应第几次尝试）

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": { "path": "/api/v1/tasks/2001/executions/4001/screenshot" }
}
```

> 返回的是**取图接口路径**（§9.7，需管理员 JWT），不是静态文件 URL ——
> 截图含客户明文信息，见 §9.7 的说明。

**为什么单独一个接口而不是塞进 `result`**：`result` 是 JSON，混文件会变成 multipart 解析，复杂度上去了。分开后职责清晰：先传图拿路径，再回传结果。Worker 端失败时的调用顺序是 **screenshot → result**。

---

## 11. 看板 Dashboard

### 11.1 汇总统计

```
GET /api/v1/dashboard/summary
```

**权限**：管理员

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "orders": {
      "total": 1280,
      "today": 120,
      "by_status": {
        "IMPORTED": 5,
        "ANALYZING": 3,
        "ANALYZED": 2,
        "TASK_CREATED": 1100,
        "COMPLETED": 150,
        "FAILED": 20
      }
    },
    "tasks": {
      "total": 1275,
      "by_status": {
        "PENDING": 0,
        "WAITING_REVIEW": 12,
        "QUEUED": 18,
        "RUNNING": 1,
        "SUCCESS": 1224,
        "FAILED": 15,
        "CANCELLED": 5
      },
      "by_priority": { "HIGH": 320, "MEDIUM": 800, "LOW": 155 }
    },
    "risk": {
      "high": 8,
      "medium": 26,
      "need_contact": 5
    },
    "rpa": {
      "workers_online": 1,
      "last_success_at": "2026-09-30 10:05:12"
    }
  }
}
```

> `rpa.workers_online` 通过 `tasks` 里最近 2 分钟内有 `heartbeat_at` 的 `claimed_by` 去重统计得出。不额外维护在线表。

### 11.2 趋势

```
GET /api/v1/dashboard/trends?days=7
```

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "days": [
      { "date": "2026-09-24", "imported": 100, "success": 95, "failed": 5 },
      { "date": "2026-09-25", "imported": 120, "success": 118, "failed": 2 }
    ]
  }
}
```

---

## 12. 通知 Notifications

### 12.1 通知列表

```
GET /api/v1/notifications?status=PENDING&page=1&page_size=20
```

**权限**：管理员

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": 9001,
        "type": "TASK_FAILED",
        "channel": "LOG",
        "title": "任务执行失败",
        "content": "订单 MOCK20260930001 在 3 次重试后仍失败",
        "status": "PENDING",
        "related_task_id": 2001,
        "created_at": "2026-09-30 10:00:00",
        "sent_at": null
      }
    ],
    "total": 15,
    "page": 1,
    "page_size": 20
  }
}
```

**说明**：v1 是**只读**列表（无「标记已读」「发送」接口），因为真实渠道未接入。后台把它当作「系统告警流水」展示。

---

## 13. 系统 System

### 13.1 健康检查

```
GET /api/v1/health
```

**权限**：公开

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "status": "healthy",
    "checks": {
      "mysql": "ok",
      "redis": "ok",
      "ai": "ok"
    },
    "version": "0.1.0"
  }
}
```

**说明**：任一依赖异常时 `status='degraded'`，对应 `checks` 项为错误信息。供 Docker healthcheck 与部署后自检使用。

---

## 14. 状态流转 ↔ 接口对照

把《需求规格》§9 的状态机翻译成「哪个接口触发哪条边」，便于开发时逐条比对。

| 状态流转 | 触发接口 | 权限 |
| --- | --- | --- |
| `IMPORTED → ANALYZING` | （AI Worker 内部消费队列） | Worker |
| `ANALYZING → ANALYZED` | （AI Worker 写回） | Worker |
| `ANALYZED → TASK_CREATED` | （硬规则合并后自动建任务） | 服务端 |
| `PENDING → QUEUED` | （自动入队） | 服务端 |
| `PENDING/QUEUED → WAITING_REVIEW` | （异常判定命中，自动） | 服务端 |
| `WAITING_REVIEW → QUEUED` | `POST /tasks/{id}/review` `{result:APPROVED}` | 管理员 |
| `WAITING_REVIEW → CANCELLED` | `POST /tasks/{id}/review` `{result:REJECTED}` | 管理员 |
| `QUEUED → RUNNING` | `POST /rpa/tasks/claim` | Worker |
| `RUNNING → SUCCESS` | `POST /rpa/tasks/{id}/result` `{success:true}` | Worker |
| `RUNNING → QUEUED`（重试） | `POST /rpa/tasks/{id}/result` `{success:false}` | Worker |
| `RUNNING → FAILED`（超限） | `POST /rpa/tasks/{id}/result` `{success:false}` | Worker |
| `RUNNING → QUEUED`（僵尸回收） | （定时任务扫描 `heartbeat_at`） | 服务端 |
| `FAILED → QUEUED` | `POST /tasks/{id}/retry` | 管理员 |
| `PENDING/QUEUED/WAITING_REVIEW → CANCELLED` | `POST /tasks/{id}/cancel` | 管理员 |

**所有状态变更必须走这张表。**「直接改数据库」是 v1 之后最常见的自伤方式——状态机一旦被绕过，`task_executions` 的记录就对不上了。

---

## 15. 待确认事项

- [ ] 重试是否需要退避延迟（v1 立即重试，已记入已知局限）
- [x] 长轮询 `wait_seconds` 的实现方式 → Redis `BZPOPMIN` 阻塞出队（§10.1）
- [x] 失败截图的访问权限 → 不放静态目录，走管理员 JWT 的取图接口（§9.7）
- [ ] 是否需要「批量重试」接口（后台勾选多条失败任务一次重试）
- [ ] 订单搜索是否需要全文检索（v1 只做精确/前缀匹配）
