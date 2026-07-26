#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/hermes-collab-engine}"
REPO_URL="${REPO_URL:-https://github.com/lpc0387/hermes-collab-engine.git}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="${VENV_DIR:-$INSTALL_DIR/.venv}"

missing=0
need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "缺少依赖: $1" >&2
    missing=1
  fi
}

print_dependency_help() {
  cat >&2 <<'HELP'
请先安装缺失依赖后重试。本脚本不会自动修改系统包管理器。
常见安装命令：
  Debian/Ubuntu: sudo apt-get update && sudo apt-get install -y git curl python3 python3-venv
  Fedora/RHEL:   sudo dnf install -y git curl python3
  macOS:         xcode-select --install 或 brew install git curl python3
HELP
}

write_template_skeletons() {
  mkdir -p \
    "$INSTALL_DIR/data" \
    "$INSTALL_DIR/templates/hermes/skills" \
    "$INSTALL_DIR/templates/hermes/memories" \
    "$INSTALL_DIR/templates/claude"

  : > "$INSTALL_DIR/templates/hermes/skills/.gitkeep"
  : > "$INSTALL_DIR/templates/hermes/memories/.gitkeep"

  if [ ! -f "$INSTALL_DIR/templates/claude/settings.example.json" ]; then
    cat > "$INSTALL_DIR/templates/claude/settings.example.json" <<'JSON'
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.example.com",
    "ANTHROPIC_API_KEY": "replace-with-your-api-key",
    "ANTHROPIC_MODEL": "replace-with-your-model"
  },
  "permissions": {
    "allow": [],
    "deny": []
  }
}
JSON
  fi
}

seed_engine_rules() {
  local skills_dir="$INSTALL_DIR/templates/hermes/skills"
  local memories_dir="$INSTALL_DIR/templates/hermes/memories"

  if [ -f "$skills_dir/leader-engine-discipline.yaml" ]; then
    echo "  ✓ leader-engine-discipline.yaml 已存在"
  else
    cat > "$skills_dir/leader-engine-discipline.yaml" <<'YAML'
name: leader-engine-discipline
description: 我是 leader 不是 worker。必须走引擎调度。禁止越权自己改代码。
version: 1.0.0
rules:
  - All code changes must go through hermes-collab run --agent hermes
  - Do NOT add external timeout wraps around engine commands
  - When engine fails: diagnose DB+logs first, repair engine, never bypass
  - Role: dispatch tasks -> monitor workers -> verify quality. Not write code.
YAML
    echo "  ✓ 创建 leader-engine-discipline.yaml"
  fi

  if [ -f "$memories_dir/leader-role.md" ]; then
    echo "  ✓ leader-role.md 已存在"
  else
    cat > "$memories_dir/leader-role.md" <<'MD'
# Leader Role (注入到所有会话)
- 我是 leader，不是 worker。
- 所有项目代码操作必须通过 hermes-collab run 调度。
- 例外：修复 8765 引擎本身的代码可以直接修改。
- 禁止给 run 加外部 timeout。
- 引擎失败先查根因再行动，不能绕过。
MD
    echo "  ✓ 创建 leader-role.md"
  fi
}

resolve_path() {
  python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$1"
}

reject_unsafe_path() {
  label="$1"
  value="$2"
  resolved="$(resolve_path "$value")"
  home_resolved="$(resolve_path "$HOME")"
  if [ -z "$value" ] || [ "$resolved" = "/" ] || [ "$resolved" = "$home_resolved" ]; then
    echo "不安全的 ${label}: $value" >&2
    exit 1
  fi
  printf '%s\n' "$resolved"
}

write_launcher() {
  target="$1"
  shift
  printf -v quoted_install_dir '%q' "$INSTALL_DIR"
  printf -v quoted_python_bin '%q' "$PYTHON_BIN"
  quoted_args=""
  for arg in "$@"; do
    printf -v quoted_arg '%q' "$arg"
    quoted_args="${quoted_args} ${quoted_arg}"
  done
  cat > "$target" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd $quoted_install_dir
exec $quoted_python_bin$quoted_args "\$@"
EOF
  chmod +x "$target"
}

need_cmd python3
need_cmd git
need_cmd curl

if [ "$missing" -eq 1 ]; then
  print_dependency_help
  exit 1
fi

echo "  ✓ python3 $(python3 --version 2>&1)"
echo ""

# ── Check required CLI agents for engine operation ──
missing_agent=0
need_agent() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "  ❌ $1: 未安装 — $2"
    missing_agent=1
  else
    echo "  ✓ $1: $(command -v "$1")"
  fi
}
echo "=== 检查 Agent CLI ==="
need_agent hermes "Leader Agent（必需）— 参考 https://hermes-agent.nousresearch.com 安装"
need_agent opencode "Worker Agent（必需）— 通过 npm install -g opencode-ai 或参考 opencode.ai 安装"
echo ""

if [ "$missing_agent" -eq 1 ]; then
  echo "请安装缺失的 Agent 后重试。"
  echo "本脚本不会自动安装外部 CLI 工具。"
  exit 1
fi

INSTALL_DIR="$(reject_unsafe_path INSTALL_DIR "$INSTALL_DIR")"
BIN_DIR="$(reject_unsafe_path BIN_DIR "$BIN_DIR")"
VENV_DIR="$(reject_unsafe_path VENV_DIR "$VENV_DIR")"

mkdir -p "$BIN_DIR"

if [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR/.git" ]; then
  echo "安装目录已存在但不是 Git 仓库: $INSTALL_DIR" >&2
  echo "请设置 INSTALL_DIR 到空目录，或手动处理该目录后重试。" >&2
  exit 1
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "==> 更新已有仓库: $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "==> 克隆仓库: $REPO_URL -> $INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

echo "==> 创建空模板目录"
write_template_skeletons

echo "==> 安装 Python 包"
if python3 -m venv "$VENV_DIR" >/dev/null 2>&1; then
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -e "$INSTALL_DIR"
  PYTHON_BIN="$VENV_DIR/bin/python"
else
  echo "无法创建虚拟环境: $VENV_DIR" >&2
  echo "请安装 python3-venv，或手动运行: python3 -m pip install --user -e '$INSTALL_DIR'" >&2
  PYTHON_BIN="$(command -v python3)"
fi

chmod +x "$INSTALL_DIR/start.py"
write_launcher "$BIN_DIR/hermes-collab" -m src.hermes_collab_engine.cli
write_launcher "$BIN_DIR/opc" start.py

echo "==> 构建协议代理 (proxy/)"
PROXY_BINARY="$INSTALL_DIR/proxy/opencode-proxy"
if command -v go >/dev/null 2>&1; then
  echo "  发现 Go，正在编译协议代理..."
  if (cd "$INSTALL_DIR/proxy" && go build -o opencode-proxy ./cmd/server 2>/dev/null); then
    echo "  ✓ Go 代理编译成功: $PROXY_BINARY"
  else
    echo "  ⚠ Go 编译失败，使用 Python 代理 (proxy.py) 作为备用"
  fi
else
  echo "  · 未安装 Go，使用 Python 代理 (proxy.py) 作为备用"
  echo "  如需 Go 代理性能优化，请手动安装 Go 后执行:"
  echo "    cd '$INSTALL_DIR/proxy' && go build -o opencode-proxy ./cmd/server"
fi

echo ""
echo "==> 注入 Leader 纪律 Skill/Memory"
seed_engine_rules

echo ""
echo "==> 总结"
echo "仓库: $INSTALL_DIR"
echo "Python: $PYTHON_BIN"
echo "命令:"
echo "  $BIN_DIR/opc                  # 选择模型并启动面板"
echo "  $BIN_DIR/hermes-collab --help # 引擎 CLI"
echo "  hermes -z \"任务描述\"          # 直接运行（Leader 模式）"
echo "  opencode run \"任务描述\"       # 直接运行（Worker 模式）"
echo ""
echo "启动引擎面板:"
echo "  hermes-collab server --host 0.0.0.0 --port 8765 --cwd '$INSTALL_DIR'"
echo ""
echo "如果 $BIN_DIR 不在 PATH 中，请执行:"
echo "  export PATH=\"$BIN_DIR:\$PATH\""
