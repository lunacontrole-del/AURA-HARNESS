# aura_context_crawler.py — Pre-Match Context Intelligence Engine
import asyncio
from typing import Dict, Any

try:
    import httpx
except ImportError:
    httpx = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


class PreMatchContextCrawler:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        self._cache: Dict[str, Dict[str, Any]] = {}

    async def buscar_previsao_tempo(self, lat: float = -23.55, lon: float = -46.63) -> Dict[str, Any]:
        if httpx is None:
            return {"temperatura_c": 22, "chuva": False, "fator_clima": 1.0}
        try:
            url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
            async with httpx.AsyncClient(timeout=4.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json().get("current_weather", {})
                    weather_code = int(data.get("weathercode", 0))
                    return {
                        "temperatura_c": float(data.get("temperature", 22)),
                        "chuva": weather_code >= 50,
                        "fator_clima": 0.88 if weather_code >= 50 else 1.0,
                    }
        except Exception:
            pass
        return {"temperatura_c": 22, "chuva": False, "fator_clima": 1.0}

    async def buscar_noticias_ge(self, time_casa: str, time_fora: str) -> Dict[str, Any]:
        desfalques_casa, desfalques_fora = 0, 0
        jogo_decisivo = False

        if httpx is None or BeautifulSoup is None:
            return {
                "peso_elenco_casa": 1.0,
                "peso_elenco_fora": 1.0,
                "jogo_decisivo": False,
                "desfalques_casa": 0,
                "desfalques_fora": 0,
            }

        query = f"{time_casa} {time_fora}".replace(" ", "%20")
        url = f"https://ge.globo.com/busca/?q={query}"

        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=5.0, follow_redirects=True) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, "html.parser")
                    textos = [elem.get_text().lower() for elem in soup.find_all(["h2", "h3", "p"]) if elem.get_text()]
                    for txt in textos[:15]:
                        if any(term in txt for term in ["desfalque", "lesionado", "fora do jogo", "suspenso", "poupado"]):
                            if time_casa.lower() in txt:
                                desfalques_casa += 1
                            if time_fora.lower() in txt:
                                desfalques_fora += 1
                        if any(term in txt for term in ["decisão", "final", "clássico", "libertadores", "rebaixamento"]):
                            jogo_decisivo = True
        except Exception:
            pass

        return {
            "peso_elenco_casa": max(0.65, 1.0 - (desfalques_casa * 0.08)),
            "peso_elenco_fora": max(0.65, 1.0 - (desfalques_fora * 0.08)),
            "jogo_decisivo": jogo_decisivo,
            "desfalques_casa": desfalques_casa,
            "desfalques_fora": desfalques_fora,
        }

    async def extrair_contexto(self, time_casa: str, time_fora: str) -> Dict[str, Any]:
        key = f"{time_casa}|{time_fora}".lower()
        if key in self._cache:
            return self._cache[key]
        noticias, clima = await asyncio.gather(
            self.buscar_noticias_ge(time_casa, time_fora),
            self.buscar_previsao_tempo(),
        )
        ctx = {
            "noticias": noticias,
            "clima": clima,
            "fator_pressao": 1.25 if noticias.get("jogo_decisivo") else 1.0,
        }
        self._cache[key] = ctx
        return ctx


context_crawler = PreMatchContextCrawler()
