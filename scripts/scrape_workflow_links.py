#!/usr/bin/env python3
"""
Scrapea TODOS los links de items en WORKFLOW de RDU (DSpace 7.6.5)
que cumplan los filtros de una URL de la UI, paginando hasta el final.

Le pegas la URL del mydspace tal cual (con sus f.* ), ignora spc.page
y recorre todas las paginas.

Ejemplo de URL de entrada:
  https://rdu.unc.edu.ar/mydspace?configuration=workflow&f.itemtype=conferenceObject,equals&spc.page=1&f.dateIssued.min=2014&f.dateIssued.max=2014

Requiere: pip install httpx
Variables de entorno:
  RDU_UI_URL   URL de la UI con los filtros (obligatoria en la practica)
  RDU_EMAIL    usuario de RDU
  RDU_PASSWORD contrasena de RDU
  RDU_DEBUG=1  vuelca el JSON del primer objeto y corta (para verificar estructura)

El filtro de fecha (f.dateIssued.min/.max) se manda al servidor como rango
[min TO max],equals (formato documentado por DSpace), y ADEMAS se vuelve a
chequear del lado del cliente contra dc.date.issued de cada item: los
items en workflow (todavia no publicados) pueden no estar indexados igual
que los archivados, asi que el facet de fecha del servidor podria no
filtrar bien. Si eso pasa, el script lo avisa por log y descarta los
sobrantes igual, asi el CSV final siempre respeta el rango pedido.

Salida: workflow_links.csv  (encabezado: Link,Titulo,Bytes,DetalleAdjunto)
Por cada item hace una consulta extra para traer el tamaño del bundle
ORIGINAL (bitstream mas grande). DetalleAdjunto queda vacio si esta OK,
o dice "SIN ARCHIVO ADJUNTO" / "ADJUNTO VACÍO/ROTO" (umbral configurable
con UMBRAL_BYTES_VACIO, por defecto 42 bytes — mismo criterio que
chequear_adjuntos.py).
"""

import csv
import json
import os
import re
import sys
import time
from urllib.parse import urlparse, parse_qs

import httpx

# --------------------------------------------------------------------------
BASE = "https://rdu.unc.edu.ar"
API = f"{BASE}/server/api"

UI_URL = os.environ.get(
    "RDU_UI_URL",
    f"{BASE}/mydspace?configuration=workflow&f.itemtype=conferenceObject,equals"
    "&spc.page=1&f.dateIssued.min=2014&f.dateIssued.max=2014",
)
EMAIL = os.environ.get("RDU_EMAIL", "")
PASSWORD = os.environ.get("RDU_PASSWORD", "")
DEBUG = os.environ.get("RDU_DEBUG", "") == "1"

PAGE_SIZE = 100
TIMEOUT = 60
OUT_CSV = "workflow_links.csv"
UMBRAL_BYTES_VACIO = int(os.environ.get("UMBRAL_BYTES_VACIO", "42"))
PAUSA_SEGUNDOS = float(os.environ.get("PAUSA_SEGUNDOS", "0.3"))  # entre consultas de bytes, para no saturar

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "es-AR,es;q=0.9",
}

# params de paginado/orden de la UI que NO se reenvian
IGNORAR = {"spc.page", "page", "size", "spc.sf", "spc.sd", "spc.rpp"}
# --------------------------------------------------------------------------


def params_desde_url(url):
    """Traduce los query params de la UI a params de /discover/search/objects.

    f.campo.min / f.campo.max (ej. f.dateIssued.min/.max) se combinan en
    un rango f.campo=[min TO max],equals — es el formato de filtro que
    documenta el RestContract de DSpace (f.<filtro>=<valor>,<operador>).
    Enviar min/max sueltos (como los usa la UI de Angular puertas adentro)
    no es un formato valido para la API REST y devuelve 422.

    Devuelve (params, rango_fechas): rango_fechas es {campo: (min, max)}
    para poder aplicar el mismo filtro del lado del cliente como red de
    seguridad, por si el facet de fecha no se aplica bien contra la
    configuracion "workflow" (los items en revision pueden no estar
    indexados igual que los archivados).
    """
    q = parse_qs(urlparse(url).query, keep_blank_values=True)
    params = {}
    mins, maxs = {}, {}

    for k, valores in q.items():
        if k in IGNORAR:
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


def login(client):
    """Autentica contra DSpace y deja el Bearer en el client."""
    if not EMAIL or not PASSWORD:
        sys.exit("Faltan RDU_EMAIL / RDU_PASSWORD.")

    r = client.get(f"{API}/security/csrf")
    xsrf = (client.cookies.get("DSPACE-XSRF-COOKIE")
            or client.cookies.get("DSPACE-XSRF-TOKEN")
            or r.headers.get("DSPACE-XSRF-TOKEN"))
    h = {"X-XSRF-TOKEN": xsrf} if xsrf else {}

    r = client.post(f"{API}/authn/login",
                    data={"user": EMAIL, "password": PASSWORD}, headers=h)
    if r.status_code not in (200, 201):
        sys.exit(f"Login fallo ({r.status_code}). Revisa usuario/contrasena.")

    auth = r.headers.get("Authorization")
    if not auth:
        sys.exit("Login sin header Authorization (¿cambio la API?).")
    client.headers["Authorization"] = auth
    print("Login OK", file=sys.stderr)


def get_json(client, url, params=None, intentos=4):
    ultimo = None
    for i in range(intentos):
        try:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except (httpx.RemoteProtocolError, httpx.ConnectError,
                httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            ultimo = e
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                print(f"  [DEBUG] Respuesta del servidor ({e.response.status_code}): "
                      f"{e.response.text[:2000]}", file=sys.stderr)
                raise
            ultimo = e
        espera = 2 ** i
        print(f"  reintento {i+1}/{intentos} ({type(ultimo).__name__}), {espera}s...",
              file=sys.stderr)
        time.sleep(espera)
    raise ultimo


def _titulo_de_sections(wfi):
    """Saca dc.title de las sections del workflowitem (traditionalpageone, etc.)."""
    for sec in (wfi.get("sections") or {}).values():
        if isinstance(sec, dict) and sec.get("dc.title"):
            v = sec["dc.title"]
            if isinstance(v, list) and v:
                return v[0].get("value", "") if isinstance(v[0], dict) else str(v[0])
    return ""


def extraer(obj):
    """
    De un indexableObject saca id del workflowitem, uuid del item y titulo.
    En configuration=workflow el objeto suele ser claimedtask/pooltask,
    que ENVUELVE al workflowitem (el id que sirve para el link es el de adentro).
    """
    tipo = (obj.get("type") or "").lower()
    emb = obj.get("_embedded", {}) or {}

    if tipo in ("claimedtask", "pooltask"):
        wfi = emb.get("workflowitem") or {}
        tarea_id = obj.get("id", "")
    elif tipo in ("workflowitem", "workspaceitem"):
        wfi = obj
        tarea_id = ""
    else:                       # por las dudas: el objeto ya es el item
        wfi = {}
        tarea_id = ""

    wf_id = wfi.get("id", "")
    item = (wfi.get("_embedded", {}) or {}).get("item") or (emb.get("item") if not wfi else None) or {}

    md = item.get("metadata", {}) or {}
    titulo = ""
    if md.get("dc.title"):
        titulo = md["dc.title"][0].get("value", "")
    if not titulo:
        titulo = _titulo_de_sections(wfi)

    uuid = item.get("uuid", "")

    fecha = ""
    if md.get("dc.date.issued"):
        fecha = md["dc.date.issued"][0].get("value", "")

    return {
        "tarea_id": tarea_id,
        "workflowitem_id": wf_id,
        "item_uuid": uuid,
        "titulo": titulo,
        "fecha": fecha,
        "link_workflow": f"{BASE}/workflowitems/{wf_id}/edit" if wf_id else "",
        "link_item": f"{BASE}/items/{uuid}" if uuid else "",
    }


def _pasa_filtro_fecha(fila: dict, rango_fechas: dict) -> bool:
    """Chequea del lado del cliente que el año de dc.date.issued caiga
    dentro de los rangos pedidos (red de seguridad si el facet del
    servidor no filtro bien para items en workflow)."""
    rango = rango_fechas.get("dateIssued")
    if not rango:
        return True
    lo, hi = rango
    m = re.match(r"^(\d{4})", fila.get("fecha") or "")
    if not m:
        return True  # sin fecha parseable: no la descartamos, mejor que se revise a mano
    anio = int(m.group(1))
    if lo != "*" and anio < int(lo):
        return False
    if hi != "*" and anio > int(hi):
        return False
    return True


def _tamano_adjunto(client, uuid: str):
    """Consulta el item por uuid con sus bundles/bitstreams y devuelve
    (detalle, bytes) del bundle ORIGINAL: detalle vacio si esta OK,
    'SIN ARCHIVO ADJUNTO' o 'ADJUNTO VACÍO/ROTO' si hay problema.
    Misma logica que chequear_adjuntos.py, para que el criterio de
    'roto' sea consistente entre los dos scripts."""
    if not uuid:
        return "SIN ITEM (solo workspace, no llego a tener uuid)", 0

    try:
        r = client.get(f"{API}/core/items/{uuid}", params={"embed": "bundles/bitstreams"})
        r.raise_for_status()
        item = r.json()
    except Exception as e:
        print(f"  [WARN] No se pudo consultar adjuntos de {uuid}: {e}", file=sys.stderr)
        return f"ERROR AL CONSULTAR ({e})", 0

    bundles = item.get("_embedded", {}).get("bundles", {}).get("_embedded", {}).get("bundles", [])
    original = next((b for b in bundles if b.get("name") == "ORIGINAL"), None)
    if not original:
        return "SIN ARCHIVO ADJUNTO", 0

    bitstreams = (original.get("_embedded", {}).get("bitstreams", {})
                  .get("_embedded", {}).get("bitstreams", []))
    if not bitstreams:
        return "SIN ARCHIVO ADJUNTO", 0

    tam = max(b.get("sizeBytes", 0) for b in bitstreams)
    if tam <= UMBRAL_BYTES_VACIO:
        return "ADJUNTO VACÍO/ROTO", tam
    return "", tam


def main():
    params, rango_fechas = params_desde_url(UI_URL)
    print("Filtros:", json.dumps(params, ensure_ascii=False), file=sys.stderr)

    filas = []
    descartados_por_fecha = 0
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        login(client)

        page = 0
        while True:
            p = dict(params)
            p["size"] = [str(PAGE_SIZE)]
            p["page"] = [str(page)]

            data = get_json(client, f"{API}/discover/search/objects", params=p)
            sr = data["_embedded"]["searchResult"]
            objetos = sr["_embedded"].get("objects", [])
            info = sr["page"]

            if page == 0:
                print(f"Total segun el servidor: {info.get('totalElements')} "
                      f"en {info.get('totalPages')} paginas", file=sys.stderr)

            if DEBUG and objetos:
                io = objetos[0]["_embedded"]["indexableObject"]
                print(json.dumps(io, ensure_ascii=False, indent=2)[:4000])
                print("\nExtraido ->", json.dumps(extraer(io), ensure_ascii=False, indent=2))
                print("\nDEBUG: corto aca (no escribo archivos).", file=sys.stderr)
                return

            if not objetos:
                break

            for o in objetos:
                fila = extraer(o["_embedded"]["indexableObject"])
                if _pasa_filtro_fecha(fila, rango_fechas):
                    filas.append(fila)
                else:
                    descartados_por_fecha += 1

            print(f"  pagina {page+1}/{info.get('totalPages')} -> {len(filas)} acumulados "
                  f"({descartados_por_fecha} descartados por fecha)",
                  file=sys.stderr)

            if page + 1 >= info.get("totalPages", 1):
                break
            page += 1

        print(f"Consultando tamaño de adjuntos de {len(filas)} item(s)...", file=sys.stderr)
        for i, fila in enumerate(filas, start=1):
            detalle, tam = _tamano_adjunto(client, fila["item_uuid"])
            fila["detalle_adjunto"] = detalle
            fila["bytes"] = tam
            if i % 20 == 0:
                print(f"  {i}/{len(filas)} adjuntos consultados...", file=sys.stderr)
            time.sleep(PAUSA_SEGUNDOS)

    if not filas:
        sys.exit("No se encontraron items con esos filtros.")

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Link", "Titulo", "Bytes", "DetalleAdjunto"])
        for f in filas:
            w.writerow([f["link_workflow"] or f["link_item"], f["titulo"],
                        f.get("bytes", ""), f.get("detalle_adjunto", "")])

    aviso = ""
    if descartados_por_fecha:
        aviso = (f"  [AVISO] {descartados_por_fecha} item(s) vinieron del servidor fuera del rango "
                 f"de fecha pedido y se descartaron aca (el filtro server-side de 'dateIssued' no "
                 f"parece aplicarse bien contra items en workflow).")
        print(aviso, file=sys.stderr)

    rotos = sum(1 for f in filas if f.get("detalle_adjunto"))
    print(f"\nOK -> {OUT_CSV}  ({len(filas)} items, {rotos} con adjunto roto/vacío/faltante)")


if __name__ == "__main__":
    main()
