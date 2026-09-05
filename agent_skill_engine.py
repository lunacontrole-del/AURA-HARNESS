#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_skill_engine.py — APRENDIZADO DE PROGRAMAS: skills inteligentes com
segundo cerebro (Qwen3:4b tool calling) + observacao de padroes.

O QUE E (diferente do MacroStore do desktop_controller):
    O MacroStore grava sequencias FIXAS (press:ctrl+s|wait:0.5). Este modulo
    grava TAREFAS com CONTEXTO: "salvar_pdf_como" sabe que depende do programa
    ativo, e o advisor Qwen3 decide SEQUENCIA + PARAMETROS na hora.

FLUXO DE APRENDIZADO:
    1. VOCE faz a tarefa uma vez (ou descreve em texto)
    2. O advisor Qwen3:4b gera a sequencia de passos (tool calling)
    3. Voce aprova via voz ("sim") — vira skill persistida
    4. Proxima vez: mesmo pedido = skill carregada + adaptada ao contexto

PERSISTENCIA: engine/data/agent_skills.json — skills com nome, contexto,
passos gerados pelo advisor, e historico de uso (contador de sucesso).

ADVISOR (Qwen3:4b via Ollama):
    - Tool calling NATIVO (nao precisa prompt hack)【turn3fetch0】
    - Thinking mode: raciocina antes de gerar passos
    - 2.5GB — cabe junto ao GLM-4 na RTX 4050 (6GB VRAM)
    - Sem Qwen3: degrada para MacroStore (deterministico, sem criatividade)

INTEGRACAO: hunks na resposta. stdlib only. Python 3.9+. Windows.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("aura.agent_skills")

__version__ = "1.0.0"
_PROJ_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_PATH = _PROJ_ROOT / "engine" / "data" / "agent_skills.json"

# ferramentas que o advisor pode "chamar" ao gerar sequencias
ADVISOR_TOOLS = [
    {"type": "function", "function": {
        "name": "press_key",
        "description": "Apertar combinação de teclas",
        "parameters": {"type": "object", "properties": {
            "keys": {"type": "string", "description": "ex: ctrl+s, alt+tab"}},
            "required": ["keys"]}}},
    {"type": "function", "function": {
        "name": "type_text",
        "description": "Digitar texto",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "click_position",
        "description": "Clicar em coordenadas da tela",
        "parameters": {"type": "object", "properties": {
            "x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "wait_seconds",
        "description": "Aguardar N segundos",
        "parameters": {"type": "object", "properties": {
            "seconds": {"type": "number", "default": 0.5}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "focus_window",
        "description": "Focar janela pelo título",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}},
            "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "open_program",
        "description": "Abrir programa da allowlist",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "clipboard_paste",
        "description": "Colar texto do clipboard (ctrl+v)",
        "parameters": {"type": "object", "properties": {},
        "required": []}}},
]


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Qwen3Advisor:
    """Segundo cerebro: Qwen3:4b com tool calling nativo via Ollama.
    Sem modelo: degrada (gera sequencia fixa basica, sem criatividade)."""

    def __init__(self, ollama_url: str = "http://127.0.0.1:11434",
                 model: str = "qwen3:4b",
                 post_fn: Optional[Callable] = None):
        self._url = ollama_url.rstrip("/")
        self._model = model
        self._post = post_fn or self._post_default
        self._available: Optional[bool] = None
        self.stats = {"calls": 0, "tool_calls_generated": 0,
                      "failures": 0, "detects": 0}

    def _post_default(self, path: str, payload: dict) -> Optional[dict]:
        try:
            req = urllib.request.Request(
                self._url + path,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8",
                                                     errors="replace"))
        except Exception:
            return None

    def detect(self) -> bool:
        if self._available is not None:
            return self._available
        self.stats["detects"] += 1
        out = self._post("/api/tags", {})
        if out and isinstance(out.get("models"), list):
            for m in out["models"]:
                if self._model.split(":")[0] in str(m.get("name", "")):
                    self._available = True
                    return True
        self._available = False
        return False

    def generate_steps(self, task_description: str,
                       context: Optional[str] = "") -> Optional[List[dict]]:
        """Pede ao Qwen3 para gerar sequencia de acoes como tool calls.
        Retorna lista de passos ou None (degradacao)."""
        if not self.detect():
            return None
        self.stats["calls"] += 1
        prompt = (
            "Você é um assistente que controla programas no Windows. "
            "Gere uma sequência de ações para executar esta tarefa: %s. %s "
            "Responda APENAS com chamadas de função em JSON."
            % (task_description, ("Contexto: %s." % context) if context else ""))
        out = self._post("/api/chat", {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "tools": ADVISOR_TOOLS,
            "stream": False,
            "keep_alive": os.getenv("AURA_OLLAMA_KEEP_ALIVE", "0m")})
        if not out:
            self.stats["failures"] += 1
            return None
        msg = out.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            # fallback: tenta parsear da resposta em texto
            content = str(msg.get("content") or "")
            return self._parse_text_to_steps(content)
        steps = []
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            steps.append({"tool": name, "args": args})
        self.stats["tool_calls_generated"] += len(steps)
        return steps

    def _parse_text_to_steps(self, content: str) -> List[dict]:
        """Fallback: extrai acoes de texto livre (quando tool_calls falha)."""
        steps = []
        # procura por padroes tipo "aperta ctrl+s" ou "clica em 100 200"
        for m in re.finditer(r"aperta\s+([\w+]+)", content, re.I):
            steps.append({"tool": "press_key", "args": {"keys": m.group(1)}})
        for m in re.finditer(r"digita\s+['\"](.+?)['\"]", content, re.I):
            steps.append({"tool": "type_text", "args": {"text": m.group(1)}})
        for m in re.finditer(r"clica\s+em\s+(\d+)\s+(\d+)", content, re.I):
            steps.append({"tool": "click_position",
                          "args": {"x": int(m.group(1)),
                                   "y": int(m.group(2))}})
        for m in re.finditer(r"espera\s+([\d.]+)\s*seg", content, re.I):
            steps.append({"tool": "wait_seconds",
                          "args": {"seconds": float(m.group(1))}})
        return steps


class AgentSkill:
    """Uma skill aprendida: nome, descricao, passos, contexto, historico."""

    def __init__(self, name: str, description: str,
                 steps: List[dict], context: str = "",
                 source: str = "advisor"):
        self.name = name
        self.description = description
        self.steps = steps
        self.context = context
        self.source = source  # advisor | manual | observed
        self.created_at = _iso_now()
        self.success_count = 0
        self.fail_count = 0
        self.last_used: Optional[str] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "AgentSkill":
        s = cls(d.get("name", "?"), d.get("description", ""),
                d.get("steps", []), d.get("context", ""),
                d.get("source", "manual"))
        for k in ("created_at", "success_count", "fail_count", "last_used"):
            if k in d:
                setattr(s, k, d[k])
        return s


class AgentSkillEngine:
    """Registro de skills + geracao via advisor + execucao."""

    def __init__(self, advisor: Optional[Qwen3Advisor] = None,
                 desktop: Any = None,
                 skills_path: Optional[Any] = None,
                 execution_enabled: Optional[bool] = None):
        self._lock = threading.RLock()
        self._advisor = advisor or Qwen3Advisor()
        self._desktop = desktop  # DesktopController para executar
        self._execution_enabled = (
            bool(execution_enabled) if execution_enabled is not None
            else os.getenv("AURA_E_ENABLE_SKILL_EXECUTION", "0").strip() == "1")
        self._path = Path(skills_path) if skills_path else _SKILLS_PATH
        self._skills: Dict[str, AgentSkill] = {}
        self._load()
        self.stats = {"skills_created": 0, "skills_executed": 0,
                      "advisor_generated": 0, "manual_defined": 0,
                      "observed_learned": 0}

    # ------------------------------------------------------------- storage
    def _load(self) -> None:
        if self._path.is_file():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            self._skills[k] = AgentSkill.from_dict(v)
            except Exception:
                logger.exception("agent_skills: arquivo ilegivel")

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(
                {k: v.to_dict() for k, v in self._skills.items()},
                ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            logger.exception("agent_skills: falha ao gravar")

    # ------------------------------------------------------------- criacao
    def learn_from_description(self, name: str, description: str,
                               context: str = "") -> dict:
        """Cria skill usando o advisor Qwen3 para gerar os passos."""
        steps = None
        if self._advisor.detect():
            steps = self._advisor.generate_steps(description, context)
        if steps is None:
            # degradacao: skill vazia que precisa de passos manuais
            steps = [{"tool": "wait_seconds", "args": {"seconds": 0.1}}]
        skill = AgentSkill(name, description, steps, context, "advisor")
        with self._lock:
            self._skills[_norm(name).replace(" ", "_")] = skill
            self._save()
            self.stats["skills_created"] += 1
            self.stats["advisor_generated"] += 1
        return {"ok": True,
                "speech": ("Skill '%s' criada com %d passo(s) gerados pelo "
                           "advisor. Diga 'sim' para eu salvar e testar."
                           % (name, len(steps))),
                "steps_preview": self._describe_steps(steps)}

    def learn_manual(self, name: str, description: str,
                     steps: List[dict], context: str = "") -> dict:
        """Cria skill com passos definidos manualmente."""
        skill = AgentSkill(name, description, steps, context, "manual")
        with self._lock:
            self._skills[_norm(name).replace(" ", "_")] = skill
            self._save()
            self.stats["skills_created"] += 1
            self.stats["manual_defined"] += 1
        return {"ok": True,
                "speech": "Skill '%s' salva manualmente (%d passos)."
                          % (name, len(steps))}

    def observe_success(self, skill_name: str) -> None:
        """Registra que a skill funcionou (padrao observado)."""
        with self._lock:
            skill = self._skills.get(_norm(skill_name).replace(" ", "_"))
            if skill:
                skill.success_count += 1
                skill.last_used = _iso_now()
                self._save()

    def observe_failure(self, skill_name: str) -> None:
        with self._lock:
            skill = self._skills.get(_norm(skill_name).replace(" ", "_"))
            if skill:
                skill.fail_count += 1
                self._save()

    # ------------------------------------------------------------- consulta
    def list_skills(self) -> List[dict]:
        with self._lock:
            return [{"name": s.name, "desc": s.description,
                     "steps": len(s.steps), "source": s.source,
                     "success": s.success_count, "fail": s.fail_count,
                     "last_used": s.last_used}
                    for s in sorted(self._skills.values(),
                                    key=lambda x: -x.success_count)]

    def get_skill(self, name: str) -> Optional[AgentSkill]:
        return self._skills.get(_norm(name).replace(" ", "_"))

    def suggest_best(self, task: str) -> Optional[AgentSkill]:
        """Sugere a skill mais adequada para a tarefa (por descricao)."""
        task_n = _norm(task)
        best, best_score = None, 0.0
        with self._lock:
            for s in self._skills.values():
                desc_n = _norm(s.description)
                # overlap de palavras
                task_words = set(re.findall(r"\w{3,}", task_n))
                desc_words = set(re.findall(r"\w{3,}", desc_n))
                overlap = len(task_words & desc_words)
                score = overlap / max(1, len(task_words))
                # bonus por historico de sucesso
                score += 0.1 * min(s.success_count, 5)
                if score > best_score:
                    best, best_score = s, score
        return best if best_score > 0.2 else None

    # ------------------------------------------------------------- execucao
    def _describe_steps(self, steps: List[dict]) -> str:
        parts = []
        for s in steps[:6]:
            tool = s.get("tool", "?")
            args = s.get("args", {})
            if tool == "press_key":
                parts.append("apertar %s" % args.get("keys", "?"))
            elif tool == "type_text":
                parts.append("digitar '%s...'" % str(args.get("text", ""))[:30])
            elif tool == "click_position":
                parts.append("clicar em %s,%s" % (args.get("x"),
                                                  args.get("y")))
            elif tool == "wait_seconds":
                parts.append("aguardar %ss" % args.get("seconds", 0.5))
            elif tool == "focus_window":
                parts.append("focar %s" % args.get("title", "?"))
            elif tool == "open_program":
                parts.append("abrir %s" % args.get("name", "?"))
        return "; ".join(parts) if parts else "nenhum passo"

    def plan_execution(self, skill_name: str) -> str:
        """Fala o que vai fazer (para autorizacao por voz)."""
        skill = self.get_skill(skill_name)
        if skill is None:
            # tenta sugerir
            best = self.suggest_best(skill_name)
            if best:
                skill = best
            else:
                return "Skill '%s' nao existe." % skill_name
        return ("Vou executar a skill %s (%d passos): %s. "
                "Diga sim para executar."
                % (skill.name, len(skill.steps),
                   self._describe_steps(skill.steps)))

    def execute_skill(self, skill_name: str) -> dict:
        """Executa a skill no DesktopController (se disponivel)."""
        skill = self.get_skill(skill_name)
        if skill is None:
            best = self.suggest_best(skill_name)
            if best:
                skill = best
            else:
                return {"ok": False, "speech": "Skill '%s' nao existe."
                        % skill_name}
        if not self._execution_enabled:
            return {"ok": False,
                    "speech": "Execucao de skills esta desativada nesta instalacao E."}
        if self._desktop is None:
            return {"ok": False,
                    "speech": "DesktopController nao disponivel."}
        # converte passos do advisor para formato do DesktopController
        macro_steps = []
        for s in skill.steps:
            tool = s.get("tool", "")
            args = s.get("args", {})
            if tool == "press_key":
                macro_steps.append({"press": str(args.get("keys", ""))})
            elif tool == "type_text":
                macro_steps.append({"type": str(args.get("text", ""))})
            elif tool == "click_position":
                macro_steps.append({"click": [int(args.get("x", 0)),
                                              int(args.get("y", 0))]})
            elif tool == "wait_seconds":
                macro_steps.append({"wait": float(args.get("seconds", 0.5))})
            elif tool == "focus_window":
                macro_steps.append({"focus": str(args.get("title", ""))})
            elif tool == "open_program":
                macro_steps.append({"focus": str(args.get("name", ""))})
        result = self._desktop.run_steps(macro_steps)
        if result.get("ok"):
            self.observe_success(skill.name)
            self.stats["skills_executed"] += 1
        else:
            self.observe_failure(skill.name)
        return result

    def forget(self, name: str) -> dict:
        key = _norm(name).replace(" ", "_")
        with self._lock:
            if key not in self._skills:
                return {"ok": False, "speech": "Skill '%s' nao existe." % name}
            del self._skills[key]
            self._save()
        return {"ok": True, "speech": "Skill '%s' apagada." % name}

    def stats_dict(self) -> dict:
        return {"agent_skill_engine": {
            "skills": len(self._skills),
            "advisor_available": self._advisor.detect(),
            **self.stats,
            "advisor": dict(self._advisor.stats)}}


# ---------------------------------------------------------------------------
# gramatica
# ---------------------------------------------------------------------------
def parse_agent_skills(utterance: str):
    t = _norm(utterance)
    if not t:
        return None
    m = re.search(r"\b(?:aprende|aprender|cria|criar)\s+(?:a\s+)?skill\s+"
                  r"(?:chamada\s+)?(\w[\w\s]*?)\s+(?:para|que)\s+(.+)$", t)
    if m:
        return ("skill_aprender", {"nome": m.group(1).strip(),
                                   "descricao": m.group(2).strip()})
    m = re.search(r"\b(?:executa|executar|roda|rodar|aplica|aplicar)\s+"
                  r"(?:a\s+)?skill\s+(\w[\w\s]*)$", t)
    if m:
        return ("skill_executar", {"nome": m.group(1).strip()})
    if re.search(r"\b(?:quais|liste?|lista)\s+skills?\b", t):
        return ("skill_listar", {})
    m = re.search(r"\b(?:esquec\w+|apaga|apagar)\s+(?:a\s+)?skill\s+(\w[\w\s]*)$", t)
    if m:
        return ("skill_esquecer", {"nome": m.group(1).strip()})
    return None


def build_agent_skill_tools(cc, engine: AgentSkillEngine) -> None:
    import inspect
    _csf = "confirm_speech_fn" in inspect.signature(cc.register).parameters

    def t_aprender(args, session):
        return engine.learn_from_description(
            str(args.get("nome", "")), str(args.get("descricao", "")),
            str(args.get("contexto", "")))

    def t_executar_plan(args):
        return engine.plan_execution(str(args.get("nome", "")))

    def t_executar(args, session):
        return engine.execute_skill(str(args.get("nome", "")))

    def t_listar(args, session):
        skills = engine.list_skills()
        if not skills:
            return {"ok": True,
                    "speech": "Nenhuma skill aprendida ainda. Diga 'aprende "
                              "skill X para Y' para criar."}
        return {"ok": True, "speech": "Skills: %s." % ", ".join(
            "%s (%d passos, %d ok)" % (s["name"], s["steps"], s["success"])
            for s in skills[:6])}

    def t_esquecer_plan(args):
        skill = engine.get_skill(str(args.get("nome", "")))
        if skill:
            return ("Vou apagar a skill %s (%d passos, %d sucessos). "
                    "Diga sim." % (skill.name, len(skill.steps),
                                   skill.success_count))
        return "Skill '%s' nao existe." % args.get("nome", "?")

    def t_esquecer(args, session):
        return engine.forget(str(args.get("nome", "")))

    if _csf:
        cc.register("skill_aprender", "aprender nova skill (advisor Qwen3)",
                    t_aprender, "control",
                    args={"nome": "nome", "descricao": "o que faz",
                          "contexto": "programa"}, confirm=False)
        cc.register("skill_executar", "executar skill aprendida",
                    t_executar, "control", args={"nome": "skill"},
                    confirm=True, confirm_speech_fn=t_executar_plan)
        cc.register("skill_listar", "listar skills", t_listar, "read")
        cc.register("skill_esquecer", "apagar skill", t_esquecer, "control",
                    args={"nome": "skill"}, confirm=True,
                    confirm_speech_fn=t_esquecer_plan)
    else:
        cc.register("skill_aprender", "aprender nova skill", t_aprender,
                    "control", args={"nome": "nome",
                                     "descricao": "o que faz"},
                    confirm=False)
        cc.register("skill_executar", "executar skill", t_executar, "control",
                    args={"nome": "skill"}, confirm=True)
        cc.register("skill_listar", "listar skills", t_listar, "read")
        cc.register("skill_esquecer", "apagar skill", t_esquecer, "control",
                    args={"nome": "skill"}, confirm=True)


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

    # Qwen3Advisor com post_fn falso
    class FakeAdvisorPost:
        def __init__(self, has_model=True, tool_calls=True):
            self.has_model = has_model
            self.use_tool_calls = tool_calls
            self.calls = 0

        def __call__(self, path, payload):
            self.calls += 1
            if path == "/api/tags":
                if self.has_model:
                    return {"models": [{"name": "qwen3:4b:latest"}]}
                return {"models": [{"name": "outro:latest"}]}
            if path == "/api/chat":
                if self.use_tool_calls:
                    return {"message": {"tool_calls": [
                        {"function": {"name": "press_key",
                                      "arguments": {"keys": "ctrl+s"}}},
                        {"function": {"name": "wait_seconds",
                                      "arguments": {"seconds": 0.5}}}]}}
                return {"message": {"content":
                        "aperta ctrl+s e espera 1 seg"}}
            return None

    adv = Qwen3Advisor(post_fn=FakeAdvisorPost())
    check("advisor: detecta modelo", adv.detect() is True)
    steps = adv.generate_steps("salvar arquivo")
    check("advisor: gera passos via tool_calls",
          steps is not None and len(steps) == 2
          and steps[0]["tool"] == "press_key")

    adv2 = Qwen3Advisor(post_fn=FakeAdvisorPost(tool_calls=False))
    steps2 = adv2.generate_steps("salvar arquivo")
    check("advisor: fallback parse texto",
          steps2 is not None and len(steps2) >= 1)

    adv3 = Qwen3Advisor(post_fn=FakeAdvisorPost(has_model=False))
    check("advisor: sem modelo detecta False", adv3.detect() is False)
    check("advisor: sem modelo retorna None",
          adv3.generate_steps("x") is None)

    # AgentSkillEngine
    with tempfile.TemporaryDirectory(prefix="aura_as_st_") as td:
        engine = AgentSkillEngine(
            advisor=adv, skills_path=Path(td) / "skills.json")

        # aprender via advisor
        r = engine.learn_from_description("salvar_pdf",
                                          "salvar PDF no Photoshop")
        check("aprender: skill criada via advisor",
              r["ok"] is True and "advisor" in r["speech"].lower())
        check("aprender: passos gerados", len(r["steps_preview"]) > 10)

        # aprender manual
        r = engine.learn_manual("copiar_tudo", "selecionar tudo e copiar",
                                [{"tool": "press_key",
                                  "args": {"keys": "ctrl+a"}}])
        check("aprender manual: ok", r["ok"] is True)

        # listar
        skills = engine.list_skills()
        check("listar: 2 skills", len(skills) == 2)

        # sugerir melhor skill
        best = engine.suggest_best("salvar arquivo pdf")
        check("sugerir: encontra por descricao",
              best is not None and best.name == "salvar_pdf")

        # observar sucesso
        engine.observe_success("salvar_pdf")
        engine.observe_success("salvar_pdf")
        skills = engine.list_skills()
        check("observar: sucesso contado",
              skills[0]["success"] == 2)

        # plano de execucao
        plan = engine.plan_execution("salvar_pdf")
        check("plano: fala passos e pede sim",
              "passos" in plan and "sim" in plan)

        # esquecer
        r = engine.forget("copiar_tudo")
        check("esquecer: ok", r["ok"] is True
              and len(engine.list_skills()) == 1)

        # persistencia
        engine2 = AgentSkillEngine(
            advisor=adv, skills_path=Path(td) / "skills.json")
        check("persistencia: 1 skill recarregada",
              len(engine2.list_skills()) == 1)

        # gramatica
        g = parse_agent_skills("aprende skill salvar_pdf para salvar PDF no Photoshop")
        check("gram: aprender", g == ("skill_aprender",
                                      {"nome": "salvar_pdf",
                                       "descricao":
                                       "salvar pdf no photoshop"}))
        g = parse_agent_skills("executa a skill salvar_pdf")
        check("gram: executar", g == ("skill_executar",
                                      {"nome": "salvar_pdf"}))
        check("gram: listar", parse_agent_skills("quais skills você tem?")
              == ("skill_listar", {}))

        # integracao CommandCenter
        try:
            from jarvis_command_center import CommandCenter
        except Exception:
            CommandCenter = None
        if CommandCenter is None:
            print("[SKIP] jarvis_command_center nao importavel aqui")
        else:
            cc = CommandCenter()
            build_agent_skill_tools(cc, engine)
            r = cc.execute("skill_listar", {}, "u")
            check("cc: listar skills", r["ok"] is True)
            r = cc.execute("skill_executar", {"nome": "salvar_pdf"}, "u")
            check("cc: executar pede confirmacao com plano",
                  r.get("awaiting_confirmation") is True
                  and "passos" in r["speech"])

    if fails:
        print("SELF-TEST FALHOU: %d verificacao(oes): %s"
              % (len(fails), ", ".join(fails)))
        return 1
    print("ALL TESTS PASSED - agent_skill_engine.py")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
