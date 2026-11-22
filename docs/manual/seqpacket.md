# Using SEQPACKET

> English | [日本語](./seqpacket-ja.md)

There are no SEQPACKET functions. Pass the path [Resolving just the path (resolve)](./resolve.md) makes to a socket made with `SOCK_SEQPACKET` ([Use it with SEQPACKET](./resolve.md#use-it-with-seqpacket)). It cannot be used on macOS, where the standard library cannot make a SEQPACKET socket.
