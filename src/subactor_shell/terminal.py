"""Safe terminal presentation for canonical Subactor resource links."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

from rich.text import Text


_TICKET = re.compile(r"\bPLF-[0-9]{1,20}\b", re.IGNORECASE)
_FALSE = frozenset({"0", "false", "no", "off", "never"})
_TRUE = frozenset({"1", "true", "yes", "on", "always"})


def terminal_hyperlinks_enabled(
    *,
    is_terminal: bool,
    env: Mapping[str, str] | None = None,
) -> bool:
    values = os.environ if env is None else env
    if not is_terminal:
        return False
    preference = values.get("SUBACTOR_TERMINAL_HYPERLINKS", "auto").strip().lower()
    if preference in _FALSE:
        return False
    if preference in _TRUE:
        return True
    if values.get("TERM", "").lower() == "dumb":
        return False
    return bool(
        values.get("WT_SESSION")
        or values.get("VTE_VERSION")
        or values.get("KONSOLE_VERSION")
        or values.get("KITTY_WINDOW_ID")
        or re.search(r"^(vscode|wezterm|iterm\.app|ghostty)$", values.get("TERM_PROGRAM", ""), re.I)
        or re.search(r"jetbrains|jediterm", values.get("TERMINAL_EMULATOR", ""), re.I)
        or re.search(r"kitty|wezterm|foot|contour", values.get("TERM", ""), re.I)
    )


def canonical_ticket_links(text: str, control_url: str, *, limit: int = 20) -> list[tuple[str, str]]:
    parsed = urlsplit(control_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return []
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return []

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _TICKET.finditer(text):
        ticket = match.group(0).upper()
        if ticket in seen:
            continue
        seen.add(ticket)
        query = urlencode({"tab": "delegation", "action": "view", "ticket": ticket, "filter": ticket})
        url = urlunsplit((parsed.scheme, parsed.netloc, "/", query, ""))
        result.append((ticket, url))
        if len(result) >= limit:
            break
    return result


def ticket_link_lines(
    text: str,
    control_url: str,
    *,
    hyperlinks: bool,
) -> list[Text]:
    lines: list[Text] = []
    for ticket, url in canonical_ticket_links(text, control_url):
        line = Text("  ")
        line.append(ticket, style="cyan")
        line.append(": ")
        line.append(url, style=f"link {url}" if hyperlinks else None)
        lines.append(line)
    return lines
