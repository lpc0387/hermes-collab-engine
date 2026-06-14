#!/usr/bin/env bash
# 一键启动 Hermes 协同引擎沙盒演示
#
# 默认运行 2 小时，超时自动停止；用户可指定运行小时数。
#
# 用法：
#   ./scripts/start_sandbox.sh              # 默认 2 小时
#   ./scripts/start_sandbox.sh 4            # 运行 4 小时
#   ./scripts/start_sandbox.sh 0.5          # 运行 30 分钟
#   ./scripts/start_sandbox.sh --hours 8    # 8 小时
#   ./scripts/start_sandbox.sh --port 8877  # 自定义端口
#   ./scripts/start_sandbox.sh --real       # 隔离数据库/工作区中启用真实 worker（默认最多 5 轮对话）
#   HOURS=3 ./scripts/start_sandbox.sh      # 环境变量

set -euo pipefail

# ---------- 配置默认值 ----------
DEFAULT_HOURS="${HOURS:-2}"
HOURS="$DEFAULT_HOURS"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8876}"
DB="${DB:-data/demo_sandbox.sqlite3}"
RESEED="${RESEED:-1}"  # 1=每次启动重置数据；0=保留
REAL="${HERMES_SANDBOX_REAL_EXECUTION:-0}"
REAL_LIMIT="${HERMES_SANDBOX_REAL_RUN_LIMIT:-5}"
WORKSPACE="${HERMES_SANDBOX_WORKSPACE:-data/sandbox_workspace}"
SANDBOX_MARKER_FILENAME=".hermes-collab-sandbox-workspace"

require_value() {
  if [[ $# -lt 2 || -z "${2:-}" ]]; then
    echo "缺少参数值：$1" >&2
    exit 2
  fi
}

# ---------- 解析参数 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    --hours)   require_value "$@"; HOURS="$2"; shift 2 ;;
    --host)    require_value "$@"; HOST="$2";  shift 2 ;;
    --port)    require_value "$@"; PORT="$2";  shift 2 ;;
    --db)      require_value "$@"; DB="$2";    shift 2 ;;
    --real)    REAL=1; RESEED=0; shift ;;
    --real-limit) require_value "$@"; REAL_LIMIT="$2"; shift 2 ;;
    --workspace) require_value "$@"; WORKSPACE="$2"; shift 2 ;;
    --no-reseed) RESEED=0; shift ;;
    --interactive|-i)
      # 交互式询问运行时长
      read -rp "运行多少小时？ [默认 ${DEFAULT_HOURS}]: " __h
      [[ -n "$__h" ]] && HOURS="$__h"
      shift
      ;;
    [0-9]*)    HOURS="$1"; shift ;;  # 第一个数字参数当作小时数
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

# ---------- 交互式选择运行时长（仅在未通过参数/环境变量指定时）----------
if [[ "$HOURS" == "$DEFAULT_HOURS" && -t 0 ]]; then
  echo "选择沙盒运行时长："
  echo "  1) 4 小时"
  echo "  2) 8 小时"
  echo "  直接回车 → 默认 ${DEFAULT_HOURS} 小时"
  read -rp "请输入选项 [默认 ${DEFAULT_HOURS}h]: " __dur_choice
  case "$__dur_choice" in
    1) HOURS=4 ;;
    2) HOURS=8 ;;
    "") ;;  # keep default
    *) echo "无效选项，使用默认 ${DEFAULT_HOURS} 小时" ;;
  esac
fi

# ---------- 交互式选择真实模式（仅在未通过 --real 参数指定时）----------
if [[ "$REAL" == "0" && -t 0 ]]; then
  echo
  echo "是否启用真实 Token 转发（调用真实 LLM API，消耗实际 token）？"
  echo "  1) 是 — 启用真实模式（${REAL_LIMIT} 轮对话额度，用完自动结束）"
  echo "  直接回车 → 否（演示模式，不消耗 token）"
  read -rp "请输入选项 [默认否]: " __real_choice
  case "$__real_choice" in
    1) REAL=1; RESEED=0; echo "  ✓ 已启用真实模式，额度 ${REAL_LIMIT} 轮" ;;
    "") ;;  # keep demo mode
    *) echo "  无效选项，使用演示模式" ;;
  esac
fi

# ---------- 路径定位 ----------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." &>/dev/null && pwd)"
cd "$REPO_ROOT"
WORKSPACE="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$WORKSPACE")"

# ---------- 校验 ----------
if [[ ! -f sandbox/server.py ]]; then
  echo "✗ 找不到 sandbox/server.py（当前目录：$REPO_ROOT）" >&2
  exit 1
fi
if [[ ! -f scripts/seed_demo_data.py ]]; then
  echo "✗ 找不到 scripts/seed_demo_data.py" >&2
  exit 1
fi
if ! command -v python3 &>/dev/null; then
  echo "✗ 未找到 python3" >&2; exit 1
fi

# 小时数转秒（支持小数）
SECS="$(python3 -c 'import sys
try:
    h = float(sys.argv[1])
except ValueError:
    print(0)
else:
    print(int(h * 3600) if h > 0 else 0)' "$HOURS")"
if [[ "$SECS" -le 0 ]]; then
  echo "✗ 无效的运行小时数：$HOURS（必须 > 0）" >&2; exit 2
fi
if [[ "$SECS" -gt 86400 ]]; then
  echo "⚠ 运行时长超过 24 小时（${HOURS}h），如确认请用环境变量 HERMES_SANDBOX_FORCE=1 跳过该提醒"
  if [[ "${HERMES_SANDBOX_FORCE:-0}" != "1" ]]; then exit 2; fi
fi

if [[ ! "$PORT" =~ ^[0-9]+$ || "$PORT" -lt 1 || "$PORT" -gt 65535 ]]; then
  echo "✗ 无效的端口：$PORT（必须是 1-65535 的整数）" >&2; exit 2
fi
if [[ ! "$REAL_LIMIT" =~ ^[0-9]+$ ]]; then
  echo "✗ 无效的真实任务额度：$REAL_LIMIT（必须是非负整数）" >&2; exit 2
fi
if [[ "$REAL" == "1" ]]; then
  case "$WORKSPACE" in
    "$REPO_ROOT"|"$REPO_ROOT/"|"$HOME"|"$HOME/"|/)
      echo "✗ 拒绝使用受保护目录作为真实沙盒工作区：$WORKSPACE" >&2; exit 2 ;;
  esac
  case "$WORKSPACE" in
    "$REPO_ROOT"/data/sandbox_workspace|"$REPO_ROOT"/data/sandbox_workspace/*) ;;
    *) echo "✗ 真实沙盒工作区必须位于 $REPO_ROOT/data/sandbox_workspace 内：$WORKSPACE" >&2; exit 2 ;;
  esac
fi

# ---------- 端口占用检查 ----------
if command -v lsof &>/dev/null && lsof -iTCP:"$PORT" -sTCP:LISTEN &>/dev/null; then
  echo "✗ 端口 $PORT 已被占用，请用 --port 指定其它端口" >&2
  lsof -iTCP:"$PORT" -sTCP:LISTEN | head -3 >&2
  exit 3
fi

# ---------- 准备数据 ----------
mkdir -p data logs
if [[ "$REAL" == "1" ]]; then
  if [[ "$DB" == "data/demo_sandbox.sqlite3" ]]; then DB="data/sandbox_real.sqlite3"; fi
  echo "▶ 启用真实沙盒执行：独立数据库 $DB，独立工作区 $WORKSPACE，额度 ${REAL_LIMIT} 个任务"
elif [[ "$RESEED" == "1" ]]; then
  echo "▶ 重置脱敏演示数据 → $DB"
  python3 scripts/seed_demo_data.py --db "$DB" --reset
else
  echo "▶ 复用现有数据库 $DB（--no-reseed）"
  if [[ ! -f "$DB" ]]; then
    echo "  ⚠ 数据库不存在，自动播种一次"
    python3 scripts/seed_demo_data.py --db "$DB" --reset
  fi
fi

# ---------- 启动 ----------
LOG_FILE="logs/sandbox-$(date +%Y%m%d-%H%M%S).log"
echo
echo "▶ 启动 Hermes 协同引擎沙盒"
echo "  地址：http://${HOST}:${PORT}/"
echo "  数据：${DB}（脱敏）"
if [[ "$REAL" == "1" ]]; then
  echo "  模式：真实沙盒执行（隔离 DB / 隔离工作区 / 不写生产库）"
  echo "  工作区：${WORKSPACE}"
  echo "  任务额度：${REAL_LIMIT}"
fi
echo "  日志：${LOG_FILE}"
echo "  运行时长：${HOURS} 小时（${SECS} 秒）"
echo "  Ctrl+C 可随时手动停止"
echo

export HERMES_SANDBOX_DB="$DB"
export HERMES_SANDBOX_MOCK_CONFIG="${HERMES_SANDBOX_MOCK_CONFIG:-config/sandbox-mocks.json}"
export HERMES_SANDBOX_REAL_EXECUTION="$REAL"
export HERMES_SANDBOX_REAL_RUN_LIMIT="$REAL_LIMIT"
export HERMES_SANDBOX_WORKSPACE="$WORKSPACE"
export HERMES_SANDBOX_AGGREGATE="${HERMES_SANDBOX_AGGREGATE:-1}"
export HERMES_SANDBOX_TTL_SECONDS="$SECS"

# 后台启动 server，捕获 PID
SERVER_ARGS=(sandbox/server.py --host "$HOST" --port "$PORT" --ttl-seconds "$SECS")
if [[ "$REAL" == "1" ]]; then SERVER_ARGS+=(--real --workspace "$WORKSPACE"); fi
python3 "${SERVER_ARGS[@]}" >"$LOG_FILE" 2>&1 &
SERVER_PID=$!

cleanup() {
  echo
  echo "▶ 正在停止沙盒（PID=$SERVER_PID）..."
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
      sleep 1
    done
    kill -KILL "$SERVER_PID" 2>/dev/null || true
  fi
  echo "✓ 沙盒已停止。日志：$LOG_FILE"
}
trap cleanup INT TERM EXIT

# 等待 server 就绪（最多 8 秒）
for i in 1 2 3 4 5 6 7 8; do
  sleep 1
  if curl -sf "http://${HOST}:${PORT}/" -o /dev/null 2>/dev/null; then
    echo "✓ 沙盒已就绪 (${i}s)"
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "✗ 沙盒进程已退出，请检查日志：$LOG_FILE" >&2
    tail -20 "$LOG_FILE" >&2
    exit 1
  fi
  if [[ $i -eq 8 ]]; then
    echo "⚠ 8 秒内未就绪，但进程仍在运行；将继续等待倒计时" >&2
  fi
done

# ---------- 倒计时（分钟级心跳）----------
END_TS=$(( $(date +%s) + SECS ))
HEART_INTERVAL=300  # 每 5 分钟打一次心跳
NEXT_HEART=$(( $(date +%s) + HEART_INTERVAL ))

while true; do
  NOW=$(date +%s)
  if [[ $NOW -ge $END_TS ]]; then
    echo
    echo "▶ 已达预定运行时长（${HOURS}h），自动停止"
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo
    echo "✗ 沙盒进程意外退出，最后日志："
    tail -10 "$LOG_FILE"
    exit 1
  fi
  if [[ $NOW -ge $NEXT_HEART ]]; then
    REMAIN=$(( END_TS - NOW ))
    printf '  · 心跳 %s · 剩余 %d 分钟 · http://%s:%s/\n' \
      "$(date +%H:%M:%S)" "$((REMAIN/60))" "$HOST" "$PORT"
    NEXT_HEART=$(( NOW + HEART_INTERVAL ))
  fi
  sleep 5
done
