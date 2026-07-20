"""
Cliente de Google Sheets.
- Lee la cola de items pendientes desde la hoja "Cola" (columnas: Link, Estado).
- Escribe el registro final en la hoja principal (Título, Link, Tipo, Estado, Fecha).
- Actualiza el Estado de cada fila de la cola a medida que se procesa.
"""
import os
from datetime import date
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
HOJA_COLA = "Cola"
HOJA_COLA_FILIACIONES = "ColaFiliaciones"  # cola paralela, solo lectura de filiaciones
HOJA_COLA_FIL_ESTANDAR = "ColaFilEstandar"  # cola para aplicar filiación fija
HOJA_REGISTRO = "Hoja 1"  # ajustar si tu hoja principal tiene otro nombre
HOJA_FILIACIONES = "Filiaciones"  # diccionario Autor -> [Filiacion, ...]

# Límite de items a procesar por corrida, para controlar gasto de tokens.
MAX_ITEMS_POR_CORRIDA = int(os.environ.get("MAX_ITEMS_POR_CORRIDA", "5"))


def _client():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    import json
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _col(fila: dict, nombre: str):
    """Busca un valor en una fila (dict) por nombre de columna, ignorando
    mayúsculas/minúsculas y espacios extra en el encabezado (ej. 'LINK',
    'Link', 'link ' apuntan todos a lo mismo)."""
    objetivo = nombre.strip().lower()
    for k, v in fila.items():
        if k and k.strip().lower() == objetivo:
            return v
    return None


def obtener_pendientes():
    """Devuelve una lista de dicts {row_index, link} para items con Estado vacío,
    limitada a MAX_ITEMS_POR_CORRIDA."""
    gc = _client()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(HOJA_COLA)
    filas = ws.get_all_records()  # asume encabezados "Link" y "Estado" en fila 1

    pendientes = []
    for i, fila in enumerate(filas, start=2):  # fila 1 = encabezado
        link = _col(fila, "Link")
        estado = _col(fila, "Estado")
        if not estado and link:
            pendientes.append({"row_index": i, "link": str(link).strip()})
        if len(pendientes) >= MAX_ITEMS_POR_CORRIDA:
            break

    return pendientes, ws


def marcar_estado_cola(ws, row_index, estado):
    """Actualiza la columna Estado de la hoja Cola para una fila puntual."""
    # Asume que Estado es la columna B. Ajustar si el orden de columnas difiere.
    ws.update_cell(row_index, 2, estado)


def obtener_pendientes_filiaciones(max_items: int | None = None):
    """Igual que obtener_pendientes() pero para la cola paralela ColaFiliaciones
    (columnas: Link, Estado). Si la hoja no existe, la crea con encabezados."""
    gc = _client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(HOJA_COLA_FILIACIONES)
    except Exception:
        ws = sh.add_worksheet(title=HOJA_COLA_FILIACIONES, rows=1000, cols=2)
        ws.update("A1:B1", [["Link", "Estado"]])
        print("  [INFO] Se creó la hoja 'ColaFiliaciones' (estaba vacía).")

    filas = ws.get_all_records()
    tope = max_items or MAX_ITEMS_POR_CORRIDA
    pendientes = []
    for i, fila in enumerate(filas, start=2):
        link = _col(fila, "Link")
        estado = _col(fila, "Estado")
        if not estado and link:
            pendientes.append({"row_index": i, "link": str(link).strip()})
        if len(pendientes) >= tope:
            break
    return pendientes, ws


def obtener_pendientes_fil_estandar(max_items: int | None = None):
    """Igual que obtener_pendientes() pero para la cola ColaFilEstandar
    (columnas: Link, Estado). Si la hoja no existe, la crea con encabezados."""
    gc = _client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(HOJA_COLA_FIL_ESTANDAR)
    except Exception:
        ws = sh.add_worksheet(title=HOJA_COLA_FIL_ESTANDAR, rows=1000, cols=2)
        ws.update("A1:B1", [["Link", "Estado"]])
        print("  [INFO] Se creó la hoja 'ColaFilEstandar' (estaba vacía).")

    filas = ws.get_all_records()
    tope = max_items or MAX_ITEMS_POR_CORRIDA
    pendientes = []
    for i, fila in enumerate(filas, start=2):
        link = _col(fila, "Link")
        estado = _col(fila, "Estado")
        if not estado and link:
            pendientes.append({"row_index": i, "link": str(link).strip()})
        if len(pendientes) >= tope:
            break
    return pendientes, ws


def registrar_item(titulo, link, tipo, estado, fecha=None):
    """Agrega una fila al registro principal (Título, Link, Tipo, Estado, Fecha)."""
    gc = _client()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(HOJA_REGISTRO)
    fecha = fecha or date.today().isoformat()
    ws.append_row([titulo, link, tipo, estado, fecha], value_input_option="USER_ENTERED")


def _normalizar_autor(autor: str) -> str:
    """Clave normalizada para comparar autores (sin acentos/espacios extra)."""
    import unicodedata
    a = autor.strip().lower()
    a = "".join(c for c in unicodedata.normalize("NFD", a)
                if unicodedata.category(c) != "Mn")  # quitar acentos
    return " ".join(a.split())  # colapsar espacios


def leer_diccionario_filiaciones() -> dict:
    """Devuelve {clave_autor_normalizada: [filiacion1, filiacion2, ...]} desde la
    hoja Filiaciones. Un autor puede tener MÁS DE UNA filiación (ej. cambió de
    institución con el tiempo, o pertenece a varias). Si la hoja no existe,
    devuelve diccionario vacío (no es un error)."""
    gc = _client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(HOJA_FILIACIONES)
    except Exception:
        print("  [INFO] No existe la hoja 'Filiaciones' todavía; se usará vacía.")
        return {}

    filas = ws.get_all_records()  # encabezados esperados: Autor, Filiacion
    dicc = {}
    for fila in filas:
        autor = str(_col(fila, "Autor") or "").strip()
        fil = str(_col(fila, "Filiacion") or "").strip()
        if autor and fil:
            clave = _normalizar_autor(autor)
            dicc.setdefault(clave, [])
            if fil not in dicc[clave]:  # evitar duplicado EXACTO de texto
                dicc[clave].append(fil)
    total_fil = sum(len(v) for v in dicc.values())
    print(f"  [INFO] Diccionario de filiaciones cargado: {len(dicc)} autores, "
          f"{total_fil} filiaciones en total.")
    return dicc


def agregar_filiacion(autor: str, filiacion: str, dicc_actual: dict | None = None):
    """Agrega una filiación al diccionario. Un mismo autor puede tener varias
    filiaciones distintas (todas se guardan); solo se evita el duplicado EXACTO
    (mismo autor + mismo texto de filiación ya cargado).

    Si se pasa `dicc_actual` (el diccionario ya cargado en memoria), se usa para
    chequear duplicados sin releer toda la hoja de nuevo (más rápido en loops)."""
    gc = _client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(HOJA_FILIACIONES)
    except Exception:
        # Crear la hoja con encabezados si no existe.
        ws = sh.add_worksheet(title=HOJA_FILIACIONES, rows=1000, cols=2)
        ws.update("A1:B1", [["Autor", "Filiacion"]])

    clave = _normalizar_autor(autor)
    existentes = dicc_actual if dicc_actual is not None else leer_diccionario_filiaciones()
    if filiacion in existentes.get(clave, []):
        return False  # ya estaba cargada tal cual, no duplicar

    ws.append_row([autor, filiacion], value_input_option="USER_ENTERED")
    print(f"  [INFO] Filiación agregada al diccionario: {autor} -> {filiacion[:60]}...")
    if dicc_actual is not None:
        dicc_actual.setdefault(clave, []).append(filiacion)  # mantener cache al día
    return True
