from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# Rutas locales
DIR_ACTUAL = os.path.dirname(os.path.abspath(__file__))
if DIR_ACTUAL not in sys.path:
    sys.path.insert(0, DIR_ACTUAL)

import formato
import sheets_client
import web_filiacion

# Configuración por defecto
MODELO_GEMINI_DEFAULT = "gemini-3.6-flash"
PAUSA_ENTRE_LLAMADAS_SEG = 4.5  # Respeta el límite de 15 RPM de Google AI Studio Free Tier (~13.3 RPM)


# ------------------------------------------------------------------- cliente gemini
def llamar_gemini(
    prompt: str,
    api_key: str,
    modelo: str = MODELO_GEMINI_DEFAULT,
    reintentos: int = 4,
) -> dict:
    """Llama a la API de Google Gemini en modo JSON estructurado.
    Utiliza thinkingBudget: 0 para obtener respuestas en <2s y evitar sobrecargas de cuota."""
    if not api_key:
        raise ValueError("No se proporcionó API key de Gemini (GEMINI_API_KEY).")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
            "thinkingConfig": {
                "thinkingBudget": 0
            }
        }
    }).encode("utf-8")

    ultimo_error = None
    for intento in range(1, reintentos + 1):
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidatos = data.get("candidates", [])
                if not candidatos:
                    return {}
                texto_json = candidatos[0]["content"]["parts"][0]["text"]
                return json.loads(texto_json)
        except urllib.error.HTTPError as e:
            codigo = e.code
            cuerpo = ""
            try:
                cuerpo = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            ultimo_error = f"HTTP {codigo}: {cuerpo[:120]}"
            print(f"  [WARN] Gemini API devolvió {codigo} (intento {intento}/{reintentos})...", file=sys.stderr)
            if codigo == 429:
                # Extraer retryDelay devuelto por la API de Google
                m_delay = re.search(r"retry in (\d+(?:\.\d+)?)s", cuerpo) or re.search(r'"retryDelay":\s*"(\d+)s"', cuerpo)
                espera = (float(m_delay.group(1)) + 2.0) if m_delay else 35.0
                print(f"  [INFO] Límite de solicitudes por minuto alcanzado. Esperando {espera:.1f}s...", file=sys.stderr)
                time.sleep(espera)
            elif codigo in (500, 503):
                espera = 5 * intento
                time.sleep(espera)
            else:
                break
        except Exception as e:
            ultimo_error = str(e)
            print(f"  [WARN] Error de conexión con Gemini: {e} (intento {intento}/{reintentos})", file=sys.stderr)
            time.sleep(3 * intento)

    print(f"  [ERROR] Fallaron todos los intentos con Gemini: {ultimo_error}", file=sys.stderr)
    return {}


# ------------------------------------------------------------------- prompts
def armar_prompt_enriquecer_filiacion(autor: str, filiacion_actual: str, contexto_academico: str) -> str:
    """Prompt para determinar Año_Inicio, Año_Fin y filiación estándar a partir de datos académicos."""
    return f"""Sos un bibliotecario catalogador experto del Repositorio Digital Universitario (RDU - Universidad Nacional de Córdoba).
Tu objetivo es analizar la trayectoria institucional de un autor para determinar el período de años (Año_Inicio y Año_Fin)
asociado a su filiación institucional existente.

DATOS DEL AUTOR:
- Autor: {autor}
- Filiación actual registrada en RDU: {filiacion_actual}

HISTORIAL DE PUBLICACIONES Y AFILIACIONES DOCUMENTADAS:
{contexto_academico if contexto_academico else "No se encontraron registros web adicionales."}

REGLAS INSTITUCIONALES RDU:
1. 'anio_inicio': Año (entero de 4 dígitos) más temprano documentado en que el autor ejerció o publicó bajo esa institución. Si no se puede determinar fehacientemente, devolvé null.
2. 'anio_fin': Año (entero de 4 dígitos) de finalización si el autor cesó en esa institución. Si sigue activo actualmente en esa institución o es la filiación vigente hoy, devolvé null.
3. 'vigente_actualidad': true si el autor continúa vinculado hoy a esa institución según los datos, false en caso contrario.
4. 'filiacion_estandar': Filiación en formato estandarizado RDU ('Fil: Apellido, Nombre. [Unidad menor]. [Institución mayor]; [País].'). Si la filiación actual ya es correcta, conservarla.
5. 'confianza': 'alta', 'media' o 'baja'.
6. 'observacion': Justificación concisa explicando de dónde provienen los años determinados.

Devuelve EXCLUSIVAMENTE un objeto JSON válido con la siguiente estructura exacta:
{{
  "anio_inicio": 2015,
  "anio_fin": 2020,
  "vigente_actualidad": false,
  "filiacion_estandar": "{filiacion_actual}",
  "confianza": "alta",
  "observacion": "Trayectoria documentada entre 2015 y 2020 según OpenAlex y publicaciones académicas."
}}"""


def armar_prompt_filiacion_por_anio(autor: str, anio_articulo: str, titulo_articulo: str, contexto_academico: str) -> str:
    """Prompt para identificar y redactar la filiación de un autor PARA EL AÑO ESPECÍFICO del artículo."""
    return f"""Sos un bibliotecario catalogador experto del Repositorio Digital Universitario (RDU - Universidad Nacional de Córdoba).
Tu objetivo es identificar la afiliación institucional precisa de un autor ACADÉMICO PARA EL AÑO DE PUBLICACIÓN de un artículo.

DATOS DEL ARTÍCULO:
- Autor: {autor}
- Año del artículo: {anio_articulo}
- Título: {titulo_articulo}

HISTORIAL ACADÉMICO Y RESULTADOS DE BÚSQUEDA:
{contexto_academico if contexto_academico else "No se encontraron registros adicionales."}

REGLAS DE FORMATO RDU:
1. La filiación DEBE comenzar con 'Fil: ' seguido de 'Apellido, Nombre. '
2. Jerarquía institucional: Unidad menor primero, luego institución mayor, separadas por punto y espacio.
   Ejemplo: 'Fil: Pérez, Juan. Facultad de Psicología. Universidad Nacional de Córdoba; Argentina.'
   O con doble dependencia: 'Fil: Pérez, Juan. Instituto de Investigaciones Psicológicas. Consejo Nacional de Investigaciones Científicas y Técnicas. Universidad Nacional de Córdoba; Argentina.'
3. País: Antecedido por punto y coma '; Argentina.'
4. 'anio_inicio': Año de inicio de esa vinculación si se conoce, o null.
5. 'anio_fin': Año de fin si cesó, o null si continuó.
6. 'confianza': 'alta', 'media' o 'baja'.
7. 'observacion': Breve explicación.

Devuelve EXCLUSIVAMENTE un objeto JSON válido:
{{
  "filiacion_estandar": "Fil: {autor}. Facultad de Filosofía y Humanidades. Universidad Nacional de Córdoba; Argentina.",
  "anio_inicio": 2010,
  "anio_fin": null,
  "confianza": "alta",
  "observacion": "Investigador activo en UNC durante el año {anio_articulo}."
}}"""


def normalizar_texto_institucion(t: str) -> str:
    import unicodedata
    if not t:
        return ""
    sin_ac = "".join(c for c in unicodedata.normalize("NFD", str(t)) if unicodedata.category(c) != "Mn")
    return " ".join(sin_ac.lower().replace("-", " ").replace(".", " ").replace(";", " ").split())


def deducir_fechas_openalex(filiacion_rdu: str, afiliaciones_alex: list) -> dict | None:
    """Compara la filiación de la fila de Google Sheets con las afiliaciones
    documentadas en OpenAlex para extraer años con 100% de rigor fáctico
    sin consumir cuota de la API de Gemini."""
    if not filiacion_rdu or not afiliaciones_alex:
        return None

    fil_norm = normalizar_texto_institucion(filiacion_rdu)
    mejor_match = None
    max_score = 0

    for aff in afiliaciones_alex:
        inst_nombre = aff.get("institucion", "")
        if not inst_nombre:
            continue
        inst_norm = normalizar_texto_institucion(inst_nombre)
        anios = aff.get("anios", [])
        if not anios:
            continue

        palabras_clave = [p for p in inst_norm.split() if len(p) > 3 and p not in (
            "universidad", "nacional", "instituto", "facultad", "investigacion",
            "investigaciones", "cientificas", "tecnicas", "mercedes", "martin"
        )]

        es_match = False
        if inst_norm in fil_norm:
            es_match = True
        elif any(p in fil_norm for p in palabras_clave) and (
            ("conicet" in fil_norm and "conicet" in inst_norm) or
            ("cordoba" in fil_norm and "cordoba" in inst_norm) or
            ("buenos aires" in fil_norm and "buenos aires" in inst_norm) or
            ("ferreyra" in fil_norm and "ferreyra" in inst_norm)
        ):
            es_match = True
        elif "mercedes y martin ferreyra" in fil_norm and "ferreyra" in inst_norm:
            es_match = True

        if es_match:
            ini = min(anios)
            fin_max = max(anios)
            vigente = fin_max >= 2023
            fin = None if vigente else fin_max

            score = len(inst_norm)
            if score > max_score:
                max_score = score
                mejor_match = {
                    "anio_inicio": ini,
                    "anio_fin": fin,
                    "vigente_actualidad": vigente,
                    "filiacion_estandar": filiacion_rdu,
                    "confianza": "alta",
                    "observacion": f"Publicaciones documentadas en '{inst_nombre}' entre {ini} y {fin_max} según OpenAlex."
                }

    return mejor_match


# ------------------------------------------------------------------- modo filiaciones
def procesar_hoja_filiaciones(args, api_key: str):
    """Recorre la pestaña Filiaciones de Google Sheets y completa Año_Inicio y Año_Fin."""
    print("\n" + "=" * 70)
    print(f"[MODO FILIACIONES] Enriqueciendo fechas institucionales en hoja 'Filiaciones'...")
    print(f"  Modelo Gemini: {args.gemini_model} | Límite: {args.limite or 'Sin límite'} | Dry-run: {args.dry_run}")
    print("=" * 70)

    filas, ws = sheets_client.obtener_filas_filiaciones(
        sheet_id=args.sheet_id,
        solo_vacios=args.solo_vacios,
        limite=args.limite
    )

    if not filas:
        print("  [OK] No se encontraron filas pendientes de enriquecer en 'Filiaciones'.")
        return []

    print(f"  [INFO] Total de filas a procesar: {len(filas)}")

    reporte = []
    actualizados = 0

    for idx, item in enumerate(filas, start=1):
        row_idx = item["row_index"]
        autor = item["autor"]
        filiacion = item["filiacion"]
        ini_prev = item["anio_inicio"]
        fin_prev = item["anio_fin"]

        print(f"\n[{idx}/{len(filas)}] Fila {row_idx}: {autor}")
        print(f"  Filiación actual: {filiacion[:70]}...")
        if ini_prev or fin_prev:
            print(f"  Años registrados previos: {ini_prev or '?'} - {fin_prev or '?'}")

        # 1. Búsqueda de perfil estructurado en OpenAlex
        alex = web_filiacion.buscar_trayectoria_openalex(autor)
        ded = deducir_fechas_openalex(filiacion, alex.get("afiliaciones", []))

        ini_nuevo = None
        fin_nuevo = None
        vigente = False
        confianza = "baja"
        obs = ""
        consumio_gemini = False

        if ded:
            print(f"  [OPENALEX MATCH] Institución y fechas extraídas de registros científicos OpenAlex.")
            ini_nuevo = ded["anio_inicio"]
            fin_nuevo = ded["anio_fin"]
            vigente = ded["vigente_actualidad"]
            confianza = "alta"
            obs = ded["observacion"]
        else:
            # 2. Si OpenAlex no tiene match exacto de la institución, invocar Gemini Flash con contexto web
            print("  Sin coincidencia directa en OpenAlex. Consultando Gemini Flash y búsqueda web...")
            ctx = web_filiacion.buscar_filiacion_temporal_web(autor, filiacion)
            prompt = armar_prompt_enriquecer_filiacion(autor, filiacion, ctx)
            res = llamar_gemini(prompt, api_key=api_key, modelo=args.gemini_model)
            consumio_gemini = True
            if res:
                ini_nuevo = res.get("anio_inicio")
                fin_nuevo = res.get("anio_fin")
                vigente = res.get("vigente_actualidad")
                confianza = res.get("confianza", "media")
                obs = res.get("observacion", "")
            else:
                obs = "Sin registros coincidentes en OpenAlex y cuota de Gemini API no disponible."

        print(f"  Resultado -> Inicio: {ini_nuevo or '-'} | Fin: {fin_nuevo or ('(vigente)' if vigente else '-')} | Confianza: {confianza}")
        if obs:
            print(f"  Nota: {obs[:85]}...")

        # 3. Aplicar en Google Sheets si no es dry-run
        if not args.dry_run:
            if ini_nuevo is not None or fin_nuevo is not None or vigente:
                val_ini = str(ini_nuevo) if ini_nuevo else ""
                val_fin = str(fin_nuevo) if fin_nuevo else ""
                try:
                    sheets_client.actualizar_fechas_filiacion(
                        ws,
                        row_index=row_idx,
                        anio_inicio=val_ini,
                        anio_fin=val_fin
                    )
                    print(f"  [GUARDADO] Google Sheets actualizado fila {row_idx}: [{val_ini}] - [{val_fin}]")
                    actualizados += 1
                except Exception as e:
                    print(f"  [ERROR] No se pudo actualizar fila {row_idx}: {e}", file=sys.stderr)
        else:
            print(f"  [SIMULACIÓN] Se escribiría en fila {row_idx}: Inicio={ini_nuevo}, Fin={fin_nuevo}")

        reporte.append({
            "fila": row_idx,
            "autor": autor,
            "filiacion": filiacion,
            "inicio_anterior": ini_prev,
            "fin_anterior": fin_prev,
            "inicio_propuesto": ini_nuevo or "",
            "fin_propuesto": fin_nuevo or "",
            "vigente": "SI" if vigente else "NO",
            "confianza": confianza,
            "observacion": obs,
        })

        # Control de cuota (solo necesario si se llamó a la API de Gemini)
        if consumio_gemini and idx < len(filas):
            time.sleep(PAUSA_ENTRE_LLAMADAS_SEG)

    print("\n" + "=" * 70)
    print(f"[FIN MODO FILIACIONES] Procesadas: {len(filas)} | Modificadas en Sheet: {actualizados}")
    print("=" * 70)
    return reporte


# ------------------------------------------------------------------- modo cola
def procesar_hoja_cola(args, api_key: str):
    """Lee autores faltantes de la columna D de la hoja Cola, busca su filiación
    para el año del artículo y los agrega a la hoja Filiaciones."""
    print("\n" + "=" * 70)
    print(f"[MODO COLA] Investigando autores faltantes (Columna D) para el año del artículo...")
    print(f"  Modelo Gemini: {args.gemini_model} | Límite: {args.limite or 'Sin límite'} | Dry-run: {args.dry_run}")
    print("=" * 70)

    try:
        ws_cola = sheets_client._hoja(sheets_client.HOJA_COLA, sheet_id=args.sheet_id)
        filas_cola = ws_cola.get_all_values()
    except Exception as e:
        print(f"  [ERROR] No se pudo leer la hoja 'Cola': {e}", file=sys.stderr)
        return []

    if not filas_cola or len(filas_cola) <= 1:
        print("  [OK] Hoja 'Cola' vacía o sin registros.")
        return []

    # Localizar índices de columnas
    headers = filas_cola[0]
    idx_link = 0
    idx_col_d = 3  # Por defecto columna D
    for i, h in enumerate(headers):
        clave_h = sheets_client._clave_encabezado(h)
        if "autor" in clave_h and "sin" in clave_h:
            idx_col_d = i
        elif "link" in clave_h:
            idx_link = i

    dicc_filiaciones = sheets_client.leer_diccionario_filiaciones(sheet_id=args.sheet_id)
    reporte = []
    agregados = 0

    candidatos_a_investigar = []
    for row_idx, r in enumerate(filas_cola[1:], start=2):
        link = r[idx_link].strip() if len(r) > idx_link else ""
        faltantes_str = r[idx_col_d].strip() if len(r) > idx_col_d else ""
        if not faltantes_str or "sin autor" in faltantes_str.lower():
            continue
        # Limpiar posibles prefijos como "(PDF sin autor)" o "Falta filiación:"
        faltantes_limpio = re.sub(r"^\(.*?sin autor.*?\)\s*", "", faltantes_str, flags=re.I).strip()
        faltantes_limpio = re.sub(r"^falta filiaci[oó]n:\s*", "", faltantes_limpio, flags=re.I).strip()
        # Autores separados por punto y coma
        autores = [a.strip() for a in faltantes_limpio.split(";") if a.strip() and "sin autor" not in a.lower()]
        for a in autores:
            candidatos_a_investigar.append({
                "row_cola": row_idx,
                "link": link,
                "autor": a,
            })
        if args.limite and len(candidatos_a_investigar) >= args.limite:
            candidatos_a_investigar = candidatos_a_investigar[:args.limite]
            break

    if not candidatos_a_investigar:
        print("  [OK] No hay autores sin filiación pendientes en la columna D de la 'Cola'.")
        return []

    print(f"  [INFO] Total de autores faltantes a investigar: {len(candidatos_a_investigar)}")

    for idx, c in enumerate(candidatos_a_investigar, start=1):
        autor = c["autor"]
        link = c["link"]
        print(f"\n[{idx}/{len(candidatos_a_investigar)}] Investigando autor: {autor} (Ref: {link})")

        # Intentar extraer año del link o artículo si está disponible
        m_anio = re.search(r"\b(19\d\d|20\d\d)\b", link)
        anio_articulo = m_anio.group(1) if m_anio else ""

        print("  Consultando historial en OpenAlex y web...")
        ctx = web_filiacion.buscar_filiacion_temporal_web(autor, anio=anio_articulo)

        prompt = armar_prompt_filiacion_por_anio(autor, anio_articulo, "", ctx)
        res = llamar_gemini(prompt, api_key=api_key, modelo=args.gemini_model)

        fil_estandar = res.get("filiacion_estandar", "").strip()
        ini = res.get("anio_inicio")
        fin = res.get("anio_fin")
        confianza = res.get("confianza", "baja")
        obs = res.get("observacion", "")

        print(f"  Filiación propuesta: {fil_estandar}")
        print(f"  Años: {ini or '?'} - {fin or '?'} | Confianza: {confianza}")

        if fil_estandar and fil_estandar.startswith("Fil:"):
            if not args.dry_run:
                exito = sheets_client.agregar_filiacion(
                    autor=autor,
                    filiacion=fil_estandar,
                    dicc_actual=dicc_filiaciones,
                    anio_inicio=ini,
                    anio_fin=fin
                )
                if exito:
                    agregados += 1
            else:
                print(f"  [SIMULACIÓN] Se agregaría a 'Filiaciones': {autor} -> {fil_estandar}")

        reporte.append({
            "autor": autor,
            "link_cola": link,
            "anio_articulo": anio_articulo,
            "filiacion_generada": fil_estandar,
            "inicio": ini or "",
            "fin": fin or "",
            "confianza": confianza,
            "observacion": obs,
        })

        if idx < len(candidatos_a_investigar):
            time.sleep(PAUSA_ENTRE_LLAMADAS_SEG)

    print("\n" + "=" * 70)
    print(f"[FIN MODO COLA] Investigados: {len(candidatos_a_investigar)} | Agregados a Filiaciones: {agregados}")
    print("=" * 70)
    return reporte


# ------------------------------------------------------------------- guardar reporte
def guardar_reporte(reporte: list[dict], modo: str):
    """Guarda un archivo CSV con el reporte de la ejecución."""
    if not reporte:
        return
    os.makedirs(os.path.join(DIR_ACTUAL, "..", "reportes"), exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path_csv = os.path.join(DIR_ACTUAL, "..", "reportes", f"enriquecimiento_{modo}_{ts}.csv")
    try:
        keys = list(reporte[0].keys())
        with open(path_csv, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(reporte)
        print(f"\n[REPORTE] Guardado exitosamente en: {path_csv}")
    except Exception as e:
        print(f"[WARN] No se pudo guardar reporte CSV: {e}", file=sys.stderr)


# ------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description="Enriquecimiento temporal de filiaciones con Gemini Flash y OpenAlex.")
    parser.add_argument("--modo", choices=["filiaciones", "cola"], default="filiaciones",
                        help="Modo de ejecución: 'filiaciones' (completa años en tab Filiaciones) o 'cola' (investiga autores faltantes en tab Cola).")
    parser.add_argument("--limite", type=int, default=10,
                        help="Cantidad máxima de entradas a procesar (0 = procesar todas las pendientes).")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Modo simulación: no escribe en Google Sheets.")
    parser.add_argument("--solo-vacios", action="store_true", default=True,
                        help="En modo 'filiaciones', procesa únicamente filas sin años asignados.")
    parser.add_argument("--reprocesar-todo", action="store_true", default=False,
                        help="Fuerza reprocesar todas las filas incluso si ya tienen años cargados.")
    parser.add_argument("--gemini-key", type=str, default="",
                        help="API Key de Google Gemini (si se omite, se lee de GEMINI_API_KEY).")
    parser.add_argument("--gemini-model", type=str, default=MODELO_GEMINI_DEFAULT,
                        help=f"Modelo de Gemini a utilizar (por defecto: {MODELO_GEMINI_DEFAULT}).")
    parser.add_argument("--sheet-id", type=str, default="",
                        help="ID del Google Sheet a utilizar (por defecto usa GOOGLE_SHEET_ID).")

    args = parser.parse_args()

    api_key = args.gemini_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[ERROR] Falta GEMINI_API_KEY. Indícala por --gemini-key o variable de entorno.", file=sys.stderr)
        sys.exit(1)

    if args.reprocesar_todo:
        args.solo_vacios = False

    if args.modo == "filiaciones":
        rep = procesar_hoja_filiaciones(args, api_key=api_key)
        guardar_reporte(rep, "filiaciones")
    elif args.modo == "cola":
        rep = procesar_hoja_cola(args, api_key=api_key)
        guardar_reporte(rep, "cola")


if __name__ == "__main__":
    main()
