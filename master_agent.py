#!/usr/bin/env python3
"""
master_agent.py — Agente Mestre de Governança, R&D e Otimização.

Cumpre o protocolo original:
  Passo 1: Auditoria do Sistema (lê artefatos do Pipeline B)
  Passo 2: Pesquisa de Mercado Online (Reddit, GitHub, blogs)
  Passo 3: Engenharia de Software (propõe refatorações)
  Saída:   Relatório diário estruturado

Usa GLM local como motor de raciocínio (via OpenAI-compatible API).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import yaml

# Path setup
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.glm_analysis_agent import GLMClient, GLMConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MESTRE] %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)


# ============================================================================
# ESTRUTURAS DE DADOS
# ============================================================================

@dataclass
class AuditFindings:
    """Resultado do Passo 1 — Auditoria."""
    precision: float = 0.0
    recall: float = 0.0
    total_decisions: int = 0
    false_positive_patterns: List[str] = field(default_factory=list)
    reliable_patterns: List[Dict] = field(default_factory=list)
    proposed_thresholds: Dict[str, Any] = field(default_factory=dict)
    raw_report: str = ""
    raw_tuning: Dict = field(default_factory=dict)


@dataclass
class ResearchFindings:
    """Resultado do Passo 2 — Pesquisa."""
    reddit_posts: List[Dict] = field(default_factory=list)
    github_repos: List[Dict] = field(default_factory=list)
    insights: str = ""  # síntese do GLM sobre o que encontrou


@dataclass
class RefactorProposals:
    """Resultado do Passo 3 — Engenharia."""
    bottlenecks: List[str] = field(default_factory=list)
    code_patches: List[Dict] = field(default_factory=list)  # {file, issue, patch}
    prompt_directives: str = ""


# ============================================================================
# PASSO 1 — AUDITOR DO SISTEMA
# ============================================================================

class SystemAuditor:
    """Lê artefatos do Pipeline B e sintetiza com GLM."""

    def __init__(self, glm: GLMClient, pipeline_dir: Path, regs_dir: Path):
        self.glm = glm
        self.pipeline_dir = pipeline_dir  # data/
        self.regs_dir = regs_dir

    async def audit(self) -> AuditFindings:
        findings = AuditFindings()

        # Lê APRENDIZADO DO DIA (mais recente)
        learning_dir = self.pipeline_dir / "daily_learning"
        if learning_dir.exists():
            reports = sorted(learning_dir.glob("*.md"), reverse=True)
            if reports:
                findings.raw_report = reports[0].read_text(encoding="utf-8")

        # Lê tuning_report (mais recente)
        tuning_dir = self.pipeline_dir / "tuning"
        if tuning_dir.exists():
            tuning_reports = sorted(tuning_dir.glob("tuning_report_*.md"), reverse=True)
            if tuning_reports:
                tuning_text = tuning_reports[0].read_text(encoding="utf-8")
                # Extrai stats globais do relatório
                findings.precision = self._extract_metric(tuning_text, "Precisão")
                findings.recall = self._extract_metric(tuning_text, "Recall")
                findings.total_decisions = self._extract_int(tuning_text, "Decisões resolvidas")

            # Lê glm_config.tomorrow.yaml (propostas)
            tomorrow_cfg = tuning_dir / "glm_config.tomorrow.yaml"
            if tomorrow_cfg.exists():
                try:
                    cfg = yaml.safe_load(tomorrow_cfg.read_text(encoding="utf-8"))
                    findings.proposed_thresholds = cfg or {}
                except yaml.YAMLError:
                    pass

        # Lê glm_memory.json (padrões confiáveis)
        memory_path = self.pipeline_dir / "glm_memory.json"
        if memory_path.exists():
            try:
                mem = json.loads(memory_path.read_text(encoding="utf-8"))
                trigger_stats = mem.get("trigger_stats", {})
                for key, stats in trigger_stats.items():
                    total = stats.get("total", 0)
                    hits = stats.get("hits", 0)
                    if total >= 5:
                        rate = hits / total if total > 0 else 0
                        if rate >= 0.6:
                            findings.reliable_patterns.append({
                                "triggers": key, "rate": rate, "n": total
                            })
                        elif rate < 0.4 and total >= 10:
                            findings.false_positive_patterns.append(
                                f"{key} ({rate:.0%}, n={total})"
                            )
            except json.JSONDecodeError:
                pass

        # Síntese com GLM (se disponível)
        if findings.raw_report:
            findings.insights = await self._glm_synthesize(findings)

        return findings

    def _extract_metric(self, text: str, metric_name: str) -> float:
        """Extrai métrica percentual do relatório."""
        pattern = rf"{metric_name}[^0-9]*([\d.]+)%"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1)) / 100.0
            except ValueError:
                pass
        return 0.0

    def _extract_int(self, text: str, label: str) -> int:
        pattern = rf"{label}[^0-9]*(\d+)"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                pass
        return 0

    async def _glm_synthesize(self, findings: AuditFindings) -> str:
        """Usa GLM para sintetizar a auditoria."""
        prompt = f"""Você é um auditor de sistema de trading. Analise os dados abaixo
e produza uma síntese executiva em texto livre (máx 300 palavras).

PRECISÃO atual: {findings.precision:.1%}
RECALL atual: {findings.recall:.1%}
Total de decisões resolvidas: {findings.total_decisions}

PADRÕES CONFIÁVEIS (manter):
{json.dumps(findings.reliable_patterns, ensure_ascii=False, indent=2)}

PADRÕES DE FALSO POSITIVO (revisar):
{findings.false_positive_patterns}

THRESHOLDS PROPOSTOS PARA AMANHÃ:
{json.dumps(findings.proposed_thresholds, ensure_ascii=False, indent=2)}

Identifique:
1. Qual o maior problema do sistema agora?
2. Quais padrões merecem mais peso?
3. Os thresholds propostos fazem sentido?
"""
        response = await self.glm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=800,
        )
        return response or "(GLM indisponível — síntese manual necessária)"


# ============================================================================
# PASSO 2 — PESQUISADOR DE MERCADO
# ============================================================================

class MarketResearcher:
    """Pesquisa fontes públicas (Reddit, GitHub) e sintetiza com GLM."""

    REDDIT_URL = "https://www.reddit.com/r/{sub}/top.json?limit=10&t=day"
    GITHUB_URL = "https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page=5"

    SUBREDDITS = ["SoccerBetting", "algobetting", "sportsbetting"]
    GITHUB_QUERIES = ["football corners prediction", "soccer betting model python", "hawkes process football"]

    def __init__(self, glm: GLMClient):
        self.glm = glm

    async def research(self) -> ResearchFindings:
        findings = ResearchFindings()
        async with aiohttp.ClientSession() as session:
            tasks = []
            # Reddit
            for sub in self.SUBREDDITS:
                url = self.REDDIT_URL.format(sub=sub)
                tasks.append(self._fetch_reddit(session, url, sub))
            # GitHub
            for q in self.GITHUB_QUERIES:
                url = self.GITHUB_URL.format(q=q.replace(" ", "+"))
                tasks.append(self._fetch_github(session, url, q))

            results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"Pesquisa falhou: {r}")
                continue
            if r.get("source") == "reddit":
                findings.reddit_posts.extend(r.get("posts", []))
            elif r.get("source") == "github":
                findings.github_repos.extend(r.get("repos", []))

        # Síntese com GLM
        if findings.reddit_posts or findings.github_repos:
            findings.insights = await self._glm_synthesize(findings)

        return findings

    async def _fetch_reddit(self, session: aiohttp.ClientSession, url: str, sub: str) -> Dict:
        """Busca top posts de subreddit."""
        headers = {"User-Agent": "AURA-Master-Agent/1.0"}
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {"source": "reddit", "posts": []}
                data = await resp.json()
                posts = []
                for child in data.get("data", {}).get("children", [])[:5]:
                    post = child.get("data", {})
                    posts.append({
                        "title": post.get("title", ""),
                        "score": post.get("score", 0),
                        "url": post.get("url", ""),
                        "selftext": (post.get("selftext") or "")[:500],
                        "subreddit": sub,
                    })
                return {"source": "reddit", "posts": posts}
        except Exception as e:
            logger.warning(f"Reddit {sub} falhou: {e}")
            return {"source": "reddit", "posts": []}

    async def _fetch_github(self, session: aiohttp.ClientSession, url: str, query: str) -> Dict:
        """Busca repositórios do GitHub."""
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "AURA-Master-Agent/1.0"}
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {"source": "github", "repos": []}
                data = await resp.json()
                repos = []
                for item in data.get("items", [])[:5]:
                    repos.append({
                        "name": item.get("full_name", ""),
                        "stars": item.get("stargazers_count", 0),
                        "description": item.get("description", ""),
                        "url": item.get("html_url", ""),
                        "query": query,
                    })
                return {"source": "github", "repos": repos}
        except Exception as e:
            logger.warning(f"GitHub '{query}' falhou: {e}")
            return {"source": "github", "repos": []}

    async def _glm_synthesize(self, findings: ResearchFindings) -> str:
        """Usa GLM para sintetizar descobertas de pesquisa."""
        reddit_summary = "\n".join(
            f"- r/{p['subreddit']}: {p['title']} (score {p['score']})"
            for p in findings.reddit_posts[:5]
        )
        github_summary = "\n".join(
            f"- {r['name']} ({r['stars']}★): {r['description']}"
            for r in findings.github_repos[:5]
        )

        prompt = f"""Você é um pesquisador de mercado de trading esportivo.
Analise as novidades encontradas hoje e produza insights acionáveis (máx 300 palavras).

REDDIT (r/SoccerBetting, r/algobetting, r/sportsbetting):
{reddit_summary or "(nenhum post relevante)"}

GITHUB (repositórios recentes):
{github_summary or "(nenhum repo relevante)"}

Identifique:
1. Há alguma estratégia ou ferramenta nova que vale adaptar?
2. Algum padrão discutido que corrobora ou contradiz nosso sistema?
3. Há repositórios com código útil para estudar?
"""
        response = await self.glm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=800,
        )
        return response or "(GLM indisponível)"


# ============================================================================
# PASSO 3 — ENGENHEIRO DE REFACTORY
# ============================================================================

class CodeRefactorer:
    """Analisa código dos agentes e propõe otimizações via GLM."""

    TARGET_FILES = [
        "agents/glm_analysis_agent.py",
        "agents/feedback_connector.py",
        "agents/threshold_tuner.py",
        "agents/prompt_builder.py",
    ]

    def __init__(self, glm: GLMClient, project_root: Path):
        self.glm = glm
        self.root = project_root

    async def analyze(self) -> RefactorProposals:
        proposals = RefactorProposals()

        for rel_path in self.TARGET_FILES:
            file_path = self.root / rel_path
            if not file_path.exists():
                continue
            code = file_path.read_text(encoding="utf-8")
            # Limita envio (custo de tokens)
            snippet = code[:3000]
            patch = await self._glm_analyze_code(rel_path, snippet)
            if patch:
                proposals.code_patches.append(patch)

        # Diretrizes de prompt para agentes de linha
        proposals.prompt_directives = await self._glm_prompt_directives(proposals.code_patches)

        return proposals

    async def _glm_analyze_code(self, filename: str, code: str) -> Optional[Dict]:
        """Usa GLM para encontrar gargalos em código."""
        prompt = f"""Você é um engenheiro de software sênior especializado em Python async.
Analise o código abaixo e identifique problemas concretos. Responda APENAS em JSON:

{{
  "file": "{filename}",
  "bottleneck": "descrição do gargalo ou null",
  "issue": "descrição específica ou null",
  "patch": "trecho de código corrigido ou null",
  "severity": "high|medium|low|null"
}}

Foque em:
1. Latência (chamadas síncronas em loop async, I/O bloqueante)
2. Memória (estruturas que crescem sem bound, caches sem TTL)
3. Concorrência (deadlocks potenciais, semáforos mal usados)
4. Bugs óbvios (variável sombreada, ternário inútil, off-by-one)

CÓDIGO:
```python
{code}
```
"""
        response = await self.glm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1500,
        )
        if not response:
            return None
        try:
            clean = response.strip()
            if "```json" in clean:
                clean = clean.split("```json")[1].split("```")[0]
            return json.loads(clean)
        except json.JSONDecodeError:
            return None

    async def _glm_prompt_directives(self, patches: List[Dict]) -> str:
        """Gera diretrizes de prompt para agentes de linha baseadas nas descobertas."""
        if not patches:
            return "(sem refatorações propostas hoje)"

        patches_summary = "\n".join(
            f"- {p.get('file')}: {p.get('issue', 'N/D')} [{p.get('severity', 'N/D')}]"
            for p in patches
        )
        prompt = f"""Você é um arquiteto de prompts. Baseado nas refatorações propostas hoje,
gere diretrizes curtas (máx 150 palavras) para os prompts dos agentes de linha de 20 jogos.

Refatorações de hoje:
{patches_summary}

Gere instruções claras que os agentes de linha devem incorporar. Foque em:
1. Palavras-chave novas que devem aparecer no prompt
2. Comportamentos a evitar (baseado nos bugs encontrados)
3. Métricas a enfatizar
"""
        response = await self.glm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=400,
        )
        return response or "(GLM indisponível)"


# ============================================================================
# RELATORISTA — MONTA O RELATÓRIO FINAL
# ============================================================================

class ReportBuilder:
    """Monta relatório diário no formato do protocolo original."""

    @staticmethod
    def build(audit: AuditFindings, research: ResearchFindings,
              refactor: RefactorProposals, date_str: str) -> str:
        lines = [
            f"# 🏆 RELATÓRIO DO AGENTE MESTRE - {date_str}",
            "",
            "### 📊 1. ATUALIZAÇÃO DA BASE DE CONHECIMENTO (Pesos Ajustados)",
            f"- **Precisão atual:** {audit.precision:.1%}",
            f"- **Recall atual:** {audit.recall:.1%}",
            f"- **Decisões resolvidas:** {audit.total_decisions}",
            "",
            "**Padrões validados (manter peso alto):**",
        ]
        if audit.reliable_patterns:
            for p in audit.reliable_patterns[:5]:
                lines.append(f"  - `{p['triggers']}` → {p['rate']:.0%} (n={p['n']})")
        else:
            lines.append("  - (nenhum padrão atingiu confiança mínima)")

        lines.append("\n**Padrões de falso positivo (reduzir peso):**")
        if audit.false_positive_patterns:
            for fp in audit.false_positive_patterns[:5]:
                lines.append(f"  - {fp}")
        else:
            lines.append("  - (nenhum identificado hoje)")

        if audit.proposed_thresholds:
            lines.append("\n**Ajustes de Filtro propostos:**")
            for key, val in audit.proposed_thresholds.items():
                if not key.startswith("_") and not key.startswith("#"):
                    lines.append(f"  - `{key}`: {val}")

        if audit.insights:
            lines.append("\n**Síntese do GLM:**")
            lines.append(audit.insights)

        # Seção 2 — Inteligência de Mercado
        lines += [
            "",
            "### 🔎 2. INTELIGÊNCIA DE MERCADO (Novidades do Front de Trading)",
        ]
        if research.reddit_posts or research.github_repos:
            if research.reddit_posts:
                lines.append("\n**Reddit (top posts do dia):**")
                for p in research.reddit_posts[:5]:
                    lines.append(f"  - r/{p['subreddit']}: [{p['title']}]({p['url']}) (score {p['score']})")
            if research.github_repos:
                lines.append("\n**GitHub (repositórios relevantes):**")
                for r in research.github_repos[:5]:
                    lines.append(f"  - [{r['name']}]({r['url']}) ({r['stars']}★): {r['description']}")
        else:
            lines.append("- (nenhuma novidade encontrada hoje)")

        if research.insights:
            lines.append("\n**Aplicação Prática no Nosso Sistema:**")
            lines.append(research.insights)

        # Seção 3 — Proposta de Atualização de Código
        lines += [
            "",
            "### 🛠️ 3. PROPOSTA DE ATUALIZAÇÃO DE CÓDIGO (Otimização)",
        ]
        if refactor.code_patches:
            for patch in refactor.code_patches:
                if patch.get("issue"):
                    lines.append(f"\n**Arquivo:** `{patch.get('file')}`")
                    lines.append(f"- **Gargalo:** {patch.get('bottleneck', 'N/D')}")
                    lines.append(f"- **Severidade:** {patch.get('severity', 'N/D')}")
                    if patch.get("patch"):
                        lines.append("\n```python")
                        lines.append(patch["patch"])
                        lines.append("```")
        else:
            lines.append("- (nenhuma refatoração necessária hoje)")

        # Seção 4 — Diretrizes de Prompts
        lines += [
            "",
            "### 📈 4. DIRETRIZES DE PROMPTS PARA OS AGENTES DE LINHA",
            refactor.prompt_directives or "- (sem diretrizes novas hoje)",
            "",
            "---",
            f"*Relatório gerado em {datetime.now(timezone.utc).isoformat()}*",
        ]
        return "\n".join(lines)


# ============================================================================
# AGENTE MESTRE — ORQUESTRADOR
# ============================================================================

class MasterAgent:
    """Agente Mestre — executa rotina diária completa."""

    def __init__(self, glm_config: GLMConfig, pipeline_dir: Path,
                 regs_dir: Path, project_root: Path, output_dir: Path):
        self.glm = GLMClient(glm_config)
        self.auditor = SystemAuditor(self.glm, pipeline_dir, regs_dir)
        self.researcher = MarketResearcher(self.glm)
        self.refactorer = CodeRefactorer(self.glm, project_root)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def run_daily(self) -> Path:
        """Executa os 3 passos + gera relatório."""
        logger.info("=" * 60)
        logger.info("🏆 AGENTE MESTRE — ROTINA DIÁRIA INICIADA")
        logger.info("=" * 60)

        # Passo 1: Auditoria
        logger.info("📋 Passo 1: Auditoria do Sistema...")
        await self.glm.start()
        audit = await self.auditor.audit()
        logger.info(f"  Precision: {audit.precision:.1%} | Recall: {audit.recall:.1%}")

        # Passo 2: Pesquisa
        logger.info("🔎 Passo 2: Pesquisa de Mercado...")
        research = await self.researcher.research()
        logger.info(f"  Reddit posts: {len(research.reddit_posts)} | GitHub repos: {len(research.github_repos)}")

        # Passo 3: Engenharia
        logger.info("🛠️ Passo 3: Análise de Código...")
        refactor = await self.refactorer.analyze()
        logger.info(f"  Refatorações propostas: {len(refactor.code_patches)}")

        # Relatório
        logger.info("📝 Gerando relatório...")
        date_str = datetime.now().strftime("%Y-%m-%d")
        report = ReportBuilder.build(audit, research, refactor, date_str)
        report_path = self.output_dir / f"master_report_{date_str}.md"
        report_path.write_text(report, encoding="utf-8")

        await self.glm.close()

        logger.info(f"✅ Relatório salvo: {report_path}")
        logger.info("=" * 60)
        return report_path


# ============================================================================
# ENTRY POINT
# ============================================================================

async def main():
    import argparse

    ap = argparse.ArgumentParser(description="Agente Mestre AURA")
    ap.add_argument("--pipeline-dir", default="data", type=Path)
    ap.add_argument("--regs-dir", default="bridge/regs", type=Path)
    ap.add_argument("--project-root", default=".", type=Path)
    ap.add_argument("--output", default="data/master_reports", type=Path)
    ap.add_argument("--glm-base", default="http://localhost:11434/v1")
    ap.add_argument("--glm-model", default="glm-4.7-flash")
    args = ap.parse_args()

    config = GLMConfig()
    config.api_base = args.glm_base
    config.model_name = args.glm_model

    master = MasterAgent(
        glm_config=config,
        pipeline_dir=args.pipeline_dir,
        regs_dir=args.regs_dir,
        project_root=args.project_root,
        output_dir=args.output,
    )
    report_path = await master.run_daily()
    print(f"\n🏆 Relatório gerado: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
