# Y-Click — pasos de carga para el bot (v1, armado con el video del parte 23359)

> **Estado:** recorrido completo visto en video (carga del parte 23359, pozo SOil-402(h), domingo 01/11/2026, equipo HO29). Lo marcado con **⚠️** no se vio o hay que confirmarlo con Fabio.
> Este documento es la base de las instrucciones del bot. Wells le da los datos; el bot no decide nada.

---

## 1. Reglas que el bot no puede romper

1. **No inferir datos.** Carga solo lo que viene en el payload. Si falta algo, ese ítem se informa como error y no se carga.
2. **Leer antes de enviar.** Antes de pulsar «Enviar a aprobar», lee en pantalla lo cargado (ubicación, posición, línea, día, cantidad) y lo compara con el payload. Si no coincide, **no envía** y lo informa.
3. **Una carga enviada solo admite dos cambios en Y-Click:** «Rectificar cantidad» (menú de tres puntos ⋮ de la fila → «Rectificar cantidad»; cambia solo la cantidad; la posición, la línea y la fecha no se pueden editar) o eliminarla. En esta versión el bot **no rectifica ni elimina**: si en una celda (ubicación + posición + línea + día) ya hay una cantidad, informa `ya_existia` junto con el valor que encontró, y sigue. No sobrescribe. Rectificar queda para una versión posterior.
4. **El bot no elimina cargas.** Eliminar es decisión de Fabio.
5. **Ante una pantalla inesperada**, se detiene en ese ítem, saca captura, informa `error` con el detalle y sigue con el próximo ítem solo si es independiente.
6. **Las credenciales** salen del almacén de secretos del bot, nunca del payload ni de los logs.
7. **Tope por ítem:** máximo de pasos y de minutos. Al pasarse, `error` con motivo `tope_excedido`.
8. **Informar avance** a Wells después de cada celda (`POST .../progreso`): una carga enviada no se puede deshacer y Wells tiene que saber qué quedó cargado si el bot se cae.

---

## 2. Datos que recibe (contrato)

El webhook de Wells trae solo el `sync_id`. El bot pide el detalle a Wells con su token (ver `docs/yclick-bot-api.md` cuando exista, v1.232.0).

```json
{
  "sync_id": "uuid",
  "cuenta": "rodial-yclick",
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

- La unidad de trabajo es la **celda**: ubicación + posición + línea + día. Dos partes del mismo pozo y día con la misma posición y línea son una celda, con las cantidades sumadas y dos PDFs.
- Cada tipo de hora es una posición distinta y por lo tanto una celda distinta.
- **El texto de la línea cambia con la posición**: termina en `(H)` para posiciones por hora y en `(DIA)` para las medidas por día. Wells manda el texto exacto.
- `ubicacion` es el nombre del pozo **sin** el prefijo `YPF.Nq.` (`YPF.Nq.SOil-402(h)` en Wells es `SOil-402(h)` en Y-Click). Las mayúsculas del paréntesis varían, así que la comparación no distingue mayúsculas.
- `doc_respaldo` es el nombre de la unidad de contrato del parte (`SH`, `LC1`, `NQG`, …).

---

## 3. Pasos

### Paso 0 — Ingresar
1. Abrir `https://yclick.ypf.com/`. Redirige a `yclick-home.ypf.com/#/login`.
2. Completar **Usuario** y **Contraseña** y pulsar **Ingresar**.
3. Aparece «Gestión de Servicios» y luego se redirige a `yclick.ypf.com/?ispr=RODIAL+S.A.`.
4. Si aparece cualquier cosa distinta (captcha, código, bloqueo, cambio de contraseña): detener todo el sync con `login_bloqueado`, captura y mensaje.

### Paso 1 — Abrir la solicitud del mes
1. Está la **Bandeja de Solicitudes**, con las pestañas PENDIENTES, **EN CURSO** y FINALIZADAS. Usar **EN CURSO**.
2. La tabla tiene: N°, Tipo OI, Servicio, Ubicación / Contrato, Fecha prob. realización y Estado.
3. Elegir la fila con **Servicio = «OYM - Desparafinación»**, **Ubicación / Contrato = «RODIAL - 4900111242»** y **Fecha prob. realización = el mes del sync** (ej. `NOV/2026`), con estado «En Ejecución». Hay una solicitud por mes (ej. `MSDS0000011770` para NOV/2026).
4. Entrar con clic en el N°. Abre `yclick.ypf.com/detailmonthly/<id>`. El `<id>` interno **no coincide** con el número de la solicitud (11780 vs. MSDS0000011770), así que no se arma la URL a mano.
5. Verificar el título «Solicitud de servicio N° …» y «Fecha de realización: NOVIEMBRE 2026».
6. Si no hay exactamente una solicitud que coincida: `solicitud_no_encontrada` y se detiene el sync.

### Paso 2 — Pestaña PARTE DE TRABAJO
La carga se hace en **PARTE DE TRABAJO**. Botones de la barra: *Ordenar por Ubicación*, *Ordenar por Posición y Línea*, *Carga simplificada*, *Descargar parte*, **Agregar ubicación**.

La lista de abajo muestra los bloques «Ubicación» ya cargados (ej. `LLL-1494(H) LOMA CAMPANA OyM`).

### Paso 3 — Ubicación (el pozo)
1. Si el pozo del payload ya está en la lista, usarlo.
2. Si no: **Agregar ubicación**. En el modal:
   - **Tipo de ubicación:** elegir `Pozo - Operación y Mantenimiento` (valor `tipo_ubicacion` del payload).
   - **Ubicación pozo:** escribir el nombre del pozo (`SOil-402`) y elegir la opción del resultado (`SOil-402(h)`). Se acepta la opción cuyo nombre sea igual al del payload sin distinguir mayúsculas, o la única que lo contenga seguido de un sufijo entre paréntesis (`(h)`, `(H)`). Si no hay una sola opción clara: `ubicacion_no_encontrada` o `ubicacion_ambigua`. El nombre exacto elegido se informa a Wells en el resultado de la celda.
   - **Unidad de Negocio** y **Activo** se completan solos al elegir el pozo (en el video: LOMA CAMPANA). Verificar que se completaron; no escribirlos.
   - **Equipo** queda vacío.
   - Confirmar con el botón de agregar. Aparece el aviso «La ubicación se agregó correctamente».
3. ⚠️ Los botones del pie del modal («Agregar» y «Agregar y posicionar») se vieron deshabilitados; falta confirmar cuál se usa y qué hace el segundo.
4. ⚠️ Para partes que **no** son de un pozo (por ejemplo plantas), el tipo de ubicación será otro; no se vio. Hasta verlo, el bot informa `ubicacion_no_soportada`.

### Paso 4 — Posición y línea
1. Marcar la casilla de la ubicación y expandirla. Si no tiene líneas aparece «Esta ubicación no tiene líneas cargadas» y el botón **Agregar líneas** (también existe «Agregar posición y línea a ubicaciones» arriba de la lista).
2. En el modal «Agregar posición y línea a ubicaciones»:
   - **Posición:** elegir la del payload (ej. `25 - NQG-SRV.DESPARAF. SUPER HEAT SAB/DOM/FER`).
   - **Línea:** se habilita al elegir la posición; elegir la opción cuyo **código** (`10`) coincide con el del payload (ej. `10 - EQ.HOT OIL DESPARAFINACION (DIA)`). El texto completo depende de la unidad de la posición y no hace falta que venga en el payload; el bot informa el que eligió.
   - **Realización de la tarea:** viene `01/11/2026 – 30/11/2026` (el mes entero). **Cambiar Desde y Hasta al día de la celda** (ambos `01/11/2026`).
   - Pulsar **Aceptar**.
3. Si la posición o la línea no existen en las listas: `posicion_no_encontrada` o `linea_no_encontrada`. No elegir otra parecida.

### Paso 5 — Cargar la cantidad
1. Dentro de la ubicación hay un navegador de día («Martes, 03 de Noviembre de 2026», con flechas «Día anterior» y siguiente). Va al día de la celda. Mientras el día elegido no tenga líneas, dice «No hay líneas para la fecha seleccionada».
2. La fila del día muestra: posición, línea, unidad (`H`, `DIA`, `KM`), una caja de texto y los íconos (código de barras, avioncito, documento, comentarios y menú).
3. Escribir la cantidad en la caja. Aparece el estado «Pendiente» y el botón **Enviar a aprobar** se activa (azul).
4. Si la caja ya tenía un valor, **no tocarla**: `ya_existia`.
5. Decimales con punto. Leer la caja y confirmar que muestra exactamente el valor.

### Paso 6 — Enviar
1. Verificar (regla 2).
2. Pulsar **Enviar a aprobar** (abajo a la derecha del bloque de la ubicación). Tras enviar, el valor queda en **verde**, el botón se deshabilita y se habilitan los íconos de código de barras y de documento.
3. ⚠️ El avioncito de la fila parece hacer lo mismo para una sola línea; en el video se usó el botón de abajo. Usar ese.
4. **Sin enviar no se guardan las horas ni se pueden adjuntar archivos.** Por eso este paso va antes de los siguientes.

### Paso 7 — Documento de respaldo (opcional, recomendado)
1. Ícono de **código de barras** de la fila. Abre «Identificador de Documento de Respaldo» con la ubicación, la posición y la línea.
2. **Documento de Respaldo:** `doc_respaldo` del payload (hasta 30 caracteres; ej. `SH`).
3. El rango Desde / Hasta viene con el día; dejarlo.
4. **Guardar** y esperar «Se guardó correctamente los identificadores de documento de respaldo».
5. Es opcional (a Fabio le sirve para filtrar). Se hace por defecto; si el payload no trae `doc_respaldo`, se omite.

### Paso 8 — Adjuntar el PDF del parte
1. Descargar el PDF desde la `url_firmada` a un archivo local del bot.
2. Ícono de **documento** de la fila. Abre «Archivos adjuntos — Para el día …» con dos secciones:
   - «Archivos de la solicitud» → **no usar**.
   - `Archivos "25 - … 10 - … (DIA) DIA 4"` → **esta es la correcta**: el PDF va asociado a las horas, no a la solicitud. El texto termina en la unidad y la cantidad.
3. **Agregar adjunto** abre el selector de archivos del sistema. Cargar el PDF directamente en el campo de archivo de la página, sin interactuar con la ventana del sistema.
4. El archivo aparece en la lista (nombre, tamaño, fecha de carga, usuario). **Pulsar «Agregar»** (azul) y esperar el aviso «Adjuntando archivos, por favor espere.».
5. Verificar que el archivo sigue en la lista, **Salir**.
6. Si hay más de un PDF en la celda, repetir 3 a 5.
7. No hay límite de tamaño (confirmado por Rodial).

### Paso 9 — Resultado
Por cada celda: `ok`, `ya_existia` o `error`. Mandar el progreso a Wells después de cada una y el resumen al final.

---

## 4. Errores previstos

| Situación | Qué hace el bot |
|---|---|
| Login distinto al normal | Detiene todo el sync: `login_bloqueado` |
| Solicitud no encontrada o ambigua | Detiene todo el sync: `solicitud_no_encontrada` |
| Pozo, posición o línea inexistente | Esa celda: `*_no_encontrada`; sigue con las demás |
| Celda con cantidad ya cargada | Esa celda: `ya_existia`; no toca nada |
| Valor leído distinto del payload | No envía esa celda: `valor_no_coincide` |
| Falla al descargar o adjuntar el PDF | La celda queda `error` con detalle (las horas ya estaban enviadas) |
| Aviso o error propio de Y-Click | Copiar el texto exacto al detalle y sacar captura |
| Tope de pasos o minutos superado | Esa celda: `tope_excedido` |

Una celda enviada con el adjunto fallido se informa de forma explícita: Fabio adjunta el PDF a mano o elimina la carga y reintenta.

---

## 5. Qué devuelve a Wells

```json
{
  "sync_id": "uuid",
  "estado": "completado | parcial | fallido",
  "resumen": { "ok": 41, "ya_existia": 2, "error": 1 },
  "celdas": [
    {
      "celda_id": "uuid",
      "resultado": "ok | ya_existia | error",
      "motivo": "valor_no_coincide",
      "detalle": "Texto libre: qué pasó y en qué paso",
      "captura_url": "https://…"
    }
  ]
}
```

`estado` es `completado` solo si **todas** las celdas son `ok` o `ya_existia`.

---

## 6. Reglas de Fabio para armar las celdas (lado Wells)

Se dicen acá para que el bot sepa que **no** las decide él:

- **Ubicación:** si el parte es de un pozo, tipo `Pozo - Operación y Mantenimiento` y el nombre del pozo.
- **Posición:** sale del **equipo** del parte (si es Hot Oil, una posición con «HOT OIL» en el nombre) y del **día** (día de semana o sábado, domingo y feriado).
- **Traslado, montaje y desmontaje no se distinguen:** cuentan como horas operativas bajo la posición de desparafinación. Solo se carga KM de traslado cuando es de un pozo a otro (caso especial).
- **Todas las horas del parte cuentan como operativas**, aunque el bombeo haya sido solo una parte (ej. el parte 23359: 4 horas de 8 a 12, con bombeo de 9:30 a 11:30, se cargan las 4).
- **Cada tipo de hora es otra posición** (50%, 100%, stand by, espera, KM).
- **Tabla de Fabio (posiciones por unidad):** lunes a viernes, LC1–LC7: 31, 33, 35, 37, 39, KM 41; NQG: 3, 5, 7, 9, 11, KM 21; SH: NOC 63, 65, 67, KM 69 y NQG 23, KM 27. **Sábado, domingo y feriado:** LC1–LC7: 37, 39, KM 41; NQG: 9, 11, KM 21; SH: 25, 67, KM 69 (NOC) y KM 27 (NQG). Es decir que **Hot Oil en fin de semana se carga como horas extra al 50% y al 100%**.
- **Sábado, domingo y feriado, Super Heat:** posición **25** (`SUPER HEAT SAB/DOM/FER`, medida por día). La jornada es de 8 a 17 (9 horas) y equivale a **1 día**; lo que exceda el horario pasa a horas adicionales. **La 25 es solo de Super Heat.** Hot Oil no tiene posición «operativa» de fin de semana: sus horas se clasifican como extra al 50% o al 100%.
- **Dos pozos el mismo día de fin de semana:** la jornada se reparte. Fabio normalmente lo hace en proporción a las horas de cada pozo sobre 9; si no, mitad y mitad («no es tan fino»). En el parte de ejemplo (SOil-402 y SOil-403) puso **0,5 días en cada uno**.
- **Super Heat en NOC, lunes a viernes:** posición **63** (por día); horas extra al 50% → **65**; al 100% → **67**; KM → **69**.
- El tipo de equipo (**inyección de vapor, hot oil o super heat**) define la familia de posiciones. Fabio marcó esas correspondencias en negrita en el Excel de posiciones.

## 7. Pendiente para completar

- [ ] ⚠️ Botones del modal «Agregar ubicación»: cuál se usa y qué hace «Agregar y posicionar».
- [ ] ⚠️ Partes que no son de un pozo (plantas): qué tipo de ubicación usan.
- [ ] ⚠️ Avioncito de la fila vs. «Enviar a aprobar».
- [ ] Un caso con horas al 50% o 100%, y uno con KM.
- [ ] Qué muestra Y-Click ante un error o un duplicado.
- [x] Fin de semana: posición 25 con cantidad en días; con dos pozos el mismo día se reparte (ver sección 6).
- [ ] ⚠️ Fin de semana con **un solo parte que no cubre las 8 a 17** (ej. 12 a 17) y sin otra operación ese día: ¿1 día entero o la fracción de horas?
- [ ] ⚠️ Qué horas extra van al **50%** y cuáles al **100%** (de semana y en fin de semana).
- [ ] ⚠️ Excel de posiciones **con las negritas de Fabio** (equipo ↔ posición).
- [ ] «Rectificar cantidad»: se abre desde el menú ⋮ de la fila. Falta ver qué pide después (campo, confirmación, si hay que reenviar a aprobar). Hoy el bot no rectifica.
- [ ] Regla del área (NQG, NOC ESTE o NOC REG) para elegir la posición de los días de semana.
