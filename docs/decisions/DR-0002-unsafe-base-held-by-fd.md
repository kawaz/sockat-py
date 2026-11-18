# DR-0002: 安全でない置き場を fd で掴む形 (Linux の connect)

- Status: Accepted (ハブの DR-0008 の Python での作り方。統括の判断 2026-11-18)
- Date: 2026-11-18

## Context

- ハブの [DR-0008](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0008-unsafe-base-held-by-fd.md) が、`/proc` を使える Linux / Android の connect で、symlink の置き場が安全でない時にそこに自分専用のディレクトリを作り、fd で辿る形を足した
- 発端は、seccomp の filter があり (スレッド cwd の経路が使えない)、`TMPDIR` も `/tmp` も安全でなく、ファイル名が /proc の綴りに収まらない時に connect が届かないこと

## Decision

| 手順 | Python での作り方 |
|---|---|
| ディレクトリを作る | `os.mkdir(<置き場>/sockat.XXXXXXXXXX, 0o700)`。名前と衝突の扱いは symlink と同じ (`FileExistsError` で引き直し、16 回で打ち切り) |
| 開いて確かめる | `os.open(..., O_RDONLY \| O_DIRECTORY \| O_NOFOLLOW)` の fd を `os.fstat` し、ディレクトリ・持ち主が `os.geteuid()`・`S_IMODE == 0o700` を確かめる |
| symlink | `os.symlink(<元のパス>, "s", dir_fd=fd)` |
| connect | `/proc/self/fd/<fd>/s` を標準の `connect` に渡す |
| 片付け | `os.unlink("s", dir_fd=fd)`。名前を `os.lstat` した `(st_dev, st_ino)` が fd と一致する時だけ `os.rmdir`。fd を閉じる |

- 使うのは `/proc` を使える (ディレクトリの fd の `/proc/self/fd/<fd>` を引ける) Linux の connect 系だけ。`run_long` が /proc の経路のために開いたディレクトリの fd でこれを確かめ、`posix_run` に渡す
- 置き場の候補ごとに、安全なら symlink の経路、安全でなければこの形を試す (`TMPDIR` → `/tmp`)
- 開けなかった時は、名前の先が自分の作ったものか分からないので消さずに残す
- umask が持ち主の権限を落とすと mode が 0700 にならず、確かめが通らないので使えないとみなす (作ったディレクトリは消す)

## Alternatives Considered

- 案 A: 作った後に `os.chmod` で 0700 にしてから確かめる
  - 不採用理由: 作ってから chmod するまでの間に名前が差し替えられうる。確かめは開いた fd の `fstat` で行い、umask の影響は使えない時として扱う

## Consequences

- seccomp の filter の下でも、`/proc` があり置き場にディレクトリを作れれば、Linux の connect はファイル名の長さによらず届く
- 途中でプロセスが死ぬと `sockat.XXXXXXXXXX` のディレクトリが置き場に残る

## 関連

- [DR-0001](DR-0001-python-port-design.md)
- [ハブの DR-0008](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0008-unsafe-base-held-by-fd.md)
