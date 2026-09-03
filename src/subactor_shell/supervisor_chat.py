"""Bounded invocation of subactor/supervisor from Subactor Shell chat."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SUPERVISOR_CHAT_ACTIONS: dict[str, tuple[str, ...]] = {
    "status": ("supervisor", "status"),
    "observe": ("supervisor", "observe"),
    "cycle": ("supervisor", "cycle", "--discover"),
    "questions": ("supervisor", "questions"),
    "report": ("supervisor", "report"),
}

BLOCKED_ACTIONS = frozenset(
    {
        "grant",
        "apply",
        "reject",
        "delegate",
        "enact",
        "pause",
        "resume",
        "start",
        "ask",
        "provider",
    }
)

PASS_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "NODE_ENV",
    "SUBACTOR_ADMIN_TOKEN",
    "SUBACTOR_ADMIN_TOKEN_FILE",
    "SUBACTOR_CONTROL_URL",
    "SUBACTOR_FOUNDER_URL",
    "SUBACTOR_PLANFILE_URL",
    "SUBACTOR_SUPERVISOR_ROOT",
    "SUBACTOR_SESSION_FILE",
    "SUBACTOR_PASS_ENV",
)

DEFAULT_SUPERVISOR_PASS_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TZ",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "SUBACTOR_ADMIN_TOKEN",
    "SUBACTOR_CONTROL_URL",
    "SUBACTOR_FOUNDER_URL",
    "SUBACTOR_PLANFILE_URL",
    "SUBACTOR_SESSION_FILE",
)

ACTION_TIMEOUT_SECONDS = {
    "status": 20.0,
    "questions": 20.0,
    "report": 20.0,
    "answer": 20.0,
    "observe": 45.0,
    "cycle": 120.0,
}

QUESTION_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$")
MAX_ANSWER_CHARS = 4000

SUPERVISOR_CHAT_USAGE = """Użycie: /supervisor [status|observe|cycle|questions|report|answer]
  status     stan LLM supervisora (domyślne); ślepy daemon dołącza obserwację czatu
  observe    jeden snapshot read-only Subactora
  cycle      jeden cykl oceny (--discover); nie jest apply; daemon może nadpisać
  questions  oczekujące pytania do Foundera; ślepy daemon dołącza obserwację czatu
  answer     /supervisor answer <id> <treść>  — HITL, nie apply
  report     skrót delegacji i fingerprintów

Poza czatem: subactor-shell supervisor status
W Founder Chat: /supervisor status

Supervisor nie jest chat-agentem. Ta komenda nie uruchamia grant, apply
ani dowolnej delegacji. Ustaw SUBACTOR_SUPERVISOR_CLI na bezwzględną
ścieżkę, jeśli `autonomy` nie jest w PATH.
"""


class SupervisorChatError(RuntimeError):
    """Safe, user-facing supervisor chat failure."""


def _bounded_answer(text: str) -> str:
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text or "")).strip()
    if not cleaned:
        raise SupervisorChatError("Użycie: /supervisor answer <id> <treść>")
    if len(cleaned) > MAX_ANSWER_CHARS:
        raise SupervisorChatError(f"Odpowiedź przekracza {MAX_ANSWER_CHARS} znaków")
    return cleaned


def parse_supervisor_chat_args(args: list[str] | tuple[str, ...] | None) -> dict[str, str]:
    tokens = [str(item).strip() for item in (args or []) if str(item).strip()]
    if not tokens:
        return {"action": "status"}
    action = tokens[0].lower().lstrip("/")
    if action in {"help", "-h", "--help"}:
        return {"action": "help"}
    if action in BLOCKED_ACTIONS:
        raise SupervisorChatError(
            f"Akcja '{action}' nie jest dostępna z czatu. "
            "Użyj autonomy supervisor poza czatem po osobnym grancie."
        )
    if action == "answer":
        question_id = tokens[1] if len(tokens) > 1 else ""
        if (
            not QUESTION_ID_RE.fullmatch(question_id)
            or ".." in question_id
            or "/" in question_id
            or "\\" in question_id
        ):
            raise SupervisorChatError("Użycie: /supervisor answer <id> <treść>")
        return {
            "action": "answer",
            "question_id": question_id,
            "answer": _bounded_answer(" ".join(tokens[2:])),
        }
    if action not in SUPERVISOR_CHAT_ACTIONS:
        raise SupervisorChatError(f"Nieznana akcja supervisora: {action}. {SUPERVISOR_CHAT_USAGE.strip()}")
    if len(tokens) > 1:
        raise SupervisorChatError("Akcja supervisora nie przyjmuje dodatkowych argumentów.")
    return {"action": action}


def supervisor_argv_tail(parsed: Mapping[str, str]) -> tuple[str, ...] | None:
    action = parsed.get("action", "")
    if action == "answer":
        return ("supervisor", "answer", parsed["question_id"], parsed["answer"])
    return SUPERVISOR_CHAT_ACTIONS.get(action)


def _workspace_dir() -> Path:
    here = Path(__file__).resolve()
    # src/subactor_shell/supervisor_chat.py → repo root, then umbrella workspace
    repo_root = here.parents[2]
    parent = repo_root.parent
    return parent.parent if parent.name == ".worktrees" else parent


def resolve_supervisor_invocation(
    env: Mapping[str, str] | None = None,
    *,
    workspace: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    values = os.environ if env is None else env
    workspace_dir = workspace or _workspace_dir()
    configured = str(values.get("SUBACTOR_SUPERVISOR_CLI", "")).strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise SupervisorChatError("SUBACTOR_SUPERVISOR_CLI musi być bezwzględną ścieżką")
        if not path.is_file():
            raise SupervisorChatError("SUBACTOR_SUPERVISOR_CLI nie wskazuje istniejącego pliku")
        return _invocation_from_cli(path, values, workspace_dir)

    sibling = workspace_dir / "supervisor" / "bin" / "autonomy.js"
    if sibling.is_file():
        return _invocation_from_cli(sibling, values, workspace_dir)

    on_path = which("autonomy")
    if on_path:
        return _invocation_from_cli(Path(on_path), values, workspace_dir)

    raise SupervisorChatError(
        "Supervisor CLI niedostępny. Ustaw SUBACTOR_SUPERVISOR_CLI na bezwzględną ścieżkę "
        "(np. …/supervisor/bin/autonomy.js) albo zainstaluj `autonomy` w PATH."
    )


def _invocation_from_cli(cli_path: Path, env: Mapping[str, str], workspace_dir: Path) -> dict[str, Any]:
    resolved = cli_path.resolve()
    configured_root = str(env.get("SUBACTOR_SUPERVISOR_ROOT", "")).strip()
    inferred = str(resolved.parent.parent) if resolved.parent.name == "bin" else ""
    root = configured_root or inferred or str(workspace_dir / "supervisor")
    is_js = resolved.suffix.lower() in {".js", ".mjs"}
    return {
        "executable": "node" if is_js else str(resolved),
        "prefix_args": [str(resolved)] if is_js else [],
        "root": root,
        "cli_path": str(resolved),
    }


def _session_token(env: Mapping[str, str]) -> str:
    if env.get("SUBACTOR_ADMIN_TOKEN"):
        return str(env["SUBACTOR_ADMIN_TOKEN"])
    path = Path(env.get("SUBACTOR_SESSION_FILE") or Path.home() / ".config" / "subactor" / "session.json").expanduser()
    try:
        st = path.stat()
        if not path.is_file() or path.is_symlink() or (st.st_mode & 0o077) != 0:
            return ""
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    if payload.get("schema") != "subactor.cli-session/v1" or payload.get("token_type") != "Bearer":
        return ""
    token = str(payload.get("access_token") or "")
    expires = str(payload.get("expires_at") or "")
    if not token:
        return ""
    if expires:
        try:
            when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if when.timestamp() <= datetime.now(timezone.utc).timestamp():
                return ""
        except ValueError:
            return ""
    return token


def supervisor_process_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    values = os.environ if env is None else env
    next_env = {key: values[key] for key in PASS_ENV if key in values and values[key] is not None}
    token = _session_token(values)
    if token and "SUBACTOR_ADMIN_TOKEN" not in next_env:
        next_env["SUBACTOR_ADMIN_TOKEN"] = token
    existing = [item.strip() for item in str(next_env.get("SUBACTOR_PASS_ENV") or values.get("SUBACTOR_PASS_ENV") or "").split(",") if item.strip()]
    base = existing or list(DEFAULT_SUPERVISOR_PASS_ENV)
    next_env["SUBACTOR_PASS_ENV"] = ",".join(dict.fromkeys([*base, "SUBACTOR_ADMIN_TOKEN", "SUBACTOR_CONTROL_URL", "SUBACTOR_SESSION_FILE"]))
    return next_env


def _parse_json_object(chunk: str) -> Any:
    try:
        value = json.loads(chunk)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, (dict, list)) else None


def _next_json_start(text: str, cursor: int) -> int:
    found = [index for index in (text.find("{", cursor), text.find("[", cursor)) if index >= 0]
    return min(found) if found else -1


def _parse_output(stdout: str) -> Any:
    text = (stdout or "").strip()
    if not text:
        return None
    direct = _parse_json_object(text)
    if direct is not None:
        return direct
    cursor = 0
    while cursor < len(text):
        idx = _next_json_start(text, cursor)
        if idx < 0:
            break
        block = _parse_json_object(text[idx:])
        if block is not None:
            return block
        cursor = idx + 1
    return None


def run_supervisor_chat_command(
    action: str | Mapping[str, str],
    *,
    env: Mapping[str, str] | None = None,
    workspace: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    invocation: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    parsed: Mapping[str, str] = action if isinstance(action, Mapping) else {"action": str(action)}
    action_name = str(parsed.get("action") or "status")
    if action_name == "help":
        return {
            "ok": True,
            "action": "help",
            "stdout": SUPERVISOR_CHAT_USAGE,
            "stderr": "",
            "data": None,
            "argv": [],
            "cli_path": "",
        }
    argv_tail = supervisor_argv_tail(parsed)
    if not argv_tail:
        raise SupervisorChatError(f"Nieznana akcja supervisora: {action_name}")
    resolved = invocation or resolve_supervisor_invocation(env, workspace=workspace)
    args = [*resolved["prefix_args"], *argv_tail, "--root", resolved["root"]]
    runner = run or subprocess.run
    try:
        completed = runner(
            [resolved["executable"], *args],
            cwd=resolved["root"],
            env=supervisor_process_env(env),
            timeout=timeout if timeout is not None else ACTION_TIMEOUT_SECONDS.get(action_name, 20.0),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SupervisorChatError("Supervisor CLI niedostępny (ENOENT). Sprawdź SUBACTOR_SUPERVISOR_CLI.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SupervisorChatError(f"Supervisor CLI przekroczył limit czasu dla akcji {action_name}") from exc
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    result = {
        "ok": completed.returncode == 0,
        "action": action_name,
        "stdout": stdout,
        "stderr": stderr[:4000],
        "data": _parse_output(stdout),
        "argv": args,
        "cli_path": resolved["cli_path"],
        "executable": resolved["executable"],
        "code": completed.returncode,
    }
    return _attach_chat_observation(
        result,
        env=env,
        workspace=workspace,
        run=runner,
        invocation=resolved,
    )


def _one_line(value: Any, max_len: int = 240) -> str:
    return " ".join(str(value or "").split()).strip()[:max_len]


def _pending_question_count(supervisor: Mapping[str, Any]) -> int | None:
    pending = supervisor.get("questionsPending")
    if pending is None:
        pending = supervisor.get("pendingQuestions")
    if pending is None and isinstance(supervisor.get("questions"), dict):
        pending = len(supervisor["questions"])
    return pending if isinstance(pending, int) else None


def _status_decision(data: Mapping[str, Any], supervisor: Mapping[str, Any], assessment: Mapping[str, Any]) -> Any:
    action_type = data.get("action")
    if isinstance(action_type, dict):
        action_type = action_type.get("type")
    return (
        assessment.get("decision")
        or (supervisor.get("lastCycleResult") or {}).get("decision")
        or data.get("decision")
        or action_type
    )


def daemon_needs_chat_observation(data: Any) -> bool:
    if not isinstance(data, dict) or data.get("source") != "running-service":
        return False
    supervisor = data["supervisor"] if isinstance(data.get("supervisor"), dict) else {}
    if supervisor.get("assessmentReady") is False:
        return True
    pending = _pending_question_count(supervisor)
    if isinstance(pending, int) and pending > 0:
        return True
    if isinstance(data.get("assessment"), dict):
        assessment = data["assessment"]
    elif isinstance(supervisor.get("lastAssessment"), dict):
        assessment = supervisor["lastAssessment"]
    else:
        assessment = data
    return _status_decision(data, supervisor, assessment) == "observe_more"


def _attach_chat_observation(
    result: dict[str, Any],
    *,
    env: Mapping[str, str] | None,
    workspace: Path | None,
    run: Callable[..., subprocess.CompletedProcess[str]],
    invocation: dict[str, Any],
) -> dict[str, Any]:
    data = result.get("data")
    values = os.environ if env is None else env
    if not result.get("ok") or not _session_token(values):
        return result
    needs_status = (
        result.get("action") == "status"
        and isinstance(data, dict)
        and data.get("chatObservation") is None
        and daemon_needs_chat_observation(data)
    )
    needs_questions = (
        result.get("action") == "questions"
        and isinstance(data, list)
        and bool(data)
        and result.get("chatObservation") is None
    )
    if not needs_status and not needs_questions:
        return result
    try:
        observe = run_supervisor_chat_command(
            "observe",
            env=env,
            workspace=workspace,
            run=run,
            invocation=invocation,
        )
    except SupervisorChatError:
        return result
    if not (observe.get("ok") and isinstance(observe.get("data"), dict)):
        return result
    if needs_status:
        result["data"] = {**data, "chatObservation": observe["data"]}
    else:
        result["chatObservation"] = observe["data"]
    return result


def _observation_lines(observation: Mapping[str, Any], *, label: str) -> list[str]:
    failed = [item for item in (observation.get("failed") or []) if item]
    lines = [
        f"  {label}: healthy={'tak' if observation.get('healthy') is True else 'nie'}"
        f" degraded={'tak' if observation.get('degraded') is True else 'nie'}"
    ]
    if failed:
        lines.append(f"  nieudane komendy: {', '.join(str(item) for item in failed[:8])}")
    commands = observation.get("commands") if isinstance(observation.get("commands"), dict) else {}
    shown = 0
    for name, entry in commands.items():
        if shown >= 4:
            break
        if not isinstance(entry, dict) or entry.get("ok"):
            continue
        reason = _one_line(entry.get("stderr") or entry.get("error") or "", 160)
        if reason:
            lines.append(f"  {name}: {reason}")
            shown += 1
    return lines


def compact_supervisor_view(data: Any, *, chat_observation: Any | None = None) -> str:
    if isinstance(data, dict) and chat_observation is None and isinstance(data.get("chatObservation"), dict):
        chat_observation = data["chatObservation"]
    if not isinstance(chat_observation, dict):
        chat_observation = None
    if isinstance(data, list):
        if not data:
            return "  brak oczekujących pytań"
        lines = ["  źródło: stan daemona (HITL, nie apply)"]
        for index, item in enumerate(data[:8]):
            if not isinstance(item, dict):
                lines.append(f"  {index + 1}. {_one_line(item, 200)}")
                continue
            ident = item.get("id") or item.get("questionId") or str(index + 1)
            question = _one_line(item.get("question") or item.get("summary") or item.get("goal"), 200)
            lines.append(f"  {ident}{f': {question}' if question else ''}")
        lines.append("  odpowiedź: /supervisor answer <id> <treść>")
        if chat_observation:
            lines.extend(_observation_lines(chat_observation, label="obserwacja czatu (sesja Foundera)"))
            if chat_observation.get("healthy") is True:
                lines.append("  pytania daemona mogą być nieaktualne wobec obserwacji czatu")
            else:
                lines.append("  przed odpowiedzią: /supervisor observe")
        else:
            lines.append("  przed odpowiedzią: /supervisor observe")
        return "\n".join(lines)
    if not isinstance(data, dict):
        return ""
    observation = data if data.get("schemaVersion") == "subactor.observation/v1" else (
        data["observation"] if isinstance(data.get("observation"), dict) else None
    )
    supervisor = data["supervisor"] if isinstance(data.get("supervisor"), dict) else data
    live = data["live"] if isinstance(data.get("live"), dict) else {}
    if isinstance(data.get("assessment"), dict):
        assessment = data["assessment"]
    elif isinstance(supervisor.get("lastAssessment"), dict):
        assessment = supervisor["lastAssessment"]
    else:
        assessment = data
    lines: list[str] = []
    if data.get("cycleId"):
        lines.append(f"  cykl: {data.get('cycleId')}")
        lines.append("  współdzielony stan: daemon bez sesji może nadpisać ten cykl")
    if data.get("analyzed") is True:
        lines.append("  ocena GLM: tak")
    elif data.get("analyzed") is False:
        reason = _one_line(data.get("reason"), 80)
        lines.append(f"  ocena GLM: nie{f' ({reason})' if reason else ''}")
    if observation:
        lines.extend(_observation_lines(observation, label="obserwacja"))
    if chat_observation:
        lines.extend(_observation_lines(chat_observation, label="obserwacja czatu (sesja Foundera)"))
    if live.get("available") is True:
        lines.append("  usługa: działająca (loopback /health)")
    elif live.get("available") is False:
        lines.append(f"  usługa: lokalna projekcja ({live.get('reason') or 'niedostępna'})")
    if data.get("source") == "running-service":
        lines.append("  źródło: daemon HTTP — observe/cycle czatu wstrzykują sesję Foundera, daemon nie")
    subactor = data.get("subactor") if isinstance(data.get("subactor"), dict) else None
    if subactor is not None:
        lines.append(f"  doctor CLI: {'ok' if subactor.get('ok') is True else 'błąd'}")
    if "paused" in supervisor:
        lines.append(f"  pauza: {'tak' if supervisor.get('paused') else 'nie'}")
    if supervisor.get("cycles") is not None:
        lines.append(f"  cykle: {supervisor.get('cycles')}")
    if supervisor.get("lastCycleAt"):
        lines.append(f"  ostatni cykl: {supervisor.get('lastCycleAt')}")
    decision = _status_decision(data, supervisor, assessment)
    if decision:
        label = "ostatnia decyzja daemona" if data.get("source") == "running-service" else "ostatnia decyzja"
        lines.append(f"  {label}: {decision}")
    state = assessment.get("systemState") or data.get("systemState")
    if state:
        state_label = "stan daemona" if data.get("source") == "running-service" else "stan"
        lines.append(f"  {state_label}: {state}")
    summary = assessment.get("summary") or data.get("summary")
    if summary:
        lines.append(f"  podsumowanie: {_one_line(summary, 240)}")
    if supervisor.get("assessmentReady") is False:
        lines.append("  ocena GLM daemona: niegotowa")
    pending = _pending_question_count(supervisor)
    if isinstance(pending, int) and pending > 0:
        lines.append(f"  pytania Foundera: {pending}  → /supervisor questions")
        if chat_observation and chat_observation.get("healthy") is True:
            lines.append("  pytania daemona mogą być nieaktualne wobec obserwacji czatu")
        elif data.get("source") == "running-service":
            lines.append("  przed odpowiedzią: /supervisor observe")
    return "\n".join(lines)


def format_supervisor_chat_result(result: Mapping[str, Any], *, verbose: bool = False) -> str:
    if result.get("action") == "help":
        return str(result.get("stdout") or SUPERVISOR_CHAT_USAGE)
    header = [
        "Supervisor (chat)",
        f"  CLI: {result['cli_path']}" if result.get("cli_path") else "",
        f"  akcja: {result.get('action') or 'status'}",
        f"  wynik: {'ok' if result.get('ok') else 'błąd'}",
    ]
    compact = compact_supervisor_view(
        result.get("data"),
        chat_observation=result.get("chatObservation"),
    )
    error_line = "" if result.get("ok") else str(result.get("stderr") or "").strip()[:1000]
    action = str(result.get("action") or "status")
    wants_body = verbose or not compact
    body = ""
    if wants_body:
        data = result.get("data")
        raw = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(
            result.get("stdout") or result.get("stderr") or ""
        ).strip()
        limit = 8000 if verbose else 2500
        body = raw[:limit] + ("\n…" if len(raw) > limit else "")
    return "\n\n".join(item for item in ("\n".join(item for item in header if item), compact, error_line, body) if item) + "\n"
