# engine/agents/reasoning_engine.py
"""ReasoningEngine V2 — features → analise → autocritica → consolidacao."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

SYSTEM_PROMPT_V2 = """Você é um ANALISTA QUANTITATIVO SÊNIOR de mercados de escanteios.
Tom frio e objetivo. paper_trade=true — nunca execute ordem.

## PROCESSO (obrigatório):
### PASSO 1 — LEITURA TEMPORAL
Analise DELTAS e TENDÊNCIAS. Números absolutos enganam.
### PASSO 2 — EXCITAÇÃO E GAP
corner_excitation >0.5 = quente; <0.2 = esfriou. Cruze com corner_rate_15min.
### PASSO 3 — MEMÓRIA HISTÓRICA
Se trigger_reliability existe, confidence NÃO pode ignorá-la.
### PASSO 4 — VEREDICTO
Só então decida. Se previous_conclusion existe, diga o que MUDOU.

## JSON estrito:
{
  "reasoning": {"temporal": "...", "excitation": "...", "memory": "...", "change": "..."},
  "decision": "ENTRA|AGUARDA|NAO_ENTRA",
  "confidence": 0.0,
  "triggers": [],
  "kills": [],
  "score": 0
}
REGRAS: confidence ≤ reliability histórica se disponível; sem tendência confidence ≤ 0.5;
ENTRA exige rising + excitation > 0.3 + ≥2 triggers SEM kills.
"""

CRITIC_PROMPT = """Você é AUDITOR adversarial de decisões de trading (paper).
Análise:
{analysis}
Features:
{features}
Responda JSON:
{{"verdict":"APROVADA|REJEITADA|AJUSTAR","flaws_found":[],"suggested_confidence":0.0,"reasoning":"..."}}
Audite: confiança vs histórico, kills ignorados, coerência temporal, mudança sem justificativa.
"""


class ReasoningEngine:
    def __init__(self, glm_client, memory, feature_engine):
        self.glm = glm_client
        self.memory = memory
        self.features = feature_engine

    async def analyze(self, view: Dict) -> Dict[str, Any]:
        feats = self.features.build_features(view)
        hints = self._extract_trigger_hint(feats)
        feats["trigger_reliability"] = self.memory.trigger_reliability(hints)
        feats["validated_patterns"] = self.memory.best_patterns()

        analysis = await self._primary_analysis(view, feats)
        if not analysis:
            return self._fallback(view)

        critique = await self._self_critique(analysis, feats)
        analysis = self._apply_critique(analysis, critique)

        # Thresholds dinamicos (conservador / paper)
        try:
            try:
                from agents.dynamic_thresholds import get_dynamic_thresholds
            except ImportError:
                from engine.agents.dynamic_thresholds import get_dynamic_thresholds
            gate = get_dynamic_thresholds().allows_enter(
                score=float(analysis.get("score") or 0),
                confidence=float(analysis.get("confidence") or 0),
                triggers=list(analysis.get("triggers") or []),
                excitation=feats.get("corner_excitation"),
                kills=list(analysis.get("kills") or []),
            )
            analysis["_thresholds"] = gate
            if analysis.get("decision") == "ENTRA" and not gate.get("allow"):
                analysis["decision"] = "AGUARDA"
                analysis.setdefault("kills", []).append("blocked_by_dynamic_threshold")
                analysis["_meta"] = {
                    **(analysis.get("_meta") or {}),
                    "threshold_block": gate.get("reasons"),
                }
        except Exception as e:
            analysis.setdefault("_meta", {})["threshold_error"] = str(e)

        self.memory.record_decision(
            fixture_id=str(feats.get("fixture_id") or "unknown"),
            triggers=list(analysis.get("triggers") or []),
            confidence=float(analysis.get("confidence") or 0.5),
            decision=str(analysis.get("decision") or "AGUARDA"),
        )
        analysis["paper_trade"] = True
        analysis["execution_allowed"] = False
        analysis["features"] = {
            k: feats[k]
            for k in (
                "dangerous_trend",
                "corner_excitation",
                "corner_gap_min",
                "trigger_reliability",
                "previous_conclusion",
            )
            if k in feats
        }
        return analysis

    async def _primary_analysis(self, view: Dict, feats: Dict) -> Optional[Dict]:
        raw = await self.glm.chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_V2},
                {"role": "user", "content": self._build_prompt(view, feats)},
            ],
            temperature=0.2,
            max_tokens=1800,
        )
        return self._parse_json_robust(raw)

    async def _self_critique(self, analysis: Dict, feats: Dict) -> Optional[Dict]:
        raw = await self.glm.chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": CRITIC_PROMPT.format(
                        analysis=json.dumps(analysis, ensure_ascii=False, default=str),
                        features=json.dumps(feats, ensure_ascii=False, default=str),
                    ),
                },
                {"role": "user", "content": "Audite esta decisão adversarialmente."},
            ],
            temperature=0.0,
            max_tokens=800,
        )
        return self._parse_json_robust(raw)

    def _apply_critique(self, analysis: Dict, critique: Optional[Dict]) -> Dict:
        if not critique:
            analysis.setdefault("_meta", {})["critique"] = "unavailable"
            return analysis
        verdict = critique.get("verdict", "APROVADA")
        analysis["_meta"] = {
            "critique_verdict": verdict,
            "critique_flaws": critique.get("flaws_found", []),
        }
        if verdict == "REJEITADA":
            analysis["decision"] = "AGUARDA"
            analysis["confidence"] = min(float(analysis.get("confidence") or 0.5), 0.4)
            kills = list(analysis.get("kills") or [])
            kills.extend(critique.get("flaws_found") or ["rejeitada_em_auditoria"])
            analysis["kills"] = list(dict.fromkeys(kills))
        elif verdict == "AJUSTAR":
            suggested = critique.get("suggested_confidence")
            if isinstance(suggested, (int, float)):
                analysis["confidence"] = round(
                    (float(analysis.get("confidence") or 0.5) + float(suggested)) / 2, 3
                )
        return analysis

    @staticmethod
    def _parse_json_robust(raw: Optional[str]) -> Optional[Dict]:
        if not raw:
            return None
        text = raw.strip()
        if "```" in text:
            matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if matches:
                text = matches[-1]
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
        depth, start = 0, None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        obj = json.loads(text[start : i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        start = None
        return None

    def _build_prompt(self, view: Dict, feats: Dict) -> str:
        body = {
            k: v
            for k, v in feats.items()
            if k not in ("fixture_id",)
        }
        return (
            f"## JOGO: {view.get('home','?')} x {view.get('away','?')} | "
            f"{view.get('minute','?')}' | {view.get('score_home',0)}x{view.get('score_away',0)}\n"
            f"## SNAPSHOT\n"
            f"Corners: {view.get('corners_home',0)}-{view.get('corners_away',0)} | "
            f"AP: {view.get('dangerous_home', view.get('dangerous_attacks',0))}-"
            f"{view.get('dangerous_away',0)}\n"
            f"## FEATURES\n{json.dumps(body, ensure_ascii=False, default=str, indent=1)}\n"
            f"## MEMORIA\n"
            f"reliability={feats.get('trigger_reliability')} "
            f"patterns={json.dumps(feats.get('validated_patterns', []), ensure_ascii=False)}\n"
            f"previous={feats.get('previous_conclusion', 'primeira analise')}\n"
            "Responda APENAS JSON."
        )

    @staticmethod
    def _extract_trigger_hint(feats: Dict) -> List[str]:
        hints = []
        if feats.get("dangerous_trend") == "rising":
            hints.append("ap_rising")
        if float(feats.get("corner_excitation") or 0) > 0.4:
            hints.append("high_excitation")
        if int(feats.get("corner_rate_15min") or 0) >= 2:
            hints.append("high_corner_rate")
        return hints

    @staticmethod
    def _fallback(view: Dict) -> Dict:
        return {
            "decision": "AGUARDA",
            "confidence": 0.0,
            "triggers": [],
            "kills": ["glm_unavailable_or_invalid"],
            "reasoning": {"error": "pipeline falhou — fallback seguro"},
            "fixture_id": view.get("fixture_id"),
            "paper_trade": True,
            "execution_allowed": False,
        }
