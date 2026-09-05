"""
greenlight_check.py — Checker objetivo do green-light do meta_labeling.

PLUG-IN para o supervisor_jarvis (register_check). NÃO É o supervisor_jarvis.py
— salvar com aquele nome apagaria o loop de checagens plugáveis.

Critérios §7, exatamente como definidos (o lote recebido relaxou dois):
    1. >= 300 resoluções com outcome preenchido (q_decision_scorecard).
    2. cobertura conformal >= 0.85 SUSTENTADA COM n >= 100 — a condição n>=100
       foi omitida na versão recebida; com n=12 qualquer coisa passa.
    3. zero feed_bus_drops por >= 5 DIAS DE JOGO OBSERVADOS — drops=0 numa
       janela curta não conta; precisa dos dias observados.
    4. p99 de TODOS os estágios dentro do orçamento POR ESTÁGIO com janela
       >= 7 dias — não existe "100 ms único" inventado; orçamento é dicionário.

FAIL-CLOSED: dado ausente, provider quebrado ou estágio sem orçamento
definido = critério NÃO atendido. O checker nunca dá green-light por
ignorância. Green-light apenas ALERTA (transição de status que o supervisor
já detecta + alert_callback/Telegram) — a ativação do meta_labeler segue
sendo decisão humana e PAPER_TRADE permanece imutável (§0).

Providers (callables que retornam dict, ou ausentes se a fonte indisponível):
    scorecard() -> {"resolved": int}                        # analytics
    coverage()  -> {"coverage": float, "n": int}            # q_coverage
    drops()     -> {"drops": int, "game_days": float}       # journals/REG
    p99()       -> {"window_days": float, "stages": {nome: p99_ms}}

INTEGRAÇÃO (supervisor_jarvis.py — mescla, não substituição):

    GREENLIGHT = GreenLightCheck(providers={...}, stage_budget_ms={...})
    JARVIS.register_check("greenlight_meta_labeling", GREENLIGHT.check)
    # o supervisor detecta a transição warn -> ok e dispara o alerta.

std-lib only. Python 3.9+. Windows compatível.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Callable, Dict, Optional

__version__ = "1.0.0"

_LOG = logging.getLogger("aura.greenlight_check")


class GreenLightCheck:
    """Avalia os 4 critérios §7 com dados reais injetados via providers."""

    STATUS_OK = "ok"
    STATUS_WARN = "warn"   # ajuste as constantes se o supervisor usar outro vocabulário

    def __init__(self,
                 providers: Dict[str, Callable[[], Optional[Dict[str, Any]]]],
                 min_resolutions: int = 300,
                 min_coverage: float = 0.85,
                 min_coverage_n: int = 100,
                 drop_window_game_days: float = 5.0,
                 p99_window_days: float = 7.0,
                 stage_budget_ms: Optional[Dict[str, float]] = None) -> None:
        self._providers = dict(providers or {})
        self._min_resolutions = int(min_resolutions)
        self._min_coverage = float(min_coverage)
        self._min_coverage_n = int(min_coverage_n)
        self._drop_window = float(drop_window_game_days)
        self._p99_window = float(p99_window_days)
        self._stage_budget = dict(stage_budget_ms or {})

        self._lock = threading.Lock()
        self._evaluations = 0
        self._greens = 0
        self._last_green: Optional[bool] = None
        self._provider_errors: Dict[str, int] = {}

    # ------------------------------------------------------------- interna
    def _provider(self, key: str) -> Optional[Dict[str, Any]]:
        fn = self._providers.get(key)
        if fn is None:
            return None
        try:
            return fn()  # I/O fora do lock (analytics pode demorar)
        except Exception:
            with self._lock:
                self._provider_errors[key] = self._provider_errors.get(key, 0) + 1
            _LOG.exception("greenlight_check: provider %r falhou", key)
            return None

    # ------------------------------------------------------------------ api
    def evaluate(self) -> Dict[str, Any]:
        sc = self._provider("scorecard") or {}
        resolved = sc.get("resolved")
        c1 = {"ok": isinstance(resolved, (int, float)) and resolved >= self._min_resolutions,
              "resolved": resolved, "need": self._min_resolutions}

        cov = self._provider("coverage") or {}
        coverage = cov.get("coverage")
        n = cov.get("n")
        c2 = {"ok": (isinstance(coverage, (int, float)) and coverage >= self._min_coverage
                     and isinstance(n, (int, float)) and n >= self._min_coverage_n),
              "coverage": coverage, "n": n,
              "need_coverage": self._min_coverage, "need_n": self._min_coverage_n}

        dr = self._provider("drops") or {}
        drops = dr.get("drops")
        game_days = dr.get("game_days")
        c3 = {"ok": (drops == 0 and isinstance(game_days, (int, float))
                     and game_days >= self._drop_window),
              "drops": drops, "game_days": game_days,
              "need_game_days": self._drop_window}

        p9 = self._provider("p99") or {}
        stages = p9.get("stages")
        window_days = p9.get("window_days")
        violations = []
        ok4 = (isinstance(stages, dict) and len(stages) > 0
               and isinstance(window_days, (int, float))
               and window_days >= self._p99_window)
        if ok4:
            for stage, p99_ms in stages.items():
                budget = self._stage_budget.get(stage)
                if budget is None:
                    ok4 = False
                    violations.append({"stage": stage, "p99_ms": p99_ms,
                                       "budget_ms": None,
                                       "reason": "sem orçamento definido"})
                elif p99_ms > budget:
                    ok4 = False
                    violations.append({"stage": stage, "p99_ms": p99_ms,
                                       "budget_ms": budget,
                                       "reason": "acima do orçamento"})
        c4 = {"ok": ok4, "window_days": window_days, "violations": violations,
              "need_window_days": self._p99_window}

        criteria = {"resolutions_ge_300": c1,
                    "coverage_ge_085_with_n_ge_100": c2,
                    "zero_drops_5_game_days": c3,
                    "p99_within_budget_7d": c4}
        green = all(c["ok"] for c in criteria.values())

        with self._lock:
            self._evaluations += 1
            if green:
                self._greens += 1
            self._last_green = green
        return {"green": green, "criteria": criteria}

    def check(self) -> Dict[str, str]:
        """Contrato register_check do supervisor: {"status", "message"}."""
        res = self.evaluate()
        if res["green"]:
            return {"status": self.STATUS_OK,
                    "message": ("GREEN-LIGHT do meta_labeling: os 4 critérios do §7 "
                                "estão cumpridos. Ativação é decisão humana; "
                                "PAPER_TRADE permanece imutável.")}
        pend = [k for k, v in res["criteria"].items() if not v["ok"]]
        return {"status": self.STATUS_WARN,
                "message": "green-light pendente: " + ", ".join(pend)}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "greenlight_check": {
                    "evaluations": self._evaluations,
                    "green_results": self._greens,
                    "last_green": self._last_green,
                    "provider_errors": dict(self._provider_errors),
                    "thresholds": {
                        "min_resolutions": self._min_resolutions,
                        "min_coverage": self._min_coverage,
                        "min_coverage_n": self._min_coverage_n,
                        "drop_window_game_days": self._drop_window,
                        "p99_window_days": self._p99_window,
                    },
                    "stage_budget_ms": dict(self._stage_budget),
                }
            }


if __name__ == "__main__":
    def check(name: str, cond: bool, extra: str = "") -> None:
        print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
        if not cond:
            sys.exit(1)

    BUDGET = {"extract": 50.0, "publish": 20.0, "mc_lookup": 30.0, "conformal_gate": 40.0}

    def make(scorecard=None, coverage=None, drops=None, p99=None):
        provs = {}
        for k, v in (("scorecard", scorecard), ("coverage", coverage),
                     ("drops", drops), ("p99", p99)):
            provs[k] = None if v is None else (lambda val: (lambda: val))(v)
        return GreenLightCheck(providers=provs, stage_budget_ms=BUDGET)

    GOOD = dict(scorecard={"resolved": 342},
                coverage={"coverage": 0.87, "n": 140},
                drops={"drops": 0, "game_days": 6.5},
                p99={"window_days": 8.0,
                     "stages": {"extract": 12.0, "publish": 5.0,
                                "mc_lookup": 9.0, "conformal_gate": 11.0}})

    r = make(**GOOD).evaluate()
    check("critérios cumpridos → green", r["green"] is True)
    c = make(**GOOD).check()
    check("check() formato supervisor", c["status"] == "ok" and "GREEN-LIGHT" in c["message"])

    # regressão: n < 100 bloqueia (condição omitida no lote recebido)
    r2 = make(**{**GOOD, "coverage": {"coverage": 0.90, "n": 40}}).evaluate()
    check("cobertura 0.90 com n=40 NÃO é green", r2["green"] is False)
    check("critério de cobertura explicitamente reprovado",
          r2["criteria"]["coverage_ge_085_with_n_ge_100"]["ok"] is False)

    # janela de dias observados insuficiente
    r3 = make(**{**GOOD, "drops": {"drops": 0, "game_days": 3.0}}).evaluate()
    check("drops=0 com 3 dias observados NÃO é green", r3["green"] is False)

    # p99 acima do orçamento (orçamento por estágio, não 100 ms único)
    r4 = make(**{**GOOD, "p99": {"window_days": 8.0,
                                 "stages": {"extract": 80.0, "publish": 5.0,
                                            "mc_lookup": 9.0,
                                            "conformal_gate": 11.0}}}).evaluate()
    check("estágio acima do orçamento reprova", r4["green"] is False)
    check("violação identificada",
          r4["criteria"]["p99_within_budget_7d"]["violations"][0]["stage"] == "extract")

    # estágio sem orçamento definido → fail-closed
    r5 = make(**{**GOOD, "p99": {"window_days": 8.0,
                                 "stages": {"extract": 12.0, "novo_estagio": 1.0,
                                            "publish": 5.0, "mc_lookup": 9.0,
                                            "conformal_gate": 11.0}}}).evaluate()
    check("estágio sem orçamento reprova (fail-closed)", r5["green"] is False)

    # provider quebrado → fail-closed, contabilizado, sem crash
    def boom():
        raise RuntimeError("analytics fora do ar")

    broken = GreenLightCheck(providers={"scorecard": boom}, stage_budget_ms=BUDGET)
    r6 = broken.evaluate()
    check("provider quebrado não aprova nem derruba", r6["green"] is False)
    check("erro de provider contabilizado",
          broken.stats()["greenlight_check"]["provider_errors"]["scorecard"] == 1)

    # provider ausente → fail-closed sem crash
    r7 = GreenLightCheck(providers={}, stage_budget_ms=BUDGET).evaluate()
    check("sem providers → não green, sem crash", r7["green"] is False)

    final = make(**GOOD)
    final.evaluate()
    st = final.stats()["greenlight_check"]
    check("stats presente", st["evaluations"] == 1 and st["green_results"] == 1)
    print("ALL TESTS PASSED - greenlight_check.py")
