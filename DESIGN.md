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

### API キー配送のセキュリティ方針 (調査: 2026-03-05)

#### 問題

v0.34.0 で `OrchestratorConfig.api_key` が導入され、スラッシュコマンド (`/send-message`、`/spawn-subagent`、`/list-agents`、`/plan`、`/tdd` など) が FastAPI サーバーへの認証済み REST 呼び出しを行えるようになった。
しかし現在の実装では **API キーをエージェントのワークツリー内の `__orchestrator_context__.json` にプレーンテキストで書き込む** (`base.py:_write_context_file` → `claude_code.py:_context_extras`)。

これには以下の具体的なリスクがある:

1. **ファイルシステム上の平文**: `ls -la` や `cat` で誰でも読める (ファイルパーミッションは `0o644`)。
2. **git 誤コミットのリスク**: ワークツリー (`{repo}/.worktrees/{agent_id}/`) に書かれたファイルが `git add -A` で誤ってステージされる可能性がある。メインリポジトリの `.gitignore` は `.worktrees/` ディレクトリごと除外しているが、**ワークツリー内の `.git` とは別の独立したワークツリーであるため、そのワークツリー内に `.gitignore` が存在しない限り保護されない**。
3. **エージェント分離の逆効果**: 分離エージェント (isolated worktree) が API キー付きファイルを持つことで、ワークツリーが侵害された場合にキーが露出する。
4. **`isolate=false` の場合のリスク最大化**: メインのリポジトリ作業ディレクトリに直接 `__orchestrator_context__.json` が書かれる。ここでも `.gitignore` が存在する (`__orchestrator_context__.json` はルートの `.gitignore` に追加済み) が、確認漏れでの誤コミットリスクは残る。

**現在の緩和策**: メインリポジトリの `.gitignore` に `__orchestrator_context__.json` を追加済み。これは必要だが十分ではない — `.gitignore` が機能するのはそのファイルがまだ git track されていない場合のみであり、pre-commit hook や CI/CD スキャンがない状況では「最後の砦」として脆弱 ([GitGuardian "Protecting Developers Secrets"](https://blog.gitguardian.com/protecting-developers-secrets/), [DEV Community "Git Security Best Practices"](https://dev.to/prankurpandeyy/git-security-best-practices-for-keeping-your-code-safe-1nep))。

---

#### 評価した選択肢

| 方法 | セキュリティリスク | メリット | TmuxAgentOrchestrator への適合性 |
|------|------------------|---------|--------------------------------|
| **A. 現状維持 (JSON ファイル平文)** | 高 — ファイルシステム露出、git 誤コミット | 実装済み、スラッシュコマンドとの互換性が高い | 不可 (v0.34.0 のバグとして扱うべき) |
| **B. `export` via `send_keys`** | 高 — シェル履歴 (`~/.bash_history`) に記録される。`ps aux` / `/proc/{pid}/environ` でプロセス実行中は読める ([Sandfly Security "Linux Process Env Vars"](https://sandflysecurity.com/blog/using-linux-process-environment-variables-for-live-forensics/)) | JSON ファイルより揮発性が高い | 不可 — シェル履歴リスクが現状より悪い |
| **C. libtmux `set_environment` (セッション環境変数)** | 中 — `tmux show-environment` で tmux セッションに接続できるユーザーは読める。シェル履歴には残らない。後から生成されたウィンドウ/ペインに継承される ([libtmux docs](https://libtmux.readthedocs.io/en/latest/api.html), [GitHub tmux Discussion #3997](https://github.com/orgs/tmux/discussions/3997)) | send_keys 不使用。JSON ファイル不要。tmux セッション内に閉じた露出範囲 | 有望 (補助的保護として有効) |
| **D. 制限付きパーミッションファイル (`chmod 600`)** | 低〜中 — 同一ユーザーアカウントからは読める。root は常に読める。git 誤コミットのリスクは `.gitignore` + 拡張子 `.secret` による二重保護で軽減可能 ([Kubernetes Secrets Best Practices](https://kubernetes.io/docs/concepts/security/secrets-good-practices/)) | 実装が単純。スラッシュコマンドとの変更コストが最小 | 最も実用的な短期解決策 |
| **E. Unix ドメインソケット** | 非常に低 — ソケットファイルのパーミッションで接続制限可能。ネットワーク不使用でスニッフィング不可 ([O'Reilly "Secure Programming Cookbook" §9.8](https://www.oreilly.com/library/view/secure-programming-cookbook/0596003943/ch09s08.html)) | 認証情報が不要になる (ソケット自体が認証チャンネル) | 中期的に理想的。REST API のソケット版が必要 |
| **F. 短命トークン (セッション限定 JWT)** | 低 — トークン有効期限でリスクウィンドウを限定。漏れても自動失効 ([HashiCorp Vault dynamic secrets](https://developer.hashicorp.com/validated-patterns/vault/ai-agent-identity-with-hashicorp-vault), [Stytch "AI agent authentication methods"](https://stytch.com/blog/ai-agent-authentication-methods/)) | セキュリティと運用性のバランスが良い | 中期的に有力。オーケストレーター起動時にトークン生成が必要 |

**参考: Kubernetes / Docker の知見**

Kubernetes のベストプラクティス ([kubernetes.io/docs/concepts/security/secrets-good-practices/](https://kubernetes.io/docs/concepts/security/secrets-good-practices/)) および Docker の推奨 ([Docker Docs: Secrets](https://docs.docker.com/engine/swarm/secrets/)) はいずれも「シークレットは環境変数よりファイルマウントが望ましく、ファイルマウントする場合は tmpfs (メモリ) 上に置き、パーミッションを制限せよ」と結論づけている。

---

#### 推奨方針

**フェーズ 1 (短期、即実装可能): `chmod 600` + ファイル名変更**

`__orchestrator_context__.json` から API キーを分離し、`__orchestrator_api_key__` という独立した制限付きファイルに書き出す。

```
{worktree}/
├── __orchestrator_context__.json   # 非機密情報のみ (agent_id, web_base_url 等)
└── __orchestrator_api_key__        # API キーのみ、chmod 600
```

- `_write_context_file()` を変更: `api_key` を `__orchestrator_context__.json` から除外
- API キー専用ファイルを `os.open(..., 0o600)` で作成 (atomic, パーミッション指定付き)
- `.gitignore` に `__orchestrator_api_key__` を追加 (ワークツリー内にも `.gitignore` をコピー)
- スラッシュコマンドは `__orchestrator_api_key__` からキーを読む (別ファイル読み込みに変更)

**利点**: スラッシュコマンドへの変更コストが最小。ファイルシステム上での露出は同様だが、`chmod 600` + 非機密コンテキストとの分離により git 誤コミットリスクが大幅に低下。

**フェーズ 2 (中期): libtmux `set_environment` によるセッション環境変数注入**

`ClaudeCodeAgent.start()` において、ファイルへの書き込みの代わりに libtmux の `session.set_environment("TMUX_ORCHESTRATOR_API_KEY", api_key)` を使用する。pane は tmux セッションの後から生成されるため環境変数を継承する ([libtmux GitHub Discussion](https://github.com/orgs/tmux/discussions/3997))。スラッシュコマンドは `os.environ.get("TMUX_ORCHESTRATOR_API_KEY")` で取得する。

**利点**: ファイルシステムにキーが書き込まれない。シェル履歴にも残らない (`send_keys` を使わないため)。tmux セッション内に露出が限定される。

**フェーズ 3 (長期): 短命セッショントークン**

オーケストレーター起動時にセッションスコープの JWT (有効期限 = セッション終了まで) を生成し、`/tmp/{session_name}.token` に `chmod 600` で書き込む。エージェントはこのトークンを起動時に一度読み取り、メモリ上に保持する。セッション終了時にオーケストレーターがトークンを失効させ、ファイルを削除する。

**利点**: セッション終了後にトークンが自動失効。ファイルが `/tmp` (tmpfs) 上にあれば再起動後にも消える。

---

#### セキュリティトレードオフ要約

- **`.gitignore` のみ**: 必要だが不十分。すでに track されたファイルや、worktree 内の独立した git 状態に対しては機能しない。
- **環境変数 (`export` via `send_keys`)**: シェル履歴リスクで現状より悪化する可能性がある — **採用しない**。
- **`chmod 600` ファイル**: 同一ユーザー内での保護は提供しない (root や同一 UID のプロセスは読める) が、git 誤コミット防止と他ユーザーからの保護として有効。実装コストが最小で現実的。
- **libtmux `set_environment`**: tmux セッション境界が保護境界になる。システム管理者は `tmux show-environment` で依然として読めるが、ファイルシステム上の平文よりは制限されている。
- **Unix ドメインソケット**: 最も強力だが、REST API 全体の再設計が必要で実装コストが高い。

**最終推奨**: フェーズ 1 (キー分離 + `chmod 600`) を即座に実装し、フェーズ 2 (libtmux `set_environment`) を次のマイナーバージョンで実装する。フェーズ 3 はセキュリティ要件が高まった時点で検討する。

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

## 11. 今後の課題

> 以下のバックログは、完了済み項目（旧 §11 テーブルの全 ~~完了~~ エントリ、§10.N 実装履歴）を除去し、
> 4 リサーチエージェントの調査結果（multi-LLM orchestration / clean arch / 形式手法 /
> コンテキストエンジニアリング / 討論・合意形成）を重複排除・統合した単一バックログである。
>
> 優先度基準:
> - **高** — 直接ユーザー向け新機能 + 強い研究的裏付け + 明確な実装パス + 既存機能を活用
> - **中** — 有用だがさらなる設計が必要、または高優先度アイテムに依存
> - **低** — あると良い、複雑、または価値が不確実

---

### 機能・ワークフロー（優先度順）

| 優先度 | 課題 | 根拠 |
|--------|------|------|
| ~~高~~ | ~~**`POST /workflows/tdd`**~~ | ~~完了 v0.36.0~~ |
| ~~高~~ | ~~**`POST /workflows/debate`**~~ | ~~完了 v0.37.0 — advocate/critic/judge 3役割, 1–3ラウンド, judge→DECISION.md. ALL 27 CHECKS PASSED. デモ: SQLite vs PostgreSQL, Advocate(PG)勝利.~~ |
| 高 | **役割別 system_prompt テンプレートライブラリ + `system_prompt_file:` YAML フィールド** — `.claude/prompts/roles/` に tester / implementer / reviewer / spec-writer / judge / advocate / critic の7種類のプロンプトファイルを提供し、`AgentConfig.system_prompt_file:` フィールドで参照できるようにする。各ファイルに「役割・禁止事項・完了条件・/plan /tdd の使い方・迎合抑制指示」を標準化して記述する | Vellum "Best practices for building multi-agent systems" (2025): 役割特化プロンプトとステート分離が精度向上に最も効果的。ChatEval ICLR 2024 (arXiv:2308.07201): 役割の多様性が討論品質を決定する最重要因子。同一ロール複数エージェントは性能低下を招く。CONSENSAGENT ACL 2025: 迎合抑制プロンプトが正答率・効率の両方を改善。v0.37.0 で advocate/critic/judge の 3 テンプレート (`.claude/prompts/roles/`) を追加。残りは tester / implementer / reviewer / spec-writer。 |
| 高 | **`POST /workflows/adr` — Architecture Decision Record (ADR) 自動生成ワークフロー** — `proposer`（案提示）→ `reviewer`（技術的批評）→ `synthesizer`（ADR 文書化）の3エージェントが MADR フォーマット (title / status / context / decision / consequences) の `DECISION.md` を生成するテンプレートを追加。`context_files` に既存 ADR を渡すことで過去の決定との整合性を保つ | MAD in Requirements Engineering arXiv:2507.05981 (2025): MAD が要件分類 F1 を 0.726→0.841 に向上。SocraSynth arXiv:2402.06634: モデレーター + 対立エージェント + ジャッジ構成が設計討論に直接適用可能。`debate` ワークフローと共通基盤で実装できる。 |
| 高 | **Codified Context インフラ** — プロジェクト規約・禁止事項を機械可読な YAML/JSON 仕様ファイルとして `.claude/specs/` に配置し、`AgentConfig.context_spec_files` (glob パターン) でタスク開始時にワークツリーへ自動コピーする。エージェントがセッションをまたいでも規約を忘れない基盤を実現する | Vasilopoulos arXiv:2602.20478 "Codified Context" (2026-02): 108,000行 C# 分散システムで 283 セッションにわたり規約を維持。Hot-memory constitution + Cold-memory spec documents の2層構造が有効。既存の `context_files` (v0.11.0) 機構の自然な拡張。 |
| 高 | **チェックポイント永続化による中断再開** — `ResultStore` (v0.24.0 JSONL) を拡張し、タスク進行状態・ワークフロー状態スナップショットを SQLite に保存。`tmux-orchestrator web --resume` フラグでプロセス再起動後に最後のチェックポイントから継続できるようにする | LangGraph は checkpointer + PostgresSaver で fault-tolerant persistence を提供 (LangChain docs 2025)。現状はプロセス再起動でキュー・ワークフロー状態が消滅する。ResultStore (v0.24.0) が JSONL 追記基盤として既に存在しており、SQLite 拡張は自然な次ステップ。 |
| 高 | **Claude Code `Stop` フックによる完了検出の置き換え** — エージェント起動時に `worktree/.claude/settings.local.json` に `Stop` フックを書き込み、Claude が応答を完了した瞬間に `POST /agents/{agent_id}/task-complete` を HTTP 呼び出しさせる。現行の 500ms ポーリング + regex パターンマッチを廃止し、決定論的な完了通知に置き換える。`SessionEnd` フックも組み合わせてエージェントライフサイクルの終了を検出する | Claude Code 公式ドキュメント "Hooks" (2025): `Stop` フックはターン完了時に必ず1回発火し、`type: "http"` で REST エンドポイントへ直接 POST 可能。現行の regex (`❯\s*$` 等) は CLI バージョンアップで壊れやすく、500ms ポーリングは CPU/I/O を継続消費する。v0.36.0 デモで 300 秒タイムアウトが発生した根本原因でもある。 |
| 高 | **`ProcessPort` 抽象インターフェースの抽出** — `ClaudeCodeAgent` が直接 `libtmux.Pane` に依存している箇所を `ProcessPort` protocol に置き換え、`TmuxProcessAdapter` と `StdioProcessAdapter`（テスト用）を実装する。`ClaudeCodeAgent` がこのポートのみに依存するよう依存方向を逆転させる | Martin "Clean Architecture" (2017) Ch.22 のポート&アダプターパターン。現状 `agents/claude_code.py` が `libtmux` を直接 import しており、tmux なし環境でのユニットテストが不可能。Naoyuki Sakai "AI Agent Architecture: Mapping Domain, Agent, and Orchestration to Clean Architecture" (2025) が同パターンを AI エージェントに適用。 |
| 中 | **`POST /workflows/clean-arch` — 4レイヤー分解ワークフロー** — Director が機能要求を domain / use-case / adapter / framework の4レイヤーに分解し、各レイヤーを専用ワーカーに割り当てるテンプレートを追加。`.claude/prompts/roles/` に `domain-agent.md` / `usecase-agent.md` / `adapter-agent.md` / `framework-agent.md` を提供し、PLAN.md 経由でハンドオフする | AgentMesh arXiv:2507.19902 (2025): Planner→Coder→Debugger→Reviewer の4ロール分担がソフトウェア開発タスクを自動化。Muthu (2025-11) "The Architecture is the Prompt": 「ヘキサゴナルアーキテクチャの境界がそのまま AI エージェントのコンテキスト制約になる」。`POST /workflows/tdd` および役割テンプレートライブラリ完成後に実装。 |
| 中 | **`POST /workflows/pair` — PairCoder (Navigator + Driver) ワークフロー** — Navigator（計画・高レベル戦略）と Driver（実装・テスト・デバッグ）の2エージェントがペアプログラミングを模倣する DAG を追加。Navigator が PLAN.md を生成し Driver に `reply_to` で送信、Driver が実装して結果を返すサイクルを `depends_on` チェーンで表現する | FlowHunt "TDD with AI Agents" (2025): 「PairCoder が単一エージェント比でコード品質が向上」。Tweag "Agentic Coding Handbook - TDD" (2025): コンテキスト分離によるペアプログラミング相当のアプローチを推奨。既存の `reply_to` (v0.14.0) + `depends_on` (v0.29.0) で実現可能。 |
| 中 | **`POST /workflows/delphi` — 複数ラウンド合意形成ワークフロー** — 3–5 名の専門家エージェント（セキュリティ / パフォーマンス / 保守性 / UX / コスト 各ペルソナ）が匿名で意見を提出し、モデレーターエージェントが集計して次ラウンドにフィードバックする Delphi サイクルを最大 3 ラウンド実行する DAG を追加。各ラウンドの出力を `delphi_round_{n}.md` に保存し、最終合意を `consensus.md` に書き出す | RT-AID "Real-Time AI Delphi" ScienceDirect 2025 (arXiv:2502.21092) が LLM によるデルファイ法の自動化を提案。Du et al. ICML 2024: 「エージェントが全員誤りでも討論で正解に収束するケースが多数存在する」→ 複数ラウンドの価値を実証。 |
| 中 | **`POST /workflows/redblue` — Red Team / Blue Team 対抗評価ワークフロー** — `blue-team`（実装・設計案を作成）・`red-team`（攻撃者視点で脆弱性・欠陥を列挙）・`arbiter`（リスク評価レポートを生成）の3エージェント構成。セキュリティレビュー・ビジネスケース検証・アーキテクチャ変更の影響分析に利用できる汎用的な対抗評価テンプレートを提供する | Adversarial Multi-Agent Evaluation arXiv:2410.04663: 反復討論型評価がバイアス削減と判断精度向上に有効。Red-Teaming LLM MAS ACL 2025 (arXiv:2502.14847): エージェント間通信への攻撃評価手法を提案。`debate` ワークフローの特殊化として実装できる。 |
| 中 | **`POST /workflows/socratic` — ソクラテス的対話ワークフロー** — `questioner`（前提・定義・論拠を問うマイウティカ法）・`responder`（回答を精緻化）・`synthesizer`（対話ログから構造化結論を抽出）の3エージェント構成。設計仕様の曖昧性解消・要件の深掘り・技術負債の根本原因分析に適する。最初のラウンドは強い反論、後のラウンドは統合的問いに変化させる段階的移行モデルを採用する | SocraSynth arXiv:2402.06634: モデレーター型ソクラテス的マルチエージェント討論プラットフォーム。KELE EMNLP 2025 (arXiv:2409.05511): LLM ベースのソクラテス教授エージェント実証。`debate` / `adr` ワークフローと共通基盤で実装できる。 |
| 中 | **`/deliberate <question>` スラッシュコマンド** — 単一の親エージェントが `/deliberate "REST vs GraphQL"` と入力すると、2 つのサブエージェント（advocate / critic）を自動スポーンし、2 ラウンドの討論後に `DELIBERATION.md` に結論を書き出して親に `deliberation_complete` STATUS を送信するコマンドを `.claude/commands/deliberate.md` として提供する | DEBATE ACL 2024 (arXiv:2405.09935): Devil's Advocate が単一 LLM 判断のバイアスを解消。CONSENSAGENT ACL 2025: 迎合抑制プロンプトで効率的な合意形成を実証。既存の `/spawn-subagent` + `reply_to` + Workflow DAG の組み合わせで実現可能。`debate` ワークフロー完成後に実装するのが自然。 |
| 中 | **`POST /workflows/ddd` — DDD Bounded Context 分解ワークフロー** — Director が機能要求をドメインイベント・集約・コマンドに分解した EventStorming マップを PLAN.md に書き出し、境界コンテキストごとに専用ワーカー（ubiquitous language 定義を `context_files` として受け取る）に実装を委任するテンプレートを追加する | Russ Miles "Domain-Driven Agent Design" (Engineering Agents 2025): DDD の Bounded Context がマルチエージェントの責務分割境界に直接対応する。Bakthavachalu (2025): 大手投資銀行の3 Bounded Context 実装事例（Risk / Regulatory / Validation）。`clean-arch` ワークフローの代替として提供。 |
| 中 | **形式仕様エージェントステップ + `/spec` スラッシュコマンド** — `POST /workflows` の DAG に `type: spec` タスクを追加可能にし、spec-writer エージェントが Alloy または TLA+ の軽量仕様を `SPEC.md` に書いてから impl エージェントがコードを書く「仕様→実装」ハンドオフパターンを公式サポートする。同時に `.claude/commands/spec.md` として `/spec <invariant description>` コマンドを提供し、`/plan` → `/spec` → `/tdd` フローを公式化する | Hou et al. "Position: Trustworthy AI Agents Require Formal Methods" (2025): TLA+/Hoare 表明の LLM エージェントへの統合を提言。SYSMOBENCH arXiv:2509.23130 (2025): LLM の TLA+ 仕様生成能力を 200 システムモデルで評価。Benjamin Congdon "The Coming Need for Formal Specification" (2025-12): 「AI 生成コードが増えるほど仕様が重要になる」。`spec-writer.md` ロールプロンプトはロールテンプレートライブラリと共同提供。 |
| 中 | **コンテキスト4戦略ガイドを CLAUDE.md と `.claude/prompts/` に体系化** — 「書き込み (NOTES.md/PLAN.md)・選択 (context_files)・圧縮 (/summarize)・分離 (worktree + 別コンテキスト)」の4戦略を役割ごとに組み合わせたベストプラクティスチートシートを提供し、CLAUDE.md の "Running as an Orchestrated Agent" セクションに追記する | Algomatic Tech Blog "AIエージェントを支える技術: コンテキストエンジニアリングの現在地" (2025-10): 書き込み・選択・圧縮・分離の4戦略フレームワーク。Anthropic "Effective Context Engineering for AI Agents" (2025-09-29): 「コンテキストエンジニアリングとはプロンプト設計を超えた、推論時の情報エコシステム全体の管理」。実装コストが低くユーザー価値が高い。 |
| 低 | **LLM-as-Judge による並列エージェント出力の自動スコアリング (BestOfN + EDDOps)** — `POST /tasks/batch` で投入した Best-of-N タスクの RESULT を自動収集し、judge エージェントが正確性・保守性・セキュリティの3軸スコアを採点して最優秀を選択・通知する `BestOfNEvaluator` を実装する。スコアが閾値未満なら自動的に再試行するループ（EDDOps）もオプションで提供する | arXiv:2411.13768 "Evaluation-Driven Development and Operations of LLM Agents" (2025): EDDOps プロセスモデルと参照アーキテクチャを提案。Cemri et al. arXiv:2503.13657: 検証ギャップ (21.30%) を主要失敗要因として特定。既存 v0.15.0 AHC デモがユーザー手動スコア確認のため自動化の余地が大きい。`debate` / `judge` ロールテンプレート完成後に実装。 |
| 低 | **ワークフローテンプレートライブラリ (`examples/workflows/`)** — TDD / PairCoder / CleanArch / DDD / SpecFirst / Debate / ADR の各ワークフローを `POST /workflows` で直接投入できる自己完結 YAML として `examples/workflows/` に収録する。各 YAML はエージェント数・ロール・`system_prompt_file` 参照・`context_files`・`required_tags` を含む。`examples/debate_config.yaml`（異種エージェント討論グループ設定）を含む | CrewAI の YAML-driven workflow approach (2025) が「ドキュメントとしての設定ファイル」を普及させた。各ワークフローエンドポイント実装後に対応 YAML を追加していく継続的タスク。A-HMAD Springer 2025 + ChatEval ICLR 2024: 異種構成エージェントと役割固定化が討論品質を決定する最重要因子。 |
| 低 | **`DECISION.md` 標準フォーマットとスクラッチパッド書き込み規約** — `debate` / `adr` / `delphi` / `redblue` / `socratic` の各ワークフローが出力を書き込む共通フォーマット (title / status / context / options_considered / decision / rationale / dissenting_opinions / consequences / references) を定義し、`GET /scratchpad/DECISION` で取得できるようにする | SocraSynth arXiv:2402.06634: 討論から「構造化された結論」を抽出する段階が必須。RT-AID ScienceDirect 2025: 各ラウンドの中間出力が最終合意文書の品質を高める。既存の `GET/PUT /scratchpad/{key}` (v0.16.0) + `context_files` (v0.11.0) を組み合わせて実現可能。各ワークフロー完成後に規約を策定する。 |
| 低 | **構造化デバッグ: トレースリプレイ CLI (`tmux-orchestrator replay`)** — `ResultStore` の JSONL + bus イベントログを組み合わせて過去の実行シーケンスを再現し、どのステップで失敗したかを特定できる CLI コマンドを追加する | LangGraph + LangSmith の replay 機能は業界標準デバッグ手法として認識 (LangChain docs 2025)。Galileo AI "Why Multi-Agent LLM Systems Fail" (2025): 「非決定的挙動のリプレイ不可」を主要問題として挙げる。チェックポイント永続化実装後の自然な拡張。 |

---

### アーキテクチャ・品質

| 優先度 | 課題 | 根拠 |
|--------|------|------|
| 高 | **OpenTelemetry GenAI Semantic Conventions 準拠トレース出力** — `gen_ai.*` 属性 (token counts, tool calls, agent spans) を既存 `trace_id` ベースの構造化ログに付加し、Datadog/Jaeger/OTLP エクスポーターへ送信できるようにする | OpenTelemetry "AI Agent Observability" (2025) が業界標準に収斂しつつあり、Datadog が GenAI Semantic Conventions にネイティブ対応済み。現状の `trace_id` は相関のみでスパン階層がない。[opentelemetry.io/blog/2025/ai-agent-observability](https://opentelemetry.io/blog/2025/ai-agent-observability/) |
| 高 | **エージェントドリフト検出 (Agent Stability Index)** — 同一エージェントの連続出力に対し role adherence スコアを算出し、閾値を下回ったら `agent_drift_warning` STATUS イベントを発行する `DriftMonitor` を実装する | arXiv:2601.04170 "Agent Drift: Quantifying Behavioral Degradation" (2025) が12次元の Agent Stability Index (ASI) を提案。現行の `ContextMonitor` はトークン量のみ監視しており、役割逸脱・タスク重複を検出できない。ロールテンプレートライブラリ実装後に効果が発揮される。 |
| 中 | **`UseCaseInteractor` 層の抽出** — `web/app.py` の FastAPI ハンドラーが `orchestrator.*` メソッドを直接呼ぶ箇所を `SubmitTaskUseCase`, `CancelTaskUseCase` 等の Use Case クラスに置き換え、Web 層とドメイン層の依存方向を逆転させる | Martin "Clean Architecture" §22: Use Case Interactor がアプリケーション固有ビジネスルールを保持し、Web/CLI/TUI のどのインターフェースからも同一ロジックを呼べる。現状は FastAPI ハンドラーにロジックが漏れており、TUI から同機能を使うときに重複する。`ProcessPort` 抽出後に実施するのが自然な順序。 |
| 中 | **MIRIX 型エピソード記憶ストア** — 各エージェントの `NOTES.md` に加えて、タスク完了ごとに `{task_id, summary, outcome, lessons}` を軽量 JSONL エピソードログとして蓄積し、次タスク開始時に直近N件を system prompt に付加するエピソード記憶を実装する | arXiv:2507.07957 "MIRIX: Multi-Agent Memory System" (2025) が RAG ベースラインより 35% 精度向上を達成。現行の `/summarize` → `NOTES.md` は単一ファイルに上書きされ過去エピソードが失われる。チェックポイント永続化の SQLite 基盤と共通化できる。 |
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
