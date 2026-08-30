"""Fleet status and startup operational banner for Subactor Shell."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass
class PullRequestSummary:
    repository: str
    number: int
    title: str
    url: str
    updated_at: str = ""


@dataclass
class DoctorIssueSummary:
    repository: str
    number: int
    title: str
    url: str
    labels: list[str] = field(default_factory=list)


@dataclass
class FleetOverview:
    prs: list[PullRequestSummary] = field(default_factory=list)
    doctor_issues: list[DoctorIssueSummary] = field(default_factory=list)
    active_services: list[tuple[str, str, str]] = field(default_factory=list)
    planfile_ok: bool = False
    planfile_version: str = ""
    error: str | None = None


def fetch_github_prs(*, limit: int = 8, timeout: float = 3.5) -> list[PullRequestSummary]:
    if not shutil.which("gh"):
        return []
    try:
        cmd = [
            "gh",
            "search",
            "prs",
            "--state=open",
            "--owner=subactor",
            "--owner=if-uri",
            "--json",
            "repository,number,title,url,updatedAt",
            "--limit",
            str(limit),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout or "[]")
        results: list[PullRequestSummary] = []
        for item in data:
            repo = item.get("repository", {}).get("nameWithOwner", "")
            num = int(item.get("number", 0))
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", ""))
            updated = str(item.get("updatedAt", ""))
            if repo and num:
                results.append(PullRequestSummary(repository=repo, number=num, title=title, url=url, updated_at=updated))
        return results
    except Exception:
        return []


def fetch_doctor_issues(*, limit: int = 8, timeout: float = 3.5) -> list[DoctorIssueSummary]:
    if not shutil.which("gh"):
        return []
    try:
        cmd = [
            "gh",
            "search",
            "issues",
            "--state=open",
            "--owner=subactor",
            "--owner=if-uri",
            "--label=doctor:detected",
            "--json",
            "repository,number,title,url,labels",
            "--limit",
            str(limit),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout or "[]")
        results: list[DoctorIssueSummary] = []
        for item in data:
            repo = item.get("repository", {}).get("nameWithOwner", "")
            num = int(item.get("number", 0))
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", ""))
            raw_labels = item.get("labels", [])
            labels = [l.get("name", "") for l in raw_labels if isinstance(l, dict) and l.get("name")]
            if repo and num:
                results.append(DoctorIssueSummary(repository=repo, number=num, title=title, url=url, labels=labels))
        return results
    except Exception:
        return []


def fetch_systemd_services(timeout: float = 2.0) -> list[tuple[str, str, str]]:
    if not shutil.which("systemctl"):
        return []
    try:
        cmd = ["systemctl", "--user", "list-units", "subactor*", "--no-pager", "--plain"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            return []
        results: list[tuple[str, str, str]] = []
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0].startswith("subactor-"):
                unit = parts[0]
                active = parts[2]
                sub = parts[3]
                results.append((unit, active, sub))
        return results
    except Exception:
        return []


def fetch_planfile_health(url: str = "http://127.0.0.1:8765/health", timeout: float = 2.5) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("status") == "ok", data.get("version", "")
    except Exception:
        return False, ""


def fetch_fleet_overview() -> FleetOverview:
    prs = fetch_github_prs()
    issues = fetch_doctor_issues()
    services = fetch_systemd_services()
    planfile_ok, planfile_ver = fetch_planfile_health()
    return FleetOverview(
        prs=prs,
        doctor_issues=issues,
        active_services=services,
        planfile_ok=planfile_ok,
        planfile_version=planfile_ver,
    )


def render_fleet_startup_banner(console: Console, overview: FleetOverview | None = None, *, hyperlinks: bool = True) -> None:
    if overview is None:
        overview = fetch_fleet_overview()

    table = Table(
        title="[bold cyan]Subactor Autonomous Ecosystem & Fleet Activity[/bold cyan]",
        title_justify="left",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
    )
    table.add_column("Kategoria", style="bold yellow", width=18)
    table.add_column("Szczegóły operacyjne", style="white")

    # 1. Pull Requests
    if overview.prs:
        pr_lines: list[str] = []
        for pr in overview.prs[:6]:
            link = f"[link={pr.url}]{pr.repository}#{pr.number}[/link]" if hyperlinks else f"{pr.repository}#{pr.number}"
            clean_title = pr.title.replace("[", "\\[").replace("]", "\\]")
            pr_lines.append(f"[green]●[/green] {link} [dim]—[/dim] {clean_title}")
        table.add_row("Pull Requests", "\n".join(pr_lines))
    else:
        table.add_row("Pull Requests", "[dim]Brak otwartych PR-ów lub tryb offline[/dim]")

    # 2. Doctor Issues
    if overview.doctor_issues:
        issue_lines: list[str] = []
        for iss in overview.doctor_issues[:5]:
            link = f"[link={iss.url}]{iss.repository}#{iss.number}[/link]" if hyperlinks else f"{iss.repository}#{iss.number}"
            clean_title = iss.title.replace("[", "\\[").replace("]", "\\]")
            badge = ""
            if "repair:completed" in iss.labels:
                badge = "[green][naprawiony][/green] "
            elif "agent:blocked" in iss.labels or "repair:failed" in iss.labels:
                badge = "[red][zablokowany][/red] "
            elif "repair:in-progress" in iss.labels:
                badge = "[yellow][w naprawie][/yellow] "
            issue_lines.append(f"{badge}{link} [dim]—[/dim] {clean_title}")
        table.add_row("Doctor / Projekt", "\n".join(issue_lines))

    # 3. Systemd Autonomous Services
    if overview.active_services:
        running_names: list[str] = []
        for unit, active, sub in overview.active_services:
            name = unit.replace(".service", "").replace(".timer", "").replace("subactor-", "")
            if active == "active" or sub == "running":
                running_names.append(f"[green]{name}[/green]")
            elif active == "failed":
                running_names.append(f"[red]{name} (fail)[/red]")
        if running_names:
            table.add_row("Autonomia & CI", " · ".join(running_names[:8]))

    # 4. Planfile status
    pf_badge = f"[green]OK (v{overview.planfile_version})[/green]" if overview.planfile_ok else "[yellow]niedostępny[/yellow]"
    table.add_row("Planfile", pf_badge)

    panel = Panel(table, border_style="cyan", padding=(0, 1))
    console.print(panel)
