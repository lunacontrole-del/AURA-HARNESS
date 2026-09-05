# engine/agents/hermes_sre.py
"""
Hermes SRE / System Maintenance
- Diagnostico via SystemMedicAgent
- Acoes de cura SOMENTE se SAFE_HEAL_ENABLED=True
- Nunca apaga DBs; restart so de BATs mapeados e sob flag
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("aura.hermes.sre")

SAFE_HEAL_ENABLED = False  # OPT-IN sob supervisao

try:
    from engine.agents_glm.system_medic_agent import SYSTEM_MEDIC
except Exception:
    SYSTEM_MEDIC = None

SRE_PROMPT = """
Voce e o Engenheiro de Confiabilidade (SRE) do sistema AURA QUANT-X.
O System Medic entregou o seguinte laudo de diagnostico:
{diagnosis_report}

Responda em JSON estrito:
{{
  "analysis": "analise tecnica",
  "action_type": "safe_restart" | "clear_cache" | "alert_operator" | "no_action",
  "target": "Bridge|Engine|Voice|Ollama|cache|none",
  "speak": "mensagem ao operador"
}}

Regras:
- Servico OFFLINE -> safe_restart (se permitido)
- DB corrompido -> alert_operator (NUNCA apagar DB)
- Sem erros -> no_action
"""

BAT_MAP = {
    "Bridge": "AURA_RUN_BRIDGE.bat",
    "Voice": "AURA_RUN_VOICE_SEGURO.bat",
    "Ollama": "AURA_RUN_OLLAMA.bat",
    "Engine": "AURA_RUN_ENGINE.bat",
}


class HermesSRE:
    def __init__(self, llm_callable: Optional[Callable] = None):
        self.llm_callable = llm_callable

    async def run_system_maintenance(self) -> Dict[str, Any]:
        if SYSTEM_MEDIC is None:
            return {"status": "error", "speak": "SystemMedic indisponivel."}

        report = SYSTEM_MEDIC.full_diagnosis()
        if not report.get("errors_found"):
            return {
                "status": "healthy",
                "speak": "Diagnostico completo. Todos os sistemas nominais.",
                "report": report,
            }

        # Sem LLM: laudo puro + sugestao conservadora
        if self.llm_callable is None:
            speak = "Problemas detectados: " + "; ".join(report["errors_found"][:3])
            if not SAFE_HEAL_ENABLED:
                speak += " | SAFE_HEAL_ENABLED=False — nenhuma acao automatica."
            return {"status": "report_only", "speak": speak, "report": report}

        prompt = SRE_PROMPT.format(diagnosis_report=json.dumps(report, indent=2))
        try:
            llm_output = await self.llm_callable(prompt)
            data = json.loads(llm_output) if isinstance(llm_output, str) else llm_output
        except Exception as e:
            logger.error("SRE LLM parse: %s", e)
            return {
                "status": "error",
                "speak": "Erro ao processar laudo medico.",
                "report": report,
            }

        action = data.get("action_type", "alert_operator")
        target = data.get("target", "none")
        speak = data.get("speak", "Avaliando falhas.")

        if not SAFE_HEAL_ENABLED:
            speak = (
                f"[SOMENTE LAUDO] {speak} "
                f"(acao sugerida: {action} / target: {target}; SAFE_HEAL_ENABLED=False)"
            )
            return {"status": "report_only", "speak": speak, "report": report, "suggestion": data}

        if action == "safe_restart":
            bat_name = BAT_MAP.get(str(target))
            if bat_name and Path(bat_name).exists():
                logger.warning("HERMES SRE: reiniciando %s via %s", target, bat_name)
                try:
                    subprocess.Popen(["cmd", "/c", "start", "", bat_name], shell=True)
                    speak = f"Detectei que {target} caiu. Reinicio disparado. Aguarde ~10s."
                except Exception as e:
                    speak = f"Falha ao reiniciar {target}: {e}"
                    action = "alert_operator"
            else:
                speak = f"Nao encontrei BAT para {target}. Intervencao manual necessaria."
                action = "alert_operator"

        elif action == "clear_cache":
            cache_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "AURA_QUANT_X" / "Cache"
            if cache_dir.exists():
                cleared = 0
                for f in cache_dir.glob("*"):
                    try:
                        if f.is_file():
                            f.unlink()
                            cleared += 1
                    except Exception:
                        pass
                speak = f"Cache limpo ({cleared} arquivos)."
            else:
                speak = "Pasta de cache nao encontrada."

        elif action == "alert_operator":
            speak = (
                f"Erro critico em {target}. Nao corrigi automaticamente. "
                f"Laudo: {data.get('analysis', '')}"
            )

        return {"status": "healed" if action != "alert_operator" else "alert", "speak": speak, "report": report}


HERMES_SRE = HermesSRE(llm_callable=None)
