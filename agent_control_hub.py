# engine/agents/agent_control_hub.py
"""
Agent Control Hub v1.0
Centro de comando para pausar, resumir e afinar agentes em runtime.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger("aura.control.hub")
STATE_FILE = Path("engine/data/agent_states.json")

# Por padrao agentes de risco ficam disponiveis; publicacao/scrape continuam gated nos proprios modulos
DEFAULT_STATES = {
    "red_team": True,
    "online_tuner": True,
    "web_scraper": False,
    "understat_xg": False,
    "crew_council": True,
    "voice_listener": False,
    "telegram_publish": False,
}


class AgentControlHub:
    def __init__(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.states = self._load_states()

    def _load_states(self) -> Dict:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    merged = dict(DEFAULT_STATES)
                    merged.update(data)
                    return merged
            except Exception as e:
                logger.error("Falha ao ler agent_states: %s", e)
        self._save_states(DEFAULT_STATES)
        return dict(DEFAULT_STATES)

    def _save_states(self, states: Dict):
        STATE_FILE.write_text(json.dumps(states, indent=4), encoding="utf-8")

    def is_active(self, agent_name: str) -> bool:
        return bool(self.states.get(agent_name, False))

    def pause_agent(self, agent_name: str) -> str:
        if agent_name in self.states:
            self.states[agent_name] = False
            self._save_states(self.states)
            logger.warning("Agente %s PAUSADO pelo operador.", agent_name)
            return f"Agente {agent_name} pausado com sucesso."
        return "Agente nao encontrado."

    def resume_agent(self, agent_name: str) -> str:
        if agent_name in self.states:
            self.states[agent_name] = True
            self._save_states(self.states)
            logger.info("Agente %s RESUMIDO pelo operador.", agent_name)
            return f"Agente {agent_name} reativado com sucesso."
        return "Agente nao encontrado."

    def get_status(self) -> Dict:
        return dict(self.states)


CONTROL_HUB = AgentControlHub()
