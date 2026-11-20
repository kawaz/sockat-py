"""Errors of sockat's own (the SPEC's "general error"), not exported."""

from __future__ import annotations


class CwdChanged(OSError):
    """The process's cwd changed during a call that went through a path
    relative to it: a connect through the relative route
    (:meth:`sockat._routes.Relative.run`), or a bind of a relative path whose
    socket file :func:`sockat.ex.bind` remembers
    (:meth:`sockat._socket_file.Recorder.bound`). Callers see an
    :class:`OSError` without an errno."""
