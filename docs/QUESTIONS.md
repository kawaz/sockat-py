# 裁定・確認待ち一覧 (ユーザ用)

## 運用規約

<details>
<summary>ゼロコンテキストエージェント向け（本セクションは消さない）</summary>

- 裁定/確認待ち項目を 1項目=1ラベル=1セクション で記載
- ラベル形式: XX-Q1（XX は 2-3 文字、バッチやセッション内で一意、Qn単独の使い回し禁止、長期一意性は不要)
- 依頼形式: 「👺XX-Q1 の裁定お願いします」（参照用途ではラベルに👺を付けない。誤陽性がユーザのハイライト/アラームを汚す）
- チャット提示と同一ターンで本ファイルに記録 + path 指定 commit (push はリリース窓に同乗)
- 裁定が下りたら該当セクションを即削除し、内容は正規の記録先 (DR / issue / journal / close_reason) へ反映。本ファイルは常に「現在待ち」だけを持つ
- 参照は[]()で提示（リポ内は相対、リポ外はフルパス）
- 初版質問/依頼は長文で書かない（ユーザが説明を求めらたら本ファイルに説明を追加し、チャットで👺ラベルで再依頼）
- **選択肢・確認項目は `- [ ] a: …` 形式（チェックボックス + ラベル）で書く**。Q / C で記法を分けない。回答は「チェックを付ける」でも「XX-Q1a」と言葉で返すでも通る（複数まとめてチェックし「チェックしたよ」の一言で済ませる運用を想定）

</details>

## 裁定待ち

### 👺PY-Q1: fork の経路のために C 拡張を入れるか

- [ ] a (推奨): 入れない。純 Python + ctypes、fork の経路なし、`connect_any` 系を出さない。wheel は `py3-none-any` 1 つ
- [ ] b: 小さな C 拡張で fork の経路を作り、`connect_any` 系も既定で import されない別のモジュール (例: `sockat.any`) に出す。OS × arch ごとの wheel (cibuildwheel)、他の Unix と PyPy は sdist からコンパイル

fork の経路まで進むのは、スレッド cwd の経路 (Linux / macOS) が使えない環境 (NetBSD / OpenBSD / illumos、`unshare` を断る seccomp の下の /proc の無い Linux) で、symlink も使えない時だけ。a から b への移行は機能の追加で済む (経路の本体は Python のまま)。

#### 背景説明

- `os.fork()` を Python から呼ぶ形は、子が Python を実行するので SPEC の「子の区間は async-signal-safe」を満たせない
- `sys.executable` を fork+exec して子に bind させる形は、SPEC の仕組みから外れ、PyInstaller 等で固めたアプリでは `sys.executable` がアプリ本体を指す
- PyO3 で sockat-rs を包む形は、Rust 標準のソケット設定 (`SO_NOSIGPIPE`、backlog) になり Python 標準と揃わず、経路を Python 側の試験から強制できない
