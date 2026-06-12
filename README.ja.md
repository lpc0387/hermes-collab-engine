# Hermes Collab Engine

<p align="center">
  <a href="README.md">简体中文</a> |
  <a href="README.en.md">English</a> |
  <a href="README.ja.md">日本語</a>
</p>

> 公式 Hermes Agent と Claude Code Worker のための独立型コラボレーションエンジンです。タスクの複雑度を判定し、WBS に分解し、複数 Worker に並列分配し、タイムアウトした作業を小さなシャードに分割して再試行します。状態は SQLite に永続化され、中国語の管理ダッシュボードを提供します。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](#必要環境)
[![SQLite](https://img.shields.io/badge/SQLite-persistence-green)](#永続化)
[![Hermes](https://img.shields.io/badge/Hermes-Agent-purple)](#実行フロー)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Worker-orange)](#実行フロー)

## ワンコマンドインストール

```bash
curl -fsSL https://raw.githubusercontent.com/lpc0387/hermes-collab-engine/main/scripts/install.sh | bash
```

インストール後に起動：

```bash
opc
```

`opc` は次の手順を案内します：

1. ローカルの Claude/Hermes 設定を自動読み取りするか、BaseURL、API Key、モデル名を手動入力するかを選択します。
2. Leader Agent モデルを選択します。
3. Worker Agent モデルを選択します。
4. 管理ダッシュボードのホスト、ポート、デフォルト作業ディレクトリを選択します。
5. コラボレーションエンジンの管理ダッシュボードを起動します。
6. 操作方式を選択します：Web ダッシュボードのタスク入力ウィンドウを使う、または公式 Hermes CLI に入る。

選択した操作方式を終了すると、`opc` はそのセッションで起動した管理ダッシュボードを停止します。ダッシュボードにはタスク入力ウィンドウが内蔵されているため、デフォルトでは Web 操作を推奨します。端末での対話が必要な場合のみ Hermes CLI を選択してください。

## 解決する課題

単一 Agent で大きなタスクを扱う場合、次の問題が起こりやすくなります：

- タスク境界が不明確。
- すべての作業が直列実行になる。
- 長時間タスクにチェックポイントや再試行戦略がない。
- 複数 Worker の状態が見えない。
- 実行履歴、失敗原因、学習経験が蓄積されない。
- ログ、Worker 状態、タスクグラフを確認する統一ダッシュボードがない。

Hermes Collab Engine は実行を「計画層」と「実行層」に分けます：

- **Leader Agent** は複雑度判定、WBS 分解、結果集約を担当します。
- **Worker Agent** は個別の WBS ノードを実行します。
- **SQLite** は run、node、worker、log、lesson を記録します。
- **管理ダッシュボード** は状態をリアルタイムに表示します。

## 実行フロー

```text
ユーザー
  ↓
公式 Hermes Agent（Parent）
  ↓ 任意：delegate_task 事前分析
Hermes Collab Engine
  ↓ Leader 駆動スコアリング / WBS 分解
ストリームスケジューラ + SQLite
  ↓ 上流コンテキスト / 親・祖父 lineage
Claude Code Worker 1..N
  ↓ デュアルトラック出力（Machine result + Human deliverable）
集約結果 / スコープ付きレッスン
  ↓
ユーザーへ返却
```

## 主な機能

| 機能 | 説明 |
|---|---|
| 複雑度判定 | ドメイン、手順数、曖昧さ、結合度、リスクを評価します |
| WBS 分解 | 複雑なタスクを実行可能な作業分解ノードに分割します |
| 並列分配 | 依存関係を満たしたノードを Claude Code Worker に並列実行させます |
| タイムアウト監視 | Worker のタイムアウト後、小さく分割して再試行します |
| シャード再試行 | 範囲、証拠、実装、リスクに焦点を当てたシャードを作成します |
| 結果集約 | 親タスクとシャード結果を集約し、成功・失敗・タイムアウトを正直に報告します |
| SQLite 永続化 | 実行履歴と状態を SQLite に保存します |
| 自己学習経験 | タイムアウト、遅いタスク、失敗、中断 run から lesson を記録します |
| 管理ダッシュボード | run、log、worker、lesson を表示する中国語 Web ダッシュボード |
| Leader 駆動スコアリング | Leader Agent が複雑度を採点し、直列実行・WBS 分解・シャード化の経路を決めます |
| セマンティック圧縮分解 | Leader 出力が不完全な場合でも、元タスクの意味を圧縮して汎用フォールバック WBS を生成します |
| デュアルトラック出力 | Worker の機械可読 JSON 結果と人間向け deliverable を分離して保存・集約します |
| 段階的上流コンテキスト | Worker prompt に親・祖父 lineage と完了済み依存ノードの要約を段階的に注入します |
| ストリームスケジューリング | 固定バッチではなく ready キューを使い、依存が満たされたノードを即時 dispatch します |
| プロアクティブ分割 | 高リスクまたは広すぎるノードを timeout 前に focused shard へ能動的に分割します |
| 親介入 | Parent / Operator が CLI からログ記録、kill、split、skip などの実行中介入を行えます |
| スコープ付きレッスン | lesson に source と scope を持たせ、局所的な経験を global ルールとして誤用しないようにします |
| 環境変数モデルフォールバック | `HERMES_COLLAB_MODEL`、`HERMES_COLLAB_LEADER_MODEL`、`HERMES_COLLAB_WORKER_MODEL`、`ANTHROPIC_MODEL` からモデルを補完します |

## 自己アップグレード同期ポリシー

AI / コラボレーションエンジンの安定したルール変更は、バックアップと移行性のため GitHub に同期する必要があります。allowlist に基づく最小コミット戦略を採用し、秘密情報、profiles、settings、実行データベース、ログ、セッション記録はコミットしません。詳細は [AI / コラボレーションエンジン自己アップグレード同期ポリシー](docs/self-upgrade-policy.md) を参照してください。

## 必要環境

- Linux / macOS / WSL
- Python 3.11+
- Git
- Claude Code CLI：`claude`
- 公式 Hermes Agent：`hermes`

Node.js 依存はなく、`npm install` も不要です。

## ランチャー

```bash
opc
```

ランチャーは 2 種類の API 設定方式をサポートします：

### ローカル設定の自動読み取り

以下を読み取ります：

```text
~/.claude/settings.json
~/.claude/profiles/*.json
```

すでに Claude Code / Hermes が設定済みのサーバーに適しています。

### 手動入力

以下を入力します：

- BaseURL
- API Key / Auth Token
- 利用可能なモデル名（カンマ区切り）

新しいサーバーや、ローカル設定を読み取りたくない環境に適しています。

## モデル選択

起動時に次を選択します：

### Leader Agent モデル

用途：

- 複雑度判定。
- WBS 分解。
- 結果集約。
- Hermes CLI に入る場合のデフォルト Hermes モデル。

### Worker Agent モデル

用途：

- Claude Code Worker の実行。
- WBS ノード処理。
- タイムアウト後のシャード再試行。

## CLI の使い方

### 1 回だけタスクを実行

```bash
hermes-collab run "現在のプロジェクト構造を分析して改善提案を出す" --cwd . --json
```

### 並列数とタイムアウト戦略を指定

```bash
hermes-collab run "コラボレーションタスクを実装する" \
  --cwd . \
  --concurrency 4 \
  --timeout 900 \
  --max-retries 2 \
  --split-count 4 \
  --json
```

### リクエストファイルを使う

```bash
hermes-collab run --request-file request.md --cwd . --json
```

### 管理ダッシュボードを起動

```bash
hermes-collab server --host 0.0.0.0 --port 8765 --cwd .
```

アクセス：

```text
http://SERVER_IP:8765
```

### 状態を確認

```bash
hermes-collab status --json
```

### レッスン管理

```bash
hermes-collab lesson add \
  --category preflight \
  --lesson "同種タスクでは先に影響範囲を確定する" \
  --source hermes-delegate-task \
  --scope project \
  --evidence-json '{"reason":"事前分析で確認"}'

hermes-collab lesson list --scope project --json
```

`lesson add` は `global`、`project`、`run`、`node`、`wbs-family` の scope を受け取り、経験の適用範囲を明示します。外部の事前分析や Parent が追加する場合は、`--source` で出所を明示してください。

### 実行中の介入

```bash
hermes-collab parent-log --run-id RUN_ID --message "依存待ちを確認" --json
hermes-collab split-node --node-id NODE_ID --reason "範囲が広すぎるため事前分割" --json
hermes-collab skip-node --node-id NODE_ID --reason "上流なしで縮退継続" --json
hermes-collab kill-node --node-id NODE_ID --reason "手動停止" --json
```

介入 CLI は Parent / Operator が実行中 run を監督するための制御面です。直接 SQLite を書き換えず、CLI 経由でログ、分割、skip、kill を記録してください。

## 管理ダッシュボード

管理ダッシュボードは次を提供します：

- 総実行回数。
- 実行中 run 数。
- 実行中 Worker 数。
- lesson 数。
- run 履歴。
- run 詳細。
- リアルタイムログ。
- 自己学習 lesson。
- オンラインタスク送信。
- SSE ライブ更新。

## API

| Method | Path | 説明 |
|---|---|---|
| GET | `/api/overview` | 概要指標 |
| GET | `/api/runs` | run 一覧 |
| GET | `/api/runs/:id` | run 詳細 |
| GET | `/api/logs` | 最近のログ |
| GET | `/api/lessons` | 自己学習 lesson |
| GET | `/api/events` | リアルタイムイベント |
| POST | `/api/runs` | 非同期 run を送信 |

## 永続化

デフォルトデータベース：

```text
data/collab.sqlite3
```

テーブル：

| テーブル | 用途 |
|---|---|
| `runs` | トップレベルタスクの実行記録 |
| `wbs_nodes` | WBS ノード、依存関係、状態、結果 |
| `workers` | Worker ライフサイクル、セッション ID、所要時間、エラー |
| `logs` | 構造化ログ |
| `lessons` | 自己学習 lesson |
| `metrics` | 拡張メトリクス |

`lessons` は `source` と `scope` を持つ証拠を保存します。`node`、`run`、`wbs-family` の lesson は局所的な経験として扱い、明示的に `project` または `global` に昇格されない限り、別 run の汎用ルールとして再利用しないでください。

## タイムアウト分割戦略

デフォルトパラメータ：

```text
--timeout 900
--max-retries 2
--split-count 4
```

Worker がタイムアウトしても、システムは単純に終了しません。対象ノードをより小さなシャードに分割します：

| シャード | 目的 |
|---|---|
| 範囲シャード | 最小の関連範囲と入口を特定します |
| 証拠シャード | ファイル、コマンド、シンボル、証拠を収集します |
| 実装シャード | 最小実装またはパッチ戦略を作成します |
| リスクシャード | ブロッカー、未知点、検証項目を特定します |

シャードは再度 Worker に分配され、最後に集約されます。

## エージェント通信プロトコル

Hermes Collab Engine v2 は ACP-Collab v0.2 を採用し、Parent、事前分析、Leader、Worker の間の責務と通信経路を明確にします。詳細は [Agent Communication Protocol](docs/agent-communication-protocol.md) を参照してください。

| 通道 | 方向 | 目的 |
|---|---|---|
| Request | Parent → Engine → Worker | 自己完結したタスク本文、allowlist、作業ディレクトリ、事前分析結果を渡します |
| Dual-Track Result | Worker → Engine → SQLite / Parent | 機械可読結果と人間向け deliverable を分離します |
| Upstream Context | Worker → Worker（Engine 経由） | 親・祖父 lineage と完了済み依存ノードの要約を伝えます |
| Scoped Lessons | 任意の來源 → SQLite → Dashboard / Planner | source と scope 付きの lesson を保存し、適用範囲を制御します |
| Dispatch Control | Scheduler / Parent 介入 → SQLite | stream dispatch、proactive split、介入 CLI を監査可能にします |
| Observability | Engine → SQLite / Dashboard / Parent | run、node、worker、log、lesson の公式な読み取り経路を提供します |

禁止事項：Worker 同士の一時ファイル共有、Parent による SQLite 直接書き込み、長い request のコマンドライン直書き、batch-blocked scheduling、局所 lesson の global ルール化。

## Hermes 連携

インストーラーは以下を作成します：

```text
~/.local/bin/hermes-collab
~/.local/bin/opc
```

任意の連携スクリプト：

```bash
~/hermes-collab-engine/scripts/install-hermes-integration.sh
```

このスクリプトは Hermes 側に以下を書き込みます：

- ローカル Skill。
- Memory。
- SOUL 行動プロンプト。

これにより Hermes は、実装、分析、デバッグ、監査、研究、計画、多段階タスクでコラボレーションエンジンを優先的に使用できます。

## セーフティ境界

- 実行時データベースをアップロードまたはコミットしない。
- `.runtime-config.json` をコミットしない。
- API Key をコミットしない。
- ユーザーの業務プロジェクトを意図せず変更しない。
- Worker の実際の動作は Claude Code CLI が実行するため、必要に応じて権限ポリシーと作業ディレクトリを設定してください。

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
