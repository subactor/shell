from __future__ import annotations

import tomllib
from pathlib import Path

from subactor_shell.app import build_parser
from subactor_shell.terminal import (
    canonical_ticket_links,
    terminal_hyperlinks_enabled,
    ticket_link_lines,
)


def test_public_entry_point_does_not_shadow_founder_chat() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"] == {
        "subactor-shell": "subactor_shell.app:main",
    }
    assert "subactor" not in project["scripts"]
    assert build_parser().prog == "subactor-shell"


def test_ticket_links_are_canonical_bounded_and_reject_unsafe_origins() -> None:
    links = canonical_ticket_links(
        "Sprawdź PLF-8348, plf-8348 oraz PLF-8539.",
        "http://127.0.0.1:8091",
    )
    assert [item[0] for item in links] == ["PLF-8348", "PLF-8539"]
    assert links[0][1] == (
        "http://127.0.0.1:8091/?tab=delegation&action=view&ticket=PLF-8348&filter=PLF-8348"
    )
    assert canonical_ticket_links("PLF-1", "http://user:password@127.0.0.1:8091") == []
    assert canonical_ticket_links("PLF-1", "javascript:alert(1)") == []


def test_terminal_link_style_requires_supported_tty_and_honors_opt_out() -> None:
    assert terminal_hyperlinks_enabled(is_terminal=False, env={"TERM_PROGRAM": "vscode"}) is False
    assert terminal_hyperlinks_enabled(is_terminal=True, env={"TERM_PROGRAM": "vscode"}) is True
    assert terminal_hyperlinks_enabled(
        is_terminal=True,
        env={"TERM_PROGRAM": "vscode", "SUBACTOR_TERMINAL_HYPERLINKS": "0"},
    ) is False

    linked = ticket_link_lines("PLF-8348", "https://founder.example.test", hyperlinks=True)[0]
    plain = ticket_link_lines("PLF-8348", "https://founder.example.test", hyperlinks=False)[0]
    assert any(str(span.style).startswith("link https://") for span in linked.spans)
    assert all(not str(span.style).startswith("link ") for span in plain.spans)
