#!/usr/bin/env python3
"""订单导入压测 —— 验证「100 单/分钟下导入不被 AI 分析阻塞」。

用法：
    <PYTHON_BIN> scripts/loadtest_import.py --total 300 --rate 100
    <PYTHON_BIN> scripts/loadtest_import.py --total 300 --burst      # 一口气全发，看队列堆积
    <PYTHON_BIN> scripts/loadtest_import.py --help

## 它证明什么（《需求规格》§4.2 / §12）

规格里 AI 分析刻意做成异步，唯一理由是：**导入接口不能同步调 LLM** ——
否则 100 单/分钟下每一单都要等模型几秒，导入直接被拖死。这个脚本用真流量
把这句话量出来：

  导入 300 单 → 导入请求的延迟始终是「亚秒级」（因为没碰 LLM）
              → 订单进 AI 队列（Redis ZSET）堆起来
              → 打桩 Worker 在后台慢慢排空（AI 侧慢，但不影响导入）

## 假 LLM 打桩（不花 API 钱）

脚本在**进程内**起一个 OpenAI 兼容的假 LLM（stdlib `http.server`，**零新依赖**），
再把它的地址用环境变量塞给一个自己拉起来的 workers 子进程
（`AI_BASE_URL` 覆盖 `.env`，pydantic-settings 里真环境变量优先）。
于是 AI 分析链路整条跑真：入队 → 出队 → 调「LLM」→ 写库 → 推进状态，
只是最后一步打到一个固定返回合法 JSON 的桩上。`--stub-latency-ms` 模拟真实
模型延迟，让 AI 侧的慢可见、可复现，且**不产生任何费用**。

## 数据隔离与幂等

压测订单一律用 `--prefix`（默认 `LT`）开头，与演示用的 7 单区分开。
脚本结束时打印**按前缀清库**的 SQL（清理方式见《部署说明》§8 与《压力测试》文档）。
单号在本轮内由生成器保证唯一（一次生成、再切片，不逐批现造），所以可反复重跑。

## 为什么是独立脚本、不并进 pytest

和 `scripts/eval_prompt.sh` 同一条理由：它要起进程、要几十秒到几分钟、
结果受机器负载影响 —— 不该拖累「改一行就跑一遍」的回归。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# scripts/ 不在包路径里 —— 与 seed_*.py 一样把 backend/ 挂上去，才能用 settings 读 .env。
ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
MOCK_PLATFORM_DIR = ROOT_DIR / "mock" / "platform"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(MOCK_PLATFORM_DIR))

import httpx  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402

import generate_orders  # noqa: E402
from app.core.config import settings  # noqa: E402

#: 桩返回的分析结果。字段与 `ai.classifier.order_classifier.AnalysisResult` 一一对应，
#: 且取值都在枚举白名单内 —— 否则会被 classifier 降级（不影响流程，但就不是「正常路径」了）。
_STUB_ANALYSIS = json.dumps(
    {
        "priority": "MEDIUM",
        "deadline": "NONE",
        "need_contact": False,
        "risk_level": "LOW",
        "risk_reason": "",
        "action": "正常安排出库",
    },
    ensure_ascii=False,
)

#: 轮询订单状态计数时关注的状态（够画出「导入 → 分析 → 建任务」的推进）。
_TRACKED_STATUSES = ("IMPORTED", "ANALYZING", "ANALYZED", "TASK_CREATED")


# --------------------------------------------------------------------------- #
# 假 LLM
# --------------------------------------------------------------------------- #
class _StubState:
    """桩的共享状态：命中计数 + 人为延迟。多线程处理器靠它统计。"""

    def __init__(self, latency_ms: int) -> None:
        self.latency = latency_ms / 1000.0
        self.hits = 0
        self._lock = threading.Lock()

    def record_hit(self) -> None:
        with self._lock:
            self.hits += 1


class _StubHandler(BaseHTTPRequestHandler):
    #: 由 start_stub() 在实例化前塞进来。
    state: _StubState

    def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的命名约定
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # 读掉请求体，避免连接挂住
        self.state.record_hit()
        if self.state.latency:
            time.sleep(self.state.latency)

        body = json.dumps(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "stub-llm",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": _STUB_ANALYSIS},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
            }
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音默认的 stderr 访问日志
        return


def start_stub(latency_ms: int) -> tuple[ThreadingHTTPServer, _StubState, str]:
    """起假 LLM，返回 (server, state, base_url)。

    端口用 0 让内核分配，避免和别的服务抢固定端口。
    """
    state = _StubState(latency_ms)

    class _Handler(_StubHandler):
        pass

    _Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return server, state, f"http://127.0.0.1:{port}/v1"


# --------------------------------------------------------------------------- #
# workers 子进程
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _ensure_no_running_worker() -> None:
    """有别的 workers 在跑就直接退出 —— 两个 Worker 会抢同一个队列，
    压测数字就没意义了（而且成对的分析/推进也会互相打架）。"""
    pid_file = ROOT_DIR / "logs" / "workers.pid"
    if not pid_file.is_file():
        return
    raw = pid_file.read_text().strip()
    if raw.isdigit() and _pid_alive(int(raw)):
        sys.exit(
            f"错误：已有作业进程在跑（pid {raw}）。先 `./scripts/stop.sh` 停掉再压测。"
        )


def start_workers(stub_base_url: str, concurrency: int) -> tuple[subprocess.Popen, Path]:
    """拉起一个指向假 LLM 的 workers 子进程，等它打印启动行。"""
    logs_dir = ROOT_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"loadtest_workers_{datetime.now():%Y%m%d_%H%M%S}.log"

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": ".",  # 与 start_workers.sh 一致：cwd=backend，从 app.* 起 import
            "AI_BASE_URL": stub_base_url,  # 真环境变量覆盖 .env → 指到桩
            "AI_API_KEY": "stub-key",
            "AI_WORKER_CONCURRENCY": str(concurrency),
            # 本机若开了系统级 HTTP 代理（macOS「网络→代理」），httpx 默认 trust_env
            # 会把对 localhost 的桩调用也甩给代理，代理转发 localhost 会 502。
            # 只排除本机回环地址，外网（真 LLM）仍照走代理。
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )

    fh = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.workers.main"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
    )

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            fh.flush()
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            sys.exit(f"错误：workers 子进程启动即退出。日志末尾：\n{tail}")
        if "后台作业进程启动" in log_path.read_text(encoding="utf-8", errors="replace"):
            return proc, log_path
        time.sleep(0.3)

    proc.terminate()
    sys.exit(f"错误：30 秒内没看到 workers 启动行，检查日志：{log_path}")


def stop_workers(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# --------------------------------------------------------------------------- #
# HTTP 侧
# --------------------------------------------------------------------------- #
async def login(client: httpx.AsyncClient) -> str:
    username = settings.admin_username.strip()
    password = settings.admin_password
    if not username or not password:
        sys.exit("错误：.env 里的 ADMIN_USERNAME / ADMIN_PASSWORD 没配好，无法登录主平台。")
    resp = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    body = resp.json()
    if resp.status_code != 200 or body.get("code") != 0:
        sys.exit(f"错误：登录失败 HTTP {resp.status_code} {resp.text[:200]}")
    return body["data"]["access_token"]


def _orders_to_csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(generate_orders.HEADERS)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


async def import_batch(
    client: httpx.AsyncClient, token: str, rows: list[list[str]], filename: str
) -> dict:
    content = _orders_to_csv(rows)
    started = time.perf_counter()
    try:
        resp = await client.post(
            "/api/v1/orders/import",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (filename, content, "text/csv")},
            data={"dry_run": "false"},
            timeout=180,
        )
    except httpx.HTTPError as exc:
        return {"filename": filename, "orders": len(rows), "ms": None, "ok": 0,
                "failed": len(rows), "error": f"{type(exc).__name__}: {exc}"}

    ms = (time.perf_counter() - started) * 1000
    try:
        body = resp.json()
    except ValueError:
        return {"filename": filename, "orders": len(rows), "ms": ms, "ok": 0,
                "failed": len(rows), "error": f"非 JSON 响应：{resp.text[:200]}"}

    if resp.status_code != 200 or body.get("code") != 0:
        return {"filename": filename, "orders": len(rows), "ms": ms, "ok": 0,
                "failed": len(rows), "error": f"HTTP {resp.status_code} {body.get('message')}"}

    data = body["data"]
    return {
        "filename": filename,
        "orders": data.get("total_rows", len(rows)),
        "ms": ms,
        "ok": data.get("success_rows", 0),
        "failed": data.get("failed_rows", 0),
        "error": None,
    }


async def _count_status(client: httpx.AsyncClient, token: str, status: str) -> int | None:
    try:
        resp = await client.get(
            "/api/v1/orders",
            headers={"Authorization": f"Bearer {token}"},
            params={"status": status, "page_size": 1},
            timeout=15,
        )
        return resp.json()["data"]["total"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None


async def sample_loop(
    client: httpx.AsyncClient,
    token: str,
    redis: "aioredis.Redis",
    stop: asyncio.Event,
    out: list[dict],
    interval: float,
    t0: float,
) -> None:
    """后台采样：AI 队列深度 + 几个订单状态计数，画一条时间序列。"""
    while not stop.is_set():
        try:
            queue_depth = int(await redis.zcard(settings.ai_queue_key))
        except Exception:
            queue_depth = None

        counts = {}
        for status in _TRACKED_STATUSES:
            counts[status] = await _count_status(client, token, status)

        out.append({"t": round(time.monotonic() - t0, 1), "ai_queue": queue_depth, **counts})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[k]


async def run(args: argparse.Namespace) -> dict:
    base_url = args.base_url.rstrip("/")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = Path(args.out) if args.out else ROOT_DIR / "backend" / "var" / f"loadtest_report_{ts}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    stub_server = stub_state = None
    stub_base_url = "（未启用桩）"
    if not args.skip_worker:
        stub_server, stub_state, stub_base_url = start_stub(args.stub_latency_ms)
        print(f"假 LLM 已就绪：{stub_base_url}（延迟 {args.stub_latency_ms}ms）")

    worker_proc = None
    worker_log = None
    if not args.skip_worker:
        _ensure_no_running_worker()
        worker_proc, worker_log = start_workers(stub_base_url, args.ai_worker_concurrency)
        print(f"打桩 Worker 已就绪：pid={worker_proc.pid} 日志={worker_log}")

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    samples: list[dict] = []

    try:
        # trust_env=False：本机可能开着系统 HTTP 代理（macOS 网络代理），
        # 默认会让 httpx 把打向 127.0.0.1 主平台的请求也交给代理 —— 直接 502。
        # 压测打的就是本机主平台，显式不走代理。
        async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
            token = await login(client)
            print(f"已登录 admin，开始造数：总 {args.total} 单，前缀 {args.prefix}")

            # 一次生成全部行再切片 —— 单号在本轮内唯一，跨批次不会撞号（撞号会让整批零插入）。
            rng = random.Random(args.seed)
            all_rows = generate_orders._rows(args.total, args.prefix, rng)
            chunks = [
                all_rows[i : i + args.batch_size]
                for i in range(0, len(all_rows), args.batch_size)
            ]

            t0 = time.monotonic()
            stop = asyncio.Event()
            sampler = asyncio.create_task(
                sample_loop(client, token, redis, stop, samples, args.sample_interval, t0)
            )

            sem = asyncio.Semaphore(args.concurrency)

            async def bounded(idx: int, chunk: list[list[str]]) -> dict:
                async with sem:
                    return await import_batch(
                        client, token, chunk, f"loadtest_{idx:03d}.csv"
                    )

            # 逐批派发；burst 时不等间隔，否则按速率拉开。
            interval = 0.0 if args.burst else (60.0 / args.rate) * args.batch_size
            tasks = []
            next_send = time.monotonic()
            for idx, chunk in enumerate(chunks):
                if interval:
                    now = time.monotonic()
                    if now < next_send:
                        await asyncio.sleep(next_send - now)
                    next_send += interval
                tasks.append(asyncio.create_task(bounded(idx, chunk)))

            per_request = await asyncio.gather(*tasks)
            import_wall = time.monotonic() - t0

            # 排空观察：等 AI 队列清空（或超时）。
            # 记下「开始排空时队列里还剩多少」——排空速率要拿它算，不能拿导入总数
            # （导入期间 Worker 是边进边出，导入结束时队列往往已接近空）。
            drain_seconds = None
            pending_at_drain = None
            if not args.skip_worker:
                try:
                    pending_at_drain = int(await redis.zcard(settings.ai_queue_key))
                except Exception:
                    pending_at_drain = None
                drain_started = time.monotonic()
                while time.monotonic() - drain_started < args.drain_timeout:
                    try:
                        if int(await redis.zcard(settings.ai_queue_key)) == 0:
                            drain_seconds = time.monotonic() - drain_started
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(1)

            stop.set()
            await sampler

        ok = sum(r["ok"] for r in per_request)
        failed = sum(r["failed"] for r in per_request)
        latencies = [r["ms"] for r in per_request if r["ms"] is not None]
        queue_depths = [s["ai_queue"] for s in samples if s.get("ai_queue") is not None]
        peak_queue = max(queue_depths) if queue_depths else 0
        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        p99 = _percentile(latencies, 99)

        report = {
            "started_at": ts,
            "config": {
                "base_url": base_url,
                "total": args.total,
                "rate_per_min": None if args.burst else args.rate,
                "burst": args.burst,
                "batch_size": args.batch_size,
                "concurrency": args.concurrency,
                "stub_latency_ms": None if args.skip_worker else args.stub_latency_ms,
                "ai_worker_concurrency": None if args.skip_worker else args.ai_worker_concurrency,
                "prefix": args.prefix,
            },
            "import": {
                "orders": sum(r["orders"] for r in per_request),
                "requests": len(per_request),
                "ok": ok,
                "failed": failed,
                "wall_seconds": round(import_wall, 2),
                "throughput_orders_per_min": round(ok / import_wall * 60, 1) if import_wall else None,
                "latency_ms": {
                    "p50": round(p50, 1),
                    "p95": round(p95, 1),
                    "p99": round(p99, 1),
                    "max": round(max(latencies), 1) if latencies else None,
                },
                "per_request": per_request,
            },
            "decoupling": {
                "samples": samples,
                "peak_ai_queue": peak_queue,
                # 判定口径：导入 p95 保持亚秒级 => LLM 的慢没有传导到导入接口。
                "import_latency_flat": p95 < 1000,
            },
            "drain": {
                "enabled": not args.skip_worker,
                "pending_at_drain": pending_at_drain,
                "drain_seconds": round(drain_seconds, 1) if drain_seconds is not None else None,
                "drained_orders": pending_at_drain if drain_seconds is not None else None,
                "rate_per_min": (
                    round(pending_at_drain / drain_seconds * 60, 1)
                    if drain_seconds and pending_at_drain
                    else None
                ),
                "stub_hits": stub_state.hits if stub_state else None,
                "timed_out": not args.skip_worker and drain_seconds is None,
            },
            "notes": [
                "AI 侧走假 LLM（OpenAI 兼容桩），不产生 API 费用；延迟由 --stub-latency-ms 模拟。",
                "本压测只覆盖「导入 + AI 队列解耦」，未含 RPA 全链路（单 Worker 串行是已知瓶颈，规格 §13 v1 不做多 Worker）。",
                "本轮创建的任务会停在 task_queue（没有 RPA Worker），属预期。",
                "压测数据用 'LT' 前缀隔离，跑完按脚本打印的 SQL 清理。",
            ],
        }

        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        _print_summary(report, worker_log)
        _print_cleanup(args.prefix, report_path)
        return report
    finally:
        if worker_proc:
            stop_workers(worker_proc)
        if stub_server:
            stub_server.shutdown()
            stub_server.server_close()
        await redis.aclose()


def _print_summary(report: dict, worker_log: Path | None) -> None:
    imp = report["import"]
    dec = report["decoupling"]
    dr = report["drain"]
    print("\n================ 压测结果 ================")
    print(f"导入：{imp['ok']}/{imp['orders']} 单，失败 {imp['failed']}，"
          f"{imp['requests']} 个批次，耗时 {imp['wall_seconds']}s")
    print(f"     吞吐 {imp['throughput_orders_per_min']} 单/分钟")
    print(f"     延迟 p50={imp['latency_ms']['p50']}ms  "
          f"p95={imp['latency_ms']['p95']}ms  "
          f"p99={imp['latency_ms']['p99']}ms  "
          f"max={imp['latency_ms']['max']}ms")
    print(f"解耦：AI 队列峰值 {dec['peak_ai_queue']} 条；"
          f"导入 p95 亚秒级 = {dec['import_latency_flat']}")
    if dr["enabled"]:
        if dr["timed_out"]:
            print(f"排空：超时未清空（还剩 {dr['pending_at_drain']} 条待分析）")
        else:
            print(f"排空：从 {dr['pending_at_drain']} 条起，{dr['drain_seconds']}s 清空"
                  f"（{dr['rate_per_min']} 单/分钟），假 LLM 命中 {dr['stub_hits']} 次")
    else:
        print("排空：未启用 Worker，跳过")
    if worker_log:
        print(f"Worker 日志：{worker_log}")
    print("=========================================\n")


def _print_cleanup(prefix: str, report_path: Path) -> None:
    print("清库（按前缀回滚本次压测数据；orders 删除会级联清 tasks/executions/analyses/review_logs）：")
    print(f"""
  mysql -uroot -e "
    DELETE n FROM ai_rpa.notifications n JOIN ai_rpa.tasks t ON n.related_task_id = t.id
      JOIN ai_rpa.orders o ON t.order_id = o.id WHERE o.order_no LIKE '{prefix}%';
    DELETE FROM ai_rpa.orders WHERE order_no LIKE '{prefix}%';
    DELETE FROM ai_rpa.import_batches WHERE filename LIKE 'loadtest%';"

  # 两个队列也应清空（排空正常的话本来就空）
  redis-cli -n {settings.redis_db} DEL {settings.ai_queue_key} {settings.task_queue_key}
""")
    print(f"报告：{report_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="订单导入压测：验证 100 单/分钟下导入不被 AI 分析阻塞",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--total", type=int, default=300, help="压测订单总数（默认 300）")
    parser.add_argument("--rate", type=float, default=100.0, help="目标速率 单/分钟（默认 100）")
    parser.add_argument("--burst", action="store_true",
                        help="忽略速率，一口气把批次全发出去（看队列堆积）")
    parser.add_argument("--batch-size", type=int, default=50, help="每个批次多少单（默认 50）")
    parser.add_argument("--concurrency", type=int, default=4, help="最多同时几个导入请求（默认 4）")
    parser.add_argument("--prefix", default="LT", help="压测订单号前缀（默认 LT）")
    parser.add_argument("--base-url", default=f"http://{settings.app_host}:{settings.app_port}",
                        help="主平台地址（默认取 .env 的 APP_HOST/APP_PORT）")
    parser.add_argument("--stub-latency-ms", type=int, default=1500,
                        help="假 LLM 延迟，模拟真实模型耗时（默认 1500）")
    parser.add_argument("--ai-worker-concurrency", type=int, default=5,
                        help="打桩 Worker 的并发（默认 5，与线上默认一致）")
    parser.add_argument("--skip-worker", action="store_true",
                        help="不起假 LLM 与 Worker，只量导入与队列堆积（不观察排空）")
    parser.add_argument("--drain-timeout", type=int, default=300, help="等待 AI 队列清空的超时秒数（默认 300）")
    parser.add_argument("--sample-interval", type=float, default=3.0, help="采样间隔秒（默认 3）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（复现数据内容）")
    parser.add_argument("--out", default=None, help="报告路径（默认 backend/var/loadtest_report_<时间>.json）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.total < 1 or args.batch_size < 1:
        print("错误：--total / --batch-size 必须 ≥ 1", file=sys.stderr)
        return 2
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
