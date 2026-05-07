"""Per-task log buffering for concurrent asyncio jobs.

Wraps `sys.stdout` once with a context-aware proxy. When a worker enters
`async with job_log():`, every `print()` from that task (and its async
descendants) is captured into a per-task buffer and flushed as a single
contiguous block when the context exits. Concurrent workers running in
parallel asyncio tasks each get their own buffer, so their output no longer
interleaves line-by-line.

The proxy uses `contextvars`, which propagate correctly through asyncio
task boundaries: a child task started from inside `job_log` inherits the
buffer; sibling tasks created by the same `gather()` get independent ones.
"""

import contextlib
import contextvars
import io
import sys

# A None default means "no active job_log → write straight through to the
# real stdout"; that keeps top-level / non-worker prints behaving normally.
_log_buffer: contextvars.ContextVar = contextvars.ContextVar(
    "job_log_buffer", default=None,
)


class _ContextAwareStdout:
    """sys.stdout proxy: writes to the per-task buffer when one is set,
    otherwise to the real stdout. Forwarded attributes keep `print`'s
    occasional `file.fileno()` / `file.encoding` accesses working."""

    def __init__(self, real):
        self._real = real

    def write(self, s):
        buf = _log_buffer.get()
        if buf is not None:
            return buf.write(s)
        return self._real.write(s)

    def flush(self):
        # No-op when buffering — we flush at job-end. Pass through otherwise.
        if _log_buffer.get() is None:
            self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


_installed = False


def install() -> None:
    """Replace sys.stdout once with the context-aware proxy. Idempotent."""
    global _installed
    if _installed:
        return
    sys.stdout = _ContextAwareStdout(sys.stdout)
    _installed = True


@contextlib.asynccontextmanager
async def job_log():
    """Buffer this task's prints; emit them as one block on exit.

    Safe to nest: a child `job_log` flushes into the parent's buffer rather
    than to real stdout, so the parent's block stays contiguous.
    """
    install()
    buf = io.StringIO()
    token = _log_buffer.set(buf)
    try:
        yield
    finally:
        _log_buffer.reset(token)
        text = buf.getvalue()
        if text:
            # After reset, the proxy routes writes based on the *outer*
            # context: parent buffer if nested, real stdout otherwise.
            sys.stdout.write(text)
            sys.stdout.flush()
