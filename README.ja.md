# Hermes Collab Engine

<p align="center">
  <a href="README.md">简体中文</a> |
  <a href="README.en.md">English</a> |
  <a href="README.ja.md">日本語</a>
</p>

> マルチエージェント向けの協調実行エンジン：タスクの複雑さを自動判定し、WBSに分解、マルチ実行器による並行分配、Claude Code / Codex / OpenCode など多様な Worker に対応、タイムアウト時の自動分割リトライ、SQLite 永続化、Skill 分配、MCP ツール管理をサポートし、ビジュアル日本語管理パネルを提供します。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](#環境要件)
[![SQLite](https://img.shields.io/badge/SQLite-永続化-green)](#永続化)
[![Hermes](https://img.shields.io/badge/Hermes-Agent-purple)](#実行パイプライン)
[![Multi-Agent](https://img.shields.io/badge/Worker-Claude%20Code%20%7C%20Codex%20%7C%20OpenCode-orange)](#agent-backend)

## ワンクリックデプロイ

```bash
curl -fsSL https://raw.githubusercontent.com/lpc0387/hermes-collab-engine/main/scripts/install.sh | bash
```

デプロイ完了後に起動：

```bash
opc
```

`opc` が以下をガイドします：

1. ローカルの Claude/Hermes 設定の自動読み取り、または BaseURL、API Key、モデルリストの手動入力の選択；
2. Leader Agent モデルの選択；
3. Worker Agent モデルの選択；
4. Worker Agent タイプの選択（Claude Code / Codex / OpenCode / カスタム）；
5. 管理パネルのリスンアドレス、ポート、デフォルト作業ディレクトリの選択；
6. 協調エンジン管理パネルの起動；
7. 操作方法の選択：Web パネル内のタスク入力ウィンドウを使用、または公式 Hermes コマンドラインに入る。

選択した操作方法を終了すると、`opc` は今回起動した管理パネルサービスを停止します。Web パネルにはタスク入力ウィンドウが内蔵されているため、デフォルトでは Web パネル操作をお勧めします。ターミナルでの対話が必要な場合に Hermes コマンドラインを選択してください。

## 解決する問題

単一の Agent が大規模なタスクを処理する際によく直面する課題：

- タスクの境界が不明確で、分解すべきかどうか判断できない；
- すべての作業が直列実行され、効率が低い；
- 長時間タスクのタイムアウト後、ブレークポイントやリトライ戦略がない；
- 複数の Worker の状態が可視化できない；
- 実行履歴、失敗原因、経験が蓄積されない；
- ログ、実行状態、タスクグラフを一元表示する統合パネルがない；
- 異なるコーディング Agent を統一的にスケジューリングできない；
- Worker にドメインスキルやツールのガイダンスがない。

Hermes Collab Engine はタスク実行を「プランニング層」と「実行層」に分離します：

- **Leader Agent** は複雑度判定、WBS 分解、Skill 分配、ツール割り当て、結果集約を担当；
- **Worker Agent** は具体的な WBS ノードを実行し、必要に応じて Skill と MCP ツールをロード；
- **Agent Backend** は異なるコーディング Agent の呼び出しと出力解析を抽象化；
- **SQLite** は実行状態、ノード、実行器、ログ、コンテキストスナップショット、学習経験を記録；
- **管理パネル** は実行状態と協調ワークフローをリアルタイムでビジュアル表示。

## 実行パイプライン

```text
ユーザー
  ↓
Hermes 親エージェント
  ├─ オプション：delegate_task 事前分析
  ↓ terminal ツール
Hermes Collab Engine
  ├─ Leader：複雑度スコアリング / WBS / Skill 分配 / ツール割り当て / 能動的分割
  ├─ Scheduler：ストリーミングスケジューリング / 介入制御
  ├─ Agent Backend：Claude Code / Codex / OpenCode / カスタム
  ├─ Skill Registry：ノードタイプに応じて Worker prompt に注入
  ├─ MCP Tool Manager：ツール特性に基づいて Worker に推奨
  ├─ SQLite：実行 / ノード / ログ / スナップショット / scoped lessons
  └─ Watchdog：タイムアウト分割
      ↓
Worker Agent 1..N（異なる Agent タイプを混在可能）
  ↓ デュアルトラック出力（マシン結果 + ヒューマンデリバラブル）
集約結果
  ↓
ユーザーに返却
```

## Agent Backend

v4.0 ではプラグイン可能な Agent Backend システムを導入し、ハードコードされた Worker 呼び出しを置き換えました。各 Backend は特定のコーディング Agent の呼び出し方法と出力解析を定義します。

### 内蔵 Backend

| Backend | コマンド | 出力解析 | 適用シナリオ |
|---|---|---|---|
| `claude-code` | `claude` | JSON envelope | 汎用コーディング（デフォルト） |
| `codex` | `codex` | JSON envelope | OpenAI Codex コーディング |
| `opencode` | `opencode` | プレーンテキスト | ライトウェイトコーディング |

### Worker Agent の選択

```bash
# デフォルト Claude Code
hermes-collab run "task"

# Codex を使用
hermes-collab run "task" --agent codex

# 利用可能な Agent を確認
hermes-collab agents
hermes-collab agents --available
```

### カスタム Agent

```python
from hermes_collab_engine.agents import register_backend, AgentBackend

register_backend(AgentBackend(
    name="my-agent",
    display_name="My Custom Agent",
    command=["my-agent-cli"],
    prompt_flag="--prompt",
    output_parser="raw_text",
    ...
))
```

### API

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/agents` | 利用可能な Agent Backend 一覧 |

## コア機能

| 機能 | 説明 |
|---|---|
| 複雑度判定 | ドメイン、ステップ数、曖昧さ、結合度、リスクに基づいてタスクの複雑度を計算 |
| WBS 分解 | 複雑なタスクを自動的に実行可能な作業分解ノードに分割 |
| Agent Backend | プラグイン可能な Worker Agent：Claude Code / Codex / OpenCode / カスタム |
| 並行分配 | 依存関係が満たされたノードを選択した Agent に並行して割り当て |
| タイムアウトガード | Worker タイムアウト後に自動的に分割とリトライのプロセスに入る |
| シャードリトライ | タイムアウトノードを範囲、証拠、実装、リスクなどのフォーカスシャードに分割 |
| 結果集約 | 親タスクとシャード結果を集約し、成功、失敗、タイムアウトを正確に報告 |
| SQLite 永続化 | 実際の SQLite ファイルを使用して実行履歴、ノード結果、コンテキストスナップショットを保存 |
| コンテキストスナップショット | ノード完了時と圧縮前にコンテキストを自動保存、圧縮後のフォーカス回復をサポート |
| 自己学習経験 | タイムアウト、低速タスク、失敗から経験を蓄積し、以降のプランニングに活用 |
| 管理パネル | 日本語 Web パネルで実行記録、ログ、実行器、経験を表示 |
| Leader 駆動スコアリング | Leader Agent が複雑度スコアリングを担当し、ドメイン、ステップ、曖昧さ、結合度、リスクに基づいて実行戦略を決定 |
| セマンティック圧縮分解 | Planner は共有 brief とノード brief を出力し、大きなタスクを Worker が実行可能な最小コンテキストに圧縮 |
| デュアルトラック出力 | Worker はマシン解析可能な結果とヒューマンリーダブルなデリバラブルを同時に生成し、スケジューリング、パネル、最終レポートに活用 |
| 段階的上流コンテキスト | Worker prompt に parent、grandparent、完了済み依存結果を自動的に含め、シャードの系譜を追跡可能に維持 |
| ストリーミングスケジューリング | スケジューラは依存関係が満たされ、空きスロットがある場合に即座にノードを配布し、固定バッチバリアによる下流の遅延を回避 |
| 能動的分割 | タイムアウトや高リスクが予想されるノードを実行前にフォーカスシャードに分割可能、タイムアウト後の対応ではなく事前対策 |
| 親による介入 | Parent / Operator は CLI を通じて実行中のノードに対してログ記録、kill、split、skip が可能、監査ログに記録 |
| 経験スコープ | lessons には global、project、run、node、wbs-family スコープがあり、局所的な経験によるグローバルプランニングの汚染を防止 |
| 環境変数モデル | CLI は HERMES_COLLAB_MODEL、HERMES_COLLAB_LEADER_MODEL、HERMES_COLLAB_WORKER_MODEL、ANTHROPIC_MODEL をモデルフォールバックとしてサポート |

## 自己アップグレード同期ポリシー

AI / 協調エンジンの安定ルール変更はバックアップと移行能力のために GitHub に同期する必要があります。コミット時には allowlist 最小コミット戦略を採用し、シークレット、profiles、settings、実行データベース、ログ、セッション記録のコミットを禁止します。詳細は [AI / 協調エンジン自己アップグレード同期ポリシー](docs/self-upgrade-policy.md) を参照してください。

## 環境要件

- Linux / macOS / WSL
- Python 3.11+
- Git
- 少なくとも1つの Worker Agent：Claude Code CLI (`claude`)、Codex CLI (`codex`) または OpenCode (`opencode`)
- 公式 Hermes Agent：`hermes`

Node.js の依存関係は不要、npm install も不要です。

## ランチャー

```bash
opc
```

ランチャーは2つの API 設定方法をサポートしています：

### ローカル設定の自動読み取り

以下を読み取ります：

```text
~/.claude/settings.json
~/.claude/profiles/*.json
```

Claude Code / Hermes が既に設定されているサーバーに適しています。

### 手動設定

以下の入力を求められます：

- BaseURL
- API Key / Auth Token
- 利用可能なモデルリスト（複数モデルはカンマ区切り）

新しいサーバーや、ローカル設定の読み取りを希望しないシナリオに適しています。

## モデル選択

起動時にそれぞれ選択します：

### Leader Agent モデル

用途：

- 複雑度判定；
- WBS 分解；
- 結果集約；
- Hermes コマンドラインに入る際の Hermes デフォルトモデル。

### Worker Agent モデル

用途：

- Worker Agent の実行；
- WBS ノードの処理；
- タイムアウト後のシャードリトライ。

## コマンドライン使用方法

### タスクを1回実行

```bash
hermes-collab run "分析当前项目结构并给出改进建议" --cwd . --json
```

### 並行数とタイムアウト戦略の指定

```bash
hermes-collab run "实现一个协同任务" \
  --cwd . \
  --agent claude-code \
  --concurrency 4 \
  --timeout 900 \
  --max-retries 2 \
  --split-count 4 \
  --json
```

### タスクファイルの使用

```bash
hermes-collab run --request-file request.md --cwd . --json
```

### 管理パネルの起動

```bash
hermes-collab server --host 0.0.0.0 --port 8765 --cwd . --agent claude-code
```

アクセス：

```text
http://サーバーIP:8765
```

### Agent Backend の確認

```bash
hermes-collab agents                # 登録済みすべて
hermes-collab agents --available    # PATH 上で利用可能なもののみ
hermes-collab agents --available --json
```

### ステータス確認

```bash
hermes-collab status --json
```

### 経験の管理

スコープ付き経験の書き込み：

```bash
hermes-collab lesson add \
  --scope project \
  --category planning \
  --lesson "类似任务优先拆成分析、实现、验证三段" \
  --source hermes-delegate-task \
  --evidence-json '{"run_id":"run_xxx"}'
```

経験の確認：

```bash
hermes-collab lesson list --scope project --json
```

サポートされる scope：`global`、`project`、`run`、`node`、`wbs-family`。

### コンテキストスナップショット

コンテキスト圧縮前の手動保存：

```bash
hermes-collab save-snapshot <run_id> \
  --type pre_compaction \
  --decisions '["chose X over Y"]' \
  --user-instructions '["prefer concise responses"]'
```

スナップショットの確認：

```bash
hermes-collab context-snapshot <run_id> --latest
hermes-collab context-snapshot <run_id> --type pre_compaction
```

### 実行中の介入

Parent / Operator は CLI を通じて実行中のノードに対して制御された介入を行えます：

```bash
hermes-collab parent-log --run-id run_xxx --message "人工确认继续执行" --json
hermes-collab split-node --node-id wbs-1 --split-count 4 --reason "范围过大，主动拆分" --json
hermes-collab skip-node --node-id wbs-2 --reason "上游已确认无需执行" --json
hermes-collab kill-node --node-id wbs-3 --signal TERM --reason "执行方向错误，停止重试" --json
```

すべての介入はログに記録され、最終レポートでは人的介入、スキップ、キャンセル、強制推進などの事実を開示する必要があります。

## 管理パネル

管理パネルが提供する機能：

- 総実行回数；
- 実行中の数；
- 実行中の実行器の数；
- 学習経験の数；
- 実行記録リスト；
- 実行詳細；
- リアルタイムログ；
- 自己学習経験；
- オンラインでの協調タスク提出；
- SSE リアルタイム更新。

## API

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/overview` | 概要指標 |
| GET | `/api/runs` | 実行記録 |
| GET | `/api/runs/:id` | 単一実行の詳細 |
| GET | `/api/logs` | 最近のログ |
| GET | `/api/lessons` | 自己学習経験 |
| GET | `/api/agents` | 利用可能な Agent Backend |
| GET | `/api/events` | リアルタイムイベントストリーム |
| POST | `/api/runs` | 非同期タスク提出 |

## 永続化

デフォルトデータベース：

```text
data/collab.sqlite3
```

データテーブル：

| テーブル | 用途 |
|---|---|
| `runs` | トップレベルタスクの実行記録（agent タイプ含む） |
| `wbs_nodes` | WBS ノード、依存関係、状態、結果、コンテキスト |
| `workers` | 実行器ライフサイクル、セッション ID、所要時間、エラー |
| `logs` | 構造化ログ |
| `lessons` | 自己学習経験（scope 含む） |
| `metrics` | 拡張指標 |
| `node_results` | Worker 結果テキストと構造化出力 |
| `run_state` | 実行時の一時停止とチェックポイント状態 |
| `context_snapshots` | コンテキストスナップショット（圧縮/再起動対策） |

`lessons` には明確なスコープがあります：`global` と `project` は以降のプランニングで再利用可能；`run`、`node`、`wbs-family` は対応する実行、ノード、または同一 WBS ファミリにのみ使用され、局所的な経験がグローバルルールとして誤用されるのを防ぎます。

## タイムアウト分割戦略

デフォルトパラメータ：

```text
--timeout 900
--max-retries 2
--split-count 4
```

Worker がタイムアウトした場合、システムはタスクを直接終了せず、そのノードをより小さなシャードに分割します：

| シャード | 目的 |
|---|---|
| 範囲シャード | 最小の関連範囲とエントリポイントを特定 |
| 証拠シャード | ファイル、コマンド、シンボル、証拠を収集 |
| 実装シャード | 最小実装またはパッチ戦略を生成 |
| リスクシャード | ブロッカー、未知項目、検証要件を特定 |

シャードは Worker に再分配され、最終的に統合集約されます。

## Agent 通信プロトコル

本プロジェクトは ACP-Collab v0.3 を使用して、Hermes 親エージェント、協調エンジン Leader、Worker、および外部事前分析層間の通信境界を規定します。完全なプロトコルは [Agent Communication Protocol](docs/agent-communication-protocol.md) を参照してください。

コア規約：

- Request チャネル：Parent は CLI/API を通じて自己完結型リクエストを提出、Worker は親セッション履歴の読み取りを前提としない；
- Dual-Track Result チャネル：Worker 出力にはマシン解析可能な結果とヒューマンリーダブルなデリバラブルの両方が含まれる；
- Upstream-Context チャネル：Engine は parent、grandparent、完了済み依存結果を下流 Worker に注入；
- Scoped Lessons チャネル：経験には source と scope が必須、Planner は適用範囲内の経験のみ再利用；
- Dispatch-Control チャネル：ストリーミングスケジューリング、能動的分割、実行中介入はすべて CLI/API と SQLite 状態マシンを通じて記録；
- Observability チャネル：runs、wbs_nodes、workers、logs、lessons はパネルと Parent の統一観測パス；
- Agent-Backend チャネル：プラグイン可能な Worker Agent は統一インターフェースで呼び出し、実行時に選択と登録が可能；
- Skill-Distribution チャネル：Leader はノードタイプに基づいて Skill ライブラリからスキルを選択し Worker prompt に注入；
- MCP-Tool チャネル：Leader はツール特性に基づいて Worker に特定の MCP ツールセットの使用を推奨。

## Hermes との統合

インストールスクリプトは以下を作成します：

```text
~/.local/bin/hermes-collab
~/.local/bin/opc
```

オプションの統合スクリプト：

```bash
~/hermes-collab-engine/scripts/install-hermes-integration.sh
```

Hermes 向けに以下を書き込みます：

- ローカル Skill；
- Memory；
- SOUL 動作プロンプト；

Hermes は以下を認識します：実装、分析、デバッグ、監査、リサーチ、プランニング、マルチステップタスクに遭遇した場合、デフォルトで協調エンジンを使用します。

## セキュリティ境界

- 実行データベースのアップロードやコミットを行わない；
- `.runtime-config.json` をコミットしない；
- API Key をコミットしない；
- ユーザーの業務プロジェクトを変更しない；
- Worker の実際の動作は選択された Agent CLI により実行されるため、必要に応じて権限ポリシーと作業ディレクトリを設定してください。

## 開発構造

```text
hermes-collab-engine/
├── hermes-collab
├── start.sh
├── start.py
├── scripts/
│   ├── install.sh
│   └── install-hermes-integration.sh
├── src/hermes_collab_engine/
│   ├── agents.py
│   ├── cli.py
│   ├── engine.py
│   ├── models.py
│   ├── planner.py
│   ├── server.py
│   └── store.py
├── web/
│   └── index.html
├── docs/
│   ├── agent-communication-protocol.md
│   └── self-upgrade-policy.md
├── examples/
│   └── im-bridge-request.md
└── data/
    └── .gitkeep
```

## ライセンス

MIT
