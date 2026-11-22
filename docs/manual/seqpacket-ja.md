# SEQPACKET を使う

> [English](./seqpacket.md) | 日本語

SEQPACKET の関数は無い。[パスだけを解決する (resolve)](./resolve-ja.md) で作ったパスを、`SOCK_SEQPACKET` で作ったソケットに渡せばよい ([SEQPACKET で使う](./resolve-ja.md#seqpacket-で使う))。macOS では標準が SEQPACKET のソケットを作れないので使えない。
