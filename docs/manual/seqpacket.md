# Using SEQPACKET

> English | [日本語](./seqpacket-ja.md)

There are no SEQPACKET functions. Pass the path [Resolving just the path (resolve)](./resolve.md) makes to a `socket.socket(AF_UNIX, SOCK_SEQPACKET)` you make yourself. On macOS the standard library cannot make a SEQPACKET socket.

- The listening (binding) side: pass the path `resolve_for_bind` makes to bind ([Listen](./resolve.md#listen))
- The connecting side: pass the path `resolve_for_connect` makes to connect ([Connect](./resolve.md#connect))
