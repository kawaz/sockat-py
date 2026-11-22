# Using SEQPACKET

> English | [日本語](./seqpacket-ja.md)

There are no SEQPACKET functions. Pass the path [Resolving just the path (resolve)](./resolve.md) makes to a `socket.socket(AF_UNIX, SOCK_SEQPACKET)` you make yourself. The listening (binding) side uses `resolve_for_bind`, the connecting side `resolve_for_connect`. On macOS the standard library cannot make a SEQPACKET socket.
