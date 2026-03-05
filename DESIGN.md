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

### 10.11 調査記録 (v0.15.0, 2026-03-05)

#### 実装: `POST /tasks/batch` + AHC Best-of-N デモ

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| REST バッチ操作 | Bulk/Batch エンドポイント | [mscharhag: "Supporting bulk operations in REST APIs"](https://www.mscharhag.com/api-design/bulk-and-batch-operations); [adidas API Guidelines "Batch Operations"](https://adidas.gitbook.io/api-guidelines/rest-api-guidelines/execution/batch-operations) |
| バッチ API の実装パターン | Resource-specific bulk sub-collection | [PayPal Tech Blog: "Batch: An API to bundle multiple REST operations"](https://medium.com/paypal-tech/batch-an-api-to-bundle-multiple-paypal-rest-operations-6af6006e002) |
| Best-of-N サンプリング | 並列エージェント + スコア集約 | [Inference Scaling Laws (ICLR 2025)](https://proceedings.iclr.cc/paper_files/paper/2025/file/8c3caae2f725c8e2a55ecd600563d172-Paper-Conference.pdf); [OpenAI "Competitive Programming with Large Reasoning Models" arXiv:2502.06807 (2025)](https://arxiv.org/abs/2502.06807) |
| 並列 LLM エージェント | Parallel agents with early termination | [arxiv:2507.08944 "Optimizing Sequential Multi-Step Tasks with Parallel LLM Agents" (2025)](https://arxiv.org/html/2507.08944v1) |
| 多エージェント失敗分析 | Why multi-agent systems fail | [Cemri et al. "Why Do Multi-Agent LLM Systems Fail?" arXiv:2503.13657 (2025)](https://arxiv.org/pdf/2503.13657) |
| AtCoder Heuristic Contest | 連続スコア最適化コンテスト | [AHC001 問題ページ](https://atcoder.jp/contests/ahc001/tasks/ahc001_a); [AHC058 Sakana AI 優勝](https://sakana.ai/ahc058/) |

**主要知見:**

1. **REST バッチ設計**: `POST /tasks/batch` は「リソース固有のバルクサブコレクション」パターンに準拠。汎用 `/batch` エンドポイント（PayPal 方式）は柔軟だが複雑。本システムではタスク提出という単一操作のバッチ化のため、シンプルな `{tasks: [...]}` リクエストボディを採用。すべてのタスクが検証済み後にキューイングされる「All or None」セマンティクスにより、部分的なエンキューによる一貫性問題を回避。

2. **Best-of-N サンプリング**: Inference Scaling Laws (ICLR 2025) によれば、best-of-N サンプリングは推論スケーリングの中で最もシンプルかつ有効な手法の一つ。N が大きくなると小モデルでも大モデルに迫るパフォーマンスを発揮できる。競技プログラミングコンテキストでは OpenAI (2025) が「数千サンプルの中から最高スコアを選択する」方式を採用。本システムでは3エージェントが異なる戦略（greedy/random/DP）を並列実行し、スコアを比較する。

3. **AHC001 問題**: 10000×10000 グリッドに N 社の広告矩形を配置し、面積満足度の総和を最大化する問題。オフラインスコアラーが公開されており、複数戦略を比較するのに適している。本デモでは実装負荷を下げるため、より単純な Weighted Knapsack 問題（0-1 ナップサック, N=15, C=50）を使用。明確な入出力フォーマットと検証可能なスコア関数を備え、greedy/random/DP の3戦略で差が出る設計。

4. **並列エージェント失敗の教訓**: Cemri et al. (2025) によれば、多エージェントシステムが失敗する主要原因は (a) エラー伝播、(b) コンテキスト汚染、(c) 非効率な通信、(d) スケーリング問題。本デモでは各エージェントが完全に独立したタスク（ファイル名で区別）を受け取ることでコンテキスト汚染を回避。

**設計決定:**

- **`POST /tasks/batch` の検証は FastAPI に委任** — `TaskBatchSubmit` の `tasks: list[TaskSubmit]` 定義により、各 TaskSubmit のバリデーションは pydantic が担う。ハンドラーはバリデーション済みデータのみを受け取る。
- **`TaskBatchSubmit` はモジュールレベルに定義** — 他の `TaskSubmit`、`AgentKillResponse` 等と同列に定義し、コードの一貫性を維持。
- **バッチ内の全タスクを逐次エンキュー** — 現在の `Orchestrator.submit_task()` は async 関数であり、バッチハンドラー内でループ実行。将来的には `asyncio.gather()` で並列エンキューも可能だが、キューの順序保証のため逐次とした。
- **デモ問題は Weighted Knapsack** — AHC001 の広告配置問題は実装が複雑すぎる（矩形重複判定など）ため、エージェントが10分以内に独立して解けるシンプルな 0-1 ナップサック問題を採用。最適解は既知（DP により score=154）なので、エージェントの解の品質を客観的に評価できる。
- **スコアラー (`score.py`) は stdlib のみ使用** — 外部依存なしで `python score.py problem.txt solution.txt` → `SCORE=N` を出力。エージェントが自分でスコアを確認できる設計。
- **`TaskResultPayload.output/error` の型強制** — 既存バグ修正: `output` フィールドが `str | None` であるにも関わらず `@field_validator` が `error` のみに適用されており、`output` に int が渡ると pydantic v2 が ValidationError を送出していた。`@field_validator("output", "error", mode="before")` に統合して解消。

---

### 10.12 調査記録 (v0.16.0, 2026-03-05)

#### 実装: 共有スクラッチパッド + target_agent ルーティング + Peer Review Pipeline デモ

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| 共有作業メモリ | Blackboard パターン | Buschmann et al. "Pattern-Oriented Software Architecture Vol 1: A System of Patterns" (1996), Ch. 4 — Blackboard |
| メッセージルーター | Message Router | Hohpe & Woolf "Enterprise Integration Patterns" (2003), p.78-82 — Message Router |
| LLM ピアレビュー | Multi-agent peer review simulation | Jin et al. "AgentReview: Exploring Peer Review Dynamics with LLM Agents" EMNLP 2024, [arXiv:2406.12708](https://arxiv.org/html/2406.12708v2) |
| LLM コードレビュー | Iterative LLM code review | ACM TOSEM "LLM-Based Multi-Agent Systems for Software Engineering" (2025) [doi:10.1145/3712003](https://dl.acm.org/doi/10.1145/3712003) |

**主要知見:**

1. **Blackboard パターン**: Buschmann et al. (1996) は、複数の独立した「ナレッジソース」（エージェント）が共有データ構造（Blackboard）を読み書きすることで協調する設計パターンを記述。各エージェントは他のエージェントの処理を知らずに、Blackboard の状態に基づいて行動する。本実装では `/scratchpad/{key}` REST API がこの役割を担い、agent-reviewer が書いたレビュー要約を agent-author と orchestrator の両方が参照できる。

2. **Message Router (EIP)**: Hohpe & Woolf (2003) の Message Router パターンは、受信者の識別に基づいてメッセージを適切なチャネルにルーティングする。本実装では `target_agent` フィールドがタスクの「宛先フィルター」として機能し、dispatch loop が条件を評価してルーティングを決定する。

3. **AgentReview (EMNLP 2024)**: LLM エージェントによる査読シミュレーション。複数の Reviewer エージェント、AC エージェント、Author エージェントが 5 フェーズのパイプラインで協調。本デモの author/reviewer 2 エージェント構成はこのアーキテクチャを単純化したもの。

**設計決定:**

- **`_scratchpad` はモジュールレベル辞書** — サーバー起動中に永続し、再起動でクリアされる。永続化が必要な場合は SQLite や Redis に置き換え可能（インターフェースは同一）。
- **`GET /scratchpad/` と `GET /scratchpad/{key}` の共存** — FastAPI はパスの最初にリテラル `/scratchpad/` を試み、次に `{key}` パスパラメータにマッチする。`/` サフィックスを必須とすることで曖昧さを排除。
- **`target_agent` が未登録 → 即死文字キュー** — 存在しないエージェントへのルーティングは明確なプログラミングエラーであり、再試行しても解消しない。再試行せず即 DLQ に移す設計でオペレーターへの早期通知を実現。
- **`target_agent` がビジー → 通常の再試行** — エージェントが現在処理中の場合は一時的な状態であり、通常の `dlq_max_retries` 上限内で再試行する。再試行間隔は既存の 0.2s sleep と同じ（後で設定可能にできる）。
- **デモ工夫**: `target_agent` ルーティングにより、agent-author → agent-reviewer → agent-author という明確な順序が保証される。両エージェントが同時に IDLE でも、タスクが「正しい」エージェントにのみ渡される。

---

### 10.13 調査記録 (v0.17.0, 2026-03-05)

#### 実装: タスクキャンセル + エージェント別タスク履歴 + Director → Workers デモ

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| タスクキャンセル | Async Request-Reply キャンセル | Microsoft Azure "Asynchronous Request-Reply pattern" (2024) [learn.microsoft.com](https://learn.microsoft.com/en-us/azure/architecture/patterns/async-request-reply) |
| タスク履歴・観測性 | Per-agent タスク履歴 | TAMAS (IBM, 2025) "Beyond Black-Box Benchmarking" arXiv:2503.06745 |
| エージェント観測性 | Langfuse AI Agent Observability | Langfuse "AI Agent Observability" (2024) [langfuse.com](https://langfuse.com/blog/2024-07-ai-agent-observability-with-langfuse) |
| Director-Worker パターン | Orchestrator-Worker / Role-Based Cooperation | Guo et al. "Designing LLM-based Multi-Agent Systems for Software Engineering Tasks" arXiv:2511.08475 (2024) |
| Director-Worker パターン | Hierarchical Coordination | Google ADK "Developer's guide to multi-agent patterns" (2025) |

**主要知見:**

1. **非同期 REST キャンセル (Azure, 2024)**: タスク提出時に返されるロケーション URL への DELETE/POST で非同期タスクをキャンセルできる。本実装では `POST /tasks/{id}/cancel` という action sub-resource パターンを採用。DELETE だとリソース削除と混同されるため POST が適切。キャンセルは冪等 (idempotent) であるべきで、既にディスパッチ済みタスクには `{cancelled: false, status: "already_dispatched"}` を返す。

2. **TAMAS エージェント分析 (IBM, 2025)**: タスク別エージェントパフォーマンストラッキング（処理時間、スループット、エラー率）が LLM マルチエージェントシステムの「ブラックボックス」問題を解決する。各エージェントのタスク履歴を `{task_id, started_at, finished_at, duration_s, status}` として記録することで、ボトルネック特定・パフォーマンス最適化が可能になる。

3. **Orchestrator-Worker パターン (Guo et al., 2024)**: 多エージェント LLM システムの 47% がロールベース協調を採用。Orchestrator が動的にサブタスクを分解し、各 Worker に割り当て、結果を統合する。本デモの Director (1) + Workers (3) 構成はこのパターンの典型例。

4. **asyncio.PriorityQueue のキャンセル実装**: `asyncio.PriorityQueue` は Python stdlib の実装で、ヒープ (`_queue`) を直接操作してキャンセルを実装した。`heapq.heapify()` でヒープ性質を再構築し、`_unfinished_tasks` カウンタを手動で調整する。これは内部 API 依存だが、asyncio のバージョン間で安定していることを確認した（Python 3.11-3.12）。

**設計決定:**

- **`cancel_task` のキュー操作**: `asyncio.PriorityQueue._queue` ヒープを直接操作してキャンセル対象を除外し再構築する。Queue の public API にはキャンセルメソッドがないため内部操作は不可避。`_unfinished_tasks` の手動デクリメントで `task_done()` の不整合を防ぐ。
- **タスク履歴の上限 200**: 履歴は append-only で蓄積するため上限 200 を設定。`get_agent_history()` は末尾 200 件 → 逆順で返す。エージェントが長期稼働する場合でも最大メモリは O(200 × entry_size)。
- **`started_at` の計算**: `time.monotonic()` で経過時間を計測し `duration_s` を算出。`started_at` は `finished_at - duration_s` として逆算する。壁時計を使わないことで clock skew に頑健。
- **REST 404 判定ロジック**: `POST /tasks/{id}/cancel` で「未知のタスク」を区別するには `_task_started_at`（インフライト）、`_completed_tasks`（完了済み）、`_dlq`（デッドレター）を参照。いずれにも見つからなければ 404。
- **デモの `wait_for_task_completion`**: v0.17.0 の新機能 `GET /agents/{id}/history` を使ってタスク完了を検知する。BUSY→IDLE ポーリングと異なり、タスクが高速完了しても history レコードが残るので見逃しがない。

**Director-Workers デモ成果 (v0.17.0)**:
- 4 エージェント並列: agent-director + agent-w1 + agent-w2 + agent-w3
- 3 ワーカーが `endpoint_post_items.py`、`endpoint_get_items.py`、`endpoint_delete_items.py` を並列実装
- タスクキャンセルをライブデモ: `b0264aed` タスクが `agent-w1` が BUSY 中にキューで待機 → キャンセル確認
- Director が `integration_report.md` (3565 bytes) を生成、CRUD サービス全体を評価
- 経過時間 70 秒、DLQ 0 件、全 OK
- デモフォルダ: `~/Demonstration/v0.17.0-director-workers/`

---

### 10.14 調査記録 (v0.18.0, 2026-03-05)

#### 実装: エージェント能力タグ + スマートディスパッチ

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| 能力ベースルーティング | FIPA Directory Facilitator (DF) — 能力広告とマッチング | FIPA Agent Communication Language Specifications (2002) [smythos.com](https://smythos.com/developers/agent-development/fipa-agent-communication-language/) |
| ラベルベースワークロード割り当て | Kubernetes nodeSelector / Node Affinity | Kubernetes Docs "Assigning Pods to Nodes" (2024) [kubernetes.io](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/) |
| シナリオ対応エージェント選択 | COLA: Collaborative Multi-Agent Framework | COLA "Dynamic Collaboration" EMNLP 2025 [aclanthology.org](https://aclanthology.org/2025.emnlp-main.227.pdf) |
| マルチエージェント能力プランニング | Agent-Oriented Planning | arXiv:2410.02189 (2024) [arxiv.org](https://arxiv.org/html/2410.02189v1) |

**主要知見:**

1. **FIPA Directory Facilitator (2002)**: FIPA準拠システムの Directory Facilitator (DF) はエージェントの能力（サービス記述）を登録・検索するサービスレジストリ。エージェントAがサービスSを必要とする場合、DFに問い合わせてSを提供するエージェントを取得する。本実装では静的な `AgentConfig.tags` リストがこの「能力広告」に相当し、オーケストレーターがディスパッチ時に `set(required_tags) <= set(agent.tags)` で評価する。

2. **Kubernetes Node Affinity (2024)**: Kubernetes の nodeSelector はポッドを特定のラベルを持つノードにのみスケジュールする。`RequiredDuringSchedulingIgnoredDuringExecution` は「条件を満たすノードがなければスケジュール不可」を意味し、本実装の「capable な idle エージェントがなければ再キューに戻す → max_retries 後に DLQ」に対応する。

3. **COLA フレームワーク (EMNLP 2025)**: Task Scheduler がシナリオ対応のマッチングで最適エージェントを動的に選択する。本実装の `find_idle_worker(required_tags)` は簡略化されたタグ部分集合マッチング版。将来的にはスコアリング（過去の成功率、負荷状況）を組み合わせた拡張が可能。

**設計決定:**

- **`AgentConfig.tags: list[str]`** — YAML で宣言的に定義する静的能力広告。実行時に変更しない（ephemeral な能力変化には向かない）。Kubernetes のノードラベルと同様の考え方。
- **`Task.required_tags: list[str]`** — タスク提出時に ALL-must-match 制約として指定。OR/NOT などの複雑な論理式は実装しない（YAGNI）。Kubernetes の `matchLabels` と同一のセマンティクス。
- **`find_idle_worker(required_tags)` のシグネチャ**: デフォルト `required_tags=None`（空リスト扱い）で後方互換性を維持。`set(required_tags).issubset(set(agent.tags))` で O(n) の評価。エージェント数が少ない（10〜100）ため線形スキャンで十分。
- **DLQ への移行**: capable な idle エージェントがいない場合は `no idle agent with required_tags=... after N retries` というメッセージで dead-letter。known target_agent not idle と同じ再試行パスを使用するため、追加のループ分岐は不要。
- **`list_all()` の `tags` フィールド**: エージェントスナップショットに `tags` を含めることで、Web UI や API クライアントが能力マップを可視化できる。
- **`list_tasks()` の `required_tags` フィールド**: 待機中タスクに `required_tags` を含めることで、どのタスクがどの能力を必要としているかを確認できる。

**デモシナリオ (v0.18.0):**
- Agent `python-expert` (tags: `["python", "testing"]`) と `docs-writer` (tags: `["markdown", "documentation"]`)
- タスク「Write unit tests for the knapsack solver」(required_tags: `["python", "testing"]`) → python-expert にのみ配送
- タスク「Write README.md for the project」(required_tags: `["markdown", "documentation"]`) → docs-writer にのみ配送
- 2エージェントが並列稼働し、タグマッチングで正しいエージェントにのみ配送されることを実証

---

### 10.15 調査記録 (v0.19.0, 2026-03-05)

#### 実装: キューポーズ/レジューム REST API + タスク優先度ライブ更新

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| キューポーズ/レジューム | Queue Pause/Resume for maintenance drain | Google Cloud Tasks [`queues.pause`](https://docs.cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues/pause) API (2024) |
| キューポーズ/レジューム | JMS Destination pause for troubleshooting | Oracle WebLogic "Pause queue message operations at runtime" (2024) |
| ライブ優先度更新 | decrease_key / increase_key ヒープ操作 | Python [`heapq` docs](https://docs.python.org/3/library/heapq.html) "Priority Queue Implementation Notes" |
| ライブ優先度更新 | スケジューリング優先度調整 | Liu, C.L.; Layland, J.W. (1973). "Scheduling Algorithms for Multiprogramming in a Hard Real-Time Environment". JACM 20(1). |
| ライブ優先度更新 | Priority Queue decrease_key / increase_key | Sedgewick & Wayne "Algorithms" 4th ed. §2.4 — Priority Queues |

**主要知見:**

1. **Google Cloud Tasks Pause API (2024)**: キューをポーズすると、レジュームするまで新しいタスクのディスパッチが停止する。インフライトのタスクは正常に完了する。ポーズ中もタスクをエンキューできる。本実装の `POST /orchestrator/pause` は同じセマンティクスを持つ。

2. **Oracle WebLogic JMS Pause (2024)**: JMS デスティネーションのメッセージ操作（本番/挿入/消費）をランタイムに個別にポーズできる。トラブルシューティング、ローリングデプロイ、メンテナンスウィンドウ確保に有用。ポーズ後は既存メッセージをドレインして問題解決後に再開できる。

3. **Python heapq decrease_key (2024)**: Python の `heapq` は `decrease_key` / `increase_key` 操作を直接提供しない。標準的なアプローチは: (a) 無効化マーク + 新エントリ追加、または (b) エントリをインプレース変更後 `heapq.heapify()` でO(n) 再構築。本実装は (b) を採用。エージェント数が少ない（< 1000 タスク）ため線形再構築で十分。

4. **Liu & Layland 優先度スケジューリング (1973)**: RTOS の Rate-Monotonic Scheduling (RMS) では固定優先度の割り当てが最適性を保証する。動的な優先度変更（Priority Ceiling Protocol, Priority Inheritance）は優先度逆転を防ぐために使用される。本実装の `PATCH /tasks/{id}` は運用者が緊急タスクを昇格させることで事実上の優先度逆転を防ぐ手段。

**設計決定:**

- **`update_task_priority(task_id, new_priority)` のシグネチャ**: タスクIDと新しい優先度を受け取り `bool` を返す。キューに見つからなければ `False`（既にディスパッチ済み or 未提出）。見つかれば変更して `task_priority_updated` STATUS イベントを発行し `True` を返す。
- **ヒープ再構築**: `asyncio.PriorityQueue._queue` ヒープをリスト化 → 対象タプルの優先度を変更 → `heapq.heapify()` で再構築。O(n) で小規模キューには十分。`cancel_task` と同じ内部 API アクセスパターンを使用。
- **`POST /orchestrator/pause` の冪等性**: 既にポーズ済みのオーケストレーターに再度 pause を送っても安全。`resume` も同様。
- **`GET /orchestrator/status` のフィールド**: `paused`（フラグ）、`queue_depth`（ペンディングタスク数）、`agent_count`（登録エージェント数）、`dlq_depth`（デッドレターキュー深さ）。運用可視性のための最小限のフィールド。

**デモシナリオ (v0.19.0):**
- 問題: Weighted Interval Scheduling (WIS, N=12, optimal=80) — Kleinberg & Tardos "Algorithm Design" §6.1
- 3 エージェント: `solver-greedy`、`solver-dp`、`solver-random` が並列稼働
- Phase 1: `target_agent` ルーティングで3タスクを投入（greedy/DP/Monte Carlo）
- Phase 2: `POST /orchestrator/pause` でポーズ（インフライトタスクは継続）
- Phase 3: ポーズ中に3タスクを投入（優先度 5, 3, 7）→ キューに待機
- Phase 4: `PATCH /tasks/{TC}` で solver-random タスクの優先度を 7→0 に昇格
- Phase 5: `POST /orchestrator/resume` でレジューム → 優先度順にディスパッチ（C→B→A）
- 全6ソリューションが有効 (score=68/80, 85%)
- デモフォルダ: `~/Demonstration/v0.19.0-pause-resume-priority/`

---

### 10.16 調査記録 (v0.20.0, 2026-03-05)

#### 実装: Token-Bucket Rate Limiter — タスク投入のバックプレッシャー

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| Rate limiting | Token Bucket Algorithm | Tanenbaum, A.S. "Computer Networks" 5th ed. §5.3 (2011) |
| Rate limiting | Two-Rate Three-Color Marker | RFC 4115 "A Differentiated Service Two-Rate, Three-Color Marker" IETF (2005) |
| asyncio 実装 | Leaky Bucket for async Python | aiolimiter v1.2.1 documentation (2024) https://aiolimiter.readthedocs.io/ |
| Web rate limiting | limit_req_zone / limit_req | NGINX HTTP rate limiting directives (2025) https://nginx.org/en/docs/http/ngx_http_limit_req_module.html |
| LLM backpressure | Async backpressure patterns | "Manage async I/O backpressure using bounded queues and timeouts" tech-champion.com (2025) |

**主要知見:**

1. **Token Bucket (Tanenbaum §5.3)**: トークンバケットは一定レート（tokens/秒）でバケットに追加され、バースト許容量（burst）を上限とする。リクエストはトークンを消費する。トークンがなければブロック（wait）またはリジェクト（try_acquire）する。Leaky Bucket（定率出力）と異なり、バースト処理に柔軟性がある。

2. **RFC 4115 Two-Rate Three-Color Marker**: CIR（committed information rate）と PIR（peak information rate）の2レートでパケットを Green/Yellow/Red に分類する。本実装は簡略化してシングルレート + バーストのみ対応。プロダクション環境では Prometheus カウンター (`rate_limit_exceeded_total`) との組み合わせが推奨。

3. **aiolimiter (2024)**: Python asyncio 向け Leaky Bucket 実装。`AsyncLimiter(max_rate, time_period)` API。本実装は Token Bucket を採用（バースト制御が必要なため）し、`asyncio.Lock` でコルーチン安全性を保証する点は同様。

4. **NGINX rate limiting (2025)**: `limit_req_zone` で共有メモリゾーン定義、`limit_req rate=N r/s burst=M` でバースト処理。`nodelay` でバースト中のキューイング遅延を排除。本実装の `wait_for_token=False` は NGINX の `nodelay` 相当（即時リジェクト）。

5. **Backpressure patterns (2025)**: asyncio における バックプレッシャー管理の要諦: (a) 有界キュー、(b) タイムアウト、(c) ロードシェディング。本実装は (c) を `RateLimitExceeded` として実装し、`rate_limit_exceeded` STATUS イベントで可観測性を提供。

**設計決定:**

- **`TokenBucketRateLimiter` の独立モジュール化**: `rate_limiter.py` として分離。オーケストレーター・Web API どちらからでも再利用可能。`asyncio.Lock` でコルーチン安全性を保証。
- **`wait_for_token=True` がデフォルト**: Director のような長期稼働エージェントは待機を許容するが、REST API クライアントは `wait_for_token=False` で即時 429 を受け取るべき。
- **`rate_limit_exceeded` STATUS イベント**: レート制限違反を bus に発行することで、TUI・WebSocket ハブ・Prometheus メトリクスが自動追跡可能。可観測性パターン (DESIGN.md §2) を遵守。
- **設定ファイル統合**: `OrchestratorConfig.rate_limit_rps` / `rate_limit_burst` で YAML からレートを設定可能。`rate_limit_burst=0` の場合は `max(1, int(rps * 2))` を自動適用（最小バースト保証）。
- **`GET /rate-limit` / `PUT /rate-limit` REST エンドポイント**: 稼働中のオーケストレーターのレートをリアルタイム変更可能（動的スロットリング）。`PUT /rate-limit` は `reconfigure()` を呼ぶことでバケット内トークンを継続しながらレートのみ変更。

**デモシナリオ (v0.20.0):**
- 問題: Graph Coloring (N=15, E=22, K=4) — NP-hard、chromatic number=3
- 3 エージェント: `solver-greedy`（次数降順greedy）、`solver-backtrack`（バックトラック+AC-3）、`solver-local`（局所探索/シミュレーテッドアニーリング）
- `rate_limit_rps=3.0 burst=3` で起動 → 最初の3タスクはバーストで即時投入
- `PUT /rate-limit` で動的に `rate=10.0 burst=10` に変更してデモタスク投入
- 各エージェントが `solver_{strategy}.py` を書いて実行 → `solution_{strategy}.txt` に出力
- `problem.py` で各ソリューションをスコアリング → 最高スコアの戦略を選択
- デモフォルダ: `~/Demonstration/v0.20.0-rate-limit-graph-coloring/`

---

### 10.17 調査記録 (v0.21.0, 2026-03-05)

**実装テーマ: エージェントのコンテキスト使用量モニタリング + NOTES.md 更新通知**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| Context saturation | Lost in the Middle | Liu et al. "Lost in the Middle: How Language Models Use Long Contexts" TACL 2024 https://arxiv.org/abs/2307.03172 |
| Token estimation | 4-char heuristic | Anthropic token counting docs (2025) https://platform.claude.com/docs/en/build-with-claude/token-counting |
| Context window limits | 200k tokens (Sonnet/Opus) | Anthropic context windows docs (2025) https://platform.claude.com/docs/en/build-with-claude/context-windows |
| File change detection | mtime polling | Python `Path.stat().st_mtime`; simpler than inotify for cross-platform compatibility |
| Observer pattern | Pub/Sub STATUS event | DESIGN.md §2 — bus-based observability |

**主要知見:**

1. **Liu et al. "Lost in the Middle" (TACL 2024)**: LLM はコンテキストウィンドウの中央部にある情報を忘れやすい（「コンテキスト腐食」）。コンテキストが 75% を超えると精度が著しく低下するため、早期に `/summarize` で圧縮することが重要。

2. **Token estimation (Anthropic 2025)**: 正確なトークン計数には `messages.countTokens` API が必要だが、ポーリングループでの呼び出しはコスト・遅延の観点から非現実的。4文字/トークンの保守的ヒューリスティックで実用上十分な精度が得られる。

3. **mtime polling vs inotify**: inotify は Linux 固有でコード複雑度が高い。mtime ポーリング（5秒ごと）はクロスプラットフォームで実装が単純。エージェントの `/summarize` 実行後の NOTES.md 更新は数秒の遅延が許容されるため、mtime ポーリングで十分。

4. **Feedback loop**: コンテキスト超過 → `/summarize` 自動注入 → NOTES.md 更新 → `notes_updated` イベント → 親エージェント/オーケストレーターへの通知 — というクローズドループが形成される。

5. **REST endpoints**: `GET /agents/{id}/stats` および `GET /context-stats` でオブザーバビリティを提供。Prometheus メトリクス (`/metrics`) と組み合わせることで、コンテキスト状態の時系列観察が可能。

**設計決定:**

- **`ContextMonitor` の独立モジュール化**: `context_monitor.py` として分離。Orchestrator は `lambda: list(registry.all_agents().values())` を渡して agents を遅延取得（動的エージェント追加に対応）。
- **`AgentContextStats` dataclass**: 各エージェントのコンテキスト状態をカプセル化。`warned`/`summarize_injected` フラグで重複イベント抑制。
- **`notes_updated` イベント**: `from_id="__context_monitor__"` で区別可能。TUI・WebSocket ハブ・Director エージェントが自動購読可能。
- **`auto_summarize=False` がデフォルト**: 本番環境ではエージェント動作への自動介入は保守的に。`config.yaml` で `context_auto_summarize: true` を明示的に設定した場合のみ有効化。
- **`config.context_monitor_poll=5.0` (秒)**: tmux pane capture のオーバーヘッドを考慮。テストでは高い値 (99.0) を設定して自動ポーリングを無効化し、`_poll_all()` を直接呼ぶ。
- **`summarize_injected` リセット**: NOTES.md 更新を検出したタイミングで `summarize_injected=False` にリセット。コンテキストが再び閾値を超えたときに再注入可能。

**テスト (21テスト, 合計347テスト):**
- pane_chars/estimated_tokens の計算精度
- context_warning イベントの発行・重複抑制
- notes_updated イベント (mtime 変化検出)
- /summarize 自動注入の 1回限り保証・リセット
- REST エンドポイントの 200/404 応答
- YAML 設定の読み込み
- Orchestrator との統合 (start/stop)

**デモシナリオ (v0.21.0):**
- 問題: Travelling Salesman Problem (TSP) with N=10 cities on a 2D grid
- 3 エージェント: `solver-nn`（最近傍法）、`solver-2opt`（2-opt局所探索）、`solver-random`（ランダム再起動+2-opt）
- 各エージェントが `solver_{strategy}.py` を書いて実行 → tour length を出力
- `GET /context-stats` で各エージェントのコンテキスト使用量をリアルタイム確認
- `context_warning` イベントを SSE ストリームで観察
- オーケストレーターが最小ツアー長（勝者）を選択
- デモフォルダ: `~/Demonstration/v0.21.0-context-monitor-tsp/`

---

### 10.18 調査記録 (v0.23.0, 2026-03-05)

**実装テーマ: Queue-Depth Autoscaling — 弾力的エージェントプール**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| Queue-depth scaling | HPA AverageValue metric | Kubernetes HPA https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/ |
| MAPE-K loop | Monitor–Analyze–Plan–Execute | Thijssen "Autonomic Computing" (MIT Press, 2009) §3 |
| Scale-down cooldown | Cooldown period | AWS Auto Scaling cooldowns https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-cooldowns.html |

**主要知見:**

1. **Kubernetes HPA の `AverageValue` モデル**: HPA は `queue_depth / running_pods` をメトリクスターゲットと比較してレプリカ数を算出する。本実装はこれを `queue_depth > threshold * idle_agent_count` という条件に単純化。idle_count=0 の cold-start ケースは `max(1, idle_count)` で安全に処理。

2. **Thijssen MAPE-K ループ**: Monitor（queue_depth と idle 数の収集）→ Analyze（閾値比較）→ Plan（scale-up/scale-down の決定）→ Execute（`create_agent()` / `agent.stop()`）の4フェーズが `_scale_loop()` 内で実装されている。Knowledge base は `_autoscaled_ids`・`_queue_empty_since`・設定パラメータ。

3. **AWS クールダウンパターン**: スケールアウト直後の連続起動嵐を防ぐため、AWS はデフォルト300秒のクールダウンを推奨。本実装は `autoscale_cooldown`（デフォルト30秒）をスケールダウン専用に使用。スケールアップはキューが実際に成長している場合にのみトリガーされるため、上方向のクールダウンは不要。

4. **Scale-to-zero**: `autoscale_min=0` のとき、キューが空でクールダウンが経過した場合にすべての autoscaled エージェントを停止できる。これにより、タスクがないときのリソース消費をゼロにできる。

5. **Pre-registered agents の保護**: `_autoscaled_ids` が自分で作ったエージェントのみを追跡するため、YAML で事前定義されたエージェントが誤ってスケールダウンされることはない。

**設計決定:**

- **`AutoScaler` の独立モジュール化**: `autoscaler.py` として分離。Orchestrator は `autoscale_max > 0` のときのみインスタンス化（`autoscale_max=0` = 無効）。
- **`isolate=False` デフォルト**: autoscaled エージェントはワークスペースを共有するのが自然（バースト処理の典型的ユースケース）。必要なら YAML の `autoscale_agent_tags` や CONTROL で変更可能。
- **1サイクル1エージェントのスケールダウン**: 一度に複数を停止すると過剰スケールダウンのリスクがある。1サイクルで1エージェントを停止し、次のポーリングで再評価する。
- **`_queue_empty_since` のリセット**: スケールダウン後にタイマーをリセットすることで、連続スケールダウン間に必ずクールダウン待機期間が挟まる。

**テスト (23テスト, 合計386テスト):**
- scale-up: 閾値超過時にエージェント作成、`_autoscaled_ids` 追跡
- scale-up: max到達時に作成しない
- scale-up: 閾値以下では作成しない
- scale-down: クールダウン後に idle エージェント停止・unregister
- scale-down: min を下回らない (respects_min)
- scale-down: キュー非空時はタイマーリセット・停止しない
- scale-down: クールダウン中は停止しない
- REST GET/PUT /orchestrator/autoscaler: 有効・無効状態それぞれ
- lifecycle: start/stop、status、reconfigure
- queue_depth(): 空・タスクあり

**デモシナリオ (v0.23.0):**
- シナリオ: "Burst Load Handling"
- 0エージェントで起動、AutoScaler (min=0, max=3, threshold=2, cooldown=60s)
- 6タスクをバースト投入 (各エージェントが fib_{N}.txt を書く)
- AutoScaler がスケールアップして最大3エージェントを起動
- 6タスクを3エージェントで並列処理（各エージェント2タスク）
- クールダウン後にスケールゼロへ
- デモフォルダ: `~/Demonstration/v0.23.0-autoscaling/`

---

### 10.19 調査記録 (v0.24.0, 2026-03-05)

**実装テーマ: Task Result Persistence — Event Sourcing + CQRS**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| 追記専用ログ | Event Sourcing | Martin Fowler "Event Sourcing" (2005) https://martinfowler.com/eaa.html |
| 読み書き分離 | CQRS (Command Query Responsibility Segregation) | Greg Young "CQRS Documents" (2010) https://cqrs.files.wordpress.com/2010/11/cqrs_documents.pdf |
| イミュータブルなファクト | "Value of Values" | Rich Hickey, Datomic (2012) https://www.infoq.com/presentations/Value-Values/ |

**主要知見:**

1. **Event Sourcing (Fowler 2005)**: アプリケーション状態のすべての変化を順序付きイベントとして記録する。「現在の状態」を直接保存するのではなく、「状態変化のシーケンス」を保存する。これにより、過去の任意の時点の状態を再現でき、監査証跡が自然に生まれる。タスク完了（RESULT メッセージ）はまさにこの意味での「イベント」であり、追記専用 JSONL ファイルが最もシンプルな実装。

2. **CQRS (Greg Young 2010)**: 書き込みパス（append）と読み取りパス（query）を分離する。`ResultStore.append()` は低遅延の単純なファイル追記、`ResultStore.query()` は任意の複雑さのフィルタリングを担う。これにより書き込みが読み取りの複雑さに影響されない。

3. **Datomic "Value of Values" (Hickey 2012)**: 各レコードはイミュータブルで時刻スタンプ付きのファクト。更新・削除は一切しない。これにより並行書き込みの複雑さが大幅に減少（ロックはファイル追記の瞬間のみ必要）。

4. **JSONL フォーマット**: 1行1レコードの JSON Lines 形式は部分読み込み・ストリーミング処理が容易で、gzip 圧縮効率も高い。バイナリフォーマット（MessagePack, Avro）より可読性が高く、外部ツール（`jq`, `grep`）との親和性が高い。

5. **Thread safety**: `threading.Lock` によるアトミックなファイル追記。asyncio との混在（orchestrator は async、result store の append は同期）のため、`threading.Lock`（asyncio ロックではなく）を使用。これにより asyncio イベントループをブロックせず、短い I/O はブロッキングでも許容範囲内。

**設計決定:**

- **JSONL ファイルは日付単位**: 1日1ファイル (`YYYY-MM-DD.jsonl`)。単一ファイルに全期間を集約すると時系列クエリがスキャン全件になるが、日付単位なら特定日のみスキャン可能。ローテーションも単純。
- **`result_store_enabled=False` がデフォルト**: 予期しない I/O を避ける。永続化が必要な場合のみ YAML で有効化する保守的設計。
- **`result_text` は 4000 文字でトランケート**: タスク出力が数万行になりうる (LLM 出力)。完全な出力は `_buffer_director_result()` の 40 行テール抽出と同様に過剰なディスク使用を防ぐためトランケート。完全な出力が必要なら agent の worktree ファイルを参照すれば良い。
- **`prompt` は 500 文字でトランケート**: 識別・デバッグには十分。
- **`_record_agent_history()` から統合呼び出し**: `_task_started_at` と `_task_started_prompt` がすでにポップされた後のデータ（duration, prompt）を再利用できるため、`_record_agent_history` の末尾で `_result_store.append()` を呼ぶ設計が最も自然。例外は `logger.exception()` でサイレント処理し、result store の失敗でタスク処理全体を止めない。

**テスト (23テスト, 合計409テスト):**
- `append()` が正しいファイルに有効な JSON 行を書き込む
- エラーフィールドの永続化
- 同日に複数レコード
- `query(agent_id=)` によるフィルタリング
- `query(task_id=)` によるフィルタリング
- `query(date=)` によるフィルタリング
- `query(limit=)` の上限適用
- `all_dates()` のソート順保証
- スレッドセーフ: 50スレッド並行 append → 全行有効 JSON
- REST `GET /results`: フィルタ動作・disabled 時の空リスト
- REST `GET /results/dates`: 日付一覧
- `Orchestrator._result_store` の有効化・無効化

**デモシナリオ (v0.24.0):**
- シナリオ: "Persistent Audit Trail"
- 2エージェント: analyst (温度データ分析 → analysis.txt), summarizer (analysis.txt 読み込み → summary.txt)
- `result_store_enabled=True`, `result_store_dir=/tmp/v024-results/`
- Orchestrator 停止後に JSONL ファイルを直接読み込んでリザルトを表示
- オーケストレーター再起動後も結果が生存することを実証
- デモフォルダ: `~/Demonstration/v0.24.0-result-persistence/`

### 10.20 調査記録 (v0.25.0, 2026-03-05)

**実装テーマ: Workflow DAG API — multi-step pipeline submission as a unit**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| タスク依存グラフ | DAG (Directed Acyclic Graph) | Apache Airflow DAG model https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html |
| パイプライン提出 | Workflow orchestration | Prefect "Modern Data Stack" https://www.prefect.io/guide/blog/modern-data-stack |
| 依存関係解決 | Topological sort / Register renaming | Tomasulo's algorithm (IBM J. Res. Dev. 1967); Cormen et al. "Introduction to Algorithms" 4th ed. §22.4 |
| ステートマシン | State machine workflow | AWS Step Functions https://docs.aws.amazon.com/step-functions/latest/dg/concepts-amazon-states-language.html |
| トポロジカルソート | Kahn's algorithm | Kahn, A.B. (1962). "Topological sorting of large networks". CACM 5(11):558–562 |

**主要知見:**

1. **Apache Airflow DAGモデル**: タスクを有向非巡回グラフ (DAG) で表現し、依存関係を宣言的に定義する。ノードはタスク、エッジは「このタスクが完了してから次を開始する」という制約。Airflowではタスク間の依存関係を`>>` 演算子で表現するが、RESTスキーマでは `depends_on: [local_id]` のリストで表現する。

2. **Tomasulo アルゴリズムとのアナロジー**: IBM の Robert Tomasulo (1967) が提案したアウトオブオーダー実行のレジスタリネーミング技術は、本実装の「local_id → global task_id の変換」と構造的に等価。local_id はユーザー空間の「レジスタ名」、global task_id はハードウェアの「物理レジスタ番号」。変換後、本来の依存関係が保持されたまま並列実行可能なタスクは即時ディスパッチされる。

3. **Kahn's アルゴリズム (1962)**: in-degree ベースのトポロジカルソート。O(V+E) の計算量で依存関係の順序を決定し、同時に閉路の検出も行う (result != len(steps) の場合、閉路が存在)。DFSベースの手法と異なり、ソート結果が直接実行順序になるため実装が自然。

4. **AWS Step Functions とのアプローチの違い**: Step Functionsは永続的なステートマシンとしてワークフローを定義し、各ステート遷移をサービス側で管理する。本実装はより軽量で、WorkflowManager は「完了の観察者」に徹し、実行制御はOrchestrator の既存の dispatch ループと depends_on メカニズムに委ねる。この分離により、workflow 機能追加がコアロジックに影響を与えない。

5. **Prefect のコンセプトとの比較**: Prefect は Python デコレータで DAG を定義し、再試行・スケジューリング・UIを提供する。本実装は「提出時点での DAG 定義」のみをサポートし、ランタイムでの再試行は既存の DLQ + retry 機構に委ねる。Prefect の "flow" 概念に対応するのが `WorkflowRun`、"task" に対応するのが `Task` である。

**設計決定:**

- **WorkflowManager は常に有効**: `result_store_enabled=False` がデフォルトだった ResultStore とは異なり、WorkflowManager はゼロオーバーヘッドなため常時インスタンス化。ワークフローが提出されない場合は `_runs` と `_task_to_workflow` が空のまま。
- **local_id → global task_id の変換はサーバーサイド**: クライアントが UUID を管理する必要がない。Apache Airflow のタスク ID がノード識別子として機能するのと同様に、local_id はワークフロー内の参照名として機能し、グローバル名前空間での衝突を避ける。
- **WorkflowManager は「観察者」パターン**: Orchestrator の dispatch ループを変更せず、RESULT メッセージ処理に `on_task_complete()` / `on_task_failed()` の呼び出しを追加するだけ。既存の `_completed_tasks` 管理と直交する。
- **validate_dag() の分離**: DAG 検証ロジック (Kahn's algorithm) を `workflow_manager.py` に独立した純粋関数として定義。テストが容易で、REST ハンドラ外での再利用が可能。
- **`WorkflowRun.status` の遷移**: `pending` → `running` (最初のタスク完了/失敗時) → `complete` (全成功) / `failed` (任意の失敗)。Prefect のフロー状態モデルに倣う。

**テスト (29テスト, 合計438テスト):**
- `WorkflowManager.submit()` がランを登録する
- `on_task_complete()` — 部分完了時: `running`
- `on_task_complete()` — 全完了時: `complete` + `completed_at` セット
- `on_task_failed()` — 即時 `failed`
- 未知の task_id は no-op
- `validate_dag()` — linear, diamond topology
- `validate_dag()` — 閉路検出で `ValueError`
- `validate_dag()` — 未知の local_id で `ValueError`
- `POST /workflows` — 正常ケース: workflow_id + task_ids マップ
- `POST /workflows` — local→global マッピングの正確性
- `POST /workflows` — depends_on が正しく変換される
- `POST /workflows` — 閉路で 400 返却
- `POST /workflows` — 未知 local_id で 400 返却
- `POST /workflows` — 認証なしで 401
- `GET /workflows` — 空リスト / 複数ワークフロー
- `GET /workflows/{id}` — 正常ケース / 404
- 統合テスト: `_route_loop` が RESULT を受信すると `WorkflowManager` が `complete` に遷移

**デモシナリオ (v0.25.0):**
- シナリオ: "3-Step Code Pipeline"
- 2エージェント: agent-implementer / agent-reviewer
- Task A: implementer が quicksort / mergesort / heapsort を実装 (`sorter.py`)
- Task B (after A): reviewer が `sorter.py` をレビューし `review.md` を書く
- Task C (after B): implementer が `review.md` を読んで修正 + エッジケーステスト追加
- `validate_dag()` + `WorkflowManager.submit()` で一括提出
- workflow の `pending` → `running` → `complete` 遷移を実証
- デモフォルダ: `~/Demonstration/v0.25.0-workflow-dag/`

---

### 10.21 調査記録 (v0.26.0, 2026-03-05)

**実装テーマ: Task-level Retry on Failure — per-task retry semantics**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| タスク再試行 | Dead Letter Queue / Redrive Policy | AWS SQS `maxReceiveCount` https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html |
| 一時障害耐性 | Retry Pattern | Netflix Hystrix retry https://github.com/Netflix/Hystrix |
| リトライポリシー | Resilience Policy | Polly .NET resilience library https://github.com/App-vNext/Polly |
| スーパーバイザー再起動 | Supervisor restart strategies | Erlang OTP `restart_one_for_one` https://www.erlang.org/docs/24/design_principles/sup_princ |

**主要知見:**

1. **AWS SQS `maxReceiveCount` / Redrive Policy**: SQS のメッセージが指定回数受信されても処理されない場合、Dead Letter Queue (DLQ) に転送される。本実装の `Task.max_retries` は `maxReceiveCount - 1` に相当し、`retry_count >= max_retries` 時に DLQ に転送される (`_dead_letter()` 呼び出し)。SQS の Visibility Timeout と同様に、タスクが再試行中はキューから一時的に消えた状態になる。

2. **Netflix Hystrix リトライ**: Hystrix では一時障害に対して `fallback` と `retry` を組み合わせる。本実装では `fallback` (代替エージェントへのルーティング) は未実装だが、`retry` はタスクレベルで実現。同じエージェントに再試行するため、エージェントがステートフルな場合は注意が必要 (将来的に `target_agent` 指定で別エージェントに再試行する拡張を検討)。

3. **Polly .NET Retry Policy**: Polly では `WaitAndRetry` ポリシーで指数バックオフ付きリトライを設定できる。本実装は現在バックオフなしで即座に再キューイングするが、将来的に `retry_delay` フィールドを追加することで Polly スタイルのウェイト付きリトライが可能。

4. **Erlang OTP `restart_one_for_one`**: OTP のスーパーバイザーは子プロセスの再起動を `max_restarts` (intensity) と `max_seconds` (period) で制御する。本実装の `max_retries` は OTP の `intensity` に相当し、超過時に DLQ に転送されるのが OTP の `permanent failure` に対応。

**設計決定:**

- **`Task.max_retries` と `Task.retry_count` フィールドの追加**: `agents/base.py` の `Task` dataclass に追加。idempotency_key や target_agent と同様に、タスクの属性として管理することでオーケストレーターの外部状態が不要になる。
- **`_active_tasks: dict[str, Task]`**: ディスパッチ時にタスクオブジェクトを保存し、RESULT 受信時に再試行可否を判断できるようにする。成功時・最終失敗時には削除。
- **`WorkflowManager.on_task_retrying()`**: ワークフローが再試行中のタスクで `"failed"` に遷移しないよう、`_failed` セットからタスクを除去し `_update_status()` を再計算する。これにより、ワークフローが "failed" → "running" → "complete" の正常遷移を辿ることができる。
- **`GET /tasks` の拡張**: キュー内 + 実行中 + 完了済みタスクを統合してリストアップ。`skip`/`limit` でページネーション。REST クライアントが全タスクの状態を一覧できる。
- **`GET /tasks/{task_id}` の追加**: 特定タスクの状態 + リトライフィールドを取得できる新エンドポイント。

**テスト (30テスト, 合計468テスト):**
- `Task` デフォルト値: `max_retries=0`, `retry_count=0`
- `Task.to_dict()` にリトライフィールドが含まれる
- `WorkflowManager.on_task_retrying()` — unknown task_id は no-op
- `WorkflowManager.on_task_retrying()` — `_failed` セットから除去
- `WorkflowManager` — retrying → complete の遷移
- `max_retries=0` のタスクは初回エラーで即時失敗 (1回のみディスパッチ)
- `max_retries=2` のタスクは合計3回ディスパッチ (1 + 2 retry)
- `retry_count` が再試行ごとに 0, 1, 2 と増加する
- `task_retrying` STATUS イベントが各リトライで発行される
- `task_retrying` イベントには `error`, `retry_count`, `max_retries` が含まれる
- 同一 priority で再キューイングされる
- 再試行後に成功するタスクは `_completed_tasks` に追加される
- `max_retries` 消耗後は DLQ に転送 (再ディスパッチなし)
- ワークフロー: 再試行中は `"failed"` にならない
- ワークフロー: `max_retries` 消耗後に `"failed"` に遷移
- ワークフロー: 再試行成功後に `"complete"` に遷移
- REST `POST /tasks` — `max_retries=3` がレスポンスに含まれる
- REST `POST /tasks/batch` — 各タスクに `max_retries`/`retry_count` が含まれる
- REST `POST /workflows` — タスクスペックに `max_retries` が受け付けられる
- REST `GET /tasks` — 空/キュー済み/ページネーション(skip/limit)/認証
- REST `GET /tasks/{id}` — `retry_count`/`max_retries` フィールドを返す / 404

**デモシナリオ (v0.26.0):**
- シナリオ: "Flaky Task Retry"
- デモフォルダ: `~/Demonstration/v0.26.0-task-retry/`
- 2 ClaudeCodeAgent インスタンス + `max_retries=2` のワークフロータスク
- 初回 50% 確率で失敗するスクリプトを使い、再試行で成功することを実証
- `task_retrying` STATUS イベントと最終 `GET /workflows/{id}` で `"complete"` を確認

---

### 10.22 調査記録 (v0.27.0, 2026-03-05)

**実装テーマ: Task Cancellation — queued and in-progress task cancellation**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| キャンセレーション信号 | POSIX SIGTERM/SIGKILL model | POSIX.1-2017 §12.2 — Signal concepts; `SIGINT`/`SIGTERM`/`SIGKILL` |
| 協調キャンセレーション | Go `context.Context` cancellation | Rob Pike "Go Concurrency Patterns: Context" (2014) https://go.dev/blog/context |
| 実行中タスクキャンセル | Java `Future.cancel(mayInterruptIfRunning)` | Java SE 21 `java.util.concurrent.Future` javadoc https://docs.oracle.com/en/java/docs/api/java.base/java/util/concurrent/Future.html |
| グレースフルシャットダウン | Kubernetes Pod deletion grace period | Kubernetes docs "Termination of Pods" https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination |

**主要知見:**

1. **POSIX SIGTERM/SIGKILL model**: UNIX プロセスキャンセレーションの標準パターンは「まず SIGTERM で協調終了を要求し、タイムアウト後に SIGKILL で強制終了する」二段階プロセス。本実装では `interrupt()` が SIGTERM 相当 (Ctrl-C / SIGINT をエージェントプロセスに送信)、オーケストレーターがその後の RESULT を tombstone で廃棄するのが「後始末」に相当する。SIGKILL 相当 (強制停止) は `agent.stop()` が担う。

2. **Go `context.Context` cancellation**: Go の context パッケージはキャンセレーションシグナルをコールスタック全体に伝播させる。`context.WithCancel()` は `cancel()` 関数を返し、呼び出し元がいつでもキャンセルできる。本実装の `_cancelled_task_ids` セットは「context のキャンセルフラグ」に相当し、`_dispatch_loop` と `_route_loop` がそれを確認して処理をスキップする。

3. **Java `Future.cancel(mayInterruptIfRunning=true)`**: Java の `Future.cancel(true)` は実行中スレッドに `InterruptedException` を投げる。本実装の `agent.interrupt()` がこれに相当し、tmux pane に Ctrl-C を送信することで実行中プロセスに SIGINT が届く。`mayInterruptIfRunning=false` 相当 (キュー内のみキャンセル) は元の実装。新実装は `true` を常に意味する。

4. **Kubernetes Pod deletion grace period**: Kubernetes では `kubectl delete pod` が SIGTERM を送り、`terminationGracePeriodSeconds`（デフォルト 30s）の間プロセスが自発的に終了するのを待ち、その後 SIGKILL で強制終了する。本実装では `interrupt()` (Ctrl-C) 後、エージェントが prompt に戻るまで `_wait_for_completion` ポーリングが続く。tombstone (`_cancelled_task_ids`) が RESULT を廃棄することで「grace period 後のクリーンアップ」を実現。

**設計決定:**

- **tombstone セット (`_cancelled_task_ids`)**: `asyncio.PriorityQueue` は任意アイテム削除をサポートしないため、tombstone アプローチを採用。`cancel_task()` でセットに追加し、`_dispatch_loop` がデキュー時にスキップ、`_route_loop` が RESULT 受信時に廃棄する。これにより再エントランス安全で、ヒープの整合性を壊さない。
- **`interrupt()` のデフォルト no-op**: `Agent` 抽象クラスの `interrupt()` は非抽象メソッド (デフォルト `return False`)。すべての Agent サブクラスが interrupt を実装する必要はなく、`ClaudeCodeAgent` のみが tmux pane への Ctrl-C を実装。将来の HTTP/gRPC エージェントは HTTP キャンセレーションリクエストを実装できる。
- **`_route_loop` での RESULT 廃棄**: キャンセルされた in-progress タスクの RESULT は silent discard。workflow callbacks (`on_task_complete`/`on_task_failed`)、`reply_to` routing、`_agent_history` 記録、`_completed_tasks` 追加、いずれも実行しない。完全なキャンセルセマンティクスを保証する。
- **`WorkflowManager.cancel()` + no-op callbacks**: ワークフローキャンセル後に遅れて到着する RESULT が `on_task_complete`/`on_task_failed` を呼び出しても状態が汚染されない。これは Kubernetes の `DeletionTimestamp` パターンに類似 — リソースが「削除中」状態である間、コントローラーは新しい操作を受け付けない。
- **`DELETE /tasks/{id}` vs `POST /tasks/{id}/cancel`**: 既存の `POST /tasks/{id}/cancel` はキュー内のみキャンセル。新しい `DELETE /tasks/{id}` は in-progress を含む完全キャンセル。REST の DELETE セマンティクス「リソースを削除する」に一致。Kubernetes の `kubectl delete` も同様に DELETE メソッドを使用。

**テスト (29テスト, 合計497テスト):**
- `Agent.interrupt()` デフォルト実装は no-op で `False` を返す
- `cancel_task()` — unknown task_id は `False` を返す
- `cancel_task()` — queued task: `True`、キューから削除
- `cancel_task()` — queued task: STATUS `task_cancelled` (was_running=False) を発行
- `cancel_task()` — 他のキュー内タスクに影響しない
- `cancel_task()` — in-progress task: `True`、`_cancelled_task_ids` に追加
- `cancel_task()` — in-progress task: `agent.interrupt()` が呼ばれる
- `cancel_task()` — in-progress task: STATUS `task_cancelled` (was_running=True) を発行
- `_route_loop` — キャンセル済み RESULT は廃棄、`_completed_tasks` に追加されない
- `_route_loop` — キャンセル済み RESULT でワークフロー callback は呼ばれない
- `WorkflowManager.cancel()` — status が "cancelled" になる
- `WorkflowManager.cancel()` — `completed_at` が設定される
- `WorkflowManager.cancel()` — unknown id は空リストを返す
- `WorkflowManager.on_task_complete()` — "cancelled" 後は no-op
- `WorkflowManager.on_task_failed()` — "cancelled" 後は no-op
- `cancel_workflow()` — すべてのキュー内タスクをキャンセル
- `cancel_workflow()` — unknown workflow_id は `None` を返す
- `cancel_workflow()` — 部分完了タスクは `already_done` に分類
- REST `DELETE /tasks/{id}` — queued task: 200 + `cancelled=true`
- REST `DELETE /tasks/{id}` — in-progress task: 200 + `cancelled=true`
- REST `DELETE /tasks/{id}` — unknown: 404
- REST `DELETE /tasks/{id}` — 認証なし: 401
- REST `DELETE /workflows/{id}` — known: 200 + `cancelled`/`already_done` リスト
- REST `DELETE /workflows/{id}` — unknown: 404
- REST `DELETE /workflows/{id}` — 認証なし: 401
- `ClaudeCodeAgent.interrupt()` — `pane.send_keys("C-c")` を呼び出して `True` を返す
- `ClaudeCodeAgent.interrupt()` — pane なしは `False` を返す
- retry 付き in-progress task のキャンセル — RESULT 廃棄後 `_active_tasks` がクリーンアップ
- 既存テスト更新: `test_cancel_dispatched_task_returns_false` → `test_cancel_dispatched_task_returns_true`

**デモシナリオ (v0.27.0):**
- シナリオ: "Task Cancellation Mix"
- デモフォルダ: `~/Demonstration/v0.27.0-task-cancellation/`
- 2 DemoAgent インスタンス (slow + fast)
- 5 タスクを提出 → 3/4 をキュー中にキャンセル → Task-1 の実行中キャンセル (interrupt 呼び出し確認)
- 残り 2 タスク (Task-2, Task-5) が正常完了
- 最終サマリ: completed=2, cancelled_queued=2, cancelled_running=1

---

### 10.23 調査記録 (v0.28.0, 2026-03-05)

**実装テーマ: Agent Drain / Graceful Shutdown**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| グレースフルシャットダウン猶予期間 | Kubernetes Pod `terminationGracePeriodSeconds` | Kubernetes docs "Termination of Pods" https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination |
| プロセス再起動時のコネクションドレイン | HAProxy graceful restart | HAProxy docs "Graceful Reload" https://www.haproxy.com/blog/zero-downtime-restarts-with-haproxy |
| ソケットのグレースフルクローズ | UNIX `SO_LINGER` | Stevens "UNIX Network Programming" Vol.1 §7.5; POSIX.1-2017 `setsockopt(SO_LINGER)` |
| コンテナ停止猶予期間 | AWS ECS `stopTimeout` | AWS docs "Amazon ECS task definition parameters — Container definitions" https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html |

**主要知見:**

1. **Kubernetes Pod `terminationGracePeriodSeconds`**: Kubernetes は Pod 削除時に
   SIGTERM を送り、`terminationGracePeriodSeconds` (デフォルト 30s) 後に SIGKILL を送る。
   本実装の DRAINING 状態はこの「SIGTERM を送った後、タスク完了を待つ」猶予期間に相当する。
   Kubernetes のアプローチは「プロセスが自分でクリーンアップする機会を与える」という点で
   本実装と一致している。

2. **HAProxy graceful restart**: HAProxy はリロード時に新プロセスを起動し、既存の
   コネクションが完了するまで旧プロセスを生かし続ける。`nbthread` やファイルディスクリプタを
   新プロセスに引き渡しつつ、旧プロセスは「もう新コネクションを受け付けない」状態になる。
   本実装の DRAINING エージェントが「新タスクを受け付けない」点が直接対応する。

3. **UNIX `SO_LINGER`**: `SO_LINGER` オプションを設定した TCP ソケットは `close()` 呼び出し後も
   送信バッファのデータが相手方に届くまで `close()` をブロックする。本実装では「エージェントを
   ドレイン状態にして RESULT が返るまで stop() を呼ばない」点が `SO_LINGER` のセマンティクスと
   構造的に同一である。

4. **AWS ECS `stopTimeout`**: ECS は `SIGTERM` 送信後 `stopTimeout` 秒待ち、タイムアウト後に
   `SIGKILL` を送る。本実装にはタイムアウト上限を設けていない (タスクが完了するまで無限待ち)
   が、既存の Watchdog (`_watchdog_loop`) が過度に長い BUSY 状態を検出して強制 RESULT を
   発行するため、実質的にタイムアウトが機能する。

**設計上の注意点:**

- `AgentStatus.DRAINING` は `_set_idle()` に追加された STOPPED/ERROR と同様の「IDLEに戻らない」
  ガードによって保護される。これにより、`_dispatch_task()` 内で `_set_idle()` が呼ばれても
  DRAINING 状態が失われない。
- `find_idle_worker()` は `agent.status != AgentStatus.IDLE` の判定で DRAINING エージェントを
  自動的にスキップするため、レジストリ側に追加のフィルタリングロジックは不要。
- DRAINING 中にキャンセル操作 (`cancel_task()`) は従来どおり動作する — DRAINING エージェントの
  現在のタスクもキャンセル可能。

**デモシナリオ (v0.28.0):**
- シナリオ: "Agent Drain — graceful shutdown"
- デモフォルダ: `~/Demonstration/v0.28.0-agent-drain/`
- 3 ClaudeCodeAgent インスタンスに各 1 タスクを提出
- 最初のタスクがディスパッチされた後、そのエージェントを `POST /agents/{id}/drain`
- 最初のエージェントがタスク完了後に自動停止
- 残り 2 エージェントが正常完了
- `POST /orchestrator/drain` で残りエージェントを一括ドレイン

---

## 11. 今後の課題

### 機能

| 優先度 | 課題 |
|--------|------|
| ~~高~~ ~~エージェントの ERROR 状態からの自動リカバリ~~ | **完了 (v0.12.0)** — `_recovery_loop` + 指数バックオフ、`agent_recovered`/`agent_recovery_failed` イベント (Issue #3 クローズ) |
| ~~高~~ ~~Director → ユーザーへの非同期プッシュ通知（現在はポーリング）~~ | **完了 (v0.12.0)** — SSE `/events` エンドポイント; Web UI の 3s ポーリングを SSE に置換 |
| ~~中~~ ~~Web UI のエージェント階層ビジュアライゼーション（ツリー表示）~~ | **完了 (v0.11.0)** — `/agents/tree` エンドポイント + List/Tree トグル |
| ~~中~~ ~~タスク結果を親エージェントのメールボックスに直接配送 (`reply_to`)~~ | **完了 (v0.14.0)** — `Task.reply_to` フィールド + `Orchestrator._route_result_reply()` + REST `POST /tasks` の `reply_to` パラメータ |
| ~~中~~ ~~`POST /tasks/batch` — 複数タスクを一括提出~~ | **完了 (v0.15.0)** — `TaskBatchSubmit` + `POST /tasks/batch`; AHC デモで 3 タスクを並列提出 |
| ~~中~~ ~~エージェント能力タグ + スマートディスパッチ~~ | **完了 (v0.18.0)** — `AgentConfig.tags` + `Task.required_tags` + `find_idle_worker(required_tags)` + REST `required_tags` パラメータ; 23 テスト |
| ~~中~~ ~~キューポーズ/レジューム + タスク優先度ライブ更新~~ | **完了 (v0.19.0)** — `POST /orchestrator/pause|resume`, `GET /orchestrator/status`, `PATCH /tasks/{id}`; 18 テスト (302 合計) |
| ~~中~~ ~~Rate limiting / バックプレッシャー (Token Bucket)~~ | **完了 (v0.20.0)** — `TokenBucketRateLimiter` + `GET /rate-limit`, `PUT /rate-limit`; `OrchestratorConfig.rate_limit_rps/burst`; `wait_for_token` param; 24 テスト (326 合計) |
| 中 | `/plan` と `/tdd` の出力を RESULT メッセージとして親に自動送信 |
| ~~高~~ ~~キュー深度オートスケーリング~~ | **完了 (v0.23.0)** — `AutoScaler` MAPE-Kループ; `GET/PUT /orchestrator/autoscaler`; `OrchestratorConfig.autoscale_*`; 23テスト (386合計) |
| ~~中~~ ~~タスク結果の永続化（Event Sourcing）~~ | **完了 (v0.24.0)** — `ResultStore` 追記専用 JSONL; `GET /results`, `GET /results/dates`; `OrchestratorConfig.result_store_enabled/dir`; 23テスト (409合計) |
| ~~高~~ ~~Workflow DAG API — パイプライン一括提出~~ | **完了 (v0.25.0)** — `WorkflowManager` + `validate_dag()` + `POST/GET /workflows`; Kahn's algorithm; local_id→task_id変換; 29テスト (438合計) |
| ~~高~~ ~~Task retry on failure — per-task retry semantics~~ | **完了 (v0.26.0)** — `Task.max_retries`/`retry_count`; `_active_tasks` lookup; `task_retrying` STATUS イベント; `WorkflowManager.on_task_retrying()`; `GET /tasks` 全タスク一覧 (pagination); `GET /tasks/{id}`; REST `max_retries` パラメータ; 30テスト (468合計) |
| ~~高~~ ~~Task cancellation — queued and in-progress~~ | **完了 (v0.27.0)** — `Agent.interrupt()`; `ClaudeCodeAgent.interrupt()` (Ctrl-C via tmux); `_cancelled_task_ids` tombstone; `cancel_task()` handles both queued and in-progress; `_dispatch_loop` tombstone check; `_route_loop` RESULT discard; `cancel_workflow()`; `WorkflowManager.cancel()` + no-ops after cancel; `DELETE /tasks/{id}`; `DELETE /workflows/{id}`; 29テスト (497合計) |
| ~~高~~ ~~Agent drain / graceful shutdown~~ | **完了 (v0.28.0)** — `AgentStatus.DRAINING`; `_set_idle()` DRAINING ガード; `Orchestrator._draining_agents: set[str]`; `drain_agent()` (IDLE即停止/BUSY→DRAINING); `drain_all()`; `_route_loop` RESULT後の auto-stop; `POST /agents/{id}/drain`; `GET /agents/{id}/drain`; `POST /orchestrator/drain`; 23テスト (520合計) |
| ~~低~~ ~~エージェントのコンテキスト使用量モニタリング~~ | **完了 (v0.21.0)** — `ContextMonitor` + `GET /agents/{id}/stats`, `GET /context-stats`; `context_warning`/`notes_updated`/`summarize_triggered` STATUS イベント; `context_auto_summarize`; 21 テスト (347 合計) |
| ~~低~~ ~~ERROR エージェントの手動リセットエンドポイント (`POST /agents/{id}/reset`)~~ | **完了 (v0.13.0)** — `Orchestrator.reset_agent()` + REST `POST /agents/{id}/reset` |
| ~~低~~ ~~Prometheus メトリクス (`/metrics`)~~ | **完了 (v0.13.0)** — `GET /metrics` (prometheus_client 直接使用; 認証不要) |

### デモシナリオ候補

| 優先度 | シナリオ | パターン |
|--------|----------|---------|
| ~~**高**~~ ~~**AtCoder Heuristic Contest (AHC) best-of-N**~~ | **完了 (v0.15.0)** — 3 ClaudeCodeAgent 並列実行、Weighted Knapsack 問題、`POST /tasks/batch` で一括提出、スコアで勝者選択 |
| ~~中~~ ~~Director → Workers でマイクロサービス API を分割実装~~ | **完了 (v0.17.0)** — 4 ClaudeCodeAgent 並列、3 ワーカーが FastAPI エンドポイントを並列実装、Director が `integration_report.md` 生成 |
| ~~中~~ ~~agent-a が実装 → agent-b がレビュー → agent-a が修正~~ | **完了 (v0.16.0)** — Peer review pipeline デモ |
| ~~中~~ ~~能力タグによる専門エージェント自動選択~~ | **完了 (v0.18.0)** — python-expert / docs-writer; タスクが正しいエージェントにのみ配送されることを実証 |
| ~~中~~ ~~ポーズ/レジューム + 優先度ライブ更新デモ~~ | **完了 (v0.19.0)** — 3 agents, WIS best-of-N, ポーズ中に3タスク投入, PATCH で優先度変更, レジューム後に優先度順ディスパッチ実証 |
| ~~中~~ ~~Rate limit + Graph Coloring best-of-N~~ | **完了 (v0.20.0)** — 3 agents (greedy/backtrack/local), rate_limit_rps=3.0 burst=3, GET/PUT /rate-limit 実証, Graph Coloring 15-node 22-edge K=4 |
| ~~低~~ ~~Context monitor + TSP best-of-N~~ | **完了 (v0.21.0)** — 3 agents (nearest-neighbor/2-opt/random-restart), GET /context-stats 実証, TSP N=10 cities |
| ~~高~~ ~~Dynamic Agent Creation — コードレビューパイプライン~~ | **完了 (v0.22.0)** — テンプレート0で起動 → `create_agent()` で generator/reviewer を動的追加 → fibonacci.py 生成 → REVIEW.md 生成; デモフォルダ: `~/Demonstration/v0.22.0-dynamic-agents/` |
| ~~高~~ ~~Queue-Depth Autoscaling — バースト負荷処理~~ | **完了 (v0.23.0)** — 0エージェントで起動 → AutoScaler (min=0, max=3, threshold=2) が6タスクバーストを検出 → 3エージェントを動的作成 → クールダウン後にスケールゼロ; デモフォルダ: `~/Demonstration/v0.23.0-autoscaling/` |
| ~~中~~ ~~Persistent Audit Trail — 結果永続化~~ | **完了 (v0.24.0)** — analyst + summarizer, result_store_enabled=True, Orchestrator停止後も JSONL ファイルが残ることを実証; デモフォルダ: `~/Demonstration/v0.24.0-result-persistence/` |
| ~~高~~ ~~Workflow DAG — 3-Step Code Pipeline~~ | **完了 (v0.25.0)** — agent-implementer + agent-reviewer, Task A→B→C (実装→レビュー→修正+テスト), `validate_dag()` + `WorkflowManager` で一括提出; デモフォルダ: `~/Demonstration/v0.25.0-workflow-dag/` |

**AHC best-of-N デモ完了 (v0.15.0)**:
- 問題: Weighted Knapsack (N=15, C=50) — 最適解 score=154 (DP で検証済み)
- 3 エージェント並列実行: agent-greedy (greedy by v/w ratio), agent-random (Monte Carlo 10k trials), agent-dp (0-1 DP exact)
- `POST /tasks/batch` で3タスクを一括提出 (新機能)
- 各エージェントが solver スクリプトを書いて実行 → solution ファイルに出力
- `score.py` で各 solution を検証・スコア計算
- オーケストレーターが最高スコアを選択して勝者を表示
- デモフォルダ: `~/Demonstration/v0.15.0-ahc-best-of-n/`

**Peer Review Pipeline デモ完了 (v0.16.0)**:
- 2 エージェント 3 フェーズ順次パイプライン
- Phase 1: agent-author が `data_processor.py` を実装 (CSV, filter, aggregate)
- Phase 2: agent-reviewer が `review.md` (MODERATE, 5 edge cases) + `test_data_processor.py` (12 tests) を書く
- Phase 3: agent-author がレビューを読んで `data_processor.py` を改善 (RFC 4180, エラー処理, 空文字対応)
- `target_agent` ルーティング (v0.16.0) でタスクが正しいエージェントに届くことを保証
- 共有スクラッチパッド (v0.16.0) でレビュー要約を報告
- デモフォルダ: `~/Demonstration/v0.16.0-peer-review-pipeline/`

### アーキテクチャ

| 優先度 | 課題 |
|--------|------|
| ~~高~~ ~~CLAUDE.md の動的更新（タスク変更時に役割説明を更新）~~ | **クローズ**: タスク生存期間 = エージェント生存期間の原則により不要。Workers は ephemeral であるべき (Issue #4, 2026-03-05) |
| ~~中~~ ~~コンテキストファイルの自動コピー (`context_files` の実装)~~ | **完了 (v0.11.0)** — `ClaudeCodeAgent._copy_context_files()` |
| ~~中~~ ~~`/summarize` による NOTES.md 更新をオーケストレーターに通知~~ | **完了 (v0.21.0)** — `ContextMonitor._check_notes_updated()` が mtime 変化を検出し `notes_updated` STATUS イベントを発行 |
| ~~低~~ ~~エージェント間の共有スクラッチパッド~~ | **完了 (v0.16.0)** — `GET/PUT/DELETE /scratchpad/{key}` REST API; Blackboard パターン; 17 テスト |
| ~~高~~ ~~Directorが実行時にエージェントを動的追加~~ | **完了 (v0.22.0)** — `Orchestrator.create_agent()` + `POST /agents/new` + CONTROL `create_agent`; テンプレート不要で agent_id/tags/system_prompt/isolate/command を指定して即時起動 (Issue #5) |
| ~~中~~ ~~worktreeからmainブランチへのコミット還元~~ | **完了 (v0.22.0)** — `WorktreeManager.teardown(merge_to_base=True)` でスカッシュマージ; `keep_branch()` でブランチ保持; `AgentConfig.merge_on_stop=True` で自動化 |
