"""Bot de Telegram de Osteria Aelia: el único canal entre JOSU y el agente Dirección.

JOSU escribe en lenguaje normal; el bot lo pasa a la sesión de Dirección y le devuelve
las respuestas. Cuando Dirección pide una aprobación, el bot muestra dos botones
("Sí" y "Cambiar"). Si JOSU escribe mientras hay una aprobación pendiente, su texto
se toma como los cambios que pide.

El bot no guarda nada en disco: la conversación activa y las aprobaciones pendientes
se leen de la propia sesión en Claude. Así puede dormirse y despertar (plan gratuito
de Render) sin perder el hilo.
"""

import asyncio
import hashlib
import json
import logging
import os
import urllib.request
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
# Render pone RENDER_EXTERNAL_URL y PORT. Sin ellos (en un ordenador) el bot pregunta a Telegram cada poco.
URL_PUBLICA = os.environ.get("RENDER_EXTERNAL_URL")
PUERTO = int(os.environ.get("PORT", "8080"))
# Mientras el equipo trabaja, el bot se visita a sí mismo para que Render no lo duerma a mitad de un encargo.
AVISO_DESPIERTO_SEG = 5 * 60

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


def sesion_activa() -> str | None:
    """La conversación de Telegram más reciente que sigue abierta (sin cerrar con /nuevo)."""
    for sesion in cliente.beta.sessions.list(
        agent_id=AGENTE_DIRECCION, statuses=["idle", "running", "rescheduling"], order="desc", limit=20
    ):
        metadatos = sesion.metadata or {}
        if metadatos.get("canal") == "telegram" and metadatos.get("cerrada") != "si":
            return sesion.id
    return None


def crear_sesion() -> str:
    sesion = cliente.beta.sessions.create(
        agent=AGENTE_DIRECCION,
        environment_id=ENTORNO,
        title="Conversación con JOSU",
        metadata={"canal": "telegram"},
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


def cerrar_sesion() -> None:
    sesion_id = sesion_activa()
    if sesion_id:
        cliente.beta.sessions.update(session_id=sesion_id, metadata={"canal": "telegram", "cerrada": "si"})


def aprobacion_pendiente(sesion_id: str) -> str | None:
    """ID de la última petición de aprobación que JOSU aún no ha contestado, si la hay."""
    eventos = cliente.beta.sessions.events.list(
        session_id=sesion_id, types=["agent.custom_tool_use", "user.custom_tool_result"], order="desc", limit=50
    )
    contestadas = set()
    for evento in eventos.data:
        if evento.type == "user.custom_tool_result":
            contestadas.add(evento.custom_tool_use_id)
        elif evento.name == "pedir_aprobacion_josu" and evento.id not in contestadas:
            return evento.id
    return None


def ejecutar_turno(sesion_id: str, eventos: list, enviar, pedir_aprobacion) -> str:
    """Envía `eventos` a la sesión y reenvía a JOSU lo que diga Dirección hasta que se quede en espera.

    Devuelve el motivo de la parada ("end_turn", "requires_action", "budget_reached" o "terminated").
    """
    espera_a_josu = False
    with cliente.beta.sessions.events.stream(session_id=sesion_id) as stream:
        cliente.beta.sessions.events.send(session_id=sesion_id, events=eventos)
        for evento in stream:
            if evento.type == "agent.message":
                texto = "".join(b.text for b in evento.content if b.type == "text").strip()
                if texto:
                    enviar(texto)
            elif evento.type == "agent.custom_tool_use" and evento.name == "pedir_aprobacion_josu":
                espera_a_josu = True
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
                motivo = evento.stop_reason.type
                # requires_action sin aprobación pendiente: ya hemos contestado nosotros, el agente sigue.
                if motivo == "requires_action" and not espera_a_josu:
                    continue
                return motivo
            elif evento.type == "session.status_terminated":
                return "terminated"
    return "end_turn"


def mantener_despierto() -> None:
    try:
        urllib.request.urlopen(URL_PUBLICA, timeout=10)
    except Exception:
        pass  # Cualquier respuesta, incluso un error, cuenta como visita.


class Puente:
    """Une el bucle de Telegram (asíncrono) con el stream de la sesión (síncrono, en un hilo)."""

    def __init__(self, app: Application):
        self.app = app
        self.turno = asyncio.Lock()

    def _desde_hilo(self, corrutina):
        asyncio.run_coroutine_threadsafe(corrutina, self.bucle).result()

    def enviar(self, texto: str) -> None:
        for trozo in range(0, len(texto), 4000):
            self._desde_hilo(self.app.bot.send_message(CHAT_JOSU, texto[trozo:trozo + 4000]))

    def pedir_aprobacion(self, tool_id: str, titulo: str, propuesta: str) -> None:
        botones = InlineKeyboardMarkup([[
            InlineKeyboardButton("Sí", callback_data=f"si:{tool_id}"),
            InlineKeyboardButton("Cambiar", callback_data=f"cambiar:{tool_id}"),
        ]])
        texto = f"Para aprobar: {titulo}\n\n{propuesta}"
        self._desde_hilo(self.app.bot.send_message(CHAT_JOSU, texto[:4000], reply_markup=botones))

    async def _vigilia(self) -> None:
        while True:
            await asyncio.sleep(AVISO_DESPIERTO_SEG)
            await asyncio.to_thread(mantener_despierto)

    async def turno_con(self, eventos: list, sesion_id: str | None = None) -> None:
        async with self.turno:
            self.bucle = asyncio.get_running_loop()
            vigilia = asyncio.create_task(self._vigilia()) if URL_PUBLICA else None
            try:
                if not sesion_id:
                    sesion_id = await asyncio.to_thread(sesion_activa) or await asyncio.to_thread(crear_sesion)
                motivo = await asyncio.to_thread(
                    ejecutar_turno, sesion_id, eventos, self.enviar, self.pedir_aprobacion
                )
            finally:
                if vigilia:
                    vigilia.cancel()
            if motivo == "budget_reached":
                await self.app.bot.send_message(
                    CHAT_JOSU,
                    "Esta conversación ha llegado a su tope de gasto. Escribe /nuevo para empezar otra; "
                    "lo importante ya está guardado en la memoria común.",
                )
            elif motivo == "terminated":
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
    sesion_id = await asyncio.to_thread(sesion_activa)
    pendiente = sesion_id and await asyncio.to_thread(aprobacion_pendiente, sesion_id)
    if pendiente:
        # Hay una aprobación esperando: lo que escribe JOSU son los cambios que pide.
        await puente.turno_con(respuesta_aprobacion(pendiente, f"cambios: {texto}"), sesion_id)
        return
    await puente.turno_con([{"type": "user.message", "content": [{"type": "text", "text": texto}]}], sesion_id)


async def al_pulsar_boton(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not es_josu(update):
        return
    consulta = update.callback_query
    await consulta.answer()
    decision, tool_id = consulta.data.split(":", 1)
    await consulta.edit_message_reply_markup(None)
    sesion_id = await asyncio.to_thread(sesion_activa)
    if not sesion_id or await asyncio.to_thread(aprobacion_pendiente, sesion_id) != tool_id:
        await consulta.message.reply_text("Esta propuesta ya no está pendiente.")
        return
    if decision == "si":
        await consulta.message.reply_text("Aprobado.")
        await context.application.bot_data["puente"].turno_con(respuesta_aprobacion(tool_id, "aprobado"), sesion_id)
    else:
        await consulta.message.reply_text("¿Qué quieres cambiar? Escríbemelo con tus palabras.")


async def al_pedir_nuevo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not es_josu(update):
        return
    await asyncio.to_thread(cerrar_sesion)
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
    if URL_PUBLICA:
        # En Render: Telegram llama al bot (y lo despierta) cada vez que JOSU escribe.
        ruta = hashlib.sha256(TELEGRAM_TOKEN.encode()).hexdigest()[:32]
        log.info("Bot de Osteria Aelia en marcha en %s", URL_PUBLICA)
        app.run_webhook(
            listen="0.0.0.0",
            port=PUERTO,
            url_path=ruta,
            webhook_url=f"{URL_PUBLICA}/{ruta}",
            secret_token=ruta,
        )
    else:
        log.info("Bot de Osteria Aelia en marcha")
        app.run_polling()


if __name__ == "__main__":
    main()
