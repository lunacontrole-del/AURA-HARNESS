#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
football_intelligence.py — inteligencia de FUTEBOL para o AURA: dados de
partidas, literatura de modelagem e calculadoras estatisticas, tudo gratuito.

O QUE ENTREGA (3 camadas):

    CAMADA 1 — DADOS: fixtures, resultados e estatisticas por liga via
    OpenFootball (GitHub, JSON aberto) e football-data.org (free tier).
    Alimenta o ToolKnowledge com contexto REAL de partidas.

    CAMADA 2 — LITERATURA: papers sobre modelagem de escanteios/gols via
    Crossref (150M+ metadados, sem key) e arXiv (ja integrado no
    research_improver). O GLM-4 passa a responder com fundamento estatistico
    de papers reais, nao alucinacao.

    CAMADA 3 — CALCULADORAS: Poisson, Poisson bivariada (gols),
    aproximacao de corners por minuto, valor esperado. Implementadas em
    stdlib puro — as ferramentas que o assistente usa para ANALISAR.

INTEGRACAO:
    - ToolKnowledge: papers e dados de partidas viram conhecimento consultavel
    - CommandCenter: tools de pesquisa e calculo
    - research_improver: campanhas especificas de futebol/esccanteios

FRONTEIRA (§0/§8): isto e INTELIGENCIA para ANALISE em paper trade. As
calculadoras dao numeros; a interpretacao e do GLM-4 com contexto real.

stdlib only. Python 3.9+. Windows. Console ASCII.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("aura.football_intel")

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# fontes de dados gratuitas
# ---------------------------------------------------------------------------

class OpenFootballSource:
    """OpenFootball (GitHub) — fixtures e resultados em JSON aberto.

    Repositorios: openfootball/football.json com dados por temporada.
    URL base: https://raw.githubusercontent.com/openfootball/football.json/master/
    """

    BASE = ("https://raw.githubusercontent.com/openfootball/"
            "football.json/master")
    LEAGUES = {
        "en": "england", "es": "spain", "de": "germany", "it": "italy",
        "fr": "france", "br": "brazil", "pt": "portugal", "nl": "netherlands",
    }

    def __init__(self, post_fn: Optional[Callable] = None,
                 min_interval: float = 2.0):
        self._post = post_fn or self._get_default
        self._min = float(min_interval)
        self._last = -1e9
        self.stats = {"fetches": 0, "failures": 0, "matches_loaded": 0}

    def _get_default(self, url: str) -> Optional[str]:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "AURA/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read(500_000).decode("utf-8", errors="replace")
        except Exception:
            return None

    def _throttle(self) -> bool:
        if time.monotonic() - self._last < self._min:
            return False
        self._last = time.monotonic()
        return True

    def get_season(self, league: str = "br", season: str = "2024-25"
                   ) -> dict:
        """Busca uma temporada completa de uma liga."""
        if not self._throttle():
            return {"ok": False, "speech": "Aguarde antes de nova consulta."}
        league_dir = self.LEAGUES.get(league.lower().strip(), league.lower())
        # tenta varios formatos de nome
        for fname in ("%s-%s.json" % (league_dir, season),
                      "%s.json" % league_dir, "%s-1.json" % league_dir):
            url = "%s/%s/%s" % (self.BASE, season, fname)
            raw = self._post(url)
            if raw:
                try:
                    data = json.loads(raw)
                    matches = data.get("matches") or []
                    self.stats["fetches"] += 1
                    self.stats["matches_loaded"] += len(matches)
                    return {"ok": True, "league": league,
                            "season": season, "matches": matches,
                            "speech": "%d partidas de %s %s carregadas."
                                      % (len(matches), league, season)}
                except ValueError:
                    continue
        self.stats["failures"] += 1
        return {"ok": False,
                "speech": "Dados de %s %s nao encontrados no OpenFootball."
                          % (league, season)}

    def get_team_matches(self, team: str, league: str = "br",
                         season: str = "2024-25") -> dict:
        """Filtra partidas de um time especifico."""
        season_data = self.get_season(league, season)
        if not season_data.get("ok"):
            return season_data
        team_n = _norm(team)
        matches = []
        for m in season_data.get("matches", []):
            home = _norm(str(m.get("team1", "")))
            away = _norm(str(m.get("team2", "")))
            if team_n in home or team_n in away:
                matches.append({
                    "data": m.get("date", ""),
                    "casa": m.get("team1", ""),
                    "fora": m.get("team2", ""),
                    "placar": "%s-%s" % (m.get("score", {}).get("ft", [0, 0])[0],
                                          m.get("score", {}).get("ft", [0, 0])[1]),
                })
        if not matches:
            return {"ok": True, "matches": [],
                    "speech": "Nenhuma partida de %s em %s %s."
                              % (team, league, season)}
        return {"ok": True, "matches": matches,
                "speech": "%d partida(s) de %s." % (len(matches), team)}


class CrossrefFootballSearch:
    """Crossref — 150M+ papers, gratuito, sem key.
    Busca por papers de modelagem de escanteios/gols em futebol."""

    SEARCH = ("https://api.crossref.org/works?query=%s&rows=%d"
              "&filter=type:journal-article")

    FOOTBALL_TERMS = [
        "football corners poisson model",
        "soccer goal scoring statistical model",
        "football match prediction machine learning",
        "in-play football betting model",
        "expected goals xG model football",
        "asian corner handicap prediction",
        "football time-varying poisson",
        "bayesian football score prediction",
    ]

    def __init__(self, post_fn: Optional[Callable] = None,
                 min_interval: float = 1.0, mailto: str = ""):
        self._post = post_fn or self._get_default
        self._min = float(min_interval)
        self._mail = (mailto or "").strip()
        self._last = -1e9
        self.stats = {"searches": 0, "papers_found": 0, "failures": 0}

    def _get_default(self, url: str) -> Optional[dict]:
        try:
            headers = {"User-Agent": "AURA-Football/1.0"}
            if self._mail:
                url += ("&" if "?" in url else "?") + "mailto=" + self._mail
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8",
                                                     errors="replace"))
        except Exception:
            return None

    def search_football_modeling(self, query: str = "", limit: int = 6
                                 ) -> dict:
        """Busca papers sobre modelagem de futebol (escanteios/gols)."""
        if not (query or "").strip():
            query = self.FOOTBALL_TERMS[0]
        if time.monotonic() - self._last < self._min:
            return {"ok": False, "speech": "Aguarde."}
        self._last = time.monotonic()
        url = self.SEARCH % (urllib.parse.quote(query), limit)
        data = self._post(url)
        if not data:
            self.stats["failures"] += 1
            return {"ok": False, "speech": "Crossref indisponivel."}
        items = (data.get("message") or {}).get("items") or []
        self.stats["searches"] += 1
        self.stats["papers_found"] += len(items)
        papers = []
        for it in items[:limit]:
            title = " ".join(it.get("title") or ["sem titulo"])
            doi = it.get("DOI", "")
            year = ((it.get("issued") or {}).get(
                "date-parts", [[None]])[0][0])
            journal = " ".join(it.get("container-title") or [""])
            abstract = (it.get("abstract") or "")[:300]
            # strip de tags XML do abstract
            abstract = re.sub(r"<[^>]+>", "", abstract)
            papers.append({"title": title, "doi": doi, "year": year,
                           "journal": journal, "abstract": abstract})
        return {"ok": True, "papers": papers,
                "speech": "Crossref: %d paper(s) sobre '%s'."
                          % (len(papers), query)}

    def learn_football_literature(self, kb: Any, limit: int = 10) -> dict:
        """Busca os termos de modelagem de futebol e aprende TUDO no kb."""
        learned = 0
        for term in self.FOOTBALL_TERMS[:limit]:
            res = self.search_football_modeling(term, limit=3)
            if not res.get("ok"):
                continue
            for p in res.get("papers", []):
                content = ("Title: %s\nDOI: %s\nYear: %s\nJournal: %s\n"
                           "Abstract: %s" % (p["title"], p["doi"],
                                              p["year"], p["journal"],
                                              p["abstract"]))
                try:
                    kb.add_text("Paper: %s" % p["title"][:80], content,
                                source="crossref:%s" % p["doi"])
                    learned += 1
                except Exception:
                    pass
            time.sleep(1.5)  # rate limit cortes
        return {"ok": True, "learned": learned,
                "speech": "Aprendi %d papers de modelagem de futebol. "
                          "Agora posso responder sobre escanteios e gols "
                          "com literatura real." % learned}


# ---------------------------------------------------------------------------
# calculadoras estatisticas (stdlib puro) — AS FERRAMENTAS DE ANALISE
# ---------------------------------------------------------------------------
class FootballCalculators:
    """Ferramentas de calculo que o assistente usa para ANALISAR.
    Tudo stdlib — deterministico, testavel."""

    @staticmethod
    def poisson_pmf(k: int, lam: float) -> float:
        """P(X=k) para X ~ Poisson(lam)."""
        if lam < 0:
            return 0.0
        if k < 0:
            return 0.0
        return math.exp(-lam) * lam ** k / math.factorial(k)

    @staticmethod
    def poisson_cdf(k: int, lam: float) -> float:
        """P(X <= k)."""
        return sum(FootballCalculators.poisson_pmf(i, lam)
                   for i in range(k + 1))

    @staticmethod
    def over_under(lam: float, line: float) -> dict:
        """Probabilidade de Over/Under para eventos Poisson.
        line: 2.5 significa over 2.5 = P(X >= 3)."""
        if line <= 0 or lam < 0:
            return {"ok": False, "speech": "Parametros invalidos."}
        k = int(math.floor(line))
        p_under = FootballCalculators.poisson_cdf(k, lam)
        p_over = 1.0 - p_under
        # valor esperado
        ev_over = p_over * (1.0 / max(0.01, 1.0 / 0.85)) - (1 - p_over)  # odd 1.85 aprox
        return {"ok": True, "lam": lam, "line": line,
                "p_over": round(p_over, 4), "p_under": round(p_under, 4),
                "p_over_pct": round(100 * p_over, 1),
                "speech": "Poisson(lambda=%.2f): Over %.1f = %.1f%%, "
                          "Under %.1f = %.1f%%" % (lam, line,
                                                    100 * p_over, line,
                                                    100 * p_under)}

    @staticmethod
    def corners_expected_rate(avg_corners_home: float,
                              avg_corners_away: float,
                              league_avg: float = 10.5) -> float:
        """Estima lambda de escanteios para uma partida a partir das medias
        dos times e da liga. Modelo multiplicativo simples:
        lam = forca_ataque_casa * forca_defesa_fora * media_liga."""
        if any(x <= 0 for x in (avg_corners_home, avg_corners_away,
                                league_avg)):
            return league_avg
        # forca relativa: quanto cada time produz acima/abaixo da media
        atk_home = avg_corners_home / league_avg
        def_away = avg_corners_away / league_avg
        # combinacao geometrica (evita valores extremos)
        lam = league_avg * math.sqrt(atk_home * def_away)
        return round(lam, 2)

    @staticmethod
    def corners_over_by_minute(current_corners: int, lam_total: float,
                               minute: int, total_minutes: int = 90,
                               line: float = 9.5) -> dict:
        """P(Over line) dado cantos atuais, lambda total, minuto atual.
        Usa Poisson condicional para o tempo restante."""
        if minute >= total_minutes or minute < 0:
            return {"ok": False, "speech": "Minuto invalido."}
        remaining_frac = (total_minutes - minute) / total_minutes
        lam_remaining = lam_total * remaining_frac
        corners_needed = max(0, int(math.ceil(line - current_corners)))
        if corners_needed == 0:
            return {"ok": True, "p_over": 1.0, "lam_remaining": 0,
                    "speech": "Linha ja batida (%d >= %.1f)."
                              % (current_corners, line)}
        # P(X >= corners_needed | X ~ Poisson(lam_remaining))
        p_at_most = FootballCalculators.poisson_cdf(
            corners_needed - 1, lam_remaining)
        p_over = 1.0 - p_at_most
        return {"ok": True,
                "corners_now": current_corners,
                "lam_remaining": round(lam_remaining, 2),
                "corners_needed": corners_needed,
                "p_over": round(p_over, 4),
                "p_over_pct": round(100 * p_over, 1),
                "speech": ("Minuto %d, %d cantos, precisa de %d mais. "
                           "Lambda restante %.2f. "
                           "P(Over %.1f) = %.1f%%"
                           % (minute, current_corners, corners_needed,
                              lam_remaining, line, 100 * p_over))}

    @staticmethod
    def kelly_fraction(p: float, odds: float) -> float:
        """Fracao de Kelly para apostar (paper trade: so calculo)."""
        if p <= 0 or p >= 1 or odds <= 1:
            return 0.0
        b = odds - 1.0
        f = (p * b - (1 - p)) / b
        return max(0.0, round(f, 4))

    @staticmethod
    def implied_probability(odds: float) -> float:
        """Probabilidade implicita de uma odd (com margem da casa)."""
        if odds <= 1.0:
            return 0.0
        return round(1.0 / odds, 4)

    @staticmethod
    def remove_margin(odds_over: float, odds_under: float) -> tuple:
        """Remove a margem da casa de um par Over/Under.
        Retorna (p_over_fair, p_under_fair)."""
        if odds_over <= 1 or odds_under <= 1:
            return (0.5, 0.5)
        p_over_raw = 1.0 / odds_over
        p_under_raw = 1.0 / odds_under
        total = p_over_raw + p_under_raw
        return (round(p_over_raw / total, 4),
                round(p_under_raw / total, 4))

    @staticmethod
    def bivariate_poisson_goals(lam_home: float, lam_away: float,
                                rho: float = 0.1) -> dict:
        """Aproximacao de Poisson bivariada para placares de futebol.
        rho: correlacao entre gols (positiva para jogos fechados)."""
        if lam_home < 0 or lam_away < 0:
            return {"ok": False, "speech": "Parametros invalidos."}
        # calcula matriz de placares ate 5x5
        probs: Dict[str, float] = {}
        for h in range(6):
            for a in range(6):
                p_h = FootballCalculators.poisson_pmf(h, lam_home)
                p_a = FootballCalculators.poisson_a(a, lam_away, rho)
                probs["%d-%d" % (h, a)] = round(p_h * p_a, 6)
        # normaliza a matriz truncada para que as agregacoes somem 1.
        total_prob = sum(probs.values()) or 1.0
        probs = {k: v / total_prob for k, v in probs.items()}
        # agregacoes uteis
        p_home = sum(v for k, v in probs.items() if int(k.split("-")[0]) >
                     int(k.split("-")[1]))
        p_draw = sum(v for k, v in probs.items()
                     if int(k.split("-")[0]) == int(k.split("-")[1]))
        p_away = sum(v for k, v in probs.items() if int(k.split("-")[0]) <
                     int(k.split("-")[1]))
        p_btts = sum(v for k, v in probs.items()
                     if int(k.split("-")[0]) > 0 and int(k.split("-")[1]) > 0)
        p_over25 = sum(v for k, v in probs.items()
                       if int(k.split("-")[0]) + int(k.split("-")[1]) >= 3)
        return {"ok": True, "probs": probs,
                "p_home": round(p_home, 3), "p_draw": round(p_draw, 3),
                "p_away": round(p_away, 3),
                "p_btts": round(p_btts, 3),
                "p_over25": round(p_over25, 3),
                "speech": ("Poisson bivariada (h=%.1f, a=%.1f): "
                           "Casa %.1f%%, Empate %.1f%%, Fora %.1f%%. "
                           "BTTS %.1f%%, Over 2.5 %.1f%%"
                           % (lam_home, lam_away, 100 * p_home,
                              100 * p_draw, 100 * p_away,
                              100 * p_btts, 100 * p_over25))}

    @staticmethod
    def poisson_a(k: int, lam: float, rho: float) -> float:
        """Poisson ajustada por correlacao (aproximacao Dixon-Coles lite)."""
        base = FootballCalculators.poisson_pmf(k, lam)
        # ajuste simples: placares baixos ficam mais provaveis com rho>0
        if k <= 1:
            return base * (1 + rho)
        if k >= 3:
            return base * (1 - rho * 0.5)
        return base


# ---------------------------------------------------------------------------
# integracao
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def parse_football_intel(utterance: str):
    t = _norm(utterance)
    if not t:
        return None
    import re
    # calculadoras
    m = re.search(r"poisson.*lambda\s*([\d.]+).*linha\s*([\d.]+)", t)
    if m:
        return ("calc_poisson", {"lam": m.group(1), "linha": m.group(2)})
    m = re.search(r"\bcantos?\b.*\bover\s*([\d.]+)\b.*"
                  r"\bminuto\s*(\d+).*\batuais?\s*(\d+).*lambda\s*([\d.]+)", t)
    if m:
        return ("calc_cantos_over", {"linha": m.group(1),
                                      "minuto": m.group(2),
                                      "atuais": m.group(3),
                                      "lam": m.group(4)})
    m = re.search(r"kelly.*probabilidade\s*([\d.]+).*odd\s*([\d.]+)", t)
    if m:
        return ("calc_kelly", {"p": m.group(1), "odd": m.group(2)})
    # literatura — aprender vem antes de pesquisar para não colidir.
    if re.search(r"aprend\w+\s+literatura\s+de\s+futebol", t):
        return ("aprender_literatura", {})
    if re.search(r"pesquis\w+\s+papers?\s+(?:sobre\s+)?futebol", t) or \
            re.search(r"literatura\s+(?:de\s+)?(?:futebol|escanteios|gols)", t):
        return ("pesquisar_literatura", {"termo": ""})
    # dados — time vem antes de liga para "partidas do Flamengo".
    m = re.search(r"partidas?\s+d[oa]\s+(.+?)(?:\s+em\s+(\w+))?$", t)
    if m:
        return ("dados_time", {"time": m.group(1).strip(),
                                "liga": m.group(2) or "br"})
    m = re.search(r"partidas?\s+do\s+(\w+)", t)
    if m:
        return ("dados_partidas", {"liga": m.group(1)})
    return None


def build_football_intel_tools(cc, openfootball: OpenFootballSource,
                                crossref: CrossrefFootballSearch,
                                calc: FootballCalculators,
                                kb: Any = None) -> None:
    """Registra as tools de inteligencia de futebol."""

    def t_poisson(args, session):
        lam = _to_float(args.get("lam"))
        line = _to_float(args.get("linha"))
        if lam is None or line is None:
            return {"ok": False,
                    "speech": "Diga lambda e a linha (ex: lambda 10 linha 9.5)."}
        return calc.over_under(lam, line)

    def t_corners_over(args, session):
        line = _to_float(args.get("linha"))
        minute = int(_to_float(args.get("minuto")) or 0)
        current = int(_to_float(args.get("atuais")) or 0)
        lam = _to_float(args.get("lam"))
        if line is None or lam is None:
            return {"ok": False, "speech": "Diga linha, minuto, cantos "
                    "atuais e lambda total."}
        return calc.corners_over_by_minute(current, lam, minute,
                                           line=line)

    def t_kelly(args, session):
        p = _to_float(args.get("p"))
        odds = _to_float(args.get("odd"))
        if p is None or odds is None:
            return {"ok": False, "speech": "Diga probabilidade e odd."}
        f = calc.kelly_fraction(p, odds)
        return {"ok": True, "kelly": f,
                "speech": "Kelly: %.1f%% da banca (fracao %.4f). "
                          "PAPER TRADE — calculo apenas."
                % (100 * f, f)}

    def t_literatura(args, session):
        res = crossref.search_football_modeling(str(args.get("termo", "")))
        if res.get("ok") and kb is not None and res.get("papers"):
            for p in res["papers"][:2]:
                content = ("Title: %s\nDOI: %s\nYear: %s\nAbstract: %s"
                           % (p["title"], p["doi"], p["year"],
                              p["abstract"]))
                kb.add_text("Paper: %s" % p["title"][:80], content,
                            source="crossref:%s" % p["doi"])
        return res

    def t_aprender_literatura(args, session):
        if kb is None:
            return {"ok": False,
                    "speech": "Base de conhecimento indisponivel."}
        return crossref.learn_football_literature(kb)

    def t_partidas(args, session):
        return openfootball.get_season(str(args.get("liga", "br")))

    def t_time(args, session):
        return openfootball.get_team_matches(
            str(args.get("time", "")), str(args.get("liga", "br")))

    cc.register("calc_poisson",
                "calcular Over/Under com distribuicao de Poisson",
                t_poisson, "read",
                args={"lam": "taxa esperada", "linha": "linha (ex 9.5)"},
                confirm=False)
    cc.register("calc_cantos_over",
                "calcular P(Over) de cantos dado minuto, cantos atuais e lambda",
                t_corners_over, "read",
                args={"linha": "linha", "minuto": "minuto atual",
                      "atuais": "cantos atuais", "lam": "lambda total"},
                confirm=False)
    cc.register("calc_kelly",
                "calcular fracao de Kelly (PAPER TRADE - apenas calculo)",
                t_kelly, "read",
                args={"p": "probabilidade 0-1", "odd": "odd decimal"},
                confirm=False)
    cc.register("pesquisar_literatura",
                "pesquisar papers sobre modelagem de futebol (Crossref)",
                t_literatura, "read",
                args={"termo": "termo de busca"}, confirm=False)
    cc.register("aprender_literatura",
                "aprender literatura completa de modelagem de futebol",
                t_aprender_literatura, "control", confirm=False)
    cc.register("dados_partidas",
                "carregar partidas de uma liga (OpenFootball)",
                t_partidas, "read",
                args={"liga": "br, en, es, de..."}, confirm=False)
    cc.register("dados_time",
                "buscar partidas de um time especifico",
                t_time, "read",
                args={"time": "nome do time", "liga": "br"}, confirm=False)


def _to_float(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import tempfile

    fails: List[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
        if not cond:
            fails.append(name)

    calc = FootballCalculators()

    # --- Poisson basico ---
    check("poisson: pmf(0, 1) = e^-1", abs(calc.poisson_pmf(0, 1.0) -
          math.exp(-1)) < 1e-12)
    check("poisson: pmf soma = 1 (aprox)",
          abs(sum(calc.poisson_pmf(k, 3.5) for k in range(30)) - 1) < 1e-9)
    check("poisson: cdf(5, 3.5) < 1", calc.poisson_cdf(5, 3.5) < 1)

    # --- Over/Under ---
    r = calc.over_under(10.5, 9.5)
    check("over/under: over 9.5 com lam 10.5 > 50%",
          r["ok"] and r["p_over"] > 0.5)
    r = calc.over_under(5.0, 9.5)
    check("over/under: over 9.5 com lam 5.0 < 10%",
          r["ok"] and r["p_over"] < 0.10)

    # --- corners_expected_rate ---
    lam = calc.corners_expected_rate(12.0, 8.0, 10.5)
    check("corners: lambda estimado entre medias",
          9.0 < lam < 11.0, "lam=%.2f" % lam)
    lam2 = calc.corners_expected_rate(10.5, 10.5, 10.5)
    check("corners: medias iguais = lambda da liga",
          abs(lam2 - 10.5) < 0.1)

    # --- corners_over_by_minute ---
    r = calc.corners_over_by_minute(8, 10.5, 80, line=9.5)
    check("cantos over: precisa de 2 no minuto 80",
          r["ok"] and r["corners_needed"] == 2
          and 0 < r["p_over"] < 0.8)
    r = calc.corners_over_by_minute(10, 10.5, 80, line=9.5)
    check("cantos over: linha ja batida", r["p_over"] == 1.0)
    r = calc.corners_over_by_minute(5, 10.5, 85, line=9.5)
    check("cantos over: precisando de 5 no 85' quase impossivel",
          r["p_over"] < 0.05)

    # --- Kelly ---
    check("kelly: p=0.6, odd=1.8 -> positivo",
          calc.kelly_fraction(0.6, 1.8) > 0)
    check("kelly: p=0.4, odd=1.8 -> zero",
          calc.kelly_fraction(0.4, 1.8) == 0.0)
    check("kelly: p=0.5, odd=2.0 -> ~0",
          abs(calc.kelly_fraction(0.5, 2.0)) < 1e-9)

    # --- implied + margin ---
    check("implied: odd 2.0 = 50%", abs(calc.implied_probability(2.0)
          - 0.5) < 1e-9)
    p_over, p_under = calc.remove_margin(1.85, 2.05)
    check("remove_margin: soma = 1",
          abs(p_over + p_under - 1.0) < 1e-9)
    check("remove_margin: over mais provavel",
          p_over > p_under)

    # --- Poisson bivariada ---
    r = calc.bivariate_poisson_goals(1.5, 1.1)
    check("bivariada: 1X2 soma ~1",
          abs(r["p_home"] + r["p_draw"] + r["p_away"] - 1.0) < 0.05)
    check("bivariada: casa favorita", r["p_home"] > r["p_away"])
    check("bivariada: BTTS entre 40-60%", 0.40 < r["p_btts"] < 0.60)

    # --- CrossrefFootballSearch com post falso ---
    class FakePost:
        def __call__(self, url):
            if "api.crossref.org" in url:
                return {"message": {"items": [{
                    "title": ["Modeling Football Corners with Poisson"],
                    "DOI": "10.1234/test.2024",
                    "issued": {"date-parts": [[2024]]},
                    "container-title": ["Journal of Sports Analytics"],
                    "abstract": "<p>We model corners as Poisson.</p>"}]}}
            return None

    cr = CrossrefFootballSearch(post_fn=FakePost(), min_interval=0.0)
    r = cr.search_football_modeling("football corners")
    check("crossref: paper encontrado", r["ok"] is True
          and len(r["papers"]) == 1
          and "Poisson" in r["papers"][0]["title"])
    check("crossref: abstract sem tags XML",
          "<" not in r["papers"][0]["abstract"])

    # --- OpenFootball com post falso ---
    class FakeOFPost:
        def __call__(self, url):
            if "openfootball" in url:
                return json.dumps({"matches": [
                    {"date": "2024-05-01", "team1": "Flamengo",
                     "team2": "Palmeiras",
                     "score": {"ft": [2, 1]}},
                    {"date": "2024-05-02", "team1": "Corinthians",
                     "team2": "Flamengo",
                     "score": {"ft": [0, 3]}}]})
            return None

    of = OpenFootballSource(post_fn=FakeOFPost(), min_interval=0.0)
    r = of.get_season("br")
    check("openfootball: temporada carregada", r["ok"] is True
          and len(r["matches"]) == 2)
    r = of.get_team_matches("flamengo", "br")
    check("openfootball: partidas do time",
          r["ok"] is True and len(r["matches"]) == 2)

    # --- gramatica ---
    g = parse_football_intel("poisson lambda 10.5 linha 9.5")
    check("gram: poisson", g is not None and g[0] == "calc_poisson")
    check("gram: kelly", parse_football_intel(
        "kelly probabilidade 0.6 odd 1.8") is not None)
    check("gram: literatura", parse_football_intel(
        "pesquisa papers sobre futebol") is not None)
    check("gram: aprender literatura", parse_football_intel(
        "aprende literatura de futebol") ==
        ("aprender_literatura", {}))
    g = parse_football_intel("partidas do flamengo")
    check("gram: partidas do time", g is not None
          and g[0] == "dados_time" and "flamengo" in g[1]["time"])

    # --- integracao CommandCenter ---
    try:
        from jarvis_command_center import CommandCenter
    except Exception:
        CommandCenter = None
    if CommandCenter is None:
        print("[SKIP] jarvis_command_center nao importavel aqui")
    else:
        cc = CommandCenter()
        build_football_intel_tools(cc, of, cr, calc)
        r = cc.execute("calc_poisson", {"lam": "10.5", "linha": "9.5"},
                       "u")
        check("cc: poisson calcula", r["ok"] is True
              and "%" in r["speech"])
        r = cc.execute("calc_kelly", {"p": "0.6", "odd": "1.8"}, "u")
        check("cc: kelly calcula com aviso paper trade",
              r["ok"] is True and "PAPER" in r["speech"])
        r = cc.execute("pesquisar_literatura", {"termo": "corners"}, "u")
        check("cc: literatura busca", r["ok"] is True)

    # --- aprender literatura no kb ---
    try:
        from web_knowledge import ToolKnowledge
    except Exception:
        ToolKnowledge = None
    if ToolKnowledge is not None:
        with tempfile.TemporaryDirectory() as td:
            kb = ToolKnowledge(kb_dir=Path(td) / "kb")
            cr2 = CrossrefFootballSearch(post_fn=FakePost(),
                                         min_interval=0.0)
            # simula: aprende 1 termo
            res = cr2.search_football_modeling("football corners")
            for p in res.get("papers", []):
                kb.add_text("Paper: %s" % p["title"][:80],
                            "DOI: %s | %s" % (p["doi"], p["abstract"]),
                            source="crossref")
            hits = kb.query("poisson corners football")
            check("kb: literatura consultavel", len(hits) >= 1
                  and "poisson" in hits[0]["text"].lower())
    else:
        print("[SKIP] web_knowledge nao importavel aqui")

    if fails:
        print("SELF-TEST FALHOU: %d verificacao(oes): %s"
              % (len(fails), ", ".join(fails)))
        return 1
    print("ALL TESTS PASSED - football_intelligence.py")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
