#!/usr/bin/env bash
#
# 跑一次 Prompt 评测 —— **真调 LLM，会花钱**
#
# 用法：
#   ./scripts/eval_prompt.sh
#
# 做什么：
#   用 30 条评测样本（含 4 条 prompt 注入）真调一次 DeepSeek，逐字段算准确率，
#   打印报告并把 JSON 写到 backend/var/eval_report.json。
#
# 为什么单独一个脚本、不并进日常 pytest：
#   它慢（要联网）、要钱、结果还会因上游模型波动 —— 不该拖累「改一行就跑一遍」
#   的回归。所以 pytest.ini 默认 `-m "not eval"`，这里是显式覆盖成 `-m eval`。
#
# 前置：
#   .env 里有可用的 AI_API_KEY / AI_BASE_URL / AI_MODEL。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

die() { echo "错误：$*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "找不到 $ENV_FILE，请先执行：cp .env.example .env"

# 只取需要的键，避免把 .env 当脚本执行
value_of() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

PYTHON_BIN="$(value_of PYTHON_BIN)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
[[ -x "$PYTHON_BIN" ]] || die "解释器不存在或不可执行：$PYTHON_BIN
  在 .env 里把 PYTHON_BIN 改成装了依赖的那个绝对路径。"

# 没有 key 就早退 —— 否则 pytest 里会 skip，看起来像「跑过了但什么都没有」。
AI_API_KEY="$(value_of AI_API_KEY)"
[[ -n "$AI_API_KEY" ]] || die "AI_API_KEY 为空。评测要真调模型，请在 .env 里配置后重试。"

cd "$ROOT_DIR"
echo "开始评测（真调 LLM，会产生少量费用）..."
# -s：让 test_prompt_eval.py 里的报告直接打到终端（默认 pytest 会吞掉 print）。
exec "$PYTHON_BIN" -m pytest tests/ai/test_prompt_eval.py -m eval -s
