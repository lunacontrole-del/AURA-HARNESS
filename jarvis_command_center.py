# bridge/telegram/jarvis_command_center.py
"""
Telegram Central Command - Roteamento direto via HERMES.

TELEGRAM_ENABLED = False por padrao.
Configure ALLOWED_CHAT_ID e bot_token antes de ativar.
"""
import logging
import asyncio

logger = logging.getLogger("aura.telegram.command")

TELEGRAM_ENABLED = False  # OPT-IN
ALLOWED_CHAT_ID = 123456789  # SUBSTITUA pelo seu chat_id real

MAIN_KEYBOARD = [
    ["Modo Trading", "Modo Criativo"],
    ["Modo Voz", "Modo Texto"],
    ["Status do Sistema", "Minhas Memorias"],
]


class JarvisCommandCenter:
    def __init__(self, bot_token: str, hermes_brain):
        self.bot_token = bot_token
        self.hermes = hermes_brain
        self.app = None
        self.voice_mode = False
        self.pending_whatsapp = None

    async def start(self, update, context):
        if not TELEGRAM_ENABLED:
            return
        if update.effective_chat.id != ALLOWED_CHAT_ID:
            return
        try:
            from telegram import ReplyKeyboardMarkup
            reply_markup = ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True)
            await update.message.reply_text("HERMES Central Online.", reply_markup=reply_markup)
        except Exception as e:
            logger.error("Telegram start error: %s", e)

    async def handle_message(self, update, context):
        if not TELEGRAM_ENABLED:
            return
        if update.effective_chat.id != ALLOWED_CHAT_ID:
            return

        text = update.message.text
        msg_lower = text.lower()

        if "modo voz" in msg_lower:
            self.voice_mode = True
            await update.message.reply_text("Modo voz ativado.")
            return
        if "modo texto" in msg_lower:
            self.voice_mode = False
            await update.message.reply_text("Modo texto ativado.")
            return

        response = await self.hermes.process_command(text)
        speak_text = response.get("speak", "Sem resposta.")

        if self.voice_mode:
            try:
                from bridge.jarvis.modules.human_voice import VOICE_SYNTH
                audio_path = await VOICE_SYNTH.synthesize(speak_text, "response.mp3")
                if audio_path:
                    with open(audio_path, "rb") as audio:
                        await context.bot.send_voice(chat_id=update.effective_chat.id, voice=audio)
                    return
            except Exception as e:
                logger.error("Voice send error: %s", e)
        await update.message.reply_text(speak_text)

    def run(self):
        if not TELEGRAM_ENABLED:
            logger.warning("TELEGRAM_ENABLED=False. Command Center nao inicia.")
            return
        try:
            from telegram.ext import Application, CommandHandler, MessageHandler, filters
            self.app = Application.builder().token(self.bot_token).build()
            self.app.add_handler(CommandHandler("start", self.start))
            self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
            self.app.run_polling()
        except Exception as e:
            logger.error("Falha ao iniciar Telegram: %s", e)
