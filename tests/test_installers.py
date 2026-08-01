from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME = "wechat-article-subscriber"


def _existing_install(root: Path, directory: str) -> Path:
    destination = root / directory / "skills" / SKILL_NAME
    destination.mkdir(parents=True)
    (destination / "SENTINEL.txt").write_text("previous-version", encoding="utf-8")
    return destination


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer test")
def test_unix_dependency_failure_preserves_existing_install(tmp_path: Path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    install_root = tmp_path / "install-root"
    destination = _existing_install(install_root, ".agents")
    invalid_home = tmp_path / "state-is-a-file"
    invalid_home.write_text("not a directory", encoding="utf-8")
    environment = os.environ.copy()
    environment["WECHAT_SKILL_INSTALL_ROOT"] = str(install_root)
    environment["WECHAT_ARTICLE_HOME"] = str(invalid_home)

    result = subprocess.run(
        [bash, str(ROOT / "install.sh"), "--target", "agents"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert (destination / "SENTINEL.txt").read_text(encoding="utf-8") == "previous-version"
    assert not list(destination.parent.glob(".wechat-article-subscriber.install.*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX custom installer test")
def test_unix_custom_install_path(tmp_path: Path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    destination = tmp_path / "custom" / SKILL_NAME
    result = subprocess.run(
        [
            bash,
            str(ROOT / "install.sh"),
            "--target",
            "agents",
            "--destination",
            str(destination),
            "--no-deps",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert (destination / "SKILL.md").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer test")
def test_unix_openclaw_and_hermes_targets(tmp_path: Path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    environment = os.environ.copy()
    environment["WECHAT_SKILL_INSTALL_ROOT"] = str(tmp_path / "install-root")
    for target, directory in (("openclaw", ".openclaw"), ("hermes", ".hermes")):
        destination = (
            tmp_path / "install-root" / directory / "skills" / SKILL_NAME
        )
        result = subprocess.run(
            [
                bash,
                str(ROOT / "install.sh"),
                "--target",
                target,
                "--no-deps",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stderr
        assert (destination / "SKILL.md").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer test")
def test_unix_all_target_covers_every_platform(tmp_path: Path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    environment = os.environ.copy()
    environment["WECHAT_SKILL_INSTALL_ROOT"] = str(tmp_path / "install-root")
    result = subprocess.run(
        [
            bash,
            str(ROOT / "install.sh"),
            "--target",
            "all",
            "--no-deps",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    expected = {
        ".agents": "agents",
        ".codex": "codex",
        ".claude": "claude",
        ".copilot": "copilot",
        ".openclaw": "openclaw",
        ".hermes": "hermes",
    }
    for directory in expected:
        destination = (
            tmp_path / "install-root" / directory / "skills" / SKILL_NAME
        )
        assert destination.is_dir(), f"--target all missed {expected[directory]}"


@pytest.mark.skipif(os.name != "nt", reason="Windows installer test")
def test_powershell_dependency_failure_preserves_existing_install(tmp_path: Path):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is unavailable")
    install_root = tmp_path / "install-root"
    destination = _existing_install(install_root, ".agents")
    invalid_home = tmp_path / "state-is-a-file"
    invalid_home.write_text("not a directory", encoding="utf-8")
    environment = os.environ.copy()
    environment["WECHAT_SKILL_INSTALL_ROOT"] = str(install_root)
    environment["WECHAT_ARTICLE_HOME"] = str(invalid_home)

    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(ROOT / "install.ps1"),
            "-Target",
            "agents",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert (destination / "SENTINEL.txt").read_text(encoding="utf-8") == "previous-version"
    assert not list(destination.parent.glob(".wechat-article-subscriber.install.*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher test")
def test_powershell_installer_accepts_py_launcher(tmp_path: Path):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is unavailable")
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "python.cmd").write_text("@exit /b 1\r\n", encoding="ascii")
    (commands / "py.cmd").write_text(
        "@echo off\r\n"
        "if \"%1\"==\"-3\" if \"%2\"==\"-c\" echo 3.11.0\r\n"
        "exit /b 0\r\n",
        encoding="ascii",
    )
    install_root = tmp_path / "install-root"
    environment = os.environ.copy()
    environment["PATH"] = str(commands)
    environment["WECHAT_SKILL_INSTALL_ROOT"] = str(install_root)

    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(ROOT / "install.ps1"),
            "-Target",
            "agents",
            "-NoDeps",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    assert (install_root / ".agents" / "skills" / SKILL_NAME / "SKILL.md").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher test")
def test_unix_wrapper_prefers_python3(tmp_path: Path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "python3.log"
    fake_python = commands / "python3"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {shlex_quote(str(log))}\n"
        "if [ \"${1:-}\" = \"-c\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{commands}{os.pathsep}{environment.get('PATH', '')}"

    result = subprocess.run(
        [bash, str(ROOT / "skills" / SKILL_NAME / "scripts" / "run.sh"), "process", "--help"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "-c import sys" in calls
    assert "runtime.py process --help" in calls


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"
