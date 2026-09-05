# pre_treinar_gpu.py — Gerador de Pesos Iniciais
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


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


def treinar(num_samples=800, epochs=50):
    print(f"🔥 [PRE-TRAIN] Treinando em {device}...")
    X_list, y_c_list, y_g_list = [], [], []
    for _ in range(num_samples):
        base = np.random.uniform(0.5, 3.0)
        mom = np.random.choice([1.0, 1.6, 2.2])
        seq = []
        da_h = da_a = xg_h = xg_a = g_h = g_a = c_h = c_a = 0.0
        for _t in range(15):
            da_h += np.random.poisson(base * mom * 0.3)
            da_a += np.random.poisson(base * 0.3)
            xg_h += np.random.exponential(0.04 * mom)
            xg_a += np.random.exponential(0.04)
            if np.random.rand() < 0.12 * mom:
                c_h += 1
            if np.random.rand() < 0.07:
                c_a += 1
            if np.random.rand() < 0.04 * mom:
                g_h += 1
            seq.append([da_h, da_a, xg_h, xg_a, g_h, g_a, c_h, c_a])
        X_list.append(seq)
        dda = (da_h - seq[0][0]) / 15
        y_c_list.append([1.0 if dda > 1.5 and mom > 1.4 else 0.0])
        y_g_list.append([1.0 if xg_h > 0.9 or g_h > 0 else 0.0])

    X = torch.tensor(np.array(X_list), dtype=torch.float32).to(device)
    y_c = torch.tensor(np.array(y_c_list), dtype=torch.float32).to(device)
    y_g = torch.tensor(np.array(y_g_list), dtype=torch.float32).to(device)

    model = DualMarketTransformerLSTM().to(device)
    optimizer = optim.AdamW(model.parameters(), lr=0.002, weight_decay=1e-4)
    criterion = nn.BCELoss()
    model.train()

    for epoch in range(1, epochs + 1):
        optimizer.zero_grad()
        p_c, p_g = model(X)
        loss = criterion(p_c, y_c) + criterion(p_g, y_g)
        loss.backward()
        optimizer.step()
        if epoch % 10 == 0 or epoch == epochs:
            print(f"  Época {epoch}/{epochs} — Loss: {loss.item():.4f}")

    torch.save(model.state_dict(), "model_weights.pt")
    print("✅ [SUCESSO] Arquivo 'model_weights.pt' gerado.")


if __name__ == "__main__":
    treinar()
