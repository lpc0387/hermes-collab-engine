# Hermes Collab Engine v5.0

[![Release v5.0.0](https://img.shields.io/badge/release-v5.0.0-blue)](CHANGELOG.md) [![Sandbox ready](https://img.shields.io/badge/sandbox-ready-success)](sandbox/README.md) [![License MIT](https://img.shields.io/badge/license-MIT-green)](#ライセンス) [![Security](https://img.shields.io/badge/security-policy-orange)](SECURITY.md)

Hermes Collab Engine v5.0 は Hermes 協調ワークフロー向け **AI multi-agent collaboration engine** の初の正式公開リリースです。Leader が要求を **WBS** ノードへ分解し、Worker が並列実行し、**Claude Code** / **Hermes Agent** / カスタム Agent Backend を同じパイプラインに接続できます。

リアルタイム **dashboard**、隔離された **sandbox**、Leader フィードバック日記、軽量 API、**one-line install** を備え、複雑な開発タスクの分解・配布・監査・要約に使えます。

![ピクセル協調オフィス ダッシュボード](docs/screenshots/dashboard.png)

![Hermes 協調フローデモ](docs/demo/hermes-flow.svg)

## リリースとコミュニティ

このプロジェクトが役立つ場合は、GitHub star で v5.0 リリースラインをフォローしてください。コントリビュート前に [`CONTRIBUTING.md`](CONTRIBUTING.md) を確認し、セキュリティ問題は [`SECURITY.md`](SECURITY.md) から報告し、計画は [`ROADMAP.md`](ROADMAP.md)、変更履歴は [`CHANGELOG.md`](CHANGELOG.md) を参照してください。コミュニティ共有文面は必要に応じて [`docs/launch/v5.0-posts.md`](docs/launch/v5.0-posts.md) を参照できます。

## ワンラインデプロイ

```bash
curl -fsSL https://raw.githubusercontent.com/lpc0387/hermes-collab-engine/main/scripts/install.sh | bash
```

インストーラーは依存関係を確認し、リポジトリを clone/update し、ローカル仮想環境と空のテンプレートディレクトリを作成します。このリポジトリは **ランタイムデータ、秘密情報、実際の Hermes/Claude 設定を同梱しません**。Hermes をローカルエンジンへ接続する場合は、先にテンプレート用インストーラーを確認してください：

```bash
cd ~/hermes-collab-engine
./scripts/install-hermes-integration.sh --dry-run
```

## クイックスタート

```bash
# ランチャー：設定を選択 → Leader/Worker モデルを選択 → dashboard + Hermes CLI
opc

# 手動インストール
python3 -m pip install -e .

# タスクを直接実行
hermes-collab run "現在のプロジェクト構造を分析" --cwd . --json
```

## ハイライト

| 機能 | リリースノート |
|---|---|
| WBS 協調 | Leader がスコアリング、分解、ノード配布を行い、Worker は依存関係に沿って並列実行 |
| Leader/Worker デュアルモデル | 起動時に Leader モデルと Worker モデルを別々に選択し、dashboard に現在のモデルを表示 |
| 実 sandbox 実行 | `scripts/start_sandbox.sh --real` で制限付きの実 Worker を起動可能。標準は引き続き mock デモ |
| 隔離 DB / workspace | sandbox はデモ SQLite を使用。実行モードは `data/sandbox_real.sqlite3` と隔離 workspace に書き込み、本番データは使わない |
| TTL クリーンアップ | sandbox は標準 2 時間で自動停止し、デモプロセスの長期滞在を避ける |
| 軽量 API payload | dashboard API は埋め込みやプロキシ転送に必要な run、node、Worker、log、model、feedback を返す |
| Leader フィードバック日記 | 完了後にピクセルノートが開き、Leader の集約フィードバック全文を表示。Markdown のコピー/ダウンロードに対応 |
| one-line install | 上記の `curl ... | bash` で導入し、必要に応じてレビュー済みテンプレートから Hermes 連携を有効化 |

## sandbox デモ

sandbox は dashboard、実行履歴、Worker 状態、モデル表示、Leader 日記をデモするための環境です。標準では mock API と匿名化済みデモデータを使い、**実 Worker を呼び出さず、本番データを書き込まず、実ランタイムデータを含みません**。

```bash
# ワンコマンド起動（デフォルト 2 時間、タイムアウトで自動停止）
./scripts/start_sandbox.sh

# 実行時間をカスタマイズ
./scripts/start_sandbox.sh 4              # 4 時間
./scripts/start_sandbox.sh 0.5            # 30 分
./scripts/start_sandbox.sh --hours 8      # 8 時間
./scripts/start_sandbox.sh --port 8877    # ポート変更
./scripts/start_sandbox.sh -i             # 対話的に時間を尋ねる

# 既存 DB を再利用、または隔離 DB/workspace で実 Worker を試行
./scripts/start_sandbox.sh --no-reseed
./scripts/start_sandbox.sh --real
```

起動後アクセス：`http://127.0.0.1:8876/`。詳細は [`sandbox/README.md`](sandbox/README.md) を参照してください。

## コアコンセプト

```text
ユーザー → Leader(AI) → WBS 分解 → Worker(AI) × N 並列 → 集約 → 結果
```

- **Leader**：複雑度スコアリング、WBS 分解、結果集約、Skill/Tool 配布。
- **Worker**：個別ノードを実行し、必要に応じて Skill とツールホワイトリストを読み込む。
- **Agent Backend**：Claude Code / Hermes Agent / Codex / OpenCode / カスタム coding agent を抽象化。
- **SQLite**：実行状態、ノード結果、コンテキストスナップショット、経験を永続化。
- **Dashboard**：パイプライン、Worker プール、Skill/Tool 注入、モデル、ログをリアルタイム表示。

## CLI コマンド

### タスク実行

```bash
hermes-collab run "現在のプロジェクト構造を分析" --cwd . --json
hermes-collab run --request-file request.md --cwd .
hermes-collab run "協調タスクを実装" --agent claude-code --concurrency 4 --timeout 900
```

### dashboard 起動

```bash
hermes-collab server --host 0.0.0.0 --port 8765 --cwd .
```

### Skill / Tool 確認

```bash
hermes-collab skills                                # 全スキル一覧
hermes-collab skills --node-type implementation      # 選択されたスキルをプレビュー
hermes-collab tools                                 # 全ツール設定
hermes-collab tools --node-type implementation       # 選択されたツールをプレビュー
```

### Agent / ステータス確認

```bash
hermes-collab agents                # 登録済み backend
hermes-collab agents --available    # PATH 上で利用可能なもの
hermes-collab status --json
```

### 経験管理

```bash
hermes-collab lessons                       # 経験一覧
hermes-collab lessons --scope global        # スコープで絞り込み
hermes-collab add-lesson --category timeout --lesson "大きなファイルは分割する" --scope global
```

### 実行中の介入

```bash
hermes-collab kill-node <run_id> <node_id>  # ノードを終了
hermes-collab split-node <run_id> <node_id> # ノードを分割
hermes-collab skip-node <run_id> <node_id>  # ノードをスキップ
hermes-collab redo-node <run_id> <node_id>  # ノードを再実行
hermes-collab log <run_id> <node_id> "msg"  # ログに書き込み
```

### 検証

```bash
hermes-collab verify-release # v5.0 リリース完全性チェック
```

## API

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/overview` | 概要データ |
| GET | `/api/runs` | 実行履歴 |
| GET | `/api/runs/:id` | dashboard の高速更新向けの軽量実行詳細（ノードと直近ログ） |
| GET | `/api/runs/:id?full=1` | Worker、完全なログ、モデル、Leader feedback を含む完全な実行詳細 |
| GET | `/api/logs` | 直近のログ |
| GET | `/api/lessons` | 自学習経験 |
| GET | `/api/agents` | 利用可能な Agent Backend |
| GET | `/api/skills?node_type=&task=` | 選択プレビュー付き Skill レジストリ |
| GET | `/api/tools?node_type=&task=` | 選択プレビュー付き Tool 設定 |
| GET | `/api/events` | SSE リアルタイムイベントストリーム |
| POST | `/api/runs` | 非同期タスク送信 |

## 設定ソース

ランチャーは以下の優先度で API 設定を自動検出します：

1. **`~/.hermes/.env`** — `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL`（推奨）
2. **`~/.hermes/config.yaml`** — `model.base_url` + `model.default`
3. **`~/.hermes/auth.json`** — credential pool 内の Anthropic 認証情報
4. **`~/.claude/settings.json`** — Claude Code 設定（フォールバック）
5. **手動入力** — BaseURL + API Key + モデル一覧

Hermes が Leader であり、その設定を主ソースとすべきです。Claude Code 設定は互換性のためのフォールバックです。リポジトリが提供するのは `templates/claude/settings.example.json` を含む空スケルトンと `.example` ファイルのみで、実際の Hermes/Claude secrets、token、session、auth、log、sqlite データ、skills、memories を読み取り・コピー・公開しません。

環境変数：

```bash
HERMES_COLLAB_MODEL=glm-5.1           # グローバルモデル
HERMES_COLLAB_LEADER_MODEL=glm-5.1    # Leader モデル
HERMES_COLLAB_WORKER_MODEL=kimi-k2.6  # Worker モデル
ANTHROPIC_MODEL=glm-5.1               # フォールバック
```

## 永続化とセキュリティ境界

SQLite ファイル（デフォルト `data/collab.sqlite3`）は runs、wbs_nodes、workers、logs、lessons、node_results、settings、context_snapshots を保存します。API Key は環境変数またはローカル設定からのみ取得し、データベースには書き込みません。

- Worker は独立したサブプロセスで実行され、`allowed_tools` ホワイトリストにより制約される。
- MCP ツールはデフォルトで読み取り専用（`mcp-readonly` profile）。
- sandbox は隔離されたデモ DB と workspace を使用し、TTL でクリーンアップされる。
- `git push` は `git-write` tool profile により制限され、implementation ノードでのみ利用可能。

## Agent Backend

| Backend | コマンド | 出力パース |
|---|---|---|
| claude-code | `claude -p` | session ID + text |
| codex | `codex` | JSON |
| opencode | `opencode` | text |

カスタム Backend：`AgentBackend` インターフェース（`name`, `build_command`, `parse_output`, `default_allowed_tools`）を実装して登録します。

## 開発

```bash
pip install -e .
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

```text
src/hermes_collab_engine/
├── cli.py           # CLI エントリポイント
├── engine.py        # コアエンジン
├── server.py        # Web dashboard
├── store.py         # SQLite 永続化
├── models.py        # データモデル
├── skills.py        # Skill 配信
├── tools.py         # MCP ツール管理
├── agents/          # Agent Backend 抽象化
├── verification.py  # v5.0 リリース完全性チェック
└── ...
web/
└── index.html       # 可視化 dashboard
```

## ライセンス

MIT
