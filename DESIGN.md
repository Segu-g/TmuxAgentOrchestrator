# TmuxAgentOrchestrator — 設計資料

> 本ドキュメントは設計上の意思決定と根拠を記録するものです。
> 開発の基礎検討資料として、指示・調査・実装の経緯をまとめます。

---

## 目次

1. [プロジェクトの目的と基本方針](#1-プロジェクトの目的と基本方針)
2. [アーキテクチャ設計原則](#2-アーキテクチャ設計原則)（エージェント間通信設計を含む）
3. [tmux 階層マッピング](#3-tmux-階層マッピング)
4. [エージェントワークフローとスキル](#4-エージェントワークフローとスキル)
5. [今後の課題](#5-今後の課題)
6. [調査記録](docs/research/README.md) — 各イテレーション調査記録（`docs/research/` フォルダ、v1.2.29移行）

**関連ドキュメント**:
- [docs/security.md](docs/security.md) — APIキー配送セキュリティ方針
- [docs/context-engineering.md](docs/context-engineering.md) — コンテキストエンジニアリング設計
- [docs/architecture.md](docs/architecture.md) — ワークフロー設計の層構造
- [docs/agent-lifetime.md](docs/agent-lifetime.md) — エージェントライフタイム

---

## 1. プロジェクトの目的と基本方針

### 目的

複数の Claude Code インスタンスを tmux セッション内で階層的にオーケストレーションする。
ユーザーはオーケストレーター（または Director エージェント）と会話するだけで、
要件定義・改善指示・仕様議論が完結すること。

### 基本方針（ユーザー指示より）

| 方針 | 詳細 |
|------|------|
| **オーケストレーター中心** | ユーザーはオーケストレーターとのみ会話し、個別エージェントへのアクセスはオプション |
| **tmux 階層の活用** | session=プロジェクト、window=上位エージェント、pane=サブエージェント |
| **通信はフレームワーク経由** | エージェント間通信はバス/REST経由。メッセージ着信を stdin 通知でエージェントに伝える |
| **階層的通信制御** | デフォルトは親子・兄弟間のみ許可。横展開は明示的な p2p_permissions で許可 |
| **コンテキスト局所化** | 各エージェントのコンテキストウィンドウを独立させ、役割に特化した情報のみ持つ |
| **TDD の統合** | エージェントの開発フローにテスト駆動開発を組み込む |

### 競合差別化と強化すべき方向性

Claude Code は `.claude/agents/`（サブエージェント）や Agent Teams（P2Pメッセージング、実験的）など、
TmuxAgentOrchestrator と重複する機能を追加しつつある。
差別化の核心は以下の2点に絞られる：

1. **宣言的ワークフロー制御** — YAML で parallel/sequence/loop の複雑な DAG を宣言できる。Claude Code ネイティブには相当機能がない。
2. **可観測性** — tmux ペインでリアルタイムに全エージェントを人間が目視できる。Web UI + REST API による外部制御も可能。

**強化すべき方向性（2026-03-14 ユーザー指示）：**

> 「このリポジトリで強めるべきはワークフローのテンプレート、プリセットとそれに付随するエージェントコンテキストのパック」

具体的には：
- **ワークフローテンプレートの拡充** — 実際のソフトウェア開発シナリオをカバーする YAML テンプレートのプリセット集
- **エージェントコンテキストパック** — ワークフローに付随するロール別ルール (`.claude/rules/`)、system_prompt、slash command のセットを1単位として提供する。ワークフローを使い始めた瞬間にエージェントが正しいコンテキストで動き出す体験を作る
- **ロール別ルールの整備** — `worker.md` / `director.md` に加え、TDD の `tester.md` / `coder.md`、レビューの `reviewer.md`、計画の `planner.md` 等を追加し、ワークフローと対応させる

---

## 2. アーキテクチャ設計原則

### コンポーネント一覧

```
TmuxAgentOrchestrator/
├── Bus (bus.py)              — 非同期 pub/sub バス
├── Orchestrator (orchestrator.py) — タスクキュー・P2P制御・階層管理
├── TmuxInterface (tmux_interface.py) — libtmux ラッパー + 監視スレッド
├── AgentBase (agents/base.py) — 抽象エージェント
├── ClaudeCodeAgent (agents/claude_code.py) — Claude CLI 駆動エージェント
├── WorktreeManager (worktree.py) — git worktree 分離
├── Mailbox (messaging.py)    — ファイルベース永続メッセージ
├── Web UI (web/app.py)       — FastAPI + 組み込み HTML/JS
└── TUI (tui/app.py)          — Textual TUI
```

### メッセージ種別

| 種別 | 用途 |
|------|------|
| `TASK` | タスクのディスパッチ |
| `RESULT` | タスク完了通知 |
| `STATUS` | エージェント状態変化・ペイン出力 |
| `PEER_MSG` | エージェント間メッセージ（P2P） |
| `CONTROL` | サブエージェント生成などの制御 |

### ドメイン分離方針

関心（ドメイン）ごとにファイル・フォルダを分離する。
1つのファイルに複数のドメインの関心が混在する場合は分割を検討する。

| ドメイン | 現在の主なファイル | 関心の範囲 |
|---|---|---|
| **tmux 操作** | `tmux_interface.py` | libtmux ラッパー、ペイン生成・監視、キー送信 |
| **エージェント状態** | `agents/base.py`, `registry.py` | AgentStatus、ライフサイクル、タスク割り当て |
| **Claude 駆動** | `agents/claude_code.py` | claude CLI 起動、ペイン出力解析、完了検出 |
| **Claude Plugin** | `agent_plugin/` | スラッシュコマンド定義、SessionStart/Stop hook |
| **タスク管理** | `orchestrator.py` | タスクキュー、ディスパッチ、P2P 制御、ウォッチドッグ |
| **ワークフロー** | `workflow.py`, `web/app.py` | フェーズ定義、依存解決、`POST /workflows` |
| **エージェントテンプレート** | `config.py`, `agent_plugin/commands/` | YAML ロール定義、system_prompt、context_files |
| **通信バス** | `bus.py`, `messaging.py` | pub/sub、P2P メッセージ、メールボックス |
| **Web/API** | `web/app.py`, `web/ws.py` | REST エンドポイント、WebSocket ハブ |

**原則**:
- ドメインをまたぐ依存は「外から中へ」（上位層が下位層を参照）。逆方向の依存は禁止。
- 新機能を追加するとき、既存ファイルに混入させず、対応するドメインのファイルに追加する。
- ドメインが明確に定まらない場合は、まず小さなモジュールとして切り出し、後から統合を判断する。

**目標とするクリーンアーキテクチャ層構造**:

```
tmux_orchestrator/
├── domain/          # 純粋ドメイン型。外部依存ゼロ。AgentStatus, Task, MessageType など
├── application/     # ユースケース・業務ロジック。domain のみに依存
│                    # orchestrator, registry, bus, supervision, workflow, phase_executor
├── infrastructure/  # 外部システム実装詳細。tmux, git worktree, SQLite, ファイルI/O
│                    # tmux_interface, messaging, result_store, checkpoint_store, worktree
├── monitoring/      # 横断的観測。context_monitor, drift_monitor, autoscaler, telemetry
├── adapters/        # フレームワーク接続。config (YAML→内部型), factory (DI), security,
│                    # schemas (Pydantic), web/ (FastAPI), tui/ (Textual)
├── agents/          # Claude CLI ドライバ (infrastructure の特殊ケース。現位置維持)
└── agent_plugin/    # Claude Code プラグイン (スラッシュコマンド・hooks)
```

現状は全ファイルがフラットに配置されており、段階的なイテレーションで上記構造へ移行する（§11 参照）。

### 設計・実装レビュープロセス

設計と実装の品質を担保するため、以下のレビュープロセスを設ける。

**原則**:
- 設計内容は必ずファイルとして書き出してから実装に入る。
- 設計文書はコンテキストを共有しないサブエージェント（または別セッションのエージェント）によるレビューを経てから実装する。
- レビューで指摘を受けた場合、修正後に再レビューを実施し、指摘が改善されていることを確認する。

**ファイル規約**:
- 設計文書: `./design/${version}.md`（例: `design/v1.0.11-clean-arch.md`）
- レビュアーの指摘は同ファイルに `## レビュー指摘` セクションとして追記する。
- 指摘は**決して削除しない**。修正内容を `> 対応: ...` として明記する。

**フロー**:
```
設計文書を design/${version}.md に書く
  → サブエージェントが設計文書のみを受け取りレビュー（コードは読まない）
  → 指摘を design/${version}.md に追記
  → 指摘を修正（設計文書 + 実装）
  → 再レビューで改善を確認
  → 実装確定・コミット
```

---

### エージェント間通信の設計

#### 通信フロー

```
エージェント A  →  Bus (PEER_MSG)  →  Orchestrator (route_loop)
                                    ↓
                              P2P 許可チェック
                                    ↓
                              Bus (PEER_MSG, _forwarded=True)
                                    ↓
                    エージェント B の _message_loop
                                    ↓
                    Mailbox に書き込み + notify_stdin("__MSG__:{id}")
                                    ↓
                    エージェント B の tmux pane に __MSG__:xxx が入力される
```

#### ポーリング不要の設計

- エージェントはメッセージをポーリングしない
- `notify_stdin(f"__MSG__:{msg.id}")` が tmux pane にキーを送信
- エージェントはこれを受けて `/check-inbox` → `/read-message` を実行

#### 通信許可ルール

```python
def _is_hierarchy_permitted(from_id, to_id):
    # 1. 両エージェントが登録済み
    if from_id not in agents or to_id not in agents:
        return False
    # 2. 親→子 / 子→親
    if from_id == to_parent or to_id == from_parent:
        return True
    # 3. 兄弟（同じ親、ルートレベル同士を含む）
    if from_parent == to_parent:
        return True
    return False
```

**明示的 P2P** (`p2p_permissions` in config) は横展開・クロスブランチ通信のエスケープハッチ。

---

## 3. tmux 階層マッピング

### 設計方針

```
tmux Session  ←→  プロジェクト（1プロジェクト = 1セッション）
tmux Window   ←→  上位エージェント（YAML config で定義）
tmux Pane     ←→  サブエージェント（動的生成、親ウィンドウ内に分割）
```

### 実装

- `TmuxInterface.new_pane(agent_id)` → 新しいウィンドウを作成（上位エージェント用）
- `TmuxInterface.new_subpane(parent_pane, agent_id)` → 親ウィンドウを分割（サブエージェント用）
- `ClaudeCodeAgent.__init__(parent_pane=...)` → 親ペインを受け取り、サブペイン生成を決定

### 視覚的表現

```
Session: my-project
├── Window 0: director
│   ├── Pane 0: director (main)
│   ├── Pane 1: director-sub-a3f2c1  ← director が生成したサブエージェント
│   └── Pane 2: director-sub-b7e4d9
├── Window 1: worker-1
│   ├── Pane 0: worker-1 (main)
│   └── Pane 1: worker-1-sub-x5k2m8
└── Window 2: worker-2
    └── Pane 0: worker-2 (main)
```


> セキュリティ方針については [docs/security.md](docs/security.md) を参照。

---

## 4. エージェントワークフローとスキル


### スラッシュコマンド一覧

| コマンド | 分類 | 説明 |
|----------|------|------|
| `/check-inbox` | 通信 | 未読メッセージ一覧 |
| `/read-message <id>` | 通信 | メッセージ詳細表示・既読 |
| `/send-message <id> <text>` | 通信 | エージェントへのメッセージ送信 |
| `/spawn-subagent <template>` | 階層管理 | サブエージェント生成 |
| `/list-agents` | 階層管理 | エージェント一覧・状態確認 |
| `/plan <description>` | TDD/CE | 実装前の計画作成 (PLAN.md) |
| `/tdd <feature>` | TDD | Red→Green→Refactor サイクル案内 |
| `/progress <summary>` | 通信 | 親エージェントへの進捗報告 |
| `/summarize` | CE | コンテキスト圧縮 → NOTES.md |
| `/delegate <task>` | 階層管理 | サブエージェントへのタスク委任 |

### 典型的なワーカーエージェントのフロー

```
タスク受信 (キュー or __MSG__)
    ↓
/plan <task>          # 受け入れ基準を明文化
    ↓
/tdd <feature>        # Red phase: 失敗するテストを書く
    ↓
実装 (Green phase)
    ↓
テスト確認 + リファクタ
    ↓
/progress "完了: <summary>"  # 親に報告
    ↓
/summarize            # 必要に応じてコンテキスト圧縮
```

### Director エージェントのフロー

```
ユーザーとの会話 (Web UI)
    ↓
/plan <project goal>  # 全体計画
    ↓
/delegate <task>      # サブタスクに分解
    ↓
/spawn-subagent worker-1  # サブエージェント生成
/send-message <id> <subtask>
    ↓
/check-inbox          # ワーカーからの進捗受信
    ↓
結果集約 → ユーザーへの報告
```

---

## 5. 今後の課題


> バックログは [docs/architecture.md](docs/architecture.md) の5層モデルに基づいて再整理されている。
> 各項目がどの層に属するかを明示することで、実装の依存関係と優先度の根拠を明確にする。
>
> 優先度基準:
> - **高** — 直接ユーザー向け新機能 + 強い研究的裏付け + 明確な実装パス + 既存機能を活用
> - **中** — 有用だがさらなる設計が必要、または高優先度アイテムに依存
> - **低** — あると良い、複雑、または価値が不確実

---

### 層1・2：ワークフロー設計 × フェーズ管理（最重要・未着手）

| 優先度 | 課題 | 層 |
|--------|------|----|
| ~~**高**~~ | ~~**汎用宣言的ワークフロー API**~~ | ~~完了 v0.48.0 — `POST /workflows` に `phases=` 配列スキーマ追加。`PhaseSpec` が single/parallel/competitive/debate をサポート。43/43 デモ PASS。~~ |
| ~~**高**~~ | ~~**Phase 一級市民化**~~ | ~~完了 v0.48.0 — `WorkflowPhaseStatus` で pending→running→complete/failed を追跡。`WorkflowRun.phases` フィールド。`GET /workflows/{id}` がフェーズ配列を返す。~~ |
| ~~**高**~~ | ~~**Planner エージェントロール**~~ | ~~完了 v0.48.0 — `planner.md` ロールテンプレート + `/plan-workflow` スラッシュコマンド追加。~~ |

---

### 層3：ステージの実行方式（追加・自律化）

| 優先度 | 課題 | 層 |
|--------|------|----|
| ~~**高**~~ | ~~**エージェント自律による実行方式変更**~~ | ~~完了 v0.49.0 — `POST /agents/{id}/change-strategy`、`ChangeStrategyRequest` モデル (single/parallel/competitive)、`/change-strategy` スラッシュコマンド。41/41 デモ PASS。~~ |
| ~~中~~ | ~~**`POST /workflows/delphi`**~~ | ~~完了 v1.0.23 — `DelphiWorkflowSubmit`, 3–5エキスパート × 1–3ラウンド, `delphi_round_{n}.md` + `consensus.md`. 22/22 デモ PASS. 40テスト.~~ |
| ~~中~~ | ~~**`POST /workflows/redblue`**~~ | ~~完了 v1.0.24 — `RedBlueWorkflowSubmit`, blue-team→red-team→arbiter 3エージェントパイプライン. `blue_design.md` + `red_findings.md` + `risk_report.md`. 20/20 デモ PASS. 28テスト.~~ |
| 中 | **`/deliberate <question>` スラッシュコマンド** — エージェントが自発的に2エージェント討論を起動し `DELIBERATION.md` を生成 | 層3 |

---

### 層4：コンテキスト伝達（改善）

| 優先度 | 課題 | 層 |
|--------|------|----|
| ~~中~~ | ~~**コンテキスト4戦略ガイド** — 書き込み・選択・圧縮・分離の4戦略チートシートを CLAUDE.md に追記~~ | ~~完了 v1.1.19~~ |
| ~~中~~ | ~~**MIRIX 型エピソード記憶ストア**~~ | ~~完了 v1.0.28~~ |
| ~~中~~ | ~~**スライディングウィンドウ + 重要度スコアによるコンテキスト圧縮** — TF-IDF 類似度でスコア下位 40% を削除する `/summarize` の上位互換~~ | ~~完了 v1.1.12 (`context_auto_compress` + `TfIdfContextCompressor`)~~ |

---

### 層5：ツール・マネジメント（アーキテクチャ品質）

| 優先度 | 課題 | 層 |
|--------|------|----|
| ~~**高**~~ | ~~**チェックポイント永続化による中断再開**~~ | ~~完了 v0.45.0 — SQLite CheckpointStore + `--resume` フラグ。~~ |
| ~~**高**~~ | ~~**`ProcessPort` 抽象インターフェース抽出**~~ | ~~完了 v1.0.34 — `ProcessPort` protocol + `TmuxProcessAdapter` / `StdioProcessAdapter`。~~ |
| ~~**高**~~ | ~~**OpenTelemetry GenAI Semantic Conventions**~~ | ~~完了 v1.1.10 — `workflow_span()` + `RingBufferSpanExporter` + `GET /telemetry/spans`。25テスト。~~ |
| ~~**高**~~ | ~~**エージェントドリフト検出 (Agent Stability Index)**~~ | ~~完了 v1.0.9 — `DriftMonitor` (role/idle/length 3サブスコア)、`agent_drift_warning` イベント、`GET /drift`・`GET /agents/{id}/drift` エンドポイント。34テスト。17/17デモPASS。~~ |
| ~~中~~ | ~~**`UseCaseInteractor` 層の抽出** — FastAPI ハンドラーから業務ロジックを分離~~ | ~~完了 v1.1.14 (SubmitTaskUseCase / CancelTaskUseCase) + v1.1.15 (ListAgentsUseCase / GetAgentUseCase wiring)~~ |
| ~~中~~ | ~~**エージェント状態機械の Hypothesis ステートフルテスト**~~ | ~~完了 v1.0.33 — `AgentStatus` / `CircuitBreaker` / `AgentRegistry` 3ステートマシン。~~ |
| 低 | **構造化デバッグ: トレースリプレイ CLI** — `ResultStore` JSONL から過去実行を再現 | 層5 |

---

### ワークフローテンプレート・ドキュメント整備

| 優先度 | 課題 |
|--------|------|
| ~~中~~ | ~~**`examples/workflows/` YAML テンプレートライブラリ**~~ | ~~完了 v1.1.16 — 12ワークフロー YAML + README。~~ |
| ~~中~~ | ~~**`POST /workflows/clean-arch`**~~ | ~~完了 v1.0.30 — 4エージェント Clean Architecture パイプライン。~~ |
| ~~中~~ | ~~**`POST /workflows/pair`**~~ | ~~完了 v1.0.27 — Navigator + Driver ペアプログラミング。~~ |
| 中 | ~~**`POST /workflows/socratic`**~~ — questioner + responder + synthesizer ソクラテス的対話 (**v1.0.25完了**) |
| 低 | **`DECISION.md` 標準フォーマット** — 全ワークフロー共通の出力フォーマット策定 |

---

### 機能・ワークフロー（優先度順）

| 優先度 | 課題 | 根拠 |
|--------|------|------|
| ~~高~~ | ~~**`POST /workflows/tdd`**~~ | ~~完了 v0.36.0~~ |
| ~~高~~ | ~~**`POST /workflows/debate`**~~ | ~~完了 v0.37.0 — advocate/critic/judge 3役割, 1–3ラウンド, judge→DECISION.md. ALL 27 CHECKS PASSED. デモ: SQLite vs PostgreSQL, Advocate(PG)勝利.~~ |
| ~~**高**~~ | ~~**スラッシュコマンド群をエージェントワークツリーで使用可能にする**~~ | ~~完了 v1.0.19以降 — `_copy_commands()` が `agent_plugin/commands/*.md` を `.claude/commands/` へ自動コピー。`ClaudeCodeAgent.start()` で worktree セットアップ後に呼び出される。確認済み。~~ |
| ~~高~~ | ~~**役割別 system_prompt テンプレートライブラリ + `system_prompt_file:` YAML フィールド**~~ | ~~完了 v1.0.27 + v1.1.19確認 — `.claude/prompts/roles/` に advocate/critic/judge/tester/implementer/reviewer/spec-writer/architect/planner の9テンプレート完備。`AgentConfig.system_prompt_file:` フィールドが `config.py` + `factory.py` に実装済み。`tests/test_system_prompt_file.py` が存在。~~ |
| ~~**高**~~ | ~~**`POST /workflows/adr` — Architecture Decision Record (ADR) 自動生成ワークフロー**~~ | ~~完了 v0.40.0 — proposer→reviewer→synthesizer 3エージェントパイプライン、MADR 4.0.0 フォーマット DECISION.md 生成。ALL 27 CHECKS PASSED。`test_workflow_adr.py` 25テスト。~~ |
| ~~**高**~~ | ~~**Codified Context インフラ**~~ | ~~完了 — `context_spec_files: list[str]` glob パターン、`_copy_context_spec_files()` 実装済み (`agents/claude_code.py`)。`tests/test_context_spec_files.py` 存在。~~ |
| ~~高~~ | ~~**チェックポイント永続化による中断再開**~~ | ~~完了 v0.45.0 — `infrastructure/checkpoint_store.py` (SQLite WAL + 3テーブル), `--resume` フラグ実装済み。`CheckpointStore` が task/waiting/workflow スナップショットを永続化。~~ |
| ~~高~~ | ~~**Claude Code `Stop` フックによる完了検出**~~ | ~~完了 v1.0.x — `agents/completion.py` の `NudgingStrategy` が `on_start()` で Stop hook を `.claude/settings.local.json` に書き込む。`ExplicitSignalStrategy` は DIRECTOR 用。`make_completion_strategy()` ファクトリーで役割別に切り替え。~~ |
| ~~高~~ | ~~**`ProcessPort` 抽象インターフェースの抽出**~~ | ~~完了 v1.0.34 — `infrastructure/process_port.py` に `ProcessPort` protocol / `TmuxProcessAdapter` / `StdioProcessAdapter` 実装済み。`ClaudeCodeAgent` は `self.process: ProcessPort` のみに依存。~~ |
| 中 | **`POST /workflows/clean-arch` — 4レイヤー分解ワークフロー** — Director が機能要求を domain / use-case / adapter / framework の4レイヤーに分解し、各レイヤーを専用ワーカーに割り当てるテンプレートを追加。`.claude/prompts/roles/` に `domain-agent.md` / `usecase-agent.md` / `adapter-agent.md` / `framework-agent.md` を提供し、PLAN.md 経由でハンドオフする | AgentMesh arXiv:2507.19902 (2025): Planner→Coder→Debugger→Reviewer の4ロール分担がソフトウェア開発タスクを自動化。Muthu (2025-11) "The Architecture is the Prompt": 「ヘキサゴナルアーキテクチャの境界がそのまま AI エージェントのコンテキスト制約になる」。`POST /workflows/tdd` および役割テンプレートライブラリ完成後に実装。 |
| ~~中~~ | ~~**`POST /workflows/pair` — PairCoder (Navigator + Driver) ワークフロー**~~ | ~~完了 v1.0.27 — `PairWorkflowSubmit`, navigator→driver 2エージェントパイプライン, `{prefix}_plan` + `{prefix}_result` スクラッチパッド. 17/17 デモ PASS. 35テスト.~~ |
| 中 | **`POST /workflows/delphi` — 複数ラウンド合意形成ワークフロー** — 3–5 名の専門家エージェント（セキュリティ / パフォーマンス / 保守性 / UX / コスト 各ペルソナ）が匿名で意見を提出し、モデレーターエージェントが集計して次ラウンドにフィードバックする Delphi サイクルを最大 3 ラウンド実行する DAG を追加。各ラウンドの出力を `delphi_round_{n}.md` に保存し、最終合意を `consensus.md` に書き出す | RT-AID "Real-Time AI Delphi" ScienceDirect 2025 (arXiv:2502.21092) が LLM によるデルファイ法の自動化を提案。Du et al. ICML 2024: 「エージェントが全員誤りでも討論で正解に収束するケースが多数存在する」→ 複数ラウンドの価値を実証。 |
| 中 | **`POST /workflows/redblue` — Red Team / Blue Team 対抗評価ワークフロー** — `blue-team`（実装・設計案を作成）・`red-team`（攻撃者視点で脆弱性・欠陥を列挙）・`arbiter`（リスク評価レポートを生成）の3エージェント構成。セキュリティレビュー・ビジネスケース検証・アーキテクチャ変更の影響分析に利用できる汎用的な対抗評価テンプレートを提供する | Adversarial Multi-Agent Evaluation arXiv:2410.04663: 反復討論型評価がバイアス削減と判断精度向上に有効。Red-Teaming LLM MAS ACL 2025 (arXiv:2502.14847): エージェント間通信への攻撃評価手法を提案。`debate` ワークフローの特殊化として実装できる。 |
| 中 | **`POST /workflows/socratic` — ソクラテス的対話ワークフロー** — `questioner`（前提・定義・論拠を問うマイウティカ法）・`responder`（回答を精緻化）・`synthesizer`（対話ログから構造化結論を抽出）の3エージェント構成。設計仕様の曖昧性解消・要件の深掘り・技術負債の根本原因分析に適する。最初のラウンドは強い反論、後のラウンドは統合的問いに変化させる段階的移行モデルを採用する | SocraSynth arXiv:2402.06634: モデレーター型ソクラテス的マルチエージェント討論プラットフォーム。KELE EMNLP 2025 (arXiv:2409.05511): LLM ベースのソクラテス教授エージェント実証。`debate` / `adr` ワークフローと共通基盤で実装できる。 |
| ~~中~~ | ~~**`/deliberate <question>` スラッシュコマンド**~~ | ~~完了 v1.0.32 — `agent_plugin/commands/deliberate.md` 実装済み。2エージェント討論（advocate/critic）、`DELIBERATION.md` 生成。~~ |
| ~~中~~ | ~~**`POST /workflows/ddd` — DDD Bounded Context 分解ワークフロー**~~ | ~~完了 v1.0.32以降 — `web/routers/workflows.py` の `submit_ddd_workflow()` 実装済み。`DDDWorkflowSubmit` スキーマ。~~ |
| ~~中~~ | ~~**形式仕様エージェントステップ + `/spec` スラッシュコマンド + `POST /workflows/spec-first`**~~ | ~~完了 v1.1.8 — `/spec` スラッシュコマンド (`agent_plugin/commands/spec.md`) + `POST /workflows/spec-first` (spec-writer→implementer 2エージェントパイプライン)。`SpecFirstWorkflowSubmit` スキーマ。57テスト新規追加。2007テスト全通過。~~ |
| ~~中~~ | ~~**コンテキスト4戦略ガイドを CLAUDE.md と `.claude/prompts/` に体系化**~~ | ~~完了 v1.1.19 — CLAUDE.md「Context Engineering Cheatsheet」セクション追加 + `.claude/prompts/context-strategies.md` ロール別マトリクス追加。~~ |
| ~~低~~ | ~~**LLM-as-Judge による並列エージェント出力の自動スコアリング (BestOfN + EDDOps)**~~ | ~~完了 v1.1.0 — `POST /workflows/competition` として実装。N 個の solver エージェントが並列で同一問題を解き、judge エージェントがスコアを比較して勝者を宣言する (N+1)-agent DAG。53 tests PASSED。~~ |
| ~~低~~ | ~~**ワークフローテンプレートライブラリ (`examples/workflows/generic/`)**~~ | ~~進行中 v1.2.28-v1.2.29 — `POST /workflows/from-template` (v1.2.28) + 10テンプレート追加済み: `tdd.yaml`, `debate.yaml`, `review.yaml` (v1.2.28), `agentmesh.yaml`, `refactor.yaml`, `redblue.yaml`, `clean-arch.yaml`, `delphi.yaml`, `deliberate.yaml`, `spec-first.yaml` (v1.2.29)。Martin Clean Architecture (2017) + Cockburn Hexagonal (2005) + SocraSynth arXiv:2402.06634 を設計根拠に採用。~~ |
| ~~新規~~ | ~~**`POST /workflows/mob-review`** — N 並列専門レビュアー + シンセサイザー DAG~~ | ~~完了 v1.1.20 — `MobReviewWorkflowSubmit`、4観点 (security/performance/maintainability/testing) 並列 + synthesizer。ChatEval (ICLR 2024) の独自ペルソナ知見を適用。41 tests PASSED。29/30 デモ PASS。~~ |
| ~~新規~~ | ~~**`POST /workflows/refactor`** — analyzer → refactorer → verifier 3エージェント リファクタリングパイプライン~~ | ~~完了 v1.2.27 — `RefactorWorkflowSubmit`, `refactor_goals` (6種), `{prefix}_analysis` + `{prefix}_refactored` + `{prefix}_verification` スクラッチパッド。60 tests PASSED。RefAgent arXiv:2511.03153 / RefactorGPT PeerJ cs-3257 / MUARF ICSE 2025 の設計原則を適用。~~ |
| ~~新規~~ | ~~**`POST /workflows/from-template` + `GET /workflows/templates` — YAML駆動ワークフローテンプレート実行**~~ | ~~完了 v1.2.28 — `WorkflowFromTemplateSubmit` スキーマ。`infrastructure/workflow_loader.py` (`load_workflow_template` / `render_template` / `list_templates`)。`examples/workflows/generic/` に `tdd.yaml` / `debate.yaml` / `review.yaml` 3テンプレート追加。`str.format_map()` による変数置換。61 tests PASSED。DESIGN.md §10.103。~~ |
| 低 | **`DECISION.md` 標準フォーマットとスクラッチパッド書き込み規約** — `debate` / `adr` / `delphi` / `redblue` / `socratic` の各ワークフローが出力を書き込む共通フォーマット (title / status / context / options_considered / decision / rationale / dissenting_opinions / consequences / references) を定義し、`GET /scratchpad/DECISION` で取得できるようにする | SocraSynth arXiv:2402.06634: 討論から「構造化された結論」を抽出する段階が必須。RT-AID ScienceDirect 2025: 各ラウンドの中間出力が最終合意文書の品質を高める。既存の `GET/PUT /scratchpad/{key}` (v0.16.0) + `context_files` (v0.11.0) を組み合わせて実現可能。各ワークフロー完成後に規約を策定する。 |
| 低 | **構造化デバッグ: トレースリプレイ CLI (`tmux-orchestrator replay`)** — `ResultStore` の JSONL + bus イベントログを組み合わせて過去の実行シーケンスを再現し、どのステップで失敗したかを特定できる CLI コマンドを追加する | LangGraph + LangSmith の replay 機能は業界標準デバッグ手法として認識 (LangChain docs 2025)。Galileo AI "Why Multi-Agent LLM Systems Fail" (2025): 「非決定的挙動のリプレイ不可」を主要問題として挙げる。チェックポイント永続化実装後の自然な拡張。 |

---

### アーキテクチャ・品質

| 優先度 | 課題 | 根拠 |
|--------|------|------|
| **高** | **クリーンアーキテクチャ層別ディレクトリ移行** — 現在フラットな `tmux_orchestrator/` 以下のモジュールを `domain/` / `application/` / `infrastructure/` / `monitoring/` / `adapters/` に段階的に移動する。後方互換シム（旧パスからの re-export）を置き、テストを壊さずに移行する。移行順: ① `domain/` (AgentStatus, Task, MessageType 抽出), ② `infrastructure/` (tmux_interface, messaging, worktree 等), ③ `application/` (orchestrator, registry, bus), ④ `adapters/` (config, factory, schemas, web, tui)。各移動は独立したコミット単位で行い、`uv run pytest tests/ -x -q` が常にグリーンであることを確認する | §2「ドメイン分離方針」および「目標とするクリーンアーキテクチャ層構造」参照。Martin "Clean Architecture" (2017): 依存は常にドメイン中心に向かう（Dependency Rule）。現状は orchestrator.py が context_monitor / drift_monitor / result_store 等のインフラを直接 import しており、依存方向が逆転している。移行することで各層の単体テストが高速化・安定化する。 |
| ~~**高**~~ | ~~**`domain/` 純粋型の抽出** — `AgentStatus`, `AgentRole` (from config.py / agents/base.py)、`Task` (from agents/base.py)、`MessageType` / `Message` (from bus.py) を `domain/agent.py` / `domain/task.py` / `domain/message.py` に移動。既存モジュールは `from tmux_orchestrator.domain.agent import AgentStatus` を re-export するシムに書き換える。domain/ は外部ライブラリを一切 import しない~~ | ~~完了 v1.0.11 — 1156 tests 全通過。Strangler Fig パターンで後方互換性を保ちつつ型を集約。`test_domain_purity.py` 20 tests で純粋性を継続保証。14/15 デモ PASS。~~ |
| ~~**高**~~ | ~~**`orchestrator.py` のインフラ依存を依存注入（DI）に置き換える**~~ | ~~完了 v1.0.35 — `ResultStoreProtocol`, `CheckpointStoreProtocol`, `AutoScalerProtocol` を `application/infra_protocols.py` に定義。`NullResultStore`, `NullCheckpointStore`, `NullAutoScaler` Null Object 実装を追加。`WorkflowManager`, `GroupManager` も constructor injection 対応。`reconfigure_autoscaler()` 公開メソッド追加。50 tests 追加 (32 protocol + 18 DI)。20/20 デモ PASS。~~ |
| ~~**高**~~ | ~~**OpenTelemetry GenAI Semantic Conventions 準拠トレース出力**~~ | ~~完了 v1.1.10 — `workflow_span()` + `RingBufferSpanExporter` + `GET /telemetry/spans` + `gen_ai.agent.description/version` + `BatchSpanProcessor` 本番パス + OTel→structlog 伝播。25テスト追加。25/25 デモ PASS。~~ |
| ~~**高**~~ | ~~**エージェントドリフト検出 (Agent Stability Index)**~~ | ~~完了 v1.0.9 — `DriftMonitor` (role/idle/length 3サブスコア)、`agent_drift_warning` イベント。34テスト。17/17デモPASS。~~ |
| 中 | **DriftMonitor — セマンティック類似度ベースの role_score 強化** — 現行のキーワードマッチを embedding コサイン類似度に置き換え、system_prompt と pane 出力の意味的乖離をより精密に測定する。`sentence-transformers` の軽量モデル (paraphrase-MiniLM-L6-v2, 22MB) を使用してランタイム外部 API 依存を回避する | Rath arXiv:2601.04170: ASI の Role Adherence 次元は「agent_id とタスクタイプの相互情報量」を使用。v1.0.9 のキーワードマッチは role_score = 1.0 に張り付く傾向（スコアが役割逸脱を検出しにくい）。embedding 距離により「形式は合っているが内容が違う」ドリフトを検出可能。 |
| ~~中~~ | ~~**Director の `agent_drift_warning` 購読による自動 re-brief**~~ | ~~完了 v1.1.18 — Orchestrator が bus の `agent_drift_warning` を購読し `_handle_drift_warning()` で自動 re-brief を注入。`drift_rebrief_enabled` / `drift_rebrief_cooldown` / `drift_rebrief_message` config フィールド。`GET /agents/{id}/drift-rebriefs` REST エンドポイント。32テスト追加 (2307→2339)。27/27デモPASS。~~ |
| 中 | **`UseCaseInteractor` 層の抽出** — `web/app.py` の FastAPI ハンドラーが `orchestrator.*` メソッドを直接呼ぶ箇所を `SubmitTaskUseCase`, `CancelTaskUseCase` 等の Use Case クラスに置き換え、Web 層とドメイン層の依存方向を逆転させる | Martin "Clean Architecture" §22: Use Case Interactor がアプリケーション固有ビジネスルールを保持し、Web/CLI/TUI のどのインターフェースからも同一ロジックを呼べる。現状は FastAPI ハンドラーにロジックが漏れており、TUI から同機能を使うときに重複する。`ProcessPort` 抽出後に実施するのが自然な順序。 |
| ~~中~~ | ~~**MIRIX 型エピソード記憶ストア**~~ | ~~完了 v1.0.28 — `EpisodeStore` (JSONL append-only), `GET/POST/DELETE /agents/{id}/memory`, 40 tests (unit 22 + API 18). 18/18 デモ PASS. writer→reviewer pipeline でクロスエージェントメモリ参照実証。~~ |
| 中 | **スライディングウィンドウ + 重要度スコアによるコンテキスト圧縮** — `ContextMonitor` が 75% 閾値を検出したとき、タスクプロンプトとの TF-IDF 類似度で行の重要度スコアを算出し、スコア下位 40% を削除したうえで圧縮済みコンテキストを注入する（単純な `/summarize` の上位互換） | Liu et al. "Lost in the Middle" TACL 2024 が中央部情報の忘却を実証。JetBrains Research "Cutting Through the Noise" (2025-12) が重要度スコアリングによるコスト削減を実証。現行の `/summarize` はプロンプトとの関連度を考慮せず一律圧縮する。 |
| 中 | **エージェント状態機械の Hypothesis ステートフルテスト拡張** — `AgentStatus` の遷移 (IDLE→BUSY→IDLE/ERROR/DRAINING) を `RuleBasedStateMachine` (Hypothesis) でモデル化し、任意の割り込み・タイムアウト・リカバリシーケンスを自動生成してデッドロック・不変量違反を検証する | Hypothesis `stateful` モジュール (QuickCheck ICFP 2000 由来)。v0.4.0 で導入した PBT はステートレスなプロパティテストのみで、状態遷移シーケンスのテストは未カバー。§10.6 の推奨事項（v0.10.0 候補）として既提案。本番コード変更なしで追加可能。 |
| 中 | **P2P 許可テーブルの TLA+ 形式仕様化** — `_is_hierarchy_permitted()` と `p2p_permissions` の状態遷移を TLA+ で記述し、TLC model checker で「メッセージがルーティングループに入らない」「P2P 許可のない経路へは届かない」を全状態空間で検証する | Lamport "Specifying and Verifying Systems with TLA+" (2024) および AWS による TLA+ 活用実績。現行の `_forwarded` フラグでループを防いでいるが、将来の変更で迂回経路が生まれるリスクを全状態空間の property 検証で事前に検出できる。形式仕様エージェントステップ（機能・ワークフロー表参照）との相補関係。 |
| 低 | **`AgentRegistry` の完全分離と依存注入** — `registry.py` の `AgentRegistry` はすでに抽出済みだが、`Orchestrator` がまだ内部フィールドを直接参照している箇所を依存注入パターンに整理し、God Object 化を完全に解消する | §10.5 / §11 旧テーブルで提案済み。`AgentRegistry` モジュール (`registry.py`) は v0.3.0 時点で存在するが、Orchestrator の全フィールドアクセスが委譲パターンに揃っていない。`UseCaseInteractor` 抽出と並行して実施すると効率的。 |

---

### デモシナリオ候補

| 優先度 | シナリオ | パターン |
|--------|----------|---------|
| ~~高~~ | ~~**TDD ワークフローデモ**~~ | ~~完了 v0.36.0 (タイムアウト 300s → 次回 900s で再試行)~~ |
| ~~高~~ | ~~**Debate ワークフローデモ**~~ | ~~完了 v0.37.0 — SQLite vs PostgreSQL、2ラウンド、ALL 27 CHECKS PASSED。Advocate(PG)勝利~~ |
| 高 | **ADR 自動生成デモ — "SQLite vs PostgreSQL 選択"** | `POST /workflows/adr`、proposer+reviewer+synthesizer、`DECISION.md` がスクラッチパッドに書き出されることを実証。past ADR を `context_files` で参照 |
| 中 | **AgentMesh 型 4ロール開発パイプラインデモ (Planner → Coder → Debugger → Reviewer)** | `examples/agentmesh_config.yaml` + `POST /workflows`、1つの機能要求から完全実装+レビューまでを4エージェントが自動処理。Workflow DAG + tags (v0.18.0) + target_group (v0.31.0) で宣言的に記述 |
| 中 | **Delphi 型合意形成デモ — "マイクロサービス vs モノリス"** | `POST /workflows/delphi`、5ペルソナエージェント、3ラウンド、各ラウンドの `delphi_round_{n}.md` 生成と最終 `consensus.md` を実証 |
| 中 | **Red Team / Blue Team セキュリティレビューデモ** | `POST /workflows/redblue`、blue-team が FastAPI エンドポイントを実装、red-team が入力検証・認証・レートリミットの欠陥を列挙、arbiter がリスク評価レポートを生成 |
| 低 | **Codified Context + PairCoder デモ — 長期プロジェクト規約維持** | `.claude/specs/` に規約 YAML を配置 → `POST /workflows/pair` で Navigator+Driver が5セッション連続で実装 → 全セッションにわたり規約違反ゼロを実証 |


---

### 新規観点 A：isolation: false の改善とエージェント間ファイル分離

> **背景**: 現状 `isolate: false` のエージェントは全員が同一の作業ディレクトリを共有するため、コンテキストファイル (`__orchestrator_context__.json`、`__orchestrator_api_key__` 等) の競合が発生しやすい。v1.0.19 でエージェントIDによるファイル名名前空間化 (`__orchestrator_context__{agent_id}__.json`) を導入したが、根本的な解決策ではない。

| 優先度 | 課題 | 解決アプローチ |
|--------|------|----------------|
| **高** | **エージェント設定ファイルの分離** — `isolate: false` 時でも各エージェントが独立した設定ファイルを持てるよう、エージェントごとのサブディレクトリ (`{cwd}/.agent/{agent_id}/`) を作成し、コンテキストファイル・API キーファイル・フック設定を格納する。既存のエージェントIDによる名前空間化を一歩進めた構造的分離 | `ClaudeCodeAgent.start()` でエージェントサブディレクトリを事前作成。`_copy_commands(cwd)` を各エージェントサブディレクトリにコピーする形に変更。`settings.local.json` もサブディレクトリ内に配置 |
| **高** | **エージェント × worktree 対応付けによる同期** — `isolate: false` の代替として、エージェントを軽量 worktree に紐づけつつ `main` ブランチとの `git pull --rebase` / `git push` を明示的に実行できる仕組みを追加する。エージェントが変更を `main` にマージしたいタイミングで `/sync-to-main` スラッシュコマンドを呼び、オーケストレーターが `git merge --squash` を実行する | `POST /agents/{id}/sync` エンドポイントを追加。`WorktreeManager.sync_to_main(agent_id)` が `git fetch` → `git rebase origin/main` → `git push` を順に実行。競合時は `sync_failed` STATUS イベントを発行 |
| **中** | **worktree 間ファイル共有チャネル** — `isolate: true` 環境で複数エージェントが共有ファイルを読み書きする必要がある場合の標準パターンを提供する。現状はスクラッチパッド (key-value) か P2P メッセージしかないが、ファイルパスベースの共有 (`GET /worktrees/{agent_id}/files/{path}`、`PUT /worktrees/{agent_id}/files/{path}`) を追加してバイナリ成果物の受け渡しを可能にする | `web/routers/agents.py` に worktree ファイル操作エンドポイントを追加。読み取りは任意エージェントから可能、書き込みは自身のworktreeのみ |
| **低** | **isolation 設計ガイド** — `isolate: true` / `isolate: false` / worktree 同期の3方式を比較したベストプラクティスガイドを CLAUDE.md に追記する。各方式の用途・制約・設定例を表形式で整理する | CLAUDE.md の "Worktree Isolation" セクションを拡充 |

---

### 新規観点 B：ワークフロー・ストラテジーの細粒度パラメータ化

> **背景**: 現状の `PhaseSpec` は `pattern` (single/parallel/competitive/debate) と `agent_count`、`debate_rounds` しかパラメータを持たない。実際のワークフローでは各ストラテジーに固有の詳細設定（タイムアウト・評価基準・フィードバック方式など）を指定したいケースが多い。`domain/phase_strategy.py` にストラテジーが整理された今、パラメータ体系を充実させる好機。

| 優先度 | 課題 | 詳細 |
|--------|------|------|
| ~~**高**~~ | ~~**StrategyConfig 値オブジェクト**~~ | ~~完了 v1.1.31 — `SingleConfig`・`ParallelConfig`・`CompetitiveConfig`・`DebateConfig` を stdlib `@dataclass` で実装 (domain purity 維持)。`StrategyConfig = Union[...]` 型エイリアス。`PhaseSpecModel` + Pydantic `*ConfigModel` 在 `web/schemas.py`。28/28 デモ PASS。29テスト追加 (2579→2608)。~~ |
| ~~**高**~~ | ~~**フェーズごとのタイムアウト設定**~~ | ~~完了 v1.1.31 — `PhaseSpec.timeout: int \| None = None` + `Task.timeout` + `GET /tasks/{id}` 全パスで `timeout` 露出。`_task_timeout` dict で history にも保存。28/28 デモ PASS。~~ |
| **中** | **Competitive ストラテジーの評価基準カスタマイズ** — judge エージェントへのプロンプトに評価基準 (`criteria`) と出力形式 (`output_format`) を注入できるようにする。現状は judge プロンプトが固定文字列で、「コードの正確性」「パフォーマンス」「可読性」等を重み付け評価できない | `CompetitiveConfig.judge_prompt_template: str` フィールド。`{criteria}`, `{solutions}`, `{context}` プレースホルダーを置換してジャッジタスクプロンプトを生成 |
| **中** | **Debate ストラテジーの動的終了条件** — 固定ラウンド数ではなく、合意形成条件（例: judge が "CONSENSUS_REACHED" を含む出力をした場合）で討論を早期終了できる仕組みを追加する | `DebateConfig.early_stop_signal: str \| None`。各ラウンド後に judge の scratchpad エントリを検査し、シグナルが見つかれば残りラウンドをスキップ |
| **中** | **ワークフローテンプレートのパラメータ継承** — `examples/workflows/*.yaml` に `defaults:` セクションを追加し、全フェーズ共通のデフォルト設定（timeout, required_tags パターン等）を一箇所で指定できるようにする | YAML スキーマ拡張。`POST /workflows` が `defaults` を各フェーズに merge する前処理を追加 |
| **低** | **PhaseSpec の条件分岐** — `condition: "scratchpad_key_exists('result')"` のような条件式でフェーズをスキップできる仕組みを追加する。決定論的なパイプラインに留まらず、前フェーズの結果に応じてフェーズを動的にスキップできる | `PhaseSpec.skip_condition: str \| None`。DAG 展開時に条件を評価し、`True` の場合はフェーズを `skipped` 状態にして依存タスクを即座に解放 |

---

### 新規観点 C：メッセージ格納フォルダの移動

> **背景**: 現状のメールボックスディレクトリはデフォルトが `~/.tmux_orchestrator/` (ホームディレクトリ下のグローバル領域) で、複数プロジェクトを同一マシンで実行した場合にメッセージが混在する。また `isolate: true` のエージェントはそれぞれ独立した worktree を持つため、メッセージもプロジェクト・セッションスコープで管理すべきである。

| 優先度 | 課題 | 解決アプローチ |
|--------|------|----------------|
| ~~**高**~~ | ~~**メールボックスをプロジェクトスコープに移動**~~ | ~~完了 v1.1.30 — `OrchestratorConfig.mailbox_dir` デフォルト変更 (`~/.tmux_orchestrator` → `.orchestrator/mailbox`)。`load_config(path, cwd=None)` に `cwd` パラメータ追加。`_resolve_dir()` ヘルパー。`result_store_dir` / `checkpoint_db` も同様に解決。15テスト追加 (2564→2579)。17/17 デモ PASS。~~ |
| **高** | **メールボックスをセッション単位で分離** — 同一プロジェクト内で複数のオーケストレーターセッションが並行動作する場合に備え、`{mailbox_dir}/{session_name}/{agent_id}/inbox/` の階層構造を維持しつつ、`session_name` をユニークな値（UUID または設定値）にする。現状は固定文字列 `"orchestrator"` がデフォルトで複数セッション間でメッセージが混在する可能性がある | `OrchestratorConfig.session_name` が未設定の場合、`f"session_{uuid4().hex[:8]}"` を自動生成する。設定ファイルに `session_name:` を明示することで再現性のある名前も使える |
| **中** | **メールボックスの自動クリーンアップ** — セッション終了時 (`Orchestrator.stop()`) にメールボックスディレクトリを自動削除するオプションを追加する。現状は明示的にディレクトリを削除しないため、長期間運用すると未読メッセージが蓄積する | `OrchestratorConfig.mailbox_cleanup_on_stop: bool = True`。`Orchestrator.stop()` で `shutil.rmtree(mailbox_dir / session_name)` を実行 |
| **低** | **メールボックスパスを CLAUDE.md エージェントガイドに明記** — エージェントが自分のメールボックスパスを確認できるよう、`__orchestrator_context__{agent_id}__.json` の `mailbox_dir` フィールドの説明と、実際のパス構成例 (`{mailbox_dir}/{session_name}/{agent_id}/inbox/`) を CLAUDE.md の "Receiving Messages" セクションに追記する | CLAUDE.md 更新のみ |

---

## §10.103 — v1.2.28 研究記録: YAML駆動ワークフローテンプレート実行

### 選択根拠

ユーザーから「ワークフローを追加するためにコードを書かなければならないのか」という質問を受けた。現状のアーキテクチャでは各ワークフロー（tdd, debate, refactor等）に専用のPythonエンドポイントが必要で、新規ワークフロー追加には必ずコード変更が伴う。この制約を解消するために汎用テンプレート実行エンドポイントを実装した。

v1.2.27（refactor ワークフロー）の直後として優先度が高い。既存の `phases=` モードの `WorkflowSubmit` をそのまま活用でき、変数置換という薄い層を追加するだけで実現できる。

### 調査結果

**1. Argo Workflows パラメータバインディング**
- URL: https://argo-workflows.readthedocs.io/en/latest/walk-through/parameters/ (2025)
- `{{inputs.parameters.message}}` 構文でテンプレートパラメータを埋め込む。WorkflowTemplate は再利用可能なテンプレートとして定義し、Workflow から `templateRef` で参照する。
- 変数はダブルブレース `{{variable}}` で、本実装の `{variable}` (Python str.format_map) とは異なる。

**2. Azure Pipelines テンプレート変数置換**
- URL: https://learn.microsoft.com/en-us/azure/devops/pipelines/process/templates (2025)
- `${{ parameters.x }}` 構文。テンプレートファイルで `parameters:` セクションを宣言し、各パラメータに型・デフォルト値・必須フラグを付与できる。
- 本実装の `variables:` セクション（`required: true/false`、`default: ""`）はこのパターンを参考にした。

**3. ワークフロー設定駆動 vs コード駆動の比較**
- URL: https://procycons.com/en/blogs/workflow-orchestration-platforms-comparison-2025/ (2025)
- Kestra（YAML設定駆動）vs Temporal（コード駆動）の比較。YAML駆動は「ドキュメントとしての設定ファイル」を実現し、非エンジニアもワークフローを追加・修正できる。
- 複雑な変換ロジックはコードで実装し、オーケストレーション定義はYAMLに委ねるハイブリッドアプローチが推奨されている（Cloud Workflows best practices）。

### 設計決定

**変数置換エンジン**: Jinja2 ではなく Python 標準ライブラリの `str.format_map()` を採用。
- Jinja2 は追加依存関係が必要で、テンプレートループ・条件分岐などの複雑な機能は今回不要。
- `str.format_map()` はカスタム辞書クラス（`_MissingKeyMap`）で未定義キーを即座に ValueError に変換できる。
- プロンプト文字列中の `{{literal brace}}` が Jinja2 では `{{ '{{' }}` のようにエスケープが必要だが、format_map では `{{` が `{` に変換されるため既存プロンプトとの互換性が高い。

**テンプレート配置**: `examples/workflows/generic/` サブディレクトリに phase-based テンプレートを配置。
- ルートレベル (`examples/workflows/`) の既存 YAML は専用エンドポイント用のパラメータファイルで、`phases:` キーを持たない。
- `generic/` サブディレクトリを検索優先することで、新旧フォーマットが共存できる。

**CleanArchitecture 層配置**: `infrastructure/workflow_loader.py` に配置（`application/` ではなく）。
- `yaml` ライブラリは `application/` 層では禁止されている（`test_application_purity.py` の規則）。
- YAMLファイル読み込みはI/O操作なのでインフラ層が適切（Martin "Clean Architecture" 2017: 外部リソースアクセスは境界の外側）。

### 実装サマリー

- `infrastructure/workflow_loader.py`: `WorkflowTemplate`, `VariableSpec`, `load_workflow_template()`, `render_template()`, `list_templates()`
- `web/schemas.py`: `WorkflowFromTemplateSubmit` スキーマ追加
- `web/routers/workflows.py`: `GET /workflows/templates` (必ず `/workflows/{id}` より前に登録)、`POST /workflows/from-template` 追加
- `web/app.py`: `_app_templates_dir` を解決して `build_workflows_router()` に渡す
- `examples/workflows/generic/tdd.yaml`, `debate.yaml`, `review.yaml`: 3テンプレート追加
- **61 tests** in `tests/test_workflow_from_template.py`

---

## §10.104 — v1.2.29 研究記録: YAML汎用ワークフローテンプレートライブラリ拡張 + docs/research/ 再編

> 詳細記録: [docs/research/v1.2.29.md](docs/research/v1.2.29.md)

### 選択根拠

v1.2.28 で `POST /workflows/from-template` エンドポイントと最初の3テンプレートを実装。次の自然な拡張はテンプレートライブラリを充実させることで、YAMLのみで追加できる仕組みの価値を実証すること。

**選択しなかったもの**: DriftMonitor embedding 強化（sentence-transformers 依存）、TLA+ 形式仕様化（研究色が強い）

### 調査結果サマリー

**1. Clean Architecture × マルチエージェント**
- Martin "Clean Architecture" (2017): 4同心円層（Entities → Use Cases → Interface Adapters → Frameworks）、依存方向は常に内側へ。
- Cockburn "Hexagonal Architecture / Ports and Adapters" (2005): アプリケーションコアと外部システムをPortとAdapterで分離。
- 直接対応: architect エージェントがポートスタブを設計 → domain/usecase/adapter エージェントが並列実装 → verifier が依存方向を静的解析確認。

**2. SocraSynth / ソクラテス的対話**
- Chang "SocraSynth: Multi-LLM Reasoning with Conditional Statistics" arXiv:2402.06634 (Stanford, 2024)。
- 2フェーズ: knowledge generation → reasoning evaluation（Socratic cross-examination）。
- `deliberate.yaml` の questioner/responder/synthesizer トリオが SocraSynth のフレームワークに対応。

**3. ワークフロー YAML 変数置換ベストプラクティス**
- GitHub Actions YAML anchors (Sep 2025): `${{ variables.x }}` 構文が標準化。
- Azure Pipelines: `required: true/false` + `default:` パターンが v1.2.28 設計と完全一致。
- Google Cloud Workflows: YAML駆動 vs コード駆動のハイブリッドアプローチが業界推奨。

### 実装サマリー

**7テンプレート追加** (`examples/workflows/generic/`):

| テンプレート | パターン | フェーズ数 |
|------------|---------|---------|
| `agentmesh.yaml` | 4段階パイプライン | 4 (sequential) |
| `refactor.yaml` | 動作保全リファクタリング | 3 (sequential) |
| `redblue.yaml` | 敵対的セキュリティレビュー | 3 (sequential) |
| `clean-arch.yaml` | Clean Architecture 層分解 | 5 (fan-out + fan-in) |
| `delphi.yaml` | デルファイ合意形成 | 4 (2-round) |
| `deliberate.yaml` | ソクラテス的対話 | 3 (sequential) |
| `spec-first.yaml` | 仕様駆動開発 | 3 (sequential) |

**docs/research/ 再編**: `docs/research-log.md` (9646行) を `docs/research/` フォルダに分割。
- `pre-v1.2.md`: v0.3.0〜v1.1.44の全エントリ（アーカイブ）
- `v1.2.{N}.md`: バージョン別研究記録（v1.2.1〜v1.2.29）
- `README.md`: インデックス

**28+ tests** added in `tests/test_workflow_from_template.py`

---
