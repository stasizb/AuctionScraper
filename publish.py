#!/usr/bin/env python3
"""Stage everything, commit with a fixed message, push to remote."""

import subprocess
import sys


def run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    run(["git", "add", "."])
    run(["git", "commit", "-m", "update html"])
    run(["git", "push"])


if __name__ == "__main__":
    main()
