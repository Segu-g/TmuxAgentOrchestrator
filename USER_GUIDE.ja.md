# TmuxAgentOrchestrator — ユーザーガイド

## 目次

1. [概要](#1-概要)
2. [動作要件](#2-動作要件)
3. [インストール](#3-インストール)
4. [クイックスタート](#4-クイックスタート)
5. [設定リファレンス](#5-設定リファレンス)
6. [起動モード](#6-起動モード)
7. [エージェントの種類](#7-エージェントの種類)
8. [タスクタイムアウト](#8-タスクタイムアウト)
9. [エージェントステータスイベント](#9-エージェントステータスイベント)
10. [タスクの投入](#10-タスクの投入)
11. [P2Pメッセージング](#11-p2pメッセージング)
12. [サブエージェントの生成](#12-サブエージェントの生成)
13. [Git Worktree 隔離](#13-git-worktree-隔離)
14. [Web UI & REST API](#14-web-ui--rest-api)
15. [スラッシュコマンド（Claude Code エージェント向け）](#15-スラッシュコマンドclaude-code-エージェント向け)
16. [トラブルシューティング](#16-トラブルシューティング)

---

## 1. 概要

TmuxAgentOrchestrator は、tmux ペイン内で Claude Code エージェントのプールを実行し、優先度付きキューからタスクを配信するシステムです。各エージェントは、専用の tmux ペイン内で動作する `claude --no-pager` プロセスで、キーボード入力とペイン出力のポーリングによって制御されます。

中央の **オーケストレーター** プロセスがキューを管理し、許可されたエージェントペア間のピアツーピア（P2P）メッセージをルーティングし、オプションで各エージェントを独自の git worktree に隔離します。

利用可能なインターフェース:

| インターフェース | コマンド | 用途 |
|---|---|---|
| Textual TUI | `tmux-orchestrator tui` | インタラクティブなターミナルダッシュボード |
| Web UI + REST | `tmux-orchestrator web` | ブラウザダッシュボード + HTTP API |
| ヘッドレス | `tmux-orchestrator run` | 単発タスクを実行して結果を出力して終了 |

---

## 2. 動作要件

| 依存関係 | 最低バージョン | 備考 |
|---|---|---|
| Python | 3.11 | `X \| Y` union 構文と `tomllib` を使用 |
| tmux | 最新の任意バージョン | 実行中であること。`$TMUX` またはサーバーがアクセス可能であること |
| git | 最新の任意バージョン | worktree 隔離を有効にする場合のみ必要 |
| `claude` CLI | 最新 | `claude_code` エージェントタイプの場合のみ必要 |

Python パッケージ依存関係（自動インストール）:

```
libtmux>=0.28   textual>=0.60   fastapi>=0.110   uvicorn[standard]
pyyaml>=6       typer>=0.12     rich>=13          websockets>=12
```

---

## 3. インストール

```bash
# プロジェクトディレクトリから
pip install -e ".[dev]"

# または uv を使用（推奨）
uv sync --extra dev
```

確認:

```bash
tmux-orchestrator --help
```

---

## 4. クイックスタート

### ステップ 1 — 設定ファイルを書く

```yaml
# myproject.yaml
session_name: mywork
mailbox_dir: ~/.tmux_orchestrator

agents:
  - id: worker-1
    type: claude_code

  - id: worker-2
    type: claude_code

p2p_permissions:
  - [worker-1, worker-2]   # この2エージェント間のメッセージを許可
```

### ステップ 2 — 起動する

```bash
# TUI（インタラクティブな使用に推奨）
tmux-orchestrator tui --config myproject.yaml

# Web サーバー（REST API + ブラウザダッシュボード）
tmux-orchestrator web --config myproject.yaml --port 8000

# 単発ヘッドレス実行
tmux-orchestrator run --config myproject.yaml --prompt "CLAUDE.md ファイルを要約して"
```

---

## 5. 設定リファレンス

### トップレベルフィールド

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `session_name` | 文字列 | `"orchestrator"` | アタッチまたは作成する tmux セッション名 |
| `mailbox_dir` | 文字列 | `"~/.tmux_orchestrator"` | ファイルベースのメッセージ保存ルートディレクトリ |
| `web_base_url` | 文字列 | `"http://localhost:8000"` | エージェントコンテキストファイルに注入される REST API ベース URL |
| `task_timeout` | 整数 | `120` | 実行中タスクを強制キャンセルするまでの秒数（0 = 無制限） |
| `p2p_permissions` | ペアのリスト | `[]` | 互いにメッセージを送信できるエージェント ID のペア |
| `agents` | リスト | `[]` | エージェント定義（下記参照） |

### エージェントフィールド

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `id` | 文字列 | 必須 | 一意のエージェント識別子 |
| `type` | `claude_code` | 必須 | 使用するエージェント実装（現在は `claude_code` のみ） |
| `isolate` | ブール | `true` | このエージェント専用の git worktree を作成するかどうか |

### 完全な例

```yaml
session_name: dev-swarm
mailbox_dir: ~/.tmux_orchestrator
web_base_url: http://localhost:9000
task_timeout: 300   # 5分後にタスクをキャンセル。0 = 無制限

agents:
  - id: planner
    type: claude_code
    isolate: true          # .worktrees/planner/ がブランチ worktree/planner に作成される

  - id: coder
    type: claude_code
    isolate: true

  - id: reviewer
    type: claude_code
    isolate: false         # メインリポジトリの作業ツリーを共有する

p2p_permissions:
  - [planner, coder]
  - [coder, reviewer]
```

---

## 6. 起動モード

### `tui` — Textual TUI

```bash
tmux-orchestrator tui --config myproject.yaml [--verbose]
```

次のパネルを持つフルスクリーンのターミナル UI を起動します:

| パネル | 内容 |
|---|---|
| Agents | エージェント ID、ステータス（IDLE/BUSY/ERROR/STOPPED）、現在のタスク |
| Task Queue | 優先度とプロンプトプレビュー付きの待機タスク |
| Log | 構造化ログストリーム |
| Status Bar | キーバインドの案内 |

**キーバインド:**

| キー | 操作 |
|---|---|
| `n` | 新しいタスクを投入（プロンプトダイアログを開く） |
| `k` | 選択したエージェントを停止（Kill） |
| `p` | タスク配信の一時停止 / 再開 |
| `q` | 終了してすべてのエージェントを停止 |

### `web` — FastAPI Web サーバー

```bash
tmux-orchestrator web --config myproject.yaml [--host 0.0.0.0] [--port 8000] [--verbose]
```

`http://{host}:{port}` に Web サーバーを起動します。ブラウザで開くとライブダッシュボードが表示されます。
エージェントのスラッシュコマンドが必要とする REST API も公開されます（[セクション 14](#14-web-ui--rest-api) 参照）。

### `run` — ヘッドレス単発実行

```bash
tmux-orchestrator run --config myproject.yaml --prompt "src/foo.py のユニットテストを書いて"
```

すべてのエージェントを起動し、プロンプトをタスクとして投入し、結果を待ち、stdout に出力してからすべてをシャットダウンします。スクリプトや CI パイプラインでの利用に適しています。

---

## 7. エージェントの種類

### `claude_code`

専用の tmux ペイン内で `claude --no-pager` CLI を駆動します。

**タスクの配信方法:** オーケストレーターは人間が入力するように `send_keys` を呼び出してペインにタスクプロンプトを送信します。

**完了の検出方法:** オーケストレーターは 500 ms ごとにペイン出力をポーリングします。3回連続で出力が変化せず、かつ最後に表示される行がプロンプトパターン（`$`、`>`、または `Human:`）に一致した場合、タスクが完了したと判断し、キャプチャしたテキストを結果として公開します。

**ライフサイクル:**

```
start()  →  新規 tmux ペイン  →  コンテキストファイル書き込み  →  cd {worktree} && claude --no-pager
stop()   →  "q" を送信  →  ペイン監視解除  →  worktree 削除
shutdown →  tmux セッションを終了
```

**コンテキストファイル:** 起動時にオーケストレーターはエージェントの作業ディレクトリに `__orchestrator_context__.json` を書き込みます。エージェントはこのファイルを読み込んで、`agent_id`、メールボックスのパス、REST API の URL を取得できます。

**受信トレイ通知:** エージェントにメッセージが届くと、オーケストレーターはペインに `__MSG__:{msg_id}` と入力します。エージェントはその後 `/check-inbox` を使って読み取ることができます。

---

## 8. タスクタイムアウト

### 強制適用の仕組み

`task_timeout` 設定値（デフォルト `120` 秒）は、すべてのエージェントタイプで タスクごとに強制適用されます。タスクが制限を超えると:

1. `_dispatch_task` コルーチンが `asyncio.wait_for` によってキャンセルされます。
2. `error: "timeout"` を含む `RESULT` メッセージがバスに公開されます。
3. エージェントは `IDLE` に戻り、次のタスクを即座に受け付けられる状態になります。

```json
{
  "type": "RESULT",
  "from_id": "worker-1",
  "payload": {
    "task_id": "3f8a2b…",
    "error": "timeout",
    "output": null
  }
}
```

### カスタムタイムアウトの設定

```yaml
task_timeout: 60     # 60秒
task_timeout: 0      # タイムアウトなし（永続実行）
```

---

## 9. エージェントステータスイベント

オーケストレーターはすべてのライフサイクルの遷移で `STATUS` バスメッセージを公開します。WebSocket 経由で接続したクライアントはこれをリアルタイムで受信します。

| `event` | タイミング | ペイロードキー |
|---|---|---|
| `task_queued` | タスクがキューに追加された | `task_id`、`prompt` |
| `agent_busy` | エージェントがタスクの実行を開始した | `agent_id`、`status`、`task_id` |
| `agent_idle` | エージェントがタスクを正常に完了した | `agent_id`、`status`、`task_id` |
| `agent_error` | エージェントのタスクで例外が発生した | `agent_id`、`status`、`task_id` |
| `subagent_spawned` | サブエージェントが作成された | `sub_agent_id`、`parent_id` |

WebSocket メッセージの例:

```json
{
  "type": "STATUS",
  "from_id": "worker-1",
  "payload": {
    "event": "agent_busy",
    "agent_id": "worker-1",
    "status": "BUSY",
    "task_id": "3f8a2b…"
  }
}
```

タイムアウトの場合は `agent_idle` イベントが発生します（エージェントは IDLE に戻ります）。未処理の例外の場合は `agent_error` が発生します。

---

## 10. タスクの投入

### TUI から

TUI で `n` キーを押し、プロンプトを入力して Enter を押します。オプションの優先度フィールド（整数。小さいほど高優先度。デフォルト 0）も使用できます。

### REST API から

```bash
curl -s -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Python の hello world を書いて", "priority": 0}'
```

レスポンス:
```json
{"task_id": "3f8a2b…", "prompt": "Python の hello world を書いて", "priority": 0}
```

### Python から（プログラム的）

```python
import asyncio
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import load_config
from tmux_orchestrator.orchestrator import Orchestrator
from tmux_orchestrator.tmux_interface import TmuxInterface

async def main():
    config = load_config("myproject.yaml")
    bus = Bus()
    tmux = TmuxInterface(session_name=config.session_name, bus=bus)
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    await orch.start()
    task = await orch.submit_task("hello", priority=0)
    print(task.id)

asyncio.run(main())
```

### 優先度

タスクは `asyncio.PriorityQueue` を使用します。整数が小さいほど先に配信されます。

```bash
# 高優先度
curl -X POST http://localhost:8000/tasks \
  -d '{"prompt": "緊急の修正", "priority": -10}'

# 通常
curl -X POST http://localhost:8000/tasks \
  -d '{"prompt": "バックログのアイテム", "priority": 100}'
```

---

## 11. P2Pメッセージング

エージェントは許可テーブルによってゲートされた P2P メッセージを互いに直接送信できます。

### 権限の有効化

YAML 設定で:

```yaml
p2p_permissions:
  - [agent-a, agent-b]   # 双方向: a→b と b→a
  - [agent-b, agent-c]
```

各エントリは双方向です。ペアが記載されていない場合、そのエージェント間のメッセージはオーケストレーターによって黙って破棄されます。

**例外:** REST API（`POST /agents/{id}/message`）経由で送信されたメッセージは `from_id = "__user__"` を持ち、常に権限チェックをバイパスします。

### REST でメッセージを送信する

```bash
curl -s -X POST http://localhost:8000/agents/worker-2/message \
  -H "Content-Type: application/json" \
  -d '{"type": "PEER_MSG", "payload": {"text": "テストを実行してもらえますか？"}}'
```

### Claude Code エージェントからメッセージを送信する

エージェントのペイン内で `/send-message` スラッシュコマンドを使用します:

```
/send-message worker-2 テストを実行してもらえますか？
```

### メッセージスキーマ

メッセージはメールボックスに JSON ファイルとして保存されます:

```
~/.tmux_orchestrator/{session_name}/{agent_id}/inbox/{msg_id}.json
```

```json
{
  "id": "uuid",
  "type": "PEER_MSG",
  "from_id": "worker-1",
  "to_id": "worker-2",
  "payload": {"text": "テストを実行してもらえますか？"},
  "timestamp": "2026-03-02T09:00:00+00:00"
}
```

### メッセージタイプ

| タイプ | 用途 |
|---|---|
| `TASK` | タスク配信情報の転送 |
| `RESULT` | エージェントが完了結果を公開する |
| `STATUS` | オーケストレーターのライフサイクルイベント（task_queued、subagent_spawned など） |
| `PEER_MSG` | エージェント間通信 |
| `CONTROL` | オーケストレーターへのコマンド（例: spawn_subagent） |

---

## 12. サブエージェントの生成

エージェントはオーケストレーターに対して自分の監督下で新しいワーカーを生成するよう要求できます。オーケストレーターは親と子の間の P2P 権限を自動的に付与します。

サブエージェントは必ず YAML 設定で**事前に定義されたエージェント**に基づいています。`template_id`（設定の `id`）を指定し、オーケストレーターがそのエージェントの新しいインスタンスを一意の ID で作成します。これにより、設定で明示的に承認されたエージェントのみが実行時に生成できます。

### REST API 経由

```bash
curl -s -X POST http://localhost:8000/agents \
  -H "Content-Type: application/json" \
  -d '{
    "parent_id": "worker-1",
    "template_id": "worker-2"
  }'
```

レスポンス:
```json
{"status": "spawning", "parent_id": "worker-1"}
```

生成されたエージェントの ID は、次の STATUS バスメッセージとして親に送り返されます:

```json
{
  "event": "subagent_spawned",
  "sub_agent_id": "worker-1-sub-a3f2c1",
  "parent_id": "worker-1"
}
```

### CONTROL メッセージ経由（プログラム的）

```python
from tmux_orchestrator.bus import Message, MessageType

await bus.publish(Message(
    type=MessageType.CONTROL,
    from_id="worker-1",
    to_id="__orchestrator__",
    payload={
        "action": "spawn_subagent",
        "template_id": "worker-2",          # 設定内のエージェント id と一致させる
        "share_parent_worktree": False,      # True で親の cwd を再利用
    }
))
```

### サブエージェント ID の形式

サブエージェントの ID は自動生成されます: `{parent_id}-sub-{6桁の16進数}`、例: `worker-1-sub-a3f2c1`。サブエージェントはテンプレート設定の `isolate` 設定を継承します。

### Claude Code エージェントから

```
/spawn-subagent worker-2
```

その後、受信トレイの STATUS メッセージを確認し、`sub_agent_id` を読み取ってタスクを委譲します:

```
/check-inbox
/read-message <受信トレイのID>
/send-message worker-1-sub-a3f2c1 最新の CI 実行のテスト失敗を分析して
```

---

## 13. Git Worktree 隔離

git リポジトリ内で実行する場合、各エージェントに独自の隔離された作業ツリーを付与できます。これにより、複数のエージェントが同じコードベースで並行して作業する際のファイル競合を防げます。

### 仕組み

1. `start()` 時に、オーケストレーターが `git worktree add .worktrees/{agent_id} -b worktree/{agent_id}` を呼び出します。
2. エージェントのプロセス（`claude` またはカスタムスクリプト）は `cwd` が `.worktrees/{agent_id}/` に設定された状態で実行されます。
3. `stop()` 時に、オーケストレーターが worktree を削除してブランチを消去します。

```
repo/
├── .git/
├── .worktrees/          ← 自動作成、gitignore 済み
│   ├── worker-1/        ← worker-1 の隔離チェックアウト、ブランチ worktree/worker-1
│   └── worker-2/        ← worker-2 の隔離チェックアウト、ブランチ worktree/worker-2
├── src/
└── ...
```

### `isolate: false` でオプトアウト

```yaml
agents:
  - id: read-only-agent
    type: claude_code
    isolate: false   # メインの作業ツリーで直接実行される
```

ファイルを読み取るだけのエージェントや、独立したブランチが不要なエージェントに使用してください。

### git リポジトリ外での動作

git リポジトリ外でオーケストレーターを起動した場合、すべてのエージェントの worktree 隔離は自動的に無効になります。警告がログに記録されます:

```
WARNING Not inside a git repository; worktree isolation disabled
```

エージェントは通常通り起動・実行されます。ただし worktree 隔離は行われません。

### `.gitignore`

`WorktreeManager` は初回使用時にリポジトリの `.gitignore` に `.worktrees/` を自動追加します。`.gitignore` が存在しない場合は新規作成されます。

### 親の worktree を共有する（サブエージェントのみ）

サブエージェントを生成する際、`share_parent_worktree: true` を渡すと、サブエージェントが新しい worktree を取得する代わりに親と同じディレクトリで実行されます:

```python
payload={
    "action": "spawn_subagent",
    "agent_type": "custom",
    "command": "python3 scripts/validator.py",
    "share_parent_worktree": True,
}
```

---

## 14. Web UI & REST API

Web サーバーを起動します:

```bash
tmux-orchestrator web --config myproject.yaml --port 8000
```

`http://localhost:8000` をブラウザで開くとライブダッシュボードが表示されます。

### エンドポイント

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/` | ブラウザダッシュボード（HTML） |
| `GET` | `/agents` | すべてのエージェントとそのステータスを一覧表示 |
| `POST` | `/agents` | サブエージェントを生成する |
| `DELETE` | `/agents/{id}` | エージェントを停止して削除する |
| `POST` | `/agents/{id}/message` | エージェントにメッセージを送信する |
| `GET` | `/tasks` | キュー内の（待機中の）タスクを一覧表示 |
| `POST` | `/tasks` | 新しいタスクを投入する |
| `WS` | `/ws` | すべてのバスイベントの WebSocket ストリーム |

### POST /tasks

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt": "こんにちは！", "priority": 0, "metadata": {}}'
```

### POST /agents/{id}/message

```bash
curl -X POST http://localhost:8000/agents/worker-1/message \
  -H "Content-Type: application/json" \
  -d '{"type": "PEER_MSG", "payload": {"text": "UI からの挨拶"}}'
```

注意: REST メッセージは `from_id = "__user__"` で送信され、常に P2P 権限チェックをバイパスします。

### POST /agents（サブエージェントの生成）

```bash
curl -X POST http://localhost:8000/agents \
  -H "Content-Type: application/json" \
  -d '{"parent_id": "worker-1", "template_id": "worker-2"}'
```

### WebSocket

`ws://localhost:8000/ws` に接続すると、すべてのバスイベントを JSON としてリアルタイムストリームで受信できます:

```json
{
  "id": "uuid",
  "type": "STATUS",
  "from_id": "__orchestrator__",
  "to_id": "*",
  "payload": {"event": "task_queued", "task_id": "…"},
  "timestamp": "2026-03-02T09:00:00+00:00"
}
```

---

## 15. スラッシュコマンド（Claude Code エージェント向け）

Claude Code エージェントが起動すると、オーケストレーターは `__orchestrator_context__.json` を作業ディレクトリに書き込みます。このファイルにより、すべてのエージェントの Claude セッションで以下のスラッシュコマンドが使用できます:

| コマンド | 使い方 | 説明 |
|---|---|---|
| `/check-inbox` | `/check-inbox` | 未読メッセージを一覧表示（ID、送信者、タイプ、ペイロードプレビュー） |
| `/read-message` | `/read-message <msg_id>` | メッセージの全内容を読み取り、既読にマーク |
| `/send-message` | `/send-message <agent_id> <テキスト>` | 別のエージェントに PEER_MSG を送信 |
| `/spawn-subagent` | `/spawn-subagent <template_id>` | 事前設定済みサブエージェントを自動 P2P 付きで生成 |
| `/list-agents` | `/list-agents` | すべてのエージェントとそのステータスを表示 |

REST API を呼び出すコマンド（`/send-message`、`/spawn-subagent`、`/list-agents`）はオーケストレーターを `web` モードで実行している必要があります。メールボックスファイルを直接使用するコマンド（`/check-inbox`、`/read-message`）はすべてのモードで動作します。

---

## 16. トラブルシューティング

### エージェントが起動しない

- オーケストレーターは起動時に自分で新しい tmux セッションを作成します。既存のセッションは不要です。
- 同じ `session_name` のセッションが存在する場合は強制終了して置き換えられます。
- `--verbose` オプションでデバッグログを確認する。

### タスクが完了しない（ClaudeCodeAgent がハングしている）

オーケストレーターはペイン出力が安定し、最後の行が `$`、`>`、または `Human:` で終わるまで待機します。`claude` がまだストリーミング中かプロンプトが認識されない場合:

- `claude_code.py` の `_SETTLE_CYCLES` を増やす（デフォルト 3 × 500 ms = 1.5 秒）。
- `_DONE_PATTERNS` のリストが使用している `claude` CLI のバージョンに一致しているか確認する。
- tmux ペインにアタッチして状態を観察する: `tmux attach -t {session_name}`。

### P2P メッセージが届かない

- 設定の `p2p_permissions` に送信者/受信者のペアが含まれているか確認する。
- オーケストレーターのログに `P2P {from} → {to} blocked (not in permission table)` がないか確認する。
- 回避策として REST API の `POST /agents/{id}/message` を使用する — 常に届けられる。

### 起動時に worktree エラーが発生する

```
fatal: '<path>' is already checked out at '...'
```

前回の実行で古い worktree が残っています。クリーンアップします:

```bash
git worktree list              # 登録されているすべての worktree を確認
git worktree remove --force .worktrees/worker-1
git branch -D worktree/worker-1
```

または `.worktrees/` ディレクトリを直接削除します:

```bash
rm -rf .worktrees/
git worktree prune
```

### `WorktreeManager: Not inside a git repository`

git リポジトリ外でオーケストレーターを実行しています。worktree 隔離は自動的に無効になります。これはエラーではありません — エージェントは現在のディレクトリで実行されます。警告を消すには `isolate: false` を明示的に設定するか、git リポジトリ内から実行してください。

### スラッシュコマンドが "connection refused" で失敗する

`/send-message`、`/spawn-subagent`、`/list-agents` コマンドは REST API を呼び出します。以下を確認してください:

1. オーケストレーターが `tmux-orchestrator web`（`tui` ではなく）で起動されているか。
2. 設定の `web_base_url` が実際のホスト:ポートと一致しているか。
3. ファイアウォールがポートをブロックしていないか。

### メールボックスディレクトリが見つからない

メールボックスのデフォルトは `~/.tmux_orchestrator` です。設定で `mailbox_dir` を変更した場合は、そのパスに書き込み権限があることを確認してください。オーケストレーターはサブディレクトリを自動的に作成します。
