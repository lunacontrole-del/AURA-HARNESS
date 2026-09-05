#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA Security Firewall v1.0
Rate limiting, sanitização de inputs, validação de tokens e firewall de agentes.
PAPER-TRADE ONLY — bloqueia qualquer tentativa de execução real.
"""
import os, sys, json, time, hashlib, re
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List
import threading

AURA_ROOT = Path(os.environ.get("AURA_ROOT", os.getcwd()))
LOGDIR = AURA_ROOT / "logs_supervisor"
LOGDIR.mkdir(exist_ok=True)

def _env_on(*names: str, default: str = "0") -> bool:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return str(default).strip().lower() in {"1", "true", "yes", "on"}


# Valores = estado real da flag (não "invariante satisfeita").
SECURITY_INVARIANTS = {
    "PAPER_TRADE": _env_on("AURA_PAPER_TRADE", "PAPER_TRADE", default="true"),
    "EXECUTION_ALLOWED": _env_on("AURA_EXECUTION_ALLOWED", "EXECUTION_ALLOWED", default="false"),
    "AURA_EXECUTION_ALLOWED": _env_on("AURA_EXECUTION_ALLOWED", default="0"),
    "AURA_UNLOCK_LIVE": _env_on("AURA_UNLOCK_LIVE", default="0"),
}


class RateLimiter:
    """Rate limiter por IP e por endpoint com janela deslizante."""

    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests = {}
        self._lock = threading.RLock()

    def is_allowed(self, key: str) -> bool:
        with self._lock:
            now = time.time()
            if key not in self._requests:
                self._requests[key] = []

            # Limpar entradas antigas
            self._requests[key] = [t for t in self._requests[key] if now - t < self.window]

            if len(self._requests[key]) >= self.max_requests:
                return False

            self._requests[key].append(now)
            return True

    def stats(self, key: str = None) -> dict:
        with self._lock:
            if key:
                return {"key": key, "count": len(self._requests.get(key, [])), "max": self.max_requests}
            return {"total_keys": len(self._requests), "window": self.window}


class InputSanitizer:
    """Sanitização de inputs para prevenir injection e XSS."""

    DANGEROUS_PATTERNS = [
        r"(<script.*?>|</script>)",
        r"(javascript:|data:text/html)",
        r"(eval\(|exec\(|system\(|os\.system)",
        r"(DROP\s+TABLE|DELETE\s+FROM|INSERT\s+INTO)\s+",
        r"(\.\./|\.\.\\|/etc/passwd|C:\\Windows)",
        r"(__import__|subprocess|pty\.spawn)",
    ]

    @classmethod
    def sanitize_string(cls, value: str, max_length: int = 4096) -> str:
        if not isinstance(value, str):
            return str(value)[:max_length]

        # Truncar
        value = value[:max_length]

        # Remover caracteres de controle exceto nova linha e tab
        value = "".join(c for c in value if c == "\n" or c == "\t" or ord(c) >= 32)

        # Verificar padrões perigosos
        for pattern in cls.DANGEROUS_PATTERNS:
            if re.search(pattern, value, re.IGNORECASE):
                raise ValueError(f"Input bloqueado por seguranca: padrao perigoso detectado")

        return value

    @classmethod
    def sanitize_dict(cls, data: dict, max_depth: int = 5, current_depth: int = 0) -> dict:
        if current_depth > max_depth:
            raise ValueError("Profundidade maxima de dict excedida")

        result = {}
        for k, v in data.items():
            safe_key = cls.sanitize_string(str(k), max_length=256)
            if isinstance(v, str):
                result[safe_key] = cls.sanitize_string(v)
            elif isinstance(v, dict):
                result[safe_key] = cls.sanitize_dict(v, max_depth, current_depth + 1)
            elif isinstance(v, list):
                result[safe_key] = cls.sanitize_list(v, max_depth, current_depth + 1)
            elif isinstance(v, (int, float, bool)):
                result[safe_key] = v
            else:
                result[safe_key] = cls.sanitize_string(str(v))
        return result

    @classmethod
    def sanitize_list(cls, data: list, max_depth: int = 5, current_depth: int = 0) -> list:
        if current_depth > max_depth:
            raise ValueError("Profundidade maxima de lista excedida")

        result = []
        for item in data:
            if isinstance(item, str):
                result.append(cls.sanitize_string(item))
            elif isinstance(item, dict):
                result.append(cls.sanitize_dict(item, max_depth, current_depth + 1))
            elif isinstance(item, list):
                result.append(cls.sanitize_list(item, max_depth, current_depth + 1))
            elif isinstance(item, (int, float, bool)):
                result.append(item)
            else:
                result.append(cls.sanitize_string(str(item)))
        return result


class AgentFirewall:
    """Firewall de agentes — valida ações antes de execução."""

    BLOCKED_ACTIONS = {
        "real_order", "live_trade", "execute_market", "place_bet",
        "transfer_funds", "withdraw", "delete_database", "drop_table",
        "shutdown_system", "kill_process", "restart_os"
    }

    ALLOWED_ONLY_IF_PAPER = {
        "simulate", "analyze", "predict", "advise", "recommend",
        "monitor", "capture", "diagnose", "report", "inspect"
    }

    def __init__(self):
        self._log = []
        self._lock = threading.RLock()

    def validate_action(self, agent_id: str, action: str, payload: dict) -> dict:
        """Valida uma ação de agente. Retorna {allowed, reason}."""
        action_lower = action.lower().strip()

        # 1. Verificar invariantes globais
        if not SECURITY_INVARIANTS["PAPER_TRADE"]:
            return {"allowed": False, "reason": "PAPER_TRADE desativado — sistema bloqueado"}

        if SECURITY_INVARIANTS["EXECUTION_ALLOWED"] or SECURITY_INVARIANTS["AURA_UNLOCK_LIVE"]:
            return {"allowed": False, "reason": "EXECUTION_ALLOWED ativado — violacao de seguranca"}

        # 2. Verificar ação na lista bloqueada
        if action_lower in self.BLOCKED_ACTIONS:
            return {"allowed": False, "reason": f"Acao '{action}' esta na lista bloqueada permanentemente"}

        # 3. Verificar se ação é apenas advisory
        if action_lower not in self.ALLOWED_ONLY_IF_PAPER:
            return {"allowed": False, "reason": f"Acao '{action}' nao esta na lista de acoes permitidas em paper-only"}

        # 4. Verificar payload
        try:
            InputSanitizer.sanitize_dict(payload)
        except ValueError as e:
            return {"allowed": False, "reason": f"Payload invalido: {e}"}

        # 5. Log e aprovação
        with self._lock:
            self._log.append({
                "timestamp": datetime.now().isoformat(),
                "agent": agent_id,
                "action": action,
                "allowed": True,
                "paper_trade": True
            })

        return {"allowed": True, "reason": "Acao validada em modo paper-only"}

    def stats(self) -> dict:
        with self._lock:
            total = len(self._log)
            blocked = sum(1 for e in self._log if not e.get("allowed", True))
            return {"total_validations": total, "blocked": blocked, "allowed": total - blocked}


class TokenValidator:
    """Validador de tokens com hash seguro e expiração."""

    def __init__(self, secret: str = None):
        import secrets as _secrets
        self.secret = secret or os.environ.get("AURA_BRIDGE_SECRET") or _secrets.token_hex(32)

    def hash_token(self, token: str) -> str:
        return hashlib.sha256(f"{token}:{self.secret}".encode()).hexdigest()

    def validate(self, token: str, expected_hash: str) -> bool:
        return self.hash_token(token) == expected_hash

    def generate(self, agent_id: str, expires_hours: int = 24) -> dict:
        import secrets
        token = secrets.token_urlsafe(32)
        expires = time.time() + (expires_hours * 3600)
        return {
            "token": token,
            "hash": self.hash_token(token),
            "agent_id": agent_id,
            "expires": expires,
            "expires_iso": datetime.fromtimestamp(expires).isoformat()
        }


def security_audit() -> dict:
    """Auditoria de segurança completa do ambiente."""
    audit = {
        "timestamp": datetime.now().isoformat(),
        "invariants": SECURITY_INVARIANTS,
        "invariants_ok": bool(
            SECURITY_INVARIANTS["PAPER_TRADE"]
            and not SECURITY_INVARIANTS["EXECUTION_ALLOWED"]
            and not SECURITY_INVARIANTS["AURA_UNLOCK_LIVE"]
        ),
        "environment": {
            "AURA_ROOT": str(AURA_ROOT),
            "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "nao definido"),
            "OLLAMA_NUM_GPU": os.environ.get("OLLAMA_NUM_GPU", "nao definido"),
        },
        "checks": {
            "db_exists": (AURA_ROOT / "engine" / "aura_quant_x.db").exists(),
            "venv_exists": (AURA_ROOT / "engine" / "venv").exists(),
            "desktop_ui_exists": (AURA_ROOT / "desktop" / "ui" / "matriz_v22" / "index.html").exists(),
        }
    }

    # Salvar relatório
    audit_path = LOGDIR / "security_audit.json"
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=2, ensure_ascii=False)

    return audit


if __name__ == "__main__":
    print("Security audit:", json.dumps(security_audit(), indent=2, ensure_ascii=False))
