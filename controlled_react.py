from __future__ import annotations
from dataclasses import dataclass

# PATCH V23-P1 (item 4.3 da auditoria): mapa de status explícito.
# Original: `plan["action"] + "PED" if plan["action"]=="STOP" else "ESCALATED"`
# Funcionava por acidente ("STOP"+"PED"=="STOPPED"); quebra silenciosamente
# se o valor de action mudar. Mapa explícito falha de forma visível (KeyError)
# em vez de produzir string errada sem aviso.
_ACTION_STATUS = {"STOP": "STOPPED", "ESCALATE": "ESCALATED"}


@dataclass(frozen=True)
class AgentBudget:
    max_steps: int = 5
    max_tool_calls: int = 3


class ControlledReactAgent:
    def __init__(self, budget: AgentBudget | None = None) -> None:
        self.budget = budget or AgentBudget()

    async def run(self, observation: dict) -> dict:
        steps = 0
        tool_calls = 0
        while steps < self.budget.max_steps:
            plan = await self.assess(observation)
            action = plan.get("action")
            if action in _ACTION_STATUS:
                return {
                    "status": _ACTION_STATUS[action],
                    "steps": steps,
                    "paper_trade": True,
                    "execution_allowed": False,
                }
            if tool_calls >= self.budget.max_tool_calls:
                return {"status": "BUDGET_EXCEEDED", "steps": steps, "paper_trade": True, "execution_allowed": False}
            result = await self.execute_allowed(plan)
            observation = {**observation, "last_result": result}
            tool_calls += 1
            steps += 1
        return {"status": "STEP_BUDGET_EXCEEDED", "steps": steps, "paper_trade": True, "execution_allowed": False}

    async def assess(self, observation: dict) -> dict:
        return {"action": "STOP"}

    async def execute_allowed(self, plan: dict) -> dict:
        return {"ok": True, "execution_allowed": False}
