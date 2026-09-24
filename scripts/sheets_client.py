"""
Cliente de Google Sheets.
- Lee la cola de items pendientes desde la hoja "Cola" (columnas: Link, Estado).
- Escribe el registro final en la hoja principal (Título, Link, Tipo, Estado, Fecha).
- Actualiza el Estado de cada fila de la cola a medida que se procesa.

CAMBIOS IMPORTANTES DE ESTA VERSIÓN
-----------------------------------
1. CLIENTE CACHEADO. Antes cada operación llamaba a _client() y volvía a
   autorizarse + abrir el spreadsheet + buscar la hoja: 3-4 llamadas de API por
   escritura. Con 5 ítems y varias filiaciones nuevas se llegaba fácil a la
   cuota de 60 escrituras/minuto y saltaba un 429 que mataba la corrida.
2. REINTENTOS ante 429 / errores 5xx, con backoff.
3. ESTADOS RETOMABLES. Antes solo se tomaban las filas con Estado VACÍO, así
   que toda fila que quedaba en "procesando" porque el job se murió (timeout de
   GitHub Actions, crash, cancelación) NUNCA se volvía a procesar: quedaba
   invisible para siempre. Ahora "procesando" y "ERROR" se reintentan.
4. Índice de la columna Estado cacheado (antes se leía la fila 1 en cada
   marcado, sumando llamadas de API al pedo).
"""
import os
import time
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1gh2n-gbxiSzrCQZG4-Pkq52eMmbLzJiGnb_6vP4zEQU")
HOJA_COLA = "Cola"
HOJA_COLA_FILIACIONES = "ColaFiliaciones"  # cola paralela, solo lectura de filiaciones
HOJA_COLA_FIL_ESTANDAR = "ColaFilEstandar"  # cola para aplicar filiación fija
HOJA_REGISTRO = "Hoja 1"  # ajustar si tu hoja principal tiene otro nombre
HOJA_FILIACIONES = "Filiaciones"  # diccionario Autor -> [Filiacion, ...]

# Límite de items a procesar por corrida, para controlar gasto de tokens.
MAX_ITEMS_POR_CORRIDA = int(os.environ.get("MAX_ITEMS_POR_CORRIDA", "5"))

# Estados de la cola que se vuelven a tomar en la próxima corrida.
# "" (vacío)     -> nunca se procesó
# "procesando"   -> quedó colgado (el job se murió a mitad de camino)
# "ERROR"        -> falló, se reintenta
# Para NO reintentar los errores, sacá "error" de este conjunto.
ESTADOS_RETOMABLES = {"", "procesando", "error"}

REINTENTOS_API = int(os.environ.get("REINTENTOS_SHEETS", "4"))


# --------------------------------------------------------------- infraestructura
_cache = {"client": None, "spreadsheet": {}, "hojas": {}, "col_estado": {}}


def _client():
    """Cliente gspread CACHEADO (antes se re-autorizaba en cada operación)."""
    if _cache["client"] is None:
        import json
        creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _cache["client"] = gspread.authorize(creds)
    return _cache["client"]


def _spreadsheet(sheet_id: str | None = None):
    """Spreadsheet CACHEADO."""
    sid = sheet_id or os.environ.get("GOOGLE_SHEET_ID") or SHEET_ID
    if sid not in _cache["spreadsheet"]:
        _cache["spreadsheet"][sid] = _client().open_by_key(sid)
    return _cache["spreadsheet"][sid]


def _hoja(nombre: str, crear_con=None, sheet_id: str | None = None):
    """Worksheet CACHEADO. Si no existe y se pasa `crear_con` (lista de
    encabezados), la crea."""
    sid = sheet_id or os.environ.get("GOOGLE_SHEET_ID") or SHEET_ID
    cache_key = (sid, nombre)
    if cache_key in _cache["hojas"]:
        return _cache["hojas"][cache_key]
    sh = _spreadsheet(sid)
    try:
        ws = sh.worksheet(nombre)
    except Exception:
        if crear_con is None:
            raise
        ws = sh.add_worksheet(title=nombre, rows=1000, cols=max(2, len(crear_con)))
        ws.update(f"A1:{chr(64 + len(crear_con))}1", [crear_con])
        print(f"  [INFO] Se creó la hoja '{nombre}' (no existía).")
    _cache["hojas"][cache_key] = ws
    return ws


def _con_reintentos(descripcion: str, fn, *args, **kwargs):
    """Ejecuta una operación de Sheets reintentando ante 429 (cuota) y 5xx.

    La cuota de la API es de 60 lecturas + 60 escrituras por minuto: con varios
    ítems seguidos se toca fácil, y antes un 429 tiraba una excepción que
    (dependiendo de dónde saltara) podía matar la corrida entera.
    """
    ultimo = None
    for intento in range(1, REINTENTOS_API + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            try:
                codigo = str(e.response.status_code)
            except Exception:
                codigo = str(e)
            ultimo = e
            recuperable = ("429" in codigo) or codigo.startswith("5")
            if not recuperable or intento == REINTENTOS_API:
                raise
            espera = min(2 ** intento * 5, 60)  # 10s, 20s, 40s...
            print(f"  [WARN] Sheets devolvió {codigo} en '{descripcion}' "
                  f"(intento {intento}/{REINTENTOS_API}). Reintentando en {espera}s...")
            time.sleep(espera)
        except Exception as e:
            ultimo = e
            if intento == REINTENTOS_API:
                raise
            espera = min(2 ** intento * 2, 20)
            print(f"  [WARN] Error en '{descripcion}' (intento {intento}/{REINTENTOS_API}): {e}. "
                  f"Reintentando en {espera}s...")
            time.sleep(espera)
    if ultimo:
        raise ultimo


# --------------------------------------------------------------- normalización
def _sin_acentos(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(t))
                   if unicodedata.category(c) != "Mn")


def _clave_encabezado(nombre: str) -> str:
    """Normaliza un nombre de encabezado para comparar: sin acentos, en
    minusculas y sin espacios extra. Asi 'Filiación', 'Filiacion' y
    'FILIACION ' apuntan todos a lo mismo."""
    return " ".join(_sin_acentos(nombre or "").strip().lower().split())


def _col(fila: dict, nombre: str):
    """Busca un valor en una fila (dict) por nombre de columna, ignorando
    mayúsculas/minúsculas, ACENTOS y espacios extra en el encabezado."""
    objetivo = _clave_encabezado(nombre)
    for k, v in fila.items():
        if k and _clave_encabezado(k) == objetivo:
            return v
    return None


def _leer_tabla(ws):
    """Lee la hoja como lista de dicts SIN usar get_all_records().

    get_all_records() explota si la fila de encabezados tiene nombres repetidos
    o celdas vacías (muy común cuando se pega un CSV y quedan columnas sueltas).
    Acá leemos los valores crudos y armamos los dicts a mano:
      - las columnas sin encabezado se ignoran
      - si un encabezado se repite, gana la PRIMERA aparición
    """
    valores = _con_reintentos(f"leer '{ws.title}'", ws.get_all_values)
    if not valores:
        return []
    encabezados = valores[0]

    columnas = {}
    for idx, h in enumerate(encabezados):
        nombre = (h or "").strip()
        if not nombre:
            continue
        clave = _clave_encabezado(nombre)
        if clave in columnas:
            continue
        columnas[clave] = (idx, nombre)

    filas = []
    for cruda in valores[1:]:
        fila = {}
        for _, (idx, nombre) in columnas.items():
            fila[nombre] = cruda[idx] if idx < len(cruda) else ""
        filas.append(fila)
    return filas


def _indice_columna(ws, nombre: str, por_defecto: int) -> int:
    """Número de columna (1-based) de un encabezado, buscándolo por nombre.
    CACHEADO por hoja+columna para no gastar una llamada de API por escritura."""
    cache_key = (ws.title, _clave_encabezado(nombre))
    if cache_key in _cache["col_estado"]:
        return _cache["col_estado"][cache_key]

    try:
        encabezados = _con_reintentos(f"encabezados de '{ws.title}'", ws.row_values, 1)
    except Exception:
        return por_defecto

    objetivo = _clave_encabezado(nombre)
    col = por_defecto
    encontrada = False
    for i, h in enumerate(encabezados, start=1):
        if _clave_encabezado(h) == objetivo:
            col, encontrada = i, True
            break
    if not encontrada:
        print(f"  [WARN] No se encontró la columna '{nombre}' en la hoja "
              f"'{ws.title}'. Se usa la columna {por_defecto} por defecto.")
    _cache["col_estado"][cache_key] = col
    return col


# ---------------------------------------------------------------------- cola
def _recolectar_pendientes(ws, tope: int):
    """Filas retomables de una hoja de cola (Link, Estado)."""
    filas = _leer_tabla(ws)
    pendientes = []
    retomadas = 0
    for i, fila in enumerate(filas, start=2):  # fila 1 = encabezado
        link = _col(fila, "Link")
        estado = str(_col(fila, "Estado") or "").strip()
        if not link:
            continue
        if _clave_encabezado(estado) not in ESTADOS_RETOMABLES:
            continue
        if estado:  # venía de una corrida que quedó colgada o falló
            retomadas += 1
        pendientes.append({"row_index": i, "link": str(link).strip(),
                           "estado_previo": estado})
        if len(pendientes) >= tope:
            break
    if retomadas:
        print(f"  [INFO] Se retoman {retomadas} fila(s) que habían quedado en "
              f"'procesando'/'ERROR' de corridas anteriores.")
    return pendientes


def obtener_pendientes():
    """Filas de la cola a procesar. Incluye las que quedaron colgadas."""
    ws = _hoja(HOJA_COLA)
    return _recolectar_pendientes(ws, MAX_ITEMS_POR_CORRIDA), ws


def marcar_estado_cola(ws, row_index, estado):
    """Actualiza la columna Estado de la hoja Cola para una fila puntual.

    Busca la columna 'Estado' POR NOMBRE en vez de asumir que es la B: si la
    hoja tiene otro orden (ej. Link | Titulo | Estado | ...), escribir en la B
    pisaria el titulo."""
    col = _indice_columna(ws, "Estado", 2)
    _con_reintentos(f"marcar estado fila {row_index}",
                    ws.update_cell, row_index, col, estado)


def obtener_pendientes_revision_cola(
    ws=None,
    tope: int | None = None,
    reprocesar_todo: bool = False,
    sheet_id: str | None = None,
):
    """Obtiene filas de la hoja Cola para revisión de autores y títulos.
    Por defecto, toma aquellas filas con Link no vacío cuya columna C (Modificaciones)
    esté vacía o empiece con ERROR.
    Si reprocesar_todo es True, toma todas las filas con Link no vacío.
    """
    if ws is None:
        ws = _hoja(HOJA_COLA, sheet_id=sheet_id)

    valores = _con_reintentos(f"leer '{ws.title}'", ws.get_all_values)
    if not valores or len(valores) < 2:
        return [], ws

    encabezados = valores[0]
    col_link = 0
    col_estado = 1
    col_mod = 2

    for idx, h in enumerate(encabezados):
        clave = _clave_encabezado(h)
        if clave in ("link", "url", "workflowitem"):
            col_link = idx
        elif clave in ("estado", "status"):
            col_estado = idx
        elif clave in ("modificaciones", "modificacion", "detalle", "revision"):
            col_mod = idx

    pendientes = []
    for row_idx, fila in enumerate(valores[1:], start=2):
        link = fila[col_link].strip() if col_link < len(fila) else ""
        if not link:
            continue
        if link.lower() in ("link", "url", "workflowitem"):
            continue

        estado = fila[col_estado].strip() if col_estado < len(fila) else ""
        modificaciones = fila[col_mod].strip() if col_mod < len(fila) else ""

        if not reprocesar_todo:
            if modificaciones and not modificaciones.upper().startswith("ERROR"):
                continue

        pendientes.append({
            "row_index": row_idx,
            "link": link,
            "estado": estado,
            "modificaciones": modificaciones,
        })
        if tope and tope > 0 and len(pendientes) >= tope:
            break

    return pendientes, ws


def marcar_modificaciones_cola(ws, row_index: int, modificaciones: str, col_c: int = 3):
    """Actualiza la columna C (Modificaciones) de la hoja Cola para una fila puntual.
    Si la fila 1 en esa columna no tiene encabezado, le coloca 'Modificaciones'.
    """
    col = _indice_columna(ws, "Modificaciones", col_c)
    try:
        val_h = ws.cell(1, col).value
        if not val_h:
            ws.update_cell(1, col, "Modificaciones")
            cache_key = (ws.title, _clave_encabezado("Modificaciones"))
            _cache["col_estado"][cache_key] = col
    except Exception:
        pass

    _con_reintentos(
        f"marcar modificaciones fila {row_index}",
        ws.update_cell,
        row_index,
        col,
        modificaciones,
    )


def obtener_pendientes_filiaciones(max_items: int | None = None):
    """Igual que obtener_pendientes() pero para la cola ColaFiliaciones."""
    ws = _hoja(HOJA_COLA_FILIACIONES, crear_con=["Link", "Estado"])
    return _recolectar_pendientes(ws, max_items or MAX_ITEMS_POR_CORRIDA), ws


def obtener_pendientes_fil_estandar(max_items: int | None = None):
    """Igual que obtener_pendientes() pero para la cola ColaFilEstandar."""
    ws = _hoja(HOJA_COLA_FIL_ESTANDAR, crear_con=["Link", "Estado"])
    return _recolectar_pendientes(ws, max_items or MAX_ITEMS_POR_CORRIDA), ws


# ------------------------------------------------------------------- registro
def registrar_item(titulo, link, tipo, estado, fecha=None):
    """Agrega una fila al registro principal (Título, Link, Tipo, Estado, Fecha)."""
    ws = _hoja(HOJA_REGISTRO)
    fecha = fecha or date.today().isoformat()
    _con_reintentos("registrar item", ws.append_row,
                    [titulo, link, tipo, estado, fecha],
                    value_input_option="USER_ENTERED")


# --------------------------------------------------------------- filiaciones
def _normalizar_autor(autor: str) -> str:
    """Clave normalizada para comparar autores (sin acentos/espacios extra)."""
    return " ".join(_sin_acentos((autor or "").strip().lower()).split())


def leer_diccionario_filiaciones() -> dict:
    """Devuelve {clave_autor_normalizada: [filiacion1, filiacion2, ...]} desde la
    hoja Filiaciones. Un autor puede tener MÁS DE UNA filiación. Si la hoja no
    existe, devuelve diccionario vacío (no es un error)."""
    try:
        ws = _hoja(HOJA_FILIACIONES)
    except Exception:
        print("  [INFO] No existe la hoja 'Filiaciones' todavía; se usará vacía.")
        return {}

    filas = _leer_tabla(ws)  # encabezados esperados: Autor, Filiacion
    dicc = {}
    nombres_originales = set()  # texto EXACTO de la columna Autor (fuente de verdad)
    for fila in filas:
        autor = str(_col(fila, "Autor") or "").strip()
        fil = str(_col(fila, "Filiacion") or "").strip()
        if autor:
            nombres_originales.add(autor)
        if autor and fil:
            clave = _normalizar_autor(autor)
            dicc.setdefault(clave, [])
            if fil not in dicc[clave]:  # evitar duplicado EXACTO de texto
                dicc[clave].append(fil)

    # indice para completar nombres abreviados usando la forma canonica exacta.
    # Se guarda bajo '__nombres__' para que formato.buscar_en_diccionario lo use.
    # (No colisiona con claves de autor, que nunca empiezan con '__'.)
    try:
        import formato as _fmt
        dicc["__nombres__"] = _fmt.indice_nombres_diccionario(nombres_originales)
    except Exception as e:
        print(f"  [WARN] No se pudo indexar nombres del diccionario: {e}")

    total_fil = sum(len(v) for v in dicc.values() if isinstance(v, list))
    n_autores = sum(1 for k in dicc if not k.startswith("__"))
    print(f"  [INFO] Diccionario de filiaciones cargado: {n_autores} autores, "
          f"{total_fil} filiaciones en total.")
    return dicc


def agregar_filiacion(autor: str, filiacion: str, dicc_actual: dict | None = None):
    """Agrega una filiación al diccionario. Un mismo autor puede tener varias
    filiaciones distintas (todas se guardan); solo se evita el duplicado EXACTO
    (mismo autor + mismo texto de filiación ya cargado)."""
    clave = _normalizar_autor(autor)
    existentes = dicc_actual if dicc_actual is not None else leer_diccionario_filiaciones()
    if filiacion in existentes.get(clave, []):
        return False  # ya estaba cargada tal cual, no duplicar

    ws = _hoja(HOJA_FILIACIONES, crear_con=["Autor", "Filiacion"])
    _con_reintentos("agregar filiacion", ws.append_row,
                    [autor, filiacion], value_input_option="USER_ENTERED")
    print(f"  [INFO] Filiación agregada al diccionario: {autor} -> {filiacion[:60]}...")

    if dicc_actual is not None:
        dicc_actual.setdefault(clave, []).append(filiacion)  # mantener cache al día
        # mantener también el índice de nombres, para que un autor nuevo pueda
        # servir para completar abreviaturas en el mismo run
        try:
            import formato as _fmt
            idx = dicc_actual.setdefault("__nombres__", {})
            ap_norm, tokens = _fmt._desarmar(autor.strip())
            if ap_norm and tokens:
                if autor.strip() not in idx.setdefault(ap_norm, []):
                    idx[ap_norm].append(autor.strip())
        except Exception:
            pass
    return True
