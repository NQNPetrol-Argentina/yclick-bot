#!/usr/bin/env python3
"""Bot de carga Y-Click. Uso: bot_yclick.py <sync_json_path>"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

SCRATCHPAD = "/tmp/claude-0/-home-user/72d13551-539f-5350-8de2-ab359451aaf7/scratchpad"
CHROMIUM = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
STEP_LIMIT = 60
TIME_LIMIT_S = 360  # 6 minutos
WELLS_API = "/usr/bin/python3 -I /home/user/scripts/wells_api.py"

BA_TZ = timezone(timedelta(hours=-3))

def now_ba():
    return datetime.now(tz=BA_TZ)

def ts():
    return now_ba().strftime("%H:%M:%S")

LOG = []
def log(msg):
    line = f"[{ts()}] {msg}"
    LOG.append(line)
    print(line, flush=True)


async def screenshot(page, nombre):
    path = f"{SCRATCHPAD}/{nombre}.png"
    try:
        await page.screenshot(path=path, full_page=False)
        log(f"  Captura guardada: {path}")
    except Exception as e:
        log(f"  No se pudo capturar: {e}")
    return None


def escribir_json_tmp(datos, nombre):
    path = f"{SCRATCHPAD}/{nombre}.json"
    with open(path, "w") as f:
        json.dump(datos, f, ensure_ascii=False)
    return path


def llamar_wells(cmd_args):
    """Llama a wells_api.py y retorna (exit_code, stdout, stderr)."""
    cmd = f"{WELLS_API} {cmd_args}"
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    log(f"  wells_api: {cmd_args.split()[0]} -> exit {r.returncode} | stderr: {r.stderr.strip()[:200]}")
    return r.returncode, r.stdout, r.stderr


def reportar_progreso(sync_id, resultado_celda):
    """Envía progreso a Wells para una celda ya procesada."""
    payload = {"celdas": [resultado_celda]}
    path = escribir_json_tmp(payload, f"progreso_{resultado_celda['celda_id'][:8]}")
    code, out, err = llamar_wells(f"progreso {sync_id} {path}")
    if code != 0:
        log(f"  WARN: progreso no enviado (exit={code}): {err[:200]}")
    return code == 0


async def run(sync_data):
    sync_id = sync_data["sync_id"]
    celdas = sync_data["celdas"]
    solicitud = sync_data["solicitud"]
    periodo = solicitud.get("periodo", "")

    meses_es = {"01":"ENE","02":"FEB","03":"MAR","04":"ABR","05":"MAY","06":"JUN",
                 "07":"JUL","08":"AGO","09":"SEP","10":"OCT","11":"NOV","12":"DIC"}
    if "-" in periodo:
        anio, mes_num = periodo.split("-", 1)
    else:
        anio, mes_num = "", ""
    mes_txt = meses_es.get(mes_num, mes_num)
    periodo_yclick = f"{mes_txt}/{anio}"
    log(f"Sync {sync_id} | periodo {periodo_yclick} | {len(celdas)} celda(s)")

    resultados = []
    login_bloqueado = False
    solicitud_no_encontrada = False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=CHROMIUM,
            headless=True,
            args=["--no-sandbox"]
        )
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        # ---- LOGIN ----
        log("Paso 0: Login Y-Click")
        await page.goto("https://yclick.ypf.com/", timeout=60000)
        await page.wait_for_url("**/login**", timeout=60000)
        await page.fill("input[name='username'], input[type='text']", os.environ["USUARIO_YCLICK"])
        await page.fill("input[name='password'], input[type='password']", os.environ["CONTRASENA_YCLICK"])
        await page.click("button[type='submit'], button:has-text('Ingresar')")
        try:
            await page.wait_for_url("**/#/home**", timeout=60000)
            log("  Login OK -> #/home")
        except PWTimeout:
            url_act = page.url
            titulo = await page.title()
            log(f"  Login FALLÓ. URL: {url_act} | Título: {titulo}")
            await screenshot(page, "login_bloqueado")
            login_bloqueado = True
            for celda in celdas:
                r = {
                    "celda_id": celda["celda_id"],
                    "resultado": "error",
                    "motivo": "login_bloqueado",
                    "detalle": f"Login no llegó a #/home. URL={url_act}. Título={titulo}",
                    "captura_url": None
                }
                resultados.append(r)
                reportar_progreso(sync_id, r)
            await browser.close()
            return resultados, login_bloqueado

        # ---- SOLICITUD ----
        log("Paso 1: Buscar solicitud del mes")
        try:
            await page.click("text=Consultar Bandeja de Solicitud de Servicio", timeout=15000)
        except PWTimeout:
            try:
                await page.click("a:has-text('Consultar Bandeja')", timeout=10000)
            except Exception as e:
                log(f"  No se encontró link Consultar Bandeja: {e}")

        await page.wait_for_load_state("networkidle", timeout=20000)
        log("  JS click en Buscar...")
        await page.evaluate("document.querySelector('button[type=\"submit\"]').click()")
        await page.wait_for_load_state("networkidle", timeout=20000)
        await asyncio.sleep(1)

        log("  Click pestaña EN CURSO")
        try:
            await page.click("text=EN CURSO", timeout=10000)
        except PWTimeout:
            try:
                await page.click("*:has-text('EN CURSO')", timeout=5000)
            except Exception as e:
                log(f"  WARN: no se encontró pestaña EN CURSO: {e}")
        await asyncio.sleep(1.5)

        log(f"  Buscando solicitud con periodo '{periodo_yclick}' + 'Desparaf' + 'En Ejecución'")
        # Estrategia: buscar el elemento que contiene el periodo y navegar al MSDS desde ahí
        import re as _re
        solicitud_encontrada = False

        # Buscar todos los elementos que contienen el periodo
        periodo_els = await page.query_selector_all(f"td, span, div")
        msds_a_clickear = None
        for el in periodo_els:
            try:
                txt = (await el.inner_text()).strip()
            except Exception:
                continue
            if periodo_yclick in txt and len(txt) < 20:
                # Este elemento es la celda del periodo. Buscar el MSDS en la misma fila/fila anterior
                log(f"  Elemento periodo encontrado: '{txt}'")
                # Buscar la fila contenedora
                try:
                    fila_el = await el.evaluate_handle("e => e.closest('tr')")
                    fila_txt = await (await fila_el.get_property("innerText")).json_value()
                    log(f"  Fila contenedora: {fila_txt[:150]}")
                    # Intentar click en MSDS dentro de la fila
                    msds_m = _re.search(r"MSDS\d+", fila_txt)
                    if msds_m:
                        msds_a_clickear = msds_m.group(0)
                        log(f"  MSDS en fila: {msds_a_clickear}")
                        break
                    # Si no hay MSDS en la misma fila, buscar en fila anterior (some tables split rows)
                    prev_txt = await el.evaluate("""e => {
                        let tr = e.closest('tr');
                        if (!tr) return '';
                        let prev = tr.previousElementSibling;
                        return prev ? prev.innerText : '';
                    }""")
                    msds_m2 = _re.search(r"MSDS\d+", prev_txt)
                    if msds_m2:
                        msds_a_clickear = msds_m2.group(0)
                        log(f"  MSDS en fila anterior: {msds_a_clickear}")
                        break
                except Exception as e:
                    log(f"  WARN fila: {e}")

        if not msds_a_clickear:
            # Fallback: buscar MSDS directamente en el texto de la tabla
            tabla_txt = ""
            try:
                tabla_txt = await page.inner_text("table", timeout=5000)
            except Exception:
                pass
            # Buscar el MSDS que está cerca de NOV/2026 y Desparaf
            lineas = tabla_txt.split("\n")
            for i, linea in enumerate(lineas):
                if periodo_yclick in linea:
                    # Buscar MSDS en las 10 líneas anteriores
                    contexto = "\n".join(lineas[max(0,i-10):i+2])
                    msds_m = _re.search(r"MSDS\d+", contexto)
                    desparaf_cerca = any("Desparaf" in l or "desparaf" in l.lower() for l in lineas[max(0,i-10):i+2])
                    if msds_m and desparaf_cerca:
                        msds_a_clickear = msds_m.group(0)
                        log(f"  MSDS fallback por contexto: {msds_a_clickear}")
                        break

        if msds_a_clickear:
            try:
                await page.click(f"text={msds_a_clickear}", timeout=8000)
                solicitud_encontrada = True
                log(f"  Click en {msds_a_clickear} OK")
            except Exception as e:
                log(f"  WARN click {msds_a_clickear}: {e}")
                # Último intento: buscar el elemento por texto exacto
                els = await page.query_selector_all(f"*")
                for el in els:
                    try:
                        t = (await el.inner_text()).strip()
                        if t == msds_a_clickear:
                            await el.click(timeout=3000)
                            solicitud_encontrada = True
                            break
                    except Exception:
                        pass

        if not solicitud_encontrada:
            await screenshot(page, "solicitud_no_encontrada")
            contenido_tabla = ""
            try:
                contenido_tabla = await page.inner_text("table", timeout=5000)
            except Exception:
                pass
            log(f"  solicitud_no_encontrada. Tabla: {contenido_tabla[:300]}")
            solicitud_no_encontrada = True
            for celda in celdas:
                r = {
                    "celda_id": celda["celda_id"],
                    "resultado": "error",
                    "motivo": "solicitud_no_encontrada",
                    "detalle": f"No se encontró 'OYM - Desparafinación' + '{periodo_yclick}' + 'En Ejecución'. Contenido tabla: {contenido_tabla[:400]}",
                    "captura_url": None
                }
                resultados.append(r)
                reportar_progreso(sync_id, r)
            await browser.close()
            return resultados, False

        await page.wait_for_url("**/detailmonthly/**", timeout=30000)
        log(f"  Solicitud abierta: {page.url}")
        await page.wait_for_load_state("networkidle", timeout=20000)

        # ---- CELDAS ----
        for celda in celdas:
            celda_id = celda["celda_id"]
            tipo_ub = celda["tipo_ubicacion"]
            ubicacion = celda["ubicacion"]
            posicion_str = celda["posicion"]
            posicion_codigo = posicion_str.split(" - ")[0].strip()
            linea_codigo = celda["linea"]
            fecha_str = celda["fecha"]
            cantidad = celda["cantidad"]
            unidad = celda["unidad"]
            doc_respaldo = celda.get("doc_respaldo")
            adjuntos = celda.get("adjuntos", [])

            log(f"\n--- Celda {celda_id[:8]}: {ubicacion} pos={posicion_codigo} linea={linea_codigo} {fecha_str} cant={cantidad}{unidad} ---")
            inicio_celda = time.time()
            pasos = [0]

            def chk_tope():
                pasos[0] += 1
                elapsed = time.time() - inicio_celda
                if pasos[0] >= STEP_LIMIT or elapsed >= TIME_LIMIT_S:
                    raise Exception(f"tope_excedido: pasos={pasos[0]}, tiempo={elapsed:.0f}s")

            # Mapeo conocido
            yclick_tipo = tipo_ub
            yclick_ubicacion = ubicacion
            yclick_activo = None
            if "COLECTOR LOC 56" in ubicacion.upper():
                yclick_tipo = "Locación - Operación y Mantenimiento"
                yclick_ubicacion = "LOCACION N° 56"
                yclick_activo = "LA AMARGA CHICA"
                log(f"  Mapeo: '{ubicacion}' -> tipo='{yclick_tipo}', nombre='{yclick_ubicacion}', activo='{yclick_activo}'")

            # Descargar PDF
            pdf_local = None
            pdf_error = None
            for adj in adjuntos:
                url_firmada = adj.get("url_firmada", "")
                archivo = adj.get("archivo", f"parte-{adj.get('parte','?')}.pdf")
                pdf_local = f"{SCRATCHPAD}/{archivo}"
                log(f"  Descargando PDF parte {adj.get('parte')}...")
                ret = subprocess.run(
                    ["/usr/bin/python3", "-I", "/home/user/scripts/wells_api.py", "download", url_firmada, pdf_local],
                    capture_output=True, text=True, timeout=120
                )
                log(f"  download exit={ret.returncode} | {ret.stderr.strip()[:150]}")
                if ret.returncode != 0 or not os.path.exists(pdf_local):
                    pdf_error = f"download exit={ret.returncode}: {ret.stderr.strip()[:200]}"
                    pdf_local = None
                    break

            if pdf_error:
                r = {
                    "celda_id": celda_id,
                    "resultado": "error",
                    "motivo": "pdf_no_descargado",
                    "detalle": pdf_error,
                    "captura_url": None
                }
                resultados.append(r)
                reportar_progreso(sync_id, r)
                continue

            linea_texto_completo = None

            try:
                # Paso 2: PARTE DE TRABAJO
                chk_tope()
                log("  Paso 2: pestaña PARTE DE TRABAJO")
                for _tab_sel in ["li:has-text('PARTE DE TRABAJO')", "a:has-text('PARTE DE TRABAJO')"]:
                    try:
                        tab_pt = await page.query_selector(_tab_sel)
                        if tab_pt:
                            await tab_pt.click(timeout=5000)
                            break
                    except Exception:
                        pass
                await page.wait_for_load_state("networkidle", timeout=10000)
                await asyncio.sleep(1)

                # Paso 3: Ubicación
                chk_tope()
                log(f"  Paso 3: Verificar/agregar ubicación '{yclick_ubicacion}'")

                body_txt_ub = await page.inner_text("body", timeout=5000)
                ubicacion_ya_existe = yclick_ubicacion.upper() in body_txt_ub.upper()
                if ubicacion_ya_existe:
                    log(f"  Ubicación ya en lista (detectada en body)")

                async def fill_generic_select_by_label(label_txt, search_term, match_term=None, wait_s=3.0):
                    """Llena un React Select buscando el .generic-select con la etiqueta dada.
                    search_term: lo que se tipea para traer opciones.
                    match_term: substring que debe tener la opción elegida (por defecto = search_term).
                    """
                    if match_term is None:
                        match_term = search_term
                    gs = await page.query_selector_all(".generic-select")
                    target = None
                    for s in gs:
                        lbl = await s.query_selector("label")
                        if lbl and label_txt.lower() in (await lbl.inner_text()).lower():
                            r = await s.bounding_box()
                            if r and r["x"] >= 0:
                                target = s
                                break
                    if not target:
                        raise Exception(f"No se encontró .generic-select con label '{label_txt}'")
                    ctrl = await target.query_selector("[class*='-control']")
                    if not ctrl:
                        raise Exception(f"No se encontró control en .generic-select '{label_txt}'")
                    await ctrl.click(timeout=5000)
                    await asyncio.sleep(0.8)
                    await page.keyboard.type(search_term, delay=60)
                    await asyncio.sleep(wait_s)
                    opts = await page.query_selector_all("[class*='-option']")
                    disponibles = []
                    for op in opts:
                        try:
                            t = (await op.inner_text()).strip()
                            if t:
                                disponibles.append(t)
                                if match_term.lower() in t.lower():
                                    await op.click()
                                    return t
                        except Exception:
                            pass
                    log(f"  Opciones para '{search_term}' (match='{match_term}'): {disponibles[:10]}")
                    return None

                if not ubicacion_ya_existe:
                    chk_tope()
                    log("  Abriendo modal 'Agregar ubicación'...")
                    await page.click("button:has-text('Agregar ubicación'), a:has-text('Agregar ubicación')", timeout=10000)
                    await asyncio.sleep(1.5)

                    # Tipo de ubicación
                    chk_tope()
                    log(f"  Tipo: '{yclick_tipo}'")
                    kw_tipo = "Locaci" if "Locaci" in yclick_tipo else "Pozo"
                    tipo_elegido = await fill_generic_select_by_label("Tipo de ubicaci", kw_tipo)
                    if tipo_elegido:
                        log(f"  Tipo elegido: {tipo_elegido}")
                    else:
                        log(f"  WARN: sin opciones para tipo '{kw_tipo}', continuando sin tipo")
                    await asyncio.sleep(2)  # esperar que aparezca el campo de ubicación

                    # Campo de ubicación: label y término de búsqueda dependen del tipo elegido
                    # Pozo → "Ubicación pozo" / Locación → "Ubicación locación"
                    chk_tope()
                    import re as _re_ub
                    if kw_tipo == "Pozo":
                        label_ub = "pozo"
                        # Buscar sin sufijo entre paréntesis: "LLL-1283(h)" -> "LLL-1283"
                        search_ub = _re_ub.sub(r'\s*\([^)]*\)\s*$', '', yclick_ubicacion).strip()
                        match_ub = yclick_ubicacion
                        log(f"  Ubicación pozo: search='{search_ub}' match='{match_ub}'")
                    else:
                        label_ub = "locaci"
                        search_ub = "LOC"
                        num_loc = _re_ub.search(r'\d+', yclick_ubicacion)
                        match_ub = num_loc.group(0) if num_loc else yclick_ubicacion[:8]
                        log(f"  Ubicación locación: '{yclick_ubicacion}'")
                    ub_elegida = await fill_generic_select_by_label(label_ub, search_ub, match_term=match_ub, wait_s=4.0)
                    if not ub_elegida:
                        await screenshot(page, f"ub_noencontrada_{celda_id[:8]}")
                        raise Exception(f"ubicacion_no_encontrada: '{yclick_ubicacion}' (match='{match_ub}') no aparece en opciones")
                    log(f"  Ubicación elegida: {ub_elegida}")

                    await asyncio.sleep(0.8)
                    chk_tope()
                    # Cerrar cualquier info-modal que haya aparecido tras la selección
                    for info_sel in [
                        ".info-modal button:has-text('Continuar')",
                        ".info-modal button:has-text('Aceptar')",
                        ".info-modal button:has-text('OK')",
                        ".info-modal .btn-primary",
                        ".info-modal .btn",
                    ]:
                        try:
                            info_btn = await page.query_selector(info_sel)
                            if info_btn and await info_btn.is_visible():
                                await info_btn.click(timeout=3000)
                                log(f"  Info-modal cerrado con '{info_sel}'")
                                await asyncio.sleep(1)
                                break
                        except Exception:
                            pass
                    # Botón confirmar: usar JS click para esquivar cualquier overlay restante
                    confirmar_clicked = False
                    for btn_txt in ["Agregar", "Continuar", "Agregar y continuar"]:
                        try:
                            btn_el = await page.query_selector(f"button:has-text('{btn_txt}')")
                            if btn_el and await btn_el.is_enabled():
                                await btn_el.evaluate("b => b.click()")
                                confirmar_clicked = True
                                log(f"  Confirmado con '{btn_txt}'")
                                break
                        except Exception:
                            pass
                    if not confirmar_clicked:
                        raise Exception("No se pudo hacer click en Agregar/Continuar del modal ubicación")
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    await asyncio.sleep(1)
                    # "Agregar y continuar" abre otro modal vacío para seguir agregando — cerrarlo
                    try:
                        salir_btn = await page.query_selector(".modal.show button:has-text('Salir')")
                        if salir_btn and await salir_btn.is_visible():
                            await salir_btn.evaluate("b => b.click()")
                            log("  Segundo modal 'Agregar ubicación' cerrado con Salir")
                            await asyncio.sleep(1)
                    except Exception:
                        pass
                    log("  Modal ubicación cerrado")

                # Paso 4: Expandir card LOCACION, click "Agregar líneas", llenar modal
                chk_tope()
                log("  Paso 4: Expandir card ubicación y agregar posición/línea")
                anio_f, mes_f, dia_f = fecha_str.split("-")
                dia_num_p4 = int(dia_f)

                # Helper: leer el día actual del picker de fecha
                async def get_picker_day():
                    for _el in await page.query_selector_all("div, span"):
                        try:
                            _txt = (await _el.inner_text()).strip()
                            if re.search(r'\d{1,2}\s+de\s+\w+\s+de\s+\d{4}', _txt):
                                _bb = await _el.bounding_box()
                                if _bb and 80 < _bb["width"] < 600 and _bb["height"] < 80:
                                    _m = re.search(r'(\d{1,2})\s+de', _txt)
                                    if _m:
                                        return int(_m.group(1)), _txt
                        except Exception:
                            pass
                    return None, None

                # Helper: navegar el picker al día target
                async def navigate_picker_to(target_day):
                    for _nav_i in range(35):
                        _curr, _curr_txt = await get_picker_day()
                        if _curr is None:
                            log("  Picker no encontrado")
                            break
                        if _curr == target_day:
                            log(f"  Picker en día {target_day}: {_curr_txt}")
                            break
                        _dir = ">" if _curr < target_day else "<"
                        _nav_clicked = False
                        for _btn in await page.query_selector_all("button"):
                            try:
                                _t = (await _btn.inner_text()).strip()
                                _bb = await _btn.bounding_box()
                                if not _bb or _bb["y"] > 700:
                                    continue
                                if _dir == ">" and _t in (">", "›", "▶", "»"):
                                    await _btn.click(timeout=2000)
                                    _nav_clicked = True
                                    break
                                if _dir == "<" and _t in ("<", "‹", "◀", "«"):
                                    await _btn.click(timeout=2000)
                                    _nav_clicked = True
                                    break
                            except Exception:
                                pass
                        if not _nav_clicked:
                            # Fallback: buscar botón SVG de navegación por posición relativa al picker
                            _curr2, _curr_txt2 = await get_picker_day()
                            if _curr_txt2:
                                await page.evaluate(f"""() => {{
                                    const els = Array.from(document.querySelectorAll('*'));
                                    const dateEl = els.find(e => {{
                                        const t = e.innerText ? e.innerText.trim() : '';
                                        return t.length > 10 && t.length < 100 && /\\d{{1,2}} de/.test(t);
                                    }});
                                    if (!dateEl) return;
                                    const dateR = dateEl.getBoundingClientRect();
                                    const btns = Array.from(document.querySelectorAll('button'));
                                    const btn = btns.find(b => {{
                                        const r = b.getBoundingClientRect();
                                        const inRow = Math.abs(r.top - dateR.top) < 50;
                                        return inRow && ('{_dir}' === '>' ? r.left > dateR.right - 10 : r.right < dateR.left + 10);
                                    }});
                                    if (btn) btn.click();
                                }}""")
                        await asyncio.sleep(0.3)

                # Helper: expandir la card de la ubicación
                async def expandir_card():
                    for _el in await page.query_selector_all("div, span"):
                        try:
                            _txt = (await _el.inner_text()).strip()
                            if yclick_ubicacion in _txt and len(_txt) < 60:
                                _loc_bb = await _el.bounding_box()
                                if _loc_bb and _loc_bb["x"] > 50:
                                    for _rc in await page.query_selector_all(".rightContent"):
                                        try:
                                            _rc_bb = await _rc.bounding_box()
                                            if _rc_bb and abs(_rc_bb["y"] - _loc_bb["y"]) < 70 and _rc_bb["x"] > 1100:
                                                await _rc.click(timeout=3000)
                                                await asyncio.sleep(1.5)
                                                return True
                                        except Exception:
                                            pass
                        except Exception:
                            pass
                    return False

                # 1. Expandir la card
                _exp_ok = await expandir_card()
                if _exp_ok:
                    log("  Card expandida (click en chevron)")
                else:
                    log("  WARN: no se encontró el div de ubicación para expandir la card")

                # 2. Verificar si ya tiene líneas: el botón "Agregar líneas" solo aparece cuando NO hay líneas
                #    Cuando hay líneas, ese botón desaparece y aparece el picker de fecha
                await asyncio.sleep(0.5)
                _btn_al = await page.query_selector("button:has-text('Agregar líneas')")
                _btn_al_visible = False
                if _btn_al:
                    try:
                        _btn_al_visible = await _btn_al.is_visible()
                    except Exception:
                        pass
                _linea_marker = f"{posicion_codigo} - "
                linea_ya_existe = not _btn_al_visible
                if linea_ya_existe:
                    log(f"  Línea Posición {posicion_codigo} ya existe (botón 'Agregar líneas' no visible)")
                else:
                    # Click en "Agregar líneas" (el botón está visible, puede estar fuera del viewport)
                    chk_tope()
                    log("  Abriendo modal 'Agregar líneas'...")
                    if _btn_al:
                        await _btn_al.evaluate("b => b.scrollIntoView({block:'center'})")
                        await asyncio.sleep(0.3)
                        await _btn_al.evaluate("b => b.click()")
                    else:
                        await page.evaluate("window.scrollBy(0, 300)")
                        await asyncio.sleep(0.3)
                        await page.click("button:has-text('Agregar líneas')", timeout=8000)
                    await asyncio.sleep(1.5)

                    # Posición
                    chk_tope()
                    log(f"  Posición código: {posicion_codigo}")
                    pos_elegida = await fill_generic_select_by_label("Posici", posicion_codigo)
                    if not pos_elegida:
                        await screenshot(page, f"no_pos_{celda_id[:8]}")
                        raise Exception(f"posicion_no_encontrada: código {posicion_codigo}")
                    log(f"  Posición: {pos_elegida}")
                    await asyncio.sleep(0.5)

                    # Línea
                    chk_tope()
                    log(f"  Línea código: {linea_codigo}")
                    lin_elegida = await fill_generic_select_by_label("nea", linea_codigo)
                    if not lin_elegida:
                        await screenshot(page, f"no_lin_{celda_id[:8]}")
                        raise Exception(f"linea_no_encontrada: código {linea_codigo}")
                    linea_texto_completo = lin_elegida
                    log(f"  Línea: {lin_elegida}")
                    await asyncio.sleep(0.5)

                    # Realización de la tarea: fecha específica (Desde y Hasta = fecha_str)
                    chk_tope()
                    log(f"  Fecha realización: {fecha_str}")
                    fecha_inputs_modal = await page.query_selector_all(".modal.show input[type='date']")
                    for _fi in fecha_inputs_modal[:2]:
                        try:
                            await _fi.fill(fecha_str)
                        except Exception:
                            pass
                    # Fallback: cualquier input date visible en el modal
                    if not fecha_inputs_modal:
                        for _fi in await page.query_selector_all("input[type='date']"):
                            try:
                                if await _fi.is_visible():
                                    await _fi.fill(fecha_str)
                            except Exception:
                                pass
                    await asyncio.sleep(0.3)

                    # Aceptar
                    chk_tope()
                    for _btn_txt in ["Aceptar", "Guardar", "Confirmar"]:
                        try:
                            _btn = await page.query_selector(f".modal.show button:has-text('{_btn_txt}')")
                            if _btn and await _btn.is_enabled():
                                await _btn.evaluate("b => b.click()")
                                log(f"  Modal cerrado con '{_btn_txt}'")
                                break
                        except Exception:
                            pass
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    await asyncio.sleep(1.5)
                    log("  Modal posición/línea cerrado")

                # Paso 5: Cantidad en el grid diario
                chk_tope()
                log(f"  Paso 5: Cargar {cantidad}{unidad} en día {dia_f}")
                dia_num = int(dia_f)

                await screenshot(page, f"paso5_antes_nav_{celda_id[:8]}")

                # Navegar al día correcto en el picker de fecha
                # Estructura real del picker: div.calendar-container con 5 hijos:
                #   [0]<< div[aria-hidden], [1]< div[aria-hidden], [2]content div, [3]> div[aria-hidden], [4]>> div[aria-hidden]
                # El input text es input#date-input-calendar (react-datepicker)
                # El display de fecha es div.date-container#calendar-div

                # Scroll al picker visible (el que tiene y > 0)
                await page.evaluate("""() => {
                    const cals = Array.from(document.querySelectorAll('.calendar-container'));
                    const vis = cals.find(c => c.getBoundingClientRect().y > 100);
                    if (vis) vis.scrollIntoView({block: 'center'});
                }""")
                await asyncio.sleep(0.5)

                # Intentar poner la fecha directamente via input (React DatePicker)
                _date_set = await page.evaluate(f"""() => {{
                    const cals = Array.from(document.querySelectorAll('.calendar-container'));
                    const visCal = cals.find(c => c.getBoundingClientRect().y > 100);
                    if (!visCal) return false;
                    const inp = visCal.querySelector('input[id="date-input-calendar"], input[type="text"]');
                    if (!inp) return false;
                    // Simular cambio con React
                    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    // Formato: DD/MM/YYYY
                    const dia = '{int(dia_f):02d}';
                    const mes = '{mes_f}';
                    const anio = '{anio_f}';
                    const dateStr = dia + '/' + mes + '/' + anio;
                    nativeSetter.call(inp, dateStr);
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return dateStr;
                }}""")
                if _date_set:
                    log(f"  Fecha seteada via input: {_date_set}")
                    await asyncio.sleep(0.5)
                else:
                    log("  No se pudo setear fecha via input, usando navegación por clicks")

                for _nav_i in range(35):
                    _picker_info = await page.evaluate("""() => {
                        // Buscar el calendar-container visible (y > 100)
                        const cals = Array.from(document.querySelectorAll('.calendar-container'));
                        const visCal = cals.find(c => c.getBoundingClientRect().y > 100);
                        if (!visCal) return null;
                        // Leer la fecha actual del div.date-container
                        const dateDiv = visCal.querySelector('.date-container, #calendar-div');
                        if (!dateDiv) return null;
                        const dateText = (dateDiv.innerText || '').trim();
                        const m = dateText.match(/(\\d{1,2})\\s+de/);
                        const day = m ? parseInt(m[1]) : null;
                        // Obtener los divs de navegación (aria-hidden)
                        const ariaDivs = Array.from(visCal.querySelectorAll(':scope > div[aria-hidden="true"]'));
                        // [0]=<<, [1]=<, [2]=>, [3]=>>
                        const prevBtn = ariaDivs[1];
                        const nextBtn = ariaDivs[2];
                        const prevR = prevBtn ? prevBtn.getBoundingClientRect() : null;
                        const nextR = nextBtn ? nextBtn.getBoundingClientRect() : null;
                        return {
                            day: day,
                            dateText: dateText,
                            prevX: prevR ? prevR.x + prevR.width/2 : null,
                            prevY: prevR ? prevR.y + prevR.height/2 : null,
                            nextX: nextR ? nextR.x + nextR.width/2 : null,
                            nextY: nextR ? nextR.y + nextR.height/2 : null,
                            numAriaDivs: ariaDivs.length
                        };
                    }""")
                    if not _picker_info or _picker_info["day"] is None:
                        log("  Picker de fecha no visible todavía")
                        break
                    _curr_day = _picker_info["day"]
                    if _nav_i == 0:
                        log(f"  Picker: día={_curr_day} target={dia_num} nearby={_picker_info.get('nearby', [])[:4]}")
                    if _curr_day == dia_num:
                        log(f"  Picker en día {dia_num}: {_picker_info['dateText']}")
                        break
                    # Navegar con mouse click directo en el botón correcto
                    if _curr_day < dia_num and _picker_info["nextX"]:
                        await page.mouse.click(_picker_info["nextX"], _picker_info["nextY"])
                    elif _curr_day > dia_num and _picker_info["prevX"]:
                        await page.mouse.click(_picker_info["prevX"], _picker_info["prevY"])
                    else:
                        log(f"  No se encontró botón nav (curr={_curr_day}, target={dia_num}, nearby={_picker_info.get('nearby', [])[:6]})")
                        break
                    await asyncio.sleep(0.3)
                await asyncio.sleep(0.5)

                await screenshot(page, f"paso5_post_nav_{celda_id[:8]}")
                await screenshot(page, f"paso5_grid_{celda_id[:8]}")

                # Buscar el input de cantidad en la fila de la línea
                cantidad_input = None

                # Encontrar la y de la fila de la línea
                linea_row_y = None
                for _search_txt in [posicion_codigo, linea_codigo, "NOC ESTE", "EQ.HOT OIL"]:
                    for _el in await page.query_selector_all("div, span, td"):
                        try:
                            _txt = (await _el.inner_text()).strip()
                            if _search_txt in _txt and len(_txt) < 100:
                                _bb = await _el.bounding_box()
                                if _bb and _bb["x"] > 50 and _bb["y"] > 100:
                                    linea_row_y = _bb["y"]
                                    log(f"  Fila de línea en y≈{linea_row_y:.0f} ('{_search_txt}')")
                                    break
                        except Exception:
                            pass
                    if linea_row_y:
                        break

                # Buscar input en la misma fila
                if linea_row_y:
                    for _inp in await page.query_selector_all("input"):
                        try:
                            if await _inp.is_visible():
                                _bb = await _inp.bounding_box()
                                _ro = await _inp.get_attribute("readonly")
                                _dis = await _inp.get_attribute("disabled")
                                if _bb and _bb["x"] > 0 and abs(_bb["y"] - linea_row_y) < 50 and not _ro and not _dis:
                                    cantidad_input = _inp
                                    log(f"  Input en fila de línea en ({_bb['x']:.0f},{_bb['y']:.0f})")
                                    break
                        except Exception:
                            pass

                # Estrategia 2: todos los inputs editables visibles
                if not cantidad_input:
                    all_inp = await page.query_selector_all("input[type='number'], input[type='text'], input:not([type])")
                    vis_inp = []
                    for _inp in all_inp:
                        try:
                            if await _inp.is_visible():
                                _bb = await _inp.bounding_box()
                                _ro = await _inp.get_attribute("readonly")
                                _dis = await _inp.get_attribute("disabled")
                                if _bb and _bb["x"] > 100 and not _ro and not _dis:
                                    vis_inp.append((_bb["x"], _bb["y"], _inp))
                        except Exception:
                            pass
                    log(f"  Inputs editables visibles: {len(vis_inp)}")
                    if vis_inp:
                        vis_inp.sort(key=lambda t: (t[1], t[0]))
                        cantidad_input = vis_inp[0][2]
                        log(f"  Primer input en ({vis_inp[0][0]:.0f},{vis_inp[0][1]:.0f})")

                if not cantidad_input:
                    await screenshot(page, f"no_input_cant_{celda_id[:8]}")
                    raise Exception("No se encontró el input de cantidad")

                val_actual = (await cantidad_input.input_value()).strip()
                if val_actual and val_actual not in ("0", "", "null", "0.00", "-"):
                    log(f"  ya_existia: valor='{val_actual}'")
                    r = {
                        "celda_id": celda_id,
                        "resultado": "ya_existia",
                        "motivo": "ya_existia",
                        "detalle": f"Caja ya tiene '{val_actual}' para {fecha_str} pos {posicion_codigo} línea {linea_codigo}",
                        "captura_url": None
                    }
                    resultados.append(r)
                    reportar_progreso(sync_id, r)
                    continue

                await cantidad_input.click(timeout=3000)
                await asyncio.sleep(0.3)
                await page.keyboard.press("Control+a")
                await cantidad_input.fill(str(cantidad))
                await asyncio.sleep(0.3)
                val_leido = (await cantidad_input.input_value()).strip()
                if val_leido != str(cantidad):
                    raise Exception(f"valor_no_coincide: se escribio {cantidad} pero caja muestra '{val_leido}'")
                log(f"  Cantidad {cantidad} cargada y verificada")

                # Paso 6: Verificar y enviar
                chk_tope()
                log("  Paso 6: Verificar y Enviar a aprobar")
                # Blur el input para que el botón Enviar a aprobar se habilite
                await page.keyboard.press("Tab")
                await asyncio.sleep(0.5)
                await screenshot(page, f"antes_envio_{celda_id[:8]}")
                # Buscar el botón "Enviar a aprobar" habilitado y visible
                _enviar_ok = False
                for _ei in range(20):
                    _btns_enviar = await page.query_selector_all("button:has-text('Enviar a aprobar')")
                    for _be in _btns_enviar:
                        try:
                            _be_bb = await _be.bounding_box()
                            _be_en = await _be.is_enabled()
                            if _be_bb and _be_en and _be_bb["y"] > 100 and _be_bb["x"] > 0:
                                await _be.evaluate("b => b.scrollIntoView({block:'center'})")
                                await asyncio.sleep(0.3)
                                await _be.evaluate("b => b.click()")
                                _enviar_ok = True
                                log(f"  Click 'Enviar a aprobar' en ({_be_bb['x']:.0f},{_be_bb['y']:.0f})")
                                break
                        except Exception:
                            pass
                    if _enviar_ok:
                        break
                    await asyncio.sleep(0.5)
                if not _enviar_ok:
                    raise Exception("boton_enviar_no_habilitado")
                await page.wait_for_load_state("networkidle", timeout=15000)
                await asyncio.sleep(1.5)
                log("  Enviado a aprobar")
                await screenshot(page, f"post_envio_{celda_id[:8]}")

                # Paso 7: doc_respaldo
                if doc_respaldo:
                    chk_tope()
                    log(f"  Paso 7: doc_respaldo '{doc_respaldo}'")
                    try:
                        await page.click(
                            "tr:has-text('barras'), [title*='barras'], [class*='barcode'], button[title*='respaldo']",
                            timeout=5000
                        )
                        await asyncio.sleep(0.5)
                        inp_doc = await page.query_selector(
                            "input[name*='respaldo'], input[placeholder*='respaldo'], input[placeholder*='Documento'], input[placeholder*='documento']"
                        )
                        if inp_doc:
                            await inp_doc.fill(doc_respaldo)
                            await page.click("button:has-text('Guardar'), button:has-text('OK'), button:has-text('Aceptar')", timeout=5000)
                            log(f"  doc_respaldo '{doc_respaldo}' cargado")
                    except Exception as e:
                        log(f"  WARN doc_respaldo (no critico): {e}")

                # Paso 8: Adjuntar PDF
                if pdf_local and os.path.exists(pdf_local):
                    chk_tope()
                    log(f"  Paso 8: Adjuntar PDF {pdf_local}")
                    try:
                        # Encontrar el ícono de adjunto en la fila de la línea
                        # La fila tiene: texto posición | texto línea | unidad | input cantidad | iconos
                        # El ícono de adjunto (documento) está cerca de linea_row_y
                        adj_clicked = False
                        # Buscar por aria-label, title, o class en la zona de la fila
                        for _adj_sel in [
                            "[aria-label*='djunt'], [title*='djunt']",
                            "[aria-label*='archivo'], [title*='archivo']",
                            "[class*='attach'], [class*='file-upload']",
                            "[class*='document'], [class*='doc']",
                        ]:
                            try:
                                _adj_els = await page.query_selector_all(_adj_sel)
                                for _adj_el in _adj_els:
                                    _bb = await _adj_el.bounding_box()
                                    if _bb and linea_row_y and abs(_bb["y"] - linea_row_y) < 60:
                                        await _adj_el.click(timeout=3000)
                                        adj_clicked = True
                                        log(f"  Ícono adjunto clickeado ({_adj_sel})")
                                        break
                            except Exception:
                                pass
                            if adj_clicked:
                                break

                        # Fallback: buscar ícono SVG de documento en la fila
                        if not adj_clicked and linea_row_y:
                            svgs_en_fila = await page.evaluate(f"""() => {{
                                const svgs = Array.from(document.querySelectorAll('svg, button'));
                                return svgs
                                    .filter(e => {{
                                        const r = e.getBoundingClientRect();
                                        return r.x > 1200 && Math.abs(r.y - {linea_row_y}) < 60;
                                    }})
                                    .map((e, i) => ({{idx: i, x: e.getBoundingClientRect().x, y: e.getBoundingClientRect().y, tag: e.tagName, cls: e.className}}));
                            }}""")
                            log(f"  SVGs/botones en fila: {svgs_en_fila}")
                            # El ícono de adjunto es típicamente el 3ro de derecha a izquierda
                            if len(svgs_en_fila) >= 3:
                                target_icon = sorted(svgs_en_fila, key=lambda x: x["x"])[-3]
                                await page.mouse.click(target_icon["x"] + 5, target_icon["y"] + 5)
                                adj_clicked = True
                                log(f"  Click en ícono adjunto por posición ({target_icon['x']:.0f},{target_icon['y']:.0f})")

                        if adj_clicked:
                            await asyncio.sleep(0.8)
                            # Buscar "Agregar adjunto" o "Subir archivo" en el popup/modal
                            for _add_txt in ["Agregar adjunto", "Subir", "Agregar archivo"]:
                                try:
                                    _btn = await page.query_selector(f"text={_add_txt}")
                                    if _btn:
                                        await _btn.click(timeout=3000)
                                        await asyncio.sleep(0.3)
                                        break
                                except Exception:
                                    pass
                            async with page.expect_file_chooser(timeout=5000) as fc_info:
                                await page.click("input[type='file'], button:has-text('Seleccionar'), button:has-text('Elegir')", timeout=3000)
                            fc = await fc_info.value
                            await fc.set_files(pdf_local)
                            await asyncio.sleep(0.5)
                            await page.click("button:has-text('Agregar'), button:has-text('Subir'), button:has-text('OK')", timeout=5000)
                            await page.wait_for_load_state("networkidle", timeout=20000)
                            log("  PDF adjuntado")
                        else:
                            raise Exception("No se encontró ícono de adjunto en la fila")
                    except Exception as e:
                        log(f"  WARN adjunto (no critico, horas ya enviadas): {e}")

                detalle = f"Cargado y enviado. Línea completa: {linea_texto_completo}. Pasos: {pasos[0]}. Tiempo: {time.time()-inicio_celda:.0f}s."
                r = {
                    "celda_id": celda_id,
                    "resultado": "ok",
                    "motivo": None,
                    "detalle": detalle,
                    "captura_url": None
                }
                resultados.append(r)
                reportar_progreso(sync_id, r)

            except Exception as e:
                motivo_raw = str(e)
                if "tope_excedido" in motivo_raw:
                    motivo = "tope_excedido"
                elif "ubicacion_no_encontrada" in motivo_raw:
                    motivo = "ubicacion_no_encontrada"
                elif "ubicacion_no_soportada" in motivo_raw:
                    motivo = "ubicacion_no_soportada"
                elif "posicion_no_encontrada" in motivo_raw:
                    motivo = "posicion_no_encontrada"
                elif "linea_no_encontrada" in motivo_raw:
                    motivo = "linea_no_encontrada"
                elif "valor_no_coincide" in motivo_raw:
                    motivo = "valor_no_coincide"
                elif "login_bloqueado" in motivo_raw:
                    motivo = "login_bloqueado"
                else:
                    motivo = "error_inesperado"
                log(f"  ERROR {celda_id[:8]}: {motivo} | {motivo_raw}")
                await screenshot(page, f"error_{celda_id[:8]}_{motivo}")
                r = {
                    "celda_id": celda_id,
                    "resultado": "error",
                    "motivo": motivo,
                    "detalle": motivo_raw[:4000],
                    "captura_url": None
                }
                resultados.append(r)
                reportar_progreso(sync_id, r)

        await browser.close()

    return resultados, login_bloqueado


if __name__ == "__main__":
    sync_json_path = sys.argv[1] if len(sys.argv) > 1 else f"{SCRATCHPAD}/sync.json"
    with open(sync_json_path) as f:
        sync_data = json.load(f)

    sync_id = sync_data["sync_id"]
    celdas = sync_data["celdas"]

    resultados, _ = asyncio.run(run(sync_data))

    # Cerrar el sync enviando resultado final a Wells
    estado_final = "completado"
    payload_resultado = {"estado": estado_final, "celdas": resultados}
    path_resultado = escribir_json_tmp(payload_resultado, "resultado_final")
    code_r, out_r, err_r = llamar_wells(f"resultado {sync_id} {path_resultado}")
    if code_r != 0:
        log(f"  WARN: resultado final no enviado (exit={code_r}): {err_r[:200]}")

    # Log completo
    print(json.dumps({"sync_id": sync_id, "resultados": resultados}, ensure_ascii=False, indent=2))
