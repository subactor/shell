"""Read the bounded local Control environment without sourcing a shell file."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import MutableMapping
from pathlib import Path


_ALLOWED_KEYS = frozenset(
    {
        "SUBACTOR_ADMIN_TOKEN",
        "SUBACTOR_CONTROL_URL",
        "SUBACTOR_FOUNDER_URL",
        "SUBACTOR_PLANFILE_URL",
    }
)
_MAX_ENV_BYTES = 1_048_576


class ControlEnvironmentError(ValueError):
    """The optional Control environment file cannot be safely used."""


def default_environment_file() -> Path:
    """Locate ``platform/.env`` from a workspace, not from site-packages."""

    roots: list[Path] = []
    configured_root = os.environ.get("SUBACTOR_WORKSPACE_ROOT", "").strip()
    if configured_root:
        roots.append(Path(configured_root).expanduser())
    for anchor in (Path.cwd(), Path(sys.argv[0]).resolve(), Path(__file__).resolve()):
        roots.extend((anchor, *anchor.parents[:6]))
    seen: set[Path] = set()
    for root in roots:
        candidate = root / "platform" / ".env"
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    return Path.cwd() / "platform" / ".env"


def _environment_file(values: MutableMapping[str, str]) -> tuple[Path, bool]:
    configured = values.get("SUBACTOR_ENV_FILE", "").strip()
    return (Path(configured).expanduser(), True) if configured else (default_environment_file(), False)


def _read_selected_values(path: Path, *, explicit: bool) -> dict[str, str]:
    if not path.exists():
        if explicit:
            raise ControlEnvironmentError("SUBACTOR_ENV_FILE nie istnieje")
        return {}
    if path.is_symlink() or not path.is_file():
        raise ControlEnvironmentError("Plik Control environment musi być zwykłym plikiem")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ControlEnvironmentError("Nie można sprawdzić pliku Control environment") from exc
    if metadata.st_size > _MAX_ENV_BYTES:
        raise ControlEnvironmentError("Plik Control environment jest zbyt duży")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ControlEnvironmentError("Plik Control environment nie może być zapisywalny dla grupy ani innych")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ControlEnvironmentError("Nie można odczytać pliku Control environment") from exc
    selected: dict[str, str] = {}
    for line in content.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in _ALLOWED_KEYS and key not in selected:
            selected[key] = value.strip().strip("\"'")
    return selected


def apply_control_environment(values: MutableMapping[str, str] | None = None) -> tuple[str, ...]:
    """Populate missing Control keys from one validated file; values are never logged."""

    target = os.environ if values is None else values
    path, explicit = _environment_file(target)
    loaded = _read_selected_values(path, explicit=explicit)
    applied: list[str] = []
    for key, value in loaded.items():
        if value and not target.get(key):
            target[key] = value
            applied.append(key)
    if not target.get("SUBACTOR_CONTROL_URL") and target.get("SUBACTOR_FOUNDER_URL"):
        target["SUBACTOR_CONTROL_URL"] = target["SUBACTOR_FOUNDER_URL"]
        applied.append("SUBACTOR_CONTROL_URL")
    return tuple(applied)
