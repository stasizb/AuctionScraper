"""Unit tests for core/job_log.py — per-task stdout buffering.

The whole point of this module is that two parallel asyncio workers each
get their own buffer and don't interleave their output. The deterministic
test here forces a known interleaving by yielding control between prints,
then asserts each worker's lines came out as a contiguous block.
"""

import asyncio
import io
import sys
import unittest
from contextlib import redirect_stdout

from tests._helpers import ROOT  # noqa: F401

import core.job_log as job_log_mod


class TestJobLogBuffering(unittest.TestCase):
    """The proxy is process-wide and lazy-installed; resetting `_installed`
    around each test lets `install()` re-wrap the test's stdout fresh,
    instead of being a no-op against whatever proxy a prior test left
    around."""

    def setUp(self):
        self._saved_stdout    = sys.stdout
        self._saved_installed = job_log_mod._installed
        job_log_mod._installed = False
        self.buf = io.StringIO()
        sys.stdout = self.buf   # install() will wrap this on first job_log

    def tearDown(self):
        sys.stdout = self._saved_stdout
        job_log_mod._installed = self._saved_installed

    def test_single_block_flushes_at_exit(self):
        async def run():
            async with job_log_mod.job_log():
                print("line A")
                print("line B")
            print("after")

        asyncio.run(run())
        out = self.buf.getvalue()
        self.assertIn("line A", out)
        self.assertIn("line B", out)
        self.assertLess(out.index("line A"), out.index("after"))
        self.assertLess(out.index("line B"), out.index("after"))

    def test_parallel_workers_emit_contiguous_blocks(self):
        """Force interleaving between two concurrent tasks by yielding
        control after every print. Without job_log, the lines would
        alternate; with job_log, each task's lines come out as one block."""
        async def worker(label: str):
            async with job_log_mod.job_log():
                for i in range(3):
                    print(f"{label}-{i}")
                    await asyncio.sleep(0)   # yield to the scheduler

        async def run():
            await asyncio.gather(worker("A"), worker("B"))

        asyncio.run(run())
        lines = [l for l in self.buf.getvalue().splitlines() if l]
        self.assertEqual(len(lines), 6)
        a_indices = [i for i, l in enumerate(lines) if l.startswith("A-")]
        b_indices = [i for i, l in enumerate(lines) if l.startswith("B-")]
        self.assertEqual(a_indices, list(range(a_indices[0], a_indices[0] + 3)))
        self.assertEqual(b_indices, list(range(b_indices[0], b_indices[0] + 3)))

    def test_outside_job_log_prints_passthrough(self):
        async def run():
            async with job_log_mod.job_log():
                await asyncio.sleep(0)   # ensure the proxy is installed
            print("not buffered")

        asyncio.run(run())
        self.assertIn("not buffered", self.buf.getvalue())

    def test_empty_job_log_does_not_emit_anything(self):
        async def run():
            async with job_log_mod.job_log():
                await asyncio.sleep(0)   # body intentionally produces no output

        asyncio.run(run())
        self.assertEqual(self.buf.getvalue(), "")

    def test_nested_job_log_flushes_into_parent_buffer(self):
        async def run():
            async with job_log_mod.job_log():
                print("outer-1")
                async with job_log_mod.job_log():
                    print("inner-1")
                print("outer-2")

        asyncio.run(run())
        out = self.buf.getvalue()
        idx = [out.index(s) for s in ("outer-1", "inner-1", "outer-2")]
        self.assertEqual(idx, sorted(idx))


if __name__ == "__main__":
    unittest.main()
