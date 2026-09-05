# aura_auto_evolver.py — Motor de Treinamento Autônomo e Otimização em Idle
import asyncio
import time
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class AutoEvolverEngine:
    def __init__(self):
        self.last_activity_time = time.time()
        self.is_training = False
        self.total_epochs = 0
        self.best_loss = float("inf")

    def notify_activity(self):
        self.last_activity_time = time.time()
        if self.is_training:
            self.is_training = False
            print("⚡ [Auto-Evolver] Telemetria ao vivo — pausando treino idle.")

    def is_idle(self) -> bool:
        return (time.time() - self.last_activity_time) > 60

    async def run_idle_training_loop(self, model_class):
        print(f"🤖 [Auto-Evolver] Engine de evolução ativa em {device}.")
        model = model_class().to(device)
        try:
            model.load_state_dict(torch.load("model_weights.pt", map_location=device))
        except Exception:
            pass

        optimizer = optim.AdamW(model.parameters(), lr=0.0005, weight_decay=1e-4)
        criterion = nn.BCELoss()

        while True:
            await asyncio.sleep(5)
            if not self.is_idle():
                continue

            self.is_training = True
            X = torch.tensor(np.random.randn(200, 15, 8), dtype=torch.float32).to(device)
            y_c = torch.tensor(np.random.choice([0.0, 1.0], size=(200, 1), p=[0.7, 0.3]), dtype=torch.float32).to(device)
            y_g = torch.tensor(np.random.choice([0.0, 1.0], size=(200, 1), p=[0.8, 0.2]), dtype=torch.float32).to(device)

            model.train()
            for _ in range(5):
                if not self.is_idle():
                    self.is_training = False
                    break
                optimizer.zero_grad()
                p_c, p_g = model(X)
                loss = criterion(p_c, y_c) + criterion(p_g, y_g)
                loss.backward()
                optimizer.step()
                self.total_epochs += 1
                if loss.item() < self.best_loss:
                    self.best_loss = loss.item()
                    torch.save(model.state_dict(), "model_weights.pt")

            if self.is_training:
                print(f"🚀 [GPU IDLE EVOLVING] Épocas: {self.total_epochs} | Best Loss: {self.best_loss:.5f}")
            self.is_training = False


evolver = AutoEvolverEngine()
