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
