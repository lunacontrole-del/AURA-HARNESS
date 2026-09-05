# bridge/telegram/elite_dashboard.py
"""
Elite Telegram Dashboard - menus InlineKeyboard.
TELEGRAM_ELITE_ENABLED = False por padrao.
"""
from __future__ import annotations
import logging

logger = logging.getLogger("aura.telegram.elite")

TELEGRAM_ELITE_ENABLED = False

try:
    from engine.agents.agent_control_hub import CONTROL_HUB
except Exception:
    CONTROL_HUB = None

try:
    from engine.agents.hermes_sre import HERMES_SRE
except Exception:
    HERMES_SRE = None


class EliteDashboard:
    def __init__(self):
        self.handlers = []
        if not TELEGRAM_ELITE_ENABLED:
            logger.info("EliteDashboard desabilitado (TELEGRAM_ELITE_ENABLED=False).")
            return
        try:
            from telegram.ext import CallbackQueryHandler
            self.handlers = [
                CallbackQueryHandler(self.main_menu, pattern="^main$"),
                CallbackQueryHandler(self.agent_menu, pattern="^agents$"),
                CallbackQueryHandler(self.toggle_agent, pattern="^toggle_"),
                CallbackQueryHandler(self.medic_scan, pattern="^medic_scan$"),
            ]
        except Exception as e:
            logger.error("python-telegram-bot ausente: %s", e)

    def get_main_keyboard(self):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [
            [InlineKeyboardButton("Controle de Agentes", callback_data="agents")],
            [InlineKeyboardButton("Diagnostico Completo", callback_data="medic_scan")],
            [
                InlineKeyboardButton("Relatorio Rapido", callback_data="report"),
                InlineKeyboardButton("Modo Sistema", callback_data="mode"),
            ],
            [InlineKeyboardButton("Atualizar Status", callback_data="main")],
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_agent_keyboard(self):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        states = CONTROL_HUB.get_status() if CONTROL_HUB else {}
        keyboard = []
        for agent, is_active in states.items():
            status_icon = "ATIVO" if is_active else "PAUSADO"
            keyboard.append(
                [InlineKeyboardButton(f"{agent}: {status_icon}", callback_data=f"toggle_{agent}")]
            )
        keyboard.append([InlineKeyboardButton("Voltar", callback_data="main")])
        return InlineKeyboardMarkup(keyboard)

    async def main_menu(self, update, context):
        if not TELEGRAM_ELITE_ENABLED:
            return
        query = update.callback_query
        await query.answer()
        states = CONTROL_HUB.get_status() if CONTROL_HUB else {}
        active_count = sum(1 for v in states.values() if v)
        text = (
            f"CENTRAL DE COMANDO AURA\n"
            f"----------------------\n"
            f"Agentes Ativos: {active_count}/{len(states)}\n"
            f"Captura: SokkerPRO\n"
            f"Modo: Trading\n\n"
            f"Selecione uma opcao abaixo:"
        )
        await query.edit_message_text(text, reply_markup=self.get_main_keyboard())

    async def agent_menu(self, update, context):
        if not TELEGRAM_ELITE_ENABLED:
            return
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "CONTROLE DE AGENTES\nSelecione um agente para PAUSAR ou REATIVAR:",
            reply_markup=self.get_agent_keyboard(),
        )

    async def toggle_agent(self, update, context):
        if not TELEGRAM_ELITE_ENABLED or CONTROL_HUB is None:
            return
        query = update.callback_query
        await query.answer()
        agent_name = query.data.replace("toggle_", "")
        current_state = CONTROL_HUB.get_status().get(agent_name, False)
        msg = (
            CONTROL_HUB.pause_agent(agent_name)
            if current_state
            else CONTROL_HUB.resume_agent(agent_name)
        )
        await query.edit_message_text(
            f"{msg}\n\nCONTROLE DE AGENTES",
            reply_markup=self.get_agent_keyboard(),
        )

    async def medic_scan(self, update, context):
        if not TELEGRAM_ELITE_ENABLED:
            return
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "SYSTEM MEDIC ATIVADO\nAnalisando sinais vitais...\nAguarde."
        )
        if HERMES_SRE is None:
            speak = "Hermes SRE indisponivel."
        else:
            result = await HERMES_SRE.run_system_maintenance()
            speak = result.get("speak", "Erro no diagnostico.")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Laudo Medico:\n{speak}",
            reply_markup=self.get_main_keyboard(),
        )


ELITE_DASHBOARD = EliteDashboard()
