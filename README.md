# Osteria Aelia · Sistema de agentes (fase 1)

Equipo de agentes IA que gestiona la comunicación de Osteria Aelia. JOSU habla solo con el agente **Dirección** por Telegram; Dirección reparte el trabajo y le pide aprobación únicamente para lo importante.

Fase 1: Dirección, Estrategia y Copywriter, con una memoria común y el bot de Telegram. Diseño completo en la [arquitectura del sistema](https://claude.ai/artifact/4qsoF9HrnFFSTe2WLdD7Ua) y las [fichas de los agentes](https://claude.ai/artifact/XfSdp5W6PeMGTRmmXvhBGF).

## Cómo funciona

- Los agentes viven en **Claude Managed Agents** (Anthropic ejecuta los agentes y su espacio de trabajo; no hace falta un servidor para ellos).
- `agents/` define cada agente en un archivo: arriba su configuración, debajo sus instrucciones. Se suben con `ant apply`.
- La **memoria común** (`memory_store-aelia.yaml`) guarda marca, decisiones de JOSU y fichas de encargo entre conversaciones. `memoria_inicial/` es su contenido de partida.
- `bot/bot.py` es el puente con Telegram. Es lo único que tiene que estar encendido: en un ordenador para probar, o en Render.

```
agents/
  direccion/agent.md              coordinador; reparte entre Estrategia y Copywriter
  direccion/environment.yaml      espacio de trabajo en la nube
  direccion/memory_store-aelia.yaml
  estrategia/agent.md
  copywriter/agent.md
memoria_inicial/                  documentos de partida de la memoria común
bot/bot.py                        bot de Telegram
bot/cargar_memoria.py             carga memoria_inicial/ una sola vez
```

## Puesta en marcha

Sin terminal: todo se hace desde la web de GitHub y de Render.

1. **Clave de Claude en GitHub:** en el repositorio, Settings > Secrets and variables > Actions > New repository secret. Nombre `ANTHROPIC_API_KEY`, valor la clave de console.anthropic.com.
2. **Subir los agentes:** pestaña Actions > "Preparar agentes" > Run workflow. Crea los tres agentes, el entorno y la memoria común, carga `memoria_inicial/` y guarda `claude-lock.json` en el repositorio.
3. **Encender el bot en Render (plan gratuito):** crear cuenta en render.com con GitHub, New > Blueprint y elegir `osteria-aelia-agentes`. Render lee `render.yaml` y pide dos valores:
   - `ANTHROPIC_API_KEY`: la clave de Claude
   - `TELEGRAM_TOKEN`: el token de @BotFather
   `TELEGRAM_CHAT_ID_JOSU` empieza en `0` y `PRESUPUESTO_SESION_USD` (tope de gasto por conversación, en dólares) en `5`.
4. **Tu número de chat:** escribe `/start` al bot en Telegram, copia el número que te responde en `TELEGRAM_CHAT_ID_JOSU` (Render > aelia-bot > Environment) y guarda; el bot se reinicia solo.

En el plan gratuito el bot se duerme tras 15 minutos sin mensajes. El primer mensaje después lo despierta y la respuesta tarda alrededor de un minuto. El bot no guarda nada en el servidor: la conversación y las aprobaciones pendientes se leen de Claude, así que dormirse no pierde nada.

Para probar en un ordenador en lugar de Railway: `pip install -r bot/requirements.txt`, copiar `.env.example` como `.env`, rellenarlo y ejecutar `python bot/bot.py`. Para subir los agentes a mano hace falta la línea de comandos `ant` (1.40.0 o posterior) y ejecutar los mismos comandos que el workflow.

## Uso diario

- Escribe al bot lo que necesites, como se lo dirías a una persona.
- Cuando Dirección necesite tu visto bueno verás los botones **Sí** y **Cambiar**. Si pulsas Cambiar, o escribes mientras hay algo pendiente, tu texto se toma como los cambios.
- `/nuevo` empieza una conversación limpia. Lo importante sigue en la memoria común.
- Cada conversación tiene un tope de gasto (`PRESUPUESTO_SESION_USD`). Al alcanzarlo, el bot avisa y basta con `/nuevo`.

## Pendiente

- **Multiagente:** la configuración de Dirección usa el tipo `multiagent_20261001` y nombra a sus subagentes por ruta, lo que necesita `ant` 1.40.0 o posterior.
- Datos de negocio que faltan en `memoria_inicial/marca/estado.md`.
