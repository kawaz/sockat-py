"""The path arithmetic of ``_path``: pure functions on the bytes of a path,
nothing touches the file system."""

from __future__ import annotations

import pytest

from sockat._path import below, components, relative, split


# `split` decides which directory a long path's socket is created in and under
# which name, for every route that does not hand the whole path to the kernel
# (the central route's `R/<name>`, /proc, the relative spelling's check,
# connectat). A wrong split creates the socket somewhere else
# than the path says, so each shape is pinned here.
@pytest.mark.parametrize(
    ("path", "directory", "name"),
    [
        # Ordinary absolute and relative paths.
        (b"/d/e/s.sock", b"/d/e", b"s.sock"),
        (b"d/s.sock", b"d", b"s.sock"),
        # A socket directly under the root keeps "/" as its directory (an
        # empty directory would open the cwd instead).
        (b"/s.sock", b"/", b"s.sock"),
        # A bare file name lives in the working directory.
        (b"s.sock", b".", b"s.sock"),
        # A doubled slash is left in the directory part; the kernel treats
        # "d/" and "d" alike, so the split does not need to normalise it.
        (b"d//s.sock", b"d/", b"s.sock"),
        # `..` inside the directory part is kept verbatim for the kernel to
        # resolve (normalising it lexically would be wrong across symlinks).
        (b"/a/../s.sock", b"/a/..", b"s.sock"),
    ],
)
def test_split_takes_the_bytes_after_the_last_slash(path: bytes, directory: bytes, name: bytes) -> None:
    assert split(path) == (directory, name)


# A path whose last segment is empty, "." or ".." names a directory, not a
# socket file. A normalising split (os.path.basename / pathlib) would map "d/"
# and "d/." to "d"; binding that would create a socket named after the
# directory in its parent, which is not what the path says. Refusing keeps long
# paths behaving like the kernel does for short ones (where such a bind fails),
# and the refusal is ValueError: an argument error, raised before anything is
# created.
@pytest.mark.parametrize("path", [b"d/", b"d/.", b"d/..", b"..", b".", b"/", b""])
def test_split_refuses_paths_that_do_not_end_with_a_file_name(path: bytes) -> None:
    with pytest.raises(ValueError, match="must end with a file name"):
        split(path)


# `components` is how the kernel walks an absolute path: empty segments
# (doubled slashes) and "." are not components, ".." is kept as written (it is
# not interpreted lexically, which would be wrong across symlinks).
def test_components_are_what_the_kernel_walks() -> None:
    assert components(b"/a/b") == [b"a", b"b"]
    assert components(b"/a//./b/") == [b"a", b"b"]
    assert components(b"/a/../b") == [b"a", b"..", b"b"]
    assert components(b"/") == []


# `below` cuts a spelling of the cwd off the front of the path. The cut is by
# components: a byte prefix would turn `/w/abc/s` with cwd `/w/ab` into `c/s`,
# which names `/w/ab/c/s`, a different file.
@pytest.mark.parametrize(
    ("target", "prefix", "want"),
    [
        # Below the cwd: the rest of the path, unchanged.
        (b"/w/a/s.sock", b"/w", b"a/s.sock"),
        # Directly in the cwd: the file name.
        (b"/w/s.sock", b"/w", b"s.sock"),
        # A trailing slash on the prefix is not a component, so it does not
        # stop the match.
        (b"/w/s.sock", b"/w/", b"s.sock"),
        # `/w/ab` is a byte prefix of `/w/abc/...` but not a component prefix.
        (b"/w/abc/s.sock", b"/w/ab", None),
        # Not below at all.
        (b"/x/s.sock", b"/w", None),
        # The path is the prefix itself: no file name left.
        (b"/w", b"/w", None),
        # Relative inputs have no defined relation to the cwd here.
        (b"w/s", b"/w", None),
        (b"/w/s", b"w", None),
    ],
)
def test_below_cuts_the_prefix_by_components(target: bytes, prefix: bytes, want: bytes | None) -> None:
    assert below(target, prefix) == want


# `relative` is what lets a long absolute path fit `sun_path` as a short
# relative one when the socket is near the working directory: climb with ".."
# out of the part of the cwd the target does not share, then descend. Each row
# pins one shape of the climb-then-descend spelling. The callers pass real
# paths (no symlinks), which is what makes the arithmetic correct.
@pytest.mark.parametrize(
    ("target", "cwd", "want"),
    [
        # Below the cwd: no climbing.
        (b"/w/a/s.sock", b"/w", b"a/s.sock"),
        # Directly in the cwd: just the file name.
        (b"/w/s.sock", b"/w", b"s.sock"),
        # A sibling subtree: climb one, descend.
        (b"/w/a/s.sock", b"/w/b", b"../a/s.sock"),
        # Nothing shared but the root: climb all the way up.
        (b"/x/s.sock", b"/w/b", b"../../x/s.sock"),
        # The cwd is the root.
        (b"/x/s.sock", b"/", b"x/s.sock"),
        # "." and doubled slashes in the target are not components, so they do
        # not stop the shared prefix.
        (b"/w/./a//s.sock", b"/w", b"a/s.sock"),
        # The arithmetic is purely by components: a ".." in the target is not
        # interpreted (callers pass real paths, which have none), it just ends
        # the shared prefix.
        (b"/w/../w/s.sock", b"/w", b"../w/s.sock"),
    ],
)
def test_relative_climbs_out_of_the_unshared_part_of_the_cwd(target: bytes, cwd: bytes, want: bytes) -> None:
    assert relative(target, cwd) == want


# Only absolute inputs have a well-defined relation to the cwd, and the result
# must still end in a file name.
@pytest.mark.parametrize(
    ("target", "cwd"),
    [
        # A relative target is already relative to the cwd.
        (b"a/s", b"/w"),
        # A relative cwd is not what getcwd(3) returns.
        (b"/a/s", b"w"),
        # The target is the cwd or an ancestor of it: nothing left to descend to.
        (b"/w", b"/w"),
        (b"/w", b"/w/x"),
    ],
)
def test_relative_is_none_when_it_cannot_name_a_file_below(target: bytes, cwd: bytes) -> None:
    assert relative(target, cwd) is None
