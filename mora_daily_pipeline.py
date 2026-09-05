#!/usr/bin/env python3
"""
mora_daily_pipeline.py — Pipeline Diário do Agente MORA.

3 Fases:
1. Auditoria Interna (omnipotent_health_profiler + quality_audit)
2. Pesquisa Externa (Reddit + GitHub + arXiv + GLM síntese)
3. Síntese de Código (análise de logs + código + GLM patches)

Saída: data/mora_reports/MORA_DAILY_REPORT_YYYYMMDD.md
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


# ============================================================================
# ESTRUTURAS DE DADOS DAS 3 FASES
# ============================================================================

@dataclass
class AuditResult:
    """Resultado da Fase 1 — Auditoria Interna."""
    health_profile: Dict[str, Any] = field(default_factory=dict)
    quality_audit: Dict[str, Any] = field(default_factory=dict)
    memory_issues: List[str] = field(default_factory=list)
    drift_issues: List[str] = field(default_factory=list)
    latency_issues: List[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class ResearchResult:
    """Resultado da Fase 2 — Pesquisa Externa."""
    reddit_posts: List[Dict] = field(default_factory=list)
    github_repos: List[Dict] = field(default_factory=list)
    arxiv_papers: List[Dict] = field(default_factory=list)
    insights: str = ""


@dataclass
class CodeSynthesis:
    """Resultado da Fase 3 — Síntese de Código."""
    log_errors: List[Dict] = field(default_factory=list)
    proposed_optimizations: List[Dict] = field(default_factory=list)
    summary: str = ""


# ============================================================================
# PIPELINE PRINCIPAL
# ============================================================================

class MoraDailyPipeline:
    """
    Pipeline diário do MORA com 3 fases.

    Uso:
        pipeline = MoraDailyPipeline(orchestrator, glm_client, project_root)
        report_path = await pipeline.run()
    """

    REDDIT_SUBS = ["SoccerBetting", "algobetting", "sportsbetting"]
    GITHUB_QUERIES = [
        "football corners prediction python",
        "hawkes process sports",
        "soccer betting model machine learning",
    ]
    ARXIV_QUERIES = [
        "cat:stat.ML+football",
        "cat:cs.AI+sports+analytics",
    ]

    CODE_TARGETS = [
        "bridge/server.py",
        "engine/server.py",
        "engine/agent_registry.py",
    ]

    LOG_TARGETS = [
        "live_feed.jsonl",
        "data/glm_decisions.jsonl",
    ]

    def __init__(
        self,
        orchestrator: Any,
        glm_client: Any,
        project_root: Path,
        output_dir: Path = Path("data/mora_reports"),
    ):
        self.orchestrator = orchestrator
        self.glm = glm_client
        self.root = project_root
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def run(self) -> Path:
        """Executa pipeline completo e gera relatório."""
        logger.info("=" * 60)
        logger.info("MORA DAILY PIPELINE — INICIADO")
        logger.info("=" * 60)

        async with self.orchestrator.acquire_resources() as lease:
            logger.info(
                "Recursos: %d agentes pausados, %dMB VRAM",
                len(lease.paused_agents),
                lease.vram_allocated_mb,
            )

            # Fase 1: Auditoria Interna
            logger.info("Fase 1: Auditoria Interna...")
            audit = await self._phase1_internal_audit()

            # Fase 2: Pesquisa Externa
            logger.info("Fase 2: Pesquisa de Mercado...")
            research = await self._phase2_external_research()

            # Fase 3: Síntese de Código
            logger.info("Fase 3: Sintese de Codigo...")
            synthesis = await self._phase3_code_synthesis()

        # Gera relatório (após liberar recursos)
        logger.info("Gerando relatorio...")
        report_path = self._build_report(audit, research, synthesis)
        logger.info("Relatorio salvo: %s", report_path)
        logger.info("=" * 60)
        return report_path

    # ================================================================
    # FASE 1 — AUDITORIA INTERNA
    # ================================================================

    async def _phase1_internal_audit(self) -> AuditResult:
        """Executa auditoria interna usando ferramentas existentes do AURA."""
        result = AuditResult()

        # 1. omnipotent_health_profiler.run_full_diagnostic()
        try:
            from engine.sre.omnipotent_health_profiler import OmnipotentHealthProfiler
            profiler = OmnipotentHealthProfiler()
            diag = profiler.run_full_diagnostic()
            if asyncio.iscoroutine(diag):
                diag = await diag
            if isinstance(diag, dict):
                result.health_profile = diag
            else:
                result.health_profile = {"raw": str(diag)[:2000]}

            # Extrair issues específicas
            self._extract_health_issues(diag, result)

        except ImportError:
            result.summary = "omnipotent_health_profiler nao disponivel"
            logger.warning("Health profiler nao encontrado")
        except Exception as e:
            result.summary = f"Erro no health profiler: {e}"
            logger.error("Erro Fase 1 health profiler: %s", e)

        # 2. aura_quality_audit.py via subprocess
        result.quality_audit = await self._run_quality_audit_script()

        # 3. Síntese via GLM
        if result.health_profile or result.quality_audit:
            result.summary = await self._glm_synthesize_audit(result)

        return result

    def _extract_health_issues(self, diag: Any, result: AuditResult) -> None:
        """Extrai issues específicos do diagnóstico do health profiler."""
        if not isinstance(diag, dict):
            return

        # Memory fragmentation
        mem_check = diag.get("check_memory_fragmentation", {})
        if isinstance(mem_check, dict) and mem_check.get("status") == "warning":
            result.memory_issues.append(
                f"Fragmentacao de memoria: {mem_check.get('detail', 'sem detalhes')}"
            )

        # Schema drift
        drift_check = diag.get("check_schema_drift", {})
        if isinstance(drift_check, dict) and drift_check.get("status") == "warning":
            result.drift_issues.append(
                f"Schema drift: {drift_check.get('detail', 'sem detalhes')}"
            )

        # VRAM fragmentation
        vram_check = diag.get("check_vram_fragmentation", {})
        if isinstance(vram_check, dict) and vram_check.get("status") == "warning":
            result.memory_issues.append(
                f"VRAM fragmentada: {vram_check.get('detail', 'sem detalhes')}"
            )

        # Time to failure
        ttf = diag.get("predict_time_to_failure", {})
        if isinstance(ttf, dict):
            hours = ttf.get("hours", 999)
            if isinstance(hours, (int, float)) and hours < 24:
                result.latency_issues.append(
                    f"Previsao de falha em {hours}h: {ttf.get('reason', 'N/D')}"
                )

    async def _run_quality_audit_script(self) -> Dict[str, Any]:
        """Executa aura_quality_audit.py como subprocess com timeout."""
        audit_script = self.root / "scripts" / "aura_quality_audit.py"
        if not audit_script.exists():
            return {"error": "script nao encontrado"}

        try:
            proc = await asyncio.create_subprocess_exec(
                "python", str(audit_script), "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.root),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

            if stdout:
                try:
                    return json.loads(stdout.decode("utf-8"))
                except json.JSONDecodeError:
                    return {"raw_output": stdout.decode("utf-8")[:2000]}
            return {"error": stderr.decode("utf-8")[:500] if stderr else "sem output"}

        except asyncio.TimeoutError:
            return {"error": "timeout (60s)"}
        except Exception as e:
            return {"error": str(e)}

    async def _glm_synthesize_audit(self, audit: AuditResult) -> str:
        """Usa GLM para sintetizar a auditoria interna."""
        # Prepara dados para o prompt (limita tamanho)
        health_str = json.dumps(audit.health_profile, ensure_ascii=False, indent=2)[:3000]
        quality_str = json.dumps(audit.quality_audit, ensure_ascii=False, indent=2)[:2000]

        prompt = (
            "Voce e um auditor de sistemas de trading. Analise os resultados abaixo "
            "e produza uma sintese executiva (max 300 palavras) identificando:\n"
            "1. Saude geral do sistema (0-10)\n"
            "2. Problemas criticos que requerem acao imediata\n"
            "3. Gargalos de performance detectados\n\n"
            f"RESULTADOS DO HEALTH PROFILER:\n{health_str}\n\n"
            f"RESULTADOS DO QUALITY AUDIT:\n{quality_str}\n\n"
            f"ISSUES DETECTADOS:\n"
            f"- Memory: {audit.memory_issues}\n"
            f"- Drift: {audit.drift_issues}\n"
            f"- Latency: {audit.latency_issues}\n"
        )

        response = await self.glm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=800,
        )
        return response or "(GLM indisponivel para sintese)"

    # ================================================================
    # FASE 2 — PESQUISA EXTERNA
    # ================================================================

    async def _phase2_external_research(self) -> ResearchResult:
        """Executa pesquisa externa via web + GLM."""
        result = ResearchResult()

        async with aiohttp.ClientSession() as session:
            tasks = []

            # Reddit
            for sub in self.REDDIT_SUBS:
                url = f"https://www.reddit.com/r/{sub}/top.json?limit=5&t=day"
                tasks.append(self._fetch_reddit(session, url, sub))

            # GitHub
            for q in self.GITHUB_QUERIES:
                encoded_q = q.replace(" ", "+")
                url = f"https://api.github.com/search/repositories?q={encoded_q}&sort=stars&per_page=3"
                tasks.append(self._fetch_github(session, url, q))

            # arXiv
            for q in self.ARXIV_QUERIES:
                encoded_q = q.replace(" ", "+")
                url = f"http://export.arxiv.org/api/query?search_query={encoded_q}&max_results=3"
                tasks.append(self._fetch_arxiv(session, url, q))

            results = await asyncio.gather(*tasks, return_exceptions=True)

        # Consolida resultados
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Pesquisa falhou: %s", r)
                continue
            source = r.get("source", "")
            if source == "reddit":
                result.reddit_posts.extend(r.get("posts", []))
            elif source == "github":
                result.github_repos.extend(r.get("repos", []))
            elif source == "arxiv":
                result.arxiv_papers.extend(r.get("papers", []))

        # Síntese via GLM
        if result.reddit_posts or result.github_repos or result.arxiv_papers:
            result.insights = await self._glm_synthesize_research(result)

        return result

    async def _fetch_reddit(
        self, session: aiohttp.ClientSession, url: str, sub: str
    ) -> Dict:
        """Busca top posts de um subreddit."""
        headers = {"User-Agent": "AURA-MORA/1.0"}
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return {"source": "reddit", "posts": []}
                data = await resp.json()
                posts = []
                for child in data.get("data", {}).get("children", [])[:5]:
                    p = child.get("data", {})
                    posts.append({
                        "title": p.get("title", ""),
                        "score": p.get("score", 0),
                        "url": p.get("url", ""),
                        "selftext": (p.get("selftext") or "")[:300],
                        "subreddit": sub,
                    })
                return {"source": "reddit", "posts": posts}
        except Exception as e:
            logger.warning("Reddit %s falhou: %s", sub, e)
            return {"source": "reddit", "posts": []}

    async def _fetch_github(
        self, session: aiohttp.ClientSession, url: str, query: str
    ) -> Dict:
        """Busca repositórios do GitHub."""
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AURA-MORA/1.0",
        }
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return {"source": "github", "repos": []}
                data = await resp.json()
                repos = []
                for item in data.get("items", [])[:3]:
                    repos.append({
                        "name": item.get("full_name", ""),
                        "stars": item.get("stargazers_count", 0),
                        "description": item.get("description", ""),
                        "url": item.get("html_url", ""),
                        "query": query,
                    })
                return {"source": "github", "repos": repos}
        except Exception as e:
            logger.warning("GitHub '%s' falhou: %s", query, e)
            return {"source": "github", "repos": []}

    async def _fetch_arxiv(
        self, session: aiohttp.ClientSession, url: str, query: str
    ) -> Dict:
        """Busca papers do arXiv (Atom XML)."""
        headers = {"User-Agent": "AURA-MORA/1.0"}
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return {"source": "arxiv", "papers": []}
                text = await resp.text()
                # arXiv retorna Atom XML — extração simples via regex
                entries = re.findall(r"<entry>(.*?)</entry>", text, re.DOTALL)
                papers = []
                for entry in entries[:3]:
                    title_match = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
                    summary_match = re.search(r"<summary>(.*?)</summary>", entry, re.DOTALL)
                    link_match = re.search(r"<id>(.*?)</id>", entry)
                    papers.append({
                        "title": title_match.group(1).strip() if title_match else "",
                        "summary": (summary_match.group(1).strip()[:300] if summary_match else ""),
                        "url": link_match.group(1).strip() if link_match else "",
                        "query": query,
                    })
                return {"source": "arxiv", "papers": papers}
        except Exception as e:
            logger.warning("arXiv '%s' falhou: %s", query, e)
            return {"source": "arxiv", "papers": []}

    async def _glm_synthesize_research(self, research: ResearchResult) -> str:
        """Usa GLM para sintetizar descobertas de pesquisa externa."""
        reddit_lines = [
            f"- r/{p['subreddit']}: {p['title']} (score {p['score']})"
            for p in research.reddit_posts[:5]
        ]
        reddit_summary = "\n".join(reddit_lines) if reddit_lines else "(nenhum post)"

        github_lines = [
            f"- {r['name']} ({r['stars']}★): {r['description']}"
            for r in research.github_repos[:5]
        ]
        github_summary = "\n".join(github_lines) if github_lines else "(nenhum repo)"

        arxiv_lines = [
            f"- {p['title']}: {p['summary'][:100]}"
            for p in research.arxiv_papers[:3]
        ]
        arxiv_summary = "\n".join(arxiv_lines) if arxiv_lines else "(nenhum paper)"

        prompt = (
            "Voce e um pesquisador de trading quantitativo. Analise as descobertas "
            "de hoje e produza insights acionaveis (max 300 palavras):\n\n"
            f"REDDIT:\n{reddit_summary}\n\n"
            f"GITHUB:\n{github_summary}\n\n"
            f"ARXIV:\n{arxiv_summary}\n\n"
            "Identifique:\n"
            "1. Estrategias ou ferramentas novas que vale adaptar?\n"
            "2. Padroes discutidos que corroboram ou contradizem nosso sistema?\n"
            "3. Papers com tecnicas aplicaveis (Hawkes, Poisson, ML)?\n"
        )

        response = await self.glm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=800,
        )
        return response or "(GLM indisponivel)"

    # ================================================================
    # FASE 3 — SÍNTESE DE CÓDIGO
    # ================================================================

    async def _phase3_code_synthesis(self) -> CodeSynthesis:
        """Analisa logs e código, propõe otimizações via GLM."""
        result = CodeSynthesis()

        # 1. Analisar logs de erro
        result.log_errors = await self._analyze_logs()

        # 2. Analisar código-fonte
        for target in self.CODE_TARGETS:
            file_path = self.root / target
            if not file_path.exists():
                continue
            try:
                code = file_path.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("Nao foi possivel ler %s: %s", target, e)
                continue

            # Limita para não estourar contexto do GLM
            snippet = code[:4000]
            optimization = await self._glm_analyze_code(target, snippet)
            if optimization:
                result.proposed_optimizations.append(optimization)

        # 3. Síntese final
        result.summary = await self._glm_synthesize_code(result)

        return result

    async def _analyze_logs(self) -> List[Dict]:
        """Extrai erros e padrões anômalos dos logs."""
        errors: List[Dict] = []

        for log_target in self.LOG_TARGETS:
            log_path = Path(log_target)
            if not log_path.is_absolute():
                log_path = self.root / log_target
            if not log_path.exists():
                continue

            try:
                # Lê últimas 200 linhas
                content = log_path.read_text(encoding="utf-8")
                lines = content.splitlines()[-200:]

                for line in lines:
                    line_lower = line.lower()
                    error_keywords = ("error", "exception", "traceback", "fail")
                    if any(kw in line_lower for kw in error_keywords):
                        try:
                            entry = json.loads(line)
                            errors.append({
                                "source": log_target,
                                "error": entry.get("error", line[:200]),
                                "timestamp": entry.get("ts", entry.get("received_at", "")),
                            })
                        except json.JSONDecodeError:
                            errors.append({
                                "source": log_target,
                                "error": line[:200],
                                "timestamp": "",
                            })
            except Exception as e:
                logger.warning("Leitura de log %s falhou: %s", log_target, e)

        # Limita a 20 erros para não sobrecarregar o GLM
        return errors[:20]

    async def _glm_analyze_code(self, filename: str, code: str) -> Optional[Dict]:
        """Usa GLM para propor otimizações em código."""
        prompt = (
            "Voce e um engenheiro Python senior especializado em async e alta performance.\n"
            "Analise o codigo abaixo e identifique problemas concretos.\n"
            "Responda APENAS em JSON:\n\n"
            "{\n"
            f'  "file": "{filename}",\n'
            '  "bottleneck": "descricao do gargalo ou null",\n'
            '  "issue": "descricao especifica ou null",\n'
            '  "patch": "trecho corrigido ou null",\n'
            '  "severity": "high|medium|low|null"\n'
            "}\n\n"
            "Foque em:\n"
            "1. Latencia (I/O sincrono em async, polling desnecessario)\n"
            "2. Memoria (estruturas sem bound, caches sem TTL)\n"
            "3. Concorrencia (locks excessivos, semaforos mal usados)\n"
            "4. Bugs (variavel sombreada, ternario inutil, off-by-one)\n\n"
            "CODIGO:\n"
            "```python\n"
            f"{code}\n"
            "```\n"
        )

        response = await self.glm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1500,
        )
        if not response:
            return None

        # Parse robusto de JSON
        clean = response.strip()
        if "```json" in clean:
            parts = clean.split("```json")
            if len(parts) > 1:
                clean = parts[1].split("```")[0]
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            return None

    async def _glm_synthesize_code(self, synthesis: CodeSynthesis) -> str:
        """Síntese final das otimizações propostas."""
        if not synthesis.proposed_optimizations and not synthesis.log_errors:
            return "Sistema estavel — sem otimizacoes urgentes hoje."

        opts_lines = [
            f"- {o.get('file')}: {o.get('issue', 'N/D')} [{o.get('severity', 'N/D')}]"
            for o in synthesis.proposed_optimizations
        ]
        opts_summary = "\n".join(opts_lines) if opts_lines else "(nenhuma)"

        errors_lines = [
            f"- {e['source']}: {e['error'][:100]}"
            for e in synthesis.log_errors[:10]
        ]
        errors_summary = "\n".join(errors_lines) if errors_lines else "(nenhum)"

        prompt = (
            "Voce e um tech lead. Sintetize as descobertas de hoje (max 200 palavras):\n\n"
            f"OTIMIZACOES PROPOSTAS:\n{opts_summary}\n\n"
            f"ERROS RECENTES:\n{errors_summary}\n\n"
            "Produza:\n"
            "1. Prioridade de acao (o que fazer primeiro)\n"
            "2. Risco de nao fazer nada\n"
            "3. Esforco estimado (baixo/medio/alto)\n"
        )

        response = await self.glm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        return response or "(GLM indisponivel)"

    # ================================================================
    # GERAÇÃO DE RELATÓRIO
    # ================================================================

    def _build_report(
        self,
        audit: AuditResult,
        research: ResearchResult,
        synthesis: CodeSynthesis,
    ) -> Path:
        """Monta relatório MORA_DAILY_REPORT_YYYYMMDD.md."""
        date_str = datetime.now().strftime("%Y%m%d")
        date_iso = datetime.now().strftime("%Y-%m-%d")
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        lines: List[str] = [
            f"# MORA DAILY REPORT — {date_iso}",
            "",
            f"**Gerado em:** {timestamp}",
            "**Pipeline:** AURA QUANT-X V25 — MORA Master Agent",
            "**Invariantes:** paper_trade=true | execution_allowed=false | GLM_ADVISORY_ONLY=true",
            "",
            "---",
            "",
            "## 1. RESUMO DE SAUDE DO SISTEMA",
            "",
            "**Sintese do GLM:**",
            audit.summary or "(sem dados)",
            "",
        ]

        # Issues detectados
        lines.append("**Issues Detectados:**")
        if audit.memory_issues:
            lines.append("")
            lines.append("### Memoria")
            for issue in audit.memory_issues:
                lines.append(f"- WARNING {issue}")
        if audit.drift_issues:
            lines.append("")
            lines.append("### Drift")
            for issue in audit.drift_issues:
                lines.append(f"- WARNING {issue}")
        if audit.latency_issues:
            lines.append("")
            lines.append("### Latencia")
            for issue in audit.latency_issues:
                lines.append(f"- WARNING {issue}")
        if not (audit.memory_issues or audit.drift_issues or audit.latency_issues):
            lines.append("- OK Nenhum issue critico detectado")

        # Detalhes do health profiler (collapsible)
        if audit.health_profile:
            lines.append("")
            lines.append("<details><summary>Detalhes do Health Profiler</summary>")
            lines.append("")
            lines.append("```json")
            health_json = json.dumps(
                audit.health_profile, ensure_ascii=False, indent=2
            )[:3000]
            lines.append(health_json)
            lines.append("```")
            lines.append("")
            lines.append("</details>")

        # Seção 2 — Pesquisa de Mercado
        lines += [
            "",
            "---",
            "",
            "## 2. PESQUISA DE MERCADO (Novidades)",
            "",
        ]

        if research.reddit_posts:
            lines.append("### Reddit (Top Posts do Dia)")
            for p in research.reddit_posts[:5]:
                lines.append(
                    f"- **r/{p['subreddit']}**: [{p['title']}]({p['url']}) "
                    f"(score {p['score']})"
                )
                if p.get("selftext"):
                    lines.append(f"  > {p['selftext'][:150]}...")

        if research.github_repos:
            lines.append("")
            lines.append("### GitHub (Repositorios Relevantes)")
            for r in research.github_repos[:5]:
                lines.append(
                    f"- **[{r['name']}]({r['url']})** ({r['stars']}★): "
                    f"{r['description']}"
                )

        if research.arxiv_papers:
            lines.append("")
            lines.append("### arXiv (Papers Academicos)")
            for p in research.arxiv_papers[:3]:
                lines.append(f"- **[{p['title']}]({p['url']})**")
                if p.get("summary"):
                    lines.append(f"  > {p['summary'][:200]}...")

        if research.insights:
            lines.append("")
            lines.append("**Insights do GLM:**")
            lines.append(research.insights)

        # Seção 3 — Otimizações de Código
        lines += [
            "",
            "---",
            "",
            "## 3. OPORTUNIDADES DE OTIMIZACAO DE CODIGO",
            "",
        ]

        if synthesis.proposed_optimizations:
            for opt in synthesis.proposed_optimizations:
                lines.append(f"### `{opt.get('file', 'N/A')}`")
                lines.append(f"- **Gargalo:** {opt.get('bottleneck', 'N/D')}")
                lines.append(f"- **Issue:** {opt.get('issue', 'N/D')}")
                lines.append(f"- **Severidade:** {opt.get('severity', 'N/D')}")
                if opt.get("patch"):
                    lines.append("")
                    lines.append("```python")
                    lines.append(opt["patch"])
                    lines.append("```")
                lines.append("")
        else:
            lines.append("- OK Nenhuma otimizacao urgente identificada")

        if synthesis.log_errors:
            lines.append("### Erros Recentes Detectados")
            for err in synthesis.log_errors[:10]:
                lines.append(
                    f"- `{err['source']}`: {err['error'][:150]}"
                )

        # Seção 4 — Ações Recomendadas
        lines += [
            "",
            "---",
            "",
            "## 4. ACOES RECOMENDADAS",
            "",
            synthesis.summary or "- Manter monitoramento regular",
            "",
            "---",
            "",
            "*Relatorio gerado automaticamente pelo MORA Master Agent*",
            f"*AURA QUANT-X V25 — {timestamp}*",
        ]

        report_path = self.output_dir / f"MORA_DAILY_REPORT_{date_str}.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path