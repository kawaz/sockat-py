# macOS の CPython が広げた sockaddr のアドレスに無関係なバイトを付ける原因

## 現象

macOS で、広げた sockaddr (パス 104 バイト超) で bind したソケットの `getsockname()` / `getpeername()` が、パスの先頭 104 バイトの後ろに実行ごとに違うバイトを付けて返す。例外にはならない。

| Python | 結果の長さ (5 回の実行から) | 後ろに付いたバイトの例 |
|---|---|---|
| 3.15.2 | 107 | `\xcbm\x01`、`\x0em\x01`、`Om\x01` (実行ごとに違う) |
| 3.13.4 | 107 / 104 (実行による) | `\x8c\x06\x01` |
| 3.11.3 | 104 / 107 (実行による) | (無し) |

## 原因の当たり (CPython 側)

`Modules/socketmodule.c` の 3 か所の組み合わせ。3.11 / 3.13 / 3.14 / main で該当箇所は同じ (makesockaddr の AF_UNIX の節は 4 版で一致)。

| 箇所 | 中身 |
|---|---|
| `getsockaddrlen` | AF_UNIX のバッファ長を `sizeof(struct sockaddr_un)` (macOS で 106) にする |
| `getsockname` / `getpeername` の実装 | `sock_addr_t addrbuf` (union、`sockaddr_storage` の 128 バイト以上) のうち、`memset` で 0 にするのは先頭の `addrlen` (106) バイトだけ |
| `makesockaddr` の AF_UNIX の節 | Linux 以外では、カーネルが返した `addrlen` を使わず、`PyUnicode_DecodeFSDefault(a->sun_path)` で `sun_path` を NUL 終端の文字列として読む |

広げた sockaddr のアドレスは NUL 無しで 104 バイトを超えるので、カーネルは 106 バイトのバッファを `sun_path` の 104 バイトまで NUL 無しで埋める。CPython は `sun_path` の配列の外 (union の 106 バイト目以降、0 にしていない領域) の次の NUL まで読む。付くバイトが実行ごとに違うのは、その領域が初期化されていないスタックだから。

## 実測 (macOS 26、ctypes で `getsockname(2)` を直接呼ぶ)

パス 228 バイトを広げた sockaddr で bind したソケット。

| 渡したバッファ | カーネルが返す `addrlen` | バッファ内の NUL | 中身 |
|---|---|---|---|
| 106 バイト | 230 | 無し | 先頭 104 バイトが元のパスと一致 |
| 128 バイト | 230 | 無し | 同上 |
| 256 バイト | 230 | 230 バイト目 | パス全体 (228 バイト) |

- XNU は切り詰めた時も、`addrlen` に切り詰める前の長さ (230) を返す
- CPython は返った `addrlen` (230) をバッファ長 (106) で抑えずに `makesockaddr` に渡し、Linux 以外ではそれも使わずに NUL を探す

## 整理

- CPython 側の不具合: 長さを返った `addrlen` (バッファ長で抑えたもの) でなく NUL で決め、初期化されていない領域を読む
- sockat の側では直せない (戻り値は標準の `socket.socket` で、アドレスを読むのは標準の `getsockname`)。sockat-py の MANUAL に「後ろに無関係なバイトが付くことがある」と書き、試験は「取得で例外にならない・先頭が綴りの前方と一致」だけを見る
- 未確認: Linux で、他のプログラムが NUL 無しの 108 バイトのパスで bind したソケットに connect / accept した時に同じことが起きるか (CPython 自身はそのパスを bind で断る)
- 2026-11-18 に security@python.org へ報告した (CPython の security policy に従い非公開で。公開の issue gh-52619 と同じ根の macOS 版として)
