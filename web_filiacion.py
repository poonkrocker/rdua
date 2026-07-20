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


def _buscar_duckduckgo(consulta: str, max_resultados: int = 5) -> str:
    """Devuelve un texto plano con los snippets de los primeros resultados."""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(consulta)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            crudo = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return f"[sin resultados de búsqueda: {e}]"

    # Extraer los snippets de texto de los resultados.
    snippets = re.findall(r'result__snippet[^>]*>(.*?)</a>', crudo, re.DOTALL)
    limpios = []
    for s in snippets[:max_resultados]:
        texto = re.sub(r"<[^>]+>", "", s)        # quitar HTML
        texto = html_lib.unescape(texto).strip()  # decodificar entidades
        if texto:
            limpios.append(texto)
    return "\n".join(limpios) if limpios else "[sin snippets]"


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
        resultado = _buscar_duckduckgo(c)
        if resultado and not resultado.startswith("[sin"):
            contexto.append(f"Búsqueda: {c}\n{resultado}")

    return "\n\n".join(contexto) if contexto else ""
