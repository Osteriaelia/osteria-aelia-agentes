"""Bot de Telegram de Osteria Aelia: el único canal entre JOSU y el agente Dirección.

JOSU escribe en lenguaje normal; el bot lo pasa a la sesión de Dirección y le devuelve
las respuestas. Cuando Dirección pide una aprobación, el bot muestra dos botones
("Sí" y "Cambiar"). Si JOSU escribe mientras hay una aprobación pendiente, su texto
se toma como los cambios que pide.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_JOSU = int(os.environ["TELEGRAM_CHAT_ID_JOSU"])
PRESUPUESTO_SESION_USD = float(os.environ["PRESUPUESTO_SESION_USD"])
ARCHIVO_ESTADO = RAIZ / "bot" / "estado.json"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("aelia")

cliente = anthropic.Anthropic()


def id_recurso(sufijo: str) -> str:
    """Busca en claude-lock.json (lo escribe `ant apply`) el ID del recurso cuyo archivo acaba en `sufijo`."""
    lock = json.loads((RAIZ / "claude-lock.json").read_text())
    for ruta, recurso in lock["resources"].items():
        if ruta.endswith(sufijo):
            return recurso["id"]
    raise SystemExit(f"No encuentro {sufijo} en claude-lock.json. ¿Has ejecutado ant apply?")


AGENTE_DIRECCION = id_recurso("agents/direccion/agent.md")
ENTORNO = id_recurso("agents/direccion/environment.yaml")
MEMORIA = id_recurso("agents/direccion/memory_store-aelia.yaml")


def leer_estado() -> dict:
    if ARCHIVO_ESTADO.exists():
        return json.loads(ARCHIVO_ESTADO.read_text())
    return {"sesion": None, "aprobaciones": {}}


def guardar_estado(estado: dict) -> None:
    ARCHIVO_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2))


def crear_sesion() -> str:
    sesion = cliente.beta.sessions.create(
        agent=AGENTE_DIRECCION,
        environment_id=ENTORNO,
        title="Conversación con JOSU",
        resources=[
            {
                "type": "memory_store",
                "memory_store_id": MEMORIA,
                "access": "read_write",
                "instructions": "Memoria común del equipo. Léela antes de cada encargo y actualiza la ficha al terminar.",
            }
        ],
        # Tope de gasto de la sesión, en céntimos de dólar.
        budget={
            "type": "limit",
            "max_list_cost": {"amount": str(round(PRESUPUESTO_SESION_USD * 100)), "currency": "USD"},
        },
    )
    log.info("Sesión nueva %s", sesion.id)
    return sesion.id


def ejecutar_turno(sesion_id: str, eventos: list, enviar, pedir_aprobacion) -> str:
    """Envía `eventos` a la sesión y reenvía a JOSU lo que diga Dirección hasta que se quede en espera.

    Devuelve el motivo de la parada ("end_turn", "requires_action", "budget_reached" o "terminated").
    """
    with cliente.beta.sessions.events.stream(session_id=sesion_id) as stream:
        cliente.beta.sessions.events.send(session_id=sesion_id, events=eventos)
        for evento in stream:
            if evento.type == "agent.message":
                texto = "".join(b.text for b in evento.content if b.type == "text").strip()
                if texto:
                    enviar(texto)
            elif evento.type == "agent.custom_tool_use" and evento.name == "pedir_aprobacion_josu":
                pedir_aprobacion(evento.id, evento.input.get("titulo", "Aprobación"), evento.input.get("propuesta", ""))
            elif evento.type == "agent.custom_tool_use":
                cliente.beta.sessions.events.send(
                    session_id=sesion_id,
                    events=[{
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": evento.id,
                        "content": [{"type": "text", "text": f"La herramienta {evento.name} no existe."}],
                    }],
                )
            elif evento.type in ("agent.tool_use", "agent.mcp_tool_use") and evento.evaluated_permission == "ask":
                # Nadie vigila las herramientas internas: JOSU no decide cosas técnicas.
                log.warning("Herramienta %s denegada por falta de confirmación", evento.name)
                cliente.beta.sessions.events.send(
                    session_id=sesion_id,
                    events=[{"type": "user.tool_confirmation", "tool_use_id": evento.id, "result": "deny"}],
                )
            elif evento.type == "session.status_idle":
                return evento.stop_reason.type
            elif evento.type == "session.status_terminated":
                return "terminated"
    return "end_turn"


class Puente:
    """Une el bucle de Telegram (asíncrono) con el stream de la sesión (síncrono, en un hilo)."""

    def __init__(self, app: Application):
        self.app = app
        self.turno = asyncio.Lock()

    def _desde_hilo(self, corrutina):
        asyncio.run_coroutine_threadsafe(corrutina, self.bucle).result()

    def enviar(self, texto: str) -> None:
        self._desde_hilo(self.app.bot.send_message(CHAT_JOSU, texto))

    def pedir_aprobacion(self, tool_id: str, titulo: str, propuesta: str) -> None:
        estado = leer_estado()
        estado["aprobaciones"][tool_id] = titulo
        guardar_estado(estado)
        botones = InlineKeyboardMarkup([[
            InlineKeyboardButton("Sí", callback_data=f"si:{tool_id}"),
            InlineKeyboardButton("Cambiar", callback_data=f"cambiar:{tool_id}"),
        ]])
        texto = f"Para aprobar: {titulo}\n\n{propuesta}"
        self._desde_hilo(self.app.bot.send_message(CHAT_JOSU, texto[:4000], reply_markup=botones))

    async def turno_con(self, eventos: list) -> None:
        async with self.turno:
            self.bucle = asyncio.get_running_loop()
            estado = leer_estado()
            if not estado["sesion"]:
                estado["sesion"] = await asyncio.to_thread(crear_sesion)
                guardar_estado(estado)
            motivo = await asyncio.to_thread(
                ejecutar_turno, estado["sesion"], eventos, self.enviar, self.pedir_aprobacion
            )
            if motivo == "budget_reached":
                await self.app.bot.send_message(
                    CHAT_JOSU,
                    "Esta conversación ha llegado a su tope de gasto. Escribe /nuevo para empezar otra; "
                    "lo importante ya está guardado en la memoria común.",
                )
            elif motivo == "terminated":
                estado = leer_estado()
                estado["sesion"] = None
                guardar_estado(estado)
                await self.app.bot.send_message(CHAT_JOSU, "La conversación se ha cerrado. Escríbeme de nuevo para seguir.")


def respuesta_aprobacion(tool_id: str, texto: str) -> list:
    return [{
        "type": "user.custom_tool_result",
        "custom_tool_use_id": tool_id,
        "content": [{"type": "text", "text": texto}],
    }]


def es_josu(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == CHAT_JOSU


async def al_recibir_texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not es_josu(update):
        return
    puente: Puente = context.application.bot_data["puente"]
    texto = update.message.text
    estado = leer_estado()
    if estado["aprobaciones"]:
        # Hay una aprobación esperando: lo que escribe JOSU son los cambios que pide.
        tool_id = next(iter(estado["aprobaciones"]))
        del estado["aprobaciones"][tool_id]
        guardar_estado(estado)
        await puente.turno_con(respuesta_aprobacion(tool_id, f"cambios: {texto}"))
        return
    await puente.turno_con([{"type": "user.message", "content": [{"type": "text", "text": texto}]}])


async def al_pulsar_boton(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not es_josu(update):
        return
    consulta = update.callback_query
    await consulta.answer()
    decision, tool_id = consulta.data.split(":", 1)
    estado = leer_estado()
    if tool_id not in estado["aprobaciones"]:
        await consulta.edit_message_reply_markup(None)
        return
    if decision == "si":
        del estado["aprobaciones"][tool_id]
        guardar_estado(estado)
        await consulta.edit_message_reply_markup(None)
        await consulta.message.reply_text("Aprobado.")
        await context.application.bot_data["puente"].turno_con(respuesta_aprobacion(tool_id, "aprobado"))
    else:
        await consulta.edit_message_reply_markup(None)
        await consulta.message.reply_text("¿Qué quieres cambiar? Escríbemelo con tus palabras.")


async def al_pedir_nuevo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not es_josu(update):
        return
    guardar_estado({"sesion": None, "aprobaciones": {}})
    await update.message.reply_text("Listo, empezamos una conversación nueva. La memoria del equipo se mantiene.")


async def al_empezar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Sirve para que JOSU averigüe su chat_id la primera vez.
    await update.message.reply_text(
        f"Hola. Tu número de chat es {update.effective_chat.id}. Si eres JOSU, ponlo en TELEGRAM_CHAT_ID_JOSU."
    )


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.bot_data["puente"] = Puente(app)
    app.add_handler(CommandHandler("start", al_empezar))
    app.add_handler(CommandHandler("nuevo", al_pedir_nuevo))
    app.add_handler(CallbackQueryHandler(al_pulsar_boton))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, al_recibir_texto))
    log.info("Bot de Osteria Aelia en marcha")
    app.run_polling()


if __name__ == "__main__":
    main()
