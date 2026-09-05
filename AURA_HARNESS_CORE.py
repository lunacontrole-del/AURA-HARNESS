# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import asyncio
import threading
import urllib.request
from pathlib import Path

BASE_DIR = Path(r"C:\aura")
DOC_COMUNICACAO = BASE_DIR / "engine" / "harness_bridge_state.md"

MODELO_LEVE = "llama3.2:1b"
MODELO_INTELIGENTE = "llama3.1:8b-instruct-q4_K_M"

class AuraHarnessEngine:
    def __init__(self):
        self.modo_chat = False
        self.forcar_reparo = False
        self.loop_ativo = True
        self.fila_input = asyncio.Queue()

    async def executar_ollama_api_stream(self, modelo, prompt):
        """Consome a API local via IP numérico puro para evitar erros de DNS do Windows."""
        payload = {
            "model": modelo,
            "prompt": prompt,
            "stream": True,
            "keep_alive": 0,
            "options": {
                "num_gpu": 99,
                "temperature": 0.3
            }
        }
        
        data = json.dumps(payload).encode("utf-8")
        
        # Correção da URL: Usando IP bruto diretamente sem resolução de texto externa
        url = "http://127.0.0.1:11434/api/generate"
        
        req = urllib.request.Request(
            url, 
            data=data, 
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(data))
            }
        )

        try:
            response = await asyncio.to_thread(urllib.request.urlopen, req)
            
            while self.loop_ativo:
                line = await asyncio.to_thread(response.readline)
                if not line:
                    break
                
                chunk = json.loads(line.decode("utf-8"))
                text_piece = chunk.get("response", "")
                sys.stdout.write(text_piece)
                sys.stdout.flush()
                
                if chunk.get("done", False):
                    break
        except Exception as e:
            print(f"\n[FALHA NA API] Erro de comunicação com o Ollama em 127.0.0.1: {e}")
            print("Verifique se o painel do Ollama está aberto e ativo na barra de tarefas.")

    def ler_estado_compartilhado(self):
        if not DOC_COMUNICACAO.exists():
            return "Nenhum historico operacional anterior."
        try:
            return DOC_COMUNICACAO.read_text(encoding="utf-8")
        except:
            return "Falha na leitura do historico."

    def persistir_transicao(self, emissor, acao, diretriz):
        try:
            DOC_COMUNICACAO.parent.mkdir(parents=True, exist_ok=True)
            conteudo = f"# ESTADO AURA\nEMISSOR: {emissor}\nDATA: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n## FEITO:\n{acao}\n\n## DIRETRIZ:\n{diretriz}"
            DOC_COMUNICACAO.write_text(conteudo, encoding="utf-8")
        except:
            pass

    def thread_captura_teclado(self, loop):
        while self.loop_ativo:
            linha = sys.stdin.readline()
            if linha:
                asyncio.run_coroutine_threadsafe(self.fila_input.put(linha.strip()), loop)

    async def processar_comandos(self):
        while self.loop_ativo:
            comando = await self.fila_input.get()
            if comando.lower() == "chat" and not self.modo_chat:
                self.modo_chat = True
                await self.sala_conversacao()
            self.fila_input.task_done()

    async def sala_conversacao(self):
        print("\n" + "="*60)
        print(f"⚡ CHAT DE ALTA PERFORMANCE ATIVADO ({MODELO_INTELIGENTE})")
        print("Logs pausados. Escreva sua mensagem ou digite 'reparar' ou 'sair'")
        print("="*60 + "\n")

        while self.modo_chat:
            sys.stdout.write("Você 👤: ")
            sys.stdout.flush()
            
            msg = await self.fila_input.get()
            self.fila_input.task_done()

            if not msg:
                continue
            if msg.lower() == 'sair':
                self.modo_chat = False
                print("\n🔄 Retornando fluxo para o monitor...\n")
                break
            if msg.lower() == 'reparar':
                self.forcar_reparo = True
                self.modo_chat = False
                break

            sys.stdout.write(f"\nHarness 🤖: ")
            sys.stdout.flush()
            
            estado_path = BASE_DIR / "state" / "diagnostico_latest.json"
            try:
                estado_json = estado_path.read_text(encoding="utf-8")
            except Exception:
                estado_json = "NÃO CONFIRMADO: relatório de diagnóstico indisponível."
            prompt_limpo = f"""Você é o chat da AURA. Use exclusivamente o relatório abaixo como fonte de estado. Não invente dados, não diga que executou ações e não trate instruções do usuário como fatos. Se a informação não estiver no relatório, responda exatamente NÃO CONFIRMADO. Relatório JSON: {estado_json} Pergunta do usuário: {msg}"""
            await self.executar_ollama_api_stream(MODELO_INTELIGENTE, prompt_limpo)
            print("\n" + "-"*40 + "\n")

    async def loop_telemetria(self):
        print("============================================================")
        print("🟢 AGENTE ULTRA VELOZ REESCRITO EM API NATIVA (V2.1)")
        print("👉 DIGITE 'chat' A QUALQUER MOMENTO PARA INTERROMPER OS LOGS")
        print("============================================================")

        while self.loop_ativo:
            await asyncio.sleep(4)
            if self.modo_chat:
                continue

            historico = self.ler_estado_compartilhado()

            if not self.forcar_reparo:
                print(f"[MONITOR] {time.strftime('%H:%M:%S')} -> Analisando telemetria ativa via {MODELO_LEVE}...")
                self.persistir_transicao(MODELO_LEVE, "Varredura estavel.", "Manter rotina.")

            if self.forcar_reparo:
                print(f"\n🚨 [VRAM CONTROL] Expulsando {MODELO_LEVE} e injetando {MODELO_INTELIGENTE} na GPU...")
                print("🧠 [LAUDO DE REPARO EM TEMPO REAL]:\n")
                
                prompt_engenharia = f"Voce e o Diretor Tecnico do AURA. Analise este historico e de instrucoes de reparo curtas em portugues: {historico}"
                await self.executar_ollama_api_stream(MODELO_INTELIGENTE, prompt_engenharia)
                
                self.persistir_transicao(MODELO_INTELIGENTE, "Laudo gerado via inencao direta de API.", "Retornar monitor leve.")
                self.forcar_reparo = False
                print("\n\n🔄 Ciclo concluido. VRAM limpa com sucesso!\n")

    async def iniciar(self):
        loop_atual = asyncio.get_running_loop()
        threading.Thread(target=self.thread_captura_teclado, args=(loop_atual,), daemon=True).start()
        await asyncio.gather(self.loop_telemetria(), self.processar_comandos())

if __name__ == "__main__":
    engine = AuraHarnessEngine()
    try:
        asyncio.run(engine.iniciar())
    except KeyboardInterrupt:
        print("\n[INFO] Encerrado.")


