# Manifesto de Proveniência — Agente Independente de Escanteios

## Estado real da incorporação

O pacote contém a memória curada `corner_pattern_memory.json` com **32 padrões** e o núcleo executável `corner_window_specialist.py`. A memória foi construída a partir de materiais de aprendizagem previamente revisados, mas não representa uma cópia integral de todos os arquivos brutos.

Para tornar a proveniência verificável, este pacote ampliado inclui também os anexos brutos disponíveis na sessão, organizados em `learning_corpus/`. Os arquivos brutos são evidência de origem e material para revisão futura; a sua presença no ZIP não significa que todos os conteúdos foram promovidos a pesos estatísticos.

## Política de curadoria

Os alertas Green, Red, Push, pré-alertas e casos sem entrada permanecem classes distintas. Resultados pós-jogo não podem ser usados como features do snapshot anterior. Dados sem ID/timestamp suficiente, snapshots de tabela não verificados e resultados sintéticos permanecem fora de treino/calibração.

O código `BRASILEIRAO_2026_AGENTE_IA_TREINAMENTO.py` está incluído apenas em `learning_corpus/reference_rejected/` para rastreabilidade. Ele não é executado nem usado como treinador, pois gera resultados aleatórios com Poisson e calcula uma acurácia artificial.

## Componentes do agente

| Componente | Estado |
|---|---|
| `corner_window_specialist.py` | Executável, independente, apenas biblioteca padrão. |
| `corner_pattern_memory.json` | Memória v7 curada, 32 padrões, paper trade. |
| `validate_agent.py` | Smoke test determinístico e verificação dos guards. |
| `learning_corpus/` | Cópia dos anexos brutos para auditoria/proveniência. |

> `paper_trade: true`, `advisory_only: true` e `execution_allowed: false` são invariantes do pacote.

O pacote continua separado do AURA: não inclui Engine, Bridge, WebView2, Ollama, GLM, SQLite, instalador, serviços Windows ou executores de ordens.
