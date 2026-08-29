import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install" / "install.sh"
GENERATE = ROOT / "scripts" / "install" / "generate-release-metadata.py"


def run_install_sh(*args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    merged = {"PATH": "/usr/bin:/bin", "HOME": "/tmp/subactor-shell-test-home"}
    if env:
        merged.update(env)
    return subprocess.run(
        ["sh", str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        env=merged,
        check=False,
    )


def test_install_sh_help():
    proc = run_install_sh("--help")
    assert proc.returncode == 0
    assert "SUBACTOR_SHELL_RELEASE" in proc.stdout


def test_generate_release_metadata(tmp_path: Path):
  wheel = tmp_path / "subactor_shell-0.2.2-py3-none-any.whl"
  wheel.write_bytes(b"wheel-bytes")
  output = tmp_path / "latest.json"
  proc = subprocess.run(
      [sys.executable, str(GENERATE), "--version", "0.2.2", "--wheel", str(wheel), "--output", str(output)],
      capture_output=True,
      text=True,
      check=True,
  )
  assert output.exists()
  payload = json.loads(output.read_text(encoding="utf-8"))
  assert payload["version"] == "0.2.2"
  assert payload["assets"]["wheel"]["filename"] == wheel.name
  assert len(payload["assets"]["wheel"]["sha256"]) == 64


def test_sync_docs_copies_installers():
    proc = subprocess.run(["sh", str(ROOT / "scripts" / "install" / "sync-docs.sh")], check=True)
    assert proc.returncode == 0
    assert (ROOT / "docs" / "install.sh").exists()
    assert (ROOT / "docs" / "install.ps1").exists()
    assert (ROOT / "docs" / "install.sh").read_text(encoding="utf-8") == INSTALL_SH.read_text(encoding="utf-8")
