#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
sandbox - 启动 Hermes 协同引擎沙盒演示

用法:
  sandbox                    # 默认 2 小时，端口 8876
  sandbox 4                  # 运行 4 小时
  sandbox --port 8877        # 自定义端口
  sandbox --hours 8          # 8 小时
  sandbox --real             # 启用真实 worker 执行

沙盒特性:
  - 独立数据库 (data/demo_sandbox.sqlite3)
  - 独立工作区 (data/sandbox_workspace/)
  - 独立端口 (默认 8876，生产 8765)
  - 预置演示数据
  - 会话链、Agent 管理、Skill/MCP 管理等完整 Web UI
  - 超时自动停止
EOF
  exit 0
fi
cd /root/hermes-collab-engine
exec ./scripts/start_sandbox.sh "$@"
