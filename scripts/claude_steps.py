"""
Llamadas a la IA, una función por paso del flujo de edición.
Cada función es independiente para poder loguear / debuggear paso a paso.

Soporta dos proveedores, elegidos por la variable de entorno PROVEEDOR:
  - PROVEEDOR=claude   -> usa la API de Anthropic (Claude)
  - PROVEEDOR=deepseek -> usa la API de DeepSeek (más barata)

Por defecto usa deepseek si no se especifica.
"""
import os
import json

PROVEEDOR = os.environ.get("PROVEEDOR", "deepseek").lower()

if PROVEEDOR == "claude":
    from anthropic import Anthropic
    _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    MODEL = "claude-sonnet-4-6"

    def _llamar_raw(system, user_content, max_tokens=500):
        resp = _client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        return resp.content[0].text.strip()

else:  # deepseek (compatible con el formato OpenAI)
    from openai import OpenAI
    _client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )
    MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

    def _llamar_raw(system, user_content, max_tokens=500):
        resp = _client.chat.completions.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        )
        contenido = resp.choices[0].message.content
        return (contenido or "").strip()


def _llamar(system, user_content, max_tokens=500, reintentos=4):
    """Wrapper con reintento y backoff exponencial: si la API devuelve vacío
    (throttling, timeout parcial, etc.) o lanza una excepción, reintenta antes
    de rendirse, esperando cada vez más entre intentos. Esto evita el bug donde
    un título/autor/filiación quedaba sin editar porque la IA falló una vez
    (frecuente en corridas reales con muchas llamadas seguidas) y el código lo
    tomaba como definitivo.

    También hace una pequeña pausa DESPUÉS de cada llamada exitosa (pacing),
    para no ráfaguear la API y reducir la chance de gatillar el throttling
    en la siguiente llamada."""
    import time
    ultimo_error = None
    for intento in range(1, reintentos + 2):
        try:
            resultado = _llamar_raw(system, user_content, max_tokens)
            if resultado:
                time.sleep(0.6)  # pacing: espaciar llamadas exitosas también
                return resultado
            print(f"  [WARN] La IA devolvió respuesta VACÍA (intento {intento}/{reintentos + 1}).")
        except Exception as e:
            ultimo_error = e
            print(f"  [WARN] Error llamando a la IA (intento {intento}): {e}.")
        espera = min(2 ** intento, 15)  # backoff exponencial, tope 15s
        print(f"  [INFO] Reintentando en {espera}s...")
        time.sleep(espera)
    print(f"  [ALERTA] La IA no respondió tras {reintentos + 1} intentos"
          f"{f' (último error: {ultimo_error})' if ultimo_error else ' (respuestas vacías)'}.")
    return ""


def formatear_titulo(titulo_original: str) -> str:
    system = (
        "Formateá títulos académicos a sentence case: mayúscula solo en la primera "
        "palabra y nombres propios, resto en minúscula salvo siglas conocidas "
        "(UNC, CONICET, etc.) y números romanos. Si hay subtítulo, separalo con "
        "' : ' (espacio, dos puntos, espacio) con la primera palabra en minúscula "
        "salvo nombre propio. Nunca termina en punto, '!', '?' ni ';'. "
        "Corregí espacios dobles y tildes faltantes. "
        "Respondé ÚNICAMENTE con el título formateado, sin explicación ni comillas."
    )
    resultado = _llamar(system, titulo_original)
    if not resultado:
        # Fallback: si la IA no respondió ni con reintentos, NO dejamos el
        # título sin tocar en silencio. Devolvemos el original tal cual (se
        # sabrá por el log) en vez de perder el trabajo de formateo.
        print("  [ALERTA] No se pudo formatear el título con la IA; se deja el original sin cambios.")
        return titulo_original
    return resultado


def separar_y_formatear_autores(campos_crudos: list) -> dict:
    """Recibe el texto crudo de TODOS los campos de autor (cada uno puede tener
    varios autores apelmazados, ej. 'SUEN PABLO - DRUBI MARIA SOLEDAD') y
    devuelve la lista de autores individuales en formato 'Apellido, Nombre'.

    Devuelve: {"autores": [str, ...], "confianza": "alta"|"baja", "nota": str}
    """
    texto = "\n".join(f"- Campo {i}: {c}" for i, c in enumerate(campos_crudos))
    system = (
        "Sos un asistente de catalogación bibliográfica argentino. Recibís el "
        "contenido crudo de los campos de 'Autores' de un trabajo académico. "
        "Cada campo puede contener UNO O VARIOS autores juntos, a veces separados "
        "por ' - ', a veces por coma, a veces con un guion suelto al inicio, a "
        "veces en MAYÚSCULAS, y en casos difíciles sin separador claro. "
        "\n\nPISTAS para separar autores:\n"
        "- Un APELLIDO EN MAYÚSCULAS suele marcar el comienzo de un autor NUEVO. "
        "Ej: 'CASTAGNO Mariel, BARTOLACCI, Verónica' son DOS autores: "
        "'Castagno, Mariel' y 'Bartolacci, Verónica' (BARTOLACCI en mayúsculas "
        "indica que empieza otro autor).\n"
        "- ' - ' separa autores. Un guion suelto al inicio es ruido, ignoralo.\n"
        "- Una coma sobrante al final de un autor (ej. 'Giorgis, Lucía,') es ruido.\n"
        "\nTu tarea: identificar TODOS los autores individuales y devolver cada uno "
        "en formato 'Apellido, Nombre' (apellido primero, luego nombre/s). "
        "Convertí de MAYÚSCULAS a mayúscula inicial (ej. 'SUEN PABLO' -> 'Suen, Pablo'). "
        "Para nombres compuestos argentinos, asumí que el primer token es el "
        "apellido y el resto el/los nombre/s, salvo apellido compuesto evidente. "
        "NO inventes autores ni completes nombres que no estén. "
        "Si NO estás razonablemente seguro de cómo separar o de cuál es el apellido, "
        "poné confianza 'baja'. "
        "Respondé SOLO en JSON con esta forma exacta: "
        '{"autores": ["Apellido, Nombre", ...], "confianza": "alta o baja", "nota": "aclaración breve si confianza es baja"}'
    )
    raw = _llamar(system, texto, max_tokens=600)
    data = _parsear_json(raw)
    if data and isinstance(data.get("autores"), list) and data["autores"]:
        conf = str(data.get("confianza", "baja")).lower()
        data["confianza"] = "alta" if "alta" in conf else "baja"
        return data
    print(f"  [WARN] No se pudo parsear la respuesta de autores. Crudo: {raw[:200]}")
    return {"autores": [], "confianza": "baja", "nota": "No se pudo parsear la respuesta de la IA."}


def formatear_materia(termino_crudo: str) -> str:
    system = (
        "Formateá un descriptor/palabra clave académica: primera letra en "
        "mayúscula, el resto en minúscula (sin mayúscula sostenida). "
        "Respondé ÚNICAMENTE con el término formateado, sin explicación."
    )
    return _llamar(system, termino_crudo, max_tokens=50)


def limpiar_resumen(texto_crudo: str) -> str:
    system = (
        "Limpiá el formato de este resumen académico SIN alterar el contenido "
        "ni la redacción original: quitá etiquetas HTML y entidades mal "
        "decodificadas, eliminá saltos de línea y tabulaciones uniendo todo en "
        "un párrafo continuo, reducí espacios múltiples a uno, corregí espacios "
        "antes/después de signos de puntuación, y convertí pares '?texto?' a "
        "comillas tipográficas. No resumas ni reescribas las ideas. "
        "Respondé ÚNICAMENTE con el texto limpio, sin explicación."
    )
    return _llamar(system, texto_crudo, max_tokens=2000)


def generar_filiacion(autor_formateado: str, contexto_pdf: str = "") -> dict:
    """Devuelve {"filiacion": str | None, "confianza": "alta"|"baja", "fuente": str}"""
    system = (
        "Generás filiaciones institucionales académicas en formato: "
        "'Fil: Apellido, Nombre. [Unidad académica]. [Universidad/Institución]; [País].' "
        "Si es UNC, escribí siempre 'Universidad Nacional de Córdoba' completo. "
        "Si es CONICET sin universidad asociada, usá "
        "'Consejo Nacional de Investigaciones Científicas y Técnicas'. "
        "País por defecto Argentina. Buscá la filiación en el contexto del PDF dado. "
        "Si no podés confirmarla con ese contexto, NO inventes una institución. "
        "Respondé ÚNICAMENTE en JSON con esta forma exacta: "
        '{"filiacion": "texto o null", "confianza": "alta o baja", "fuente": "de dónde la sacaste o vacío"}'
    )
    user = f"Autor: {autor_formateado}\n\nContexto extraído del PDF:\n{contexto_pdf[:3000]}"
    raw = _llamar(system, user, max_tokens=300)
    parsed = _parsear_json(raw)
    if parsed and "filiacion" in parsed:
        return parsed
    return {"filiacion": None, "confianza": "baja", "fuente": ""}


def verificar_pdf(titulo_formateado: str, autores_formateados: list, texto_pdf: str) -> dict:
    """Devuelve {"coincide": bool, "detalle": str}"""
    system = (
        "Comparás el contenido de un PDF académico contra metadatos cargados. "
        "Evaluá si el título y los autores del PDF corresponden (en contenido, "
        "no en formato exacto) a los metadatos dados. "
        "Respondé ÚNICAMENTE en JSON: "
        '{"coincide": true o false, "detalle": "breve explicación"}'
    )
    user = (
        f"Título cargado: {titulo_formateado}\n"
        f"Autores cargados: {', '.join(autores_formateados)}\n\n"
        f"Primeras ~3000 caracteres del PDF:\n{texto_pdf[:3000]}"
    )
    raw = _llamar(system, user, max_tokens=300)
    parsed = _parsear_json(raw)
    if parsed and "coincide" in parsed:
        return parsed
    return {"coincide": False, "detalle": "No se pudo interpretar la respuesta de verificación."}


def _parsear_json(raw: str) -> dict | None:
    """Extrae y parsea un objeto JSON de la respuesta de la IA, tolerando texto
    extra o backticks alrededor."""
    import json, re
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    for cand in (cleaned, (re.search(r"\{.*\}", raw, re.DOTALL) or [None])[0] if re.search(r"\{.*\}", raw, re.DOTALL) else None):
        if not cand:
            continue
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None
