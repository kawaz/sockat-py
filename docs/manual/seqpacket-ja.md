# SEQPACKET を使う

> [English](./seqpacket.md) | 日本語

SEQPACKET の関数は無い。[パスだけを解決する (resolve)](./resolve-ja.md) で作ったパスを、自分で作った `socket.socket(AF_UNIX, SOCK_SEQPACKET)` に渡せばよい。macOS では標準が SEQPACKET のソケットを作れない。

- 待ち受ける (bind する) 側: `resolve_for_bind` で作ったパスを bind に渡す ([待ち受ける](./resolve-ja.md#待ち受ける))
- つなぐ (connect する) 側: `resolve_for_connect` で作ったパスを connect に渡す ([つなぐ](./resolve-ja.md#つなぐ))
