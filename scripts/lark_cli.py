#!/usr/bin/env python3
"""Run lark-cli with a stable executable, config directory, and working directory."""

from __future__ import annotations

import subprocess
import sys

from lark_runtime import LarkCLIError, run_agent_lark


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    try:
        returncode, global_unchanged, stdout, stderr = run_agent_lark(arguments)
        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr:
            print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
        if not global_unchanged:
            print(
                "refusing success: the user's global ~/.lark-cli/config.json changed "
                "during an isolated Skill command",
                file=sys.stderr,
            )
            return 1
        return returncode
    except subprocess.TimeoutExpired:
        print("cannot run isolated lark-cli: command timed out after 60 seconds", file=sys.stderr)
        return 1
    except (FileNotFoundError, LarkCLIError, OSError, ValueError) as exc:
        print(f"cannot run isolated lark-cli: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
