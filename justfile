# sockat-py justfile
#
# version の正本は pyproject.toml の project.version で、push 時に
# check-version-bumped が「src/ を変えたのに version が据え置き」を止める。
# README / docs の利用者向けの文書は ja が正本、en が翻訳先。push 時に
# check-outdated-translations が「en が ja より古い」を止める。
# VCS 操作は bump-semver vcs に委譲して jj / git を透過にする。

set shell := ["bash", "-euo", "pipefail", "-c"]

set script-interpreter := ["bash", "-euo", "pipefail"]

set positional-arguments

# default: list
default: list

# show recipes
list:
    @just --list --unsorted

# ---------- lint / test ----------

# ruff (lint + format の検査) と mypy (strict、OS ごとの分岐を platform ごとに)
check:
    uv run ruff check
    uv run ruff format --check
    for p in darwin linux freebsd netbsd; do uv run mypy --platform "$p"; done

# ruff で書き換える (format と自動修正)
fmt:
    uv run ruff check --fix
    uv run ruff format

# 試験 (この OS の経路)
test *ARGS: check
    uv run pytest "$@"

# Linux の試験を docker で回す (macOS の手元から Linux の経路を確かめる)
#
# 手元の .venv は macOS 用なので使わず、コンテナの中で pytest を /tmp/deps に入れる。
# リポは読み取り専用で mount し、bytecode も書かせない。docker の既定の seccomp は
# unshare(2) を EPERM で断る (スレッド cwd の経路が使えない側) ので、
# seccomp=unconfined (使える側) と両方で回す。どちらの側かは
# SOCKAT_TEST_THREAD_CWD で試験に伝え、試験の探りと食い違ったら落とす (探りが壊れて
# 「使えない側」の試験だけが走るのを防ぐ)。root は権限の検査を素通りするので、読み取り
# 権限の無いディレクトリの試験は nobody (uid 65534) の回でだけ走る。
[doc("Linux の試験を docker で回す (python 3.11 と最新 × seccomp の既定 / unconfined、unconfined は root と nobody)")]
[script]
test-linux *ARGS:
    for py in 3.11 3.15; do
      for run in default:root unconfined:root unconfined:nobody; do
        sec=${run%%:*}
        user=${run##*:}
        echo "== python:${py}-slim, seccomp=${sec}, user=${user}"
        opt=()
        thread=0
        if [ "$sec" = unconfined ]; then
          opt=(--security-opt seccomp=unconfined)
          thread=1
        fi
        uid=0:0
        [ "$user" = nobody ] && uid=65534:65534
        docker run --rm "${opt[@]}" --user "$uid" -v "$PWD":/work:ro -w /work \
          -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e PYTHONPATH=/work/src:/tmp/deps \
          -e SOCKAT_TEST_THREAD_CWD="$thread" \
          "python:${py}-slim" sh -c 'pip install -q --root-user-action=ignore --disable-pip-version-check --target /tmp/deps pytest && python -m pytest -q -p no:cacheprovider "$@"' sh "$@"
      done
    done

# ハブの適合試験 (docs/design/conformance.md) の試験用 CLI を起こす (標準入力に JSON 1 つ、標準出力に結果)
#
# 場面ごとの cwd・環境変数・umask はハブのスクリプトが整えるので、cwd を変えずに (no-cd)
# .venv の python で直に起こす。uv run を挟むと uv が $TMPDIR に uv-*.lock を残し、
# 場面の置き場を汚す ([script] にしないのも just が $TMPDIR に script を書くため)。
# .venv は uv sync (just check / uv run でも作られる) で用意しておく。
# CLI は src/ の外 (conformance/) にあり、wheel にも sdist にも入らない。
[no-cd]
conformance-cli:
    @[ -x {{quote(justfile_directory() / ".venv" / "bin" / "python")}} ] || { echo "conformance-cli: .venv が無い。先にリポで uv sync を" >&2; exit 2; }
    @exec {{quote(justfile_directory() / ".venv" / "bin" / "python")}} {{quote(justfile_directory() / "conformance" / "cli.py")}}

# check + test (CI entry point)
ci: check test

# wheel と sdist を dist/ に作る
build:
    uv build

# ---------- gates (push の内部) ----------

# uncommitted change がない状態か確認
[private]
ensure-clean:
    bump-semver vcs is clean

# 現在の bookmark/branch が default (= main) 上にあるか確認
[private]
[script]
check-on-default-branch:
    if ! bump-semver vcs is on-default-branch; then
        bn=$(bump-semver vcs get default-branch)
        printf >&2 "default branch (%s) に合流してから push してください\n  1. just sync         # %s@origin に rebase\n  2. just promote      # %s bookmark を current commit に forward\n" "$bn" "$bn" "$bn"
        exit 1
    fi

# 翻訳ペアの鮮度 (正本 = *-ja.md、翻訳先 = 同じ場所の *.md)
[private]
check-outdated-translations: ensure-clean
    bump-semver vcs outdated 'glob:**/*-ja.md' '$1/$2.md'

# src/ が main@origin から変わっているのに pyproject.toml の version が上がっていなければ fail
#
# 対象を src/ に絞るのは、パッケージの中身に影響しない変更 (docs / justfile / CI / tests) で
# version bump を強制しないため。
#
# bump-semver vcs diff の exit code:
#   0 = 対象 path に変更なし → bump 不要
#   1 = 変更あり → version bump 済みかチェックに進む
#   その他 = VCS error (main@origin 未 track 等)
#
# remote にまだ main が無い (= 初回の push) 時は比べる相手が無いので通す。
# git ls-remote --exit-code は ref が無いと 2 を返す。
[private]
[script]
check-version-bumped:
    lr=0
    git ls-remote --exit-code origin refs/heads/main >/dev/null || lr=$?
    if [ "$lr" = 2 ]; then
      echo "origin に main がまだ無い (初回の push) ので version の検査を飛ばす"
      exit 0
    fi
    rc=0
    bump-semver vcs diff -q main@origin -- src/ || rc=$?
    case "$rc" in
      0) exit 0 ;;
      1) ;;
      *) echo "ERROR: bump-semver vcs diff failed (rc=$rc). main@origin が track されていない可能性。先に 'jj git fetch' / 'git fetch' を試してください" >&2; exit 1 ;;
    esac
    bump-semver compare gt pyproject.toml vcs:main@origin:pyproject.toml --no-hint && exit 0
    echo 'ERROR: src/ が変わっているが pyproject.toml の version 未 bump。"just bump-version" を実行してください' >&2
    exit 1

# ---------- release flow ----------

# pyproject.toml の version を上げて (既定 patch)、uv.lock と一緒に Release commit を作る
[doc("version を上げて Release commit を作る")]
[script]
bump-version level="patch": ensure-clean
    new_version=$(bump-semver "$1" pyproject.toml --write --no-hint)
    uv lock
    bump-semver vcs commit -m "Release v${new_version}" pyproject.toml uv.lock
    echo "Version: -> ${new_version}"

# 現在の version を表示
version:
    @bump-semver get pyproject.toml --no-hint

# 現在の worktree を default branch (= origin/<default>) に rebase
sync:
    bump-semver vcs sync --onto $(bump-semver vcs get default-branch)@origin

# default branch を現在の commit に forward (push しない)
promote:
    bump-semver vcs promote

# push (gate を全部通してから)
push: ci check-on-default-branch ensure-clean check-outdated-translations check-version-bumped
    bump-semver vcs push --branch main --jj-bookmark-auto-advance
    ccmsg notify --self --text "Monitor で 'just watch' を起動して" 2>/dev/null || true

# push した commit の workflow (CI と Release) を SHA-pinned で watch する
#
# watch-workflow.sh は gh-monitor plugin の中にあり PATH に乗らないので、
# cache から最新版を探して使う。
[doc("push した commit の workflow (CI と Release) の完了を SHA-pinned watch する")]
[script]
watch:
    ws=$(ls "$HOME"/.claude-personal/plugins/cache/gh-monitor/gh-monitor/*/scripts/watch-workflow.sh 2>/dev/null | sort -V | tail -1)
    if [ -z "$ws" ]; then
        echo "watch-workflow.sh が見つかりません (gh-monitor plugin が未インストール?)" >&2
        exit 1
    fi
    "$ws" --sha "$(bump-semver vcs get commit-id --rev "$(bump-semver vcs get default-branch)")" kawaz/sockat-py
