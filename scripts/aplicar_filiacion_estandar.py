"""
Aplicador de filiación ESTÁNDAR fija a todos los autores de una cola de ítems.

Pensado para lotes donde SABÉS que todos los autores pertenecen a la misma
institución (ej. todos los trabajos de un congreso interno de la Facultad de
Psicología UNC), así te ahorrás la búsqueda en PDF/web/diccionario y aplicás
directo el formato estándar a cada autor.

Formato aplicado (personalizable con variables de entorno):
  "Fil: {Apellido, Nombre}. {UNIDAD_ACADEMICA}. {INSTITUCION}; {PAIS}."

⚠️ IMPORTANTE — usar solo en ítems con autores YA bien separados:
Este script toma el texto que HAY AHORA MISMO en cada campo de Autor tal cual
está, y arma la filiación con ese nombre. Si el campo de autor todavía tiene
varios nombres apelmazados o mal formateados (no pasó por el workflow
principal de formateo), la filiación va a quedar pegada a ese texto sucio.
Por eso: encolá acá solo ítems que ya estén [VISADO] o [ADJUNTO] con autores
correctos (podés revisarlo en la hoja de registro antes de encolar).

Respeta MODO_PRUEBA igual que el workflow principal: en modo prueba arma todo
y saca captura, pero no guarda nada en RDU.
"""
import os
import traceback

import rdu_navigation as nav
import sheets_client as sc

MODO_PRUEBA = os.environ.get("MODO_PRUEBA", "true").lower() == "true"

UNIDAD_ACADEMICA = os.environ.get("FIL_UNIDAD_ACADEMICA", "Facultad de Psicología")
INSTITUCION = os.environ.get("FIL_INSTITUCION", "Universidad Nacional de Córdoba")
PAIS = os.environ.get("FIL_PAIS", "Argentina")


def armar_filiacion(autor: str) -> str:
    return f"Fil: {autor}. {UNIDAD_ACADEMICA}. {INSTITUCION}; {PAIS}."


def procesar_item(page, link: str, dicc_cache: dict) -> dict:
    print(f"\n=== Aplicando filiación estándar: {link} ===")
    nav.abrir_item(page, link)

    autores = [a.strip() for a in nav.leer_autores(page) if a.strip()]
    if not autores:
        print("  [ALERTA] No hay autores cargados en este ítem. Se omite.")
        return {"link": link, "estado": "SIN_AUTORES", "autores": 0}

    print(f"  [DEBUG] Autores encontrados: {autores}")

    # Asegurar que haya suficientes campos de Filiación para todos los autores.
    nav.asegurar_cantidad_campos_filiacion(page, len(autores))

    for i, autor in enumerate(autores):
        filiacion = armar_filiacion(autor)
        nav.escribir_filiacion(page, i, filiacion)
        print(f"  [OK] Filiación de {autor}: {filiacion}")
        # Sumar al diccionario compartido para que otros ítems la reusen.
        try:
            sc.agregar_filiacion(autor, filiacion, dicc_cache)
        except Exception as e:
            print(f"  [WARN] No se pudo guardar en el diccionario: {e}")

    # Captura antes de decidir guardar (siempre útil para revisar).
    item_id = link.rstrip("/").split("/")[-2] if "/edit" in link else link.split("/")[-1]
    nav.screenshot(page, f"filestandar_{item_id}_formulario")

    if MODO_PRUEBA:
        print("  [MODO PRUEBA] No se guarda (modo prueba activo).")
        return {"link": link, "estado": "PRUEBA", "autores": len(autores)}

    guardado_ok = nav.guardar(page)
    if not guardado_ok:
        nav.screenshot(page, f"filestandar_{item_id}_ERROR_GUARDADO")
    return {
        "link": link,
        "estado": "GUARDADO" if guardado_ok else "ERROR_AL_GUARDAR",
        "autores": len(autores),
    }


def main():
    pendientes, ws_cola = sc.obtener_pendientes_fil_estandar()
    if not pendientes:
        print("No hay ítems pendientes en ColaFilEstandar.")
        return

    print(f"Procesando {len(pendientes)} ítem(s) con filiación estándar "
          f"'{UNIDAD_ACADEMICA}. {INSTITUCION}; {PAIS}.' "
          f"({'MODO PRUEBA' if MODO_PRUEBA else 'MODO REAL'})")

    dicc_cache = sc.leer_diccionario_filiaciones()
    playwright, browser, page = nav.abrir_navegador()

    try:
        nav.login(page)
        for item in pendientes:
            sc.marcar_estado_cola(ws_cola, item["row_index"], "procesando")
            try:
                resultado = procesar_item(page, item["link"], dicc_cache)
                sc.marcar_estado_cola(ws_cola, item["row_index"], resultado["estado"])
            except Exception as e:
                print(f"  [ERROR] Falló el ítem {item['link']}: {e}")
                traceback.print_exc()
                try:
                    nav.screenshot(page, f"filestandar_ERROR_{item['link'].split('/')[-2]}")
                except Exception:
                    pass
                sc.marcar_estado_cola(ws_cola, item["row_index"], "ERROR")
    finally:
        nav.cerrar_navegador(playwright, browser)


if __name__ == "__main__":
    main()
