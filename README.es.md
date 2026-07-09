# saiyia

**[English](README.md) | [Español](README.es.md) | [中文](README.zh.md) | [日本語](README.ja.md)**

Un servidor de pasarela (gateway) de IA de voz de código abierto.

Hace una sola cosa: dar a cualquier cliente que pueda abrir un WebSocket o hacer una petición HTTP (teléfono, web, hardware tipo ESP32) un sistema de cuentas unificado más un proxy para chat de IA / reconocimiento de voz / síntesis de voz, sin que ese cliente tenga que guardar nunca la clave de un proveedor de IA externo. Encaja bien en proyectos de hardware — por ejemplo, un robot de compañía con micrófono y altavoz.

## Decisiones de diseño

Se mantiene deliberadamente minimalista — solo hace "cuentas + proxy de capacidades de IA": sin sistema de pago/suscripción (el modelo `User` no tiene ningún campo de suscripción, todas las cuentas son iguales, solo con límite de tasa), sin sincronización de datos multi-dispositivo, sin panel de administración. Si cobrar, cómo cobrar, si guardar el historial de conversaciones — todo eso se deja para que tú lo decidas e implementes por encima.

## Soporte de proveedores

Chat, reconocimiento de voz y síntesis de voz eligen proveedor **de forma independiente** — no estás atado a un solo proveedor para todo. Configura `CHAT_PROVIDER` / `ASR_PROVIDER` / `TTS_PROVIDER` en tu `.env`:

| Proveedor | Chat | ASR de grabación completa | TTS en streaming | Notas |
|---|---|---|---|---|
| `dashscope` (por defecto) | ✅ | ✅ (diarización de hablantes nativa) | ✅ | Alibaba Cloud Model Studio |
| `openai` | ✅ | ✅ (Whisper, sin diarización) | ✅ | También funciona con cualquier endpoint compatible con OpenAI (Groq, Together, DeepSeek, un servidor vLLM propio, etc.) para chat, apuntando `OPENAI_BASE_URL` a otro sitio |

**Añadir un proveedor no requiere modificar todo el proyecto** — mira `app/providers/`: `chat.py` simplemente resuelve un par `(base_url, api_key)` (la mayoría de proveedores de LLM son compatibles con OpenAI, así que normalmente no hace falta código nuevo); `tts.py` y `asr.py` definen cada uno una pequeña interfaz `Protocol` que implementas una vez por proveedor. El reconocimiento de voz en tiempo real (`WS /asr/stream`) no necesita ningún adaptador — es una retransmisión transparente de bytes/texto, así que funciona con *cualquier* API de voz en tiempo real basada en WebSocket; basta con apuntar `REALTIME_ASR_WS_URL` / `REALTIME_ASR_AUTH_HEADER` a ella.

## Endpoints disponibles

| Endpoint | Descripción |
|---|---|
| `POST /api/v1/auth/register` `/login` `/me` `/logout` `/change-password` `/delete-account` | Sistema de cuentas, autenticación JWT, el mecanismo `token_version` permite revocar instantáneamente los tokens antiguos al cerrar sesión |
| `POST /api/v1/chat/completions` | Redirige al proveedor que seleccione `CHAT_PROVIDER` (formato compatible con OpenAI, con soporte de streaming) |
| `POST /api/v1/audio/tts` | Síntesis de voz de una sola vez, devuelve un MP3 completo |
| `POST /api/v1/audio/tts/stream` | Síntesis de voz en streaming, emite PCM crudo a medida que se genera (la frecuencia de muestreo depende del proveedor — viene indicada en el `Content-Type` de la respuesta), baja latencia del primer byte, ideal para reproducir mientras se recibe |
| `POST /api/v1/asr` | Transcripción de grabación completa |
| `WS /api/v1/asr/stream` | Retransmisión de reconocimiento de voz en tiempo real, el texto llega mientras hablas |

## Inicio rápido

```bash
cp .env.example .env   # rellena SECRET_KEY, ALIBABA_API_KEY (u OPENAI_API_KEY), DB_PASSWORD
docker compose up -d --build
curl http://localhost:8000/api/v1/health
```

## Guía de integración con hardware (p. ej. ESP32)

### Autenticación

Primero llama a `/api/v1/auth/register` o `/login` para obtener un JWT, luego envíalo como `Authorization: Bearer <token>` en cada petición.

Si tu cliente WebSocket no puede establecer cabeceras personalizadas en el handshake (esto ocurre con el WebSocket nativo del navegador), puedes pasar el token como query string: `wss://.../asr/stream?token=xxx` — el servidor acepta ambas formas.

### Protocolo de reconocimiento de voz en tiempo real

`WS /api/v1/asr/stream` es una **retransmisión transparente** al endpoint WebSocket que indique `REALTIME_ASR_WS_URL` (por defecto, paraformer-realtime-v2 de Alibaba Cloud DashScope) — el servidor solo gestiona la autenticación y el reenvío, no toca el contenido de los mensajes. Con el proveedor DashScope por defecto, tras conectar:

1. Envía un frame de texto JSON para iniciar una tarea de reconocimiento:

```json
{
  "header": { "action": "run-task", "task_id": "<id hex aleatorio de 32 caracteres>", "streaming": "duplex" },
  "payload": {
    "task_group": "audio",
    "task": "asr",
    "function": "recognition",
    "model": "paraformer-realtime-v2",
    "parameters": {
      "format": "pcm",
      "sample_rate": 16000,
      "punctuation_prediction_enabled": true
    },
    "input": {}
  }
}
```

2. Al recibir `{"header":{"event":"task-started"}}`, empieza a enviar **frames binarios**: PCM crudo a 16kHz / 16-bit / mono / little-endian. El tamaño del frame es libre — desde decenas hasta cientos de milisegundos, cuanto más pequeño, menor latencia.

3. El servidor irá devolviendo frames de texto a medida que avanza el reconocimiento:

```json
{"header":{"event":"result-generated"},"payload":{"output":{"sentence":{"text":"hola","sentence_end":false}}}}
```

`sentence_end: false` es un resultado provisional (todavía hablando), `true` significa que esa frase es definitiva.

4. Cuando termines, envía `finish-task`:

```json
{"header":{"action":"finish-task","task_id":"<mismo id que arriba>","streaming":"duplex"},"payload":{"input":{}}}
```

Si apuntas `REALTIME_ASR_WS_URL` a otro proveedor (p. ej. la Realtime API de OpenAI), sigue el formato de mensajes propio de ese proveedor — la retransmisión en sí es agnóstica al protocolo.

**En ESP32 recomendamos usar [ESP-SR](https://github.com/espressif/esp-sr) para la detección local de palabra de activación y la cancelación de eco acústico (AEC) a nivel de hardware.** Abre este WebSocket y empieza a transmitir después de que se detecte la palabra de activación — esto es lo que hace que "el usuario pueda interrumpir mientras la IA habla" funcione bien; el AEC lo gestiona el front-end de audio de ESP-SR, sin trabajo extra en el servidor o la capa de aplicación.

### Síntesis de voz

`POST /api/v1/audio/tts/stream`, cuerpo:

```json
{ "input": { "text": "texto a sintetizar" }, "voice": "longxiaochun" }
```

La respuesta es un stream de PCM crudo — revisa la cabecera `Content-Type` (`audio/L16; rate=<N>; channels=1`) para saber la frecuencia de muestreo real, que depende del proveedor (DashScope: 22050Hz, OpenAI: 24000Hz). Pásalo directamente a tu salida de audio (p. ej. la reproducción I2S del ESP32) a medida que lo recibes, sin necesidad de decodificar. El valor de `voice` también es específico de cada proveedor — las voces de CosyVoice de DashScope frente a las de OpenAI (`alloy`, `echo`, `fable`...).

### Chat

`POST /api/v1/chat/completions`, formato compatible con OpenAI (un array `messages`), soporta `"stream": true` para salida token a token. Conecta los tokens en streaming directamente al TTS y tienes un bucle completo de "escuchar → pensar → hablar".

## Soporte de idiomas

La cobertura de idiomas depende completamente del proveedor que use cada capacidad — la pasarela en sí no restringe nada.

| Capacidad | Cómo se controla el idioma | Cobertura de DashScope | Cobertura de OpenAI |
|---|---|---|---|
| `chat/completions` | Sin restricción en ningún proveedor — el modelo responde en el idioma que uses en el prompt | Chino, inglés, japonés, coreano, francés, alemán, español y otros idiomas principales funcionan bien | Igual — cobertura multilingüe amplia |
| `asr` (transcripción de grabación) | DashScope usa el campo `language_hints` del cuerpo de la petición; OpenAI Whisper detecta automáticamente o acepta un único código de idioma | Chino (incl. dialectos como el cantonés), inglés, japonés, coreano — ver la [documentación de paraformer-v2](https://help.aliyun.com/zh/model-studio/paraformer-speech-recognition) | Whisper cubre más de 50 idiomas, incluidos español, francés, alemán y la mayoría de los que DashScope no cubre — si necesitas amplia cobertura de idiomas europeos, `ASR_PROVIDER=openai` es el camino más sencillo |
| `asr/stream` (reconocimiento en tiempo real) | Depende de lo que soporte el proveedor al que apunte `REALTIME_ASR_WS_URL` | Cobertura de idiomas de paraformer-realtime-v2 (igual que arriba) | No soportado directamente por defecto — apunta `REALTIME_ASR_WS_URL` a un endpoint en tiempo real compatible con OpenAI si lo necesitas |
| `audio/tts` / `/tts/stream` | Parámetro `voice`, lista de voces específica de cada proveedor | Chino (incl. voces con acentos regionales) e inglés son la cobertura principal — ver la [lista de voces de CosyVoice](https://help.aliyun.com/zh/model-studio/cosyvoice-speech-synthesis) | Las voces de OpenAI son principalmente en inglés, pero producen resultados razonables en muchos otros idiomas también |

**En resumen**: si tu hardware está dirigido a usuarios que hablan idiomas europeos, `ASR_PROVIDER=openai` y `TTS_PROVIDER=openai` te llevarán ahí más rápido que intentar forzarlo con DashScope. El chat funciona bien con cualquiera de los dos.

Todos los textos del lado del servidor (mensajes de error, logs, etc.) están en inglés por defecto, sin capa de i18n — la pasarela solo devuelve el texto original en el campo `detail`. Si tu cliente está dirigido a usuarios que no hablan inglés, traduce en el lado del cliente. Se aceptan PRs que añadan i18n para los mensajes del lado del servidor.

## Estructura del proyecto

```
app/
├── config.py           # Configuración de variables de entorno, selección de proveedor
├── database.py         # Modelo User + conexión a base de datos
├── security.py         # Hashing de contraseñas, JWT
├── ratelimit.py         # Límite de tasa
├── main.py              # Punto de entrada de FastAPI
├── providers/
│   ├── chat.py           # Resolución del endpoint de chat (compatible con OpenAI)
│   ├── tts.py             # Adaptadores de proveedores de TTS (DashScope, OpenAI)
│   └── asr.py             # Adaptadores de proveedores de ASR de archivo (DashScope, OpenAI)
└── routers/
    ├── auth.py            # Registro/login/gestión de cuenta
    ├── proxy.py            # Núcleo: proxy de chat/ASR/TTS, distribuye a los proveedores
    └── health.py
```

## Licencia

[PolyForm Noncommercial 1.0.0](LICENSE) — de código abierto, libre de usar, modificar y distribuir para cualquier fin no comercial (proyectos personales, investigación, hardware de aficionado, etc.). El uso comercial requiere una licencia separada del titular de los derechos.
