# AURA Quant X 12.7.7 — pacote atualizado autorizado

Este é um pacote local de staging, paper-only e fail-closed. Não é release assinado do fornecedor.

## Componentes incluídos

O pacote inclui os patches anteriores de drift, odds, slippage, proteção de capital, compactação de contexto, veracidade, snapshot, rate limiting, hot-reload seguro, voz autorizada, fase de jogo, liquidez e memória negativa. Também inclui módulos inertes para Safe Executor observacional, Model Promoter advisory, Dead Man’s Switch observacional, UI broadcaster em memória, healer JSON do LLM, sanitizador/wake-word e alertas táticos sem hardware.

## Invariantes

```text
AURA_PAPER_TRADE=1
AURA_EXECUTION_ALLOWED=0
AURA_UNLOCK_LIVE=0
paper_trade=true
execution_allowed=false
approved=false
Bridge token obrigatório
```

## Não ativado

DOM stealth/anti-bot bypass, execução arbitrária de código, promoção automática, kill/restart automático, Telegram, WebSocket, STT real, áudio real, captura browser, prefetch DOM, Desktop WebView2, APIs externas, Ollama, Voice externo, GPU pre-warm e execução real permanecem bloqueados ou inertes.

A razão está documentada: esses itens exigem ambiente/credenciais/rede específicos ou removem controles de segurança. Os módulos inertes não iniciam esses recursos por import, instalação ou default.

## Validação

A suíte offline autorizada passou integralmente. O precheck passou com zero erros e um aviso não bloqueante. A análise AST passou para 459 arquivos Python. Nenhum processo ou serviço externo foi iniciado durante o empacotamento.
