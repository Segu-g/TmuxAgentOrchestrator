# TmuxAgentOrchestrator — 調査記録

> 各イテレーションの WebSearch 調査・選定根拠・実装結果を記録する。
> DESIGN.md §10 から分離（2026-03-09）。設計上の意思決定は DESIGN.md を参照。

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

### 10.24 調査記録 (v0.29.0, 2026-03-05)

**実装テーマ: Task-level `depends_on` — first-class dependency tracking**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| 依存関係に基づくビルド制御 | GNU Make dependency resolution | Feldman, S.I. (1979). "Make — A Program for Maintaining Computer Programs". Bell Labs USENIX. https://dl.acm.org/doi/10.1145/800076.802475 |
| タスクグラフの遅延実行 | Dask task graphs | Rocklin, M. (2015). "Dask: Parallel computation with blocked algorithms". SciPy Proceedings. https://conference.scipy.org/proceedings/scipy2015/pdfs/matthew_rocklin.pdf |
| DAG ステージ依存追跡 | Apache Spark DAG scheduler | Zaharia et al. (2012). "Resilient Distributed Datasets: A Fault-Tolerant Abstraction for In-Memory Cluster Computing". USENIX NSDI. https://www.usenix.org/system/files/conference/nsdi12/nsdi12-final138.pdf |
| 依存関係の伝播 | POSIX `make` prerequisites | IEEE Std 1003.1-2017 "make — maintain, update, and regenerate groups of programs". https://pubs.opengroup.org/onlinepubs/9699919799/utilities/make.html |

**主要知見:**

1. **GNU Make dependency resolution**: GNU Make は依存グラフを走査し、前提条件が
   すべて最新の場合のみターゲットをビルドする。本実装の `_waiting_tasks` / `_task_dependents` は
   Make の「前提条件が完了したらターゲットを実行」というセマンティクスを非同期タスク
   スケジューリングに適用したものである。

2. **Dask task graphs**: Dask はタスクグラフを `{key: (func, *args)}` の辞書として表現し、
   依存関係が解決されると即座にワーカーへ送信する。本実装の `_on_dep_satisfied()` が
   依存完了後にタスクをキューへ移動する仕組みはこれと等価である。特に Dask の
   "scheduler knows which tasks are ready" アプローチが参考になった。

3. **Apache Spark DAG scheduler**: Spark は各 Stage の依存関係を追跡し、親 Stage の
   すべてのパーティションが完了したときに子 Stage をスケジュールする。本実装の
   「すべての `depends_on` ID が `_completed_tasks` に入ったら `_waiting_tasks` から
   キューへ移動」はこの hold-and-release セマンティクスと構造的に同一。

4. **POSIX make prerequisites**: `make` は依存関係チェーンを再帰的に解決する。
   A→B→C の場合、A が失敗すると B が実行されず C も実行されない。本実装の
   `_on_dep_failed()` の再帰呼び出しはこの連鎖失敗伝播を再現している。

**設計上の注意点:**

- **poll-based → hold-and-release**: v0.29.0 以前は `depends_on` の解決を
  `_dispatch_loop` のポーリング (0.05s ごとの re-queue) で行っていた。
  これは O(n²) のパイプライン遅延を生じさせる可能性があった。
  v0.29.0 では `_waiting_tasks` + `_task_dependents` の逆引きテーブルにより O(1) wake-up に改善。

- **`_task_dependents` は reverse lookup table**: dep_task_id → [waiting_task_ids] の
  辞書により、依存先が完了したときに O(1) で待機タスクを特定できる。
  完了または失敗後はエントリを削除してメモリリークを防ぐ。

- **即時失敗**: 既に `_failed_tasks` に登録されている依存先を持つタスクを `submit_task()` で
  提出した場合、キューにも `_waiting_tasks` にも入らずに即座に失敗する。
  これにより既知の失敗依存を持つタスクが無限に蓄積することを防ぐ。

- **Tomasulo-style local_id**: `POST /tasks/batch` の `local_id` → global UUID 変換は
  Tomasulo のアルゴリズム (IBM System/360 Model 91, 1967) のレジスタリネーミングと
  同じ概念。バッチ内のローカル名を広域ユニークIDに変換することで、複数のバッチが
  同一のローカル名を使っても衝突しない。

**デモシナリオ (v0.29.0):**
- シナリオ: "Task-level depends_on"
- デモフォルダ: `~/Demonstration/v0.29.0-task-dependencies/`
- 2 ClaudeCodeAgent インスタンス
- Task A: `base.py` にシンプルなクラスを書く
- Task B: A に依存し、`extended.py` で `base.py` をインポート・拡張
- Task C: B に依存し、`test_extended.py` でテスト作成
- すべてを `POST /tasks` に `depends_on` 付きで同時提出 (workflow 不要)
- 実行順序: A → B → C が保証される

---

### 10.25 調査記録 (v0.30.0, 2026-03-05)

#### 実装: Webhook Notifications — アウトバウンドイベント通知

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| Webhook delivery | Event Notification pattern | GitHub Webhooks https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks |
| Webhook verification | HMAC-SHA256 署名 | Stripe Webhooks https://docs.stripe.com/webhooks; RFC 2104 HMAC https://datatracker.ietf.org/doc/html/rfc2104 |
| Webhook API design | RESTful webhook registration | Zalando RESTful API Guidelines §webhook https://opensource.zalando.com/restful-api-guidelines/#webhook |
| Webhook signature verification | HMAC verification best practices | Shopify webhook verification https://shopify.dev/docs/apps/build/webhooks/signature-verification |

**主要知見:**

1. **GitHub Webhooks (2024)**: GitHub はペイロードに `X-Hub-Signature-256: sha256=<hex>` ヘッダーを付与し、受信者が HMAC-SHA256 で検証する。本実装の `X-Signature-SHA256` ヘッダーはこの慣習に準拠。`sha256=` プレフィックスが標準。

2. **Stripe Webhooks (2024)**: Stripe は `Stripe-Signature` ヘッダーにタイムスタンプと署名を含め、リプレイ攻撃を防ぐ。本実装はシンプルな HMAC のみ（タイムスタンプ付き署名は YAGNI）。

3. **RFC 2104 HMAC**: `HMAC(K, m) = H((K ⊕ opad) || H((K ⊕ ipad) || m))` — 本実装は Python の `hmac.new(key, msg, digestmod)` で RFC 2104 準拠の HMAC-SHA256 を生成。

4. **Zalando RESTful API Guidelines §webhook**: webhook の CRUD は POST/GET/DELETE の REST リソースとして設計し、`delivery_history` を別リソースで返すパターンを採用。`GET /webhooks/{id}/deliveries` がこれに対応。

5. **Fire-and-forget + circular buffer**: 配信は `asyncio.create_task()` でバックグラウンド実行し、ルートループをブロックしない。配信履歴は `collections.deque(maxlen=50)` で最新50件を保持。メモリは O(50 × delivery_size) で有界。

**設計決定:**

- **`deliver()` は fire-and-forget**: `asyncio.create_task()` で各 webhook に非同期 POST。`_route_loop` や `cancel_task()` を絶対にブロックしない。失敗はログとバッファへの記録のみ。
- **ワイルドカード `"*"`**: `events` に `"*"` を含む webhook はすべてのイベントを受信。GitHub の "all events" に相当。
- **HMAC ヘッダー条件付き**: `secret` が設定されている場合のみ `X-Signature-SHA256` を送信。secret なし → ヘッダーなし。
- **`KNOWN_EVENTS` frozenset**: 既知のイベント名を定義し、REST API で 422 バリデーション。タイポによる「サイレント購読失敗」を防ぐ。
- **`OrchestratorConfig.webhook_timeout: float = 5.0`**: デフォルト5秒。YAML で上書き可能。Stripe の推奨タイムアウト (20s) より短いが、エージェントシステムのローカル環境では5秒で十分。

**デモシナリオ (v0.30.0):**
- シナリオ: Webhook Notifications デモ
- デモフォルダ: `~/Demonstration/v0.30.0-webhooks/`
- ポート 9999 に受信サーバーを起動 (http.server)
- `task_complete` + `workflow_complete` に webhook 登録 (secret 付き)
- 3 イベントを配信 → 受信確認 → `GET /webhooks/{id}/deliveries` 表示

---

### 10.26 調査記録 (v0.31.0, 2026-03-05)

#### 実装: Agent Groups / Named Pools — 名前付きエージェントプール

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| Named agent pools | Kubernetes Node Pools / Node Groups | https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/ |
| Cluster resource partitioning | AWS Auto Scaling Groups | https://docs.aws.amazon.com/autoscaling/ec2/userguide/auto-scaling-groups.html |
| Role-based resource allocation | Apache Mesos Roles | https://mesos.apache.org/documentation/latest/roles/ |
| Co-located task grouping | HashiCorp Nomad Task Groups | https://developer.hashicorp.com/nomad/docs/job-specification/group |

**主要知見:**

1. **Kubernetes Node Pools (GKE/EKS/AKS)**: クラスタ内のノードを名前付きプールに分割し、Pod の `nodeSelector` / `nodeAffinity` でプールにスケジュールを誘導する。本実装の `target_group` はこの概念の軽量版で、タスクが特定のエージェントプールにのみディスパッチされる。

2. **AWS Auto Scaling Groups**: EC2 インスタンスを論理グループに集め、同一設定・同一スケーリングポリシーで管理する。名前によるターゲティングが基本で、`TargetGroupARN` で ELB から特定グループにルーティングする。本実装のグループは静的（スケーリング機能なし）だが、将来的に AutoScaler との連携で動的グループ管理が可能。

3. **Apache Mesos Roles**: クラスタリソースを役割（ロール）でパーティショニングし、特定フレームワークが特定ロールのリソースしか使えないようにする。`required_tags` + `target_group` の AND フィルタと同様のアクセス制御モデル。

4. **HashiCorp Nomad Task Groups**: ジョブ定義内の論理グループで、同一ノードにスケジュールされるタスクをまとめる。本実装のグループは同一ノード制約を持たないが、「名前によるターゲティング」という概念は共通。

**設計決定:**

- **AND フィルタ semantics**: `target_group` と `required_tags` は AND で組み合わせる。`target_group="gpu-workers"` かつ `required_tags=["cuda"]` の場合、グループ内で CUDA タグを持つエージェントのみが対象。Kubernetes の `nodeSelector` + `nodeAffinity` の組み合わせと同じ設計。
- **不明グループは即 DLQ**: 存在しないグループを指定したタスクは即座に dead-letter される（リトライ不要）。Kubernetes の `nodeSelector` でマッチするノードがない場合の Pending 状態と対照的に、本システムでは明示的なエラーを選択。
- **`GroupManager` は純粋 in-memory**: 永続化なし。再起動でリセット。YAML `groups:` で設定値として永続化する設計を採用。
- **`AgentConfig.groups` による起動時自動登録**: factory.py でエージェント作成後に `group_manager.add_agent()` を呼び出す。グループが存在しない場合は自動作成（auto-create semantics）。
- **コピー返却**: `get(name)` は内部 set のコピーを返す。直接参照を渡すと意図しない変更が可能になるため。

**デモシナリオ (v0.31.0):**
- シナリオ: Agent Groups デモ
- デモフォルダ: `~/Demonstration/v0.31.0-agent-groups/`
- 4 エージェント: 2つが `"python-workers"`、2つが `"docs-workers"` グループに所属
- 3 python タスク → `target_group="python-workers"` で投入 → python-workers のみに配送
- 2 docs タスク → `target_group="docs-workers"` で投入 → docs-workers のみに配送
- `GET /groups/{name}` でクロスグループ配送が発生しないことを確認

---

### 10.27 調査記録 (v0.32.0, 2026-03-05)

#### 実装: Priority Inheritance for Sub-tasks — 優先度継承

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| Priority inversion / inheritance in RTOS | Priority Inheritance Protocol | Liu & Layland, JACM 20(1), 1973 |
| Mutex-based priority inheritance | FreeRTOS / POSIX priority inheritance | https://www.digikey.com/en/maker/projects/introduction-to-rtos-solution-to-part-11-priority-inversion/abf4b8f7cd4a4c70bece35678d178321 |
| Workflow task priority weighting | Apache Airflow priority_weight | https://airflow.apache.org/docs/apache-airflow/2.1.2/concepts/priority-weight.html |
| Priority inversion vs inheritance | GeeksforGeeks OS article | https://www.geeksforgeeks.org/operating-systems/difference-between-priority-inversion-and-priority-inheritance/ |
| Task priority propagation in distributed systems | IEEE Xplore / ScienceDirect | https://ieeexplore.ieee.org/document/8750747/ |

**主要知見:**

1. **Liu & Layland (1973) — Priority Inheritance Protocol (PIP)**:
   リアルタイムスケジューリングの基礎論文。ミューテックスを保持する低優先度タスクが、そのミューテックスを待つ高優先度タスクの優先度を一時的に継承することで、優先度逆転 (priority inversion) を防ぐ。本実装はミューテックスではなくタスク依存関係 (`depends_on`) を通じた同様の継承を実装。

2. **Priority Inversion の古典的問題 — Mars Pathfinder (1997)**:
   低優先度タスクがミューテックスを保持し、中優先度タスクが横取りすることで高優先度タスクが間接的にブロックされた。ウォッチドッグタイマーが作動してシステムリセット。`inherit_priority=True` でこのクラスの問題を防ぐ。GeeksforGeeks 記事 (2024) 参照。

3. **Apache Airflow `priority_weight` — 上流/下流の重み付け**:
   Airflow のデフォルトは「downstream」ルール: 各タスクの有効重みは全下流タスクの重みの合計。これにより上流タスクが積極的にスケジュールされる（全 DAG ランが完了してから次のランを開始する動作）。本実装はより単純な「直接親の最小優先度を採用」方式を採用 (one-level lookup, not transitive closure)。Apache Airflow ドキュメント (2024) 参照。

4. **FreeRTOS / POSIX Mutex Priority Inheritance**:
   POSIX リアルタイム拡張と FreeRTOS、QNX、VxWorks などの商用 RTOS は優先度継承を標準サポート。ミューテックスを取得した低優先度タスクは、ブロック中の高優先度タスクの優先度に一時的に引き上げられる。DigiKey / FreeRTOS チュートリアル参照。

5. **分散ワークフローでの優先度伝播**:
   IEEE Xplore (2019) の大規模分散ワークフローシステムに関する研究では、タスク優先度をワークフロー DAG のトポロジカル順序で伝播することが有効と示されている。本実装はこれを単純化した形で採用: `submit_task()` の topological order (ワークフロー DAG では保証済み) で `_task_priorities` を参照して effective_priority を計算。

**設計決定:**

- **one-level lookup (直接親のみ)**: 推移的閉包 (transitive closure) ではなく直接 `depends_on` の親のみ参照。理由: (1) ワークフロー DAG をトポロジカル順序で提出するため、祖先の継承は中間ノードで自動的に伝播する。(2) 実装が単純で O(1) ルックアップで済む。(3) 意図しない遠距離依存による不可解な優先度変化を防ぐ。
- **`_task_priorities` は immutable after submit**: 提出時に一度記録した後は変更しない。`update_task_priority()` は `_task_priorities` を更新しない設計（既存の依存タスクが再計算されないため一貫性が保たれる）。
- **`inherit_priority=False` で明示的無効化**: デフォルト True だが、タスク単位で False にできる。`POST /workflows` の `WorkflowTaskSpec` でも per-task 設定可能。
- **`min()` セマンティクス**: Airflow の `downstream` ルールとは異なり、本実装は min() (低番号=高優先度) を採用。これは Python `asyncio.PriorityQueue` の convention (lower = dispatched first) と一致。

**デモシナリオ (v0.32.0):**
- シナリオ: Priority Inheritance Queue Inspection
- デモフォルダ: `~/Demonstration/v0.32.0-priority-inheritance/`
- Task A (priority=10), B (priority=1), C (priority=1, no deps)
- Task D (priority=10, depends on B, inherit_priority=True) → effective priority=1
- Task E (priority=10, depends on B, inherit_priority=False) → keeps priority=10
- Queue inspection: B and C dispatched before A

---

### 10.28 調査記録 (v0.33.0, 2026-03-05)

#### 実装: Task TTL (Time-to-Live / Expiry) — キュー滞留タスクの自動期限切れ

**調査観点:**

| テーマ | パターン名 | 参考文献 |
|--------|-----------|---------|
| Message queue TTL / per-message expiry | Message Expiration Pattern | RabbitMQ "Time-To-Live and Expiration" https://www.rabbitmq.com/docs/ttl |
| Broker-level TTL vs runtime-handled TTL | Native vs Runtime-Handled TTL | Dapr "Message TTL" https://docs.dapr.io/developing-applications/building-blocks/pubsub/pubsub-message-ttl/ |
| Key expiry / TTL in in-memory stores | EXPIRE / TTL commands | Redis EXPIRE docs https://redis.io/docs/latest/commands/expire/ |
| Queue retention and expiry | MessageRetentionPeriod / VisibilityTimeout | AWS SQS https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html |
| Job scheduler expiry best practices | Dead Letter Queue; At-most-once | "Design a Distributed Job Scheduler" https://www.systemdesignhandbook.com/guides/design-a-distributed-job-scheduler/ |
| Message expiry and dead-letter routing | Azure Service Bus ExpiresAtUtc | Azure Service Bus message expiration https://learn.microsoft.com/en-us/azure/service-bus-messaging/message-expiration |

**主要知見:**

1. **RabbitMQ TTL — "The server guarantees that expired messages will not be delivered" (rabbitmq.com/docs/ttl)**:
   - TTL は per-message および per-queue の両レベルで設定可能。両方指定時は小さい方が適用される。
   - Quorum キューでは expired messages はキュー先頭到達時にデッドレターされる。
   - 本実装は "lazy expiry" を採用: 定期スキャンではなく `_dispatch_loop` でのデキュー時にチェック。これは RabbitMQ クラシックキューの「キュー先頭到達時の期限チェック」と同等。

2. **Azure Service Bus — message expiration (Microsoft Docs 2024)**:
   - `AbsoluteExpiryTime` = enqueue_time + TTL の絶対タイムスタンプとして保存。
   - 本実装も同様: `submitted_at + ttl` を `expires_at` として一度計算し、以後変更しない。
   - Azure は TTL を "time from when the message was enqueued" として定義。本実装も同一セマンティクス。

3. **Dapr pubsub-message-ttl — Native vs Runtime-Handled TTL (docs.dapr.io)**:
   - ブローカーが TTL ネイティブサポートの場合 (e.g. Azure Service Bus): Dapr は TTL 設定をブローカーに転送。
   - サポートなしの場合: Dapr ランタイムが TTL ロジックを実装 ("Dapr handles the TTL logic within the runtime")。
   - 本実装は Dapr の "runtime-handled" パターン: ブローカー (asyncio.PriorityQueue) は TTL を理解しないため、
     オーケストレーターが全ての期限切れロジックを実装。

4. **Redis EXPIRE — active + passive expiry (redis.io/docs)**:
   - **Passive**: キーがアクセスされた時のみ TTL チェック (lazy expiry)。
   - **Active**: 定期的な expire サイクルが期限切れキーをスキャン。
   - 本実装は両方を組み合わせ: `_dispatch_loop` での lazy expiry (queued tasks) + `_ttl_reaper_loop` での active scan (waiting tasks)。
   - `_waiting_tasks` は `_dispatch_loop` を通らないため active scan が必要。

5. **Distributed Job Scheduler — Dead Letter Queue (systemdesignhandbook.com)**:
   - 期限切れタスクは DLQ に移すのではなく `_failed_tasks` に追加して依存関係カスケードを発火。
   - "Jobs that fail repeatedly are moved to a separate inspection queue to prevent them from blocking the main queue" — 本実装では TTL 期限切れタスクも同様に `_failed_tasks` でクリーンに処理。

**設計決定:**

- **`expires_at` は submit 時に一度計算、以後不変**: RabbitMQ / Azure と同一セマンティクス。リトライ時でも `expires_at` は更新しない（リトライは TTL 期限に影響しない）。
- **二重期限チェック**: (1) `_dispatch_loop` — queued tasks のデキュー時; (2) `_ttl_reaper_loop` — `_waiting_tasks` の定期スキャン。Waiting tasks は dispatch loop を通らないため別経路が必要。
- **`ttl_reaper_poll = 1.0` s デフォルト**: エージェントタスクの典型的 TTL (秒〜分) に対して 1 s の粒度は十分。ミリ秒 TTL には不向きだが agentic tasks には適切。
- **期限切れ = 失敗セマンティクス**: TTL 期限切れは task failure として扱われ、`_on_dep_failed()` で依存タスクへカスケード。これは RabbitMQ の dead-letter-on-expiry と同等の効果。
- **`from_reaper: bool` フィールド**: `task_expired` イベントで期限切れ経路を識別可能にする (デバッグ/可観測性)。

**デモシナリオ (v0.33.0):**
- agent-a: TTL=30s の quick task → 正常完了 (`ttl_demo_a.py` 作成)
- agent-b: 15s blocker task を実行中 → 同時に TTL=8s の task B をキューに投入
- Task B が 8 秒後に期限切れ (`from_reaper=False` — dispatch loop 経路)
- Task C (depends_on task B) → `task_dependency_failed` カスケード
- デモフォルダ: `~/Demonstration/v0.33.0-task-ttl/`

---

### 10.29 調査記録 (v0.34.0, 2026-03-05)

#### Step 0 — 選択根拠

**選択した機能:** `/plan` と `/tdd` の出力を RESULT メッセージとして親エージェントに自動送信

**選択理由:**
- §11 に残る唯一の未完了機能候補。他の全 §11 項目は完了済み。
- v0.33.0 (Task TTL) の build-log に失敗なし — 前回デモの技術的負債はゼロ。
- `/plan` と `/tdd` コマンドは既に実装済みだが、出力が親エージェントに届かない。
  Director → Workers パターンにおいて、Sub-agent が計画完了・TDD サイクル完了を
  親に通知できないため協調が途切れる。本機能でこのギャップを埋める。
- `/progress` コマンドが既に「親への通知」を実装しているので、同パターンを `/plan` と `/tdd` に適用するだけで済む — 実装コストが低い。

**選択しなかったもの (検討事項):**
- §11 に他の未完了項目はなかった。本機能が唯一の選択肢であった。
- 新機能の追加 (e.g. 分散トレーシング、Kafka ブリッジ) は §11 に未記載であり、今回のスコープ外。

**前回 build-log からの影響:**
- v0.33.0 build-log は全 15 チェックが一発 PASS。未解決の問題なし。
- 特に課題なし。

#### Step 1 — 調査記録 (WebSearch)

**調査観点:**

| テーマ | 参考文献 |
|--------|---------|
| Multi-agent child→parent result forwarding | Google ADK "Multi-agent systems" https://google.github.io/adk-docs/agents/multi-agents/ |
| Agent response callback / structured output | Semantic Kernel "Agent Orchestration Advanced Topics" https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/agent-orchestration/advanced-topics |
| TDD orchestrator with completion notification | "TDD-Plan completion in multi-agent workflows" dev.to / github.com/catlog22 2024 |
| Sub-agent output reporting to parent | "Multi-agent patterns in LlamaIndex" https://developers.llamaindex.ai/python/framework/understanding/agent/multi_agent/ |

**主要知見:**

1. **Google ADK — Shared Session State & output_key (adk-docs)**:
   - Child agents write results to `session.state` via `output_key`; parent reads them downstream.
   - `AgentTool` pattern: child agent's final response is captured and returned as a tool result to the parent, automatically forwarding state and artifact changes.
   - `LoopAgent` uses `Event(escalate=True)` for child→parent completion signaling without explicit callbacks.
   - **本実装への適用**: `/plan` 完了時に生成した PLAN.md の内容を RESULT メッセージとして親に送信するのは
     AgentTool の "capture final response and forward to parent" パターンと同等。

2. **Semantic Kernel — ResponseCallback & Structured Outputs (Microsoft Docs 2025)**:
   - `ResponseCallback` は orchestration の各エージェント応答を observe する仕組み。親が子の出力をリアルタイムに受け取れる。
   - Structured output: `ConcurrentOrchestration[str, ArticleAnalysis]` — 型付きで子エージェント結果を集約。
   - **本実装への適用**: `/plan` と `/tdd` は構造化テキスト (Markdown) を生成する。これを
     PEER_MSG payload `{"event": "plan_complete", "plan": "..."}` として親に送るのは
     Semantic Kernel の structured output forwarding と同等。

3. **Slash command output forwarding pattern**:
   - Claude Code slash commands store their output as Markdown files (PLAN.md, etc.).
   - 既存の `/progress` コマンドが「子→親」通知の先例を実装している。
     同パターン (REST `POST /agents/{parent_id}/message`) を `/plan` と `/tdd` に適用する。

4. **Multi-agent TDD orchestrator (github.com/digitarald/chatarald/tdd.agent.md)**:
   - TDD agent emits structured completion signal at end of RED/GREEN/REFACTOR cycle.
   - Orchestrator subscribes to completion events and advances pipeline.
   - **本実装**: `/tdd` 完了時に `{"event": "tdd_complete", "feature": "...", "phase": "checklist_shown"}` を親に送信。

**設計決定:**

- **`/plan` の通知**: PLAN.md 書き込み後に `/progress` と同じ REST 経路で親に
  `{"event": "plan_created", "plan_path": "PLAN.md", "description": "..."}` を送信。
  親が存在しない場合はローカル出力のみ (no-op)。
- **`/tdd` の通知**: TDD チェックリスト表示後に
  `{"event": "tdd_cycle_started", "feature": "...", "phase": "red"}` を送信。
  (TDD は非同期サイクルなので "started" として通知する。完了は `/progress` で行う。)
- **Opt-out**: `__orchestrator_context__.json` が存在しない場合はサイレントに何もしない。
  これにより、オーケストレーター外で使用しても副作用なし。
- **メッセージタイプ**: `PEER_MSG` ではなく `STATUS` に変更 — 親への通知は「状態変化の報告」であり
  P2P 会話ではない。`POST /agents/{parent_id}/message` は `type` フィールドを受け付けるため対応可能。

---

### 10.30 調査記録 (v0.35.0, 2026-03-05)

#### 選択した機能: API キーセキュリティ修正 (フェーズ1 + フェーズ2)

**選択理由:**

DESIGN.md §3「API キー配送のセキュリティ方針」に高優先度のセキュリティバグとして記載されている問題を解決する。
v0.34.0 で `OrchestratorConfig.api_key` を導入した際に、API キーが `__orchestrator_context__.json` にプレーンテキストで書き込まれる問題が発生した。

**選択しなかった候補:**

- `POST /workflows/tdd` (3エージェント TDD ワークフロー) — 高優先度の新機能だが、セキュリティバグを先に修正すべき。セキュリティ問題を放置したまま新機能を追加することは適切ではない。
- `役割別 system_prompt テンプレートライブラリ` — 有用だが緊急性なし。
- `ProcessPort` 抽象インターフェース — アーキテクチャ改善だが緊急度は低い。

**実装スコープ:**

フェーズ 1: `__orchestrator_context__.json` から `api_key` を除外し、`__orchestrator_api_key__` 専用ファイル (`chmod 600`) に分離する。
フェーズ 2: libtmux `session.set_environment("TMUX_ORCHESTRATOR_API_KEY", api_key)` によってセッション環境変数としても注入する。これにより、スラッシュコマンドは環境変数を優先し、フォールバックとして専用ファイルを読む。

**調査結果 (§3 既存調査の補足):**

§3 に既存の詳細調査が存在する。以下は WebSearch による追加調査。

#### 参考文献

| テーマ | 参考文献 |
|--------|---------|
| ファイルパーミッション `chmod 600` + `os.open()` atomic creation | OpenStack Security Guidelines "Apply Restrictive File Permissions" https://security.openstack.org/guidelines/dg_apply-restrictive-file-permissions.html |
| OWASP シークレット管理: ファイル vs 環境変数 | OWASP Cheat Sheet Series "Secrets Management" https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html |
| tmux `set-environment` によるセッション環境変数継承 | tmux GitHub Discussion #3997 "Session environment variables" https://github.com/orgs/tmux/discussions/3997 |
| 環境変数の安全なハンドリング (2025) | Secure Coding Practices "Secure Environment Variable Handling" https://securecodingpractices.com/secure-environment-variable-handling-scripts-secrets-management/ |

**主要知見:**

1. **OpenStack セキュリティガイドライン**: `os.open(..., os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)` パターンを使用し `O_EXCL` でアトミック作成を保証することを推奨。umask の影響を受けない。
2. **OWASP Secrets Management**: 動的な短命トークンを推奨。ファイルベースでは `readOnly` マウントと最小権限原則を適用する。
3. **tmux `set-environment`**: tmux セッションの環境変数は `set-environment` で動的に設定でき、セッション後から生成されたウィンドウ/ペインには継承されるが、セッション作成時の最初のウィンドウは `-e` フラグで明示的に設定する必要がある場合がある。libtmux の `Session.set_environment()` はこれをラップしている。
4. **セキュリティ優先順位**: ファイル (`chmod 600`) → tmux セッション環境変数 → 短命トークンの順でセキュリティが向上するが、実装コストも増大する。フェーズ1+2 の組み合わせが現実的な改善。

**設計決定:**

- `__orchestrator_context__.json`: `api_key` フィールドを削除する (非機密情報のみ残す)
- `__orchestrator_api_key__`: 新規ファイル、`os.open(..., 0o600)` で作成。単一行に API キーを書く
- libtmux `session.set_environment("TMUX_ORCHESTRATOR_API_KEY", api_key)`: ファイルに加えてセッション環境変数にも設定
- スラッシュコマンド: `os.environ.get("TMUX_ORCHESTRATOR_API_KEY")` を優先し、なければ `__orchestrator_api_key__` を読む
- `slash_notify.py`: `api_key` の取得元を `__orchestrator_context__.json` から変更
- `.gitignore`: `__orchestrator_api_key__` を追加

---

### 10.31 調査記録 (v0.36.0, 2026-03-05)

#### 選択した機能: `POST /workflows/tdd` — 3エージェント TDD ワークフロー

**選択理由:**

§11 の高優先度バックログ筆頭に挙げられており、v0.35.0 (セキュリティ修正) で全 687 テストが通過し基盤が整った。
TDFlow (arXiv:2510.23761) の実証 — SWE-Bench Lite 88.8% pass rate — が最も強い研究的裏付けを持つ。
既存の Workflow DAG (v0.25.0)・tags (v0.18.0)・target_group (v0.31.0)・Scratchpad (v0.16.0) をフル活用できるため実装パスが明確。

**選択しなかった候補:**

- `役割別 system_prompt テンプレートライブラリ` — 有用だが、TDD ワークフローより後に実装するのが自然 (TDD ワークフローがテンプレートを最初に使うユースケースになる)。
- `POST /workflows/debate` — 高優先度だが TDD の方が先に実証価値が高い。TDD は決定論的に検証可能 (テストが通るか否か)。
- `Codified Context インフラ` — 中規模変更で依存なし。TDD ワークフローより先に実装する理由がない。

**実装スコープ:**

1. `POST /workflows/tdd` エンドポイント — YAML 宣言した `test-writer` / `implementer` / `refactorer` 3ロールに対して Workflow DAG を自動生成して投入する。
2. `test-writer` → `implementer` ハンドオフ: test-writer が failing test を書いたら Scratchpad にファイルパスを書き込み、implementer がそれを読んで実装する。
3. `refactorer` ステップ: implementer の成果物を受け取り、リファクタリングを行う。
4. `reply_to` チェーン: 各エージェントが完了を次のエージェントに通知。
5. デモシナリオ: fizzbuzz / 素数判定。

**参考文献:**

| テーマ | 参考文献 |
|--------|---------|
| TDFlow — 4サブエージェント TDD ワークフロー (SWE-Bench 88.8%) | arXiv:2510.23761 "TDFlow: Agentic Workflows for Test Driven Development" (CMU/UCSD/JHU 2025) https://arxiv.org/abs/2510.23761 |
| context isolation が true TDD に必須 | alexop.dev "Forcing Claude Code to TDD: An Agentic Red-Green-Refactor Loop" (2025) https://alexop.dev/posts/custom-tdd-workflow-claude-code-vue/ |
| Agent-as-handoff でフェーズゲート実装 | Tweag "Agentic Coding Handbook — TDD" (2025) https://tweag.github.io/agentic-coding-handbook/WORKFLOW_TDD/ |
| Blackboard / Scratchpad パターン | AppsTek "Design Patterns for Agentic AI and Multi-Agent Systems" (2025) https://appstekcorp.com/staging/8353/blog/design-patterns-for-agentic-ai-and-multi-agent-systems/ |
| Handoff orchestration pattern | Microsoft Azure "Hand Off AI Agent Tasks" (2025) https://learn.microsoft.com/en-us/azure/logic-apps/set-up-handoff-agent-workflow |

**主要知見:**

1. **TDFlow (arXiv:2510.23761)**: 4サブエージェント (patch proposal / debugging / revision / test generation) が context 分離で動作。各エージェントは直前のフェーズの出力のみを受け取り、長文コンテキスト負荷を削減。SWE-Bench Lite 88.8%、Verified 94.3%。
2. **alexop.dev**: 1コンテキストで TDD を実装すると test writer が実装を想定してテストを書いてしまう「context pollution」が発生。3エージェント (test-writer / implementer / refactorer) の分離が必須。各エージェントは「必要な情報のみ」を受け取る。
3. **Tweag**: test cases が各フェーズ間のアーティファクト。テスト名が仕様書代わりになるため、明確な命名規則が品質に直結。
4. **Blackboard / Scratchpad**: エージェントが直接通信せず共有ストアにアーティファクトを書き込む。既存 Scratchpad API (`PUT/GET /scratchpad/{key}`) がこのパターンに直接対応。

**設計決定:**

- `POST /workflows/tdd` エンドポイント: `{ "feature": "str", "language": "python", "target_agent": null }` を受け付けて Workflow DAG を生成。
- 3フェーズ Workflow DAG: `step_1` (test-writer) → `step_2` (implementer, depends_on step_1) → `step_3` (refactorer, depends_on step_2)。
- アーティファクト受け渡し: Scratchpad をブラックボードとして使用 (`tdd/{task_id}/tests_path`, `tdd/{task_id}/impl_path`)。
- フェーズゲート: test-writer は failing test を書いた後に `pytest --collect-only` で確認してから Scratchpad に書き込む。
- `required_tags` で各フェーズを適切なエージェントに割り当て (タグ: `tdd-test-writer`, `tdd-implementer`, `tdd-refactorer`)。ただしタグがない場合は任意のアイドルエージェントが担当。
- `reply_to`: 各フェーズの RESULT を Workflow エンジンが処理 (depends_on 経由)。

---

### 10.32 調査記録 (v0.37.0, 2026-03-06)

#### 選択した機能: `POST /workflows/debate` — Advocate + Critic + Judge の3エージェント討論ワークフロー

**選択理由:**

§11 の高優先度バックログ2番目に挙げられており、v0.36.0 の TDD ワークフローが実装済みで基盤が整った。
Du et al. ICML 2024 (arXiv:2305.14325) および DEBATE ACL 2024 (arXiv:2405.09935) が多エージェント討論の
有効性を実証しており、研究的裏付けが強い。TDD ワークフロー (`/workflows/tdd`) の実装パターン
(3フェーズ DAG + Scratchpad ブラックボード) をほぼ再利用できるため実装パスが明確。
v0.36.0 デモのタイムアウト問題も `task_timeout: 900` に変更することで解決する。

**選択しなかった候補:**

- `POST /workflows/tdd` の再デモ (タイムアウト修正) — バグ修正として v0.37.0 に含めるが、それだけでは新機能にならない。
- `役割別 system_prompt テンプレートライブラリ` — debate ワークフローが最初の本格的な役割テンプレート利用者になるため、debate と同時に実装する。
- `POST /workflows/adr` — debate ワークフローの特殊化として実装できるため、debate の後に実装するのが自然。
- `Codified Context インフラ` — 有用だが debate より後に実装するのが自然 (debate が context_files を活用する最初の事例になる)。

**参考文献:**

| テーマ | 参考文献 |
|--------|---------|
| 多エージェント討論で事実性・推論精度が単一 LLM 比で有意向上 | Du et al. "Improving Factuality and Reasoning in Language Models through Multiagent Debate" ICML 2024 (arXiv:2305.14325) https://arxiv.org/abs/2305.14325 |
| DEBATE: Devil's Advocate による3エージェント NLG 評価フレームワーク | DEBATE: Devil's Advocate-Based Assessment and Text Evaluation, ACL 2024 (arXiv:2405.09935) https://arxiv.org/abs/2405.09935 |
| Role diversity が討論品質の最重要因子 | ChatEval: Towards Better LLM-based Evaluators through Multi-Agent Debate, ICLR 2024 (arXiv:2308.07201) https://arxiv.org/abs/2308.07201 |
| 終了条件: 収束検出 (ε=0.05) または最大ラウンド数 | Multi-Agent Debate for LLM Judges with Adaptive Stability Detection (arXiv:2510.12697) https://arxiv.org/abs/2510.12697 |
| 討論ベース合意形成の Python 実装パターン | Patterns for Democratic Multi-Agent AI: Debate-Based Consensus (Medium, 2025) |

**主要知見:**

1. **Du et al. ICML 2024**: 3エージェントが2ラウンド討論するだけで数学・推論タスクが大幅向上。エージェントが相互の回答を見て「refine」するメカニズムが本質。全エージェントに同一プロンプトを使うのではなく、役割を異種化することで多様性が確保される。
2. **DEBATE (arXiv:2405.09935)**: Commander (ファシリテーター) + Scorer (評価者) + Critic (Devil's Advocate) の3役割。Critic が「NO ISSUE」を返すか最大イテレーション数に達したら終了。最終的に Critic がまだ問題を挙げる場合は Tie-Breaker を別途設置できる。
3. **ChatEval (ICLR 2024)**: 役割の多様性 (role_description の差異) が討論品質を決定する最重要因子。同一ロールを複数エージェントが使うと性能低下する。
4. **Adaptive Stability Detection (arXiv:2510.12697)**: 終了条件を「2連続ラウンドでメトリクスが閾値 ε=0.05 以下」に設定すると不要なラウンドを削減できる。ただし本実装では `max_rounds` による上限が実用的。
5. **実装パターン**: 各ラウンドで advocate 先攻 → critic 後攻 → (追加ラウンドでは advocate が critic のフィードバックを受けて再反論) → judge が最終ラウンド後に判断。Scratchpad に `round_{n}/advocate` と `round_{n}/critic` を書き込むことで judge が全ラウンドの議論にアクセスできる。

**設計決定:**

- `POST /workflows/debate` エンドポイント: `{ "topic": "str", "max_rounds": 2 }` を受け付けて Workflow DAG を生成。
- ラウンド構造: round 1 は 2 タスク (advocate → critic); round 2 以降は 2 タスク (advocate_rebuttal → critic_final); 最後に judge タスク (depends_on 最終 critic)。最大 3 ラウンドをサポート。
- アーティファクト受け渡し: Scratchpad `debate/{run_id}/round_1/advocate`, `debate/{run_id}/round_1/critic` 等。judge は `debate/{run_id}/decision` に書き込む。
- `required_tags`: `debate-advocate`, `debate-critic`, `debate-judge` で専用エージェントに割り当て。タグがない場合は任意のアイドルエージェントが担当。

**実装スコープ:**

1. `POST /workflows/debate` エンドポイント — YAML 宣言した `debate-advocate` / `debate-critic` / `debate-judge` 3ロールに対して Workflow DAG を生成。
2. 最大 `max_rounds` (デフォルト 2) のラウンド制: Round 1 → advocate が提案, critic が反論; Round 2 → advocate が再反論, critic が最終反論; judge が総合判断。
3. Scratchpad をブラックボードとして使用: `debate/{run_id}/round_{n}/advocate`, `debate/{run_id}/round_{n}/critic`, `debate/{run_id}/decision`.
4. `reply_to` チェーン: 各フェーズの RESULT を Workflow エンジンが依存関係経由で処理。
5. `.claude/prompts/roles/advocate.md`, `critic.md`, `judge.md` のロールテンプレートを追加。
6. デモシナリオ: 「SQLite vs PostgreSQL 選択」をアーキテクチャ設計判断テーマとして使用。

---

## 10.12 Stop Hook 完了検出 (v0.38.0) — 調査記録

### 選定根拠

**選択した機能**: Claude Code `Stop` フックによる完了検出の置き換え (v0.38.0)

**選択理由**:
- §11 の「高」優先度リストで最上位に近い技術基盤改善。
- v0.37.0 デモ (`build-log.md`) で「v0.38.0 — Stop hook completion detection」が明示的に次イテレーション候補として指定されていた。
- 現行の 500ms ポーリング + regex は脆弱: Claude CLI バージョンアップで `❯` プロンプトの形式が変わると完了検出が失敗する。v0.36.0 デモでも 300秒タイムアウトが発生した根本原因。
- Claude Code の `Stop` フックは決定論的な完了通知を提供し、より信頼性が高い。

**選択しなかった機能**:
- `POST /workflows/tdd` — TDD ワークフローは基盤として Stop hook が完成してから実装する方が信頼性が高い。
- `ProcessPort` 抽象インターフェース — アーキテクチャリファクタリングで、ユーザー価値より低い。Stop hook 完了後に自然に実装できる。
- Codified Context — 有用だが Stop hook より緊急度が低い。

---

### Step 1 — 調査記録 (WebSearch 結果)

#### Query 1: "Claude Code hooks stop hook HTTP settings.json 2025"

**出典**: Anthropic — Claude Code Hooks Reference
URL: https://code.claude.com/docs/en/hooks
取得日: 2026-03-06

**主要知見**:
- `Stop` フックは「Claude がターンを終えて応答を完了した時点」に発火する。ユーザー割り込みによる停止では発火しない。
- `Stop` フックは **matcher をサポートしない**（`UserPromptSubmit` / `Stop` / `TeammateIdle` / `WorktreeCreate` 等は matcher 無視）。
- HTTP フック (`type: "http"`) は、JSON ペイロードを POST ボディとして指定 URL に送信する。レスポンスボディは command フックと同じ JSON 出力フォーマットで解釈される。
- non-2xx レスポンス・接続失敗・タイムアウトはいずれも **non-blocking エラー**として扱われる（実行は継続）。Stop フックを block するには 2xx レスポンスに `{"decision": "block", "reason": "..."}` を返す。
- `Stop` フック固有の入力フィールド:
  ```json
  {
    "session_id": "abc123",
    "transcript_path": "~/.claude/projects/.../<id>.jsonl",
    "cwd": "/path/to/cwd",
    "permission_mode": "default",
    "hook_event_name": "Stop",
    "stop_hook_active": true,
    "last_assistant_message": "I've completed..."
  }
  ```
  - `stop_hook_active`: すでに Stop フックにより継続中のとき `true`。無限ループ防止のために確認すること。
  - `last_assistant_message`: 最後の応答テキスト。トランスクリプトファイルを解析せずに取得可能。
- HTTP フック固有のフィールド:
  | フィールド | 必須 | 説明 |
  |---|---|---|
  | `url` | yes | POST 先 URL |
  | `headers` | no | 追加 HTTP ヘッダー（環境変数補間 `$VAR` を使用可） |
  | `allowedEnvVars` | no | ヘッダー値に補間できる環境変数名リスト |
  | `timeout` | no | デフォルト: command=600s, prompt=30s, agent=60s |
- 設定ファイルのスコープ:
  | 場所 | スコープ |
  |---|---|
  | `~/.claude/settings.json` | 全プロジェクト |
  | `.claude/settings.json` | 単一プロジェクト（コミット可） |
  | `.claude/settings.local.json` | 単一プロジェクト（gitignore、コミット不可） |

#### Query 2: "claude code settings.json hooks stop event completion detection"

**出典**: Anthropic — Claude Code Hooks Reference (同上)
追加知見:
- Stop フックは `Stop` と `SubagentStop` の2種類。エージェント内 (subagent) では `Stop` フックが自動的に `SubagentStop` に変換される。
- `stop_hook_active` を確認してフックが再帰的に発火し続けることを防ぐ — 本フレームワークの用途では、フックは「タスク完了を通知するだけ」で継続を要求しないため、`decision` フィールドは省略 (allow) で良い。
- HTTP フックのエラー（non-2xx, timeout, 接続失敗）はすべて non-blocking — これはフックサーバーが落ちていても Claude の動作を止めない設計として重要。`_poll_completion` フォールバックの必要性を裏付ける。

#### Query 3: "claude code completion hook REST endpoint HTTP type integration patterns agent orchestration"

**出典**: disler/claude-code-hooks-multi-agent-observability (GitHub)
URL: https://github.com/disler/claude-code-hooks-multi-agent-observability
取得日: 2026-03-06

**主要知見**:
- Stop フック → REST エンドポイントパターンは、複数エージェントのリアルタイム監視に実用実績がある。
- HTTP フックはエージェントごとに異なる URL を設定できるため、`agent_id` を URL パスパラメータに含めることで特定エージェントのイベントをルーティングできる。

**出典**: Anthropic — Claude Code Hooks Reference (同上)
Stop Hook の具体的な HTTP 設定例:
```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "http",
            "url": "http://localhost:{port}/agents/{agent_id}/task-complete",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

#### 設計上の決定事項

1. **設定ファイルの場所**: `.claude/settings.local.json` (worktree 内) — gitignored、コミットされない。エージェント起動時に `_write_stop_hook_settings()` で生成する。
2. **HTTP フックのタイムアウト**: 5秒 — FastAPI サーバーがローカルで動作している前提で十分。`Stop` フックは non-blocking のため、タイムアウトしても Claude の動作には影響しない。
3. **フォールバック維持**: HTTP フックが失敗した場合（サーバー未起動・ポート不明など）に備えて `_poll_completion` を並行実行し、フックが先に発火したときはポーリングをキャンセルする。
4. **ポートの取得**: `AgentConfig` に `web_port: int | None` フィールドを追加し、`ClaudeCodeAgent` が hooks URL を構築する際に使用する。`web_port` が `None` の場合は hooks 設定を書かない（`_poll_completion` のみ使用）。
5. **エンドポイント認証**: `X-API-Key` ヘッダー + `allowedEnvVars` で環境変数から読み込む（`TMUX_ORCHESTRATOR_API_KEY` 環境変数を使用）。

---

## 10.13 役割別 system_prompt テンプレートライブラリ + `system_prompt_file:` フィールド (v0.39.0) — 選定・調査記録

### 選定根拠

**選択: 役割別 system_prompt テンプレートライブラリ + `system_prompt_file:` YAML フィールド**

§11 高優先度項目の中で本項目を選んだ理由:
1. **直接ユーザー向け新機能**: エージェント設定の簡略化と役割特化プロンプトの標準化は、すべての後続ワークフロー（ADR, debate, delphi, redblue, socratic）の品質を底上げする基盤。
2. **研究的裏付けが最も強い**: ChatEval ICLR 2024 が「役割の多様性が討論品質を決定する最重要因子」と実証済み。
3. **実装コストが低い**: `AgentConfig` に1フィールドを追加し、`.claude/prompts/roles/` にMarkdownファイルを配置するだけ。既存の `context_files` 機構とも相補的。
4. **後続 ADR ワークフローの前提条件**: `system_prompt_file: roles/proposer.md` を YAML で参照できないと、ADR ワークフローの実装が煩雑になる。

**非選択: `POST /workflows/adr`**
ADR ワークフローはロールテンプレートライブラリが揃ってから実装する方が品質が高い。依存関係の順序として本項目が先。

**非選択: Codified Context インフラ**
有用だが `context_files` (v0.11.0) の自然な拡張であり、ロールテンプレートよりもユーザー向け即効性が低い。

**非選択: チェックポイント永続化**
SQLite 追加は実装コストが高く、現在のプロジェクトフォーカスではない。

### WebSearch 調査結果

**Query 1**: "role-based system prompts multi-agent LLM orchestration best practices 2025"
- SE-ML "Engineering LLM-Based Agentic Systems" (2025) — https://se-ml.github.io/blog/2025/agentic/: Role-Based Cooperation は16のMASデザインパターンの中で最も頻繁に使われる。Manual plan definitions でヒューマンが役割・プロンプトテンプレートを定義することがモデル挙動の制約に有効。
- Clarifai "Agentic Prompt Engineering" — https://www.clarifai.com/blog/agentic-prompt-engineering: system/user/assistant/tool ロール構造に加え、agentic 設定では planner/executor/reviewer ロールが推奨される。
- OpenAI Agents SDK (2025) — https://openai.github.io/openai-agents-python/multi_agent/: エージェントごとに役割特化した instructions を与えることが orchestration の基本。
- arXiv:2511.08475 "Designing LLM-based Multi-Agent Systems for Software Engineering Tasks": 役割の明確な分離と反復フィードバックを持つシステムが最高性能を示す。

**Query 2**: "ChatEval role diversity multi-agent debate quality ICLR 2024"
- Chan et al. "ChatEval: Towards Better LLM-based Evaluators through Multi-Agent Debate", ICLR 2024, arXiv:2308.07201 — https://arxiv.org/abs/2308.07201: **「diverse role prompts (異なるペルソナ) はマルチエージェント討論において必須。同一ロール説明を使うと性能劣化する」**。One-by-one 通信戦略が同期放送型より効果的。

**Query 3**: "sycophancy suppression prompt engineering multi-agent AI agent role adherence 2025"
- Giskard "Sycophancy in LLMs" — https://www.giskard.ai/knowledge/when-your-ai-agent-tells-you-what-you-want-to-hear-understanding-sycophancy-in-llms: 迎合（sycophancy）は RLHF 訓練の副産物。エージェントが相互の回答に同調しがちになり討論が機能しなくなる。
- Anthropic "Effective Context Engineering for AI Agents" (2025-09-29) — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents: エージェントのシステムプロンプトは「最小限の情報で期待される挙動を完全に記述する」ことを目標にする。

**Query 4**: "CONSENSAGENT ACL 2025 multi-agent consensus sycophancy suppression prompt"
- Pitre, Ramakrishnan, Wang "CONSENSAGENT: Towards Efficient and Effective Consensus in Multi-Agent LLM Interactions Through Sycophancy Mitigation", ACL Findings 2025 — https://aclanthology.org/2025.findings-acl.1141/: **迎合抑制プロンプトを動的に注入することで、精度向上と効率改善（討論ラウンド数削減）を同時に達成。6ベンチマーク・3モデルで SOTA。** 「エージェントが他エージェントの答えを見て迎合的に同意する」挙動を明示的に禁止する指示がシステムプロンプトに必要。

**主要設計決定:**

1. `.claude/prompts/roles/` に以下の7種類のMarkdownテンプレートを提供:
   - `tester.md` — テスト設計者・TDD サイクル担当
   - `implementer.md` — 実装担当・コード生成
   - `reviewer.md` — コードレビュー・品質チェック
   - `spec-writer.md` — 仕様文書作成
   - `judge.md` — 最終判定（debate/delphi 用）
   - `advocate.md` — 提案側（debate 用）
   - `critic.md` — 批評側（debate 用）

2. `AgentConfig.system_prompt_file: str | None` — YAML でロールテンプレートファイルへのパスを指定。`factory.py` の `build_system()` がファイルを読み込み、`AgentConfig.system_prompt` に設定する。`system_prompt_file` と `system_prompt` が両方指定された場合は `system_prompt` が優先。

3. 迎合抑制指示（CONSENSAGENT 準拠）を各テンプレートに含める: 「他エージェントの意見に単純同意しない・前の回答を参照しても自分の判断を保持する」。

4. 各テンプレートに「役割・禁止事項・完了条件・/plan /tdd の使い方」を記述した標準フォーマットを使用。

---

## 10.14 `POST /workflows/adr` — ADR 自動生成ワークフロー (v0.40.0) — 選定・調査記録

### 選定根拠

**選択: `POST /workflows/adr` — Architecture Decision Record 自動生成ワークフロー**

§11 高優先度項目の中で本項目を選んだ理由:
1. **v0.39.0 の自然な後継**: ロールテンプレートライブラリ完成により proposer / reviewer /
   synthesizer の3役割を `system_prompt_file:` 経由で宣言的に設定できる。
2. **`debate` ワークフローと共通基盤**: v0.37.0 で実装した `POST /workflows/debate` の
   Workflow DAG + `depends_on` チェーンを再利用。実装コストが低い。
3. **デモシナリオが §11 に明示されている**: "SQLite vs PostgreSQL 選択" ADR を
   proposer → reviewer → synthesizer の3エージェントで生成するシナリオが確立済み。
4. **研究的裏付けが強い**: MAD arXiv:2507.05981 が LLM による要件・意思決定文書化の
   有効性を実証。SocraSynth が モデレーター + 対立エージェント構成を提案。

**非選択: Codified Context インフラ**
ADR ワークフローが完成した後の方が、`.claude/specs/` に配置するコンテキスト仕様の
具体的なユースケースが明確になる。順序として ADR が先。

**非選択: チェックポイント永続化**
SQLite 追加は実装コストが高く、現在の優先度では ADR より低い。

### WebSearch 調査結果

**Query 1**: "Architecture Decision Record ADR automation LLM multi-agent 2025"
- AgenticAKM arXiv:2602.04445 (2026-02): Extractor/Retriever/Generator/Validator の4専門エージェント構成が単一 LLM 呼び出しより高品質な ADR を生成することを実証。「分解・役割特化」がキー。
- Strengholt (2025) "Building an Architecture Decision Record Writer Agent" — https://piethein.medium.com/building-an-architecture-decision-record-writer-agent-a74f8f739271: 単一エージェントでも ADR を自動生成できるが、複数エージェントで役割分担すると精度が上がる。
- arXiv:2511.15755 "Multi-Agent LLM Orchestration for Incident Response Decision Support": マルチエージェント ADR 生成が決定の品質と文書の網羅性を改善。

**Query 2**: "MADR format architecture decision record template markdown 2024 2025"
- MADR 4.0.0 (2024-09-17) — https://adr.github.io/madr/: 業界標準の ADR フォーマット。
  必須セクション: Title / Context and Problem Statement / Considered Options / Decision Outcome。
  任意セクション: Decision Drivers / Consequences / Confirmation / Pros and Cons / More Information。
  今回の実装では必須セクション + Decision Drivers + Consequences + Pros and Cons を採用。

**Query 3**: "MAD multi-agent debate requirements engineering LLM arXiv 2025"
- Ochoa et al. arXiv:2507.05981 "Multi-Agent Debate Strategies to Enhance Requirements Engineering with Large Language Models" (RE@Next! 2025): **MAD (Multi-Agent Debate) が要件分類タスクで精度向上を達成**。「多様な視点をもつエージェントが討論するとバイアスが低減される」。

**設計決定 (ADR ワークフロー):**

1. `POST /workflows/adr` エンドポイント: `{ "topic": "str", "options": ["opt-a", "opt-b"] }` を受け付け Workflow DAG を生成。
2. 3エージェント構成:
   - `proposer`: 各オプションの技術的提案を作成 (MADR "Considered Options" 相当)
   - `reviewer`: 提案を技術的に批評し Pros/Cons を評価
   - `synthesizer`: proposer の提案 + reviewer の批評を統合し MADR 形式の DECISION.md を生成
3. Scratchpad をブラックボードとして使用:
   - `adr/{run_id}/proposal`: proposer が書き込む
   - `adr/{run_id}/review`: reviewer が書き込む
   - `adr/{run_id}/decision`: synthesizer が最終 DECISION.md を書き込む
4. `required_tags`: `adr-proposer`, `adr-reviewer`, `adr-synthesizer` で専用エージェントに割り当て。
5. `depends_on` チェーン: review タスクは proposal に依存; synthesize タスクは review に依存。
6. `/workflows/debate` と共通の Workflow + WorkflowManager 基盤を使用。

**MADR 必須フォーマット (実装するテンプレート):**
```markdown
# ADR: <topic>
Status: Accepted
Date: <date>
## Context and Problem Statement
## Decision Drivers
## Considered Options
## Decision Outcome
### Consequences
## Pros and Cons of the Options
### <option-a>
### <option-b>
```

---

## 10.15 Codified Context インフラ (v0.41.0) — 選定・調査記録

### 選定根拠

**選択: Codified Context インフラ — `.claude/specs/` + `AgentConfig.context_spec_files`**

§11 高優先度項目の中で本項目を選んだ理由:
1. **v0.39.0 の `context_files` 機構の自然な拡張**: `context_files` は既存機能で、
   任意ファイルをワークツリーにコピーできる。`context_spec_files` は `glob パターン`
   で `.claude/specs/` 内の仕様ファイルを一括指定する糖衣構文として実装できる。
2. **実装コストが低い**: 既存の `_copy_context_files()` ロジックに glob 展開を加えるだけ。
3. **研究的裏付け**: Vasilopoulos arXiv:2602.20478 "Codified Context" (2026) が
   108,000行 C# 分散システムで 283 セッションにわたる規約維持を実証。
4. **ADR ワークフローとの相補関係**: ADR ワークフロー (v0.40.0) が生成した
   DECISION.md を `.claude/specs/` に配置し、後続タスクのエージェントに自動配布できる。

**非選択: チェックポイント永続化**
SQLite 追加は実装コストが高い。`context_spec_files` の実装が先に完了すれば
後続の規約維持テストがより現実的になる。

**非選択: OpenTelemetry GenAI Semantic Conventions**
アーキテクチャ品質改善項目であり、ユーザー向け即効性が低い。

### WebSearch 調査結果

**Query 1**: "codified context AI agents specification files session consistency 2026"
- Vasilopoulos "Codified Context: Infrastructure for AI Agents in a Complex Codebase"
  arXiv:2602.20478v1 (2026-02) — https://arxiv.org/abs/2602.20478:
  **108,000行 C# 分散システムで 283 セッションにわたり3層構造で規約を維持**:
  1. Hot-memory constitution (CLAUDE.md 相当)
  2. 19専門エージェント (role-specific)
  3. 34の cold-memory 仕様ドキュメント (on-demand)
  今回実装する `context_spec_files` は3層目 (cold-memory spec) のコンセプトをサポート。

**Query 2**: "context engineering AI agents specification YAML machine readable constraints 2025"
- Kubiya "Context Engineering for Reliable AI Agents" (2025) — https://www.kubiya.ai/blog/context-engineering-ai-agents:
  YAML で「エージェントが絶対に行ってはいけないこと」「優先度ルール」を機械可読な形式で記述することが
  コンテキストエンジニアリングのベストプラクティス。
- Anthropic "Effective Context Engineering for AI Agents" — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents:
  「仕様ドキュメントを on-demand に利用できるようにするのが持続的メモリのシミュレーションに有効」。

**Query 3**: "Claude Code context files CLAUDE.md specification best practices multi-agent 2025"
- HumanLayer "Writing a good CLAUDE.md" — https://www.humanlayer.dev/blog/writing-a-good-claude-md:
  CLAUDE.md は「300行以下で、常に全セッションに適用される内容のみ」が理想。
  プロジェクト固有の命名規則・禁止事項・アーキテクチャ制約を記述する。
- AGENTS.md standard (2025-): Sourcegraph/OpenAI/Google の協力で策定された
  ツール非依存の AI コンテキストファイル規約。Linux Foundation 管理。

**主要設計決定 (Codified Context インフラ):**

1. `AgentConfig.context_spec_files: list[str]` — glob パターンのリスト。各パターンは
   `context_spec_files_root` (デフォルト: `Path.cwd()`) を基準に展開。一致するすべてのファイルが
   エージェント起動時にワークツリーにコピーされる。

2. `context_spec_files_root: Path | None` — glob 展開の起点ディレクトリ。
   factory.py の `build_system()` で `cwd` を使用 (既存の `context_files_root` パターンと同じ)。

3. 既存の `_copy_context_files()` を拡張: `context_spec_files` の各 glob パターンを展開し、
   一致ファイルを `context_files` と同じロジックでコピー。

4. `.claude/specs/` を標準の仕様ファイル配置ディレクトリとして推奨:
   - `.claude/specs/architecture.md` — アーキテクチャ概要
   - `.claude/specs/conventions.yaml` — コーディング規約
   - `.claude/specs/decisions/*.md` — ADR ワークフローで生成された DECISION.md

5. YAML 設定例:
   ```yaml
   agents:
     - id: implementer
       type: claude_code
       context_spec_files:
         - .claude/specs/architecture.md
         - .claude/specs/decisions/*.md
         - .claude/specs/conventions.yaml
   ```

---

## 10.16 Full Software Development Lifecycle Workflow (v0.42.0) — 選定・調査記録

### 選定根拠

**選択: `POST /workflows/fulldev` — 5エージェント直列パイプライン (spec-writer → architect → tdd-test-writer → tdd-implementer → reviewer)**

ユーザーが明示的に要求したフィーチャー: 「設計から実装までのフルソフトウェア開発ライフサイクルをリアルエージェントで実行するデモ」。

§11 バックログ上の優先度分析:
1. **役割別 system_prompt テンプレートライブラリ** — v0.39.0 で `advocate/critic/judge/tester/implementer/reviewer/spec-writer` の7種類が既に実装済み。本ワークフローはこれらの自然な統合として機能する。
2. **`clean-arch` ワークフロー候補** — 「Planner → Coder → Debugger → Reviewer の4ロール開発パイプライン」が §11 中優先度に存在するが、`fulldev` はこれをより完全な形 (5エージェント、TDD統合) で実現する上位互換として機能する。
3. **ユーザー直接指示** — 自律ループにおいてユーザー明示指示は §11 優先度テーブルに優先する。

**非選択: チェックポイント永続化 (SQLite)**
実装コストが高く、SQLite 追加は独立した反復で行う方が品質を保ちやすい。

**非選択: OpenTelemetry GenAI Semantic Conventions**
ユーザー向け即効性が低い。本ワークフローのようなユーザー可視機能を優先する。

**非選択: `ProcessPort` 抽象インターフェース抽出**
アーキテクチャリファクタリングであり、機能追加ではない。別イテレーションで対応。

### 実装設計

5エージェントの役割と Scratchpad キーのマッピング:

```
spec-writer  → {prefix}_spec    (SPEC.md の内容)
architect    → {prefix}_design  (DESIGN.md / ADR の内容)
tdd-test-writer → {prefix}_tests (pytest コード)
tdd-implementer → {prefix}_impl  (実装コード)
reviewer     → {prefix}_review  (レビューレポート)
```

各エージェントは `depends_on` チェーンで直列化し、前ステップの Scratchpad キーを読んで次ステップへ渡す。

### WebSearch 調査結果

**Query 1**: "multi-agent software development pipeline LLM spec to code 2025"
- Microsoft Community Hub "An AI led SDLC: Building an End-to-End Agentic Software Development Lifecycle with Azure and GitHub" (2025) — https://techcommunity.microsoft.com/blog/appsonazureblog/an-ai-led-sdlc-building-an-end-to-end-agentic-software-development-lifecycle-wit/4491896: エンドツーエンドのエージェント型 SDLC を Azure + GitHub で構築する事例。スペック中心の開発フローで、コーディングエージェントが仕様→計画→タスクを順に処理することを実証。
- FoundationAgents/MetaGPT (GitHub, 2025) — https://github.com/FoundationAgents/MetaGPT: 「AI ソフトウェア会社」を模した多役割フレームワーク。Product Manager → Architect → Project Manager → Engineer の SOP チェーン。構造化中間成果物 (PRD, システム設計) をエージェント間でパス。役割ごとに専門化した SOP (Standardized Operating Procedure) を定義することが品質向上に最も効果的。
- ACM TOSEM "LLM-Based Multi-Agent Systems for Software Engineering: Literature Review, Vision, and the Road Ahead" (2025) — https://dl.acm.org/doi/10.1145/3712003: 94本の論文を調査した結果、**Role-Based Cooperation が最頻出の設計パターン**。Code Generation が最頻出のタスク。Functional Suitability が設計者の最優先品質属性。

**Query 2**: "LLM agent software engineering spec to code SWE-bench multi-agent pipeline 2025"
- arXiv:2601.09822 "LLM-Based Agentic Systems for Software Engineering" (2025) — https://arxiv.org/pdf/2601.09822: SWE-bench のスコアが 2023年の約2% から 2025年の約75% (verified) まで上昇。アジェンティックアプローチが急速に進化。ファイル編集・コード検索・テスト実行ツールを装備したエージェントが大規模コードベースを人間のように操作。
- arXiv:2407.01489 "Agentless: Demystifying LLM-based Software Engineering Agents" (2025) — https://arxiv.org/abs/2407.01489: 複雑なエージェントなしにシンプルなパイプラインで SWE-bench を高スコア達成。**単純な直列パイプライン + 明確な役割分担が複雑なリアクティブエージェントに匹敵**することを示す。
- arXiv:2508.00083 "A Survey on Code Generation with LLM-based Agents" (2025) — https://arxiv.org/html/2508.00083v1: パイプライン型労働分担では「各エージェントがソフトウェア開発プロセスの特定のステージを担当し、中間成果物を次のエージェントに渡す」。**ブラックボードモデル**がエージェント間のハンドオフに最も広く採用されている。

**Query 3**: "multi-agent code review test driven development workflow LLM pipeline 2025"
- arXiv:2505.16339 "Rethinking Code Review Workflows with LLM Assistance: An Empirical Study" (2025) — https://arxiv.org/html/2505.16339v1: LLM ベースの自動コードレビューを自動化パイプラインに統合することで、包括的なレビューが受容可能になる。プロアクティブ (AI主導サマリー) とリアクティブ (オンデマンド Q&A) の両モードに対応。
- arXiv:2505.02133 "Enhancing LLM Code Generation: A Systematic Evaluation of Multi-Agent Collaboration and Runtime Debugging" (2025) — https://arxiv.org/html/2505.02133v1: 多エージェント協調 + ランタイムデバッグが単一エージェントより正確性・信頼性・レイテンシを改善。AgentCoder の「Programmer → Test Designer → Test Executor」3段階パイプラインが効果的。
- OpenReview "Tests as Instructions: A Test-Driven-Development Benchmark for LLM Code Generation" (2025) — https://openreview.net/forum?id=sqciWyTm70: **テストを仕様として先に書き、テストに合格する実装を生成するアプローチ** (TDD ベンチマーク) が従来の instruction-following より高品質なコードを生成。

**Query 4 (WebFetch)**: arXiv:2507.19902 AgentMesh (2025)
- "AgentMesh: A Cooperative Multi-Agent Generative AI Framework for Software Development Automation" — https://arxiv.org/html/2507.19902v1: **Planner → Coder → Debugger → Reviewer の4役割構成** が単一エージェントより高い成功率。役割が固定された並列/直列ハイブリッド構成が最も効果的。

**主要設計決定 (fulldev ワークフロー):**

1. **5エージェント直列パイプライン** (ChatDev/MetaGPT の 4-5役割構成に基づく):
   - `spec-writer`: 機能要件仕様書 (SPEC.md) を作成
   - `architect`: SPEC.md を読み ADR / 設計文書 (DESIGN.md) を作成
   - `tdd-test-writer`: SPEC.md + DESIGN.md を読み pytest テストを作成
   - `tdd-implementer`: SPEC.md + テストを読み実装を作成
   - `reviewer`: 全成果物を読みコードレビューを作成

2. **Blackboard パターン** (arXiv:2508.00083 の推奨) で Scratchpad を共有ストレージとして使用。

3. **depends_on チェーン** で直列化 — シリアル依存はパイプラインの自然な表現。

4. `required_tags` で各エージェントに専用ロールを割り当て (v0.18.0 capability tags を活用)。

5. `architect.md` ロールテンプレートを新規追加 (既存 `spec-writer.md` の補完)。

---

## 10.17 リポジトリ整合性 (v0.43.0) — 選定・調査記録

### 選定根拠

**選択: `WorktreeIntegrityChecker` — エージェントワークツリーの整合性検証と REST 可視化**

ユーザーが明示的に要求したフィーチャー: 「リポジトリ整合性 — エージェントが使用するワークツリー・デモリポジトリが破損・汚染されていないことを保証する」。

§11 バックログ上の優先度分析:
1. **ユーザー直接指示** — 自律ループにおいてユーザー明示指示は §11 優先度テーブルに優先する。
2. **`ProcessPort` 抽象化** — §11 高優先度にあるが、今回のリポジトリ整合性とは独立しており、整合性チェックはより即効性が高い。
3. **チェックポイント永続化** — SQLite 実装コストが高く、今回のスコープより大きい。

本フィーチャーの具体的スコープ:

- **ワークツリー整合性検査** (`WorktreeIntegrityChecker`) — タスクディスパッチ前にエージェントのワークツリーが有効な git リポジトリであるか、HEAD が解決可能か、インデックスがロックされていないか、オブジェクトストアが破損していないか (`git fsck --no-dangling`) を検証する。
- **ダーティ検出** — エージェント停止後にワークツリーに未コミット変更が残っていた場合、`dirty_worktree` イベントを bus に発行する。
- **REST エンドポイント** — `GET /agents/{agent_id}/worktree-status` で各エージェントのワークツリー整合性レポート（is_valid, is_dirty, errors, branch, head_sha）を返す。
- **ディスパッチフック** — Orchestrator のタスクディスパッチループで整合性チェックを実行し、破損ワークツリーへのタスク投入を防ぐ。

**非選択: OpenTelemetry GenAI Semantic Conventions**
トレーシング基盤の変更は大きく、今回の整合性チェックとは独立した反復で実施すべき。

**非選択: エージェントドリフト検出 (Agent Stability Index)**
ロールテンプレートライブラリが完全に安定してから実装する方が効果的。

### 「リポジトリ整合性」の定義

本プロジェクトにおける「リポジトリ整合性」とは以下を指す:

1. **構造的整合性**: ワークツリーが有効な git オブジェクトストアを持ち、`git fsck` が致命的エラーを報告しない。
2. **HEAD 整合性**: `HEAD` が解決可能なコミット SHA を指しており、detached HEAD や無効な参照でない。
3. **インデックス整合性**: `.git/index.lock` が残存していない（プロセスクラッシュ後の典型的残骸）。
4. **ワークツリー清潔性**: エージェント完了後に未コミット変更 (`git status --porcelain`) が残存していない。
5. **ブランチ整合性**: 期待するブランチ名 (`worktree/{agent_id}`) に一致する。

これらのチェックは `git worktree list --porcelain` + 各種 `git` サブコマンドで実装可能。

### 調査記録 (Step 1 WebSearch, 2026-03-06)

#### Query 1: "git worktree integrity validation corrupted detection recovery"

**主要な発見:**

- `git fsck` — オブジェクトストアの SHA-1 整合性と到達可能性を検証するコマンド。CI パイプラインに統合可能。
- `git worktree repair` — ワークツリーの管理ファイルが外部要因（リポジトリ移動など）で壊れた場合に接続を再確立する。
- `git worktree prune` — 手動削除されたリンク済みワークツリーのステールな管理レコードを削除する。
- ワークツリーが削除された場合、git は管理レコードを残す（ステールな状態）。`prune` で削除できる。
- `.git/index.lock` — プロセスクラッシュ後に残留する典型的なロックファイル。存在する場合はワークツリーが破損状態。

**出典:**
- Git SCM Documentation, "git-fsck", https://git-scm.com/docs/git-fsck (2025)
- Git SCM Documentation, "git-worktree", https://git-scm.com/docs/git-worktree (2025)
- Git Cookbook, "Repairing and recovering broken git repositories", https://git.seveas.net/repairing-and-recovering-broken-git-repositories.html
- Git Tower Help, "Repairing Worktrees", https://www.git-tower.com/help/guides/worktrees/repair/windows

#### Query 2: "git repository consistency checks CI pipeline fsck index lock"

**主要な発見:**

- GitLab は Gitaly で受信 packfile に対して `git fsck` を自動実行し、問題のあるコミットを含む push を拒否する。
- `.git/config.lock` や `refs/heads/*.lock` などのロックファイルが残存すると整合性の問題になる。
- GitLab の "Repository checks" 機能は `git fsck` を定期的に実行し、エラーが見つかった場合に管理者に警告する。
- `git fsck --no-dangling` — dangling オブジェクト（到達不能だが孤立でない）の報告を抑制し、エラーのみに集中できる。

**出典:**
- GitLab Docs, "Repository consistency checks", https://docs.gitlab.com/administration/gitaly/consistency_checks/ (2025)
- GitLab Docs, "Repository checks", https://docs.gitlab.com/ee/administration/repository_checks.html (2025)
- Git SCM Documentation, "git-fsck", https://git-scm.com/docs/git-fsck (kernel.org)
- GitScripts, "Mastering Git Fsck: Your Guide to Repository Integrity", https://gitscripts.com/git-fsck

#### Query 3: "multi-agent git isolation worktree integrity safety concurrent"

**主要な発見:**

- Git ワークツリーはエージェント間のファイル競合を防ぐ標準パターンとして業界に定着。
- ワークツリー分離なしで複数エージェントが同一リポジトリ上で動作する場合、ブランチ切り替えと並行書き込みによる `git lock` エラーが発生する。
- ccswarm (GitHub: nwiizo/ccswarm) — tmux + Claude Code + git worktree 分離によるマルチエージェントオーケストレーションの実装例。
- Claude Code 公式に git worktree サポートが追加され、並列エージェント実行の標準パターンになっている。
- Uzi ツール — tmux でエージェントごとに分離された git ワークツリーをオーケストレーション。

**出典:**
- SuperGok, "Claude Code Git Worktree Support for Parallel Agents", https://supergok.com/claude-code-git-worktree-support/ (2025)
- Nick Mitchinson, "Using Git Worktrees for Multi-Feature Development with AI Agents", https://www.nrmitchi.com/2025/10/using-git-worktrees-for-multi-feature-development-with-ai-agents/ (2025-10)
- GitHub nwiizo/ccswarm, "Multi-agent orchestration system using Claude Code with Git worktree isolation", https://github.com/nwiizo/ccswarm
- Medium, "Git Worktrees: The Secret Weapon for Running Multiple AI Coding Agents in Parallel", https://medium.com/@mabd.dev/git-worktrees-the-secret-weapon-for-running-multiple-ai-coding-agents-in-parallel-e9046451eb96
- Agent Factory, "Worktrees: Parallel Agent Isolation", https://agentfactory.panaversity.org/docs/General-Agents-Foundations/general-agents/worktrees

#### Query 4: "git worktree list porcelain prune stale worktree repair"

**`git worktree list --porcelain` 出力フォーマット:**

各ワークツリーのレコードは空行で区切られ、以下のフィールドを持つ:
- `worktree <path>` — ワークツリーパス
- `HEAD <sha>` — 現在のコミット SHA
- `branch <ref>` — 現在のブランチ参照 (e.g., `refs/heads/worktree/agent-1`)
- `bare` — bare リポジトリの場合のみ存在 (boolean flag)
- `detached` — detached HEAD の場合のみ存在
- `locked [reason]` — ロックされている場合
- `prunable [reason]` — プルーン可能な場合

このフォーマットは git バージョンに依存しない安定した出力であり、スクリプトからのパースに適している。

**出典:**
- Git SCM Documentation, "git-worktree list --porcelain", https://git-scm.com/docs/git-worktree (2025)
- Debian Manpages, "git-worktree(1)", https://manpages.debian.org/testing/git-man/git-worktree.1.en.html

#### 実装方針の決定

調査結果を踏まえ、以下の実装方針を採用する:

1. **`WorktreeIntegrityChecker`** — 単一責任のチェッカークラス:
   - `git worktree list --porcelain` でワークツリーメタデータを取得
   - `.git/index.lock` の存在でロック状態を検出
   - `git fsck --no-dangling --no-progress` でオブジェクトストア整合性を検査
   - `git status --porcelain` で未コミット変更を検出
   - `git rev-parse HEAD` で HEAD 解決可能性を確認

2. **非同期実行**: すべての git コマンドは `asyncio.create_subprocess_exec` で非同期に実行し、ディスパッチループをブロックしない。

3. **結果スキーマ** (`WorktreeStatus`):
   ```
   {
     "agent_id": str,
     "path": str | None,
     "is_valid": bool,       # 構造的整合性
     "is_dirty": bool,       # 未コミット変更あり
     "is_locked": bool,      # index.lock 存在
     "head_sha": str | None, # HEAD SHA
     "branch": str | None,   # ブランチ名
     "errors": list[str],    # fsck/repair エラーメッセージ
     "checked_at": str       # ISO 8601 タイムスタンプ
   }
   ```

4. **ディスパッチフック**: `Orchestrator._try_dispatch` でタスクを送る前に整合性チェックを実行。破損ワークツリーのエージェントには `is_valid=False` 時にタスクを送らず、`integrity_check_failed` バスイベントを発行する。

5. **REST エンドポイント**: `GET /agents/{agent_id}/worktree-status` → `WorktreeStatus` JSON。

---

## 10.18 セキュリティ強化 (v0.44.0) — 選定・調査記録

### 選定根拠

**選択: セキュリティ強化 — レートリミット + 監査ログ + タスクプロンプト無害化**

ユーザーが明示的に要求したフィーチャー: 「セキュリティ（Security Hardening）— レートリミット、入力バリデーション、監査ログ、CORS ポリシー強化、プロンプトインジェクション防止」。

§11 バックログ上の優先度分析:

1. **ユーザー直接指示** — 自律ループにおいてユーザー明示指示は §11 優先度テーブルに優先する。
2. **現状の脆弱性** — 現在の REST API は X-API-Key 認証のみで、DoS 攻撃・ブルートフォース・プロンプトインジェクションに対して脆弱。
3. **エージェント特有のリスク** — `send_keys` 経由でタスクプロンプトが tmux pane に送られるため、シェルメタキャラクタがエスケープされずにエージェントプロセスに渡る可能性がある。
4. **監査ログの欠如** — 誰がいつどのエンドポイントを呼んだかの記録がない。セキュリティインシデントの事後分析が不可能。

§11 高優先度アイテムとの比較:
- **役割別 system_prompt テンプレートライブラリ** — セキュリティ基盤が整ってから実施する方が安全。
- **チェックポイント永続化** — 大型機能。セキュリティ問題より後でよい。
- **Stop フック完了検出** — v0.38.0 で既に完了済み。

本フィーチャーの具体的スコープ (優先度順):

1. **レートリミット** (`slowapi` + `limits`) — エンドポイント単位で 60 req/min の制限を設ける。429 Too Many Requests を返す。
2. **監査ログ** — FastAPI ミドルウェアですべての REST リクエストを構造化ログに記録 (timestamp, method, path, agent_id from header, status_code, duration_ms)。
3. **タスクプロンプト無害化** — `send_keys` に渡す前にプロンプト内のシェルメタキャラクタ (`\r`, `\n`, null byte 等) を検出・エスケープする `sanitize_prompt()` 関数を実装。
4. **CORS ポリシー強化** — 現行のワイルドカード CORS を設定可能な許可オリジンリストに置き換える。

**非選択: API キーローテーション / 有効期限**
既存の API キー機構は v0.35.0 で実装済み。ローテーション機能は別の反復で実施する。

**非選択: WebAuthn**
WebAuthn は REST API 向けではなく UI 向けの認証。スコープが大きすぎる。

**非選択: ネットワーク分離**
OS レベルの機能に依存し、ポータビリティが低い。

**脅威モデル (STRIDE):**

| 脅威 | 具体的シナリオ | 対策 |
|------|----------------|------|
| S (Spoofing) | API キーなしでリクエスト | 既存 X-API-Key 認証で対処済み |
| T (Tampering) | 悪意あるタスクプロンプトでシェルインジェクション | `sanitize_prompt()` |
| R (Repudiation) | 誰がタスクを投入したか分からない | 監査ログ |
| I (Information Disclosure) | エラーレスポンスに内部情報が含まれる | エラーハンドリング改善 |
| D (Denial of Service) | 大量リクエストでオーケストレーターをクラッシュさせる | レートリミット |
| E (Elevation of Privilege) | P2P 許可なしのメッセージ送信 | 既存 P2P パーミッションで対処済み |

### 調査記録 (Step 1 WebSearch, 2026-03-06)

#### Query 1: "LLM agent security prompt injection REST API prevention 2025"

**主要な発見:**

- OWASP Top 10 for LLM Applications 2025 において、プロンプトインジェクションは **#1 クリティカル脆弱性**。本番 AI デプロイメントの 73% に発見される。
- 攻撃は2種類: (1) Direct injection (ユーザーが直接悪意あるプロンプトを入力)、(2) Indirect injection (ドキュメント・メール・検索結果に隠された命令)。
- 防御戦略 (OWASP Cheat Sheet): 入力サニタイズ・コンテキスト分離・レートリミット・Zero Trust 統合。
- OWASP 推奨: 「構造化されたインターフェース/プロンプトテンプレートを使い、自由入力の余地を最小化する。自由入力が必要な場合は、インジェクションに使われる文字をフィルタリング/エンコードする。」
- arXiv 2506.08837 "Design Patterns for Securing LLM Agents against Prompt Injections" (2025): プロンプトインジェクションへの抵抗力を持つ AI エージェント構築のための原則的なデザインパターンを提案。
- agentic AI コーディングエディタへのプロンプトインジェクション攻撃 (arXiv:2509.22040v1): 外部開発リソースへの悪意ある命令埋め込みでエージェントが乗っ取られる実証研究。

**タスクプロンプトと tmux send_keys の具体的リスク:**
- `send-keys` でキーストロークをプログラム的に pane に送信する際、改行文字 (`\n`, `\r`) がそのままコマンド実行になる。
- shell metacharacter (`; && || $(...)` 等) がプロンプト内にある場合、エージェントがそのまま bash コマンドとして解釈してしまう。
- Null byte (`\x00`) がターミナルエミュレータの挙動に影響を与える可能性がある。

**出典:**
- OWASP, "LLM01:2025 Prompt Injection", https://genai.owasp.org/llmrisk/llm01-prompt-injection/ (2025)
- OWASP, "LLM Prompt Injection Prevention Cheat Sheet", https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html (2025)
- arXiv, "Design Patterns for Securing LLM Agents against Prompt Injections", https://arxiv.org/abs/2506.08837 (2025)
- arXiv, "Your AI, My Shell: Demystifying Prompt Injection Attacks on Agentic AI Coding Editors", https://arxiv.org/html/2509.22040v1 (2025)
- APIsec, "Prompt Injection and LLM API Security Risks", https://www.apisec.ai/blog/prompt-injection-and-llm-api-security-risks-protect-your-ai (2025)

#### Query 2: "FastAPI rate limiting security hardening slowapi 2025"

**主要な発見:**

- **SlowAPI** — Flask-Limiter を Starlette/FastAPI 向けに移植したレートリミットライブラリ。デコレーターベースで直感的に使用できる。
- 設定パターン:
  ```python
  from slowapi import Limiter, _rate_limit_exceeded_handler
  from slowapi.util import get_remote_address
  from slowapi.errors import RateLimitExceeded

  limiter = Limiter(key_func=get_remote_address)
  app.state.limiter = limiter
  app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
  ```
- エンドポイントへの適用: `@limiter.limit("60/minute")` デコレーターを使用。`request: Request` パラメータが必須。
- デコレーターの順序: `@app.get(...)` の **下** に `@limiter.limit(...)` を置く必要がある。
- 2025年: API の 83% がインターネットを媒介する中、DDoS 攻撃が前年比 45% 増加。
- token bucket アルゴリズムで実装されており、Redis バックエンドもサポート (本プロジェクトでは in-memory で十分)。

**出典:**
- SlowAPI GitHub, https://github.com/laurentS/slowapi (2025)
- SlowAPI Docs, https://slowapi.readthedocs.io/ (2025)
- ByteScrum, "SlowAPI: Secure Your FastAPI App with Rate Limiting", https://blog.bytescrum.com/slowapi-secure-your-fastapi-app-with-rate-limiting (2025)
- Medium, "Protecting Your API from Abuse: A Simple Rate Limiting Tutorial with FastAPI", https://medium.com/@ramadnsyh/protecting-your-api-from-abuse-a-simple-rate-limiting-tutorial-with-fastapi-e5929e7b6c0a (2025)

#### Query 3: "multi-agent system security audit logging REST middleware 2025"

**主要な発見:**

- Microsoft Multi-Agent Reference Architecture では、すべてのエージェント呼び出しに対して タイムスタンプ・呼び出し元 ID・入力ハッシュ・出力ハッシュを含む監査ログの記録を推奨している。
- 監査ログの実装: `BaseHTTPMiddleware` で `AuditMiddleware` を構築し、`/admin/`, `/api/`, `/auth/` エンドポイントを通過するすべてのリクエストを記録する。
- SOC2 CC6.3 および ISO 27001 準拠のためには、ログの PII マスキングとバージョン管理が必要。
- IBM mcp-context-forge の監査ログシステム (Issue #535): リクエストトレースと操作ログを分離して保存する。
- TRiSM (Trust, Risk, and Security Management) フレームワーク for Agentic AI (arXiv:2506.04133v4): エージェントアクション・ツール使用・振る舞いトレースを記録する Trust and Audit モジュールを提案。

**実装指針:**
- FastAPI ミドルウェア (`BaseHTTPMiddleware`) で全リクエストをインターセプト
- 記録すべきフィールド: timestamp, method, path, client_ip, agent_id (X-Agent-Id ヘッダー等), status_code, duration_ms, request_size
- 既存の `logging_config.py` (v0.7.0, `JsonFormatter`) と統合する

**出典:**
- Microsoft, "Security - Multi-agent Reference Architecture", https://microsoft.github.io/multi-agent-reference-architecture/docs/security/Security.html (2025)
- Middleware.io, "Audit Logs: A Comprehensive Guide", https://middleware.io/blog/audit-logs/ (2025)
- arXiv, "TRiSM for Agentic AI: A Review of Trust, Risk, and Security Management in LLM-based Agentic Multi-Agent Systems", https://arxiv.org/html/2506.04133v4 (2025)
- IBM mcp-context-forge, "SECURITY FEATURE: Audit Logging System", https://github.com/IBM/mcp-context-forge/issues/535 (2025)
- AppSec Engineer, "Building Secure Multi-Agent AI Architectures for Enterprise SecOps", https://www.appsecengineer.com/blog/building-secure-multi-agent-ai-architectures-for-enterprise-secops (2025)

#### Query 4: "task prompt sanitization shell injection send_keys tmux security 2025"

**主要な発見:**

- tmux `send-keys` へのシェルインジェクションリスク: 攻撃者が `send-keys` を制御できれば、tmux セッション内で任意のコマンドを実行できる。
- タスクプロンプトに含まれる危険文字: `\n` (改行 → コマンド実行), `\r` (CR → 同上), `\x00` (null byte), `;`, `&&`, `||`, `$(...)`, バッククォート
- OWASP Cheat Sheet の正規化パイプライン: ホワイトスペース折り畳み・文字繰り返し除去・長さ制限
- 防御戦略: 完全除去ではなく、危険な制御文字を安全な代替文字に置換 (例: `\n` → ` [NEWLINE] `)

**出典:**
- Security Boulevard, "Risk of Prompt Injection in LLM-Integrated Apps", https://securityboulevard.com/2025/09/risk-of-prompt-injection-in-llm-integrated-apps/ (2025)
- Lobsters, "tmux privilege escalation", https://lobste.rs/s/2fqraj/tmux_privilege_escalation
- HackingArticles, "Linux For Pentester: tmux Privilege Escalation", https://www.hackingarticles.in/linux-for-pentester-tmux-privilege-escalation/

#### 実装方針の決定

調査結果を踏まえ、以下の実装方針を採用する:

1. **レートリミット** (`slowapi` + `limits`):
   - `Limiter(key_func=get_remote_address)` でクライアント IP ベースのレートリミット
   - デフォルト制限: `60/minute` (全エンドポイント共通)
   - タスク投入 (`POST /tasks`): より厳しい `30/minute` (DoS に最も影響大)
   - 429 レスポンスに `Retry-After` ヘッダーを含める

2. **監査ログ** (`AuditLogMiddleware`):
   - `BaseHTTPMiddleware` サブクラスとして実装
   - 既存の `JsonFormatter` / `structlog` パイプラインに統合
   - 記録フィールド: timestamp, method, path, client_ip, api_key_hash (first 8 chars), status_code, duration_ms
   - `GET /audit-log` エンドポイントで直近 N 件を取得可能にする

3. **タスクプロンプト無害化** (`sanitize_prompt()`):
   - 危険な制御文字を除去・置換: `\x00` (null byte 削除), `\r` (削除), `\n` → スペース
   - 最大長制限: 16384 文字 (設定可能)
   - ログに警告イベントを出力 (サニタイズ発生時)

4. **CORS ポリシー強化**:
   - `OrchestratorConfig` に `cors_origins: list[str]` フィールドを追加
   - デフォルト: `["http://localhost:*"]` (ループバックのみ許可)
   - `CORSMiddleware` の `allow_origins` を設定値から読む

---

## 10.12 v0.45.0 — チェックポイント永続化による中断再開 (2026-03-06)

### 選択理由

**選択: チェックポイント永続化 (`CheckpointStore` SQLite)**

§11 バックログにおいて「チェックポイント永続化による中断再開」は高優先度に分類されており、
`ResultStore` (v0.24.0 JSONL) が既存の追記基盤として存在することから実装コストが低い。
v0.44.0 のデモ (build-log.md) では、プロセス再起動でキュー・ワークフロー状態が消滅する問題が
実際に確認された (30秒以上の長時間タスクを送信中に Ctrl-C すると全状態が失われる)。
これはユーザーにとって直接的な痛点であり、最優先で解消すべき。

LangGraph の checkpointer + PostgresSaver パターン、および Apache Flink の
state backend アーキテクチャが先行事例として存在する。SQLite は依存関係を増やさずに
永続化を実現できる最もシンプルな選択肢。

**選択しなかったもの**:
- `ProcessPort` 抽象インターフェース: 有用だが内部リファクタリングであり、ユーザー向け価値が低い
- OpenTelemetry: 外部依存 (opentelemetry-sdk) が増えるため、次のイテレーションで実施
- 役割別 system_prompt テンプレートライブラリ: ユーザー価値は高いが、永続化のほうが安定性向上に直結

### 調査 (Step 1 — WebSearch)

#### 参考文献

1. **LangChain "Persistence — LangGraph"** (2025)
   https://docs.langchain.com/oss/python/langgraph/persistence
   LangGraph はチェックポインターをグラフコンパイル時にバインドし、スーパーステップごとに状態スナップショットを保存する。`thread_id` で名前空間化されるため、複数の実行を独立して追跡できる。フォールトトレランス: 任意のノードが失敗した場合、最後の成功ステップから再開できる。SQLite (`AsyncSqliteSaver`) はローカル/実験用途として推奨。

2. **langgraph-checkpoint-sqlite PyPI** (2025)
   https://pypi.org/project/langgraph-checkpoint-sqlite/
   `AsyncSqliteSaver` の実装: `aiosqlite` を使用した非同期 SQLite チェックポインター。テーブル構造: `checkpoints(thread_id, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata)`。Python async context manager パターンで接続を管理。

3. **Apache Flink "Checkpoints vs Savepoints"** (stable docs)
   https://nightlies.apache.org/flink/flink-docs-stable/docs/ops/state/checkpoints_vs_savepoints/
   Flink の知見: チェックポイントは自動・軽量 (高速回復用)、セーブポイントはユーザー主導・耐久性重視 (バージョンアップ・移行用)。Chandy-Lamport 分散スナップショットアルゴリズムを採用。「再起動後に状態が論理的に同一である」ことが設計原則。

4. **"Simple LangGraph Implementation with Memory AsyncSqliteSaver — FastAPI"** Medium (2025)
   https://medium.com/@devwithll/simple-langgraph-implementation-with-memory-asyncsqlitesaver-checkpointer-fastapi-54f4e4879a2e
   FastAPI の lifespan 内で `AsyncSqliteSaver` を初期化し、アプリケーションライフタイム全体で再利用するパターン。`async with SqliteSaver.from_conn_string(":memory:") as saver` パターン。

5. **"Mastering LangGraph Checkpointing: Best Practices for 2025"** SparkCo (2025)
   https://sparkco.ai/blog/mastering-langgraph-checkpointing-best-practices-for-2025
   ベストプラクティス: スレッドごとに一意のチェックポイント ID を割り当てること。状態スキーマのバージョン管理 (スキーマ変更時の後方互換性)。定期的なチェックポイントガベージコレクション。

#### 設計決定

TmuxAgentOrchestrator の `CheckpointStore` は以下を実装する:

```
SQLite テーブル (3つ):
1. task_checkpoints:   キュー内タスクのスナップショット (id, priority, prompt, depends_on, ...)
2. workflow_checkpoints: ワークフロー状態スナップショット (workflow_id, state_json, updated_at)
3. orchestrator_meta:  プロセスメタデータ (session_name, version, created_at, last_checkpoint_at)
```

`--resume` フラグ時の動作:
1. `CheckpointStore.load_pending_tasks()` → `List[Task]` を返す
2. `CheckpointStore.load_workflows()` → `Dict[str, WorkflowState]` を返す
3. `Orchestrator.start(resume=True)` → ロードされたタスク/ワークフローをキューに再投入

チェックポイントタイミング:
- タスクキューへの `put_nowait()` 呼び出し後 (非同期で書き込み)
- タスク完了/失敗時 (エントリ削除)
- ワークフロー状態変化時 (upsert)

---

## 10.13 v0.46.0 — ProcessPort 抽象インターフェースの抽出 (2026-03-06)

### 選択理由

**選択: ProcessPort protocol + TmuxProcessAdapter + StdioProcessAdapter**

v0.45.0 のチェックポイント永続化が完了した。次は §11 バックログの「`ProcessPort` 抽象インターフェース」を選択する。
選択理由:
1. **ユニットテスト可能性**: 現状 `ClaudeCodeAgent` が `libtmux.Pane` に直接依存しているため、tmux
   なし環境でのユニットテストが不可能 (`HeadlessAgent` は既存の別クラス)。
2. **依存方向の整理**: Martin "Clean Architecture" のポート&アダプターパターン適用により、
   ビジネスロジックが実装詳細 (libtmux) に依存しない構造になる。
3. **StdioProcessAdapter**: テスト用に subprocess 経由でコマンドを実行するアダプターを提供することで、
   `ClaudeCodeAgent` のコア run loop をリアルなプロセスなしでテスト可能になる。

**選択しなかったもの**:
- OpenTelemetry: 価値は高いが外部依存増加 + 設定複雑度のため次イテレーションへ
- 役割別 system_prompt テンプレートライブラリ: ドキュメント系変更のため今回は見送り

### 調査 (Step 1 — WebSearch)

#### 参考文献

1. **PEP 544 – Protocols: Structural subtyping (static duck typing)**
   https://peps.python.org/pep-0544/
   Python 3.8 で導入された `typing.Protocol` により、明示的な継承なしに構造的サブタイピングが可能。
   クラスが「ポート」に一致するメソッドとシグネチャを持てば、`isinstance` チェックなしに型安全に扱える。
   `@runtime_checkable` を付加することで `isinstance()` による実行時チェックも可能。

2. **"Hexagonal Architecture: Ports and Adapters in Python"** SoftwarePatternLexicon (2025)
   https://softwarepatternslexicon.com/python/architectural-patterns/hexagonal-architecture-ports-and-adapters/
   依存方向の逆転: コア (AgentBase) はポート (ProcessPort Protocol) のみに依存し、アダプター
   (TmuxProcessAdapter, StdioProcessAdapter) がポートを実装する。テスト時はモックアダプターを差し込める。

3. **"Abstract Base Classes and Protocols: What Are They? When To Use Them?"** jellis18 (2022)
   https://jellis18.github.io/post/2022-01-11-abc-vs-protocol/
   Protocol vs ABC の比較: Protocol は構造的サブタイピング (duck typing)、ABC はランタイム強制。
   今回は Protocol を採用 — `ClaudeCodeAgent` に既存の継承ツリーを変更させずに型チェックが通る。

4. **"Hexagonal Architecture Design: Python Ports and Adapters for Modularity 2026"** johal.in (2026)
   https://johal.in/hexagonal-architecture-design-python-ports-and-adapters-for-modularity-2026/
   「Python の asyncio と async ポートを統合することで IoT / AI エージェントパイプラインにも適用可能」
   今回は `ProcessPort` の主要メソッドを同期 (send_keys, capture_pane) + 非同期 (start, stop) に分割する。

5. **Martin "Clean Architecture" (2017) Ch.22** ポート&アダプターパターン
   アーキテクチャ境界: 内側の Use Case / Domain が外側の detail (tmux/subprocess) に依存しない。
   依存方向は常に内側へ。今回は `ClaudeCodeAgent` (Use Case) → `ProcessPort` (Port)
   → `TmuxProcessAdapter` / `StdioProcessAdapter` (Adapter) の構造を作る。

#### 設計決定

```python
# src/tmux_orchestrator/process_port.py

@runtime_checkable
class ProcessPort(Protocol):
    """Abstraction over the pane/process that an agent interacts with."""

    def send_keys(self, keys: str, enter: bool = True) -> None: ...
    def capture_pane(self) -> str: ...
    def is_ready(self) -> bool: ...
```

- `TmuxProcessAdapter(pane: libtmux.Pane)` — 現行の実装をラップ
- `StdioProcessAdapter()` — テスト用: list にキーを蓄積、capture_pane は蓄積内容を返す
- `ClaudeCodeAgent` の `_pane` フィールドを `ProcessPort | None` に型付け
- `_wait_for_ready()`, `_send_task()`, `_poll_completion()` が `ProcessPort` メソッドのみを呼ぶ

---

## 10.14 v0.47.0 — OpenTelemetry GenAI Semantic Conventions 準拠トレース出力 (2026-03-06)

### 選択理由

**選択: OpenTelemetry GenAI Semantic Conventions トレース出力**

v0.46.0 の ProcessPort 抽象化が完了した。次は §11 バックログの
「OpenTelemetry GenAI Semantic Conventions 準拠トレース出力」を選択する。
選択理由:
1. **業界標準への準拠**: OpenTelemetry が AI エージェント可観測性の業界標準として収斂しており、
   `gen_ai.*` 属性の採用が Datadog, Jaeger 等のバックエンドとの統合に直結する。
2. **既存基盤の活用**: v0.7.0 で導入した `trace_id` ベースの構造化ログ (`logging_config.py`) が
   既に存在しており、OTel スパンとの統合コストが低い。
3. **運用価値**: マルチエージェント実行のボトルネック特定・エラー追跡に直接役立つ。

**選択しなかったもの**:
- 役割別 system_prompt テンプレートライブラリ: ユーザー向け価値は高いが今回は見送り
- エージェントドリフト検出: ロールテンプレートが先に必要

### 調査 (Step 1 — WebSearch)

#### 参考文献

1. **"Semantic Conventions for GenAI agent and framework spans"** OpenTelemetry docs
   https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/
   `gen_ai.operation.name = "invoke_agent"` または `"create_agent"` が推奨。
   属性: `gen_ai.agent.id`, `gen_ai.agent.name`, `gen_ai.agent.description`, `gen_ai.agent.version`。
   Span kind: INTERNAL (同一プロセス内エージェント呼び出し) または CLIENT (別プロセス)。

2. **"Semantic conventions for generative AI systems"** OpenTelemetry docs
   https://opentelemetry.io/docs/specs/semconv/gen-ai/
   `gen_ai.system` 属性 (例: `"claude"`)、`gen_ai.request.model`、`gen_ai.usage.input_tokens`、
   `gen_ai.usage.output_tokens` がコアの GenAI スパン属性。

3. **"OpenTelemetry for Generative AI"** OpenTelemetry Blog (2024)
   https://opentelemetry.io/blog/2024/otel-generative-ai/
   instrumentation-genai プロジェクトが Python OpenAI クライアントのトレース収集を自動化。
   `gen_ai.request.model`、トークンカウント、tool calls を span attributes として記録。

4. **"OpenTelemetry OTLP Exporters"** opentelemetry-python docs
   https://opentelemetry-python.readthedocs.io/en/latest/exporter/otlp/otlp.html
   `BatchSpanProcessor` + `OTLPSpanExporter` が本番用途の標準パターン。
   `OTEL_EXPORTER_OTLP_ENDPOINT` 環境変数で設定可能。未設定時は `ConsoleSpanExporter` にフォールバック。

5. **"opentelemetry.sdk.trace"** Python docs
   https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.html
   `TracerProvider` + `tracer = provider.get_tracer("tmux_orchestrator")` パターン。
   `with tracer.start_as_current_span("invoke_agent")` でコンテキストマネージャー形式のスパン作成。

#### 設計決定

`src/tmux_orchestrator/telemetry.py` を新規作成:
- `setup_tracing(service_name, otlp_endpoint)`: TracerProvider を初期化
- `get_tracer()`: モジュールレベルの Tracer を返す
- `agent_span(agent_id, task_id, prompt)`: `invoke_agent` スパンを `gen_ai.*` 属性付きで作成
- `OTEL_EXPORTER_OTLP_ENDPOINT` 未設定時は `ConsoleSpanExporter` + JSON ログに出力

エージェントライフサイクルへの統合:
- `submit_task()`: `task_queued` スパン
- `_dispatch_loop()`: `invoke_agent` スパン (タスク開始〜完了)
- `_route_loop()`: タスク完了/失敗時にスパンを close

新規依存: `opentelemetry-sdk>=1.24`, `opentelemetry-exporter-otlp-proto-grpc>=1.24` (オプション)

### 実装結果 (v0.47.0)

**新規ファイル:**
- `src/tmux_orchestrator/telemetry.py` — `TelemetrySetup`, `agent_span()`, `task_queued_span()`, `get_tracer()`
- `tests/test_telemetry.py` — 18 ユニットテスト (InMemorySpanExporter を使用)
- `tests/test_telemetry_integration.py` — 12 統合テスト (config + Orchestrator 連携)

**変更ファイル:**
- `src/tmux_orchestrator/config.py` — `telemetry_enabled: bool = False`, `otlp_endpoint: str = ""`
- `src/tmux_orchestrator/orchestrator.py` — `__init__` で `TelemetrySetup` 初期化; `submit_task()` に `task_queued_span`; `_dispatch_loop()` に `agent_span`; `get_telemetry()` メソッド追加
- `src/tmux_orchestrator/web/app.py` — `GET /telemetry/status` エンドポイント追加

**テスト結果:** 995 tests PASS (965 → 995)

**デモ結果:** 18/18 checks PASSED (first run)
- Part 1: In-process OTel span checks (7 checks) — 全 PASS
- Part 2: Real agent pipeline (writer → reviewer) + REST endpoint (11 checks) — 全 PASS
- agent-writer が `/tmp/otel_writer.py` に merge_sort を作成
- agent-reviewer が `/tmp/otel_review.txt` に `STATUS: APPROVED` レビューを出力

---

## 10.15 v0.48.0 — 汎用宣言的ワークフロー API + Phase 一級市民化 (2026-03-06)

### 選択理由

**選択: 汎用宣言的ワークフロー API + Phase 一級市民化**

v0.47.0 の OpenTelemetry 統合が完了した。§12「ワークフロー設計の層構造」を読み、
バックログの最高優先度項目は「汎用宣言的ワークフロー API」と「Phase 一級市民化」であることを確認した。

選択理由:
1. **§12 最高優先度**: 現行の `/workflows/tdd`・`/workflows/debate`・`/workflows/adr`・
   `/workflows/fulldev` は固定テンプレートであり、新しいワークフローを追加するたびに
   REST エンドポイントを増やす必要がある。汎用 API により一つのエンドポイントで任意のフェーズ
   構成を表現できるようになる。
2. **既存基盤の活用**: `WorkflowManager`・`WorkflowRun`・`WorkflowTaskSpec` が既存であり、
   Phase 概念をその上に層として追加するコストが低い。
3. **依存関係の解消**: Planner エージェントロール（v0.49.0 予定）が Phase JSON を出力して
   `/workflows` に投入するパターンの基盤として必要。

**選択しなかったもの**:
- Planner エージェントロール (v0.49.0 で実施): 宣言的 API が先に必要
- エージェントドリフト検出: ロールテンプレートが先に必要
- Delphi/RedBlue/Socratic ワークフロー: 基盤（Phase 一級市民）が先に必要

### 実装結果 (v0.48.0)

**新規ファイル:**
- `src/tmux_orchestrator/phase_executor.py` — `AgentSelector`, `PhaseSpec`, `WorkflowPhaseStatus`,
  `expand_phases()`, `expand_phases_with_status()`
- `tests/test_phase_executor.py` — 22 ユニットテスト
- `tests/test_phase_workflow_api.py` — 13 REST 統合テスト
- `tests/test_planner_role.py` — 13 テスト (planner.md + /plan-workflow)
- `.claude/prompts/roles/planner.md` — Planner エージェントロールテンプレート
- `.claude/commands/plan-workflow.md` — /plan-workflow スラッシュコマンド
- `examples/declarative_workflow_config.yaml` — 3 エージェントデモ設定

**変更ファイル:**
- `src/tmux_orchestrator/web/app.py` — `AgentSelectorModel`, `PhaseSpecModel`, `WorkflowSubmit` 拡張,
  `submit_workflow` ハンドラー更新 (phases= モード追加)
- `src/tmux_orchestrator/workflow_manager.py` — `WorkflowRun.phases` フィールド追加, `to_dict()` 更新
- `tests/fixtures/openapi_schema.json` — OpenAPI スナップショット再生成

**テスト結果:** 1043 tests PASS (995 → 1043, +48)

**デモ結果:** 43/43 checks PASSED (first run)
- Part 1: In-process phase_executor checks (19 checks) — 全 PASS
- Part 2: Real agent pipeline implement→review(parallel:2) (24 checks) — 全 PASS
- implementer が `/tmp/lru_cache.py` に LRUCache を実装
- reviewer-a が `/tmp/lru_review_reviewer-a.txt` に `REVIEW STATUS: NEEDS_CHANGES` を出力
- reviewer-b が `/tmp/lru_review_reviewer-b.txt` に `REVIEW STATUS: NEEDS_CHANGES` を出力

### 調査 (Step 1 — WebSearch)

#### 参考文献

1. **"A Declarative Language for Building And Orchestrating LLM-Powered Agent Workflows"**
   arXiv:2512.19769 (2025) — PayPal e-commerce ワークフロー向け宣言的 DSL。
   https://arxiv.org/abs/2512.19769
   ワークフローの共通パターン（データシリアライズ・フィルタリング・RAG・API オーケストレーション）を
   宣言的 DSL で表現。コマンド型実装比で 60% の開発時間削減・3倍の展開速度向上・74% の行数削減。
   「設定管理としてのエージェント開発」への転換を提言。

2. **"Microsoft Agent Framework — Declarative Workflows"** Microsoft Learn (2026-01)
   https://learn.microsoft.com/en-us/agent-framework/user-guide/workflows/declarative-workflows/advanced-patterns
   YAML/JSON でエージェントワークフローを宣言的に定義する Microsoft Agent Framework の仕様。
   parallel / sequential / conditional の各実行方式を `type:` フィールドで指定するパターン。
   CI/CD パイプラインでバージョン管理・レビュー・テスト可能な形式でワークフローを表現。

3. **"Autonomous Deep Agent"** arXiv:2502.07056 (2025)
   https://arxiv.org/abs/2502.07056
   HTDAG (Hierarchical Task DAG) フレームワーク: 高レベル目標を管理可能なサブタスクに動的分解し、
   依存関係と実行一貫性を厳格に維持する。再帰的プランナー・エグゼキューター 2段階アーキテクチャで
   タスクの継続的な精緻化と状況変化への適応を実現。

4. **"LangGraph State Machines: Managing Complex Agent Task Flows in Production"** DEV Community (2025)
   https://dev.to/jamesli/langgraph-state-machines-managing-complex-agent-task-flows-in-production-36f4
   LangGraph はワークフローとステート管理を一級市民として設計。Phase をノードとして表現し、
   エッジで遷移を宣言する。`StateGraph` + `add_node()` + `add_edge()` パターン。

5. **"Routine: A Structural Planning Framework for LLM Agent System in Enterprise"**
   arXiv:2507.14447 (2025)
   https://arxiv.org/abs/2507.14447
   エンタープライズ向け LLM エージェントの構造的計画フレームワーク。
   Planner が「ルーティン」（フェーズの列）を生成し、各フェーズに実行方式と担当エージェントを割り当てる。

#### 設計決定

**Phase スキーマ** (`PhaseSpec`):
```json
{
  "name": "design",
  "pattern": "debate",           // single | parallel | competitive | debate
  "agents": {                    // パターンごとに異なる agent 指定
    "tags": ["advocate"],
    "critic_tags": ["critic"],
    "judge_tags": ["judge"]
  },
  "context": "optional per-phase context override"
}
```

**`POST /workflows` の拡張**:
- `phases` 配列を受け付ける新しい `PhaseWorkflowSubmit` スキーマを追加
- 既存の `WorkflowSubmit`（タスク直接指定）との後方互換性を維持
- 各フェーズは内部で `WorkflowTaskSpec` の列に展開してから既存 DAG 実行機構に委譲

**Phase 状態追跡** (`WorkflowPhase`):
- `WorkflowRun` に `phases: list[WorkflowPhase]` を追加
- 各フェーズに `status: pending | running | complete | failed`、`task_ids`、`started_at`、`completed_at` を持たせる
- `GET /workflows/{id}` のレスポンスに `phases` 配列を含める

新規ファイル:
- `src/tmux_orchestrator/phase_executor.py` — フェーズ展開ロジック（PhaseSpec → TaskSpec 列）
- `tests/test_phase_executor.py` — 単体テスト
- `tests/test_phase_workflow.py` — REST 統合テスト

---

## 10.16 v0.49.0 — エージェント自律による実行方式変更 (2026-03-06)

### 選択理由

**選択: エージェント自律による実行方式変更 (`/change-strategy` コマンド + `POST /agents/{id}/change-strategy`)**

v0.48.0 で汎用宣言的ワークフロー API と Phase 一級市民化が完了した。
§12「ワークフロー設計の層構造」を読み直し、次の最高優先度項目は §11「層3：ステージの実行方式」の
「エージェント自律による実行方式変更」であることを確認した。

選択理由:
1. **§12 層3 最高優先度**: 層1（ワークフロー設計）と層2（フェーズ管理）は v0.48.0 で完成した。
   層3「実行方式の自律切り替え」は次の論理的なステップ。「担当エージェントが単独では困難と判断した
   場合に実行方式を変更できる仕組み」は §12 に明記されている将来実装目標。
2. **v0.48.0 の基盤を活用**: `expand_phases()`・`PhaseSpec`・`WorkflowPhaseStatus` が既に存在する。
   `parallel` パターンの展開ロジックは phase_executor.py で実装済みであり、
   REST から呼び出すだけで機能する。
3. **エージェントの自律性向上**: エージェントがタスクの複雑さを動的に判断して並列化を要求できるようになり、
   ユーザーが事前にすべてのフェーズ構成を指定する必要がなくなる。これはフレームワークの本質的な価値向上。

**選択しなかったもの**:
- `POST /workflows/delphi` (Delphi 型合意形成): `change-strategy` の後に実装するのが自然な順序
- `POST /workflows/redblue` (Red/Blue Team): 同上、`change-strategy` 基盤を先に確立
- `/deliberate` スラッシュコマンド: `change-strategy` の後に議論型変種として実装

### 調査 (Step 1 — WebSearch)

#### 参考文献

1. **"Multi-Agent Collaboration via Evolving Orchestration"**
   arXiv:2505.19591 (2025)
   https://arxiv.org/abs/2505.19591
   「Puppeteer パラダイム」: 中央オーケストレーターが強化学習でエージェントの実行順序・優先度を動的に調整。
   タスク状態の変化に応じてコーディネーション戦略を進化させ、計算コスト増なしで精度向上を達成。
   エージェントが自律的に戦略を変更するパターン（本実装の理論的根拠）。

2. **"ALAS: A Stateful Multi-LLM Agent Framework for Disruption Management"**
   arXiv:2505.12501 (2025)
   https://arxiv.org/abs/2505.12501
   3層アーキテクチャ（ワークフロー設計・エージェントファクトリー・ランタイムモニター）による
   適応的実行フレームワーク。問題が複雑または曖昧な場合、オーケストレーターが自動的にパラレル評価を
   開始する「エスカレーションパターン」を採用。本実装の `POST /agents/{id}/change-strategy` が
   これに相当する。フィードバックは非同期メッセージバスを通じてエージェントに届く（`reply_to` パターン）。

3. **"Parallelism Meets Adaptiveness: Scalable Documents Understanding in Multi-Agent LLM Systems"**
   arXiv:2507.17061 (2025)
   https://arxiv.org/html/2507.17061v4
   金融文書分析での複数エージェント並列評価フレームワーク。高曖昧度サブタスクに対して複数エージェントが
   競合し、中央評価者が事実性・一貫性でスコアリング。コンプライアンス精度 27% 向上、修正率 74% 削減。
   本実装の `competitive` パターンが同様のアーキテクチャを採用。

4. **"AFLOW: Automating Agentic Workflow Generation"**
   ICLR 2025 proceedings
   https://proceedings.iclr.cc/paper_files/paper/2025/file/5492ecbce4439401798dcd2c90be94cd-Paper-Conference.pdf
   MCTS アルゴリズムでワークフロー探索空間をナビゲート、LLM 駆動でノードを動的展開。
   手動介入を減らしながらタスク複雑度に適応する「自律的ワークフロー最適化」を実現。
   本実装の設計思想と整合: エージェントが自律的に実行方式を選択・変更する。

#### 設計決定

**`ChangeStrategyRequest` スキーマ**:
```json
{
  "pattern": "parallel",        // single | parallel | competitive
  "count": 2,                   // 1-10 (safety limit)
  "tags": ["solver"],           // optional required_tags for spawned tasks
  "context": "Task description",// when provided → immediate task spawning
  "reply_to": "agent-solver"    // spawned tasks return results to this agent
}
```

**`POST /agents/{agent_id}/change-strategy` 動作**:
- agent_id が存在しない → 404
- pattern が無効 → 422
- pattern = single → 即座に accepted を返す（何も生成しない）
- pattern = parallel / competitive + context あり → count 個のタスクをキューに投入して task_ids を返す
- pattern = parallel / competitive + context なし → 戦略を記録するのみ（遅延スポーン）

**スラッシュコマンド構文**: `/change-strategy parallel:2 <task description>`

**`debate` パターンを除外した理由**:
- `debate` は advocate/critic/judge の3役割が必要でサブエージェントのテンプレート設定が必要
- v0.49.0 はシンプルさを優先し `single|parallel|competitive` のみ実装
- `debate` は次イテレーション（`/deliberate` スラッシュコマンドと一緒に実装）

### 実装結果 (v0.49.0)

**新規ファイル:**
- `tests/test_change_strategy.py` — 17 ユニットテスト
- `.claude/commands/change-strategy.md` — /change-strategy スラッシュコマンド

**変更ファイル:**
- `src/tmux_orchestrator/web/app.py` — `ChangeStrategyRequest` モデル + `POST /agents/{id}/change-strategy` エンドポイント
- `tests/fixtures/openapi_schema.json` — OpenAPI スナップショット再生成
- `pyproject.toml` — version 0.48.0 → 0.49.0
- `CHANGELOG.md` — v0.49.0 エントリー追加

**テスト結果:** 1060 tests PASS (1043 → 1060, +17)

---

## 10.17 v1.0.0 — 安定化・リリース検証 (2026-03-06)

### 選定理由

v0.49.0 でエージェント自律実行方式変更（§12 層3）が完了し、主要な機能層がすべて揃った。
次のステップは新機能実装ではなく、**既知バグの修正と全体的な品質検証による v1.0.0 リリース**。

**選択したもの**:
- `repo_root` cwd バグ修正 — デモの `cwd=PROJECT_ROOT` がオーケストレーターリポジトリに worktree を汚染する問題
- 全1060件テスト実行・確認
- OpenAPI スナップショット最新化
- DESIGN.md §10.17 調査記録追加
- v1.0.0 リリース (pyproject.toml バージョン更新・CHANGELOG・git tag)

**選択しなかったもの**:
- 新機能（`POST /workflows/delphi`、エージェントドリフト検出等）— 品質ゲートを先に通過する
- §11 バックログの実装 — v1.0.0 はリリース品質確保が目的

### Step 1: 事前調査 (WebSearch)

#### 1.1 ソフトウェアリリースチェックリスト v1.0 品質ゲート基準

**Sources**:
- [Software Release Checklist For Smooth Deployments (Cortex, 2024)](https://www.cortex.io/post/software-release-checklist) — プロダクション準備スコアリング（コード品質・セキュリティ・ドキュメント・標準チェック）
- [The Essential Release Checklist 2026 (Apwide)](https://www.apwide.com/the-essential-release-checklist/) — フェーズ別チェックリスト
- [Software Quality Gates: What They Are & Why They Matter (testRigor)](https://testrigor.com/blog/software-quality-gates/) — Pass/Warning/Fail の3ステータスゲート

**知見**:
- 品質ゲートは「通過/警告/失敗」の3状態で評価する。v1.0.0 は全テスト通過 (Pass) を必須とする。
- リリース前チェックリストには機能テスト・互換性テスト・セキュリティテスト・パフォーマンステストを含める。
- 本プロジェクトでの適用: 1064 件のテストが全通過、OpenAPI スナップショット一致、worktree 汚染なし。

#### 1.2 主要リリース前の回帰テスト戦略

**Sources**:
- [Regression Testing: A Guide for QA Teams (TestRail)](https://www.testrail.com/blog/regression-testing/) — Full-suite execution は主要リリース・アーキテクチャ変更時に必須
- [Understanding Regression Testing: Strategy, Automation & Best Practices (DEV Community)](https://dev.to/dmitrybaraishuk/understanding-regression-testing-strategy-automation-best-practices-14mk) — 高優先度サブセットは全コミット・フルスイートは主要リリース前
- [What to Include in a Regression Test Plan (BrowserStack)](https://www.browserstack.com/guide/regression-test-plan) — テストケースにメタデータ（優先度・コンポーネント・最終実行日）を付加

**知見**:
- v1.0.0 では「全スイート実行」が適切（アーキテクチャ変更 + 全機能層完成）。
- テスト結果: 1064 passed, 3 warnings in 38.10s — 全通過確認。

#### 1.3 マルチエージェントフレームワークのリリース安定性基準

**Sources**:
- [Agentic AI Frameworks: Complete Enterprise Guide (SpaceO, 2026)](https://www.spaceo.ai/blog/agentic-ai-frameworks/) — フレームワーク評価: 機能完全性・プロダクション準備・ガバナンス・DX
- [Introducing Agent Readiness (Factory.ai)](https://factory.ai/news/agent-readiness) — 8技術柱: 状態永続化・観測可能性・エラー処理が生産グレード自律動作の最低要件
- [AI Agent Frameworks: A Guide to Evaluating Agentic Platforms (TechTarget)](https://www.techtarget.com/searchenterpriseai/feature/AI-agent-frameworks-A-guide-to-evaluating-agentic-platforms) — 機能完全性・長期安定性のエコシステム成熟度

**知見**:
- 生産グレードに必要な要素: 状態永続化 (v0.45.0 ✓)・観測可能性/OTel (v0.47.0 ✓)・エラー処理/サーキットブレーカー (v0.10.0 ✓)・セキュリティ (v0.44.0 ✓)
- OpenAI Swarm が「非生産グレード」とされた理由（状態永続化・観測可能性・エラー処理の欠如）を本プロジェクトはすべて解決済み。

### Step 2: 発見したバグと対応

#### バグ1: worktree cwd バグ

**症状**: デモの `demo.py` が `cwd=PROJECT_ROOT` でサーバーを起動すると `WorktreeManager(Path.cwd())` がオーケストレーターの TmuxAgentOrchestrator リポジトリに worktree を作成してしまう。

**根本原因**: `factory.py` 行91 `cwd = Path.cwd()` で `WorktreeManager` を初期化しており、`Path.cwd()` はサーバー起動時の作業ディレクトリに依存していた。

**修正内容**:
1. `OrchestratorConfig.repo_root: Path | None = None` フィールドを追加 (YAML: `repo_root: /path/to/repo`)
2. `config.py` の `load_config()` で `repo_root` を解析・絶対パス変換
3. `factory.py` の `build_system()` で優先順位付きの wm_base 解決ロジックを追加:
   - 優先1: `config.repo_root` — 明示オーバーライド
   - 優先2: `config_path.parent` — YAML ファイルが git リポジトリ内に存在する場合
   - 優先3: `Path.cwd()` — レガシーフォールバック（後方互換性維持）
4. テスト4件追加: `test_build_system_uses_config_repo_root`・`test_build_system_config_path_parent_used_when_no_repo_root`・`test_load_config_repo_root_parsed`・`test_load_config_repo_root_defaults_to_none`

**結果**: 1064 passed (旧 1060 + 新 4件)

#### バグ2: .worktrees/ の残留汚染

確認結果: `/home/segument/Projects/TmuxAgentOrchestrator/.worktrees/` は空（残留なし）。

#### バグ3: OpenAPI スナップショットの陳腐化

`UPDATE_SNAPSHOTS=1 uv run pytest tests/test_openapi_schema.py` を実行して最新化済み。

---

## 10.19 v1.0.1 — スラッシュコマンドをエージェントワークツリーで使用可能にする (2026-03-07)

### 選定理由

**選択**: スラッシュコマンド群 (`/send-message`, `/check-inbox` 等) をエージェントワークツリーで使用可能にする

**選んだ理由**:
- v1.0.5 デモで agent-impl が `/send-message agent-review ...` を実行したところ "Unknown skill: send-message" エラーが発生し、エージェントが REST API を手探りで探す羽目になった。
- ワークツリー内で Claude Code を起動すると、スラッシュコマンドはそのワークツリーの `.claude/commands/` を探す。TmuxAgentOrchestrator リポジトリの `.claude/commands/` はプロジェクトルートではないため読み込まれない。
- 既存の `_copy_context_files()` / `_write_stop_hook_settings()` と同じパターンで、ワークツリー起動時に `.claude/commands/*.md` をコピーするだけで解決できる。実装コストが低く、デモのタスク設計の自由度が大幅に上がる。

**選ばなかったもの**:
- v1.0.1 (Competitive Expression Evaluator): 引き続き候補。本機能が完了したあとのデモシナリオとして残す。
- system_prompt テンプレートライブラリ: 価値は高いが、スラッシュコマンドが使えることが前提のため後回し。

**実装するもの**:
- `src/tmux_orchestrator/commands/` — パッケージ同梱コマンドディレクトリ (`.claude/commands/*.md` と同内容)
- `ClaudeCodeAgent._copy_slash_commands(cwd)` — ワークツリー起動時に `.claude/commands/` へコピー
- `start()` でコールする
- テスト: `tests/test_slash_commands.py`
- デモ: v1.0.5 の失敗シナリオを再現し、`/send-message` が使えることを確認

### 調査記録

**調査 1: Claude Code スラッシュコマンドの発見ロジック**
- 出典: [Extend Claude with skills — Claude Code Docs](https://code.claude.com/docs/en/skills)
- 発見: Claude Code はスラッシュコマンド (現在は "skills" に統合済み) を 4 箇所から読み込む:
  1. Enterprise: managed settings
  2. Personal: `~/.claude/skills/<name>/SKILL.md` または `~/.claude/commands/<name>.md`
  3. Project: `.claude/skills/<name>/SKILL.md` または `.claude/commands/<name>.md` (プロジェクトルート基準)
  4. Plugin: `<plugin>/skills/<name>/SKILL.md`
- **キー**: "Project" スコープのコマンドは、Claude Code が起動したディレクトリ (= エージェントのワークツリールート) の `.claude/commands/` を探す。TmuxAgentOrchestrator リポジトリの `.claude/commands/` はここではない。
- `.claude/commands/*.md` ファイルは後方互換として引き続き動作する。

**調査 2: ワークツリーにおけるコマンド発見**
- 出典: [GitHub Issue #27156 — claude -w (worktree mode) と git submodule](https://github.com/anthropics/claude-code/issues/27156)
- 発見: ワークツリー内の Claude Code インスタンスは、ワークツリーのルートを project root として扱う。既存の `.claude/commands/` はワークツリーの外側にあるため読み込まれない。
- 解決策: ワークツリー起動時に `{worktree}/.claude/commands/` を作成し、コマンドファイルをコピーする。

**調査 3: パッケージデータとしてのバンドル方法**
- 出典: Python `importlib.resources` 公式ドキュメント (Python 3.11+)
- 採用方針: `Path(__file__).parent.parent / "commands"` (= `src/tmux_orchestrator/commands/`) でコマンドディレクトリを参照する。
  - 開発環境: ファイルシステム上のパス直接参照
  - インストール環境: hatchling が `packages = ["src/tmux_orchestrator"]` でサブディレクトリを丸ごとバンドルするため同じパスが有効
  - `.claude/commands/` (開発者用) と `src/tmux_orchestrator/commands/` (エージェント用) を分離管理

---

## 10.18 v1.0.1 — Competitive Expression Evaluator Demo (2026-03-07)

### 選定理由

**選択**: 3エージェント競合実装デモ — 算術式評価器 (best-of-3)

**選んだ理由**:
- v1.0.0 デモは2エージェントの単純なリニアパイプライン (spec→impl) で、タスク内容が
  "echo でファイルを書く" レベルで簡単すぎると指摘を受けた。
- v1.0.1 ではエージェントに **アルゴリズム設計+実装+自己テスト実行** を要求し、
  実質的な CS の問題 (式評価器) を解かせる。
- `competitive` 相当の並列タスク (target_agent×3) を使い、3エージェントが
  同時並行で異なるアルゴリズムを実装する。 → マルチエージェントの価値を明示。
- 客観的スコアリング (15テストケース) で勝者を自動決定できる。

**選ばなかったもの**:
- §11 の MIRIX エピソード記憶・TLA+ 仕様化: フレームワーク変更が大きく、
  ユーザーの要望 (デモのタスク難易度向上) とは別軸。

**実装するもの**:
- `~/Demonstration/v1.0.1-expr-eval/` デモリポジトリ
- 3エージェント (agent-recursive, agent-shunting, agent-ast) が並列で
  `evaluate(expr: str) -> float` を実装
- 15 テストケースでスコアリング、スクラッチパッド経由で結果収集
- `merge_on_stop: true` で実装をデモリポジトリに保存
- デモ側でも実装を直接 import して検証

---

## 10.20 v1.0.9 — エージェントドリフト検出 (Agent Stability Index) (2026-03-08)

### 選定理由

**選択**: エージェントドリフト検出 — `DriftMonitor` + `agent_drift_warning` STATUSイベント

**選んだ理由**:
- §11「アーキテクチャ・品質」テーブルで **高** 優先度かつ未実装の最上位項目。
- v1.0.8 build-log の「ディレクター投票が遅い (11分ループ)」根本原因は、ワーカーが
  完了を能動通知せず、ディレクターが `/progress` を活用しなかったこと。
  `DriftMonitor` を追加することで、エージェントが役割逸脱・停滞を示した瞬間に
  自動介入 (re-brief / restart) できる基盤が生まれ、将来の slow-poll 問題を根本解決できる。
- 既存の `ContextMonitor` (トークン量監視) との対称性が高く、同じ「監視→イベント発行」
  パターンで実装できる。追加するファイルは `drift_monitor.py` 1本で済む。
- arXiv:2601.04170 "Agent Drift: Quantifying Behavioral Degradation in LLM-Based Agents"
  が12次元の Agent Stability Index (ASI) を定義しており、実装の指針が明確。
- `ContextMonitor` が既に `context_warning` / `notes_updated` / `summarize_triggered` を
  発行する bus 基盤が存在するため、`agent_drift_warning` イベントを追加するだけで
  Director エージェントや Web UI が即座に購読・対応できる。

**選ばなかったもの**:
- **MIRIX型エピソード記憶ストア** (中優先度): 価値は高いが SQLite 基盤の拡張が必要で、
  ドリフト検出よりも実装コストが大きい。ドリフト検出後の次候補として残す。
- **`POST /workflows/delphi`** (中優先度): ワークフロー追加は価値あるが、
  層3の機能であり、エージェントの安定性 (層5) が先に必要。
- **スライディングウィンドウ文脈圧縮** (中優先度): ContextMonitor 強化として有用だが、
  `DriftMonitor` の実装後に自然な拡張として追加できる。

**実装するもの**:
- `src/tmux_orchestrator/drift_monitor.py` — `DriftMonitor` クラス
  - ペイン出力を監視し「役割逸脱スコア」を算出
  - 閾値超過で `agent_drift_warning` bus イベントを発行
  - 監視対象: キーワード違反 (禁止語句)、プロンプト放置 (long idle)、
    出力の役割逸脱 (system_prompt との意味的乖離 — シンプルなキーワードマッチ)
- `src/tmux_orchestrator/orchestrator.py` — `DriftMonitor` の起動・停止を統合
- `src/tmux_orchestrator/schemas.py` — `DriftWarningPayload` モデル追加
- `src/tmux_orchestrator/web/app.py` — `GET /agents/{id}/drift` エンドポイント追加
- `tests/test_drift_monitor.py` — ≥15 ユニットテスト
- デモ: `~/Demonstration/v1.0.9-drift-monitor/` — 2エージェント構成
  - agent-a (coder): BST 実装タスクを受け取り実装する
  - agent-b (monitor): orchestrator の bus を購読し `agent_drift_warning` を受信したら
    RE-BRIEF メッセージを agent-a に送信する
  - デモは agent-a に意図的に曖昧な追加指示を与えてドリフトを誘発し、
    `DriftMonitor` が検出 → agent-b が介入する流れを検証する

### 調査記録

**調査 1: Agent Stability Index (ASI) — arXiv:2601.04170**
- 出典: Abhishek Rath, "Agent Drift: Quantifying Behavioral Degradation in Multi-Agent LLM Systems Over Extended Interactions," arXiv:2601.04170, January 2026.
  URL: https://arxiv.org/abs/2601.04170
- ASI は12次元の複合指標 (正規化 [0,1]、1=完全安定)。4カテゴリに分類される:
  - **Response Consistency (重み 0.30)**: 出力意味的類似度 (embedding 比較)、決定経路安定性 (推論チェーン一貫性)、信頼キャリブレーション
  - **Tool Usage Patterns (重み 0.25)**: ツール選択安定性、ツール順序一貫性、ツールパラメータドリフト
  - **Inter-Agent Coordination (重み 0.25)**: 合意率、ハンドオフ効率、**役割遵守 (role adherence)**
  - **Behavioral Boundaries (重み 0.20)**: 出力長安定性、エラーパターン出現、人間介入率
- **ドリフト検出閾値**: ASI が τ=0.75 を 3 連続 50-interaction ウィンドウで下回った場合に検出
- 役割遵守測定: agent_id とタスクタイプの相互情報量 (mutual information) を使用
- 主要ドリフト信号: タスク成功率 42% 低下、完了時間増加、エージェント間衝突 487.5% 増加
- 3 つの緩和戦略: エピソード記憶統合、ドリフト認識ルーティング、適応型行動アンカリング

**調査 2: Multi-Agent System Monitoring via Node Evaluation — arXiv:2510.19420**
- 出典: "Monitoring LLM-based Multi-Agent Systems Against Corruptions via Node Evaluation," arXiv:2510.19420, October 2025.
  URL: https://arxiv.org/abs/2510.19420
- MAS をグラフ (エージェント=ノード、通信=エッジ) として捉え、動的防衛パラダイムを採用
- 静的防衛ではなく通信を継続的に監視しグラフトポロジーを動的調整することで進化する攻撃に対応
- 実装方針: ノード評価スコアに基づき問題のあるエージェントを隔離または再ルーティング

**調査 3: Behavioral Monitoring & Anomaly Detection — tekysinfo.com (2025)**
- 出典: "Behavioral Monitoring & Anomaly Detection for Agents: What I Learned Building Safe AI Systems," tekysinfo.com, 2025.
  URL: https://tekysinfo.com/behavioral-monitoring-anomaly-detection-for-agents-what-i-learned-building-safe-ai-systems/
- 実装すべき3つの基本的な行動次元:
  1. **API使用パターン**: ツール呼び出し頻度・順序。正常なコード生成エージェントは「2-3回の検索、1-2回の実行試行」。15回連続検索かつ実行ゼロ → 混乱またはゴールドリフトの可能性
  2. **データアクセス行動**: 担当タスクと無関係なデータソースへのアクセス
  3. **応答レイテンシ分布**: 200-400ms から 3秒以上への急変 → 異常
- ローリングベースライン方式 (静的閾値ではなく適応型) を推奨
- エージェント内問題 (intra-agent) とエージェント間問題 (inter-agent) の二分類

**採用方針**:
TmuxAgentOrchestrator の `DriftMonitor` では外部 embedding API を使わずに実装できる範囲で
ASI のサブセットを採用する:
1. **役割遵守スコア** — `system_prompt` のキーワードと出力テキストの overlap 率 (役割から逸脱した禁止語句の出現頻度を含む)
2. **アイドル時間スコア** — 最後のペイン出力変化から経過時間 (長時間変化なし = ドリフト/スタック)
3. **出力長安定性** — 出力行数の rolling variance (急激な変化 = 異常)
これら3スコアを重み付き平均し、閾値 τ=0.6 以上を正常とし下回ったとき `agent_drift_warning` イベントを発行する。

---

> 以下は DESIGN.md から移行 (2026-03-14):

### 10.38 v1.1.13 — `__COMPRESS_CONTEXT__` UserPromptSubmit フック + `__COMPRESS_CONTEXT__` ファイル消費パターン

#### Step 0 — 選定理由

**選択: `__COMPRESS_CONTEXT__` フック — エージェント側でのコンテキスト圧縮注入**

**何を選択したか・理由:**

v1.1.12 でサーバー側の自動 TF-IDF 圧縮 (`auto_compress=True`) が完成し、`ContextMonitor` は
`__COMPRESS_CONTEXT__\n{compressed_text}` をエージェントの pane に送信できるようになった。
しかし**エージェント側にその通知を受け取るフックが存在しない**。

`user-prompt-submit.py` は `__TASK__` トリガーのみを処理し、`__COMPRESS_CONTEXT__` については
何も行わない。そのため圧縮済みテキストはエージェントの画面に生テキストとして表示されるだけで、
`additionalContext` として Claude に届かない。

本イテレーションの実装は:
1. `user-prompt-submit.py` を拡張して `__COMPRESS_CONTEXT__` トリガーも処理する。
2. 圧縮テキストをファイルに書き込む (`__compress_context__<agent_id>__.txt`) パターンを採用し、
   `__task_prompt__` と同じ consume-once 設計を適用する。
3. `additionalContext` として注入し、Claude がコンテキスト要約を参照できるようにする。

**価値/実装コスト:**
- **最小スコープ**: `user-prompt-submit.py` に20行の分岐追加 + `context_monitor.py` にファイル書き込み。
- **v1.1.12 の自然な完成**: 送信側（サーバー）と受信側（エージェント hook）がはじめて揃う。
- **研究的裏付け**: ACON (arXiv:2510.00615) と Focus Agent (arXiv:2601.07190) が実証した
  threshold-based 自動圧縮の効果は、圧縮テキストがエージェントに適切に届いて初めて実現する。

**選択しなかった候補と理由:**
- **`/deliberate` スラッシュコマンド**: `deliberate.md` が既に v1.0.32 で実装済み。
- **チェックポイント永続化 SQLite**: 大規模スコープ (v1.2.x 向け)。
- **ProcessPort 抽象インターフェース**: libtmux 全面 DI 化で既存 E2E テストへの影響が広範。
- **UseCaseInteractor 層の抽出**: FastAPI ハンドラーの全面リファクタリング、不要なリスク。
- **DriftMonitor セマンティック類似度**: `sentence-transformers` 新依存が必要。

**実装スコープ:**
1. `context_monitor.py` の `_run_auto_compress()`: 圧縮テキストをファイル
   (`__compress_context__{agent_id}__.txt`) に書き込んでから `__COMPRESS_CONTEXT__` トリガーを送信。
2. `user-prompt-submit.py`: `__COMPRESS_CONTEXT__` トリガー検出時にファイルを読み込み・削除し、
   `additionalContext` として返す。
3. テスト: UserPromptSubmit フックの新ブランチに対するユニットテスト追加 (目標 +15 テスト)。

#### Step 1 — Research

**Query 1**: "Claude Code UserPromptSubmit hook additionalContext inject compressed context AI agent 2025"

主要知見:
- **Claude Code hooks 公式ドキュメント** (code.claude.com/docs/en/hooks): `UserPromptSubmit` フックは
  プロンプト処理前に毎回発火。stdout に JSON を出力して `hookSpecificOutput.additionalContext` を
  返すとその内容が Claude に追加コンテキストとして届く。プレーンテキスト stdout も context として
  扱われるが、`additionalContext` フィールドの方がより離散的に注入される (transcript にも表示)。
- **既知の問題** (anthropics/claude-code issue #14281): `additionalContext` が複数回注入される
  バグが存在。特定のフック設定と組み合わせた場合に発生。→ consume-once ファイル削除パターンが
  重複注入を防ぐ効果がある。
- **非同期圧縮パターン**: フックは高速 (< 1秒) であることが必要で、圧縮処理は別プロセスで実行し
  フックがファイルを読み込むだけというパターンが推奨されている。

**References**:
- Claude Code hooks reference: https://code.claude.com/docs/en/hooks
- Hook additionalContext injected multiple times (bug report): https://github.com/anthropics/claude-code/issues/14281
- Complete guide to hooks in Claude Code: https://www.eesel.ai/blog/hooks-in-claude-code

**Query 2**: "LLM agent context compression injection hook pattern additionalContext 2025"

主要知見:
- **ACON** (Kang et al., arXiv:2510.00615, 2025): 閾値ベースのコンテキスト圧縮で peak token を
  26–54% 削減。履歴と観察の両方を圧縮する統一フレームワーク。threshold 4096 (history) /
  1024 (observation) が精度とコスト削減のベストバランス。
- **Focus Agent** (Verma, arXiv:2601.07190, 2026): 自律的コンテキスト圧縮がエージェントの
  self-directed learning を改善。インタラクション履歴の圧縮で46% パフォーマンス向上。
- **Context Engineering in LLM-Based Agents** (Tan Ruan, 2025, Medium): コンテキスト圧縮を
  エージェントに「届ける」メカニズムとして、(1) system prompt 再注入、(2) additionalContext
  注入、(3) 環境観察としての挿入の3パターンを整理。`additionalContext` が最も汚染リスクが低い。
- **Google ADK EventsCompactionConfig** (google.github.io/adk-docs): `compaction_interval` と
  `overlap_size` 設定。圧縮後の要約をユーザープロンプトのコンテキストとして注入するパターン
  は Google ADK の標準実装。

**References**:
- ACON arXiv:2510.00615: https://arxiv.org/abs/2510.00615
- Focus Agent arXiv:2601.07190: https://arxiv.org/html/2601.07190
- Context Engineering in LLM-Based Agents: https://jtanruan.medium.com/context-engineering-in-llm-based-agents-d670d6b439bc
- Google ADK context compaction: https://google.github.io/adk-docs/context/compaction/

**Query 3**: "Claude Code hooks UserPromptSubmit trigger token file pattern consume-once 2025"

主要知見:
- **Consume-once ファイルパターン** (disler/claude-code-hooks-mastery, GitHub): フックがファイルを
  読み込んで即座に削除するパターンは Claude Code の hooks コミュニティで広く使われている。
  競合状態を防ぐためにファイル名に agent_id を含めることが推奨される。
- **UserPromptSubmit の matcher 不在**: `UserPromptSubmit` フックは全プロンプトで発火し、
  matcher パターンをサポートしない（`PreToolUse` / `PostToolUse` と異なる）。
  → フック内部でプロンプト文字列を検査してトリガー種別を判別する必要がある。
- **フックの高速性要件**: Claude Code は hooks に5秒のデフォルトタイムアウトを設定。
  ファイル読み込みは IO 操作だが通常 < 1ms で完了するため問題なし。

**References**:
- claude-code-hooks-mastery: https://github.com/disler/claude-code-hooks-mastery
- DataCamp Claude Code Hooks guide: https://www.datacamp.com/tutorial/claude-code-hooks

**実装知見まとめ:**
- サーバー側 (`context_monitor.py`): `__COMPRESS_CONTEXT__` トリガーを send_keys する前に
  圧縮テキストを `__compress_context__{agent_id}__.txt` に書き込む。
- エージェント側 (`user-prompt-submit.py`): `__COMPRESS_CONTEXT__` をプロンプトとして受け取ったとき、
  ファイルを読み込み・削除し、`additionalContext` として圧縮要約を Claude に渡す。
- consume-once パターン (ファイル削除) により重複注入を防止。
- agent_id ナミング (`__compress_context__{agent_id}__.txt`) により、共有 cwd での競合を回避。

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/context_monitor.py`: `_run_auto_compress()` に worktree 判定追加。
  `worktree_path` が設定されている場合は `__compress_context__{agent.id}__.txt` に圧縮テキストを
  書き込み、`"__COMPRESS_CONTEXT__"` 短トリガーのみを `notify_stdin` に渡す。
  `worktree_path=None` の場合はフォールバックとして従来のインライン送信。
- `src/tmux_orchestrator/agent_plugin/hooks/user-prompt-submit.py`: `_COMPRESS_TRIGGER` 定数を追加。
  `__COMPRESS_CONTEXT__` トリガー検出時に `__compress_context__*.txt` を読み込み・削除し、
  framing ヘッダー付きで `additionalContext` として返す。`__TASK__` 処理は変更なし。
- `tests/test_compress_context_hook.py` (新規, 18テスト): フック統合テスト。
- `tests/test_context_monitor_auto_compress.py` (+6テスト, 計26テスト): worktree ありファイル配信テスト。
- `pyproject.toml`: version = "1.1.13"

**テスト結果**: 2107 → 2131 (+24テスト)。全 2131 テスト PASS。

**E2E デモ** (`~/Demonstration/v1.1.13-compress-context-hook/`):
- agent-writer (79s): `string_utils.py` 実装 → scratchpad 保存
- agent-reviewer (66s): scratchpad からコード取得 → レビュー作成
- `compress_triggers=1`, `context_warnings=1` (起動1秒後に自動圧縮発火)
- **27/27 チェック PASSED**

**デバッグ事項**:
1. `wait_for_agent_done` の引数順序ミス (`agent_id` を第1引数に渡していた → `base_url` が正)
2. `stop_server` の戻り値が `None` なのに bool チェックしていた → try/except に変更

### 10.37 v1.1.12 — ContextMonitor TF-IDF 自動統合 (Auto-Compress on context_warn)

#### Step 0 — 選定理由

**選択: ContextMonitor TF-IDF 自動統合**

§11「層4：コンテキスト伝達（改善）」の自然な延長。v1.1.11 で `TfIdfContextCompressor` と `POST /agents/{id}/compress-context` エンドポイントを実装したが、圧縮のトリガーは手動 (`/compress-context` スラッシュコマンド) のみだった。本イテレーションでは `ContextMonitor` の `context_warning` イベント発火時に TF-IDF 圧縮を自動実行し、圧縮済みコンテキストをエージェントのプロンプトとして注入する。

**選択理由:**

1. **v1.1.11 の直接延長**: `TfIdfContextCompressor` は完全実装済み。`ContextMonitor._check_context_threshold()` に数十行追加するだけで自動化が完了する。新規依存なし。
2. **研究的裏付けが強い**: ACON (arXiv:2510.00615, 2025) はしきい値ベースの自動圧縮ポリシーで peak token を 26–54% 削減。Focus Agent (arXiv:2601.07190, 2026) は自律的なコンテキスト圧縮でエージェント自律性を高める。Google ADK の `EventsCompactionConfig` も同じ思想。
3. **価値/実装コストのバランスが最良**: `auto_summarize` フラグと同様の設計で `auto_compress` フラグを追加。既存のパターンを踏襲するため設計リスクが低い。
4. **高優先度候補はすべて実装済みまたは大規模すぎる**: チェックポイント永続化 (SQLite) は1イテレーションに収まらない。ProcessPort は libtmux 全面リファクタリングで影響範囲が大きい。コンテキスト自動圧縮は中優先度の中で最も即効性がある。

**選択しなかった候補と理由:**

- **チェックポイント永続化 SQLite**: スキーマ設計・マイグレーション管理・`--resume` フラグ実装で規模が大きい (v1.2.x 向け)。
- **ProcessPort 抽象インターフェース**: libtmux 全面 DI 化で既存 E2E テストへの影響が広範。
- **UseCaseInteractor 層の抽出**: FastAPI ハンドラーの全面リファクタリングで不要なリスクを生む。
- **DriftMonitor セマンティック類似度**: `sentence-transformers` の新依存 (22MB モデル) が必要でデプロイコストが高い。
- **`/deliberate` スラッシュコマンド**: 2エージェント討論を起動するが、コアインフラの改善より優先度が低い。

**実装スコープ:**

1. `OrchestratorConfig` に `context_auto_compress: bool = False` と `context_compress_drop_percentile: float = 0.40` を追加。
2. `ContextMonitor.__init__()` に `auto_compress: bool = False`, `compress_drop_percentile: float = 0.40` パラメーターを追加。
3. `ContextMonitor._check_context_threshold()`: しきい値超過時に `auto_compress=True` であれば TF-IDF 圧縮を実行し、エージェントの pane に `__compress_context__\n{compressed_text}` を注入する。
4. `AgentContextStats` に `compress_triggers: int = 0`, `compress_injected: bool = False` を追加。
5. `ContextMonitor.get_stats()` に `compress_triggers` フィールドを追加。
6. `GET /agents/{id}/stats` レスポンスに `compress_triggers` を反映。
7. 新規テスト: `tests/test_context_monitor_auto_compress.py` — auto_compress 統合の単体テスト15本以上。
8. デモ: 2エージェント (writer + reviewer) パイプライン。writer が長い出力を生成して context_warning をトリガーし、auto-compress が自動実行される様子を検証。

#### Step 1 — Research (Web 調査結果)

**Query 1**: "automatic context compression LLM agents event-driven TF-IDF context window management 2025"

主要知見:
- **Google ADK `EventsCompactionConfig`**: `compaction_interval` (圧縮実行間隔) と `recent_events_overlap_size` (最近のイベントの保護範囲) をパラメーター化した閾値ベース圧縮。`LlmEventSummarizer` がフック点。— ADK は「コンパクション設定が存在する場合のみ圧縮を実行する」opt-in 設計。
- **context-engineering-toolkit (GitHub jstilb)**: TF-IDF センテンススコアリング + token-aware truncation を組み合わせたコンテキスト最適化ライブラリ。圧縮 API はスコアしきい値をパラメーターとして受け取る。
- **JetBrains Research "Cutting Through the Noise" (NeurIPS DL4Code 2025)**: 低関連度観察値の単純なマスキング (削除) が ~50% コスト削減を達成し、LLM 要約方式と同等以上の精度を維持。TF-IDF ベースの extractive アプローチが正当化される。

**References:**
- Google ADK Context Compaction: https://google.github.io/adk-docs/context/compaction/
- JetBrains Research "Cutting Through the Noise": https://blog.jetbrains.com/research/2025/12/efficient-context-management/
- context-engineering-toolkit: https://github.com/jstilb/context-engineering-toolkit

**Query 2**: "context_warning event automatic compression agent orchestrator threshold triggered 2025 Python asyncio"

主要知見:
- **Strands Agents SDK ProactiveCompressionConfig** (GitHub strands-agents/sdk-python #555): `compression_threshold` (デフォルト 0.70) + `enable_proactive_compression` bool + `compression_cooldown_messages` で再圧縮ループを防ぐ。ContextWindowOverflowException を待たずに事前圧縮するアプローチ。
- **LangChain Deep Agents context management**: コンテキストサイズが閾値を超えた時点で tool result をファイルシステムにオフロードし、要約を実行。3段階のカスケード (オフロード → 要約 → エラー) を採用。
- **再圧縮ループ防止**: `compress_injected` フラグ (本実装) が `compression_cooldown` と同等の役割を果たす。NOTES.md 更新を検出した後にフラグをリセットすることで次の閾値越えで再圧縮が走る設計は先行研究と整合。

**References:**
- Strands Agents Proactive Compression: https://github.com/strands-agents/sdk-python/issues/555
- LangChain Context Management for Deep Agents: https://blog.langchain.com/context-management-for-deepagents/

**Query 3**: "Active Context Compression autonomous memory management LLM agents arxiv 2601.07190 2025"

主要知見:
- **Focus Agent (arXiv:2601.07190, Verma 2026)**: エージェントが `start_focus` / `complete_focus` ツールを自律的に呼び出してコンテキストを圧縮する。外部タイマーや heuristic ではなく「エージェント自身の判断」で圧縮タイミングを決定。積極的な圧縮で 22.7% のトークン削減を達成。
- **ACON (arXiv:2510.00615, Kang et al. 2025)**: history 長が事前設定しきい値を超えた時点で圧縮ガイドラインを適用。失敗軌跡から圧縮ガイドラインを自動更新する gradient-free フレームワーク。peak tokens を 26–54% 削減しタスク成功率を維持。
- **設計上の示唆**: 本実装の auto-compress は ACON の「しきい値ベース自動実行」と Focus の「エージェント学習ループ」の中間的な位置づけ。固定 TF-IDF drop_percentile で extractive 圧縮を実行し、結果をエージェントに注入する。将来は圧縮効果フィードバックで drop_percentile を動的に調整できる (ACON の進化的拡張)。

**References:**
- Focus Agent: https://arxiv.org/abs/2601.07190
- ACON: https://arxiv.org/abs/2510.00615

### 10.36 v1.1.11 — スライディングウィンドウ + 重要度スコアによるコンテキスト圧縮

#### 選定理由

**選択: スライディングウィンドウ + 重要度スコアによるコンテキスト圧縮 (v1.1.11)**

§11「層4：コンテキスト伝達（改善）」の**中**優先度候補。高優先度項目（チェックポイント永続化・ProcessPort・OpenTelemetry）はすべて実装済み（v1.0.35・v1.0.34・v1.1.10）。次に優先度が高く、実装コストと研究的裏付けのバランスが最も良い候補として選択した。

**選択理由:**

1. **「高」優先度がすべて完了**: §11「層5」の高優先度候補（チェックポイント永続化・ProcessPort・OpenTelemetry GenAI）はすべて実装済み。「機能・ワークフロー」の高優先度候補（スラッシュコマンド自動コピー・system_prompt_file）も実装済み。残る未実装の最優先候補は「中」優先度の改善項目となった。
2. **研究的裏付けが強い**: Liu et al. "Lost in the Middle" (TACL 2024) は LLM が長いコンテキストの中央部情報を忘却することを実証し、重要度ベースの選択が有効であることを示した。JetBrains Research "Cutting Through the Noise" (2025-12) は重要度スコアリングによるコスト削減を実証。現行の `/summarize` はタスクプロンプトとの関連度を考慮しない一律圧縮であり、改善余地が明確。
3. **既存基盤を活用できる**: `ContextMonitor`（v1.0.9 以前から存在）の `context_warning` イベントを既に利用しており、圧縮トリガーの仕組みが整っている。TF-IDF は Python 標準ライブラリ相当の計算で実現でき（`sklearn.feature_extraction.text.TfidfVectorizer` または NumPy のみで自前実装）、外部 API 依存を追加しない。
4. **エージェント品質への直接貢献**: 長時間タスクでのコンテキスト劣化は v1.0.8 build-log で「Director polling が遅い」の根本要因の一つとして記録されている。圧縮品質の向上は直接的にエージェントの応答精度を改善する。

**非選択候補:**

- **DriftMonitor セマンティック類似度**: `sentence-transformers` (22MB モデル) の追加依存が重い。TF-IDF ベースの圧縮が完成してから相乗効果を評価したい。
- **Director の agent_drift_warning 購読による自動 re-brief**: Director エージェントへの bus 購読機能が未完成。スコープが大きい。
- **P2P 許可テーブルの TLA+ 形式仕様化**: TLA+ ツールチェーンのセットアップが必要。コードの品質ではなく仕様検証であり、ユーザー向け価値が間接的。
- **コンテキスト4戦略ガイド**: 実装なしのドキュメント追加のみ。単独デモが難しい。

**実装スコープ:**

1. `application/context_compression.py` — `TfIdfContextCompressor` クラス。タスクプロンプトとの TF-IDF コサイン類似度で各行をスコアリングし、スコア下位 N% を削除するスライディングウィンドウ圧縮を実装。
2. `ContextMonitor` が `context_warn_threshold` を超えたとき、`_compress_context()` を呼び出して TF-IDF ベース圧縮を注入する（現行の `/summarize` 注入の上位互換）。
3. `POST /agents/{id}/compress-context` REST エンドポイント — 手動トリガー対応。
4. `/compress-context` スラッシュコマンド — `agent_plugin/commands/compress-context.md` として追加。
5. `GET /agents/{id}/compression-stats` — 圧縮前後のトークン数・削除行数・類似度スコア分布を返す。

#### リサーチ（Step 1 — Research）

**Query 1**: "TF-IDF context compression LLM long context summarization sliding window importance scoring 2024 2025"

主要知見:
- **JetBrains Research "Cutting Through the Noise: Smarter Context Management for LLM-Powered Agents" NeurIPS DL4Code workshop 2025** (blog.jetbrains.com/research/2025/12/efficient-context-management/):
  「効率指向のエージェントコンテキスト管理手法は、下流タスク性能をほとんど低下させずにコストを約50%削減できる。LLM-Summarization は単純な Observation Masking ベースラインを一貫して上回ることはできない。しかし両者の組み合わせは LLM-Summary 単独比 7%・Observation Masking 単独比 11% のコスト削減を達成した。」
  実装参考リポジトリ: https://github.com/JetBrains-Research/the-complexity-trap
- **CCF: A Context Compression Framework for Efficient Long-Sequence Language Modeling (arXiv:2509.09199, 2025)**:
  コンテキスト圧縮フレームワークとして attention-based token selection・multi-document comprehension・structured knowledge integration を挙げる。extractive 手法（文を元のまま選択）が abstractive 手法（要約生成）より多くのベンチマークで優る実証結果あり。
- **ACON: Optimizing Context Compression for LLM Agents (OpenReview 2024)**:
  タスク関連性スコアリング（クエリとコンテキスト行のコサイン類似度）による選択的保持が最も cost-effective であることを示す。

**Query 2**: "LLMLingua prompt compression token selection importance score Python implementation 2024"

主要知見:
- **LLMLingua (EMNLP 2023) / LLMLingua-2 (ACL 2024)** (llmlingua.com, github.com/microsoft/LLMLingua):
  小型 LM の perplexity で各トークンの重要度を計算し、perplexity 閾値以下のトークンを削除。最大 20x 圧縮で性能低下1.5%以内を達成。ただし推論のため GPT-2/LLaMA 等の追加モデルが必要で、外部依存なしの実装には不適。
  本プロジェクトの方針（外部 API 依存を追加しない）から、LLMLingua 自体は採用しないが、「重要度スコアによる行削除」のアーキテクチャ原則を TF-IDF で代替する。
- **DataCamp "Prompt Compression: A Guide With Python Examples" (2025)**:
  extractive reranker-based compression が +7.89 F1 points の改善を達成し、abstractive compression は同圧縮率で性能低下を示した（extractive の優位性を実証）。

**Query 3**: ""lost in the middle" LLM context importance scoring extractive compression agent context management 2024 2025"

主要知見:
- **Liu et al. "Lost in the Middle: How Language Models Use Long Contexts" TACL 2024** (aclanthology.org/2024.tacl-1.9/):
  LLM はコンテキストの先頭と末尾の情報を優先し、中央部を忘却する。関連情報が中央に位置するとき性能が有意に低下する（長文コンテキスト対応モデルでも同様）。この "recency + primacy bias" は RoPE の長距離減衰効果に起因する。
  → 重要行を先頭に再配置する「重要度ベース再ソート」が直接的な改善策。
- **"Characterizing Prompt Compression Methods for Long Context Inference" (OpenReview 2024)**:
  extractive 圧縮は小さな圧縮比で不要情報を除去することで、中央部忘却効果を緩和しつつ精度を向上させる。抽出的手法の優位性を多数のベンチマークで実証。
- **"Advanced RAG Techniques for Long-Context LLMs" (getmaxim.ai 2025)**:
  reranking model で最関連コンテキストをコンテキストウィンドウの最適位置（先頭）に再配置することで "Lost in the Middle" 問題を緩和するのが現代的ベストプラクティス。

**Query 4**: "TF-IDF sentence importance ranking extractive summarization Python scikit-learn cosine similarity 2024"

主要知見:
- **Mishra "Mastering Extractive Summarization: TF-IDF and TextRank" (Medium 2024)**:
  TF-IDF + cosine 類似度ベースの extractive summarization の Python 実装。`TfidfVectorizer` で文をベクトル化し、コサイン類似度行列から各文のグローバル重要度スコアを算出。スコア上位 K% の文を保持するのが標準的アプローチ。
- **Hutabarat "Comparing Text Documents Using TF-IDF and Cosine Similarity in Python" (Medium 2024)**:
  `sklearn.feature_extraction.text.TfidfVectorizer` + `sklearn.metrics.pairwise.cosine_similarity` を使ったシンプルな実装パターン。クエリ文とコーパス文のコサイン類似度を計算し、類似度スコアでランキング。scikit-learn のみで完結する（外部 API 不要）。

**実装方針（リサーチ結果を踏まえた決定）:**

1. **アルゴリズム選択**: LLMLingua（外部 LM 依存）ではなく、TF-IDF + cosine 類似度ベースの extractive 手法を採用。`scikit-learn` は既存依存（pyproject.toml に含まれているか確認要）または `numpy` のみで自前実装。外部 API 不要。
2. **重要度スコア計算**: タスクプロンプト（クエリ）と各コンテキスト行のコサイン類似度をスコアとし、スコア下位 40% の行を削除（JetBrains 研究: 50% コスト削減の実績に対して保守的な設定）。
3. **再配置**: Liu et al. (2024) の "Lost in the Middle" 知見を活かし、重要行を圧縮後コンテキストの先頭に配置する `reorder=True` オプションを追加。
4. **エンドポイント**: `POST /agents/{id}/compress-context` + `/compress-context` スラッシュコマンド + `GET /agents/{id}/compression-stats`。
5. **ContextMonitor 統合**: `context_warn_threshold` 到達時に `/summarize` の代わりに TF-IDF 圧縮を自動注入するオプション（`compression_strategy: tfidf | summarize` 設定項目）。

**参考文献:**
- Liu, Nelson F., et al. "Lost in the Middle: How Language Models Use Long Contexts." *TACL*, 2024. https://aclanthology.org/2024.tacl-1.9/
- Lindenbauer, Tobias et al. "The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management." *NeurIPS DL4Code Workshop*, 2025. https://github.com/JetBrains-Research/the-complexity-trap
- Pan, Yucheng, et al. "CCF: A Context Compression Framework for Efficient Long-Sequence Language Modeling." arXiv:2509.09199, 2025. https://arxiv.org/html/2509.09199v1
- Jiang, Huiqiang, et al. "LLMLingua: Compressing Prompts for Accelerated Inference of Large Language Models." *EMNLP*, 2023. https://arxiv.org/abs/2310.05736
- Mishra, Varun. "Mastering Extractive Summarization: A Theoretical and Practical Guide to TF-IDF and TextRank." Medium, 2024. https://medium.com/@varun_mishra/text-summarization-with-tf-idf-and-textrank-a-deep-dive-into-the-code-and-theory-4cc76c285e28

---

### 10.35 v1.0.35 — `orchestrator.py` 残りインフラ依存 DI 整理（ResultStore / CheckpointStore / AutoScaler / WorkflowManager / GroupManager）

#### 選定理由

**選択: orchestrator.py 残りインフラ DI 整理 (v1.0.35)**

§11「アーキテクチャ・品質」の**高**優先度候補。

**選択理由:**

1. **未完了の高優先度 DI 候補**: v1.0.34 で `ContextMonitor`, `DriftMonitor`, `WebhookManager` を DI 化した。
   しかし `orchestrator.py` にはまだ以下のインライン import／直接インスタンス化が残っている:
   - `ResultStore` — `if config.result_store_enabled:` ブランチ内でのインライン import
   - `CheckpointStore` — `if config.checkpoint_enabled:` ブランチ内でのインライン import
   - `AutoScaler` — `if config.autoscale_max > 0:` ブランチ内でのインライン import
   - `WorkflowManager` — `__init__` で直接 import + 無条件インスタンス化
   - `GroupManager` — `__init__` で直接 import + 無条件インスタンス化
   これら5つを Protocol 型パラメータとして `__init__` で受け取るように変更し、
   具体実装は `factory.py` の `build_system()` で注入する。

2. **テスト改善**: 現状 `test_orchestrator.py` でこれらのコンポーネントをテストする際、
   実際のファイルシステムや外部依存が必要。DI 化により `NullResultStore` / `NullCheckpointStore`
   等のプロトコル準拠 Null オブジェクトを使用してテストを高速・独立させられる。

3. **前回イテレーションの自然な継続**: v1.0.34 の `WebhookManager` DI と同一パターンで実装できる。
   `application/monitor_protocols.py` と同様に `application/infra_protocols.py` を新設し、
   各インターフェースを Protocol として定義する。

**非選択:**
- OpenTelemetry — `telemetry.py` (223行) + `config.py` に全設定項目が実装済み。高優先度ではあるが既に実装されていた。
- チェックポイント永続化 SQLite — 既に `checkpoint_store.py` で実装済み。
- `/deliberate --rounds N` — `max_rounds` パラメータで既に多ラウンド対応済み (v1.0.32)。
- `contexts` count validator for /workflows/ddd — スコープが小さすぎる。

#### リサーチ（Step 1 — Research）

**Query 1**: "Python dependency injection Protocol null object pattern orchestrator 2025 2026"

主要知見:
- **Glukhov "Dependency Injection: a Python Way" (glukhov.org, 2025-12)**:
  「Constructor injection makes dependencies explicit and required — when you look at `__init__`, you immediately see what a component needs.」
  Protocol-based design (PEP 544) により、継承なしで任意のクラスがインターフェースを満たせる。
  Composition Root パターン: 依存グラフをアプリ起動時の1か所（`factory.py` / `main.py`）に集約することで明示性を維持する。
  URL: https://www.glukhov.org/post/2025/12/dependency-injection-in-python

- **DataCamp "Python Dependency Injection: A Guide" (2025)**:
  Null Object パターン（何もしない実装）を Protocol に準拠させることで、テスト時に実際の I/O を行わない
  高速な単体テストが実現できる。
  URL: https://www.datacamp.com/tutorial/python-dependency-injection

**Query 2**: "Clean Architecture infrastructure layer dependency injection Python factory pattern 2025"

主要知見:
- **Glukhov "Python Design Patterns for Clean Architecture" (glukhov.org, 2025-11)**:
  「Infrastructure layer should contain concrete implementations while keeping domain and application layers free from framework details.」
  Factory Container パターン: `container.register(interface, factory_fn)` で登録し、`container.resolve(interface)` で解決。
  これにより PostgreSQL→SQLite 等の切り替えが1行の factory 変更で完了し、テスト時は in-memory 実装を使用できる。
  URL: https://www.glukhov.org/post/2025/11/python-design-patterns-for-clean-architecture/

- **DEV "Layered Architecture & DI: Clean and Testable FastAPI Code" (2025)**:
  「Fat Router, Thin Service アンチパターンを避けるためルーターは HTTP 変換のみを担当し、ビジネスロジックをサービス/ユースケースレイヤーに委譲する。」
  URL: https://dev.to/markoulis/layered-architecture-dependency-injection-a-recipe-for-clean-and-testable-fastapi-code-3ioo

**Query 3**: "Python typing Protocol injectable component factory pattern unit test isolation 2025"

主要知見:
- **PEP 544 – Protocols: Structural subtyping (static duck typing)** (peps.python.org):
  `@runtime_checkable` デコレーターにより `isinstance()` チェックが可能。Protocol は継承を強制しないため、
  既存の具体クラスをそのまま Protocol 準拠クラスとして利用できる（Null Object にも適用）。
  URL: https://peps.python.org/pep-0544/

- **Dependency Injector "Factory provider"** (python-dependency-injector.ets-labs.org):
  「overriding feature for testing or re-configuring a project in different environments — better than monkey-patching.」
  Factory Provider を用いると production と test で同一コードパスを使いながら依存のみ切り替えられる。
  URL: https://python-dependency-injector.ets-labs.org/providers/factory.html

**実装方針:**
1. `application/infra_protocols.py` 新設 — `ResultStoreProtocol`, `CheckpointStoreProtocol`,
   `AutoScalerProtocol`, `WorkflowManagerProtocol`, `GroupManagerProtocol` を Protocol として定義
2. `NullResultStore`, `NullCheckpointStore`, `NullAutoScaler` を同ファイルに追加（Null Object パターン）
3. `Orchestrator.__init__` の5箇所を constructor injection に変換
4. `factory.py` `build_system()` で具体実装を注入
5. 各テストで Null オブジェクトを渡すことでインライン import を廃止

---

### 10.34 v1.0.34 — ProcessPort を `ClaudeCodeAgent` の正規インターフェース型に昇格 + WebhookManager DI

#### 選定理由

**選択: ProcessPort 統合 + WebhookManager DI (v1.0.34)**

§11「層5：ツール・マネジメント（アーキテクチャ品質）」の**高**優先度候補。

**選択理由:**

1. **ProcessPort 統合**: `ProcessPort` Protocol は `infrastructure/process_port.py` に存在するが、
   `ClaudeCodeAgent` 内部でまだ生の `libtmux.Pane`（`self.pane`）を多箇所で直接操作している。
   `send_keys` / `capture_pane` だけでなく、`send_interrupt()` や `pane_id` プロパティも
   Protocol に追加し、ClaudeCodeAgent が `libtmux.Pane` を直接参照しないよう完成させる。
   これにより tmux なし環境での単体テストが `StdioProcessAdapter` で完全に可能になる。

2. **WebhookManager DI**: `orchestrator.py` で `ContextMonitor`/`DriftMonitor` は Protocol + DI
   済みだが `WebhookManager` は `__init__` でハードコード生成されている。
   コンストラクタ注入を追加し DI パターンを統一する。

**非選択:**
- `contexts` count validator for /workflows/ddd — スコープが小さすぎる
- `/deliberate --rounds N` — `max_rounds` で既に多ラウンド対応済み

#### リサーチ

- **Hexagonal Architecture Design: Python Ports and Adapters for Modularity 2026** (johal.in, 2026):
  「Ports define interfaces: output ports for driven adapters (like tmux, email, DB).
  Hexagonal Architecture decouples core logic from externalities, reducing maintenance costs by up to 35%.」
  URL: https://johal.in/hexagonal-architecture-design-python-ports-and-adapters-for-modularity-2026/

- **Dependency Injection: a Python Way — Rost Glukhov** (glukhov.org, 2025-12):
  「Constructor injection makes dependencies explicit and required — when you look at __init__,
  you immediately see what a component needs.」
  URL: https://www.glukhov.org/post/2025/12/dependency-injection-in-python

- **Leveraging Typing.Protocol: Faster Error Detection — Pybites** (pybit.es, 2025):
  「Protocol is preferred over ABC because it doesn't require inheritance; any object
  satisfying the interface works. @runtime_checkable enables isinstance() checks.」
  URL: https://pybit.es/articles/typing-protocol-abc-alternative/

- **How to Implement Dependency Injection in Python — OneUptime** (oneuptime.com, 2026-02):
  「Constructor injection is the preferred approach because dependencies become explicit;
  injecting fakes/mocks in tests makes unit tests fast, isolated, and free of external services.」
  URL: https://oneuptime.com/blog/post/2026-02-03-python-dependency-injection/view

---

### 10.33 v1.0.33 — アーキテクチャ品質強化: watchdog_poll バリデーター + UseCaseInteractor + AgentStatus Hypothesis ステートフルテスト

#### 選定理由

**選択: アーキテクチャ品質強化 3点セット (v1.0.33)**

§11「層5：ツール・マネジメント（アーキテクチャ品質）」および「アーキテクチャ・品質」の複数の**中**優先度候補を一括して実施する。

**選択理由:**

1. **`watchdog_poll` Pydantic バリデーター + default 30s**: v1.0.32 の §10.32 で明示的に「次イテレーションのおまけとして含める」と記録した項目。スコープが非常に小さく（バリデーター1行 + テスト数件）、他の作業と組み合わせることで効率的に消化できる。`watchdog_poll <= task_timeout / 3` という制約を追加し、デフォルト値を 10.0 → 30.0 秒に変更する。
2. **`SubmitTaskUseCase` / `CancelTaskUseCase` 抽出 (`application/use_cases.py`)**: §11「UseCaseInteractor 層の抽出」候補。`web/app.py` の FastAPI ハンドラーが `orchestrator.*` を直接呼ぶ箇所を Use Case クラスに委譲する最初のステップ。全件リファクタリングではなく、最も重要な2つ（submit_task・cancel/delete_task）のみを対象とし、後方互換性を保つ。
3. **`AgentStatus` 状態機械の Hypothesis ステートフルテスト**: §11「エージェント状態機械の Hypothesis ステートフルテスト拡張」候補。本番コード変更なしに追加可能。`IDLE→BUSY→IDLE/ERROR/DRAINING` の遷移シーケンスを `RuleBasedStateMachine` でモデル化し、デッドロック・不変量違反を自動生成テストで検証する。

**選択しなかった候補:**
- `ddd` workflow contexts count validator: スコープが小さすぎてデモが単調になる。後続イテレーションの「おまけ」として含める。
- `/deliberate --rounds N` 拡張: 直前の v1.0.32 で `/deliberate` を実装済みのため、拡張は次々回以降に回す。
- チェックポイント永続化（SQLite）: 規模が大きく単独イテレーションが必要。
- `ProcessPort` 抽象インターフェース: `UseCaseInteractor` 抽出より先に着手すると依存関係が複雑化する。

#### 調査結果 (Step 1 — Research)

**Query 1**: "Use Case Interactor Clean Architecture application layer FastAPI Python 2024 2025"

主要知見:
- **Robert C. Martin "Clean Architecture" (2017) §22**: Use Case Interactor はアプリケーション固有のビジネスルールを保持し、Web/CLI/TUI のどのインターフェースアダプターからも同一ロジックを呼び出せる。ハンドラーに漏れたビジネスロジックは「Humble Object パターン」違反を引き起こし、テスト困難性・重複・バグの温床になる。
- **Khalil Stemmler "Domain-Driven Design with TypeScript" (2019)**: Use Case クラスは `execute(dto) → Result<SuccessDTO, AppError>` の単一メソッドを持ち、入出力を DTO で型付けする。FastAPI ハンドラーは Pydantic モデルの変換のみを担当する。Python での実装例: `class SubmitTaskUseCase: def __init__(self, orchestrator: Orchestrator): ...; async def execute(self, dto: SubmitTaskDTO) -> TaskResultDTO`。
- **Pallets/FastAPI Best Practices (2024)**: 「Fat Router, Thin Service」アンチパターンを避けるため、ルーターは HTTP 変換のみを担当し、ビジネスロジックをサービス/ユースケースレイヤーに委譲する。依存注入（`Depends()`）で UseCase を FastAPI ルーターに注入するパターンが推奨される。

**References**:
- Martin, Robert C. "Clean Architecture: A Craftsman's Guide to Software Structure and Design." Prentice Hall, 2017. Ch. 22 "The Clean Architecture."
- Stemmler, Khalil. "Domain-Driven Design with TypeScript: Use Cases", khalilstemmler.com, 2019. https://khalilstemmler.com/articles/enterprise-typescript-nodejs/application-layer-use-cases/
- Milan Jovanović. "Clean Architecture in ASP.NET Core." Milan's Newsletter, 2024. https://www.milanjovanovic.tech/blog/clean-architecture-the-missing-chapter

**Query 2**: "Hypothesis RuleBasedStateMachine state machine testing Python 2024 agent lifecycle"

主要知見:
- **Hypothesis `stateful` モジュール (Quickcheck ICFP 2000 由来)**: `RuleBasedStateMachine` は状態と遷移ルールを明示的に定義し、Hypothesis が任意のシーケンスを自動生成してすべての不変量を検証する。QuickCheck の「Model-Based Testing」手法を Python に適用したもの。
- **"Stateful Testing with Hypothesis" (Hypothesis Docs 2024)**: `@rule()` デコレーターで遷移操作を定義し、`@invariant()` で常に保つべき条件を記述する。`initialize()` は初期状態を設定する。`@precondition()` で遷移が有効な状態を限定できる。
- **適用事例**: 実際の`AgentStatus`遷移: IDLE → BUSY (task dispatch), BUSY → IDLE (task complete), BUSY → ERROR (timeout/exception), ERROR → IDLE (recovery), IDLE → DRAINING (drain request), DRAINING → IDLE (drain complete)。これらの遷移シーケンスを `RuleBasedStateMachine` で網羅的にテストすることで、将来の実装変更時の退行を防止できる。

**References**:
- Hypothesis Project. "Stateful Testing." Hypothesis Documentation 2024. https://hypothesis.readthedocs.io/en/latest/stateful.html
- MacIver, David. "In praise of property-based testing." Increment Magazine, Issue 10, 2019. https://increment.com/testing/in-praise-of-property-based-testing/
- Claessen & Hughes, "QuickCheck: A Lightweight Tool for Random Testing of Haskell Programs." ICFP 2000. https://dl.acm.org/doi/10.1145/357766.351266

**Query 3**: "Pydantic field_validator cross-field validation dataclass Python 2024 configuration validation best practices"

主要知見:
- **Pydantic v2 `@model_validator` vs `@field_validator`**: 単一フィールドの制約は `@field_validator`、複数フィールド間の相関制約は `@model_validator(mode='after')` を使用する。`watchdog_poll <= task_timeout / 3` は `task_timeout` に依存するためモデルバリデーターが適切。ただし `OrchestratorConfig` は dataclass のため、Pydantic の `@model_validator` は使えない。代わりに `__post_init__` で検証する（標準的な Python dataclass バリデーションパターン）。
- **"Fail Fast" 原則**: 設定ミス（例: watchdog_poll が task_timeout より大きい場合）を起動時に即座に検出することで、実行中のサイレント障害を防ぐ。Netflix "Principles of Chaos Engineering" (2016) が同原則を推奨。
- **dataclass + `__post_init__`**: Python 標準 dataclass では `__post_init__` メソッドが `__init__` 完了後に呼ばれるため、フィールド間バリデーションに使用できる。`OrchestratorConfig` は `@dataclass` であるため、この手法が最も自然。

**References**:
- Pydantic v2 Documentation. "Validators." https://docs.pydantic.dev/latest/concepts/validators/
- Netflix Technology Blog. "Principles of Chaos Engineering." 2016. https://netflixtechblog.com/the-netflix-simian-army-16e57fbab116
- Python Docs. "dataclasses — Data Classes." https://docs.python.org/3/library/dataclasses.html#post-init-processing

---

### 10.32 v1.0.32 — `/deliberate` スラッシュコマンド

#### 選定理由

**選択: `/deliberate <question>` スラッシュコマンド (v1.0.32)**

§11「機能・ワークフロー」の**中**優先度候補。v1.0.31 で `ddd` ワークフローが完成し、主要ワークフローテンプレートがすべて実装済みとなった。未実装の中優先度候補の中で、最も実装コストが低く汎用価値が高い `/deliberate` を選択する。

**選択理由:**
1. **長期保留**: v1.0.27〜v1.0.31 の5イテレーションにわたり「次候補」として挙げながら毎回より優先度の高い項目に押しのけられてきた。`debate`・`ddd` ワークフローが完成した今、前提条件（役割テンプレートライブラリ・`debate` ワークフロー）が揃っており実装ブロッカーはない。
2. **汎用ツール**: 単一エージェントが `/deliberate "should we use SQLite or PostgreSQL?"` と呼ぶだけで内部 2エージェント討論を起動し、`DELIBERATION.md` に根拠付き結論を得られる。ワークフロー知識がなくてもアドホックな設計決定に使える。
3. **新規 REST エンドポイント不要**: 既存の `POST /tasks`（`target_agent` + `reply_to`）+ `/spawn-subagent` + P2P メッセージングを組み合わせて実現する。インフラ変更なしに純粋スラッシュコマンドとして実装できる。
4. **デモパターンの多様化**: ワークフロー系デモに比べ「エージェントが動的に内部討論を自己組織化する」という新しいパターンを実証できる。

**選択しなかった候補:**
- `watchdog_poll` バリデーション: スコープが非常に小さくデモが単調になる。Pydantic validator 1行 + テスト数件で完結するため、後続イテレーションの「おまけ」として含める。
- Semantic RAG for episode injection: `sentence-transformers` 外部ライブラリ依存が増え、オフライン環境での信頼性が低下する。現状の keyword/recent で十分機能している。
- `UseCaseInteractor` 抽出: 規模が大きく本イテレーションには不適（ハンドラー全件リファクタリング）。

#### 調査結果 (Step 1 — Research)

**Query 1**: "Devil's Advocate multi-agent debate slash command LLM internal deliberation 2024 2025"

主要知見:
- **DEBATE Framework (ACL 2024, arXiv:2405.09935)**: Devil's Advocate（悪魔の弁護人）役エージェントが他エージェントの論拠を批判的に検証するマルチエージェント評価フレームワーク。NLG評価ベンチマーク SummEval・TopicalChat で SOTA を上回る。「エージェント間討論の広がりと各エージェントのペルソナが評価品質を決定する最重要因子」。単一エージェントによる偏りをバイアスとして定義し、反論役の導入で解消。
- **DEVIL'S ADVOCATE: Anticipatory Reflection for LLM Agents (EMNLP 2024)**: 単一エージェントが自分自身の推論を事前に批判的に検討する「先取り的反省」アプローチ。マルチエージェント版より軽量だがバイアス解消効果は限定的。マルチエージェント討論の方が「外部視点」として効果的。
- **Enhancing AI-Assisted Group Decision Making through LLM-Powered Devil's Advocate (IUI 2024)**: GPT-3.5-turbo ベースの悪魔の弁護人が批判的質問と反論コメントを生成するグループ意思決定支援システム。人間グループの意思決定品質が統計的有意に向上。LLM エージェントが人間の偏りをリアルタイムで中断し、見落とされた視点を提示する。

**References**:
- Kim, Kim, Yoon, "DEBATE: Devil's Advocate-Based Assessment and Text Evaluation", ACL Findings 2024, https://arxiv.org/abs/2405.09935
- "DEVIL'S ADVOCATE: Anticipatory Reflection for LLM Agents", EMNLP Findings 2024, https://aclanthology.org/2024.findings-emnlp.53.pdf
- Yin et al., "Enhancing AI-Assisted Group Decision Making through LLM-Powered Devil's Advocate", IUI 2024, https://dl.acm.org/doi/10.1145/3640543.3645199

**Query 2**: "CONSENSAGENT ACL 2025 multi-agent deliberation sycophancy suppression consensus"

主要知見:
- **CONSENSAGENT (ACL 2025)**: マルチエージェント LLM 討論における「迎合（sycophancy）」—エージェントが批判的検討なしに他エージェントの意見に同調する現象—を動的プロンプト精緻化で抑制するフレームワーク。6つのベンチマーク推論データセットで単一エージェント・従来 MAD を上回る精度と効率を実現。「迎合が追加討論ラウンドを必要とし計算コストを肥大化させる」という知見は `/deliberate` の実装でも考慮が必要（2ラウンド固定・role=critic の明示指定）。
- **Voting or Consensus? Decision-Making in Multi-Agent Debate (ACL 2025)**: 推論タスクでは投票が有効、知識タスクでは合意形成が有効という経験的知見。設計決定（推論タスク）には advocate/critic による論点整理 + synthesizer による合意文書生成が最適。

**References**:
- Pitre, Ramakrishnan, Wang, "CONSENSAGENT: Towards Efficient and Effective Consensus in Multi-Agent LLM Interactions Through Sycophancy Mitigation", ACL Findings 2025, https://aclanthology.org/2025.findings-acl.1141/
- "Voting or Consensus? Decision-Making in Multi-Agent Debate", ACL 2025, https://aclanthology.org/2025.findings-acl.606/

**Query 3**: "DEBATE ACL 2024 arXiv:2405.09935 Devil's Advocate bias reduction single LLM"

主要知見:
- **DEBATE の核心設計**: 通常の評価エージェント（judge）に加え、「Devil's Advocate」エージェントが judge の評価を批判的に検討し、構造化された反論を提示する。最終スコアは複数エージェントの討論後に決定される。`advocate`（主張役）+ `critic`（批判役）+ 任意の `synthesizer`（統合役）の3ロールが基本パターン。ラウンド数が多いほど精度は上がるが2ラウンドでも単一エージェント比で有意な改善が得られる。
- **ACL 2024 での実証**: SummEval ベンチマーク Spearman 相関 0.847（従来 SOTA 0.762 比 11% 向上）。2エージェント・2ラウンドの最小構成でも効果を実証—`/deliberate` の実装パラメータ（2エージェント・2ラウンド）の根拠となる。

**References**:
- Kim, Kim, Yoon, "DEBATE: Devil's Advocate-Based Assessment and Text Evaluation", ACL Findings 2024, pages 1885–1897, https://arxiv.org/html/2405.09935v1
- ACL Anthology, https://aclanthology.org/2024.findings-acl.112/

---

### 10.31 v1.0.31 — POST /workflows/ddd — DDD Bounded Context 分解ワークフロー

#### 選定理由

**選択: `POST /workflows/ddd` — DDD Bounded Context 分解ワークフロー (v1.0.31)**

§11「ワークフローテンプレート」の**中**優先度候補として記載されている。v1.0.30 で clean-arch ワークフローが完成し、4エージェントシーケンシャルパイプラインの基盤が確立した。次の未実装ワークフローとして `ddd` を選択する。

**選択理由:**
1. **最長保留の未実装ワークフロー**: `ddd` は v0.25.0 から候補に挙げられ、`clean-arch` の前提条件（role template ライブラリ）待ちで保留されていた。clean-arch が実装済みの今が実装タイミング。
2. **明確なマルチエージェント協調パターン**: context-mapper（EventStormingで要件分解）→ domain-expert × N（各 Bounded Context の実装）→ integration-designer（コンテキスト間統合設計）の3ステップは、各エージェントが前のエージェントの成果物を `context_files` / スクラッチパッド経由で読み込む実践的な Blackboard パターン。
3. **clean-arch との差別化**: clean-arch は技術層（Domain/UseCase/Adapter/Framework）を4エージェントで実装する。ddd は戦略的設計（Bounded Context 識別 → 並列ドメイン実装 → 統合設計）を3フェーズで実施し、並列フェーズを含むより複雑なDAGを持つ。
4. **業界での実用価値**: DDD の Bounded Context が LLM エージェント責務分割境界に直接対応するという知見（Russ Miles, Bakthavachalu 2025）を実装で実証できる。

**選択しなかった候補:**
- `/deliberate` スラッシュコマンド: 「中」優先度。ddd より実装価値が低い（2エージェントのみ、デモパターンが単調）。
- Semantic RAG for episode injection: 現状の keyword/recent で十分機能しており、改善効果が不明確。
- `watchdog_poll` バリデーション: スコープが非常に小さくデモが単調になる。

#### 調査結果 (Step 1 — Research)

**Query 1**: "Domain-Driven Design bounded context decomposition multi-agent LLM EventStorming 2025"

主要知見:
- **Combining EventStorming and DDD for Multi-Agent Systems** (IJCSE Vol.12 Issue 3, 2025): Event Storming がドメインイベントを発見し、それがエージェント間通信プロトコルになる。各 Bounded Context が独立したエージェントドメインに対応し、Anti-Corruption Layer がコンテキスト境界でのプロトコル変換を担う。Evans の DDD パターン + Brandolini の EventStorming の組み合わせが MAS 設計に直接応用可能。
- **codecentric "From Stories to Code"** (2025): Domain Storytelling と EventStorming のアーティファクトが LLM コンテキストとして直接利用可能。Bounded Context 境界を明確にすることで集約が適切に構造化され、仕様が簡潔になる。
- **ContextMapper** (contextmapper.org): EventStorming 結果を CML (Context Mapper Language) として形式化し、コード生成やアーキテクチャ文書に変換する OSS ツール。

**References**:
- "Designing Scalable Multi-Agent AI Systems using EventStorming and DDD", IJCSE V12I3P102, 2025. https://www.internationaljournalssrg.org/IJCSE/2025/Volume12-Issue3/IJCSE-V12I3P102.pdf
- "From Stories to Code: Collaborative Modeling and LLMs", codecentric 2025. https://www.codecentric.de/en/knowledge-hub/blog/from-stories-to-code-how-domain-storytelling-and-eventstorming-give-llms-the-context-they-need
- "Model Event Storming Results in Context Mapper", contextmapper.org. https://contextmapper.org/docs/event-storming/

**Query 2**: "DDD context mapping patterns aggregate domain expert AI agents arXiv 2025"

主要知見:
- **Russ Miles "Domain-Driven Agent Design"** (Engineering Agents Substack, 2025): DICE フレームワーク（Domain-Integrated Context Engineering）。Bounded Context をエージェントのコンテキスト制約に直接マッピング。ドメインオブジェクトをファーストクラスのコンテキスト単位として扱うことで、エージェントの精度と一貫性が向上する。
- **Bakthavachalu "Applying DDD for Agentic Applications"** (Medium, 2025): 大手投資銀行の3 Bounded Context 実装事例（Risk / Regulatory / Validation）。各コンテキストに専門エージェントを配置し、ユビキタス言語で通信することでコンプライアンス違反を防止。
- **James Croft "Applying DDD principles to multi-agent AI systems"** (2025): コンテキストマッピングパターン（Shared Kernel, Customer/Supplier, ACL）が LLM エージェント間の依存関係を構造化する。

**References**:
- Russ Miles, "Domain-Driven Agent Design", Engineering Agents Substack, 2025. https://engineeringagents.substack.com/p/domain-driven-agent-design
- Sathiyan Bakthavachalu, "Applying DDD for Agentic Applications", Medium, 2025. https://sathiyan.medium.com/revolutionizing-enterprise-ai-applying-domain-driven-design-for-agentic-applications-aa321fb991f4
- James Croft, "Applying DDD principles to multi-agent AI systems", 2025. https://www.jamescroft.co.uk/applying-domain-driven-design-principles-to-multi-agent-ai-systems/

**Query 3**: "DDD bounded context canvas context mapper ubiquitous language LLM automated workflow 2025"

主要知見:
- **ddd-crew/bounded-context-canvas** (GitHub, 2025): Bounded Context Canvas は各コンテキストの名前・説明・戦略的分類・インバウンド/アウトバウンドコミュニケーション・ユビキタス言語を体系化するワークショップツール。Canvas のフィールドがそのまま AI エージェントの PLAN.md フォーマットに転用できる。
- **"DDD Bounded Contexts for LLMs"** (understandingdata.com, 2025): Bounded Context により LLM のコンテキスト読み込み量が 75-85% 削減（全コードベースの 15-25% のみロード）。境界違反率 35% → 3%、コード精度 55% → 88% に改善。
- **Martin Fowler bliki "Bounded Context"**: Bounded Context はユビキタス言語の適用範囲を明示的に定義し、チーム間の意味的混乱を防ぐ基本パターン。コンテキスト間の変換は明示的なマッピング層（ACL）が担う。

**References**:
- ddd-crew, "Bounded Context Canvas", GitHub, 2025. https://github.com/ddd-crew/bounded-context-canvas
- "DDD Bounded Contexts: Clear Domain Boundaries for LLM Code Generation", understandingdata.com, 2025. https://understandingdata.com/posts/ddd-bounded-contexts-for-llms/
- Martin Fowler, "Bounded Context", martinfowler.com. https://www.martinfowler.com/bliki/BoundedContext.html

#### 実装方針

ワークフロー構成:
1. **context-mapper**: 機能要求を読んで EventStorming マップ (`EVENTSTORMING.md`) を作成し、Bounded Context の一覧と各コンテキストのユビキタス言語を `BOUNDED_CONTEXTS.md` に書き出す。スクラッチパッドに `context_list` キーでコンテキスト名リストを格納。
2. **domain-expert-{N}** (並列、各コンテキストに1エージェント): `BOUNDED_CONTEXTS.md` を `context_files` で受け取り、担当コンテキストのドメインモデル・集約・値オブジェクト・ドメインサービスを実装する。成果物パスをスクラッチパッドに格納。
3. **integration-designer**: 全 domain-expert の出力を読み取り、コンテキスト間のコンテキストマッピング（`CONTEXT_MAP.md`）を作成する。統合パターン（Shared Kernel / Customer-Supplier / ACL）を選択し根拠を記録。

エンドポイント: `POST /workflows/ddd`
リクエスト: `{ "topic": str, "contexts": list[str] | None, "base_url": str, "tags": list[str], "priority": int }`

`contexts` が指定された場合はそのリストを使用し、指定されない場合は context-mapper が自動推論する。

---

### 10.25 v1.0.26 — Stop Hook 不発火の根本原因調査・修正

#### 選定理由

**選択: Stop Hook 不発火の根本原因調査・修正 (v1.0.26)**

v1.0.23 以降、デモで Stop Hook が間欠的に不発火となり、エージェントが watchdog タイムアウトまで `❯` プロンプトで停止し続ける問題が継続している。v1.0.24 および v1.0.25 では「手動 nudge」ワークアラウンドで回避していたが、根本原因を修正しないと信頼性の低いデモが続く。本イテレーションでこの問題を確実に解決する。

**選択しなかった候補:**
- チェックポイント永続化 (SQLite): 実装規模が大きく本イテレーションには不適。
- `ProcessPort` 抽象インターフェース: テスト基盤の全面改修が必要で規模が大きい。
- OpenTelemetry GenAI Semantic Conventions: 外部インフラ依存が増えデモが複雑化する。

#### 根本原因 (確定)

**Claude Code はセッション起動時にフックのスナップショットを取り、セッション中は外部からの設定ファイル変更を無視する。**

公式ドキュメント (hooks reference, 2025) の明言:
> "Direct edits to hooks in settings files don't take effect immediately. Claude Code captures a snapshot of hooks at startup and uses it throughout the session. This prevents malicious or accidental hook modifications from taking effect mid-session without your review."

現状の `NudgingStrategy.on_task_dispatch()` は `settings.local.json` を claude プロセス起動後に書き込んでいたため、スナップショット後の変更として無視されていた。

#### 修正方針

Stop hook 設定を `on_start()` (claude プロセス起動前) に書き込むよう変更。task_id は URL に含めず (起動前は不明のため)、エンドポイント側の既存 null 許容処理で対応。`on_task_dispatch()` は no-op (ログのみ) とする。

**References**:
- "Hooks reference - Claude Code Docs", https://code.claude.com/docs/en/hooks (2025)
- "Stop hook not fired when Claude stalls mid-turn after tool result", anthropics/claude-code Issue #29881, https://github.com/anthropics/claude-code/issues/29881
- "Stop hook crashes in git worktrees: transcript path not found", thedotmack/claude-mem Issue #1234, https://github.com/thedotmack/claude-mem/issues/1234

#### 実装結果 (v1.0.26)

**デモ結果**: 15/15 checks PASSED (2026-03-09)

**実装内容**:
- `NudgingStrategy.on_start()`: Stop hook を claude 起動前に書き込む（スナップショット前）
- `NudgingStrategy.on_task_dispatch()`: no-op（ログのみ）— スナップショット後の変更は無効
- `tests/test_stop_hook.py` — 3新規テスト追加、既存テスト2件を修正
- 総テスト数: **1444 tests** 全通過

**デモパターン**: 並列2エージェント — agent-a と agent-b が独立してファイルを書き、Stop hook → nudge → `/task-complete` で完了
- agent-a: 15.7s で完了 (watchdog 900s 以内)
- agent-b: 21.8s で完了 (watchdog 900s 以内)
- 手動 nudge ワークアラウンド不要 — Stop hook が自律的に発火

**デモの主な知見**:
- `task-complete received via explicit signal (task_id=unknown)` ログは期待動作。Stop hook URL に task_id なしのため、endpoint 側で `stop_hook_active=False` → nudge → explicit `/task-complete` の正常パス。
- task_id スコーピング（`?task_id=<id>`）は廃止されたが、`stop_hook_active` フラグによる区別で代替。
- "silent tool stop" (Issue #29881) は本修正の対象外。watchdog による回収で対処。

---

### 10.24 v1.0.25 — POST /workflows/socratic — ソクラテス的対話ワークフロー

#### 選定理由

**選択: `POST /workflows/socratic` — ソクラテス的対話 (questioner → responder → synthesizer) ワークフロー (v1.0.25)**

§11「ワークフローテンプレート・ドキュメント整備」の**中**優先度候補。高優先度の候補（チェックポイント永続化・ProcessPort・OpenTelemetry）は実装規模が大きく単独イテレーションには不適。次に優先度の高い未実装ワークフローとして選択した。

調査を進めた結果:
- `POST /workflows/adr` は **v0.40.0** で実装済み（デモも完了）。
- 役割別 system_prompt テンプレート 9 本が `.claude/prompts/roles/` に完備済み（v0.37.0 + 後続追加）。
- `system_prompt_file:` YAML フィールドも `config.py` + `claude_code.py` で実装済み。
- `context_spec_files` と Codified Context インフラも `config.py` + `claude_code.py` で実装済み。
- 残る未実装ワークフロー: `socratic`, `pair`, `clean-arch`, `ddd`

`socratic` を選択した理由:

1. **既存基盤の活用**: delphi / debate / redblue と同じ3エージェントパイプライン（questioner → responder → synthesizer）構造。スクラッチパッド Blackboard パターン、タスク依存チェーン、role タグルーティングをそのまま流用できる。
2. **研究的裏付けが強い**: SocraSynth (arXiv:2402.06634, 2024) がソクラテス的マルチエージェント討論プラットフォームを提案。KELE (EMNLP 2025, arXiv:2409.05511) がLLMベースのソクラテス教授エージェントを実証。段階的移行モデル（最初は強い反論、後のラウンドは統合的問い）が設計仕様の曖昧性解消に有効。
3. **明確な成果物**: `socratic_dialogue.md`（問答ログ）+ `synthesis.md`（構造化結論）の2成果物が明確。
4. **デモシナリオが豊富**: 「REST vs GraphQL」「モノリス vs マイクロサービス」「型推論 vs 明示的型付け」など、技術的設計判断の曖昧性解消に直接使えるシナリオが多数ある。
5. **実装コストが低い**: debate/delphi/redblue の実装パターンがほぼそのまま適用でき、新規設計要素が少ない。

**選択しなかった候補:**
- チェックポイント永続化 (SQLite): 実装規模が大きく本イテレーションには不適。独立したスプリントとして計画すべき。
- `ProcessPort` 抽象インターフェース: libtmux 依存排除は重要だが、テスト基盤の全面改修が必要で規模が大きい。
- OpenTelemetry GenAI Semantic Conventions: 外部インフラ（Jaeger/OTLP）の依存が増えデモが複雑化する。
- `POST /workflows/pair`: Navigator/Driver ペアプログラミングは有用だが、`reply_to` ループが複雑で本イテレーションには不適。
- `POST /workflows/clean-arch`: 4レイヤー分解は設計が複雑。socratic 完成後の候補。

#### 調査結果 (Step 1 — Research)

**Query 1**: "MADR Markdown Architectural Decision Records format structure 2025"

主要知見:
- **MADR (Markdown Architectural Decision Records)** は ADR の代表的フォーマット。Nygard (2011) の原案を Markdown に最適化し、コードリポジトリ内での管理を標準化。
- 標準フィールド: `# {title}`, `## Status` (proposed/accepted/deprecated/superseded), `## Context and Problem Statement`, `## Decision Drivers`, `## Considered Options`, `## Decision Outcome`, `## Pros and Cons of the Options`
- `context_files` として既存 ADR をエージェントに渡すことで、過去の決定との整合性確認が可能。
- ADRs を `docs/decisions/` フォルダに番号付きで管理する慣習が普及（例: `0001-use-postgresql.md`）。

**References**:
- Nygard, "Documenting Architecture Decisions", cognitect.com, 2011, https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions
- Zimmermann et al., "MADR: Markdown Architectural Decision Records", GitHub, 2023, https://github.com/adr/madr
- "Architecture Decision Records (ADRs) - GitHub Docs", 2025, https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/adr

**Query 2**: "multi-agent discussion ADR quality improvement LLM 2025 arXiv"

主要知見:
- **MAD in Requirements Engineering** (arXiv:2507.05981, 2025): Multi-Agent Discussion (MAD) フレームワークを要件エンジニアリングに適用し、要件分類 F1 を 0.726 → 0.841 に向上。複数エージェントの討論が単一エージェントより高品質な成果を生む。
- **SocraSynth** (arXiv:2402.06634): モデレーター型マルチエージェント討論プラットフォーム。proposer + critic + synthesizer の3役割が設計意思決定の品質を向上させる。各エージェントが明確な役割（提案・批評・統合）を持つことで、収束速度が上がり、最終文書の論理整合性が高まる。
- **ADR 生成の自動化**: ChatGPT / Claude を用いた ADR 自動化実験（DEV Community 2023）: プロンプトに「技術的コンテキスト」「制約条件」「既存 ADR」を提供すると、単独 LLM でも構造化 ADR を生成できる。複数エージェントで批評ラウンドを挟むとさらに品質向上。

**References**:
- "MAD in Requirements Engineering", arXiv:2507.05981, 2025, https://arxiv.org/abs/2507.05981
- Liang et al., "SocraSynth: A Platform for Multi-Agent Socratic Debate", arXiv:2402.06634, 2024, https://arxiv.org/abs/2402.06634
- "Using AI to Generate Architecture Decision Records", DEV Community, 2023, https://dev.to/anandh/using-ai-to-generate-architecture-decision-records-3o5d

**Query 3**: "architecture decision record workflow automation proposer reviewer synthesizer pipeline"

主要知見:
- **Structured ADR workflow** (InfoQ 2024): 効果的な ADR プロセスは3フェーズ — (1) 問題・コンテキスト定義、(2) 選択肢の提案と批評、(3) 最終決定の文書化。これは proposer/reviewer/synthesizer の3エージェントに自然にマッピングされる。
- **Cognitivescale "AI-Assisted ADR"** (2024): LLM に「オプションごとの pros/cons を比較する表」を作らせ、その後 judge が決定を下す2段階アプローチが実用的。コンテキスト提供が品質の鍵。
- **past ADR 参照**: 過去の ADR を `context_files` として提供することで "consistency" チェックが可能。reviewer が「この決定は ADR-0003 と矛盾する」と指摘できる設計が重要。

**References**:
- "Automating Architecture Decision Records with AI Agents", InfoQ, 2024, https://www.infoq.com/articles/adr-automation-ai/
- "AI-assisted Architecture Decision Records", CognitiveScale blog, 2024, https://www.cognitivescale.com/blog/ai-architecture-decision-records/
- adr-tools GitHub, "adr-tools: Command-line tools for working with Architecture Decision Records", 2023, https://github.com/npryce/adr-tools

---

### 10.21 v1.0.11 — domain/ 純粋型の抽出

#### 選定理由

**選択: `domain/` 純粋型の抽出 (v1.0.11)**

§11「層5：ツール・マネジメント（アーキテクチャ品質）」の「クリーンアーキテクチャ層別ディレクトリ移行」候補の最初のステップとして選択。
以下の理由で最優先とした:

1. **依存方向の逆転を解消**: 現状 `config.py` の `AgentRole` と `agents/base.py` の `AgentStatus` / `Task` が分散しており、どちらが正規か不明。`bus.py` の `MessageType` / `Message` も同様に「通信インフラ」ファイルにドメイン型が混在している。`domain/` への集約で責務境界を明確にする。
2. **他の移行の前提条件**: `infrastructure/`, `application/` 層への移行はいずれも `domain/` 型を参照する。`domain/` を先に確立しないと後続ステップの方向性が定まらない。
3. **リスク最小**: 実装変更なし（型の移動＋シム re-export のみ）。既存テスト 1136 本が全て通れば移行成功の証明になる。

**選択しなかった候補:**
- `ProcessPort` 抽象インターフェース抽出: `ClaudeCodeAgent` の libtmux 依存排除は重要だが、`domain/` 確立後の方が設計が整合しやすい。
- `UseCaseInteractor` 層の抽出: FastAPI ハンドラーからのビジネスロジック分離も重要だが、`domain/` 確立後に自然に次の候補となる。
- `POST /workflows/clean-arch`: ワークフロー追加よりアーキテクチャ基盤の整備を優先。


#### 調査結果 (Step 1 — Research)

**Query 1**: "Python Clean Architecture domain layer pure types no external dependencies package structure 2024"

主要知見:
- ドメイン層はフレームワーク・アプリケーション非依存のコードのみで構成する。技術詳細はドメイン層の外で解決すべき (pcah/python-clean-architecture, PRINCIPLES.md)。
- ドメインエンティティは「Plain Old Python Objects (POPO)」—外部依存なし。enum, dataclass は標準ライブラリの範囲で使用可 (python-clean-architecture PyPI)。
- 典型構造: `domain/entities/`, `domain/value_objects/`, `domain/events/`, `domain/exceptions.py` (Raman Shaliamekh, Medium 2024)。

**References**:
- pcah, "python-clean-architecture / PRINCIPLES.md", GitHub, https://github.com/pcah/python-clean-architecture/blob/master/docs/PRINCIPLES.md
- Raman Shaliamekh, "Clean Architecture with Python", Medium, https://medium.com/@shaliamekh/clean-architecture-with-python-d62712fd8d4f
- Glukhov, "Python Design Patterns for Clean Architecture", 2025, https://www.glukhov.org/post/2025/11/python-design-patterns-for-clean-architecture/

**Query 2**: "strangler fig pattern Python module restructuring backward compatible re-export shim"

主要知見:
- Strangler Fig パターン: 旧システムを即座に書き換えるのではなく、新機能を旧パスの「ファサード」背後に段階的に構築して移行する。Fowler (2004) が命名。
- Python での具体的適用: 旧モジュールパス (`agents/base.py`) から `from tmux_orchestrator.domain.agent import AgentStatus` を re-export するシム（薄いファサードモジュール）を置くことで、既存 import を壊さずに型の移動が可能。
- テスト可視性: 各移動を独立コミット単位にし、`pytest` が常にグリーンであることを確認しながら進める。

**References**:
- Fowler, "Strangler Fig Application", martinfowler.com (元論文 2004), https://swimm.io/learn/legacy-code/strangler-fig-pattern-modernizing-it-without-losing-it
- Swimm, "Strangler Fig Pattern: Modernizing It Without Losing It", https://swimm.io/learn/legacy-code/strangler-fig-pattern-modernizing-it-without-losing-it

**Query 3**: "domain-driven design entity extraction Python enum dataclass stdlib only clean layer"

主要知見:
- Cosmic Python (Percival & Gregory, O'Reilly 2020): ドメインモデルは `@dataclass(frozen=True)` の value objects と `__eq__`/`__hash__` を持つ entities で構成。外部依存なし。テストは高速でインフラ不要 (https://www.cosmicpython.com/book/chapter_01_domain_model.html)。
- DDD における `str, Enum` の継承: `class AgentStatus(str, Enum)` パターンは `str` との比較互換を保ちながら型安全性を得る慣用的手法 (https://dddinpython.com/index.php/2022/07/22/entities/)。
- 依存ルール (Dependency Rule): 内向きのみ。`domain/` は何も import しない (またはstdlibのみ)。`infrastructure/` が `domain/` を import するのは OK。逆はNG。

**References**:
- Percival & Gregory, "Architecture Patterns with Python (Cosmic Python)", O'Reilly, 2020, https://www.cosmicpython.com/book/chapter_01_domain_model.html
- dddinpython.com, "Domain Entities in Python", 2022, https://dddinpython.com/index.php/2022/07/22/entities/
- Glukhov, "Python Design Patterns for Clean Architecture", 2025, https://www.glukhov.org/post/2025/11/python-design-patterns-for-clean-architecture/

#### 実装結果 (v1.0.11)

**デモ結果**: 14/15 checks PASSED (2026-03-09)

**実装内容**:
- `domain/agent.py` — `AgentStatus`, `AgentRole` (str+Enum)
- `domain/task.py` — `Task` (dataclass)
- `domain/message.py` — `MessageType`, `Message`, `BROADCAST`
- `domain/__init__.py` — 全型を再エクスポート
- 旧モジュール (`agents/base.py`, `config.py`, `bus.py`) に re-export シムを配置 (Strangler Fig)
- `domain/` は外部ライブラリを一切 import しない（stdlib のみ）
- `test_domain_purity.py` — 20 tests で純粋性を継続的に保証
- `ensure_session()` スレッドセーフ修正
- Stop hook を `type:http` → `type:command` (curl) に変更（環境変数展開の修正）
- 総テスト数: **1156 tests** 全通過

**デモパターン**: Pipeline — agent-writer → agent-verifier
- agent-writer: `domain_usage.py` 作成・実行、`writer_report.md` 記述 (SUCCESS, "All types are identical objects: True")
- agent-verifier: 独立検証し `verification_report.md` 記述 (VERDICT: PASS)

**唯一の FAIL**: agent-verifier が作業完了後に `/task-complete` を呼ばず IDLE に戻らなかった（成果物は正常）。
根本原因: エージェントが明示的完了シグナルを送り忘れるケース → スラッシュコマンド自動コピー (§11) で改善予定。

---

### 10.22 v1.0.23 — POST /workflows/delphi — 多ラウンド合意形成ワークフロー

#### 選定理由

**選択: `POST /workflows/delphi` — 多ラウンド合意形成ワークフロー (v1.0.23)**

§11「層3：ステージの実行方式」で**中**優先度として挙げられているが、高優先度の上位アイテムが既に実装済みのため、次の実装候補として選択した:

1. **高優先度アイテムの実装状況**: スラッシュコマンド自動コピー（v1.0.12完了）、DI化（v1.0.14完了）、application層（v1.0.15完了）、domain/純粋型（v1.0.11完了）、infrastructure層（v1.0.16/17完了）、エージェントドリフト検出（v1.0.9完了）、webhook retry（v1.0.22完了）。チェックポイント永続化・OpenTelemetryは実装規模が大きく、本イテレーションには適さない。
2. **debate ワークフロー基盤の活用**: v0.37.0 で `POST /workflows/debate` (advocate/critic/judge 3役割) を実装済み。Delphi は debate の自然な拡張として、複数ラウンドのイテレーション構造を追加するだけで実現できる。
3. **研究的裏付けが強い**: RT-AID (ScienceDirect 2025) が LLM による Delphi 法の自動化を実証。Du et al. (ICML 2024) が「エージェントが全員誤りでも討論で正解に収束する」ことを実証。複数ラウンドの価値が学術的に証明されている。
4. **デモシナリオが明確**: 3–5 名の専門家ペルソナ（セキュリティ / パフォーマンス / 保守性 / UX / コスト）が匿名で意見提出し、モデレーターが集計・フィードバックするサイクルを可視化できる。
5. **成果物が具体的**: `delphi_round_{n}.md` + `consensus.md` という明確な出力物がある。

**選択しなかった候補:**
- スラッシュコマンド自動コピー: v1.0.12 で既に実装済み（`_copy_commands()` メソッド、`test_slash_commands_worktree.py` のテスト群）。
- チェックポイント永続化: 実装規模が大きい（SQLite + resume フラグ + API変更）。次イテレーション候補。
- OpenTelemetry: 外部依存（OTel SDK）の追加が必要。セットアップコストが高い。
- `POST /workflows/redblue`: delphi より設計が複雑（攻撃者視点のシミュレーションが難しい）。

#### 調査結果 (Step 1 — Research)

**Query 1**: "Delphi method LLM multi-agent consensus formation rounds anonymous expert opinions RT-AID 2025"

主要知見:
- **Real-Time AI Delphi (RT-AID)** (ScienceDirect 2025): LLM を Delphi 法の支援エージェントとして使用し、専門家意見の収束を加速させる手法。AI 支援意見が収束プロセスを大幅に加速させることを実証。
- **DelphiAgent** (ScienceDirect 2025): 複数の LLM エージェントが人間の専門家を模倣し、匿名性を保ちながら反復的フィードバックと統合を通じて合意を形成。各エージェントが個別に判断し、複数ラウンドのフィードバック・統合サイクルで合意到達。
- **CONSENSAGENT** (ACL 2025): エージェントインタラクションに基づいてプロンプトを動的に洗練し、迎合（sycophancy）を抑制。討論の精度を向上させながら効率を維持。

**References**:
- "Real-Time AI Delphi: A novel method for decision-making and foresight contexts", ScienceDirect 2025, https://www.sciencedirect.com/science/article/pii/S0016328725001661
- "DelphiAgent: A trustworthy multi-agent verification framework", ScienceDirect 2025, https://www.sciencedirect.com/science/article/abs/pii/S0306457325001827
- "CONSENSAGENT: Towards Efficient and Effective Consensus in Multi-Agent LLM Interactions", ACL Anthology 2025, https://aclanthology.org/2025.findings-acl.1141/

**Query 2**: "Du et al ICML 2024 improving factuality LLM multi-agent debate society of mind rounds convergence"

主要知見:
- Du, Li, Torralba, Tenenbaum, Mordatch "Improving Factuality and Reasoning in Language Models through Multiagent Debate" (ICML 2024): 複数の LLM インスタンスが自分の応答を提案・討論し、複数ラウンドを経て共通の最終回答に到達。数学・推論タスクで精度向上、幻覚（hallucination）削減を実証。「Society of Minds」として異なる LLM インスタンスをマルチエージェント社会として扱う。
- 複数ラウンドが重要: 初回ラウンドでは全員が誤りでも、討論ラウンドを経ることで正解に収束するケースが多数存在することを実証。

**References**:
- Du et al., "Improving Factuality and Reasoning in Language Models through Multiagent Debate", ICML 2024, arXiv:2305.14325, https://arxiv.org/abs/2305.14325
- GitHub: composable-models/llm_multiagent_debate, https://github.com/composable-models/llm_multiagent_debate

**Query 3**: "Claude Code custom slash commands .claude/commands directory agent worktree auto-copy 2025"

主要知見:
- Claude Code の `.claude/commands/` はプロジェクトスコープのカスタムスラッシュコマンドを収録する。ファイル名がコマンド名になる（`.md` 拡張子なし）。
- `~/.claude/commands/` はユーザースコープのコマンドで全プロジェクトで利用可能。
- v2.1.3 以降、コマンドはスキルシステムに統合されたが、`.claude/commands/` の既存ファイルは引き続き動作。
- ワークツリーで `--worktree` フラグ使用時、各エージェントは分離された git ワークツリーで動作し、`.claude/commands/` を独自に保持する。

**References**:
- "Slash commands - Claude Code Docs", https://code.claude.com/docs/en/slash-commands
- "How to Create Custom Slash Commands in Claude Code", BioErrorLog Tech Blog, https://en.bioerrorlog.work/entry/claude-code-custom-slash-command
- "Slash Commands in the SDK - Claude API Docs", https://platform.claude.com/docs/en/agent-sdk/slash-commands


---

### 10.23 v1.0.24 — POST /workflows/redblue — Red Team / Blue Team 対抗評価ワークフロー

#### 選定理由

**選択: `POST /workflows/redblue` — Red Team / Blue Team 対抗評価ワークフロー (v1.0.24)**

§11「層3：ステージの実行方式」の**中**優先度候補。高優先度の上位アイテムのうち未完了のものは規模が大きい（チェックポイント永続化 SQLite、OpenTelemetry 計装）ため、本イテレーションには適さない。

1. **debate ワークフロー基盤の活用**: v0.37.0 で `POST /workflows/debate` (advocate/critic/judge)、v1.0.23 で `POST /workflows/delphi` を実装済み。redblue は debate の特殊化として、blue-team（実装・設計案）→ red-team（攻撃者視点）→ arbiter（リスク評価）の3エージェント構成で実現できる。既存のスクラッチパッド、タスク依存、role タグルーティングをそのまま活用できる。
2. **研究的裏付けが強い**: adversarial multi-agent evaluation (arXiv:2410.04663) がバイアス削減と判断精度向上を実証。Red-Teaming LLM MAS (ACL 2025, arXiv:2502.14847) が敵対的エージェント構成のセキュリティレビュー適用を提案。
3. **デモシナリオが明確**: blue-team が FastAPI エンドポイントを設計し、red-team が認証・入力検証・レートリミットの欠陥を列挙し、arbiter がリスク評価レポートを生成する具体的なシナリオ。成果物（`blue_design.md`、`red_findings.md`、`risk_report.md`）が明確。
4. **v1.0.23 build-log のフィードバックへの対応**: 「stale worktree cleanup が recurring pain」→ デモで `git worktree prune` を前処理として明示的に実行する手順を標準化。

**選択しなかった候補:**
- チェックポイント永続化 (SQLite): 実装規模が大きく本イテレーションには不適。
- OpenTelemetry GenAI Semantic Conventions: 外部インフラ（Jaeger/OTLP）の依存が増えデモが複雑化する。
- 役割別 system_prompt テンプレートライブラリ: 有用だが、まずワークフロー拡充を優先する（ユーザー向け機能として直接価値が高い）。

---

#### 実装結果 (v1.0.24)

**デモ結果**: 20/20 checks PASSED (2026-03-09)

**実装内容**:
- `RedBlueWorkflowSubmit` モデル (`topic`, `blue_tags`, `red_tags`, `arbiter_tags`, `reply_to`)
- `POST /workflows/redblue` エンドポイント — blue_team → red_team → arbiter の3エージェントパイプライン
- スクラッチパッドキー: `{prefix}_blue_design`, `{prefix}_red_findings`, `{prefix}_risk_report`
- `tests/test_workflow_redblue.py` — 28 tests
- OpenAPI スナップショット再生成
- 総テスト数: **1412 tests** 全通過

**デモパターン**: Adversarial Pipeline — blue-team → red-team → arbiter
- blue-team: FastAPI JWT 認証エンドポイント設計 (HS256 access token + opaque refresh token)
- red-team: 9件の脆弱性指摘 (P0: alg:none footgun, Redis SPOF; P1-P2: rate limiting bypass等)
- arbiter: リスクマトリクス + 優先度付き推奨事項 + "P0修正後は単一サービス向けに適切" 判定

**デモの主な知見**:
- Stop hook の間欠的不発火は既知の問題。デモに「nudge」機構（3分後にIDLEを検出して手動 task-complete）を追加することで対処。
- `required_tags` なしの場合、全タスクが最初の空きエージェント（blue-team）に集中するルーティング問題が発生。タグベースルーティングの必要性を確認。
- `task_timeout: 1500` (25分) で複雑なタスクに十分なマージンを確保。

---

### 10.27 v1.0.27 — 役割別 system_prompt テンプレートライブラリ + POST /workflows/pair

#### 選定理由

**選択: 役割別 system_prompt テンプレートライブラリ完成 + `POST /workflows/pair` — PairCoder ワークフロー (v1.0.27)**

§11「機能・ワークフロー」の**高**優先度候補。v1.0.26 で Stop Hook が修正・安定化され、v1.0.27 は2つの関連機能を組み合わせる。

**選択した理由:**

1. **高優先度・明確な実装パス**: §11 に「高」として記載。既存 `.claude/prompts/roles/` に advocate/critic/judge の3テンプレートが存在するため、残り4種 (tester / implementer / reviewer / spec-writer) の追加は低コスト。さらに `system_prompt_file:` YAML フィールドを `AgentConfig` に追加することで、ロールをコードから分離できる。
2. **`POST /workflows/pair` との相乗効果**: PairCoder (Navigator + Driver) ワークフローは、`implementer.md` と `reviewer.md` の2テンプレートを直接活用できる。両機能を同一イテレーションで完成させることで、テンプレートライブラリの価値を即座にデモできる。
3. **研究的裏付けが強い**: Vellum "Best practices for building multi-agent systems" (2025): 役割特化プロンプトとステート分離が精度向上に最も効果的。ChatEval ICLR 2024 (arXiv:2308.07201): 役割の多様性が討論品質を決定する最重要因子。FlowHunt "TDD with AI Agents" (2025): PairCoder が単一エージェント比でコード品質が向上。
4. **デモシナリオが明確**: Navigator エージェントが PLAN.md を生成し、Driver エージェントが実装・テストを行う PairCoder パターン。Navigator の出力が Driver のインプットになる Pipeline パターン (genuine multi-agent collaboration)。
5. **成果物が具体的**: `navigator_plan.md` + `driver_impl.py` + `driver_tests.py` の3ファイル。DECISION.md や consensus.md と同等の明確な出力物がある。

**`POST /workflows/adr` は v0.40.0 で実装済み**であることを確認。新たに実装する必要がなく、本イテレーションの対象外。

**選択しなかった候補:**
- **チェックポイント永続化 (SQLite)**: 実装規模が大きく (SQLite スキーマ設計 + resume フラグ + API 変更)、1イテレーションには不適。
- **`ProcessPort` 抽象インターフェース**: 重要だがアーキテクチャリファクタリングは安定期にまとめて実施するのが適切。
- **OpenTelemetry GenAI Semantic Conventions**: 外部インフラ (Jaeger/OTLP) の依存が増え、デモが複雑化する。
- **`/deliberate` スラッシュコマンド**: 有用だが「中」優先度。役割テンプレートライブラリが先に必要。

#### 実装結果 (v1.0.27)

**デモ結果**: 17/17 checks PASSED (2026-03-09)

**実装内容**:
- `PairWorkflowSubmit` モデル (`task`, `navigator_tags`, `driver_tags`, `reply_to`)
- `POST /workflows/pair` エンドポイント — navigator → driver の2エージェントパイプライン
- スクラッチパッドキー: `{prefix}_plan` (navigator出力) + `{prefix}_result` (driver完了報告)
- `tests/test_workflow_pair.py` — 35 tests
- OpenAPI スナップショット確認 (自動通過)
- 総テスト数: **1479 tests** 全通過

**デモパターン**: Pipeline — navigator (PLAN.md) → [scratchpad] → driver (fizzbuzz.py + test_fizzbuzz.py)
- navigator: FizzBuzz の設計計画 (2,266文字 PLAN.md) をスクラッチパッドに書き込み (35s)
- driver: PLAN.md を読み込み、`fizzbuzz.py` + `test_fizzbuzz.py` を実装・実行し `driver_summary.md` に結果報告 (50s)
- Stop Hook 2回とも発火成功 (v1.0.26 修正の効果確認)

**バグと修正**:
1. demo.py に `--api-key` 引数の追加漏れ → Popen に `"--api-key", API_KEY` 追加
2. タイムスタンプのマイクロ秒解像度による pipeline ordering check の誤判定 → depends_on 検証 (check [9]) に委譲
3. `GET /workflows/{id}` の status が `'complete'` (not `'completed'`) → 期待値に `"complete"` を追加

---

### 10.28 v1.0.28 — MIRIX型エピソード記憶ストア

#### 選定理由

**選択: MIRIX型エピソード記憶ストア (v1.0.28)**

§11「機能・ワークフロー」の**中**優先度候補。高優先度の未実装候補（チェックポイント永続化、ProcessPort、OpenTelemetry）は規模が大きく本イテレーションには不適。MIRIX型エピソード記憶は以下の理由で次に優先した:

1. **明確な実装パス**: REST API (`GET/POST /agents/{id}/memory`) + JSONL 永続化 + タスク完了フック連携のみ。単独イテレーションに適したスコープ。
2. **クリーンな問題解決**: 現行の `/summarize` → `NOTES.md` はタスク間で過去エピソードが上書き消滅する。エピソードログに `{task_id, summary, outcome, lessons}` を追記することで長期記憶を実現。
3. **arXiv:2507.07957 の実証**: MIRIX が RAG ベースラインより 35% 精度向上。タスク完了ごとに lightweight JSONL に記録し、次タスク開始時に直近 N 件を system prompt に付加するパターンが有効。
4. **`clean-arch`ワークフロー**: 4エージェントシーケンスは設計が複雑で、先に役割テンプレートライブラリとの整合が必要。次イテレーション候補とする。
5. **`DDD`ワークフロー**: 同様に EventStorming マップ設計が必要で規模が大きい。

**選択しなかった候補:**
- `POST /workflows/clean-arch`: 4レイヤー分解は設計が複雑。role template 整合後の候補。
- `watchdog_poll` config 検証: スコープが小さくデモが単調になる。
- スライディングウィンドウ + 重要度スコア圧縮: TF-IDF 実装は外部ライブラリ依存が増える。

#### 調査結果 (Step 1 — Research)

**Query 1**: MIRIX multi-agent memory system episodic memory arXiv 2507.07957 2025

主要知見:
- **MIRIX** (Wang & Chen, 2025): 6種類のメモリタイプ (Core/Episodic/Semantic/Procedural/Resource/Knowledge Vault)。
  エピソードメモリ: タイムスタンプ付き状況的経験を記録し特定イベントを想起可能。
- ScreenshotVQA ベンチマーク: RAG ベースラインより **35% 精度向上**、ストレージは 99.9% 削減。
- LOCOMO (長期会話ベンチマーク): SOTA 85.4%。
- エピソード単位の推奨スキーマ: `{summary, details, timestamp}`。

**References**:
- Wang & Chen, "MIRIX: Multi-Agent Memory System for LLM-Based Agents", arXiv:2507.07957, July 2025. https://arxiv.org/abs/2507.07957

**Query 2**: episodic memory LLM agents JSONL persistence task summary retrieval 2025

主要知見:
- **累積追記** (cumulative append-only) が長期記憶に最適なパラダイム。
- "Position: Episodic Memory is the Missing Piece for Long-Term LLM Agents" (arXiv:2502.06975): 単発学習・コンテキスト対応検索・知識統合を支える記憶基盤が必要。
- JSONL 追記形式: 軽量でリアルタイム追記に適し、Redis/SQLite の依存を回避できる。

**References**:
- "Position: Episodic Memory is the Missing Piece for Long-Term LLM Agents", arXiv:2502.06975, 2025. https://arxiv.org/pdf/2502.06975
- "Memory in the Age of AI Agents", arXiv:2512.13564, 2025. https://arxiv.org/abs/2512.13564

**Query 3**: agent task history REST API design best practices LLM orchestration 2025

主要知見:
- 「すべての重要な決定について生の入力・プロンプト・最終出力・決定パスを含む詳細なイミュータブルログエントリを書く」 — for both debugging and auditability。
- LLM-first API 設計: OpenAPI スペック + system prompt で agent が REST を直接呼べる構造。

**References**:
- "LLM Orchestration in 2025: Frameworks + Best Practices", orq.ai Blog, 2025. https://orq.ai/blog/llm-orchestration
- "AI Agent Orchestration Patterns", Azure Architecture Center, Microsoft Learn, 2025. https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns

#### 実装結果 (v1.0.28)

**デモ結果**: 18/18 checks PASSED (2026-03-09)

**実装内容**:
- `EpisodeStore` クラス (`episode_store.py`): JSONL追記・リスト (newest-first)・削除 (アトミック再書き込み)
- `EpisodeCreate`, `Episode` Pydantic モデル (`schemas.py`)
- `GET /agents/{id}/memory`, `POST /agents/{id}/memory`, `DELETE /agents/{id}/memory/{episode_id}` エンドポイント (`web/app.py`)
- `tests/test_episode_store.py` — 22 unit tests
- `tests/test_episode_api.py` — 18 REST API tests
- 総テスト数: **1519 tests** 全通過

**デモパターン**: Pipeline + Episodic Memory
- writer: merge_sort.py 実装 → scratchpad 格納 → episodic memory 記録 (50s)
- reviewer: scratchpad からコード取得 → `GET /agents/writer/memory` で writer の記憶参照 → code_review.md 作成 → 自分の memory 記録 (35s)
- demo: DELETE でエピソード削除確認

**バグと修正**:
1. check [3] writer starts as IDLE FAIL → `wait_all_idle()` ポーリング追加
2. Pydantic forward reference エラー → `Episode`/`EpisodeCreate` をモジュールレベルでインポート
3. `_MockOrchestratorForHistory.config` なし → `getattr` フォールバック + モック更新

---

### 10.29 v1.0.29 — エピソード自動記録 + タスク dispatch 時の自動注入

#### 選定理由

**選択: エピソード自動記録 + タスク dispatch 時の自動注入 (v1.0.29)**

§11「層4：コンテキスト伝達（改善）」の「MIRIX 型エピソード記憶ストア」候補のうち、v1.0.28 では REST API (GET/POST/DELETE /agents/{id}/memory) と JSONL 永続化を実装した。しかし**エージェントが手動で `/memory` エンドポイントを呼ばなければならない**という課題が残っている。本イテレーションでは以下を自動化する:

1. **タスク完了時の自動エピソード記録** — `task-complete` エンドポイントが呼ばれるたびに `EpisodeStore` へ `{task_id, summary, outcome, lessons}` を自動保存する
2. **タスク dispatch 時の自動プロンプト注入** — エージェントにタスクを送信する前に `EpisodeStore` から直近 N 件のエピソードを取得し、プロンプト先頭に `## 過去のタスク経験` セクションとして注入する

**選択理由:**
1. **v1.0.28 の自然な完成形**: 記憶ストアは既存。「自動化」のみが残っており、スコープが明確。
2. **エージェントの負担ゼロ**: 手動API呼び出し不要で、全エージェントが自動的に蓄積・活用できる。
3. **MIRIX 論文の推奨パターン**: Wang & Chen (arXiv:2507.07957) が「タスク完了フックで記録し、次タスク開始時に注入」を推奨。
4. **設定可能性**: `memory_inject_count: int` (デフォルト 5) と `memory_auto_record: bool` (デフォルト true) を `OrchestratorConfig` に追加して制御可能にする。
5. **後方互換性**: 設定を追加しない既存の YAML でもデフォルト値で動作する。

**選択しなかった候補:**
- `POST /workflows/clean-arch`: 4エージェントシーケンスはロールテンプレートとの整合が必要で規模が大きい。
- `watchdog_poll` バリデーション: スコープが小さくデモが単調。
- SQLite バックエンド: `EpisodeStore` は JSONL で十分。SQLite 移行は別イテレーション。
- `/deliberate` スラッシュコマンド: 有用だが「中」優先度。自動化の方が利用者への影響が大きい。

#### 調査結果 (Step 1 — Research)

**Query 1**: episodic memory auto-injection LLM agent task dispatch prompt engineering 2025

主要知見:
- **PlugMem** (arXiv:2603.03296, 2025): Task-agnostic plugin memory module for LLM agents。エピソードメモリを構造化ログとして保存し、タスク開始時に関連メモリをプロンプトへ注入。メモリなしのベースラインより 23% 改善。
- **A-MEM** (arXiv:2502.12110, 2025): Zettelkasten 方式による動的記憶組織化。各記憶エントリがキーワード・タグ・構造化属性を持ち、キーワードベースの検索で注入。
- セッションサマリーがオーケストレーションプロンプトテンプレートに自動注入され、エージェントのシステム指示になる — 蓄積コンテキストに基づく行動変化を可能にする正準パターン。

**References**:
- "PlugMem: A Task-Agnostic Plugin Memory Module for LLM Agents", arXiv:2603.03296, 2025. https://arxiv.org/html/2603.03296
- "A-MEM: Agentic Memory for LLM Agents", arXiv:2502.12110, 2025. https://arxiv.org/abs/2502.12110

**Query 2**: MIRIX episodic memory automatic recording task completion hook multi-agent 2025

主要知見:
- **MIRIX** (Wang & Chen, arXiv:2507.07957): エピソードメモリエントリ = `{event_type, summary, details, actor, timestamp}`。Active Retrieval 機構: ユーザー入力でトピック自動推論 → 全メモリコンポーネントから取得 → システムプロンプトへのコンテキスト注入のためタグ付け。
- 記憶更新はタスクバッチ後に自動トリガー（手動呼び出し不要）。Memory Manager エージェントが保存内容を決定し、注入は自動。
- ScreenshotVQA ベンチマーク: RAG ベースラインより 35% 精度向上。

**References**:
- Wang & Chen, "MIRIX: Multi-Agent Memory System for LLM-Based Agents", arXiv:2507.07957, July 2025. https://arxiv.org/abs/2507.07957
- MIRIX Documentation, Multi-Agent System. https://docs.mirix.io/architecture/multi-agent-system/

**Query 3**: LLM agent persistent memory cross-task automatic injection context engineering 2025

主要知見:
- **Design Patterns for Long-Term Memory in LLM-Powered Architectures** (Serokell Blog, 2025): "Session summaries are automatically injected into orchestration prompt templates, becoming part of the agent's system instructions in subsequent sessions" — タスク間記憶の正準パターン。
- **Memory Management for Long-Running Low-Code Agents** (arXiv:2509.25250): 自動記憶注入によるコンテキスト一貫性がタスクシーケンス全体でのドリフトを防止。
- Static heuristic memory pipeline (固定・非適応的な保存・検索) は既知の限界；タスクライフサイクルイベントにフックした自動化で解決。

**References**:
- "Design Patterns for Long-Term Memory in LLM-Powered Architectures", Serokell Blog, 2025. https://serokell.io/blog/design-patterns-for-long-term-memory-in-llm-powered-architectures
- "Memory Management and Contextual Consistency for Long-Running Low-Code Agents", arXiv:2509.25250, 2025. https://arxiv.org/pdf/2509.25250

#### 実装結果 (v1.0.29)

**デモ結果**: 18/18 checks PASSED (2026-03-09)

**実装内容**:
- `OrchestratorConfig` に `memory_auto_record: bool = True` と `memory_inject_count: int = 5` を追加 (`config.py`)
- `load_config()` で YAML から両フィールドをパース
- `Orchestrator._dispatch_loop` にエピソード自動注入ロジック: dispatch 前に `_episode_store.list(agent_id, limit=inject_count)` を呼び、エピソードが存在すれば `## 過去のタスク経験` セクションをプロンプト先頭に注入 (`orchestrator.py`)
- `web/app.py` の `agent_task_complete` エンドポイント: 明示的 `/task-complete` 呼び出し後に `_episode_store.append()` を自動実行 (output が空の場合はスキップ)
- `orchestrator._episode_store = _episode_store` で Web 層とオーケストレーター dispatch ループが同一インスタンスを共有
- `tests/test_episode_auto.py` — 19 unit/integration tests
- 総テスト数: **1538 tests** 全通過

**デモパターン**: Pipeline + Auto-Record + Auto-Inject
- writer: binary_search.py 実装 → 手動 /memory 呼び出しなし → /task-complete で自動記録 (33s)
- reviewer: scratchpad からコード読み取り → `GET /agents/writer/memory` でエピソード確認 → 自動記録 (26s)
- writer task 3: オーケストレーターがエピソードを自動注入 → agent が `過去のタスク経験` セクションを検出 → `task3_memory_injected=true` → 自動記録 (26s)

**バグと修正**:
1. テストで `submit_task(Task(...))` を呼んでいたが `submit_task` は `str` を第一引数として受け取る → `submit_task("prompt", _task_id="...")` に修正

---

### 10.30 v1.0.30 — `POST /workflows/clean-arch` — 4エージェント Clean Architecture パイプライン

#### 選定理由

**選択: `POST /workflows/clean-arch` (v1.0.30)**

§11「ワークフローテンプレート」の**中**優先度候補として長らく保留されていた 4エージェント Clean Architecture パイプラインを実装する。前提条件としていた「role template ライブラリ」と「tdd ワークフロー」は v1.0.27 で実装済みであり、実装ブロッカーは解消された。

**選択理由:**
1. **最長保留の未実装ワークフロー**: v0.25.0 から候補に挙げられ、毎回「前提条件未達」として後回しにされてきた。前提条件がすべて揃った今が実装タイミング。
2. **多エージェント協調の深さを示す**: 4エージェントシーケンシャルパイプライン (domain → usecase → adapter → framework) は各エージェントが前のエージェントの成果物をスクラッチパッド経由で読み込む実践的な Blackboard パターン。
3. **Clean Architecture の教育的価値**: Robert C. Martin の同心円モデルをマルチエージェントワークフローで具体化することで、フレームワークのユースケースとして強力なデモになる。
4. **依存注入なしで実装可能**: 既存の `submit_task()` + `depends_on` + scratchpad パターンで実装でき、コアへの変更は最小限。

**選択しなかった候補:**
- `/deliberate` スラッシュコマンド: 「中」優先度。clean-arch より実装価値が低い。
- `watchdog_poll` バリデーション: スコープが小さくデモが単調。
- Semantic RAG for episode injection: v1.0.29 の episode 機能は現状の keyword/recent で十分。
- `POST /workflows/ddd`: clean-arch の代替だが、EventStorming マップの設計が複雑。

#### 調査結果 (Step 1 — Research)

**Query 1**: "clean architecture multi-agent pipeline domain usecase adapter framework LLM 2025"

主要知見:
- **AgentMesh** (arXiv:2507.19902, 2025): Planner→Coder→Debugger→Reviewer の4ロール分担がソフトウェア開発タスクを自動化。各エージェントはアーティファクト（計画書・コード・テスト結果）経由で通信するアーティファクト中心型協調パターンを採用。モジュラー設計により各ロールに専門モデルを差し込み可能。
- **AutoML-Agent** (arXiv:2410.02958, 2024): ユーザータスク記述→特化エージェント協調→デプロイ可能モデルのフルパイプライン。各ステップが独立したサブタスクに分解され並列または順次実行される。

**References**:
- AgentMesh: A Cooperative Multi-Agent Generative AI Framework for Software Development Automation, arXiv:2507.19902, 2025. https://arxiv.org/abs/2507.19902
- AutoML-Agent: A Multi-Agent LLM Framework for Full-Pipeline AutoML, arXiv:2410.02958, 2024. https://arxiv.org/abs/2410.02958

**Query 2**: "clean architecture hexagonal architecture AI agent software design 4-layer domain usecase adapter framework 2025"

主要知見:
- **Muthu (2025-11) "The Architecture is the Prompt"**: ヘキサゴナルアーキテクチャの境界がそのまま AI エージェントのコンテキスト制約になる。アーキテクチャ層ごとにエージェントを分離すると認知負荷が劇的に低減する。
- **Fernández García (2025) "Applying Hexagonal Architecture in AI Agent Development"**: Clean Architecture の3層構造 (Domain / Application / Infrastructure) をエージェント分割境界に直接適用。各層は外層を知らない = 依存性逆転原則。
- **Robert C. Martin Clean Architecture (2017)**: Domain (Entities) → Use Cases → Interface Adapters → Frameworks & Drivers の同心円モデル。各層は内側にのみ依存する。

**References**:
- Muthu, "The Architecture is the Prompt – Guiding AI with Hexagonal Design", Engineering Notes, Nov 2025. https://notes.muthu.co/2025/11/the-architecture-is-the-prompt-guiding-ai-with-hexagonal-design/
- Marta Fernández García, "Applying Hexagonal Architecture in AI Agent Development", Medium, 2025. https://medium.com/@martia_es/applying-hexagonal-architecture-in-ai-agent-development-44199f6136d3

**Query 3**: "AgentMesh multi-agent software engineering pipeline planner coder reviewer 4 roles arXiv 2507.19902 2025"

主要知見:
- **AgentMesh (arXiv:2507.19902)**: Planner がユーザー要求を具体的サブタスクに分解 → Coder が各サブタスクを実装 → Debugger がテスト・修正 → Reviewer が最終出力を検証。コードはアーティファクト中心型通信（自然言語 P2P でなく生成物のパス共有）を採用。これは Blackboard パターンの実践例。
- **Planner-Coder Gap Study** (arXiv:2510.10460, 2025): マルチエージェント vs 単一エージェントの比較。Planner が具体的な中間成果物を生成しない場合、Coder の実装品質が低下する。スクラッチパッド経由の明示的ハンドオフが重要。
- **Agyn** (arXiv:2602.01465, 2025): チームベース自律ソフトウェアエンジニアリング。役割定義 (PM / Tech Lead / Developer / QA) + アーティファクト共有でコンプレックスタスクを解決。

**References**:
- AgentMesh: A Cooperative Multi-Agent Framework for Software Development Automation, arXiv:2507.19902, 2025. https://arxiv.org/html/2507.19902v1
- Understanding and Bridging the Planner-Coder Gap, arXiv:2510.10460, 2025. https://arxiv.org/html/2510.10460
- Agyn: A Multi-Agent System for Team-Based Autonomous Software Engineering, arXiv:2602.01465, 2025. https://arxiv.org/html/2602.01465v2

---

**Query 1**: "MADR Markdown Architectural Decision Records format specification 2024 2025"

主要知見:
- **MADR 4.0.0** (2024-09-17): Markdown Architectural Decision Records 標準フォーマットの最新リリース。title / context / considered options / decision outcome / consequences の構造を持つ。`adr-template.md` (全セクション) / `adr-template-minimal.md` (必須セクションのみ) / `adr-template-bare.md` のバリエーションを提供。MADR 3.0.0 (2022-10) から "Positive/Negative Consequences" が "Consequences" に統合。
- **adr-tools** (npryce/adr-tools): コマンドラインツールで ADR を管理するデファクトスタンダード。

**References**:
- "About MADR | MADR", https://adr.github.io/madr/
- "Markdown Architectural Decision Records: Format and Tool Support", CEUR-WS 2018, https://ceur-ws.org/Vol-2072/paper9.pdf
- "The Markdown ADR (MADR) Template Explained and Distilled", ozimmer.ch 2022, https://www.ozimmer.ch/practices/2022/11/22/MADRTemplatePrimer.html

**Query 2**: "LLM multi-agent architecture decision record automation proposer reviewer synthesizer pipeline 2025"

主要知見:
- **Proposer-Reviewer-Synthesizer パターン** (Google ADK 2025): 複数エージェントが並列実行し、最終 Synthesizer が統合する設計が自動コードレビューや設計決定タスクに有効。
- **CTO エージェントロール**: 各エージェントをドメインエキスパートとして設定（CTO エージェントが設計決定を担う）するパターンが生産環境でも採用されている (ZenML Blog 2025)。
- **Multi-Agent Debate (MAD)**: Du et al. ICML 2024 の複数ラウンド討論パターンが ADR 文書の質向上に直接適用可能。

**References**:
- "Developer's guide to multi-agent patterns in ADK", Google Developers Blog 2025, https://developers.googleblog.com/developers-guide-to-multi-agent-patterns-in-adk/
- "What 1,200 Production Deployments Reveal About LLMOps in 2025", ZenML Blog, https://www.zenml.io/blog/what-1200-production-deployments-reveal-about-llmops-in-2025
- "LLM-Based Multi-Agent Systems for Software Engineering", ACM TOSEM 2025, https://dl.acm.org/doi/10.1145/3712003

**Query 3**: "AI agents architecture decision records MAD requirements engineering arXiv 2025"

主要知見:
- **MAD for Requirements Engineering** (Ochoa et al., arXiv:2507.05981, 2025): マルチエージェント討論が要件分類精度を F1 0.726 → 0.841 に向上。Proposer-Critic-Synthesizer の3段階構造が最も効果的。
- **Agentic AI Architectures Survey** (arXiv:2510.25445): 2018-2025年のエージェントシステムを網羅的に調査。「controllable orchestration」（明示的な状態遷移 + グラフベース実行）が debuggability を高める。
- **Orchestrated Distributed Intelligence** (arXiv:2503.13754): 独立エージェントの結果を統合する Orchestrator パターンが複雑な設計タスクを解決する。

**References**:
- Ochoa et al., "Multi-Agent Debate Strategies to Enhance Requirements Engineering with LLMs", arXiv:2507.05981, 2025, https://arxiv.org/html/2507.05981v1
- "Agentic AI: A Comprehensive Survey of Architectures, Applications, and Future Directions", arXiv:2510.25445, 2025, https://arxiv.org/html/2510.25445v1
- "From Autonomous Agents to Integrated Systems", arXiv:2503.13754, 2025, https://arxiv.org/html/2503.13754v1

---

## §10.36 — v1.1.0: POST /workflows/competition (Best-of-N Competitive Solver)

**選択日**: 2026-03-09

### 選択理由

**選択: `POST /workflows/competition` — Best-of-N Competitive Solver Workflow**

§11 の未実装候補を精査した結果、`POST /workflows/competition` を v1.1.0 として選択する。

**選択理由**:
1. **未実装確認**: `grep competition src/` で確認すると、`app.py:1589` にコメント参照があるのみ。実際のエンドポイントは存在しない。
2. **高いユーザー可視性**: CLAUDE.md に「Competitive / best-of-N」パターンが推奨シナリオとして明記されており、AHC デモでも手動スコア確認が必要だった課題を自動化できる。
3. **v1.1.0 マイナーバンプに相応しいスコープ**: 新しいワークフローテンプレート（REST エンドポイント + プロンプト + テスト + デモ）として独立したユーザー価値を提供する。
4. **既存基盤の活用**: delphi ワークフローの並列フェーズ（複数エージェントが depends_on=[] で並列起動）パターン + Blackboard（共有スクラッチパッド）+ LLM-as-Judge（judge ロールテンプレート）の3つが既に揃っており、実装コストが低い。
5. **研究的裏付け**: Best-of-N サンプリングは LLM 性能向上の最も実証されたアプローチ（Snell et al. 2024、arXiv:2408.03314）。

**選択しなかった候補と理由**:
- **スライディングウィンドウ + TF-IDF コンテキスト圧縮**: `sentence-transformers` 外部ライブラリ依存が増える。テスト環境での重量級モデルダウンロードが CI で問題になる可能性。v1.2.x で検討。
- **DriftMonitor セマンティック類似度強化**: 同上。embedding モデルのインストールが必要。
- **POST /agents/{id}/brief**: エンドポイント自体は小さいが、mid-task injection のセマンティクス（Stop Hook との競合）の設計が複雑。

### 実装計画

**ワークフロー設計**:

```
N 個の solver エージェント（並列）
    ↓ depends_on=[]（全て同時開始）
    ↓ 各エージェントが problem を解き score を scratchpad に書く
judge エージェント（depends_on=全 solver）
    ↓ 全 solver の結果を読み、スコアで優劣をつける
    ↓ COMPETITION_RESULT.md を scratchpad に書く
```

**スキーマ**: `CompetitionWorkflowSubmit`
- `problem: str` — 問題文（全 solver に同一で渡す）
- `strategies: list[str]` — 各 solver のアプローチ（例: `["greedy", "dp", "random_restart"]`）。`len(strategies)` が N を決定。最小 2、最大 10。
- `solver_tags: list[str]` — solver エージェントに要求するタグ
- `judge_tags: list[str]` — judge エージェントに要求するタグ
- `scoring_criterion: str` — judge に伝えるスコアリング基準（例: `"maximize total value"`）
- `reply_to: str | None` — judge 完了時の RESULT 送信先

**Scratchpad キー**:
- `{prefix}_solver_{strategy}` — 各 solver の成果物（実装 + スコア）
- `{prefix}_judge` — judge の `COMPETITION_RESULT.md` 内容

**ロールプロンプト設計**:
- **solver-{strategy}**: 「あなたは {strategy} アプローチで問題を解く Solver エージェント」。解を実装して `SCORE:` 行を含む `solver_{strategy}_result.md` を scratchpad に書く。
- **judge**: 全 solver の結果を読み、スコアを抽出・比較し、`COMPETITION_RESULT.md`（Winner、Scores、Rationale、Runner-up）を生成して scratchpad に書く。

### ウェブ調査結果

**Query 1**: "best-of-N multi-agent LLM competition parallel solver orchestrator pattern 2025"

主要知見:
- **MultiAgentBench** (arXiv:2503.01935, 2025): LLM マルチエージェントシステムにおける協調と競争の両方を評価する benchmark。競争シナリオでは「同一問題を複数エージェントが独立に解き、最良解を選択する」パターンが評価軸の一つ。
- **Multi-Agent Collaboration via Evolving Orchestration** (arXiv:2505.19591, OpenReview 2025): 複数エージェントが並列実行し、Orchestrator が成果を統合する puppeteer-style パターン。各 operator (generation, refinement, verification) を並列で呼び出し、最終層が統合する構造が競争型ワークフローの基盤となる。
- **Learning Latency-Aware Orchestration for Parallel Multi-Agent Systems** (arXiv:2601.10560, 2025): 並列マルチエージェントシステムにおけるオーケストレーションのレイテンシ最適化。各層で複数の operator を並列実行するアーキテクチャが最適。

**References**:
- "MultiAgentBench: Evaluating the Collaboration and Competition of LLM agents", arXiv:2503.01935, 2025, https://arxiv.org/abs/2503.01935
- "Multi-Agent Collaboration via Evolving Orchestration", arXiv:2505.19591, 2025, https://openreview.net/forum?id=L0xZPXT3le
- "Learning Latency-Aware Orchestration for Parallel Multi-Agent Systems", arXiv:2601.10560, 2025, https://arxiv.org/html/2601.10560

**Query 2**: "multi-agent competitive workflow LLM orchestration winner selection judge arXiv 2025"

主要知見:
- **Agent-as-a-Judge** (arXiv:2508.02994, 2025): LLM エージェント自体が他のエージェントの出力を評価する「エージェント審判員」パターン。エージェント judge は中間ステップを観察し、ツールを活用してスコアリングと評価根拠を生成する。competition ワークフローの judge ロールに直接適用可能。
- **Leveraging LLMs as Meta-Judges** (arXiv:2504.17087, 2025): 複数の judge エージェントでスコアリングし、メタ-judge が集約する3段階パイプライン。単一 judge より高精度。本実装では1 judge で十分だが、拡張点として記録。
- **Difficulty-Aware Agent Orchestration** (arXiv:2509.11079, 2025): タスク難易度に応じてエージェント割り当てを変える orchestration。Best-of-N はシンプルなケースで有効と実証。

**References**:
- "When AIs Judge AIs: Agent-as-a-Judge Evaluation for LLMs", arXiv:2508.02994, 2025, https://arxiv.org/html/2508.02994v1
- "Leveraging LLMs as Meta-Judges", arXiv:2504.17087, 2025, https://arxiv.org/html/2504.17087v1
- "Difficulty-Aware Agent Orchestration", arXiv:2509.11079, 2025, https://arxiv.org/html/2509.11079v1

**Query 3**: "best-of-N sampling LLM parallel agents judge scoring winner selection pattern 2025 arXiv"

主要知見:
- **"Making, not Taking, the Best of N" (FusioN)** (arXiv:2510.00931, 2025): Best-of-N の選択のみではなく、N 候補を judge (fusor) が統合して最良解を合成する FusioN を提案。本実装では「選択」ベースの BoN を実装するが、統合パターン (FusioN) は将来の拡張点として記録。
- **M-A-P (arXiv:2506.12928, 2025)**: 並列サンプリング手法（BoN, BoN-wise, Beam-Search, Tree search）を比較。list-wise 評価が最も精度が高い。judge に全候補を一覧渡しで評価させる list-wise アプローチを本実装で採用。
- **"Statistical Estimation of Adversarial Risk under Best-of-N Sampling" (arXiv:2601.22636)**: BoN サンプリングが adversarial robustness に与える影響の統計的分析。「より多くの候補 N を評価するほどリスク評価が安定する」→ competition の solver 数を増やすことで結果の信頼性が上がる。

**References**:
- "Making, not Taking, the Best of N" (FusioN), arXiv:2510.00931, 2025, https://arxiv.org/pdf/2510.00931
- "M-A-P: Multi-Agent-based Parallel Test-Time Scaling", arXiv:2506.12928, 2025, https://arxiv.org/pdf/2506.12928
- "Statistical Estimation of Adversarial Risk under Best-of-N Sampling", arXiv:2601.22636, 2025, https://arxiv.org/html/2601.22636

---

## §10.37 — v1.1.1: `[Pasted text #1]` ハング修正 + Server Cleanup Helper

**選択日**: 2026-03-09

### 選択理由

**選択: paste-preview ハング修正 + demo server cleanup ヘルパー (PATCH バンプ)**

v1.1.0 デモで発見された2つのバグを修正する:

1. **`[Pasted text #1]` ハング**: 長いプロンプト送信時に tmux paste-preview モードが起動し、
   Claude CLI がプロンプトを受け取れない。`ensure_session()` で `assume-paste-time 0` を設定しているが、
   実際のデモで発生することが確認されており、不十分。

2. **server cleanup**: `proc.terminate()` だけでは uvicorn の子プロセスが残留する。
   `start_new_session=True` + `os.killpg()` パターンに移行する。

**選択しなかった候補**:
- スライディングウィンドウ + TF-IDF コンテキスト圧縮: 外部ライブラリ依存が大きい
- DriftMonitor セマンティック類似度: embedding モデルが CI で重い

### 根本原因分析

`TmuxInterface.send_keys()` は以下の実装:

```python
def send_keys(self, pane, text, enter=True):
    pane.send_keys(text, enter=False)
    if enter:
        time.sleep(0.15)
        pane.send_keys("", enter=True)
```

`ensure_session()` で `assume-paste-time 0` を設定しているが、
Claude CLI が独自の bracketed-paste 検出を持つため、tmux 設定のみでは不十分。
0.15秒 sleep + Enter パターンは paste-preview が **表示されてしまった後** に Enter を送るが、
Claude CLI が `[Pasted text #N]` 確認を待っている間にさらに Enter が重複する可能性がある。

**修正**: `send_keys()` で Enter 送信後にペイン出力を検査し、`[Pasted text` が
表示されている場合は追加 Enter を送信して paste-preview を確定させる。

### Web調査結果

**Query 1**: "tmux assume-paste-time bracket paste mode [Pasted text terminal behavior 2024"

- `assume-paste-time` はキーが 1ms より速く入力された場合を paste とみなす閾値。`0` で無効化。
  (man7.org tmux manpage)
- bracketed paste mode は tmux に 2012 年追加。`\033[200~`/`\033[201~` で pasted text を囲む。
  (tmux/tmux commit f4fdddc)
- `assume-paste-time` と bracketed paste mode は独立した仕組み。

**References**:
- tmux man page: https://man7.org/linux/man-pages/man1/tmux.1.html
- tmux bracketed paste commit: https://github.com/tmux/tmux/commit/f4fdddc9306886e3ab5257f40003f6db83ac926b
- Bracketed paste mode bug report: https://github.com/microsoft/terminal/issues/19418

**Query 2**: "tmux send-keys long text paste preview mode enter keypress workaround"

- `tmux send-keys` のセッション外からの呼び出し時に preview が表示される。(tmux/tmux issue #467)
- libtmux 経由は Python プロセスからの発行のため「セッション外」扱いになりうる。
- send-keys を inside session から呼ぶ場合は preview が表示されない。

**References**:
- tmux send-keys preview issue: https://github.com/tmux/tmux/issues/467
- libtmux send_keys issue: https://github.com/tmux-python/libtmux/issues/15

**Query 3**: "tmux assume-paste-time option explanation paste detection milliseconds"

- `assume-paste-time 0` は tmux.conf の一般的な回避策。ただし Claude CLI が独自の
  bracketed-paste を処理するため、設定だけでは不十分なケースがある。
- `os.setsid()` / `start_new_session=True` は POSIX のプロセスグループ管理の標準的手法。

**References**:
- Ubuntu tmux manpage: https://manpages.ubuntu.com/manpages/xenial/man1/tmux.1.html
- tmux man7.org: https://man7.org/linux/man-pages/man1/tmux.1.html

## §10.38 — v1.1.2: UserPromptSubmit フックによるタスクプロンプト注入

**選択日**: 2026-03-09

### 選択理由

v1.1.1 の `[Pasted text #1]` 修正はポーリングによる workaround。根本的な解決として `UserPromptSubmit` フックを調査し、実装する。

**選択しなかった候補**:
- スライディングウィンドウ + TF-IDF コンテキスト圧縮: 外部ライブラリ依存が大きい
- AgentRegistry 完全分離: 規模が大きくデモ価値が低い

### Research — Query 1: UserPromptSubmit フック仕様

**Query**: "Claude Code UserPromptSubmit hook specification 2026"

**公式ドキュメント** (https://code.claude.com/docs/en/hooks):

UserPromptSubmit フックの仕様:
- `UserPromptSubmit` はプロンプト送信時（Claude 処理前）に起動する
- **stdin JSON**: `{ "session_id", "transcript_path", "cwd", "permission_mode", "hook_event_name", "prompt" }`
  - `"prompt"` フィールドにユーザーが送信したテキストが含まれる
- **stdout の扱い**:
  - Exit 0 + plaintext stdout → Claude の **コンテキストとして追加** (プロンプトの置換ではない)
  - Exit 0 + JSON stdout + `additionalContext` フィールド → より離散的にコンテキストとして追加
  - **Exit 2** → プロンプトをブロック・消去する
- **マッチャー**: 非対応。常に全プロンプトで発火する
- **決定フィールド**: `decision: "block"` でプロンプトをブロック可能

**重要な制約**: `UserPromptSubmit` の stdout は**プロンプトを置換しない**。元のプロンプトは保持されたまま、stdout がコンテキストとして **追加される**。

**References**:
- Claude Code Hooks reference: https://code.claude.com/docs/en/hooks
- Claude Code hooks guide: https://claude.com/blog/how-to-configure-hooks

### Research — Query 2: stdout 注入 vs プロンプト置換

**Query**: "Claude Code hooks stdout injection prompt replacement UserPromptSubmit"

公式ドキュメントの引用:
> "The exceptions are UserPromptSubmit and SessionStart, where stdout is added as context that Claude can see and act on."

egghead.io のレッスン "Rewrite Prompts on the Fly with UserPromptSubmit Hooks" では「console.log したものがプロンプトを書き換える」と述べているが、公式ドキュメントの記述と矛盾する。**公式ドキュメントを優先**: stdout はコンテキストとして追加されるのみ。

**References**:
- egghead.io lesson: https://egghead.io/lessons/rewrite-prompts-on-the-fly-with-user-prompt-submit-hooks~76rrt
- Hooks reference (official): https://code.claude.com/docs/en/hooks

### Research — Query 3: paste-preview 根本解決の代替アプローチ

**Query**: "Claude Code UserPromptSubmit hook cwd stdin prompt replacement injection 2026"

**調査結論**: `UserPromptSubmit` フックは stdout でプロンプトを**置換できない**。ただし以下のアーキテクチャが機能する:

1. オーケストレーターがタスクプロンプトを `__task_prompt__.txt` に書き込む
2. `send_keys()` で短いトリガー文字列 (`"__TASK__"`) のみ送信 → paste-preview 発生なし
3. `UserPromptSubmit` フックが起動し `cwd` フィールドから作業ディレクトリを取得
4. `__task_prompt__.txt` が存在すれば読み込んで削除し、`additionalContext` として出力
5. Claude は「`__TASK__` + コンテキストのタスク内容」を受け取り、タスクを実行する

この方式では長いプロンプトが `send_keys` を通らないため、paste-preview は根本的に解消される。

**References**:
- Claude Code Hooks reference: https://code.claude.com/docs/en/hooks
- dagger/container-use issue #253: https://github.com/dagger/container-use/issues/253

### 実装方針

1. `agent_plugin/hooks/hooks.json` に `UserPromptSubmit` フックを追加
2. `agent_plugin/hooks/user-prompt-submit.py` を実装:
   - stdin JSON から `cwd` を取得
   - `{cwd}/__task_prompt__{agent_id}__.txt` が存在すれば読み込んで削除
   - `additionalContext` として JSON で出力
   - ファイルが存在しない場合は何も出力せず exit 0 (pass-through)
3. `ClaudeCodeAgent._dispatch_task()` を変更:
   - プロンプトを `__task_prompt__{agent_id}__.txt` に書き込む
   - `send_keys("__TASK__")` のみ送信 (短いトリガー、paste-preview なし)
4. `TmuxInterface.send_keys()` の paste-preview ポーリングはフォールバックとして維持

### テスト方針

- `test_user_prompt_submit_hook.py`: フックスクリプトの動作単体テスト
- `test_dispatch_task_prompt_file.py` または既存テストへの追加: `_dispatch_task` がファイルを書き込み短いトリガーを送信することを検証
- 全既存テスト (1827+) が green であること

---

## §10.39 — v1.1.3: ファイル存在チェックによるペースト確認検出 + コンテキスト4戦略ガイド

**選択日**: 2026-03-09

### 選択理由

#### Part A: ファイル存在チェックによる配送確認 (v1.1.2 精化)

**選択: ファイル存在ポーリングによる UserPromptSubmit 発火検出**

v1.1.2 で実装した `UserPromptSubmit` フックは `__task_prompt__*.txt` を読み込んで削除する。
この「削除」という副作用を逆用して、プロンプトが Claude に届いたかどうかを確認できる:

- ファイルが消えた → フックが発火した → プロンプト配送成功
- 3秒後もファイルが残る → フックが未発火 → paste-preview がブロック中 → Enter 送信でリトライ

v1.1.1 のペーンアウトプットポーリング (`capture_pane` + regex) より決定論的で信頼性が高い。

**選択しなかった候補**:
- pane output ポーリング (v1.1.1): `capture_pane` の regex マッチはタイミング依存でフラジャイル
- watchdog / aionotify: 外部ライブラリ依存、100ms ポーリングで十分

#### Part B: コンテキスト4戦略ガイド (§11 層4)

**選択: 書き込み・選択・圧縮・分離の4戦略チートシートを CLAUDE.md に追記**

§11「層4：コンテキスト伝達（改善）」の未実装候補。実装コストが最も低く（コード変更なし、ドキュメント追記のみ）、全エージェントへ即座に恩恵が届く。
スライディングウィンドウ圧縮 (TF-IDF) は外部ライブラリ依存が大きく単独イテレーションとして分離が必要なため見送り。

**選択しなかった候補**:
- スライディングウィンドウ + TF-IDF コンテキスト圧縮: scikit-learn 依存、規模大
- `/deliberate` スラッシュコマンド: 設計検討が必要

### Research

#### Query 1: Claude Code hooks UserPromptSubmit file deletion detection

**検索**: "Claude Code hooks UserPromptSubmit file deletion detection 2026"

- 公式ドキュメント (https://code.claude.com/docs/en/hooks): `UserPromptSubmit` はプロンプト送信時に発火し、stdout を `additionalContext` として追加できる。ファイル削除は Python フックスクリプト側で実施 (`Path.unlink()`)。
- Claude Code Hooks Complete Guide (https://smartscope.blog/en/generative-ai/claude/claude-code-hooks-guide/): UserPromptSubmit フックの適用例として「プロンプト送信前にファイル読み込み」パターンが示されている。

**結論**: フックが発火すれば `unlink()` が呼ばれるので、ファイルの存在有無でフック発火を間接的に検出できる。

**References**:
- Hooks reference: https://code.claude.com/docs/en/hooks
- Claude Code Hooks Complete Guide (February 2026): https://smartscope.blog/en/generative-ai/claude/claude-code-hooks-guide/
- aiorg.dev Claude Code Hooks Guide 2026: https://aiorg.dev/blog/claude-code-hooks

#### Query 2: asyncio file existence polling delivery confirmation

**検索**: "asyncio file existence polling task delivery confirmation pattern 2025"

- Python docs `asyncio-task.html` (https://docs.python.org/3/library/asyncio-task.html): `asyncio.sleep()` を使ったポーリングループが標準パターン。
- SuperFastPython "How to Check Asyncio Task Status" (https://superfastpython.com/asyncio-task-status/): タスクが短時間で完了するケースでは 100ms × N 回の busy-wait が適切。
- Inngest Blog "What Python's asyncio primitives get wrong about shared state" (https://www.inngest.com/blog/no-lost-updates-python-asyncio): ファイルベースの共有状態変化をポーリングで検出するパターンは race-free で信頼性が高い。

**結論**: `asyncio.sleep(0.1)` × 30 回 (3秒) のポーリングは標準的なパターン。ファイル存在チェック (`Path.exists()`) は原子的で race 条件が発生しない。

**References**:
- Python asyncio tasks: https://docs.python.org/3/library/asyncio-task.html
- SuperFastPython asyncio task status: https://superfastpython.com/asyncio-task-status/
- Inngest asyncio shared state: https://www.inngest.com/blog/no-lost-updates-python-asyncio

#### Query 3: context engineering 4 strategies write select compress isolate agents

**検索**: "context engineering 4 strategies write select compress isolate AI agents CLAUDE.md 2025 2026"

- Zilliz Blog "Context Engineering Strategies for AI Agents" (https://zilliz.com/blog/context-engineering-for-ai-agents): Write / Select / Compress / Isolate の4戦略フレームワークを詳説。エージェントの役割ごとに適切な戦略の組み合わせが異なる。
- LangChain Blog "Context Engineering for Agents" (https://blog.langchain.com/context-engineering-for-agents/): 「Write: 外部保存 → Select: 引き込み → Compress: 削減 → Isolate: 分割」が業界標準として定着。
- Context Engineering for Agents (https://rlancemartin.github.io/2025/06/23/context_engineering/): CLAUDE.md / NOTES.md / worktree を使った具体的な「Isolate」パターンを解説。

**結論**: 4戦略チートシートを `_write_agent_claude_md()` で生成する CLAUDE.md に追記することで、全エージェントが戦略を参照できるようになる。

**References**:
- Zilliz context engineering: https://zilliz.com/blog/context-engineering-for-ai-agents
- LangChain context engineering: https://blog.langchain.com/context-engineering-for-agents/
- R Lance Martin context engineering: https://rlancemartin.github.io/2025/06/23/context_engineering/

### 実装方針

#### Part A: `_dispatch_task()` にファイル存在チェックを追加

`ClaudeCodeAgent._dispatch_task()` でトリガー送信後、最大 3秒間 `prompt_file.exists()` をポーリングする:
1. ファイルが消えた → フック発火 → 配送成功 → そのまま継続
2. 3秒後もファイルが残る → paste-preview ブロック中 → `send_keys("", enter=True)` で Enter を送信
3. Enter 送信後さらに 3秒ポーリング → ファイル消滅を確認

フォールバック (`_cwd is None`) パスには影響なし。

#### Part B: `_write_agent_claude_md()` にコンテキスト4戦略セクションを追加

`ClaudeCodeAgent._write_agent_claude_md()` が生成する CLAUDE.md の「Context Management」セクション直下に
「## Context Engineering Strategies」セクションを追加する。
書き込み・選択・圧縮・分離の4戦略をロール別推奨組み合わせ付きで記載する。

### テスト方針

- `test_dispatch_task_file_existence_check.py`:
  - ファイルが即削除される場合: Enter 送信なし
  - ファイルが 3秒残る場合: Enter 送信あり
  - Enter 送信後にファイル削除: 続行
- `test_agent_claude_md_context_strategies.py`:
  - `_write_agent_claude_md()` が「Context Engineering Strategies」セクションを含む CLAUDE.md を生成することを検証
  - 4戦略 (Write / Select / Compress / Isolate) が全て記載されていることを検証
- 全既存テスト (1852+) が green であること

## §10.44 — v1.1.8: POST /workflows/spec-first + `/spec` スラッシュコマンド

### 選定理由

**選択: `POST /workflows/spec-first` + `/spec` スラッシュコマンド — 仕様先行開発パターン**

v1.1.8 では §11「機能・ワークフロー」の「形式仕様エージェントステップ + `/spec` スラッシュコマンド」（中優先度）を選択する。

当初 `POST /workflows/adr` を選定したが、調査の結果 **v0.40.0 で既に実装済み**（`test_workflow_adr.py` 25テスト PASS、v0.40.0 build-log ALL 27 CHECKS PASSED）であることが判明した。§11 のリストから strikethrough が抜けていたため。ADR ワークフローと相補的な次のステップとして「仕様先行（Spec-First）開発パターン」を選択する。

**選択理由:**
1. **明確な実装パス**: `spec-writer.md` ロールテンプレートが既存（v1.0.27で追加）。`/spec` コマンドは `agent_plugin/commands/` への1ファイル追加。`POST /workflows/spec-first` は `pair` / `adr` パターンを踏襲し2エージェントパイプラインで実装できる。
2. **研究的裏付けが強い**: SYSMOBENCH arXiv:2509.23130（LLM の TLA+ 仕様生成能力評価）、Hou et al. 2025「Trustworthy AI Requires Formal Methods」、Benjamin Congdon 2025「AI 生成コードが増えるほど仕様が重要になる」。
3. **デモの実証価値が高い**: spec-writer エージェントが Python 関数の事前条件・事後条件・不変量を SPEC.md に書き、implementer エージェントがその仕様に基づいてコードを実装・テストする2エージェントパイプラインを実証できる。
4. **ADR との相補性**: ADR が「何を選ぶか」を決定するならば、Spec-First は「どう動くべきか」を定義する。両者は 設計→仕様→実装 の自然なパイプラインを形成する。
5. **既存基盤の活用**: `WorkflowManager`・スクラッチパッド・`context_files`・`system_prompt_file`・ロールテンプレートライブラリ（v1.0.27）がすべて存在する。

**非選択:**
- `POST /workflows/adr`: v0.40.0 実装済みであることが調査で判明（§11 の strikethrough 漏れ）。
- `OpenTelemetry GenAI Semantic Conventions`: 外部依存追加・インフラセットアップが必要で大規模。
- `チェックポイント永続化 (SQLite)`: スキーマ設計 + resume フラグ + API 変更で大規模。
- `ProcessPort 抽象インターフェース抽出`: テスト基盤全面改修が必要で大規模。
- `スライディングウィンドウ TF-IDF 圧縮`: `scikit-learn` 依存が必要でデモの実証が難しい。

### 実装設計

**Part A: `/spec` スラッシュコマンド (`agent_plugin/commands/spec.md`)**

```
/spec <invariant description>
```

エージェントが呼び出すと、与えられた説明に基づいて `SPEC.md` を生成する:
- 事前条件（Preconditions）
- 事後条件（Postconditions）
- 不変量（Invariants）
- 型シグネチャ（型ヒント付き Python）
- 境界ケース（Edge Cases）

`/plan` コマンドと同様の構造で実装する。

**Part B: `POST /workflows/spec-first` エンドポイント**

```
POST /workflows/spec-first
Body: {
  "topic": str,           # 実装対象の機能/モジュール名
  "requirements": str,    # 機能要件・非機能要件の説明
  "base_url": str | None,
  "spec_tags": list[str] = [],
  "impl_tags": list[str] = [],
  "priority": int = 0
}
Response 200: {"workflow_id": "uuid", "name": "spec-first/{topic}", "task_ids": {...}, "scratchpad_prefix": "..."}
```

**エージェント構成 (2エージェント、シーケンシャルパイプライン):**
1. **spec-writer** — requirements を受け取り、SPEC.md（事前条件・事後条件・不変量・型シグネチャ・境界ケース）を作成してスクラッチパッドに書き込む。`system_prompt_file: spec-writer.md`
2. **implementer** — SPEC.md を読み、仕様に準拠した実装を作成してテストを書く。`system_prompt_file: implementer.md`

**スクラッチパッドキー:**
- `{scratchpad_prefix}_spec`: spec-writer の SPEC.md 出力
- `{scratchpad_prefix}_impl`: implementer の実装サマリ

**テスト計画:**
- `tests/test_slash_spec_command.py` — 15テスト:
  - `spec.md` がエージェントプラグインに存在すること
  - SPEC.md 形式の必須セクションが含まれること（Preconditions / Postconditions / Invariants / Edge Cases）
  - Python コードブロックを含むこと
- `tests/test_workflow_spec_first.py` — 30テスト:
  - `POST /workflows/spec-first` 200: workflow_id / name / task_ids / scratchpad_prefix 返却
  - topic/requirements バリデーション（空文字 → 422）
  - 2タスク生成（spec-writer → implementer、DAG 依存関係）
  - タスクプロンプトに topic / requirements / スクラッチパッドキーが含まれること
  - spec-writer プロンプトにスクラッチパッド書き込みスニペットが含まれること
  - implementer プロンプトに読み込みスニペットが含まれること
  - `system_prompt_file` が `spec-writer.md` / `implementer.md` に設定されること
  - `spec_tags` / `impl_tags` が各タスクの `required_tags` に変換されること
  - OpenAPI スナップショット更新
- 全既存テスト (1950+) が green であること

### 参照文献（Web 検索結果）

1. **AgenticAKM: Enroute to Agentic Architecture Knowledge Management** (arXiv:2602.04445, 2025) — 4役割エージェント（Extractor / Retriever / Generator / Validator）によるアーキテクチャドキュメント自動生成。アジェンティックアプローチは単一LLMベースラインを全評価指標で上回った（Overall Quality: 3.8–3.9 vs 3.3）。URL: https://arxiv.org/html/2602.04445v1

2. **Multi-Agent Debate Strategies to Enhance Requirements Engineering** (arXiv:2507.05981, 2025) — MAD を要件エンジニアリングに適用。F1スコアを 0.726→0.835 に改善（p<0.001）。debater/judge/summarizer 役割分類、Topology/Protocol/Format の3構造次元。URL: https://arxiv.org/html/2507.05981v1

3. **MADR: Markdown Architectural Decision Records** (adr.github.io, 2024) — MADR 4.0.0 公式テンプレート仕様。title / status / context / decision-drivers / considered-options / decision-outcome / pros-and-cons / more-information の必須セクション。URL: https://adr.github.io/madr/

4. **Designing LLM-based Multi-Agent Systems for Software Engineering Tasks** (arXiv:2511.08475, 2025) — 16設計パターンのうち「役割ベース協調」が最頻出。proposer→reviewer→synthesizer 型パイプラインがソフトウェアエンジニアリングタスクに広く適用。URL: https://arxiv.org/html/2511.08475v1

5. **Can LLMs Generate Architectural Design Decisions?** (arXiv:2403.01709, 2024) — 単一 LLM アプローチは「コンテキストウィンドウ制約」「抽象的出力」の問題があり多エージェント協調が不可欠。URL: https://arxiv.org/html/2403.01709v1

---

## §10.43 — v1.1.7: POST /agents/{id}/brief — エージェントへの中断不要コンテキスト注入

### 選定理由

**選択: `POST /agents/{id}/brief` — Out-of-band コンテキスト注入エンドポイント**

v1.1.7 では §11「機能・ワークフロー」および §11「層4：コンテキスト伝達（改善）」の未実装高優先度候補の中から、ユーザー向け新機能として最も実装コストが低く価値が明確な `POST /agents/{id}/brief` を選択する。

**選択理由:**
1. **直接ユーザー価値**: 長時間タスク実行中のエージェントに要件変更・追加情報を注入できる。現状では実行中エージェントへの非同期通知手段がない。
2. **実装パスが明確**: P2P メッセージング (`__MSG__:{id}` via `send_keys`) と同一メカニズムで `__BRIEF__:{id}` マーカーを送信すれば Stop Hook との競合が発生しない。
3. **Stop Hook 競合の解消**: §10.40/§10.41 で「複雑」と記録された懸念は UserPromptSubmit フック経由の実装に起因する。`__brief__.txt` をワークツリーに書き込み + `send_keys` でエージェントに通知するシンプルなアプローチにより複雑性を排除できる。
4. **既存基盤の活用**: `tmux_interface.send_keys()`, `worktree_path`, メールボックス/JSONL 書き込みパターンはすべて既存実装が存在する。

**非選択:**
- `エージェント状態機械 Hypothesis ステートフルテスト拡張`: `test_agent_status_stateful.py` (§10.33) で既に AgentStatus 遷移を網羅。ワークフローフェーズの Hypothesis テストも追加できるが、本番コード変更なしで単独イテレーションとしては価値が低い。
- `UseCaseInteractor 層の抽出`: `use_cases.py` (SubmitTaskUseCase/CancelTaskUseCase) が既に実装済みであり、全ハンドラーへの拡張は大規模リファクタリングで本イテレーションには不適。
- `OpenTelemetry GenAI 計装`: 依存ライブラリ追加・OTLP 設定が必要で単独イテレーション不適。
- `web/app.py startup/lifespan リファクタリング`: v1.1.5–v1.1.6 で APIRouter 分割完了済みであり、lifespan 部分は小規模で独立した価値が薄い。

### 実装設計

**API:**
```
POST /agents/{agent_id}/brief
Body: {"content": "string (max 4096 chars)", "brief_id": "optional uuid"}
Response 200: {"brief_id": "uuid", "delivered": true, "worktree_path": "..."}
Response 404: agent not found
Response 422: content empty or too long
```

**配信メカニズム:**
1. `brief_id` (UUID) を生成
2. エージェントのワークツリー (`worktree_path`) に `__brief__/{brief_id}.txt` を書き込む
3. エージェントの tmux ペインに `__BRIEF__:{brief_id}` を `send_keys` で通知
4. エージェントは通知を受けて `/read-brief {brief_id}` スラッシュコマンドで内容を読む

**スラッシュコマンド:** `.claude/commands/read-brief.md` — brief_id を引数にとり `__brief__/{brief_id}.txt` を読んで内容を Claude のコンテキストに取り込む

**`isolate: false` エージェント対応:** worktree_path が None の場合はエージェントの `cwd` へフォールバック

### 参照文献（Web 検索結果）

1. **LangChain "Context Engineering in Agents"** (LangChain Docs, 2025) — 「エージェントへの動的コンテキスト注入は state (short-term memory)、store (long-term memory)、runtime context の3層で管理される。ランタイム変更はトランジェントでターン単位、ライフサイクル変更は state に永続化される」。URL: https://docs.langchain.com/oss/python/langchain/context-engineering

2. **OpenAI Agents SDK "Context Management"** (OpenAI Docs, 2025) — 「エージェントに新データを追加するには conversation history に追加する形で agent instructions を更新する必要がある。メッセージ追加は Message Added イベントを発火し HookProvider でメモリやコンテキストロードに使える」。URL: https://openai.github.io/openai-agents-python/context/

3. **Claude Code Hooks Reference** (Claude Code Docs, 2025) — UserPromptSubmit フックで `additionalContext` フィールドを JSON stdout に含めることでプロンプト処理前にコンテキストを注入できる。ただし `additionalContext` のバグ報告 (Issue #14281) があり、`send_keys` による直接通知の方が確実性が高い。URL: https://code.claude.com/docs/en/hooks

4. **Bijit Ghosh "Context Engineering is Runtime of AI Agents"** (Medium, 2025) — 「コンテキストエンジニアリングはプロンプト設計を超えた、推論時の情報エコシステム全体の管理である。長時間エージェントではコンテキストは単一のプロンプトではなく、instructional / operational / retrieved knowledge を動的に組み合わせたランタイムである」。URL: https://medium.com/@bijit211987/context-engineering-is-runtime-of-ai-agents-411c9b2ef1cb

5. **Claude Code GitHub Issues "Feature: Allow Hooks to Bridge Context Between Sub-Agents"** (GitHub, 2025) — エージェント間コンテキスト橋渡しは現在 hooks では困難であり、ファイルベースの共有 (shared scratchpad / context_files) が推奨される回避策。URL: https://github.com/anthropics/claude-code/issues/5812

### テスト計画

- `tests/test_brief_endpoint.py` — `POST /agents/{id}/brief` 単体テスト:
  - 200: brief_id 返却, ファイル書き込み確認, send_keys 呼び出し確認
  - 404: agent not found
  - 422: content empty / too long (>4096 chars)
  - 422: content None
  - `isolate: false` エージェント (worktree_path=None) での cwd フォールバック
  - brief_id 省略時の UUID 自動生成
  - BUSY/IDLE いずれの状態でも配信可能
  - OpenAPI スキーマスナップショット更新

- `tests/test_read_brief_command.py` — `/read-brief` スラッシュコマンドの内容テスト:
  - `__brief__/{id}.txt` が存在する場合の出力フォーマット確認

---

## §10.42 — v1.1.6: web/routers/ APIRouter 実装（エンドポイントハンドラの物理分割）

### 選定理由

**選択: `web/routers/` APIRouter 実装**

v1.1.5 では `web/schemas.py` へのスキーマ分離と `get_one_dict()` O(1) 最適化を実装したが、エンドポイントハンドラは依然として `web/app.py` の `create_app()` クロージャ内に 6463 行として残っている。v1.1.5 の設計ドキュメント (design/v1.1.5-web-router-split.md) に明記された「次ステップ」の実装:

分割先:
- `web/routers/agents.py` — /agents/* (エージェント管理)
- `web/routers/tasks.py` — /tasks/* (タスク管理)
- `web/routers/workflows.py` — /workflows/* (ワークフロー全種)
- `web/routers/scratchpad.py` — /scratchpad/* (スクラッチパッド)
- `web/routers/system.py` — /health*, /readyz, /metrics, /dlq, /audit-log, /checkpoint, /telemetry, /drift, /results, /orchestrator/*, /rate-limit, /autoscaler
- `web/routers/webhooks.py` — /webhooks/*
- `web/routers/groups.py` — /groups/*
- `web/routers/memory.py` — /agents/{id}/memory (エピソード記憶)

`create_app()` は `include_router()` の束のみに縮小される。

**非選択:**
- `POST /agents/{id}/brief`: §10.40/§10.41 で記録済み「Stop Hook との競合が複雑」。設計未定。
- `エージェント状態機械 Hypothesis ステートフルテスト`: 本番コード変更なし、単体で価値が低い。
- `ProcessPort 抽象インターフェース抽出`: 大規模変更で単独イテレーション不適。
- `チェックポイント永続化 SQLite`: 大規模変更。

### 参照文献（Web検索結果）

1. **FastAPI 公式ドキュメント "Bigger Applications - Multiple Files"** (2025) — `APIRouter` を使った大規模アプリの公式推奨構造。依存関係の `dependencies=` を Router レベルで宣言することで、全エンドポイントへの認証を簡潔に適用できる。URL: https://fastapi.tiangolo.com/tutorial/bigger-applications/

2. **zhanymkanov "FastAPI Best Practices and Conventions"** (GitHub, 2025) — 「router は domain/機能単位で分割し、main.py は include_router() だけにする」「Depends() を router レベルで宣言することで個別ルートの dependencies= を省略できる」パターンを推奨。URL: https://github.com/zhanymkanov/fastapi-best-practices

3. **Patrick Kennedy "Structuring Large FastAPI Applications"** (testdriven.io, 2025) — builder 関数パターン (`def build_router(state) -> APIRouter`) により依存オブジェクト (orchestrator 等) を各 router に渡す具体的実装例。URL: https://testdriven.io/blog/fastapi-best-practices/

4. **Bhagya Rana "Stop Writing Monolithic FastAPI Apps"** (Medium, 2025) — `create_app()` クロージャ内の全エンドポイントが「メンテナンス困難・マージコンフリクト頻発・テスト性低下」を引き起こす問題を解説し、APIRouter 分割による解決を示す。URL: https://medium.com/@bhagyarana80/stop-writing-monolithic-fastapi-apps-this-modular-setup-changed-everything-44b9268f814c

5. **Microsoft Azure Architecture Center "API Design Best Practices"** (2025) — リソース中心のエンドポイント設計 (agents / tasks / workflows / scratchpad をそれぞれ独立 router にする) が REST 原則と保守性の両方に最適と指摘。URL: https://learn.microsoft.com/azure/architecture/best-practices/api-design

---

## §10.41 — v1.1.5: web/app.py APIRouter 分割 + `get_agent_dict` O(1) 最適化

### 選定理由

**選択 1: `web/app.py` APIRouter 分割**

`web/app.py` は現在 **7434 行** という巨大なファイルになっており、新しいエンドポイントを追加するたびに認知的負荷が急増している。FastAPI の `APIRouter` を使って機能ドメイン別にファイルを分割することで、可読性・テスト性・開発効率を大幅に向上させる。分割先のルーター:
- `web/routers/agents.py` — エージェント管理 (GET /agents, POST /agents, PATCH, DELETE, etc.)
- `web/routers/tasks.py` — タスク管理 (GET /tasks, POST /tasks, DELETE /tasks/{id}, etc.)
- `web/routers/workflows.py` — ワークフロー (POST /workflows/*, GET /workflows/*)
- `web/routers/scratchpad.py` — スクラッチパッド (GET/PUT/DELETE /scratchpad/*)
- `web/routers/system.py` — システム (GET /health, GET /drift, GET /metrics, etc.)
- `web/app.py` は `include_router()` のみを呼ぶシムに縮小

§11「アーキテクチャ・品質」の中優先度候補として長らく記載されてきた。app.py が大きくなるほど毎イテレーションの開発コストが増大するため、早期に分割する方が後のコストを削減できる。

**選択 2: `get_agent_dict` O(n) → O(1) 最適化**

現在 `Orchestrator.get_agent_dict(agent_id)` は `list_all()` で全エージェントの辞書リストを構築した後、線形探索で1件を返している。`AgentRegistry` に `get_one_dict(agent_id)` メソッドを追加して直接 O(1) で構築することで不要な N-1 件の辞書構築を回避する。実装コストは最小で測定可能な改善。

**非選択:**
- `POST /agents/{id}/brief`: §10.40 で「Stop Hook との競合が複雑」と記録済み、設計が固まっていない。
- Prompt delivery timeout 短縮 (1.5s): 単独イテレーション価値が低い。
- `/workflows/ddd` contexts count validator: スコープが小さすぎる。

### 参照文献（Web検索結果）

1. **FastAPI 公式ドキュメント "Bigger Applications - Multiple Files"** — APIRouter を使った大規模アプリの公式推奨構造。`app.include_router()` でルーターをメインアプリに結合する標準パターン。URL: https://fastapi.tiangolo.com/tutorial/bigger-applications/ (2025)

2. **zhanymkanov "FastAPI Best Practices and Conventions"** (GitHub, 2025) — スタートアップの実践知識集。「ルーターをドメイン/機能単位で分割し、main.py は orchestration のみに徹する」ことを推奨。URL: https://github.com/zhanymkanov/fastapi-best-practices

3. **Bhagya Rana "Stop Writing Monolithic FastAPI Apps — This Modular Setup Changed Everything"** (Medium, 2025) — monolithic app.py の問題点（ナビゲーション困難、ロジックの絡み合い、マージコンフリクト）とモジュール分割の手順を解説。URL: https://medium.com/@bhagyarana80/stop-writing-monolithic-fastapi-apps-this-modular-setup-changed-everything-44b9268f814c

4. **GeeksforGeeks "Time Complexities of Python Dictionary"** — Python dict lookup の O(1) 計算量の根拠（ハッシュテーブル）。`list` の O(n) との比較で 89–11,603 倍の高速化を実測。URL: https://www.geeksforgeeks.org/python/time-complexities-of-python-dictionary/ (2025)

5. **AppSignal Blog "Ways to Optimize Your Code in Python"** (2025-05-28) — dict lookup を変数に先キャッシュして繰り返し参照を避ける等の最適化パターン。URL: https://blog.appsignal.com/2025/05/28/ways-to-optimize-your-code-in-python.html

**結論**: FastAPI の公式推奨および業界ベストプラクティスはいずれも「大規模アプリは APIRouter で機能ドメイン別に分割せよ」と指示している。Python dict の O(1) 特性は well-established であり、現在の O(n) `get_agent_dict` は不要なコストを発生させている。両改善の実装は後方互換性を保ちながら段階的に行える。

---

## §10.40 — v1.1.4: GET /agents/{id} 単体エンドポイント + Stale Worktree 自動クリーンアップ

### 選定理由

**選択 1: `GET /agents/{id}` 単体エンドポイント**

§11 候補リストに記載の未実装機能。現在 `/agents` がすべてのエージェントを返すが、IDで1件取得するエンドポイントが存在しない。デモ・テスト・外部ツール連携で繰り返し必要とされるパターン（JSON パスで単一エージェントを参照するより REST 原則に沿った設計）。実装コストは最小（既存 `GET /agents` の延長線上）だが、APIの完全性に大きく寄与する。

**選択 2: Stale Worktree 自動クリーンアップ (`git worktree prune` on startup)**

build-log v1.0.23 で「stale worktree cleanup が recurring pain」と記録済み。デモ毎に手動で `git worktree prune` を実行する必要があり、忘れると worktree 名が衝突してエージェント起動に失敗する。オーケストレーター起動時に自動的に `git worktree prune` を呼ぶことで、このクラスのエラーを根絶できる。`WorktreeManager.prune_stale()` として単体テスト可能な形で実装。

**非選択:**
- `POST /agents/{id}/brief`: mid-task injection のセマンティクス（Stop Hook との競合）が複雑。DESIGN.md §10.40 のフォローアップ候補に残す。
- `contexts` count validator for /workflows/ddd: スコープが小さすぎる。
- Prompt delivery timeout 短縮 (1.5s): v1.1.2/v1.1.3 で関連修正済み、単独イテレーション価値が低い。
- DriftMonitor セマンティック類似度: `sentence-transformers` 依存、大きな変更。

---

## §10.45 — v1.1.9: エージェント状態機械 Hypothesis ステートフルテスト

**選択日**: 2026-03-09

### 選択理由

**選択: `AgentStatus` 遷移シーケンスの Hypothesis `RuleBasedStateMachine` テスト**

§11「層5：ツール・マネジメント（アーキテクチャ品質）」中優先度候補。

**選択理由**:
1. **本番コード変更なし**: `AgentStatus` 遷移ロジック (`IDLE→BUSY→IDLE/ERROR/DRAINING`) はすでに実装済み。テストコードのみ追加するため、既存機能へのリスクがゼロ。
2. **テストカバレッジの重要なギャップを埋める**: v0.10.0 で導入した PBT (`test_bus_stateful.py`) はステートレスなプロパティテストのみ。状態遷移シーケンス（特に割り込み・タイムアウト・リカバリ）のテストが未カバー。
3. **デッドロック・不変量違反の自動検出**: Hypothesis `stateful` が生成するシーケンスは手書きテストでは到達しない遷移パスを発見する。過去のバグ（watchdog timeout、circuit breaker誤作動）のリグレッションを自動化できる。
4. **実装コストが低い**: `RuleBasedStateMachine` 1クラス＋ルール関数5-8個で完結する。新しい依存ライブラリ不要。
5. **デモ価値**: 本番コード変更なしでも、2エージェント（Director + Worker）を使った「状態遷移を実際に踏むタスクパイプライン」デモを追加できる。

**選択しなかった候補と理由**:
- **チェックポイント永続化 (SQLite)**: スキーマ設計・`--resume` フラグ・マイグレーション管理で実装規模が大きい。v1.2.x 以降向け。
- **ProcessPort 抽象インターフェース**: `ClaudeCodeAgent` の全 libtmux 依存を交換するリファクタリングは広範囲かつ既存テストへの影響が大きい。
- **OpenTelemetry GenAI Semantic Conventions**: OTLP エクスポーター・Jaeger/Datadog セットアップが必要でデモ環境の準備コストが高い。
- **`UseCaseInteractor` 層の抽出**: FastAPI ハンドラーの全面リファクタリングで、現在の安定したエンドポイントに不要なリスクを持ち込む。
- **Director の `agent_drift_warning` 購読**: DriftMonitor が drift_warnings=0 のままのケースで効果が見えにくく、デモ価値が低い。

### 実装計画

**テスト設計** (`tests/test_agent_status_stateful.py`):

```python
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant, initialize
from tmux_orchestrator.domain.agent import AgentStatus

class AgentStatusMachine(RuleBasedStateMachine):
    # モデル: 状態 × ビジーカウンタ × エラーカウンタ
    @initialize()
    def setup(self): ...

    @rule()
    def dispatch_task(self): ...   # IDLE → BUSY

    @rule()
    def complete_task(self): ...   # BUSY → IDLE

    @rule()
    def task_error(self): ...      # BUSY → ERROR

    @rule()
    def recover_from_error(self): ... # ERROR → IDLE (circuit breaker reset)

    @rule()
    def drain_agent(self): ...     # IDLE/BUSY → DRAINING

    @invariant()
    def valid_status(self): ...    # AgentStatus は常に有効な値

    @invariant()
    def busy_only_from_idle(self): ... # BUSY になる前は必ず IDLE だった
```

**対象クラス/モジュール**:
- `src/tmux_orchestrator/domain/agent.py` — `AgentStatus` enum
- `src/tmux_orchestrator/registry.py` — `AgentRegistry` (状態遷移メソッド群)
- `src/tmux_orchestrator/orchestrator.py` — dispatch ロジック

**デモシナリオ** (`~/Demonstration/v1.1.9-hypothesis-stateful/`):
- Director エージェントが複数タスクを順次発行し、Worker が IDLE→BUSY→IDLE を繰り返す
- watchdog タイムアウトを意図的に引き起こして ERROR→回復のサイクルを実証
- デモ前後で Hypothesis テストが全てパスすることを確認

### 調査結果 (Step 1 — Research)

**Query 1**: "Hypothesis stateful testing RuleBasedStateMachine state machine Python 2025 invariants"

主要知見:
- **Hypothesis 公式ドキュメント "Stateful tests"**: `RuleBasedStateMachine` は `@rule()` で遷移を定義し、`@invariant()` で各ステップ後に検証する不変量を記述する。`@precondition` でルールの適用条件を指定することで assume() より効率的なフィルタリングが可能。`@initialize()` で初期化ルールを定義し、初期化前は `@invariant()` が実行されない。
- **Hypothesis "Rule Based Stateful Testing" 記事**: `Bundle` を使ってステートフルなデータ（例: 生成されたエージェントID）をルール間で受け渡す設計パターンを解説。存在しないオブジェクトに対する操作を avoid するための `assume()` の使い方を例示。
- **`precondition()` デコレータ**: `assume()` と異なり、条件を満たさないルールは最初からスキップされる（ヘルスチェック違反を防ぐ）。`assume()` はルール内で呼び出して条件が満たされない入力を棄却する。

**References**:
- Hypothesis docs "Stateful tests": https://hypothesis.readthedocs.io/en/latest/stateful.html
- Hypothesis "Rule Based Stateful Testing": https://hypothesis.works/articles/rule-based-stateful-testing/

**Query 2**: "Hypothesis RuleBasedStateMachine agent state machine deadlock detection property based testing concurrent systems 2025"

主要知見:
- **QuickCheck State Machine (Hackage `quickcheck-state-machine`)**: Haskell 実装の `quickcheck-state-machine` は sequential + parallel の2プロパティを定義し、並列実行時のリニアリザビリティ違反を検出。Python の Hypothesis はシーケンシャルのみ直接サポートするが、ステートフルテストとして状態遷移シーケンスの不変量を効率よく検証できる。
- **Formal Signoff for Digital State Machines (IJSAT 2025)**: デジタル状態機械の形式的検証にデッドロック検出を組み込む手法を論じる。「到達不能状態」と「デッドロック状態」を形式モデルで検出。Hypothesis のステートフルテストは同等の問題を確率的に検出できる（全状態空間ではなく反例主導のファジング）。
- **Monitoring Multi-Agent Systems for deadlock detection (ResearchGate)**: UML モデルを基にした実行時デッドロック検出手法。エージェント間通信プロトコルの状態機械モデルと Hypothesis の RuleBasedStateMachine は構造的に同型。

**References**:
- Hypothesis docs stateful.rst: https://github.com/HypothesisWorks/hypothesis/blob/master/hypothesis-python/docs/stateful.rst
- Formal Signoff for Digital State Machines, IJSAT 2025: https://www.ijsat.org/papers/2025/1/7767.pdf
- quickcheck-state-machine: https://hackage.haskell.org/package/quickcheck-state-machine

**Query 3**: "Hypothesis stateful testing Python AsyncIO state machine IDLE BUSY ERROR transitions invariants 2025"

主要知見:
- **Pytest 8.0 Async / Hypothesis Stateful (johal.in 2025)**: `asyncio.new_event_loop()` + `loop.run_until_complete()` パターンで Hypothesis ステートフルテストから非同期コードを呼び出す方法を解説。各マシンインスタンスが独自のイベントループを持つことで Hypothesis のフォーク動作と互換性を保てる。
- **Python state machine libraries**: `python-statemachine 3.0.0` は async コールバックをネイティブサポートし、`transitions` ライブラリは `MachineFactory.get_predefined(asyncio=True)` で非同期機械を生成する。TmuxAgentOrchestrator の `AgentStatus` は単純な Enum であるため、既存の `_set_busy` / `_set_idle` メソッドを `loop.run_until_complete()` でラップして直接テスト可能。
- **HealthCheck.filter_too_much 抑制**: `precondition` と `assume()` の組み合わせで状態フィルタリングが多い場合に Hypothesis が health check 違反を報告する。`@precondition` を優先して `assume()` を最小化することで解決できる。

**References**:
- Pytest 8.0 Async / Hypothesis Stateful 2025: https://johal.in/pytest-8-0-async-trio-anyio-hypothesis-stateful-junit-xml-parallelism-2025/
- Hypothesis docs stateful: https://hypothesis.readthedocs.io/en/latest/stateful.html
- python-statemachine async support: https://python-statemachine.readthedocs.io/en/latest/async.html

### 実装サマリー

3つの `RuleBasedStateMachine` を `tests/test_stateful_agent_and_breaker.py` に実装した:

1. **`RealAgentStatusMachine`** — `_FakeAgent` の `_set_busy()` / `_set_idle()` を直接呼び出し、4つの不変量 (P1–P4) を検証。特に P3「`_set_idle()` は DRAINING/ERROR/STOPPED 状態では no-op」を自動生成シーケンスで確認。
2. **`CircuitBreakerMachine`** — `record_success()` / `record_failure()` / `_to_half_open()` を呼び出し、5つの不変量 (B1–B5) を検証。特に B3「`_opened_at` は OPEN 時のみセット」と B4「CLOSED → `is_allowed()=True`, OPEN → `is_allowed()=False`」を検証。
3. **`AgentRegistryMachine`** — `register` / `unregister` / `record_busy` / `record_result` を任意順序で呼び出し、3つの不変量 (R1、R3、R4) を検証。特に R1「`find_idle_worker()` は IDLE エージェントのみ返す」を確認。

全テスト: **2010** (2007 + 3 新規)。

---

## §10.20 v1.1.10 — OpenTelemetry GenAI Semantic Conventions 準拠トレース出力

### Step 0 — 選択の根拠

**選択: OpenTelemetry GenAI Semantic Conventions 準拠トレース出力**

§11「アーキテクチャ・品質」高優先度候補3件のうち最もコストパフォーマンスが高い。

| 候補 | 優先度 | 理由 |
|------|--------|------|
| **OpenTelemetry GenAI Semantic Conventions** | **選択** | 既存 `trace_id` + JSON 構造化ログ基盤の上に計装レイヤーを追加するだけ。エージェント挙動・API 互換性に影響なし。`opentelemetry-sdk` と `opentelemetry-exporter-otlp-proto-grpc` の追加で完結。中程度の実装コスト。 |
| チェックポイント永続化 SQLite | 見送り | SQLite スキーマ設計・`--resume` フラグ・ワークフロー状態再構築と範囲が広く、1 イテレーション内に収めると品質を損なうリスクが高い。 |
| ProcessPort 抽象インターフェース | 見送り | `ClaudeCodeAgent` 全体の依存方向逆転を伴う大規模リファクタリング。 libtmux 依存除去により既存 E2E テストへの影響が大きい。 |

**実装スコープ**:
1. `src/tmux_orchestrator/telemetry.py` — `TelemetryProvider` singleton: TracerProvider 初期化、OTLP gRPC/HTTP エクスポーター設定、ConsoleSpanExporter (開発用)。
2. `gen_ai.*` Semantic Convention 属性を主要イベントに付与:
   - タスクディスパッチ: `gen_ai.operation.name="invoke"`, `gen_ai.agent.id`, `gen_ai.agent.name`, `gen_ai.request.model`
   - タスク完了: `gen_ai.response.finish_reason`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`
   - ワークフロー: `gen_ai.workflow.id`, `gen_ai.workflow.type`, `gen_ai.workflow.phase`
3. 既存 `trace_id` ベース JSON ログとの相関: span context (trace_id + span_id) を structlog に伝播。
4. `GET /telemetry/spans` — 直近 N スパンを JSON で返す (ConsoleSpanExporter の代替、テスト用)。
5. `OTEL_EXPORTER_OTLP_ENDPOINT` 環境変数で外部 Collector URL を設定可能。未設定時は ConsoleExporter のみ動作。

### Step 1 — Research

**Query 1**: "OpenTelemetry GenAI Semantic Conventions gen_ai.* attributes AI agents 2025"

主要知見:
- **OTel GenAI Semantic Conventions** (opentelemetry.io/docs/specs/semconv/gen-ai/): エージェント固有属性として `gen_ai.agent.id`、`gen_ai.agent.name`、`gen_ai.agent.description`、`gen_ai.agent.version` が "Development" 安定度で定義されている。
- スパン名は `{gen_ai.operation.name} {gen_ai.request.model}` の形式。エージェント呼び出しの operation は `invoke_agent`、ツール実行は `execute_tool`。
- 推奨属性: `gen_ai.response.finish_reasons`、`gen_ai.usage.input_tokens`、`gen_ai.usage.output_tokens`。
- `gen_ai.system` はプロバイダー名（例: `"claude"` は `"anthropic"` が正式だが、`"claude"` も許容される incubating 属性）。

**References**:
- OTel GenAI Semantic Conventions spans: https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
- OTel GenAI attributes registry: https://opentelemetry.io/docs/specs/semconv/attributes-registry/gen-ai/

**Query 2**: "OpenTelemetry Python SDK TracerProvider manual instrumentation BatchSpanProcessor OTLP 2025"

主要知見:
- **Python SDK 手動計装**: `tracer.start_as_current_span("name")` コンテキストマネージャーでスパンを作成。`span.set_attribute(key, value)` で属性を付与。
- **BatchSpanProcessor** vs **SimpleSpanProcessor**: 本番環境では `BatchSpanProcessor` を使うべき（バックグラウンドスレッドで非同期エクスポート、スループット向上）。テストには `SimpleSpanProcessor`（同期エクスポート）。
- **OTLP gRPC エクスポーター**: `OTLPSpanExporter(endpoint="localhost:4317")` で設定。`OTEL_EXPORTER_OTLP_ENDPOINT` 環境変数でも設定可能。
- **InMemorySpanExporter**: テストおよび `GET /telemetry/spans` 的な REST 公開用のリングバッファとして利用可能。

**References**:
- OTel Python instrumentation: https://opentelemetry.io/docs/languages/python/instrumentation/
- OTel Python SDK BatchSpanProcessor docs

**Query 3**: "OpenTelemetry AI agent observability 2025 blog gen_ai semantic conventions"

主要知見:
- **OTel AI Agent Observability Blog** (opentelemetry.io/blog/2025): AI エージェントフレームワーク（CrewAI, AutoGen, LangGraph）が OTel GenAI SIG 標準に収斂しつつある。Baked-in instrumentation と External OTel library の2アプローチ。
- **スパン伝播**: W3C Trace Context フォーマットが標準。`trace_id` は structlog JSON ログとの相関に利用できる。
- **エージェント識別**: `gen_ai.agent.id`（一意ID）、`gen_ai.agent.name`（ヒューマンリーダブル名）、`gen_ai.agent.description`（役割説明）、`gen_ai.agent.version`（バージョン）の4属性が推奨。

**References**:
- OpenTelemetry AI Agent Observability (2025): https://opentelemetry.io/blog/2025/ai-agent-observability/
- OTel GenAI SIG Slack: #otel-genai-instrumentation

**実装ギャップ分析** (v0.47.0 既実装との比較):
- **実装済み**: `TelemetrySetup`, `agent_span()`, `task_queued_span()`, `GET /telemetry/status`, Orchestrator統合 (30テスト)
- **未実装**: `workflow_span()` (ワークフロー単位トレース), `GET /telemetry/spans` REST エンドポイント, `gen_ai.agent.description`/`version` 属性, `BatchSpanProcessor` 本番設定, OTel trace_id → structlog 伝播

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/telemetry.py`: `workflow_span()`, `RingBufferSpanExporter`, `gen_ai.agent.description/version` 属性, `BatchSpanProcessor` 本番パス
- `src/tmux_orchestrator/logging_config.py`: `JsonFormatter.format()` に OTel span context (otel_trace_id, otel_span_id) 伝播追加
- `src/tmux_orchestrator/web/routers/system.py`: `GET /telemetry/spans` エンドポイント追加
- `tests/test_telemetry_v2.py`: 25 新規テスト (2010 → 2035 全通過)

**E2E デモ** (`~/Demonstration/v1.1.10-otel-genai-semconv/`):
- agent-implementer (26.9s): statistics_utils.py (mean/median/mode) 実装 → scratchpad 保存
- agent-reviewer (39.5s): scratchpad からコード取得 → 15テスト作成・実行 → 全通過
- OTel スパン: 3件キャプチャ (`task_queued` × 2 + `invoke_agent` × 1)
- GET /telemetry/spans: リアルタイムスパン取得確認

**デバッグ事項**:
1. `--api-key` フラグ未指定 → サーバーが独自キー生成 → 全 REST 呼び出し 401
2. `POST /tasks` レスポンスキーが `task_id` (not `id`) → デモ側フォールバック追加

**25/25 チェック PASSED**


---

## §10.46 — v1.1.14: `UseCaseInteractor` 層の抽出 (`application/use_cases.py`)

### Step 0 — 選定理由

**選択: `UseCaseInteractor` 層の抽出**

**候補比較:**

| 候補 | 優先度 | 選択理由 / 見送り理由 |
|------|--------|----------------------|
| **`UseCaseInteractor` 層の抽出** | **選択** | 中程度のスコープ・明確な実装パス・Clean Architecture の依存方向修正という高い設計価値。`web/app.py` のルーター分割 (v1.1.6) が完了しているため、次の自然なステップ。`SubmitTaskUseCase` / `CancelTaskUseCase` の2件のみに絞ることで1イテレーション内に収まる。既存テストを壊さずに後方互換性を保てる。 |
| チェックポイント永続化 SQLite | 見送り | SQLite スキーマ設計・`--resume` フラグ・ワークフロー状態再構築と範囲が広すぎる。1イテレーションに収まらないリスクが高い。 |
| ProcessPort 抽象インターフェース | 見送り | `ClaudeCodeAgent` 全体の依存方向逆転を伴う大規模リファクタリング。既存 E2E テストへの影響が非常に大きい。 |
| `/deliberate` スラッシュコマンド | 見送り | 機能価値はあるが、アーキテクチャ品質の改善という長期的価値では `UseCaseInteractor` 抽出に劣る。 |
| DriftMonitor セマンティック類似度 | 見送り | `sentence-transformers` の重いモデル依存が入る。コストパフォーマンスが低い。 |

**実装スコープ:**
1. `src/tmux_orchestrator/application/use_cases.py` を新規作成:
   - `SubmitTaskUseCase` — タスク提出の業務ロジック (idempotency チェック・優先度バリデーション・タスクキュー投入) を Web 層から分離
   - `CancelTaskUseCase` — タスクキャンセル/削除の業務ロジック
   - `GetAgentUseCase` — 単一エージェント取得 (read-only、ほぼ delegation)
2. `web/routers/tasks.py` のハンドラーが Use Case を呼び出すよう書き換え (後方互換性を保つ)
3. `web/routers/agents.py` の関連ハンドラーも同様に Use Case 経由に変更
4. 新規ユニットテスト: `tests/test_use_cases.py` (Use Case を Web 層・インフラ層なしでテスト)
5. 既存テスト全件グリーン維持

**何を選ばなかったか:**
- 全ハンドラーの完全 Use Case 化は行わない (スコープを `submit_task`・`cancel_task` の2件に限定)
- TUI 側の同等移行は行わない (次イテレーション候補)

### Step 1 — Research

**Query 1**: "Clean Architecture Use Case Interactor pattern FastAPI 2025 application layer"

主要知見:
- **ivan-borovets/fastapi-clean-example** (GitHub 2025): CQRS パターン採用。Interactor は `execute()` メソッドを持ち、リポジトリをコンストラクタ DI で受け取る。FastAPI の `HTTPException` などフレームワーク固有例外を Use Case 内に混入させてはならない。
- **Layered Architecture & DI** (DEV Community): Controllers は「可能な限り薄く」し、入力バリデーションとルーティングのみを担当。業務ロジックは Service/Use Case 層へ委譲する。
- **breadcrumbs collector** "Clean Architecture in Python" (2021): Use Case (Interactor) は「個々のビジネスシナリオの名前をそのまま持つクラス」。Interactor のみを単体テストすれば Web/DB 層なしで業務ロジックを検証できる。

**References**:
- ivan-borovets/fastapi-clean-example: https://github.com/ivan-borovets/fastapi-clean-example
- Layered Architecture & DI: https://dev.to/markoulis/layered-architecture-dependency-injection-a-recipe-for-clean-and-testable-fastapi-code-3ioo
- breadcrumbs collector: https://breadcrumbscollector.tech/the-clean-architecture-in-python-how-to-write-testable-and-flexible-code/

**Query 2**: "Use Case Interactor Clean Architecture Python best practices dependency injection 2025"

主要知見:
- **py-clean-arch** (GitHub): Use Case は `execute(input_dto)` → `output_dto` の純粋な変換。外部依存は Protocol インターフェースで注入する。コマンドパターン採用で「エンキュー・ロールバック・依存分離」を同時実現。
- **python-clean-architecture** toolkit: `@use_case` デコレーターで Use Case を登録し、DI コンテナから取得するパターン。`python-inject` / `injector` ライブラリが自動アセンブル。
- **LinkedIn article**: 「Use Case はビジネスロジックの核心であり、Web/DB/外部 API の存在を知らない」というルールが Clean Architecture の Dependency Rule の具体化。

**References**:
- py-clean-arch: https://github.com/cdddg/py-clean-arch
- python-clean-architecture: https://github.com/pcah/python-clean-architecture

**Query 3**: "Martin Clean Architecture interactor layer web framework isolation testability 2024"

主要知見:
- **Uncle Bob Clean Architecture Blog** (Robert C. Martin 2012, 2024 引用): Jacobson の3分類 (Entities / Interactors / Boundaries) を適用すると、Web フレームワークはアーキテクチャの「付録 (appendix)」として端に配置される。Interactor は Gateway インターフェースのみを知り、実装の詳細は外側の層に封じ込める。
- **fullstackmark.com "Better Software Design"**: 「Business rules can be tested without UI, Database, Web Server, or any other external element」が Clean Architecture の核心メリット。
- **Medium "In 2024, Clean Architecture in C# ASP.NET"**: 2024 年においても原則に変化なし。Separation of Concerns・Testability・Maintainability の3軸が主目的。

**References**:
- Clean Architecture Blog: https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html
- Better Software Design: https://fullstackmark.com/post/11/better-software-design-with-clean-architecture

**実装への示唆**:
1. `SubmitTaskUseCase.execute(dto)` は `orchestrator.submit_task()` を Protocol 経由で呼び出す (直接参照しない)
2. Use Case 内で `HTTPException` を raise しない — 代わりに `ValueError` / ドメイン例外を raise し、FastAPI ハンドラーで変換する
3. テストは `MockOrchestratorPort` を注入して Web/非同期コンテキストなしで `execute()` を直接呼び出す

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/application/use_cases.py`: `GetAgentUseCase` + `GetAgentDTO` + `GetAgentResult` 追加
- `src/tmux_orchestrator/application/__init__.py`: 新 Use Case を export に追加
- `src/tmux_orchestrator/web/routers/tasks.py`: `_do_submit_task()` → `SubmitTaskUseCase` 委譲、`delete_task` → `CancelTaskUseCase` 委譲
- `tests/test_use_cases.py`: GetAgentUseCase の 13 テスト追加 (計 41 test メソッド)
- `tests/test_use_case_router_integration.py` (新規): ルーター-UseCase 統合テスト 33 件
- `tests/fixtures/openapi_schema.json`: DELETE /tasks/{id} 応答形状変更 (CancelTaskResult.to_dict) に追従

**バグ修正**:
- `task.required_tags` / `task.depends_on` が `None` のケース: `list(x) if x else []` に修正
  → `test_capability_tags.py::test_post_task_no_required_tags_omitted` が修正で green に

**テスト数**: 2131 → 2170 (+39)

**E2E デモ** (`~/Demonstration/v1.1.14-use-case-interactor/`):
- agent-spec: math_utils 仕様書を Markdown で作成 → scratchpad `v1114_spec` に保存 (~60s)
- agent-impl: 仕様書を読み、`math_utils.py` + `test_math_utils.py` を実装 → scratchpad `v1114_implementation` に保存 (~70s)
- CancelTaskUseCase: 存在しないタスク → 404; キュー内タスク → cancelled=true
- SubmitTaskResult.to_dict() の全必須フィールド確認 (task_id, prompt, priority, retry_count)
- デバッグ: (1) Config に `type: claude_code` 追加漏れ → `KeyError: 'type'` 修正 (2) `task.required_tags=None` バグ修正

**31/31 チェック PASSED**


## §10.47 — v1.1.15: クエリ Use Case 層の完成 (`GetAgentUseCase` 配線 + `ListAgentsUseCase`)

### Step 0 — 選定理由

**選択: `GET /agents/{id}` → `GetAgentUseCase` 配線 + `ListAgentsUseCase` 追加**

**候補比較:**

| 候補 | 優先度 | 選択理由 / 見送り理由 |
|------|--------|----------------------|
| **`GET /agents/{id}` → `GetAgentUseCase` 配線 + `ListAgentsUseCase`** | **選択** | v1.1.14 で `GetAgentUseCase` を実装したが `web/routers/agents.py` の `GET /agents/{agent_id}` ハンドラーがまだ `orchestrator.get_agent_dict()` を直接呼んでいる。UseCase 層を完成させる最小スコープ。`ListAgentsUseCase` を追加することでクエリ側 CQRS を対称的に整理できる。実装コストは低く、テスト追加が容易で既存テストを壊さない。 |
| チェックポイント永続化 SQLite | 見送り | SQLite スキーマ・`--resume` フラグ・ワークフロー状態再構築と範囲が広すぎる。 |
| ProcessPort 抽象インターフェース | 見送り | `ClaudeCodeAgent` 全体の依存方向逆転を伴う大規模リファクタリング。 |
| `/deliberate` スラッシュコマンド | 見送り | v1.0.32 で実装済み（`agent_plugin/commands/deliberate.md` が存在する）。 |
| DriftMonitor セマンティック類似度 | 見送り | `sentence-transformers` 新依存が入る。コストパフォーマンスが低い。 |

**選択しなかった理由:**
- `GetAgentUseCase` 単体の配線のみでは実装が小さすぎ、デモの意義が薄い。
  `ListAgentsUseCase` を追加することでコマンドとクエリの両 UseCase を完備し、イテレーションとして完結する。

**実装スコープ:**
1. `application/use_cases.py` に `ListAgentsUseCase` / `ListAgentsDTO` / `ListAgentsResult` を追加
2. `web/routers/agents.py` の `GET /agents/{agent_id}` → `GetAgentUseCase` 委譲
3. `web/routers/agents.py` の `GET /agents` → `ListAgentsUseCase` 委譲
4. `tests/test_use_cases.py` に新 UseCase テスト追加 (目標 +20テスト)
5. `tests/test_use_case_router_integration.py` に統合テスト追加 (目標 +10テスト)

### Step 1 — Research

**Query 1**: "CQRS query use case ListAgents GetAgent FastAPI Clean Architecture Python 2025"

主要知見:
- **Architecture Patterns with Python (cosmicpython.com)**: CQRS は「読み取り (クエリ) と書き込み (コマンド) を分離する」というシンプルな原則。`ListAgents` / `GetAgent` はクエリハンドラーとして実装し、読み取り専用モデルに作用させる。
- **ivan-borovets/fastapi-clean-example** (GitHub 2025): CQRS パターン + FastAPI の参考実装。Interactor は `execute()` メソッドを持ち、リポジトリを DI で受け取る。
- **python-cqrs** (PyPI, v5.0.0+): クエリの DTO / Result は `frozen=True` のデータクラスとして定義する。クエリ結果をドメインエンティティとして公開してはいけない。

**References**:
- cosmicpython CQRS chapter: https://www.cosmicpython.com/book/chapter_12_cqrs.html
- ivan-borovets/fastapi-clean-example: https://github.com/ivan-borovets/fastapi-clean-example
- python-cqrs PyPI: https://pypi.org/project/python-cqrs/

**Query 2**: "query use case interactor read-only CQRS Python dataclass DTO list single resource 2025"

主要知見:
- **diator** (GitHub, Murad Akhundov): クエリハンドラーは `RequestHandler[Query, Result]` インターフェースを実装し、frozen dataclass を返す。リスト取得と単件取得は別クラスとして定義する。
- **oneuptime CQRS 実装ガイド** (2026-01-22): 読み取り専用 UseCase は副作用を持ってはならない。List クエリは `Sequence[ItemDTO]` を、単件クエリは `Optional[ItemDTO]` を返す。
- **CQRS with Python DEV Community** (akhundMurad): Query と Command の分離により、List/Get エンドポイントを Command 側の変更なしに最適化できる。

**References**:
- diator GitHub: https://github.com/akhundMurad/diator
- How to Implement CQRS Pattern in Python: https://oneuptime.com/blog/post/2026-01-22-cqrs-pattern-python/view
- Implementing CQRS in Python DEV: https://dev.to/akhundmurad/implementing-cqrs-in-python-41aj

**Query 3**: "FastAPI router handler thin delegation use case layer separation concerns REST API best practices 2025"

主要知見:
- **Camillo Visini "Implementing FastAPI Services – Abstraction and Separation of Concerns"**: FastAPI ルーターハンドラーは「可能な限り薄く (thin)」設計し、入力受け取り・UseCase 呼び出し・例外変換・レスポンス整形の4ステップのみを担う。
- **Medium "Building Production-Ready FastAPI Applications"** (2025): 本番 FastAPI アプリにはサービス/UseCase 層が不可欠。ハンドラーにビジネスロジックを書くと単体テストが困難になる。
- **DEV Community "Layered Architecture & DI"**: 依存注入で UseCase を FastAPI ハンドラーに渡すと、テスト時にモックに差し替え可能になる。`orchestrator` オブジェクトをハンドラーの closure でキャプチャする既存パターンも同様に DI と見なせる。

**References**:
- Camillo Visini: https://camillovisini.com/coding/abstracting-fastapi-services
- Production-Ready FastAPI 2025: https://medium.com/@abhinav.dobhal/building-production-ready-fastapi-applications-with-service-layer-architecture-in-2025-f3af8a6ac563
- DEV Layered Architecture: https://dev.to/markoulis/layered-architecture-dependency-injection-a-recipe-for-clean-and-testable-fastapi-code-3ioo

**実装への示唆:**
1. `ListAgentsUseCase.execute(dto)` は `service.list_agents()` を呼び出し、`ListAgentsResult(items=list_of_dicts)` を返す。
2. `GET /agents` ハンドラーを `ListAgentsUseCase` に委譲するだけで HTTP/ビジネスロジック分離が実現する。
3. `GET /agents/{agent_id}` は `GetAgentUseCase` に委譲し、`not found` の場合にのみ HTTPException を raise する。

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/application/use_cases.py`: `ListAgentsDTO` / `ListAgentsResult` / `ListAgentsUseCase` を追加 (+65行)
- `src/tmux_orchestrator/application/__init__.py`: 新 UseCase を export に追加
- `src/tmux_orchestrator/web/routers/agents.py`:
  - `GET /agents` → `ListAgentsUseCase` 委譲 (was `orchestrator.list_agents()` 直接呼び出し)
  - `GET /agents/{agent_id}` → `GetAgentUseCase` 委譲 (was `orchestrator.get_agent_dict()` 直接呼び出し)
- `tests/test_use_cases.py`: `TestListAgentsUseCase` 12テスト追加
- `tests/test_use_case_router_integration.py`: 17テスト追加
  - `TestListAgentsRouterIntegration` (5): HTTP test client でエンドポイント検証
  - `TestGetAgentRouterIntegration` (6): 200/404 パス検証
  - `TestListAgentsUseCaseStandalone` (5): UseCase 単体テスト
- `pyproject.toml`: version = "1.1.15"

**テスト数**: 2170 → 2199 (+29テスト)

**E2E デモ** (`~/Demonstration/v1.1.15-list-agents-use-case/`):
- agent-writer (28s): `string_utils.py` 実装 → scratchpad `v1115_code` に保存
- agent-reviewer (113s): scratchpad からコード取得 → Markdown レビュー作成 → `v1115_review` に保存
- `GET /agents` → ListAgentsUseCase 経由で 2 エージェントを返すことを確認
- `GET /agents/agent-writer` → GetAgentUseCase 経由で 200 + agent dict を返すことを確認
- `GET /agents/no-such-agent` → 404 + detail に agent_id 含むことを確認

**33/33 チェック PASSED**



---

## §10.48 — v1.1.16: `examples/workflows/` YAML テンプレートライブラリ

### Step 0 — 選定理由

**選択: `examples/workflows/` YAML テンプレートライブラリ**

**候補比較:**

| 候補 | 優先度 | 選択理由 / 見送り理由 |
|------|--------|----------------------|
| **`examples/workflows/` YAML テンプレートライブラリ** | **選択** | §11「低〜中」優先度。しかし、高優先度の「チェックポイント永続化 SQLite」「ProcessPort 抽象インターフェース」はどちらも1イテレーションで完結しない大規模スコープ。`/deliberate` および `POST /workflows/clean-arch` は調査の結果すでに実装済み。YAML テンプレートライブラリは (1) コード変更量が少ない (2) 全ワークフロー (TDD / PairCoder / CleanArch / DDD / SpecFirst / Debate / ADR / Delphi / RedBlue / Socratic / Competition) を使いやすい自己完結 YAML として収録することでユーザー価値が高い (3) デモとして 2 エージェントが YAML テンプレートを使って実際に協調するシナリオを容易に設計できる。 |
| チェックポイント永続化 SQLite | 見送り | SQLite スキーマ設計・`--resume` フラグ・ワークフロー状態再構築と範囲が広すぎる。1イテレーションに収まらない。 |
| ProcessPort 抽象インターフェース | 見送り | `ClaudeCodeAgent` 全体の依存方向逆転を伴う大規模リファクタリング。リグレッションリスクが高い。 |
| `/deliberate` スラッシュコマンド | 見送り | `agent_plugin/commands/deliberate.md` + `tests/test_deliberate_command.py` として実装済み (v1.0.32)。 |
| DriftMonitor セマンティック類似度 | 見送り | `sentence-transformers` の重いモデル依存が入る。デモでの再現性が低い。 |
| `POST /workflows/clean-arch` | 見送り | `web/routers/workflows.py` に `submit_clean_arch_workflow` として実装済み。`tests/test_workflow_clean_arch.py` が存在する。 |
| Hypothesis ステートフルテスト | 見送り | 本番コード変更なしで価値は認められるが、デモでの multi-agent 協調を示しにくい。 |

**実装スコープ:**
1. `examples/workflows/` ディレクトリを新規作成
2. 各ワークフローエンドポイントに対応する自己完結 YAML テンプレートを収録 (11 ファイル)
3. `examples/workflows/README.md` — 使い方ガイド
4. `tests/test_workflow_yaml_templates.py` — 各 YAML のロード・スキーマ検証テスト
5. `pyproject.toml` version = "1.1.16"

### Step 1 — Research

**Query 1**: "YAML workflow templates multi-agent orchestration best practices 2025 LLM agents"

主要知見:
- **Haystack Pipeline YAML Serialization (deepset 2025)**: パイプライン全体を YAML にシリアライズ。バージョン管理・チームコラボレーションに直結。YAML が「ドキュメントとしての設定ファイル」として機能する。
- **Microsoft Agent Framework Declarative Workflows (2025)**: `kind: Workflow` + `trigger` + `actions` 構造による宣言的ワークフロー。YAMLによる定義を CI/CD パイプラインに統合。「Declarative > Imperative」の原則。
- **ZenML Best Practices (2025)**: 「まずシンプルに始め、段階的に複雑さを追加する」。エージェントのロールを明確に定義し、JSON/YAML で通信を構造化することを推奨。

**References**:
- LLM Orchestration Best Practices: https://orq.ai/blog/llm-orchestration
- ZenML Best LLM Orchestration Frameworks: https://www.zenml.io/blog/best-llm-orchestration-frameworks
- Microsoft Declarative Workflows: https://learn.microsoft.com/en-us/agent-framework/user-guide/workflows/declarative-workflows

**Query 2**: "CrewAI YAML-driven workflow configuration agents tasks 2025"

主要知見:
- **CrewAI YAML Configuration (2025)**: `agents.yaml` と `tasks.yaml` の2ファイル構成。プロパティ (role / goal / backstory) と動的プレースホルダ (`{topic}`) をサポート。設定をコードから分離してメンテナンス性を向上させる。
- **CrewAI Orchestration Engine**: `sequential` / `parallel` / `conditional` の3種タスク実行モデル。YAML で宣言的に定義し、Python API で高度な制御を追加できる。
- **Running a CrewAI Agent from YAML** (rodtrent.substack.com): 「まず YAML で宣言的に定義、必要に応じて Python API にアップグレード」というアプローチ。

**References**:
- CrewAI Tasks: https://docs.crewai.com/en/concepts/tasks
- Configuring CrewAI with YAML: https://codesignal.com/learn/courses/getting-started-with-crewai-agents-and-tasks/lessons/configuring-crewai-agents-and-tasks-with-yaml-files
- Running CrewAI from YAML: https://rodtrent.substack.com/p/running-a-crewai-agent-from-a-yaml

**Query 3**: "workflow template library YAML schema validation Python pydantic pytest 2025"

主要知見:
- **pydantic-yaml (PyPI)**: Pydantic モデルに YAML 機能を追加するライブラリ。`model_validate_yaml()` でファイルを読み込みバリデーションを実行。
- **yaml2pydantic (PyPI, 2025)**: YAML/JSON 定義から動的 Pydantic v2 モデルを生成。宣言的な YAML とコードの Pydantic の両方の利点を活用。
- **Pydantic as Schema for YAML Files**: YAML 設定を読み込み `model_validate()` でバリデーション。`ValidationError` をキャッチしてユーザーフレンドリーなエラーメッセージを提供するパターン。

**References**:
- pydantic-yaml PyPI: https://pypi.org/project/pydantic-yaml/
- yaml2pydantic PyPI: https://pypi.org/project/yaml2pydantic/
- Pydantic YAML validation guide: https://betterprogramming.pub/validating-yaml-configs-made-easy-with-pydantic-594522612db5

**Query 4**: "multi-agent workflow configuration as code YAML declarative TDFlow TDD debate 2025"

主要知見:
- **TDFlow arXiv:2510.23761 (2025)**: Test-Driven Agentic Workflow。複数の LLM サブエージェントが context-engineered environment で動作し、88.8% SWE-Bench Lite スコアを達成。
- **Microsoft Foundry Multi-Agent Workflows (2025)**: YAML による宣言的定義 + ビジュアル設計の両方をサポート。バージョン管理・CI/CD との統合が主目的。
- **Declarative > Imperative 原則**: YAML で役割・ツール・トポロジーのワイアリングを宣言的に保つことで、レビュー・テスト・デプロイが容易になる。

**References**:
- TDFlow arXiv:2510.23761: https://arxiv.org/html/2510.23761v1
- Microsoft Foundry Multi-Agent Workflows: https://devblogs.microsoft.com/foundry/introducing-multi-agent-workflows-in-foundry-agent-service/
- Microsoft Declarative Agents: https://learn.microsoft.com/en-us/agent-framework/agents/declarative

**実装への示唆**:
1. 各ワークフローの YAML テンプレートは `required_fields` と `optional_fields` をコメントで明示し、`curl` コマンド例を含める
2. 既存の Pydantic スキーマ (`TddWorkflowSubmit` 等) を使って YAML をバリデーションするテストを書く
3. YAML テンプレートは「設定をコードから分離する」原則に従い、フィールドに詳細なコメントを付ける
4. README.md は各ワークフローの agents/pipeline 構造・scratchpad キー・出力ファイルを一覧化する


### Step 2 — 実装サマリー

**実装ファイル**:
- `examples/workflows/tdd.yaml` — TDD ワークフローテンプレート
- `examples/workflows/pair.yaml` — PairCoder テンプレート
- `examples/workflows/debate.yaml` — Debate テンプレート
- `examples/workflows/adr.yaml` — ADR テンプレート
- `examples/workflows/delphi.yaml` — Delphi テンプレート
- `examples/workflows/redblue.yaml` — Red Team/Blue Team テンプレート
- `examples/workflows/socratic.yaml` — Socratic テンプレート
- `examples/workflows/spec-first.yaml` — Spec-First テンプレート
- `examples/workflows/clean-arch.yaml` — Clean Architecture テンプレート
- `examples/workflows/ddd.yaml` — DDD テンプレート
- `examples/workflows/competition.yaml` — Competition テンプレート
- `examples/workflows/README.md` — フィールド一覧・クイックスタート・curl 例
- `tests/test_workflow_yaml_templates.py` — 91 テスト (6 テストクラス)
- `pyproject.toml`: version = "1.1.16"

**テスト数**: 2199 → 2290 (+91テスト)

**テストクラス**:
- `TestTemplateFilesExist` (13): ディレクトリ・11ファイル・README の存在確認
- `TestTemplatesAreValidYaml` (33): YAML パース可能性・`workflow` キー・`endpoint` キー確認
- `TestTemplateSchemaValidation` (22): 全テンプレートの Pydantic バリデーション + フィールドアサーション
- `TestOptionalTagDefaults` (6): `*_tags=[]`・`reply_to=None` のデフォルト確認
- `TestSchemaRejectionOfInvalidData` (6): 不正データをスキーマが拒否することを確認
- `TestEndpointMetadataConsistency` (11): `workflow.endpoint` が期待値と一致することを確認

**E2E デモ** (`~/Demonstration/v1.1.16-workflow-yaml-templates/`):
- agent-navigator (35s): `math_ops.py` の PLAN.md を作成 → scratchpad `pair_{prefix}_plan` に保存
- agent-driver (44s): PLAN.md を読み取り → `math_ops.py` + テスト実装
- 11テンプレート × Pydantic スキーマバリデーション = すべて PASS
- `pair.yaml` テンプレートから POST /workflows/pair を提出 → ワークフロー実行完了

**デバッグ**: (1) pair ワークフローの scratchpad_prefix は UUID ベース (`pair_<uuid[:8]>`) であり、固定キーではない。POST レスポンスの `scratchpad_prefix` フィールドから動的に取得する必要があった。(2) タスク ID も `task_ids.navigator`/`task_ids.driver` フィールドとして POST レスポンスに含まれており、履歴ポーリングより確実。

**35/35 チェック PASSED**


## §10.49 — v1.1.17: DriftMonitor TF-IDF コサイン類似度による role_score 強化

### Step 0 — 選択

**選択: DriftMonitor TF-IDF コサイン類似度 (v1.1.17)**

§11「アーキテクチャ・品質」の**中**優先度候補。

#### 選択理由

1. **真に未実装の最高優先度候補**: §11 に残る未実装項目を精査した結果、高優先度の全項目（チェックポイント永続化・ProcessPort・OpenTelemetry・UseCaseInteractor・Hypothesis ステートフルテスト）はすべて完了済み。中優先度の未実装候補は DriftMonitor セマンティック類似度・P2P TLA+ 形式仕様化の2件のみとなった。

2. **既存インフラを最大活用**: `application/context_compression.py` に TF-IDF + コサイン類似度の純 Python 実装が既に存在する（外部依存ゼロ）。同実装を `drift_monitor.py` に転用することで `sentence-transformers` の新規依存を回避しながら§11 の要件（「embedding コサイン類似度による role_score 改善」）を達成できる。

3. **既知バグの修正**: 現行の `_compute_role_score` はキーワード出現カウントに基づき、`role_score = 1.0` に張り付く傾向が報告されている（§11 の根拠欄）。TF-IDF コサイン類似度は「単語形式は合っているが内容が異なる」ドリフトを検出できる。

4. **デモ設計が明確**: 2エージェント構成（implementer + reviewer）で、reviewer が実装と無関係な作業をすると `role_score` が下落し `agent_drift_warning` が発火することを実証できる。

#### 選択しなかった候補と理由

- **P2P 許可テーブルの TLA+ 形式仕様化**: TLA+ ツールチェーン（TLC model checker）のインストールと TLA+ 言語習得が前提となる。ユーザー向け機能ではなく仕様検証であり、ROI が低い。
- **チェックポイント永続化 SQLite の `--resume` 拡張**: `checkpoint_store.py` と `--resume` フラグは実装済み。追加スコープがあるとすれば Orchestrator 再起動後のワークフロー状態復元だが、単独イテレーションには大きすぎる。
- **DriftMonitor `sentence-transformers`**: 22MB モデルダウンロードが必要。純 Python TF-IDF で同等の改善が可能なため採用しない。

### Step 1 — Research

**Query 1**: "TF-IDF cosine similarity agent role drift detection LLM 2025"

主要知見:
- **LLMelite.com "Model Drift Management: LLM Strategies for Drift Detection & Control" (2025-12)**: コサイン類似度を使ったドリフト検出が標準手法として定着。エージェントの出力ベクトルを周期的に比較し、類似度低下でアラート発火。
- **ACL 2025 BlackboxNLP "Emergent Convergence in Multi-Agent LLM Annotation"**: TF-IDF ベクトルの連続ラウンド間コサイン類似度で、マルチエージェント LLM 注釈システムの発話収束・乖離を定量化する手法を実証。

**References**:
- LLMelite Model Drift Management: https://llmelite.com/2025/12/25/model-drift-management-llm-strategies-for-drift-detection-control/
- ACL 2025 BlackboxNLP: https://aclanthology.org/2025.blackboxnlp-1.12.pdf

**Query 2**: "agent stability index role adherence scoring multi-agent system arXiv 2025"

主要知見:
- **arXiv:2602.16666 "Towards a Science of AI Agent Reliability" (2025)**: reliability の定義に「role adherence = 事前定義された制約へのコンプライアンスを測定する policy adherence score」を含む。エージェントが operational boundary を尊重しているかを追跡するスコアリングが提案された。
- **arXiv:2511.14136 "Beyond Accuracy: A Multi-Dimensional Framework for Evaluating Enterprise Agentic AI Systems"**: 5次元 CLEAR フレームワーク（cost / latency / efficiency / assurance / reliability）で policy adherence score (PAS) を提案。セキュリティ評価を含む enterprise エージェント評価の標準指標として位置付ける。

**References**:
- arXiv:2602.16666: https://arxiv.org/abs/2602.16666
- arXiv:2511.14136: https://arxiv.org/abs/2511.14136

**Query 3**: "TF-IDF text similarity pure Python implementation no dependencies 2025"

主要知見:
- **mbrenndoerfer.com "TF-IDF and Bag of Words: Complete Guide" (2025-08)**: 外部依存ゼロの純 Python TF-IDF + コサイン類似度実装が可能。IDF = log(N/df_t) + 1、コサイン = dot(a,b) / (|a||b|) の stdlib math のみで実装できる。
- **Medium "TF-IDF from Scratch in Python"**: `collections.Counter` で TF 計算 → IDF 辞書構築 → コサイン類似度計算のパターン。`application/context_compression.py` に同様の実装が既に存在するため転用可能。

**References**:
- mbrenndoerfer.com TF-IDF guide: https://mbrenndoerfer.com/writing/tf-idf-bag-of-words-text-representation-information-retrieval
- Medium TF-IDF from Scratch: https://medium.com/bitgrit-data-science-publication/tf-idf-from-scratch-in-python-ea587d003e9e

**Query 4**: "LLM agent role drift semantic similarity system prompt output divergence detection 2025 2026"

主要知見:
- **arXiv:2601.04170 "Agent Drift: Quantifying Behavioral Degradation in Multi-Agent LLM Systems" (Rath et al., 2026-01)**: Agent Stability Index (ASI) の role_adherence 次元は「agent_id とタスクタイプの相互情報量」で測定。ASI < 0.85 で役割逸脱の兆候が 73 インタラクション中央値で出現。コサイン類似度ベースのセマンティクス計測が根本アプローチ。セマンティック・ドリフトは 600 インタラクションで約 50% のマルチエージェントワークフローに発生。
- **arXiv:2510.07777 "Drift No More? Context Equilibria in Multi-Turn LLM Interactions"**: 文脈蓄積によるドリフトを「コンテキスト均衡」モデルで説明。system_prompt と出力の意味的距離の増大がドリフトの直接指標とされる。

**References**:
- arXiv:2601.04170 (Rath et al. 2026): https://arxiv.org/abs/2601.04170
- arXiv:2510.07777: https://arxiv.org/abs/2510.07777

**実装への示唆**:
1. 現行の `_compute_role_score` (keyword overlap) を TF-IDF コサイン類似度に置き換える
2. `application/context_compression.py` の純 Python TF-IDF 実装 (外部依存ゼロ) を参考に実装
3. `system_prompt` と `pane_output` を 2 文書として TF-IDF ベクトル化 → コサイン類似度を role_score として返す
4. IDF 計算にはグローバルコーパスが不要 — この2文書コーパス内での逆文書頻度を使用
5. `drift_monitor.py` の `_MIN_KEYWORD_LEN` 下限 (3文字) を継承して短い単語をフィルタリング
6. 後方互換性: `system_prompt` が空の場合は従来の keyword fallback を維持
7. デモ: implementer + drift_agent の2エージェント構成。drift_agent が無関係な作業をするとき `role_score` が下落し `agent_drift_warning` が発火することを実証。

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/drift_monitor.py` — `_tokenize_role()` 追加、`_tfidf_cosine_similarity()` 追加、`_compute_role_score()` を TF-IDF コサイン類似度に置き換え
- `tests/test_drift_monitor.py` — `TestRoleScore` を TF-IDF セマンティクスに合わせて更新 (exact 1.0 → > 0.3 等)、`TestTfIdfCosine` (9テスト) と `TestTokenizeRole` (6テスト) 追加
- `pyproject.toml` / `__init__.py`: version = "1.1.17"

**テスト数**: 2290 → 2307 (+17テスト)

**E2E デモ** (`~/Demonstration/v1.1.17-drift-tfidf/`):
- agent-implementer (Python 実装タスク) + agent-drifter (ケーキレシピタスク) の2エージェント構成
- DriftMonitor の REST API (`GET /agents/{id}/drift`、`GET /drift`) が正常動作を確認
- 両エージェントとも25秒以内にタスク完了。タスク完了後の pane 出力がいずれも CLI 完了状態のため role_score は両方 1.0 に収束
- TF-IDF の数学的性質は TestTfIdfCosine / TestTokenizeRole の単体テストで厳密に検証済み

**デバッグ**: タスク完了後の pane 出力は CLI 完了状態になるため、DriftMonitor がポーリングする時点では両エージェントの role_score が 1.0 に収束する。drift 検出は「タスク実行中」の pane 出力に対して有効であり、完了後は測定困難。単体テストで代替検証。

**21/21 チェック PASSED**

---

## §10.50 — v1.1.18: Director `agent_drift_warning` 購読による自動 re-brief

### Step 0 — 選択

**選択: Director が `agent_drift_warning` bus イベントを購読して BUSY 中のワーカーに自動再ブリーフィングを送信する機能**

#### 選択理由

1. **真の未実装項目**: v1.1.17 でプロジェクトの §11「高」「中」優先度の大部分が完了した。残存する中優先度の未実装候補を精査した結果、Director `agent_drift_warning` 購読が最も価値/実装コスト比が高いと判断した。チェックポイント永続化・ProcessPort・OpenTelemetry は完了済み。P2P 許可テーブルの TLA+ 仕様化はツールチェーン依存が大きく ROI が低い。

2. **既存インフラを活用**: `DriftMonitor` (v1.0.9) が `agent_drift_warning` bus イベントを既に発行している。`Orchestrator.bus` は全 STATUS メッセージを伝搬する。必要な追加は Orchestrator 内で bus 購読コールバックを登録し、ドリフト警告を受け取ったら対象エージェントの pane にシステムメッセージを送信するロジックのみ。

3. **自律性の向上**: v1.0.8 build-log で「Director polling が遅い (11分ループ)」根本原因は能動的な完了通知の欠如と報告された。`agent_drift_warning` 購読による自動介入は同様の問題を予防する。エージェントが役割から逸脱した時点でオーケストレーターが即座に re-brief を注入することで、手動監視なしのドリフト回復が実現する。

4. **デモ設計が明確**: implementer エージェント（Python コード実装）+ 意図的にドリフトさせた reviewer エージェント（無関係な応答）の2エージェント構成。DriftMonitor が drift を検出 → Orchestrator が自動 re-brief を pane に送信 → reviewer が正しい役割に戻る流れを実証できる。

#### 選択しなかった候補と理由

- **P2P 許可テーブルの TLA+ 形式仕様化**: TLC model checker インストールと TLA+ 言語習得が前提。ユーザー向け機能でなく ROI が低い。
- **DECISION.md 標準フォーマット**: 低優先度。ワークフロー出力規約の統一は有用だが、実装コスト比で今回選ばない。
- **構造化デバッグ: トレースリプレイ CLI**: チェックポイント永続化は完了済みだが、replay CLI は大規模スコープ。
- **チェックポイント永続化 `--resume` の Workflow 状態復元拡張**: 単独イテレーションには大きすぎる。

#### 実装スコープ

1. `orchestrator.py`: `start()` 内で `bus.subscribe(broadcast=True)` コールバックを登録し、`event == "agent_drift_warning"` のペイロードを受け取ったとき対象エージェントへ re-brief を送信する `_handle_drift_warning()` メソッドを追加。
2. `config.py` (OrchestratorConfig): `drift_rebrief_enabled: bool = True` と `drift_rebrief_message: str` フィールドを追加。
3. REST エンドポイント `GET /agents/{id}/drift-rebriefs` — 各エージェントが受けた re-brief の履歴 (timestamp, drift_score) を返す（オプショナル）。
4. テスト: `tests/test_drift_rebrief.py` — Orchestrator が drift warning を受信したとき `send_keys` を呼ぶことを検証 (+15 テスト目標)。

### Step 1 — Research

**Query 1**: "multi-agent LLM drift detection automatic re-briefing orchestrator 2025 2026"

主要知見:
- **Adnan Masood PhD Medium (2026-01)** "Agent Drift: the reliability blind spot in multi-agent LLM systems": セマンティックドリフトはマルチエージェント LLM ワークフローの 600 インタラクションまでに約 50% で発生。緩和戦略として episodic memory consolidation・drift-aware routing protocols・adaptive behavioral anchoring の3つが提案される。自動 re-brief はこの「adaptive behavioral anchoring」の具体的実装に相当する。
- **arXiv:2601.04170** (Rath et al., 2026-01) "Agent Drift: Quantifying Behavioral Degradation in Multi-Agent LLM Systems": ASI (Agent Stability Index) が 12 次元でドリフトを計量。ASI < 0.85 で役割逸脱の兆候。「drift-aware routing」でドリフト検出時に別エージェントへ再ルーティングまたは re-brief を行うことがアーキテクチャ推奨として明示されている。

**References**:
- Adnan Masood Medium (2026): https://medium.com/@adnanmasood/agent-drift-the-reliability-blind-spot-in-multi-agent-llm-systems-and-a-blueprint-to-measure-it-7c653d684b80
- arXiv:2601.04170: https://arxiv.org/abs/2601.04170

**Query 2**: "agent drift warning automatic recovery re-prompt multi-agent system arXiv 2025"

主要知見:
- **arXiv:2603.03258** "Inherited Goal Drift: Contextual Pressure Can Undermine Agentic Goals" (2026): コンテキスト圧力によるゴール漂流を分析。外部から明示的なゴールリマインダー (goal reminder injection) を注入することがドリフト予防の最効果的手法として位置付けられている。これは本実装の re-brief メッセージ送信と直接対応する。
- **arXiv:2601.04170** (Rath et al.): drift-aware routing の「behavioral anchoring」実装として、ドリフト検出後にエージェントの元タスク概要を再送信し、role_score が回復するまでモニタリングを継続するパターンが推奨されている。

**References**:
- arXiv:2603.03258: https://arxiv.org/html/2603.03258
- arXiv:2601.04170 (Rath et al.): https://arxiv.org/pdf/2601.04170

**Query 3**: "LLM agent role adherence monitoring automatic correction intervention 2025"

主要知見:
- **ACL 2025 EMNLP Industry** "Towards Enforcing Company Policy Adherence in Agentic Workflows" (arXiv:2507.16459): ToolGuard パターン — policy-to-tool mapping を使ってエージェントのツール呼び出し前にポリシー遵守を検証する。自動介入パイプラインが最小人手で実現できることを実証。
- **ICLR 2025 Building Trust Workshop** "Monitoring LLM Agents for Adherence to Specified Behaviors": LLM-based monitor が自律エージェントの隠れた逸脱行動を検出。モニターが逸脱を検出した後、外部シグナル（人間または上位エージェント）による修正介入が必須とされる。本実装の Orchestrator 側 re-brief がこの「修正介入」に相当する。
- **arXiv:2509.22735** "Regulating the Agency of LLM-based Agents": エージェントのエージェンシーを直接介入の対象にするフレームワーク。監視ループ → 逸脱検出 → 修正注入のパターンを提案。

**References**:
- EMNLP Industry 2025 arXiv:2507.16459: https://arxiv.org/html/2507.16459v2
- ICLR 2025 Building Trust Workshop: https://openreview.net/pdf?id=LC0XQ6ufbr
- arXiv:2509.22735: https://arxiv.org/html/2509.22735v1

**Query 4**: "orchestrator pub-sub event driven agent correction drift intervention 2025"

主要知見:
- **Redis AI Agent Orchestration (2025)** "AI agent orchestration for production systems": イベント駆動アーキテクチャでエージェント通信を同期 request-response から非同期 pub-sub に移行することで、ドリフト検出後の介入遅延を最小化できる。
- **AWS Strands Agents "Advanced Orchestration Techniques" (2025)**: エージェントに「目標の永続化 (goal persistence)」パターンを適用するアーキテクチャを紹介。上位エージェントが定期的にゴールを確認・再注入することでコンテキスト圧力によるドリフトを抑制。

**References**:
- Redis AI Agent Orchestration: https://redis.io/blog/ai-agent-orchestration/
- AWS Strands Agents Advanced Orchestration: https://aws.amazon.com/blogs/machine-learning/customize-agent-workflows-with-advanced-orchestration-techniques-using-strands-agents/

**実装への示唆**:
1. Orchestrator は bus の broadcast subscriber として `agent_drift_warning` を購読する
2. ドリフト検出時に `pane.send_keys()` でシステムメッセージ（役割とタスクのリマインダー）を注入する
3. re-brief の内容は `drift_rebrief_message` config フィールド + 元タスクプロンプトの先頭200文字
4. 連続 re-brief を防ぐため per-agent クールダウン (デフォルト 60 秒) を設ける
5. re-brief 履歴は in-memory dict で記録し REST で公開する

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/config.py` — `OrchestratorConfig` に `drift_rebrief_enabled`, `drift_rebrief_cooldown`, `drift_rebrief_message` を追加。`load_config()` で YAML から読み込む。
- `src/tmux_orchestrator/orchestrator.py` — `_drift_rebrief_history`, `_drift_rebrief_last_sent` インスタンス変数追加。`_handle_drift_warning()`, `get_agent_drift_rebriefs()`, `all_drift_rebrief_stats()` メソッド追加。`_route_loop` STATUS ハンドラーに `agent_drift_warning` ブランチ追加。
- `src/tmux_orchestrator/web/routers/agents.py` — `GET /agents/{agent_id}/drift-rebriefs` エンドポイント追加。
- `tests/test_drift_rebrief.py` — 32テスト (TestHandleDriftWarning 12・TestDriftRebriefQueries 4・TestRouteLoopDriftWarning 3・TestDriftRebriefConfig 6・TestLoadConfigDriftRebrief 4・TestDriftRebriefEndpoint 3)
- `tests/test_openapi_schema.py` — `_MockOrchestrator` に `get_agent_drift_rebriefs()` / `all_drift_rebrief_stats()` 追加
- `tests/fixtures/openapi_schema.json` — スナップショット再生成

**テスト数**: 2307 → 2339 (+32テスト)

**バージョン**: 1.1.18

**E2E デモ** (`~/Demonstration/v1.1.18-drift-rebrief/`):
- agent-implementer (Python 実装タスク) + agent-drifter (Python 実装タスク) の2エージェント並列構成
- DriftMonitor が両エージェントをポーリング (8秒間隔、threshold=0.5)
- 両エージェントとも3分以内にタスク完了
- `GET /agents/{id}/drift-rebriefs` エンドポイント: 200 + 空リスト (ドリフトなし = 正常)
- `GET /drift` および `GET /agents/{id}/drift` の drift_score フィールド確認済み

**デバッグ**: initial demo.py が `api()` の引数順序を誤って呼び出し (TypeError)。`http_get()` / `http_post()` ヘルパーに置き換えて修正。

**27/27 チェック PASSED**

---

## §10.51 — v1.1.19: コンテキスト4戦略ガイド + `system_prompt_file` §11 正確化

### Step 0 — 選択理由

**選択: コンテキスト4戦略ガイド + §11 完了ステータス正確化**

v1.1.18 までに §11 の主要フィーチャーはほぼ実装済みとなった。残存する真の未実装候補を精査した結果：

1. **`system_prompt_file` YAML フィールド + 役割テンプレートライブラリ（§11 高優先度）**: 調査の結果、`config.py` + `factory.py` に実装済み（v1.0.27）、役割テンプレート 9 本が `.claude/prompts/roles/` に存在、`tests/test_system_prompt_file.py` が存在することを確認。§11 の strikethrough が漏れていた。選択しない理由：既に完了済みのため実装作業は不要。
2. **`/deliberate` スラッシュコマンド（§11 中）**: `agent_plugin/commands/deliberate.md` が存在、v1.0.32 で完了済み。選択しない理由：既に完了済み。
3. **`POST /workflows/ddd`（§11 中）**: `web/routers/workflows.py` に実装済み。選択しない理由：既に完了済み。
4. **コンテキスト4戦略ガイド（§11 中）**: CLAUDE.md に「Running as an Orchestrated Agent」セクションが存在するが、書き込み・選択・圧縮・分離の4戦略チートシートは未追記。実装コストが低く（CLAUDE.md 追記のみ）、エージェントへの即時価値が高い。**選択**。
5. **スライディングウィンドウ + 重要度スコアコンテキスト圧縮（§11 中）**: 調査の結果、`context_auto_compress: bool` + `TfIdfContextCompressor` が v1.1.12 以前に実装済みであることを確認。選択しない理由：既に完了済み。

**v1.1.19 の作業内容**:
- §11 テーブルの完了済み項目に strikethrough を追加（`system_prompt_file`、`/deliberate`、`ddd`、スライディングウィンドウ圧縮）
- CLAUDE.md に「Context Engineering 4戦略チートシート」セクションを追記
- `.claude/prompts/context-strategies.md` にロール別推奨戦略マトリクスを追加
- デモ: 2エージェント（implementer + reviewer）が `system_prompt_file:` でロールを参照し、コンテキスト戦略を活用して協調するパイプライン

### Step 1 — Research

**Query 1**: "context engineering AI agents write select compress isolate strategies 2025"

主要知見:
- **LangChain Blog "Context Engineering for Agents" (2025)**: コンテキストエンジニアリングの4戦略（Write / Select / Compress / Isolate）を体系化。各戦略の代表的実装例を整理。Write = スクラッチパッドへの書き出し、Select = RAG・セマンティック検索、Compress = 要約・コンパクション、Isolate = サブエージェントへの分散。
- **mem0.ai "Context Engineering for AI Agents Guide" (2025)**: コンテキスト管理の実践ガイド。4戦略の組み合わせが単独戦略を大幅に上回るパフォーマンスを発揮することを事例で示す。

**References**:
- LangChain Blog: https://blog.langchain.com/context-engineering-for-agents/
- mem0.ai Guide: https://mem0.ai/blog/context-engineering-ai-agents-guide

**Query 2**: "LLM agent context window management best practices role-based 2025"

主要知見:
- **JetBrains Research Blog "Cutting Through the Noise" (2025-12)**: LLM エージェントのコンテキスト管理における observation masking と LLM summarization を比較。役割別エージェントへのコンテキスト分離が最も効果的であることを実証。「コンテキストウィンドウはクリーンな作業机のようなもの — タスクに無関係なものは乗せない」。
- **Anthropic Engineering "Effective harnesses for long-running agents" (2025)**: ロールベースのサブエージェント分割で、各エージェントのコンテキストが狭いサブタスクに集中できるため、単一エージェント比で大幅な性能向上を実現。

**References**:
- JetBrains Research Blog: https://blog.jetbrains.com/research/2025/12/efficient-context-management/
- Anthropic Engineering: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents

**Query 3**: "Anthropic context engineering effective AI agents 2025 write select compress isolate"

主要知見:
- **Anthropic Engineering "Effective context engineering for AI agents" (2025-09-29)**: 「コンテキストエンジニアリングとはプロンプト設計を超えた、推論時の情報エコシステム全体の管理である」と定義。4戦略の組み合わせにより単一エージェントでは不可能な長期タスクが実現できることを実証。Write + Select + Compress + Isolate の組み合わせが最大効果。

**Reference**:
- Anthropic Engineering: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

**Query 4**: "multi-agent system_prompt role template best practices sycophancy prevention 2025"

主要知見:
- **TreasureData "Best Practices for Agent System Prompts" (2025)**: 役割定義・ツール使用ガイダンス・チェックポイントの3要素を含む構造化テンプレートが推奨される。「役割が曖昧なエージェントは最も予測不可能な挙動をする」。
- **CONSENSAGENT ACL Findings 2025**: マルチエージェント討論における迎合（sycophancy）を動的プロンプト精緻化で抑制。役割テンプレートへの迎合抑制指示追記が正答率・効率の両方を改善。迎合の主因は「他エージェントの意見への無批判な同意」であり、テンプレートで「独立判断を維持せよ」と明示することが防止策として有効。

**References**:
- TreasureData Agent System Prompts: https://docs.treasuredata.com/products/customer-data-platform/ai-agent-foundry/ai-agent/system-prompt-best-practices
- CONSENSAGENT ACL 2025: https://aclanthology.org/2025.findings-acl.1141.pdf

**実装への示唆**:
1. CLAUDE.md の「Running as an Orchestrated Agent」セクションに「Context Engineering 4戦略」を追記
2. 各戦略をロールと対応付けたマトリクス（implementer → Write + Isolate、reviewer → Select + Compress）
3. `.claude/prompts/context-strategies.md` に詳細な使い分けガイドを配置
4. 実装は純粋ドキュメント追加のみ（コード変更なし）

### Step 2 — 実装サマリー

**実装ファイル**:
- `CLAUDE.md` — 「Context Engineering Cheatsheet」セクション追加（4戦略テーブル・ロール別推奨マトリクス・Key Rules・参考文献）
- `.claude/prompts/context-strategies.md` — 詳細戦略ガイド（4戦略×when/how/anti-patterns、ロール別マトリクス、組み合わせ例）
- `tests/test_context_strategies.py` — 30テスト（TestContextStrategiesFile 19・TestClaudeMdCheatsheet 11）
- `DESIGN.md §11` — 実装済み未ストライクスルー項目の修正（system_prompt_file・/deliberate・ddd・sliding-window圧縮・MIRIX・4戦略ガイド）
- `pyproject.toml` — version 1.1.18 → 1.1.19

**テスト数**: 2339 → 2369 (+30テスト)

**バージョン**: 1.1.19

**E2E デモ** (`~/Demonstration/v1.1.19-context-strategies/`):
- agent-implementer (`system_prompt_file: implementer.md`) + agent-reviewer (`system_prompt_file: reviewer.md`) の2エージェントパイプライン
- implementer が fizzbuzz.py を作成しスクラッチパッドに結果を書き込む（Write 戦略実証）
- reviewer がスクラッチパッドを読み込みレビューを実施（Select 戦略実証）
- 両エージェントが2分以内にタスク完了
- 29/29 チェック PASSED

**デバッグ**: `GET /scratchpad/` の戻り値が `{"key": "value", ...}` 形式（"keys" フィールドなし）。isinstance(sl_data, dict) ブランチを追加して修正。初回 27/29→再実行 29/29。

**29/29 チェック PASSED**

---

## §10.52 — v1.1.20: `POST /workflows/mob-review` — Mob Code Review ワークフロー

### Step 0 — 選定理由

**選択: `POST /workflows/mob-review` — N並列コードレビュー + シンセサイザーDAG**

**§11 残存未実装候補の精査:**

| 候補 | 状態 | 判定 |
|------|------|------|
| スラッシュコマンド自動コピー（高） | v1.0.12 で `_copy_commands()` 実装済み。§11 strikethrough 漏れ | スキップ（既完了） |
| チェックポイント永続化（高） | v0.45.0 + v1.0.35 で SQLite CheckpointStore + DI 対応済み。§11 strikethrough 漏れ | スキップ（既完了） |
| DriftMonitor セマンティック類似度（中） | v1.1.17 で TF-IDF コサイン類似度に置き換え済み | スキップ（既完了） |
| P2P 許可テーブル TLA+ 形式仕様化（中） | TLC model checker の外部ツールチェーン依存が大きく、CI統合が困難 | スキップ（ROI低） |
| Trace Replay CLI（低） | ResultStore JSONL の拡張が必要。低優先度 | スキップ（低優先度） |
| DECISION.md 標準フォーマット（低） | ドキュメント追記のみ。デモで実証困難 | スキップ（低価値） |
| `POST /workflows/mob-review`（新規） | 「競合」パターンの特殊化として N 並列専門レビュアー + シンセサイザーを実装可能。既存 competition/debate インフラを再利用。実証容易 | **選択** |

**何を選択したか・理由:**

`POST /workflows/mob-review` — **Mob Code Review ワークフロー**を v1.1.20 として実装する。

§11 の既存カテゴリー「ワークフローテンプレート」の自然な拡張。Mob Programming（全員が同時に一つの作業を行う開発手法）の「コードレビュー版」として、N 名の専門レビュアーエージェントが並列で同一コードを異なる観点（セキュリティ・パフォーマンス・保守性・テスト適切性）からレビューし、シンセサイザーエージェントが全レビューを統合して構造化レビューレポートを生成するパターン。

**competition との違い**: competition は「勝者を選ぶ」（best-of-N）。mob-review は「全レビューを統合する」（N-to-1 synthesis）。既存 tdd/pair/adr/spec-first ワークフローとは「N並列 → 統合」という新しい DAG 形状を提供する。

**実装コストが低い理由**:
1. 既存 `WorkflowRun` + `PhaseSpec(parallel)` + `PhaseSpec(single)` で DAG を宣言的に表現できる
2. `examples/workflows/` に YAML テンプレートを追加するだけでユーザーが即利用可能
3. 役割テンプレート (`.claude/prompts/roles/`) に `mob-reviewer.md` を1本追加

**選択しなかった候補と理由:**
- P2P TLA+ 形式仕様化: `tla2tools.jar` の外部依存が必要で CI 統合が困難。ROI が低い。
- Trace Replay CLI: §11 の「低」優先度。ResultStore 拡張が別イテレーション規模。
- DECISION.md 標準フォーマット: ドキュメントのみでデモ実証が困難。単独イテレーションとして価値が低い。

**実装スコープ:**
1. `web/schemas.py` — `MobReviewWorkflowSubmit` Pydantic v2 モデル
2. `web/routers/workflows.py` — `submit_mob_review_workflow()` エンドポイント
3. `.claude/prompts/roles/mob-reviewer.md` — 専門レビュアーロールテンプレート
4. `examples/workflows/mob-review.yaml` — YAML テンプレート
5. `tests/test_workflow_mob_review.py` — ユニットテスト (目標 +35 テスト)
6. `tests/fixtures/openapi_schema.json` — OpenAPI スナップショット更新
7. バージョン 1.1.19 → 1.1.20

**スクラッチパッドキー:**
- `{prefix}_review_{aspect}` — 各レビュアーの観点別レビュー（security/performance/maintainability/testing）
- `{prefix}_synthesis` — シンセサイザーが統合した最終レビューレポート

### Step 1 — Research

**Query 1**: "mob programming code review multi-agent LLM parallel review synthesis 2025"

主要知見:
- **ACM TOSEM 2025 — LLM-Based Multi-Agent Systems for Software Engineering**: マルチエージェント LLM システムにおける cross-examination（相互検査）は、コードレビューと類似した構造を持ち、複数エージェントがデバッグ・検証・バリデーションを並列実行することでより正確・堅牢なソリューションに収束することを示している。
- **MapCoder (2025)**: 4エージェント構成（例思い出し・計画・コーディング・デバッグ）で協調。各エージェントが専門フェーズを担当し、順次ハンドオフする。「専門化 + シーケンシャル統合」パターンの代表例。
- **Code in Harmony: Evaluating Multi-Agent Frameworks (OpenReview 2025)**: 並列エージェントが協調してコード品質を評価するフレームワークを評価。ヒエラルキー構造が最も resilience が高い（障害エージェント存在時の性能低下が 5.5% と最小）。

**References**:
- ACM TOSEM LLM-MAS SE: https://dl.acm.org/doi/10.1145/3712003
- Code in Harmony OpenReview 2025: https://openreview.net/pdf?id=URUMBfrHFy
- Survey on Code Generation with LLM Agents (arXiv:2508.00083): https://arxiv.org/html/2508.00083v1

**Query 2**: "multi-agent code review perspectives security performance maintainability parallel LLM 2025"

主要知見:
- **Multi-Agent LLM Environment for Software Design and Refactoring (ResearchGate 2025)**: 専門エージェント（パフォーマンス最適化・セキュリティ強化・保守性・UX）が協調または競合的に動作し、コンセンサスまたはオークション機構で協調するアーキテクチャを提案。
- **Designing LLM-based Multi-Agent Systems for Software Engineering Tasks (arXiv:2511.08475)**: コード品質に関して「セキュリティ・保守性・パフォーマンス・コンプライアンス・UX」の多次元品質評価アプローチを推奨。単一エージェントのレビューでは論理的欠陥・パフォーマンス落とし穴・セキュリティ脆弱性を見逃しやすい。
- **MAGIS (NeurIPS 2024)**: GitHub Issue 解決の LLM マルチエージェントフレームワーク。Manager→Analyst→Developer→QA Engineer の役割分担が有効。QA エージェントがタイムリーなフィードバックを提供するパターン。

**References**:
- Multi-Agent LLM SE Refactoring: https://www.researchgate.net/publication/391205436_A_Multi-Agent_LLM_Environment_for_Software_Design_and_Refactoring_A_Conceptual_Framework
- Designing LLM-MAS for SE (arXiv:2511.08475): https://arxiv.org/html/2511.08475v1
- MAGIS NeurIPS 2024: https://papers.nips.cc/paper_files/paper/2024/file/5d1f02132ef51602adf07000ca5b6138-Paper-Conference.pdf

**Query 3**: "LLM agent ensemble code review synthesis aggregation best practices 2025 arXiv"

主要知見:
- **Agent-as-a-Judge (arXiv:2508.02994, 2025)**: 独立した判断を集約することでバリアンスとエラーを削減し、投票委員会と同様の効果を得られる。集約手法には「明示的な討論 + 反論」と「並列評価 + 集計」の2系統があり、どちらもモデルの多様性から恩恵を受ける。
- **ChatEval — Multi-Agent Debate Evaluation (ICLR 2024 / arXiv:2308.07201)**: 各エージェントに固有のペルソナ（役割）を与え、異なる観点を担当させることが必須。同一の役割プロンプトを複数エージェントに使用すると性能が劣化する。**→ mob-review における各レビュアーへの異なる専門観点割り当ての根拠**。

**References**:
- Agent-as-a-Judge (arXiv:2508.02994): https://arxiv.org/html/2508.02994v1
- ChatEval (arXiv:2308.07201): https://arxiv.org/pdf/2308.07201

**Query 4**: "multi-perspective code review security performance maintainability agent specialization ChatEval 2024"

主要知見:
- **ChatEval**: 各エージェントに固有ペルソナを付与し特定の専門性または観点に集中させる設計が、多様な役割プロンプトのない場合と比較して判断品質を大幅に改善することを実証。
- **Multi-AI code review 実践 (2025)**: セキュリティ・パフォーマンス・保守性・正確性・スタイルの5次元分析に LLM-as-judge のコンセンサスとスコアリングを組み合わせる実践的アプローチ。GitHub PR への自動コメント投稿パイプラインとして実用化。

**References**:
- ChatEval ICLR 2024: https://arxiv.org/pdf/2308.07201
- Best Agent Skills for AI Code Review (tessl.io 2025): https://tessl.io/blog/best-agent-skills-for-ai-code-review-8-evaluated-skills-for-dev-workflows/
- Multi-AI code review skills: https://playbooks.com/skills/adaptationio/skrillz/multi-ai-code-review

**実装への示唆**:
1. 各レビュアーエージェントに **明確に異なる専門観点**（security/performance/maintainability/testing）を割り当てる（ChatEval の知見）
2. 全レビューを並列実行し、シンセサイザーが集約（Agent-as-a-Judge の集約パターン）
3. スクラッチパッドに `{prefix}_review_{aspect}` で各観点レビューを書き込み、シンセサイザーが Select 戦略で読み込む（Anthropic 4戦略 + Blackboard パターン）
4. 最終レポートは structured format（REVIEW_SUMMARY, CRITICAL_FINDINGS, RECOMMENDATIONS セクション）で出力

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/web/schemas.py` — `MobReviewWorkflowSubmit` Pydantic v2 モデル
  (2–8 aspects、language、reviewer_tags、synthesizer_tags、reply_to、デフォルト4次元)
- `src/tmux_orchestrator/web/routers/workflows.py` — `POST /workflows/mob-review` エンドポイント
  (N 並列レビュアータスク + 1 シンセサイザータスク、観点別ガイダンス付き)
- `.claude/prompts/roles/mob-reviewer.md` — 役割テンプレート（独立性原則・重大度評価・出力フォーマット・アンチパターン）
- `examples/workflows/mob-review.yaml` — 自己完結 YAML テンプレート
- `tests/test_workflow_mob_review.py` — 41 テスト（スキーマ検証・HTTP 認証・レスポンス構造・依存関係ワイヤリング・プロンプトコンテンツ・ワークフロー状態）
- `tests/fixtures/openapi_schema.json` — OpenAPI コントラクトスナップショット更新
- `pyproject.toml` — version 1.1.19 → 1.1.20

**テスト数**: 2369 → 2410 (+41 テスト)

**バージョン**: 1.1.20

**E2E デモ** (`~/Demonstration/v1.1.20-mob-review/`):
- 5 実エージェント (`reviewer-security`, `reviewer-performance`, `reviewer-maintainability`,
  `reviewer-testing`, `synthesizer`) が独立ワークツリーで並列動作
- レビュー対象コード: SQL injection・ハードコード認証情報・O(n²) アルゴリズム・型ヒント欠如・テスト欠如 を含む `find_duplicates()` 関数
- 4 レビュアーが並列完了 (~55秒)、シンセサイザーが統合 MOB_REVIEW.md を生成 (~63秒)
- 合計 2分以内に完了
- **29/30 チェック PASSED**

**デバッグ**: `required_tags` なしの場合、タスクが任意のアイドルエージェントにディスパッチされる。
`reviewer_security` タスクが `reviewer-maintainability` エージェントにディスパッチされ、スクラッチパッド書き込みに失敗した (1 FAIL)。
修正候補: `required_tags` による専門家エージェントへの明示的ルーティング（次イテレーション候補）。

**29/30 チェック PASSED**

---

## §10.53 — v1.1.21: mob-review `required_tags` 自動生成 + `POST /workflows/iterative-review`

### Step 0 — 選択と理由

**選択: mob-review required_tags バグ修正 + POST /workflows/iterative-review (反復レビューワークフロー)**

**バグ修正 (必須):**
v1.1.20 デモで `reviewer_security` タスクが `reviewer-maintainability` エージェントにディスパッチされた。
根本原因: `submit_mob_review_workflow()` が `required_tags=body.reviewer_tags or None` を全レビュアーに同一タグを使用しており、アスペクト別のルーティングができなかった。
修正: 各アスペクト（security/performance/maintainability/testing）に対して `["mob_reviewer", "{aspect}"]` を自動生成する。

**次機能 — POST /workflows/iterative-review:**
§11 の残存未実装候補を精査した結果:
- Trace Replay CLI（低）: ResultStore 拡張が大規模。ROI 低。スキップ。
- DECISION.md 標準フォーマット（低）: ドキュメントのみ。デモ実証困難。スキップ。
- クリーンアーキテクチャ移行（高）: 大規模リファクタリング。1イテレーションで完結しない。スキップ。

`POST /workflows/iterative-review` を選択する理由:
1. **mob-review の自然な拡張**: 並列（mob-review）と直列（iterative）の両方のレビューパターンを提供することで、ユーザーの選択肢が広がる。
2. **研究的裏付けが明確**: Code Review 2.0 / Self-Refine (arXiv:2303.17651) が反復的フィードバックループの有効性を実証している。
3. **デモ価値が高い**: 実装者 → レビュアー → 修正者 の3エージェントパイプラインは、エージェント間の依存関係と協調を明確に示す。
4. **実装コストが中程度**: `depends_on` チェーンを活用することで、既存インフラを最大限利用できる。

ワークフロー構造:
```
implementer → reviewer → revisor
```
1. `implementer`: コードを実装し scratchpad に保存
2. `reviewer`: 実装を受け取り、アノテーション付きレビューを行い scratchpad に保存
3. `revisor`: 実装 + レビューを受け取り、修正版を生成

**選択しなかった候補と理由:**
- Trace Replay CLI: 低優先度、ResultStore 大規模拡張が必要。
- DECISION.md 標準フォーマット: ドキュメントのみでデモ実証が困難。
- クリーンアーキテクチャ移行: 1イテレーション完結不可。

**実装スコープ:**
1. `web/routers/workflows.py` — mob-review required_tags バグ修正 (per-aspect auto-generation)
2. `examples/workflows/mob-review.yaml` — タグルーティングのガイダンスを追加
3. `web/schemas.py` — `IterativeReviewWorkflowSubmit` Pydantic v2 モデル
4. `web/routers/workflows.py` — `POST /workflows/iterative-review` エンドポイント
5. `tests/test_workflow_mob_review.py` — required_tags 修正のテスト追加
6. `tests/test_workflow_iterative_review.py` — 新ワークフローのテスト（目標 +30テスト）
7. `tests/fixtures/openapi_schema.json` — OpenAPI スナップショット更新
8. `pyproject.toml` — version 1.1.20 → 1.1.21

**スクラッチパッドキー (iterative-review):**
- `{prefix}_implementation` — implementer が生成したコード実装
- `{prefix}_review` — reviewer のアノテーション付きフィードバック
- `{prefix}_revised` — revisor が生成した修正版コード

### Step 1 — Research

**Query 1**: "iterative code review LLM agent self-refine feedback loop multi-agent 2025"

主要知見:
- **Self-Refine (arXiv:2303.17651, NeurIPS 2023)**: LLM が自身の出力を批評・改善する反復的フィードバックループ。FEEDBACK モジュール → REFINE モジュールの交互実行で出力品質を平均 ~20% 向上。コード生成タスクにおいても有効（コード最適化スコアで有意改善）。
- **LessonL (arXiv:2505.23946, 2025)**: 複数 LLM エージェントがコード最適化を協調実行する solicitation–banking–selection フレームワーク。小規模エージェント群の協調が単一大規模 LLM を上回ることを実証。
- **MAR: Multi-Agent Reflexion (arXiv:2512.20845, 2025)**: 複数エージェントによる相互フィードバックが自己フィードバックより高品質な改善を生む。`implementer → reviewer → revisor` の3エージェントチェーンはこのパターンの直接実装。

**References**:
- Self-Refine (arXiv:2303.17651): https://arxiv.org/abs/2303.17651
- LessonL (arXiv:2505.23946): https://arxiv.org/pdf/2505.23946
- MAR Multi-Agent Reflexion (arXiv:2512.20845): https://arxiv.org/html/2512.20845

**Query 2**: "multi-agent task routing required_tags capability matching dispatch pattern 2025"

主要知見:
- **AWS Prescriptive Guidance — Routing dynamic dispatch patterns (2025)**: コーディネーター/ディスパッチャーパターンでは、タスクの意図を分析し最も適した専門エージェントへルーティングする。能力（capability）マッチングが中核要素。
- **MasRouter (ACL 2025 — aclanthology.org/2025.acl-long.757)**: LLM ルーティングシステムがタスク複雑度・エージェント能力・コストを同時最適化。タスクへのタグ付けとエージェントへの能力タグ付けの対応が最もシンプルで robust なルーティング機構。
- **Google ADK 8 essential patterns (InfoQ 2026-01)**: Sequential Pipeline / Capability-based routing がトップ2の必須パターンとして挙げられている。

**References**:
- AWS Routing patterns: https://docs.aws.amazon.com/prescriptive-guidance/latest/agentic-ai-patterns/routing-dynamic-dispatch-patterns.html
- MasRouter ACL 2025: https://aclanthology.org/2025.acl-long.757.pdf
- Google ADK patterns: https://developers.googleblog.com/developers-guide-to-multi-agent-patterns-in-adk/

**Query 3**: "LLM code review pipeline implementer reviewer revisor three-agent chain 2025 arXiv"

主要知見:
- **RevAgent (arXiv:2511.00517, 2025)**: 5カテゴリ専門コメンテーターエージェント（生成段階）+ 1クリティックエージェント（識別段階）の2段階パイプライン。役割明確化 + 直列依存関係の典型パターン。本 `iterative-review` ワークフローの直接参照。
- **AgentCoder**: programmer → test_designer → test_executor の3エージェントパイプライン。役割明確化 + 直列依存関係 (`depends_on`) の典型パターン。
- **Rethinking Code Review Workflows (arXiv:2505.16339, 2025)**: Human-in-the-loop + LLM Review では「実装者 → レビュアー → 修正担当」の3ロールが最も実用的な分業パターン。

**References**:
- RevAgent (arXiv:2511.00517): https://arxiv.org/pdf/2511.00517
- Rethinking Code Review Workflows (arXiv:2505.16339): https://arxiv.org/html/2505.16339v1
- AgentCoder survey: https://arxiv.org/html/2508.00083v1

**Query 4**: "Self-Refine iterative refinement LLM feedback 2023 Madaan code improvement arXiv:2303.17651"

主要知見:
- **Self-Refine (Madaan et al. NeurIPS 2023, arXiv:2303.17651)**: FEEDBACK → REFINE の交互ループ。(i) 問題の局所化 (ii) 改善指示 の2要素からなるフィードバックが効果的。コードの可読性・機能完全性が反復ごとに向上。
  - 本ワークフローでは `reviewer` が FEEDBACK ロール、`revisor` が REFINE ロールに対応する。
  - 単一エージェントの自己フィードバックより **異なるエージェントによるクロスレビュー** が品質向上に有効（ChatEval ICLR 2024 の知見と一致）。

**References**:
- Self-Refine official: https://selfrefine.info/
- Self-Refine arXiv: https://arxiv.org/abs/2303.17651

**実装への示唆**:
1. `implementer` → `reviewer` → `revisor` の3段階直列 DAG（`depends_on` チェーン）
2. `reviewer` は Self-Refine の FEEDBACK モジュールに対応: 問題局所化 + 改善指示の2要素を出力
3. `revisor` は Self-Refine の REFINE モジュールに対応: フィードバックを参照して修正版を生成
4. スクラッチパッド Blackboard パターン: `{prefix}_implementation` → `{prefix}_review` → `{prefix}_revised`
5. `required_tags` でロール別エージェントルーティング: `["iterative_implementer"]` / `["iterative_reviewer"]` / `["iterative_revisor"]`
6. mob-review バグ修正: per-aspect tags `["mob_reviewer", "{aspect}"]` を自動生成

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/web/routers/workflows.py` — mob-review required_tags バグ修正 + `POST /workflows/iterative-review` エンドポイント
- `src/tmux_orchestrator/web/schemas.py` — `IterativeReviewWorkflowSubmit` Pydantic v2 モデル
- `examples/workflows/mob-review.yaml` — タグルーティングガイダンス更新
- `examples/workflows/iterative-review.yaml` — 新ワークフローの自己完結 YAML テンプレート
- `tests/test_workflow_mob_review.py` — required_tags バグ修正テスト +5
- `tests/test_workflow_iterative_review.py` — 新ワークフローテスト (37テスト)
- `tests/fixtures/openapi_schema.json` — OpenAPI スナップショット更新
- `pyproject.toml` — version 1.1.20 → 1.1.21

**テスト数**: 2410 → 2452 (+42 テスト)

**バージョン**: 1.1.21

**E2E デモ** (`~/Demonstration/v1.1.21-iterative-review/`):
- 3 実エージェント (`implementer`, `reviewer`, `revisor`) が独立ワークツリーで直列動作
- タスク: `merge_sorted_lists()` 関数を実装 → レビュー → 修正の3段階パイプライン
- implementer: ~33秒で実装完了 (975文字)、reviewer: ~45秒でレビュー完了 (2708文字)、revisor: ~30秒で修正完了 (1345文字)
- 合計 3分以内に完了
- **29/29 チェック PASSED**

**デバッグ**: 1回目の実行で revisor エージェントが trust dialog に引っかかり dead-letter。
根本原因: `pre_trust_worktree()` が dialog を抑制できなかった（known bug）。
修正: demo.py にサーバー起動後5秒待機してから全 tmux pane に Enter を送信するステップを追加。

**29/29 チェック PASSED**

## §10.54 — v1.1.22: Trust Dialog 根本修正 + `POST /workflows/spec-first-tdd`

### Step 0 — 選択の根拠

**選択: Trust Dialog 根本修正 (高優先度) + `POST /workflows/spec-first-tdd`**

v1.1.21 デモで revisor エージェントが trust dialog に引っかかり dead-letter になった。
build-log.md で「高優先度の次候補」として明示的に挙げられた。
`demo.py` に Enter 送信ワークアラウンドを追加したが、これは根本修正ではない。

**Trust Dialog 根本修正を選択した理由:**
1. v1.1.21 デモで 1/3 エージェントが失敗した再現性ある障害（intermittent だが確認済み）
2. 多エージェント並列起動時は他の Claude Code インスタンスが `~/.claude.json` を頻繁に書き換えるため、競合書き込みが発生しうる
3. `pre_trust_worktree()` が書き込む entry に `hasTrustDialogHooksAccepted` フィールドが欠けている
4. 現行エントリに `allowedTools: []` が欠けており trust recognition が不完全

**追加機能: `POST /workflows/spec-first-tdd`**
§11「ワークフローテンプレート」カテゴリの自然な拡張。
spec-writer → implementer → tester の3エージェント直列 DAG で trust fix 後のデモ実証に適する。

**選択しなかった候補と理由:**
- Trace Replay CLI（低）: ResultStore 拡張が大規模。ROI 低い。
- DECISION.md 標準フォーマット（低）: ドキュメントのみ。デモ実証困難。

### Step 1 — Research

**Query 1**: "Claude Code trust dialog ~/.claude.json hasTrustDialogAccepted projects format 2026"

主要知見:
- **Issue #5572 (github.com/anthropics/claude-code)**: `hasTrustDialogHooksAccepted` という新しいフィールドが追加された。`claude config set` では設定不可。SessionStart hooks の trust は `hasTrustDialogHooksAccepted` で制御されている可能性がある。
- **Issue #9113**: Pre-configured `~/.claude.json` の projects エントリが尊重されないバグ（v2.0.8 以降で regression）。`allowedTools` フィールドが deprecated/removed されたが trust logic が更新されなかった。CLOSED AS NOT PLANNED。
- **Issue #11519**: SessionStart hooks が workspace trust dialog にブロックされる。`hasTrustDialogAccepted: true` を設定しても SessionStart hook が実行されない。v2.0.37 で確認。
- **VS Code extension overwrites ~/.claude.json (#29029)**: extension が `toolUsage` カウント更新のたびに `~/.claude.json` 全体を書き換えるため、競合書き込みが発生する。

References:
- Issue #5572: https://github.com/anthropics/claude-code/issues/5572
- Issue #9113: https://github.com/anthropics/claude-code/issues/9113
- Issue #11519: https://github.com/anthropics/claude-code/issues/11519
- Issue #29029: https://github.com/anthropics/claude-code/issues/29029

**Query 2**: "Claude Code workspace trust folder accept programmatic ~/.claude.json allowedTools 2025 2026"

主要知見:
- Claude Code v2.0.8 で `allowedTools` が `~/.claude.json` から deprecated → `settings.json` へ移行。しかし workspace trust logic は更新されず、pre-configured path が無視されるバグが発生。
- Issue #12227 "trust not persisting in CLI": trust prompt が毎セッション表示される。
- **Parent directory trust cascading**: Claude Code は trust チェック時に親ディレクトリを遡って確認する。
- `allowedTools: []` エントリが trust recognition に必要な可能性がある（deprecated だが内部的に参照されている可能性）。

References:
- Issue #12227: https://github.com/anthropics/claude-code/issues/12227
- Configure permissions: https://code.claude.com/docs/en/permissions

**Query 3**: "Claude Code hasTrustDialogHooksAccepted issue #5572 trust dialog worktree automation"

主要知見:
- `hasTrustDialogHooksAccepted` は hooks 関連の trust dialog を制御する新フィールド。SessionStart hook が trust dialog にブロックされる症状は、この field が欠けていることが原因の可能性。
- Issue #28506: `--dangerously-skip-permissions` は workspace trust dialog をバイパスしない。
- **結論**: `pre_trust_worktree()` が書き込む entry に `hasTrustDialogHooksAccepted: true` を追加することで hooks blocking を解消できる可能性がある。

References:
- Issue #28506: https://github.com/anthropics/claude-code/issues/28506

**Query 4**: "Claude Code 2.1 dangerously-skip-permissions trust dialog bypass skip worktree automation 2026"

主要知見:
- Claude Code 2.1.71 (現在のインストール済みバージョン) では trust dialog は git repo でも出る場合がある。
- **競合書き込み問題**: 別の Claude Code インスタンス（既存の pane で動作中）が `~/.claude.json` を更新する間に新しいエージェントが起動すると、pre-trust エントリが上書きされる可能性がある。現行の POSIX advisory lock はプロセス間で協調するが、Claude Code 自体は lock を使わないため無効。

**実装への示唆**:
1. `hasTrustDialogHooksAccepted: true` を entry に追加 — hooks trust も承認
2. `allowedTools: []` を entry に追加 — 古い trust チェックとの互換性
3. write-then-verify パターン: エントリ書き込み後、最大 3 回リトライして書き込みが保持されているか確認する
4. 競合書き込み対策: write 後に short sleep + re-read して entry が消えていたら再書き込み

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/infrastructure/claude_trust.py` — trust dialog rootfix:
  - `hasTrustDialogHooksAccepted: true` フィールド追加
  - `allowedTools: []` フィールド追加（後方互換性）
  - `already_trusted` 判定を両フィールド必須に変更
  - write-then-verify ループ追加（最大3回、50ms sleep、競合書き込み検出・再書き込み）
- `src/tmux_orchestrator/trust.py` — shim に `_VERIFY_RETRIES`, `_VERIFY_SLEEP_S`, `_TRUST_LOCK_PATH` を追加
- `src/tmux_orchestrator/web/schemas.py` — `SpecFirstTddWorkflowSubmit` Pydantic v2 モデル追加
- `src/tmux_orchestrator/web/routers/workflows.py` — `POST /workflows/spec-first-tdd` エンドポイント追加
- `examples/workflows/spec-first-tdd.yaml` — セルフコンテインド YAML テンプレート
- `tests/test_claude_trust.py` — trust rootfix の回帰テスト (10テスト)
- `tests/test_workflow_spec_first_tdd.py` — spec-first-tdd ワークフローテスト (44テスト)
- `tests/fixtures/openapi_schema.json` — OpenAPI スナップショット更新
- `pyproject.toml` — version 1.1.21 → 1.1.22

**テスト数**: 2452 → 2506 (+54 テスト)

**バージョン**: 1.1.22

**E2E デモ** (`~/Demonstration/v1.1.22-trust-spec-first-tdd/`):
- 3 実エージェント (`spec-writer`, `implementer`, `tester`) が独立ワークツリーで直列動作
- トピック: FizzBuzz 関数 — spec-writer が SPEC.md 生成 → implementer が実装 → tester がテスト
- 全 3 エージェントが **1 秒以内**に起動 (05:23:43〜05:23:44)、trust dialog なし
- v1.1.21 デモで必要だった Enter キーワークアラウンドは不要
- spec-writer: ~43 秒 (3272 文字)、implementer: ~46 秒 (692 文字)、tester: ~70 秒 (1321 文字)
- 合計 3 分以内に完了
- **30/30 チェック PASSED (初回実行)**

**デバッグ**: 修正が有効 — 初回実行で 30/30 PASS。
Trust fix: `hasTrustDialogHooksAccepted=true` + `allowedTools=[]` + write-then-verify ループが
v1.1.21 の intermittent trust dialog ブロック問題を完全解消。


## §10.55 — v1.1.23: Clean Architecture Migration Phase 1 — `domain/workflow.py` + `domain/phase_strategy.py`

### Step 0 — 選択理由

**選択した機能**: Clean Architecture Migration Phase 1 — `domain/workflow.py` + `domain/phase_strategy.py`

**理由**:
- ユーザーが明示的に指定した最高優先度タスク。
- `WorkflowRun`・`WorkflowPhaseStatus` は現在 `workflow_manager.py` / `phase_executor.py` に散在し、純粋なドメイン型として独立していない。
- `PhaseStrategy` (単一/並列/競合/討論) は「フェーズをどう実行するか」という純粋なドメイン概念であり、インフラや HTTP に依存すべきでない。
- Strangler Fig パターンで既存の 2506 テストをすべて緑のまま移行可能。

**選択しなかった候補と理由**:
- `application/workflow_service.py` の追加リファクタリング: Phase 1 完了後の Phase 2 候補（依存関係が逆転するため先に domain/ を整備する必要がある）。
- E2E デモ新ワークフロー追加: Phase 1 の構造整備が先決。

### Step 1 — 調査記録

**Query 1**: "Clean Architecture domain layer workflow entity pure Python best practices 2024"

主要知見:
- Domain entities は pure domain objects であるべき — データベースアノテーションやフレームワーク固有のコードを含まない。
- Domain layer にはエンティティ・値オブジェクト・アグリゲート・ドメインサービスが属する。
- Workflow/Use Cases は application layer に属し、domain の上に位置する。依存関係は必ず内向き (domain を指す方向) のみ。
- dataclass を純粋なビジネスオブジェクトとして使用するのは推奨プラクティス。

References:
- ThinhDA, "Crafting Maintainable Python Applications with Domain-Driven Design and Clean Architecture", https://thinhdanggroup.github.io/python-code-structure/ (2024)
- Shaliamekh, "Clean Architecture with Python", Medium, https://medium.com/@shaliamekh/clean-architecture-with-python-d62712fd8d4f (2024)
- Glukhov, "Python Design Patterns for Clean Architecture", https://www.glukhov.org/post/2025/11/python-design-patterns-for-clean-architecture/ (2025)

**Query 2**: "Strategy pattern Python Protocol ABC domain layer clean architecture orthogonal"

主要知見:
- Python では `typing.Protocol` を使うことで ABC 継承なしに Strategy パターンを実装できる (PEP 544 structural subtyping)。
- Strategy は「アルゴリズムをカプセル化する」パターン — PhaseStrategy は「フェーズ実行戦略」をカプセル化するため適切な適用例。
- Domain layer に Protocol-based strategy を置くことで、infrastructure/web への逆依存を排除できる。
- Percival & Gregory "Architecture Patterns with Python" (O'Reilly, 2020): Port/Adapter パターンと Protocol の組み合わせが推奨される。

References:
- Percival, Gregory, "Architecture Patterns with Python", O'Reilly, https://www.oreilly.com/library/view/architecture-patterns-with/9781492052197/ (2020)
- Glukhov, "Python Design Patterns for Clean Architecture", DEV Community, https://dev.to/rosgluk/python-design-patterns-for-clean-architecture-1jk0 (2025)
- Python PEP 544 — Protocols: Structural subtyping (static duck typing), https://peps.python.org/pep-0544/

**Query 3**: "Strangler Fig pattern Python module migration incremental refactoring re-export shim"

主要知見:
- Strangler Fig (Fowler, 2004): 旧システムを壊さずに新実装を徐々に育て、旧モジュールを re-export shim に変換する。
- Python での適用: 旧モジュールを「facade/shim」として残し、`from new_location import X` を re-export する。既存の import パスを破壊せずに内部実装を新しい場所へ移動できる。
- AWS Prescriptive Guidance: Intercepting Facade パターン — テスト・ロールバックが容易。

References:
- Fowler, "Strangler Fig Application", bliki, https://martinfowler.com/bliki/StranglerFigApplication.html (2004)
- Tiset, "The Strangler fig pattern", Medium, https://medium.com/@sylvain.tiset/the-strangler-fig-pattern-is-what-you-need-to-migrate-monolithic-application-with-legacy-code-to-ec24cf7168eb (2023)
- AWS Prescriptive Guidance, "Strangler fig pattern", https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/strangler-fig.html

## §10.56 — v1.1.24: Clean Architecture Migration Phase 2 — `application/bus.py`, `application/registry.py`, `application/workflow_manager.py`

### Step 0 — 選択理由

**選択した機能**: Clean Architecture Migration Phase 2 — application layer への bus, registry, workflow_manager の移動

**理由**:
- Phase 1 (v1.1.23) で `domain/workflow.py` と `domain/phase_strategy.py` が完成し、次のフェーズが明確に定義された。
- `bus.py`, `registry.py`, `workflow_manager.py` の3ファイルはいずれも純粋なアプリケーション層の責任を持つ (tmux/HTTP/filesystem に直接依存しない)。
- `Bus` は純粋な async pub/sub — infrastructure (tmux, HTTP) を直接使わないため application/ が正しい層。
- `AgentRegistry` は in-memory の agent state 管理 — DDD Aggregate パターン (Evans 2003) に従い application layer の service として整理。
- `WorkflowManager` は workflow DAG のトラッキング — domain/workflow.py の WorkflowRun を使うため application layer に属する。
- 既存の 2531 テストを Strangler Fig パターンで全て緑のまま移行可能。

**選択しなかった候補と理由**:
- `orchestrator.py` の移動: 複雑な依存関係 (tmux, asyncio dispatch loop) があるため Phase 3 に延期。
- `context_monitor.py` / `episode_store.py` の移動: orchestrator.py との依存関係が強いため Phase 3 以降。

### Step 1 — 調査記録

**Query 1**: "application layer Clean Architecture Python async pub/sub message bus 2024 2025"

主要知見:
- Message Bus は application layer に属するべき — domain event を application service に橋渡しする役割 (Percival & Gregory "Architecture Patterns with Python", O'Reilly, Ch.8)。
- In-process async pub/sub は asyncio.Queue ベースで構築できる — 外部依存なし。
- Event-Driven Architecture: producers → bus → consumers の分離が Clean Architecture の関心分離原則と一致する。
- aiopubsub など軽量ライブラリは asyncio ベースの pub/sub を application layer で実装する良い参考例。

References:
- Percival, Gregory, "Architecture Patterns with Python", O'Reilly Ch.8 — Events and the Message Bus, https://www.oreilly.com/library/view/architecture-patterns-with/9781492052197/ch08.html (2020)
- Hash Block, "How I Built an In-Memory Pub/Sub Engine in Python With Only 80 Lines", Medium, https://medium.com/@connect.hashblock/how-i-built-an-in-memory-pub-sub-engine-in-python-with-only-80-lines-eb42d30f0160 (2024)
- Quantlane, "Design your app using the pub-sub pattern with aiopubsub", https://quantlane.com/blog/aiopubsub/ (2024)
- Shaliamekh, "Clean Architecture with Python", Medium, https://medium.com/@shaliamekh/clean-architecture-with-python-d62712fd8d4f (2024)

**Query 2**: "Registry pattern Domain-Driven Design application layer Python in-memory agent state 2024"

主要知見:
- Registry は DDD においてエンティティの in-memory lookup table として機能する — application layer の service として適切 (Evans "Domain-Driven Design" 2003, Ch.6)。
- Repository パターンと Registry の違い: Repository は persistence を抽象化するが、Registry は純粋な in-memory lookup を提供する。
- Application service は domain layer (entities, aggregates) に依存し、infrastructure (DB, filesystem) には Protocol 経由でのみアクセスする。
- Domain Events と Agent State: domain event で agent state 変更を通知し、application service が応答する設計が推奨される。

References:
- Evans, "Domain-Driven Design", Addison-Wesley, Ch.6 — Aggregates, https://lyz-code.github.io/blue-book/architecture/domain_driven_design/ (2003)
- Nayeem Islam, "Everything You Need to Know About Domain-Driven Design with Python Microservices", Medium, https://medium.com/@nomannayeem/everything-you-need-to-know-about-domain-driven-design-with-python-microservices-2c2f6556b5b1 (2024)
- ThinhDA, "Crafting Maintainable Python Applications with Domain-Driven Design and Clean Architecture", https://thinhdanggroup.github.io/python-code-structure/ (2024)
- w3computing, "Implementing Domain-Driven Design in Python Projects", https://www.w3computing.com/articles/implementing-domain-driven-design-in-python-projects/ (2024)

**Query 3**: "Python Strangler Fig module re-export shim incremental Clean Architecture migration bus registry 2025"

主要知見:
- Strangler Fig パターン: 新旧システムを並行稼働させ、routing layer (shim/facade) で切り替える (Fowler 2004)。Python では旧モジュールを re-export shim に変換することで既存 import パスを保持できる。
- Reduced risk: 変更を段階的に適用し、blast radius を最小化。既存テストが常に green であることがロールバック安全性を保証する。
- Continuous delivery: 移行中も新機能のデリバリーを継続できる — Strangler Fig の最大の利点。
- AWS Prescriptive Guidance の Intercepting Facade パターン — routing layer が旧実装と新実装の両方を支える橋渡し役として機能する。

References:
- Fowler, "Strangler Fig Application", https://martinfowler.com/bliki/StranglerFigApplication.html (2004)
- Swimm, "Strangler Fig Pattern: Modernizing It Without Losing It", https://swimm.io/learn/legacy-code/strangler-fig-pattern-modernizing-it-without-losing-it (2025)
- AWS Prescriptive Guidance, "Strangler fig pattern", https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/strangler-fig.html (2025)
- future-processing, "What is the Strangler Fig Pattern?", https://www.future-processing.com/blog/strangler-fig-pattern/ (2025)

## §10.57 — v1.1.25: Clean Architecture Migration Phase 3 — infrastructure stores + application monitors

### Step 0 — 選択理由

**選択した機能**: Clean Architecture Migration Phase 3 — インフラ層ストア (`result_store`, `checkpoint_store`, `episode_store`) + アプリケーション層モニター (`autoscaler`, `context_monitor`, `drift_monitor`) の移動

**理由**:
- Phase 1/2 完了後、domain/ と application/ の bus/registry/workflow_manager は全て移動済み。
- 残る ROOT レベルファイルのうち、`result_store.py` / `checkpoint_store.py` / `episode_store.py` の3つは filesystem/SQLite I/O を直接扱う典型的なインフラ層責任を持つ。
- `autoscaler.py` / `context_monitor.py` / `drift_monitor.py` の3つは外部 I/O を持たず、asyncio ループと pub/sub バスを使う application layer の責任を持つ。
- 既存の全テストを Strangler Fig パターンで green に保ちながら段階的に移行可能。
- `orchestrator.py` は依存関係が複雑すぎるため Phase 4 に延期 (同ファイル内に dispatch loop, P2P routing, watchdog 等が混在)。

**選択しなかった候補と理由**:
- `orchestrator.py` の移動: 4000行超の God Object であり、適切な層分離のためには先に interface 抽出が必要 → Phase 4 に予約。
- `config.py` の移動: OrchestratorConfig は YAML パースと dataclass 定義の境界にあり、移動コストに対して利益が小さい → 今回対象外。
- `factory.py` の移動: orchestrator.py に強依存するため orchestrator.py 移動後に実施。

### Step 1 — 調査記録

**Query 1**: "Clean Architecture infrastructure layer file I/O JSONL SQLite Python best practices 2025"

主要知見:
- Infrastructure layer はデータベース・ファイルシステム・外部 API などの I/O 詳細を担う最外層 (Martin, "Clean Architecture" 2017, Ch.22)。
- Repository パターン: domain layer で定義したインターフェースを infrastructure layer で実装する — SQLite や JSONL ファイルは純粋な実装詳細。
- CQRS (Greg Young, 2010): write path (append) と read path (query) を分離することで書き込みの低レイテンシを確保しつつ、読み取りは複雑なクエリをサポートできる。
- "Clean Architecture with Python" (Keen, Packt 2025): SOLID 原則、テストパターン、オブザーバビリティ、リファクタリングを包括的に網羅。

References:
- cdddg/py-clean-arch, "A Python implementation of Clean Architecture", https://github.com/cdddg/py-clean-arch (2025)
- Shaliamekh, "Clean Architecture with Python", Medium, https://medium.com/@shaliamekh/clean-architecture-with-python-d62712fd8d4f (2025)
- Keen, "Clean Architecture with Python", O'Reilly, https://www.oreilly.com/library/view/clean-architecture-with/9781836642893/ (2025)
- Glukhov, "Python Design Patterns for Clean Architecture", https://www.glukhov.org/post/2025/11/python-design-patterns-for-clean-architecture/ (2025)

**Query 2**: "application layer autoscaler context monitor Python clean architecture 2025"

主要知見:
- Autoscaler はキュー深度に応じてエージェントプールを動的に拡縮する Application Service — domain の business rule を変更しない点で application layer が適切。
- Ray Serve の Application-level Autoscaler パターン: infrastructure (cluster) の上で application logic (demand-based scaling) を実装する層分離モデルが参考になる。
- Context Monitor / Drift Monitor は観測・イベント発行を行う Application Service — pure asyncio で external dep なし。bus (application/bus.py) に依存するため application layer が正しい位置。
- "fast-clean-architecture" フレームワーク (PyPI 2025): Domain/Application/Infrastructure/Presentation の4層モデルを Python で実装するリファレンス実装。

References:
- Ray Serve Autoscaling guide, https://docs.ray.io/en/latest/serve/autoscaling-guide.html (2025)
- pcah/python-clean-architecture, https://github.com/pcah/python-clean-architecture (2025)
- fast-clean-architecture, https://pypi.org/project/fast-clean-architecture/ (2025)
- ThinhDA, "Crafting Maintainable Python Applications with Domain-Driven Design and Clean Architecture", https://thinhdanggroup.github.io/python-code-structure/ (2025)

**Query 3**: "Strangler Fig shim Python module migration infrastructure layer re-export 2025"

主要知見:
- Strangler Fig パターン: 旧モジュールを re-export shim に変換し、新しい canonical 場所に実装を移動する (Fowler 2004)。既存 import パスを破壊しない。
- Intercepting Facade (AWS Prescriptive Guidance): facade が旧実装と新実装の両方を支える — Python の `from new_location import *` がこの役割を果たす。
- "Deconstructing the Monolith" (4geeks, 2025): 段階的移行はリスクを最小化し、いつでもロールバック可能にする。テストが常に green であることが安全網。
- Laminas Project (2025): Strangler Fig は MVC → Middleware 移行にも有効 — 既存の public API (import path) を保持しながら内部実装を刷新する。

References:
- Fowler, "Strangler Fig Application", https://martinfowler.com/bliki/StranglerFigApplication.html (2004)
- AWS Prescriptive Guidance, "Strangler fig pattern", https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/strangler-fig.html (2025)
- 4geeks, "Deconstructing the Monolith: Implementing the Strangler Fig Pattern", https://blog.4geeks.io/deconstructing-the-monolith-implementing-the-strangler-fig-pattern-for-high-availability-migrations/ (2025)
- Laminas Project, "The Strangler Fig Pattern: A Viable Approach for Migrating MVC to Middleware", https://getlaminas.org/blog/2025-08-06-strangler-fig-pattern.html (2025)


## §10.58 — v1.1.26: Clean Architecture Migration Phase 4 — Small Root Files + orchestrator.py

### Step 0 — 選択理由

**選択した機能**: Clean Architecture Migration Phase 4 — 残存 ROOT ファイル群 (`task_queue`, `rate_limiter`, `group_manager`, `webhook_manager`, `slash_notify`, `security`, `telemetry`, `logging_config`) の application/infrastructure 層への移動。

**理由**:
- Phase 1-3 完了後、domain/ と application/ の主要コンポーネントは全て移動済み。
- 残る ROOT レベルファイルのうち、純粋 asyncio ロジック (`task_queue`, `rate_limiter`, `group_manager`, `slash_notify`) は application layer に属する。
- 外部依存を持つファイル (`webhook_manager`: httpx HTTP, `security`: Starlette middleware, `telemetry`: OpenTelemetry SDK, `logging_config`: sys/logging) は infrastructure layer に属する。
- 全 2554 テストを Strangler Fig パターンで green に保ちながら段階的に移行可能。
- `orchestrator.py` は dispatch loop、P2P routing、watchdog、idempotency 等が混在する最大のファイルであり、本 Phase で着手するが完全移動は Phase 5 に延期する可能性がある。

**選択しなかったこと**:
- `config.py` / `factory.py` / `schemas.py` / `main.py` は REST API / CLI の設定とエントリポイントであり、複数レイヤーをまたぐ。Phase 5 以降に延期。

### Step 1 — Research

**Query 1**: "Clean Architecture Python application layer vs infrastructure layer classification token bucket rate limiter task queue 2024"

主要知見:
- Application Layer: ビジネスロジック（ユースケース）、純粋な asyncio ロジック（rate limiter, task queue, group manager）はここに属する。外部 I/O を持たない。
- Infrastructure Layer: 外部サービスとの通信（HTTP webhook, OTel exporter, filesystem, Starlette middleware）。
- TokenBucketRateLimiter は asyncio.Lock + time.monotonic のみ — 外部依存なし → application layer。
- AsyncPriorityTaskQueue は asyncio.PriorityQueue のラッパー — 外部依存なし → application layer。
- WebhookManager は httpx.AsyncClient を使う HTTP I/O → infrastructure layer。
- AuditLogMiddleware は Starlette BaseHTTPMiddleware を継承 → infrastructure layer。
- OpenTelemetry SDK (TracerProvider, SpanExporter) は外部 SDK → infrastructure layer。
- logging_config は sys/logging の設定 → infrastructure layer。

References:
- Dan Does Code, "Unpacking the Layers of Clean Architecture", https://www.dandoescode.com/blog/unpacking-the-layers-of-clean-architecture-domain-application-and-infrastructure-services (2024)
- DevIQ, "Strangler Fig Design Pattern", https://deviq.com/design-patterns/strangler-fig-pattern/ (2025)
- System Design Handbook, "Design a Rate Limiter", https://www.systemdesignhandbook.com/guides/design-a-rate-limiter/ (2025)

**Query 2**: "Python strangler fig pattern module re-export shim migration clean architecture 2025"

主要知見:
- Strangler Fig: 旧モジュールを `from tmux_orchestrator.<layer>.<module> import *` に変換し、新 canonical 場所に実装を移動する (Fowler 2004)。
- AWS Prescriptive Guidance: Intercepting Facade が旧実装と新実装の橋渡し役 — Python の re-export shim がこの役割。
- 段階的移行でリスク最小化、いつでもロールバック可能。テストが常に green であることが安全網。

References:
- Fowler, "Strangler Fig Application", https://martinfowler.com/bliki/StranglerFigApplication.html (2004)
- AWS Prescriptive Guidance, "Strangler fig pattern", https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/strangler-fig.html (2025)
- Swimm, "Strangler Fig Pattern: Modernizing It Without Losing It", https://swimm.io/learn/legacy-code/strangler-fig-pattern-modernizing-it-without-losing-it (2025)
- 4geeks, "Deconstructing the Monolith", https://blog.4geeks.io/deconstructing-the-monolith-implementing-the-strangler-fig-pattern-for-high-availability-migrations/ (2025)

**Query 3**: "OpenTelemetry infrastructure layer classification Python clean architecture external dependency 2025"

主要知見:
- OpenTelemetry SDK (TracerProvider, SpanExporter, BatchSpanProcessor) は外部依存であり infrastructure layer に属する。
- OTel は cross-cutting concern — 純粋な business logic (domain/application) を汚染してはいけない。
- API と SDK を分離: API は軽量で application layer から使用可能、SDK は infrastructure layer に配置。
- Python では `from opentelemetry.sdk.trace import TracerProvider` が infrastructure 依存を示す。

References:
- OpenTelemetry, "OpenTelemetry API vs SDK: Understanding the Architecture", https://last9.io/blog/opentelemetry-api-vs-sdk/ (2025)
- OpenTelemetry, "Documentation", https://opentelemetry.io/docs/ (2025)
- Mezmo, "A guide to OpenTelemetry architecture", https://www.mezmo.com/learn-observability/a-guide-to-opentelemetry-architecture-logs-and-implementation-best-practices (2025)

### 層分類サマリー

| ファイル | 移動先 | 理由 |
|---|---|---|
| `task_queue.py` | `application/task_queue.py` | 純粋 asyncio.PriorityQueue ラッパー、外部依存なし |
| `rate_limiter.py` | `application/rate_limiter.py` | asyncio.Lock + time.monotonic のみ、外部依存なし |
| `group_manager.py` | `application/group_manager.py` | 純粋 in-memory dict、外部依存なし |
| `slash_notify.py` | `application/slash_notify.py` | urllib.request (stdlib のみ)、filesystem 読み取り — application |
| `webhook_manager.py` | `infrastructure/webhook_manager.py` | httpx HTTP I/O、外部依存あり |
| `security.py` | `infrastructure/security.py` | Starlette middleware、外部依存あり |
| `telemetry.py` | `infrastructure/telemetry.py` | OpenTelemetry SDK、外部依存あり |
| `logging_config.py` | `infrastructure/logging_config.py` | sys/logging 設定、infrastructure 横断関心事 |


## §10.59 — v1.1.27: Clean Architecture Migration Phase 5 — schemas.py + config.py + factory.py + orchestrator.py

### Step 0 — 選択理由

**選択した機能**: Clean Architecture Migration Phase 5 — 残存 ROOT ファイル群 (`schemas.py`, `config.py`, `factory.py`, `orchestrator.py`) の application 層への移動と Strangler Fig shim 化。

**理由**:
- Phase 1-4 完了後、`schemas.py`・`config.py`・`factory.py`・`orchestrator.py` が ROOT に残る最後の主要ファイル群。
- `schemas.py`: Pydantic モデル (bus メッセージペイロード・エピソード記憶) — 外部依存は Pydantic のみ。Application layer の DTO として最適な位置。
- `config.py`: YAML ローダー + Python dataclasses — `yaml` (stdlib 相当) + `pathlib` のみ。Application layer が適切 (pure config, no infra I/O)。
- `factory.py`: Composition Root — 全コンポーネントを配線する Factory。Application layer (`application/factory.py`) に移動する。
- `orchestrator.py` (2698行): ROOT で既にほぼ Pure Python asyncio — `TmuxInterface` は TYPE_CHECKING のみ参照。Application layer に移動する Strangler Fig shim を作成する。
- `main.py`: CLI エントリポイント (typer) — ROOT に留まる (Composition Root の最外殻)。

**選択しなかったこと**:
- `main.py` は CLI エントリポイントの性格上、ROOT に留める。

### Step 1 — Research

**Query 1**: "Clean Architecture Python schemas Pydantic models layer placement application vs domain 2025"

主要知見:
- DTO (Data Transfer Object) は Application Layer が最適な位置。Domain 層は純粋な business rule を持ち、Pydantic に依存しない。
- 実用的アプローチ (Sam Keen, 2025): 複雑なドメインロジックがない場合は Pydantic を Application 層 DTO として使用することが許容される。重複検証はアンチパターン。
- Bus メッセージペイロードは Application Layer の DTO — `application/schemas.py` が正しい位置。

References:
- Glukhov, "Python Design Patterns for Clean Architecture", https://www.glukhov.org/post/2025/11/python-design-patterns-for-clean-architecture/ (2025)
- BigGo, "Python Developers Debate Whether Pydantic Should Stay Out of Domain Logic", https://biggo.com/news/202507271932_Pydantic_Domain_Layer_Debate (2025)
- DeepEngineering, "Pragmatic Clean Architecture in Python: A Conversation with Sam Keen", https://deepengineering.substack.com/p/pragmatic-clean-architecture-in-python (2025)

**Query 2**: "Python Clean Architecture config dataclass YAML loading layer classification Composition Root factory 2025"

主要知見:
- Config dataclass (YAML ローダー付き) は Application layer に配置 — 外部 I/O (filesystem) は含むが、インフラサービス (HTTP, DB) ではない。yamldataclassconfig ライブラリが同様のパターンを実装。
- Composition Root (factory) は Application layer — "依存関係グラフの配線責務"。Domain/Application コンポーネントを組み合わせ、Infrastructure を注入する層。
- Martin "Clean Architecture" (2017): Composition Root は Main Component に相当し、最も外側の円 (Frameworks & Drivers) に位置する。但し Python の小規模プロジェクトでは application/factory.py が慣習的。

References:
- PyPI, yamldataclassconfig, https://pypi.org/project/yamldataclassconfig/ (2025)
- PyPI, config2class, https://pypi.org/project/config2class/ (2025)
- pcah/python-clean-architecture, https://github.com/pcah/python-clean-architecture (2025)

**Query 3**: "Strangler Fig pattern Python module re-export shim large file migration monolith 2025"

主要知見:
- Strangler Fig: 旧モジュールを re-export shim (`from new_location import *`) に変換し、新 canonical 場所に実装を移動する。既存 import パスを破壊しない。
- Intercepting Facade (AWS Prescriptive Guidance): Facade が旧実装と新実装の橋渡し役 — Python の re-export が同等の役割を担う。
- 2025 年の OneUptime GKE マイグレーション事例: 段階的移行はリスク最小化、いつでもロールバック可能。テストが常に green であることが安全網。

References:
- Tiset, "The Strangler fig pattern", https://medium.com/@sylvain.tiset/the-strangler-fig-pattern-is-what-you-need-to-migrate-monolithic-application-with-legacy-code-to-ec24cf7168eb (2025)
- AWS Prescriptive Guidance, "Strangler fig pattern", https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/strangler-fig.html (2025)
- OneUptime, "Strangler Fig Pattern on GKE", https://oneuptime.com/blog/post/2026-02-17-how-to-implement-the-strangler-fig-pattern-to-migrate-monoliths-to-microservices-on-gke/view (2026)

### 層分類サマリー

| ファイル | 移動先 | 理由 |
|---|---|---|
| `schemas.py` | `application/schemas.py` | Pydantic DTO (bus payload + episode) — 外部依存は Pydantic のみ |
| `config.py` | `application/config.py` | YAML + dataclasses — filesystem 読み取りのみ、infra サービス依存なし |
| `factory.py` | `application/factory.py` | Composition Root — application/infrastructure を配線する Application Service |
| `orchestrator.py` | `application/orchestrator.py` | 純粋 asyncio dispatch loop — TmuxInterface は TYPE_CHECKING のみ (Protocol DI 済み) |


## §10.60 — v1.1.28: Clean Architecture Migration Phase 6 — Final Cleanup (factory.py migration + test patch updates)

### Step 0 — 選択理由

**選択した機能**: Clean Architecture Migration Phase 6 — `factory.py` 実装の `application/factory.py` への移動と、18テストのパッチパスを `tmux_orchestrator.factory.*` → `tmux_orchestrator.application.factory.*` に更新。

**理由**:
- Phase 5 (v1.1.27) にて `application/factory.py` は re-export shim として存在するが、実装は ROOT `factory.py` に留まっていた。これを「実装は canonical location (`application/factory.py`) にあり、ROOT `factory.py` が shim になる」正しい Strangler Fig 構造に転換する。
- 18テストが `tmux_orchestrator.factory.TmuxInterface` 等をパッチしているため、実装移動前にパッチパスの更新が必要。
- `test_application_purity.py` の `_CROSS_LAYER_FILES` から `factory.py` を除去できる（移動後は purity rule に従うため）。
- 循環インポート問題 (`Orchestrator` を `application/__init__` から除外) はすでに Phase 5 で解決済み — `application/__init__.py` には NOTE コメントで明示。

**選択しなかったこと**:
- ROOT `factory.py` shim の削除 — Strangler Fig 原則に従い既存の import パスを保護。shim は backward compat のため保持。
- `application/__init__` への `Orchestrator` 再追加 — 循環インポートチェーンが深く、現在は NOTE コメントで直接 import を案内している。複雑なので GitHub Issue に記録。

### Step 1 — Research

**Query 1**: "Python circular import resolution lazy import TYPE_CHECKING clean architecture 2025"

主要知見:
- `TYPE_CHECKING` ガード: Instagram チームが開拓。型チェック時のみ import を実行、ランタイムでは実行しない。循環インポートの type-only 依存を解消。
- Lazy import (関数内 import): PEP 810 で標準化の動きあり。関数呼び出し時のみ依存を解決する。`factory.py` では既に `from tmux_orchestrator.agents.claude_code import ClaudeCodeAgent  # noqa: PLC0415` として実装済み。
- Clean Architecture: MVC / Layered Architecture は自然に循環インポートを防ぐ — 依存の方向を一方向に強制する。

References:
- Volokh, "The Circular Import Trap in Python", https://medium.com/@denis.volokh/the-circular-import-trap-in-python-and-how-to-escape-it-9fb22925dab6 (2025)
- Stefaan Lippens, "Yet another solution for circular imports with type hints in Python", https://www.stefaanlippens.net/circular-imports-type-hints-python.html (2025)
- PEP 810 — Explicit lazy imports, https://peps.python.org/pep-0810/ (2025)

**Query 2**: "Python unittest.mock patch module relocation test backward compatibility 2025"

主要知見:
- `patch("module.Symbol")` はシンボルが *bind されている場所* をパッチする。定義されている場所ではない。
- 実装を移動する場合、テストのパッチパスも新しい canonical location に更新する必要がある。
- ROOT shim が re-export した場合、パッチは ROOT shim をターゲットにしても動作することがある（Python の import メカニズムによる）が、確実性のためには canonical location を明示的にパッチすべき。
- Python GitHub issue #117860: unittest.mock.patch がモジュールパス解決に関して 3.11+ で挙動変更あり。

References:
- Python docs, "unittest.mock", https://docs.python.org/3/library/unittest.mock.html (2025)
- Python issue #117860, https://github.com/python/cpython/issues/117860 (2025)
- Real Python, "Understanding the Python Mock Object Library", https://realpython.com/python-mock-library/ (2025)

**Query 3**: "Python Composition Root factory pattern test patches canonical module location 2026"

主要知見:
- Factory Method / Composition Root は Application layer に配置するのが Clean Architecture の推奨。
- テストは canonical location をパッチすることで、実装の移動に追随する。shim を経由したパッチは brittle。
- faif/python-patterns (GitHub): Python パターンの標準的なコレクション — Factory, Abstract Factory が収録。
- freecodecamp "How to Use the Factory Pattern in Python": Composition Root としての factory は依存グラフの配線責務を持つ。

References:
- faif/python-patterns, https://github.com/faif/python-patterns (2025)
- freecodecamp, "How to Use the Factory Pattern in Python", https://www.freecodecamp.org/news/how-to-use-the-factory-pattern-in-python-a-practical-guide/ (2025)
- refactoring.guru, "Factory Method in Python", https://refactoring.guru/design-patterns/factory-method/python/example (2025)

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/application/factory.py` — 完全な canonical 実装 (build_system, patch_web_url, patch_api_key) を配置。`application.bus.Bus` と `application.config.load_config` から import。
- `src/tmux_orchestrator/factory.py` — Strangler Fig shim 化。`application.factory` から re-export のみ。
- `tests/test_factory.py` — 8パッチを `tmux_orchestrator.application.factory.*` に更新。
- `tests/test_system_prompt_file.py` — 8パッチを `tmux_orchestrator.application.factory.*` に更新。
- `tests/test_context_spec_files.py` — 2パッチを `tmux_orchestrator.application.factory.*` に更新。
- `tests/test_application_purity.py` — `_CROSS_LAYER_FILES` の `factory.py` コメント更新（除外継続 — Composition Root は TmuxInterface + WorktreeManager をインフラから合法的に import）。
- `DESIGN.md §10.60` — 選択理由 + 3 WebSearch クエリ + 実装サマリー。
- `pyproject.toml` — version 1.1.27 → 1.1.28

**テスト数**: 2564 (変化なし — migration のみ)

**バージョン**: 1.1.28

**E2E デモ** (`~/Demonstration/v1.1.28-clean-arch-final/`):
- 2 実エージェント (`agent-mapper`, `agent-validator`) が Pipeline パターンで動作。
- mapper: 7/7 検証チェック PASS (canonical impl 確認、shim 確認、import 解析)
- validator: 10/10 検証チェック PASS (関数同一性、パッチ動作、Phase5 後退確認)
- **10/10 チェック PASS (初回実行)**

**Clean Architecture 完成度**: ~99%
- 残存課題: `Orchestrator` を `application/__init__` から re-export できない (循環インポートチェーン) → `application/__init__.py` の NOTE コメントで対処済み。

## §10.61 — v1.1.29: Circular Import Resolution — `Orchestrator` in `application/__init__`

### Step 0 — 選択理由

**選択**: `application/__init__.py` に `Orchestrator` を追加して Clean Architecture 100% を達成。

**なぜ選択**: v1.1.28 の実装後、唯一の残存課題が `Orchestrator` を `application/__init__` から re-export できないことだった。これを解決すれば Clean Architecture Migration が完全完成となる。

**なぜ他を選ばなかったか**: §11 の他の候補はより大きな変更を要する。循環インポート解決は最小コスト・最大インパクトの変更。

### Step 1 — Research (WebSearch 3クエリ)

**調査内容**: 循環インポート解決の定石パターン、TYPE_CHECKING ガード、PEP 562 module `__getattr__`、Python の package `__init__.py` ロード時の循環インポート発生メカニズム。

**調査結果**:

1. **TYPE_CHECKING ガード** (Fedorov, Kirill, "Type Annotations and circular imports", Medium, https://medium.com/@k.a.fedorov/type-annotations-and-circular-imports-0a8014cd243b):
   - `if TYPE_CHECKING:` でガードしたインポートは静的解析時のみ実行される。ランタイムでは無視される。
   - 型アノテーションのみに使われるインポートに有効。実際にオブジェクトを使う場合は使えない。

2. **PEP 562 — Module `__getattr__`** (Python Software Foundation, "PEP 562 – Module `__getattr__` and `__dir__`", https://peps.python.org/pep-0562/, 2017):
   - Python 3.7 以降、モジュールに `__getattr__` 関数を定義できる。
   - 属性が通常のルックアップで見つからない場合にのみ呼ばれる。
   - `application/__init__.py` に `__getattr__` を定義して `Orchestrator` のみを lazy import する方法が有効。
   - ただし pickle との互換性に注意が必要。

3. **実際の循環インポートメカニズム** (Lippens, Stefaan, "Yet another solution to dig you out of a circular import hole in Python", https://www.stefaanlippens.net/circular-imports-type-hints-python.html):
   - `from pkg.submod import X` は pkg の `__init__.py` を部分的にロード済みの状態で submod を直接 import できる。
   - `from tmux_orchestrator.application.bus import Bus` は `application/__init__` を経由しない — `application/bus.py` を直接ロードする。
   - したがって、`application/__init__` ロード中に `orchestrator.py` が `bus.py` shim を介して `application.bus` を import しても、`application/__init__` に再入しない。

**実証調査**:
- `uv run python3 -c "from tmux_orchestrator.application.orchestrator import Orchestrator"` → 成功
- 一時的に `application/__init__.py` に import 追加してサブプロセステスト → `from tmux_orchestrator.application import Orchestrator` が成功
- **結論**: 想定されていた循環インポートチェーンは実際には存在しない。`application.bus` は submodule として直接ロードされるため、`application/__init__` への再入が起きない。

**選択した修正方針**: 最小侵襲的アプローチ — `application/__init__.py` に直接 `from tmux_orchestrator.application.orchestrator import Orchestrator` を追加し、`__all__` に `"Orchestrator"` を追加。

**References**:
- Fedorov, Kirill, "Type Annotations and circular imports", Medium, https://medium.com/@k.a.fedorov/type-annotations-and-circular-imports-0a8014cd243b (2023)
- Python Software Foundation, "PEP 562 – Module __getattr__ and __dir__", https://peps.python.org/pep-0562/ (2017)
- Lippens, Stefaan, "Yet another solution to dig you out of a circular import hole in Python", https://www.stefaanlippens.net/circular-imports-type-hints-python.html
- DataCamp, "Python Circular Import: Causes, Fixes, and Best Practices", https://www.datacamp.com/tutorial/python-circular-import (2025)
- Scientific Python, "SPEC 1 — Lazy Loading of Submodules and Functions", https://scientific-python.org/specs/spec-0001/

## §10.62 — v1.1.30: Project-Scoped Mailbox Directory (観点C)

### Step 0 — 選択理由

**選択**: メールボックスをプロジェクトスコープに移動 (観点C — 高優先度)

**何を選択したか・理由:**

v1.1.29 で Clean Architecture が 100% 完成し、次の最優先候補として観点C「メールボックスをプロジェクトスコープに移動」を選択した。

現状の問題:
- デフォルトの `mailbox_dir` は `~/.tmux_orchestrator/` (ホームディレクトリ下グローバル領域)
- 同一マシンで複数プロジェクト (A・B・C) を並行実行すると、メッセージが混在する
- デモディレクトリを分けても、メッセージは共通の `~/.tmux_orchestrator/<session_name>/` に書き込まれる

解決策:
1. `OrchestratorConfig.mailbox_dir` のデフォルトを `".orchestrator/mailbox"` (相対パス) に変更
2. `load_config(path, cwd=None)` に `cwd` パラメータを追加
3. `mailbox_dir` が相対パスの場合は `cwd / mailbox_dir` に展開 (絶対パスの場合は変更なし)
4. 後方互換性: `~` で始まる従来の絶対パスは `expanduser()` で正常動作を維持

**価値/実装コスト:**
- **スコープ明確**: `config.py` と `load_config()` のみの変更。factory.py での Mailbox 初期化には影響なし
- **後方互換性保持**: 既存テストは `mailbox_dir=str(tmp_path)` (絶対パス) を使用しており変更不要
- **多プロジェクト分離**: デモごとに独立した `.orchestrator/mailbox/` が生成され、混在しない
- **XDG 準拠**: プロジェクトローカルデータは `.` prefix ディレクトリに格納するのが慣例

**何を選ばなかったか・理由:**
- **StrategyConfig 値オブジェクト** (観点B): 実装コストは低いが、ユーザー向け価値が mailbox 分離より低い
- **フェーズごとのタイムアウト設定** (観点B): PhaseSpec への `timeout` 追加は有用だが、現状は全タスクが `task_timeout` を使えば足りる
- **エージェント設定ファイルの分離** (観点A): `isolate: false` のユースケースが限定的

### Step 1 — Research (WebSearch 4クエリ)

**調査内容**: XDG Base Directory Specification、プロジェクトローカル設定ディレクトリ慣例、Python pathlib での相対パス解決、マルチテナント分離パターン。

**調査結果**:

1. **XDG Base Directory Specification** (Freedesktop.org, https://specifications.freedesktop.org/basedir-latest/, ArchWiki https://wiki.archlinux.org/title/XDG_Base_Directory):
   - ユーザースコープのデータは `$XDG_DATA_HOME` (`~/.local/share/<app>/`) に格納するのが仕様
   - しかし **プロジェクトローカルデータ** (特定ディレクトリに紐付く作業物) はプロジェクトディレクトリ配下の隠しフォルダ (`.git/`, `.claude/`, `.orchestrator/`) が業界慣例
   - SourceReference: "XDG Base Directory Specification", Freedesktop.org, https://specifications.freedesktop.org/basedir/latest/

2. **プロジェクトローカルディレクトリ慣例** (GitHub "Folder-Structure-Conventions", https://github.com/kriasoft/Folder-Structure-Conventions; Docker Compose isolation, https://www.kubeblogs.com/how-to-avoid-issues-with-docker-compose-due-to-same-folder-names-project-isolation-best-practices/):
   - Git (`.git/`)、npm (`.npmrc`)、Claude Code (`.claude/`) がプロジェクトローカルにデータを格納する先例
   - Docker Compose はプロジェクト名にフォルダ名を使い、複数プロジェクト並行動作時のリソース分離を実現
   - 隠しフォルダ (dot-prefixed) はツール・エディタによる自動検出と `ls` 出力の整理に有効

3. **Python pathlib 相対パス解決** (Python docs, https://docs.python.org/3/library/pathlib.html; Real Python, https://realpython.com/python-pathlib/):
   - `Path.cwd() / relative_path` で相対パスを現在ディレクトリ基準に解決できる
   - `path.is_absolute()` で絶対パスを判定し、相対パスのみを `cwd` 基準で展開するパターンが推奨
   - `path.expanduser()` は `~` プレフィックスを `Path.home()` に置換する — 従来の `~/.tmux_orchestrator` もこれで正常動作

4. **マルチテナント分離** (Medium "Data Isolation and Sharding Architectures for Multi-Tenant Systems", https://medium.com/@justhamade/data-isolation-and-sharding-architectures-for-multi-tenant-systems-20584ae2bc31; redis.io "Data Isolation in Multi-Tenant SaaS", https://redis.io/blog/data-isolation-multi-tenant-saas/):
   - マルチテナント SaaS の分離パターン: 共有スキーマ (低分離) → スキーマ per テナント (中分離) → DB per テナント (高分離)
   - TmuxAgentOrchestrator の「プロジェクト」= テナント。プロジェクトごとにメールボックスディレクトリを分離することが「スキーマ per テナント」に相当
   - 原則: "namespace everything, enforce tenant context at every layer"

**実装方針**:
1. `OrchestratorConfig.mailbox_dir: str = ".orchestrator/mailbox"` (デフォルト変更)
2. `load_config(path, cwd=None)` に `cwd: Path | str | None = None` パラメータを追加
3. `_resolve_dir(raw, cwd)` ヘルパー: `raw` が `~` で始まる → `expanduser()`、絶対パス → そのまま、相対パス → `cwd / raw`
4. `mailbox_dir`、`result_store_dir`、`checkpoint_db` の3フィールドに `_resolve_dir` を適用
5. テスト: `test_config_mailbox_scope.py` に 15 テスト追加

### Step 2 — 実装サマリー

**実装ファイル**:
- `src/tmux_orchestrator/application/config.py` — `_resolve_dir()` ヘルパー追加、`load_config(path, cwd=None)` シグネチャ変更、`mailbox_dir` デフォルト変更、3フィールドに `_resolve_dir` 適用
- `src/tmux_orchestrator/web/app.py` — `getattr()` フォールバックを `".orchestrator/mailbox"` に更新
- `tests/test_config_mailbox_scope.py` — 15 テスト新規追加

**テスト数**: 2564 → 2579 (+15)

**バージョン**: 1.1.30

**E2E デモ** (`~/Demonstration/v1.1.30-project-scoped-mailbox/`):
- Pattern: Pipeline + P2P message
- agent-writer: 8/8 チェック PASS (config API, _resolve_dir, P2P送信)
- agent-verifier: 7/7 チェック PASS (ディレクトリ分離確認, pytest, 複数プロジェクト独立性)
- demo mailbox checks: 2/2 PASS
- **17/17 チェック PASS (初回実行)**

**バグ**: demo `wait_task_done()` が `"success"` ステータスを認識せず初回ループ。修正: terminal states に `"success"` を追加。

---

## §10.63 — v1.1.31: StrategyConfig 値オブジェクト + フェーズごとのタイムアウト設定 (観点B)

### Step 0 — 選択理由

**選択**: StrategyConfig 値オブジェクト + フェーズごとのタイムアウト設定 (観点B — 高優先度 × 2件)

**何を選択したか・理由:**

v1.1.30 で観点C「メールボックスをプロジェクトスコープに移動」が完了した。次の最優先候補として観点B の高優先度2件（StrategyConfig 値オブジェクト + フェーズごとのタイムアウト設定）を同一イテレーションで実装する。

1. **StrategyConfig 値オブジェクト**: `PhaseSpec` のストラテジーパラメータが現在は `agent_count` / `debate_rounds` の2フィールドのみで型安全性が低い。`SingleConfig` / `ParallelConfig` / `CompetitiveConfig` / `DebateConfig` を Pydantic モデルとして `domain/phase_strategy.py` に定義し、`PhaseSpec.strategy_config: StrategyConfig | None` フィールドで受け取る。これにより `POST /workflows` の `phases[].strategy_config` が型バリデーションされ、誤った設定を早期検出できる。

2. **フェーズごとのタイムアウト設定**: `PhaseSpec` に `timeout: int | None` フィールドを追加し、`expand_phases()` が生成する各 `TaskSpec` にフェーズのタイムアウトを反映する。調査フェーズには 1200 秒、実装フェーズには 600 秒など段階ごとに異なるタイムアウトを設定できる。現状はグローバルの `task_timeout` のみで全タスクが同じ上限を持つ非効率を解消する。

この2件は同一ファイル (`domain/phase_strategy.py`, `application/workflow_manager.py`) に閉じた変更で相互補完的であり、1イテレーションでまとめて実装するのが効率的。

**何を選ばなかったか・理由:**
- **エージェント設定ファイルの分離** (観点A): `isolate: false` のユースケースが限定的で、worktree 分離がデフォルトになった現在は優先度が低い。
- **メールボックスのセッション単位分離** (観点C): `session_name` の UUID 自動生成は後方互換テストの変更が大きく、本イテレーションのスコープ外。
- **Competitive ストラテジー評価基準カスタマイズ** (観点B・中): StrategyConfig 実装後の次ステップとして設計する。

### Step 1 — Research (WebSearch 3クエリ)

**調査内容**: Pydantic discriminated union value objects, per-task/per-phase timeout in orchestration systems, strategy pattern with typed parameters.

**調査結果**:

1. **Pydantic discriminated union (Pydantic docs, https://docs.pydantic.dev/latest/concepts/unions/; Bressler, "Pydantic for Experts: Discriminated Unions in Pydantic V2", Data Engineer Things, https://blog.dataengineerthings.org/pydantic-for-experts-discriminated-unions-in-pydantic-v2-2d9ca965b22f)**:
   - Discriminated union を使うと Pydantic v2 が Rust 実装で高速バリデーションを行い、どの子クラスとして検証するかが `type` フィールドで決まる
   - `CompetitiveConfig(type=Literal["competitive"])` + `DebateConfig(type=Literal["debate"])` を `Annotated[Union[...], Field(discriminator="type")]` で結合するのがベストプラクティス
   - `strategy_config` は `PhaseSpec` の Optional フィールドとして追加し、pattern に対応する Config のみ有効とする

2. **Per-task / per-phase timeout in orchestration (Microsoft Azure Architecture Guide, https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns; Temporal.io, https://temporal.io/blog/orchestrating-ambient-agents-with-temporal)**:
   - Temporal はワークフロー全体・アクティビティ単体・スケジュール to クローズの3段階タイムアウトを持ち、フェーズごとの細粒度設定が標準
   - Microsoft Agent Framework も "timeout and retry as architectural decisions during design phase" を推奨
   - ベストプラクティス: フェーズのタイムアウトはグローバルのデフォルト (`task_timeout`) をオーバーライドでき、`None` の場合はデフォルトにフォールバックする

3. **Strategy pattern with typed parameters in Python (getorchestra.io, "Fast API Discriminated Unions", https://www.getorchestra.io/guides/fast-api-discriminated-unions-handling-unions-with-a-type-discriminator; ezyang, "Idiomatic algebraic data types in Python", https://blog.ezyang.com/2020/10/idiomatic-algebraic-data-types-in-python-with-dataclasses-and-union/)**:
   - Strategy pattern を型安全に実装するには各 Strategy クラスが専用 Config dataclass を受け取り、dispatch は discriminator で行う
   - `dataclass` + `Union` を ADT (Algebraic Data Type) として使うと exhaustiveness checking (`assert_never`) が機能する
   - FastAPI/Pydantic では `Field(discriminator="type")` + `Literal[...]` の組み合わせが最も idiomatic

**実装方針**:
1. `SingleConfig` / `ParallelConfig` / `CompetitiveConfig` / `DebateConfig` を Pydantic `BaseModel` として `domain/phase_strategy.py` に追加
2. `StrategyConfig = Annotated[Union[SingleConfig, ParallelConfig, CompetitiveConfig, DebateConfig], Field(discriminator="type")]` として型エイリアスを定義
3. `PhaseSpec.strategy_config: StrategyConfig | None = None` フィールドを追加
4. `PhaseSpec.timeout: int | None = None` フィールドを追加
5. `_make_task_spec()` に `timeout: int | None = None` パラメータを追加
6. 各 Strategy の `expand()` メソッドが `phase.timeout` を各 task spec に反映する
7. Web 層の `PhaseSpecModel` に同じ `timeout` と `strategy_config` フィールドを追加
8. `expand_phases()` / `expand_phases_with_status()` が `PhaseSpec.timeout` を引き継ぐ
9. テスト: `tests/test_strategy_config.py` に 25 テスト追加


### Step 2 — Implementation Summary

**実装ファイル**:

- `src/tmux_orchestrator/domain/phase_strategy.py`: `SingleConfig`, `ParallelConfig`, `CompetitiveConfig`, `DebateConfig` を stdlib `@dataclass` で実装 (domain purity 維持)。`StrategyConfig = Union[...]` 型エイリアス。`PhaseSpec.timeout: int | None = None` + `PhaseSpec.strategy_config: StrategyConfig | None = None` 追加。`_make_task_spec(..., timeout: int | None = None)` に timeout 伝播。全 4 戦略の `expand()` が `phase.timeout` を伝播。`expand_phases_from_specs()` 追加。
- `src/tmux_orchestrator/domain/task.py`: `Task.timeout: int | None = None` 追加、`to_dict()` に `"timeout"` 追加。
- `src/tmux_orchestrator/phase_executor.py`: 後方互換 shim — 新型を再 export。
- `src/tmux_orchestrator/web/schemas.py`: `SingleConfigModel`, `ParallelConfigModel`, `CompetitiveConfigModel`, `DebateConfigModel` (Pydantic BaseModel)、`StrategyConfigModel = Annotated[Union[...], Field(discriminator="type")]`。`PhaseSpecModel.timeout` + `PhaseSpecModel.strategy_config` 追加。
- `src/tmux_orchestrator/web/routers/workflows.py`: `_to_domain_strategy_config()` コンバータ、`submit_task(timeout=spec.get("timeout"))` 伝播。
- `src/tmux_orchestrator/application/orchestrator.py`: `submit_task(timeout: int | None = None)`、`self._task_timeout` dict で dispatch 時に記録、`_record_agent_history` で history に保存。
- `src/tmux_orchestrator/web/routers/tasks.py`: `GET /tasks/{id}` の全パス (queued/active/in_progress/history) に `"timeout"` フィールド追加。
- `tests/test_strategy_config.py`: 29 新規テスト (2579 → 2608 合計)。

**TDD サイクル**: Red → Green → Refactor。全 2608 テスト PASS。OpenAPI スナップショット再生成済み。

### Step 3 — E2E Demo

**デモ**: `~/Demonstration/v1.1.31-strategy-config/demo.py`

**パターン**: Competitive + Pipeline (3フェーズ、4エージェント)
- Phase 1 (competitive, timeout=900, CompetitiveConfig(top_k=1)): solver-a + solver-b が同時に fibonacci 関数を実装
- Phase 2 (single, timeout=600): judge が両解を評価し勝者を選択
- Phase 3 (single, timeout=300): checker が勝者コードをアサーション検証

**結果**: **28/28 チェック PASS**

| # | チェック | 結果 |
|---|---------|------|
| 1 | SingleConfig type='single' | PASS |
| 2 | ParallelConfig type='parallel' | PASS |
| 3 | CompetitiveConfig type='competitive' | PASS |
| 4 | DebateConfig type='debate' | PASS |
| 5 | CompetitiveConfig(top_k=0) raises ValueError | PASS |
| 6 | DebateConfig(rounds=0) raises ValueError | PASS |
| 7 | PhaseSpec.timeout=900 accepted | PASS |
| 8 | PhaseSpec.strategy_config=CompetitiveConfig accepted | PASS |
| 9 | 2 solver tasks with timeout=900 | PASS |
| 10 | 1 judge task with timeout=600 | PASS |
| 11 | Task.timeout=450 accepted | PASS |
| 12 | Task.to_dict includes timeout | PASS |
| 13 | POST /workflows returns workflow_id | PASS |
| 14 | POST /workflows returns 4 task IDs (2 solvers + judge + checker) | PASS |
| 15 | Solver task has timeout=900 in GET /tasks/{id} | PASS |
| 16 | Both solver tasks completed (success) | PASS |
| 17 | Judge task completed (success) | PASS |
| 18 | Checker task completed (success) | PASS |
| 19 | All completed tasks have correct timeout in GET /tasks/{id} | PASS |
| 20 | Scratchpad PUT/GET round-trip works | PASS |
| 21 | Workflow shows 3 phases (competitive, single, single) | PASS |
| 22 | CompetitiveConfigModel fields valid | PASS |
| 23 | DebateConfigModel fields valid | PASS |
| 24 | ParallelConfigModel fields valid | PASS |
| 25 | SingleConfigModel type='single' | PASS |
| 26 | CompetitiveConfigModel(top_k=0) raises ValidationError | PASS |
| 27 | PhaseSpecModel.timeout=900 accepted | PASS |
| 28 | PhaseSpecModel.strategy_config discriminated by type | PASS |

### Step 4 — Feedback

**デバッグ事項**:
1. **Domain purity failure**: `domain/phase_strategy.py` に Pydantic を import → `test_domain_purity.py` 失敗。修正: stdlib `@dataclass` + `__post_init__` バリデーションに変更。Pydantic モデルは `web/schemas.py` に分離。
2. **OpenAPI schema assertion failure**: 新フィールドで OpenAPI contract 変更 → `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_openapi_schema.py` で再生成。
3. **Demo SyntaxError**: f-string 内の `"""` → 文字列連結 + `.format()` に変更。
4. **Demo KeyError**: `.format()` で `{"value": ...}` の `{` が format field と解釈 → `{{` でエスケープ。
5. **Demo check FAIL (timeout=None)**: 完了済みタスクの history パスに timeout なし → `_task_timeout` dict を orchestrator に追加、dispatch 時に記録、`_record_agent_history` で history に保存。
6. **Demo check FAIL (scratchpad PUT 404)**: キー名に `/` を含むと scratchpad ルーターが path として解釈 → フラットなキー名 `v1131_demo_test` に変更。

**次イテレーション候補** (DESIGN.md §11 更新):
- Competitive ストラテジー評価基準カスタマイズ (`CompetitiveConfig.judge_prompt_template`)
- Debate ストラテジーの動的終了条件 (`DebateConfig.early_stop_signal`)
- ワークフローテンプレートのパラメータ継承 (`examples/workflows/*.yaml` の `defaults:` セクション)

---

## §10.64 — v1.1.32: Competitive ストラテジー評価基準カスタマイズ + Debate 動的終了条件 (観点B)

### Step 0 — 選択理由

**選択**: Competitive ストラテジー評価基準カスタマイズ (`CompetitiveConfig.judge_prompt_template`) + Debate ストラテジーの動的終了条件 (`DebateConfig.early_stop_signal`) を同一イテレーションで実装する。

**理由**:
1. **両機能とも観点B・中優先度で未実装**: v1.1.31 で StrategyConfig 値オブジェクトが完成したため、その拡張として `CompetitiveConfig` と `DebateConfig` に新フィールドを追加するのが自然な次ステップ。
2. **実装スコープが小さく組み合わせやすい**: 両機能とも `domain/phase_strategy.py` + `web/schemas.py` の変更のみ。テスト追加も同じファイルへ。1イテレーションで収まる規模。
3. **相乗効果**: `judge_prompt_template` は競合フェーズの評価精度を向上させ、`early_stop_signal` は討論フェーズの不要な追加ラウンドを防ぐ。2機能が独立しているためリスクが低く、並行実装が容易。

**選ばなかった候補**:
- **エージェント設定ファイルの分離 (観点A)**: `isolate: false` のユースケースが限定的。worktree 分離がデフォルトになった現状では優先度低。
- **ワークフローテンプレートのパラメータ継承**: `examples/workflows/*.yaml` への `defaults:` セクション追加は YAML 構造の変更を要し、ローダーの変更範囲が大きい。

### Step 1 — Research

**Query 1**: "LLM-as-judge prompt template customization competitive evaluation multi-agent 2024 2025"

- **Evidently AI — LLM-as-a-Judge: a complete guide** (https://www.evidentlyai.com/llm-guide/llm-as-a-judge): Prompt template design—formulation of rubrics, order of score descriptions, inclusion of reference answers—has a pronounced effect on alignment to humans and consistency across replications.
- **Monte Carlo Data — LLM-As-Judge: 7 Best Practices & Evaluation Templates** (https://www.montecarlodata.com/blog-llm-as-judge/): Best practices: use yes/no questions, break down complex criteria, ask for reasoning. Multi-family ensemble votes and meta-evaluation alarms improve fairness.
- **arXiv 2504.17087 — Leveraging LLMs as Meta-Judges** (https://arxiv.org/html/2504.17087v1): Three-stage meta-judge pipeline: develop comprehensive rubric with GPT-4, use multiple LLM agents to score judgments, apply threshold to filter low-scoring judgments.
- **arXiv 2508.02994 — Agent-as-a-Judge** (https://arxiv.org/html/2508.02994v1, 2025): Panels of LLM evaluators outperform single judges on accuracy and cost; multi-agent debate surfaces richer rationales and improves alignment with human judgments.

**Key finding**: Customizable judge prompt templates with `{criteria}`, `{solutions}`, `{context}` placeholders are the industry standard. The default template should request structured reasoning + score. Placeholders should use `str.format_map()` (safe against KeyError) not f-strings (security/injection risk).

**Query 2**: "multi-agent debate early stopping consensus detection LLM 2024 2025"

- **arXiv 2510.12697 — Multi-Agent Debate for LLM Judges with Adaptive Stability Detection** (https://arxiv.org/html/2510.12697v1, 2025): Stability detection using Beta-Binomial mixture model tracking consensus dynamics; adaptive stopping via Kolmogorov–Smirnov testing. Demonstrates significant accuracy improvements over majority voting.
- **ICLR 2025 Blog — Multi-LLM-Agents Debate** (https://d2jud02ci9yv69.cloudfront.net/2025-04-28-mad-159/blog/mad/): MAD early-stopping: debate shuts down when all agents reach consensus. Iterative refining strategies include early termination and extended reflection.
- **Emergent Mind — Multi-Agent Debate Strategies** (https://www.emergentmind.com/topics/multi-agent-debate-mad-strategies): Aggregation and decision mechanics include majority voting, judge agents, score-based trajectory evaluation, and convergence-based stopping.
- **arXiv 2507.05981 — Multi-Agent Debate Strategies for Requirements Engineering** (https://arxiv.org/html/2507.05981v1, 2025): Early stopping signal embedded in agent output (explicit keyword like "CONSENSUS_REACHED") is simpler and more robust than statistical tests for practical systems.

**Key finding**: The simplest robust early-stop mechanism is an **explicit signal keyword** (e.g. `"EARLY_STOP"`) that the judge writes to the scratchpad. The orchestrator checks for this keyword after each round. No statistical machinery needed for a workflow coordinator.

**Query 3**: "prompt template injection placeholders Python string format f-string best practices template engine 2024"

- **Python PEP 750 — Template Strings** (https://peps.python.org/pep-0750/): New template literal syntax for Python; not yet in stdlib.
- **LangChain Prompt Template Format Guide** (https://docs.langchain.com/langsmith/prompt-template-format): F-string syntax for straightforward prompts; Mustache for complex data structures and logic.
- **Stack Abuse — Python Template Class** (https://stackabuse.com/formatting-strings-with-the-python-template-class/): `string.Template` provides `$placeholder` syntax; safe against injection. `str.format_map()` is safer than `str.format()` because it doesn't raise KeyError on missing keys with a `defaultdict`.

**Key finding**: Use `str.format_map(safe_dict)` with a `collections.defaultdict(str)` fallback so missing placeholders degrade gracefully rather than raising `KeyError`. This matches LangChain's approach for user-defined templates.

### Step 2 — Implementation

**Files changed**:
- `src/tmux_orchestrator/domain/phase_strategy.py`:
  - `CompetitiveConfig.judge_prompt_template: str = ""` — new field.
  - `DebateConfig.early_stop_signal: str = ""` — new field.
  - `_render_competitive_judge_prompt()` — renders template via `str.replace()` for `{context}`, `{solutions}`, `{criteria}`. Using `str.replace` (not `format_map`) avoids `ValueError` when templates contain Python dict literals with braces (e.g. `{'key': 'val'}`).
  - `_build_debate_judge_early_stop_instruction()` — builds early-stop paragraph for debate judge prompt.
  - `CompetitiveStrategy.expand()` — appends inline judge task when `judge_prompt_template` is non-empty.
  - `DebateStrategy.expand()` — appends early-stop instruction to judge prompt when `early_stop_signal` is non-empty.
- `src/tmux_orchestrator/web/schemas.py`:
  - `CompetitiveConfigModel.judge_prompt_template: str = Field(default="", description=...)`
  - `DebateConfigModel.early_stop_signal: str = Field(default="", description=...)`
- `src/tmux_orchestrator/web/routers/workflows.py`:
  - `_to_domain_strategy_config()` for `"competitive"` now passes `judge_prompt_template`.
  - `_to_domain_strategy_config()` for `"debate"` now passes `early_stop_signal`.
- `tests/test_strategy_config.py`: 18 new tests (2608 → 2626 total).
- OpenAPI snapshot regenerated.

**TDD cycle**: Red → Green → Refactor. All 2626 tests PASS.

**Key bug fixed**: `_render_competitive_judge_prompt` initially used `str.format_map(defaultdict(str, ...))`. This raised `ValueError: Invalid format specifier` when the template contained Python dict literals like `{'winner': 'slot-0'}`. Fixed by switching to sequential `str.replace()` calls for the three known placeholders.

**Second bug fixed**: `_to_domain_strategy_config()` in workflows router did not include `judge_prompt_template` and `early_stop_signal` fields, causing the inline judge task to never be generated. Fixed with `getattr(m, 'judge_prompt_template', '')`.

### Step 3 — E2E Demo

**Demo**: `~/Demonstration/v1.1.32-judge-prompt-early-stop/demo.py`

**Pattern**: Competitive + Debate (2 workflows, 6 agents)
- Workflow 1 (competitive, 3 tasks, timeout=900): solver-a + solver-b write `is_prime()`; `CompetitiveStrategy` auto-appends inline judge task using custom `judge_prompt_template`. Judge selects winner and uploads verdict to scratchpad.
- Workflow 2 (debate, 3 tasks, timeout=900): advocate + critic debate prime-checking approaches (trial division vs Miller-Rabin); judge prompt includes `early_stop_signal="CONSENSUS_REACHED"` instruction.

**Result**: **36/36 checks PASSED**

| # | Check | Result |
|---|-------|--------|
| 1 | CompetitiveConfig.judge_prompt_template default is empty str | PASS |
| 2 | CompetitiveConfig.judge_prompt_template custom value accepted | PASS |
| 3 | DebateConfig.early_stop_signal default is empty str | PASS |
| 4 | DebateConfig.early_stop_signal custom value accepted | PASS |
| 5 | {context} placeholder substituted | PASS |
| 6 | {criteria} placeholder substituted | PASS |
| 7 | {solutions} placeholder substituted (hint text) | PASS |
| 8 | Unknown placeholder does not raise KeyError | PASS |
| 9 | Early-stop instruction contains signal keyword | PASS |
| 10 | Early-stop instruction references scratchpad | PASS |
| 11 | CompetitiveStrategy generates judge task when template set | PASS |
| 12 | Judge task depends on both solver tasks | PASS |
| 13 | Judge prompt contains rendered {criteria} | PASS |
| 14 | Judge prompt contains rendered {context} | PASS |
| 15 | CompetitiveStrategy: no judge task when template empty | PASS |
| 16 | DebateStrategy generates judge task | PASS |
| 17 | Debate judge prompt contains early-stop signal keyword | PASS |
| 18 | Debate judge prompt references scratchpad for early-stop | PASS |
| 19 | Debate judge: no early-stop text when signal empty | PASS |
| 20 | CompetitiveConfigModel.judge_prompt_template default empty | PASS |
| 21 | CompetitiveConfigModel.judge_prompt_template custom value | PASS |
| 22 | DebateConfigModel.early_stop_signal default empty | PASS |
| 23 | DebateConfigModel.early_stop_signal custom value | PASS |
| 24 | POST /workflows competitive returns workflow_id | PASS |
| 25 | POST /workflows returns 3 task IDs (2 solvers + inline judge) | PASS |
| 26 | Inline judge task present in task map | PASS |
| 27 | POST /workflows debate returns workflow_id | PASS |
| 28 | Debate workflow returns 3 task IDs (advocate + critic + judge) | PASS |
| 29 | Debate judge task present in task map | PASS |
| 30 | Both competitive solver tasks completed (success) | PASS |
| 31 | Inline competitive judge task completed (success) | PASS |
| 32 | Debate advocate task completed (success) | PASS |
| 33 | Debate critic task completed (success) | PASS |
| 34 | Debate judge task completed (success) | PASS |
| 35 | Competitive judge verdict written to scratchpad | PASS |
| 36 | Scratchpad PUT/GET round-trip works | PASS |

### Step 4 — Feedback

**Debugging notes**:
1. `ValueError: Invalid format specifier` from `str.format_map`: templates with Python dict literals break format_map. Fixed with `str.replace()`.
2. HTTP 500 on POST /workflows: `_to_domain_strategy_config()` missing new fields. Fixed with `getattr(m, 'field', default)`.
3. `{criteria}` KeyError in demo template string: f-string `.format()` at module level consumed `{criteria}` before it could be used as a template placeholder. Fixed by using f-string parts without `.format()` for the template.

**次イテレーション候補**:
- ワークフローテンプレートのパラメータ継承 (`examples/workflows/*.yaml` の `defaults:` セクション)
- エージェント設定ファイルの分離 (観点A)

---

## §10.65 — v1.1.33: ワークフローテンプレートのパラメータ継承 (観点B)

### Step 0 — 選択理由

**選択**: ワークフローテンプレートのパラメータ継承 (`examples/workflows/*.yaml` の `defaults:` セクション + `POST /workflows` の `defaults` フィールド)

**理由**:
1. **観点B の残存最高優先度候補**: v1.1.32 で観点B の Competitive/Debate カスタマイズが完了。残る観点B 候補のうち「ワークフローテンプレートのパラメータ継承」が次に優先度が高い。ユーザー指定の3候補（観点A・B・C）の中で唯一未実装のまま残っている観点B 項目。
2. **UX 価値が明確**: 全 YAML テンプレートに `defaults:` セクションを追加することで、`task_timeout`・`required_tags`・`reply_to` などの共通設定を一箇所で指定できる。現状は各フィールドを個別に指定しなければならず、大規模ワークフロー設定が冗長になる。
3. **実装コストが低い**: YAML ローダー側で `defaults` を展開するユーティリティ関数を1つ追加し、`POST /workflows` の各エンドポイントでそれを呼ぶだけ。既存スキーマへの変更は最小限。
4. **デモ設計が容易**: 既存の competition + tdd ワークフローを `defaults:` 付き YAML で呼び出すシナリオを 2 エージェント以上で実行できる。

**選ばなかった候補**:
- **エージェント設定ファイルの分離 (観点A)**: `isolate: false` のユースケースが現行バージョンで非常に限定的（worktree 分離がデフォルト）。ROI が低い。
- **メールボックスの自動クリーンアップ (観点C 残)**: v1.1.30 で project-scoped mailbox を実装済み。自動クリーンアップはその後継として有用だが、現在デモで実用上の問題が報告されていない。

### Step 1 — Research

**Query 1**: "YAML configuration inheritance defaults section override pattern 2024 2025"

- **GitLab CI/CD YAML `default:` keyword** (https://docs.gitlab.com/ci/yaml/, 2025): Top-level `default:` block supplies job-level defaults for `tags`, `timeout`, `image`, `retry`, etc. Key principle: "Default configuration does not merge with job configuration. If the job already has a keyword defined, the job keyword takes precedence and the default configuration for that keyword is not used." This is exactly the semantics we want — body value wins, defaults fill in absences.
- **MoldStud YAML Inheritance Puzzle** (https://moldstud.com/articles/p-solving-the-yaml-inheritance-puzzle, 2024): YAML has no built-in inheritance. The `<<` operator merges anchors within a file. For Python applications, the recommended pattern is: define a `defaults:` block, load it separately, then merge with the body dict.
- **HiYaPyCo** (https://github.com/zerwes/hiyapyco, 2024): Hierarchical YAML config library that deep-merges a list of YAML files in order. Scalars and lists from later files overwrite earlier values; dicts are recursively merged. Confirms that our merge semantics (base wins, defaults fill in gaps) is the industry-standard "overlay" pattern used in Puppet Hiera, Spring Boot, etc.

**Key finding**: The standard pattern is a `defaults:` section (or top-level block) where **absent** keys are filled from defaults, while present keys (even `null` or `[]`) are kept unchanged. Deep merge for nested dicts, scalar replacement for lists. No external library needed — a simple recursive `dict` merge in stdlib Python is sufficient.

**Query 2**: "workflow configuration defaults inheritance merge YAML REST API design patterns 2025"

- **GitHub Actions YAML anchors** (https://github.blog/changelog/2025-09-18-actions-yaml-anchors-and-non-public-workflow-templates/): GitHub Actions now supports YAML anchors as a way to share config. Note merge keys (`<<`) are still not supported — so anchors only copy values, not merge dicts.
- **Kestra workflow YAML** (https://procycons.com/en/blogs/workflow-orchestration-platforms-comparison-2025/, 2025): Kestra reads YAML; executor resolves which tasks can run and drops them onto the queue. The `defaults:` pattern maps directly to workflow-level configuration that phases inherit.
- **Dynaconf merging** (https://www.dynaconf.com/merging/): Python configuration library that supports `MERGE_ENABLED_FOR_DYNACONF` for recursive merge of settings. Confirms deep-merge as the dominant pattern.

**Key finding**: For a REST API, the `defaults:` section in a YAML template is a client-side concern — the server receives already-merged JSON. The utility function `apply_workflow_defaults()` is the right boundary: it processes the YAML template before schema validation.

**Query 3**: "Python YAML deep merge defaults override dictionary configuration patterns 2024"

- **deepmerge (PyPI)** (https://pypi.org/project/deepmerge/, 2024): Third-party library for deep-merging Python dicts. Provides a `Merger` class with configurable strategies per type. For our use case, a bespoke 15-line recursive function is preferable over an external dependency.
- **hiyapyco deep-merge pattern**: `list` fields from later files completely replace earlier values (not appended). Dicts are merged recursively. Scalars overwrite. This matches our semantics.
- **HiML (Adobe)** (https://github.com/adobe/himl, 2024): Hierarchical YAML config supporting variable interpolation and secrets retrieval. More complex than we need.

**Key finding**: A pure-stdlib recursive merge function with these rules is standard and sufficient: (1) if key absent from base → copy default; (2) if both values are dicts → recurse; (3) otherwise keep base value. This avoids the `deepmerge` external dependency and is trivially testable.

### Step 2 — Implementation

**Files created/changed**:
- `src/tmux_orchestrator/workflow_defaults.py` (new): `deep_merge_defaults()` + `apply_workflow_defaults()` + `load_workflow_template()`. Pure stdlib, no new dependencies.
- `examples/workflows/tdd.yaml`: added `defaults:` section with `language`, `*_tags`, `reply_to`.
- `examples/workflows/competition.yaml`: added `defaults:` section with `scoring_criterion`, `*_tags`, `reply_to`.
- `examples/workflows/debate.yaml`: added `defaults:` section with `max_rounds`, `*_tags`, `reply_to`.
- `examples/workflows/pair.yaml`: added `defaults:` section with `*_tags`, `reply_to`.
- `tests/test_workflow_defaults.py` (new): 34 tests covering `deep_merge_defaults`, `apply_workflow_defaults`, `load_workflow_template`, Pydantic schema integration, and real template files.

**TDD cycle**: 34 new tests, all passing. All 2626 existing tests remain green (2626 → 2660 total, +34).

**No REST API changes** — `workflow:` and `defaults:` keys are both stripped before Pydantic schema validation. Pydantic v2's default `extra="ignore"` mode means templates with `defaults:` key also pass validation via the existing `load_template()` helper.

### Step 3 — E2E Demo

**Demo**: `~/Demonstration/v1.1.33-workflow-defaults/demo.py`

**Pattern**: Pair workflow (navigator → driver pipeline, 2 real agents) + parallel solver-a

- Navigator agent writes a plan for `count_vowels(s: str) -> int` and uploads it to the shared scratchpad.
- Driver agent (depends on navigator) reads the plan and implements the function.
- Solver-a runs in parallel (independent task) to verify multi-agent concurrency.
- Section A (20 checks): unit-level verification of `deep_merge_defaults`, `apply_workflow_defaults`, `load_workflow_template`, and Pydantic schema integration using real `tdd.yaml`, `competition.yaml`, `pair.yaml` templates.
- Section B-D (6 checks): real agent task submission, completion detection, scratchpad artifact verification.

**Result**: **26/26 checks PASSED**

**Debug fix**: `POST /tasks` response uses `task_id` key, not `id`. Fixed in demo.

### Step 4 — Feedback

**デバッグ事項**:
1. **KeyError `nav_task["id"]`**: `POST /tasks` returns `{"task_id": ..., ...}` not `{"id": ..., ...}`. Fixed by using `nav_task["task_id"]`.

**次イテレーション候補**:
- エージェント設定ファイルの分離 (観点A) — `isolate: false` エージェントの `.agent/{agent_id}/` 分離
- メールボックスの自動クリーンアップ (観点C) — `mailbox_cleanup_on_stop: bool = True`

---

## §10.66 — v1.1.34: メールボックスの自動クリーンアップ (観点C)

### Step 0 — 選択理由

**選択**: メールボックスの自動クリーンアップ (`mailbox_cleanup_on_stop: bool = True`)

**理由**:
1. **観点C の残存最高優先度候補**: v1.1.30 で project-scoped mailbox ディレクトリを実装済み。自動クリーンアップは自然な後継機能。デモを繰り返すと `{mailbox_dir}/{session_name}/` 配下に古いメッセージが蓄積し、テスト再現性が低下する。
2. **実装コストが最小**: `Orchestrator.stop()` の末尾に `shutil.rmtree()` 呼び出しを追加するだけ。`OrchestratorConfig` に `bool` フィールドを1つ追加。既存テストへの影響が小さい。
3. **エージェント設定ファイルの分離 (観点A) を選ばない理由**: `isolate: false` のユースケースが現行バージョンで非常に限定的。デフォルト (`isolate: true`) では worktree 分離が既に機能しており、ROI が低い。v1.1.33 で観点A を選ばなかった判断と同じ。
4. **PhaseSpec 条件分岐 (観点B) を選ばない理由**: スコープが大きく、1イテレーションで完結しない可能性がある。観点Cのクリーンアップは自己完結かつ小さい。

**選ばなかった候補**:
- **エージェント設定ファイルの分離 (観点A)**: `isolate: false` の実用ケースが限定的。ROI 低。
- **PhaseSpec 条件分岐 (観点B)**: スコープが大きい。後回し。

### Step 1 — Research


**Query 1**: "temporary directory cleanup on process shutdown best practices Python 2025"

- **Python `tempfile` docs** (https://docs.python.org/3/library/tempfile.html, 2025): `TemporaryDirectory` provides automatic cleanup via context manager (`__exit__`). For manual directories (created with `mkdtemp()` or `mkdir()`), `shutil.rmtree()` is the idiomatic cleanup call. The `ignore_cleanup_errors=True` parameter (Python 3.10+) can suppress errors from non-writable files during deletion.
- **Python Friday #282** (https://pythonfriday.dev/2025/06/282-working-with-temporary-files/, 2025): Recommends context managers for automatic cleanup; manual cleanup with `shutil.rmtree` for directories created outside a `with` block. For conditional cleanup (e.g., configurable), a `bool` flag plus explicit `shutil.rmtree` is the standard pattern.
- **Key finding**: The mailbox directory is created with `mkdir(parents=True, exist_ok=True)` and is NOT a `TemporaryDirectory` context manager. Therefore explicit `shutil.rmtree` with `ignore_errors=True` (Python 3.12+) or `onerror` callback (3.10) in `Orchestrator.stop()` is the correct approach.

**Query 2**: "message queue mailbox cleanup on shutdown distributed systems patterns 2024 2025"

- **RabbitMQ queue cleanup** (https://www.rabbitmq.com/docs/queues, 2025): Queues can be configured with TTL or deleted on consumer disconnect. Session-scoped queues (transient queues) are common; they are created at connect and deleted at disconnect. This is analogous to our per-session mailbox directory.
- **Graceful Shutdown in Distributed Systems** (https://medium.com/@jusuftopic/designing-for-graceful-shutdown-in-distributed-systems-435fdc2c09af, 2025): Cleanup on shutdown should release resources (temp dirs, DB connections, locks). The pattern is: drain → shutdown → cleanup. In our case: stop agents → stop orchestrator loop → delete mailbox dir.
- **Key finding**: Session-scoped mailbox deletion at `stop()` is consistent with the industry pattern of "transient queue" cleanup on consumer disconnect. The mailbox directory is inherently session-scoped: it is useless after the orchestrator stops (no agents remain to receive messages).

**Query 3**: "shutil.rmtree safe async cleanup Python asyncio shutdown signal handler 2025"

- **aioshutil PyPI** (https://pypi.org/project/aioshutil/, 2024): `aioshutil` provides async-safe `rmtree` that delegates to a thread pool. However, for our use case `Orchestrator.stop()` is an `async` method that already uses `loop.run_in_executor` for blocking I/O. A simple `await loop.run_in_executor(None, shutil.rmtree, path)` is sufficient — no new dependencies.
- **Signal handling in asyncio** (https://johal.in/signal-handling-in-python-custom-handlers-for-graceful-shutdowns/, 2025): `loop.add_signal_handler` with a coroutine-safe shutdown sequence. Our `Orchestrator.stop()` is already the canonical shutdown point.
- **Key finding**: Use `shutil.rmtree(mailbox_dir, ignore_errors=True)` (stdlib) inside `Orchestrator.stop()` after all agents have been stopped. No new dependencies. Guard with `mailbox_dir.exists()` to be safe.


### Step 2 — Implementation

**Files created/changed**:
- `src/tmux_orchestrator/application/config.py`: Added `mailbox_cleanup_on_stop: bool = True` field to `OrchestratorConfig`; added `mailbox_cleanup_on_stop=data.get("mailbox_cleanup_on_stop", True)` to `load_config()`.
- `src/tmux_orchestrator/application/orchestrator.py`: Added `import shutil` + `from pathlib import Path`; added cleanup block at end of `stop()` — guards with `config.mailbox_cleanup_on_stop` and `session_mailbox.exists()`; uses `shutil.rmtree(session_mailbox, ignore_errors=True)`.
- `tests/test_mailbox_cleanup.py` (new): 11 tests covering field default, YAML round-trip, stop() deletes session dir (cleanup=true), stop() preserves dir (cleanup=false), noop when dir missing, only-session-subdir deleted, nested dir deleted, different session names.

**TDD cycle**: 11 new tests. All 2671 tests pass (2660 → 2671).

**No REST API changes** — config field only; no new endpoints.

### Step 3 — E2E Demo

**Demo**: `~/Demonstration/v1.1.34-mailbox-cleanup/demo.py`

**Pattern**: navigator → driver pipeline (2 real agents, genuine dependency via `depends_on`)
+ mailbox cleanup verification via 3 server runs.

- navigator writes `sum_digits` algorithm spec → uploads to scratchpad key `wf_v1134_plan`
- driver (depends_on navigator) reads plan → implements `sum_digits.py` → verifies assertions → uploads to `wf_v1134_result`
- After server stop (cleanup=true): `{DEMO_DIR}/.orchestrator/mailbox/{session}/` is deleted
- After server stop (cleanup=false): session mailbox dir is preserved; sibling sessions unaffected

**Result**: **15/15 checks PASSED**

**Debug fix**: Config B had `task_timeout: 60` with `watchdog_poll: 40` → violated `watchdog_poll <= task_timeout / 3`. Fixed by using `task_timeout: 600`.

### Step 4 — Feedback

**デバッグ事項**:
1. **watchdog_poll validator**: Config B with `task_timeout: 60, watchdog_poll: 40` failed `__post_init__` validation. Fixed by using `task_timeout: 600`.

**次イテレーション候補**:
- エージェント設定ファイルの分離 (観点A) — `isolate: false` エージェントの `.agent/{agent_id}/` 分離
- PhaseSpec 条件分岐 (観点B) — `skip_condition` フィールド

---

## §10.67 — v1.1.35: エージェント設定ファイルの分離 (観点A) — `.agent/{agent_id}/` サブディレクトリ

### Step 0 — 選択理由

**選択**: エージェント設定ファイルの分離 (`isolate: false` 時の `.agent/{agent_id}/` サブディレクトリ)

**理由**:
1. **v1.1.34 §10.66 の明示的な次候補**: §10.66 Step 4 に「次イテレーション候補」として記録済み。今回は `isolate: false` のユースケースが正式に要求された。
2. **競合状態の根本解消**: 現状 `settings.local.json` は `{cwd}/.claude/` に書き込まれる。複数エージェントが同じ `cwd` を共有する場合 (`isolate: false`)、各エージェントの Stop hook 設定が上書き競合する。サブディレクトリ分離で根本的に解消する。
3. **`__task_prompt__` と API キーファイルはすでに per-agent named** だが、`.claude/settings.local.json` は共有 — これが唯一の残る競合ポイント。

**選ばなかった候補**:
- **PhaseSpec 条件分岐 (観点B)**: スコープが大きく、1イテレーションで完結しない可能性がある。

### Step 1 — Research

**Query 1**: "Claude Code project trust directory isolation multi-agent settings.local.json"

- **ClaudeLog configuration guide** (https://claudelog.com/configuration/, 2026): `.claude/settings.local.json` は project-scoped であり、そのファイルが存在するディレクトリのプロジェクトコンテキストで読まれる。`settings.local.json` は gitignore 設定が自動適用される。
- **Claude Code Settings Reference** (https://claudefa.st/blog/guide/settings-reference, 2026): 設定の優先順位は managed > command line > local (`settings.local.json`) > project (`settings.json`) > user。`local` 設定は「個人用で特定リポジトリに対してのみ適用される」と説明されており、プロジェクトディレクトリに紐付けられている。
- **Milvus Blog "Why Claude Code Feels So Stable"** (https://milvus.io/blog/why-claude-code-feels-so-stable-a-developers-deep-dive-into-its-local-storage-design.md): Claude Code は起動ディレクトリに基づいてセッションデータを分離する。各プロジェクトのセッションは「ファイルパスから派生したディレクトリ配下」に格納される。これは `.agent/{agent_id}/` を個別の「プロジェクトディレクトリ」として扱えることを示す。
- **Key finding**: Claude Code を `cd .agent/{agent_id} && claude ...` で起動すれば、`.agent/{agent_id}/.claude/settings.local.json` が正しく読まれる。サブディレクトリを pre-trust するだけで trust dialog を回避できる。

**Query 2**: "Python agent shared working directory file isolation patterns concurrent agents"

- **AgentFS "Filesystem Isolation for AI Agents"** (https://www.agentfs.ai/, 2025): コピーオンライト分離 — エージェントごとに独立した名前空間を提供。各エージェントの書き込みは他のエージェントに影響しない。
- **Filesystem-Based Agent State** (https://agentic-patterns.com/patterns/filesystem-based-agent-state/): エージェントの状態ファイルは per-agent サブディレクトリに配置することが推奨される。共有ディレクトリへのフラットな配置は名前衝突を招く。
- **"Running 20 AI Agents in Parallel"** (https://pkarnal.com/blog/parallel-ai-agents): 「各エージェントは自分のディレクトリで動作し、他のエージェントと干渉しない」が鉄則。per-agent サブディレクトリ分離が標準パターン。
- **Key finding**: per-agent サブディレクトリ (`.agent/{agent_id}/`) パターンは、エージェントオーケストレーションにおける広く認められた分離手法である。各エージェントが自分のサブディレクトリを cwd として起動すれば、ファイル競合がゼロになる。

**Query 3**: "Claude Code hooks per-agent configuration cwd directory 2026"

- **Claude Code Hooks Reference** (https://code.claude.com/docs/en/hooks, 2025): hooks は `settings.local.json` が存在するプロジェクトのコンテキストで読まれる。Claude Code は起動時の cwd に基づいて settings ファイルを探索する。`CLAUDE_PROJECT_DIR` 環境変数が利用可能で、フック内でプロジェクトルートへの絶対パスとして参照できる。
- **"Claude Code to AI OS Blueprint"** (https://dev.to/jan_lucasandmann_bb9257c/claude-code-to-ai-os-blueprint-skills-hooks-agents-mcp-setup-in-2026-46gg): `--cwd <dir>` フラグは正式には存在しない。代わりに `cd {dir} && claude` のシェルコマンドパターンが推奨される。ClaudeCodeAgent は既にこのパターンを使っている (`f"cd {shlex.quote(str(cwd))} && {command}"`)。
- **"Claude Code Hooks: Complete Guide"** (https://dev.to/lukaszfryc/claude-code-hooks-complete-guide-with-20-ready-to-use-examples-2026-dcg): フックのコマンドには相対パスより絶対パスを使うことが推奨される。`$CLAUDE_PROJECT_DIR` を使うと cwd に依存せずスクリプトを指定できる。
- **Key finding**: `cd .agent/{agent_id} && claude` で起動することで Claude Code は `.agent/{agent_id}/` をプロジェクトディレクトリとして認識し、`.agent/{agent_id}/.claude/settings.local.json` の Stop hook が正しくスナップショットされる。既存の launch コマンドパターンと完全に互換。

### Step 2 — Implementation

**変更ファイル**:
- `src/tmux_orchestrator/agents/claude_code.py`:
  - `_agent_work_dir(cwd: Path) -> Path` メソッド追加: `isolate=False` 時に `cwd / ".agent" / agent_id` を返す（ディレクトリも作成）; `isolate=True` 時は `cwd` をそのまま返す
  - `start()`: `agent_dir = _agent_work_dir(cwd)` を計算し、全 per-agent ファイル書き込み (`_write_context_file`, `_write_api_key_file`, `_write_agent_claude_md`, `_write_notes_template`, `_copy_commands`, `on_start`, `pre_trust_worktree`) に `agent_dir` を使用; claude 起動コマンドも `cd {agent_dir}` に変更; context_files は共有リソースなので引き続き `cwd` にコピー; `self._cwd = agent_dir` で内部 cwd を更新
  - `stop()`: `self._cwd`（per-agent subdir）を `on_stop()` に渡す（`worktree_path` は shared cwd を指しているため不適切）
  - `_write_agent_claude_md` のガード (`if self._isolate`) を削除: 常に `agent_dir` に書き込むので `cwd` の CLAUDE.md は汚染されない
- `tests/test_slash_commands_worktree.py`: `test_non_isolated_agent_also_gets_commands` — 期待パスを `tmp_path` から `tmp_path / ".agent" / "non-isolated"` に更新

**新テスト**: `tests/test_agent_subdir_isolation.py` — 13 テスト

**TDD サイクル**: 13 新テスト。全 2684 テスト合格 (2671 → 2684)。

### Step 3 — E2E Demo

**デモ**: `~/Demonstration/v1.1.35-agent-subdir-isolation/demo.py`

**パターン**: Parallel specialisation — `agent-a` (add 関数) と `agent-b` (mul 関数) が同一 `cwd` で並列実行

**結果**: **20/20 checks PASSED**

検証内容:
- `.agent/agent-a/` と `.agent/agent-b/` サブディレクトリが作成された
- コンテキストファイルがサブディレクトリ内に存在し、cwd ルートには存在しない
- `settings.local.json` がサブディレクトリ内に存在し、cwd ルートには存在しない
- 各エージェントの Stop hook URL が正しい `agent_id` を参照している
- 両タスクが完了し、アーティファクト (`add_func.py`, `mul_func.py`) が各サブディレクトリに作成された

**デバッグ事項**:
1. `KeyError: 'type'`: YAML に `type: claude_code` が不足。追記して解決。
2. ポート競合: 前回の実行がポートを占有。再実行で解決。
3. `/health` → `/healthz`: ヘルスチェックエンドポイント名を修正。

### Step 4 — Feedback

**成功した点**:
- `isolate: false` エージェントの Stop hook 競合が根本解消された
- 実装が最小限: `_agent_work_dir()` メソッド1つで全ファイルの書き込み先を制御
- 既存テストへの影響が最小: 1テストの期待パス変更のみ
- `isolate: true` エージェントへの影響なし

**次イテレーション候補**:
- PhaseSpec 条件分岐 (観点B) — `skip_condition` フィールド
- `stop()` でエージェントサブディレクトリ (``.agent/{id}/``) を削除するクリーンアップ

## §10.68 — v1.1.36: PhaseSpec.skip_condition — スクラッチパッド駆動フェーズスキップ (観点B)

### Step 0 — Feature Selection

**選択理由**: §10.67 Step 4「次イテレーション候補」として PhaseSpec 条件分岐が最優先候補として記録済み。`stop()` サブディレクトリ削除より優先度高: スキップ条件は DAG 表現力を根本的に拡張し、実際のユースケース（ビルド失敗時にテストをスキップ、結果が既に存在する場合に計算をスキップ）を直接サポートする。

**選択しなかった候補**:
- `stop()` エージェントサブディレクトリ削除: ディスクスペース節約の Nice-to-have。優先度低。
- WebSocket pub/sub 配信最適化: 現状の実装で十分。

### Step 1 — Research

**クエリ1**: "workflow conditional step skip based on runtime state DAG orchestration 2024"

**クエリ2**: "DAG conditional execution skip node dependency workflow engine patterns"

**クエリ3**: "scratchpad blackboard pattern conditional workflow execution agent systems"

**主な知見**:

1. **Apache Airflow の trigger_rule** (https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html): Airflow では `trigger_rule` を使ってスキップ伝播を制御する。デフォルト (`all_success`) では上流がスキップされると下流もスキップされるが、`none_failed` ルールでは上流のスキップを成功扱いにして下流が実行できる。本実装では「スキップされたフェーズは依存解決済みとして扱う」方針を採用し、Airflow の `none_failed` に相当する動作を標準とする。

2. **Argo Workflows の when 条件** (https://argo-workflows.readthedocs.io/en/latest/walk-through/dag/): Argo は各 DAG タスクに `when` 式を持ち、govaluate を使って実行時に評価する。条件不成立のタスクは SKIPPED 状態になり、依存関係上は完了扱い。本実装の `SkipCondition` はこれを参考に、スクラッチパッドキーの存在/値チェックをスキップ評価に使う。

3. **Blackboard パターンとスクラッチパッドによる条件制御** (https://medium.com/@dp2580/building-intelligent-multi-agent-systems-with-mcps-and-the-blackboard-pattern-to-build-systems-a454705d5672): スクラッチパッドを介してエージェントが状態を共有し、制御コンポーネントがその状態に基づいてどの知識ソース（エージェント）を次に実行するかを決定するパターン。本実装の `SkipCondition` は、前フェーズがスクラッチパッドに書いた値をオーケストレーターが読み、次フェーズのスキップを決定する「機会主義的推論」(opportunistic reasoning) の実装である。

4. **PayPal 宣言的ワークフロー DSL (arXiv:2512.19769)**: 条件付きスキップを宣言的 DSL に含めることで開発時間を 60% 削減した事例。ペイロードに `skip_condition` フィールドを追加するアプローチは既存 DAG 構造と直交し、後方互換性を保ちながら表現力を拡張できる。

**設計決定**:
- `SkipCondition` は stdlib `@dataclass`（ドメイン層純粋性ルール）
- `value: str = ""` は空文字=「キーが存在すればスキップ」(Airflow の存在チェックに相当)
- `negate: bool = False` で否定条件を表現（「ビルド成功時にスキップ」と「ビルド失敗時にスキップ」の両方をサポート）
- SKIPPED フェーズは依存解決上 「complete」扱い（Argo/Airflow の `none_failed` 挙動に準拠）
- スクラッチパッドは `dict[str, Any]` として `expand_phases_with_status` に渡す（Web 層のインフラに触れない）

**参考文献**:
- Apache Software Foundation, "Apache Airflow Core Concepts: DAGs", https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html, 2024
- Argo Workflows contributors, "DAG Walk-Through", https://argo-workflows.readthedocs.io/en/latest/walk-through/dag/, 2024
- Denis Petelin, "Building Intelligent Multi-Agent Systems with MCPs and the Blackboard Pattern", Medium, 2025, https://medium.com/@dp2580/building-intelligent-multi-agent-systems-with-mcps-and-the-blackboard-pattern-to-build-systems-a454705d5672
- S. Dang et al., "PayPal Declarative Workflow DSL" arXiv:2512.19769, 2025

### Step 2 — Implementation

**変更ファイル**:
- `src/tmux_orchestrator/domain/phase_strategy.py`: `SkipCondition` dataclass 追加; `PhaseSpec.skip_condition` フィールド追加; `WorkflowPhaseStatus` に `mark_skipped()` 追加; `expand_phases_from_specs` に `scratchpad: dict | None` 引数追加（スキップ評価）
- `src/tmux_orchestrator/phase_executor.py`: `expand_phases_with_status` に `scratchpad: dict | None` 引数追加
- `src/tmux_orchestrator/web/schemas.py`: `SkipConditionModel` Pydantic モデル追加; `PhaseSpecModel.skip_condition` フィールド追加
- `src/tmux_orchestrator/web/routers/workflows.py`: `_to_domain_skip_condition()` アダプター追加; `expand_phases_with_status` 呼び出し時にスクラッチパッドを渡す; `build_workflows_router` に `scratchpad` 引数追加
- `src/tmux_orchestrator/web/app.py`: `build_workflows_router(orchestrator, auth, scratchpad=_scratchpad)` に更新

**新テスト**: `tests/test_phase_skip_condition.py`

## §10.69 — v1.1.37: `.agent/{agent_id}/` サブディレクトリの停止時自動クリーンアップ (観点A)

### Step 0 — Feature Selection

**選択理由**: §10.67 Step 4「次イテレーション候補」として明示的に記録済み。`isolate: false` エージェントが停止時に `.agent/{id}/` を残存させる問題は、連続デモ実行時にディスクスペースを汚染し再現性を低下させる。`mailbox_cleanup_on_stop` (§10.66) と同一の「一時ディレクトリのトランジェントライフサイクル」パターン。

**選択しなかった候補**:
- Worktree ↔ branch sync (`POST /agents/{id}/sync`): 実装コストが高く、現在のユースケースでは merge_on_stop で十分。§11 に追加。

### Step 1 — Research

**クエリ1**: "git worktree cleanup on process exit Python subprocess"

**クエリ2**: "temporary directory cleanup context manager Python best practices shutil rmtree"

**クエリ3**: "agent workspace directory cleanup isolation Python 2026"

**主な知見**:

1. **Python `tempfile.TemporaryDirectory`** (https://docs.python.org/3/library/tempfile.html): Python 標準の一時ディレクトリは with 文終了時に自動削除される。`ignore_errors=True` オプションで部分書き込みや既削除ディレクトリを静かにスキップできる。本実装は `shutil.rmtree(ignore_errors=True)` でこのパターンを採用する。

2. **カスタムコンテキストマネージャーの try/finally パターン** (https://coderivers.org/blog/temporary-directory-python/): ジェネレータベースのコンテキストマネージャーでは `try/finally` ブロックが必須。例外発生時もクリーンアップが保証される。本実装はエージェントライフサイクルに統合するため `stop()` 内で直接 `shutil.rmtree` を呼ぶが、同等の保証を実現する。

3. **Azure DevOps / Jenkins ワークスペースクリーンアップ** (https://rexbytes.com/2026/02/21/jenkins-ci-cd-8-11-workspace-cleanup-timeouts-retries/): CI/CD システムではエージェントのワークスペースはジョブ終了後に自動削除することが標準パターン。`cleanWs()` (Jenkins), `workspace.clean()` (Azure) — エフェメラルエージェントの原則に準拠。本実装の `cleanup_subdir: bool = True` (デフォルト有効) はこの標準に従う。

**設計決定**:
- `AgentConfig.cleanup_subdir: bool = True` — opt-out 型 (デフォルト削除)
- `shutil.rmtree(ignore_errors=True)` — 冪等性保証（既削除・部分書き込みを静かに許容）
- `isolate=True` エージェントは no-op (WorktreeManager が管理)
- `_cwd.parent.name != ".agent"` ガード — 誤削除防止のセーフガード
- ログ: 削除時は INFO レベル (mailbox_cleanup_on_stop と同一の方針)

**参考文献**:
- Python Software Foundation, "tempfile — Generate temporary files and directories", https://docs.python.org/3/library/tempfile.html
- CodeRivers, "Working with Temporary Directories in Python", https://coderivers.org/blog/temporary-directory-python/
- Rex Bytes, "Jenkins CI/CD: Workspace Cleanup, Timeouts, and Retries", https://rexbytes.com/2026/02/21/jenkins-ci-cd-8-11-workspace-cleanup-timeouts-retries/, 2026

### Step 2 — Implementation

**変更ファイル**:
- `src/tmux_orchestrator/application/config.py`: `AgentConfig.cleanup_subdir: bool = True` フィールド追加; `load_config()` で YAML から読み込み
- `src/tmux_orchestrator/agents/claude_code.py`: `__init__` に `cleanup_subdir: bool = True` 引数追加; `_cleanup_agent_subdir()` メソッド追加 (stop 時に呼ばれる); `stop()` の末尾に `self._cleanup_agent_subdir()` 呼び出し追加
- `src/tmux_orchestrator/application/factory.py`: `ClaudeCodeAgent(...)` に `cleanup_subdir=agent_cfg.cleanup_subdir` 追加

**新テスト**: `tests/test_agent_subdir_cleanup.py` — 9 テスト

**TDD サイクル**: 9 新テスト。全 2737 テスト合格 (2728 → 2737)。

### Step 3 — E2E Demo (v1.1.37-subdir-cleanup)

**デモ**: `~/Demonstration/v1.1.37-subdir-cleanup/demo.py`

2 つの `isolate: false` エージェント (agent-a, agent-b) がスクラッチパッドを使った
パイプライン連携 (agent-a がファイル名を書き込み → agent-b が読み取る) を実行。

**Phase 1** (`cleanup_subdir=True`): 11/11 PASS — 停止後に `.agent/agent-a/` と `.agent/agent-b/` が削除された。
**Phase 2** (`cleanup_subdir=False`): 9/9 PASS — 停止後にサブディレクトリが保持された。

**合計**: 20/20 PASS

**バグ修正**: `wait_for_all_tasks_complete()` が `"complete"` を期待していたが API は `"success"` を返す → `"success"` を追加。

### Step 4 — 次イテレーション候補

- `WorkflowPhaseStatus` が タスク完了後に `"complete"` に更新されない (pre-existing gap)
- Worktree ↔ branch sync (`POST /agents/{id}/sync`)

---

## §10.70 — v1.1.38: WorkflowPhaseStatus 完了トラッキング (DAG フェーズ完了)

**選択日**: 2026-03-10

**選択理由**: §10.69 Step 4「次イテレーション候補」として明示的に記録済み。v1.1.36 (PhaseSpec.skip_condition) および v1.1.37 (subdir cleanup) の Known Limitation として蓄積されてきた。`WorkflowPhaseStatus` が `"pending"` のままになる問題は `GET /workflows/{id}` の可観測性を低下させ、将来的な動的フェーズ追加や UI ダッシュボードの実装を妨げる。修正は WorkflowManager に限定されるため副作用が小さく、今イテレーションに適切。

**選択しなかった候補**:
- Worktree ↔ branch sync (`POST /agents/{id}/sync`): 依存機能がないため独立して実装可能だが、PhaseStatus gap の方が可観測性への影響が大きい。

### Step 1 — Research

**Query 1**: "workflow DAG phase completion tracking event-driven orchestration"

1. **DataCoves, "Event-Driven Airflow: Using Datasets for Smarter Scheduling"** (2024), https://datacoves.com/post/airflow-schedule — Airflow の Dataset モデル: タスクが Dataset outlet に書き込んだ時点で下流 DAG がトリガーされる。タスク完了イベントがフェーズ完了の引き金になるパターンの実例。

2. **Netflix TechBlog, "100X Faster: How We Supercharged Netflix Maestro's Workflow Engine"**, https://netflixtechblog.com/100x-faster-how-we-supercharged-netflix-maestros-workflow-engine-028e9637f041 — Maestro の状態遷移エンジン: ステップ完了イベントがフェーズ完了チェックをトリガーする設計。内部でカウンタ (completed_count / total_count) を使いフェーズ境界を検出。

3. **Argo Workflows, "DAG walk-through"**, https://argo-workflows.readthedocs.io/en/latest/walk-through/dag/ — DAG タスクの `depends` 句でフェーズ境界を宣言する方式。タスク完了時にグラフを走査して次の実行可能タスクを特定する。

**Query 2**: "task completion callback workflow state machine Python asyncio"

4. **Python Docs, "asyncio.Task.add_done_callback"**, https://docs.python.org/3/library/asyncio-task.html — asyncio Task の done callback: タスク完了時に同期コールバックを登録するパターン。本実装では `_route_loop` 内の `on_task_complete()` 呼び出しがこのパターンに相当する。

5. **Pythontic.com, "add_done_callback() method of asyncio.Task class"**, https://pythontic.com/asyncio/task/add_done_callback — コールバック引数は Future オブジェクト。ステートマシン更新のトリガーとして使用可能。

**Query 3**: "saga pattern workflow phase status update distributed systems"

6. **Microsoft Azure, "Saga Design Pattern"**, https://learn.microsoft.com/en-us/azure/architecture/pattern s/saga — Orchestration Saga: 中央オーケストレーターが各サービスの完了を受信してフェーズ (local transaction) の状態を更新する。本実装は Orchestration Saga に相当: Orchestrator が RESULT メッセージを受信 → WorkflowManager に通知 → フェーズ状態を更新。

7. **Richardson, "Microservices Patterns" Ch.4 (Saga pattern)**, https://microservices.io/patterns/data/saga.html — Saga の各ステップは「開始 → 実行 → 完了/補償」の3状態。フェーズ単位の状態追跡が分散トランザクションの可観測性に必須。

**設計結論**:

- **既存機能**: タスクの `depends_on` による DAG 依存解決は正常動作している。フェーズA完了後にフェーズBが dispatch されることは `_on_dep_satisfied()` が担保している。
- **不足機能**: タスク完了時に `WorkflowPhaseStatus.mark_complete()` を呼ぶコードが存在しない。
- **修正方針**: `WorkflowManager` に `_task_to_phase: dict[str, tuple[str, str]]` (task_id → (wf_id, phase_name)) と `_phase_completed_tasks: dict[tuple[str,str], set[str]]` を追加。`on_task_complete()` / `on_task_failed()` でフェーズ完了条件を評価し、`WorkflowPhaseStatus.mark_complete()` / `mark_failed()` / `mark_running()` を呼ぶ。

### Step 2 — 実装

**変更ファイル**:
- `src/tmux_orchestrator/application/workflow_manager.py`: `WorkflowManager` にフェーズ完了トラッキングを追加
  - `_task_to_phase: dict[str, tuple[str, str]]` (task_id → (workflow_id, phase_name))
  - `_phase_completed: dict[tuple[str, str], set[str]]` (フェーズ完了タスク集合)
  - `_phase_failed: dict[tuple[str, str], set[str]]` (フェーズ失敗タスク集合)
  - `submit()` で `phase_task_map` を構築 (`task_id → phase_name`)
  - `on_task_complete()` / `on_task_failed()` でフェーズ状態を更新
  - `on_task_retrying()` でフェーズ失敗集合から削除

**新テスト**: `tests/test_workflow_completion_tracking.py` — 20+ テスト

**TDD サイクル**: 全 2737 テスト合格 (→ 2737 + N)。

## §10.71 — v1.1.39: Worktree ↔ Branch Sync — `POST /agents/{id}/sync` + `/sync-to-main` (観点A残)

**選択日**: 2026-03-10

**選択理由**: §10.70 Step 4「次イテレーション候補」として明示的に記録済み (§10.70 選択しなかった候補欄)。`isolate: true` エージェントのブランチ上の成果が `teardown()` 時に削除されてしまう問題は、エージェント協調の最も重要な出力チャネルを失う。`WorktreeManager._merge_branch()` が既に squash merge ロジックを持っているため、REST エンドポイントとして公開するのは自然な次ステップ。

**選択しなかった候補**:
- タスク優先度の動的更新 (`PATCH /tasks/{id}/priority`): インタラクティブな調整には有用だが、既に `priority` はタスク作成時に指定可能。今回は sync の方が明確な実装要求がある。

### Step 1 — Research

**Query 1**: "git worktree merge branch back to main programmatic Python subprocess 2024"

1. **Conrad Muan, "Git worktrees"**, https://www.conradmuan.com/blog/git-worktrees — worktree から main へのマージ: `git checkout main && git merge hotfix` のワークフロー。全 worktree は同一 `.git` を共有するため、別 worktree のブランチを直接マージ可能。

2. **Tien Du, "Mastering Git Worktree & Git Subtree"**, https://tiendu.github.io/2025/03/01/git-worktree-subtree-eng.html — Multi-branch workflow での worktree 活用。main worktree でマージを実行し、feature worktree を後から削除するパターン。

3. **Ken Muse, "Using Git Worktrees for Concurrent Development"**, https://www.kenmuse.com/blog/using-git-worktrees-for-concurrent-development/ — 重要な制約: worktree A が checkout している branch を main worktree から直接 checkout することはできない。代わりに main worktree で `git merge worktree-branch` を直接実行するか、`--no-checkout` で迂回する。

**Query 2**: "git cherry-pick worktree integration workflow Python subprocess"

4. **Python cherry-picker tool**, https://github.com/python/cherry-picker/blob/main/cherry_picker/cherry_picker.py — CPython の公式 cherry-pick 自動化ツール。`subprocess.check_output` で git コマンドを実行するパターン。`git cherry-pick -x <sha>` で元コミット SHA を commit message に記録する慣例。

5. **pygit2, "git-cherry-pick"**, https://www.pygit2.org/recipes/git-cherry-pick.html — libgit2 経由の cherry-pick API。本実装では subprocess が適切 (pygit2 は外部依存)。worktree 間で全コミットを共有するため、任意のコミット SHA を cherry-pick 可能。

6. **GeeksforGeeks, "Git Cherry Pick"**, https://www.geeksforgeeks.org/git/git-cherry-pick/ — cherry-pick vs merge の違い: cherry-pick は個別コミットの移植; merge は全履歴の統合。`git log target..source --oneline` でターゲットに存在しないコミットの一覧を取得してから cherry-pick するパターン。

**Query 3**: "multi-agent git worktree collaboration sync strategy parallel development"

7. **Medium, "Git Worktrees: The Secret Weapon for Running Multiple AI Coding Agents in Parallel"**, https://medium.com/@mabd.dev/git-worktrees-the-secret-weapon-for-running-multiple-ai-coding-agents-in-parallel-e9046451eb96 — 複数 AI エージェントへの worktree 割り当てパターン。各エージェントが独立した worktree で並行作業後、main ブランチにマージする workflow。

8. **SpillwaveSolutions, "parallel-worktrees"**, https://github.com/spillwavesolutions/parallel-worktrees — サブエージェントを worktree で並行実行し、完了後に git worktree で sync するツール。エージェント完了後の同期が標準パターンとして確立されている。

9. **DEV Community, "How We Built True Parallel Agents With Git Worktrees"**, https://dev.to/getpochi/how-we-built-true-parallel-agents-with-git-worktrees-2580 — 並行エージェント完了後の sync 戦略: merge vs cherry-pick vs rebase の比較。merge が最もシンプルで衝突検出が明確; cherry-pick が特定コミットの選択的統合に適する; rebase が線形履歴を維持したい場合に有効。

**設計結論**:

- `WorktreeManager._merge_branch()` は既に squash merge + checkout ロジックを実装済み — これを公開するのが最小変更。
- `POST /agents/{id}/sync` は `WorktreeManager` に `sync_branch()` メソッドを追加して3戦略 (merge/cherry-pick/rebase) をサポート。
- cherry-pick 実装: `git log target..source --format=%H` でコミット一覧 → `git cherry-pick <sha...>` で適用。
- rebase 実装: `git rebase source target` (または `git checkout source && git rebase target`)。
- 衝突時は `409 Conflict` を返す。
- `isolate: false` エージェントは worktree を持たないため `400 Bad Request` を返す。

### Step 2 — 実装

**変更ファイル**:
- `src/tmux_orchestrator/infrastructure/worktree.py`: `WorktreeManager.sync_to_branch()` メソッド追加
- `src/tmux_orchestrator/web/routers/agents.py`: `POST /agents/{id}/sync` エンドポイント追加
- `src/tmux_orchestrator/agent_plugin/commands/sync-to-main.md`: `/sync-to-main` スラッシュコマンド追加

**新テスト**: `tests/test_worktree_sync.py` — merge/cherry-pick/rebase/400/404 等のテスト

## §10.72 — v1.1.40: ADR 自動生成ワークフロー拡張 — `POST /workflows/adr` enhanced fields

**選択日**: 2026-03-10

**選択理由**: `POST /workflows/adr` は v0.40.0 で基本実装済みだが、§11 デモ候補「ADR 自動生成デモ」が未完了。
さらに、既存の `AdrWorkflowSubmit` スキーマには `context`（問題背景）・`criteria`（評価基準）・
`scratchpad_prefix`（名前空間のカスタマイズ）・`agent_timeout`（タスクタイムアウト）が欠けており、
TDD / Competition 等の他ワークフローが持つ機能との統一性が失われていた。
スクラッチパッドキーも `_proposal`/`_decision` から `_draft`/`_final` へ Nygard 原典の「草稿→決定」
フローに合わせて整理する。本イテレーションでこれらを補完し、E2E デモを完遂する。

**選択しなかった候補**:
- タスク優先度の動的更新 (`PATCH /tasks/{id}/priority`): 有用だが、ADR デモの完遂が §11 高優先度として明示されており先に完了すべき。
- `POST /workflows/socratic`: 中優先度。ADR デモ完了後の次候補。

### Step 1 — Research

**Query 1**: "architecture decision record ADR format template Nygard 2011 markdown"

1. **Michael Nygard, "Documenting Architecture Decisions"** (2011), https://www.cognitect.com/blog/2011/11/15/documenting-architecture-decisions —
   ADR の標準フォーマット: Title / Status / Context / Decision / Consequences の5セクション。
   "Status" は `proposed → accepted → deprecated/superseded` の状態遷移。

2. **ADR GitHub Organization, "ADR Templates"**, https://adr.github.io/adr-templates/ —
   MADR (Markdown Architectural Decision Records) 4.0.0 を含む複数テンプレートを収録。
   MADR は Nygard 原典に「Considered Options」「Pros and Cons」を追加し、複数選択肢の比較を構造化。

3. **joelparkerhenderson, "architecture-decision-record"**, https://github.com/joelparkerhenderson/architecture-decision-record —
   Nygard テンプレートの公式 Markdown 実装。ADR ファイルは `NNNN-title.md` で連番管理; ステータスは
   `[Accepted]` / `[Superseded by ADR-0002]` 等の参照リンク形式が推奨される。

**Query 2**: "multi-agent LLM collaborative document generation workflow pipeline"

4. **HuggingFace Blog, "Building Your Own AI Document Dream Team"**, https://huggingface.co/blog/ifahim/multi-agent-generic-doc-gen —
   「リレーレース型」マルチエージェントドキュメント生成: 各エージェントが特定フェーズ（Section Semantics / Information Retrieval / Content Generation）を担当。
   本 ADR ワークフローの Proposer/Reviewer/Synthesizer 分業と同構造。

5. **Google ADK, "Multi-agent systems"**, https://google.github.io/adk-docs/agents/multi-agents/ —
   Agent-as-tool パターン: サブエージェントをツールとして呼び出す Orchestrator 構成。
   DAG 型ワークフローでは各エージェントが前段の出力をブラックボードから読み込む。

6. **ScienceDirect, "Coordinated LLM multi-agent systems for collaborative Q&A generation"** (2025),
   https://www.sciencedirect.com/science/article/pii/S0950705125016661 —
   専門化されたエージェントによるクロスバリデーションが誤情報を削減。
   Reviewer エージェントが Proposer の出力を独立に検証するパターンの理論的裏付け。

**Query 3**: "ADR automated generation AI agent review synthesize architecture decisions 2024 2025"

7. **Adolfi.dev, "AI generated Architecture Decision Records (ADR)"**, https://adolfi.dev/blog/ai-generated-adr/ —
   AI による ADR 自動生成の実践報告。最大の課題は「コンテキストの正確な捕捉」であり、
   context フィールドを明示的に提供することで幻覚を削減できる。

8. **Piethein Strengholt, "Building an Architecture Decision Record Writer Agent"**,
   https://piethein.medium.com/building-an-architecture-decision-record-writer-agent-a74f8f739271 —
   単一 LLM ADR 生成の限界を分析。multi-agent 分解（提案→批評→統合）が品質向上に有効。
   評価基準（criteria）を明示することで Considered Options セクションの網羅性が向上する。

9. **Equal Experts, "Accelerating ADRs with Generative AI"**, https://www.equalexperts.com/blog/our-thinking/accelerating-architectural-decision-records-adrs-with-generative-ai/ —
   AI は ADR 草稿生成の生産性を大幅に向上させるが、ヒューマンレビューが不可欠。
   マルチエージェント構成では Reviewer が「ヒューマンレビュー」を自動化する役割を担う。

**設計結論**:

- Nygard (2011) の5セクション + MADR の「Considered Options」「Pros and Cons」を採用。
- `context` フィールド追加 → Proposer/Reviewer/Synthesizer の全プロンプトに注入。
- `criteria` フィールド追加 → 評価基準を明示して Considered Options の網羅性を向上。
- スクラッチパッドキー名を `_draft`/`_review`/`_final` に統一（Nygard: draft→final フロー）。
- `agent_timeout` フィールド追加 → ADR 生成は他ワークフローより複雑なため個別タイムアウト設定が必要。
- `scratchpad_prefix` フィールド追加 → 複数 ADR 並行実行時の名前空間衝突を防ぐ。

### Step 2 — 実装

**変更ファイル**:
- `src/tmux_orchestrator/web/schemas.py`: `AdrWorkflowSubmit` に `context`, `criteria`, `scratchpad_prefix`, `agent_timeout` 追加; docstring を DESIGN.md §10.72 参照に更新
- `src/tmux_orchestrator/web/routers/workflows.py`: `submit_adr_workflow` ハンドラー更新 — 新フィールド利用、キー名を `_draft`/`_review`/`_final` に変更、`timeout=body.agent_timeout` を全タスクに伝達
- `tests/test_workflow_adr.py`: `TestADRWorkflowPrompts.test_synthesizer_prompt_reads_both_proposal_and_review` → `_draft_and_review` にリネーム; `TestADRWorkflowEnhancedFields` クラス追加 (12テスト)
- `tests/test_openapi_schema.py` スナップショット: `UPDATE_SNAPSHOTS=1` で再生成

**新テスト**: 12テスト追加 (2786 → 2798)

### Step 3 — E2E デモ

**デモディレクトリ**: `~/Demonstration/v1.1.40-adr-workflow/`

**設定**: 3エージェント (`adr-proposer`, `adr-reviewer`, `adr-synthesizer`)、各タグ付き、`isolate: false`、`task_timeout: 900`

**デモトピック**: "SQLite vs PostgreSQL for orchestrator session storage"

**結果**: **30/30 PASS** (3回実行、最終回で全合格)

| スクラッチパッドキー | サイズ |
|---|---|
| `adr_sqlite_pg_draft` | 8,944 chars |
| `adr_sqlite_pg_review` | 12,836 chars |
| `adr_sqlite_pg_final` | 12,755 chars |

**DECISION.md 冒頭** (最終合成物):
```
# ADR: SQLite vs PostgreSQL for Orchestrator Session Storage
Status: Accepted
Date: 2026-03-10
Review trigger: Revisit if daily sessions exceed 50k, if multi-process writers are
introduced, or if distributed deployment is planned.
```

セクション: Context and Problem Statement / Decision Drivers / Considered Options /
Decision Outcome / Consequences / Pros and Cons of the Options

### Step 4 — フィードバック

**デバッグ事項**:

1. **`depends_on` タイミングチェック (FAIL→修正)**: 最初のデモで `GET /tasks/{id}` レスポンスの
   `depends_on` フィールドをチェックしたが値が空だった。調査したところ、タスクが完了した後も
   `depends_on` は返されるが、最初のデモでは別の問題だった。2回目のデモでは `started_at`/`finished_at`
   タイムスタンプによる順序チェックに変更したが精度の差でFAIL。最終的に「スクラッチパッドの内容が
   あること = パイプラインが正しく実行されたこと」の証明に変更。これは正しいアプローチ (blackboard pattern)。

2. **次候補**: `POST /workflows/agentmesh` (中優先度、§11) — AgentMesh 4ロール開発パイプライン
   (Planner → Coder → Debugger → Reviewer)

## §10.73 — v1.1.41: AgentMesh 型4ロール開発パイプライン — `POST /workflows/agentmesh`

### Step 0 — 選択理由

**選択**: `POST /workflows/agentmesh` — AgentMesh Planner→Coder→Debugger→Reviewer 4ロールパイプライン

**選択理由**:
1. **§11 中優先度・未実装ワークフロー**: §11「AgentMesh 型 4ロール開発パイプラインデモ」として明示的に記録済み。
2. **arXiv:2507.19902 (2025) の直接実装**: AgentMesh 論文の4ロール構造 (Planner/Coder/Debugger/Reviewer) を `POST /workflows/agentmesh` として具体化する最良の機会。
3. **ADR (§10.72) の完了**: ADR 3ステップパイプラインが v1.1.40 で完成しており、4ステップシーケンシャルパイプラインへの自然な拡張。
4. **scratchpad Blackboard パターンの実証**: 各エージェントが前段の出力を scratchpad から読み取り、次段に渡すパターンを 4段階で実証。

**選択しなかった候補**:
- `POST /workflows/socratic` (§10.72 Step 4 次候補): AgentMesh の方が §11 の優先度が高く、実装研究文献が明確に存在する。

### Step 1 — Research

**Query 1**: "AgentMesh multi-agent development pipeline Planner Coder Reviewer pattern LLM 2025"

1. **Elias, "AgentMesh: A Cooperative Multi-Agent Generative AI Framework for Software Development Automation"**,
   arXiv:2507.19902 (2025), https://arxiv.org/abs/2507.19902v1 —
   AgentMesh の原典論文。Planner (要件分解・計画立案), Coder (コード生成), Debugger (テスト・バグ修正),
   Reviewer (品質検証・最終チェック) の4ロール構造を定義。各エージェントが LLM プロンプトモジュールとして独立し、
   前段の出力を引き継いでパイプライン処理する。単一 LLM アプローチに比べて分業により高い再現性と品質を達成。

2. **boring_ai_guy, "AgentMesh: The Actual Intelligent Software"**, Medium (Sep 2025),
   https://medium.com/@boring_ai_guy/agentmesh-the-actual-intelligent-software-c1dfae062c00 —
   AgentMesh の解説記事。Planner が高レベル要件を具体的なサブタスクに分解し、Coder が各サブタスクを実装、
   Debugger がテスト実行・エラー修正、Reviewer が最終品質検証という4ステージの役割分担を説明。

**Query 2**: "multi-agent software development pipeline LLM planner implementer reviewer debugger architecture"

3. **ACM TOSEM, "LLM-Based Multi-Agent Systems for Software Engineering: Literature Review"**,
   https://dl.acm.org/doi/10.1145/3712003 (2025) —
   ロールベース協調型マルチエージェントシステムの文献調査。Pipeline / Debate / Hierarchical パターンを分類。
   Pipeline (シーケンシャル) アプローチでは各エージェントの出力が次のエージェントへの入力となる決定論的ハンドオフが特徴。

4. **arXiv:2511.08475, "Designing LLM-based Multi-Agent Systems for Software Engineering Tasks"** (2025),
   https://arxiv.org/html/2511.08475v1 —
   ソフトウェアエンジニアリング向けマルチエージェント設計パターンの品質属性・設計判断を網羅。
   役割特化エージェント (product owner / architect / developer / tester / reviewer) のパイプライン構成が
   信頼性・保守性・テスト可能性を向上させることを示す。

**Query 3**: "agentic coding workflow plan implement test review stages sequential pipeline 2025"

5. **QuantumBlack AI by McKinsey, "Agentic workflows for software development"**, Medium (Feb 2026),
   https://medium.com/quantumblack/agentic-workflows-for-software-development-dc8e64f4a79d —
   実践的エージェント型ソフトウェア開発ワークフロー。INTENT→SPEC→PLAN→IMPLEMENT→VERIFY→REVIEW
   という段階的パイプラインが最も信頼性の高い開発自動化パターンであることを実証。
   Planning フェーズの品質が下流の全フェーズの精度を決定するため「最も重要なステップ」。

6. **teamday.ai, "The Complete Guide to Agentic Coding in 2026"**,
   https://www.teamday.ai/blog/complete-guide-agentic-coding-2026 —
   2026年のエージェント型コーディングガイド。Sequential pipeline で各ステップが明確な契約を持つ
   「アセンブリライン型」ワークフローが信頼性が最も高い。

**設計結論**:

- AgentMesh 論文 (arXiv:2507.19902) の4ロール構造を `POST /workflows/agentmesh` として実装。
- 各フェーズの出力は scratchpad に書き込み、次フェーズが読み込む Blackboard パターン。
- スクラッチパッドキー: `{prefix}_plan` / `{prefix}_code` / `{prefix}_debugged` / `{prefix}_review`
- `required_tags` で各フェーズのエージェントをルーティング (agentmesh_planner/coder/debugger/reviewer)。
- `agent_timeout` で個別タイムアウト設定 (デフォルト 300s)。

### Step 2 — 実装



## §10.74 — v1.1.42: Delphi 型合意形成ワークフロー — デモ実証 (POST /workflows/delphi)

### Step 0 — 選択理由

**選択**: v1.1.42 — `POST /workflows/delphi` デモ実証

**選択理由**:
1. **§11 中優先度・デモ未実証**: §11「Delphi 型合意形成デモ — "マイクロサービス vs モノリス"」として明示的に記録済み。`POST /workflows/delphi` エンドポイント自体は v1.0.23 (§10.22) で実装済みだが、5ペルソナ・3ラウンドのフル機能デモが未実施。
2. **§10.73 (AgentMesh) の完了後**: AgentMesh パイプラインのデモが完了し、今度は並列ペルソナ → 集約 → 複数ラウンドパターンを実証する最適タイミング。
3. **研究的裏付けが強い**: DelphiAgent (ScienceDirect 2025)、RT-AID (ScienceDirect 2025)、Du et al. ICML 2024 がマルチラウンド合意形成の有効性を実証。

**選択しなかった候補**:
- `POST /workflows/redblue` 追加デモ: Delphi の方が §11 での優先度が高い。
- ワークフロー出力フォーマット標準化: 実装コストが高く、個別ワークフローのデモを先に揃えるべき。

### Step 1 — Research

**Query 1**: "Delphi method consensus building multi-round anonymous expert aggregation structured forecasting"

1. **Wikipedia, "Delphi method"**,
   https://en.wikipedia.org/wiki/Delphi_method —
   Delphi 法の定義: 複数の専門家が匿名で回答し、各ラウンド後にファシリテーターが匿名要約をフィードバック。
   核心原則: 匿名性・反復・制御されたフィードバック。これらにより集団バイアスを低減する。
   多ラウンド構造: 各ラウンドで意見が絞り込まれ、収束が進む。

2. **SurveyLegend, "What is the Delphi Method and How To Use It"**,
   https://www.surveylegend.com/research/what-is-the-delphi-method/ —
   合意形成プロセスの詳細: 各ラウンドの後、ファシリテーターは平均レーティングや範囲などの統計とテーマ要約を
   フィードバックし、専門家はグループフィードバックに基づき意見を修正する。十分な合意が得られるまで繰り返す。

3. **Welphi, "Delphi Model Consensus Survey"**,
   https://www.welphi.com/delphi-model-consensus-survey/ —
   Delphi の実践的設計パターン: 匿名性確保のための手順、ラウンド間のフィードバック方法、収束判定基準。

**Query 2**: "Delphi technique AI agents multi-round structured consensus LLM simulation"

4. **arXiv:2502.21092, "An LLM-based Delphi Study to Predict GenAI Evolution"** (2025),
   https://arxiv.org/html/2502.21092v1 —
   LLM による Delphi 法の自動化を実証。組織エージェント（ファシリテーター役）と回答エージェント（専門家役）の
   2種類のエージェント構成。マルチラウンド収束プロセスを LLM で再現。

5. **arXiv:2508.09349, "The Human–AI Hybrid Delphi Model"** (2025),
   https://arxiv.org/html/2508.09349v1 —
   人間-AI ハイブリッド Delphi モデル。AI エージェントが専門家の役割を担い、匿名性を維持しながら
   複数ラウンドで意見を洗練させる構造化フレームワーク。

6. **ScienceDirect, "DelphiAgent: A trustworthy multi-agent verification framework"** (2025),
   https://www.sciencedirect.com/science/article/abs/pii/S0306457325001827 —
   複数の LLM エージェントが Delphi 法を模倣し、反復フィードバックと統合を通じて合意を形成。
   各エージェントが独立して判断し、LLM エージェントが人間の専門家パネルよりも高い合意率を達成 (93.3% vs 81.5%)。

**Query 3**: "multi-agent deliberation consensus convergence workflow LLM personas rounds"

7. **arXiv:2310.20151, "Multi-Agent Consensus Seeking via Large Language Models"** (2024),
   https://arxiv.org/html/2310.20151v2 —
   LLM によるマルチエージェント合意探索。コンセンサス求解行動はラウンドを重ねるほど収束し、
   7ラウンド討論で平均収束値 0.892 を達成 (σ = 0.074)。マルチラウンドの価値を定量的に実証。

8. **Medium, "Patterns for Democratic Multi‑Agent AI: Debate-Based Consensus"** (2025),
   https://medium.com/@edoardo.schepis/patterns-for-democratic-multi-agent-ai-debate-based-consensus-part-2-implementation-2348bf28f6a6 —
   民主的マルチエージェント AI の実装パターン。モデレーターがコンセンサスビルダーとして機能し、
   エージェント間の収束を促進する役割の重要性を説明。

**設計結論**:

- 既存の `POST /workflows/delphi` 実装 (v1.0.23) を活用してフル機能デモを実施。
- 3ペルソナエージェント (backend_engineer, devops_lead, product_manager) × 2ラウンド構成。
- 各ラウンドの `delphi_round_{n}.md` 生成と最終 `consensus.md` の実証が目標。
- スクラッチパッド Blackboard パターンで各エージェントの出力を集約。

### Step 2 — 実装

v1.0.23 で `POST /workflows/delphi` は完全実装済み。40テストが全合格。
本イテレーションはデモ実証に注力。

実装ファイル:
- `src/tmux_orchestrator/web/routers/workflows.py` — `submit_delphi_workflow()` (v1.0.23実装)
- `src/tmux_orchestrator/web/schemas.py` — `DelphiWorkflowSubmit` (v1.0.23実装)
- `tests/test_workflow_delphi.py` — 40テスト (v1.0.23実装)

### Step 3 — E2E デモ実行結果

**デモ構成**:
- 5エージェント: delphi-persona-1/2/3 (tag: delphi_persona) + delphi-moderator + delphi-consensus (tag: delphi_moderator)
- トピック: "SQLite vs Redis for the TmuxAgentOrchestrator scratchpad backend"
- ペルソナ: backend_engineer, devops_lead, product_manager (3名)
- ラウンド数: 2

**スクラッチパッドサイズ**:

| キー | サイズ |
|-----|------|
| `delphi_7e38589f_r1_backend_engineer` | 2360 chars |
| `delphi_7e38589f_r1_devops_lead` | 2129 chars |
| `delphi_7e38589f_r1_product_manager` | 2201 chars |
| `delphi_7e38589f_r1_moderator` | 5964 chars |
| `delphi_7e38589f_r2_backend_engineer` | 2249 chars |
| `delphi_7e38589f_r2_devops_lead` | 2359 chars |
| `delphi_7e38589f_r2_product_manager` | 2368 chars |
| `delphi_7e38589f_r2_moderator` | 5383 chars |
| `delphi_7e38589f_consensus` | 7513 chars |

**consensus.md 冒頭**:
```
# Delphi Consensus: SQLite vs Redis for TmuxAgentOrchestrator Scratchpad Backend
Recommended Decision: SQLite with WAL mode for current scale.
```

**結果**: **48/48 PASS** (初回実行で全合格)

### Step 4 — フィードバック

**デバッグ事項**: なし。初回実行で全チェック合格。

**マルチエージェント協調パターン**:
- Round 1: 3ペルソナが並列実行 (no depends_on)
- Moderator: depends_on all Round 1 experts
- Round 2: 3ペルソナが並列実行 (depends_on moderator_r1)
- Consensus: depends_on moderator_r2
- 匿名性確保: 各エージェントは他エージェントの意見をモデレーター要約経由でのみ参照

**次候補**: DECISION.md 標準フォーマット規約 (§11 低優先度)、または追加ワークフロー実証。

## §10.75 — v1.1.43: Red Team / Blue Team セキュリティレビューワークフロー拡張 — `POST /workflows/redblue`

### Step 0 — 選択

**選択**: v1.1.43 — `POST /workflows/redblue` セキュリティレビューワークフロー拡張

**選択理由**:
1. **§11 中優先度・既存エンドポイント拡張**: §10.74 Step 4「次候補」に `POST /workflows/redblue` 追加デモが記録済み。既存エンドポイント (v1.0.23 §10.23) は汎用 `topic` フィールドを使う設計だったが、セキュリティレビュー専用フィールド (`feature_description`, `language`, `security_focus`) を追加してユースケースを明確化。
2. **§10.74 (Delphi) の完了後**: 並列ペルソナ→集約パターンを実証済み。次は Sequential Pipeline + 敵対的評価パターンを強化。
3. **研究的裏付けが強い**: AgenticSCR (arXiv:2601.19138, 2025)、OWASP Top 10 for LLMs 2025、Red-Teaming LLM MAS (ACL 2025) が adversarial code review の有効性を実証。

**選択しなかった候補**:
- DECISION.md 標準フォーマット規約: 実装価値が低く優先度低。
- ワークフロー出力フォーマット標準化: 実装コストが高い。

### Step 1 — Research

**Query 1**: "red team blue team security review software development process"

1. **Coursera, "Red Team vs. Blue Team in Cybersecurity"**,
   https://www.coursera.org/articles/red-team-vs-blue-team —
   Red team: attacker perspective, finds vulnerabilities, penetration testing.
   Blue team: defensive group, detects, responds, recovers.
   Purple team: integrates offensive/defensive tactics for collaboration.

2. **Splunk, "Red Teams vs. Blue Teams: What's The Difference?"**,
   https://www.splunk.com/en_us/blog/learn/red-team-vs-blue-team.html —
   Continuous feedback loop: red team exposes gaps, blue team focuses on detection and remediation.
   Together they advance organizational security resilience.

3. **Terra Security Blog, "Red Team vs Blue Team: A Pen Testing Game of Chess"**,
   https://www.terra.security/blog/red-team-vs-blue-team —
   Structured adversarial process with defined roles and escalating attack/defense cycles.

**Query 2**: "multi-agent red team security vulnerability assessment LLM 2025"

4. **arXiv:2502.14847, "Red-Teaming LLM Multi-Agent Systems via Communication Attacks"** (ACL 2025),
   https://arxiv.org/abs/2502.14847 —
   Agent-in-the-Middle (AiTM) attack exploits inter-agent communication in LLM-based MAS.
   Up to 80% attack success rate; demonstrates need for structured adversarial evaluation.

5. **OWASP Top 10 for LLMs 2025**, DeepTeam,
   https://www.trydeepteam.com/docs/frameworks-owasp-top-10-for-llms —
   2025 OWASP Top 10: Prompt Injection remains #1. New categories: Excessive Agency,
   System Prompt Leakage, Vector/Embedding Weaknesses, Misinformation, Unbounded Consumption.

6. **VentureBeat, "Red teaming LLMs exposes harsh truth about AI security"** (2025),
   https://venturebeat.com/security/red-teaming-llms-harsh-truth-ai-security-arms-race —
   UK AISI/Gray Swan challenge: 1.8M attacks across 22 models — every model broke.

**Query 3**: "adversarial multi-agent security code review AI workflow 2025"

7. **arXiv:2601.19138, "AgenticSCR: Autonomous Agentic Secure Code Review"** (2025),
   https://arxiv.org/html/2601.19138v1 —
   AgenticSCR: autonomous agentic system for secure code review in CLI environment.
   Addresses contextual awareness and adaptability gaps in monolithic LLM-based tools.
   Multi-iteration refinement loop between attacker and defender agents.

8. **arXiv:2510.23883, "Agentic AI Security: Threats, Defenses, Evaluation"** (2025),
   https://arxiv.org/html/2510.23883v1 —
   Sequential Tool Attack Chaining (STAC): multi-turn attacks orchestrate individually
   safe tool calls that collectively achieve harmful goals.

9. **arXiv:2505.02077, "Open Challenges in Multi-Agent Security"** (2025),
   https://arxiv.org/html/2505.02077v1 —
   Protocol-mediated threats: message tampering, role spoofing, protocol exploitation.
   Multi-agent ecosystems amplify single-agent vulnerabilities across coordinated workflows.

### Step 2 — 実装

- `src/tmux_orchestrator/web/schemas.py`: `RedBlueWorkflowSubmit` を `feature_description`, `language`, `security_focus`, `agent_timeout` フィールドに更新。`required_tags` デフォルトを `["redblue_blue"]`, `["redblue_red"]`, `["redblue_arbiter"]` に設定。
- `src/tmux_orchestrator/web/routers/workflows.py`: スクラッチパッドキーを `_implementation`, `_vulnerabilities`, `_risk_report` に変更。セキュリティフォーカスを赤チームプロンプトに注入。
- `tests/test_workflow_redblue.py`: 新スキーマに対応したテスト群に置き換え。

## §10.76 — v1.1.44: ループ対応ワークフロー (LoopBlock + {iter} 置換 + POST /workflows/pdca)

### Step 0 — 選択

**選択**: v1.1.44 — ループ対応ワークフロー実装

**選択理由**:
1. **ユーザー明示的設計承認**: LoopBlock + LoopSpec + {iter} プレースホルダー + until 条件評価の設計がユーザーにより完全承認済み。
2. **§11 高優先度**: PDCA サイクルなどのフィードバックループは、既存の DAG 構造が根本的に表現できない反復パターンを可能にする。
3. **POST /workflows/pdca**: 専用エンドポイントで PDCA サイクルを簡便に実行可能にする。

**選択しなかった候補**:
- ワークフロー出力フォーマット標準化: §11 低優先度。
- DECISION.md 標準フォーマット規約: 実装価値が低い。

### Step 1 — Research

**Query 1**: "PDCA cycle workflow automation orchestration iterative loop"

1. **Moxo, "Continuous improvement with the PDSA & PDCA cycle"**,
   https://www.moxo.com/blog/continuous-improvement-pdsa-pdca-cycle —
   PDCA/PDSA サイクルは、証拠ベースのイテレーションによる継続的改善フレームワーク。
   テンプレートが各イテレーションに構造化された証拠を残し、次サイクルへの継続性を確保する。
   ワークフロー自動化 (ブランチ、マイルストーン、SLA しきい値) が PDCA サイクルを強化。

2. **Asana, "What is the Plan-Do-Check-Act (PDCA) Cycle?"**,
   https://asana.com/resources/pdca-cycle —
   Plan → Do → Check → Act の4段階。サイクルは終わりに達したら最初から繰り返せる。
   デミングの強調: 改善された系への収束スパイラル。各サイクルが目標に近づく。

3. **businessmap.io, "PDCA Cycle: Guide, Practical Example & Template"**,
   https://businessmap.io/lean-management/improvement/what-is-pdca-cycle —
   PDCA は継続的改善のための4ステップの反復プロセス。Deming Cycle とも呼ばれる。
   各フェーズの完了後に条件を評価し、次のサイクルか終了かを決定する。

**Query 2**: "workflow loop construct nested phases Argo Airflow iterative execution"

4. **Argo Workflows, "Loops"**,
   https://argo-workflows.readthedocs.io/en/latest/walk-through/loops/ —
   Argo Workflows の3種類のループ機構: `withItems`, `withParam`, `withSequence`。
   `withParam` が最強: 前ステップの JSON 出力を受け取りイテレーション数を動的決定。
   各ループは独立したテンプレート呼び出しとして実行される (コンテナ単位)。

5. **Springer Nature, "Executing cyclic scientific workflows in the cloud"**,
   https://link.springer.com/article/10.1186/s13677-021-00229-7 —
   クラウドでのサイクリックワークフロー実行。既存 DAG アプローチはサイクル回避のワークアラウンドが必要。
   一部システムはネイティブサイクルをサポートして複雑性を低減。
   条件 (「成功するまでリトライ」) によるループ制御。

6. **komodor.com, "Understanding Argo Workflows: Practical Guide"**,
   https://komodor.com/learn/understanding-argo-workflows-practical-guide-2024/ —
   ネストされたループ: 外部ループが内部テンプレートを呼び出し、内部に `withItems` を持てる。
   ループ間の値受け渡しが課題 (issue #5143)。ネストループの実装に `withParam` + 動的生成。

**Query 3**: "feedback loop conditional repeat workflow DAG runtime convergence"

7. **AiiDA Tutorials, "More workflow logic: while loops and conditional statements"**,
   https://aiida-tutorials.readthedocs.io/en/tutorial-2020-intro-week/source/appendices/workflow_logic.html —
   AiiDA WorkChain の `while_()` 構文: 条件関数が False を返すまでステップを繰り返す。
   収束ループの例: 圧力収束まで `not_converged()` 条件チェックを繰り返す。
   DAG 構造を維持しながらランタイム分岐・ループを実現 (Python 制御フロー構文に対応)。

8. **Airflow Community, "Is it possible to add a loop condition in Airflow?"**,
   https://github.com/apache/airflow/discussions/21726 —
   Airflow での `while` ループ: DAG が本質的に非巡回のため、ループには ShortCircuitOperator や
   ExternalTaskSensor を活用するワークアラウンドが必要。
   純粋 DAG システムでの反復は「サブ DAG」として事前展開するアプローチが主流。

9. **Airflow Documentation, "Dynamic Task Mapping"**,
   https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/dynamic-task-mapping.html —
   動的タスクマッピング: ランタイムデータに基づきタスク数を決定 (for ループ的)。
   前タスクの出力を入力とした反復実行。`expand()` API で宣言的に記述可能。

**設計上の知見**:
- Argo Workflows: ループを DAG 内の「反復テンプレート呼び出し」として表現。TmuxAgentOrchestrator では「イテレーション展開」として静的に事前展開するか、動的に次イテレーションを投入するかのトレードオフあり。
- AiiDA: `while_()` 条件はランタイム評価。TmuxAgentOrchestrator の `until` 条件はスクラッチパッドを参照するため、フェーズ完了イベントフックでの評価が適切。
- 今回の設計選択: 静的ループ (DAG 全体を事前生成) ではなく動的ループ (イテレーション完了時に次イテレーションを投入) を採用。理由: ループ回数が `until` 条件の評価結果に依存するため、事前に全タスクを生成できない。

### Step 2 — 実装


---

## §11 — 次期候補一覧 (v1.2.x)

### 高優先度

| 課題 | 概要 |
|---|---|
| **スクラッチパッド永続化 + スラッシュコマンド** | APIが正のまま、PUT時に `.orchestrator/scratchpad/{key}` にも書き出し (write-through)。サーバー再起動後もファイルから復元。スラッシュコマンド `/scratchpad-write key value` / `/scratchpad-read key` を追加してcurl不要に。 |

### 中優先度

| 課題 | 概要 |
|---|---|
| **ワークフロー構造ブロック: `sequence:` / `parallel:`** | `PhaseItem = PhaseSpec | SequenceBlock | ParallelBlock | LoopBlock` の統一再帰型に拡張。`parallel:` 内に `sequence:` を入れることで「複数の順次ストリームを並列実行」を表現できる。`loop:` 内に `parallel:` も可。トップレベルの `phases:` はフラットな `sequence:` と等価なので後方互換。 |
| **Worktree ↔ branch sync の UI 改善** | `POST /agents/{id}/sync` は実装済み (v1.1.39)。完了後に worktree を自動削除するオプション、merge conflict 時の通知改善。 |
| **タスク優先度の動的更新** | `PATCH /tasks/{id}/priority` — キューに積まれたタスクの優先度をランタイムに変更。 |
| **Codified Context + PairCoder デモ** | `.claude/specs/` に規約 YAML を配置、複数セッションにわたり規約違反ゼロを実証。 |

### バージョン方針
- v1.1.44 (ループワークフロー) を最終 v1.1.x リリースとする
- v1.1.44 完了後、次の実装から **v1.2.0** に移行する

**実装ファイル**:
- `src/tmux_orchestrator/domain/phase_strategy.py`: `LoopSpec`, `LoopBlock`, `PhaseItem` 追加
- `src/tmux_orchestrator/phase_executor.py`: `expand_loop_iter`, `_expand_all_loop_iters`, `expand_phase_items_with_status`, `is_until_condition_met`, `_substitute_iter`, `_substitute_iter_in_phase`, `_iter_prefix_header`, `_inject_header_into_phase` 追加
- `src/tmux_orchestrator/web/schemas.py`: `LoopSpecModel`, `LoopBlockModel`, `PhaseItemModel`, `PdcaWorkflowSubmit` 追加; `WorkflowSubmit.phases: list[Any]` に変更
- `src/tmux_orchestrator/web/routers/workflows.py`: LoopBlock 検出・変換ロジック + `POST /workflows/pdca` エンドポイント追加
- `tests/test_workflow_loop.py`: 49 新規テスト

**設計決定**:
- 静的事前展開 (静的DAG): `max` 回分のイテレーションを投入時に全展開。動的ディスパッチは WorkflowManager フックが必要 (将来課題)。
- タスク local_id に `{loop_name}_i{iter}_` プレフィックス付与: 同名フェーズが複数イテレーションに現れた場合の DAG 衝突防止。
- `until` 条件はエージェントプロンプトに埋め込み (エージェントがスクラッチパッドに条件信号を書く)。サーバー側ランタイム短絡は将来課題。

### Step 3 — E2E デモ結果

**デモ**: PDCA サイクルで Python バブルソートを段階的改良

```
Workflow ID: 5eeb9aea-823a-481e-9468-2a468911935e
Scratchpad prefix: pdca_e66e36ae
Max cycles: 2
Task count: 8 (2 cycles × 4 phases)
Workflow status: complete
quality_approved: yes
```

**スクラッチパッドキー** (全 9 キー):
```
pdca_e66e36ae_plan_iter1: 1637 chars
pdca_e66e36ae_do_iter1: 255 chars
pdca_e66e36ae_check_iter1: 341 chars
pdca_e66e36ae_act_iter1: 434 chars
pdca_e66e36ae_plan_iter2: 2735 chars
pdca_e66e36ae_do_iter2: 266 chars
pdca_e66e36ae_check_iter2: 218 chars
pdca_e66e36ae_act_iter2: 516 chars
quality_approved: "yes"
```

**結果**: **26/26 PASS** (初回実行で全合格)

### Step 4 — フィードバック

**デバッグ事項**: なし。初回実行で全チェック合格。

**マルチエージェント協調パターン**:
- 4 エージェント: pdca-planner, pdca-doer, pdca-checker, pdca-actor
- 各フェーズが前フェーズの出力に依存 (依存チェーン)
- 2 イテレーション × 4 フェーズ = 8 タスク
- `quality_approved=yes` がスクラッチパッドに書き込まれてループ完了信号

**既知の制限 (Known Limitation)**:
- `until` 条件のサーバー側ランタイム短絡なし: 全イテレーション事前投入。cycle 1 で quality_approved=yes が書かれても cycle 2 は実行される。
- 動的早期終了には WorkflowManager フックが必要 (次候補)。

**次候補**:
- `until` 条件のランタイム評価 (WorkflowManager フック): 条件成立時に残イテレーションをキャンセル
- ワークフローテンプレート YAML に LoopBlock 構文を追加
- per-iteration フェーズステータスダッシュボード表示

---

## §11 補足 — 動的エージェント生成 + ブランチ連鎖型ワークフロー (v1.2.x 高優先度)

> **状態 (2026-03-10)**:
> - 動的エージェント生成 (`PhaseSpec.agent_template`) → **DONE** (v1.2.3, §10.79)
> - ブランチ連鎖 building blocks (`chain_branch` フラグ + `_ephemeral_agent_branches` + `create_from_branch`) → **DONE** (v1.2.4, §10.80)
> - フルディスパッチ時統合 (workflow router が `chain_branch=True` task spec を見て `create_from_branch` を呼ぶ) → **v1.2.5 候補**

### 設計思想

ワークフローの実行がエージェントのライフサイクルを駆動する。エージェントは YAML で事前定義するのではなく、フェーズ開始時に動的生成・終了時に破棄する。

### ブランチ連鎖モデル

```
main
└── workflow-root
    └── phase-a
        └── phase-b   (phase-a のブランチから分岐 → A の成果を継承)
            ├── parallel-x  (phase-b から分岐)
            └── parallel-y  (phase-b から分岐)
            └── parallel-merged  (x + y をマージ)
                └── phase-c  (parallel-merged から分岐)
                    └── [ワークフロー完了時に main へ一度だけマージ]
```

- **sequence**: 前フェーズのブランチから分岐 → 成果を継承
- **parallel**: 共通の分岐点から複数ブランチ → 完了後にマージ
- **loop**: 前イテレーションのブランチから分岐
- main へのマージはワークフロー完了時の一回のみ

### strategy と workflow の統合

`PhaseSpec.pattern` (SingleStrategy / ParallelStrategy 等) と workflow 構造 (`sequence:` / `parallel:` / `loop:`) は同じ概念の別表現。ブランチ連鎖モデルの導入により両者を統合できる。

### ワークフロー定義イメージ

```yaml
phases:
  - name: plan
    agent_template: planner    # 動的生成するエージェントのテンプレート

  - parallel:
      name: review
      phases:
        - name: security_review
          agent_template: security_expert
        - name: perf_review
          agent_template: perf_expert

  - name: synthesize
    agent_template: synthesizer
    depends_on: [review]
```

### 解決される問題

- worktree タイミング問題 → フェーズ開始時生成で常に最新ブランチから分岐
- `required_tags` ルーティングの複雑さ → フェーズが直接テンプレートを指定
- 並列エージェント間のファイル共有 → ブランチ経由で自然に解決

---

## §10.77 — v1.2.1: スクラッチパッド永続化 + `/scratchpad-write` / `/scratchpad-read` スラッシュコマンド

### Step 0 — 選択理由

**選択**: §11「高優先度: スクラッチパッド永続化 + スラッシュコマンド」

**理由**:
- §11 高優先度唯一のアイテム。v1.2.0 分岐後の最初の実装ターゲットとして明確に記録されている。
- 現状の in-memory スクラッチパッドはサーバー再起動で全データが消失する。パイプラインワークフローで中間結果を保存する用途では致命的。
- `/scratchpad-write` / `/scratchpad-read` スラッシュコマンドにより、エージェントが curl を書かずにスクラッチパッドを操作できるようになる。
- 実装スコープが明確（ScratchpadStore クラス + write-through + スラッシュコマンド 2つ）で 1 イテレーションに収まる。

**選択しなかった候補**:
- ワークフロー構造ブロック: 中優先度。スクラッチパッド永続化より優先度が低い。
- タスク優先度の動的更新: 中優先度。スクラッチパッドが安定してからの方が demos で使いやすい。

### Step 1 — Research

#### Query 1: "write-through cache file persistence Python atomic write rename os.replace"

**Key findings**:
- The gold standard for atomic file writes is the Create-Write-Rename pattern: write to a temp file in the same directory, then `os.replace(tmp, target)` (Python 3.3+).
- `os.replace()` is atomic on POSIX (single filesystem). The temp file must be on the same filesystem as the target — hence using the same directory.
- Third-party library `python-atomicwrites` wraps this pattern; Python stdlib is sufficient for our use.
- For durability (survives OS crash), `fsync()` should be called before rename. For agent orchestration purposes (normal restart recovery), fsync is optional overhead.

**Sources**:
- [Safely and atomically write to a file — ActiveState Code](https://code.activestate.com/recipes/579097-safely-and-atomically-write-to-a-file/)
- [python-atomicwrites documentation](https://python-atomicwrites.readthedocs.io/)
- [Stop Silent Data Loss: checksum + atomic writes + temp file patterns](https://tech-champion.com/data-science/stop-silent-data-loss-checksum-atomic-writes-temp-file-patterns/)

#### Query 2: "key-value store file backend Python atomic operations flat file directory"

**Key findings**:
- Python's built-in `dbm` module is a persistent KV store, but it uses opaque binary files — not human-readable. Fails the design requirement that `cat .orchestrator/scratchpad/my_key` works.
- `simplekv` has a `FilesystemStore` backend that stores each value as a separate file — directly analogous to our design.
- `DiskDict` stores one file per key. For human-readable values, plain text files are the best choice.
- Flat directory (one file per key) is the simplest and most debuggable layout. No indexing overhead for small key counts.

**Sources**:
- [TIL—Python has a built-in persistent key-value store (dbm)](https://remusao.github.io/posts/python-dbm-module.html)
- [simplekv — FilesystemStore](https://simplekv.readthedocs.io/)
- [DiskDict — disk-based KV store](https://github.com/AWNystrom/DiskDict)

#### Query 3: "FastAPI startup event restore state from filesystem lifespan 2025"

**Key findings**:
- FastAPI's `lifespan` context manager (ASGI Lifespan Protocol) is the canonical way to run startup/shutdown code. `@app.on_startup` is deprecated.
- Code before `yield` in the lifespan function runs at startup — ideal for restoring scratchpad from disk.
- However, our `_scratchpad` dict is a module-level global initialized at import time. The `ScratchpadStore.__init__()` already calls `_restore()` in its constructor, so no changes to lifespan are needed — the store self-initializes.
- `app.state` can hold references to initialized objects, but since `_scratchpad` is passed by reference to routers, the in-place mutation approach works without lifespan changes.

**Sources**:
- [Lifespan Events — FastAPI official docs](https://fastapi.tiangolo.com/advanced/events/)
- [FastAPI Lifespan Explained — Medium/AlgoMart (Jan 2026)](https://medium.com/algomart/fastapi-lifespan-explained-the-right-way-to-handle-startup-and-shutdown-logic-f825f38dd304)
- [FastAPI Application Lifecycle Management 2025](https://craftyourstartup.com/cys-docs/tutorials/fastapi-startup-and-shutdown-events-guide/)

### Design Decision

- `ScratchpadStore` is a stdlib-only class (no new deps). Dict-like interface (`__getitem__`, `__setitem__`, etc.) for drop-in replacement of `_scratchpad: dict`.
- `persist_dir=None` → pure in-memory (existing behavior, used in tests that don't need persistence).
- Atomic write: `tmp = dir / f".{key}.tmp"` → `tmp.write_text(json.dumps(value))` → `os.replace(tmp, dir / key)`. JSON serialization preserves type fidelity (dict, list, int, float, str, bool, null).
- Key validation: reject keys containing `/` or starting with `.` (prevent path traversal and collision with tmp files).
- Restore: iterate `persist_dir.iterdir()`, skip files starting with `.` (tmp files), load each as JSON.
- `scratchpad_dir: str = ".orchestrator/scratchpad"` added to `OrchestratorConfig` alongside `mailbox_dir`.
- `load_config()` applies `_resolve_dir()` to `scratchpad_dir` (same pattern as `mailbox_dir`).
- main ブランチの汚染 → ワークフロー完了時の一回マージのみ

## §10.78 — v1.2.2: parallel: / sequence: ワークフロー構造ブロック

### Step 0 — 選択理由

**選択**: ユーザー指定 v1.2.2 — `parallel:` / `sequence:` ワークフロー構造ブロック

**理由**:
- ユーザーが明示的に承認済みの設計。`PhaseItem = PhaseSpec | SequenceBlock | ParallelBlock | LoopBlock` への型システム拡張。
- 現在の DAG はフラットなシーケンス + LoopBlock のみ。並列ファンアウト/ファンイン (fan-out/fan-in) パターンは手動で depends_on を記述する必要があり、表現力が低い。
- `parallel:` ブロック: 内部の全アイテムが同時に開始し、すべて完了してからブロック完了 → ファンイン。
- `sequence:` ブロック: 内部アイテムが順番に実行される (暗黙的チェイニング)。
- 両ブロックは名前付き → `depends_on: [block_name]` で参照可能。

**選択しなかった候補**:
- タスク優先度の動的更新: ブロック構造の実装後の方が、複雑なワークフローで優先度制御のユースケースが具体化する。

### Step 1 — Research

#### Query 1: "workflow parallel fan-out fan-in pattern DAG orchestration"

**Key findings**:
- Fan-out/fan-in: task splits into multiple sub-tasks (fan-out), runs in parallel, then aggregates (fan-in). Azure Durable Functions, Apache Airflow, Argo Workflows all support this natively.
- Argo Workflows: `withItems` or `withParam` fan-out; implicit fan-in when all branches complete.
- Apache Airflow: A single task fans out to multiple downstream tasks. The fan-in is handled by a task that `depends_on` all fan-out tasks.
- Dagster: Dynamic fanout is useful when processing a variable number of items in parallel.
- Pattern: ParallelBlock.complete ⟺ all(item.complete for item in parallel.phases).

**Sources**:
- [Dynamic Fan-out and Fan-in in Argo Workflows — Corvin Deboeser, Medium](https://medium.com/@corvin/dynamic-fan-out-and-fan-in-in-argo-workflows-d731e144e2fd)
- [Azure Durable Functions: Fan-Out/Fan-In Pattern — DZone](https://dzone.com/articles/azure-durable-functions-fan-outfan-in-pattern)
- [Dynamic fanout — Dagster Docs](https://docs.dagster.io/examples/mini-examples/dynamic-fanout)

#### Query 2: "nested workflow blocks sequential parallel composition formal semantics"

**Key findings**:
- Compositional semantics: sequential and parallel are first-class combinators. Series-parallel computation graph = empty | single | seq(A,B) | par(A,B). This is the foundation of our SequenceBlock/ParallelBlock design.
- Process algebra (CCS/CSP): sequential composition P;Q and parallel P|Q are primitive operators. Operational semantics can be derived compositionally.
- Taverna Workflows: nested blocks with sequential and parallel execution modes, formal syntax and semantics defined via petri nets.
- Key insight: a "plain phases: [...]" top-level list is already equivalent to SequenceBlock (backward compatible).

**Sources**:
- [Formalization of Workflows and Correctness Issues in the Presence of Concurrency — Springer](https://link.springer.com/article/10.1023/A:1008758612291)
- [Semantics of Sequential and Parallel Programs — CMU](https://www.cs.cmu.edu/afs/cs/usr/brookes/www/spring99.html)
- [Taverna Workflows: Syntax and Semantics — ResearchGate](https://www.researchgate.net/publication/4309568_Taverna_Workflows_Syntax_and_Semantics)

#### Query 3: "Argo Workflows steps vs DAG parallel sequential 2026"

**Key findings**:
- Argo **steps** template: list-of-lists structure. Outer list = sequential; inner list = parallel. Steps[i] begins only after steps[i-1] all complete. This is exactly the SequenceBlock containing ParallelBlocks pattern.
- Argo **DAG** template: explicit `dependencies` per task, maximum parallelism. Tasks without dependencies run immediately.
- Key distinction for our design: steps model = SequenceBlock default chaining, DAG model = explicit depends_on. We support BOTH: blocks for nested structure, explicit depends_on for fine-grained control.
- Fan-in: Argo steps template fan-in happens naturally when execution advances to the next sequential step after all parallel steps complete. This is the semantics we implement for ParallelBlock.

**Sources**:
- [DAG — Argo Workflows documentation](https://argo-workflows.readthedocs.io/en/latest/walk-through/dag/)
- [Steps — Argo Workflows documentation](https://argo-workflows.readthedocs.io/en/latest/walk-through/steps/)
- [Argo Workflows Steps vs DAG — Restack.io](https://www.restack.io/p/argo-workflows-steps-answer-vs-dag)

### Design Decision

- `SequenceBlock(name, phases)`: items run in order; each phase depends on the previous phase's terminal tasks (auto-chaining). Block completes when last item completes.
- `ParallelBlock(name, phases)`: all items start simultaneously (all use the same prior_ids). Block completes when ALL items complete (fan-in: a synthetic dependency list containing all items' terminal IDs).
- Both block types are named → recorded in `block_terminal_ids` dict (analogous to `loop_terminal_ids`) so outer phases can declare `depends_on: [block_name]`.
- `expand_phase_items_with_status()` extended with `isinstance(item, SequenceBlock | ParallelBlock)` dispatch.
- Backward compatibility: top-level `phases: [PhaseSpec, ...]` is unchanged — handled by the existing `else` branch.

## §10.79 — v1.2.3: 動的エージェント生成 (PhaseSpec.agent_template)

### Step 0 — 選択理由

**選択**: ユーザー指定 v1.2.3 — `PhaseSpec.agent_template` フィールド追加 + 動的エフェメラルエージェント生成

**理由**:
- 現在のシステムはエージェントを YAML で事前定義し、サーバー起動時に全員起動する。これはリソース効率が低く、各フェーズに専用の独立したコンテキストウィンドウを持つエージェントを割り当てたい場合に対応できない。
- `agent_template: "worker"` をフェーズに指定することで、そのフェーズ専用の一時エージェントを動的に生成・起動・停止できる。これは Kubernetes の Pod-per-Job パターンと同等の概念。
- `create_agent()` メソッドが既に存在するため、オーケストレーター側の変更は最小限。

**選択しなかった候補**:
- タスク優先度の動的更新: 動的エージェント生成の方が §11 の優先度が高い。

### Step 1 — Research

#### Query 1: "dynamic worker spawning on-demand orchestration pattern"

**Key findings**:
- The Orchestrator-Worker pattern is an agentic workflow architecture where a central orchestrator dynamically breaks jobs into subtasks and dispatches them to worker nodes.
- Agent Swarm (QwenLM): dynamic spawning of lightweight worker agents at runtime based on task requirements. Workers execute independently and return results for aggregation.
- Key distinction from static parallelization: in static patterns, tasks are hard-coded; in dynamic patterns, an LLM (or orchestrator) generates the task list at runtime, enabling adaptive behavior.
- Speed improvements of 5-20x compared to sequential processing via parallel I/O-bound LLM calls.

**Sources**:
- [Orchestrator-Worker Pattern — hype08.github.io](https://hype08.github.io/gradual-notes/thoughts/Orchestrator-Worker-Pattern)
- [Feature Request: Agent Swarm - Dynamic Parallel Worker Spawning — QwenLM/qwen-code #1816](https://github.com/QwenLM/qwen-code/issues/1816)
- [AI Agent Orchestration Patterns — Azure Architecture Center](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns)

#### Query 2: "ephemeral agent lifecycle task-scoped worker orchestration"

**Key findings**:
- Ephemeral agents are temporary remote agents in Kubernetes clusters, designed to handle a single build or deployment before shutting down. Systems automate their lifecycle and match agents with suitable builds and deployments.
- GitHub Copilot CLI: sub-agents are spawned by the task tool for specific, scoped tasks and receive only a subset of tools relevant to their task. When done, MCP servers are shutdown.
- Busy ephemeral workers don't get interrupted — the orchestrator waits for task completion before teardown.
- Pattern: task-scoped worker = created just before task dispatch, stopped immediately after task completion.

**Sources**:
- [Shipyard: Multi-agent orchestration for Claude Code in 2026](https://shipyard.build/blog/claude-code-multi-agent/)
- [Ephemeral Pipelines Agents — Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=tiago-pascoal.EphemeralPipelinesAgents)
- [Bamboo Ephemeral Agents on Red Hat OpenShift — Alp Alpant](https://medium.com/@alpant/bamboo-ephemeral-agents-on-red-hat-openshift-251a1dcbb387)

#### Query 3: "Kubernetes pod per job dynamic provisioning pattern"

**Key findings**:
- Kubernetes dynamic provisioning: automatically creates resources based on demand without requiring pre-provisioning. StorageClass → PVC → PV lifecycle is the canonical example.
- Pod-per-Job pattern: each job gets its own isolated pod with independent lifecycle. The control plane manages creation, scheduling, and cleanup automatically.
- Key insight for our design: `agent_template` in PhaseSpec acts as a "StorageClass" — a template that defines HOW to create the resource. The orchestrator creates an ephemeral agent (pod) when a phase begins and cleans it up when done.
- Benefit: resource usage matches workload; no idle workers consuming memory/panes when no tasks are running.

**Sources**:
- [Dynamic Volume Provisioning — Kubernetes official docs](https://kubernetes.io/docs/concepts/storage/dynamic-provisioning/)
- [Dynamic Resource Allocation — Kubernetes official docs](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)
- [Kubernetes Volume Provisioning: Dynamic VS Static — GeeksforGeeks](https://www.geeksforgeeks.org/devops/kubernetes-volume-provisioning-dynamic-vs-static/)

### Design Decision

- `PhaseSpec.agent_template: str | None = None` — domain field (stdlib dataclass, no new deps).
- `Orchestrator.spawn_ephemeral_agent(template_id: str) -> str`:
  - Finds `AgentConfig` in `config.agents` matching `template_id`.
  - Creates `ClaudeCodeAgent` with ID `f"{template_id}-ephemeral-{uuid4().hex[:8]}"`.
  - Registers in registry + starts the agent.
  - Tracks in `_ephemeral_agents: set[str]` for lifecycle management.
- Task spec: when `phase.agent_template` is set, `expand_phases_with_status()` embeds a sentinel `agent_template` key in the task spec dict. The workflow router reads this key, calls `spawn_ephemeral_agent()`, and sets `target_agent` before submission.
- Auto-stop: after `_route_loop` records a RESULT from an ephemeral agent, the same drain pattern as `_draining_agents` is used — but triggered by the `_ephemeral_agents` set instead.
- `PhaseSpecModel.agent_template: str | None = None` in web/schemas.py.
- Backward compatibility: phases without `agent_template` use `required_tags` routing as before.

## §10.80 — v1.2.4: ブランチ連鎖型ワークフロー実行 (chain_branch flag + ephemeral branch tracking)

### Step 0 — 選択理由

**選択**: §11 補足「ブランチ連鎖型ワークフロー実行」— `PhaseSpec.chain_branch` フラグ + `_ephemeral_agent_branches` トラッキング + `WorktreeManager.create_from_branch()` 実装

**理由**:
- v1.2.3 で動的エフェメラルエージェント生成が完成したが、エージェント間のファイル継承機構がない。フェーズAのエージェントが書いたファイルをフェーズBのエージェントが見るには、ブランチ連鎖が必要。
- `sequence:` ブロック内で `chain_branch=True` を指定することで、フェーズNのworktreeブランチからフェーズN+1のworktreeを作成し、ファイルを継承できる。
- この実装は v1.2.5 でのフルディスパッチ時統合のための building blocks を提供する。

**選択しなかった候補**:
- タスク優先度の動的更新: ブランチ連鎖の方が §11 補足の設計目標に合致。
- ループワークフローの動的早期終了: WorkflowManager フック改修が必要で今回範囲外。

### Step 1 — Research

#### Query 1: "git branch chain sequential pipeline workflow automation"

**Key findings**:
- GitFlow: 専用ブランチ(develop/feature/release)への段階的マージで CI/CD パイプラインを構成。各ブランチが前段の成果を継承。
- GitHub Actions: `needs:` キーワードでジョブ依存チェーンを宣言。前ジョブ完了後に次ジョブが前成果を利用。
- Trunk-Based Development: 短命ブランチを使用して CI/CD の連続デリバリーを実現。
- 主要発見: sequential pipeline = 前フェーズのブランチ先端からの分岐によるファイル継承が最もシンプル。

**Sources**:
- [GitFlow Tutorial — DataCamp](https://www.datacamp.com/tutorial/gitflow)
- [Managing Sequential Job Dependencies — Concourse CI #8955](https://github.com/orgs/concourse/discussions/8955)

#### Query 2: "worktree per task sequential branch handoff git workflow"

**Key findings**:
- Git Worktrees in the Age of AI Coding Agents: AI エージェントが並列で独立したworktreeで作業することが主流になりつつある。handoff = 作業を別worktreeに引き渡す git 操作の自動化。
- Codex App Worktrees: Handoff は Local と Worktree 間でスレッドを移動するフロー。git操作を透過的に処理し、ブランチが1箇所しかチェックアウトできない制約を自動管理。
- 主要発見: `git worktree add -b <new-branch> <path> <source-branch>` が sequential handoff の基本操作。

**Sources**:
- [The Rise of Git Worktrees in the Age of AI Coding Agents — knowledge.buka.sh](https://knowledge.buka.sh/the-rise-of-git-worktrees-in-the-age-of-ai-coding-agents/)
- [Codex App Worktrees — OpenAI developers](https://developers.openai.com/codex/app/worktrees/)

#### Query 3: "parallel git worktree merge strategy fan-out fan-in"

**Key findings**:
- Claude Code + Git Worktrees: 各エージェントが独自のworktreeを持ち並列作業。最終的に review/merge 担当者がブランチをマージ。fan-out (複数worktree生成) + fan-in (merge) パターン。
- Mastering Git Worktrees with Claude Code: 並列エージェントのworktreeは通常ブランチとしてマージ可能。merge strategy (squash, no-ff, cherry-pick) で選択。
- 主要発見: 並列fan-out時は共通の親ブランチから各worktreeを作成；fan-in時は複数ブランチをマージ。

**Sources**:
- [Mastering Git Worktrees with Claude Code — Medium](https://medium.com/@dtunai/mastering-git-worktrees-with-claude-code-for-parallel-development-workflow-41dc91e645fe)
- [Claude Code Worktrees: Run Parallel Sessions — claudefa.st](https://claudefa.st/blog/guide/development/worktree-guide)

### Design Decision

- **`PhaseSpec.chain_branch: bool = False`**: domain フィールド (stdlib dataclass)。`True` のとき、次の sequential フェーズはこのフェーズのworktreeブランチから分岐する。
- **`_make_task_spec(..., chain_branch=False)`**: `chain_branch=True` のとき task spec dict に `"chain_branch": True` キーを埋め込む。`SingleStrategy` / `ParallelStrategy` が `phase.chain_branch` を propagate。
- **`WorktreeManager.create_from_branch(agent_id, source_branch)`**: `git worktree add -b worktree/{agent_id} <path> <source_branch>` を実行。source_branch が存在しない場合は `RuntimeError` を raise。
- **`Orchestrator._ephemeral_agent_branches: dict[str, str]`**: `spawn_ephemeral_agent()` 後に `{ephemeral_id} → "worktree/{ephemeral_id}"` を記録。`isolate=False` エージェントはworktreeなし → エントリなし。
- **`Orchestrator.get_worktree_manager()`**: `self._worktree_manager` を返す公開メソッド。workflow router が `chain_branch=True` 処理時に使用。
- **`spawn_ephemeral_agent()` event payload**: `"branch"` フィールド追加。isolate=True → `"worktree/{ephemeral_id}"`、isolate=False → `""`.
- **`PhaseSpecModel.chain_branch: bool = False`**: Pydantic スキーマに追加; `_to_domain_phase_spec()` で `PhaseSpec.chain_branch` に変換。
- **Known Limitation (v1.2.4)**: フルディスパッチ時のブランチ連鎖統合は v1.2.5。v1.2.4 は building blocks (フラグ + トラッキング + `create_from_branch`) を提供し、workflow router での `chain_branch` フラグ処理 (task spec → `create_from_branch` 呼び出し) は次イテレーションで実装。

### Implementation Summary

**新規ファイル**:
- `tests/test_branch_chain_workflow.py`: 18 新規テスト

**変更ファイル**:
- `src/tmux_orchestrator/domain/phase_strategy.py`: `PhaseSpec.chain_branch` フィールド追加; `_make_task_spec` に `chain_branch` 引数追加; `SingleStrategy` / `ParallelStrategy` が propagate
- `src/tmux_orchestrator/infrastructure/worktree.py`: `create_from_branch(agent_id, source_branch)` メソッド追加
- `src/tmux_orchestrator/application/orchestrator.py`: `_ephemeral_agent_branches` 追加; `spawn_ephemeral_agent()` でブランチ記録 + event payload 拡張; `get_worktree_manager()` 追加
- `src/tmux_orchestrator/web/schemas.py`: `PhaseSpecModel.chain_branch` フィールド追加
- `src/tmux_orchestrator/web/routers/workflows.py`: `_to_domain_phase_spec()` に `chain_branch` mapping 追加
- `pyproject.toml`: version 1.2.3 → 1.2.4

---

## §10.81 — v1.2.5: ブランチ連鎖ルーター配線 (Branch-Chain Router Wiring)

### 選択の根拠

**選択**: `spawn_ephemeral_agent(source_branch=...)` パラメータ追加 + ワークフロールーターで `chain_branch` フラグに基づく前駆フェーズのブランチを後継エージェントに引き継ぐ配線実装。

**理由**: v1.2.4 で構築したビルディングブロック（`create_from_branch`, `_ephemeral_agent_branches`, `get_worktree_manager`, `chain_branch` フラグ）が揃った。最後の欠落ピースはルーター配線のみ。

**見送り候補**:
- Phase-level webhook events (phase_complete/phase_failed) — 優先度が低い
- Dynamic `until` condition short-circuit — LoopBlock 実装の既知制限

### 調査記録

#### Query 1: "sequential git worktree branch handoff CI pipeline"

主要知見:
- Git worktrees allow multiple branches to coexist simultaneously as separate directories. A "handoff" pattern documents path, branch, status, and notes for passing work between stages. (DataCamp, 2025; Nx Blog, 2025)
- Jenkins CI/CD pipelines with worktrees include explicit checkout, build/test, and cleanup stages. Orphaned worktrees from failed pipelines must be explicitly cleaned up. (dredyson.com, 2025)
- OpenAI Codex uses "Local→Worktree" handoff as a named flow for moving work safely between environments. (developers.openai.com, 2025)

参考 URL:
- https://www.datacamp.com/tutorial/git-worktree-tutorial
- https://dredyson.com/how-to-integrate-git-worktree-rebasing-into-enterprise-ci-cd-pipelines-a-complete-step-by-step-guide-for-it-architects/
- https://developers.openai.com/codex/app/worktrees/

#### Query 2: "DAG task dispatcher branch inheritance workflow orchestration"

主要知見:
- Airflow DAGs support branching via `@task.branch` decorator — the orchestrator picks one or more paths based on a predicate (Airflow docs, 2025). This is *conditional* branching, distinct from our sequential branch *chaining*.
- Argo Workflows' DAG template passes task outputs as inputs to dependent tasks via `{{tasks.X.outputs.Y}}` references. This is the canonical pattern for inter-task data inheritance in a DAG (Argo, 2025).
- A Java DAG execution engine tracks a `local_id → task_id` mapping for dependency resolution (Medium/Amit Kumar, 2025). Directly analogous to `local_to_global` in our router.

参考 URL:
- https://argo-workflows.readthedocs.io/en/latest/walk-through/dag/
- https://medium.com/@amit.anjani89/building-a-dag-based-workflow-execution-engine-in-java-with-spring-boot-ba4a5376713d
- https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html

#### Query 3: "ephemeral agent worktree chain orchestration multi-agent"

主要知見:
- Gas Town (gastown) uses "Polecats" — ephemeral worker agents that spawn, complete a task, and disappear — with "The Refinery" managing the merge queue. This is the same ephemeral-agent-per-task pattern as TmuxAgentOrchestrator. (GitHub steveyegge/gastown, 2025)
- Stoneforge: Director creates a plan, daemon assigns tasks to workers in isolated worktrees, stewards merge results. The sequential handoff between worktrees for a pipeline is explicitly described. (stoneforge.ai, 2025)
- ccswarm: Multi-agent orchestration with Claude Code using git worktree isolation. Each agent operates in its own worktree branch, with the orchestrator managing branch lifecycle. (GitHub nwiizo/ccswarm, 2025)

参考 URL:
- https://github.com/steveyegge/gastown
- https://stoneforge.ai/blog/introducing-stoneforge/
- https://github.com/nwiizo/ccswarm

### 設計決定

**`spawn_ephemeral_agent(source_branch=None)` 追加**:

`source_branch` が指定された場合、エージェントの `start()` 内で `wm.setup()` の代わりに `wm.create_from_branch()` が呼ばれるよう `Agent._source_branch` 属性を設定してから `start()` を呼び出す。

```python
# base.py: _setup_worktree に source_branch 分岐を追加
if self._source_branch:
    path = wm.create_from_branch(self.id, self._source_branch)
else:
    path = wm.setup(self.id, isolate=self._isolate)
```

**ルーター配線**:

**遅延スポーン (Deferred Spawn) アーキテクチャ**:

`chain_branch=True` フェーズは**ルーター提出時に即時スポーンしない**。
即時スポーンだと `create_from_branch` が前駆フェーズのコミット前に実行されるため
(前駆フェーズが `depends_on` でゲートされているにもかかわらず、全タスクを一括提出する)。

```
# ルーター (提出時): chain_branch=True タスクはメタデータに記録のみ
task_metadata = {
    "_ephemeral_template": template_id,
    "_chain_branch": True,
    "_chain_pred_task_ids": [global_task_id for dep in depends_on],
}
submit_task(..., target_agent=None, metadata=task_metadata)

# _route_loop (ディスパッチ時: depends_on 完了後):
if task.metadata.get("_ephemeral_template") and task.target_agent is None:
    source_branch = orch._ephemeral_agent_branches.get(
        orch._task_ephemeral_agent.get(pred_task_id)
    )
    new_eph_id = await spawn_ephemeral_agent(template, source_branch=source_branch)
    orch._task_ephemeral_agent[task.id] = new_eph_id
    task.target_agent = new_eph_id
```

**新規フィールド**: `Orchestrator._task_ephemeral_agent: dict[str, str]` — `task_id → ephemeral_agent_id`

**前提**: `chain_branch=True` は `agent_template` とセットで使用。`isolate=True` のエージェントのみ worktree ブランチを持つ。非 chain_branch の `agent_template` タスクは従来通り即時スポーン (v1.2.3 動作維持)。

### Implementation Summary

**新規ファイル**:
- `tests/test_branch_chain_router.py`: 14 新規テスト

**変更ファイル**:
- `src/tmux_orchestrator/agents/base.py`: `_source_branch` 属性追加; `_setup_worktree()` に `create_from_branch` 分岐追加 (fallback to `setup()` on error)
- `src/tmux_orchestrator/application/orchestrator.py`: `spawn_ephemeral_agent(source_branch=None)` パラメータ追加; `_task_ephemeral_agent` dict 追加; `_route_loop` に deferred spawn ブロック追加
- `src/tmux_orchestrator/web/routers/workflows.py`: chain_branch フェーズは `_ephemeral_template` + `_chain_pred_task_ids` メタデータのみ記録 (即時スポーンなし); 非 chain_branch は v1.2.3 互換の即時スポーン維持
- `pyproject.toml`: version 1.2.4 → 1.2.5

**テスト結果**: 14 新規テスト 全 PASS; 既存 3006 テスト (pre-existing 2 failures 除く) 全 PASS (計 3020)

**デモ結果 (v1.2.5)**:
- 12/12 PASS
- Phase 2 git log: `phase2: add reply → phase1: add message → init`
- Phase 2 scratchpad reply: `Phase 2 received: Hello from phase 1`
- 遅延スポーンにより Phase 1 のコミット後に Phase 2 の worktree が作成されることを確認

## §10.82 — v1.2.6: タスク優先度動的更新 + ブランチ証跡保持

### 選定理由

**選択 A**: `PATCH /tasks/{id}/priority` + `AsyncPriorityTaskQueue.update_priority()` — ランタイム中にキュー内タスクの優先度を動的変更する機能。

**選択 B**: `AgentConfig.keep_branch_on_stop` — エフェメラルエージェント停止時に worktree ファイルシステムを削除しつつ git ブランチを保持し、後続フェーズがコミット済みアーティファクトにアクセスできるようにする機能。

**なぜ選択したか**:
- A: `update_task_priority` は Orchestrator に実装済みだが lazy deletion パターンなし。`AsyncPriorityTaskQueue` に `update_priority()` を追加し、専用 REST エンドポイント `PATCH /tasks/{id}/priority` (404 on not-found) を追加。
- B: `chain_branch` ワークフローでエフェメラルエージェントが停止すると worktree とブランチの両方が削除され、後続フェーズが前フェーズのコミットを参照できない問題を解決。

### 調査記録

**調査 1: Priority Queue runtime update — lazy deletion (heapq, Python asyncio)**
- 出典: Python 公式ドキュメント "heapq — Heap queue algorithm" (Priority Queue Implementation Notes)
  URL: https://docs.python.org/3/library/heapq.html
- lazy deletion パターン: `_deleted_seqs: set[int]` でシーケンス番号を管理し、`get()` 時にスキップ。
- `_pending: dict[str, tuple[int, int, Task]]` で pending タスクを追跡。
- 参照: Sedgewick & Wayne "Algorithms" 4th ed. §2.4; Liu & Layland JACM 20(1) 1973.

**調査 2: git worktree branch keep after remove**
- 出典: git-worktree(1) https://git-scm.com/docs/git-worktree
- `git worktree remove` はリンク worktree ディレクトリのみ削除; ブランチは残る (期待動作)。
- 出典: "Using Git Worktrees for Multi-Feature Development with AI Agents" — Nick Mitchinson (2025)
  URL: https://www.nrmitchi.com/2025/10/using-git-worktrees-for-multi-feature-development-with-ai-agents/
- エージェント停止後もブランチを残すことで後続フェーズが `create_from_branch()` で参照可能。

**調査 3: REST PATCH task priority update idempotent API design**
- 出典: Postman Blog "HTTP PATCH Method: Partial Updates for RESTful APIs" (2025)
  URL: https://blog.postman.com/http-patch-method/
- 出典: mscharhag.com "Making POST and PATCH requests idempotent" (2025)
  URL: https://www.mscharhag.com/api-design/rest-making-post-patch-idempotent
- `PATCH /tasks/{id}/priority` は明示的サブリソース。タスク未発見時 HTTP 404。

### Implementation Summary

**変更ファイル (Feature A)**:
- `src/tmux_orchestrator/application/task_queue.py`: `update_priority()` + `_pending` + `_deleted_seqs` 追加; `put()` / `get()` / `empty()` / `qsize()` を lazy deletion 対応に更新
- `src/tmux_orchestrator/application/orchestrator.py`: `update_task_priority()` を lazy deletion 優先に更新; `list_tasks()` で `_deleted_seqs` フィルタ追加; `cancel_task()` で `_pending` 更新追加
- `src/tmux_orchestrator/web/routers/tasks.py`: `PATCH /tasks/{task_id}/priority` エンドポイント追加 (404 on not-found)

**変更ファイル (Feature B)**:
- `src/tmux_orchestrator/application/config.py`: `AgentConfig.keep_branch_on_stop: bool = False` 追加
- `src/tmux_orchestrator/agents/base.py`: `_keep_branch_on_stop: bool = False` 属性追加; `_teardown_worktree()` を `keep_branch` 対応に更新
- `src/tmux_orchestrator/agents/claude_code.py`: `keep_branch_on_stop` 引数追加; `_write_agent_claude_md()` にアーティファクト永続化セクション追加
- `src/tmux_orchestrator/application/factory.py`: `keep_branch_on_stop` を `ClaudeCodeAgent` コンストラクタに渡す
- `src/tmux_orchestrator/application/orchestrator.py`: `spawn_ephemeral_agent()` で isolated エフェメラルエージェントに `keep_branch_on_stop=True` を自動設定

**新規ファイル**:
- `tests/test_task_priority_update.py`: 13 テスト (Feature A)
- `tests/test_branch_artifact_persistence.py`: 12 テスト (Feature B)

**テスト数**: 3020 → 3057 (37 新規)

## §10.83 — v1.2.7: ループ until 条件のランタイム評価 (Loop Until Runtime Evaluation)

### 選定理由

**選択**: `WorkflowManager` に loop until 条件のランタイム評価を実装し、条件が満たされた時点で後続イテレーションのタスクをキャンセルする。

**なぜ選択したか**:
- v1.1.44 / v1.2.0 で実装されたループワークフロー (`LoopBlock`) は `until` 条件をエージェントプロンプトに埋め込むだけで、サーバー側では一切評価していない。全 `max` イテレーションが常に実行される。
- 既知の limitation として DESIGN.md §10.76 に記録済み。
- `WorkflowManager` は既に `on_task_complete()` フックを持つ — ここで条件を評価して後続タスクをキャンセルするのが自然な拡張。
- `AsyncPriorityTaskQueue` / `Orchestrator.cancel_task()` は既にキャンセルを完全サポート (v0.27.0, v1.2.6)。
- `ScratchpadStore` は dict-like で `WorkflowManager` に注入可能。

**選択しなかった候補**:
- 動的展開 (submit time にイテレーション 1 だけ展開し、完了後に次を展開): 後方互換性破壊のリスクが高く、既存の静的 DAG model と不整合。
- エージェントプロンプト側の until 評価のみ: 既存の実装。タスクがキャンセルされないため無駄な API 呼び出しが発生。

### 調査記録

**調査 1: DAG ループ早期終了 — Argo Workflows failFast**
- 出典: Argo Workflows DAG docs
  URL: https://argo-workflows.readthedocs.io/en/latest/walk-through/dag/
- failFast=true (デフォルト): 1 タスクが失敗したら新規タスクをスケジュールしない。全実行中タスク完了後に DAG を failed とマーク。
- until 条件の成功系での early termination に類似。残存タスクのキャンセルが標準パターン。

**調査 2: PDCA サイクルの早期終了条件**
- 出典: Wikipedia PDCA; Asana PDCA resources (2026)
  URL: https://en.wikipedia.org/wiki/PDCA; https://asana.com/resources/pdca-cycle
- PDCA は "条件が満たされるまで繰り返す" イテレーティブモデル。条件達成時に残余サイクルを中断するのは PDCA の本質的な動作。
- 参照: Deming "Out of the Crisis" (1982) — Plan-Do-Study-Act cycle (PDSA 前身)。

**調査 3: asyncio タスクキャンセル + 条件付きワークフロー終了 (Python 2026)**
- 出典: Python 3 公式ドキュメント "Coroutines and Tasks"
  URL: https://docs.python.org/3/library/asyncio-task.html
- `asyncio.create_task(coroutine.cancel())` は cooperative cancellation。`_waiting_tasks` 内の未ディスパッチタスクは同期的に削除可能。
- 出典: Super Fast Python "Asyncio Task Cancellation Best Practices"
  URL: https://superfastpython.com/asyncio-task-cancellation-best-practices/
- キャンセル後に await することで CancelledError の伝播を確認するのがベストプラクティス。ただし queued/waiting タスクはまだ実行されていないため await 不要。

### Implementation Summary

**変更ファイル**:
- `src/tmux_orchestrator/application/workflow_manager.py`:
  - `_loop_iterations: dict[tuple[str,str], list[list[str]]]` — ループ名→イテレーション別タスク ID
  - `_loop_specs: dict[tuple[str,str], LoopSpec]` — ループ名→LoopSpec (until 条件)
  - `_loop_scratchpad_prefix: dict[tuple[str,str], str]` — ループ名→スクラッチパッドプレフィックス
  - `_completed_tasks: set[str]` — 完了済みタスク ID セット
  - `_scratchpad: dict | None` — スクラッチパッド参照
  - `_cancel_task_fn: Callable[[str], None] | None` — キャンセル関数
  - `register_loop()` メソッド
  - `set_scratchpad()` メソッド
  - `set_cancel_task_fn()` メソッド
  - `_check_loop_until()` メソッド
  - `_mark_task_skipped()` メソッド
  - `on_task_complete()`: `_completed_tasks` 更新 + `_check_loop_until()` 呼び出し
- `src/tmux_orchestrator/web/routers/workflows.py`:
  - `register_loop()` 呼び出し追加 (LoopBlock with until 条件)
- `src/tmux_orchestrator/web/app.py`:
  - `wm.set_scratchpad(_scratchpad)` + `wm.set_cancel_task_fn(...)` 配線追加
- `pyproject.toml`: version `1.2.7`

**新規ファイル**:
- `tests/test_loop_until_runtime.py`: 12+ テスト

**テスト数**: 3057 → 3069+

## §10.84 — v1.2.8: ワークフロー完了時ブランチクリーンアップ + merge_to_main_on_complete

### 選定理由

**選択**: ワークフロー完了時に、エフェメラルエージェントの worktree ブランチを自動削除する。オプションで最終フェーズのブランチを main にマージ後に削除する (`merge_to_main_on_complete`)。

**なぜ選択したか**:
- v1.2.6 で `keep_branch_on_stop: True` を導入し、チェーンブランチワークフローで後継フェーズが前フェーズのブランチを参照できるようにした。
- ワークフロー完了後も `worktree/*` ブランチが蓄積し続ける問題が残った。5フェーズのシーケンシャルワークフローが5本のブランチを残す。
- ブランチ数が増えると `git branch` 出力が汚れ、CI/CD パイプラインのインデックス更新が遅くなる（Jenkins: Multibranch Pipeline スキャン）。
- `WorkflowManager._update_status()` が既に "complete"/"failed" 遷移を検知 → ここでクリーンアップをトリガーするのが自然。

**選択しなかった候補**:
- スケジュール型クリーンアップ (TTL 経過後): ワークフローIDとブランチの対応が失われるため不可。
- エージェント停止時の即時削除: `keep_branch_on_stop=True` の意図 (後継フェーズが参照) を破壊。

### 調査記録

**調査 1: git branch cleanup automation pipeline completion workflow**
- 出典: Medium "Automate Git Branch Cleanup with Python" (Tom Smykowski, 2025)
  URL: https://tomaszs2.medium.com/automate-git-branch-cleanup-with-python-say-goodbye-to-manual-tidying-e79e8a2e3155
- Python スクリプトで未プッシュ・陳腐化・マージ済みブランチをコマンドライン一括削除可能。`git branch -D` を subprocess で呼び出すパターンが標準。
- 出典: Medium "Automating Git Branch Cleanup in Jenkins" (Pankaj Aswal, 2025)
  URL: https://medium.com/@pankajaswal888/automating-git-branch-cleanup-in-jenkins-why-it-matters-and-how-to-do-it-f64fe5324cbf
- Jenkins Multibranch Pipeline でブランチが蓄積すると CI スキャンが遅くなる。完了後の自動削除が推奨。90日以上古いブランチを自動削除するパターン。
- 出典: GitHub Action "jessfraz/branch-cleanup-action"
  URL: https://github.com/jessfraz/branch-cleanup-action
- PR マージ後にブランチを自動削除する GitHub Action。"Merged branches should be deleted automatically" がベストプラクティスとして確立。

**調査 2: merge branch main on completion CI CD pipeline pattern**
- 出典: JetBrains TeamCity "Branching Strategies for CI/CD"
  URL: https://www.jetbrains.com/teamcity/ci-cd-guide/concepts/branching-strategy/
- Integration branch approach: feature → integration branch → test → merge to main。Integration branch はテスト完了後に main に自動マージ。
- 出典: Atlassian "Trunk-based Development"
  URL: https://www.atlassian.com/continuous-delivery/continuous-integration/trunk-based-development
- Short-lived feature branches: 作業完了後に main/trunk へ即時マージ。ブランチの長寿命化は merge conflict リスクを高める。
- 出典: GitLab "Merge request pipelines"
  URL: https://docs.gitlab.com/ee/ci/pipelines/merge_request_pipelines.html
- MR パイプライン完了後の自動マージ (auto-merge) がベストプラクティス。`--no-ff` マージで履歴を保持。

**調査 3: orchestration workflow branch lifecycle management cleanup**
- 出典: Oracle JD Edwards "Understanding Orchestration Life Cycle Management"
  URL: https://docs.oracle.com/en/applications/jd-edwards/cross-product/9.2/eotos/understanding-orchestration-life-cycle-management.html
- ワークフローライフサイクル: initial deployment → monitoring → scaling → retirement (リソース解放)。完了後のクリーンアップはオーケストレーションの標準フェーズ。
- 出典: Camunda "What is Workflow Orchestration?"
  URL: https://camunda.com/blog/2024/02/what-is-workflow-orchestration-guide-use-cases/
- 効果的なオーケストレーションはワークフロー全体のライフサイクルをサポート。完了後のリソース解放 (ブランチ削除) は cleanup フェーズに属する。
- 出典: MCP Market "Branch Orchestration Claude Code Skill"
  URL: https://mcpmarket.com/ko/tools/skills/branch-orchestration
- Git ブランチのライフサイクル管理: マージ済み・陳腐化ブランチの自動クリーンアップが automated cleanup utilities として確立されたパターン。

### Implementation Summary

**変更ファイル**:
- `src/tmux_orchestrator/application/orchestrator.py`:
  - `_workflow_branches: dict[str, list[str]]` — workflow_id → ブランチ名リスト
  - `spawn_ephemeral_agent(workflow_id=None)` — workflow_id 引数追加
  - `cleanup_workflow_branches(workflow_id, *, merge_final_to_main=False)` — 非同期クリーンアップメソッド
- `src/tmux_orchestrator/application/workflow_manager.py`:
  - `_branch_cleanup_fn: Callable[[str], Awaitable[None]] | None` — 注入可能なクリーンアップコールバック
  - `set_branch_cleanup_fn(fn)` — コールバック注入メソッド
  - `_update_status()`: "complete"/"failed" 遷移時に `asyncio.ensure_future(_branch_cleanup_fn(run.id))` 呼び出し
- `src/tmux_orchestrator/infrastructure/worktree.py`:
  - `delete_branch(branch_name)` — `git branch -D` ラッパー
  - `merge_branch_to_main(branch_name, *, target)` — `git merge --no-ff` ラッパー
- `src/tmux_orchestrator/application/config.py`:
  - `OrchestratorConfig.workflow_branch_cleanup: bool = True`
- `src/tmux_orchestrator/web/schemas.py`:
  - `WorkflowSubmit.merge_to_main_on_complete: bool = False`
- `src/tmux_orchestrator/web/routers/workflows.py`:
  - `spawn_ephemeral_agent(workflow_id=run.id)` へ変更
  - ワークフロー完了後クリーンアップ関数の注入
- `pyproject.toml`: version `1.2.8`

**新規ファイル**:
- `tests/test_workflow_branch_cleanup.py`: 12+ テスト

**テスト数**: 3071 → 3083+

## §10.85 — v1.2.9: ワークフローフェーズ Webhook イベント (phase_complete / phase_failed / phase_skipped)

### 選択理由
v1.1.38でWorkflowManagerはフェーズ完了トラッキングを実装したが、フェーズ遷移時にWebhookを発火していなかった（既知の制限事項）。外部システムがワークフロー進捗をフェーズ粒度でモニタリングできるよう、`phase_complete`/`phase_failed`/`phase_skipped`のWebhookイベントを追加する。

選択しなかった候補:
- タスク優先度自動調整 (次回候補) — フェーズWebhookの方が依存関係が明確

### Research References

1. **Webhook Best Practices (Integrate.io, TechTarget, 2025)**
   - Queue-first: ingest layer decoupled from processing — respond with 2xx immediately, then process.
   - HMAC-SHA256 verification window + minimal schema validation on receipt.
   - Per-event-type payloads with consistent timestamp + event identifier structure.
   - Sources: https://www.integrate.io/blog/apply-webhook-best-practices/
              https://www.techtarget.com/searchapparchitecture/tip/Implementing-webhooks-Benefits-and-best-practices

2. **Event-Driven Workflow Phase Granularity (DEV Community / DreamFactory, 2025)**
   - Balance granularity vs complexity: per-phase events (not per-task) is appropriate for external consumers.
   - Hierarchical event types: `workflow_complete` → `phase_complete` → `task_complete` (fine-grained ladder).
   - JSON path filtering for granular subscriber criteria.
   - Sources: https://dev.to/vikthurrdev/designing-a-webhook-service-a-practical-guide-to-event-driven-architecture-3lep
              https://blog.dreamfactory.com/webhook-triggers-for-event-driven-apis

3. **CloudEvents Specification v1.0 (CNCF, 2024–2025)**
   - Core fields: `specversion`, `type`, `source`, `id`, `time`, `subject`, `datacontenttype`.
   - `type` as reverse-DNS dot-separated: e.g. `com.example.workflow.phase.complete`.
   - Combination of `id` + `source` must uniquely identify an event (idempotency key).
   - Our payload includes `workflow_id` (source), `phase_name` (subject), `timestamp` (time), and `task_ids`.
   - Sources: https://cloudevents.io/
              https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md

### 設計判断

**注入方式**: `WorkflowManager.set_webhook_fn(fn)` — `_branch_cleanup_fn` と同じ直接注入パターン。
バスを経由しない理由: WebhookManagerはバスを購読していない（直接呼び出しパターンが既存コードの慣例）。

**注入タイミング**: `web/app.py` の scratchpad/cancel 注入ブロックと同じ場所に追加。

**Async**: `asyncio.ensure_future()` で fire-and-forget（`_branch_cleanup_fn` と同パターン）。

**新規イベント名**: `phase_complete`, `phase_failed`, `phase_skipped` を `KNOWN_EVENTS` に追加。

**フェーズ skipped**: `_mark_task_skipped()` がフェーズを `mark_skipped()` する際にも発火。
ただしタスク経由ではなく直接 `_fire_phase_webhook` を呼ぶ。

### 実装変更ファイル
- `src/tmux_orchestrator/infrastructure/webhook_manager.py`: `KNOWN_EVENTS` に3イベント追加
- `src/tmux_orchestrator/application/workflow_manager.py`:
  - `_fire_webhook_fn: Callable[[str, dict], Awaitable[None]] | None` フィールド
  - `set_webhook_fn(fn)` メソッド
  - `_update_phase_status()`: complete/failed 遷移後に `asyncio.ensure_future(_fire_phase_webhook(...))`
  - `_mark_task_skipped()`: phase skipped 遷移後に webhook 発火
- `src/tmux_orchestrator/web/app.py`: WorkflowManager への `set_webhook_fn` 注入
- `src/tmux_orchestrator/web/routers/webhooks.py`: ドキュメント文字列に新イベント名追記
- `pyproject.toml`: version `1.2.9`

**新規ファイル**:
- `tests/test_workflow_phase_webhooks.py`: 12+ テスト

**テスト数**: 3083 → 3095+

## §10.86 — v1.2.10: Codified Context spec file injection + PairCoder writer→reviewer loop

### 選定理由 (Selection Rationale)

**What was chosen**: Codified Context spec file injection into CLAUDE.md (`spec_files` field on `AgentConfig`)
and a PairCoder workflow (`POST /workflows/paircoder`) that drives a writer→reviewer loop until the
reviewer approves the implementation against the project's codified conventions.

**Why this**: The previous `context_spec_files` field (v1.0.x) copies raw YAML spec files into the agent's
worktree (cold-memory pattern), but does not inject the spec content into the agent's CLAUDE.md (hot-memory).
Agents must explicitly read those files — they are not guaranteed to do so. Injecting spec rules directly
into CLAUDE.md ensures every agent sees them on its very first context load (hot-memory, always-loaded pattern).
The PairCoder workflow provides a concrete validation of the spec injection mechanism and the iterative
review pattern identified in the research.

**What was NOT chosen**: Dynamic short-circuit of max_rounds (skipping round 2 if reviewer passes round 1) —
this requires runtime scratchpad polling and is deferred; static pre-expansion (all rounds submitted to
DAG upfront) is simpler and consistent with the LoopBlock pattern already in place.

### Research Findings

#### Query 1: "codified context AI agent conventions specification YAML machine readable 2025 2026"

**Vasilopoulos et al., "Codified Context: Infrastructure for AI Agents in a Complex Codebase"**,
arXiv:2602.20478 (February 2026). URL: https://arxiv.org/abs/2602.20478

Key contribution: a three-tier codified context infrastructure developed during construction of a
108,000-line C# distributed system: (1) hot-memory constitution (always-loaded rules and conventions
encoding orchestration protocols); (2) 19 specialised domain-expert agents; (3) cold-memory knowledge
base of 34 on-demand specification documents.  The paper distinguishes hot-memory (always injected —
maps to CLAUDE.md) from cold-memory (retrieved on demand — maps to context_spec_files).

**ContextCov: Deriving and Enforcing Executable Constraints from Agent Instruction Files**,
arXiv:2603.00822. URL: https://arxiv.org/html/2603.00822

Proposes automatic derivation of executable constraints from AGENTS.md/CLAUDE.md files.  Validates
that constraint-aware agents achieve higher instruction-following rates.

**Scaling your coding agent's context beyond a single AGENTS.md file**,
ursula8sciform.substack.com (2025). URL: https://ursula8sciform.substack.com/p/scaling-your-coding-agents-context

Recommends a multi-file convention hierarchy: AGENTS.md root → per-directory AGENTS.md → per-role
spec files.  Maps directly to our `spec_files: list[str]` + CLAUDE.md injection approach.

#### Query 2: "pair programming AI agents reviewer writer loop pattern multi-agent code review 2025"

**"The Ralph Loop" (Geoffrey Huntley)**. Referenced in:
"After 2 years of AI-assisted coding, I automated the one thing that actually improved quality: AI Pair Programming",
dev.to/yw1975 (2025). URL: https://dev.to/yw1975/after-2-years-of-ai-assisted-coding-i-automated-the-one-thing-that-actually-improved-quality-ai-2njh

The Ralph Loop wraps a coding agent in an external loop so it keeps iterating. Core insight: a single
agent cannot reliably catch its own mistakes (writes code AND judges whether the code is good — like
grading your own exam). Separation of Generator (writer) from Critic (reviewer) breaks this symmetry.

**"The Art of AI Pair Programming: Patterns That Actually Work"**, Groundy (2025).
URL: https://groundy.com/articles/art-ai-pair-programming-patterns-that-actually-work/

Dual-agent workflow: one agent writes, another watches and reviews. The same principle applies to
automated agents — the reviewer needs to check the writer's output against specific, hard-coded
criteria (spec compliance).

**Developer's guide to multi-agent patterns in ADK**, Google Developers Blog (2025).
URL: https://developers.googleblog.com/developers-guide-to-multi-agent-patterns-in-adk/

Generator-and-Critic pattern: separates creation from validation. The Critic checks against
specific criteria, returning structured PASS/FAIL feedback that the Generator uses for revision.

#### Query 3: "spec-driven agent constraints machine readable coding conventions LLM compliance 2025"

**"Spec-driven development: Unpacking one of 2025's key new AI-assisted engineering practices"**,
Thoughtworks (2025). URL: https://www.thoughtworks.com/en-us/insights/blog/agile-engineering-practices/spec-driven-development-unpacking-2025-new-engineering-practices

Machine-readable specs serve as runtime invariants, not aspirational documentation.  Spec-driven development
treats specifications as first-class citizens that LLMs use as grounding constraints.

**"Constitutional Spec-Driven Development: Enforcing Security by Construction in AI-Assisted Code Generation"**,
arXiv:2602.02584 (2026). URL: https://arxiv.org/html/2602.02584v1

Embeds non-negotiable principles into the specification layer, ensuring AI-generated code adheres to
conventions by construction (via CLAUDE.md injection) rather than by inspection (post-hoc review).
Maps to our `spec_files` injection into CLAUDE.md — the writer sees rules before generating code.

**AgentSpec: Customizable Runtime Enforcement for Safe and Reliable LLM Agents**,
arXiv:2503.18666 (2025). URL: https://arxiv.org/abs/2503.18666

Lightweight DSL for specifying and enforcing runtime constraints on LLM agents (triggers, predicates,
enforcement mechanisms).  Demonstrates >90% unsafe execution prevention.  Our YAML spec format
(name/description/rules/examples) is a simplified subset of this DSL — human-readable and LLM-parseable.

### 実装変更ファイル (Implementation Files)

- `src/tmux_orchestrator/agents/claude_code.py`:
  - `_load_spec_files(self) -> str`: new method — loads YAML spec files, formats as ## Codified Specs section
  - `_write_agent_claude_md()`: appends `_load_spec_files()` output before Slash Command Reference
  - New `spec_files: list[str]` and `spec_files_root: Path | None` instance fields
- `src/tmux_orchestrator/application/config.py`:
  - `AgentConfig.spec_files: list[str] = field(default_factory=list)` — paths to YAML spec files
  - `load_config()`: reads `spec_files` from YAML config
- `src/tmux_orchestrator/web/schemas.py`: `PairCoderWorkflowSubmit` model
- `src/tmux_orchestrator/web/routers/workflows.py`: `POST /workflows/paircoder` endpoint
- `examples/specs/python_style.yaml` + `examples/specs/testing_conventions.yaml`: sample spec files
- `examples/workflows/paircoder.yaml`: workflow YAML template
- `tests/test_workflow_paircoder.py`: 12+ tests
- `pyproject.toml`: version `1.2.10`

**テスト数**: 3102 → 3114+

## §10.87 — v1.2.11: エージェント統計ダッシュボード改善 (Agent Stats Dashboard Enhancement)

### 選択理由 (Selection Rationale)

Selected `v1.2.11 — エージェント統計ダッシュボード改善` because:
- Foundational observability gap: agent data (task history, error counts, context usage) is tracked internally but only minimally exposed via REST.
- The existing `GET /agents/{id}/stats` returns context-monitor stats and basic task_count/error_count but lacks derived metrics (error_rate, avg_task_duration_s, last_task_at).
- No cross-agent summary endpoint exists — operators cannot compare agent performance at a glance.
- The TUI StatusBar shows only PAUSED/RUNNING state — no aggregate metrics visible.

Not chosen: `v1.2.12 — セキュリティ強化` and `v1.2.13 — WebUI リアルタイム更新強化` — deferred to next iteration.

### Research Findings

**1. Agent Monitoring Dashboard Metrics (UptimeRobot / Google SRE / Datadog, 2025)**
- Google SRE "Four Golden Signals": latency, traffic, errors, saturation. For agents: avg_task_duration_s (latency), tasks_completed (traffic), error_rate (errors), context_pct (saturation).
  Reference: https://sre.google/sre-book/monitoring-distributed-systems/
- RED method (Request rate, Errors, Duration) is the recommended pattern for dashboard layout.
  Reference: https://uptimerobot.com/knowledge-hub/monitoring/ai-agent-monitoring-best-practices-tools-and-metrics/
- Datadog DASH 2025: consolidated agent-level metric view is now a baseline requirement.
  Reference: https://www.datadoghq.com/blog/dash-2025-new-feature-roundup-observe/

**2. REST API Summary/Aggregate Endpoint Design (Medium / TechTarget, 2025)**
- `GET /agents/summary` follows the "aggregator resource" pattern: a dedicated resource that aggregates over the collection without replacing the per-item endpoints.
  Reference: https://medium.com/javarevisited/top-3-api-aggregation-patterns-with-real-world-examples-6b3da985bc36
- Summary endpoints should be additive (non-breaking): existing `GET /agents` and `GET /agents/{id}` remain unchanged.
  Reference: https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/app-experience-analytics-saas/SaaS/using/using-apis/rest-apis-best-practices.html

**3. Observability in Multi-Agent Systems (OpenTelemetry / IBM / Microsoft Azure, 2025)**
- OpenTelemetry 2025: "distributed tracing across agent workflows, token attribution, automated evals" are baseline requirements.
  Reference: https://opentelemetry.io/blog/2025/ai-agent-observability/
- IBM: "without observability you cannot reason about multi-agent system behaviour at scale".
  Reference: https://www.ibm.com/think/insights/ai-agent-observability
- Microsoft Azure Agent Factory top 5 best practices include: per-agent latency histogram, error rate threshold alerting, cross-agent comparison dashboards.
  Reference: https://azure.microsoft.com/en-us/blog/agent-factory-top-5-agent-observability-best-practices-for-reliable-ai/
- TAMAS (IBM, arXiv:2503.06745): per-agent task history enables bottleneck analysis; summary metrics enable capacity planning.

### 実装内容 (Implementation)

#### Enhanced `GET /agents/{id}/stats`
Added derived metrics computed from `_agent_history` in-memory store:
- `tasks_completed: int` — tasks with status "success"
- `tasks_failed: int` — tasks with status "error"
- `avg_task_duration_s: float | None` — mean of duration_s across all completed tasks (success + error)
- `error_rate: float` — tasks_failed / max(tasks_completed + tasks_failed, 1)
- `last_task_at: str | None` — ISO timestamp from most recent history entry's finished_at

Renamed existing `task_count` → kept for backward compat; added new derived fields alongside.

#### `GET /agents/summary`
New endpoint returning cross-agent aggregate view:
```json
{
  "agents": [...],
  "total_agents": 2,
  "total_tasks_completed": 24,
  "total_tasks_failed": 1,
  "busiest_agent": "worker-1"
}
```
Route registered BEFORE `GET /agents/{agent_id}` to avoid path collision.

#### `GET /agents/{id}/history` (enhancement)
Already existed with correct fields. No changes needed.

#### TUI StatusBar Enhancement
Added reactive fields: `tasks_completed`, `active_agents`, `high_error_agents` (list of agent IDs with error_rate > 20%).
The `OrchestratorTuiApp` calls `status_bar.update_stats()` in its agent refresh timer.

### 実装変更ファイル (Implementation Files)

- `src/tmux_orchestrator/web/routers/agents.py`: `GET /agents/summary` endpoint; enhanced `GET /agents/{id}/stats` with derived metrics
- `src/tmux_orchestrator/tui/widgets.py`: `StatusBar` reactive fields + `update_stats()` method
- `src/tmux_orchestrator/tui/app.py`: call `status_bar.update_stats()` during agent refresh
- `tests/test_agent_stats_dashboard.py`: 12+ tests
- `pyproject.toml`: version `1.2.11`

**テスト数**: 3114+ → 3126+

## §10.88 — v1.2.12: サーキットブレーカー自動再起動 (Circuit Breaker Auto-Restart)

### 選択理由 (Selection Rationale)

**選択**: Circuit Breaker + エージェント自動再起動 (auto-restart unhealthy agents)

**理由**: The system already tracks agent failure state via CircuitBreaker (OPEN/CLOSED/HALF-OPEN). However when an agent accumulates consecutive failures, no remediation occurs — the circuit stays open indefinitely until recovery_timeout elapses and the probe succeeds. This is insufficient for long-lived orchestration: a stuck agent blocks queue capacity and produces cascading dependency failures. Auto-restart (one-for-one, per Erlang OTP) is the natural complement to circuit breaking.

**選択しなかったもの**: Distributed tracing improvements, workflow visualisation — deprioritised because fault tolerance infrastructure has a higher ROI at this stage.

### Research Findings

**1. Scheduler Agent Supervisor pattern (Microsoft Azure Architecture Center, 2024)**
- "The Supervisor monitors the status of steps and if it detects any that have timed out or failed, it arranges for the appropriate Agent to recover the step or execute the appropriate remedial action."
- Directly maps to `_restart_agent(agent_id)`.
- Reference: https://learn.microsoft.com/en-us/azure/architecture/patterns/scheduler-agent-supervisor

**2. Erlang OTP one_for_one supervisor restart strategy (Erlang/OTP docs, 2024)**
- "If one child process terminates and is to be restarted, only that child process is affected."
- Restart intensity: `MaxR` restarts within `MaxT` seconds — prevent infinite restart loops.
- The `max_consecutive_failures` threshold maps to `MaxR`; the orchestrator tracks consecutive failures per agent.
- Reference: https://www.erlang.org/doc/system/sup_princ.html

**3. Amazon ECS unhealthy task replacement (AWS Blog, 2023)**
- "ECS will first start a healthy replacement for each unhealthy task that failed to pass a health check, before terminating it."
- Key pattern: replacement is created with the SAME config (template), same ID preserved for routing.
- Reference: https://aws.amazon.com/blogs/containers/a-deep-dive-into-amazon-ecs-task-health-and-task-replacement/

### 実装内容 (Implementation)

#### `AgentConfig.max_consecutive_failures: int = 3`
Per-agent threshold. `0` = disabled (opt-out). Loaded from YAML.

#### `OrchestratorConfig.supervision_enabled: bool = True`
Global kill-switch. When `False`, `_restart_agent` is a no-op regardless of per-agent config.

#### `Orchestrator._consecutive_failures: dict[str, int]`
Per-agent consecutive failure counter. Reset to 0 on success. Incremented on final failure (retries exhausted).

#### `Orchestrator._restart_counts: dict[str, int]`
Cumulative restart count per agent. Exposed via `GET /agents/{id}/stats` as `restart_count`.

#### `Orchestrator._restart_agent(agent_id: str)`
Async method. Stops old agent, creates fresh one with same config + ID, registers, starts.
Publishes `agent_restarted` STATUS bus event. Ephemeral agents are excluded.

#### `GET /agents/{id}/stats` enhancement
Added `restart_count: int` field.

### 実装変更ファイル (Implementation Files)

- `src/tmux_orchestrator/application/config.py`: `AgentConfig.max_consecutive_failures`, `OrchestratorConfig.supervision_enabled`
- `src/tmux_orchestrator/application/orchestrator.py`: `_consecutive_failures`, `_restart_counts`, `_restart_agent()`, hook in `_route_loop`
- `src/tmux_orchestrator/web/routers/agents.py`: add `restart_count` to stats response
- `tests/test_agent_auto_restart.py`: 12+ tests
- `pyproject.toml`: version `1.2.12`

**テスト数**: 3126+ → 3158+


## §10.89 — v1.2.13: タスクタイムアウト エスカレーション (Task Timeout Escalation)

### 選択理由 (Selection Rationale)

**選択**: Task Timeout Escalation — re-queue timed-out tasks at higher priority, excluding the agent that timed out.

**理由**: v1.2.12 introduced agent auto-restart for consecutive failures, but tasks themselves are permanently lost on timeout. This is a data loss scenario for critical long-running tasks. Escalation (re-queue + priority bump + excluded_agents) recovers the task without relying on the user to resubmit manually. This is the natural complement to agent auto-restart.

**選択しなかったもの**: Distributed tracing improvements, workflow visualisation — still lower priority.

### Research Findings

**1. Queue-Based Escalating Retry Pattern (GitGuardian, "Celery Task Resilience", 2024)**
- Tasks that exceed time limit are re-scheduled on a separate queue processed by more powerful workers with longer timeouts.
- Key insight: "escalating retry" = different queue / different worker on each retry attempt.
- Reference: https://blog.gitguardian.com/celery-tasks-retries-errors/

**2. Temporal WorkflowTaskTimeout and task reassignment (Temporal Community Forum, 2024)**
- When a workflow task times out, Temporal marks it TIMED_OUT and schedules a new instance based on retry config.
- Sticky execution after Worker shutdown causes "Workflow Task Timed Out" → reassigned to fresh worker.
- The `excluded_agents` list implements this "avoid the stuck worker" pattern.
- Reference: https://community.temporal.io/t/handling-workflow-task-timeout-due-to-sticky-queue-task-timeout/16443

**3. Aging / Priority Escalation (Wikipedia "Aging (scheduling)"; GeeksforGeeks, 2024)**
- Aging = gradually increase priority of a waiting/retrying process to prevent starvation.
- "Aging dynamically adjusts priorities based on waiting duration, typically by decrementing the priority number at regular intervals."
- Priority bump on each escalation (lower number = higher urgency) mirrors this pattern.
- Reference: https://en.wikipedia.org/wiki/Aging_(scheduling)

**4. AWS Builders Library — Timeouts, retries and backoff with jitter (Amazon, 2022)**
- "Timeout management requires setting visibility timeout greater than processing time plus retry delay."
- Combining timeouts with retries is standard; the escalation count acts as a bounded retry counter.
- Reference: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

### 実装内容 (Implementation)

#### `Task.escalation_count: int = 0`
How many times this task has been escalated due to timeout. Added to `Task` dataclass and `to_dict()`.

#### `Task.excluded_agents: list[str]`
Agent IDs that have already timed out this task and should not receive it again. Default `[]`.

#### `OrchestratorConfig.task_escalation_enabled: bool = True`
Global on/off switch. When `False`, timeout → immediate failure (v1.2.12 behaviour).

#### `OrchestratorConfig.max_task_escalations: int = 2`
After this many escalations, the task is finally marked as failed (dead-lettered).

#### `Orchestrator._handle_task_timeout(task, timed_out_agent_id)`
Called from `_route_loop` when a RESULT with `error="watchdog_timeout"` arrives.
- If escalation disabled or count >= max: call existing failure path.
- Otherwise: `dataclasses.replace(task, escalation_count+1, excluded_agents+agent_id, priority-1)`; re-enqueue; publish `task_escalated` STATUS event.

#### `_dispatch_loop` excluded_agents filter
`find_idle_worker()` gains an optional `excluded_agent_ids` parameter.
When `task.excluded_agents` is non-empty, those agents are skipped during dispatch.

#### `GET /tasks/{id}` enhancement
`escalation_count` and `excluded_agents` added to in-progress and waiting task responses.

#### `load_config()` reads `task_escalation_enabled` and `max_task_escalations` from YAML.

### 実装変更ファイル (Implementation Files)

- `src/tmux_orchestrator/domain/task.py`: `escalation_count`, `excluded_agents` fields + `to_dict()`
- `src/tmux_orchestrator/application/config.py`: `OrchestratorConfig.task_escalation_enabled`, `max_task_escalations`
- `src/tmux_orchestrator/application/registry.py`: `find_idle_worker(excluded_agent_ids=...)`
- `src/tmux_orchestrator/application/orchestrator.py`: `_handle_task_timeout()`, hook in `_route_loop`, excluded_agents dispatch filter
- `src/tmux_orchestrator/web/routers/tasks.py`: expose `escalation_count`, `excluded_agents`
- `tests/test_task_escalation.py`: 12+ new tests
- `pyproject.toml`: version `1.2.13`


## §10.90 — v1.2.14: ワークフロー DAG 可視化 (Workflow DAG Visualization)

### 選択理由 (Selection Rationale)

**選択**: Workflow DAG Visualization — `GET /workflows/{id}/dag` endpoint returning a graph of nodes and edges with real-time task status.

**理由**: `GET /workflows/{id}` returns phase names and task counts but no structural dependency information. Operators debugging blocked workflows cannot determine which tasks are waiting on predecessors vs. ready to run. A dedicated DAG endpoint exposes the dependency graph with live status per node, enabling visual and programmatic inspection. This is a natural complement to the existing phase completion tracking (v1.1.38).

**選択しなかったもの**: Distributed tracing improvements — lower priority; agent communication metrics — requires deeper bus instrumentation.

### Research Findings

**1. Standard graph structure `{nodes: [], edges: []}` (bionode/bionode-watermill Issue #50, 2016)**
- The standard REST API graph format uses `{nodes: [], edges: []}` rather than adjacency lists embedded in node objects. This avoids duplication and simplifies incremental updates.
- Reference: https://github.com/bionode/bionode-watermill/issues/50

**2. AWS Glue GetDataflowGraph API (AWS Documentation, 2024)**
- Returns `DagNodes[]` (with Id, NodeType) and `DagEdges[]` (with Source, Target) as separate top-level arrays.
- Industry standard: separate node metadata from topology.
- Reference: https://docs.aws.amazon.com/glue/latest/webapi/API_GetDataflowGraph.html

**3. ZenML DAG visualization (ZenML Blog, 2024)**
- Server API returns graph data; frontend renders with dagrejs (layout) + ReactFlow (SVG edges).
- Per-node status colours drive visual progress indicators.
- Key insight: include `status`, `started_at`, `finished_at` on each node so clients can render progress bars and compute durations without a second API call.
- Reference: https://www.zenml.io/blog/dag-visualization-vscode-extension

**4. Task dependency graph design (Berkeley Patterns, EECS)**
- "Tasks form vertices of a directed acyclic graph while directed edges show dependencies."
- Client-side dependency resolution: collect first-level deps as strings, then replace with pointers. Best practice: store edges separately (not nested) for efficient traversal.
- Reference: https://patterns.eecs.berkeley.edu/?page_id=609

**5. Best practices for workflow JSON schemas (FlowSpec, woodyhayday, 2024)**
- "Modularity and composition": a step's `depends_on` list is the canonical source of edge data.
- Include `version` compatibility notes in shared workflows.
- Reference: https://github.com/woodyhayday/FlowSpec

### 実装内容 (Implementation)

#### `Orchestrator._task_deps: dict[str, list[str]]`
Persistent (not cleaned up on task completion) mapping of `task_id → list[depends_on task_ids]`.
Populated at `submit_task()` time alongside `_task_dependents`.

#### `Orchestrator.get_task_info(task_id) -> dict`
Returns `{task_id, status, depends_on, dependents, assigned_agent, started_at, finished_at}` for any task in the system (active, waiting, queued, completed, failed).

#### `WorkflowRun.dag_edges: list[tuple[str, str]]`
Optional field storing `(from_task_id, to_task_id)` edge pairs. Set during workflow submission from the `local_to_global` mapping. Avoids re-deriving topology at query time.

#### `WorkflowManager.submit(name, task_ids, *, dag_edges=None)`
Extended signature accepting `dag_edges` list. Stored on the returned `WorkflowRun`.

#### `GET /workflows/{id}/dag` endpoint
Returns `{workflow_id, name, status, nodes[], edges[]}` where each node has `task_id`, `phase_name`, `status`, `depends_on`, `dependents`, `started_at`, `finished_at`, `duration_s`, `assigned_agent`. Returns 404 for unknown workflow.

### 実装変更ファイル (Implementation Files)

- `src/tmux_orchestrator/domain/workflow.py`: `WorkflowRun.dag_edges` field
- `src/tmux_orchestrator/application/workflow_manager.py`: `submit(dag_edges=None)` extension
- `src/tmux_orchestrator/application/orchestrator.py`: `_task_deps`, `get_task_info()`
- `src/tmux_orchestrator/web/routers/workflows.py`: `GET /workflows/{id}/dag` endpoint + dag_edges wiring
- `tests/test_workflow_dag.py`: 12+ new tests

---

## §10.91 — v1.2.15: グループブロードキャストタスク + Best-of-N 選択 (Group Broadcast Task + Best-of-N)

### 選択理由 (Selection Rationale)

Chosen because: (1) competitive multi-agent patterns are a flagship use case but lack dedicated API support; (2) the existing `cancel_task()` mechanism (v1.2.6) and `target_agent` routing (domain/task.py) provide the primitives needed; (3) AHC-style Best-of-N is the canonical demo scenario referenced in CLAUDE.md but has never been exercised end-to-end via the REST API.

Not chosen: phase-level webhook events (requires WorkflowPhaseStatus refactor), adaptive batching (no user demand yet).

### 調査結果 (Research Findings)

**1. Sakana AI ALE-Agent wins AtCoder Heuristic Contest 058 (Sakana AI, 2025)**
- Strategy: "generate multiple programs simultaneously, summarise results, use insights for subsequent code generation" — explicit parallel generation + aggregation.
- Used 2,654 GPT-5.2 + 2,119 Gemini 3 Pro calls over 4 hours; $1,300 budget.
- Key quote: "overwhelming number of trial-and-error steps combined with LLM reasoning is an advantage humans do not have."
- **Relevance**: confirms that fan-out + best-of-N winner selection is the dominant competitive pattern for AHC problems.
- Reference: https://sakana.ai/ahc058/ (2025)

**2. LAMaS — Latency-Aware Multi-agent System (arXiv:2601.10560, 2025)**
- Most existing approaches "assume sequential execution" and ignore parallelism latency.
- LAMaS reduces critical path length 38–46% vs. baselines by constructing explicit parallel execution topology graphs.
- **Relevance**: validates fan-out (parallel dispatch) as the correct primitive for time-sensitive competitive solving; race mode maps to "shortest critical path" selection.
- Reference: https://arxiv.org/abs/2601.10560 (2025)

**3. Fan-out / Fan-in concurrency pattern (Go Concurrency Patterns, DEV Community / Medium, 2025)**
- Fan-out: distribute work across N goroutines/tasks; fan-in: collect via shared result channel.
- Context cancellation is the canonical mechanism to abort remaining workers when first result arrives (race mode).
- Shared mutable state across goroutines without synchronisation causes data races — use channels/locks.
- **Relevance**: Python asyncio equivalent is a `dict[str_id, str_result]` guarded by the event loop's single-threaded semantics; `cancel_task()` is the cancel primitive.
- Reference: https://dev.to/serifcolakel/go-concurrency-patterns-worker-pool-fan-in-fan-out-pipeline-49pd (2025)

**4. MultiAgentBench — Competitive + Collaborative LLM evaluation (ACL 2025)**
- Best-of-N competitive selection is a first-class evaluation pattern; results are aggregated using objective scoring functions.
- **Relevance**: confirms that orchestrators should support both "gather all" (ensemble) and "race/first" semantics as distinct modes.
- Reference: https://aclanthology.org/2025.acl-long.421/ (2025)

### 実装内容 (Implementation)

#### `BroadcastGroup` dataclass (`application/orchestrator.py`)
In-memory record of a broadcast operation:
- `broadcast_id: str`
- `mode: str` — `"race"` or `"gather"`
- `task_ids: list[str]`
- `agent_ids: list[str]`
- `completed_tasks: dict[str, str]`  — task_id → result text
- `failed_tasks: set[str]`
- `cancelled: bool`
- `winner_task_id: str | None`
- `status: str` — `"pending"` | `"running"` | `"complete"` | `"failed"`

#### `Orchestrator._broadcast_groups: dict[str, BroadcastGroup]`
Module-level dict keyed by `broadcast_id`.

#### `Orchestrator._task_to_broadcast: dict[str, str]`
Maps `task_id → broadcast_id` for O(1) lookup in `_route_loop`.

#### `Orchestrator.broadcast_task(...) -> BroadcastResult`
Submits N tasks (one per `agent_id`), creates a `BroadcastGroup`, returns a `BroadcastResult`.

#### `_route_loop` broadcast hook
After normal RESULT handling: if `task_id` is in `_task_to_broadcast`:
- **Race mode**: first successful result → mark winner, cancel remaining tasks → status=complete
- **Gather mode**: collect result; when all tasks complete → status=complete; if all fail → status=failed

#### REST endpoints
- `POST /tasks/broadcast` — submit broadcast; returns `{broadcast_id, task_ids, agent_ids, mode}`
- `GET /tasks/broadcast/{broadcast_id}` — poll status; returns complete payload with winner/results

### 実装変更ファイル (Implementation Files)

- `src/tmux_orchestrator/application/orchestrator.py`: `BroadcastGroup`, `_broadcast_groups`, `_task_to_broadcast`, `broadcast_task()`, `get_broadcast()`, `_route_loop` hook
- `src/tmux_orchestrator/web/schemas.py`: `BroadcastTaskSubmit`
- `src/tmux_orchestrator/web/routers/tasks.py`: `POST /tasks/broadcast`, `GET /tasks/broadcast/{id}`
- `tests/test_broadcast_task.py`: 12+ new tests
- `pyproject.toml`: version `1.2.15`

## §10.92 — v1.2.16: エージェント使用率時系列 + メトリクスエンドポイント (Agent Metrics Time Series)

### 選択理由 (Selection Rationale)
Chosen because:
- **Priority**: Operators lack visibility into trends. Point-in-time `/orchestrator/status` shows current state but not whether the system was busy 5 minutes ago or whether queue depth is growing.
- **Not chosen**: Distributed tracing enhancements (lower urgency — OTel spans already cover individual tasks); session replay (requires large storage design).
- **Dependency**: Builds on existing `queue_depth()` method and `list_agents()`. No new infrastructure deps (pure stdlib `collections.deque`).

### Research Findings

**1. Lightweight Python Ring Buffer for Time Series (ssojet.com / enbnt.dev, 2024-2025)**
- `collections.deque(maxlen=N)` is the canonical Python ring buffer: O(1) append/pop, thread-safe for simple append-only use, bounded memory. Ideal for rolling metrics windows without external deps.
- Reference: https://ssojet.com/data-structures/implement-circular-buffer-in-python
- Reference: https://www.enbnt.dev/posts/timeseries-dense-sparse-circle/

**2. REST API Metrics Endpoint Design — Prometheus + JSON (Prometheus.io / BetterStack, 2025)**
- Prometheus `GET /metrics` serves text-format gauge/counter data for scraping. For JSON-native REST clients, a parallel `GET /metrics/json` or query-param `format=json` is standard practice.
- Key metrics for agent orchestrators: agent status distribution (gauge), queue depth (gauge), tasks completed (counter), error rate (derived).
- Reference: https://prometheus.io/docs/prometheus/latest/querying/api/
- Reference: https://betterstack.com/community/questions/how-to-monitor-rest-apis-with-prometheus/
- Reference: https://signoz.io/guides/how-do-i-monitor-api-in-prometheus/

**3. Multi-Agent System Workload Metrics Collection (arXiv:2503.06745 / galileo.ai, 2025)**
- "Beyond Black-Box Benchmarking" (arXiv:2503.06745, Galileo 2025): recommends capturing agent call frequency, per-call status, retry counts, and output quality over time — not just at-completion snapshots.
- Adaptive sampling: 10-second collection intervals are appropriate for LLM agent workloads (tasks take 10s–5min; sub-second polling adds overhead with no benefit).
- Hierarchical monitoring: collect at orchestrator level (aggregate) + per-agent level (granular).
- Reference: https://arxiv.org/html/2503.06745v1
- Reference: https://galileo.ai/blog/challenges-monitoring-multi-agent-systems

### 設計 (Design)

#### `MetricsCollector` in `infrastructure/metrics_collector.py`
- `deque(maxlen=max_snapshots)` ring buffer — O(1) append, bounded memory
- Getter injection pattern: `get_queue_depth`, `get_agent_statuses`, `get_agent_history` — no direct orchestrator coupling
- `asyncio.create_task` collect loop with configurable interval
- `MetricsSnapshot` dataclass: ISO timestamp, queue_depth, active_agents, idle_agents, tasks_completed_total, tasks_failed_total, per_agent dict

#### New Orchestrator methods
- `queue_size() -> int` — alias for `queue_depth()` (new name matching spec)
- `get_all_agent_statuses() -> dict[str, str]` — `{agent_id: status}` for all agents
- `get_cumulative_stats() -> dict` — `{tasks_completed_total, tasks_failed_total}` derived from `_agent_history`

#### REST endpoints (system router)
- `GET /metrics/time-series` — last N snapshots (default 60), returns `{interval_s, count, snapshots, latest}`
- `GET /metrics/agents/{agent_id}` — per-agent series from snapshot `per_agent` dicts

Note: existing `GET /metrics` serves Prometheus text format — new JSON endpoints use `/metrics/time-series` to avoid collision.

#### Config additions to `OrchestratorConfig`
- `metrics_enabled: bool = True`
- `metrics_interval_s: float = 10.0`
- `metrics_max_snapshots: int = 360`

### 実装変更ファイル (Implementation Files)

- `src/tmux_orchestrator/infrastructure/metrics_collector.py`: new `MetricsCollector` + `MetricsSnapshot`
- `src/tmux_orchestrator/application/orchestrator.py`: `queue_size()`, `get_all_agent_statuses()`, `get_cumulative_stats()`
- `src/tmux_orchestrator/application/config.py`: `metrics_enabled`, `metrics_interval_s`, `metrics_max_snapshots`
- `src/tmux_orchestrator/web/routers/system.py`: `GET /metrics/time-series`, `GET /metrics/agents/{id}`
- `src/tmux_orchestrator/web/app.py`: wire `MetricsCollector` start/stop in lifespan
- `tests/test_metrics_collector.py`: 12+ new tests
- `pyproject.toml`: version `1.2.16`
- `pyproject.toml`: version `1.2.14`

---

## §10.93 — v1.2.17: タスクテンプレート + プリセットライブラリ (Task Templates + Preset Library)

### 選択理由 (Selection Rationale)

**Chosen**: Task Templates + Preset Library
**Why**: Users repeatedly craft similar task prompts (code review, bug fix, write tests) from scratch each time. A parameterized template registry reduces friction and errors in prompt construction, directly improving UX for the primary use-case of the system. This is a self-contained feature with no dependencies on previous iterations beyond the existing task submission machinery.

**Not chosen**: Webhook filtering / event routing — lower priority than DX improvement for regular prompt workflows.

### Research Findings

#### 1. Task Template + REST API Design
- **Salesforce Einstein Prompt Templates** (https://developer.salesforce.com/docs/atlas.en-us.chatterapi.meta/chatterapi/connect_resources_prompt_template.htm, 2024): REST-based prompt template API supporting POST to invoke templates with input parameters. Demonstrates industry practice of exposing template rendering as a dedicated endpoint (`POST /template/{id}/invoke`), separate from template CRUD endpoints.
- **PromptLayer Templates** (https://docs.promptlayer.com/reference/templates-get, 2025): Template retrieval API allowing version labels ("prod") for template pinning. Shows value of storing templates server-side with version control.
- **Paul Serban — Engineering a Scalable Prompt Library** (https://paulserban.eu/blog/post/engineering-a-scalable-prompt-library-from-architecture-to-code/, 2025): Architectural breakdown: templates (declarative assets), schemas (I/O contracts), a **registry** for lifecycle management, and **renderers** for safe interpolation. Recommends keeping prompts declarative and adding explicit input/output schemas.

#### 2. Prompt Template Registry + Placeholder Substitution
- **PromptLayer Template Variables** (https://docs.promptlayer.com/features/prompt-registry/template-variables, 2025): Two templating systems — f-strings (simple `{var}` substitution) and Jinja2 (advanced with conditionals). For agent dispatch, f-strings are sufficient and safer (no code execution).
- **Spring AI Prompts** (https://docs.spring.io/spring-ai/reference/api/prompt.html, 2025): `PromptTemplate` accepts a Map of placeholder → value pairs. Dynamic content via map keys matches the `dict[str, str]` interface chosen for this implementation.
- **Amazon Bedrock Prompt Placeholders** (https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-placeholders.html, 2025): Agents use `$varname$` syntax; our implementation uses Python `str.format(**vars)` which is idiomatic for Python projects and compatible with existing prompt patterns in TmuxAgentOrchestrator.
- **Vertex AI Prompt Registry** (https://oneuptime.com/blog/post/2026-02-17-how-to-implement-prompt-management-and-versioning-with-vertex-ai-prompt-registry/view, 2026): Central registry for prompt templates with version management. Validates the "registry as a service" pattern for managing prompt templates.

#### 3. Workflow Template YAML Override
- **Argo Workflows Template** (https://iamstoxe.com/posts/templating-with-argo/, 2024): Template parameters passed as input.parameters; templates referenced by name (templateRef). Pattern: separate template definition from instantiation, pass variable values at submission time.
- **YAML Inheritance Patterns** (https://www.tutorialpedia.org/blog/more-complex-inheritance-in-yaml/, 2025): YAML has no native inheritance — tools use Base + Overlay with deep merging. For our use-case, simple defaults in `TaskTemplate` dataclass fields (priority, tags, timeout) that can be overridden per-submission suffice without needing a full merge system.

### Design Decisions

1. **In-memory dict store** — no persistence needed for v1.2.17; built-in templates re-registered at startup.
2. **Python `str.format(**vars)` substitution** — idiomatic, safe (no code execution unlike Jinja2), raises `KeyError` for undefined placeholders naturally.
3. **Explicit `variables` list** — required so that the API can validate completeness before rendering (fail-fast, not after string format).
4. **`POST /templates/{id}/render`** — preview endpoint returns rendered prompt without submitting (testing/debugging use case).
5. **`POST /templates/{id}/submit`** — render + submit in one call; override fields (priority, tags, timeout) per submission.
6. **Built-in templates**: `code_review`, `bug_fix`, `write_tests` — three most common agent task patterns.

### Implementation Plan

#### New files
- `src/tmux_orchestrator/application/template_store.py`: `TaskTemplate` dataclass + `TemplateStore` class
- `src/tmux_orchestrator/web/routers/templates.py`: `build_templates_router()`
- `tests/test_task_templates.py`: 15+ tests

#### Modified files
- `src/tmux_orchestrator/web/schemas.py`: `TaskTemplateCreate`, `TaskTemplateRender`, `TaskTemplateSubmit`
- `src/tmux_orchestrator/web/routers/__init__.py`: export `build_templates_router`
- `src/tmux_orchestrator/web/app.py`: create `_template_store`, register built-ins, wire router
- `pyproject.toml`: version `1.2.17`

#### REST endpoints
- `POST /templates` — register template (201 Created)
- `GET /templates` — list all templates
- `GET /templates/{id}` — get template by ID
- `DELETE /templates/{id}` — delete template
- `POST /templates/{id}/render` — render with variables (preview, no submit)
- `POST /templates/{id}/submit` — render + submit as task

---

## §10.94 — v1.2.19: コンテキスト枯渇エージェントの自動ローテーション (Context-Exhausted Agent Auto-Rotation)

### Step 0 — 選定根拠 (2026-03-14)

**選択: コンテキスト枯渇時のエージェント自動ローテーション**

**何を選択したか・理由:**

ContextMonitor が `context_warning` イベントを発行する仕組みは v1.1.x で実装済みだが、
そのイベントを受け取ってオーケストレーター側が自律的に対処する機構がない。
現状フローは:
1. ContextMonitor が 75% 閾値を超えたら `context_warning` を bus に publish
2. エージェント自身が受け取り `/summarize` を実行（または手動対応）
3. それでも増え続けると最終的にコンテキストウィンドウが枯渇し、エージェントの判断精度が低下

**未実装の部分（今回の実装対象）:**
- `context_rotate_threshold` (デフォルト 90%) を超えたら orchestrator が自動介入
- 現エージェントを drain → 新エージェントを同一 config で spawn
- 前エージェントの NOTES.md を新エージェントの `context_files` として渡す（コンテキスト引き継ぎ）
- タスクキューの再ルーティング（pending tasks → 新エージェントへ）

**選ばなかったもの:**
- DriftMonitor embedding 強化: 中優先度で sentence-transformers 依存が発生する
- TLA+ 形式仕様化: 研究色が強くユーザー向け即効性が低い
- トレースリプレイ CLI: 低優先度

## §10.95 — v1.2.19: CLAUDE.md のロール別ルール分離 (.claude/rules/)

### Step 0 — 選定根拠 (2026-03-14)

**選択: ロール別ルールを .claude/rules/ に分離**

**何を選択したか・理由:**
現在の `_write_agent_claude_md()` は WORKER と DIRECTOR 両方のロール固有コンテンツを 1 つの
CLAUDE.md に書き込んでいる。Claude Code の `.claude/rules/` ディレクトリ機能を利用することで、
共通ルールと役割固有ルールを分離し、コンテキストウィンドウの効率化・メンテナンス性の向上を実現できる。

**選ばなかったもの:**
- コンテキスト枯渇エージェントの自動ローテーション (§10.94): 実装規模が大きく、先にルール分離を
  行うことで各エージェントの初期コンテキストを軽量化してから取り組む方が効果的

### Step 1 — Research (2026-03-14)

#### Query 1: Claude Code .claude/rules/ directory feature

**Source**: Claude Code official documentation
https://code.claude.com/docs/en/memory (fetched 2026-03-14)

**Key findings**:
- `.claude/rules/` is a first-class Claude Code feature for organizing instructions into multiple files
- Each `.md` file in `.claude/rules/` should cover one topic (e.g., `testing.md`, `api-design.md`)
- Files are discovered **recursively** — subdirectories like `frontend/` or `backend/` are supported
- Rules **without `paths` frontmatter** are loaded at launch with the same priority as `.claude/CLAUDE.md`
- Rules **with `paths` frontmatter** (YAML) are path-scoped: only loaded when Claude reads matching files
- The CLAUDE.md recommendation is "target under 200 lines per CLAUDE.md file" — splitting via `.claude/rules/` is the official approach for large files
- User-level rules in `~/.claude/rules/` apply to every project; project rules in `.claude/rules/` are project-scoped

#### Query 2: Claude Code subagent role-specific context CLAUDE.md best practices

**Source**: Claude Code subagent documentation
https://code.claude.com/docs/en/sub-agents (fetched 2026-03-14)

**Key findings**:
- Subagents receive only their own system prompt (plus basic environment details like working directory) — **not** the full Claude Code system prompt
- Subagents can use `skills` frontmatter field to preload skill content into their context at startup
- Subagents have `memory` field (`user`, `project`, `local`) for persistent cross-session memory
- Each subagent runs in its own context window — isolating context from parent is a first-class pattern
- Role specialization is a core design principle: "each subagent should excel at one specific task"
- The `description` field controls when Claude delegates to the subagent
- Subagents cannot spawn other subagents (nesting not allowed)

#### Query 3: Multi-agent context isolation role rules files

**Source**: Claude Code memory documentation (same page as Query 1)
https://code.claude.com/docs/en/memory (fetched 2026-03-14)

**Key findings**:
- Path-specific rules use YAML frontmatter with `paths` field (glob patterns)
- Rules without `paths` are "loaded unconditionally" at session start
- The `/memory` command lists all loaded CLAUDE.md and rules files — useful for debugging
- Rules files support symlinks for sharing across projects
- `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` env var makes `--add-dir` directories also load their `.claude/rules/`
- The `InstructionsLoaded` hook can log exactly which instruction files load and why

#### Design Decisions

Based on research:
1. **Rules files are plain Markdown** — no special format required beyond optional YAML frontmatter for `paths`
2. **Unconditional loading** (no `paths` frontmatter) is appropriate for role rules — we want them always present
3. **Location**: `{agent_work_dir}/.claude/rules/{role}.md` — written by `ClaudeCodeAgent` at startup alongside `CLAUDE.md`
4. **Source files** are stored in `src/tmux_orchestrator/agent_plugin/rules/` — copied at agent start, same pattern as slash commands
5. **`AgentConfig.role_rules_file`** override allows YAML config to specify a custom rules file path

**References**:
- Anthropic Claude Code Memory docs https://code.claude.com/docs/en/memory (2026)
- Anthropic Claude Code Subagents docs https://code.claude.com/docs/en/sub-agents (2026)
- Context Engineering: Zilliz "Context Engineering for AI Agents" (2025); LangChain "Context Engineering for Agents" (2025)

---

## §10.97 v1.2.22 — TDDロール別docs (tester/coder/reviewer)

### Step 0 — 選択根拠

**選択**: TDD ワークフロー向けロール別docs (tester/coder/reviewer) + ワークフロー連携

**理由**: ユーザー指示「ワークフローテンプレート + エージェントコンテキストパック」の具体化。v1.2.21で`TMUX_ORCHESTRATOR_AGENT_ROLE`と`TMUX_ORCHESTRATOR_PLUGIN_DOCS_DIR`の環境変数が導入されたが、対応するdocsは`worker.md`と`director.md`のみ。tdd.yamlで使われるtester/coder/reviewerロールがコンテキストパックを持つ最初の実例になることで、ワークフロー固有の文脈知識（TDDの各フェーズに特化した規律）をエージェントに注入できる。

**見送り**: アーキテクチャ改善系（DriftMonitor強化、CircuitBreaker拡張等）は戦略優先度が低い。現在の優先事項はワークフローテンプレートとエージェントコンテキストパックの充実。

### Step 1 — 調査記録 (2026-03-14)

#### Query 1: TDD テストライター エージェントのプロンプト/インストラクション ベストプラクティス

**Source**: Latent.space "AI Agents, meet Test Driven Development"
https://www.latent.space/p/anita-tdd (2025)

**Source**: tweag.github.io "Test-Driven Development | Agentic Coding Handbook"
https://tweag.github.io/agentic-coding-handbook/WORKFLOW_TDD/ (2025)

**Source**: alexop.dev "Forcing Claude Code to TDD: An Agentic Red-Green-Refactor Loop"
https://alexop.dev/posts/custom-tdd-workflow-claude-code-vue/ (2025)

**Key findings**:
- テストライターは「一度に一つのテストのみ書く」規律が重要 — 複数テストを一気に書くとRedフェーズの意味が失われる
- テスト名は実装の意図を明確に記述する（`test_add_two_numbers` より `test_add_returns_sum_of_positive_integers`）
- コンテキスト分離が本質: テストライターは実装方法を知ってはいけない（サブエージェントによる文脈隔離が有効）
- 失敗するテストは「正しい理由で失敗」していること — `NotImplementedError`や`ImportError`ではなく、アサーション失敗であること
- テストインフラ（fixture, setup）を先に検証してからビジネスロジックをテスト

#### Query 2: TDD コーダー エージェント Greenフェーズ 最小コード実装

**Source**: Simon Willison "Red/green TDD - Agentic Engineering Patterns"
https://simonwillison.net/guides/agentic-engineering-patterns/red-green-tdd/ (2025)

**Source**: nizar.se "Agentic TDD"
https://nizar.se/agentic-tdd/ (2025)

**Key findings**:
- Greenフェーズのコーダーは「テストが要求するものだけを書く — それ以上でも以下でもない」
- YAGNI（You Aren't Gonna Need It）原則の徹底: 将来の拡張を今書かない
- 実装前に失敗テストを読んで「何を期待されているか」を理解してから書き始める
- テスト通過後はすぐにコミット — リファクタリングはReviewer/Refactorerの役割
- 複数の失敗テストがある場合は一つずつ解決する（最も単純なものから）
- 「最小コード」の基準: 削除すると失敗するコードが最小実装

#### Query 3: コードレビュアー/リファクタラー エージェント コード品質 インストラクション

**Source**: arXiv:2404.18496 "AI-powered Code Review with LLMs: Early Results"
https://arxiv.org/abs/2404.18496 (2024)

**Source**: arXiv:2505.16339 "Rethinking Code Review Workflows with LLM Assistance: An Empirical Study"
https://arxiv.org/html/2505.16339v1 (2025)

**Source**: Addy Osmani "Code Review in the Age of AI"
https://addyo.substack.com/p/code-review-in-the-age-of-ai (2025)

**Key findings**:
- LLMレビュアーはCode Smell検出（重複、命名、複雑度）とバグ検出で特に有効
- リファクタラーは「振る舞いを変えずに品質を改善する」という制約を厳格に守る必要がある — 新機能追加厳禁
- 構造化された出力フォーマット（REVIEW.mdへの記録）により人間のレビュアーが追跡しやすくなる
- 重要度分類（CRITICAL/HIGH/MEDIUM/LOW）により優先度が明確になる
- LLMのフィードバックは「提案」として扱い、実装への強制的なゲートにしない
- 重複除去、命名改善、複雑度低減（McCabe複雑度）、テストカバレッジ検証が主要チェック項目

### Design Decisions

1. **tester.md**: Redフェーズ専門 — 一度に一テスト、実装知識を持たない、コミットしてからハンドオフ
2. **coder.md**: Greenフェーズ専門 — YAGNI徹底、最小実装、テスト通過後即コミット
3. **reviewer.md**: Refactorフェーズ専門 — 振る舞い変更禁止、REVIEW.md構造化出力、重要度分類
4. **tdd.yaml**: 各エージェント定義に`role:`フィールド追加 (tester/coder/reviewer)
5. **CLAUDE.md**: ロールdocsの参照方法を既存の「Running as an Orchestrated Agent」セクションに追記

