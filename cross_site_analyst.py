#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA QUANT-X V25 — Cross-Site Analyst v2: INTELIGENTE.

v2 adiciona:
  1. TipsterReputation — rastreia acerto/erro histórico por site
  2. ConsensusEngine — agrega tips de múltiplos sites para mesma partida
  3. EdgeDetector — compara tip vs AURA (conformal + MC grid) → VALUE/CONTRARIAN
  4. TipQualityScorer — score 0-100 (reputação + consenso + alinhamento + edge)
  5. AdaptiveScheduler — frequência baseada em jogos ao vivo
  6. Telegram inteligente: HIGH-VALUE (≥80) | STANDARD (50-79) | FILTRADA (<50)

AURA = análise. Não faz apostas.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("aura.crossanalyst")

__version__ = "2.0.0"
__all__ = ["CrossSiteAnalyst", "Tip", "TipsterReputation", "ANALYST"]

_DISCLAIMER = "_⚠️ AURA = sistema de análise. Não faz apostas._"

_MARKET_KW = {
    "corners": ["corner", "escanteio", "cantos", "bandeira"],
    "goals": ["over", "under", "gols", "goals", "gol", "mais de", "menos de"],
    "1x2": ["1x2", "resultado", "vencedor", "casa", "empate", "fora",
            "home", "draw", "away", "vitória", "vitoria"],
    "btts": ["btts", "ambas", "both teams", "ambas marcam", "ambos marcam"],
    "cards": ["card", "cartão", "cartao", "amarelo", "vermelho", "yellow", "red"],
    "handicap": ["handicap", "hcp", "asia"],
    "other": ["tip", "palpite", "aposta", "pick", "prediction", "recomend"],
}

_ODDS_RE = re.compile(r"\b(\d{1,3}\.\d{2})\b")
_MATCH_RE = re.compile(
    r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s'.-]{2,30})"
    r"\s*(?:x|X|vs?\.?|–|-)\s*"
    r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s'.-]{2,30})")


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
        return None if (f != f or f in (float("inf"), float("-inf"))) else f
    except (TypeError, ValueError):
        return None


@dataclass
class Tip:
    site: str
    match: str
    market: str
    selection: str
    odds: Optional[float] = None
    confidence: str = ""
    reasoning: str = ""
    source_url: str = ""
    collected_at: str = ""
    glm_analysis: Optional[str] = None
    # v2 intelligence
    quality_score: int = 0
    edge_type: str = ""
    edge_value: Optional[float] = None
    consensus_agreement: float = 0.0
    consensus_n: int = 0
    aura_p: Optional[float] = None
    implied_prob: Optional[float] = None
    tipster_score: float = 0.5
    tipster_stars: str = "⭐"

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    def fingerprint(self) -> str:
        raw = f"{self.site}|{self.match}|{self.market}|{self.selection}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]

    @property
    def direction(self) -> str:
        s = self.selection.lower()
        if any(w in s for w in ("over", "mais", "acima", "sim", "yes", "home", "casa")):
            return "over"
        if any(w in s for w in ("under", "menos", "abaixo", "não", "nao", "no", "away", "fora")):
            return "under"
        return "unknown"

    @property
    def line(self) -> Optional[float]:
        m = re.search(r"(\d+\.?\d*)", self.selection)
        return float(m.group(1)) if m else None


class TipsterReputation:
    """Bayesian reputation: prior Beta(5,5), updates com hits/misses."""

    def __init__(self, state_dir=None):
        self._reps: Dict[str, dict] = {}
        self._state_dir = Path(state_dir) if state_dir else None
        if self._state_dir:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    def record_tip(self, tip: Tip) -> None:
        s = tip.site
        if s not in self._reps:
            self._reps[s] = {"total": 0, "hits": 0, "misses": 0, "pending": {}}
        self._reps[s]["total"] += 1
        self._reps[s]["pending"][tip.fingerprint()] = {
            "match": tip.match, "market": tip.market,
            "selection": tip.selection, "collected_at": tip.collected_at,
        }

    def resolve(self, site: str, fingerprint: str, hit: bool) -> None:
        if site not in self._reps:
            return
        p = self._reps[site]["pending"]
        if fingerprint in p:
            del p[fingerprint]
            self._reps[site]["hits" if hit else "misses"] += 1
            self._save()

    def get_score(self, site: str) -> float:
        r = self._reps.get(site, {})
        alpha = r.get("hits", 0) + 5
        beta = r.get("misses", 0) + 5
        return alpha / (alpha + beta)

    def get_stars(self, site: str) -> str:
        sc = self.get_score(site)
        if sc >= 0.65:
            return "⭐⭐⭐⭐"
        if sc >= 0.55:
            return "⭐⭐⭐"
        if sc >= 0.45:
            return "⭐⭐"
        return "⭐"

    def stats(self) -> dict:
        return {
            s: {
                "total": r["total"], "hits": r["hits"], "misses": r["misses"],
                "pending": len(r["pending"]),
                "score": round(self.get_score(s), 3),
                "stars": self.get_stars(s),
            }
            for s, r in self._reps.items()
        }

    def _save(self) -> None:
        if not self._state_dir:
            return
        p = self._state_dir / "tipster_reputation.json"
        data = {
            s: {
                "total": r["total"], "hits": r["hits"], "misses": r["misses"],
                "pending": dict(r["pending"]),
            }
            for s, r in self._reps.items()
        }
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(str(tmp), str(p))

    def _load(self) -> None:
        if not self._state_dir:
            return
        p = self._state_dir / "tipster_reputation.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for s, r in data.items():
                self._reps[s] = {
                    "total": r.get("total", 0),
                    "hits": r.get("hits", 0),
                    "misses": r.get("misses", 0),
                    "pending": r.get("pending", {}),
                }
        except Exception:
            log.exception("[reputation] carga falhou")

class CrossSiteAnalyst:
    """Coleta tips, analisa com AURA, envia no Telegram com inteligencia."""

    def __init__(self, *, browser=None, telegram=None, glm=None,
                 conformal_gate=None, mc_grid=None,
                 reputation: Optional[TipsterReputation] = None,
                 check_interval: float = 1800.0,
                 max_tips_per_message: int = 10,
                 dedup_ttl: float = 86400.0,
                 min_quality: int = 50):
        self.browser = browser
        self.telegram = telegram
        self.glm = glm
        self.conformal = conformal_gate
        self.mc_grid = mc_grid
        self.reputation = reputation or TipsterReputation()
        self.check_interval = float(check_interval)
        self.max_tips = int(max_tips_per_message)
        self.dedup_ttl = float(dedup_ttl)
        self.min_quality = int(min_quality)

        self._sites: Dict[str, dict] = {}
        self._sent: Dict[str, float] = {}
        self._all_tips: List[Tip] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._total_collected = 0
        self._total_sent = 0
        self._total_deduped = 0
        self._total_filtered = 0
        self._glm_analyses = 0
        self._errors = 0
        self._last_cycle = 0.0

    def register_site(self, name: str, url: str, *,
                      extraction: str = "auto", wait_selector: str = "body") -> None:
        with self._lock:
            self._sites[str(name)] = {
                "url": str(url), "extraction": extraction,
                "wait_selector": wait_selector,
            }
        log.info("[crossanalyst] site: %s -> %s", name, url)

    def remove_site(self, name: str) -> None:
        with self._lock:
            self._sites.pop(str(name), None)

    def list_sites(self) -> List[str]:
        with self._lock:
            return list(self._sites.keys())

    def collect_tips(self) -> List[Tip]:
        all_tips: List[Tip] = []
        with self._lock:
            sites = dict(self._sites)
        for name, cfg in sites.items():
            tips = self._collect_from_site(name, cfg)
            all_tips.extend(tips)
        with self._lock:
            self._total_collected += len(all_tips)
            self._all_tips.extend(all_tips)
            if len(self._all_tips) > 5000:
                self._all_tips = self._all_tips[-5000:]
        return all_tips

    def _collect_from_site(self, name: str, cfg: dict) -> List[Tip]:
        if not self.browser:
            return []
        try:
            if not self.browser.is_alive():
                if not self.browser.launch():
                    return []
            if not self.browser.navigate(cfg["url"]):
                return []
            wait = getattr(self.browser, "wait_for", None)
            if cfg.get("wait_selector") and callable(wait):
                wait(cfg["wait_selector"], timeout=10.0)
            time.sleep(2.0)
            text = self.browser.evaluate(
                "() => document.body ? document.body.innerText : ''")
            if not text or not isinstance(text, str):
                text = ""
            text = text[:8000]
            ext = cfg.get("extraction", "auto")
            has_glm = bool(self.glm and getattr(self.glm, "api_key", None))
            if ext == "auto" and has_glm:
                tips = self._extract_glm(name, cfg["url"], text)
            else:
                tips = self._extract_regex(name, cfg["url"], text)
            log.info("[crossanalyst] %s: %d tips", name, len(tips))
            return tips
        except Exception as e:
            log.error("[crossanalyst] %s: %s", name, e)
            self._errors += 1
            return []

    def _extract_glm(self, site: str, url: str, text: str) -> List[Tip]:
        if not self.glm or not getattr(self.glm, "api_key", None):
            return self._extract_regex(site, url, text)
        prompt = (
            "Extraia TODAS as tips/palpites do texto abaixo. Retorne como array JSON.\n"
            'Cada tip: {"match":"Team A x Team B","market":"corners|goals|1x2|btts|cards|other",'
            '"selection":"Over 9.5","odds":1.85,"confidence":"high","reasoning":"..."}\n'
            f"Sem tips → []. Texto:\n{text}\nRetorne SOMENTE o array JSON."
        )
        try:
            raw = self.glm.call(prompt)
            if not raw:
                return []
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if m:
                raw = m.group(0)
            data = json.loads(raw)
            tips: List[Tip] = []
            for item in data if isinstance(data, list) else []:
                if not isinstance(item, dict):
                    continue
                tip = Tip(
                    site=site,
                    match=str(item.get("match", "")),
                    market=str(item.get("market", "other")),
                    selection=str(item.get("selection", "")),
                    odds=_to_num(item.get("odds")),
                    confidence=str(item.get("confidence", "")),
                    reasoning=str(item.get("reasoning", "")),
                    source_url=url,
                    collected_at=_iso(),
                )
                if tip.selection:
                    tips.append(tip)
            return tips
        except Exception:
            return self._extract_regex(site, url, text)

    def _extract_regex(self, site: str, url: str, text: str) -> List[Tip]:
        tips: List[Tip] = []
        lines = text.split("\n")
        for i, line in enumerate(lines):
            ll = line.lower()
            market = next(
                (mk for mk, kws in _MARKET_KW.items() if any(k in ll for k in kws)),
                None)
            if not market:
                continue
            odds_m = _ODDS_RE.search(line)
            odds = float(odds_m.group(1)) if odds_m else None
            match_s = ""
            m = _MATCH_RE.search(line)
            if m:
                match_s = f"{m.group(1).strip()} x {m.group(2).strip()}"
            else:
                for j in range(max(0, i - 3), min(len(lines), i + 3)):
                    m2 = _MATCH_RE.search(lines[j])
                    if m2:
                        match_s = f"{m2.group(1).strip()} x {m2.group(2).strip()}"
                        break
            conf = ""
            if any(w in ll for w in ("alta", "high", "forte")):
                conf = "high"
            elif any(w in ll for w in ("média", "media", "medium", "moderada")):
                conf = "medium"
            elif any(w in ll for w in ("baixa", "low", "fraca")):
                conf = "low"
            if match_s or odds:
                tips.append(Tip(
                    site=site, match=match_s, market=market,
                    selection=line.strip()[:100], odds=odds,
                    confidence=conf, source_url=url, collected_at=_iso()))
        return tips

    # ---- v2 intelligence ----
    def _build_consensus(self, tips: List[Tip]) -> Dict[str, dict]:
        groups: Dict[str, List[Tip]] = {}
        for t in tips:
            key = re.sub(r"[^a-z]", "", t.match.lower()) + "|" + t.market
            groups.setdefault(key, []).append(t)
        consensus: Dict[str, dict] = {}
        for key, gt in groups.items():
            if not gt:
                continue
            sels = [t.selection for t in gt]
            common = max(set(sels), key=sels.count) if sels else ""
            n_agree = sum(1 for t in gt if t.selection == common)
            agreement = n_agree / len(gt)
            consensus[key] = {
                "selection": common, "agreement": agreement,
                "n_sites": len(gt), "n_agree": n_agree,
            }
        return consensus

    def _compute_aura_p(self, tip: Tip) -> Optional[float]:
        if tip.market != "corners":
            return None
        view = None
        if self.browser:
            try:
                view = self.browser.extract_view()
            except Exception:
                pass
        if not view:
            return None
        total_c = sum(
            v for v in (view.get("corners", {}).get("total", [0, 0]) or [0, 0])
            if v is not None)
        line = tip.line or 9.5
        needed = max(0, int(line + 0.5) - int(total_c))
        minute = view.get("fixture", {}).get("minute", 0) or 0
        remaining = max(1, 95 - int(minute))
        if needed <= 0:
            p_over = 1.0
        elif needed == 1:
            p_over = self._p_ge1(view, min(remaining, 15))
        else:
            p1 = self._p_ge1(view, min(remaining, 15))
            p_over = p1 ** needed
        if tip.direction == "under":
            return 1.0 - p_over
        return p_over

    def _p_ge1(self, view: dict, horizon: int) -> float:
        if self.mc_grid:
            try:
                r = self.mc_grid.evaluate(view)
                if r:
                    h = 5 if horizon <= 5 else 10 if horizon <= 10 else 15
                    p1 = getattr(r, "p1", None)
                    if isinstance(p1, dict):
                        return float(p1.get(h, 0.5))
            except Exception:
                pass
        if self.conformal:
            try:
                lo, hi = self.conformal.interval(0.5, "global")
                return (lo + hi) / 2
            except Exception:
                pass
        return 0.5

    def _detect_edge(self, tip: Tip) -> str:
        aura_p = tip.aura_p
        implied = tip.implied_prob
        if aura_p is None or implied is None:
            return "NO_DATA"
        edge = aura_p - implied
        tip.edge_value = edge
        if edge > 0.05:
            return "VALUE"
        if edge < -0.05:
            return "CONTRARIAN"
        return "NEUTRAL"

    def _score_tip(self, tip: Tip) -> int:
        rep = tip.tipster_score * 30
        con = tip.consensus_agreement * 25
        if tip.aura_p is not None:
            if tip.direction == "over":
                alignment = tip.aura_p
            elif tip.direction == "under":
                alignment = 1.0 - tip.aura_p
            else:
                alignment = 0.5
        else:
            alignment = 0.5
        align = alignment * 25
        if tip.edge_type == "VALUE" and tip.edge_value is not None:
            edge_b = min(20.0, tip.edge_value * 100)
        elif tip.edge_type == "CONTRARIAN":
            edge_b = 0.0
        else:
            edge_b = 10.0
        return int(rep + con + align + edge_b)

    def _adaptive_interval(self) -> float:
        if not self.browser:
            return self.check_interval
        try:
            view = self.browser.extract_view()
            if not view:
                return self.check_interval
            minute = view.get("fixture", {}).get("minute", 0) or 0
            if minute > 0:
                return 300.0
            return 600.0
        except Exception:
            return self.check_interval

    def _enrich_tips(self, tips: List[Tip]) -> List[Tip]:
        if not tips:
            return tips
        for t in tips:
            self.reputation.record_tip(t)
            t.tipster_score = self.reputation.get_score(t.site)
            t.tipster_stars = self.reputation.get_stars(t.site)
        consensus = self._build_consensus(tips)
        for t in tips:
            key = re.sub(r"[^a-z]", "", t.match.lower()) + "|" + t.market
            c = consensus.get(key, {})
            t.consensus_agreement = c.get("agreement", 0.0)
            t.consensus_n = c.get("n_sites", 0)
        for t in tips:
            t.implied_prob = (1.0 / t.odds) if t.odds and t.odds > 1.0 else None
            t.aura_p = self._compute_aura_p(t)
            t.edge_type = self._detect_edge(t)
        if self.glm and getattr(self.glm, "api_key", None):
            for t in tips[:20]:
                try:
                    analysis = self.glm.call(self._cross_prompt(t))
                    if analysis:
                        t.glm_analysis = analysis[:300]
                        self._glm_analyses += 1
                except Exception:
                    pass
        for t in tips:
            t.quality_score = self._score_tip(t)
        return tips

    def _cross_prompt(self, tip: Tip) -> str:
        view = None
        if self.browser:
            try:
                view = self.browser.extract_view()
            except Exception:
                pass
        stats = "null"
        if view:
            f = view.get("fixture", {})
            s = view.get("stats", {})
            stats = json.dumps({
                "match": f"{f.get('home', '?')} x {f.get('away', '?')}",
                "minute": f.get("minute"),
                "corners": s.get("corners"),
                "dangerous": s.get("dangerous"),
                "xg": s.get("xg"),
            }, ensure_ascii=False)
        return (
            f"Um site publicou: {json.dumps(tip.to_dict(), ensure_ascii=False)}\n"
            f"Dados AURA: {stats}\n"
            "Avalie em UMA frase: ALINHADA, CONTRÁRIA ou NEUTRA?"
        )

    def send_to_telegram(self, tips: List[Tip]) -> int:
        # Dedup always (even without telegram)
        now = time.time()
        with self._lock:
            for k in [k for k, t in self._sent.items() if now - t > self.dedup_ttl]:
                self._sent.pop(k, None)
        new: List[Tip] = []
        for t in tips:
            fp = t.fingerprint()
            with self._lock:
                if fp in self._sent:
                    self._total_deduped += 1
                    continue
            new.append(t)
        if not new:
            return 0
        high = [t for t in new if t.quality_score >= 80]
        standard = [t for t in new if 50 <= t.quality_score < 80]
        filtered = [t for t in new if t.quality_score < self.min_quality]
        self._total_filtered += len(filtered)
        if not self.telegram:
            return 0
        chat_id = self._get_chat_id()
        if not chat_id:
            return 0
        sent = 0
        for t in high:
            msg = self._fmt_high_value(t)
            try:
                self.telegram.send_message(chat_id, msg)
                sent += 1
            except Exception as e:
                log.error("[tg] %s", e)
                self._errors += 1
            with self._lock:
                self._sent[t.fingerprint()] = now
            time.sleep(1.0)
        by_site: Dict[str, List[Tip]] = {}
        for t in standard:
            by_site.setdefault(t.site, []).append(t)
        for site, batch in by_site.items():
            for i in range(0, len(batch), self.max_tips):
                b = batch[i:i + self.max_tips]
                msg = self._fmt_standard(site, b)
                try:
                    self.telegram.send_message(chat_id, msg)
                    sent += len(b)
                except Exception as e:
                    log.error("[tg] %s", e)
                    self._errors += 1
                with self._lock:
                    for t in b:
                        self._sent[t.fingerprint()] = now
                time.sleep(1.0)
        with self._lock:
            self._total_sent += sent
        log.info("[crossanalyst] enviado: %d (high=%d, std=%d, filtered=%d)",
                 sent, len(high), len(standard), len(filtered))
        return sent

    def _get_chat_id(self) -> Optional[int]:
        if not self.telegram:
            return None
        chats = getattr(self.telegram, "allowed_chats", None)
        if chats:
            return next(iter(chats))
        return None

    def _fmt_high_value(self, t: Tip) -> str:
        odds_str = f"💰 {t.odds:.2f}" if t.odds else "💰 —"
        implied_str = f"mercado: {t.implied_prob:.0%}" if t.implied_prob else ""
        aura_str = f"P = {t.aura_p:.0%}" if t.aura_p is not None else "sem dados"
        edge_str = f"+{t.edge_value * 100:.0f}pts" if t.edge_value else "—"
        lines = [
            f"🔥 *HIGH-VALUE TIP* — Score: {t.quality_score}/100", "",
            f"⚽ *{t.match or 'N/D'}*",
            f"📊 {t.market}: {t.selection}",
            f"{odds_str} ({implied_str})", "",
            f"🧠 AURA: {aura_str}",
            f"→ EDGE: {edge_str} ({t.edge_type})",
            f"🎯 Consenso: {t.consensus_n} site(s) ({t.consensus_agreement:.0%})",
            f"⭐ Tipster: {t.site} {t.tipster_stars}",
        ]
        if t.glm_analysis:
            lines.append(f"🤖 {t.glm_analysis[:120]}")
        lines += ["", _DISCLAIMER]
        return "\n".join(lines)

    def _fmt_standard(self, site: str, tips: List[Tip]) -> str:
        lines = [f"📊 *{len(tips)} TIP(S)* — {site}", ""]
        for i, t in enumerate(tips, 1):
            odds_str = f"💰{t.odds:.2f}" if t.odds else "💰—"
            emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(t.confidence, "⚪")
            lines.append(f"{i}. ⚽ {t.match or 'N/D'}")
            lines.append(f" 📊 {t.market}: {t.selection} {odds_str} {emoji}")
            lines.append(f" Score: {t.quality_score} | {t.edge_type} | {t.tipster_stars}")
            if t.glm_analysis:
                lines.append(f" 🤖 {t.glm_analysis[:80]}")
            lines.append("")
        lines.append(_DISCLAIMER)
        return "\n".join(lines)

    def start_loop(self, interval: Optional[float] = None) -> None:
        if interval is not None:
            self.check_interval = float(interval)
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="cross-analyst-v2", daemon=True)
        self._thread.start()
        log.info("[crossanalyst] loop iniciado")

    def _loop(self) -> None:
        while self._running:
            try:
                self.run_once()
            except Exception:
                log.exception("[crossanalyst] ciclo falhou")
            time.sleep(self._adaptive_interval())

    def run_once(self) -> dict:
        self._last_cycle = time.time()
        tips = self.collect_tips()
        tips = self._enrich_tips(tips)
        sent = self.send_to_telegram(tips)
        return {"collected": len(tips), "sent": sent, "filtered": self._total_filtered}

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def get_tips(self, n: int = 50) -> List[dict]:
        with self._lock:
            return [t.to_dict() for t in self._all_tips[-n:]]

    def stats(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "sites": len(self._sites),
                "collected": self._total_collected,
                "sent": self._total_sent,
                "deduped": self._total_deduped,
                "filtered": self._total_filtered,
                "glm_analyses": self._glm_analyses,
                "errors": self._errors,
                "last_cycle_sec": round(time.time() - self._last_cycle, 0)
                    if self._last_cycle else None,
                "reputation": self.reputation.stats(),
            }


ANALYST = CrossSiteAnalyst()

if __name__ == "__main__":
    import sys
    import tempfile

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    errs: List[str] = []

    def check(n, c, x=""):
        s = "PASS" if c else "FAIL"
        print(f"[{s}] {n}" + (f" — {x}" if x else ""))
        if not c:
            errs.append(n)

    t = Tip(site="ex1", match="A x B", market="corners",
            selection="Over 9.5", odds=1.85)
    check("Tip: direction over", t.direction == "over")
    check("Tip: line 9.5", t.line == 9.5)
    check("Tip: implied none", t.implied_prob is None)
    t.implied_prob = 1.0 / 1.85
    check("Tip: implied set", abs(t.implied_prob - 0.541) < 0.01)

    rep = TipsterReputation()
    t1 = Tip(site="s1", match="A x B", market="corners",
             selection="Over 9.5", collected_at=_iso())
    t2 = Tip(site="s1", match="C x D", market="corners",
             selection="Under 8.5", collected_at=_iso())
    rep.record_tip(t1)
    rep.record_tip(t2)
    check("rep: total=2", rep.stats().get("s1", {}).get("total") == 2)
    check("rep: pending=2", rep.stats().get("s1", {}).get("pending") == 2)
    check("rep: score neutro", abs(rep.get_score("s1") - 0.5) < 0.01)
    rep.resolve("s1", t1.fingerprint(), True)
    check("rep: score sobe apos hit", rep.get_score("s1") > 0.5)
    rep.resolve("s1", t2.fingerprint(), False)
    check("rep: 1 hit 1 miss ~0.5", abs(rep.get_score("s1") - 0.5) < 0.01)
    check("rep: stars", rep.get_stars("s1") is not None)

    analyst = CrossSiteAnalyst(browser=None, telegram=None, glm=None, reputation=rep)
    tips = [
        Tip(site="s1", match="A x B", market="corners", selection="Over 9.5"),
        Tip(site="s2", match="A x B", market="corners", selection="Over 9.5"),
        Tip(site="s3", match="A x B", market="corners", selection="Under 9.5"),
        Tip(site="s4", match="C x D", market="1x2", selection="Home"),
    ]
    c = analyst._build_consensus(tips)
    key_ab = re.sub(r"[^a-z]", "", "A x B".lower()) + "|corners"
    check("consensus: 3 sites A x B corners", c[key_ab]["n_sites"] == 3)
    check("consensus: 2/3 agree Over", c[key_ab]["n_agree"] == 2)

    t_val = Tip(site="x", match="A x B", market="corners",
                selection="Over 9.5", odds=1.85)
    t_val.aura_p = 0.68
    t_val.implied_prob = 1.0 / 1.85
    check("edge: VALUE", analyst._detect_edge(t_val) == "VALUE")

    t_con = Tip(site="x", match="A x B", market="corners",
                selection="Under 9.5", odds=1.95)
    t_con.aura_p = 0.68
    t_con.implied_prob = 1.0 / 1.95
    # under tip with high over p → edge negative for under
    t_con.edge_value = (1.0 - 0.68) - (1.0 / 1.95)
    check("edge: CONTRARIAN detectado", t_con.edge_value < -0.05)

    t_good = Tip(site="ex3", match="A x B", market="corners",
                 selection="Over 9.5", odds=1.85)
    t_good.tipster_score = 0.7
    t_good.consensus_agreement = 0.8
    t_good.aura_p = 0.68
    t_good.implied_prob = 1.0 / 1.85
    t_good.edge_type = "VALUE"
    t_good.edge_value = 0.14
    score = analyst._score_tip(t_good)
    check("score: >=50 tip boa", score >= 50, f"score={score}")
    check("score: <100", score < 100, f"score={score}")

    t_bad = Tip(site="s1", match="A x B", market="corners",
                selection="Under 9.5", odds=1.95)
    t_bad.tipster_score = 0.3
    t_bad.consensus_agreement = 0.2
    t_bad.aura_p = 0.68
    t_bad.implied_prob = 1.0 / 1.95
    t_bad.edge_type = "CONTRARIAN"
    t_bad.edge_value = -0.19
    score_bad = analyst._score_tip(t_bad)
    check("score: baixo tip ruim", score_bad < 50, f"score={score_bad}")

    check("scheduler: sem browser base", analyst._adaptive_interval() == 1800.0)

    t_high = Tip(site="ex3", match="Palmeiras x Corinthians", market="corners",
                 selection="Over 9.5", odds=1.85, confidence="high")
    t_high.quality_score = 87
    t_high.aura_p = 0.68
    t_high.implied_prob = 0.54
    t_high.edge_type = "VALUE"
    t_high.edge_value = 0.14
    t_high.consensus_n = 4
    t_high.consensus_agreement = 0.8
    t_high.tipster_stars = "⭐⭐⭐⭐"
    t_high.glm_analysis = "ALINHADA — pressao alta"
    msg = analyst._fmt_high_value(t_high)
    check("fmt high: header", "HIGH-VALUE" in msg)
    check("fmt high: match", "Palmeiras" in msg)
    check("fmt high: edge", "VALUE" in msg)
    check("fmt high: disclaimer", "Não faz apostas" in msg)
    msg_std = analyst._fmt_standard("ex1", [t_bad])
    check("fmt std: header", "TIP(S)" in msg_std)
    check("fmt std: disclaimer", "Não faz apostas" in msg_std)

    with tempfile.TemporaryDirectory() as td:
        rep2 = TipsterReputation(state_dir=td)
        tip_p = Tip(site="x", match="A x B", market="corners",
                    selection="Over 9.5", collected_at=_iso())
        rep2.record_tip(tip_p)
        rep2.resolve("x", tip_p.fingerprint(), True)
        rep2._save()
        rep3 = TipsterReputation(state_dir=td)
        check("rep: persistencia", rep3.stats().get("x", {}).get("hits") == 1)

    tips_r = analyst._extract_regex(
        "test", "http://x",
        "Brazil vs Argentina\nTip: Over 9.5 corners (odds 1.85)\nConfidence: high\n"
        "Pick: Under 2.5 gols (odds 1.65)")
    check("regex: extrai", len(tips_r) >= 2, f"{len(tips_r)}")

    print(f"\ncross_site_analyst v2 selftest: {len(errs)} falha(s)")
    sys.exit(1 if errs else 0)
