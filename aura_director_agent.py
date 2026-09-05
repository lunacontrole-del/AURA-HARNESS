# engine/aura_director_agent.py
# AURA QUANT-X Director V2 — Propose-and-Approve + Causal Intelligence
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from typing import Any, Dict, List, Optional

import psutil

try:
    import pynvml
    pynvml.nvmlInit()
    GPU_AVAILABLE = True
except Exception:
    GPU_AVAILABLE = False


class GLMInterface:
    def generate(self, prompt: str) -> str:
        return json.dumps({
            "diagnostico_causal": (
                "A analise causal mostra concentracao de erros nos minutos 75-90. "
                "Traceback indica IndexError na leitura de pressao dual nesse intervalo."
            ),
            "acao_necessaria": True,
            "tipo_acao": "python_patch",
            "codigo_gerado": (
                "import sqlite3\n"
                "conn = sqlite3.connect('aura_quant_x.db')\n"
                "c = conn.cursor()\n"
                "c.execute(\"CREATE TABLE IF NOT EXISTS risk_manager "
                "(id INTEGER PRIMARY KEY, late_game_penalty REAL)\")\n"
                "c.execute(\"INSERT OR REPLACE INTO risk_manager (id, late_game_penalty) "
                "VALUES (1, 0.8)\")\n"
                "conn.commit()\n"
                "conn.close()\n"
                "print('Patch late_game_penalty aplicado.')\n"
            ),
            "justificativa": (
                "Penalidade no final de jogo reduz agressividade do Kelly e evita entradas "
                "em scams causados por IndexError nos minutos 75-90."
            ),
        }, ensure_ascii=False)


class AuraDirectorAgentV2:
    def __init__(
        self,
        db_path: str = "aura_quant_x.db",
        queue_path: str = "director_pending_actions.json",
    ):
        self.db_path = db_path
        self.queue_path = queue_path
        self.glm_interface = GLMInterface()
        self.log_path = "runtime_engine.log"
        self._init_director_db()

    def _init_director_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS director_memory ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL, "
            "problem_context TEXT, "
            "action_taken TEXT, "
            "outcome TEXT)"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS outcomes ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "trade_id INTEGER, match_id TEXT, result TEXT, "
            "profit_loss REAL, feedback_processed INTEGER, "
            "odds_velocity REAL, system_snapshot TEXT)"
        )
        conn.commit()
        conn.close()

    def _get_system_telemetry(self) -> Dict[str, Any]:
        telemetry: Dict[str, Any] = {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "ram_available_gb": round(psutil.virtual_memory().available / (1024 ** 3), 3),
            "ram_total_gb": round(psutil.virtual_memory().total / (1024 ** 3), 3),
            "disk_usage_percent": psutil.disk_usage("/").percent,
        }
        if GPU_AVAILABLE:
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                telemetry["vram_used_gb"] = round(mem_info.used / (1024 ** 3), 3)
                telemetry["vram_total_gb"] = round(mem_info.total / (1024 ** 3), 3)
                telemetry["gpu_util"] = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
            except Exception:
                telemetry["gpu"] = "read_failed"
        return telemetry

    def _get_system_performance_metrics(self) -> Dict[str, Any]:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            cur.execute("SELECT result FROM outcomes ORDER BY id DESC LIMIT 50")
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            rows = []
        conn.close()
        if not rows:
            return {"status": "no_data"}
        acertos = sum(1 for r in rows if r[0] == "Acertou")
        total = len(rows)
        return {
            "status": "active",
            "recent_accuracy": acertos / total if total else 0.0,
            "total_sampled": total,
        }

    def _analyze_failure_patterns(self) -> str:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        time_analysis: List[Any] = []
        velocity_analysis: List[Any] = []
        try:
            cur.execute(
                """
                SELECT
                    CASE
                        WHEN CAST(json_extract(system_snapshot, '$.match_minute') AS INTEGER) > 75 THEN '75-90'
                        WHEN CAST(json_extract(system_snapshot, '$.match_minute') AS INTEGER) > 45 THEN '45-75'
                        ELSE '0-45'
                    END AS time_bucket,
                    SUM(CASE WHEN result = 'Errou' THEN 1 ELSE 0 END) as erros,
                    COUNT(*) as total
                FROM outcomes
                WHERE result IS NOT NULL
                GROUP BY time_bucket
                """
            )
            time_analysis = cur.fetchall()
        except sqlite3.OperationalError:
            time_analysis = []
        try:
            cur.execute(
                """
                SELECT
                    CASE
                        WHEN odds_velocity > 1.5 THEN 'Alta (>1.5)'
                        WHEN odds_velocity < -5.0 THEN 'Dropping (<-5)'
                        ELSE 'Estavel'
                    END AS velocity_bucket,
                    SUM(CASE WHEN result = 'Errou' THEN 1 ELSE 0 END) as erros,
                    COUNT(*) as total
                FROM outcomes
                WHERE result IS NOT NULL AND odds_velocity IS NOT NULL
                GROUP BY velocity_bucket
                """
            )
            velocity_analysis = cur.fetchall()
        except sqlite3.OperationalError:
            velocity_analysis = []
        conn.close()
        return (
            f"Padroes de Falha por Tempo: {time_analysis}. "
            f"Padroes por Velocidade: {velocity_analysis}."
        )

    def _read_tracebacks(self) -> str:
        if not os.path.exists(self.log_path):
            return "Nenhum log encontrado."
        tracebacks: List[str] = []
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            for i, line in enumerate(lines):
                if "Traceback" in line:
                    trace_context = "".join(lines[i : i + 5])
                    tracebacks.append(trace_context)
        except OSError:
            return "Falha ao ler log."
        if not tracebacks:
            return "Nenhuma excecao grave recente."
        return f"Excecoes recentes nos logs: {tracebacks[-2:]}"

    def _check_director_memory(self, current_problem: str) -> str:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT action_taken, outcome FROM director_memory ORDER BY id DESC LIMIT 5"
        )
        past_actions = cur.fetchall()
        conn.close()
        return f"Acoes passadas do Diretor (Nao repita falhas): {past_actions}"

    def _log_director_action(self, problem: str, action: str, outcome: str) -> None:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO director_memory (timestamp, problem_context, action_taken, outcome) "
            "VALUES (?, ?, ?, ?)",
            (time.time(), problem, action, outcome),
        )
        conn.commit()
        conn.close()

    def run_thinking_cycle(self) -> Dict[str, Any]:
        print("[Director-V2] Iniciando ciclo de raciocinio causal profundo...")
        hw_telemetry = self._get_system_telemetry()
        perf = self._get_system_performance_metrics()
        failure_patterns = self._analyze_failure_patterns()
        tracebacks = self._read_tracebacks()
        memory = self._check_director_memory(failure_patterns)

        prompt = f"""
        Voce e o Aura Director V2, engenheiro de sistemas autonomo senior.
        1. TELEMETRIA: {json.dumps(hw_telemetry)}
        2. PERFORMANCE: {json.dumps(perf)}
        3. FALHA CAUSAL: {failure_patterns}
        4. TRACEBACKS: {tracebacks}
        5. MEMORIA: {memory}
        Responda SOMENTE JSON valido com diagnostico_causal, acao_necessaria,
        tipo_acao (python_patch|config_tweak|pip_install|none), codigo_gerado, justificativa.
        """
        glm_response = self.glm_interface.generate(prompt)
        try:
            analysis = json.loads(glm_response)
            self._process_v2_decision(analysis)
            return analysis
        except json.JSONDecodeError as e:
            self._log_director_action("JSON Parse Error", str(e), "FAILED")
            return {"error": "json_parse", "detail": str(e)}

    def _process_v2_decision(self, analysis: Dict[str, Any]) -> None:
        if not analysis.get("acao_necessaria") or analysis.get("tipo_acao") == "none":
            print(f"[Director-V2] Sistema estavel. Diagnostico: {analysis.get('diagnostico_causal')}")
            return
        print(f"[Director-V2] Diagnostico Causal: {analysis.get('diagnostico_causal')}")
        action_payload: Dict[str, Any] = {
            "timestamp": time.time(),
            "diagnostico": analysis.get("diagnostico_causal"),
            "tipo_acao": analysis.get("tipo_acao"),
            "justificativa": analysis.get("justificativa"),
            "status": "PENDING_ADMIN_APPROVAL",
        }
        if analysis.get("tipo_acao") == "python_patch" and analysis.get("codigo_gerado"):
            filename = f"patch_{int(time.time())}.py"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(str(analysis["codigo_gerado"]))
            action_payload["arquivo_para_executar"] = filename
            print(f"[Director-V2] Patch gerado: {filename}. Aguardando aprovacao.")
        elif analysis.get("tipo_acao") == "pip_install":
            action_payload["command"] = analysis.get("comando_exato") or analysis.get("codigo_gerado")
            print("[Director-V2] pip_install proposto. Aguardando aprovacao.")
        else:
            action_payload["detalhes_tweak"] = analysis.get("justificativa")
            print("[Director-V2] Ajuste de configuracao proposto. Aguardando aprovacao.")
        with open(self.queue_path, "w", encoding="utf-8") as f:
            json.dump(action_payload, f, indent=2, ensure_ascii=False)
        print(f"[Director-V2] ACAO BLOQUEADA em {self.queue_path} (Propose-and-Approve).")

    @staticmethod
    def execute_approved_action(queue_path: str = "director_pending_actions.json") -> Dict[str, Any]:
        if not os.path.exists(queue_path):
            return {"status": "no_pending"}
        with open(queue_path, "r", encoding="utf-8") as f:
            action = json.load(f)
        if action.get("status") != "PENDING_ADMIN_APPROVAL":
            return {"status": "not_pending", "current": action.get("status")}
        try:
            if action.get("tipo_acao") == "python_patch":
                script = action.get("arquivo_para_executar")
                if not script or not os.path.exists(script):
                    raise FileNotFoundError(f"patch ausente: {script}")
                result = subprocess.run(
                    ["python3", script],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                outcome = f"SUCCESS: {result.stdout[:500]}"
            elif action.get("tipo_acao") == "pip_install":
                pkg = str(action.get("command") or "").strip()
                if not pkg or any(c in pkg for c in [";", "|", "&", ">", "<", "`", "$"]):
                    raise ValueError("comando pip invalido ou inseguro")
                result = subprocess.run(
                    ["python3", "-m", "pip", "install", "--break-system-packages", pkg],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                outcome = f"SUCCESS_PIP: {result.stdout[:300]}"
            else:
                outcome = "MANUAL_TWEAK_REQUIRED"
            action["status"] = outcome
            with open(queue_path.replace(".json", "_done.json"), "w", encoding="utf-8") as f:
                json.dump(action, f, indent=2, ensure_ascii=False)
            os.remove(queue_path)
            director = AuraDirectorAgentV2()
            director._log_director_action(
                str(action.get("diagnostico")), str(action), "APPROVED_AND_RAN"
            )
            return {"status": "executed", "outcome": outcome}
        except Exception as e:
            err = str(e)
            action["status"] = f"EXECUTION_FAILED: {err}"
            with open(queue_path.replace(".json", "_failed.json"), "w", encoding="utf-8") as f:
                json.dump(action, f, indent=2, ensure_ascii=False)
            try:
                director = AuraDirectorAgentV2()
                director._log_director_action(
                    str(action.get("diagnostico")), str(action), f"FAILED: {err}"
                )
            except Exception:
                pass
            return {"status": "failed", "error": err}


# Alias for V1 compatibility
AuraDirectorAgent = AuraDirectorAgentV2


if __name__ == "__main__":
    director = AuraDirectorAgentV2()
    while True:
        director.run_thinking_cycle()
        print("[Director-V2] Hibernando por 600s...")
        time.sleep(600)


    def check_zombie_telemetry(self, match_minute: float = 0.0):
        try:
            from engine.infra.resilience.director_zombie_escalator import DirectorZombieEscalator
            return DirectorZombieEscalator(self.db_path).check_and_escalate(match_minute)
        except Exception as e:
            return {"escalated": False, "error": str(e)}
