# APIキー配送のセキュリティ方針

> DESIGN.md §3 から分離。実装: v0.34.x (フェーズ1), v0.35.x (フェーズ2)。

**現状（v1.2.x）**: フェーズ1・フェーズ2は実装済み。
- `__orchestrator_api_key__{agent_id}__` ファイル（chmod 600）で分離
- `libtmux session.set_environment('TMUX_ORCHESTRATOR_API_KEY', ...)` で環境変数注入

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
