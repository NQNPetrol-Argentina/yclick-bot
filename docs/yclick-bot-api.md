# Y-Click — API entre Wells y el bot (v1)

Este documento es el contrato técnico para quien arma el bot. El contrato de **datos** (qué carga, en qué pantalla de Y-Click y qué devuelve) está en `docs/yclick-bot-pasos.md`, secciones 2 y 5.

> **Credenciales de Y-Click:** nunca pasan por Wells. El bot las guarda en su almacén de secretos. Wells no las recibe, no las guarda ni las loguea.

---

## 1. Flujo

1. Wells crea el sync y lo dispara: envía al bot un **webhook autenticado** (con la key del bot como Bearer y, si hay secreto, también firmado) con **solo el `sync_id`**.
2. El bot pide el detalle: `GET /api/yclick-bot/syncs/{sync_id}`. Recibe las celdas y, por cada adjunto, una **URL firmada de corta vida** para bajar el PDF del parte.
3. Por **cada celda procesada**, el bot informa avance: `POST …/progreso`. Es obligatorio: una carga enviada a Y-Click solo se puede rectificar en su cantidad o eliminar, y si el bot se cae, Wells tiene que saber qué quedó cargado.
4. Al terminar, el bot manda el resultado final: `POST …/resultado`.

Si pasan más de `YCLICK_SYNC_TIMEOUT_MINUTOS` (90 por defecto) sin avance, Wells marca el sync **Sin respuesta**. El plazo se cuenta desde el último `progreso` (o desde el inicio si nunca hubo). Un resultado que llega tarde **sí se acepta**.

---

## 2. Autenticación

### 2.1 Token (todos los endpoints salvo el PDF)

Header `Authorization: Bearer <token>`. El token se crea en Wells con:

```bash
php artisan mcp:create-token <userId> yclick_bot
```

El token tiene una sola ability, `yclick:bot` (exclusiva de este bot), y vence al año.

| Situación | Respuesta |
|---|---|
| Sin token, token inválido o vencido | `401` |
| Token sin la ability `yclick:bot` | `403` |
| Más de 60 pedidos por minuto | `429` |

### 2.2 Firma HMAC (webhook y callbacks)

Los dos sentidos usan el mismo esquema, con **secretos distintos**:

| Sentido | Secreto |
|---|---|
| Wells → bot (webhook) | `YCLICK_WEBHOOK_SECRET` (opcional: sin secreto, el webhook va sin firma y se autentica solo con la key, ver sección 3) |
| Bot → Wells (`progreso`, `resultado`) | `YCLICK_CALLBACK_SECRET` |

Cabeceras:

- `X-Webhook-Timestamp`: segundos Unix (UTC) en el momento de firmar.
- `X-Webhook-Signature-V2`: `hex( HMAC-SHA256( secreto, "<timestamp>.<cuerpo>" ) )`.

`<cuerpo>` son **los bytes exactos del cuerpo HTTP**. Se firma el string que se envía; no se vuelve a serializar el JSON.

**Ejemplo (webhook).** Secreto `secreto-de-ejemplo`, timestamp `1793900000` y este cuerpo, en una sola línea y sin espacios extra:

```
{"evento":"yclick.sync.creado","sync_id":"6f1c2a0e-3b7d-4c55-9a41-0d2e8b7a1f33","api_base":"https://wells.example.com/api/yclick-bot","version_contrato":1}
```

El texto firmado es `1793900000.` seguido del cuerpo, y la firma es:

```
1993556a23b3777b8799f4887bea2b4a651f90db8453867502d1d441d8c8ac15
```

En PHP: `hash_hmac('sha256', $timestamp.'.'.$cuerpo, $secreto)`. En Python: `hmac.new(secreto.encode(), f"{timestamp}.{cuerpo}".encode(), hashlib.sha256).hexdigest()`. En shell:

```bash
printf '%s' "1793900000.${CUERPO}" | openssl dgst -sha256 -hmac 'secreto-de-ejemplo'
```

**Ejemplo (callback `progreso`).** Mismo secreto y timestamp, cuerpo:

```
{"celdas":[{"celda_id":"0b6d7f0e-52f4-4b8e-8f3a-9c1d2e3f4a5b","resultado":"ok"}]}
```

Firma:

```
6109a89aeaf27966c06fb51612482893edd7bf35851a72929675b745baa0bb7f
```

**Cómo validar el webhook que manda Wells:** recalcular la firma con el secreto y los bytes recibidos, comparar en tiempo constante y rechazar si el timestamp difiere más de 5 minutos del reloj del bot.

**Cómo valida Wells los callbacks:** responde `401` si falta una cabecera, si la firma no coincide o si el timestamp difiere más de 300 segundos.

---

## 3. Webhook Wells → bot

`POST <YCLICK_WEBHOOK_URL>`. Hace falta la URL y al menos una de estas dos formas de autenticar, que se pueden usar juntas:

- **Key del bot** (`YCLICK_WEBHOOK_KEY`): el pedido lleva `Authorization: Bearer <key>`. Es lo que usa el bot de Rodial.
- **Firma HMAC** (`YCLICK_WEBHOOK_SECRET`): el pedido lleva `X-Webhook-Timestamp` y `X-Webhook-Signature-V2` (sección 2.2). Si el secreto está vacío, esas dos cabeceras no se mandan.

Si no hay ni key ni secreto, o falta la URL, el webhook no se envía y el sync queda en «No se pudo completar». Cuerpo:

```json
{ "evento": "yclick.sync.creado", "sync_id": "uuid", "api_base": "https://<wells>/api/yclick-bot", "version_contrato": 1 }
```

- Cualquier respuesta `2xx` cuenta como recibido.
- Una respuesta no `2xx` o un error de red se reintenta hasta 3 veces (esperas de 30, 120 y 300 segundos).
- Si tras el último intento no se pudo entregar, el sync queda **No se pudo completar** con todas sus celdas en `error` y motivo `webhook_fallido`. En ese caso se sabe que no se cargó nada.
- El bot debe responder rápido y trabajar en segundo plano; no hace falta devolver ningún cuerpo.

---

## 4. Endpoints

Base: `<api_base>` (`/api/yclick-bot`).

### 4.1 `GET /syncs/{sync_id}` — detalle del sync

Requiere token. Responde `200`:

```json
{
  "sync_id": "uuid",
  "cuenta": "rodial-yclick",
  "estado": "en_proceso",
  "solicitud": { "periodo": "2026-11", "numero": "MSDS0000011770" },
  "celdas": [
    {
      "celda_id": "uuid",
      "tipo_ubicacion": "Pozo - Operación y Mantenimiento",
      "ubicacion": "SOil-402(h)",
      "posicion": "25 - NQG-SRV.DESPARAF. SUPER HEAT SAB/DOM/FER",
      "linea": "10 - EQ.HOT OIL DESPARAFINACION (DIA)",
      "fecha": "2026-11-01",
      "cantidad": 4,
      "unidad": "DIA",
      "doc_respaldo": "SH",
      "adjuntos": [
        { "parte": 23359, "archivo": "parte-23359.pdf", "url_firmada": "https://…" }
      ]
    }
  ]
}
```

- `cuenta` es solo el **nombre** de la cuenta que el bot busca en su almacén de secretos.
- Las `url_firmada` se generan en cada consulta y vencen a los `YCLICK_PDF_URL_MINUTOS` (120 por defecto). Ante un reintento, volver a pedir el detalle para tener URLs vigentes.
- Si el sync ya terminó, igual devuelve el payload con su `estado`; el bot decide qué hacer.
- `404` si el sync no existe.

### 4.2 `GET <url_firmada>` — PDF del parte

**Sin token:** la firma de la URL es la credencial. No modificar la URL.

| Respuesta | Significado | Qué hace el bot |
|---|---|---|
| `200` `application/pdf` | El PDF (`Content-Disposition: parte-<numero>.pdf`) | Adjuntarlo |
| `403` | Firma inválida o vencida | Pedir el detalle de nuevo para obtener URLs vigentes |
| `404` | El parte no figura en ninguna celda del sync | Celda en `error` con el detalle |
| `409` | El parte ya no está validado | Celda en `error`; no cargar |
| `503` | No se pudo generar el PDF (por ejemplo, falla SCADA) | Celda en `error`; **no cargarla sin adjunto** |

El PDF se genera en el primer pedido (puede tardar unos segundos) y desde ahí queda guardado para ese sync: los pedidos siguientes devuelven el mismo archivo. Se conserva `YCLICK_PDF_RETENCION_DIAS` (30) días desde que inició el sync.

### 4.3 `POST /syncs/{sync_id}/progreso` — avance

Requiere token **y firma HMAC** (`YCLICK_CALLBACK_SECRET`). Enviar **al menos una vez por celda procesada**: además de guardar la celda, renueva el latido que evita que el sync venza.

```json
{ "celdas": [ { "celda_id": "uuid", "resultado": "ok", "motivo": null, "detalle": null, "captura_url": null } ] }
```

- `resultado`: `ok`, `ya_existia` o `error`.
- `motivo` (máx. 100), `detalle` (máx. 5000) y `captura_url` (`http(s)://…`, máx. 2000) son opcionales.
- Una celda que ya tiene resultado **no se pisa**. Un `celda_id` desconocido se ignora.
- Un sync ya finalizado no cambia: responde `200` con su estado.
- Sobre un sync `sin_respuesta` renueva el latido pero no cambia su estado; lo resuelve el resultado final.

Responde `200`:

```json
{ "estado": "en_proceso", "procesadas": 12, "total": 44 }
```

### 4.4 `POST /syncs/{sync_id}/resultado` — resultado final

Requiere token **y firma HMAC**. Formato de la sección 5 de `docs/yclick-bot-pasos.md`, más un `mensaje` opcional con el detalle general:

```json
{
  "estado": "completado | parcial | fallido",
  "mensaje": "Texto libre opcional",
  "resumen": { "ok": 41, "ya_existia": 2, "error": 1 },
  "celdas": [
    { "celda_id": "uuid", "resultado": "ok | ya_existia | error", "motivo": "valor_no_coincide", "detalle": "…", "captura_url": "https://…" }
  ]
}
```

- Wells recalcula el resumen y el estado a partir de las celdas. Una celda que el resultado no menciona queda en `error` con motivo `sin_resultado`.
- Estado final: `completado` si todas las celdas quedaron `ok` o `ya_existia`; `completado_con_observaciones` si hay al menos una de esas y al menos un `error`; `no_se_pudo_completar` si todas son `error` o el estado enviado es `fallido`.
- Es **idempotente**: repetirlo sobre un sync ya finalizado no cambia nada. Sobre un sync `sin_respuesta` sí se acepta (el bot terminó tarde).

Responde `200`: `{ "estado": "completado" }`.

---

## 5. Errores comunes

| Código | Causa |
|---|---|
| `401` | Falta el token, es inválido o venció; o la firma HMAC falta, es incorrecta o está fuera de tolerancia |
| `403` | Falta la ability `yclick:bot`; o la URL firmada del PDF es inválida o venció |
| `404` | Sync inexistente; o parte que no figura en el sync |
| `409` | Parte desvalidado |
| `422` | Cuerpo inválido (`estado` o `resultado` fuera de los permitidos, `celda_id` que no es uuid, textos demasiado largos) |
| `429` | Más de 60 pedidos por minuto |
| `503` | No se pudo generar el PDF |

Para cualquier error que impida cargar una celda, el bot la informa como `error` con el texto exacto en `detalle` y sigue con las demás cuando sean independientes.
