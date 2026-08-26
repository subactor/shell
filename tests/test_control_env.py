from __future__ import annotations

import pytest

from subactor_shell.config import load_config
from subactor_shell.control_env import ControlEnvironmentError, apply_control_environment


def write_environment(path, values: dict[str, str], mode: int = 0o600) -> None:
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
    path.chmod(mode)


def test_applies_only_missing_allowlisted_values_from_private_file(tmp_path) -> None:
    environment_file = tmp_path / "control.env"
    admin_key = "SUBACTOR_ADMIN_TOKEN"
    write_environment(
        environment_file,
        {admin_key: "fixture-secret", "SUBACTOR_FOUNDER_URL": "http://control.test", "OTHER": "ignored"},
    )
    values = {"SUBACTOR_ENV_FILE": str(environment_file), admin_key: "existing"}
    applied = apply_control_environment(values)
    assert values[admin_key] == "existing"
    assert values["SUBACTOR_CONTROL_URL"] == "http://control.test"
    assert "OTHER" not in values
    assert applied == ("SUBACTOR_FOUNDER_URL", "SUBACTOR_CONTROL_URL")


def test_rejects_unsafe_or_missing_explicit_environment_file(tmp_path) -> None:
    unsafe_file = tmp_path / "unsafe.env"
    write_environment(unsafe_file, {"SUBACTOR_PLANFILE_URL": "http://planfile.test"}, mode=0o622)
    with pytest.raises(ControlEnvironmentError, match="grupy"):
        apply_control_environment({"SUBACTOR_ENV_FILE": str(unsafe_file)})
    with pytest.raises(ControlEnvironmentError, match="nie istnieje"):
        apply_control_environment({"SUBACTOR_ENV_FILE": str(tmp_path / "missing.env")})


def test_control_provider_honors_explicit_environment_override(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SUBACTOR_CONTROL_URL", "http://control.test")
    config = load_config(config_path, data_dir)
    assert config.provider("control").base_url == "http://control.test"
