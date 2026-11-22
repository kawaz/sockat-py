# SEQPACKET を使う

> [English](./seqpacket.md) | 日本語

SEQPACKET の関数は無い。[パスだけを解決する (resolve)](./resolve-ja.md) で作ったパスを、自分で作った `socket.socket(AF_UNIX, SOCK_SEQPACKET)` に渡せばよい。待ち受ける (bind する) 側は `resolve_for_bind`、つなぐ (connect する) 側は `resolve_for_connect` を使う。macOS では標準が SEQPACKET のソケットを作れない。
