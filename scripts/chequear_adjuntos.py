"""
Chequeo liviano de adjuntos rotos en RDU.

A diferencia de main.py (que abre cada ítem con Playwright, llama a una IA y
necesita login), este script SOLO lee la API pública de DSpace — sin
credenciales, sin navegador, sin dependencias externas (pura librería
estándar de Python) — para detectar adjuntos vacíos o ausentes. Nunca escribe
ni modifica nada en RDU: el resultado es un reporte en CSV
(reportes/adjuntos_rotos.csv) para revisar a mano.

Dos chequeos:
  1. RÁPIDO (siempre completo, cada corrida): ítems sin NINGÚN archivo en el
     bundle ORIGINAL. Usa el filtro nativo de DSpace
     `has_content_in_original_bundle=false`, así que es una sola consulta
     liviana sin importar el tamaño del repositorio.
  2. GRADUAL (avanza de a tramos, con checkpoint): recorre TODO el
     repositorio buscando ítems que sí tienen archivo pero pesa
     UMBRAL_BYTES_VACIO bytes o menos (los vacíos observados en RDU pesan 42
     bytes). Pedirle a la API los bitstreams de cada ítem es lento en el
     servidor (varios segundos cada 20 ítems), así que recorrer los ~35000
     ítems del repositorio de una sola corrida tardaría horas. Por eso cada
     corrida procesa hasta MAX_PAGINAS_POR_CORRIDA páginas y guarda dónde
     quedó en reportes/checkpoint_adjuntos.txt; la corrida siguiente retoma
     ahí. Al llegar al final vuelve a arrancar desde el principio, así que
     con un cron cada pocas horas el repositorio entero queda re-chequeado
     cada uno o dos días, en bucle continuo.
"""
import csv
import json
import os
import time
import urllib.request
from datetime import date

BASE_URL = os.environ.get("RDU_BASE_URL", "https://rdu.unc.edu.ar").rstrip("/")
API = f"{BASE_URL}/server/api"

TAMANO_PAGINA = int(os.environ.get("TAMANO_PAGINA", "20"))
MAX_PAGINAS_POR_CORRIDA = int(os.environ.get("MAX_PAGINAS_POR_CORRIDA", "150"))
UMBRAL_BYTES_VACIO = int(os.environ.get("UMBRAL_BYTES_VACIO", "42"))
PAUSA_SEGUNDOS = float(os.environ.get("PAUSA_SEGUNDOS", "0.3"))  # para no saturar el servidor

DIR_REPORTES = "reportes"
ARCHIVO_CHECKPOINT = os.path.join(DIR_REPORTES, "checkpoint_adjuntos.txt")
ARCHIVO_REPORTE = os.path.join(DIR_REPORTES, "adjuntos_rotos.csv")

CAMPOS_REPORTE = ["handle", "titulo", "tipo", "problema", "bytes", "link", "detectado"]

MARCA = "[ADJUNTO NO FUNCIONA]"


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read())


def _link(handle: str) -> str:
    return f"{BASE_URL}/handle/{handle}"


def _metadata(item: dict, campo: str) -> str:
    valores = item.get("metadata", {}).get(campo, [])
    return "; ".join(v.get("value", "") for v in valores if v.get("value"))


def _leer_reporte() -> dict:
    """Devuelve {handle: fila} con lo que ya está reportado como roto de corridas anteriores."""
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


def _fila_para(item: dict, problema: str, tam: int) -> dict:
    handle = item.get("handle", "")
    return {
        "handle": handle,
        "titulo": item.get("name", ""),
        "tipo": _metadata(item, "dc.type"),
        "problema": f"{MARCA} {problema}",
        "bytes": tam,
        "link": _link(handle),
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


# ---------- CHEQUEO RÁPIDO: ítems sin ningún archivo original ----------
def chequeo_rapido(filas_reporte: dict):
    print("[RÁPIDO] Buscando ítems sin ningún archivo en el bundle ORIGINAL...")
    encontrados_ahora = set()
    pagina = 0
    while True:
        url = (f"{API}/discover/search/objects?dsoType=item"
               f"&f.has_content_in_original_bundle=false,equals"
               f"&size=100&page={pagina}")
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


# ---------- CHEQUEO GRADUAL: recorre todo el repositorio buscando adjuntos rotos ----------
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
        url = (f"{API}/discover/search/objects?dsoType=item"
               f"&embed=bundles/bitstreams&size={TAMANO_PAGINA}&page={pagina}")
        try:
            data = _get(url)
        except Exception as e:
            print(f"  [WARN] Falló la página {pagina}: {e}. Se reintenta en la próxima corrida.")
            break

        resultado = data["_embedded"]["searchResult"]
        total_paginas = resultado["page"]["totalPages"]
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
                # Ya no está roto (se corrigió desde el último chequeo de este tramo).
                del filas_reporte[handle]
                resueltos += 1

        paginas_procesadas += 1
        pagina += 1
        if pagina >= total_paginas:
            print("  [GRADUAL] Se completó una vuelta entera al repositorio. Reiniciando desde el principio.")
            pagina = 0
        time.sleep(PAUSA_SEGUNDOS)

    _guardar_checkpoint(pagina)
    print(f"[GRADUAL] {paginas_procesadas} página(s) procesada(s) esta corrida "
          f"({paginas_procesadas * TAMANO_PAGINA} ítems). "
          f"Rotos detectados: {rotos_detectados}. Resueltos desde la última vez: {resueltos}.")
    if total_paginas:
        avance = min(100.0, round(pagina / total_paginas * 100, 1))
        print(f"[GRADUAL] Próxima corrida retoma en la página {pagina} de {total_paginas} (~{avance}% del ciclo).")


def main():
    filas_reporte = _leer_reporte()
    chequeo_rapido(filas_reporte)
    chequeo_gradual(filas_reporte)
    _guardar_reporte(filas_reporte)
    print(f"\nReporte actualizado: {ARCHIVO_REPORTE} ({len(filas_reporte)} ítem(s) con adjunto roto).")


if __name__ == "__main__":
    main()
