"""
Scraper de filiaciones ya cargadas.

Recorre los ítems listados en la hoja 'ColaFiliaciones' de tu Google Sheets
(columnas: Link, Estado) — pensada para ítems que YA tienen las filiaciones
completas y correctas (ya revisados/[REVISADO], o cualquiera que quieras usar
como fuente) — y extrae los pares (Autor, Filiación) para engordar el
diccionario compartido en la hoja 'Filiaciones'.

IMPORTANTE: este script es de SOLO LECTURA sobre RDU. Nunca escribe, edita ni
guarda nada en los ítems — solo abre la página de edición para leer los campos
de Autores y Filiación tal como están, y los copia a Sheets. Cero riesgo de
modificar el repositorio.
"""
import traceback

import rdu_navigation as nav
import sheets_client as sc


def procesar_item_filiaciones(page, link: str, dicc_cache: dict) -> dict:
    print(f"\n=== Leyendo filiaciones: {link} ===")
    nav.abrir_item(page, link)

    autores = nav.leer_autores(page)
    filiaciones = nav.leer_filiaciones(page)

    print(f"  [DEBUG] Autores: {autores}")
    print(f"  [DEBUG] Filiaciones: {filiaciones}")

    agregadas = 0
    # Emparejamos por índice: el campo de filiación N corresponde al autor N
    # (así es como el flujo principal los escribe).
    for i, autor in enumerate(autores):
        autor = autor.strip()
        if not autor or i >= len(filiaciones):
            continue
        filiacion = filiaciones[i].strip()
        if not filiacion:
            continue  # este autor no tiene filiación cargada en este ítem
        try:
            se_agrego = sc.agregar_filiacion(autor, filiacion, dicc_cache)
            if se_agrego:
                agregadas += 1
        except Exception as e:
            print(f"  [WARN] No se pudo agregar la filiación de {autor}: {e}")

    print(f"  [OK] {agregadas} filiación(es) nueva(s) agregada(s) al diccionario.")
    return {"agregadas": agregadas, "autores_vistos": len([a for a in autores if a.strip()])}


def main():
    pendientes, ws_cola = sc.obtener_pendientes_filiaciones()
    if not pendientes:
        print("No hay ítems pendientes en ColaFiliaciones.")
        return

    print(f"Procesando {len(pendientes)} ítem(s) para extraer filiaciones...")
    dicc_cache = sc.leer_diccionario_filiaciones()

    playwright, browser, page = nav.abrir_navegador()
    try:
        nav.login(page)
        for item in pendientes:
            sc.marcar_estado_cola(ws_cola, item["row_index"], "procesando")
            try:
                resultado = procesar_item_filiaciones(page, item["link"], dicc_cache)
                estado = f"OK ({resultado['agregadas']} nuevas)"
                sc.marcar_estado_cola(ws_cola, item["row_index"], estado)
            except Exception as e:
                print(f"  [ERROR] Falló el ítem {item['link']}: {e}")
                traceback.print_exc()
                sc.marcar_estado_cola(ws_cola, item["row_index"], "ERROR")
    finally:
        nav.cerrar_navegador(playwright, browser)


if __name__ == "__main__":
    main()
