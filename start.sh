#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
opc - 启动 Hermes 协同引擎

用法:
  opc

流程:
  1. 选择自动读取本机 Claude/Hermes 配置，或手动填写 BaseURL、API Key 和模型列表
  2. 选择 Leader Agent（Hermes 命令行 / 规划与聚合大脑）模型
  3. 选择 Worker Agent（Claude Code 执行器大脑）模型
  4. 启动协同引擎管理面板
  5. 自动进入官方 Hermes 命令行
  6. 退出 Hermes 后自动停止本次启动的管理面板
EOF
  exit 0
fi
cd /root/hermes-collab-engine
exec python3 start.py
