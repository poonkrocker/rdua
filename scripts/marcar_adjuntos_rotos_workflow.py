#!/usr/bin/env python3
"""
marcar_adjuntos_rotos_workflow.py

Busca entradas en WORKFLOW de RDU (DSpace 7.6.5) a partir de los filtros de una URL de MyDSpace.
Para todos los ítems cuyos archivos adjuntos sean menores al umbral (por defecto 1024 bytes / 1 KB)
o no tengan adjunto:
  1. Verifica que no tengan ya la marca en el Resumen (dc.description.abstract) para no duplicarla.
  2. "Asume la tarea" automáticamente vía REST API (POST /api/workflow/claimedtasks).
  3. Modifica el Resumen anteponiendo '[ADJUNTO NO FUNCIONA]' vía JSON Patch (PATCH /api/workflow/workflowitems/{id}).
  4. Devuelve la tarea al pool general (DELETE /api/workflow/claimedtasks/{id}).
  5. Genera un reporte detallado en CSV.

Soporta:
  - Modo simulación (DRY_RUN): recorre, evalúa y genera el reporte SIN modificar RDU.
  - Límite de procesamiento (LIMITE): permite procesar N ítems (ideal para pruebas con 1 o 2 ítems).
"""

import argparse
import csv
from datetime import datetime
import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

import httpx

BASE_URL = os.environ.get("RDU_BASE_URL", "https://rdu.unc.edu.ar").rstrip("/")
API = f"{BASE_URL}/server/api"

DEFAULT_UI_URL = (
    f"{BASE_URL}/mydspace?configuration=workflow&f.itemtype=article,equals"
    "&spc.page=1&f.dateIssued.min=2014&f.dateIssued.max=2014"
)

MARCA = "[ADJUNTO NO FUNCIONA]"
_RE_MARCA = re.compile(r"ADJUNTO\s+NO\s+FUNCIONA", re.IGNORECASE)

IGNORAR_PARAMS = {"spc.page", "page", "size", "spc.sf", "spc.sd", "spc.rpp"}

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "es-AR,es;q=0.9",
}


def params_desde_url(url: str):
    """Traduce los query params de la UI a params de /discover/search/objects."""
    q = parse_qs(urlparse(url).query, keep_blank_values=True)
    params = {}
    mins, maxs = {}, {}

    for k, valores in q.items():
        if k in IGNORAR_PARAMS:
            continue
        m = re.match(r"^f\.(.+)\.(min|max)$", k)
        if m:
            campo, cual = m.groups()
            (mins if cual == "min" else maxs)[campo] = valores[0]
            continue
        params.setdefault(k, []).extend(valores)

    rango_fechas = {}
    for campo in set(mins) | set(maxs):
        lo = mins.get(campo, "*")
        hi = maxs.get(campo, "*")
        params.setdefault(f"f.{campo}", []).append(f"[{lo} TO {hi}],equals")
        rango_fechas[campo] = (lo, hi)

    params.setdefault("configuration", ["workflow"])
    params.setdefault("query", ["*"])
    return params, rango_fechas


def _headers_con_csrf(client: httpx.Client, extra_headers: dict | None = None) -> dict:
    """Retorna headers con el token CSRF actualizado desde las cookies de sesión."""
    xsrf = (
        client.cookies.get("DSPACE-XSRF-COOKIE")
        or client.cookies.get("DSPACE-XSRF-TOKEN")
    )
    headers = dict(extra_headers or {})
    if xsrf:
        headers["X-XSRF-TOKEN"] = xsrf
    return headers


def login(client: httpx.Client, email: str, password: str):
    """Autentica contra DSpace y configura el Bearer Token en el cliente."""
    if not email or not password:
        sys.exit("[ERROR] Faltan credenciales RDU_EMAIL / RDU_PASSWORD (o RDU_USER / RDU_PASS).")

    r = client.get(f"{API}/security/csrf")
    xsrf = (
        client.cookies.get("DSPACE-XSRF-COOKIE")
        or client.cookies.get("DSPACE-XSRF-TOKEN")
        or r.headers.get("DSPACE-XSRF-TOKEN")
    )
    h = {"X-XSRF-TOKEN": xsrf} if xsrf else {}

    r = client.post(f"{API}/authn/login", data={"user": email, "password": password}, headers=h)
    if r.status_code not in (200, 201):
        sys.exit(f"[ERROR] Login falló (HTTP {r.status_code}): revisá tus credenciales.")

    auth = r.headers.get("Authorization")
    if not auth:
        sys.exit("[ERROR] Login no devolvió header Authorization.")

    client.headers["Authorization"] = auth
    print("[AUTH] Login exitoso contra RDU.", file=sys.stderr)


def get_json(client: httpx.Client, url: str, params: dict | None = None, intentos: int = 4):
    """Petición GET con reintentos para fallas transitorias de red."""
    ultimo = None
    for i in range(intentos):
        try:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except (
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
        ) as e:
            ultimo = e
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                print(
                    f"  [DEBUG] HTTP {e.response.status_code}: {e.response.text[:500]}",
                    file=sys.stderr,
                )
                raise
            ultimo = e
        espera = 2**i
        print(f"  [REINTENTO] {i+1}/{intentos} ({type(ultimo).__name__}), espera {espera}s...", file=sys.stderr)
        time.sleep(espera)
    raise ultimo


def extraer_objeto(obj: dict) -> dict:
    """Extrae información del indexableObject (pooltask, claimedtask o workflowitem)."""
    tipo = (obj.get("type") or "").lower()
    emb = obj.get("_embedded", {}) or {}

    pooltask_id = ""
    claimedtask_id = ""

    if tipo == "pooltask":
        pooltask_id = str(obj.get("id", ""))
        wfi = emb.get("workflowitem") or {}
    elif tipo == "claimedtask":
        claimedtask_id = str(obj.get("id", ""))
        wfi = emb.get("workflowitem") or {}
    elif tipo in ("workflowitem", "workspaceitem"):
        wfi = obj
    else:
        wfi = {}

    wf_id = str(wfi.get("id", ""))
    item = (wfi.get("_embedded", {}) or {}).get("item") or (emb.get("item") if not wfi else None) or {}

    md = item.get("metadata", {}) or {}
    titulo = ""
    if md.get("dc.title"):
        titulo = md["dc.title"][0].get("value", "")

    uuid = item.get("uuid", "")
    fecha = ""
    if md.get("dc.date.issued"):
        fecha = md["dc.date.issued"][0].get("value", "")

    resumen = ""
    if md.get("dc.description.abstract"):
        resumen = md["dc.description.abstract"][0].get("value", "")

    return {
        "tipo_objeto": tipo,
        "pooltask_id": pooltask_id,
        "claimedtask_id": claimedtask_id,
        "workflowitem_id": wf_id,
        "item_uuid": uuid,
        "titulo": titulo,
        "fecha": fecha,
        "resumen_item": resumen,
        "link_workflow": f"{BASE_URL}/workflowitems/{wf_id}/edit" if wf_id else "",
        "link_item": f"{BASE_URL}/items/{uuid}" if uuid else "",
    }


def pasa_filtro_fecha(fila: dict, rango_fechas: dict) -> bool:
    """Filtro de seguridad del lado del cliente para dateIssued."""
    rango = rango_fechas.get("dateIssued")
    if not rango:
        return True
    lo, hi = rango
    m = re.match(r"^(\d{4})", fila.get("fecha") or "")
    if not m:
        return True
    anio = int(m.group(1))
    if lo != "*" and anio < int(lo):
        return False
    if hi != "*" and anio > int(hi):
        return False
    return True


def evaluar_adjunto(client: httpx.Client, uuid: str, umbral_bytes: int):
    """Evalúa los adjuntos del bundle ORIGINAL.
    Devuelve (es_candidato, tam_max, detalle).
    """
    if not uuid:
        return False, 0, "SIN UUID DE ITEM"

    try:
        r = client.get(f"{API}/core/items/{uuid}", params={"embed": "bundles/bitstreams"})
        r.raise_for_status()
        item_data = r.json()
    except Exception as e:
        return False, 0, f"ERROR AL CONSULTAR BUNDLES ({e})"

    bundles = item_data.get("_embedded", {}).get("bundles", {}).get("_embedded", {}).get("bundles", [])
    original = next((b for b in bundles if b.get("name") == "ORIGINAL"), None)
    if not original:
        return True, 0, "SIN BUNDLE ORIGINAL"

    bitstreams = original.get("_embedded", {}).get("bitstreams", {}).get("_embedded", {}).get("bitstreams", [])
    if not bitstreams:
        return True, 0, "SIN ARCHIVO ADJUNTO (0 bitstreams)"

    tam_max = max(b.get("sizeBytes", 0) for b in bitstreams)
    if tam_max < umbral_bytes:
        return True, tam_max, f"ADJUNTO < {umbral_bytes} bytes ({tam_max} B)"

    return False, tam_max, f"OK ({tam_max} B)"


def buscar_pooltask_id(client: httpx.Client, item_uuid: str) -> str:
    """Si el objeto no vino como pooltask directo, busca su pooltask por uuid de item."""
    try:
        r = client.get(f"{API}/workflow/pooltasks/search/findByItem", params={"uuid": item_uuid})
        if r.status_code == 200:
            data = r.json()
            return str(data.get("id", ""))
    except Exception:
        pass
    return ""


def asumir_tarea(client: httpx.Client, pooltask_id: str) -> str:
    """Asume una tarea del pool (POST /workflow/claimedtasks con text/uri-list).
    Devuelve el claimedtask_id generado.
    """
    url = f"{API}/workflow/claimedtasks"
    body = f"{API}/workflow/pooltasks/{pooltask_id}"
    headers = _headers_con_csrf(client, {"Content-Type": "text/uri-list"})

    r = client.post(url, content=body, headers=headers)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Fallo al asumir tarea {pooltask_id}: HTTP {r.status_code} - {r.text[:300]}")

    data = r.json()
    claimed_id = str(data.get("id", ""))
    return claimed_id


def agregar_etiqueta_resumen(client: httpx.Client, workflowitem_id: str) -> tuple[str, str]:
    """Modifica el campo dc.description.abstract del workflowitem anteponiendo [ADJUNTO NO FUNCIONA].
    Devuelve (resumen_previo, resumen_nuevo).
    """
    url_wfi = f"{API}/workflow/workflowitems/{workflowitem_id}"
    r = client.get(url_wfi)
    r.raise_for_status()
    wfi_data = r.json()

    sections = wfi_data.get("sections", {}) or {}

    # Buscar en qué sección vive dc.description.abstract
    sid_hallado = None
    abstract_list = None
    for s_id, s_val in sections.items():
        if isinstance(s_val, dict) and "dc.description.abstract" in s_val:
            sid_hallado = s_id
            abstract_list = s_val["dc.description.abstract"]
            break

    # Si no existe en ninguna sección, buscar tradicionalpageone o la primera disponible
    if not sid_hallado:
        for candidato in ("traditionalpageone", "traditionalpagetwo", "submission-form"):
            if candidato in sections:
                sid_hallado = candidato
                break
        if not sid_hallado and sections:
            sid_hallado = list(sections.keys())[0]

    if not sid_hallado:
        raise RuntimeError(f"Workflowitem {workflowitem_id} no tiene secciones de formulario reconocibles.")

    patch_ops = []
    resumen_previo = ""
    resumen_nuevo = ""

    if abstract_list and isinstance(abstract_list, list) and len(abstract_list) > 0:
        primer_elem = abstract_list[0] or {}
        resumen_previo = primer_elem.get("value", "")
        lang = primer_elem.get("language") or "es"
        resumen_nuevo = f"{MARCA} {resumen_previo}".strip()

        patch_ops.append({
            "op": "replace",
            "path": f"/sections/{sid_hallado}/dc.description.abstract/0",
            "value": {"value": resumen_nuevo, "language": lang},
        })
    elif abstract_list is not None and isinstance(abstract_list, list):
        # La lista existe pero está vacía
        resumen_nuevo = MARCA
        patch_ops.append({
            "op": "add",
            "path": f"/sections/{sid_hallado}/dc.description.abstract/-",
            "value": {"value": resumen_nuevo, "language": "es"},
        })
    else:
        # El campo no existía aún en la sección
        resumen_nuevo = MARCA
        patch_ops.append({
            "op": "add",
            "path": f"/sections/{sid_hallado}/dc.description.abstract",
            "value": [{"value": resumen_nuevo, "language": "es"}],
        })

    headers = _headers_con_csrf(client, {"Content-Type": "application/json"})
    r_patch = client.patch(url_wfi, json=patch_ops, headers=headers)
    if r_patch.status_code not in (200, 201):
        raise RuntimeError(
            f"Fallo al actualizar resumen en workflowitem {workflowitem_id}: "
            f"HTTP {r_patch.status_code} - {r_patch.text[:300]}"
        )

    return resumen_previo, resumen_nuevo


def devolver_tarea_al_pool(client: httpx.Client, claimedtask_id: str):
    """Devuelve la tarea al pool general de revisores (DELETE /workflow/claimedtasks/{id})."""
    url = f"{API}/workflow/claimedtasks/{claimedtask_id}"
    headers = _headers_con_csrf(client)
    r = client.delete(url, headers=headers)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Fallo al devolver tarea {claimedtask_id} al pool: HTTP {r.status_code} - {r.text[:300]}")


def procesar_flujo(
    ui_url: str,
    umbral_bytes: int = 1024,
    dry_run: bool = False,
    limite: int = 0,
    pausa_segundos: float = 0.5,
    email: str = "",
    password: str = "",
    archivo_csv: str = "",
):
    params, rango_fechas = params_desde_url(ui_url)
    print(f"[INICIO] Filtros interpretados: {json.dumps(params, ensure_ascii=False)}", file=sys.stderr)
    print(f"[INICIO] Umbral bytes: {umbral_bytes} B | Dry-run: {dry_run} | Límite: {limite or 'Sin límite'}", file=sys.stderr)

    if not archivo_csv:
        os.makedirs("reportes", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archivo_csv = os.path.join("reportes", f"adjuntos_rotos_marcados_{timestamp}.csv")

    columnas_csv = [
        "Fecha",
        "Workflowitem_ID",
        "Item_UUID",
        "Titulo",
        "Bytes_Adjunto",
        "Detalle_Adjunto",
        "Resumen_Anterior",
        "Resumen_Nuevo",
        "Accion",
        "Link_Workflow",
        "Link_Item",
    ]

    filas_reporte = []
    conteo_marcados = 0
    conteo_omitidos = 0
    conteo_errores = 0

    with httpx.Client(timeout=60, headers=HEADERS_BASE, follow_redirects=True) as client:
        login(client, email, password)

        # 1. Paginado y recolección de candidatos
        candidatos = []
        page = 0
        page_size = 50

        print("[BUSQUEDA] Consultando items en workflow...", file=sys.stderr)
        while True:
            p = dict(params)
            p["size"] = [str(page_size)]
            p["page"] = [str(page)]

            data = get_json(client, f"{API}/discover/search/objects", params=p)
            sr = data.get("_embedded", {}).get("searchResult", {})
            objetos = sr.get("_embedded", {}).get("objects", [])
            info = sr.get("page", {})

            if page == 0:
                print(
                    f"[BUSQUEDA] Total encontrados según servidor: {info.get('totalElements')} "
                    f"en {info.get('totalPages')} página(s)",
                    file=sys.stderr,
                )

            if not objetos:
                break

            for o in objetos:
                io = o.get("_embedded", {}).get("indexableObject", {})
                fila = extraer_objeto(io)
                if pasa_filtro_fecha(fila, rango_fechas):
                    candidatos.append(fila)

            page += 1
            if page >= info.get("totalPages", 1):
                break
            time.sleep(0.2)

        print(f"[BUSQUEDA] Total candidatos tras filtro de fecha: {len(candidatos)}", file=sys.stderr)

        # 2. Procesamiento de cada candidato
        for idx, item in enumerate(candidatos, start=1):
            uuid = item["item_uuid"]
            wf_id = item["workflowitem_id"]
            titulo = item["titulo"]

            # Evaluar adjunto
            es_candidato, tam, detalle = evaluar_adjunto(client, uuid, umbral_bytes)
            if not es_candidato:
                # Adjunto OK (> umbral)
                continue

            # Revisar si ya tiene la marca en el resumen
            resumen_previo = item.get("resumen_item", "")
            if _RE_MARCA.search(resumen_previo):
                print(f"  [OMITIDO] #{idx} {titulo[:60]}... ya tiene la marca en el resumen.")
                filas_reporte.append({
                    "Fecha": datetime.now().isoformat(),
                    "Workflowitem_ID": wf_id,
                    "Item_UUID": uuid,
                    "Titulo": titulo,
                    "Bytes_Adjunto": tam,
                    "Detalle_Adjunto": detalle,
                    "Resumen_Anterior": resumen_previo,
                    "Resumen_Nuevo": resumen_previo,
                    "Accion": "YA_TENIA_ETIQUETA",
                    "Link_Workflow": item["link_workflow"],
                    "Link_Item": item["link_item"],
                })
                conteo_omitidos += 1
                continue

            print(f"\n[PROCESANDO] #{idx} Item: {titulo[:70]} | {detalle}")

            if dry_run:
                print(f"  [SIMULACIÓN] Se asumiría tarea, se agregaría '{MARCA}' al resumen y se devolvería al pool.")
                filas_reporte.append({
                    "Fecha": datetime.now().isoformat(),
                    "Workflowitem_ID": wf_id,
                    "Item_UUID": uuid,
                    "Titulo": titulo,
                    "Bytes_Adjunto": tam,
                    "Detalle_Adjunto": detalle,
                    "Resumen_Anterior": resumen_previo,
                    "Resumen_Nuevo": f"{MARCA} {resumen_previo}".strip(),
                    "Accion": "SIMULADO_MARCAR",
                    "Link_Workflow": item["link_workflow"],
                    "Link_Item": item["link_item"],
                })
                conteo_marcados += 1
            else:
                try:
                    # Determinar o buscar pooltask_id
                    pool_id = item.get("pooltask_id")
                    claimed_id = item.get("claimedtask_id")

                    if not pool_id and not claimed_id:
                        pool_id = buscar_pooltask_id(client, uuid)

                    # Asumir tarea
                    if not claimed_id:
                        if not pool_id:
                            raise RuntimeError(f"No se encontró pooltask ni claimedtask para el ítem {uuid}")
                        claimed_id = asumir_tarea(client, pool_id)
                        print(f"  [1/3] Tarea asumida con éxito (claimedtask_id: {claimed_id})")
                    else:
                        print(f"  [1/3] Tarea ya estaba asumida (claimedtask_id: {claimed_id})")

                    # Modificar Resumen
                    ant, nuevo = agregar_etiqueta_resumen(client, wf_id)
                    print(f"  [2/3] Resumen actualizado con '{MARCA}'.")

                    # Devolver tarea al pool
                    devolver_tarea_al_pool(client, claimed_id)
                    print(f"  [3/3] Tarea devuelta al pool general.")

                    filas_reporte.append({
                        "Fecha": datetime.now().isoformat(),
                        "Workflowitem_ID": wf_id,
                        "Item_UUID": uuid,
                        "Titulo": titulo,
                        "Bytes_Adjunto": tam,
                        "Detalle_Adjunto": detalle,
                        "Resumen_Anterior": ant,
                        "Resumen_Nuevo": nuevo,
                        "Accion": "MARCADO_Y_DEVUELTO_AL_POOL",
                        "Link_Workflow": item["link_workflow"],
                        "Link_Item": item["link_item"],
                    })
                    conteo_marcados += 1

                except Exception as e:
                    conteo_errores += 1
                    print(f"  [ERROR] Falló el procesamiento de {wf_id}: {e}", file=sys.stderr)
                    filas_reporte.append({
                        "Fecha": datetime.now().isoformat(),
                        "Workflowitem_ID": wf_id,
                        "Item_UUID": uuid,
                        "Titulo": titulo,
                        "Bytes_Adjunto": tam,
                        "Detalle_Adjunto": detalle,
                        "Resumen_Anterior": resumen_previo,
                        "Resumen_Nuevo": "",
                        "Accion": f"ERROR: {e}",
                        "Link_Workflow": item["link_workflow"],
                        "Link_Item": item["link_item"],
                    })

            time.sleep(pausa_segundos)

            if limite > 0 and conteo_marcados >= limite:
                print(f"\n[LIMITE] Se alcanzó el límite configurado de {limite} ítem(s) procesados. Cortando ejecución.")
                break

    # Guardar reporte CSV
    os.makedirs(os.path.dirname(archivo_csv) or ".", exist_ok=True)
    with open(archivo_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columnas_csv)
        writer.writeheader()
        writer.writerows(filas_reporte)

    print(
        f"\n[FINALIZADO] Procesamiento completo:\n"
        f"  - Ítems marcados / simulados: {conteo_marcados}\n"
        f"  - Ítems ya marcados previamente: {conteo_omitidos}\n"
        f"  - Ítems con error: {conteo_errores}\n"
        f"  - Reporte guardado en: {archivo_csv}"
    )


def main():
    parser = argparse.ArgumentParser(description="Marca adjuntos rotos en workflow RDU.")
    parser.add_argument("--url", default=os.environ.get("RDU_UI_URL", DEFAULT_UI_URL), help="URL de MyDSpace con filtros")
    parser.add_argument("--umbral", type=int, default=int(os.environ.get("UMBRAL_BYTES", "1024")), help="Umbral de bytes (default 1024)")
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN", "0") in ("1", "true", "True"), help="Modo simulación sin escribir")
    parser.add_argument("--limite", type=int, default=int(os.environ.get("LIMITE", "0")), help="Límite máximo de ítems a marcar (0 = sin límite)")
    parser.add_argument("--output", default=os.environ.get("ARCHIVO_REPORTE", ""), help="Ruta del CSV de salida")

    args = parser.parse_args()

    email = os.environ.get("RDU_EMAIL") or os.environ.get("RDU_USER") or ""
    password = os.environ.get("RDU_PASSWORD") or os.environ.get("RDU_PASS") or ""

    procesar_flujo(
        ui_url=args.url,
        umbral_bytes=args.umbral,
        dry_run=args.dry_run,
        limite=args.limite,
        email=email,
        password=password,
        archivo_csv=args.output,
    )


if __name__ == "__main__":
    main()
