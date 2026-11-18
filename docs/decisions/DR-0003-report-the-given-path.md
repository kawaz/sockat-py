# DR-0003: 長いパスのソケットはアドレスを元のパスで返す

- Status: Accepted (kawaz 裁定 2026-11-19。短いパスの扱いは統括の判断)
- Date: 2026-11-19

## Context

- 長いパスでは、カーネルが返すアドレス (`getsockname(2)` / `getpeername(2)`) は bind に使った綴りで、元のパスではない (`/proc/self/fd/<fd>/<ファイル名>`、相対パス、もう無い symlink、ファイル名だけ)
- Python 3.13 以降の asyncio の `start_unix_server(sock=)` は、既定の `cleanup_socket=True` で、開始時と閉じる時に listener の `getsockname()` を `os.stat` し、閉じる時に inode が一致すれば消す。長いパスの綴りでは次のように壊れる (実測)
  - /proc の綴り: 綴りの fd は bind の後に閉じていて、イベントループの epoll 等が同じ番号を使い回す。`sockat.bind(p)` をループより先に呼ぶと、`start_unix_server` が `NotADirectoryError` で必ず落ちる (Linux、Python 3.13 / 3.15)
  - スレッド cwd の綴り: ファイル名だけなので、プロセスの cwd に同じ名前のファイルがあると閉じる時にそれを消す
- 利用者は `server = await asyncio.start_unix_server(handle, sock=sockat.bind(PATH))` と書き、引数を足さずに短いパスと同じに動く (閉じた時に元のパスのソケットファイルが消える) ことを期待している (kawaz)

## Decision

| 項目 | 決定 |
|---|---|
| 型 | 長いパスの経路のソケットは `socket.socket` の subclass (`sockat._sockets.PathSocket`)。`isinstance(x, socket.socket)` は真 |
| listener | `getsockname()` が元のパス |
| `accept()` が返すソケット | `PathSocket` で、`getsockname()` が listener の元のパス。`getpeername()` は標準のまま |
| connect したソケット | `getpeername()` が元のパス。`getsockname()` は標準のまま |
| 元のパスの型 | `os.fsdecode` した `str` (標準がパスのアドレスを返すのと同じ型)。相対パスで渡したら相対パスのまま |
| `dup()` | 元のパスを引き継ぐ |
| 標準が失敗する時 | 先にカーネルに聞き、標準と同じに失敗する (閉じたソケットの `EBADF`、相手が閉じた後の `getpeername` の `EINVAL`、listener の `getpeername` の `ENOTCONN`)。通った時だけ元のパスに差し替える |
| 短いパス (標準が受け付ける長さ、Linux の先頭 NUL) | 素の `socket.socket` のまま (統括の判断) |
| 公開 API | 型は公開しない。関数の戻り値の注釈は `socket.socket` のまま |

- `accept()` は標準の `accept()` が作った `socket.socket` の fd を `detach()` して `PathSocket` に移し、標準と同じ blocking の規則 (bpo-7995: 既定の timeout が無く listener に timeout があれば blocking) を当てる
- asyncio の selector のループは `sock.accept()` と `getsockname()` / `getpeername()` を呼ぶので、`get_extra_info('sockname')` / `('peername')` も元のパスになる

理由:

- 短いパスと同じ書き方で同じに動くことが、標準の代わりに置き換えるだけで使える、という約束の中身。上位が読み返すアドレスが元のパスなら、後始末やログはそのまま正しく動く
- 短いパスで素の `socket.socket` を返すのは、標準が受け付けるパスには何も足さない (DR-0001 決定 3) を守るため。短いパスでは標準の `getsockname` が元のパスを返すので、振る舞いは同じ

## Alternatives Considered

- 案 A: 文書で `cleanup_socket=False` を案内し、ソケットファイルは利用者が消す
  - 不採用理由: 利用者に回避を強いる。案内を読まない利用者は Linux で落ちる (kawaz 裁定)
- 案 B: bind で、カーネルの綴りがそのまま使える経路 (相対パス、symlink) を先に試す
  - 不採用理由: /proc やスレッド cwd でしか届かないパスは残る。相対パスは cwd が変わると別の場所を指す
- 案 C: 短いパスも `PathSocket` で返す
  - 不採用理由: 振る舞いは同じだが、標準が受け付けるパスに何も足さない決まりに反する

## Consequences

- `type(x) is socket.socket` を見るコードは、長いパスでは偽になる (`isinstance` を使う)
- `socket.fromfd` 等で fd から作り直したソケットは標準の型なので、カーネルの綴りを返す
- uvloop のように fd を直接扱うループは、`getsockname()` を Python から呼ばない限り元のパスを見ない (未確認)

## 関連

- [DR-0001](DR-0001-python-port-design.md)
- [ハブの SPEC](https://github.com/kawaz/sockat/blob/main/docs/SPEC-ja.md)
