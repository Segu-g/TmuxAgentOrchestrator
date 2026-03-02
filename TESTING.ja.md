# TmuxAgentOrchestrator — テストスイートリファレンス

## 目次

1. [概要](#1-概要)
2. [テストの実行](#2-テストの実行)
3. [テストアーキテクチャ](#3-テストアーキテクチャ)
4. [test_bus.py — メッセージバス（6テスト）](#4-test_buspy--メッセージバス6テスト)
5. [test_messaging.py — メールボックス（10テスト）](#5-test_messagingpy--メールボックス10テスト)
6. [test_orchestrator.py — タスク配信・P2P・タイムアウト（11テスト）](#6-test_orchestratorpy--タスク配信p2pタイムアウト11テスト)
7. [test_tmux_interface.py — tmux ラッパー（8テスト）](#7-test_tmux_interfacepy--tmux-ラッパー8テスト)
8. [test_worktree.py — Git Worktree マネージャー（9テスト）](#8-test_worktreepy--git-worktree-マネージャー9テスト)
9. [テストインフラストラクチャ](#9-テストインフラストラクチャ)
10. [カバレッジとギャップ](#10-カバレッジとギャップ)
11. [新しいテストの追加方法](#11-新しいテストの追加方法)

---

## 1. 概要

スイートには 5 ファイルにわたる **43 のテスト**があります。コアライブラリモジュールのすべての公開動作がカバーされています。実際の外部プロセス（tmux サーバー、`claude` CLI）との統合は完全に回避されています — モックまたは一時的な実 git リポジトリに置き換えられています。

```
tests/
├── test_bus.py             6テスト  — 非同期 pub/sub メッセージバス
├── test_messaging.py      10テスト  — ファイルベースのメールボックス（4クラス）
├── test_orchestrator.py   11テスト  — タスクキュー、配信、P2Pルーティング、タイムアウト、イベント
├── test_tmux_interface.py  8テスト  — libtmux ラッパー
└── test_worktree.py        9テスト  — git worktree ライフサイクル（実 git 使用）
```

**実行結果:**

```
43 passed in ~3.4 s
```

---

## 2. テストの実行

```bash
# すべてのテストを詳細表示
uv run pytest tests/ -v

# 単一ファイル
uv run pytest tests/test_bus.py -v

# テスト名で指定
uv run pytest tests/test_orchestrator.py::test_p2p_allowed -v

# print 出力を表示（デバッグに便利）
uv run pytest tests/ -v -s

# 最初の失敗で停止
uv run pytest tests/ -x
```

**前提条件:** `uv sync --extra dev` で `pytest>=8` と `pytest-asyncio>=0.23` がインストールされます。`asyncio_mode = "auto"`（`pyproject.toml` で設定）により、すべての非同期テストが自動的に実行されます。

---

## 3. テストアーキテクチャ

### 分離戦略

| テスト対象モジュール | 分離方法 |
|---|---|
| `bus.py` | 純粋なインプロセス asyncio、I/O なし |
| `messaging.py` | `tmp_path` フィクスチャ（pytest が一時ディレクトリを提供） |
| `orchestrator.py` | `DummyAgent` が実エージェントを置換；`MagicMock` が tmux を置換 |
| `tmux_interface.py` | `@patch("…libtmux.Server")` が tmux サーバーをモック |
| `worktree.py` | `tmp_path` 内に実際の `git init`；モックなし |

### 非同期テストランナー

すべての非同期テスト関数は自動的に検出・実行されます。`pyproject.toml` の設定:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

これにより `@pytest.mark.asyncio` デコレーターが不要になります。

### 主要な共有ヘルパー

**`DummyAgent`**（`test_orchestrator.py` 内）:

`ClaudeCodeAgent` の代わりに使用する最小限の `Agent` サブクラス。
- `start()` でステータスを `IDLE` に設定し、`_run_loop` を起動する。
- `_dispatch_task` で各受信タスクを `self.dispatched` に追記し、`_set_idle()` を呼び出す。
- `stop()`、`handle_output()`、`notify_stdin()` はno-opで実装。

これにより、オーケストレーターのテストは、ライブの tmux ペインやサブプロセスを必要とせずに配信ロジックを検証できます。

---

## 4. `test_bus.py` — メッセージバス（6テスト）

`tmux_orchestrator.bus` の `Bus`、`Message`、`MessageType` をテストします。

### フィクスチャ

```python
@pytest.fixture
def bus() -> Bus:
    return Bus()
```

テストごとに新鮮な `Bus` インスタンスを用意します。共有状態なし。

---

### `test_broadcast_delivery`

**テスト内容:** デフォルトの `to_id="*"`（ブロードキャストセンチネル）で公開された `Message` が、エージェント ID に関わらずすべてのサブスクライバーに届くこと。

**セットアップ:** 2 つのサブスクライバー（`agent-a`、`agent-b`）。STATUS ブロードキャストメッセージ 1 つ。

**アサーション:**
- 両キューにちょうど 1 アイテムが入っている。
- `q_a` のアイテムが公開されたメッセージと同じ `id` を持つ。

**重要な理由:** ブロードキャストチャンネルは TUI、web ハブ、オーケストレーターのルーターが使用します。これらはすべて特定の ID に登録せずにすべてのメッセージを見る必要があります。

---

### `test_directed_delivery`

**テスト内容:** 宛先指定メッセージ（`to_id="agent-a"`）が意図した受信者にのみ届くこと。

**セットアップ:** 2 つのサブスクライバー。`agent-a` 宛の TASK メッセージ 1 つ。

**アサーション:**
- `q_a.qsize() == 1`
- `q_b.qsize() == 0`

**重要な理由:** エージェント間のメッセージ漏洩を防ぎます。あるエージェント宛の RESULT や PEER_MSG が別のエージェントのキューに現れてはなりません。

---

### `test_broadcast_subscriber_receives_all`

**テスト内容:** `broadcast=True` で登録されたサブスクライバーが、異なるエージェント ID に宛てられた指定メッセージも受信すること。

**セットアップ:** `hub` が `broadcast=True` でサブスクライブ；`agent-x` が通常通りサブスクライブ。RESULT メッセージを `agent-x` に宛てて送信。

**アサーション:**
- `q_hub` と `q_agent` の両方がそれぞれ 1 つのメッセージを持つ。

**重要な理由:** TUI と WebSocket ハブは `broadcast=True` を使って表示のためにすべてのトラフィックを監視します。オーケストレーターの内部ルーターも PEER_MSG と CONTROL メッセージを傍受するためにこれを使用します。

---

### `test_unsubscribe`

**テスト内容:** `bus.unsubscribe("gone")` 後に、ブロードキャストメッセージを公開してもサブスクライブ解除されたエージェントのキューに何も追加されないこと。

**アサーション:** 公開後もキューサイズが 0 のまま。

**重要な理由:** 停止したエージェントにメッセージが蓄積されてメモリリークしないことを保証します。

---

### `test_queue_full_drops_message`

**テスト内容:** サブスクライバーのキューが満杯（`maxsize=1`）のとき、2 番目のメッセージが例外を発生させることなく黙って破棄されること。

**セットアップ:** `maxsize=1` でサブスクライブ。最初のメッセージでキューを埋める。2 番目を公開。

**アサーション:** キューはまだサイズ 1（2 番目のメッセージは破棄された）。

**重要な理由:** バスはノンブロッキングで、遅い消費者に対して耐障害性を持つ必要があります。遅い TUI や web ハブがオーケストレーターの配信ループを停止させてはなりません。

---

### `test_message_iter`

**テスト内容:** `Bus.iter_messages()` がメッセージを FIFO 順で yield し、`task_done()` を正しく呼び出すこと。

**セットアップ:** サブスクライブ後、`{"n": 0}`、`{"n": 1}`、`{"n": 2}` のペイロードを持つ STATUS メッセージを 3 つ公開。

**アサーション:** `received == [0, 1, 2]`。

**重要な理由:** `iter_messages` は `run` CLI コマンドでタスク結果を待つために使用されます。`asyncio.Queue.join()` メカニズムには正しい順序と `task_done()` のセマンティクスが必要です。

---

## 5. `test_messaging.py` — メールボックス（10テスト）

`tmux_orchestrator.messaging` の `Mailbox` クラスをテストします。

### フィクスチャ

```python
@pytest.fixture
def mailbox(tmp_path: Path) -> Mailbox:
    return Mailbox(root_dir=tmp_path, session_name="test-session")
```

各テストは新鮮な一時ディレクトリを取得します。実際の `~/.tmux_orchestrator` には触れません。

### ヘルパー

```python
def _make_msg(to_id="agent-b", text="hello") -> Message:
    return Message(
        type=MessageType.PEER_MSG,
        from_id="agent-a",
        to_id=to_id,
        payload={"text": text},
    )
```

---

### `TestMailboxWrite`

#### `test_write_creates_file`

`mailbox.write("agent-b", msg)` を呼び出して検証します:
- 返されたパスがディスク上に存在する。
- ファイルが正しい `id` と `payload.text` を持つ有効な JSON としてパースできる。

#### `test_write_in_inbox`

`write()` が返したファイルパスに文字列 `"inbox"` が含まれていることを確認し、`inbox/` サブディレクトリに配置されていること（`read/` ではないこと）を確認します。

---

### `TestMailboxRead`

#### `test_read_from_inbox`

メッセージを書き込み、`mailbox.read("agent-b", msg.id)` で読み返します。`data["id"] == msg.id` をアサートします。

#### `test_read_missing_raises`

何も書き込まずに `mailbox.read("agent-b", "nonexistent-id")` を呼び出します。`FileNotFoundError` が発生することをアサートします。

#### `test_read_after_mark_read`

メッセージを書き込み、`mark_read()` を呼び出し、再度 `read()` を呼び出します。メッセージがまだ読み取れること（`read/` ディレクトリから）をアサートします。これにより、既読マークがメッセージを破壊しないことを検証します。

---

### `TestMailboxListInbox`

#### `test_empty_inbox`

メッセージのないエージェントで `list_inbox()` を呼び出します。空のリストが返されることをアサートします（ディレクトリが存在しなくてもエラーなし）。

#### `test_lists_messages`

異なる ID で 2 つのメッセージを書き込みます。`list_inbox()` が両方の ID を返すことをアサートします（順序はファイルシステムに依存するため集合比較）。

#### `test_mark_read_removes_from_inbox`

メッセージを書き込み、既読にマークし、`list_inbox()` を呼び出します。リストが空であることをアサートします — メッセージは `inbox/` にもはや存在しません。

---

### `TestMailboxMarkRead`

#### `test_mark_read_moves_file`

メッセージを書き込み、`mark_read()` を呼び出し、期待される `read/` パスにファイルが存在することを直接確認します:
```
{tmp_path}/test-session/agent-b/read/{msg.id}.json
```

これは `/read-message` スラッシュコマンドが依存する正確なファイルシステムレイアウトを検証します。

#### `test_mark_read_nonexistent_noop`

空のメールボックスで `mark_read("agent-b", "nonexistent-id")` を呼び出します。例外が発生しないことをアサートします。オーケストレーターがすでに処理されたメッセージを確認しようとした場合のクラッシュを防ぎます。

---

## 6. `test_orchestrator.py` — タスク配信・P2P・タイムアウト（11テスト）

`DummyAgent` と `SlowDummyAgent` を使って `tmux_orchestrator.orchestrator` の `Orchestrator` をテストします。

### ヘルパー

**`make_config(**kwargs)`** は、デフォルト値（空のエージェント/権限、10 秒タイムアウト）を持つ `OrchestratorConfig` を構築します。

**`make_tmux_mock()`** は `TmuxInterface` インターフェースを満たす `MagicMock` を返します。

**`SlowDummyAgent`** — `_dispatch_task` が 9 999 秒スリープ（事実上完了しない）する `Agent` サブクラス。`task_timeout` を発動させるために使用します。

---

### `test_submit_and_dispatch`

**テスト内容:** `orch.submit_task()` で投入されたタスクが登録済みのアイドルエージェントに配信されること。

**セットアップ:** `DummyAgent("a1")` 1 つを登録・起動。タスク 1 つを投入。

**タイミング:** 配信ループ（200 ms ごとにポーリング）のために 300 ms 待機。

**アサーション:** `agent.dispatched` に正しい ID のタスクが含まれる。

**重要な理由:** コアの配信パス — これが壊れると何も実行されない。

---

### `test_no_idle_agent_requeues`

**テスト内容:** すべてのエージェントが BUSY のとき、投入されたタスクがキューに残り配信されないこと。

**セットアップ:** エージェントを起動（`start()` によって IDLE にリセット）、その後 `orch.start()` 後に手動で BUSY に設定。タスク 1 つを投入。

**アサーション:** 300 ms 後に `agent.dispatched` が空。

**注意:** `start()` は `agent.start()` を呼び出してステータスを IDLE にリセットするため、`orch.start()` を呼び出した*後*にステータスを BUSY に設定する必要があります。

**重要な理由:** オーケストレーターが BUSY エージェントに誤って配信しないこと、およびタスクがキューに残ることを検証します。

---

### `test_p2p_allowed`

**テスト内容:** 権限テーブルに含まれる 2 つのエージェント間の PEER_MSG が転送されて受信者に届くこと。

**セットアップ:** `p2p_permissions=[("a1", "a2")]` の設定。`q_a2` をサブスクライブ。`orch.route_message()` を直接呼び出す。

**アサーション:**
- 100 ms 後に `q_a2.qsize() == 1`。
- 受信したメッセージが正しいペイロードを持つ。

---

### `test_p2p_blocked`

**テスト内容:** 権限エントリのないエージェント間の PEER_MSG が黙って破棄されること。

**セットアップ:** `p2p_permissions` が空の設定。`q_b` をサブスクライブ。`"a"` から `"b"` へのメッセージをルーティング試行。

**アサーション:** `q_b.qsize() == 0`。

---

### `test_pause_and_resume`

**テスト内容:** `orch.pause()` が配信を防ぎ、`orch.resume()` が再有効化すること。

**シーケンス:**
1. 一時停止 → `is_paused == True`。
2. タスクを投入 → 300 ms 待機 → `agent.dispatched` が空。
3. 再開 → 500 ms 待機 → `agent.dispatched` にアイテムが 1 つ。

**重要な理由:** TUI の `p` キーバインドはユーザーがエージェントにタスクを消費させずにキューの状態を検査するために一時停止/再開を使用します。

---

### `test_list_agents`

**テスト内容:** `orch.list_agents()` が登録済みすべてのエージェントの正しい ID を返すこと。

**セットアップ:** 2 つの DummyAgent を登録。

**アサーション:** 返された ID の集合が `{"agent-1", "agent-2"}` と等しい。

---

### `test_task_timeout_publishes_result`

**テスト内容:** タスクが `task_timeout` より長く実行された場合、`error: "timeout"` を含む `RESULT` メッセージがバスに公開されること。

**セットアップ:** `task_timeout=0.1` 秒の `SlowDummyAgent`。ブロードキャストサブスクライバーがすべてのメッセージをキャプチャ。タスクを送信して 500 ms スリープ。

**アサーション:** `payload["task_id"] == "t-timeout"` かつ `payload["error"] == "timeout"` を持つ RESULT メッセージが少なくとも 1 つキャプチャされている。

**重要な理由:** `run` CLI コマンドとすべての結果リスニングクライアントは、タスクが失敗したことを知るためにこの RESULT に依存します。これがないと永遠に待ち続けます。

---

### `test_task_timeout_agent_returns_to_idle`

**テスト内容:** タイムアウト後にエージェントのステータスが `BUSY` や `ERROR` ではなく `IDLE` であること。

**セットアップ:** 同じ `SlowDummyAgent`、`task_timeout=0.1` 秒、500 ms 待機。

**アサーション:** `agent.status == AgentStatus.IDLE`。

**重要な理由:** タイムアウト後に BUSY のままになったエージェントはそれ以降タスクを受け取れません。エージェントプールが機能し続けるために IDLE に戻ることが必要です。

---

### `test_agent_busy_event_published`

**テスト内容:** タスクの実行が開始されると、`agent_busy` STATUS イベントがバスに公開されること。

**セットアップ:** 通常の `DummyAgent`、ブロードキャストサブスクライバー、1 つのタスクを投入。

**アサーション:** 300 ms 以内に `event == "agent_busy"` かつ `agent_id == "ev-1"` を持つ STATUS メッセージが少なくとも 1 つ受信される。

**重要な理由:** TUI と WebSocket クライアントはポーリングなしでリアルタイムに UI を更新するためにこれらのイベントを使用します。イベントが欠落すると、エージェントが作業中でも IDLE と表示されたままになります。

---

### `test_agent_idle_event_published`

**テスト内容:** タスクが正常に完了した後、`agent_idle` STATUS イベントが公開されること。

**セットアップ:** 上記と同じ；タスク完了が期待された後、サブスクライバーのキューを排出。

**アサーション:** `event == "agent_idle"` かつ `agent_id == "ev-2"` を持つ STATUS メッセージが少なくとも 1 つ。

**重要な理由:** ライフサイクル通知ペアを完成させます。`agent_idle` がないと、タスクが完了した後も UI はエージェントを永遠に BUSY と表示します。

---

## 7. `test_tmux_interface.py` — tmux ラッパー（8テスト）

`tmux_orchestrator.tmux_interface` の `TmuxInterface` と `_hash` ヘルパーをテストします。tmux サーバーに触れるすべてのテストは `libtmux.Server` をモックします。

---

### `test_hash_deterministic`

`_hash("hello") == _hash("hello")` かつ `_hash("hello") != _hash("world")` をアサートします。監視対象ペインの追跡に使用されるペイン ID ハッシュの基本的な健全性確認。

### `test_hash_uses_md5`

stdlib を使って `"test content"` の期待される MD5 16 進ダイジェストを計算し、`_hash()` が同じ値を返すことをアサートします。動作の変更が検出されるよう、正確なアルゴリズムを固定します。

---

### `test_ensure_session_creates_new`

**シナリオ:** 名前に一致する既存のセッションがない。

**モックセットアップ:** `mock_server.find_where.return_value = None`。

**アサーション:** `mock_server.new_session` が `session_name="test-session"` で 1 回呼ばれ、返されたセッションオブジェクトが転送される。

---

### `test_ensure_session_kills_existing_and_creates_fresh`

**シナリオ:** 指定された名前のセッションがすでに存在し、ユーザーが強制終了を確認する。

**モックセットアップ:** `mock_server.find_where.return_value = existing_mock`；`TmuxInterface` を `confirm_kill=lambda _: True` で構築。

**アサーション:** `existing.kill_session()` が 1 回呼ばれる；次に `new_session` が呼ばれて新しいセッションが作成される；新しいセッションが返される。

---

### `test_ensure_session_aborts_when_user_declines`

**シナリオ:** 指定された名前のセッションがすでに存在するが、ユーザーが確認を拒否する。

**モックセットアップ:** `mock_server.find_where.return_value = existing_mock`；`TmuxInterface` を `confirm_kill=lambda _: False` で構築。

**アサーション:** `pytest.raises(RuntimeError, match="already exists")` — `RuntimeError` が発生する。`existing.kill_session()` は**呼ばれない**。

**重要な理由:** 既存セッションの誤った破壊を防ぎます。`confirm_kill` コールバックは `main.py` が `typer.confirm` を `default=False` で接続する方法であり、「中止」を安全なデフォルトにします。

---

### `test_watch_and_unwatch_pane`

`TmuxInterface` を作成し、`watch_pane(pane, "agent-1")` を呼び出し、次に `unwatch_pane(pane)` を呼び出します。

**アサーション:**
- `watch_pane` 後: `iface._watched` にペインの ID（`"%42"`）が含まれる。
- `unwatch_pane` 後: `iface._watched` にそれが含まれない。

監視レジストリが正しく維持されていることを検証します — バックグラウンドのウォッチャースレッドがどのペインをポーリングするかを知るために使用します。

---

### `test_send_keys_delegates`

`iface.send_keys(mock_pane, "echo hello")` を呼び出します。

**アサーション:** `mock_pane.send_keys` が `("echo hello", enter=True)` で呼ばれた。

`enter=True` フラグが常に渡されること（デフォルト）を確認します。コマンドを実行するために必要です。

---

### `test_capture_pane_joins_lines`

`mock_pane.capture_pane()` が `["line 1", "line 2", "line 3"]` を返します。

**アサーション:** `iface.capture_pane(pane)` が `"line 1\nline 2\nline 3"` を返す。

libtmux の行リスト形式を `_looks_done()` と `handle_output()` が期待する単一文字列に変換する改行結合ロジックを検証します。

---

## 8. `test_worktree.py` — Git Worktree マネージャー（9テスト）

`tmux_orchestrator.worktree` の `WorktreeManager` をテストします。すべてのテストは pytest の `tmp_path` 内に作成された**実際の git リポジトリ**を使用します。

### フィクスチャ

```python
@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("hello\n")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    return tmp_path
```

`git worktree add` はチェックアウト元となるコミットが少なくとも 1 つ必要なため、1 コミットを持つ実際の git リポジトリが必要です。

---

### `test_setup_creates_worktree`

`wm.setup("agent-1")` を呼び出してアサートします:
- 返されたパスがディスク上に存在する。
- パスが `{repo}/.worktrees/agent-1` と等しい。

**重要な理由:** マネージャーの最も基本的な契約。

---

### `test_setup_isolate_false_returns_repo_root`

`wm.setup("agent-2", isolate=False)` を呼び出してアサートします:
- 返されたパスがリポジトリルートと等しい（worktree ディレクトリは作成されない）。
- `.worktrees/agent-2/` が存在しない。

**重要な理由:** 設定の `isolate: false` オプトアウトが不要な worktree ディレクトリを作成してはなりません。

---

### `test_teardown_removes_worktree_and_branch`

`"agent-3"` をセットアップし、パスが存在することをアサートし、`teardown("agent-3")` を呼び出します。

**アサーション:**
- パスが存在しなくなる。
- `git branch --list worktree/agent-3` が空の出力を返す。

ファイルシステムと git の両方の状態がクリーンアップされていることを確認するために直接 `git` サブプロセス呼び出しを使用します。

---

### `test_teardown_shared_is_noop`

`isolate=False` で `"agent-4"` をセットアップし、git ブランチリストを記録し、`teardown("agent-4")` を呼び出し、ブランチリストを比較します。

**アサーション:** ブランチリストが変わらない — git 操作は実行されなかった。

**重要な理由:** 共有エージェントの `teardown` が誤ってブランチを削除してはなりません。

---

### `test_gitignore_entry_added`

マネージャーの初期化前に `.gitignore` が存在しないことをアサートします。`WorktreeManager(git_repo)` 後:
- `.gitignore` が存在する。
- `".worktrees/"` がその中の 1 行になっている。

---

### `test_gitignore_not_duplicated`

同じリポジトリで `WorktreeManager` を 2 回初期化します。`.gitignore` を読み取ります。

**アサーション:** `lines.count(".worktrees/") == 1` — 冪等性。

**重要な理由:** オーケストレーターが繰り返し再起動されたときに `.gitignore` が肥大化しないようにします。

---

### `test_not_in_git_repo_raises`

`.git` ディレクトリのない `tmp_path` を渡します。

**アサーション:** `"Not inside a git repository"` というメッセージの `RuntimeError` が発生する。

**重要な理由:** `main.py` のフォールバックパスは worktree 隔離を優雅に無効化するためにこの正確な例外タイプに依存しています。

---

### `test_worktree_path_before_setup_returns_none`

マネージャーを初期化し、`setup()` を呼ばずに `wm.worktree_path("nonexistent-agent")` を呼び出します。

**アサーション:** `None` が返される。

**重要な理由:** オーケストレーターは `share_parent_worktree` のために `_spawn_subagent` 内で `parent_agent.worktree_path` を呼び出します。`None` の結果は安全に処理される必要があります。

---

### `test_duplicate_setup_cleaned_and_recreated`

`wm.setup("agent-5")` を 2 回呼び出します。

**アサーション:**
- 最初のセットアップ後に `path1.exists()`。
- `path2 == path1`（同じ場所）。
- 2 番目のセットアップ後に `path2.exists()`（クリーンアップして再作成された）。
- `teardown("agent-5")` 後に `path2` が存在しない。

**重要な理由:** クラッシュリカバリ — エージェントが `stop()` を呼ばずにクラッシュした場合、次の起動時に前回の実行からの古い worktree がエラーなしでクリーンアップされる必要があります。

---

## 9. テストインフラストラクチャ

### `pyproject.toml` の設定

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- `asyncio_mode = "auto"` — すべての `async def test_*` 関数が `asyncio.run` で自動的に実行される。明示的な `@pytest.mark.asyncio` は不要。
- `testpaths` — 検出を `tests/` に制限し、サンプルスクリプトが誤って収集されることを防ぐ。

### `conftest.py`

存在しません。すべてのフィクスチャは各テストモジュールにローカルに定義されており、各ファイルが自己完結しています。

### 非同期タイミング

いくつかのオーケストレーターテストは、バックグラウンドの配信ループ（200 ms サイクル）またはルーティングループを待つために `await asyncio.sleep(N)` を使用しています。スリープの詳細:

| テスト | スリープ | 理由 |
|---|---|---|
| `test_submit_and_dispatch` | 300 ms | 配信ループは 200 ms ごとにポーリング |
| `test_no_idle_agent_requeues` | 300 ms | タスクが配信されないことを確認 |
| `test_p2p_allowed` | 100 ms | ルーティングループがメッセージを処理する |
| `test_p2p_blocked` | 100 ms | 配信されないことを確認 |
| `test_pause_and_resume` | 300 ms + 500 ms | 一時停止の確認、再開後の確認 |

---

## 10. カバレッジとギャップ

### カバーされている内容

| モジュール | カバレッジ |
|---|---|
| `bus.py` | すべての公開メソッド；キュー満杯のエッジケース |
| `messaging.py` | すべての CRUD 操作；ファイル未存在のエッジケース |
| `orchestrator.py` | 配信、再キュー、P2P 許可/ブロック、一時停止/再開、一覧、タスクタイムアウト、ステータスイベント |
| `tmux_interface.py` | セッション作成/既存セッション強制終了・新規作成/ユーザー拒否時中止、監視/監視解除、send_keys、キャプチャ、ハッシュ |
| `worktree.py` | セットアップ、削除、isolate=false、.gitignore、git 外、重複セットアップ |
| `agents/base.py` | タイムアウト強制、ステータスイベント |

### 既知のギャップ（未テスト）

| 領域 | 理由 |
|---|---|
| `ClaudeCodeAgent` エンドツーエンド | ライブの tmux セッションと `claude` バイナリが必要 |
| `_patch_web_url`（main.py） | ClaudeCodeAgent の属性をモックするテストが必要；未追加 |
| TUI ウィジェット（`tui/`） | Textual はディスプレイが必要；手動テスト |
| Web サーバー（`web/`） | FastAPI TestClient でカバーできる；未追加 |
| サブエージェント生成の統合 | 手動テストでカバー；ユニットテストは DummyAgent のみ使用 |
| `_message_loop` の並行性 | 基本パスはカバー；エッジケース（キューのキャンセル）はカバーされていない |

---

## 11. 新しいテストの追加方法

### 非同期テスト

```python
# tests/test_my_feature.py
import asyncio
import pytest
from tmux_orchestrator.bus import Bus, Message, MessageType

async def test_my_feature() -> None:
    bus = Bus()
    q = await bus.subscribe("agent-x")
    await bus.publish(Message(type=MessageType.STATUS, from_id="src", payload={}))
    msg = q.get_nowait()
    assert msg.from_id == "src"
```

デコレーターは不要です — `asyncio_mode = "auto"` が処理します。

### 実 git リポジトリを使ったテスト

```python
import subprocess
from pathlib import Path
import pytest
from tmux_orchestrator.worktree import WorktreeManager

@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path

def test_my_worktree_feature(git_repo: Path) -> None:
    wm = WorktreeManager(git_repo)
    path = wm.setup("my-agent")
    assert path.exists()
    wm.teardown("my-agent")
    assert not path.exists()
```

### DummyAgent を使ったテスト

```python
from tmux_orchestrator.agents.base import Agent, AgentStatus, Task
from tmux_orchestrator.bus import Bus
from tmux_orchestrator.config import OrchestratorConfig
from tmux_orchestrator.orchestrator import Orchestrator
from unittest.mock import MagicMock
import asyncio

class DummyAgent(Agent):
    def __init__(self, agent_id, bus):
        super().__init__(agent_id, bus)
        self.dispatched = []

    async def start(self):
        self.status = AgentStatus.IDLE
        self._run_task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self.status = AgentStatus.STOPPED
        if self._run_task:
            self._run_task.cancel()

    async def _dispatch_task(self, task):
        self.dispatched.append(task)
        self._set_idle()

    async def handle_output(self, text): pass
    async def notify_stdin(self, n): pass

async def test_my_orchestrator_feature() -> None:
    bus = Bus()
    tmux = MagicMock()
    config = OrchestratorConfig(session_name="test", agents=[], p2p_permissions=[])
    orch = Orchestrator(bus=bus, tmux=tmux, config=config)
    agent = DummyAgent("a1", bus)
    orch.register_agent(agent)
    await orch.start()
    try:
        # ... テストロジック
        pass
    finally:
        await orch.stop()
```
