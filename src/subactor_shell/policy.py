from __future__ import annotations

import json
from dataclasses import dataclass

from .compiler import ExecutionPlan


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str
    requires_approval: bool = False
    auto_execute: bool = False


class PolicyEngine:
    def __init__(self, *, allow_destructive: bool = False):
        self.allow_destructive = allow_destructive

    def evaluate(self, plan: ExecutionPlan) -> PolicyDecision:
        encoded = json.dumps(plan.to_dict(), ensure_ascii=False).casefold()
        if "{{secret:" in encoded or "vault://" in encoded or "env://" in encoded or "file://" in encoded:
            return PolicyDecision(
                False,
                "Plan nie może przenosić wartości ani referencji sekretów; connector może użyć wyłącznie lokalnego env_ref z konfiguracji",
            )
        if plan.effect == "destructive" and not self.allow_destructive:
            return PolicyDecision(False, "Operacje destructive są wyłączone przez politykę")
        if plan.effect == "read":
            if plan.mode == "execute":
                return PolicyDecision(True, "Odczyt może zostać wykonany lokalnie", auto_execute=True)
            return PolicyDecision(True, "Użytkownik zażądał tylko planu odczytu")
        return PolicyDecision(
            True,
            "Operacja zmienia stan i wymaga jawnego grantu apply",
            requires_approval=True,
        )
