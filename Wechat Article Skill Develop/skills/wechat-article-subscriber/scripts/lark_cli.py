#!/usr/bin/env python3
"""Run lark-cli with a stable executable, config directory, and working directory."""

from __future__ import annotations

import subprocess
import sys

from lark_runtime import (
    global_lark_config_fingerprint,
    lark_cli_config_dir,
    lark_cli_environment,
    lark_cli_home_dir,
    lark_cli_work_dir,
    resolve_lark_cli,
    safe_lark_arguments,
)


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    try:
        executable = resolve_lark_cli()
        arguments = safe_lark_arguments(arguments)
        lark_cli_home_dir().mkdir(parents=True, exist_ok=True)
        lark_cli_config_dir().mkdir(parents=True, exist_ok=True)
        work_dir = lark_cli_work_dir()
        work_dir.mkdir(parents=True, exist_ok=True)
        global_before = global_lark_config_fingerprint()
        result = subprocess.run(
            [str(executable), *arguments],
            cwd=work_dir,
            env=lark_cli_environment(),
            check=False,
        )
        if global_lark_config_fingerprint() != global_before:
            print(
                "refusing success: the user's global ~/.lark-cli/config.json changed "
                "during an isolated Skill command",
                file=sys.stderr,
            )
            return 1
        return result.returncode
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"cannot run isolated lark-cli: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
