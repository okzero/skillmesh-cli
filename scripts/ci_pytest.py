#!/usr/bin/env python3
"""Run pytest and expose failures in the GitHub Actions job summary."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    command = [
        sys.executable, "-m", "pytest", "tests/", "-v", "-p", "no:cacheprovider"
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    output = result.stdout + result.stderr
    print(output, end="")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if result.returncode and summary:
        tail = output[-30000:].replace("```", "` ` `")
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("## Pytest failure output\n\n```text\n")
            stream.write(tail)
            stream.write("\n```\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
