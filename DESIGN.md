# TmuxAgentOrchestrator — 設計資料

> 本ドキュメントは設計上の意思決定と根拠を記録するものです。
> 開発の基礎検討資料として、指示・調査・実装の経緯をまとめます。

---

## 目次

1. [プロジェクトの目的と基本方針](#1-プロジェクトの目的と基本方針)
2. [アーキテクチャ設計原則](#2-アーキテクチャ設計原則)
3. [tmux 階層マッピング](#3-tmux-階層マッピング)
4. [エージェント間通信の設計](#4-エージェント間通信の設計)
5. [コンテキストエンジニアリング](#5-コンテキストエンジニアリング)
6. [テスト駆動開発 (TDD) の統合](#6-テスト駆動開発-tdd-の統合)
7. [エージェントワークフローとスキル](#7-エージェントワークフローとスキル)
8. [参考文献](#8-参考文献)
9. [実装履歴](#9-実装履歴)
10. [今後の課題](#10-今後の課題)

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

---

## 4. エージェント間通信の設計

### 通信フロー

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

### ポーリング不要の設計

- エージェントはメッセージをポーリングしない
- `notify_stdin(f"__MSG__:{msg.id}")` が tmux pane にキーを送信
- エージェントはこれを受けて `/check-inbox` → `/read-message` を実行

### 通信許可ルール

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

## 5. コンテキストエンジニアリング

### 参考: Anthropic Engineering Blog (2025)

> "Find the smallest set of high-signal tokens that maximize the likelihood of your desired outcome."

**コンテキスト rot の問題**: コンテキストウィンドウが大きくなるほど、情報の再現精度が下がる。

### 本フレームワークでの対策

| 手法 | 実装 |
|------|------|
| **コンテキスト局所化** | 各エージェントに個別 `CLAUDE.md` を生成（役割・通信プロトコル・規約） |
| **構造化ノート** | `NOTES.md` テンプレートを自動生成（スクラッチパッド） |
| **コンテキスト圧縮** | `/summarize` コマンドで作業状態を NOTES.md に凝縮 |
| **タスク計画の外部化** | `/plan` コマンドで `PLAN.md` に受け入れ基準を書き出す |
| **サブエージェント分業** | `/delegate` で集中した文脈を持つサブエージェントに分散 |

### AgentConfig のコンテキスト設定

```yaml
agents:
  - id: specialist-agent
    type: claude_code
    role: worker
    system_prompt: |
      You specialise in database schema design.
      Focus only on the data model; delegate API concerns to sibling agents.
    context_files:
      - docs/schema.md
      - docs/data-dictionary.md
```

### 生成されるファイル（ワークツリー内）

```
{worktree}/
├── CLAUDE.md                    # 役割・通信プロトコル・規約（自動生成）
├── NOTES.md                     # 構造化ノート（自動生成テンプレート）
├── PLAN.md                      # タスク計画（/plan コマンドで生成）
└── __orchestrator_context__.json # オーケストレーター接続情報
```

---

## 6. テスト駆動開発 (TDD) の統合

### 参考: Kent Beck / Takuto Wada

TDD の本質は「テストを**仕様**として先に書き、それをパスする最小の実装を作る」こと。
AI エージェントにとって TDD は「ガードレール」として機能し、
実装の暴走を防ぎ保守性を保証する。

### Red → Green → Refactor サイクル

```
1. SPEC   : /plan で受け入れ基準を明文化
2. RED    : /tdd で失敗するテストを書く（仕様の形式化）
3. GREEN  : 最小実装でテストをパスさせる
4. REFACTOR: テストを保ちながらコードを改善
5. REPORT  : /progress で親エージェントに報告
```

### `/tdd` コマンドの役割

- TDD サイクルの各フェーズを明示的に案内
- チェックリストでフェーズ完了を確認
- NOTES.md への記録を促す

---

## 7. エージェントワークフローとスキル

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

## 8. 参考文献

### コンテキストエンジニアリング

- **Anthropic Engineering Blog** — [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) ★推奨
  - 核心概念: 最小高信号トークン集合、コンパクション、構造化ノート、サブエージェント分業
- **Prompt Engineering Guide** — [Context Engineering Guide](https://www.promptingguide.ai/guides/context-engineering-guide)
- **arXiv 2510.04618** — [Agentic Context Engineering](https://arxiv.org/abs/2510.04618)

### テスト駆動開発

- **Kent Beck** — _Test-Driven Development: By Example_ (原典)
- **Agile Journey (Uzabase)** — [AIエージェント時代のテスト駆動開発](https://agilejourney.uzabase.com/entry/2025/08/29/103000) ★推奨（和田卓人/安井力による技術的議論）
- **KINTO Tech Blog** — [TDD × AI](https://blog.kinto-technologies.com/posts/2025-04-02-tdd_x_ai/)

### マルチエージェントオーケストレーション

- **Claude Code 公式ドキュメント** — [Agent Teams](https://code.claude.com/docs/en/agent-teams) ★推奨
- **OpenAI Agents SDK** — [Agent Orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
- **AWS** — [CLI Agent Orchestrator](https://aws.amazon.com/blogs/opensource/introducing-cli-agent-orchestrator-transforming-developer-cli-tools-into-a-multi-agent-powerhouse/)

---

## 9. 実装履歴

### 2026-03-04 v1: 基礎バグ修正と構造改善

- `POST /agents` の template_id バグ修正（agent_type/command → template_id）
- `_spawn_subagent` の role 引き継ぎバグ修正
- `asyncio.get_event_loop()` → `get_running_loop()` 全面置換
- `AgentConfig` に `task_timeout`, `command` を追加
- 親子関係トラッキング (`_agent_parents`, `list_agents()` に `parent_id`)
- Director バッファリングを実際の出力テキストを含む形に改善
- tmux 階層化: `new_subpane()` 追加、サブエージェントが親ウィンドウ内に生成される
- FastAPI `on_event` 非推奨 → `lifespan` に移行

### 2026-03-04 v2: 通信制御の階層化

- P2P ルーティングに階層ベースの自動許可ルールを追加
  - 親↔子・兄弟間: 自動許可
  - 異なるブランチ: `p2p_permissions` による明示的設定が必要
- `register_agent(parent_id=)` パラメータ追加
- テスト 6 本追加（59 テスト合格）

### 2026-03-05 v0.11.0: context_files auto-copy + Web UI hierarchy tree

- `ClaudeCodeAgent._copy_context_files(cwd)` — context_files を worktree に実コピー (`shutil.copy2`)
  - 欠損ファイルは警告のみ（例外なし）
  - `context_files_root` パラメータで解決ベースパスを注入
- `factory.py` / `orchestrator._spawn_subagent()` に `context_files_root=Path.cwd()` を渡す
- `GET /agents/tree` — `parent_id` ベースの nested JSON tree エンドポイント
- `_build_agent_tree()` ヘルパー — flat list → nested tree 変換（サーバーサイド）
- Web UI Agents パネルに List/Tree トグル追加
- 純 CSS ツリーレンダラー（D3 依存なし）— `refreshAgentTree()` で取得・表示
- テスト 15 本追加（171 テスト合格）
- GitHub Issue #1, #2 クローズ

### 2026-03-04 v3: コンテキストエンジニアリング + TDD 統合

- `AgentConfig` に `system_prompt`, `context_files` 追加
- `ClaudeCodeAgent.start()` が各エージェントの CLAUDE.md と NOTES.md を自動生成
- スラッシュコマンド 5 本追加: `/plan`, `/tdd`, `/progress`, `/summarize`, `/delegate`

---

## 10. 調査記録 (2026-03-05)

> 自律開発における調査の根拠を記録する。
> 本セクションは調査のたびに追記・更新する。

### 10.1 参考文献・調査観点

以下の観点から4エージェント並行調査を実施（2026-03-05）:

| 観点 | 主要参考文献 |
|------|------------|
| LLMマルチエージェントのベストプラクティス | arXiv 2503.13657 (Why Do Multi-Agent LLM Systems Fail?), Augment Code, Azure AI Agent Design Patterns |
| DDD / クリーンアーキテクチャ | Evans "Domain-Driven Design" (2003), Martin "Clean Architecture" (2017) |
| 形式的手法 / 型安全 | TLA+ (Lamport), Lean 4, Rust ownership model, Python Hypothesis |
| オブザーバビリティ / SRE | Google SRE Book, AWS exponential backoff guide (Polly-style jitter), Prometheus text format |

### 10.2 主要な知見と対応

| 知見 | 根拠 | 実装方針 |
|------|------|----------|
| マルチエージェント障害の41.8%は仕様の曖昧さが原因 | arXiv 2503.13657 | `AgentRole` enum化、`TaskSpec`のメタデータ構造化 |
| メッセージの型安全性がない→CONTROL誤送信時サイレント失敗 | Pydantic既存、bus.pyのpayloadがdict | `schemas.py`でPydantic型定義 |
| `_set_idle()`が必ずしもSTATUSイベントを発行しない | `base.py`コード確認 | `_set_idle()`内で常に`agent_idle`発行 |
| Directorへの結果バッファリングが文字数カット | `orchestrator.py:_buffer_director_result()` | テール抽出（最終N行）に変更 |
| ERRORエージェントに自動リカバリなし | Issue #3 | サーキットブレーカー + リカバリループ実装 |
| バスのキュー溢れがサイレント | `bus.py:91-95` | STATUS/queue_overflowイベント発行 |
| `AgentRole`が`str`で型安全性なし | DDD「ユビキタス言語」 | `AgentRole(str, Enum)`定義 |
| OrchestratorのプライベートフィールドをWeb層が直接アクセス | ヘキサゴナルアーキテクチャ違反 | `flush_director_pending()`, `get_director()`メソッド追加 |
| BUSY状態のエージェントに無制限タイムアウトが発生しうる | 完了検知のパターン不一致 | `max_settle_wait`デッドライン追加 |

### 10.3 実装計画 (v0.3.0)

**イテレーション1 — 即座に改善効果が高いもの（全てv0.3.0対象）:**

| # | 改善内容 | 複雑度 | 変更ファイル |
|---|----------|--------|------------|
| R1 | `AgentRole` enum化 | 低 | `config.py`, 各消費箇所 |
| R2 | `_set_idle()`で常に`agent_idle` STATUS発行 | 低 | `agents/base.py` |
| R3 | `_buffer_director_result()`テール抽出 | 低 | `orchestrator.py` |
| R4 | バスQueueFullをSTATUSイベントとして発行 | 低 | `bus.py`, `orchestrator.py` |
| R5 | Orchestratorに`flush_director_pending()`, `get_director()` | 低 | `orchestrator.py`, `web/app.py` |
| R6 | サーキットブレーカー (`circuit_breaker.py`) | 中 | 新規 + `orchestrator.py`, `config.py` |
| R7 | `Task.trace_id`フィールド追加 | 低 | `agents/base.py` |
| R8 | `/healthz`, `/readyz`エンドポイント | 低 | `web/app.py` |
| R9 | `AgentStatus`イベント駆動dispatch (sleep 0.2 → Event) | 中 | `orchestrator.py` |

### 10.4 実装済み改善 (v0.3.0 / v0.4.0)

| 実装内容 | バージョン | 根拠 |
|----------|-----------|------|
| `AgentRole(str, Enum)` — ユビキタス言語 | v0.3.0 | DDD原則 |
| サーキットブレーカー (CLOSED→OPEN→HALF_OPEN) | v0.3.0 | Martin Fowler "Release It!" Ch.5 |
| バスQueueFull → drop_count記録 | v0.3.0 | オブザーバビリティ |
| `/healthz`, `/readyz`ヘルスプローブ | v0.3.0 | SRE ベストプラクティス |
| `Task.trace_id` — クロスエージェント相関 | v0.3.0 | 分散トレーシング原則 |
| `get_director()`, `flush_director_pending()` | v0.3.0 | Clean Architecture (ヘキサゴナル境界) |
| `_buffer_director_result()` テール抽出 (最終40行) | v0.3.0 | コンテキストエンジニアリング |
| `_set_idle()` 常に `agent_idle` STATUS発行 | v0.3.0 | イベント一貫性 |
| デッドレターキュー (`dlq_max_retries`) | v0.4.0 | SRE DLQパターン |
| Pydantic型付きペイロードスキーマ (`schemas.py`) | v0.4.0 | 型安全性・ドキュメント化 |
| Hypothesis property-based tests | v0.4.0 | 形式的テスト手法 (TLA+精神) |

### 10.5 次回イテレーション候補 (v0.5.0)

優先度順:

| 改善内容 | 優先度 | 根拠 |
|----------|--------|------|
| `AgentRegistry`抽出 — Orchestratorのゴッドオブジェクト解消 | 高 | DDD Aggregate原則、研究調査 |
| `SystemFactory`抽出 — main.pyのワイヤリング分離 | 高 | Layered Architecture |
| 構造化JSONログ (trace_idコンテキスト付き) | 中 | SREオブザーバビリティ (分散トレーシング) |
| タスク依存関係 (`depends_on`) + Workflow原始型 | 中 | Saga/ワークフローパターン |
| `ProcessAdapter`ポート — ClaudeCodeAgentのtmux抽象化 | 低 (大規模) | ヘキサゴナルアーキテクチャ |

### 10.6 調査記録 (v0.9.0 完了後, 2026-03-05)

v0.9.0 完了後に実施した調査。以下5テーマを調査エージェントが分析。

#### 調査テーマと主要知見

| テーマ | パターン名 | 根拠文献 | 新依存関係 | 実装規模 |
|--------|-----------|---------|-----------|---------|
| ウォッチドッグループ | Heartbeat / Watchdog Timer | "Release It!" Ch.5 (Nygard, 2018) | なし | 単一バージョン |
| 冪等キー | Idempotent Receiver (EIP) | Hohpe & Woolf (2004) p.349 | なし | 単一バージョン |
| Prometheus メトリクス | USE Method + prometheus_client | SRE Book; Gregg (2012) | あり | 複数バージョン |
| ステートフル仮説テスト | RuleBasedStateMachine | Hypothesis; QuickCheck ICFP 2000 | なし (dev済み) | 単一バージョン |
| タスクスーパービジョン | Supervisor Pattern | Erlang OTP; Hattingh (2020) Ch.4 | なし | 単一バージョン |

#### 推奨実装順序 (v0.10.0)

1. **タスクスーパービジョン** — `_dispatch_loop` / `_route_loop` のクラッシュリカバリを先行。他すべての改善の基盤
2. **ウォッチドッグループ** — `AgentRegistry._busy_since` + `find_timed_out_agents()` → 既存サーキットブレーカーに統合
3. **冪等キー** — `submit_task(idempotency_key=)` + `_idempotency_keys: dict[str,str]` + 1時間TTL
4. **ステートフル仮説テスト** — `tests/test_bus_stateful.py` の `BusStateMachine` (本番コード変更なし)
5. **Prometheus メトリクス** (別バージョン) — `metrics.py` モジュール新設 + `prometheus-fastapi-instrumentator`

#### 主要設計決定

- **ウォッチドッグは RESULT を publish する** — `asyncio.Task.cancel()` ではなく `MessageType.RESULT(error="watchdog_timeout")` を publish → 既存の `_route_loop` → `registry.record_result()` → サーキットブレーカーパスを再利用
- **スーパービジョンは `supervised_task()` ラッパー** — `asyncio.TaskGroup` (crash-together) ではなく指数バックオフ付き独立再起動。`CancelledError` は常に伝播 (再起動しない)
- **冪等キーは in-process のみ** — プロセス再起動で保護ウィンドウは消える。永続化は要件になった時点で SQLite 追加 (過度な設計をしない)
- **Prometheus は別バージョン** — 新依存関係 (`prometheus-fastapi-instrumentator`, `prometheus-client`) は独立 PR が適切。`/metrics` はデフォルト無認証 → ポートバインディング要件が増える


### 10.7 調査記録 (v0.11.0, 2026-03-05)

#### 実装: context_files auto-copy (Issue #1) と hierarchy tree view (Issue #2)

**調査観点:**

| テーマ | 参考文献 |
|--------|---------|
| git worktree + context isolation | [Git worktrees for parallel AI agents (Upsun, 2025)](https://devcenter.upsun.com/posts/git-worktrees-for-parallel-ai-coding-agents/) |
| Agent hierarchy tree visualization | [d3-hierarchy (Observable, 2025)](https://d3js.org/d3-hierarchy/tree) |
| Rate limiting/backpressure in LLM systems | [Rate Limiting and Backpressure for LLM APIs (dasroot.net, 2026)](https://dasroot.net/posts/2026/02/rate-limiting-backpressure-llm-apis/) |
| WebSocket/SSE real-time push | [Real-Time Features in FastAPI (Python in Plain English, 2025)](https://python.plainenglish.io/real-time-features-in-fastapi-websockets-event-streaming-and-push-notifications-fec79a0a6812) |

**主要知見:**

1. **context_files**: git worktree 内の各エージェントは独立したファイルシステムを持つ。context_files の実コピー（`shutil.copy2`）により、エージェント起動時に関連ドキュメントが worktree に配置される。欠損ファイルは警告のみ（例外を投げない）— robustness が優先。

2. **Hierarchy tree**: エージェント一覧を flat list → nested JSON tree に変換する `_build_agent_tree()` を実装。`/agents/tree` REST エンドポイントとして公開。Web UI は List/Tree トグルでこれを切り替え表示。純 CSS + HTML でツリーをレンダリング（D3 等の外部依存なし）。

**設計決定:**

- `context_files_root` を独立パラメータとして渡す — `ClaudeCodeAgent` 自体はパス解決ロジックを知る必要なし; factory/orchestrator が `Path.cwd()` を渡す。
- ツリーはサーバーサイドで `parent_id` から構築 → クライアントは単純なレンダリングのみ
- D3 は embedding に大きすぎるため純 CSS ツリー + Vanilla JS で実装

### 10.8 調査記録 (v0.12.0, 2026-03-05)

#### 実装: ERROR 状態自動リカバリ (Issue #3) と SSE プッシュ通知

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| ERROR 自動リカバリ | Erlang OTP `restart_one_for_one` | [Erlang OTP supervisor behaviour](https://www.erlang.org/docs/24/design_principles/sup_princ); Nygard "Release It!" (2018) Ch.5; [GenServer state recovery (Bounga, 2020)](https://www.bounga.org/elixir/2020/02/29/genserver-supervision-tree-and-state-recovery-after-crash/) |
| 指数バックオフ | Exponential Backoff with Jitter | [AWS Exponential Backoff guide](https://docs.aws.amazon.com/general/latest/gr/api-retries.html); Nygard "Release It!" (2018) |
| SSE プッシュ通知 | Server-Sent Events (EventSource API) | [FastAPI SSE (v0.135+)](https://fastapi.tiangolo.com/tutorial/server-sent-events/); [SSE vs WebSocket (Plain English, 2025)](https://plainenglish.io/blog/server-sent-events-for-push-notifications-on-fastapi); [Real-Time Notifications in Python (Medium, 2025)](https://medium.com/@inandelibas/real-time-notifications-in-python-using-sse-with-fastapi-1c8c54746eb7) |

**主要知見:**

1. **ERROR 自動リカバリ**: Erlang OTP の `restart_one_for_one` 戦略を参考に、エージェントが ERROR 状態になった際に指数バックオフ付きで再起動を試みる `_recovery_loop` を実装。最大再試行回数 (`recovery_attempts`) を超えた場合は `agent_recovery_failed` STATUS イベントを発行してオペレーターに通知。サーキットブレーカー（既存）との違い: サーキットブレーカーはタスクのディスパッチを制御するのに対し、リカバリループはエージェントプロセス自体を再起動する。

2. **SSE プッシュ通知**: `EventSource` API (ブラウザネイティブ) + FastAPI v0.135 の `EventSourceResponse` を使用。既存 WebSocket hub と異なり、SSE はシンプルな一方向ストリームで実装がシンプル。クライアントは自動再接続を持つ。ポーリング間隔を 3s → 30s に延長（SSE が大部分のリアルタイム更新を担当）。

**設計決定:**

- **リカバリループは独立タスク** — `supervised_task()` ではなくシンプルな `asyncio.create_task()`。リカバリループ自身がクラッシュしても致命的ではなく、次の restart attempt で復帰する
- **バックオフは `backoff_base^attempt` 秒** (デフォルト: 5^1=5s, 5^2=25s, 5^3=125s) — 指数的増加でリソース枯渇を防ぐ
- **永続失敗エージェントは `_permanently_failed` セットで管理** — 再起動しない。将来的には手動 reset エンドポイントで解除可能
- **SSE 認証は `Depends(auth)`** — `raise HTTPException` はジェネレータの外で実行するためフレームワークが正しく 401 を返せる
- **SSE のデータは `event=` フィールドでタイプ分け** (status, result, peer_msg) — クライアントは `addEventListener('status', ...)` で選択購読できる

### 10.9 調査記録 (v0.13.0, 2026-03-05)

#### 実装: 手動エージェントリセット (`POST /agents/{id}/reset`) と Prometheus メトリクス (`GET /metrics`)

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| REST ステートマシン操作 | Action Sub-Resource Pattern | [Nordic APIs "Designing a True REST State Machine"](https://nordicapis.com/designing-a-true-rest-state-machine/); [Spring Statemachine REST API Guide](https://otrosien.github.io/spring-statemachine-jpa-and-rest/html5/api-guide.html) |
| Prometheus Python メトリクス | USE Method (Utilisation, Saturation, Errors) | [OneUptime blog (2025-01-06): Python Custom Metrics Prometheus](https://oneuptime.com/blog/post/2025-01-06-python-custom-metrics-prometheus/view); [prometheus_client PyPI](https://pypi.org/project/prometheus-client/) |
| FastAPI + Prometheus 統合 | Gauge / Counter / Histogram | [prometheus-fastapi-instrumentator](https://github.com/trallnag/prometheus-fastapi-instrumentator); [DEV Community: Instrument Python FastAPI with Prometheus](https://dev.to/agardnerit/hands-on-instrument-python-fastapi-with-prometheus-metrics-3m1f) |

**主要知見:**

1. **手動リセットの REST パターン**: `POST /agents/{id}/reset` は「アクションサブリソース」パターンに準拠。`PUT` による状態置換（リソース全体の更新）ではなく、副作用を伴う命令的アクションとして `POST` を使用するのが適切。Nordic APIs の記事によると、ハイパーメディア駆動の API では操作を `operations` 配列に記述し、状態が許可する場合のみクライアントに提示する設計が望ましい。

2. **Prometheus メトリクス実装**: `prometheus_client` を直接使用し、per-request で `CollectorRegistry` を生成してメトリクスを計算・返却する。`prometheus-fastapi-instrumentator` は HTTP リクエスト自動計装に有効だが、エージェント固有の業務メトリクス（ステータス分布、キュー深度）は手動 Gauge で実装する方がシンプル。認証不要（Prometheus スクレイパー互換）とするため `include_in_schema=False` で `/metrics` を公開。

**設計決定:**

- **`reset_agent()` はオーケストレーターメソッド** — Web層から直接エージェント状態を変更しない (Hexagonal Architecture boundary)。`orchestrator.reset_agent(id)` → stop → clear bookkeeping → start → publish `agent_reset` STATUS event の順序を保証。
- **per-request CollectorRegistry** — グローバル Prometheus レジストリを使わず、各リクエストで新しい `CollectorRegistry` を生成。これにより並列リクエスト間のラベル衝突を防ぎ、リアルタイム値（スナップショット）として正確な値を返せる。テストでの分離も容易になる。
- **認証不要 `/metrics`** — Prometheus スクレイパーは認証ヘッダーを持たないことが多い。ネットワークレベルで保護（localhost バインドまたはファイアウォール）することを推奨。ドキュメントコメントに明記。
- **`prometheus-client` を main deps に追加** — 既存の `httpx`, `webauthn` と同様に main deps へ追加。dev-only にしないのは、本番 web server が `/metrics` を提供するため。

### 10.10 調査記録 (v0.14.0, 2026-03-05)

#### 実装: タスク結果ルーティング (`reply_to` フィールド)

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| リクエスト-返信パターン | Request-Reply with Correlation IDs | ["Learning Notes #15 – Request Reply Pattern \| RabbitMQ" (parottasalna.com, 2024)](https://parottasalna.com/2024/12/28/learning-notes-15-request-reply-pattern-rabbitmq/) |
| 階層エージェントシステムの情報フロー | 5軸タクソノミー (Control Hierarchy, Information Flow, Role Delegation, Temporal Layering, Communication Structure) | [Moore, D.J. "A Taxonomy of Hierarchical Multi-Agent Systems: Design Patterns, Coordination Mechanisms, and Industrial Applications" arXiv:2508.12683 (2025)](https://arxiv.org/abs/2508.12683) |
| LLM エージェント間のコーディネーション | Pub/Sub vs Request-Reply のトレードオフ | [Galileo "Multi-Agent Coordination Strategies" (2025)](https://galileo.ai/blog/multi-agent-coordination-strategies) |

**主要知見:**

1. **Request-Reply パターン**: RabbitMQ のリクエスト-返信パターンは3つのコアコンポーネントから構成される — (a) 返信先キューを指定する `reply_to` プロパティ、(b) リクエストと返信を対応させる `correlation_id`、(c) リクエスト単位の一時的なルーティングテーブル。このシステムでは `task_id` が correlation ID として機能し、`_task_reply_to[task_id] = agent_id` が per-request routing table を提供する。

2. **階層エージェントの情報フロー**: Moore (2025) によれば、階層エージェントシステムにおける情報フローは「上位から下位へのタスク配信」と「下位から上位への結果報告」の2方向で完結する必要がある。現行システムは RESULT を broadcast するのみで、親エージェントが結果を受信するには bus を直接購読する必要があった。`reply_to` により、暗黙的なバス監視なしに結果を親のメールボックスへ直接配送できる。

3. **ピア比較**: LangChain LCEL、AutoGen、CrewAI はいずれも直接的な callback 関数 / return value パターンを使うため、メールボックスベースの非同期配送は本システム固有の非同期性・永続性要件 (プロセス境界を越えた配送) に対応したもの。

**設計決定:**

- **`Task.reply_to: str | None`** — Task dataclass に追加。ディスパッチループは `reply_to` を意識せず (透過的)、`_route_loop` のみが RESULT 処理時に確認する。SRP (Single Responsibility Principle) 維持。
- **`_task_reply_to: dict[str, str]`** — task_id → agent_id のテーブルはオーケストレーター内でのみ保持。`submit_task()` で設定、`_route_result_reply()` で取得・削除 (配送後に自動クリーンアップ)。
- **MailBox write + notify_stdin の2段階配送** — ファイル永続化 (Mailbox) と即時通知 (notify_stdin) を組み合わせることで、エージェントが後でスラッシュコマンドで読める状態を保ちつつリアルタイム通知も提供。
- **未登録 `reply_to` エージェントは警告のみ** — クラッシュしない。エージェントがすでに停止している場合でも Mailbox への書き込みは試みる (将来の読み取り用)。
- **`_mailbox` は orchestrator の設定可能属性** — `main.py` が `Mailbox` インスタンスを注入する設計 (依存性逆転; Mailbox をオーケストレーターの責務にしない)。

---

## 11. 今後の課題

### 機能

| 優先度 | 課題 |
|--------|------|
| ~~高~~ ~~エージェントの ERROR 状態からの自動リカバリ~~ | **完了 (v0.12.0)** — `_recovery_loop` + 指数バックオフ、`agent_recovered`/`agent_recovery_failed` イベント (Issue #3 クローズ) |
| ~~高~~ ~~Director → ユーザーへの非同期プッシュ通知（現在はポーリング）~~ | **完了 (v0.12.0)** — SSE `/events` エンドポイント; Web UI の 3s ポーリングを SSE に置換 |
| ~~中~~ ~~Web UI のエージェント階層ビジュアライゼーション（ツリー表示）~~ | **完了 (v0.11.0)** — `/agents/tree` エンドポイント + List/Tree トグル |
| ~~中~~ ~~タスク結果を親エージェントのメールボックスに直接配送 (`reply_to`)~~ | **完了 (v0.14.0)** — `Task.reply_to` フィールド + `Orchestrator._route_result_reply()` + REST `POST /tasks` の `reply_to` パラメータ |
| 中 | `/plan` と `/tdd` の出力を RESULT メッセージとして親に自動送信 |
| 低 | エージェントのコンテキスト使用量モニタリング |
| ~~低~~ ~~ERROR エージェントの手動リセットエンドポイント (`POST /agents/{id}/reset`)~~ | **完了 (v0.13.0)** — `Orchestrator.reset_agent()` + REST `POST /agents/{id}/reset` |
| ~~低~~ ~~Prometheus メトリクス (`/metrics`)~~ | **完了 (v0.13.0)** — `GET /metrics` (prometheus_client 直接使用; 認証不要) |

### デモシナリオ候補

| 優先度 | シナリオ | パターン |
|--------|----------|---------|
| **高** | **AtCoder Heuristic Contest (AHC) best-of-N** | Competitive / best-of-N |
| 中 | Director → Workers でマイクロサービス API を分割実装 | Director → Workers |
| 中 | agent-a が実装 → agent-b がレビュー → agent-a が修正 | Peer review pipeline |

**AHC best-of-N シナリオ詳細**:
- 過去の AHC 問題（AHC001〜AHC030）を使用（公開済み・オフラインスコアラーあり）
- N ≥ 3 エージェントが同一問題を異なる戦略（greedy / random restart / simulated annealing など）で解く
- 各エージェントがソルバースクリプトを書いて実行し、スコアを出力
- オーケストレーターがスコアを収集して最高得点の解を選択
- 検証項目: 正しい本数のソルバーが生成された・スコアが数値・勝者が選択された

→ スコア関数が完全に定義されていて曖昧さがなく、複数エージェントの並列実行価値が明確。
→ 詳細は CLAUDE.md「Recommended concrete scenario」を参照。

### アーキテクチャ

| 優先度 | 課題 |
|--------|------|
| ~~高~~ ~~CLAUDE.md の動的更新（タスク変更時に役割説明を更新）~~ | **クローズ**: タスク生存期間 = エージェント生存期間の原則により不要。Workers は ephemeral であるべき (Issue #4, 2026-03-05) |
| ~~中~~ ~~コンテキストファイルの自動コピー (`context_files` の実装)~~ | **完了 (v0.11.0)** — `ClaudeCodeAgent._copy_context_files()` |
| 中 | `/summarize` による NOTES.md 更新をオーケストレーターに通知 |
| 低 | エージェント間の共有スクラッチパッド（読み取り専用の共有ファイル）|
