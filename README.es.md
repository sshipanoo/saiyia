# saiyia

**[English](README.md) | [Español](README.es.md) | [中文](README.zh.md) | [日本語](README.ja.md)**

Un servidor de pasarela (gateway) de IA de voz de código abierto.

Hace una sola cosa: dar a cualquier cliente que pueda abrir un WebSocket o hacer una petición HTTP (teléfono, web, hardware tipo ESP32) un sistema de cuentas unificado más un proxy para chat de IA / reconocimiento de voz / síntesis de voz, sin que ese cliente tenga que guardar nunca la clave de un proveedor de IA externo. Encaja bien en proyectos de hardware — por ejemplo, un robot de compañía con micrófono y altavoz.

## Decisiones de diseño

Se mantiene deliberadamente minimalista — solo hace "cuentas + proxy de capacidades de IA": sin sistema de pago/suscripción (el modelo `User` no tiene ningún campo de suscripción, todas las cuentas son iguales, solo con límite de tasa), sin sincronización de datos multi-dispositivo, sin panel de administración. Si cobrar, cómo cobrar, si guardar el historial de conversaciones — todo eso se deja para que tú lo decidas e implementes por encima.

## Endpoints disponibles

| Endpoint | Descripción |
|---|---|
| `POST /api/v1/auth/register` `/login` `/me` `/logout` `/change-password` `/delete-account` | Sistema de cuentas, autenticación JWT, el mecanismo `token_version` permite revocar instantáneamente los tokens antiguos al cerrar sesión |
| `POST /api/v1/chat/completions` | Redirige a Alibaba Cloud Model Studio (DashScope) para completar el chat (formato compatible con OpenAI, con soporte de streaming) |
| `POST /api/v1/audio/tts` | Síntesis de voz de una sola vez, devuelve un MP3 completo |
| `POST /api/v1/audio/tts/stream` | Síntesis de voz en streaming, emite PCM crudo (16-bit/mono/22050Hz) a medida que se genera, baja latencia del primer byte, ideal para reproducir mientras se recibe |
| `POST /api/v1/asr` | Transcripción de grabación completa (soporte nativo de diarización multi-hablante) |
| `WS /api/v1/asr/stream` | Retransmisión de reconocimiento de voz en tiempo real, el texto llega mientras hablas |

## Inicio rápido

```bash
cp .env.example .env   # rellena SECRET_KEY, ALIBABA_API_KEY, DB_PASSWORD
docker compose up -d --build
curl http://localhost:8000/api/v1/health
```

## Guía de integración con hardware (p. ej. ESP32)

### Autenticación

Primero llama a `/api/v1/auth/register` o `/login` para obtener un JWT, luego envíalo como `Authorization: Bearer <token>` en cada petición.

Si tu cliente WebSocket no puede establecer cabeceras personalizadas en el handshake (esto ocurre con el WebSocket nativo del navegador), puedes pasar el token como query string: `wss://.../asr/stream?token=xxx` — el servidor acepta ambas formas.

### Protocolo de reconocimiento de voz en tiempo real

`WS /api/v1/asr/stream` es una **retransmisión transparente** del protocolo de reconocimiento de voz en tiempo real de Alibaba Cloud DashScope (paraformer-realtime-v2) — el servidor solo gestiona la autenticación y el reenvío, no toca el contenido de los mensajes. Tras conectar:

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

**En ESP32 recomendamos usar [ESP-SR](https://github.com/espressif/esp-sr) para la detección local de palabra de activación y la cancelación de eco acústico (AEC) a nivel de hardware.** Abre este WebSocket y empieza a transmitir después de que se detecte la palabra de activación — esto es lo que hace que "el usuario pueda interrumpir mientras la IA habla" funcione bien; el AEC lo gestiona el front-end de audio de ESP-SR, sin trabajo extra en el servidor o la capa de aplicación.

### Síntesis de voz

`POST /api/v1/audio/tts/stream`, cuerpo:

```json
{ "input": { "text": "texto a sintetizar" }, "voice": "longxiaochun" }
```

La respuesta es un stream de PCM crudo con `Content-Type: audio/L16; rate=22050; channels=1` — pásalo directamente a tu salida de audio (p. ej. la reproducción I2S del ESP32) a medida que lo recibes, sin necesidad de decodificar.

### Chat

`POST /api/v1/chat/completions`, formato compatible con OpenAI (un array `messages`), soporta `"stream": true` para salida token a token. Conecta los tokens en streaming directamente al TTS y tienes un bucle completo de "escuchar → pensar → hablar".

## Soporte de idiomas

La pasarela en sí no te limita a ningún idioma — el límite lo marcan los modelos de DashScope a los que hace proxy. **Algo que conviene aclarar de antemano**: los modelos de voz de DashScope (reconocimiento/síntesis) están pensados principalmente para chino e idiomas asiáticos — esto no significa que "cualquier idioma principal esté soportado". Idiomas europeos como español, francés o alemán actualmente no están dentro de la cobertura principal de paraformer / CosyVoice en el lado de voz; pruébalo en el Playground oficial antes de confiar en ello. El chat de texto no tiene esta limitación — cualquier idioma funciona ahí.

| Capacidad | Cómo se controla el idioma | Idiomas principales conocidos como soportados |
|---|---|---|
| `chat/completions` | Sin restricción de idioma — el modelo entiende y responde en el idioma que uses en el prompt, sin configuración adicional | Chino, inglés, japonés, coreano, francés, alemán, español y otros idiomas principales funcionan bien en el chat (esta es la capacidad lingüística general del LLM, algo distinto de los modelos de voz específicos de abajo) |
| `asr` (transcripción de grabación completa) | Controlado por el campo `language_hints` en el cuerpo de la petición, por defecto `["zh", "en"]`; pasa otros códigos de idioma como pistas de reconocimiento | Chino (incluidos dialectos como el cantonés), inglés, japonés, coreano — consulta la [documentación de paraformer-v2](https://help.aliyun.com/zh/model-studio/paraformer-speech-recognition) para la lista actualizada de idiomas soportados |
| `asr/stream` (reconocimiento en tiempo real) | Retransmisión transparente — el idioma/modelo depende enteramente de lo que el cliente especifique en los `parameters` del mensaje `run-task`; la pasarela no restringe ni reescribe nada | Igual que arriba (paraformer-realtime-v2) |
| `audio/tts` / `/tts/stream` | Determinado por el parámetro `voice` en el cuerpo de la petición — diferentes voces corresponden a diferentes idiomas/acentos | Chino (incluidas voces con acentos regionales) e inglés son la cobertura principal; las voces en japonés/coreano varían — consulta la [lista de voces de CosyVoice](https://help.aliyun.com/zh/model-studio/cosyvoice-speech-synthesis) para ver lo disponible actualmente |

Si tu hardware está dirigido a usuarios que hablan idiomas europeos, sustituye las partes de reconocimiento/síntesis de voz por otro proveedor (la capa de proxy es intercambiable — basta con apuntar las funciones correspondientes en `proxy.py` a otra API; el sistema de cuentas y la arquitectura general no necesitan cambiar). El chat no se ve afectado y funciona tal cual.

Los textos de la interfaz (mensajes de error, etc.) están actualmente escritos directamente en chino — todavía no se ha hecho la internacionalización (i18n). Si tu cliente está dirigido a usuarios que no hablan chino, te sugerimos traducir en el lado del cliente por ahora; la pasarela solo devuelve el texto original en el campo `detail`, lo cual no afecta a la funcionalidad. Se aceptan PRs que añadan i18n para los mensajes de error del lado del servidor.

## Estructura del proyecto

```
app/
├── config.py       # Configuración de variables de entorno
├── database.py     # Modelo User + conexión a base de datos
├── security.py     # Hashing de contraseñas, JWT
├── ratelimit.py     # Límite de tasa
├── main.py          # Punto de entrada de FastAPI
└── routers/
    ├── auth.py       # Registro/login/gestión de cuenta
    ├── proxy.py       # Núcleo: proxy de chat/ASR/TTS
    └── health.py
```

## Licencia

[PolyForm Noncommercial 1.0.0](LICENSE) — de código abierto, libre de usar, modificar y distribuir para cualquier fin no comercial (proyectos personales, investigación, hardware de aficionado, etc.). El uso comercial requiere una licencia separada del titular de los derechos.
