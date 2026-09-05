# Melhorias pré-prática (obrigatórias antes de stake real)

## O que foi adicionado

| Módulo | Função |
|--------|--------|
| `risk_manager.py` | Kelly fracionário, stop diário, limite por jogo/dia, cooldown |
| `metrics.py` | Brier, log-loss, ROI, profit factor, max drawdown, calibração |
| `features.py` | Features estáveis (ΔDA, ΔxG, ritmo de cantos) + baseline |
| `backtest_engine.py` | Backtest a partir de SQLite/JSONL com outcomes |
| `train_pipeline.py` | Treino com split temporal + comparação com baseline Brier |
| `data_store.py` | `signal_outcomes` + `paper_trades` |
| `signals_service.py` | Une modelo + baseline + risco + paper trade |

## Fluxo correto (ainda NÃO é dinheiro real)

1. Rode o master / extensão como sempre.
2. Cada BUY aprovado pelo `RiskManager` abre um **paper trade**.
3. Quando souber o resultado, registre outcome:
   ```python
   from data_store import resolve_paper_trade
   resolve_paper_trade(trade_id=123, outcome=1)  # 1=green 0=red
   ```
4. Rode o backtest:
   ```bash
   python backtest_engine.py --db aura_quant_x.db
   ```
5. Só considere stake real se:
   - ROI paper > 0 em amostra relevante
   - Brier < baseline 0.5
   - max drawdown aceitável

## Treino

```bash
python train_pipeline.py --epochs 40
```

Salva `model_weights.pt` e `train_report.json` (informa se bateu baseline).

## O que ainda falta para “10/10”

- Dataset real rotulado (não só sintético)
- Semanas de paper trading
- Feed estável (API) além de DOM
- Calibração em dados reais
