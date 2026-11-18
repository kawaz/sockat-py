# AGENTS.md — sockat-py の作業ルール

sockat-py は、`sun_path` の上限を超えるパスでも Unix domain socket を bind / connect できる Python のライブラリ (PyPI の `sockat`)。短いパスは標準 (`socket` モジュール) にそのまま渡し、長いパスだけを OS ごとの経路で短い綴りにして渡す。

言語によらない振る舞いの正本はハブの [sockat の仕様](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md) (kawaz/sockat) で、sockat-py はその Python 実装。基準の実装は Rust 版 [sockat-rs](https://github.com/kawaz/sockat-rs)。

## 必読

- [docs/decisions/DR-0001](docs/decisions/DR-0001-python-port-design.md) — Python 版で作る経路と作らない経路、公開 API、標準に揃える点、エラーの型
- [docs/MANUAL-ja.md](docs/MANUAL-ja.md) — 利用者に約束している振る舞い (標準と違う点、エラー)

## 構成

| パス | 中身 |
|---|---|
| `src/sockat/__init__.py` | 公開 API (`bind` / `connect` / `connect_nonblocking`) |
| `src/sockat/_route.py` | 経路の順番と各経路、試験から差し替える `Config` |
| `src/sockat/_path.py` | パスの分割・相対パスの計算 (ファイルシステムに触らない) |
| `src/sockat/_sys.py` | ctypes の境界。`socket` モジュールで呼べないシステムコールはここだけ |
| `tests/` | 経路を強制する試験と公開 API・asyncio の試験 |

## 守ること

- **振る舞いを変える時は、先にハブの SPEC を直す。** sockat-py の docs と実装はその後に追従させる
- **ctypes は `src/sockat/_sys.py` にだけ書く。** 最初に要った時に import する (Linux は読み込まない)
- **標準が受け付けるパス (OS ごとの `sun_path` − 1 バイト以内) には何も足さない。** `socket` モジュールをそのまま呼ぶ
- **どの経路のソケットも `socket.socket(AF_UNIX, SOCK_STREAM)` で作る。** 標準と同じ設定にするため
- **経路の本当の失敗は次の経路でやり直さない。** 次へ進むのは経路自体が使えない時だけ
- **プロセスの cwd を変えない。** cwd を移すのはスレッド cwd の経路の使い捨てのスレッドだけ
- **経路は公開 API に出さない。** 試験はパッケージの中で各経路の関数を呼ぶ
- 公開 API・docstring・エラーの文言は英語。DR と日本語の docs は日本語

## 試験

- `just test` (macOS)、`just test-linux` (docker で Linux。Python 3.11 / 3.15 × seccomp の既定 / unconfined)。経路は OS で違うので、変更したら両方で回す
- `just check` は ruff と、OS ごとの分岐を platform ごとに見る mypy (strict)
- 一時ディレクトリはランダム名で作り、試験の終わりに消す。長さを決めたいので、基本は `/tmp` の下に作る
- cwd・環境変数・シグナルのようにプロセス全体に効くものを変える試験は、子プロセスの中で行う
- 各試験の直前に「何を確かめ、なぜそうなるべきか」をコメントで書く

## VCS と push

- jj colocate。commit は `jj commit -m "..." <files...>` でパスを指定する
- push は `just push` だけ (lint・試験・翻訳ペアの鮮度・version の上げ忘れを検査してから push する)
- README と docs/MANUAL は日本語版 (`*-ja.md`) が正本。英語版は日本語版を直した同じ変更で直す
- `src/` を変えたら `just bump-version` で version を上げる。pyproject.toml の version が上がった push で release.yml が CI を通してから PyPI に publish し、その後に tag を打つ
