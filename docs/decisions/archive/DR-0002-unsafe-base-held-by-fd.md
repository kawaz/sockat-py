# DR-0002: proc-held (`/proc` を使える Linux の connect の経路)

- Status: Superseded by [DR-0001](../DR-0001-python-port-design.md) (ハブの DR-0017 の作り直し)
- Date: 2026-11-19

## Context

- ハブの [DR-0012](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0012-own-directory-and-thread-cwd.md) の connect の順は、`/proc` を使える Linux / Android で proc-direct → thread-cwd → proc → proc-held → symlink-file
- proc-held は、proc-direct の綴りに収まらず、thread-cwd も proc も使えない (seccomp の filter の下でファイル名が proc に収まらない等) connect を、ファイル名の長さによらず届かせる

## Decision

| 手順 | Python での作り方 |
|---|---|
| D を作る | 前の thread-cwd・proc が作った D があればそれを使う。無ければ D を通す経路と同じ置き場の順に作る (作る・開く・確かめる・0700 にそろえる、[DR-0004](DR-0004-own-directory-route.md) の決定 2 と決定 5)。置き場の安全は問わない |
| symlink | `os.symlink(<ソケットのファイルのパス>, "s", dir_fd=<D>)`。相対パスなら cwd を前に付ける |
| connect | `/proc/self/fd/<D>/s` を標準の `connect` に渡す |
| 片付け | 自分が作れた `s` だけを `os.unlink("s", dir_fd=<D>)`。D は connect の最後に D を通す経路と同じ条件で `os.rmdir`。成功でも失敗でもする |

- 使うのは、ソケットのディレクトリの fd の `/proc/self/fd/<fd>` を引ける Linux / Android の connect 系だけ。bind は使わない
- `s` の先はソケットのファイルのパスそのもの (realpath にしない) なので、カーネルは元のパスと同じに解決する
- D をどこにも作れない時、`s` を作れない時は使えないとして symlink-file の経路に進む。D をどこにも作れなかったことが前の経路で分かっていれば、作り直さない

## Alternatives Considered

- 案 A: 置き場の安全を確かめ、安全な置き場には symlink-file、安全でない置き場にだけこの形を使う
  - 不採用理由: D を fd で掴めば置き場の安全は関係しない (ハブの DR-0012)。処理と試験が増えるだけ
- 案 B: D の中に R (ソケットのディレクトリを指す symlink) を作って `/proc/self/fd/<D>/R/<ファイル名>` で connect する
  - 不採用理由: それは D を通す経路の proc で、ファイル名が収まらない時にここへ来る。ソケットのファイルを指す `s` なら綴りがファイル名の長さによらない

## Consequences

- `/proc` があり D をどこかに作れれば、Linux の connect はファイル名の長さによらず届く
- 途中でプロセスが死ぬと、D (`sockat.XXXXXXXXXX`、ソケットのディレクトリでは `.sockat.XXXXXXXXXX`) が残る

## 関連

- [DR-0001](../DR-0001-python-port-design.md)、[DR-0004](DR-0004-own-directory-route.md)
- [ハブの DR-0012](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0012-own-directory-and-thread-cwd.md)
