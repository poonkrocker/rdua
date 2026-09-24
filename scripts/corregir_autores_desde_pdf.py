#!/usr/bin/env python3
"""
corregir_autores_desde_pdf.py

Coteja y corrige automáticamente los autores (y el título) de los artículos en
revisión en RDU (DSpace 7.6.5) contra el texto real del PDF adjunto, asistido por
la API de DeepSeek.

Permite:
  A) Pegar el link específico de un artículo
     (ej. https://rdu.unc.edu.ar/workflowitems/50076/edit o el ID 50076).
  B) O pegar una URL de búsqueda de MyDSpace con filtros
     (ej. .../mydspace?configuration=workflow&f.itemtype=article...).

Flujo por cada ítem:
  1. Localiza el adjunto (en sections['upload']['files'] o en bundles ORIGINAL)
     y lo descarga vía REST API autenticada.
  2. Extrae el texto de las primeras páginas del PDF (donde están título y autores).
  3. Consulta a DeepSeek para evaluar dos situaciones:
     - CASO A: SI EL ADJUNTO NO CORRESPONDE AL ARTÍCULO (título y autores completamente
       diferentes):
       * NO sobreescribe los autores ni el título de RDU.
       * Agrega al INICIO del campo Resumen: [ADJUNTO NO CORRESPONDE].
       * Agrega al final una nota con el artículo al que pertenecía el archivo adjunto.
     - CASO B: SI EL ADJUNTO SÍ CORRESPONDE AL ARTÍCULO (mismo trabajo pero con diferencias):
       * Corrige/agrega autores y ajusta el título según el PDF.
       * Agrega al final del Resumen entre corchetes: [Modificaciones: ...].
  4. Si hubo cambios o marca:
     - Asume la tarea en el workflow si está en el pool (POST /api/workflow/claimedtasks).
     - Aplica los cambios vía JSON Patch en el workflowitem.
     - Devuelve la tarea al pool general (DELETE /api/workflow/claimedtasks/{id}).
  5. Genera un reporte detallado en CSV.

Soporta --dry-run (modo simulación) y --limite para pruebas seguras.
"""

import argparse
import csv
from datetime import datetime
import io
import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

import httpx
from openai import OpenAI
from pypdf import PdfReader

# Importar reglas de formato institucional de RDU
try:
    from formato import (
        dividir_titulo_subtitulo,
        estandarizar_titulo,
        son_titulos_equivalentes,
        limpiar_descripcion,
    )
except ImportError:
    sys.path.append(os.path.dirname(__file__))
    from formato import (
        dividir_titulo_subtitulo,
        estandarizar_titulo,
        son_titulos_equivalentes,
        limpiar_descripcion,
    )

BASE_URL = os.environ.get("RDU_BASE_URL", "https://rdu.unc.edu.ar").rstrip("/")
API = f"{BASE_URL}/server/api"

DEFAULT_UI_URL = f"{BASE_URL}/workflowitems/50076/edit"

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
    xsrf = (
        client.cookies.get("DSPACE-XSRF-COOKIE")
        or client.cookies.get("DSPACE-XSRF-TOKEN")
    )
    headers = dict(extra_headers or {})
    if xsrf:
        headers["X-XSRF-TOKEN"] = xsrf
    return headers


def login(client: httpx.Client, email: str, password: str):
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
        sys.exit(f"[ERROR] Login falló (HTTP {r.status_code}): revisá credenciales de RDU.")

    auth = r.headers.get("Authorization")
    if not auth:
        sys.exit("[ERROR] Login no devolvió header Authorization.")

    client.headers["Authorization"] = auth
    print("[AUTH] Login exitoso contra RDU.", file=sys.stderr)


def get_json(client: httpx.Client, url: str, params: dict | None = None, intentos: int = 4):
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
                print(f"  [DEBUG] HTTP {e.response.status_code}: {e.response.text[:500]}", file=sys.stderr)
                raise
            ultimo = e
        espera = 2**i
        print(f"  [REINTENTO] {i+1}/{intentos} ({type(ultimo).__name__}), espera {espera}s...", file=sys.stderr)
        time.sleep(espera)
    raise ultimo


def extraer_campo_metadatos(sections: dict, metadata_item: dict, campos: list[str] | str) -> list[str]:
    """Busca uno o más campos de metadatos en las secciones del workflowitem,
    y si no se encuentran, en el diccionario metadata del item.
    Devuelve lista de strings con los valores encontrados.
    """
    if isinstance(campos, str):
        campos = [campos]

    for campo in campos:
        # 1. Buscar en secciones vivas del workflowitem
        for sid, sval in (sections or {}).items():
            if isinstance(sval, dict) and campo in sval:
                arr = sval[campo]
                valores = []
                if isinstance(arr, list):
                    for elem in arr:
                        if isinstance(elem, dict) and elem.get("value") is not None:
                            v = str(elem["value"]).strip()
                            if v:
                                valores.append(v)
                        elif isinstance(elem, str) and elem.strip():
                            valores.append(elem.strip())
                elif isinstance(arr, dict) and arr.get("value") is not None:
                    v = str(arr["value"]).strip()
                    if v:
                        valores.append(v)
                elif isinstance(arr, str) and arr.strip():
                    valores.append(arr.strip())

                if valores:
                    return valores

        # 2. Respaldo en metadata de item (por si ya está indexado en /core/items/{uuid})
        if metadata_item and campo in metadata_item:
            arr = metadata_item[campo]
            valores = []
            if isinstance(arr, list):
                for elem in arr:
                    if isinstance(elem, dict) and elem.get("value") is not None:
                        v = str(elem["value"]).strip()
                        if v:
                            valores.append(v)
                    elif isinstance(elem, str) and elem.strip():
                        valores.append(elem.strip())
            elif isinstance(arr, dict) and arr.get("value") is not None:
                v = str(arr["value"]).strip()
                if v:
                    valores.append(v)
            elif isinstance(arr, str) and arr.strip():
                valores.append(arr.strip())

            if valores:
                return valores

    return []


def extraer_objeto(obj: dict) -> dict:
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
    if not wf_id:
        links = obj.get("_links", {}) or {}
        if "workflowitem" in links:
            href = links["workflowitem"].get("href", "")
            m = re.search(r"/workflowitems/(\d+)", href)
            if m:
                wf_id = m.group(1)

    sections = wfi.get("sections", {}) or {}
    item = (wfi.get("_embedded", {}) or {}).get("item") or (emb.get("item") if not wfi else None) or {}
    uuid = item.get("uuid") or item.get("id") or ""
    md = item.get("metadata", {}) or {}

    titulos = extraer_campo_metadatos(sections, md, ["dc.title", "dc.title.alternative"])
    titulo = titulos[0] if titulos else ""

    autores = extraer_campo_metadatos(sections, md, ["dc.contributor.author", "dc.creator"])

    resumenes = extraer_campo_metadatos(sections, md, ["dc.description.abstract", "dc.description"])
    resumen = resumenes[0] if resumenes else ""

    fechas = extraer_campo_metadatos(sections, md, ["dc.date.issued", "dc.date"])
    fecha = fechas[0] if fechas else ""

    return {
        "tipo_objeto": tipo,
        "pooltask_id": pooltask_id,
        "claimedtask_id": claimedtask_id,
        "workflowitem_id": wf_id,
        "item_uuid": uuid,
        "titulo": titulo,
        "autores": autores,
        "resumen": resumen,
        "fecha": fecha,
        "link_workflow": f"{BASE_URL}/workflowitems/{wf_id}/edit" if wf_id else "",
        "link_item": f"{BASE_URL}/items/{uuid}" if uuid else "",
    }


def buscar_pooltask_id(client: httpx.Client, item_uuid: str) -> str:
    if not item_uuid:
        return ""
    try:
        r = client.get(f"{API}/workflow/pooltasks/search/findByItem", params={"uuid": item_uuid})
        if r.status_code == 200:
            data = r.json()
            return str(data.get("id", ""))
    except Exception:
        pass
    return ""


def buscar_claimedtask_id(client: httpx.Client, item_uuid: str) -> str:
    if not item_uuid:
        return ""
    try:
        r = client.get(f"{API}/workflow/claimedtasks/search/findByItem", params={"uuid": item_uuid})
        if r.status_code == 200:
            data = r.json()
            return str(data.get("id", ""))
    except Exception:
        pass
    return ""


def obtener_info_workflowitem(client: httpx.Client, wf_id: str) -> dict:
    """Obtiene la información completa de un workflowitem por su ID,
    extrayendo metadatos de sus secciones vivas y vinculando su item y tareas.
    """
    url = f"{API}/workflow/workflowitems/{wf_id}?embed=item"
    r = client.get(url)
    r.raise_for_status()
    data = r.json()

    sections = data.get("sections", {}) or {}
    item = (data.get("_embedded", {}) or {}).get("item", {}) or {}
    uuid = item.get("uuid") or item.get("id") or ""
    md = item.get("metadata", {}) or {}

    # Si aún no tenemos uuid, consultar el link del item asociado al workflowitem
    if not uuid:
        item_href = ((data.get("_links", {}) or {}).get("item", {}) or {}).get("href")
        if not item_href:
            item_href = f"{API}/workflow/workflowitems/{wf_id}/item"
        try:
            r_item = client.get(item_href)
            if r_item.status_code == 200:
                item_data = r_item.json()
                uuid = item_data.get("uuid") or item_data.get("id") or ""
                if not md:
                    md = item_data.get("metadata", {}) or {}
        except Exception as e:
            print(f"  [DEBUG] Error consultando item de workflowitem {wf_id}: {e}", file=sys.stderr)

    # Extraer metadatos desde sections (prioridad) o metadata del item
    titulos = extraer_campo_metadatos(sections, md, ["dc.title", "dc.title.alternative"])
    titulo = titulos[0] if titulos else ""

    autores = extraer_campo_metadatos(sections, md, ["dc.contributor.author", "dc.creator"])

    resumenes = extraer_campo_metadatos(sections, md, ["dc.description.abstract", "dc.description"])
    resumen = resumenes[0] if resumenes else ""

    fechas = extraer_campo_metadatos(sections, md, ["dc.date.issued", "dc.date"])
    fecha = fechas[0] if fechas else ""

    pool_id = buscar_pooltask_id(client, uuid) if uuid else ""
    claimed_id = ""
    if not pool_id and uuid:
        claimed_id = buscar_claimedtask_id(client, uuid)

    return {
        "tipo_objeto": "workflowitem",
        "pooltask_id": pool_id,
        "claimedtask_id": claimed_id,
        "workflowitem_id": str(wf_id),
        "item_uuid": uuid,
        "titulo": titulo,
        "autores": autores,
        "resumen": resumen,
        "fecha": fecha,
        "link_workflow": f"{BASE_URL}/workflowitems/{wf_id}/edit",
        "link_item": f"{BASE_URL}/items/{uuid}" if uuid else "",
    }


def resolver_candidatos(client: httpx.Client, entrada_url: str) -> list[dict]:
    """Detecta si entrada_url es un ítem puntual (workflowitem/item) o una búsqueda de MyDSpace."""
    entrada = entrada_url.strip()

    # 1. Caso: Link o ID numérico de workflowitem (ej. https://rdu.unc.edu.ar/workflowitems/50076/edit o 50076)
    m_wf = re.search(r"/workflowitems/(\d+)", entrada)
    if not m_wf and entrada.isdigit():
        wf_id = entrada
    elif m_wf:
        wf_id = m_wf.group(1)
    else:
        wf_id = None

    if wf_id:
        print(f"[MODO INDIVIDUAL] Detectado workflowitem específico con ID {wf_id}", file=sys.stderr)
        try:
            return [obtener_info_workflowitem(client, wf_id)]
        except Exception as e:
            sys.exit(f"[ERROR] No se pudo obtener el workflowitem {wf_id} de RDU: {e}")

    # 2. Caso: Link o UUID de item (/items/<uuid>)
    m_uuid = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", entrada, re.IGNORECASE)
    if m_uuid and ("/items/" in entrada.lower() or len(entrada) == 36):
        uuid_item = m_uuid.group(0)
        print(f"[MODO INDIVIDUAL] Detectado UUID de item: {uuid_item}", file=sys.stderr)
        pool_id = buscar_pooltask_id(client, uuid_item)
        claimed_id = buscar_claimedtask_id(client, uuid_item)
        wf_id = None
        if pool_id:
            r = client.get(f"{API}/workflow/pooltasks/{pool_id}?embed=workflowitem")
            wf_id = r.json().get("_embedded", {}).get("workflowitem", {}).get("id")
        elif claimed_id:
            r = client.get(f"{API}/workflow/claimedtasks/{claimed_id}?embed=workflowitem")
            wf_id = r.json().get("_embedded", {}).get("workflowitem", {}).get("id")

        if wf_id:
            return [obtener_info_workflowitem(client, str(wf_id))]
        else:
            sys.exit(f"[ERROR] No se encontró workflowitem activo para el item {uuid_item}.")

    # 3. Caso: Búsqueda MyDSpace con filtros
    params, rango_fechas = params_desde_url(entrada)
    print(f"[MODO LOTE] Filtros interpretados: {json.dumps(params, ensure_ascii=False)}", file=sys.stderr)

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

    return candidatos


def pasa_filtro_fecha(fila: dict, rango_fechas: dict) -> bool:
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


def obtener_archivos_workflowitem(client: httpx.Client, wf_id: str, item_uuid: str) -> list[dict]:
    """Localiza todos los archivos adjuntos del ítem revisando tanto
    las secciones del workflowitem (sections['upload']['files']) como los bundles del item.
    """
    archivos = []

    # 1. Buscar en las secciones del workflowitem (sections['upload']['files'])
    if wf_id:
        try:
            r = client.get(f"{API}/workflow/workflowitems/{wf_id}")
            if r.status_code == 200:
                wfi = r.json()
                sections = wfi.get("sections", {}) or {}
                for sname, sdata in sections.items():
                    if isinstance(sdata, dict) and "files" in sdata and isinstance(sdata["files"], list):
                        for f in sdata["files"]:
                            nombre = ""
                            if f.get("metadata", {}).get("dc.title"):
                                nombre = f["metadata"]["dc.title"][0].get("value", "")
                            elif f.get("name"):
                                nombre = f["name"]

                            fid = f.get("uuid") or f.get("id") or ""
                            url_descarga = f.get("url") or (f.get("_links", {}).get("content", {}).get("href"))
                            if not url_descarga and fid:
                                url_descarga = f"{API}/core/bitstreams/{fid}/content"

                            archivos.append({
                                "nombre": nombre or f"archivo_{fid}.pdf",
                                "sizeBytes": f.get("sizeBytes", 0),
                                "url": url_descarga,
                                "uuid": fid,
                                "origen": f"section_{sname}",
                            })
        except Exception as e:
            print(f"  [DEBUG] Error leyendo secciones de workflowitem {wf_id}: {e}", file=sys.stderr)

    # 2. Si no encontró en sections, buscar en bundles del item
    if not archivos and item_uuid:
        try:
            r = client.get(f"{API}/core/items/{item_uuid}?embed=bundles/bitstreams")
            if r.status_code == 200:
                item_data = r.json()
                bundles = item_data.get("_embedded", {}).get("bundles", {}).get("_embedded", {}).get("bundles", [])
                for b in bundles:
                    if b.get("name") == "ORIGINAL":
                        bitstreams = (b.get("_embedded", {}).get("bitstreams", {})
                                       .get("_embedded", {}).get("bitstreams", []))
                        for bit in bitstreams:
                            fid = bit.get("id") or bit.get("uuid") or ""
                            url_d = bit.get("_links", {}).get("content", {}).get("href")
                            if not url_d and fid:
                                url_d = f"{API}/core/bitstreams/{fid}/content"
                            archivos.append({
                                "nombre": bit.get("name", ""),
                                "sizeBytes": bit.get("sizeBytes", 0),
                                "url": url_d,
                                "uuid": fid,
                                "origen": "bundle_ORIGINAL",
                            })
        except Exception as e:
            print(f"  [DEBUG] Error leyendo bundles de {item_uuid}: {e}", file=sys.stderr)

    # 3. Endpoint alternativo directo /core/items/{uuid}/bundles
    if not archivos and item_uuid:
        try:
            r = client.get(f"{API}/core/items/{item_uuid}/bundles")
            if r.status_code == 200:
                bundles = r.json().get("_embedded", {}).get("bundles", [])
                for b in bundles:
                    if b.get("name") == "ORIGINAL":
                        r_bits = client.get(f"{API}/core/bundles/{b.get('id')}/bitstreams")
                        if r_bits.status_code == 200:
                            bits = r_bits.json().get("_embedded", {}).get("bitstreams", [])
                            for bit in bits:
                                fid = bit.get("id") or ""
                                url_d = bit.get("_links", {}).get("content", {}).get("href") or f"{API}/core/bitstreams/{fid}/content"
                                archivos.append({
                                    "nombre": bit.get("name", ""),
                                    "sizeBytes": bit.get("sizeBytes", 0),
                                    "url": url_d,
                                    "uuid": fid,
                                    "origen": "bundles_endpoint",
                                })
        except Exception as e:
            print(f"  [DEBUG] Error consultando /bundles: {e}", file=sys.stderr)

    return archivos


def descargar_y_extraer_texto_pdf(client: httpx.Client, wf_id: str, item_uuid: str) -> tuple[str, str]:
    """Descarga el PDF y extrae texto de las primeras páginas."""
    archivos = obtener_archivos_workflowitem(client, wf_id, item_uuid)
    if not archivos:
        return "", "SIN ARCHIVOS ADJUNTOS ENCONTRADOS (ni en secciones de upload ni en bundles)"

    nombres_archivos = [f"{a.get('nombre')} ({a.get('sizeBytes')}B, {a.get('origen')})" for a in archivos]
    print(f"  [ARCHIVOS] Detectados {len(archivos)} archivo(s): {', '.join(nombres_archivos)}", file=sys.stderr)

    candidato_pdf = None
    for a in archivos:
        nombre = (a.get("nombre") or "").lower()
        if nombre.endswith(".pdf"):
            candidato_pdf = a
            break
    if not candidato_pdf:
        candidato_pdf = max(archivos, key=lambda x: x.get("sizeBytes", 0))

    content_url = candidato_pdf.get("url")
    if not content_url and candidato_pdf.get("uuid"):
        content_url = f"{API}/core/bitstreams/{candidato_pdf['uuid']}/content"

    if not content_url:
        return "", f"SIN LINK DE DESCARGA PARA '{candidato_pdf.get('nombre')}'"

    print(f"  [DESCARGA] Descargando '{candidato_pdf.get('nombre')}' ({candidato_pdf.get('sizeBytes')} B)...", file=sys.stderr)
    try:
        resp = client.get(content_url)
        if resp.status_code != 200:
            return "", f"HTTP {resp.status_code} AL DESCARGAR '{candidato_pdf.get('nombre')}'"
        pdf_bytes = resp.content
    except Exception as e:
        return "", f"ERROR AL DESCARGAR ARCHIVO: {e}"

    if len(pdf_bytes) < 100:
        return "", f"ARCHIVO DEMASIADO PEQUEÑO ({len(pdf_bytes)} B)"

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        num_paginas = len(reader.pages)
        texto = ""
        for p in reader.pages[:4]:
            texto += (p.extract_text() or "") + "\n"

        texto = texto.strip()
        print(f"  [TEXTO] Extraídos {len(texto)} caracteres de {min(4, num_paginas)} página(s).", file=sys.stderr)
        if not texto:
            return "", f"PDF IMAGEN ESCANEADA / SIN TEXTO EXTRAIBLE ({num_paginas} págs)"
        return texto, f"OK ({len(texto)} chars de {min(4, num_paginas)} págs)"
    except Exception as e:
        return "", f"ERROR AL PARSEAR PDF: {e}"


def es_token_inicial_secundaria(token: str) -> bool:
    """Detecta si un token posterior al primer nombre es una inicial suelta o tanda de iniciales
    (ej: 'B.', 'B', 'M.', 'E.', 'MB', 'J.C.').
    """
    tok = token.strip(".,;:()")
    if not tok:
        return True
    if len(tok) == 1:
        return True
    # Iniciales pegadas sin vocales (ej: 'MB', 'Rm')
    if len(tok) <= 3 and not any(c.lower() in "aeiouáéíóúü" for c in tok):
        return True
    # Iniciales con puntos múltiples (ej: 'J.C' o 'M.E')
    partes = [p for p in re.split(r"\.+", token) if p]
    if partes and all(len(p) == 1 for p in partes):
        return True
    return False


def depurar_autor_sin_iniciales(autor: str) -> str:
    """Asegura que el autor tenga formato estrictamente 'Apellido, Nombre' eliminando
    iniciales secundarias/intermedias (ej: 'Suárez, Andrea B.' -> 'Suárez, Andrea').
    """
    s = (autor or "").strip()
    if not s or "," not in s:
        return s

    partes = s.split(",", 1)
    apellido = partes[0].strip()
    resto = partes[1].strip()

    tokens = resto.split()
    if not tokens:
        return f"{apellido},"

    # Conservar el primer token (nombre de pila)
    conservados = [tokens[0]]
    # Filtrar tokens posteriores descartando iniciales secundarias
    for tok in tokens[1:]:
        if es_token_inicial_secundaria(tok):
            continue
        conservados.append(tok)

    nombres = " ".join(conservados)
    return f"{apellido}, {nombres}".strip()


def son_autores_equivalentes(autores_a: list[str], autores_b: list[str]) -> bool:
    """Verifica si dos listas de autores representan exactamente a las mismas personas
    ignorando iniciales secundarias (ej: 'Suárez, Andrea' y 'Suárez, Andrea B.').
    """
    if len(autores_a) != len(autores_b):
        return False

    for a, b in zip(autores_a, autores_b):
        limpio_a = depurar_autor_sin_iniciales(a).strip().lower()
        limpio_b = depurar_autor_sin_iniciales(b).strip().lower()
        if limpio_a != limpio_b:
            return False
    return True


def consultar_deepseek(client_ai: OpenAI, model: str, titulo_rdu: str, autores_rdu: list[str], texto_pdf: str) -> dict:
    """Evalúa si el PDF corresponde al artículo registrado y propone correcciones o marca de discrepancia."""
    system_prompt = (
        "Sos un catalogador bibliográfico experto en el Repositorio Digital Universitario (RDU - Universidad Nacional de Córdoba, Argentina).\n"
        "Tu tarea es cotejar los metadatos cargados en RDU (Título y Autores) contra el texto real de las primeras páginas del documento adjunto (PDF).\n\n"
        "CRÍTICO - EVALUACIÓN DE CORRESPONDENCIA:\n"
        "1. ¿El documento adjunto corresponde realmente al trabajo registrado en RDU, o se adjuntó un DOCUMENTO EQUIVOCADO?\n"
        "   - Si el título del PDF y los autores del PDF NO TIENEN NADA QUE VER con los metadatos de RDU (artículo completamente diferente),\n"
        "     debes indicar obligatoriamente: 'adjunto_corresponde': false.\n"
        "     En 'motivo_no_corresponde' explica brevemente cuál es el artículo real que viene en el PDF.\n"
        "   - Si el documento sí es el mismo artículo (aunque tenga autores que no habían sido cargados, nombres con iniciales a completar,\n"
        "     o pequeñas erratas en el título), indica: 'adjunto_corresponde': true.\n\n"
        "Instrucciones si 'adjunto_corresponde' es true:\n"
        "1. TÍTULO (REGLAS INSTITUCIONALES RDU):\n"
        "   - Formato obligatorio: 'formato oración' (mayúscula solo al inicio del título, resto en minúsculas salvo nombres propios y siglas como UNC, CONICET, etc.).\n"
        "   - SEPARADOR OBLIGATORIO DE SUBTÍTULO: ' : ' (espacio, dos puntos, espacio). Ejemplo: 'Título principal : subtítulo'.\n"
        "   - El subtítulo DEBE empezar en MINÚSCULA (salvo que la primera palabra sea un nombre propio o sigla).\n"
        "   - SIN PUNTO FINAL.\n"
        "   - NUNCA cambies ' : ' por ': ' (en RDU el espacio antes de los dos puntos es la norma institucional oficial).\n"
        "   - Si el título en RDU ya cumple esto o es equivalente, consérvalo tal cual.\n\n"
        "2. REGLA ESTRICTA PARA AUTORES:\n"
        "   - Formato OBLIGATORIO: SOLO 'Apellido, Nombre' (para que coincida con el registro institucional de filiaciones en Sheets).\n"
        "   - NUNCA agregues iniciales intermedias o secundarias (ej: si en el PDF dice 'Suárez, Andrea B.', debe quedar 'Suárez, Andrea'; si dice 'Mustaca, Alba E.', debe quedar 'Mustaca, Alba'). No incluyas iniciales sueltas como 'B.', 'E.', 'M.', etc.\n"
        "   - NO MODIFICAR AUTORES QUE YA COINCIDEN: Si los autores registrados en RDU ya corresponden a los del PDF y la única diferencia es que el PDF trae iniciales intermedias (ej: RDU tiene 'Suárez, Andrea' y el PDF tiene 'Suárez, Andrea B.'), NO DEBES MODIFICARLOS. Mantén exactamente los autores de RDU y reporta que NO hubo cambios de autores.\n"
        "   - Solo modifica autores si:\n"
        "     * Falta un autor en RDU que sí está en el PDF (agrégalo respetando el orden, formato 'Apellido, Nombre' sin iniciales secundarias).\n"
        "     * En RDU el nombre de pila está solo como inicial y en el PDF figura completo (ej: 'Abate, P.' en RDU -> 'Abate, Paula' según PDF).\n"
        "     * El orden de los autores en RDU está invertido respecto al PDF.\n"
        "     * Hay una errata ortográfica evidente en el apellido o nombre.\n\n"
        "3. RESUMEN DE MODIFICACIONES:\n"
        "   - Si realizas cambios efectivos en título o autores, redacta una nota BREVE y precisa.\n"
        "     Ejemplo: 'Modificaciones: se completó el nombre de Abate, P. a Abate, Paula; se agregó autor Gómez, María'\n"
        "   - Si no hubo cambios reales, deja cadena vacía y marca 'hubo_cambios': false.\n\n"
        "FORMATO DE RESPUESTA OBLIGATORIO (JSON ESTRICTO):\n"
        "{\n"
        '  "adjunto_corresponde": true o false,\n'
        '  "motivo_no_corresponde": "Breve explicación si adjunto_corresponde es false, o vacío",\n'
        '  "titulo_corregido": "Título corregido o idéntico",\n'
        '  "autores_corregidos": ["Apellido, Nombre", ...],\n'
        '  "hubo_cambios": true o false,\n'
        '  "cambios_titulo": "descripción del cambio o vacío",\n'
        '  "cambios_autores": "descripción del cambio o vacío",\n'
        '  "resumen_modificaciones": "Modificaciones: ... o vacío"\n'
        "}"
    )

    autores_str = "\n".join(f"- {a}" for a in autores_rdu) if autores_rdu else "(sin autores cargados)"
    user_prompt = (
        f"Título en RDU: {titulo_rdu}\n\n"
        f"Autores en RDU:\n{autores_str}\n\n"
        f"Texto extraído del PDF (primeras páginas):\n"
        f"\"\"\"\n{texto_pdf[:4500]}\n\"\"\""
    )

    for intento in range(1, 4):
        try:
            resp = client_ai.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=1000,
            )
            raw = (resp.choices[0].message.content or "").strip()
            limpio = raw.replace("```json", "").replace("```", "").strip()
            m = re.search(r"\{.*\}", limpio, re.DOTALL)
            res_dict = json.loads(m.group(0)) if m else json.loads(limpio)
            if isinstance(res_dict, dict) and "autores_corregidos" in res_dict:
                # Depurar autores devueltos para quitar iniciales secundarias
                res_dict["autores_corregidos"] = [
                    depurar_autor_sin_iniciales(a) for a in (res_dict.get("autores_corregidos") or []) if a
                ]
            return res_dict
        except Exception as e:
            print(f"  [WARN] Falló llamada a DeepSeek (intento {intento}/3): {e}", file=sys.stderr)
            time.sleep(2 * intento)

    return {"adjunto_corresponde": True, "hubo_cambios": False, "error": "No se pudo obtener respuesta estructurada de DeepSeek"}


def asumir_tarea(client: httpx.Client, pooltask_id: str) -> str:
    url = f"{API}/workflow/claimedtasks"
    body = f"{API}/workflow/pooltasks/{pooltask_id}"
    headers = _headers_con_csrf(client, {"Content-Type": "text/uri-list"})

    r = client.post(url, content=body, headers=headers)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Fallo al asumir tarea {pooltask_id}: HTTP {r.status_code} - {r.text[:300]}")
    return str(r.json().get("id", ""))


def marcar_adjunto_no_corresponde(
    client: httpx.Client,
    workflowitem_id: str,
    motivo: str,
) -> str:
    """Precede el campo Resumen con [ADJUNTO NO CORRESPONDE] y añade nota explicativa sin tocar autores ni título."""
    url_wfi = f"{API}/workflow/workflowitems/{workflowitem_id}"
    r = client.get(url_wfi)
    r.raise_for_status()
    wfi_data = r.json()

    sections = wfi_data.get("sections", {}) or {}
    sid_resumen = None
    val_resumen = None
    for sid, sval in sections.items():
        if isinstance(sval, dict) and "dc.description.abstract" in sval:
            sid_resumen = sid
            val_resumen = sval["dc.description.abstract"]
            break

    sid_resumen = sid_resumen or ("traditionalpageone" if "traditionalpageone" in sections else list(sections.keys())[0])

    resumen_actual = ""
    lang_resumen = "es"
    if val_resumen and len(val_resumen) > 0:
        resumen_actual = val_resumen[0].get("value", "")
        lang_resumen = val_resumen[0].get("language") or "es"

    marca = "[ADJUNTO NO CORRESPONDE]"
    # Agregar marca al inicio si no la tiene
    if not re.search(r"ADJUNTO\s+NO\s+CORRESPONDE", resumen_actual, re.IGNORECASE):
        nuevo_cuerpo = f"{marca} {resumen_actual}".strip()
    else:
        nuevo_cuerpo = resumen_actual

    # Agregar nota explicativa si se proporcionó motivo
    if motivo and f"[{motivo}]" not in nuevo_cuerpo:
        resumen_nuevo = f"{nuevo_cuerpo}\n\n[{motivo}]".strip()
    else:
        resumen_nuevo = nuevo_cuerpo

    if resumen_nuevo == resumen_actual:
        return resumen_actual

    patch_ops = []
    if val_resumen and len(val_resumen) > 0:
        patch_ops.append({
            "op": "replace",
            "path": f"/sections/{sid_resumen}/dc.description.abstract/0",
            "value": {"value": resumen_nuevo, "language": lang_resumen},
        })
    elif val_resumen is not None and isinstance(val_resumen, list):
        patch_ops.append({
            "op": "add",
            "path": f"/sections/{sid_resumen}/dc.description.abstract/-",
            "value": {"value": resumen_nuevo, "language": lang_resumen},
        })
    else:
        patch_ops.append({
            "op": "add",
            "path": f"/sections/{sid_resumen}/dc.description.abstract",
            "value": [{"value": resumen_nuevo, "language": lang_resumen}],
        })

    headers = _headers_con_csrf(client, {"Content-Type": "application/json"})
    r_patch = client.patch(url_wfi, json=patch_ops, headers=headers)
    if r_patch.status_code not in (200, 201):
        raise RuntimeError(f"Fallo al marcar resumen en workflowitem {workflowitem_id}: HTTP {r_patch.status_code} - {r_patch.text[:300]}")

    return resumen_nuevo


def aplicar_cambios_workflowitem(
    client: httpx.Client,
    workflowitem_id: str,
    titulo_nuevo: str,
    titulo_actual: str,
    autores_nuevos: list[str],
    autores_actuales: list[str],
    resumen_modificaciones: str,
) -> tuple[str, str, str]:
    """Aplica las modificaciones vía JSON Patch en el workflowitem."""
    url_wfi = f"{API}/workflow/workflowitems/{workflowitem_id}"
    r = client.get(url_wfi)
    r.raise_for_status()
    wfi_data = r.json()

    sections = wfi_data.get("sections", {}) or {}

    def _buscar_seccion(campo: str) -> tuple[str | None, list | None]:
        for sid, sval in sections.items():
            if isinstance(sval, dict) and campo in sval:
                return sid, sval[campo]
        return None, None

    sid_titulo, val_titulo = _buscar_seccion("dc.title")
    sid_autores, val_autores = _buscar_seccion("dc.contributor.author")
    sid_resumen, val_resumen = _buscar_seccion("dc.description.abstract")

    sid_default = "traditionalpageone" if "traditionalpageone" in sections else (list(sections.keys())[0] if sections else None)
    if not sid_default:
        raise RuntimeError(f"Workflowitem {workflowitem_id} no tiene secciones reconocibles.")

    sid_titulo = sid_titulo or sid_default
    sid_autores = sid_autores or sid_default
    sid_resumen = sid_resumen or sid_default

    patch_ops = []

    # 1. Título si cambió
    if titulo_nuevo and titulo_nuevo.strip() != titulo_actual.strip():
        lang_t = val_titulo[0].get("language") if (val_titulo and len(val_titulo) > 0 and isinstance(val_titulo[0], dict)) else "es"
        if val_titulo and len(val_titulo) > 0:
            patch_ops.append({
                "op": "replace",
                "path": f"/sections/{sid_titulo}/dc.title/0",
                "value": {"value": titulo_nuevo.strip(), "language": lang_t or "es"},
            })
        elif val_titulo is not None and isinstance(val_titulo, list):
            patch_ops.append({
                "op": "add",
                "path": f"/sections/{sid_titulo}/dc.title/-",
                "value": {"value": titulo_nuevo.strip(), "language": "es"},
            })
        else:
            patch_ops.append({
                "op": "add",
                "path": f"/sections/{sid_titulo}/dc.title",
                "value": [{"value": titulo_nuevo.strip(), "language": "es"}],
            })

    # 2. Autores si cambiaron
    if autores_nuevos and autores_nuevos != autores_actuales:
        if val_autores is None:
            patch_ops.append({
                "op": "add",
                "path": f"/sections/{sid_autores}/dc.contributor.author",
                "value": [{"value": a} for a in autores_nuevos],
            })
        else:
            cant_existente = len(val_autores) if val_autores else 0
            cant_nueva = len(autores_nuevos)

            for i in range(min(cant_existente, cant_nueva)):
                patch_ops.append({
                    "op": "replace",
                    "path": f"/sections/{sid_autores}/dc.contributor.author/{i}",
                    "value": {"value": autores_nuevos[i]},
                })

            if cant_nueva > cant_existente:
                for i in range(cant_existente, cant_nueva):
                    patch_ops.append({
                        "op": "add",
                        "path": f"/sections/{sid_autores}/dc.contributor.author/-",
                        "value": {"value": autores_nuevos[i]},
                    })

            if cant_existente > cant_nueva:
                for i in reversed(range(cant_nueva, cant_existente)):
                    patch_ops.append({
                        "op": "remove",
                        "path": f"/sections/{sid_autores}/dc.contributor.author/{i}",
                    })

    # 3. Resumen: concatenar nota al final entre corchetes
    resumen_actual = ""
    lang_resumen = "es"
    if val_resumen and len(val_resumen) > 0:
        resumen_actual = val_resumen[0].get("value", "")
        lang_resumen = val_resumen[0].get("language") or "es"

    nota_final = f"[{resumen_modificaciones.strip('[]')}]"
    if resumen_actual.strip():
        if nota_final not in resumen_actual:
            resumen_nuevo = f"{resumen_actual.strip()}\n\n{nota_final}"
        else:
            resumen_nuevo = resumen_actual
    else:
        resumen_nuevo = nota_final

    if resumen_nuevo != resumen_actual:
        if val_resumen and len(val_resumen) > 0:
            patch_ops.append({
                "op": "replace",
                "path": f"/sections/{sid_resumen}/dc.description.abstract/0",
                "value": {"value": resumen_nuevo, "language": lang_resumen},
            })
        elif val_resumen is not None and isinstance(val_resumen, list):
            patch_ops.append({
                "op": "add",
                "path": f"/sections/{sid_resumen}/dc.description.abstract/-",
                "value": {"value": resumen_nuevo, "language": lang_resumen},
            })
        else:
            patch_ops.append({
                "op": "add",
                "path": f"/sections/{sid_resumen}/dc.description.abstract",
                "value": [{"value": resumen_nuevo, "language": lang_resumen}],
            })

    if not patch_ops:
        return titulo_actual, "; ".join(autores_actuales), resumen_actual

    headers = _headers_con_csrf(client, {"Content-Type": "application/json"})
    r_patch = client.patch(url_wfi, json=patch_ops, headers=headers)
    if r_patch.status_code not in (200, 201):
        raise RuntimeError(
            f"Fallo al aplicar cambios en workflowitem {workflowitem_id}: "
            f"HTTP {r_patch.status_code} - {r_patch.text[:300]}"
        )

    return (
        titulo_nuevo if titulo_nuevo else titulo_actual,
        "; ".join(autores_nuevos) if autores_nuevos else "; ".join(autores_actuales),
        resumen_nuevo,
    )


def devolver_tarea_al_pool(client: httpx.Client, claimedtask_id: str):
    url = f"{API}/workflow/claimedtasks/{claimedtask_id}"
    headers = _headers_con_csrf(client)
    r = client.delete(url, headers=headers)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Fallo al devolver tarea {claimedtask_id} al pool: HTTP {r.status_code} - {r.text[:300]}")


def procesar_flujo(
    ui_url: str,
    dry_run: bool = False,
    limite: int = 0,
    pausa_segundos: float = 0.5,
    email: str = "",
    password: str = "",
    deepseek_key: str = "",
    deepseek_model: str = "deepseek-chat",
    archivo_csv: str = "",
):
    if not deepseek_key:
        sys.exit("[ERROR] Falta DEEPSEEK_API_KEY. Configurala en tu entorno o en los secrets de GitHub.")

    client_ai = OpenAI(api_key=deepseek_key, base_url="https://api.deepseek.com")

    print(f"[INICIO] Entrada: {ui_url}", file=sys.stderr)
    print(f"[INICIO] Modelo DeepSeek: {deepseek_model} | Dry-run: {dry_run} | Límite: {limite or 'Sin límite'}", file=sys.stderr)

    if not archivo_csv:
        os.makedirs("reportes", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archivo_csv = os.path.join("reportes", f"autores_corregidos_pdf_{timestamp}.csv")

    columnas_csv = [
        "Fecha",
        "Workflowitem_ID",
        "Item_UUID",
        "Titulo_Anterior",
        "Titulo_Nuevo",
        "Autores_Anteriores",
        "Autores_Nuevos",
        "Modificaciones",
        "Accion",
        "Link_Workflow",
        "Link_Item",
    ]

    filas_reporte = []
    conteo_modificados = 0
    conteo_sin_cambios = 0
    conteo_errores = 0

    with httpx.Client(timeout=60, headers=HEADERS_BASE, follow_redirects=True) as client:
        login(client, email, password)

        # Resuelve si es link individual o búsqueda en lote
        candidatos = resolver_candidatos(client, ui_url)
        print(f"[PROCESO] Cantidad de items a evaluar: {len(candidatos)}", file=sys.stderr)

        for idx, item in enumerate(candidatos, start=1):
            wf_id = item.get("workflowitem_id")
            if not wf_id:
                print(f"[SKIP] Objeto #{idx} sin workflowitem_id.", file=sys.stderr)
                continue

            # Traer SIEMPRE o enriquecer con los metadatos vivos y completos de RDU (sections, uuid, etc.)
            try:
                info_wfi = obtener_info_workflowitem(client, wf_id)
                for k, v in info_wfi.items():
                    if v or not item.get(k):
                        item[k] = v
            except Exception as e:
                print(f"  [WARN] No se pudo obtener información ampliada de workflowitem {wf_id}: {e}", file=sys.stderr)

            uuid = item.get("item_uuid", "")
            titulo_rdu = item.get("titulo", "")
            autores_rdu = item.get("autores", [])

            print(f"\n[PROCESANDO] #{idx} Item: {titulo_rdu[:70]}...")
            print(f"  Autores RDU: {autores_rdu}")
            print(f"  UUID: {uuid or '(no encontrado)'} | Workflowitem: {wf_id}")

            # Descargar PDF y extraer texto (busca en upload sections y en bundles)
            texto_pdf, det_pdf = descargar_y_extraer_texto_pdf(client, wf_id, uuid)
            if not texto_pdf:
                print(f"  [OMITIDO] No se pudo extraer texto del PDF: {det_pdf}")
                filas_reporte.append({
                    "Fecha": datetime.now().isoformat(),
                    "Workflowitem_ID": wf_id,
                    "Item_UUID": uuid,
                    "Titulo_Anterior": titulo_rdu,
                    "Titulo_Nuevo": titulo_rdu,
                    "Autores_Anteriores": "; ".join(autores_rdu),
                    "Autores_Nuevos": "; ".join(autores_rdu),
                    "Modificaciones": det_pdf,
                    "Accion": f"OMITIDO: {det_pdf}",
                    "Link_Workflow": item["link_workflow"],
                    "Link_Item": item["link_item"],
                })
                conteo_errores += 1
                continue

            # Cotejar con DeepSeek
            print("  Consultando a DeepSeek para evaluar correspondencia y metadatos vs PDF...")
            analisis = consultar_deepseek(client_ai, deepseek_model, titulo_rdu, autores_rdu, texto_pdf)

            # CASO A: EL PDF ADJUNTO NO CORRESPONDE AL ARTÍCULO
            if analisis.get("adjunto_corresponde") is False:
                motivo = analisis.get("motivo_no_corresponde") or "El PDF adjunto pertenece a otro artículo completamente diferente"
                print(f"  [ALERTA] ADJUNTO NO CORRESPONDE: {motivo}")
                print(f"  -> NO se modifican autores ni título. Se marcará [ADJUNTO NO CORRESPONDE] en el resumen.")

                if dry_run:
                    print("  [SIMULACIÓN] Se agregaría '[ADJUNTO NO CORRESPONDE]' al inicio del Resumen sin modificar autores/título.")
                    filas_reporte.append({
                        "Fecha": datetime.now().isoformat(),
                        "Workflowitem_ID": wf_id,
                        "Item_UUID": uuid,
                        "Titulo_Anterior": titulo_rdu,
                        "Titulo_Nuevo": titulo_rdu,
                        "Autores_Anteriores": "; ".join(autores_rdu),
                        "Autores_Nuevos": "; ".join(autores_rdu),
                        "Modificaciones": motivo,
                        "Accion": "SIMULADO_ADJUNTO_NO_CORRESPONDE",
                        "Link_Workflow": item["link_workflow"],
                        "Link_Item": item["link_item"],
                    })
                    conteo_modificados += 1
                else:
                    try:
                        pool_id = item.get("pooltask_id")
                        claimed_id = item.get("claimedtask_id")
                        if not pool_id and not claimed_id:
                            pool_id = buscar_pooltask_id(client, uuid)

                        if not claimed_id:
                            if not pool_id:
                                raise RuntimeError(f"No se encontró pooltask para el ítem {uuid}")
                            claimed_id = asumir_tarea(client, pool_id)
                            print(f"  [1/3] Tarea asumida (claimedtask_id: {claimed_id})")
                        else:
                            print(f"  [1/3] Tarea ya estaba asumida (claimedtask_id: {claimed_id})")

                        marcar_adjunto_no_corresponde(client, wf_id, motivo)
                        print(f"  [2/3] Resumen marcado con '[ADJUNTO NO CORRESPONDE]'. Autores y título intactos.")

                        devolver_tarea_al_pool(client, claimed_id)
                        print(f"  [3/3] Tarea devuelta al pool general.")

                        filas_reporte.append({
                            "Fecha": datetime.now().isoformat(),
                            "Workflowitem_ID": wf_id,
                            "Item_UUID": uuid,
                            "Titulo_Anterior": titulo_rdu,
                            "Titulo_Nuevo": titulo_rdu,
                            "Autores_Anteriores": "; ".join(autores_rdu),
                            "Autores_Nuevos": "; ".join(autores_rdu),
                            "Modificaciones": motivo,
                            "Accion": "MARCADO_ADJUNTO_NO_CORRESPONDE",
                            "Link_Workflow": item["link_workflow"],
                            "Link_Item": item["link_item"],
                        })
                        conteo_modificados += 1
                    except Exception as e:
                        conteo_errores += 1
                        print(f"  [ERROR] Falló al marcar {wf_id}: {e}", file=sys.stderr)
                        filas_reporte.append({
                            "Fecha": datetime.now().isoformat(),
                            "Workflowitem_ID": wf_id,
                            "Item_UUID": uuid,
                            "Titulo_Anterior": titulo_rdu,
                            "Titulo_Nuevo": titulo_rdu,
                            "Autores_Anteriores": "; ".join(autores_rdu),
                            "Autores_Nuevos": "; ".join(autores_rdu),
                            "Modificaciones": f"ERROR: {e}",
                            "Accion": "ERROR",
                            "Link_Workflow": item["link_workflow"],
                            "Link_Item": item["link_item"],
                        })

                time.sleep(pausa_segundos)
                if limite > 0 and conteo_modificados >= limite:
                    print(f"\n[LIMITE] Se alcanzó el límite de {limite} ítem(s). Finalizando.")
                    break
                continue

            # CASO B: EL ADJUNTO SÍ CORRESPONDE AL ARTÍCULO
            # 1. Depurar autores propuestos: estrictamente SOLO 'Apellido, Nombre' sin iniciales secundarias
            autores_propuestos_crudos = analisis.get("autores_corregidos") or autores_rdu
            autores_propuestos = [depurar_autor_sin_iniciales(a) for a in autores_propuestos_crudos]

            # 2. Si los autores propuestos coinciden con los de RDU (la única diferencia eran iniciales intermedias),
            # NO modificarlos: conservar exactamente los de RDU.
            if son_autores_equivalentes(autores_rdu, autores_propuestos):
                autores_nuevos = list(autores_rdu)
                cambio_autores = False
            else:
                autores_nuevos = autores_propuestos
                cambio_autores = (autores_nuevos != autores_rdu)

            # 3. Título corregido y estandarizado según normas institucionales RDU
            # Formato RDU: minúsculas (formato oración), ' : ' entre título y subtítulo,
            # subtítulo en minúscula, sin punto final.
            titulo_propuesto_raw = (analisis.get("titulo_corregido") or titulo_rdu).strip()
            titulo_rdu_std = estandarizar_titulo(titulo_rdu)
            titulo_propuesto_std = estandarizar_titulo(titulo_propuesto_raw)

            # Si el título propuesto es semánticamente equivalente al de RDU (mismas palabras, solo puntuación/mayúsculas)
            if son_titulos_equivalentes(titulo_rdu, titulo_propuesto_std):
                # Si el título en RDU ya cumplía la estandarización exacta (ej: ya tenía ' : ' y minúsculas)
                if titulo_rdu.strip() == titulo_rdu_std:
                    titulo_nuevo = titulo_rdu.strip()
                    cambio_titulo = False
                else:
                    # El título en RDU tenía mayúsculas sostenidas, faltaba espacio en dos puntos, etc.
                    titulo_nuevo = titulo_rdu_std
                    cambio_titulo = True
            else:
                # El título del PDF es diferente (corrección de palabras o erratas)
                titulo_nuevo = titulo_propuesto_std
                cambio_titulo = True

            # 4. Determinar si realmente existen cambios efectivos
            hay_cambios = cambio_titulo or cambio_autores
            if not hay_cambios:
                print("  [OK] DeepSeek y validación determinaron que los metadatos coinciden (sin iniciales extra, formato RDU ' : ') y NO se requieren modificaciones.")
                filas_reporte.append({
                    "Fecha": datetime.now().isoformat(),
                    "Workflowitem_ID": wf_id,
                    "Item_UUID": uuid,
                    "Titulo_Anterior": titulo_rdu,
                    "Titulo_Nuevo": titulo_rdu,
                    "Autores_Anteriores": "; ".join(autores_rdu),
                    "Autores_Nuevos": "; ".join(autores_rdu),
                    "Modificaciones": "Sin cambios requeridos (metadatos coincidentes)",
                    "Accion": "SIN_CAMBIOS",
                    "Link_Workflow": item["link_workflow"],
                    "Link_Item": item["link_item"],
                })
                conteo_sin_cambios += 1
                continue

            # Construir resumen de modificaciones conciso y preciso
            if cambio_titulo and not cambio_autores:
                if son_titulos_equivalentes(titulo_rdu, titulo_nuevo):
                    modificaciones = "Modificaciones: se estandarizó el formato del título (minúsculas y ' : ')"
                else:
                    modificaciones = "Modificaciones: se corrigió el título según PDF"
            elif cambio_autores and not cambio_titulo:
                modificaciones = analisis.get("resumen_modificaciones") or "Modificaciones en autores según PDF"
                if "inicial" in modificaciones.lower() and not any(es_token_inicial_secundaria(tok) for a in autores_nuevos for tok in a.split()[1:]):
                    modificaciones = "Modificaciones: autores actualizados según PDF"
            else:
                if son_titulos_equivalentes(titulo_rdu, titulo_nuevo):
                    mod_autores = analisis.get("resumen_modificaciones") or "Modificaciones en autores según PDF"
                    modificaciones = f"{mod_autores}; se estandarizó el formato del título (minúsculas y ' : ')"
                else:
                    modificaciones = analisis.get("resumen_modificaciones") or "Modificaciones en título y autores según PDF"

            print(f"  [CAMBIOS DETECTADOS]: {modificaciones}")
            if cambio_titulo:
                print(f"    - Título: {titulo_rdu} -> {titulo_nuevo}")
            if cambio_autores:
                print(f"    - Autores: {autores_rdu} -> {autores_nuevos}")

            if dry_run:
                print("  [SIMULACIÓN] No se escriben cambios en RDU (dry-run activo).")
                filas_reporte.append({
                    "Fecha": datetime.now().isoformat(),
                    "Workflowitem_ID": wf_id,
                    "Item_UUID": uuid,
                    "Titulo_Anterior": titulo_rdu,
                    "Titulo_Nuevo": titulo_nuevo,
                    "Autores_Anteriores": "; ".join(autores_rdu),
                    "Autores_Nuevos": "; ".join(autores_nuevos),
                    "Modificaciones": modificaciones,
                    "Accion": "SIMULADO_CORREGIDO",
                    "Link_Workflow": item["link_workflow"],
                    "Link_Item": item["link_item"],
                })
                conteo_modificados += 1
            else:
                try:
                    pool_id = item.get("pooltask_id")
                    claimed_id = item.get("claimedtask_id")

                    if not pool_id and not claimed_id:
                        pool_id = buscar_pooltask_id(client, uuid)

                    if not claimed_id:
                        if not pool_id:
                            raise RuntimeError(f"No se encontró pooltask para el ítem {uuid}")
                        claimed_id = asumir_tarea(client, pool_id)
                        print(f"  [1/3] Tarea asumida (claimedtask_id: {claimed_id})")
                    else:
                        print(f"  [1/3] Tarea ya estaba asumida (claimedtask_id: {claimed_id})")

                    t_fin, a_fin, r_fin = aplicar_cambios_workflowitem(
                        client=client,
                        workflowitem_id=wf_id,
                        titulo_nuevo=titulo_nuevo,
                        titulo_actual=titulo_rdu,
                        autores_nuevos=autores_nuevos,
                        autores_actuales=autores_rdu,
                        resumen_modificaciones=modificaciones,
                    )
                    print("  [2/3] Cambios aplicados con éxito en metadatos y resumen.")

                    devolver_tarea_al_pool(client, claimed_id)
                    print("  [3/3] Tarea devuelta al pool general.")

                    filas_reporte.append({
                        "Fecha": datetime.now().isoformat(),
                        "Workflowitem_ID": wf_id,
                        "Item_UUID": uuid,
                        "Titulo_Anterior": titulo_rdu,
                        "Titulo_Nuevo": t_fin,
                        "Autores_Anteriores": "; ".join(autores_rdu),
                        "Autores_Nuevos": a_fin,
                        "Modificaciones": modificaciones,
                        "Accion": "MODIFICADO_Y_DEVUELTO_AL_POOL",
                        "Link_Workflow": item["link_workflow"],
                        "Link_Item": item["link_item"],
                    })
                    conteo_modificados += 1

                except Exception as e:
                    conteo_errores += 1
                    print(f"  [ERROR] Falló la actualización de {wf_id}: {e}", file=sys.stderr)
                    filas_reporte.append({
                        "Fecha": datetime.now().isoformat(),
                        "Workflowitem_ID": wf_id,
                        "Item_UUID": uuid,
                        "Titulo_Anterior": titulo_rdu,
                        "Titulo_Nuevo": titulo_nuevo,
                        "Autores_Anteriores": "; ".join(autores_rdu),
                        "Autores_Nuevos": "; ".join(autores_nuevos),
                        "Modificaciones": f"ERROR: {e}",
                        "Accion": "ERROR",
                        "Link_Workflow": item["link_workflow"],
                        "Link_Item": item["link_item"],
                    })

            time.sleep(pausa_segundos)

            if limite > 0 and conteo_modificados >= limite:
                print(f"\n[LIMITE] Se alcanzó el límite de {limite} ítem(s) procesados. Finalizando.")
                break

    os.makedirs(os.path.dirname(archivo_csv) or ".", exist_ok=True)
    with open(archivo_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columnas_csv)
        writer.writeheader()
        writer.writerows(filas_reporte)

    print(
        f"\n[FINALIZADO] Procesamiento completo:\n"
        f"  - Ítems modificados / simulados: {conteo_modificados}\n"
        f"  - Ítems sin cambios necesarios: {conteo_sin_cambios}\n"
        f"  - Ítems con error / omitidos: {conteo_errores}\n"
        f"  - Reporte guardado en: {archivo_csv}"
    )


def main():
    parser = argparse.ArgumentParser(description="Corrige autores y título en RDU a partir del PDF con DeepSeek.")
    parser.add_argument("--url", default=os.environ.get("RDU_UI_URL", DEFAULT_UI_URL), help="URL de MyDSpace o link específico de workflowitem")
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN", "0") in ("1", "true", "True"), help="Modo simulación sin escribir en RDU")
    parser.add_argument("--limite", type=int, default=int(os.environ.get("LIMITE", "0")), help="Límite de ítems a modificar (0 = sin límite)")
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"), help="Modelo de DeepSeek (default deepseek-chat)")
    parser.add_argument("--output", default=os.environ.get("ARCHIVO_REPORTE", ""), help="Ruta del CSV de salida")

    args = parser.parse_args()

    email = os.environ.get("RDU_EMAIL") or os.environ.get("RDU_USER") or ""
    password = os.environ.get("RDU_PASSWORD") or os.environ.get("RDU_PASS") or ""
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY") or ""

    procesar_flujo(
        ui_url=args.url,
        dry_run=args.dry_run,
        limite=args.limite,
        email=email,
        password=password,
        deepseek_key=deepseek_key,
        deepseek_model=args.model,
        archivo_csv=args.output,
    )


if __name__ == "__main__":
    main()
