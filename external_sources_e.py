#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
external_sources_e.py — fontes externas opcionais da Instalação E.

Este módulo é deliberadamente inerte no import: não inicia polling, não cria
threads, não instala dependências e não faz chamadas de rede sozinho. A rede
somente é acessada quando um método de uma fonte é chamado explicitamente.

As ferramentas recebem o prefixo ``e_`` para não colidir com ferramentas que
possam ser adicionadas por futuras instalações. O registro também não é feito
automaticamente no CommandCenter; o integrador precisa chamar
``build_external_intel_tools_v2`` de forma explícita.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

__version__ = "1.0.0-e"


class _RateLimitedSource:
    def __init__(self, min_interval: float = 1.0) -> None:
        self._min = max(0.0, float(min_interval))
        self._last = -1e9

    def _allowed(self) -> bool:
        now = time.monotonic()
        if now - self._last < self._min:
            return False
        self._last = now
        return True


class MicrolinkSource(_RateLimitedSource):
    """Converte uma URL pública para conteúdo Markdown via Microlink.

    Por segurança, caminhos locais, ``file://`` e esquemas não HTTP(S) são
    rejeitados. O limite de resposta impede que uma resposta remota consuma
    memória indefinidamente.
    """

    API = "https://api.microlink.io/"

    def __init__(self, post_fn: Optional[Callable[[str], Optional[dict]]] = None,
                 min_interval: float = 3.0, max_chars: int = 100_000):
        super().__init__(min_interval)
        self._post = post_fn or self._get_default
        self._max_chars = max(1_000, int(max_chars))
        self.stats = {"reads": 0, "failures": 0, "rejected": 0}

    def _get_default(self, url: str) -> Optional[dict]:
        try:
            query = urllib.parse.urlencode({"url": url, "meta": "false",
                                             "insights": "false"})
            req = urllib.request.Request(
                self.API + "?" + query,
                headers={"User-Agent": "AURA-Installation-E/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None

    def read_markdown(self, url_or_path: str) -> dict:
        raw = str(url_or_path or "").strip()
        parsed = urllib.parse.urlparse(raw)
        if not raw:
            return {"ok": False, "speech": "Diga uma URL pública HTTP ou HTTPS."}
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            self.stats["rejected"] += 1
            return {"ok": False,
                    "speech": "Somente URLs públicas HTTP ou HTTPS são aceitas."}
        if not self._allowed():
            return {"ok": False, "speech": "Aguarde antes de nova leitura."}
        data = self._post(raw)
        if not isinstance(data, dict) or data.get("status") != "success":
            self.stats["failures"] += 1
            return {"ok": False, "speech": "Microlink não conseguiu converter a URL."}
        content = str((data.get("data") or {}).get("content") or "")
        if not content:
            self.stats["failures"] += 1
            return {"ok": False, "speech": "Microlink retornou conteúdo vazio."}
        self.stats["reads"] += 1
        clipped = content[:self._max_chars]
        return {"ok": True, "content": clipped,
                "speech": "Converti a URL para Markdown (%d caracteres)." % len(clipped)}


class CrossrefSource(_RateLimitedSource):
    """Pesquisa metadados bibliográficos no Crossref, sem chave."""

    API = "https://api.crossref.org/works"

    def __init__(self, post_fn: Optional[Callable[[str], Optional[dict]]] = None,
                 min_interval: float = 1.0, mailto: str = ""):
        super().__init__(min_interval)
        self._post = post_fn or self._get_default
        self._mail = str(mailto or "").strip()
        self.stats = {"searches": 0, "failures": 0}

    def _get_default(self, url: str) -> Optional[dict]:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "AURA-Installation-E/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None

    def search(self, query: str, limit: int = 5) -> dict:
        term = str(query or "").strip()
        if not term:
            return {"ok": False, "speech": "Diga o assunto para pesquisar."}
        if not self._allowed():
            return {"ok": False, "speech": "Aguarde antes de nova pesquisa."}
        n = max(1, min(int(limit), 10))
        params = {"query": term, "rows": str(n)}
        if self._mail:
            params["mailto"] = self._mail
        url = self.API + "?" + urllib.parse.urlencode(params)
        data = self._post(url)
        if not isinstance(data, dict):
            self.stats["failures"] += 1
            return {"ok": False, "speech": "Crossref indisponível."}
        items = ((data.get("message") or {}).get("items") or [])
        self.stats["searches"] += 1
        findings: List[dict] = []
        for item in items[:n]:
            title = " ".join(item.get("title") or ["sem título"])
            issued = (item.get("issued") or {}).get("date-parts") or [[None]]
            findings.append({
                "title": title,
                "doi": str(item.get("DOI") or ""),
                "year": (issued[0][0] if issued and issued[0] else None),
                "authors": ", ".join(
                    (str(a.get("family") or "") + ", " + str(a.get("given") or "")).strip(", ")
                    for a in (item.get("author") or [])[:3]),
            })
        return {"ok": True, "findings": findings,
                "speech": "Crossref encontrou %d resultado(s) para '%s'." %
                          (len(findings), term[:80])}


class OpenLibrarySource(_RateLimitedSource):
    """Pesquisa livros no catálogo público Open Library."""

    API = "https://openlibrary.org/search.json"

    def __init__(self, post_fn: Optional[Callable[[str], Optional[dict]]] = None,
                 min_interval: float = 1.0):
        super().__init__(min_interval)
        self._post = post_fn or self._get_default
        self.stats = {"searches": 0, "failures": 0}

    def _get_default(self, url: str) -> Optional[dict]:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "AURA-Installation-E/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None

    def search(self, query: str, limit: int = 5) -> dict:
        term = str(query or "").strip()
        if not term:
            return {"ok": False, "speech": "Diga o título ou autor."}
        if not self._allowed():
            return {"ok": False, "speech": "Aguarde antes de nova pesquisa."}
        n = max(1, min(int(limit), 10))
        url = self.API + "?" + urllib.parse.urlencode({"q": term, "limit": n})
        data = self._post(url)
        if not isinstance(data, dict):
            self.stats["failures"] += 1
            return {"ok": False, "speech": "Open Library indisponível."}
        docs = data.get("docs") or []
        self.stats["searches"] += 1
        findings = [{
            "title": str(doc.get("title") or ""),
            "author": ", ".join(str(x) for x in (doc.get("author_name") or [])[:3]),
            "year": doc.get("first_publish_year"),
            "pages": doc.get("number_of_pages_median"),
        } for doc in docs[:n]]
        return {"ok": True, "findings": findings,
                "speech": "Open Library encontrou %d livro(s) para '%s'." %
                          (len(findings), term[:80])}


class FrankfurterSource:
    """Consulta uma taxa de câmbio pública por código ISO de três letras."""

    API = "https://api.frankfurter.dev/v1/latest"
    _CURRENCY = re.compile(r"^[A-Z]{3}$")

    def __init__(self, post_fn: Optional[Callable[[str], Optional[dict]]] = None):
        self._post = post_fn or self._get_default
        self.stats = {"queries": 0, "failures": 0, "rejected": 0}

    def _get_default(self, url: str) -> Optional[dict]:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None

    def rate(self, base: str = "EUR", target: str = "BRL") -> dict:
        source = str(base or "").strip().upper()
        destination = str(target or "").strip().upper()
        if not self._CURRENCY.fullmatch(source) or not self._CURRENCY.fullmatch(destination):
            self.stats["rejected"] += 1
            return {"ok": False, "speech": "Use códigos de moeda ISO com três letras."}
        url = self.API + "?" + urllib.parse.urlencode(
            {"from": source, "to": destination})
        data = self._post(url)
        if not isinstance(data, dict):
            self.stats["failures"] += 1
            return {"ok": False, "speech": "Frankfurter indisponível."}
        rate = (data.get("rates") or {}).get(destination)
        if not isinstance(rate, (int, float)):
            self.stats["failures"] += 1
            return {"ok": False, "speech": "Moeda de destino não encontrada."}
        self.stats["queries"] += 1
        return {"ok": True, "rate": float(rate), "base": source,
                "target": destination,
                "speech": "1 %s = %.4f %s" % (source, float(rate), destination)}


def build_external_intel_tools_v2(cc: Any, wiki: Any = None, ddg: Any = None,
                                  jina: Any = None, football: Any = None,
                                  microlink: Any = None, crossref: Any = None,
                                  openlib: Any = None, frankfurter: Any = None,
                                  kb: Any = None, name_prefix: str = "e_") -> List[str]:
    """Registra somente as fontes E fornecidas pelo integrador.

    ``wiki``, ``ddg``, ``jina`` e ``football`` são aceitos apenas para
    compatibilidade com o hunk do anexo; a Instalação E não os instancia nem
    os altera. Os nomes registrados usam ``name_prefix`` para evitar colisão.
    """
    prefix = str(name_prefix or "e_")
    registered: List[str] = []

    def register(name: str, desc: str, handler: Callable[..., Any], args: Dict[str, str]) -> None:
        if cc.register(prefix + name, desc, handler, "read", args=args,
                       confirm=False):
            registered.append(prefix + name)

    if microlink is not None:
        def t_microlink(args: dict, session: str) -> dict:
            result = microlink.read_markdown(str(args.get("url", "")))
            if result.get("ok") and kb is not None and hasattr(kb, "add_text"):
                kb.add_text("Doc E: %s" % str(args.get("url", ""))[:80],
                            result.get("content", ""), source="installation_e_microlink")
            return result
        register("doc_converter", "converter URL pública para Markdown (Microlink E)",
                 t_microlink, {"url": "URL pública HTTP ou HTTPS"})

    if crossref is not None:
        def t_crossref(args: dict, session: str) -> dict:
            result = crossref.search(str(args.get("termo", "")))
            if result.get("ok") and kb is not None and hasattr(kb, "add_text"):
                for finding in result.get("findings", [])[:2]:
                    kb.add_text("Paper E: %s" % finding.get("title", ""),
                                "DOI: %s | Autores: %s | Ano: %s" % (
                                    finding.get("doi", ""), finding.get("authors", ""),
                                    finding.get("year", "")),
                                source="installation_e_crossref")
            return result
        register("paper_pesquisar", "pesquisar papers acadêmicos (Crossref E)",
                 t_crossref, {"termo": "assunto"})

    if openlib is not None:
        def t_openlib(args: dict, session: str) -> dict:
            return openlib.search(str(args.get("termo", "")))
        register("livro_pesquisar", "buscar livros no catálogo Open Library E",
                 t_openlib, {"termo": "título ou autor"})

    if frankfurter is not None:
        def t_moeda(args: dict, session: str) -> dict:
            return frankfurter.rate(str(args.get("de", "EUR")),
                                    str(args.get("para", "BRL")))
        register("moeda_cotacao", "consultar cotação pública de câmbio E",
                 t_moeda, {"de": "ISO", "para": "ISO"})

    return registered


def _self_test() -> int:
    failures: List[str] = []

    def check(label: str, condition: bool) -> None:
        print("[%s] %s" % ("PASS" if condition else "FAIL", label))
        if not condition:
            failures.append(label)

    def fake_microlink(url: str) -> dict:
        return {"status": "success", "data": {"content": "# documento de teste"}}

    def fake_crossref(url: str) -> dict:
        return {"message": {"items": [{"title": ["Paper de teste"],
                                          "DOI": "10.0000/teste",
                                          "issued": {"date-parts": [[2026]]},
                                          "author": [{"family": "Silva", "given": "Ana"}]}]}}

    def fake_openlib(url: str) -> dict:
        return {"docs": [{"title": "Livro de teste", "author_name": ["Autor"],
                            "first_publish_year": 2026, "number_of_pages_median": 100}]}

    def fake_fx(url: str) -> dict:
        return {"rates": {"BRL": 6.0}}

    micro = MicrolinkSource(fake_microlink, min_interval=0)
    cross = CrossrefSource(fake_crossref, min_interval=0)
    books = OpenLibrarySource(fake_openlib, min_interval=0)
    fx = FrankfurterSource(fake_fx)
    check("Microlink converte URL pública", micro.read_markdown("https://example.com")['ok'])
    check("Microlink rejeita caminho local", not micro.read_markdown("C:/arquivo.pdf")['ok'])
    check("Crossref retorna achado", len(cross.search("aura")["findings"]) == 1)
    check("Open Library retorna achado", len(books.search("python")["findings"]) == 1)
    check("Frankfurter valida cotação", fx.rate("EUR", "BRL")["rate"] == 6.0)
    check("Frankfurter rejeita código inválido", not fx.rate("EURO", "BRL")["ok"])

    try:
        try:
            from engine.agents.jarvis_command_center import CommandCenter
        except ModuleNotFoundError:
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from engine.agents.jarvis_command_center import CommandCenter
        cc = CommandCenter()
        names = build_external_intel_tools_v2(
            cc, microlink=micro, crossref=cross, openlib=books, frankfurter=fx)
        check("registro E usa quatro nomes prefixados", len(names) == 4 and all(n.startswith("e_") for n in names))
        check("registro E não contém vocabulário proibido", cc.stats()["command_center"]["denylist_rejected"] == 0)
        check("ferramenta E executa somente leitura", cc.execute("e_moeda_cotacao", {"de": "EUR", "para": "BRL"}, "teste")["ok"])
    except Exception as exc:
        check("integração opcional com CommandCenter", False)
        print("integration_error=%s" % exc)

    if failures:
        print("SELF-TEST FALHOU: %s" % ", ".join(failures))
        return 1
    print("ALL TESTS PASSED - external_sources_e.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
