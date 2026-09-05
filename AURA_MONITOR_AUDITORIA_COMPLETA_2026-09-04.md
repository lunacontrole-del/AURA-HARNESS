# AURA QUANT-X — Auditoria Completa Manual × AURA Monitor

Data: 2026-09-04
Base documental: MANUAL.txt, versão 12.6.17, última atualização indicada no manual: 2026-08-18.
Arquivo auditado: AURA_MONITOR_FIXED_v3_MAX.ps1 (62.054 bytes / 1.250 linhas) e versão corrigida: AURA_MONITOR_FIXED_v4_AUDIT_MAX.ps1.

## Resultado executivo

A V3 MAX tinha boa cobertura de inventário, serviços e diagnóstico, mas não deveria ser considerada final. Foram encontrados pontos técnicos e de cobertura que poderiam gerar diagnóstico falso ou deixar funções do manual sem validação real.

### Problemas encontrados na V3 MAX

1. **Integridade de dados incompleta:** a reconciliação de escanteios praticamente não comparava estruturas aninhadas; quando estatística/eventos vinham como objetos, o valor podia permanecer `$null` e um conflito autoritativo não era necessariamente detectado.
2. **Auditoria do próprio manual ausente:** a V3 verificava existência, mas não comparava cabeçalho/rodapé nem detectava inconsistências documentais.
3. **CORS da voz não era exercitado:** o manual registra que CORS `chrome-extension://*` foi uma causa real de falha; o monitor não testava esse caminho.
4. **SQLite era apenas tratado como arquivo:** não havia inspeção das tabelas reais do banco, portanto não se confirmava a persistência de telemetria/paper/outcomes/risk calibration.
5. **Cobertura de extensão incompleta:** não havia inventário explícito dos módulos `lib/*`, partes de `ui/dashboard` e `visao/chat.css` descritos no manual.
6. **WoM pouco auditado:** o campo `market` era exibido, mas não havia classificação explícita dos thresholds documentados (>1,5% divergência e <=-5% confluência).
7. **Documentação operacional não era totalmente inventariada:** relatórios/scripts introduzidos na auditoria 12.6.17 não estavam todos na lista de verificação.
8. **A própria V3 MAX carregava inconsistências de montagem que impediam tratá-la como entrega final sem correção/validação adicional.**

## Achados do manual

O manual declara 4 serviços oficiais: Bridge :8080, Engine :8765, Voice :8099 e Ollama :11434; também define persistência, extensão MV3, Jarvis, WoM, orquestrador, recuperação, observabilidade, gates e diagnóstico Windows.

### Inconsistências documentais confirmadas

- O cabeçalho declara **12.6.17**, mas a linha de encerramento antiga ainda diz **"Fim do Manual v12.5.10"**.
- O manual manda consultar a **seção 8**, mas a estrutura carregada possui as seções numeradas 1–7; essa referência precisa ser corrigida.
- A parte 12.6.17 foi acrescentada depois do encerramento 12.5.10, sem consolidar o fechamento/versionamento do documento.
- O arquivo recebido para esta auditoria se chama `MANUAL.txt`, enquanto o manual exige `MANUAL.md` dentro da árvore de release; isso não prova erro da instalação, mas é uma diferença de artefato que o monitor deve sinalizar.

## Cobertura do monitor corrigido

### Serviços

- TCP loopback e HTTP health.
- Latência.
- Fallback de health quando documentado.
- Estado crítico/degradado.
- Voice `loading`, `engineReady`, `llmHealth`.
- Ollama `/api/tags` e lista de modelos instalados.

### Engine / observabilidade

- `/health`.
- `/api/status`.
- `/api/orchestrator/status`.
- `/api/observability/events`.
- Análise atual por `fixtureId`.
- Campos de explicabilidade: `decision`, `reason`, `risk_gate`, `edge`, `uncertainty`, `market`, `data_integrity`.
- Inventário de módulos Core, improvements e reliability.

### Voz

- `/api/voice/health`.
- `/api/voice/diagnostic`.
- Estado do STT/TTS/LLM exposto pelo health.
- Modelo solicitado/ativo/fallback.
- Teste de CORS com origem `chrome-extension://...`.
- Inventário dos módulos Jarvis e scripts de inicialização.

### Dados e gates

- Freshness do feed com limite de 45 s.
- Valores negativos conhecidos.
- Fixture/times/relógio quando presentes no feed.
- Reconciliação estatística/eventos quando houver valores comparáveis.
- WoM e thresholds documentados.
- Risk gate e códigos de bloqueio quando expostos pela análise.
- SQLite com enumeração das tabelas usando o Python do venv.

### Windows / ambiente

- RAM / disco / CPU / GPU.
- `nvidia-smi`.
- Torch e CUDA dentro do Python detectado.
- venv e pip.
- Imports básicos.
- Firewall/Defender/DISM/SFC como disponibilidade/estado de diagnóstico, sem executar reparos.
- Proprietário das portas AURA.

### Estrutura e release

- MANUAL.
- instalador.
- validadores.
- diagnóstico.
- recuperação.
- documentação operacional 12.6.17.
- componentes esperados da extensão.
- arquivos críticos do Engine/Bridge/Jarvis.

## Limitações que permanecem deliberadamente

O monitor é **READ-ONLY** em relação à operação AURA. Não executa `POST` de telemetria, feedback, Telegram, reload de voz, treino, recovery, liberação de firewall ou reinício de serviço. Assim, ele pode afirmar que uma rota existe e que um endpoint GET responde, mas não deve simular uma ação mutável só para obter um "OK".

Também não é possível provar captura DOM real do SokkerPRO, Chrome real, GPU real, Ollama real ou pipeline de áudio completo a partir deste ambiente. O próprio manual registra que esses testes exigem a máquina real.

## Recomendações de governança

Antes de chamar a entrega de final, atualizar o `MANUAL.md` com o changelog desta mudança e corrigir as inconsistências de versão/referência. Depois executar no Windows real: `INSTALAR_E_INICIAR_TUDO.bat`, `VALIDAR_AURA_QUANT_X.bat`, `VALIDAR_AURA_TESTES.bat --services` e 🩺 Testar Voz.

## Verificação estática desta entrega corrigida

- Funções identificadas no V4: 66
- Nomes de função duplicados: nenhum
- Hash SHA-256 do V4: `cd698552e801afbff77133a2f57e46cf5ad8c484bc46a5d2456e828f766080a6`
- Tamanho V4: 71,850 bytes / 1,390 linhas
- Nota: a contagem simples de delimitadores não substitui o parser PowerShell. Este ambiente não disponibiliza `pwsh`/Windows PowerShell para execução real.
