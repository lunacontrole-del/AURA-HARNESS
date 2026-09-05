# Agente Independente de Escanteios — AURA Quant-X

Este pacote contém somente o especialista de escanteios para as janelas críticas **35’–intervalo** e **85’–fim**, separado do sistema AURA. Não inclui Engine, Bridge, WebView2, Ollama, GLM, banco SQLite, instalador, serviços Windows ou executores de ordens.

A memória `corner_pattern_memory.json` foi atualizada com a curadoria dos seis anexos mais recentes. As informações da Série A e Série B estão classificadas como contexto pré-jogo. O código de treinamento recebido foi auditado e não foi usado como treinador, porque gera resultados sintéticos com Poisson e não realiza aprendizagem supervisionada.

## Conteúdo

| Arquivo | Função |
|---|---|
| `corner_window_specialist.py` | Núcleo independente do especialista, sem dependências externas. |
| `corner_pattern_memory.json` | Memória de padrões, experiência Green/Red/Push e política de curadoria. |
| `validate_agent.py` | Validação estática e smoke test determinístico local. |
| `README_AGENTE_ESCANTEIOS_INDEPENDENTE.md` | Instruções e limites do pacote. |

## Uso manual

```bash
python validate_agent.py
```

Para integrar manualmente, importe `CornerWindowSpecialist` e forneça snapshots atuais com `minute`, `period` e `data_quality`, além dos indicadores live disponíveis. O especialista retorna `OBSERVE`, `PREPARE` ou `NO_BET`. `PREPARE` é apenas pré-alerta e nunca é entrada executável.

> Este pacote é advisory-only e opera exclusivamente em `SHADOW_PAPER_TRADE`. O campo `execution_allowed` permanece sempre `false`.

A memória não é um modelo estatístico calibrado. Para promoção a treino, os dados precisam de partidas reais rotuladas, IDs, timestamps, ordenação temporal, validação walk-forward, holdout futuro e calibração independente. Snapshots de tabela não são ground truth para as janelas live.
