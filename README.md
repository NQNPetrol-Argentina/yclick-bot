# Bot Y-Click (rutina de Claude)

Contenido:
- `PROMPT.md` — texto para pegar como prompt de la tarea en https://claude.ai/code/routines
- `docs/` — documentos de pasos y de API (el prompt los lee)
- `scripts/wells_api.py` — cliente de Wells con firma HMAC (probado contra los vectores del documento: `python3 -I scripts/wells_api.py selftest`)

## Checklist de configuración
1. **Repositorio.** Subí esta carpeta a un repositorio de GitHub y adjuntalo a la tarea para que cada sesión tenga `docs/` y `scripts/`. Si no, pegá el contenido de los dos `.md` al final del prompt y creá `wells_api.py` desde el prompt.
2. **Entorno (Cloud environment)**, variables de entorno:
   - `USUARIO_YCLICK`, `CONTRASENA_YCLICK` (ya cargadas)
   - `TOKEN_WELLS` (token de `php artisan mcp:create-token <userId> yclick_bot`, ability `yclick:bot`)
   - `YCLICK_CALLBACK_SECRET` (el mismo que tiene Wells)
   - Cualquiera que use ese entorno puede leerlas: usá un entorno dedicado.
3. **Network access = Custom**, dominios permitidos: `yclick.ypf.com`, `yclick-home.ypf.com`, `wells.nqnpetrol.com`. Si el login de YPF usa otros dominios (SSO, CDN), se agregan cuando aparezca el bloqueo.
4. **Pegar `PROMPT.md`** en la tarea.
5. **Cómo llega el webhook de Wells.** Hay un problema que resolver (ver abajo).

## Importante: Wells no puede llamar directo a la URL de disparo
El endpoint de Anthropic exige las cabeceras `anthropic-version` y `anthropic-beta`, y espera el dato en un campo `{"text": "..."}`. El webhook de Wells manda `{"evento","sync_id","api_base","version_contrato"}` con solo `Authorization: Bearer <key>` (y, si hay secreto, las cabeceras HMAC). Sin cambios en Wells, el disparo falla con 400 o llega sin el `sync_id`.

Dos salidas:
- **A. Adaptar Wells** para mandar esas cabeceras y `{"text": "<json con sync_id>"}`. El token de la rutina quedaría guardado en Wells como `YCLICK_WEBHOOK_KEY`.
- **B. Un adaptador intermedio** (recomendado: el token de la rutina no sale del adaptador y solo reenvía el `sync_id`). Ejemplo de Cloudflare Worker, **sin probar**:

```js
export default {
  async fetch(req, env) {
    if (req.method !== 'POST') return new Response('method', { status: 405 });
    if (req.headers.get('Authorization') !== `Bearer ${env.YCLICK_WEBHOOK_KEY}`)
      return new Response('unauthorized', { status: 401 });
    let sync_id;
    try { ({ sync_id } = await req.json()); } catch { return new Response('bad json', { status: 400 }); }
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(sync_id || ''))
      return new Response('bad sync_id', { status: 422 });
    const r = await fetch(env.FIRE_URL, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${env.ROUTINE_TOKEN}`,
        'anthropic-version': '2023-06-01',
        'anthropic-beta': 'experimental-cc-routine-2026-04-01',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ text: JSON.stringify({ sync_id }) }),
    });
    return new Response(null, { status: r.ok ? 202 : 502 });
  },
};
```
Secretos del Worker: `YCLICK_WEBHOOK_KEY` (una key nueva que se pone también en Wells), `ROUTINE_TOKEN` (el token de la rutina) y `FIRE_URL` (la Fire URL). En Wells, `YCLICK_WEBHOOK_URL` apunta al Worker.

## Riesgos y cosas a confirmar antes de usar datos reales
- **Cargas irreversibles.** "Enviar a aprobar" no se deshace y el bot no rectifica ni elimina. Probá primero con un sync de 1 o 2 celdas conocidas.
- **Disparos duplicados.** Si Wells reintenta el webhook o dispara dos veces, habría dos sesiones sobre el mismo sync. `ya_existia` evita pisar cantidades, pero no garantiza que no se pisen entre ellas en simultáneo.
- **Duración.** Un sync de ~44 celdas puede tardar mucho. El límite de tiempo de una sesión de rutina no está documentado. Si se corta, el resultado final nunca llega y el sync queda `sin_respuesta`. Conviene que Wells divida los syncs.
- **Tope por celda.** El documento pide un tope pero no da valores. El prompt usa 6 minutos y 60 pasos: confirmá o cambiá esos números.
- **Logs.** Cada sesión es efímera. El log de `logs/` queda en la sesión, no en el repo, salvo que la rutina haga commit y push (no está habilitado en el prompt).
- **Pendientes del documento de pasos** (sección 7, marcados con ⚠️): plantas, avioncito, botones del modal. El bot debe informar `error` en esos casos, no adivinar.
