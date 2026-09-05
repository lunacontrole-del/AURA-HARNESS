#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
domestic_operator.py — assistente DOMESTICO v2: sandbox de arquivos, abridor
de programas permitidos e pausa/retomada do AURA.

SUBSTITUI pc_operator.py — APAGUE o antigo:
    del engine\\agents\\pc_operator.py
e troque o hunk dele no jarvis_voice_server.py pelo hunk desta rodada.

FRONTEIRA (decisao do dono — rodada de revogacao):
    - Arquivos: SOMENTE em Documentos/AURA_Domestico. Fora disso, negado —
      nem traversal, nem symlink, nem caminho absoluto escapam (resolve +
      contencao). Sem busca no sistema, sem web, sem tocar no projeto AURA
      (unica excecao: LER weekly_report.md para entregar copia no sandbox).
    - Programas: SOMENTE entradas de engine/data/app_allowlist.json, editada
      MANUALMENTE pelo dono. O arquivo esta fora do sandbox — o assistente
      NAO consegue editar a propria allowlist. Nenhum caminho dito por voz
      vira processo (recusa explicita de .exe/caminho).
    - Mutacao de arquivo: PLANO falado + "sim" (regra mantida).
    - Abrir programa: direto com anuncio (o pedido por voz e a autorizacao).
      Entrada heavy:true pausa o AURA automaticamente ao abrir.

PAUSA — o que libera o que (honesto):
    - VRAM: unload do modelo no Ollama (keep_alive=0). E o ganho real.
    - CPU: camera de reconhecimento parada.
    - Hooks: captura/grade MC plugam depois (boot.py); sem hook, reporta.
    - Servidor de voz CONTINUA VIVO — precisa ouvir "retoma o aura".
      Whisper roda em CPU int8; TTS edge-tts e leve.

stdlib only. Python 3.9+. Windows. Console ASCII.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("aura.domestic")

__version__ = "2.0.0"

_PROJ_ROOT = Path(__file__).resolve().parents[2]


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# gramatica domestica (deterministica; o command_center delega pra ca)
# ---------------------------------------------------------------------------
def parse_domestic(utterance: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    t = _norm(utterance)
    if not t:
        return None
    if re.search(r"\b(?:pausa|pausar|suspende|suspender)\b.*"
                 r"\b(?:aura|sistema|tudo|analise)\b", t) and "camera" not in t:
        return ("pausar_aura", {})
    if re.search(r"\b(?:retoma|retomar|continua|continuar|despausa|despausar|"
                 r"volta|voltar)\b.*\b(?:aura|sistema|tudo|analise)\b", t):
        return ("retomar_aura", {})
    m = re.search(r"\b(?:abre|abrir|abra|roda|rodar|executa|executar|execute)\b"
                  r"\s+(?:os|as|um|uma|o|a)?\s*(.{2,})", t)
    if m:
        alvo = m.group(1).strip()
        if "analytics" in alvo or "relatorio" in alvo:
            return None  # tools proprias (rodar_analytics / entregar)
        m2 = re.match(r"^pasta\s+(.+)$", alvo)
        if m2:
            return ("domestico_listar", {"caminho": m2.group(1).strip()})
        return ("abrir_programa", {"programa": alvo})
    return None


# ---------------------------------------------------------------------------
# sandbox domestico
# ---------------------------------------------------------------------------
class DomesticSandbox:
    """Arquivos domesticos: TUDO confinado a um unico diretorio raiz."""

    MAX_READ_BYTES = 200_000
    MAX_WRITE_BYTES = 200_000
    MAX_LIST = 100
    MAX_EDIT_OCCURRENCES = 200

    def __init__(self, root: Optional[Any] = None,
                 trash_dir: Optional[Any] = None,
                 audit_path: Optional[Any] = None):
        self._lock = threading.RLock()
        self._root = (Path(root) if root is not None
                      else Path.home() / "Documents" / "AURA_Domestico"
                      ).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        for sub in ("notas", "listas", "relatorios"):
            (self._root / sub).mkdir(exist_ok=True)
        self._trash = (Path(trash_dir) if trash_dir is not None
                       else self._root / ".lixeira")
        self._trash.mkdir(exist_ok=True)
        self._audit = (Path(audit_path) if audit_path is not None
                       else self._root / ".audit.jsonl")
        self.counts = {"reads": 0, "mutations": 0, "denied": 0, "plans": 0}

    @property
    def root_path(self) -> Path:
        return self._root

    # ------------------------------------------------------------- guardas
    def _resolve(self, user_path: str) -> Path:
        raw = (user_path or "").strip().strip('"').strip("'")
        p = Path(raw) if raw else self._root
        if not p.is_absolute():
            p = self._root / p
        try:
            return p.resolve(strict=False)
        except (OSError, RuntimeError):
            return p

    def _contained(self, child: Path, root: Path) -> bool:
        try:
            child.relative_to(root)
            return True
        except ValueError:
            return False

    def _guard(self, rp: Path, write: bool) -> Optional[str]:
        if not self._contained(rp, self._root):
            self.counts["denied"] += 1
            return "fora da pasta domestica"
        if rp != self._trash and self._contained(rp, self._trash):
            return "dentro da lixeira domestica"
        return None

    def _audit_line(self, op: str, args: Dict[str, Any], ok: bool,
                    speech: str) -> None:
        try:
            with open(self._audit, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": _iso_now(), "op": op, "ok": ok,
                                     "args": {k: str(v)[:80]
                                              for k, v in (args or {}).items()},
                                     "speech": speech[:140]},
                                    ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("sandbox: audit falhou")

    def _backup(self, rp: Path) -> Optional[Path]:
        if not rp.exists():
            return None
        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = self._trash / ("%s__%s" % (ts, rp.name))
        i = 1
        while dest.exists():
            dest = self._trash / ("%s__%d__%s" % (ts, i, rp.name))
            i += 1
        try:
            shutil.copy2(rp, dest)
            return dest
        except OSError:
            logger.exception("sandbox: backup falhou")
            return None

    # ------------------------------------------------------------- leituras
    def _op_listar(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rp = self._resolve(str(args.get("caminho", "")))
        err = self._guard(rp, write=False)
        if err:
            return {"ok": False, "speech": "Negado: %s." % err}
        if not rp.is_dir():
            return {"ok": False, "speech": "%s nao e uma pasta." % rp.name}
        try:
            entries = [e for e in sorted(
                rp.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
                if not e.name.startswith(".")]
        except OSError as exc:
            return {"ok": False, "speech": "falha ao listar: %s" % exc}
        self.counts["reads"] += 1
        nomes = [e.name + ("/" if e.is_dir() else "")
                 for e in entries[:8]]
        return {"ok": True,
                "speech": "%d itens. %s." % (len(entries),
                                             ", ".join(nomes) or "vazia"),
                "entries": [{"name": e.name, "dir": e.is_dir()}
                            for e in entries[: self.MAX_LIST]]}

    def _op_ler(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rp = self._resolve(str(args.get("caminho", "")))
        err = self._guard(rp, write=False)
        if err:
            return {"ok": False, "speech": "Negado: %s." % err}
        if not rp.is_file():
            return {"ok": False, "speech": "%s nao e um arquivo." % rp.name}
        try:
            blob = rp.read_bytes()[: self.MAX_READ_BYTES]
        except OSError as exc:
            return {"ok": False, "speech": "falha ao ler: %s" % exc}
        if b"\0" in blob[:1024]:
            return {"ok": False, "speech": "arquivo binario — so leio texto"}
        self.counts["reads"] += 1
        text = blob.decode("utf-8", errors="replace")
        head = text[:300].replace("\n", " ")
        return {"ok": True, "speech": "%s: %s%s" % (
            rp.name, head, "..." if len(text) > 300 else "")}

    def _op_buscar(self, args: Dict[str, Any]) -> Dict[str, Any]:
        term = _norm(str(args.get("termo", "")))
        if len(term) < 2:
            return {"ok": False, "speech": "Diga o termo da busca."}
        results: List[str] = []
        t0 = time.monotonic()
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if time.monotonic() - t0 > 2.0 or len(results) >= 20:
                    break
                if term in _norm(fn) and not fn.startswith("."):
                    results.append(fn)
        self.counts["reads"] += 1
        if not results:
            return {"ok": True, "speech": "Nada com '%s' na pasta domestica."
                    % term, "results": []}
        return {"ok": True, "speech": "%d resultado(s): %s." % (
            len(results), ", ".join(results[:5])), "results": results}

    # ------------------------------------------------------------- mutacoes
    def _plan_escrever(self, args: Dict[str, Any]) -> str:
        rp = self._resolve(str(args.get("caminho", "")))
        err = self._guard(rp, write=True)
        if err:
            return "Negado: %s." % err
        content = str(args.get("conteudo", ""))
        if not content.strip():
            return "Conteudo vazio — nada a escrever."
        if len(content.encode("utf-8")) > self.MAX_WRITE_BYTES:
            return "Conteudo acima do limite de 200KB."
        if rp.exists():
            return ("Vou SOBRESCREVER %s com %d bytes (original vai para a "
                    "lixeira domestica). Diga sim para executar."
                    % (rp.name, len(content.encode("utf-8"))))
        if not rp.parent.exists():
            return "A pasta %s nao existe; crie antes." % rp.parent.name
        return ("Vou CRIAR %s com %d bytes. Diga sim para executar."
                % (rp.name, len(content.encode("utf-8"))))

    def _op_escrever(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rp = self._resolve(str(args.get("caminho", "")))
        err = self._guard(rp, write=True)
        if err:
            return {"ok": False, "speech": "Negado: %s." % err}
        content = str(args.get("conteudo", ""))
        if not content.strip():
            return {"ok": False, "speech": "conteudo vazio"}
        if len(content.encode("utf-8")) > self.MAX_WRITE_BYTES:
            return {"ok": False, "speech": "conteudo acima do limite"}
        if not rp.parent.exists():
            return {"ok": False, "speech": "pasta pai inexistente"}
        backup = self._backup(rp) if rp.exists() else None
        tmp = rp.with_suffix(rp.suffix + ".aura_tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, rp)
        except OSError as exc:
            return {"ok": False, "speech": "falha ao gravar: %s" % exc}
        self.counts["mutations"] += 1
        speech = "Salvo: %s%s." % (rp.name,
                                   " (sobrescrito)" if backup else "")
        self._audit_line("escrever", args, True, speech)
        return {"ok": True, "speech": speech}

    def _plan_editar(self, args: Dict[str, Any]) -> str:
        rp = self._resolve(str(args.get("caminho", "")))
        err = self._guard(rp, write=False)
        if err:
            return "Negado: %s." % err
        if not rp.is_file():
            return "%s nao encontrado." % rp.name
        find = str(args.get("procurar", ""))
        replace = str(args.get("substituir", ""))
        if not find:
            return "Falta o texto a procurar."
        try:
            text = rp.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return "falha ao ler: %s" % exc
        n = text.count(find)
        if n == 0:
            return "Nenhuma ocorrencia de '%s' em %s." % (find, rp.name)
        if n > self.MAX_EDIT_OCCURRENCES:
            return "%d ocorrencias — padrao largo demais, recuso." % n
        i = text.find(find)
        ctx = text[max(0, i - 40):i + len(find) + 40].replace("\n", " ")
        return ("Vou editar %s: %d substituicao(s) de '%s' por '%s'. "
                "Contexto: ...%s... Original vai para a lixeira domestica. "
                "Diga sim para executar." % (rp.name, n, find, replace, ctx))

    def _op_editar(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rp = self._resolve(str(args.get("caminho", "")))
        err = self._guard(rp, write=True)
        if err:
            return {"ok": False, "speech": "Negado: %s." % err}
        if not rp.is_file():
            return {"ok": False, "speech": "%s nao encontrado." % rp.name}
        find = str(args.get("procurar", ""))
        replace = str(args.get("substituir", ""))
        if not find:
            return {"ok": False, "speech": "falta o texto a procurar"}
        try:
            text = rp.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"ok": False, "speech": "falha ao ler: %s" % exc}
        n = text.count(find)
        if n == 0:
            return {"ok": False, "speech": "nada a substituir"}
        if n > self.MAX_EDIT_OCCURRENCES:
            return {"ok": False, "speech": "padrao largo demais; recusado"}
        new_text = text.replace(find, replace)
        if len(new_text.encode("utf-8")) > self.MAX_WRITE_BYTES:
            return {"ok": False, "speech": "resultado acima do limite"}
        backup = self._backup(rp)
        tmp = rp.with_suffix(rp.suffix + ".aura_tmp")
        try:
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, rp)
        except OSError as exc:
            return {"ok": False, "speech": "falha ao gravar: %s" % exc}
        self.counts["mutations"] += 1
        speech = "Editado: %d substituicao(s) em %s." % (n, rp.name)
        self._audit_line("editar", args, True, speech)
        return {"ok": True, "speech": speech, "ocorrencias": n}

    def _plan_mover(self, args: Dict[str, Any]) -> str:
        src = self._resolve(str(args.get("de", "")))
        dst = self._resolve(str(args.get("para", "")))
        e1 = self._guard(src, write=True)
        e2 = self._guard(dst, write=True)
        if e1 or e2:
            return "Negado: %s." % (e1 or e2)
        if not src.exists():
            return "%s nao existe." % src.name
        if dst.exists():
            return "destino %s ja existe." % dst.name
        return ("Vou mover %s para %s. Diga sim para executar."
                % (src.name, dst.name))

    def _op_mover(self, args: Dict[str, Any]) -> Dict[str, Any]:
        src = self._resolve(str(args.get("de", "")))
        dst = self._resolve(str(args.get("para", "")))
        e1 = self._guard(src, write=True)
        e2 = self._guard(dst, write=True)
        if e1 or e2:
            return {"ok": False, "speech": "Negado: %s." % (e1 or e2)}
        if not src.exists() or dst.exists():
            return {"ok": False, "speech": "origem/destino invalido"}
        try:
            shutil.move(str(src), str(dst))
        except (OSError, shutil.Error) as exc:
            return {"ok": False, "speech": "falha ao mover: %s" % exc}
        self.counts["mutations"] += 1
        self._audit_line("mover", args, True, "movido")
        return {"ok": True, "speech": "Movido para %s." % dst.name}

    def _plan_apagar(self, args: Dict[str, Any]) -> str:
        rp = self._resolve(str(args.get("caminho", "")))
        err = self._guard(rp, write=True)
        if err:
            return "Negado: %s." % err
        if not rp.exists():
            return "%s nao existe." % rp.name
        if rp.is_dir():
            return "Apagar pastas nao e suportado (mova os arquivos)."
        return ("Vou apagar %s — na pratica, mover para a lixeira domestica, "
                "de onde da para recuperar. Diga sim para executar." % rp.name)

    def _op_apagar(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rp = self._resolve(str(args.get("caminho", "")))
        err = self._guard(rp, write=True)
        if err:
            return {"ok": False, "speech": "Negado: %s." % err}
        if not rp.exists() or rp.is_dir():
            return {"ok": False, "speech": "so apago arquivos existentes"}
        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = self._trash / ("%s__%s" % (ts, rp.name))
        i = 1
        while dest.exists():
            dest = self._trash / ("%s__%d__%s" % (ts, i, rp.name))
            i += 1
        try:
            shutil.move(str(rp), str(dest))
        except OSError as exc:
            return {"ok": False, "speech": "falha ao apagar: %s" % exc}
        self.counts["mutations"] += 1
        self._audit_line("apagar", args, True, "para lixeira domestica")
        return {"ok": True, "speech": "Apagado (lixeira domestica): %s."
                % rp.name, "lixeira": str(dest)}

    def _plan_criar_pasta(self, args: Dict[str, Any]) -> str:
        rp = self._resolve(str(args.get("caminho", "")))
        err = self._guard(rp, write=True)
        if err:
            return "Negado: %s." % err
        if rp.exists():
            return "%s ja existe." % rp.name
        return "Vou criar a pasta %s. Diga sim para executar." % rp.name

    def _op_criar_pasta(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rp = self._resolve(str(args.get("caminho", "")))
        err = self._guard(rp, write=True)
        if err:
            return {"ok": False, "speech": "Negado: %s." % err}
        if rp.exists():
            return {"ok": False, "speech": "%s ja existe." % rp.name}
        try:
            rp.mkdir(parents=False)
        except OSError as exc:
            return {"ok": False, "speech": "falha ao criar: %s" % exc}
        self.counts["mutations"] += 1
        self._audit_line("criar_pasta", args, True, "pasta criada")
        return {"ok": True, "speech": "Pasta criada: %s." % rp.name}

    def _plan_lixeira_esvaziar(self, args: Dict[str, Any]) -> str:
        n = sum(1 for _ in self._trash.iterdir())
        if n == 0:
            return "Lixeira domestica ja esta vazia."
        return ("Vou apagar DEFINITIVAMENTE %d item(s) da lixeira domestica "
                "— isso e irreversivel. Diga sim para executar." % n)

    def _op_lixeira_esvaziar(self, args: Dict[str, Any]) -> Dict[str, Any]:
        n = 0
        for f in list(self._trash.iterdir()):
            try:
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
                n += 1
            except OSError:
                logger.warning("lixeira: falha ao remover %s", f)
        self.counts["mutations"] += 1
        self._audit_line("lixeira_esvaziar", args, True, "%d itens" % n)
        return {"ok": True, "speech": "Lixeira domestica esvaziada: %d itens."
                % n}

    # ------------------------------------------------------------- dispatch
    def plan(self, op: str, args: Optional[Dict[str, Any]] = None) -> str:
        self.counts["plans"] += 1
        fn = getattr(self, "_plan_" + op, None)
        if fn is None:
            return "Confirmar %s?" % op
        try:
            return fn(args or {})
        except Exception:
            logger.exception("sandbox: plan falhou (%s)", op)
            return "Nao consegui preparar o plano de %s." % op

    def do(self, op: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fn = getattr(self, "_op_" + op, None)
        if fn is None:
            return {"ok": False, "speech": "operacao desconhecida: %s" % op}
        try:
            return fn(args or {})
        except Exception as exc:
            logger.exception("sandbox: do falhou (%s)", op)
            self._audit_line(op, args or {}, False, "erro: %s" % exc)
            return {"ok": False, "speech": "falha interna em %s" % op}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            trash_n = 0
            try:
                trash_n = sum(1 for _ in self._trash.iterdir())
            except OSError:
                pass
            return {"domestic_sandbox": {
                "root": str(self._root), **self.counts,
                "trash_items": trash_n}}


# ---------------------------------------------------------------------------
# abridor de programas (allowlist editavel SOMENTE pelo dono)
# ---------------------------------------------------------------------------
DEFAULT_ALLOWLIST: List[Dict[str, Any]] = [
    {"name": "bloco de notas", "aliases": ["notepad", "editor de texto"],
     "cmd": ["notepad.exe"], "heavy": False},
    {"name": "calculadora", "aliases": ["calc"], "cmd": ["calc.exe"],
     "heavy": False},
    {"name": "paint", "aliases": ["mspaint", "desenho"],
     "cmd": ["mspaint.exe"], "heavy": False},
    {"name": "explorador de arquivos",
     "aliases": ["explorador", "meu computador"], "cmd": ["explorer.exe"],
     "heavy": False},
    {"name": "navegador", "aliases": ["browser", "internet", "chrome", "edge"],
     "url": "https://www.google.com", "heavy": False},
    {"name": "sokkerpro", "aliases": ["sokker"], "url": "https://sokkerpro.com",
     "heavy": False},
    {"name": "gerenciador de tarefas", "aliases": ["task manager", "tarefas"],
     "cmd": ["taskmgr.exe"], "heavy": False},
]


class ProgramLauncher:
    """Abre SOMENTE o que consta na allowlist. Caminho/.exe dito por voz =
    recusa. A allowlist fica fora do sandbox: o assistente nao a edita."""

    def __init__(self, allowlist_path: Optional[Any] = None,
                 exec_fn: Optional[Callable[[dict], None]] = None):
        self._path = (Path(allowlist_path) if allowlist_path is not None
                      else _PROJ_ROOT / "engine" / "data" / "app_allowlist.json")
        self._exec = exec_fn if exec_fn is not None else self._exec_default
        self._entries = self._load()
        self.launched = 0
        self.refused = 0

    def _load(self) -> List[dict]:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [e for e in data if isinstance(e, dict)
                            and e.get("name")]
        except Exception:
            logger.exception("allowlist ilegivel — usando defaults")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(DEFAULT_ALLOWLIST, indent=1, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            logger.warning("nao consegui gravar allowlist default")
        return [dict(e) for e in DEFAULT_ALLOWLIST]

    def find(self, query: str) -> Optional[dict]:
        q = _norm(query)
        if len(q) < 2 or "/" in q or "\\" in q or q.endswith(".exe"):
            return None
        for e in self._entries:
            names = [_norm(e["name"])] + \
                [_norm(a) for a in e.get("aliases", [])]
            for n in names:
                if q == n or (len(q) >= 4 and len(n) >= 3
                              and (q in n or n in q)):
                    return e
        return None

    @staticmethod
    def _exec_default(entry: dict) -> None:
        if entry.get("url"):
            if os.name == "nt":
                os.startfile(entry["url"])  # type: ignore[attr-defined]
            else:
                import webbrowser
                webbrowser.open(entry["url"])
            return
        subprocess.Popen(entry["cmd"])

    def launch(self, query: str) -> Dict[str, Any]:
        entry = self.find(query)
        if entry is None:
            self.refused += 1
            return {"ok": False,
                    "speech": ("Programa '%s' nao esta na lista permitida. "
                               "Para liberar, edite o arquivo "
                               "engine/data/app_allowlist.json." % query)}
        try:
            self._exec(entry)
        except Exception as exc:
            return {"ok": False,
                    "speech": "Falha ao abrir %s: %s" % (entry["name"], exc)}
        self.launched += 1
        return {"ok": True, "speech": "Abri %s." % entry["name"],
                "programa": entry["name"],
                "heavy": bool(entry.get("heavy"))}

    def stats(self) -> Dict[str, Any]:
        return {"program_launcher": {
            "entries": len(self._entries), "launched": self.launched,
            "refused": self.refused,
            "allowlist": str(self._path)}}


# ---------------------------------------------------------------------------
# pausa/retomada do AURA (recursos)
# ---------------------------------------------------------------------------
class AuraPause:
    """Pausa o que pesa: modelo na VRAM (Ollama), camera, hooks plugaveis.
    O servidor de voz continua vivo para ouvir 'retoma o aura'."""

    def __init__(self, camera: Optional[Any] = None,
                 hooks: Optional[Dict[str, Dict[str, Any]]] = None,
                 ollama_url: str = "http://127.0.0.1:11434",
                 flag_path: Optional[Any] = None,
                 unload_fn: Optional[Callable[[], List[str]]] = None):
        self._lock = threading.RLock()
        self._camera = camera
        self._hooks = dict(hooks or {})
        self._ollama = ollama_url.rstrip("/")
        self._flag = (Path(flag_path) if flag_path is not None
                      else _PROJ_ROOT / "engine" / "data" / "aura_paused.json")
        self._unload_fn = unload_fn if unload_fn is not None \
            else self._http_unload_models
        self._paused = False
        self.counts = {"pauses": 0, "resumes": 0, "models_unloaded": 0,
                       "hook_failures": 0}

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _http_unload_models(self) -> List[str]:
        try:
            with urllib.request.urlopen(self._ollama + "/api/ps",
                                        timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8",
                                                     errors="replace"))
        except Exception:
            return []
        names = [str(m.get("name")) for m in (data.get("models") or [])
                 if m.get("name")]
        for name in names:
            try:
                req = urllib.request.Request(
                    self._ollama + "/api/generate",
                    data=json.dumps({"model": name,
                                     "keep_alive": 0}).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                logger.warning("unload falhou para %s", name)
        return names

    def pause(self) -> Dict[str, Any]:
        with self._lock:
            if self._paused:
                return {"ok": True, "speech": "AURA ja esta pausado."}
            feitos: List[str] = []
            if self._camera is not None:
                try:
                    self._camera.stop()
                    feitos.append("camera parada")
                except Exception:
                    feitos.append("camera falhou ao parar")
            models: List[str] = []
            try:
                models = self._unload_fn() or []
            except Exception:
                logger.exception("unload de modelos falhou")
            if models:
                feitos.append("modelo(s) %s liberado(s) da VRAM"
                              % ", ".join(models[:3]))
            for key, h in self._hooks.items():
                try:
                    h["pause"]()
                    feitos.append("%s pausado" % h.get("label", key))
                except Exception:
                    self.counts["hook_failures"] += 1
            try:
                self._flag.write_text(json.dumps({"ts": _iso_now()}),
                                      encoding="utf-8")
            except OSError:
                pass
            self._paused = True
            self.counts["pauses"] += 1
            self.counts["models_unloaded"] += len(models)
            speech = ("AURA pausado: %s. A voz continua de plantao — diga "
                      "'retoma o aura' quando terminar."
                      % ("; ".join(feitos) if feitos
                         else "nada pesado estava rodando"))
            return {"ok": True, "speech": speech, "feitos": feitos}

    def resume(self) -> Dict[str, Any]:
        with self._lock:
            if not self._paused:
                return {"ok": True, "speech": "AURA nao esta pausado."}
            feitos: List[str] = []
            if self._camera is not None:
                try:
                    self._camera.start()
                    feitos.append("camera ligada")
                except Exception:
                    feitos.append("camera falhou ao ligar")
            for key, h in self._hooks.items():
                try:
                    h["resume"]()
                    feitos.append("%s retomado" % h.get("label", key))
                except Exception:
                    self.counts["hook_failures"] += 1
            try:
                self._flag.unlink()
            except OSError:
                pass
            self._paused = False
            self.counts["resumes"] += 1
            return {"ok": True,
                    "speech": "AURA retomado: %s. O modelo recarrega "
                              "sozinho na primeira pergunta."
                              % ("; ".join(feitos) or "ok")}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"aura_pause": {"paused": self._paused, **self.counts}}


# ---------------------------------------------------------------------------
# registro no CommandCenter
# ---------------------------------------------------------------------------
def build_domestic_tools(cc, sandbox: DomesticSandbox,
                         launcher: ProgramLauncher, pause: AuraPause,
                         report_src: Optional[Any] = None) -> None:
    _supports_csf = "confirm_speech_fn" in inspect.signature(
        cc.register).parameters

    def reg(op: str, name: str, desc: str, confirm: bool,
            args: Optional[Dict[str, str]] = None) -> None:
        handler = lambda a, s, _op=op: sandbox.do(_op, a)
        csf = (lambda a, _op=op: sandbox.plan(_op, a)) if confirm else None
        if _supports_csf:
            cc.register(name, desc, handler,
                        "control" if confirm else "read", args=args,
                        confirm=confirm, confirm_speech_fn=csf)
        else:
            cc.register(name, desc, handler,
                        "control" if confirm else "read", args=args,
                        confirm=confirm)

    reg("listar", "domestico_listar",
        "listar arquivos da pasta domestica", False, {"caminho": "subpasta"})
    reg("ler", "domestico_ler", "ler arquivo domestico", False,
        {"caminho": "arquivo"})
    reg("buscar", "domestico_buscar",
        "buscar arquivo por nome na pasta domestica", False,
        {"termo": "nome"})
    reg("escrever", "domestico_escrever",
        "criar/sobrescrever arquivo domestico", True,
        {"caminho": "arquivo", "conteudo": "texto"})
    reg("editar", "domestico_editar",
        "editar arquivo domestico (substituicao de texto)", True,
        {"caminho": "arquivo", "procurar": "texto",
         "substituir": "novo texto"})
    reg("mover", "domestico_mover",
        "mover/renomear dentro da pasta domestica", True,
        {"de": "origem", "para": "destino"})
    reg("apagar", "domestico_apagar",
        "apagar arquivo domestico (lixeira domestica)", True,
        {"caminho": "arquivo"})
    reg("criar_pasta", "domestico_criar_pasta",
        "criar subpasta domestica", True, {"caminho": "pasta"})
    reg("lixeira_esvaziar", "domestico_lixeira_esvaziar",
        "apagar definitivamente a lixeira domestica", True)

    def t_abrir(args: Dict[str, Any], session: str) -> Dict[str, Any]:
        r = launcher.launch(str(args.get("programa", "")))
        if r.get("ok") and r.get("heavy") and not pause.is_paused:
            pr = pause.pause()
            r["speech"] = r["speech"] + " " + pr.get("speech", "")
        return r

    cc.register("abrir_programa",
                "abrir programa da lista permitida (app_allowlist.json)",
                t_abrir, "control", args={"programa": "nome"},
                confirm=False)
    cc.register("pausar_aura",
                "pausar o AURA (modelo na VRAM + camera) para liberar recursos",
                lambda a, s: pause.pause(), "control", confirm=False)
    cc.register("retomar_aura", "retomar o AURA pausado",
                lambda a, s: pause.resume(), "control", confirm=False)

    src = (Path(report_src) if report_src is not None
           else _PROJ_ROOT / "engine" / "data" / "weekly_report.md")

    def t_rel_plan(args: Dict[str, Any]) -> str:
        if not src.is_file():
            return ("Ainda nao existe relatorio gerado. Diga 'rodar o "
                    "analytics' primeiro e depois me peca de novo.")
        return ("Vou copiar o relatorio semanal para a pasta domestica, em "
                "relatorios, com a data de hoje. Diga sim para executar.")

    def t_rel(args: Dict[str, Any], session: str) -> Dict[str, Any]:
        if not src.is_file():
            return {"ok": False, "speech": "relatorio inexistente"}
        dest = sandbox.root_path / "relatorios" / \
            ("weekly_%s.md" % time.strftime("%Y%m%d"))
        try:
            shutil.copy2(src, dest)
        except OSError as exc:
            return {"ok": False, "speech": "falha ao copiar: %s" % exc}
        return {"ok": True,
                "speech": "Relatorio entregue em relatorios/%s." % dest.name}

    if _supports_csf:
        cc.register("entregar_relatorio",
                    "copiar o relatorio semanal do AURA para a pasta domestica",
                    t_rel, "control", confirm=True, confirm_speech_fn=t_rel_plan)
    else:
        cc.register("entregar_relatorio",
                    "copiar o relatorio semanal do AURA para a pasta domestica",
                    t_rel, "control", confirm=True)


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

    # --- gramatica ---
    r = parse_domestic("abre o bloco de notas")
    check("gram: abre programa", r == ("abrir_programa",
                                       {"programa": "bloco de notas"}))
    r = parse_domestic("quero abrir a calculadora")
    check("gram: artigo consumido", r is not None
          and r[1].get("programa") == "calculadora")
    r = parse_domestic("abre a pasta notas")
    check("gram: pasta -> listar", r == ("domestico_listar",
                                         {"caminho": "notas"}))
    check("gram: pausa o aura",
          parse_domestic("pausa o aura") == ("pausar_aura", {}))
    check("gram: retoma o sistema",
          parse_domestic("retoma o sistema") == ("retomar_aura", {}))
    check("gram: pausa a camera NAO e pausa_aura",
          parse_domestic("pausa a camera") is None)
    check("gram: rodar analytics NAO e abrir_programa",
          parse_domestic("rodar o analytics") is None)
    check("gram: conversa comum", parse_domestic("e o jogo?") is None)

    with tempfile.TemporaryDirectory(prefix="aura_dom_st_") as td:
        home = Path(td)
        sb = DomesticSandbox(root=home / "dom")
        root = sb.root_path

        # sandbox: contencao
        r = sb.do("ler", {"caminho": str(home / "segredo.txt")})
        check("sandbox: fora negado", r["ok"] is False)
        r = sb.do("ler", {"caminho": "../fora.txt"})
        check("sandbox: traversal negado", r["ok"] is False)

        # escrever: plano + confirm + backup
        plan = sb.plan("escrever", {"caminho": "notas/compras.txt",
                                     "conteudo": "leite e cafe"})
        check("escrever: plano pede sim", "Diga sim" in plan)
        r = sb.do("escrever", {"caminho": "notas/compras.txt",
                               "conteudo": "leite e cafe"})
        check("escrever: cria arquivo", r["ok"] is True
              and (root / "notas" / "compras.txt").is_file())
        r = sb.do("escrever", {"caminho": "notas/compras.txt",
                               "conteudo": "leite, cafe e pao"})
        check("escrever: sobrescreve", r["ok"] is True
              and "leite, cafe e pao" in
              (root / "notas" / "compras.txt").read_text(encoding="utf-8"))

        # editar
        plan = sb.plan("editar", {"caminho": "notas/compras.txt",
                                  "procurar": "leite",
                                  "substituir": "leite desnatado"})
        check("editar: plano conta ocorrencia",
              "1 substituicao" in plan and "Diga sim" in plan)
        r = sb.do("editar", {"caminho": "notas/compras.txt",
                             "procurar": "leite",
                             "substituir": "leite desnatado"})
        check("editar: executa", r["ok"] is True
              and r["ocorrencias"] == 1)
        plan0 = sb.plan("editar", {"caminho": "notas/compras.txt",
                                   "procurar": "zzz", "substituir": "x"})
        check("editar: zero ocorrencias avisado",
              "Nenhuma ocorrencia" in plan0)

        # apagar -> lixeira recuperavel; pasta recusada
        r = sb.do("apagar", {"caminho": "notas/compras.txt"})
        check("apagar: recuperavel", r["ok"] is True
              and not (root / "notas" / "compras.txt").exists()
              and Path(r["lixeira"]).is_file())
        r = sb.do("apagar", {"caminho": "notas"})
        check("apagar: pasta recusada", r["ok"] is False)

        # buscar / listar
        sb.do("escrever", {"caminho": "listas/presentes.txt",
                           "conteudo": "anel"})
        r = sb.do("buscar", {"termo": "presentes"})
        check("buscar: acha no sandbox", r["ok"] is True
              and "presentes.txt" in r["results"])
        r = sb.do("listar", {"caminho": ""})
        check("listar: ok", r["ok"] is True)

        # audit gravado
        check("audit: linhas de mutacao",
              (root / ".audit.jsonl").is_file())

        # --- launcher ---
        opened: List[str] = []
        allow = home / "allowlist.json"
        allow.write_text(json.dumps(DEFAULT_ALLOWLIST + [
            {"name": "jogo pesado", "aliases": ["jogo"],
             "cmd": ["notepad.exe"], "heavy": True}]), encoding="utf-8")
        la = ProgramLauncher(allowlist_path=allow,
                             exec_fn=lambda e: opened.append(e["name"]))
        r = la.launch("notepad")
        check("launcher: alias abre", r["ok"] is True
              and opened == ["bloco de notas"])
        r = la.launch("calculadora")
        check("launcher: nome abre", r["ok"] is True)
        r = la.launch("jogo")
        check("launcher: heavy sinalizado", r["ok"] is True
              and r.get("heavy") is True)
        r = la.launch("C:\\Windows\\System32\\cmd.exe")
        check("launcher: caminho cru RECUSADO", r["ok"] is False)
        r = la.launch("programa inexistente")
        check("launcher: desconhecido recusado com dica",
              r["ok"] is False and "app_allowlist" in r["speech"])

        # --- AuraPause ---
        cam_calls: List[str] = []

        class FakeCamera:
            def stop(self):
                cam_calls.append("stop")

            def start(self):
                cam_calls.append("start")
                return True

        flag = home / "paused.json"
        pa = AuraPause(camera=FakeCamera(),
                       flag_path=flag,
                       unload_fn=lambda: ["llama3.2:3b"])
        r = pa.pause()
        check("pause: fala menciona VRAM e plantao",
              "VRAM" in r["speech"] and "retoma" in r["speech"])
        check("pause: camera parada e flag criada",
              cam_calls == ["stop"] and flag.is_file())
        check("pause: idempotente",
              pa.pause()["speech"] == "AURA ja esta pausado.")
        r = pa.resume()
        check("resume: flag removida e camera ligada",
              not flag.exists() and cam_calls == ["stop", "start"]
              and "recarrega" in r["speech"])
        pa2 = AuraPause(camera=None, flag_path=home / "f2.json",
                        unload_fn=lambda: [])
        r = pa2.pause()
        check("pause sem nada pesado: honesto",
              "nada pesado" in r["speech"])

        # --- integracao com CommandCenter ---
        try:
            from jarvis_command_center import CommandCenter
        except Exception:
            CommandCenter = None  # type: ignore
        if CommandCenter is None:
            print("[SKIP] jarvis_command_center nao importavel aqui")
        else:
            cc = CommandCenter()
            report = home / "weekly_report.md"
            report.write_text("# relatorio\n", encoding="utf-8")
            build_domestic_tools(cc, sb, la, pa2, report_src=report)

            r = cc.execute("domestico_escrever",
                           {"caminho": "notas/tarefa.txt",
                            "conteudo": "ligar pro tecnico"}, "u1")
            check("cc: mutacao pede confirmacao",
                  r.get("awaiting_confirmation") is True
                  and "Diga sim" in r["speech"])
            r2 = cc.handle_utterance("sim", "u1")
            check("cc: sim executa", r2 is not None
                  and r2.get("tool") == "domestico_escrever"
                  and (root / "notas" / "tarefa.txt").is_file())

            opened.clear()
            pa3 = AuraPause(camera=None, flag_path=home / "f3.json",
                            unload_fn=lambda: ["m"])
            build_domestic_tools(CommandCenter(), sb, la, pa3,
                                 report_src=report)
            cc3 = CommandCenter()
            build_domestic_tools(cc3, sb, la, pa3, report_src=report)
            r = cc3.execute("abrir_programa", {"programa": "jogo"}, "u2")
            check("cc: heavy pede confirmacao",
                  r.get("awaiting_confirmation") is True)
            r = cc3.handle_utterance("sim", "u2")
            check("cc: heavy abre E pausa numa fala",
                  r is not None and r.get("ok") is True
                  and "AURA pausado" in r.get("speech", "")
                  and pa3.is_paused)

            r = cc3.execute("retomar_aura", {}, "u2", confirmed=True)
            check("cc: retomar limpa pausa",
                  r["ok"] is True and not pa3.is_paused)

            r = cc3.execute("entregar_relatorio", {}, "u3")
            check("cc: relatorio pede confirmacao",
                  r.get("awaiting_confirmation") is True)
            cc3.handle_utterance("sim", "u3")
            check("cc: relatorio entregue no sandbox",
                  any("weekly_" in f.name for f in
                      (root / "relatorios").iterdir()))

        st = sb.stats()["domestic_sandbox"]
        check("stats: mutacoes e negacoes contadas",
              st["mutations"] >= 4 and st["denied"] >= 2)

    if fails:
        print("SELF-TEST FALHOU: %d verificacao(oes): %s"
              % (len(fails), ", ".join(fails)))
        return 1
    print("ALL TESTS PASSED - domestic_operator.py")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
