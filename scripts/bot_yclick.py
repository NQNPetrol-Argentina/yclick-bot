#!/usr/bin/env python3
"""Bot Y-Click — carga un sync de Wells.

Uso: python3 -I scripts/bot_yclick.py <sync_id>

Lee el detalle del sync con wells_api.py, carga cada celda en Y-Click con
Playwright y reporta progreso y resultado final a Wells.
"""
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
import zoneinfo

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ── constantes ────────────────────────────────────────────────────────────────
CHROMIUM = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
WELLS_API = str(pathlib.Path(__file__).parent / "wells_api.py")
ARS_TZ = zoneinfo.ZoneInfo("America/Argentina/Buenos_Aires")
TOPE_SEG = 360
TOPE_PASOS = 60

# ── log ───────────────────────────────────────────────────────────────────────
_log: list[str] = []
_log_path: pathlib.Path | None = None


def log(msg: str):
    ts = datetime.datetime.now(ARS_TZ).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _log.append(line)
    print(line, file=sys.stderr)


def flush_log():
    if _log_path:
        _log_path.write_text("\n".join(_log) + "\n", encoding="utf-8")


# ── Wells API ─────────────────────────────────────────────────────────────────
def _run_api(*args) -> tuple[int, str]:
    r = subprocess.run(
        ["/usr/bin/python3", "-I", WELLS_API, *args],
        capture_output=True, text=True,
    )
    log(f"wells_api {' '.join(args[:3])} rc={r.returncode} "
        f"out={r.stdout.strip()[:200]} err={r.stderr.strip()[:200]}")
    return r.returncode, r.stdout.strip()


def wells_get(sync_id: str, out: str) -> dict:
    rc, _ = _run_api("get", sync_id, "-o", out)
    if rc != 0:
        raise RuntimeError(f"wells_get falló rc={rc}")
    return json.loads(pathlib.Path(out).read_bytes())


def wells_progreso(sync_id: str, celdas: list[dict]):
    body = {"celdas": celdas}
    p = f"/tmp/progreso_{sync_id[:8]}.json"
    pathlib.Path(p).write_text(json.dumps(body), encoding="utf-8")
    _run_api("progreso", sync_id, p)


def wells_resultado(sync_id: str, estado: str, celdas: list[dict], msg: str = ""):
    body = {
        "estado": estado,
        "mensaje": msg,
        "resumen": {
            "ok":         sum(1 for c in celdas if c["resultado"] == "ok"),
            "ya_existia": sum(1 for c in celdas if c["resultado"] == "ya_existia"),
            "error":      sum(1 for c in celdas if c["resultado"] == "error"),
        },
        "celdas": celdas,
    }
    p = f"/tmp/resultado_{sync_id[:8]}.json"
    pathlib.Path(p).write_text(json.dumps(body), encoding="utf-8")
    rc, out = _run_api("resultado", sync_id, p)
    return rc == 0, out


def wells_get_estado(sync_id: str) -> str:
    rc, out = _run_api("get", sync_id)
    if rc == 0:
        try:
            return json.loads(out).get("estado", "desconocido")
        except Exception:
            pass
    return "error_al_verificar"


# ── contador de pasos ─────────────────────────────────────────────────────────
_pasos = 0
_celda_inicio = 0.0


def paso(desc: str):
    global _pasos
    _pasos += 1
    log(f"Paso {_pasos}: {desc}")
    if _pasos > TOPE_PASOS:
        raise RuntimeError("tope_pasos")
    if _celda_inicio and (time.time() - _celda_inicio) > TOPE_SEG:
        raise RuntimeError("tope_tiempo")


def reset_pasos():
    global _pasos, _celda_inicio
    _pasos = 0
    _celda_inicio = time.time()


# ── helpers de React Select ───────────────────────────────────────────────────
def react_select_elegir(page, label_text: str, buscar: str, timeout=10000) -> str:
    """
    Busca el React Select cuyo <label> contiene label_text, tipea `buscar`,
    espera las opciones y hace clic en la primera que coincida (case-insensitive).
    Devuelve el texto de la opción elegida.
    """
    # El input del react-select está inmediatamente después del label dentro del mismo bloque
    container = page.locator(
        f".modal-body .generic-select:has(label:has-text('{label_text}'))"
    ).first
    inp = container.locator("input[type='text']").first
    inp.fill(buscar)
    page.wait_for_timeout(1500)

    # Opciones visibles
    opts = page.locator("[class*='-option']").all()
    for op in opts:
        txt = op.inner_text().strip()
        if buscar.lower() in txt.lower() or txt.lower() in buscar.lower():
            op.click(timeout=timeout)
            log(f"  react-select '{label_text}': elegido '{txt}'")
            page.wait_for_timeout(800)
            return txt

    # Si no hubo coincidencia exacta, clic en la primera opción disponible
    if opts:
        txt = opts[0].inner_text().strip()
        opts[0].click(timeout=timeout)
        log(f"  react-select '{label_text}': elegido (primera opción) '{txt}'")
        page.wait_for_timeout(800)
        return txt

    raise RuntimeError(f"react_select sin opciones para '{label_text}' buscando '{buscar}'")


def react_select_opciones(page, label_text: str, buscar: str = "") -> list[str]:
    """Devuelve las opciones disponibles en un react-select sin elegir ninguna."""
    container = page.locator(
        f".modal-body .generic-select:has(label:has-text('{label_text}'))"
    ).first
    inp = container.locator("input[type='text']").first
    inp.fill(buscar)
    page.wait_for_timeout(1500)
    opts = page.locator("[class*='-option']").all()
    return [op.inner_text().strip() for op in opts]


# ── captura ───────────────────────────────────────────────────────────────────
def captura(page, nombre: str) -> str:
    p = f"/tmp/cap_{nombre}.png"
    try:
        page.screenshot(path=p, full_page=True)
        log(f"Captura: {p}")
    except Exception as e:
        log(f"Captura fallida: {e}")
    return p


# ── login ─────────────────────────────────────────────────────────────────────
def login(page):
    paso("Navegar a yclick.ypf.com")
    page.goto("https://yclick.ypf.com/", wait_until="networkidle", timeout=30000)

    paso("Completar usuario")
    page.locator("input[name='user']").fill(os.environ["USUARIO_YCLICK"])
    paso("Completar contraseña")
    page.locator("input[name='password']").fill(os.environ["CONTRASENA_YCLICK"])
    paso("Pulsar Ingresar")
    page.locator("button[type='submit']").click()

    # La redirección a #/home puede tardar unos segundos
    try:
        page.wait_for_url("**/#/home*", timeout=60000)
    except PWTimeout:
        pass

    page.wait_for_timeout(2000)
    log(f"URL post-login: {page.url}")

    # Detectar pantallas bloqueantes
    html = page.content().lower()
    for kw in ["captcha", "verify you are human", "bloqueo", "cambio de contraseña", "change password"]:
        if kw in html:
            captura(page, "login_bloqueado")
            raise RuntimeError(f"login_bloqueado: detectado '{kw}'")

    if "login" in page.url.lower() and "#/home" not in page.url:
        captura(page, "login_bloqueado")
        raise RuntimeError("login_bloqueado: sigue en pantalla de login")


# ── abrir solicitud del mes ────────────────────────────────────────────────────
def abrir_solicitud(page, periodo: str) -> str:
    """
    Navega a la bandeja EN CURSO y hace clic en la solicitud del periodo dado
    (ej. '2026-11' → fila con 'NOV/2026').
    Devuelve la URL de detalle.
    """
    # periodo '2026-11' → 'NOV/2026'
    meses = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN",
             "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]
    año, mes = periodo.split("-")
    mes_txt = f"{meses[int(mes)-1]}/{año}"

    paso("Ir a Consultar Bandeja de Solicitud de Servicio")
    page.locator("text=Consultar Bandeja de Solicitud").first.click()
    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(2000)

    paso("Buscar todas las solicitudes (JS click en Buscar)")
    # El botón está fuera del viewport en el layout actual; se usa JS
    page.evaluate("document.querySelector('button[type=\"submit\"]').click()")
    page.wait_for_timeout(3000)

    paso("Pestaña EN CURSO")
    page.locator("text=EN CURSO").first.click()
    page.wait_for_timeout(3000)

    paso(f"Buscar fila {mes_txt} + Desparafinación")
    # Buscar la fila correcta
    filas = page.locator("tr").all()
    fila_ok = None
    for fila in filas:
        txt = fila.inner_text().lower()
        if mes_txt.lower() in txt and "desparaf" in txt:
            fila_ok = fila
            break

    if not fila_ok:
        captura(page, "solicitud_no_encontrada")
        raise RuntimeError(f"solicitud_no_encontrada: no hay fila con {mes_txt} + Desparafinación en EN CURSO")

    paso(f"Clic en N° de solicitud ({mes_txt})")
    # El número de solicitud es el primer link/celda de la fila
    fila_ok.locator("td").first.click(timeout=10000)
    page.wait_for_url("**/detailmonthly/**", timeout=20000)
    page.wait_for_timeout(2000)

    log(f"URL detalle: {page.url}")
    return page.url


# ── verificar título de la solicitud ─────────────────────────────────────────
def verificar_solicitud(page, periodo: str):
    meses_es = ["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO",
                "JULIO", "AGOSTO", "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"]
    año, mes = periodo.split("-")
    mes_es = meses_es[int(mes) - 1]
    contenido = page.content().upper()
    if mes_es not in contenido and f"{mes_es[:3]}/{año}" not in contenido:
        log(f"Aviso: no se confirmó '{mes_es} {año}' en el título de la solicitud")


# ── agregar o ubicar ubicación ────────────────────────────────────────────────
def gestionar_ubicacion(page, celda: dict) -> str:
    """
    Verifica si la ubicación ya existe; si no, la agrega.
    Devuelve el nombre exacto que quedó en Y-Click.
    """
    ubicacion = celda["ubicacion"]           # ej. "COLECTOR LOC 56"
    tipo_payload = celda["tipo_ubicacion"]   # ej. "Pozo - Operación y Mantenimiento"

    paso(f"Verificar si '{ubicacion}' ya existe")
    contenido = page.content().upper()
    # Buscar el nombre del payload o variantes conocidas
    busqueda_ub = ubicacion.upper()
    # Para "COLECTOR LOC 56" la variante en Y-Click es "LOCACION N° 56"
    # Estrategia: si no encontramos el nombre exacto, buscamos por número si lo hay
    ya_existe = busqueda_ub in contenido

    if ya_existe:
        log(f"Ubicación '{ubicacion}' ya existe en la lista")
        return ubicacion

    # --- Agregar ubicación ---
    paso("Clic en Agregar ubicación")
    page.locator("text=Agregar ubicación").first.click()
    page.wait_for_selector(".modal-body", timeout=10000)
    page.wait_for_timeout(1500)

    # Elegir tipo de ubicación
    # Estrategia: intentar con el tipo exacto del payload; si no existe, con la variante "Locación"
    paso(f"Elegir tipo de ubicación: '{tipo_payload}'")
    tipos_disponibles = react_select_opciones(page, "Tipo de ubicación", tipo_payload)
    log(f"  Tipos disponibles: {tipos_disponibles}")

    tipo_elegido = None
    # Buscar coincidencia en las opciones disponibles (case-insensitive, parcial)
    palabras_clave = [w for w in tipo_payload.split() if len(w) > 3]
    for op in tipos_disponibles:
        matches = sum(1 for w in palabras_clave if w.lower() in op.lower())
        if matches >= 2:  # al menos 2 palabras coinciden
            tipo_elegido = op
            break
    if not tipo_elegido and tipos_disponibles:
        # Fallback: primera opción que contenga "Operación"
        for op in tipos_disponibles:
            if "operaci" in op.lower():
                tipo_elegido = op
                break

    if not tipo_elegido:
        captura(page, "ubicacion_no_soportada")
        raise RuntimeError(f"ubicacion_no_soportada: no se encontró tipo '{tipo_payload}' en Y-Click. Disponibles: {tipos_disponibles}")

    # Ahora elegir el tipo
    container_tipo = page.locator(
        ".modal-body .generic-select:has(label:has-text('Tipo de ubicación'))"
    ).first
    inp_tipo = container_tipo.locator("input[type='text']").first
    inp_tipo.fill(tipo_elegido[:10])
    page.wait_for_timeout(1500)
    page.locator(f"[class*='-option']:has-text('{tipo_elegido[:30]}')").first.click()
    log(f"  Tipo elegido: '{tipo_elegido}'")
    page.wait_for_timeout(2000)

    # El label del campo de ubicación cambia con el tipo:
    # "Pozo" → "Ubicación pozo" / "Locación" → "Ubicación locación"
    # Buscar el label que contiene "Ubicación"
    labels_modal = page.locator(".modal-body .form-label").all()
    label_ub = None
    for lbl in labels_modal:
        txt = lbl.inner_text().strip()
        if "ubicaci" in txt.lower() and "tipo" not in txt.lower():
            label_ub = txt
            break
    if not label_ub:
        label_ub = "Ubicación"
    log(f"  Label del campo de ubicación: '{label_ub}'")

    # Buscar la ubicación por su nombre (o parte de él)
    # Para "COLECTOR LOC 56" intentamos buscar "56" o "COLECTOR" o "LOCACION"
    terminos_busqueda = [
        ubicacion,            # nombre exacto del payload
        ubicacion.split()[-1],  # última palabra (ej. "56")
        ubicacion[:8],        # primeras letras
    ]
    nombre_elegido = None
    for termino in terminos_busqueda:
        paso(f"Buscar ubicación con término '{termino}'")
        try:
            opciones_ub = react_select_opciones(page, label_ub, termino)
            log(f"  Opciones para '{termino}': {opciones_ub}")
            if opciones_ub:
                # Elegir la primera opción (o la que coincida mejor)
                mejor = None
                for op in opciones_ub:
                    # La última palabra del nombre del payload suele estar en el nombre Y-Click
                    if ubicacion.split()[-1].upper() in op.upper():
                        mejor = op
                        break
                if not mejor:
                    mejor = opciones_ub[0]

                # Elegir
                container_ub = page.locator(
                    f".modal-body .generic-select:has(label:has-text('{label_ub}'))"
                ).first
                inp_ub = container_ub.locator("input[type='text']").first
                inp_ub.fill(termino)
                page.wait_for_timeout(1500)
                page.locator(f"[class*='-option']:has-text('{mejor[:30]}')").first.click()
                nombre_elegido = mejor
                log(f"  Ubicación elegida: '{nombre_elegido}'")
                page.wait_for_timeout(2000)
                break
        except Exception as e:
            log(f"  Fallo con término '{termino}': {e}")
            continue

    if not nombre_elegido:
        captura(page, "ubicacion_no_encontrada")
        raise RuntimeError(f"ubicacion_no_encontrada: no se encontró '{ubicacion}' en Y-Click")

    # Verificar que Unidad de Negocio y Activo se autocomplete
    paso("Verificar autocompletado de Unidad de Negocio / Activo")
    page.wait_for_timeout(1000)
    # (Solo verificación visual; no forzamos valores)

    # Clic en Agregar
    paso("Clic en Agregar (modal ubicación)")
    # Intentar primero "Agregar" habilitado (no "Agregar y continuar")
    btns = page.locator(".modal-content button:not([class*='btn-close'])").all()
    btn_agregar = None
    for btn in btns:
        txt = btn.inner_text().strip()
        if txt == "Agregar" and not btn.is_disabled():
            btn_agregar = btn
            break
    if not btn_agregar:
        # Intentar cualquier botón de agregar
        btn_agregar = page.locator(
            ".modal-content button:has-text('Agregar'):not(:has-text('continuar')):not([class*='btn-close'])"
        ).first

    btn_agregar.click(timeout=8000)
    page.wait_for_timeout(2000)

    # Esperar cierre del modal o aviso
    try:
        page.wait_for_selector("text=correctamente, text=agregó", timeout=8000)
        log("Ubicación agregada correctamente")
    except PWTimeout:
        log("Aviso de ubicación no detectado, continuando")

    page.wait_for_timeout(1500)
    return nombre_elegido


# ── agregar posición y línea ──────────────────────────────────────────────────
def gestionar_posicion_linea(page, celda: dict):
    """Agrega la posición y línea para la fecha de la celda."""
    posicion = celda["posicion"]     # ej. "31 - NOC ESTE -SRV.DESPARAF.HOT OIL L a V H OPERATIVA"
    linea_cod = celda["linea"]       # ej. "10"
    fecha = celda["fecha"]           # ej. "2026-11-03"
    año, mes, dia = fecha.split("-")
    dia_str = f"{dia}/{mes}/{año}"   # "03/11/2026"

    pos_cod = posicion.split(" - ")[0].strip()  # "31"

    paso("Expandir el bloque de ubicación")
    # Marcar el checkbox de la ubicación para habilitarla
    try:
        checkboxes = page.locator(".parte-trabajo input[type='checkbox'], [class*='ubicacion'] input[type='checkbox']").all()
        for cb in checkboxes:
            if not cb.is_checked():
                cb.click()
                page.wait_for_timeout(500)
                break
    except Exception:
        pass

    # Verificar si ya tiene la posición/línea para esta fecha
    contenido = page.content()
    if pos_cod in contenido and linea_cod + " -" in contenido:
        log(f"Posición {pos_cod} / línea {linea_cod} ya visible en la página")
        return

    paso("Clic en Agregar posición y línea")
    try:
        page.locator(
            "text=Agregar posición y línea a ubicaciones, "
            "button:has-text('Agregar líneas'), "
            "button:has-text('Agregar posici')"
        ).first.click(timeout=10000)
    except PWTimeout:
        # Intentar desde dentro del bloque de ubicación
        page.locator("button:has-text('Agregar l')").first.click(timeout=8000)

    page.wait_for_selector(".modal-body", timeout=10000)
    page.wait_for_timeout(1500)

    paso(f"Elegir posición: código {pos_cod}")
    nombre_pos = react_select_elegir(page, "Posición", pos_cod)
    log(f"  Posición elegida: '{nombre_pos}'")
    page.wait_for_timeout(1500)

    paso(f"Elegir línea: código {linea_cod}")
    # La línea se habilita al elegir la posición; buscar por código
    opciones_linea = react_select_opciones(page, "Línea", linea_cod)
    log(f"  Opciones de línea: {opciones_linea}")
    linea_elegida = None
    for op in opciones_linea:
        cod = op.split(" - ")[0].strip() if " - " in op else op.strip()
        if cod == linea_cod:
            linea_elegida = op
            break
    if not linea_elegida:
        captura(page, "linea_no_encontrada")
        raise RuntimeError(f"linea_no_encontrada: no existe línea con código '{linea_cod}'. Opciones: {opciones_linea}")

    container_lin = page.locator(".modal-body .generic-select:has(label:has-text('Línea'))").first
    inp_lin = container_lin.locator("input[type='text']").first
    inp_lin.fill(linea_cod)
    page.wait_for_timeout(1500)
    page.locator(f"[class*='-option']:has-text('{linea_elegida[:30]}')").first.click()
    log(f"  Línea elegida: '{linea_elegida}'")
    page.wait_for_timeout(1000)

    paso(f"Ajustar Desde y Hasta a {dia_str}")
    for label in ["Desde", "Hasta"]:
        try:
            inp = page.locator(f"input:near(:text('{label}'))").first
            inp.triple_click()
            inp.fill(dia_str)
            page.wait_for_timeout(300)
        except Exception as e:
            log(f"Aviso: no pude ajustar '{label}': {e}")

    paso("Clic en Aceptar")
    page.locator(
        ".modal-content button:has-text('Aceptar'), "
        ".modal-content button[type='submit']:has-text('Aceptar')"
    ).first.click(timeout=8000)
    page.wait_for_timeout(2000)
    page.wait_for_load_state("networkidle", timeout=15000)
    log("Posición y línea agregadas")


# ── navegar al día ────────────────────────────────────────────────────────────
def navegar_al_dia(page, fecha: str):
    """Navega en el selector de día hasta llegar a la fecha indicada (YYYY-MM-DD)."""
    año, mes, dia = fecha.split("-")
    dia_int = int(dia)
    meses_es = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    mes_es = meses_es[int(mes) - 1]

    paso(f"Navegar al día {dia}/{mes}/{año}")
    for _ in range(45):
        contenido = page.content().lower()
        if (f"{dia_int} de {mes_es}" in contenido or
                f"0{dia_int} de {mes_es}" in contenido or
                f"{dia}/{mes}/{año}" in contenido):
            log(f"Día {dia}/{mes} visible")
            return
        try:
            page.locator(
                "button[aria-label*='siguiente'], button:has-text('>'), "
                "[class*='next'], [class*='siguiente']"
            ).first.click(timeout=4000)
            page.wait_for_timeout(400)
        except PWTimeout:
            log("Aviso: no se encontró botón de día siguiente")
            break

    log(f"Aviso: no se pudo confirmar el día {dia}/{mes} en el navegador de fecha")


# ── cargar cantidad ────────────────────────────────────────────────────────────
def cargar_cantidad(page, celda: dict) -> str:
    """
    Busca la caja de cantidad de la fila correcta.
    Si ya tiene valor devuelve 'ya_existia'.
    Si está vacía escribe la cantidad y devuelve 'pendiente'.
    """
    pos_cod = celda["posicion"].split(" - ")[0].strip()
    cantidad = celda["cantidad"]
    linea_cod = celda["linea"]

    paso("Buscar caja de cantidad")
    # Buscar dentro de la fila que tenga el código de posición y línea
    caja = None
    for sel in [
        f"tr:has-text('{pos_cod}'):has-text('{linea_cod}') input[type='number']",
        f"tr:has-text('{pos_cod}'):has-text('{linea_cod}') input[type='text']",
        f"[class*='row']:has-text('{pos_cod}') input[type='number']",
        "input[type='number']",
    ]:
        try:
            locs = page.locator(sel).all()
            if locs:
                caja = locs[0]
                break
        except Exception:
            pass

    if caja is None:
        raise RuntimeError("caja_no_encontrada: no se ubicó el input de cantidad")

    val_actual = caja.input_value(timeout=5000).strip()
    if val_actual not in ("", "0"):
        log(f"Celda ya tiene valor '{val_actual}' → ya_existia")
        return "ya_existia"

    paso(f"Escribir cantidad {cantidad}")
    caja.triple_click()
    caja.fill(str(cantidad))
    page.wait_for_timeout(500)

    # Verificar
    val_leido = caja.input_value(timeout=5000).strip()
    if val_leido != str(cantidad):
        captura(page, "valor_no_coincide")
        raise RuntimeError(f"valor_no_coincide: se escribió {cantidad} pero se lee '{val_leido}'")

    log(f"Valor confirmado en pantalla: {val_leido}")
    return "pendiente"


# ── verificar y enviar ────────────────────────────────────────────────────────
def verificar_y_enviar(page, celda: dict):
    pos_cod = celda["posicion"].split(" - ")[0].strip()
    ubicacion = celda["ubicacion"]
    cantidad = celda["cantidad"]

    paso("Verificar datos antes de Enviar a aprobar")
    contenido = page.content()
    checks = {
        "pos_cod": pos_cod in contenido,
        "linea":   celda["linea"] in contenido,
        "cantidad": str(cantidad) in contenido,
    }
    log(f"Verificación: {checks}")
    if not all(checks.values()):
        fallidos = [k for k, v in checks.items() if not v]
        captura(page, "verificacion_fallida")
        raise RuntimeError(f"valor_no_coincide: no confirmados en pantalla: {fallidos}")

    paso("Pulsar Enviar a aprobar")
    page.locator("button:has-text('Enviar a aprobar')").first.click(timeout=10000)
    page.wait_for_timeout(2500)

    paso("Verificar envío (botón deshabilitado o valor en verde)")
    try:
        page.wait_for_selector(
            "button:has-text('Enviar a aprobar')[disabled], "
            "[class*='verde'], [class*='green'], [class*='enviado']",
            timeout=8000,
        )
        log("Envío confirmado")
    except PWTimeout:
        log("Aviso: no se detectó confirmación visual de envío")


# ── doc_respaldo ──────────────────────────────────────────────────────────────
def cargar_doc_respaldo(page, celda: dict):
    if not celda.get("doc_respaldo"):
        return
    pos_cod = celda["posicion"].split(" - ")[0].strip()
    doc = celda["doc_respaldo"]

    paso(f"Ícono código de barras → doc_respaldo '{doc}'")
    try:
        page.locator(
            f"tr:has-text('{pos_cod}') [class*='barcode'], "
            f"tr:has-text('{pos_cod}') [title*='espaldo'], "
            f"tr:has-text('{pos_cod}') [aria-label*='espaldo'], "
            f"tr:has-text('{pos_cod}') button:nth-child(1)"
        ).first.click(timeout=8000)
        page.wait_for_selector(".modal-body", timeout=8000)
        page.wait_for_timeout(1000)

        inp = page.locator(
            "input[name*='respaldo'], input[name*='documento'], "
            "input[placeholder*='ocumento'], input[id*='respaldo']"
        ).first
        inp.triple_click()
        inp.fill(doc)
        page.wait_for_timeout(500)

        page.locator("button:has-text('Guardar'), button[type='submit']").first.click(timeout=8000)
        try:
            page.wait_for_selector("text=correctamente, text=guardó", timeout=8000)
            log(f"Doc respaldo '{doc}' guardado OK")
        except PWTimeout:
            log("Aviso: confirmación de doc_respaldo no detectada")
        page.wait_for_timeout(1000)
        # Cerrar modal si sigue abierto
        try:
            page.locator(".modal-content button:has-text('Cerrar'), .btn-close").first.click(timeout=3000)
        except Exception:
            pass

    except Exception as e:
        log(f"Aviso: fallo en doc_respaldo: {e} (no bloqueante)")


# ── adjuntar PDF ──────────────────────────────────────────────────────────────
def adjuntar_pdf(page, celda: dict, pdf_path: str) -> bool:
    pos_cod = celda["posicion"].split(" - ")[0].strip()
    parte_num = celda["adjuntos"][0]["parte"] if celda.get("adjuntos") else None

    paso("Ícono documento → adjuntar PDF")
    try:
        page.locator(
            f"tr:has-text('{pos_cod}') [class*='attach'], "
            f"tr:has-text('{pos_cod}') [class*='document'], "
            f"tr:has-text('{pos_cod}') [class*='file'], "
            f"tr:has-text('{pos_cod}') [title*='djunto'], "
            f"tr:has-text('{pos_cod}') [aria-label*='djunto'], "
            f"tr:has-text('{pos_cod}') button:nth-child(3)"
        ).first.click(timeout=8000)
        page.wait_for_selector(".modal-body", timeout=8000)
        page.wait_for_timeout(1500)

        # Buscar el botón "Agregar adjunto" en la sección de la LÍNEA (no de la solicitud)
        paso("Buscar sección de adjuntos de la línea")
        btns_agregar = page.locator(
            ".modal-content button:has-text('Agregar adjunto'), "
            ".modal-content button:has-text('Agregar')"
        ).all()

        btn_ok = None
        for btn in btns_agregar:
            try:
                # Subir en el DOM para ver el texto del contenedor padre
                padre_txt = btn.evaluate(
                    "el => el.closest('div[class]')?.innerText || ''"
                ).lower()
                if "solicitud" not in padre_txt:
                    btn_ok = btn
                    break
            except Exception:
                pass
        if not btn_ok and btns_agregar:
            btn_ok = btns_agregar[-1]  # el último suele ser de la línea

        paso("Seleccionar PDF con file chooser")
        with page.expect_file_chooser() as fc_info:
            btn_ok.click(timeout=8000)
        fc_info.value.set_files(pdf_path)
        page.wait_for_timeout(2000)

        paso("Pulsar Agregar para confirmar subida")
        page.locator(
            ".modal-content button:has-text('Agregar'):not(:has-text('adjunto'))"
        ).first.click(timeout=10000)

        try:
            page.wait_for_selector(
                "text=Adjuntando archivos, text=adjuntando", timeout=10000
            )
        except PWTimeout:
            log("Aviso: no se detectó 'Adjuntando archivos'")

        page.wait_for_timeout(3000)

        # Verificar que el archivo aparece en la lista
        modal_txt = page.locator(".modal-content").inner_text().lower()
        nombre_pdf = pathlib.Path(pdf_path).name.lower()
        if nombre_pdf in modal_txt or (parte_num and str(parte_num) in modal_txt):
            log(f"PDF confirmado en la lista de adjuntos")
        else:
            log(f"Aviso: no se confirmó el PDF en la lista")

        paso("Salir del modal de adjuntos")
        page.locator(
            ".modal-content button:has-text('Salir'), "
            ".modal-content button:has-text('Cerrar'), "
            ".btn-close"
        ).first.click(timeout=8000)
        page.wait_for_timeout(1000)
        return True

    except Exception as e:
        log(f"Error al adjuntar PDF: {e}")
        return False


# ── procesar una celda ────────────────────────────────────────────────────────
def procesar_celda(page, celda: dict, sync_id: str, pdf_dir: str) -> dict:
    """Ejecuta los pasos 3-8 para una celda. Devuelve el dict de resultado."""
    global _pasos, _celda_inicio
    reset_pasos()

    celda_id = celda["celda_id"]
    resultado = "error"
    motivo = None
    detalle = None

    try:
        # Descargar PDF si lo hay
        pdf_path = None
        if celda.get("adjuntos"):
            adj = celda["adjuntos"][0]
            pdf_path = str(pathlib.Path(pdf_dir) / adj["archivo"])
            if not pathlib.Path(pdf_path).exists():
                # Pedir URL fresca y descargar
                sync_data = json.loads(
                    subprocess.run(
                        ["/usr/bin/python3", "-I", WELLS_API, "get", sync_id],
                        capture_output=True, text=True
                    ).stdout
                )
                url_fresca = next(
                    (a["url_firmada"]
                     for c in sync_data["celdas"] if c["celda_id"] == celda_id
                     for a in c.get("adjuntos", []) if a["archivo"] == adj["archivo"]),
                    None
                )
                if url_fresca:
                    rc_dl = subprocess.run(
                        ["/usr/bin/python3", "-I", WELLS_API, "download", url_fresca, pdf_path],
                        capture_output=True, text=True
                    ).returncode
                    if rc_dl != 0:
                        raise RuntimeError(f"error_descarga_pdf: no se pudo descargar {adj['archivo']}")
                    log(f"PDF descargado: {pdf_path}")
                else:
                    raise RuntimeError("error_descarga_pdf: no se encontró url_firmada fresca")

        # Paso 3 — Ubicación
        nombre_ub = gestionar_ubicacion(page, celda)

        # Paso 4 — Posición y línea
        gestionar_posicion_linea(page, celda)

        # Paso 5 — Navegar al día y cargar cantidad
        navegar_al_dia(page, celda["fecha"])
        estado_carga = cargar_cantidad(page, celda)

        if estado_carga == "ya_existia":
            resultado = "ya_existia"
            detalle = "La celda ya tenía un valor en Y-Click"
        else:
            # Paso 6 — Enviar a aprobar
            verificar_y_enviar(page, celda)

            # Paso 7 — Doc respaldo
            cargar_doc_respaldo(page, celda)

            # Paso 8 — Adjuntar PDF
            if pdf_path:
                adj_ok = adjuntar_pdf(page, celda, pdf_path)
                if adj_ok:
                    resultado = "ok"
                    detalle = "Carga enviada a aprobar con PDF adjunto"
                else:
                    resultado = "error"
                    motivo = "error_adjunto"
                    detalle = "Horas enviadas a aprobar pero falló el adjunto del PDF"
            else:
                resultado = "ok"
                detalle = "Carga enviada a aprobar (sin adjuntos)"

    except RuntimeError as e:
        msg = str(e)
        log(f"ERROR en celda {celda_id}: {msg}")
        if any(msg.startswith(k) for k in [
            "login_bloqueado", "solicitud_no_encontrada", "ubicacion_no_encontrada",
            "ubicacion_ambigua", "ubicacion_no_soportada", "posicion_no_encontrada",
            "linea_no_encontrada", "valor_no_coincide", "error_descarga_pdf",
        ]):
            motivo = msg.split(":")[0]
            detalle = msg
        elif "tope_pasos" in msg:
            motivo = "tope_excedido"
            detalle = "Se superó el límite de 60 pasos"
        elif "tope_tiempo" in msg:
            motivo = "tope_excedido"
            detalle = "Se superó el límite de 6 minutos"
        else:
            motivo = "error_inesperado"
            detalle = msg
        resultado = "error"

    except Exception as e:
        log(f"EXCEPCION en celda {celda_id}: {type(e).__name__}: {e}")
        resultado = "error"
        motivo = "excepcion"
        detalle = f"{type(e).__name__}: {e}"

    return {"celda_id": celda_id, "resultado": resultado,
            "motivo": motivo, "detalle": detalle, "captura_url": None}


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    global _log_path

    if len(sys.argv) < 2:
        print("Uso: bot_yclick.py <sync_id>", file=sys.stderr)
        sys.exit(2)

    sync_id = sys.argv[1]

    # Validar UUID
    import re
    if not re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", sync_id):
        print(f"sync_id inválido: {sync_id}", file=sys.stderr)
        sys.exit(2)

    inicio = datetime.datetime.now(ARS_TZ)
    log_dir = pathlib.Path("/tmp/yclick_logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    _log_path = log_dir / f"{inicio.strftime('%Y-%m-%d_%H%M%S')}_{sync_id}.md"
    pdf_dir = str(log_dir)

    log(f"=== INICIO sync_id={sync_id} ===")
    log(f"Hora ARS: {inicio.isoformat()}")

    # Verificar credenciales
    for var in ["TOKEN_WELLS", "YCLICK_CALLBACK_SECRET", "USUARIO_YCLICK", "CONTRASENA_YCLICK"]:
        if not os.environ.get(var):
            log(f"FALTA variable de entorno: {var}")
            flush_log()
            print(f"ERROR: falta {var}", file=sys.stderr)
            sys.exit(1)

    # GET sync
    sync_path = str(log_dir / f"sync_{sync_id[:8]}.json")
    try:
        sync = wells_get(sync_id, sync_path)
    except Exception as e:
        log(f"No se pudo obtener el sync: {e}")
        flush_log()
        sys.exit(1)

    estado_sync = sync.get("estado")
    log(f"Estado del sync: {estado_sync}")
    if estado_sync != "en_proceso":
        log(f"Sync no está en_proceso (estado={estado_sync}). No se hace nada.")
        flush_log()
        print(f"Sync estado={estado_sync}, no en_proceso. Saliendo.")
        sys.exit(0)

    celdas = sync.get("celdas", [])
    periodo = sync.get("solicitud", {}).get("periodo", "")
    log(f"Celdas a procesar: {len(celdas)} | Periodo: {periodo}")

    resultados: list[dict] = []
    detener_sync = False
    detener_motivo = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=CHROMIUM,
            headless=True,
            args=["--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 1800},
            accept_downloads=True,
        )
        page = context.new_page()

        try:
            # Paso 0 — Login
            login(page)

            # Paso 1 — Abrir solicitud del mes
            abrir_solicitud(page, periodo)
            verificar_solicitud(page, periodo)

            # Paso 2 — PARTE DE TRABAJO ya activa por defecto

            for celda in celdas:
                if detener_sync:
                    resultados.append({
                        "celda_id": celda["celda_id"],
                        "resultado": "error",
                        "motivo": detener_motivo,
                        "detalle": f"Sync detenido por {detener_motivo}",
                        "captura_url": None,
                    })
                    continue

                log(f"--- Celda {celda['celda_id']} | {celda['ubicacion']} | {celda['posicion']} | {celda['fecha']} ---")
                res = procesar_celda(page, celda, sync_id, pdf_dir)
                resultados.append(res)
                log(f"Resultado celda: {res['resultado']} motivo={res['motivo']}")

                # Enviar progreso a Wells
                wells_progreso(sync_id, [res])

                # Detectar condiciones que detienen el sync completo
                if res["motivo"] in ("login_bloqueado", "solicitud_no_encontrada"):
                    detener_sync = True
                    detener_motivo = res["motivo"]
                    log(f"Deteniendo sync por {detener_motivo}")

        except RuntimeError as e:
            msg = str(e)
            log(f"ERROR global: {msg}")
            motivo_global = msg.split(":")[0] if ":" in msg else "error_global"
            # Marcar todas las celdas no procesadas como error
            ids_procesados = {r["celda_id"] for r in resultados}
            for celda in celdas:
                if celda["celda_id"] not in ids_procesados:
                    res = {
                        "celda_id": celda["celda_id"],
                        "resultado": "error",
                        "motivo": motivo_global,
                        "detalle": msg,
                        "captura_url": None,
                    }
                    resultados.append(res)
                    wells_progreso(sync_id, [res])

        except Exception as e:
            log(f"EXCEPCION global: {type(e).__name__}: {e}")
            ids_procesados = {r["celda_id"] for r in resultados}
            for celda in celdas:
                if celda["celda_id"] not in ids_procesados:
                    res = {
                        "celda_id": celda["celda_id"],
                        "resultado": "error",
                        "motivo": "excepcion_global",
                        "detalle": f"{type(e).__name__}: {e}",
                        "captura_url": None,
                    }
                    resultados.append(res)
                    wells_progreso(sync_id, [res])

        finally:
            browser.close()

    # Resultado final
    todos_ok = all(r["resultado"] in ("ok", "ya_existia") for r in resultados)
    alguno_ok = any(r["resultado"] in ("ok", "ya_existia") for r in resultados)
    if todos_ok:
        estado_envio = "completado"
    elif alguno_ok:
        estado_envio = "parcial"
    else:
        estado_envio = "fallido"

    log(f"Enviando resultado final: estado={estado_envio}")
    res_ok, res_body = wells_resultado(sync_id, estado_envio, resultados)

    # Verificar estado final en Wells
    estado_final = wells_get_estado(sync_id)
    log(f"Estado final verificado en Wells: {estado_final}")

    fin = datetime.datetime.now(ARS_TZ)
    duracion = (fin - inicio).total_seconds()
    log(f"=== FIN {fin.isoformat()} | Duración: {duracion:.0f}s ===")
    flush_log()

    # Informe stdout
    ok_n = sum(1 for r in resultados if r["resultado"] == "ok")
    ya_n = sum(1 for r in resultados if r["resultado"] == "ya_existia")
    err_n = sum(1 for r in resultados if r["resultado"] == "error")

    print(f"""
=== INFORME FINAL ===
sync_id: {sync_id}
Estado final en Wells: {estado_final}
Resultado enviado OK: {res_ok}

Resumen: ok={ok_n} ya_existia={ya_n} error={err_n}
""")
    for r in resultados:
        print(f"  celda {r['celda_id']}: {r['resultado']}"
              + (f" [{r['motivo']}] {r['detalle']}" if r["resultado"] == "error" else ""))

    if not res_ok:
        print("\nAVISO: el envío del resultado a Wells falló. Wells NO fue notificado.", file=sys.stderr)

    print(f"\nLog: {_log_path}")


if __name__ == "__main__":
    main()
