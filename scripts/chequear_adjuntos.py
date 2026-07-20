"""
Chequeo liviano de adjuntos rotos en RDU.

A diferencia de main.py (que abre cada ítem con Playwright, llama a una IA y
necesita login para EDITAR), este script solo LEE — nunca escribe ni modifica
nada en RDU. El resultado es un reporte en CSV (reportes/adjuntos_rotos.csv)
para revisar a mano, marcado con [ADJUNTO NO FUNCIONA].

Tres chequeos, todos filtrables por tipo de ítem y rango de años (variables
TIPOS / ANIO_DESDE / ANIO_HASTA):
  1. RÁPIDO (siempre completo, cada corrida): ítems PÚBLICOS sin NINGÚN
     archivo en el bundle ORIGINAL. Usa el filtro nativo de DSpace
     `has_content_in_original_bundle=false`, así que es liviano sin importar
     el tamaño del repositorio. No requiere login.
  2. GRADUAL (avanza de a tramos, con checkpoint): recorre los ítems
     PÚBLICOS que sí tienen archivo pero pesa UMBRAL_BYTES_VACIO bytes o
     menos (los vacíos observados en RDU pesan 42 bytes). Pedirle a la API
     los bitstreams de cada ítem es lento del lado del servidor, así que
     recorrer TODO el repositorio de una sola corrida sería demasiado lento.
     Por eso cada corrida procesa hasta MAX_PAGINAS_POR_CORRIDA páginas y
     guarda dónde quedó en reportes/checkpoint_adjuntos.txt; la corrida
     siguiente retoma ahí, y al llegar al final vuelve a arrancar. No
     requiere login (usa la búsqueda pública).
  3. WORKFLOW (opcional, solo si hay RDU_USER/RDU_PASS configurados): ítems
     que TODAVÍA NO son públicos porque están en el circuito de revisión de
     DSpace (enviados, en aprobación) — esos no aparecen en la búsqueda
     anónima, así que hace falta autenticarse contra la API con las mismas
     credenciales que ya usa main.py. Este chequeo es best-effort: si algo
     falla (credenciales ausentes, permisos insuficientes, forma de
     respuesta inesperada), lo avisa por log y NO hace fallar el resto del
     script — los chequeos 1 y 2 se guardan igual.
"""
import csv
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

BASE_URL = os.environ.get("RDU_BASE_URL", "https://rdu.unc.edu.ar").rstrip("/")
API = f"{BASE_URL}/server/api"

TAMANO_PAGINA = int(os.environ.get("TAMANO_PAGINA", "20"))
MAX_PAGINAS_POR_CORRIDA = int(os.environ.get("MAX_PAGINAS_POR_CORRIDA", "150"))
UMBRAL_BYTES_VACIO = int(os.environ.get("UMBRAL_BYTES_VACIO", "42"))
PAUSA_SEGUNDOS = float(os.environ.get("PAUSA_SEGUNDOS", "0.3"))  # para no saturar el servidor
REINTENTOS = int(os.environ.get("REINTENTOS", "3"))

# Filtros opcionales. TIPOS: valores de dc.type separados por coma (ej.
# "doctoralThesis,masterThesis"), vacío = todos. ANIO_DESDE/ANIO_HASTA:
# años de dc.date.issued, cualquiera de los dos puede quedar vacío para
# dejar el rango abierto de ese lado.
TIPOS = [t.strip() for t in os.environ.get("TIPOS", "").split(",") if t.strip()]
ANIO_DESDE = os.environ.get("ANIO_DESDE", "").strip()
ANIO_HASTA = os.environ.get("ANIO_HASTA", "").strip()

DIR_REPORTES = "reportes"
ARCHIVO_CHECKPOINT = os.path.join(DIR_REPORTES, "checkpoint_adjuntos.txt")
ARCHIVO_REPORTE = os.path.join(DIR_REPORTES, "adjuntos_rotos.csv")

CAMPOS_REPORTE = ["handle", "titulo", "tipo", "problema", "bytes", "link", "detectado"]

MARCA = "[ADJUNTO NO FUNCIONA]"

# Algunos WAF/firewalls institucionales cortan la conexión sin responder
# (síntoma: "Remote end closed connection without response") cuando ven el
# user-agent por defecto de Python ("Python-urllib/3.x"), porque lo
# reconocen como firma de bot. Mandamos cabeceras de navegador real para
# evitarlo.
CABECERAS_BASE = {
    "Accept": "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}


# ---------- HTTP CON REINTENTOS ----------
def _abrir_con_reintentos(opener, req):
    ultimo_error = None
    for intento in range(1, REINTENTOS + 1):
        try:
            return opener.open(req, timeout=40)
        except Exception as e:
            ultimo_error = e
            if intento < REINTENTOS:
                print(f"  [WARN] Falló la petición (intento {intento}/{REINTENTOS}): {e}. Reintentando...")
                time.sleep(1.5 * intento)
    raise ultimo_error


_OPENER_ANONIMO = urllib.request.build_opener()


def _get(url: str, opener=None) -> dict:
    opener = opener or _OPENER_ANONIMO
    req = urllib.request.Request(url, headers=CABECERAS_BASE)
    resp = _abrir_con_reintentos(opener, req)
    return json.loads(resp.read())


def _link(handle: str) -> str:
    return f"{BASE_URL}/handle/{handle}"


def _metadata(item: dict, campo: str) -> str:
    valores = item.get("metadata", {}).get(campo, [])
    return "; ".join(v.get("value", "") for v in valores if v.get("value"))


# ---------- FILTRO POR TIPO / AÑO ----------
def _query_filtro() -> str | None:
    partes = []
    if TIPOS:
        partes.append("itemtype:(" + " OR ".join(TIPOS) + ")")
    if ANIO_DESDE or ANIO_HASTA:
        # OJO: el rango va con años pelados ("[2014 TO 2014]"), NO con fechas
        # completas ("2014-01-01"). RDU indexa la mayoría de los ítems solo
        # con el año, y un rango con fecha completa no matchea esos valores
        # (probado contra la API real: [2014-01-01 TO 2014-12-31] da 0
        # resultados, [2014 TO 2014] da el conteo correcto). "*" deja ese
        # extremo del rango abierto.
        desde = ANIO_DESDE or "*"
        hasta = ANIO_HASTA or "*"
        partes.append(f"dateIssued:[{desde} TO {hasta}]")
    return " AND ".join(partes) if partes else None


def _url_discover(extra: str, size: int, pagina: int) -> str:
    url = f"{API}/discover/search/objects?dsoType=item&{extra}&size={size}&page={pagina}"
    query = _query_filtro()
    if query:
        url += f"&query={urllib.parse.quote(query, safe='')}"
    return url


def _item_cumple_filtro(item: dict) -> bool:
    """Para chequeos que NO pasan por la búsqueda (ej. workflow), aplica el
    mismo filtro de tipo/año a mano sobre los metadatos del ítem."""
    if TIPOS:
        tipo = _metadata(item, "dc.type")
        if tipo not in TIPOS:
            return False
    if ANIO_DESDE or ANIO_HASTA:
        fecha = _metadata(item, "dc.date.issued")[:4]
        if not fecha.isdigit():
            return False
        anio = int(fecha)
        if ANIO_DESDE and anio < int(ANIO_DESDE):
            return False
        if ANIO_HASTA and anio > int(ANIO_HASTA):
            return False
    return True


# ---------- REPORTE ----------
def _leer_reporte() -> dict:
    if not os.path.exists(ARCHIVO_REPORTE):
        return {}
    with open(ARCHIVO_REPORTE, newline="", encoding="utf-8") as f:
        return {fila["handle"]: fila for fila in csv.DictReader(f)}


def _guardar_reporte(filas: dict):
    os.makedirs(DIR_REPORTES, exist_ok=True)
    with open(ARCHIVO_REPORTE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CAMPOS_REPORTE)
        writer.writeheader()
        for handle in sorted(filas):
            writer.writerow(filas[handle])


def _fila_para(item: dict, problema: str, tam: int, handle_alternativo: str = "") -> dict:
    handle = item.get("handle") or handle_alternativo
    return {
        "handle": handle,
        "titulo": item.get("name", ""),
        "tipo": _metadata(item, "dc.type"),
        "problema": f"{MARCA} {problema}",
        "bytes": tam,
        "link": _link(handle) if handle else "(sin handle: revisar en el panel de RDU)",
        "detectado": date.today().isoformat(),
    }


def _evaluar_bundle_original(item: dict):
    """Devuelve (problema, bytes) si el adjunto ORIGINAL está roto, o None si está OK."""
    bundles = item.get("_embedded", {}).get("bundles", {}).get("_embedded", {}).get("bundles", [])
    original = next((b for b in bundles if b.get("name") == "ORIGINAL"), None)
    if not original:
        return "SIN ARCHIVO ADJUNTO", 0

    bitstreams_meta = original.get("_embedded", {}).get("bitstreams", {})
    bitstreams = bitstreams_meta.get("_embedded", {}).get("bitstreams", [])
    if not bitstreams:
        return "SIN ARCHIVO ADJUNTO", 0

    if bitstreams_meta.get("page", {}).get("totalPages", 1) > 1:
        print(f"  [WARN] {item.get('handle')} tiene más de {len(bitstreams)} archivos originales; "
              f"solo se revisaron los primeros {len(bitstreams)}.")

    for b in bitstreams:
        tam = b.get("sizeBytes", 0)
        if tam <= UMBRAL_BYTES_VACIO:
            return "ADJUNTO VACÍO/ROTO", tam

    return None


# ---------- CHEQUEO 1: RÁPIDO, ítems públicos sin ningún archivo original ----------
def chequeo_rapido(filas_reporte: dict):
    print("[RÁPIDO] Buscando ítems públicos sin ningún archivo en el bundle ORIGINAL...")
    encontrados_ahora = set()
    pagina = 0
    while True:
        url = _url_discover("f.has_content_in_original_bundle=false,equals", 100, pagina)
        data = _get(url)
        resultado = data["_embedded"]["searchResult"]
        objetos = resultado["_embedded"].get("objects", [])
        for obj in objetos:
            item = obj["_embedded"]["indexableObject"]
            handle = item.get("handle", "")
            encontrados_ahora.add(handle)
            filas_reporte[handle] = _fila_para(item, "SIN ARCHIVO ADJUNTO", 0)
        total_paginas = resultado["page"]["totalPages"]
        pagina += 1
        if pagina >= total_paginas:
            break

    # Sacar del reporte los que ya no figuran sin adjunto (se cargó el archivo mientras tanto).
    resueltos = [h for h, fila in list(filas_reporte.items())
                 if fila["problema"] == f"{MARCA} SIN ARCHIVO ADJUNTO" and h not in encontrados_ahora]
    for h in resueltos:
        del filas_reporte[h]

    extra = f" ({len(resueltos)} resuelto(s) desde la última corrida)" if resueltos else ""
    print(f"[RÁPIDO] {len(encontrados_ahora)} ítem(s) sin ningún archivo adjunto{extra}.")


# ---------- CHEQUEO 2: GRADUAL, recorre los ítems públicos buscando adjuntos rotos ----------
def _leer_checkpoint() -> int:
    if not os.path.exists(ARCHIVO_CHECKPOINT):
        return 0
    try:
        return int(open(ARCHIVO_CHECKPOINT, encoding="utf-8").read().strip())
    except ValueError:
        return 0


def _guardar_checkpoint(pagina: int):
    os.makedirs(DIR_REPORTES, exist_ok=True)
    with open(ARCHIVO_CHECKPOINT, "w", encoding="utf-8") as f:
        f.write(str(pagina))


def chequeo_gradual(filas_reporte: dict):
    pagina = _leer_checkpoint()
    print(f"[GRADUAL] Retomando desde la página {pagina} (tamaño de página {TAMANO_PAGINA}).")

    total_paginas = None
    paginas_procesadas = 0
    rotos_detectados = 0
    resueltos = 0

    while paginas_procesadas < MAX_PAGINAS_POR_CORRIDA:
        url = _url_discover("embed=bundles/bitstreams", TAMANO_PAGINA, pagina)
        try:
            data = _get(url)
        except Exception as e:
            print(f"  [WARN] Falló la página {pagina} tras varios intentos: {e}. Se retoma en la próxima corrida.")
            break

        resultado = data["_embedded"]["searchResult"]
        total_paginas = resultado["page"]["totalPages"]
        if total_paginas == 0:
            print("  [GRADUAL] El filtro de tipo/año actual no matchea ningún ítem público. Nada para revisar.")
            pagina = 0
            break
        objetos = resultado["_embedded"].get("objects", [])

        for obj in objetos:
            item = obj["_embedded"]["indexableObject"]
            handle = item.get("handle", "")
            evaluacion = _evaluar_bundle_original(item)
            if evaluacion:
                problema, tam = evaluacion
                filas_reporte[handle] = _fila_para(item, problema, tam)
                rotos_detectados += 1
                print(f"  {MARCA} {handle} — {problema} ({tam} bytes) — {item.get('name', '')}")
            elif handle in filas_reporte:
                del filas_reporte[handle]  # ya no está roto (se corrigió desde el último paso por acá)
                resueltos += 1

        paginas_procesadas += 1
        pagina += 1
        if pagina >= total_paginas:
            print("  [GRADUAL] Se completó una vuelta entera al filtro actual en esta corrida.")
            pagina = 0
            break  # no tiene sentido volver a recorrer el mismo filtro de nuevo en la misma corrida
        time.sleep(PAUSA_SEGUNDOS)

    _guardar_checkpoint(pagina)
    print(f"[GRADUAL] {paginas_procesadas} página(s) procesada(s) esta corrida "
          f"({paginas_procesadas * TAMANO_PAGINA} ítems). "
          f"Rotos detectados: {rotos_detectados}. Resueltos desde la última vez: {resueltos}.")
    if total_paginas:
        avance = min(100.0, round(pagina / total_paginas * 100, 1)) if total_paginas else 100.0
        print(f"[GRADUAL] Próxima corrida retoma en la página {pagina} de {total_paginas} (~{avance}% del ciclo).")


# ---------- CHEQUEO 3: WORKFLOW, ítems todavía no públicos (requiere login) ----------
def _login_rdu():
    """Autentica contra la API de RDU. Devuelve un opener de urllib listo para
    usar en pedidos autenticados, o None si no hay credenciales o el login
    falló (en cuyo caso el chequeo de workflow simplemente se omite)."""
    usuario = os.environ.get("RDU_USER")
    clave = os.environ.get("RDU_PASS")
    if not usuario or not clave:
        print("[WORKFLOW] RDU_USER/RDU_PASS no están configurados; se omite el chequeo de ítems en revisión.")
        return None

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    # 1) Pedir el token CSRF: viene en el header DSPACE-XSRF-TOKEN y además
    #    DSpace deja la cookie correspondiente guardada en el cookiejar.
    req = urllib.request.Request(f"{API}/authn/status", headers=CABECERAS_BASE)
    resp = _abrir_con_reintentos(opener, req)
    xsrf = resp.headers.get("DSPACE-XSRF-TOKEN")
    if not xsrf:
        print("[WORKFLOW] No se pudo obtener el token CSRF de RDU; se omite el chequeo de ítems en revisión.")
        return None

    # 2) Login. La cookie CSRF ya está en el cookiejar del opener.
    body = urllib.parse.urlencode({"user": usuario, "password": clave}).encode()
    req = urllib.request.Request(
        f"{API}/authn/login", data=body, method="POST",
        headers={
            **CABECERAS_BASE,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-XSRF-TOKEN": xsrf,
        },
    )
    try:
        resp = _abrir_con_reintentos(opener, req)
    except urllib.error.HTTPError as e:
        print(f"[WORKFLOW] Login a RDU falló ({e.code}): revisá RDU_USER/RDU_PASS. Se omite este chequeo.")
        return None

    token = resp.headers.get("Authorization")
    if not token:
        print("[WORKFLOW] El login a RDU no devolvió token de autorización; se omite este chequeo.")
        return None

    opener.addheaders = [("Authorization", token)] + list(CABECERAS_BASE.items())
    print("[WORKFLOW] Login a RDU exitoso.")
    return opener


def chequeo_workflow(filas_reporte: dict):
    """Revisa ítems que todavía están en el circuito de revisión de DSpace
    (no públicos). Best-effort: cualquier error (incluido el login) se
    loguea y se sigue de largo sin hacer fallar el resto del script — la
    forma exacta de esta respuesta no se pudo confirmar contra un ítem real
    en revisión, así que si falla, avisá el mensaje de [WARN]/[ERROR] para
    ajustar el endpoint."""
    print("[WORKFLOW] Buscando ítems en revisión (no públicos) con adjunto roto...")
    try:
        opener = _login_rdu()
        if not opener:
            return

        encontrados = 0
        pagina = 0
        while True:
            url = (f"{API}/workflow/workflowitems"
                   f"?embed=item&embed=item/bundles/bitstreams&size=20&page={pagina}")
            data = _get(url, opener=opener)
            resultado = data.get("_embedded", {})
            workflowitems = resultado.get("workflowitems", [])

            for wi in workflowitems:
                item = wi.get("_embedded", {}).get("item")
                if not item:
                    continue
                if not _item_cumple_filtro(item):
                    continue
                handle = item.get("handle") or f"workflow-{wi.get('id', '')}"
                evaluacion = _evaluar_bundle_original(item)
                if evaluacion:
                    problema, tam = evaluacion
                    filas_reporte[handle] = _fila_para(item, f"{problema} (EN REVISIÓN, no público)", tam, handle)
                    encontrados += 1
                    print(f"  {MARCA} {handle} — {problema} (en revisión) — {item.get('name', '')}")

            pagina_info = data.get("page", {})
            total_paginas = pagina_info.get("totalPages", 1)
            pagina += 1
            if pagina >= total_paginas:
                break
            time.sleep(PAUSA_SEGUNDOS)

        print(f"[WORKFLOW] {encontrados} ítem(s) en revisión con adjunto roto.")
    except Exception as e:
        print(f"[WORKFLOW] [ERROR] No se pudo completar el chequeo de ítems en revisión: {e}. "
              f"Se ignora y se sigue con el resto (puede que el endpoint o el formato de "
              f"respuesta necesiten ajuste — revisar scripts/chequear_adjuntos.py::chequeo_workflow).")


def main():
    if TIPOS:
        print(f"Filtro de tipo activo: {', '.join(TIPOS)}")
    if ANIO_DESDE or ANIO_HASTA:
        print(f"Filtro de año activo: {ANIO_DESDE or '(sin mínimo)'} a {ANIO_HASTA or '(sin máximo)'}")

    filas_reporte = _leer_reporte()

    # Cada chequeo es independiente: si uno falla (ej. la API no responde en
    # ese momento), los otros dos igual corren y el reporte se guarda con lo
    # que sí se pudo juntar. Al final, si hubo algún fallo, el script termina
    # con código de error para que quede visible en Actions — pero SIN perder
    # el progreso ya guardado.
    hubo_fallas = False
    for nombre, chequeo in (
        ("rápido", chequeo_rapido),
        ("gradual", chequeo_gradual),
        ("workflow", chequeo_workflow),
    ):
        try:
            chequeo(filas_reporte)
        except Exception as e:
            hubo_fallas = True
            print(f"[ERROR] El chequeo '{nombre}' falló y se omitió el resto de esa sección: {e}")

    _guardar_reporte(filas_reporte)
    print(f"\nReporte actualizado: {ARCHIVO_REPORTE} ({len(filas_reporte)} ítem(s) con adjunto roto).")

    if hubo_fallas:
        sys.exit(1)


if __name__ == "__main__":
    main()
