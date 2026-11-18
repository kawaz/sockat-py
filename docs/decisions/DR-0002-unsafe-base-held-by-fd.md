# DR-0002: 安全でない置き場を fd で掴む形 (Linux の connect)

- Status: Accepted (ハブの DR-0008 の Python での作り方。統括の判断 2026-11-18)
- Date: 2026-11-18

## Context

- ハブの [DR-0008](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0008-unsafe-base-held-by-fd.md) が、`/proc` を使える Linux / Android の connect で、symlink の置き場が安全でない時にそこに自分専用のディレクトリを作り、fd で辿る形を足した
- 発端は、seccomp の filter があり (スレッド cwd の経路が使えない)、`TMPDIR` も `/tmp` も安全でなく、ファイル名が /proc の綴りに収まらない時に connect が届かないこと

## Decision

| 手順 | Python での作り方 |
|---|---|
| /proc を確かめる | 何かを作る前に、置き場を `O_PATH \| O_DIRECTORY` で開いた fd の `/proc/self/fd/<fd>` を引けるか確かめる。引けなければ何も作らず次の候補へ |
| ディレクトリを作る | `os.mkdir(<置き場>/sockat.XXXXXXXXXX, 0o700)`。名前と衝突の扱いは symlink と同じ (`FileExistsError` で引き直し、16 回で打ち切り) |
| 開いて確かめる | `os.open(..., O_PATH \| O_DIRECTORY \| O_NOFOLLOW)` の fd を `os.fstat` し、ディレクトリ・持ち主が `os.geteuid()`・group と other に権限が無い (`mode & 0o077 == 0`) を確かめ、通ったら `os.chmod("/proc/self/fd/<fd>", 0o700)` で持ち主のビットをそろえる |
| symlink | `os.symlink(<元のパス>, "s", dir_fd=fd)` |
| connect | `/proc/self/fd/<fd>/s` を標準の `connect` に渡す |
| 片付け | 自分が作った `s` だけ `os.unlink("s", dir_fd=fd)` (差し替えられた他人のディレクトリの `s` は消さない)。ディレクトリは、確かめが通っていて、名前を `os.lstat` した `(st_dev, st_ino)` が fd と一致する時だけ `os.rmdir` (確かめに落ちたものは中身も含めて触らない)。fd を閉じる |

- 使うのは `/proc` を使える (ディレクトリの fd の `/proc/self/fd/<fd>` を引ける) Linux の connect 系だけ。`run_long` が /proc の経路のために開いたディレクトリの fd でこれを確かめ、`posix_run` に渡す
- 置き場の候補ごとに、安全なら symlink の経路、安全でなければこの形を試す (`TMPDIR` → `/tmp`)
- 開けなかった時は、名前の先が自分の作ったものか分からないので消さずに残す
- umask が持ち主の権限を削っても (0477 等で読み取りまで削っても)、`O_PATH` は読み取り権限なしで開け、確かめるのは group と other だけなので通り、0700 に戻す (umask の環境で毎回使えずに残骸が溜まらないように)。`O_PATH` の fd には `fchmod` ができないので、fd が指す実体に効く `/proc/self/fd/<fd>` の綴りで `chmod` する。`os.symlink` / `os.unlink` の `dir_fd` は `O_PATH` の fd で使える

## Alternatives Considered

- 案 A: 作った後に名前で `os.chmod` してから確かめる
  - 不採用理由: 作ってから chmod するまでの間に名前が差し替えられうる。mode を直すのは確かめた後の fd が指す実体に対して (`/proc/self/fd/<fd>`) 行う
- 案 B: `O_RDONLY` で開いて `fchmod` する
  - 不採用理由: umask が持ち主の読み取りまで削ると、非 root では開けず (`EACCES`)、作ったディレクトリが残る

## Consequences

- seccomp の filter の下でも、`/proc` があり置き場にディレクトリを作れれば、Linux の connect はファイル名の長さによらず届く
- 途中でプロセスが死ぬと `sockat.XXXXXXXXXX` のディレクトリが置き場に残る

## 関連

- [DR-0001](DR-0001-python-port-design.md)
- [ハブの DR-0008](https://github.com/kawaz/sockat/blob/main/docs/decisions/DR-0008-unsafe-base-held-by-fd.md)
