# engine/agents/dynamic_thresholds.py
"""Online Threshold Tuner - calibracao paper-only (nao altera execucao real)."""
import logging
from pathlib import Path
from collections import deque

logger = logging.getLogger("aura.agent.tuner")

# Nao escreve YAML automaticamente a menos que AUTO_WRITE_CONFIG=True
AUTO_WRITE_CONFIG = False


class OnlineThresholdTuner:
    def __init__(self, config_path: str = "agents/glm_config.yaml"):
        self.config_path = Path(config_path)
        self.recent_results = deque(maxlen=10)
        self.pending_updates = {}

    def record_result(self, result: str, minute: int, score: int):
        self.recent_results.append({"result": result, "minute": minute, "score": score})
        if len(self.recent_results) >= 5:
            self._auto_calibrate()

    def _auto_calibrate(self):
        losses = [r for r in self.recent_results if r["result"] == "LOSS"]
        wins = [r for r in self.recent_results if r["result"] == "WIN"]
        loss_rate = len(losses) / len(self.recent_results) if self.recent_results else 0

        if loss_rate > 0.4:
            logger.warning("Taxa de perda alta. Ajustando para Modo Conservador.")
            self._update_config("entra_min_score", 75)
            self._update_config("entra_min_confidence", 0.80)
        elif loss_rate < 0.2 and len(wins) > 5:
            logger.info("Taxa de acerto elevada. Ajustando limiares.")
            self._update_config("entra_min_score", 70)

    def _update_config(self, key: str, value):
        self.pending_updates[key] = value
        logger.info("Parametro dinamico (pending): %s = %s (AUTO_WRITE=%s)", key, value, AUTO_WRITE_CONFIG)
        if not AUTO_WRITE_CONFIG:
            return
        # Escrita real desabilitada por padrao - so loga


ONLINE_TUNER = OnlineThresholdTuner()
