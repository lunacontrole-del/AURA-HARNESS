# aura_quant_x_master.py — Unified Master Engine (FastAPI + PyTorch + Quant Pipeline)
import asyncio
import json
import sqlite3
import time
from typing import Dict, Any

import numpy as np
import torch
import torch.nn as nn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from aura_context_crawler import context_crawler
from aura_auto_evolver import evolver, device

# --- Arquitetura de Deep Learning ---
class DualMarketTransformerLSTM(nn.Module):
    def __init__(self, input_dim=8, hidden_dim=64, num_layers=2):
        super().__init__()
        self.lstm = nn.LSTM(input_dim, hidden_dim, batch_first=True, num_layers=num_layers)
        self.attention = nn.Linear(hidden_dim, 1)
        self.fc_corners = nn.Sequential(
            nn.Linear(hidden_dim, 32), nn.ReLU(), nn.Linear(32, 1), nn.Sigmoid()
        )
        self.fc_goals = nn.Sequential(
            nn.Linear(hidden_dim, 32), nn.ReLU(), nn.Linear(32, 1), nn.Sigmoid()
        )

    def forward(self, x):
        out, _ = self.lstm(x)
        weights = torch.softmax(self.attention(out), dim=1)
        context = torch.sum(weights * out, dim=1)
        return self.fc_corners(context), self.fc_goals(context)


app = FastAPI(title="AURA QUANT-X Master Engine", version="3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^chrome-extension://[a-z]{32}$|^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-AURA-Token"],
)

match_buffers: Dict[str, Dict[str, Any]] = {}


def init_db():
    conn = sqlite3.connect("aura_quant_x.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs_telemetria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            payload_json TEXT,
            signal TEXT,
            corner_prob REAL,
            goal_prob REAL
        )
    """)
    conn.commit()
    conn.close()


model = DualMarketTransformerLSTM().to(device)
try:
    model.load_state_dict(torch.load("model_weights.pt", map_location=device))
    model.eval()
    print(f"✅ Model weights carregados em {device}.")
except Exception:
    print("⚠️ Usando inicialização padrão de pesos (rode pre_treinar_gpu.py).")

print(f"💎 [AURA QUANT-X] Dispositivo: {device}")


@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(evolver.run_idle_training_loop(DualMarketTransformerLSTM))
    print("🤖 Auto-Evolver iniciado em background")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "device": str(device),
        "version": "3.0",
        "cuda": torch.cuda.is_available(),
    }


@app.websocket("/ws/telemetria")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            evolver.notify_activity()

            teams = payload.get("teams", {})
            home = teams.get("home", "Home")
            away = teams.get("away", "Away")
            fid = payload.get("fixtureId") or f"{home}-{away}"
            stats = payload.get("stats", {})

            # Contexto pré-jogo (cache interno do crawler)
            ctx = await context_crawler.extrair_contexto(home, away)
            fator_clima = float(ctx.get("clima", {}).get("fator_clima", 1.0))
            fator_pressao = float(ctx.get("fator_pressao", 1.0))
            peso_casa = float(ctx.get("noticias", {}).get("peso_elenco_casa", 1.0))
            peso_fora = float(ctx.get("noticias", {}).get("peso_elenco_fora", 1.0))

            da_h = float(stats.get("dangerous", {}).get("home", 0))
            da_a = float(stats.get("dangerous", {}).get("away", 0))
            xg_h = float(stats.get("xg", {}).get("home", 0)) * peso_casa * fator_clima
            xg_a = float(stats.get("xg", {}).get("away", 0)) * peso_fora * fator_clima
            goals_h = float(stats.get("goals", {}).get("home", 0))
            goals_a = float(stats.get("goals", {}).get("away", 0))
            corners_h = float(stats.get("corners", {}).get("home", 0))
            corners_a = float(stats.get("corners", {}).get("away", 0))

            if fid not in match_buffers:
                match_buffers[fid] = {"history": []}
            history = match_buffers[fid]["history"]
            history.append([da_h, da_a, xg_h, xg_a, goals_h, goals_a, corners_h, corners_a])
            if len(history) > 15:
                history.pop(0)

            # Inferência neural
            if len(history) >= 3:
                tensor = torch.tensor([history], dtype=torch.float32).to(device)
                with torch.no_grad():
                    p_corner, p_goal = model(tensor)
                corner_prob = float(p_corner.cpu().numpy()[0][0])
                goal_prob = float(p_goal.cpu().numpy()[0][0])
            else:
                # Fallback até acumular frames
                input_tensor = torch.randn(1, 15, 8).to(device)
                with torch.no_grad():
                    p_corner, p_goal = model(input_tensor)
                corner_prob = float(p_corner.item())
                goal_prob = float(p_goal.item())

            # Ajustes contextuais
            corner_prob = float(np.clip(corner_prob * fator_clima * (1.0 + 0.1 * (fator_pressao - 1.0)), 0.0, 1.0))
            goal_prob = float(np.clip(goal_prob * fator_clima * ((peso_casa + peso_fora) / 2.0), 0.0, 1.0))

            # Kelly + EVM (odds padrão 1.85)
            odds = 1.85
            signal = "HOLD"
            kelly_stake = 0.0
            if corner_prob > 0.72:
                signal = "BUY_CORNER"
                kelly_stake = max(0.0, min((corner_prob * odds - 1) / (odds - 1) * 0.25, 0.05))
            elif goal_prob > 0.75:
                signal = "BUY_GOAL"
                kelly_stake = max(0.0, min((goal_prob * odds - 1) / (odds - 1) * 0.25, 0.05))

            evm = corner_prob * odds - 1.0 if signal == "BUY_CORNER" else (goal_prob * odds - 1.0 if signal == "BUY_GOAL" else 0.0)

            response = {
                "signal": signal,
                "corner_prob": corner_prob,
                "goal_prob": goal_prob,
                "kelly_stake": kelly_stake,
                "evm": evm,
                "match": f"{home} vs {away}",
                "context": {
                    "chuva": ctx.get("clima", {}).get("chuva"),
                    "fator_clima": fator_clima,
                    "fator_pressao": fator_pressao,
                    "jogo_decisivo": ctx.get("noticias", {}).get("jogo_decisivo"),
                },
            }

            try:
                conn = sqlite3.connect("aura_quant_x.db")
                c = conn.cursor()
                c.execute(
                    "INSERT INTO logs_telemetria (timestamp, payload_json, signal, corner_prob, goal_prob) VALUES (?, ?, ?, ?, ?)",
                    (time.time(), json.dumps(payload), signal, corner_prob, goal_prob),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

            await websocket.send_json(response)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"⚠️ WS error: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
