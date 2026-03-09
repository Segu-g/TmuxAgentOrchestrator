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
| 中 | **`UseCaseInteractor` 層の抽出** — FastAPI ハンドラーから業務ロジックを分離 | 層5 |
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
| 高 | **`POST /workflows/adr` — Architecture Decision Record (ADR) 自動生成ワークフロー** — `proposer`（案提示）→ `reviewer`（技術的批評）→ `synthesizer`（ADR 文書化）の3エージェントが MADR フォーマット (title / status / context / decision / consequences) の `DECISION.md` を生成するテンプレートを追加。`context_files` に既存 ADR を渡すことで過去の決定との整合性を保つ | MAD in Requirements Engineering arXiv:2507.05981 (2025): MAD が要件分類 F1 を 0.726→0.841 に向上。SocraSynth arXiv:2402.06634: モデレーター + 対立エージェント + ジャッジ構成が設計討論に直接適用可能。`debate` ワークフローと共通基盤で実装できる。 |
| 高 | **Codified Context インフラ** — プロジェクト規約・禁止事項を機械可読な YAML/JSON 仕様ファイルとして `.claude/specs/` に配置し、`AgentConfig.context_spec_files` (glob パターン) でタスク開始時にワークツリーへ自動コピーする。エージェントがセッションをまたいでも規約を忘れない基盤を実現する | Vasilopoulos arXiv:2602.20478 "Codified Context" (2026-02): 108,000行 C# 分散システムで 283 セッションにわたり規約を維持。Hot-memory constitution + Cold-memory spec documents の2層構造が有効。既存の `context_files` (v0.11.0) 機構の自然な拡張。 |
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
| **高** | **クリーンアーキテクチャ層別ディレクトリ移行** — 現在フラットな `tmux_orchestrator/` 以下のモジュールを `domain/` / `application/` / `infrastructure/` / `monitoring/` / `adapters/` に段階的に移動する。後方互換シム（旧パスからの re-export）を置き、テストを壊さずに移行する。移行順: ① `domain/` (AgentStatus, Task, MessageType 抽出), ② `infrastructure/` (tmux_interface, messaging, worktree 等), ③ `application/` (orchestrator, registry, bus), ④ `adapters/` (config, factory, schemas, web, tui)。各移動は独立したコミット単位で行い、`uv run pytest tests/ -x -q` が常にグリーンであることを確認する | §2「ドメイン分離方針」および「目標とするクリーンアーキテクチャ層構造」参照。Martin "Clean Architecture" (2017): 依存は常にドメイン中心に向かう（Dependency Rule）。現状は orchestrator.py が context_monitor / drift_monitor / result_store 等のインフラを直接 import しており、依存方向が逆転している。移行することで各層の単体テストが高速化・安定化する。 |
| ~~**高**~~ | ~~**`domain/` 純粋型の抽出** — `AgentStatus`, `AgentRole` (from config.py / agents/base.py)、`Task` (from agents/base.py)、`MessageType` / `Message` (from bus.py) を `domain/agent.py` / `domain/task.py` / `domain/message.py` に移動。既存モジュールは `from tmux_orchestrator.domain.agent import AgentStatus` を re-export するシムに書き換える。domain/ は外部ライブラリを一切 import しない~~ | ~~完了 v1.0.11 — 1156 tests 全通過。Strangler Fig パターンで後方互換性を保ちつつ型を集約。`test_domain_purity.py` 20 tests で純粋性を継続保証。14/15 デモ PASS。~~ |
| ~~**高**~~ | ~~**`orchestrator.py` のインフラ依存を依存注入（DI）に置き換える**~~ | ~~完了 v1.0.35 — `ResultStoreProtocol`, `CheckpointStoreProtocol`, `AutoScalerProtocol` を `application/infra_protocols.py` に定義。`NullResultStore`, `NullCheckpointStore`, `NullAutoScaler` Null Object 実装を追加。`WorkflowManager`, `GroupManager` も constructor injection 対応。`reconfigure_autoscaler()` 公開メソッド追加。50 tests 追加 (32 protocol + 18 DI)。20/20 デモ PASS。~~ |
| 高 | **OpenTelemetry GenAI Semantic Conventions 準拠トレース出力** — `gen_ai.*` 属性 (token counts, tool calls, agent spans) を既存 `trace_id` ベースの構造化ログに付加し、Datadog/Jaeger/OTLP エクスポーターへ送信できるようにする | OpenTelemetry "AI Agent Observability" (2025) が業界標準に収斂しつつあり、Datadog が GenAI Semantic Conventions にネイティブ対応済み。現状の `trace_id` は相関のみでスパン階層がない。[opentelemetry.io/blog/2025/ai-agent-observability](https://opentelemetry.io/blog/2025/ai-agent-observability/) |
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
