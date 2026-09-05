#!/usr/bin/env python3
"""
CornerAI Local Engine Core — processamento máximo no dispositivo.
Usa NumPy (CPU vetorizado). Se PyTorch+CUDA estiver instalado, acelera
operações em GPU automaticamente.
"""
from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("aura.engine_core")

try:
    from skill_runtime import _data_quality, skill_info
except Exception:
    _data_quality = None
    skill_info = lambda: {"installed": False}

try:
    # v12.6.0 — Weight of Money: filtro Anti-Red (puramente defensivo, nunca
    # gera sinal sozinho — só pode vetar um BUY_CORNER já decidido pelo modelo).
    from risk_manager import validate_trade_with_market
except Exception:
    def validate_trade_with_market(signal, odds_velocity, threshold: float = 1.5) -> bool:  # type: ignore
        if signal == "BUY_CORNER" and odds_velocity > threshold:
            return False
        return True

# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------
_TORCH = None
_GPU_GOVERNOR = None
_DEVICE = "cpu"
try:
    import torch
    _TORCH = torch
    try:
        from gpu_resource_manager import GPU_GOVERNOR, resolve_cuda_device
        _GPU_GOVERNOR = GPU_GOVERNOR
    except Exception:
        resolve_cuda_device = None  # type: ignore
    if torch.cuda.is_available():
        try:
            _DEVICE = resolve_cuda_device() if resolve_cuda_device is not None else "cuda"
        except Exception:
            _DEVICE = "cuda"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        _DEVICE = "mps"
except Exception as exc:
    logger.debug("Torch/CUDA indisponível; usando backend CPU: %s", exc)

BACKEND = f"torch-{_DEVICE}" if _TORCH is not None else "numpy-cpu"

def _to_device(arr):
    """Converte lista/array para tensor somente se houver margem segura."""
    a = np.asarray(arr, dtype=np.float32)
    target = _DEVICE
    if _TORCH is not None and _DEVICE.startswith("cuda") and _GPU_GOVERNOR is not None:
        target = _GPU_GOVERNOR.get_best_device(required_gb=1.5)
    if _TORCH is not None and (target.startswith("cuda") or target == "mps"):
        try:
            return _TORCH.tensor(a, device=target)
        except Exception:
            logger.debug("Falha ao mover tensor para %s; mantendo CPU", target, exc_info=True)
    return a



def backend_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {"backend": BACKEND, "torch": _TORCH is not None, "device": _DEVICE}
    if _TORCH is not None and _DEVICE.startswith("cuda"):
        try:
            idx = int(_DEVICE.split(":", 1)[1]) if ":" in _DEVICE else 0
            info["gpu_name"] = _TORCH.cuda.get_device_name(idx)
            info["vram_gb"] = round(_TORCH.cuda.get_device_properties(idx).total_memory / (1024**3), 2)
        except Exception:
            pass
    return info


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WINDOW_MICRO, WINDOW_MID, WINDOW_MACRO = 3, 5, 10
MAX_HISTORY = 120
LEARNING_RATE = 0.15
DEFAULT_ALPHA = 1.0
MIN_ALPHA, MAX_ALPHA = 0.4, 2.5
MIN_EDGE = 0.045
MIN_DDA_DT = 0.15
MAX_KELLY = 0.05
BURST_35 = (2100, 2400)  # seconds
RACE_80 = (4800, 5220)


def _poisson_at_least(lam: float, k: int) -> float:
    if lam <= 0:
        return 0.0
    if k <= 0:
        return 1.0
    # P(X >= k) = 1 - sum_{i=0}^{k-1} e^{-λ} λ^i / i!
    term = math.exp(-lam)
    s = term
    for i in range(1, k):
        term *= lam / i
        s += term
    return max(0.0, 1.0 - s)


def _clamp_alpha(a: float) -> float:
    return max(MIN_ALPHA, min(MAX_ALPHA, a))


@dataclass
class MatchContext:
    fixture_id: str
    home: str
    away: str
    base_corner_rate: float = 0.12
    danger_home: np.ndarray = field(default_factory=lambda: np.zeros(MAX_HISTORY, dtype=np.float64))
    danger_away: np.ndarray = field(default_factory=lambda: np.zeros(MAX_HISTORY, dtype=np.float64))
    xg_home: np.ndarray = field(default_factory=lambda: np.zeros(MAX_HISTORY, dtype=np.float64))
    xg_away: np.ndarray = field(default_factory=lambda: np.zeros(MAX_HISTORY, dtype=np.float64))
    last_minute: int = -1
    updated_at: float = 0.0


class KnowledgeBase:
    def __init__(self) -> None:
        self.weights: Dict[str, float] = {}

    def get_alpha(self, team: Optional[str]) -> float:
        if not team:
            return DEFAULT_ALPHA
        return self.weights.get(str(team).upper().strip(), DEFAULT_ALPHA)

    def train(self, matches: List[Dict[str, Any]]) -> int:
        n = 0
        for m in matches or []:
            if not m or not m.get("home") or not m.get("away"):
                continue
            stats = m.get("stats") or {}
            corners = stats.get("corners") or {}
            danger = stats.get("dangerous") or stats.get("dangerousAttacks") or {}
            for side, team in (("home", m["home"]), ("away", m["away"])):
                da = float(danger.get(side) or 0)
                fc = float(corners.get(side) or 0)
                if da <= 20:
                    continue
                expected = da * 0.08
                ratio = fc / max(0.01, expected)
                key = str(team).upper().strip()
                cur = self.weights.get(key, DEFAULT_ALPHA)
                new = _clamp_alpha(cur + LEARNING_RATE * (ratio - cur))
                self.weights[key] = new
                n += 1
        return n

    def export(self) -> Dict[str, float]:
        return dict(self.weights)

    def load_file(self, path: str) -> int:
        import json, os
        if not os.path.isfile(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            w = data.get("weights") if isinstance(data, dict) else data
            if isinstance(w, dict):
                for k, v in w.items():
                    try:
                        self.weights[str(k).upper().strip()] = _clamp_alpha(float(v))
                    except Exception:
                        pass
                return len(self.weights)
        except Exception:
            pass
        # torch state dict optional
        if _TORCH is not None:
            try:
                obj = _TORCH.load(path, map_location=_DEVICE)
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        try:
                            self.weights[str(k).upper().strip()] = _clamp_alpha(float(v))
                        except Exception:
                            pass
                    return len(self.weights)
            except Exception:
                pass
        return 0

    def save_file(self, path: str) -> bool:
        import json
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"weights": self.export()}, f)
            return True
        except Exception:
            return False



class MarketAnalyzer:
    """Rastreia o 'Peso do Dinheiro' (Weight of Money / dropping odds) por
    fixture, a partir da odd asiática de escanteios enviada pela extensão.

    Uso exclusivamente analítico e de filtragem de risco (Filtro Anti-Red) —
    esta classe NUNCA decide um sinal, só mede a velocidade da odd para que
    o motor de sinais e o risk_manager possam vetar/confirmar decisões já
    tomadas pelo modelo de campo (pressão/xG).
    """

    def __init__(self, timeframe_seconds: int = 180) -> None:
        self.timeframe_seconds = timeframe_seconds
        self._history: Dict[str, List[Tuple[float, float]]] = defaultdict(list)

    def calculate_odds_velocity(self, fixture_id: str, current_odd: Optional[float]) -> float:
        """Delta % da odd asiática de escanteios na janela (default 3min).
        Odd caindo (mercado "derretendo") = velocity negativa = dinheiro
        entrando no Over. Retorna 0.0 sem odd válida ou histórico insuficiente.
        """
        if not current_odd or current_odd <= 0:
            return 0.0
        now = time.time()
        hist = self._history[fixture_id]
        hist.append((now, float(current_odd)))
        hist[:] = [h for h in hist if now - h[0] <= self.timeframe_seconds]
        if len(hist) < 2:
            return 0.0
        oldest_odd = hist[0][1]
        if oldest_odd <= 0:
            return 0.0
        return round(((current_odd - oldest_odd) / oldest_odd) * 100.0, 4)

    def reset(self, fixture_id: str) -> None:
        self._history.pop(fixture_id, None)



def _extract_captured_at(payload):
    """Best-effort capture timestamp as epoch seconds for quality ledger."""
    stamp = payload.get("capturedAt") or payload.get("captured_at") or payload.get("timestamp") or payload.get("ts")
    if stamp is None:
        return None
    try:
        if isinstance(stamp, (int, float)):
            v = float(stamp)
            return v if v < 10_000_000_000 else v / 1000.0
        if isinstance(stamp, str):
            from datetime import datetime, timezone
            text_ts = stamp.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text_ts)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    except Exception:
        return None
    return None


class LocalAIEngine:
    """Motor preditivo local — GPU via torch se disponível, senão NumPy."""

    def __init__(self) -> None:
        self.kb = KnowledgeBase()
        self.contexts: Dict[str, MatchContext] = {}
        self.last_analysis: Dict[str, Any] = {}
        self.last_payloads: Dict[str, Dict[str, Any]] = {}
        self.stats = {"ingested": 0, "alerts": 0, "trained": 0, "wom_blocked": 0, "wom_confluence": 0, "integrity_blocks": 0, "integrity_warnings": 0}
        self.last_integrity: Dict[str, Any] = {}
        self.market = MarketAnalyzer()  # v12.6.0 — Weight of Money
        # carrega pesos locais se existirem
        import os
        base = os.path.dirname(os.path.abspath(__file__))
        for name in ("model_weights.json", "kb_weights.json", "model_weights.pt"):
            n = self.kb.load_file(os.path.join(base, name))
            if n:
                self.stats["trained"] = max(self.stats["trained"], n)
                break

    def feed_historical(self, matches: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = self.kb.train(matches)
        self.stats["trained"] += n
        return {"ok": True, "updated": n, "teams": len(self.kb.weights), "weights": self.kb.export()}

    def _resolve_seconds(self, payload: Dict[str, Any]) -> int:
        if isinstance(payload.get("minute"), (int, float)):
            extra = payload.get("extraMinute") or 0
            return int((payload["minute"] + extra) * 60)
        clock = payload.get("clock")
        if clock:
            parts = str(clock).replace("'", "").split("+")
            try:
                return (int(parts[0] or 0) + (int(parts[1]) if len(parts) > 1 else 0)) * 60
            except Exception:
                pass
        return 0

    def _get_ctx(self, payload: Dict[str, Any]) -> MatchContext:
        fid = str(payload.get("fixtureId") or payload.get("fixture") or "")
        if not fid:
            raise ValueError("fixtureId ausente")
        if fid not in self.contexts:
            h2h = payload.get("h2hResumido") or payload.get("h2h") or {}
            rate = 0.12
            if h2h.get("mediaEscanteiosTotal"):
                try:
                    rate = float(h2h["mediaEscanteiosTotal"]) / 90.0
                except Exception:
                    pass
            self.contexts[fid] = MatchContext(
                fixture_id=fid,
                home=str(payload.get("home") or payload.get("mandante") or ""),
                away=str(payload.get("away") or payload.get("visitante") or ""),
                base_corner_rate=rate,
            )
        else:
            # FIX: o primeiro snapshot de um fixture pode chegar antes da
            # extensão resolver os nomes dos times no DOM (corrida no settle
            # inicial da captura). Sem isto, ctx.home/away ficavam travados
            # em "" para a partida inteira mesmo com snapshots seguintes já
            # trazendo os nomes corretos — o chat/skill mostrava "? × ?"
            # enquanto o topo do painel (lido direto do state da extensão)
            # mostrava os nomes certos.
            ctx = self.contexts[fid]
            if not ctx.home:
                new_home = str(payload.get("home") or payload.get("mandante") or "")
                if new_home:
                    ctx.home = new_home
            if not ctx.away:
                new_away = str(payload.get("away") or payload.get("visitante") or "")
                if new_away:
                    ctx.away = new_away
        return self.contexts[fid]

    def _bind(self, ctx: MatchContext, payload: Dict[str, Any], minute: int) -> Dict[str, Any]:
        """Bind stats into context; return feature quality summary (P0)."""
        from features import read_numeric, STATUS_OK, STATUS_MISSING, STATUS_INVALID

        stats = payload.get("stats") or payload.get("estatisticas") or {}
        if not isinstance(stats, dict):
            stats = {}
        def _norm_side(block):
            if isinstance(block, dict):
                return block
            if isinstance(block, (list, tuple)) and len(block) >= 2:
                return {"home": block[0], "away": block[1]}
            return {}

        danger = _norm_side(
            stats.get("dangerous")
            or stats.get("dangerousAttacks")
            or stats.get("ataquesPerigosos")
            or payload.get("dangerous")
        )
        xg = _norm_side(stats.get("xg") or stats.get("xG") or payload.get("xg"))
        corners = _norm_side(
            stats.get("corners")
            or stats.get("escanteios")
            or payload.get("corners")
        )

        minute = max(0, min(MAX_HISTORY - 1, minute))

        def _side(block: dict, side: str, field: str):
            if side not in block:
                return read_numeric(None, field_name=field)
            return read_numeric(block.get(side), field_name=field)

        reads = {
            "dangerous.home": _side(danger, "home", "dangerous.home"),
            "dangerous.away": _side(danger, "away", "dangerous.away"),
            "xg.home": _side(xg, "home", "xg.home"),
            "xg.away": _side(xg, "away", "xg.away"),
            "corners.home": _side(corners, "home", "corners.home"),
            "corners.away": _side(corners, "away", "corners.away"),
        }

        # Legacy-compatible write into series (preserve prior behaviour for valid payloads)
        ctx.danger_home[minute] = float(reads["dangerous.home"]["legacy_default"])
        ctx.danger_away[minute] = float(reads["dangerous.away"]["legacy_default"])
        ctx.xg_home[minute] = float(reads["xg.home"]["legacy_default"])
        ctx.xg_away[minute] = float(reads["xg.away"]["legacy_default"])
        ctx.last_minute = minute
        ctx.updated_at = time.time()

        status_counts = {STATUS_OK: 0, STATUS_MISSING: 0, STATUS_INVALID: 0}
        missing_fields = []
        invalid_fields = []
        legacy_default_count = 0
        for name, r in reads.items():
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
            if r["status"] == STATUS_MISSING:
                missing_fields.append(name)
                legacy_default_count += 1
            elif r["status"] == STATUS_INVALID:
                invalid_fields.append(name)
                legacy_default_count += 1

        # Critical for live p5 path: fixtureId handled elsewhere; dangerous + xg + timestamp
        critical_names = ("dangerous.home", "dangerous.away", "xg.home", "xg.away")
        critical_missing = [n for n in critical_names if reads[n]["status"] in (STATUS_MISSING, STATUS_INVALID)]

        quality = {
            "status_counts": status_counts,
            "missing_fields": missing_fields,
            "invalid_fields": invalid_fields,
            "critical_missing_fields": critical_missing,
            "legacy_default_count": legacy_default_count,
            "schema_version": "p0_quality_v1",
            "provenance": {k: {"status": v["status"], "value": v["value"]} for k, v in reads.items()},
        }
        return quality

    def _derivatives(self, ctx: MatchContext, minute: int) -> Dict[str, float]:
        """Derivadas vetorizadas (NumPy). Com torch+GPU poderia mover arrays para device."""
        m = minute
        micro = max(0, m - WINDOW_MICRO)
        mid = max(0, m - WINDOW_MID)
        macro = max(0, m - WINDOW_MACRO)
        dt_micro = max(1, m - micro)
        dt_mid = max(1, m - mid)
        dt_macro = max(1, mid - macro)

        def _rate(series, start: int, end: int, dt: int) -> float:
            cur = float(series[end])
            prev = float(series[start])
            # Stats are cumulative. Empty history at start>0 would look like
            # a 3-minute explosion and push P(canto) to 100%.
            if prev <= 0.0 and start > 0 and cur > 0.0:
                return cur / max(end, 1)
            return (cur - prev) / max(1, dt)

        dda_h = _rate(ctx.danger_home, micro, m, dt_micro)
        dda_a = _rate(ctx.danger_away, micro, m, dt_micro)
        total_dda = float(dda_h + dda_a)

        dxg_h_cur = _rate(ctx.xg_home, mid, m, dt_mid)
        dxg_h_pri = _rate(ctx.xg_home, macro, mid, dt_macro)
        d2_h = (dxg_h_cur - dxg_h_pri) / dt_mid

        dxg_a_cur = _rate(ctx.xg_away, mid, m, dt_mid)
        dxg_a_pri = _rate(ctx.xg_away, macro, mid, dt_macro)
        d2_a = (dxg_a_cur - dxg_a_pri) / dt_mid

        total_acc = float(d2_h + d2_a)
        intensity = 1.0 + total_dda * 0.45 + total_acc * 1.8
        return {
            "total_dda": total_dda,
            "total_acc": total_acc,
            "intensity": intensity,
        }

    def _parse_market_stats(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Lê o bloco 'market_stats' (WoM) enviado pela extensão em
        background.js::pushLocalAITelemetry (chave state.wom)."""
        ms = payload.get("market_stats") or payload.get("wom") or {}
        return ms if isinstance(ms, dict) else {}

    def _parse_odds(self, payload: Dict[str, Any]) -> Optional[float]:
        def _num(value):
            try:
                n = float(value)
                return n if 1.01 <= n <= 50 else None
            except (TypeError, ValueError):
                return None

        markets = payload.get("oddsMarkets") or payload.get("odds") or {}
        if isinstance(markets, dict):
            node = markets.get("asianCorners") or markets.get("overCorners") or {}
            if isinstance(node, dict):
                found = _num(node.get("odds"))
                if found:
                    return found
            if _num(markets.get("liveOverCornerOdds")):
                return _num(markets["liveOverCornerOdds"])
            for key, node in markets.items():
                blob = " ".join(str(x) for x in (key, node if not isinstance(node, dict) else node.get("market"), node.get("marketType") if isinstance(node, dict) else "")).lower()
                if "corner" not in blob and "escante" not in blob:
                    continue
                if isinstance(node, dict):
                    found = _num(node.get("odds"))
                    if found:
                        return found
                    quotes = node.get("quotes") or []
                    if isinstance(quotes, list) and quotes:
                        last = quotes[-1] if isinstance(quotes[-1], dict) else {}
                        found = _num(last.get("odds"))
                        if found:
                            return found
        if isinstance(markets, list):
            for node in markets:
                if isinstance(node, dict):
                    found = _num(node.get("odds"))
                    if found:
                        return found
        for row in payload.get("oddsHistory") or []:
            if not isinstance(row, dict):
                continue
            blob = " ".join(str(row.get(k) or "") for k in ("marketType", "market", "selection")).lower()
            if "corner" in blob or "escante" in blob:
                found = _num(row.get("odds"))
                if found:
                    return found
        return None


    def _attach_accuracy(self, analysis: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Anexa score de acertividade e perguntas proativas (não altera risk gates)."""
        try:
            from analysis_accuracy_toolkit import attach_to_analysis
            return attach_to_analysis(analysis, payload)
        except Exception as exc:
            analysis = dict(analysis or {})
            analysis["accuracy_pack"] = {"schema": "aura-accuracy-pack-1", "error": str(exc), "safe": False}
            return analysis

    def _validate_integrity(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Checagem máxima de veracidade (SokkerPro): identidade, frescor,
        limites físicos, consistência stats/eventos, conflitos de fonte e
        monotonicidade por fixture. BLOQUEIA análise se status=BLOCK.
        """
        try:
            from data_veracity import verify_payload
            result = verify_payload(payload)
        except Exception as exc:
            # Fail-closed: se o módulo falhar, bloqueia com motivo explícito
            return {
                "status": "BLOCK",
                "issues": ["veracity_module_error"],
                "warnings": [],
                "checks": {"module": "BLOCK"},
                "veracity_score": 0,
                "checked_at": int(time.time() * 1000),
                "fixture_id": str(payload.get("fixtureId") or ""),
                "error": str(exc),
                "safe_to_analyze": False,
                "safe_to_trade": False,
                "policy": "max_veracity_v1",
            }
        # Garante chaves legadas usadas pelo restante do engine
        result.setdefault("issues", [])
        result.setdefault("warnings", [])
        result.setdefault("status", "BLOCK")
        return result

    def _integrity_label(self, code: str) -> str:
        labels = {
            "corners_authoritative_sources_conflict": "escanteios das fontes oficiais não reconciliam",
            "corners_stats_events_mismatch": "escanteios em estatísticas e eventos divergem",
            "capture_stale_over_45s": "captura antiga demais",
            "capture_older_than_15s": "captura com atraso",
            "capture_timestamp_missing": "timestamp ausente",
            "capture_timestamp_invalid": "timestamp inválido",
            "teams_invalid": "times ausentes ou inválidos",
            "fixture_id_missing": "fixture ausente",
            "critical_features_missing": "features críticas ausentes ou inválidas",
            "minute_regressed": "minuto regrediu de forma impossível",
            "corners_regressed": "escanteios regrediram",
            "score_regressed": "placar regrediu",
            "score_events_conflict": "placar não bate com eventos de gol",
            "score_out_of_range": "placar fora de faixa",
            "shots_on_gt_shots_home": "chutes no gol > chutes (casa)",
            "shots_on_gt_shots_away": "chutes no gol > chutes (fora)",
            "possession_home_out_of_range": "posse casa inválida",
            "possession_away_out_of_range": "posse fora inválida",
            "xg_home_out_of_range": "xG casa fora de faixa",
            "xg_away_out_of_range": "xG fora fora de faixa",
            "veracity_module_error": "falha no módulo de veracidade",
            "minute_out_of_range": "minuto fora de faixa",

        }
        return labels.get(str(code), str(code).replace("_", " "))

    def _decorate_analysis_contract(self, analysis: Dict[str, Any], price: Optional[float] = None) -> Dict[str, Any]:
        """Expõe explicação e risco em formato estável para HUD, chat e API."""
        integrity = analysis.get("data_integrity") or {}
        issues = list(integrity.get("issues") or [])
        warnings = list(integrity.get("warnings") or [])
        signal = str(analysis.get("signal") or "HOLD").upper()
        prob = float(analysis.get("corner_prob") or analysis.get("goal_prob") or 0.0)
        analytics = analysis.get("analytics") or {}
        edge = analytics.get("edgeCalculado", analysis.get("edge"))
        try:
            edge = float(edge) if edge is not None else None
        except (TypeError, ValueError):
            edge = None
        if issues:
            reason = "BLOQUEADO por integridade: " + ", ".join(self._integrity_label(x) for x in issues[:4]) + "."
        elif signal == "WATCH_CORNER":
            if price is None:
                reason = f"Observação: probabilidade do modelo {prob:.1%}; sem odds/mercado validado, portanto sem edge e sem entrada."
            else:
                reason = f"Observação: probabilidade do modelo {prob:.1%}; aguardando edge e confirmação do mercado."
        elif signal in ("HOLD", "WATCH_ATTACK"):
            reason = "Sem entrada: dados ainda não formam um sinal operacional validado."
            if price is None:
                reason += " Odds/mercado não validados."
        elif signal in ("BUY_CORNER", "BUY_GOAL"):
            reason = f"Sinal preliminar {signal}: probabilidade {prob:.1%}. Aguardando aprovação do Risk Manager."
        elif signal == "BLOCK":
            reason = "BLOQUEADO pelo gate fail-closed; nenhuma entrada autorizada."
        else:
            reason = f"Estado {signal}: sem autorização de entrada."
        if warnings and not issues:
            reason += " Avisos: " + ", ".join(self._integrity_label(x) for x in warnings[:3]) + "."
        intensity = float(analytics.get("intensidade") or 0.0)
        dda = float(analytics.get("velocidadeAtaques_dDA_dt") or 0.0)
        analysis["pressure"] = analysis.get("pressure") if analysis.get("pressure") is not None else round(max(0.0, min(1.0, intensity / 3.0)), 4)
        analysis["momentum"] = analysis.get("momentum") if analysis.get("momentum") is not None else round(max(0.0, min(1.0, abs(dda) / 2.0)), 4)
        analysis["regime"] = analysis.get("regime") or analysis.get("skill_regime") or analysis.get("strategy") or "neutral"
        analysis["decision"] = signal
        analysis["reason"] = reason
        analysis["explanation"] = reason
        analysis["market"] = analysis.get("market") or ("Escanteios ao vivo" if signal in ("WATCH_CORNER", "BUY_CORNER") else None)
        analysis["market_prob"] = analysis.get("market_prob")
        analysis["edge"] = edge
        analysis["uncertainty"] = round(max(0.0, min(1.0, 1.0 - min(prob, 0.99))), 6)
        analysis["risk"] = {
            "state": "PENDING" if signal in ("BUY_CORNER", "BUY_GOAL") and not issues else "BLOCK",
            "approved": False,
            "reason": reason,
            "exposure": 0.0,
            "kelly": float(analysis.get("kelly") or 0.0),
        }
        return analysis

    def ingest(self, raw: Dict[str, Any], *, write_ledger: bool = True) -> Dict[str, Any]:
        payload = raw.get("dados") if isinstance(raw.get("dados"), dict) else raw
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload inválido"}
        integrity = self._validate_integrity(payload)
        self.last_integrity = integrity
        fid_key = str(payload.get("fixtureId") or payload.get("fixture") or "").strip()
        if fid_key:
            self.last_payloads[fid_key] = payload
        if integrity["status"] == "BLOCK":
            self.stats["integrity_blocks"] += 1
        elif integrity["status"] == "WARN":
            self.stats["integrity_warnings"] += 1
        if "fixture_id_missing" in integrity["issues"]:
            return {"ok": False, "error": "fixtureId ausente", "data_integrity": integrity}

        ctx = self._get_ctx(payload)
        seconds = self._resolve_seconds(payload)
        minute = min(MAX_HISTORY - 1, seconds // 60)
        feature_quality = self._bind(ctx, payload, minute)
        self.stats["ingested"] += 1

        # P0: critical feature gate — BLOCKED_BY_DATA before any operational signal
        critical_missing = list(feature_quality.get("critical_missing_fields") or [])
        if critical_missing:
            integrity = dict(integrity)
            issues = list(integrity.get("issues") or [])
            issues.append("critical_features_missing")
            integrity["issues"] = issues
            integrity["status"] = "BLOCK"
            integrity["critical_missing_fields"] = critical_missing
            self.last_integrity = integrity
            self.stats["integrity_blocks"] = self.stats.get("integrity_blocks", 0) + 1
            blocked = {
                "ok": True,
                "fixtureId": ctx.fixture_id,
                "signal": "BLOCKED_BY_DATA",
                "decision": "BLOCKED_BY_DATA",
                "corner_prob": 0.0,
                "goal_prob": 0.0,
                "kelly": 0.0,
                "approved": False,
                "data_integrity": integrity,
                "accuracy_pack": self._attach_accuracy({"data_integrity": integrity}, payload).get("accuracy_pack"),
                "feature_quality": feature_quality,
                "reason": "BLOCKED_BY_DATA: campos críticos ausentes ou inválidos: " + ", ".join(critical_missing),
                "explanation": "BLOCKED_BY_DATA: campos críticos ausentes ou inválidos: " + ", ".join(critical_missing),
                "risk": {
                    "state": "BLOCK",
                    "approved": False,
                    "reason": "critical_features_missing",
                    "exposure": 0.0,
                    "kelly": 0.0,
                },
                "analytics": {},
                "skill_kills": ["critical_features_missing"] + critical_missing,
            }
            try:
                from data_store import enqueue_feature_quality
                enqueue_feature_quality(
                    fixture_id=str(ctx.fixture_id),
                    captured_at=_extract_captured_at(payload),
                    quality=feature_quality,
                )
            except Exception:
                pass
            try:
                from corner_intelligence import analyze_corners
                blocked["corner_intelligence"] = analyze_corners(blocked, payload if isinstance(payload, dict) else {})
            except Exception as _ci_exc:
                blocked["corner_intelligence"] = {"ok": False, "error": str(_ci_exc), "product": "corner_analysis_central"}
            try:
                from hawkes_corners import build_hawkes_from_payload
                blocked["hawkes_corners"] = build_hawkes_from_payload(payload if isinstance(payload, dict) else {}, analysis=blocked)
            except Exception as _hk_exc:
                blocked["hawkes_corners"] = {"ok": False, "error": str(_hk_exc)}
            if write_ledger:
                blocked = self._persist_ledger(payload, blocked)
            self.last_analysis[ctx.fixture_id] = blocked
            return blocked

        # Persist quality summary for every successful bind path (once per ingest)
        try:
            from data_store import enqueue_feature_quality
            enqueue_feature_quality(
                fixture_id=str(ctx.fixture_id),
                captured_at=_extract_captured_at(payload),
                quality=feature_quality,
            )
        except Exception:
            pass

        alpha_h = self.kb.get_alpha(ctx.home)
        alpha_a = self.kb.get_alpha(ctx.away)
        match_alpha = (alpha_h + alpha_a) / 2.0

        deriv = self._derivatives(ctx, minute)
        lam = max(0.01, ctx.base_corner_rate * 5.0 * deriv["intensity"] * match_alpha)
        p5 = _poisson_at_least(lam, 1)

        mode = None
        if BURST_35[0] <= seconds <= BURST_35[1]:
            mode = "1H_LATE_BURST_35"
        elif RACE_80[0] <= seconds <= RACE_80[1]:
            mode = "2H_LATE_RACE_80"

        signal = "HOLD"
        edge = 0.0
        kelly = 0.0
        price = self._parse_odds(payload)
        if mode and price and price > 1.0:
            edge = p5 - (1.0 / price)
            if edge >= MIN_EDGE and deriv["total_dda"] >= MIN_DDA_DT:
                signal = "BUY_CORNER"
                od = price - 1.0
                raw_k = ((p5 * price) - 1.0) / od if od > 0 else 0.0
                kelly = max(0.0, min(raw_k * 0.25, MAX_KELLY)) if raw_k > 0 else 0.0
            elif deriv["total_dda"] > MIN_DDA_DT * 2.2:
                signal = "ATTACK_IMMINENT"
        # sem odds: ainda gera sinal observacional para HUD
        if signal == "HOLD":
            if p5 >= 0.55 and deriv["total_dda"] >= MIN_DDA_DT:
                signal = "WATCH_CORNER"
                kelly = max(0.0, min(0.05, (p5 - 0.5) * 0.2))
            elif deriv["total_dda"] > MIN_DDA_DT * 2.0:
                signal = "WATCH_ATTACK"

        # --- Weight of Money (v12.6.0) -------------------------------------
        # Puramente analítico/defensivo: só pode vetar um BUY_CORNER já
        # decidido acima, ou marcar confluência — nunca gera sinal sozinho.
        market_stats = self._parse_market_stats(payload)
        asian_corner_odds = market_stats.get("asian_corner_odds")
        odds_velocity = self.market.calculate_odds_velocity(ctx.fixture_id, asian_corner_odds)
        wom_confluence = False
        wom_blocked = False
        if signal == "BUY_CORNER":
            if not validate_trade_with_market(signal, odds_velocity):
                # Filtro Anti-Red: odd asiática subindo (dinheiro saindo do
                # Over) contra o sinal de campo -> aborta a entrada.
                signal = "BLOCKED_BY_MARKET"
                wom_blocked = True
                self.stats["wom_blocked"] += 1
            elif odds_velocity <= -5.0:
                # Confluência: campo (pressão/xG) e mercado (odd derretendo)
                # concordam — não altera o sinal, só sinaliza força extra.
                wom_confluence = True
                self.stats["wom_confluence"] += 1

        # goal proxy from xG intensity
        goal_p = max(0.0, min(0.85, 0.06 + deriv["intensity"] * 0.12 + abs(deriv["total_acc"]) * 0.05))
        analysis = {
            "ok": True,
            "schema": "cornerai-local-analysis-1",
            "skill_version": (skill_info().get("version") if callable(skill_info) else "unknown"),
            "ts": int(time.time() * 1000),
            "fixtureId": ctx.fixture_id,
            "teams": {"home": ctx.home, "away": ctx.away},
            "clock": payload.get("clock"),
            "minute": payload.get("minute"),
            "extraMinute": payload.get("extraMinute"),
            "liveStatus": payload.get("liveStatus") or payload.get("status"),
            "score": payload.get("score"),
            "quality": payload.get("quality"),
            "h2h": payload.get("h2h"),
            "oddsMarkets": payload.get("oddsMarkets"),
            "oddsHistory": payload.get("oddsHistory"),
            "market_stats": payload.get("market_stats"),
            "backend": BACKEND,
            "strategy": mode,
            "signal": signal,
            # campos flat para HUD / extensão
            "corner_prob": round(p5, 6),
            "goal_prob": round(goal_p, 6),
            "kelly": round(kelly, 6),
            "analytics": {
                "probabilidadeEscanteio5Min": round(p5, 6),
                "probabilidadeGol": round(goal_p, 6),
                "velocidadeAtaques_dDA_dt": round(deriv["total_dda"], 6),
                "aceleracaoxG_d2xG_dt2": round(deriv["total_acc"], 6),
                "intensidade": round(deriv["intensity"], 6),
                "lambda": round(lam, 6),
                "pesoIA": round(match_alpha, 4),
                "edgeCalculado": round(edge, 6),
                "gestaoKellyRecomendada": round(kelly, 6),
                "odds": price,
            },
            "device": backend_info(),
            "stats": payload.get("stats") or payload.get("estatisticas") or {},
            "events": payload.get("events") or payload.get("eventos") or [],
            "data_integrity": integrity,
            "wom": {
                "asian_corner_line": market_stats.get("asian_corner_line"),
                "asian_corner_odds": asian_corner_odds,
                "odds_velocity": round(odds_velocity, 4),
                "confluence": wom_confluence,
                "blocked": wom_blocked,
            },
        }

        # Skill 10.x production gates: quality is explicit and never coerced to zero.
        if _data_quality is not None:
            try:
                analysis["skill_data_quality"] = _data_quality(analysis)
            except Exception:
                analysis["skill_data_quality"] = {"status": "UNKNOWN", "issues": ["quality_gate_error"]}
        else:
            analysis["skill_data_quality"] = {"status": "UNKNOWN", "issues": ["skill_runtime_unavailable"]}
        analysis["skill_regime"] = mode or "neutral"
        analysis["skill_kills"] = []
        if not price and signal == "BUY_CORNER":
            analysis["skill_kills"].append("market_odds_missing")
            analysis["signal"] = "HOLD"
        if wom_blocked:
            analysis["skill_kills"].append("smart_money_divergence")
        if analysis.get("skill_data_quality", {}).get("status") == "BLOCK":
            analysis["skill_kills"].append("data_quality_block")
            analysis["signal"] = "BLOCK"
        if integrity["status"] == "BLOCK":
            analysis["skill_kills"].extend(integrity["issues"][:6])
            analysis["signal"] = "BLOCKED_BY_DATA"
            analysis["decision"] = "BLOCKED_BY_DATA"
            analysis["kelly"] = 0.0
            analysis["approved"] = False
            if isinstance(analysis.get("risk"), dict):
                analysis["risk"]["state"] = "BLOCK"
                analysis["risk"]["approved"] = False
                analysis["risk"]["exposure"] = 0.0
                analysis["risk"]["kelly"] = 0.0
        elif integrity["status"] == "WARN":
            analysis["skill_kills"].extend(integrity["warnings"][:4])

        self._decorate_analysis_contract(analysis, price=price)
        analysis = self._attach_accuracy(analysis, payload)
        # P1 Fase 4: challenger shadow — NÃO altera signal/decision reais
        try:
            from model_shadow import shadow_predict, baseline_from_payload
            from features import engineer_sequence, frame_from_stats
            stats = payload.get("stats") or payload.get("estatisticas") or {}
            frame = frame_from_stats(stats if isinstance(stats, dict) else {})
            # minimal history from current frame only
            feats = engineer_sequence([frame] if isinstance(frame, list) else [])
            base_info = baseline_from_payload(
                payload,
                horizon_sec=300.0,
                legacy_lambda=float(analysis.get("analytics", {}).get("lambda") or 0) or None,
            )
            p_base = float(base_info.get("p_baseline") or p5)
            shadow = shadow_predict(feats, p_base)
            analysis["shadow_model"] = shadow
            analysis["baseline_hawkes"] = {
                "p_baseline": base_info.get("p_baseline"),
                "intensity": base_info.get("intensity"),
                "event_types": base_info.get("event_types"),
                "params_version": base_info.get("params_version"),
            }
        except Exception as _shadow_exc:
            analysis["shadow_model"] = {"error": str(_shadow_exc), "shadow": True}
        analysis["feature_quality"] = feature_quality
        # Fase 3: contrato de alvo (não confundir com over_9_5 / GREEN-RED)
        analysis["target_contract"] = {
            "target_name": "next_corner_within_horizon",
            "primary_product": "next_corner_within_300s",
            "horizons_sec": [60, 180, 300, 600],
            "label_version": "label_v1_p0_censor",
            "note": "label 0/1 apenas com janela completa; incompleta = censored NULL",
        }
        
        try:
            from corner_intelligence import analyze_corners
            analysis["corner_intelligence"] = analyze_corners(analysis, payload if isinstance(payload, dict) else {})
        except Exception as _ci_exc:
            analysis["corner_intelligence"] = {"ok": False, "error": str(_ci_exc), "product": "corner_analysis_central"}
        try:
            from hawkes_corners import build_hawkes_from_payload
            hk = build_hawkes_from_payload(payload if isinstance(payload, dict) else {}, analysis=analysis, base_rate=float(ctx.base_corner_rate or 0.12))
            analysis["hawkes_corners"] = hk
            # merge into baseline_hawkes for observability
            bh = dict(analysis.get("baseline_hawkes") or {})
            bh["lambda_match"] = (hk.get("lambda") or {}).get("match")
            bh["lambda_home_for"] = (hk.get("lambda") or {}).get("home_for")
            bh["lambda_away_for"] = (hk.get("lambda") or {}).get("away_for")
            bh["lambda_home_conceded"] = (hk.get("lambda") or {}).get("home_conceded")
            bh["lambda_away_conceded"] = (hk.get("lambda") or {}).get("away_conceded")
            bh["defensive_stress"] = hk.get("defensive_stress")
            bh["p_match_5m_hawkes"] = hk.get("p_match_5m")
            bh["model"] = "hawkes_corners_v1"
            analysis["baseline_hawkes"] = bh
        except Exception as _hk_exc:
            analysis["hawkes_corners"] = {"ok": False, "error": str(_hk_exc)}
        if write_ledger:
            analysis = self._persist_ledger(payload, analysis)
        self.last_analysis[ctx.fixture_id] = analysis
        if signal != "HOLD":
            self.stats["alerts"] += 1
        return analysis


    def _persist_ledger(self, payload: Dict[str, Any], analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Append raw event + decision log before external side-effects.

        Rules:
        - Ledger failure on an otherwise operational signal → BLOCKED_BY_LEDGER.
        - If already BLOCKED_BY_DATA / BLOCK, keep that signal and attach ledger_error.
        """
        prior_signal = str(analysis.get("signal") or "")
        already_blocked = prior_signal in (
            "BLOCKED_BY_DATA", "BLOCKED_BY_LEDGER", "BLOCK", "BLOCKED_BY_MODEL", "BLOCKED_BY_MARKET", "BLOCKED_BY_RISK"
        )

        def _mark_ledger_fail(analysis: Dict[str, Any], err: str) -> Dict[str, Any]:
            analysis = dict(analysis)
            analysis["ledger_error"] = err
            analysis["ledger"] = {"ok": False, "error": err}
            if already_blocked:
                # preserve primary block reason
                return analysis
            analysis["signal"] = "BLOCKED_BY_LEDGER"
            analysis["decision"] = "BLOCKED_BY_LEDGER"
            analysis["kelly"] = 0.0
            analysis["approved"] = False
            analysis["reason"] = f"BLOCKED_BY_LEDGER: {err}"
            if isinstance(analysis.get("risk"), dict):
                analysis["risk"]["state"] = "BLOCK"
                analysis["risk"]["approved"] = False
                analysis["risk"]["exposure"] = 0.0
                analysis["risk"]["kelly"] = 0.0
            return analysis

        try:
            from data_store import (
                append_raw_event,
                append_decision_log,
                append_data_quality_flag,
            )
        except Exception as exc:
            return _mark_ledger_fail(analysis, str(exc))

        fid = str(analysis.get("fixtureId") or payload.get("fixtureId") or "").strip()
        ev = append_raw_event(payload, fixture_id=fid, source="telemetry")
        if not ev.get("ok"):
            return _mark_ledger_fail(analysis, str(ev.get("error")))

        event_id = ev.get("event_id")
        analysis = dict(analysis)
        analysis["event_id"] = event_id

        integrity = analysis.get("data_integrity") or {}
        for code in list(integrity.get("issues") or []):
            append_data_quality_flag(
                str(code), severity="BLOCK", fixture_id=fid,
                event_id=event_id or "", message=str(code),
            )
        for code in list(integrity.get("warnings") or []):
            append_data_quality_flag(
                str(code), severity="WARN", fixture_id=fid,
                event_id=event_id or "", message=str(code),
            )
        crit = (analysis.get("feature_quality") or {}).get("critical_missing_fields") or []
        for field in crit:
            append_data_quality_flag(
                "critical_feature_missing", severity="BLOCK", fixture_id=fid,
                event_id=event_id or "", message=str(field), details={"field": field},
            )

        dec = append_decision_log(analysis, event_id=event_id)
        if not dec.get("ok"):
            return _mark_ledger_fail(analysis, str(dec.get("error")))

        analysis["decision_id"] = dec.get("decision_id")
        analysis["ledger"] = {"event_id": event_id, "decision_id": dec.get("decision_id"), "ok": True}
        return analysis


    def get_analysis(self, fixture_id: str) -> Optional[Dict[str, Any]]:
        return self.last_analysis.get(str(fixture_id))

    def recompute(self, fixture_id: str) -> Optional[Dict[str, Any]]:
        payload = self.last_payloads.get(str(fixture_id))
        if not isinstance(payload, dict):
            return self.get_analysis(str(fixture_id))
        return self.ingest(payload)

    def status(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "engine": "CornerAI LocalAI",
            "version": "12.7.0-RECONSOLIDADO",
            "device": backend_info(),
            "trained": self.stats.get("trained", 0),
            "ingested": self.stats.get("ingested", 0),
            "alerts": self.stats.get("alerts", 0),
            "teams_weighted": len(self.kb.weights),
            "contexts": len(self.contexts),
            "teams_learned": len(self.kb.weights),
            "stats": self.stats,
            "data_integrity": self.last_integrity,
        }


# ---------------------------------------------------------------------------
# Compatibilidade AURA QUANT-X 12.7.0 — API usada pelo servidor atual.
# O motor permanece local, fail-closed e paper-only.
# ---------------------------------------------------------------------------
DB_NAME = os.environ.get("AURA_DB_PATH", str(Path(__file__).resolve().parent / "aura_quant_x.db"))

def init_db_wal() -> None:
    try:
        from data_store import init_schema
        init_schema(DB_NAME)
    except Exception as exc:
        logger.exception("Falha ao inicializar schema WAL do Engine: %s", exc)


def _compat_analyze(self, payload: Dict[str, Any]) -> Dict[str, Any]:
    return self.ingest(payload, write_ledger=False)


def _compat_feedback(self, equipe: str, resultado: str, alpha: float, features: Optional[List[float]] = None) -> None:
    # Feedback só atualiza memória/estatística; não executa ordem nem promove modelo.
    try:
        self.stats["feedback"] = int(self.stats.get("feedback", 0)) + 1
    except Exception as exc:
        logger.debug("Falha ao registrar feedback legado: %s", exc)


def _compat_shutdown(self) -> None:
    return None


if not hasattr(LocalAIEngine, "analyze"):
    LocalAIEngine.analyze = _compat_analyze
if not hasattr(LocalAIEngine, "feedback"):
    LocalAIEngine.feedback = _compat_feedback
if not hasattr(LocalAIEngine, "shutdown"):
    LocalAIEngine.shutdown = _compat_shutdown

_engine_singleton: Optional[LocalAIEngine] = None

def get_local_ai_engine() -> LocalAIEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = LocalAIEngine()
    return _engine_singleton


class _LazyEngineProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_local_ai_engine(), name)


ENGINE = _LazyEngineProxy()
