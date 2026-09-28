"""
Búsqueda web de filiaciones institucionales.

DeepSeek no tiene búsqueda web nativa (sus "tool calls" son propuestas que el
backend debe ejecutar). Así que acá hacemos la búsqueda nosotros: consultamos
fuentes públicas y devolvemos texto de contexto que después DeepSeek formatea.

Fuentes consultadas (todas públicas, sin login):
- DuckDuckGo HTML (buscador sin API key)
El texto crudo encontrado se le pasa a claude_steps.generar_filiacion() como
contexto adicional, junto con lo que ya se extrajo del PDF.
"""
from __future__ import annotations

import urllib.parse
import urllib.request
import re
import html as html_lib

UA = "Mozilla/5.0 (compatible; rdu-agent/1.0)"


def _buscar_web(consulta: str, max_resultados: int = 5) -> str:
    """Busca en la web y devuelve snippets. Usa el endpoint lite de DuckDuckGo
    que es más tolerante a peticiones automatizadas; si falla, devuelve vacío."""
    import time
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(consulta)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "es-AR,es;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            crudo = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  [DEBUG] Búsqueda web falló para '{consulta[:40]}': {e}")
        return ""
    # En la versión lite los resultados están en celdas de tabla.
    textos = re.findall(r'<td[^>]*>(.*?)</td>', crudo, re.DOTALL)
    limpios = []
    for t in textos:
        txt = html_lib.unescape(re.sub(r"<[^>]+>", "", t)).strip()
        if len(txt) > 30:  # descartar celdas de navegación/ruido
            limpios.append(txt)
        if len(limpios) >= max_resultados:
            break
    time.sleep(1)  # cortesía para no ser bloqueado
    if not limpios:
        print(f"  [DEBUG] Búsqueda web sin snippets útiles para '{consulta[:40]}'.")
    return "\n".join(limpios)


def buscar_filiacion_web(autor: str, titulo_trabajo: str = "") -> str:
    """Arma consultas orientadas a encontrar la afiliación institucional de un
    autor académico argentino y devuelve el texto de contexto encontrado."""
    consultas = [
        f'"{autor}" filiación universidad',
        f'"{autor}" CONICET OR "Universidad Nacional"',
    ]
    if titulo_trabajo:
        consultas.append(f'"{autor}" {titulo_trabajo[:60]}')

    contexto = []
    for c in consultas:
        resultado = _buscar_web(c)
        if resultado:
            contexto.append(f"Búsqueda: {c}\n{resultado}")

    return "\n\n".join(contexto) if contexto else ""


def buscar_trayectoria_openalex(autor: str) -> dict:
    """Consulta la API abierta de OpenAlex para obtener el perfil del autor y
    sus afiliaciones institucionales con sus respectivos años de actividad."""
    import json
    # Convertir "Apellido, Nombre" -> "Nombre Apellido" para búsqueda óptima
    partes = [p.strip() for p in (autor or "").split(",") if p.strip()]
    nombre_busqueda = " ".join(reversed(partes)) if len(partes) > 1 else (autor or "")
    if not nombre_busqueda:
        return {}

    url = "https://api.openalex.org/authors?search=" + urllib.parse.quote(nombre_busqueda)
    req = urllib.request.Request(url, headers={
        "User-Agent": "rdu-enricher/1.0 (mailto:biblioteca@unc.edu.ar)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])
            if not results:
                return {}
            # Tomar el resultado más relevante (con mayor número de obras / relevancia)
            autor_match = results[0]
            afiliaciones = []
            for aff in autor_match.get("affiliations", []):
                inst = aff.get("institution", {})
                anios = sorted(aff.get("years", []))
                afiliaciones.append({
                    "institucion": inst.get("display_name", ""),
                    "pais": inst.get("country_code", ""),
                    "anios": anios,
                    "inicio": anios[0] if anios else None,
                    "fin": anios[-1] if anios else None,
                })
            return {
                "nombre_oficial": autor_match.get("display_name", ""),
                "obras": autor_match.get("works_count", 0),
                "afiliaciones": afiliaciones,
            }
    except Exception as e:
        print(f"  [DEBUG] OpenAlex query falló para '{nombre_busqueda}': {e}")
        return {}


def buscar_filiacion_temporal_web(autor: str, filiacion_actual: str = "", anio: int | str | None = None) -> str:
    """Arma consultas orientadas a determinar la trayectoria temporal y afiliación
    institucional de un autor combinando la API científica OpenAlex y búsqueda web."""
    contexto = []

    # 1. Fuente primaria estructurada: OpenAlex
    alex = buscar_trayectoria_openalex(autor)
    if alex and alex.get("afiliaciones"):
        lineas = [f"Perfil académico OpenAlex: {alex.get('nombre_oficial')} ({alex.get('obras')} obras registradas)"]
        lineas.append("Afiliaciones documentadas por publicaciones científicas:")
        for aff in alex["afiliaciones"][:10]:
            anios_str = f"años: {aff['anios']}" if aff['anios'] else "sin años registrados"
            lineas.append(f" - {aff['institucion']} ({aff['pais']}): {anios_str}")
        contexto.append("\n".join(lineas))

    # 2. Búsqueda web complementaria
    partes = [p.strip() for p in (autor or "").split(",") if p.strip()]
    nombre_dir = " ".join(reversed(partes)) if len(partes) > 1 else (autor or "")

    consultas = [
        f'"{nombre_dir}" CONICET OR "Universidad Nacional"',
    ]
    if filiacion_actual:
        partes_fil = re.sub(r"^Fil:\s*[^.]+\.\s*", "", filiacion_actual)
        inst_limpia = " ".join(partes_fil.replace(";", " ").replace(".", " ").split()[:6])
        if inst_limpia:
            consultas.append(f'"{nombre_dir}" {inst_limpia}')

    if anio:
        m = re.search(r"\b(19\d\d|20\d\d)\b", str(anio))
        if m:
            consultas.append(f'"{nombre_dir}" {m.group(1)} filiación OR UNC')

    for c in consultas:
        resultado = _buscar_web(c, max_resultados=3)
        if resultado:
            contexto.append(f"Búsqueda web ({c}):\n{resultado}")

    return "\n\n".join(contexto) if contexto else ""


