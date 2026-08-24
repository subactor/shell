from __future__ import annotations

import pytest

from subactor_shell.app import main


def test_provider_error_is_reported_without_secondary_console_failure(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "--config",
                str(tmp_path / "config.toml"),
                "--data-dir",
                str(tmp_path / "data"),
                "one",
                "--provider",
                "missing-provider",
                "test",
            ]
        )

    captured = capsys.readouterr()
    assert exit_info.value.code == 1
    assert "Błąd:" in captured.err
    assert "missing-provider" in captured.err
    assert "unexpected keyword argument 'file'" not in captured.err
