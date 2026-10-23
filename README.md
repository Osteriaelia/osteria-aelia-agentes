# Osteria Aelia · Sistema de agentes (fase 1)

Equipo de agentes IA que gestiona la comunicación de Osteria Aelia. JOSU habla solo con el agente **Dirección** por Telegram; Dirección reparte el trabajo y le pide aprobación únicamente para lo importante.

Fase 1: Dirección, Estrategia y Copywriter, con una memoria común y el bot de Telegram. Diseño completo en la [arquitectura del sistema](https://claude.ai/artifact/4qsoF9HrnFFSTe2WLdD7Ua) y las [fichas de los agentes](https://claude.ai/artifact/XfSdp5W6PeMGTRmmXvhBGF).

## Cómo funciona

- Los agentes viven en **Claude Managed Agents** (Anthropic ejecuta los agentes y su espacio de trabajo; no hace falta un servidor para ellos).
- `agents/` define cada agente en un archivo: arriba su configuración, debajo sus instrucciones. Se suben con `ant apply`.
- La **memoria común** (`memory_store-aelia.yaml`) guarda marca, decisiones de JOSU y fichas de encargo entre conversaciones. `memoria_inicial/` es su contenido de partida.
- `bot/bot.py` es el puente con Telegram. Es lo único que tiene que estar encendido: en un ordenador para probar, y después en un servidor pequeño.

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

Se hace una vez, paso a paso y con ayuda de Claude.

1. **Cuenta de la API de Claude:** crear cuenta en console.anthropic.com, añadir saldo y crear una clave (API key).
2. **Bot de Telegram:** hablar con @BotFather, enviar `/newbot` y guardar el token que da.
3. **Instalar las herramientas:** Python 3.10 o superior y la línea de comandos `ant` de Anthropic. Después:
   ```sh
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r bot/requirements.txt
   cp .env.example .env    # y rellenar los valores
   ```
4. **Subir los agentes** (primero se revisa el plan, luego se aplica):
   ```sh
   ant apply --dry-run -v agents/direccion/agent.md agents/direccion/environment.yaml agents/direccion/memory_store-aelia.yaml
   ant apply agents/direccion/agent.md agents/direccion/environment.yaml agents/direccion/memory_store-aelia.yaml
   ```
   `ant apply` sube también Estrategia y Copywriter porque Dirección los nombra, y escribe `claude-lock.json`, que hay que guardar en el repositorio.
5. **Cargar la memoria de partida:** `python bot/cargar_memoria.py`
6. **Arrancar el bot:** `python bot/bot.py`, escribirle `/start` desde Telegram, copiar el número de chat en `TELEGRAM_CHAT_ID_JOSU` y reiniciar el bot.

## Uso diario

- Escribe al bot lo que necesites, como se lo dirías a una persona.
- Cuando Dirección necesite tu visto bueno verás los botones **Sí** y **Cambiar**. Si pulsas Cambiar, o escribes mientras hay algo pendiente, tu texto se toma como los cambios.
- `/nuevo` empieza una conversación limpia. Lo importante sigue en la memoria común.
- Cada conversación tiene un tope de gasto (`PRESUPUESTO_SESION_USD`). Al alcanzarlo, el bot avisa y basta con `/nuevo`.

## Pendiente

- **Acceso a internet de Estrategia:** el entorno está en modo `limited`, que bloquea la búsqueda web. Para que Estrategia investigue competencia y tendencias hay que cambiar `networking` a `{type: unrestricted}` en `environment.yaml`. En ese espacio de trabajo no se guardan claves.
- **Multiagente:** la configuración de Dirección usa el tipo `multiagent_20261001` y nombra a sus subagentes por ruta, lo que necesita `ant` 1.40.0 o posterior.
- Datos de negocio que faltan en `memoria_inicial/marca/estado.md`.
