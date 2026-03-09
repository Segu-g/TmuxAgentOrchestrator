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
10. [調査記録](docs/research-log.md) — 各イテレーション調査記録（別ファイル）
11. [今後の課題](#11-今後の課題)
12. [ワークフロー設計の層構造（概念モデル）](#12-ワークフロー設計の層構造概念モデル)

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


## 12. ワークフロー設計の層構造（概念モデル）

> このセクションはユーザーとの議論（2026-03-06）から生まれた設計思想の整理です。
> 現在の実装はこのモデルの下位層（層4・5）から段階的に構築されており、
> 上位層（層1・2）は今後の実装目標です。

---

### 設計思想

フレームワークの機能は「**ワークフロー本体**」と「**ツール・マネジメント**」に明確に分離できる。
ワークフロー本体はさらに「フェーズ設計」「フェーズ管理」「実行方式」「コンテキスト伝達」の4層に分解される。

---

### 5層モデル

#### 層1：ワークフロー設計（フェーズを決める）

「何フェーズで何をするか」を決める。

- **宣言的モード**: ユーザーが YAML/JSON で事前に指定する
- **自律モード**: Planner ロールのエージェントが自律的にフェーズを設計する

エージェントが設計する場合、タスクの複雑さ・依存関係・リスクを評価して
フェーズ構成を動的に決定する。これは現行の Director パターンの発展形。

#### 層2：フェーズ管理

フェーズを一級市民として追跡・管理する仕組み。

- フェーズの生成・遷移（進行中→完了→次フェーズ解放）・失敗のハンドリング
- **現状の実装**: `depends_on` + Task の組み合わせでフェーズを暗黙的に表現している。
  明示的な `Phase` 概念は存在しない。これが今後の中心的な抽象化課題。

#### 層3：ステージの実行方式

各フェーズを**どう動かすか**。ユーザーが指定しない場合はエージェントが自律判断する。

| 方式 | 説明 | 現在の実装 |
|------|------|-----------|
| `single` | 単一エージェントが担当 | デフォルト |
| `parallel` | 複数エージェントが同一フェーズを並列実行 | `target_group` + agent groups |
| `competitive` | N エージェントが同問題に取り組み、最良解を選択 | Best-of-N（手動評価） |
| `debate` | Advocate/Critic/Judge 構成で合議 | `POST /workflows/debate` |
| `delphi` | 複数専門家ペルソナが多ラウンド合意形成 | 予定 |

**自律切り替え**: 担当エージェントがフェーズの複雑さを判断し、
単独では困難と判断した場合に実行方式を変更できる仕組みが必要（将来実装）。

#### 層4：コンテキスト伝達

フェーズ間で成果物・情報をどう受け渡すか。

| 手段 | 用途 |
|------|------|
| Scratchpad (Blackboard) | フェーズ成果物の非同期共有（主要手段） |
| `context_files` | 静的ファイルのワークツリーへのコピー |
| `context_spec_files` | glob パターンで仕様書を自動配布 |
| `system_prompt_file` | ロールテンプレートを CLAUDE.md に注入 |
| `reply_to` | タスク完了時に結果を別エージェントへ直接配送 |

#### 層5：ツール・マネジメント（直交する別概念）

ワークフロー定義とは独立して動作する横断的機能。

| カテゴリ | 機能 |
|----------|------|
| **通信** | P2P メッセージング、mailbox、Bus pub/sub |
| **ライフサイクル** | retry、TTL、watchdog、circuit breaker、drain、idempotency |
| **観測** | ContextMonitor、audit log、worktree integrity、agent stats |
| **セキュリティ** | rate limit、prompt sanitization、API key 認証、CORS |

---

### 2つの制御モード

```
宣言的モード（ユーザー指定）
  POST /workflows
  {
    "phases": [
      { "name": "design",     "pattern": "debate"     },
      { "name": "implement",  "pattern": "parallel:3" },
      { "name": "review",     "pattern": "single"     }
    ]
  }

自律モード（エージェント自律）
  → Planner エージェントがフェーズを設計
  → 各フェーズ担当エージェントが実行方式を自律判断
  → 未指定フィールドのデフォルト: single
```

---

### 現状と今後の目標

| 層 | 現状 | 今後 |
|----|------|------|
| 層1 ワークフロー設計 | ハードコードされたテンプレート群 | Planner エージェントによる自律設計・汎用宣言API |
| 層2 フェーズ管理 | `depends_on` + Task で暗黙表現 | Phase 一級市民 + フェーズ状態 API |
| 層3 実行方式 | テンプレートに固定（/workflows/debate 等） | フェーズごとに動的指定・エージェント自律変更 |
| 層4 コンテキスト伝達 | Scratchpad + context_files 等、充実 | 維持・改善 |
| 層5 ツール・マネジメント | セキュリティ・観測・通信、充実 | 維持・改善 |

---


## 11. 今後の課題

> バックログは §12「ワークフロー設計の層構造」の5層モデルに基づいて再整理されている。
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
| 中 | **コンテキスト4戦略ガイド** — 書き込み・選択・圧縮・分離の4戦略チートシートを CLAUDE.md に追記 | 層4 |
| 中 | **MIRIX 型エピソード記憶ストア** — タスク完了ごとに `{task_id, summary, outcome, lessons}` を JSONL に記録し、次タスク開始時に直近N件を system prompt に付加 | 層4 |
| 中 | **スライディングウィンドウ + 重要度スコアによるコンテキスト圧縮** — TF-IDF 類似度でスコア下位 40% を削除する `/summarize` の上位互換 | 層4 |

---

### 層5：ツール・マネジメント（アーキテクチャ品質）

| 優先度 | 課題 | 層 |
|--------|------|----|
| **高** | **チェックポイント永続化による中断再開** — SQLite による状態永続化、`--resume` フラグ（v0.45.0 実装中） | 層5 |
| **高** | **`ProcessPort` 抽象インターフェース抽出** — `ClaudeCodeAgent` の libtmux 直接依存を排除（v0.46.0 予定） | 層5 |
| **高** | **OpenTelemetry GenAI Semantic Conventions** — `gen_ai.*` 属性 + OTLP エクスポーター（v0.47.0 予定） | 層5 |
| ~~**高**~~ | ~~**エージェントドリフト検出 (Agent Stability Index)**~~ | ~~完了 v1.0.9 — `DriftMonitor` (role/idle/length 3サブスコア)、`agent_drift_warning` イベント、`GET /drift`・`GET /agents/{id}/drift` エンドポイント。34テスト。17/17デモPASS。~~ |
| ~~中~~ | ~~**`UseCaseInteractor` 層の抽出** — FastAPI ハンドラーから業務ロジックを分離~~ | ~~完了 v1.1.14 (SubmitTaskUseCase / CancelTaskUseCase) + v1.1.15 (ListAgentsUseCase / GetAgentUseCase wiring)~~ |
| 中 | **エージェント状態機械の Hypothesis ステートフルテスト** — `AgentStatus` 遷移シーケンスの自動生成テスト | 層5 |
| 低 | **構造化デバッグ: トレースリプレイ CLI** — `ResultStore` JSONL から過去実行を再現 | 層5 |

---

### ワークフローテンプレート・ドキュメント整備

| 優先度 | 課題 |
|--------|------|
| 中 | **`examples/workflows/` YAML テンプレートライブラリ** — 各ワークフローを自己完結 YAML として収録 |
| 中 | **`POST /workflows/clean-arch`** — 4レイヤー分解ワークフロー（domain/usecase/adapter/framework） |
| 中 | **`POST /workflows/pair`** — Navigator + Driver ペアプログラミング |
| 中 | ~~**`POST /workflows/socratic`**~~ — questioner + responder + synthesizer ソクラテス的対話 (**v1.0.25完了**) |
| 低 | **`DECISION.md` 標準フォーマット** — 全ワークフロー共通の出力フォーマット策定 |

---

### 機能・ワークフロー（優先度順）

| 優先度 | 課題 | 根拠 |
|--------|------|------|
| ~~高~~ | ~~**`POST /workflows/tdd`**~~ | ~~完了 v0.36.0~~ |
| ~~高~~ | ~~**`POST /workflows/debate`**~~ | ~~完了 v0.37.0 — advocate/critic/judge 3役割, 1–3ラウンド, judge→DECISION.md. ALL 27 CHECKS PASSED. デモ: SQLite vs PostgreSQL, Advocate(PG)勝利.~~ |
| **高** | **スラッシュコマンド群をエージェントワークツリーで使用可能にする** — エージェント起動時に TmuxAgentOrchestrator の `.claude/commands/*.md` をワークツリーの `.claude/commands/` へ自動コピーする。これにより `/send-message`, `/check-inbox`, `/read-message`, `/spawn-subagent`, `/list-agents`, `/progress`, `/summarize`, `/delegate` 等の全スラッシュコマンドをエージェントが利用できるようになる。既存の `_copy_context_files()` 機構を拡張して実装する | v1.0.5 デモで agent-impl が `/send-message` を使おうとして "Unknown skill: send-message" エラー。エージェントがタスクプロンプトに書かれたコマンドを実行できない → タスク設計の自由度が下がる。`context_files` (v0.11.0) の自動コピー機構が既に存在し、`.claude/commands/` への拡張は少ない変更で実現できる。 |
| 高 | **役割別 system_prompt テンプレートライブラリ + `system_prompt_file:` YAML フィールド** — `.claude/prompts/roles/` に tester / implementer / reviewer / spec-writer / judge / advocate / critic の7種類のプロンプトファイルを提供し、`AgentConfig.system_prompt_file:` フィールドで参照できるようにする。各ファイルに「役割・禁止事項・完了条件・/plan /tdd の使い方・迎合抑制指示」を標準化して記述する | Vellum "Best practices for building multi-agent systems" (2025): 役割特化プロンプトとステート分離が精度向上に最も効果的。ChatEval ICLR 2024 (arXiv:2308.07201): 役割の多様性が討論品質を決定する最重要因子。同一ロール複数エージェントは性能低下を招く。CONSENSAGENT ACL 2025: 迎合抑制プロンプトが正答率・効率の両方を改善。v0.37.0 で advocate/critic/judge の 3 テンプレート (`.claude/prompts/roles/`) を追加。残りは tester / implementer / reviewer / spec-writer。 |
| ~~**高**~~ | ~~**`POST /workflows/adr` — Architecture Decision Record (ADR) 自動生成ワークフロー**~~ | ~~完了 v0.40.0 — proposer→reviewer→synthesizer 3エージェントパイプライン、MADR 4.0.0 フォーマット DECISION.md 生成。ALL 27 CHECKS PASSED。`test_workflow_adr.py` 25テスト。~~ |
| ~~**高**~~ | ~~**Codified Context インフラ**~~ | ~~完了 — `context_spec_files: list[str]` glob パターン、`_copy_context_spec_files()` 実装済み (`agents/claude_code.py`)。`tests/test_context_spec_files.py` 存在。~~ |
| 高 | **チェックポイント永続化による中断再開** — `ResultStore` (v0.24.0 JSONL) を拡張し、タスク進行状態・ワークフロー状態スナップショットを SQLite に保存。`tmux-orchestrator web --resume` フラグでプロセス再起動後に最後のチェックポイントから継続できるようにする | LangGraph は checkpointer + PostgresSaver で fault-tolerant persistence を提供 (LangChain docs 2025)。現状はプロセス再起動でキュー・ワークフロー状態が消滅する。ResultStore (v0.24.0) が JSONL 追記基盤として既に存在しており、SQLite 拡張は自然な次ステップ。 |
| 高 | **Claude Code `Stop` フックによる完了検出の置き換え** — エージェント起動時に `worktree/.claude/settings.local.json` に `Stop` フックを書き込み、Claude が応答を完了した瞬間に `POST /agents/{agent_id}/task-complete` を HTTP 呼び出しさせる。現行の 500ms ポーリング + regex パターンマッチを廃止し、決定論的な完了通知に置き換える。`SessionEnd` フックも組み合わせてエージェントライフサイクルの終了を検出する | Claude Code 公式ドキュメント "Hooks" (2025): `Stop` フックはターン完了時に必ず1回発火し、`type: "http"` で REST エンドポイントへ直接 POST 可能。現行の regex (`❯\s*$` 等) は CLI バージョンアップで壊れやすく、500ms ポーリングは CPU/I/O を継続消費する。v0.36.0 デモで 300 秒タイムアウトが発生した根本原因でもある。 |
| 高 | **`ProcessPort` 抽象インターフェースの抽出** — `ClaudeCodeAgent` が直接 `libtmux.Pane` に依存している箇所を `ProcessPort` protocol に置き換え、`TmuxProcessAdapter` と `StdioProcessAdapter`（テスト用）を実装する。`ClaudeCodeAgent` がこのポートのみに依存するよう依存方向を逆転させる | Martin "Clean Architecture" (2017) Ch.22 のポート&アダプターパターン。現状 `agents/claude_code.py` が `libtmux` を直接 import しており、tmux なし環境でのユニットテストが不可能。Naoyuki Sakai "AI Agent Architecture: Mapping Domain, Agent, and Orchestration to Clean Architecture" (2025) が同パターンを AI エージェントに適用。 |
| 中 | **`POST /workflows/clean-arch` — 4レイヤー分解ワークフロー** — Director が機能要求を domain / use-case / adapter / framework の4レイヤーに分解し、各レイヤーを専用ワーカーに割り当てるテンプレートを追加。`.claude/prompts/roles/` に `domain-agent.md` / `usecase-agent.md` / `adapter-agent.md` / `framework-agent.md` を提供し、PLAN.md 経由でハンドオフする | AgentMesh arXiv:2507.19902 (2025): Planner→Coder→Debugger→Reviewer の4ロール分担がソフトウェア開発タスクを自動化。Muthu (2025-11) "The Architecture is the Prompt": 「ヘキサゴナルアーキテクチャの境界がそのまま AI エージェントのコンテキスト制約になる」。`POST /workflows/tdd` および役割テンプレートライブラリ完成後に実装。 |
| ~~中~~ | ~~**`POST /workflows/pair` — PairCoder (Navigator + Driver) ワークフロー**~~ | ~~完了 v1.0.27 — `PairWorkflowSubmit`, navigator→driver 2エージェントパイプライン, `{prefix}_plan` + `{prefix}_result` スクラッチパッド. 17/17 デモ PASS. 35テスト.~~ |
| 中 | **`POST /workflows/delphi` — 複数ラウンド合意形成ワークフロー** — 3–5 名の専門家エージェント（セキュリティ / パフォーマンス / 保守性 / UX / コスト 各ペルソナ）が匿名で意見を提出し、モデレーターエージェントが集計して次ラウンドにフィードバックする Delphi サイクルを最大 3 ラウンド実行する DAG を追加。各ラウンドの出力を `delphi_round_{n}.md` に保存し、最終合意を `consensus.md` に書き出す | RT-AID "Real-Time AI Delphi" ScienceDirect 2025 (arXiv:2502.21092) が LLM によるデルファイ法の自動化を提案。Du et al. ICML 2024: 「エージェントが全員誤りでも討論で正解に収束するケースが多数存在する」→ 複数ラウンドの価値を実証。 |
| 中 | **`POST /workflows/redblue` — Red Team / Blue Team 対抗評価ワークフロー** — `blue-team`（実装・設計案を作成）・`red-team`（攻撃者視点で脆弱性・欠陥を列挙）・`arbiter`（リスク評価レポートを生成）の3エージェント構成。セキュリティレビュー・ビジネスケース検証・アーキテクチャ変更の影響分析に利用できる汎用的な対抗評価テンプレートを提供する | Adversarial Multi-Agent Evaluation arXiv:2410.04663: 反復討論型評価がバイアス削減と判断精度向上に有効。Red-Teaming LLM MAS ACL 2025 (arXiv:2502.14847): エージェント間通信への攻撃評価手法を提案。`debate` ワークフローの特殊化として実装できる。 |
| 中 | **`POST /workflows/socratic` — ソクラテス的対話ワークフロー** — `questioner`（前提・定義・論拠を問うマイウティカ法）・`responder`（回答を精緻化）・`synthesizer`（対話ログから構造化結論を抽出）の3エージェント構成。設計仕様の曖昧性解消・要件の深掘り・技術負債の根本原因分析に適する。最初のラウンドは強い反論、後のラウンドは統合的問いに変化させる段階的移行モデルを採用する | SocraSynth arXiv:2402.06634: モデレーター型ソクラテス的マルチエージェント討論プラットフォーム。KELE EMNLP 2025 (arXiv:2409.05511): LLM ベースのソクラテス教授エージェント実証。`debate` / `adr` ワークフローと共通基盤で実装できる。 |
| 中 | **`/deliberate <question>` スラッシュコマンド** — 単一の親エージェントが `/deliberate "REST vs GraphQL"` と入力すると、2 つのサブエージェント（advocate / critic）を自動スポーンし、2 ラウンドの討論後に `DELIBERATION.md` に結論を書き出して親に `deliberation_complete` STATUS を送信するコマンドを `.claude/commands/deliberate.md` として提供する | DEBATE ACL 2024 (arXiv:2405.09935): Devil's Advocate が単一 LLM 判断のバイアスを解消。CONSENSAGENT ACL 2025: 迎合抑制プロンプトで効率的な合意形成を実証。既存の `/spawn-subagent` + `reply_to` + Workflow DAG の組み合わせで実現可能。`debate` ワークフロー完成後に実装するのが自然。 |
| 中 | **`POST /workflows/ddd` — DDD Bounded Context 分解ワークフロー** — Director が機能要求をドメインイベント・集約・コマンドに分解した EventStorming マップを PLAN.md に書き出し、境界コンテキストごとに専用ワーカー（ubiquitous language 定義を `context_files` として受け取る）に実装を委任するテンプレートを追加する | Russ Miles "Domain-Driven Agent Design" (Engineering Agents 2025): DDD の Bounded Context がマルチエージェントの責務分割境界に直接対応する。Bakthavachalu (2025): 大手投資銀行の3 Bounded Context 実装事例（Risk / Regulatory / Validation）。`clean-arch` ワークフローの代替として提供。 |
| ~~中~~ | ~~**形式仕様エージェントステップ + `/spec` スラッシュコマンド + `POST /workflows/spec-first`**~~ | ~~完了 v1.1.8 — `/spec` スラッシュコマンド (`agent_plugin/commands/spec.md`) + `POST /workflows/spec-first` (spec-writer→implementer 2エージェントパイプライン)。`SpecFirstWorkflowSubmit` スキーマ。57テスト新規追加。2007テスト全通過。~~ |
| 中 | **コンテキスト4戦略ガイドを CLAUDE.md と `.claude/prompts/` に体系化** — 「書き込み (NOTES.md/PLAN.md)・選択 (context_files)・圧縮 (/summarize)・分離 (worktree + 別コンテキスト)」の4戦略を役割ごとに組み合わせたベストプラクティスチートシートを提供し、CLAUDE.md の "Running as an Orchestrated Agent" セクションに追記する | Algomatic Tech Blog "AIエージェントを支える技術: コンテキストエンジニアリングの現在地" (2025-10): 書き込み・選択・圧縮・分離の4戦略フレームワーク。Anthropic "Effective Context Engineering for AI Agents" (2025-09-29): 「コンテキストエンジニアリングとはプロンプト設計を超えた、推論時の情報エコシステム全体の管理」。実装コストが低くユーザー価値が高い。 |
| ~~低~~ | ~~**LLM-as-Judge による並列エージェント出力の自動スコアリング (BestOfN + EDDOps)**~~ | ~~完了 v1.1.0 — `POST /workflows/competition` として実装。N 個の solver エージェントが並列で同一問題を解き、judge エージェントがスコアを比較して勝者を宣言する (N+1)-agent DAG。53 tests PASSED。~~ |
| 低 | **ワークフローテンプレートライブラリ (`examples/workflows/`)** — TDD / PairCoder / CleanArch / DDD / SpecFirst / Debate / ADR の各ワークフローを `POST /workflows` で直接投入できる自己完結 YAML として `examples/workflows/` に収録する。各 YAML はエージェント数・ロール・`system_prompt_file` 参照・`context_files`・`required_tags` を含む。`examples/debate_config.yaml`（異種エージェント討論グループ設定）を含む | CrewAI の YAML-driven workflow approach (2025) が「ドキュメントとしての設定ファイル」を普及させた。各ワークフローエンドポイント実装後に対応 YAML を追加していく継続的タスク。A-HMAD Springer 2025 + ChatEval ICLR 2024: 異種構成エージェントと役割固定化が討論品質を決定する最重要因子。 |
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
| 中 | **Director の `agent_drift_warning` 購読による自動 re-brief** — Director エージェントが bus の `agent_drift_warning` イベントを購読し、ドリフトを検出したワーカーに自動で re-brief メッセージを送信する仕組みを追加する。v1.0.8 の「ディレクター投票が遅い」問題の根本解決。`/delegate` スラッシュコマンドで受信後に再ブリーフィングを実行 | v1.0.9 build-log: drift_warnings=0 は正常動作だが、将来の曖昧タスクでワーカーがドリフトする場合に Director が自動介入できる仕組みが必要。v1.0.8 build-log: 「Director polling が遅い (11分ループ)」根本原因は能動的な完了通知の欠如。`agent_drift_warning` bus イベントを Director が購読することで同様の問題を予防できる。 |
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

## 10. 調査記録

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

