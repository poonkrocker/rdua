"""
Orquestador principal. Por cada item pendiente en la cola de Sheets:
  1. Abre el item en RDU (Playwright)
  2. Formatea título, autores, materias, resumen (Claude, paso por paso)
  3. Genera filiaciones (Claude, con contexto del PDF si está disponible)
  4. Verifica el PDF adjunto (Claude)
  5. Decide la marca: [VISADO] / [ADJUNTO] / sin marca
  6. Escribe todo en el formulario y guarda
  7. Registra el resultado en la hoja principal de Sheets y actualiza la cola

Corre sin supervisión humana (pensado para GitHub Actions). Por eso, ante
cualquier duda (filiación no confirmable, PDF que no coincide, archivo Word
sin convertir), el script NO fuerza una decisión: deja el item sin marca o con
[ADJUNTO] según corresponda, y lo registra como "revisar manualmente" en vez
de adivinar.
"""
import sys
import os
import re
import traceback
from pypdf import PdfReader
import io

import claude_steps as cs
import rdu_navigation as nav
import sheets_client as sc
import web_filiacion as wf
import ilovepdf_client as ilp

# Modo prueba: si está activo, hace TODO el formateo y saca captura del
# formulario lleno, pero NO aprieta Guardar (no toca nada en RDU). Para la etapa
# de ajuste. Cuando todo funcione, poner "false" en el workflow para que guarde.
MODO_PRUEBA = os.environ.get("MODO_PRUEBA", "true").lower() == "true"


def extraer_texto_pdf(pdf_bytes: bytes) -> str:
    """Extrae texto de los bytes de un PDF ya descargado (autenticado)."""
    if not pdf_bytes:
        return ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        texto = ""
        for p in reader.pages[:3]:  # alcanza con las primeras páginas
            texto += p.extract_text() or ""
        return texto
    except Exception as e:
        print(f"  [WARN] No se pudo leer el PDF descargado: {e}")
        return ""


def es_word(nombre_archivo: str) -> bool:
    """Detecta si el nombre de archivo es Word. Usa regex en vez de endswith()
    porque el nombre visible en RDU trae el tamaño al final, ej.
    'trabajo.doc (101 KB)' — eso NO termina en '.doc' literal, así que un
    endswith() simple nunca da verdadero."""
    return bool(re.search(r"\.docx?\b", nombre_archivo, re.IGNORECASE))


def procesar_item(page, link: str, dicc_filiaciones: dict):
    print(f"\n=== Procesando: {link} ===")
    nav.abrir_item(page, link)

    # --- 1. Título (LEER ahora, ESCRIBIR después de los autores) ---
    # Importante: manipular los autores (crear/borrar campos) re-renderiza el
    # formulario de Angular y puede borrar el título si lo escribimos antes. Por
    # eso lo leemos ahora pero lo escribimos al final de los autores.
    titulo_original = nav.leer_titulo(page, esperar_hidratacion=True).strip()
    print(f"  [DEBUG] Título original leído: {titulo_original!r}")
    titulo_sin_origen = False
    titulo_final = ""
    if not titulo_original:
        titulo_sin_origen = True
        print("  [ALERTA] El ítem NO tiene título en origen. Queda para revisión manual.")
    else:
        titulo_final = cs.formatear_titulo(titulo_original)
        print(f"  Título (se escribirá tras los autores): {titulo_final}")

    # --- 2. Autores ---
    # Los campos pueden traer VARIOS autores apelmazados (ej. "SUEN PABLO - DRUBI
    # MARIA SOLEDAD"). La IA los separa y formatea a "Apellido, Nombre".
    campos_autor_crudos = nav.leer_autores(page)
    print(f"  [DEBUG] Campos de autor crudos: {campos_autor_crudos}")
    resultado_autores = cs.separar_y_formatear_autores(campos_autor_crudos)
    autores_final = resultado_autores.get("autores", [])
    autores_confianza = resultado_autores.get("confianza", "baja")

    if not autores_final:
        print("  [ALERTA] La IA no pudo separar los autores. Se deja para revisión manual.")
        autores_final = [a.strip() for a in campos_autor_crudos if a.strip()]  # fallback: lo que había
        autores_confianza = "baja"
    else:
        # Asegurar que haya suficientes campos para todos los autores separados.
        nav.asegurar_cantidad_campos_autor(page, len(autores_final))
        # Escribir cada autor en su propio campo.
        for i, autor in enumerate(autores_final):
            nav.escribir_autor(page, i, autor)
        # Si quedaron campos viejos de más (porque antes había menos campos con
        # varios autores juntos), limpiarlos.
        nav.limpiar_campos_autor_sobrantes(page, len(autores_final))

    print(f"  Autores ({autores_confianza}): {autores_final}")

    # --- Escribir el título AHORA (después de los autores, para que el
    #     re-render de Angular al manipular autores no lo borre) ---
    if titulo_final:
        nav.escribir_titulo(page, titulo_final)
        # Verificar que quedó escrito; reintentar una vez si se perdió.
        if nav.leer_titulo(page).strip() != titulo_final.strip():
            print("  [WARN] El título no quedó tras escribirlo; reintentando.")
            nav.escribir_titulo(page, titulo_final)
        print(f"  Título escrito: {titulo_final}")

    # --- 3. Materias --- NO se tocan: vocabulario controlado, se revisan a mano.

    # --- 4. Resumen ---
    resumen_original = nav.leer_resumen(page)
    resumen_limpio = cs.limpiar_resumen(resumen_original)

    # --- PDF: leer adjunto y decidir flujo ---
    link_pdf = nav.obtener_link_pdf(page)
    nombre_archivo = nav.obtener_nombre_archivo(page)  # tiene la extensión real
    print(f"  [DEBUG] Nombre de archivo detectado: {nombre_archivo!r}")
    pdf_texto = ""
    adjunto_es_word = False
    pdf_es_imagen_escaneada = False
    pdf_verificacion = {"coincide": False, "detalle": "Sin adjunto detectado."}

    if link_pdf and nombre_archivo and es_word(nombre_archivo):
        print("  [INFO] Adjunto es Word. Intentando convertir a PDF con iLovePDF...")
        # Descargar el Word con la sesión autenticada.
        word_bytes = nav.descargar_pdf_bytes(page, link_pdf)  # sirve para cualquier bitstream
        # Usamos el nombre REAL visible (con extensión correcta), no el de la URL.
        nombre = nombre_archivo.split("(")[0].strip() or "documento.docx"
        if not nombre.lower().endswith((".doc", ".docx")):
            nombre += ".docx"
        pdf_convertido = ilp.convertir_a_pdf(nombre, word_bytes) if word_bytes else None

        if pdf_convertido:
            pdf_texto = extraer_texto_pdf(pdf_convertido)
            if pdf_texto:
                print(f"  [DEBUG] PDF convertido leído OK ({len(pdf_texto)} caracteres).")
                pdf_verificacion = cs.verificar_pdf(titulo_final, autores_final, pdf_texto)
            # Re-subir el PDF convertido al ítem (SOLO en modo real).
            if not MODO_PRUEBA:
                nombre_pdf = nombre.rsplit(".", 1)[0] + ".pdf"
                subido = nav.subir_pdf_al_item(page, nombre_pdf, pdf_convertido)
                if not subido:
                    adjunto_es_word = True  # quedó sin convertir efectivamente
            else:
                print("  [MODO PRUEBA] No se re-sube el PDF convertido (modo prueba).")
        else:
            adjunto_es_word = True
            print("  [ALERTA] No se pudo convertir el Word a PDF. Queda pendiente.")
    elif link_pdf:
        # Descargar con la sesión autenticada (los bitstreams de RDU lo exigen).
        pdf_bytes = nav.descargar_pdf_bytes(page, link_pdf)
        pdf_texto = extraer_texto_pdf(pdf_bytes)
        if pdf_texto:
            print(f"  [DEBUG] PDF leído OK ({len(pdf_texto)} caracteres extraídos).")
            pdf_verificacion = cs.verificar_pdf(titulo_final, autores_final, pdf_texto)
        else:
            # PDF que se descargó pero no tiene texto extraíble = imagen escaneada.
            pdf_es_imagen_escaneada = True
            print("  [INFO] El PDF no tiene texto extraíble: es una IMAGEN ESCANEADA. "
                  "Se marcará como tal.")
            pdf_verificacion = {"coincide": False, "detalle": "PDF imagen escaneada (sin texto)."}
    else:
        print("  [WARN] No se encontró link de PDF en el ítem.")

    # --- 5. Filiaciones ---
    # Orden de búsqueda: 1) diccionario local  2) PDF  3) web.
    # Si un autor tiene VARIAS filiaciones conocidas (distintas instituciones a
    # lo largo del tiempo), se incluyen TODAS en el campo, no solo la primera.
    filiaciones_ok = True
    for i, autor in enumerate(autores_final):
        clave = sc._normalizar_autor(autor)
        fuente = ""

        # 1) Diccionario local (gratis, ya validado). Puede tener varias.
        filiaciones_conocidas = list(dicc_filiaciones.get(clave, []))

        # 2) PDF + 3) web (solo si el diccionario no tenía ninguna todavía).
        if not filiaciones_conocidas:
            resultado = cs.generar_filiacion(autor, pdf_texto)
            if not (resultado.get("filiacion") and resultado.get("confianza") == "alta"):
                print(f"  [INFO] Filiación de {autor} no estaba en el PDF, buscando en la web...")
                contexto_web = wf.buscar_filiacion_web(autor, titulo_final)
                if contexto_web:
                    contexto_combinado = (pdf_texto or "") + "\n\n--- Búsqueda web ---\n" + contexto_web
                    resultado = cs.generar_filiacion(autor, contexto_combinado)
            if resultado.get("filiacion") and resultado.get("confianza") == "alta":
                filiaciones_conocidas = [resultado["filiacion"]]
                fuente = "PDF/web"
        else:
            fuente = "diccionario"

        # Escribir (TODAS combinadas) o marcar pendiente.
        if filiaciones_conocidas:
            filiacion_final = " ".join(filiaciones_conocidas)  # todas en el mismo campo
            nav.escribir_filiacion(page, i, filiacion_final)
            extra = f" ({len(filiaciones_conocidas)} filiaciones combinadas)" \
                    if len(filiaciones_conocidas) > 1 else ""
            print(f"  [OK] Filiación de {autor} ({fuente}){extra}: {filiacion_final}")
            # Si vino de PDF/web (no del diccionario), guardarla para la próxima.
            if fuente == "PDF/web":
                try:
                    sc.agregar_filiacion(autor, filiaciones_conocidas[0], dicc_filiaciones)
                except Exception as e:
                    print(f"  [WARN] No se pudo guardar la filiación en el diccionario: {e}")
        else:
            filiaciones_ok = False
            print(f"  [ALERTA] Filiación no confirmada para {autor} (ni diccionario, ni PDF, ni web).")

    # --- Decidir marca ---
    # Siempre guardamos el formateo logrado (título, autores, resumen). La marca
    # indica el nivel de revisión que el robot pudo alcanzar:
    #   [VISADO]                  -> todo OK, incluida verificación del PDF y filiaciones.
    #   [VISADO-IMAGEN ESCANEADA] -> todo OK salvo que el PDF es imagen sin texto;
    #                                no se pudo verificar autores contra el PDF, PERO
    #                                eso es un dato útil (descarta selectividad).
    #   [ADJUNTO]                 -> formateado, pero algo quedó pendiente de revisión
    #                                manual (filiación sin confirmar, PDF que no
    #                                coincide, o Word sin convertir).
    pendientes_revision = []
    if titulo_sin_origen:
        pendientes_revision.append("sin título en origen")
    if autores_confianza != "alta":
        nota = resultado_autores.get("nota", "")
        pendientes_revision.append(f"autores con separación dudosa ({nota})" if nota
                                   else "autores con separación dudosa")
    if not filiaciones_ok:
        pendientes_revision.append("filiación sin confirmar")
    if adjunto_es_word:
        pendientes_revision.append("adjunto Word sin convertir")
    elif not pdf_es_imagen_escaneada and not pdf_verificacion.get("coincide"):
        # Si es imagen escaneada NO lo contamos como pendiente: es su propia marca.
        pendientes_revision.append(f"PDF no verificado ({pdf_verificacion.get('detalle')})")

    if pendientes_revision:
        marca = "[ADJUNTO]"
        print(f"  [INFO] Guardando con [ADJUNTO]. Pendiente de revisión manual: "
              f"{'; '.join(pendientes_revision)}")
    elif pdf_es_imagen_escaneada:
        marca = "[VISADO-IMAGEN ESCANEADA]"
        print("  [INFO] Guardando con [VISADO-IMAGEN ESCANEADA] "
              "(PDF sin texto, descarta selectividad).")
    else:
        marca = "[VISADO]"

    # Nota adicional visible en el propio resumen cuando los autores quedaron
    # con separación dudosa (caso complejo, ej. varios nombres apelmazados sin
    # separador claro). Así el motivo queda a la vista directamente en RDU,
    # no solo en el log o en Sheets.
    etiquetas = [marca]
    if autores_confianza != "alta":
        etiquetas.append("[revisar autores]")
    if not filiaciones_ok:
        etiquetas.append("[FIL]")

    resumen_final = f"{' '.join(etiquetas)} {resumen_limpio}"
    nav.escribir_resumen(page, resumen_final)

    # --- RED DE SEGURIDAD: no guardar si vamos a corromper el ítem ---
    titulo_verif = nav.leer_titulo(page).strip()
    autores_verif = [a.strip() for a in nav.leer_autores(page) if a.strip()]
    problemas = []
    # El título vacío solo es PROBLEMA si el ítem SÍ tenía título y el robot lo
    # borró. Si ya venía sin título de origen, no es corrupción (queda [ADJUNTO]).
    if not titulo_verif and not titulo_sin_origen:
        problemas.append("el título quedó VACÍO (el robot lo borró)")
    if len(autores_verif) != len(set(autores_verif)):
        problemas.append(f"hay autores DUPLICADOS: {autores_verif}")
    if not autores_verif:
        problemas.append("no quedó ningún autor")

    # Captura del formulario lleno (siempre, antes de cualquier decisión).
    item_id = link.rstrip("/").split("/")[-2] if "/edit" in link else link.split("/")[-1]
    nav.screenshot(page, f"item_{item_id}_formulario_lleno")

    if problemas:
        print(f"  [ERROR] NO se guarda para no corromper el ítem. Problemas: "
              f"{'; '.join(problemas)}. Revisar manualmente.")
        nav.screenshot(page, f"item_{item_id}_PROBLEMA")
        return {
            "titulo": titulo_verif or titulo_final, "link": link,
            "tipo": "conferenceObject", "estado": "ERROR_VALIDACION", "guardado": False,
        }

    # --- MODO PRUEBA: no guardar, solo dejar constancia ---
    if MODO_PRUEBA:
        print("  [MODO PRUEBA] Formateo completo y validado. NO se guarda "
              "(modo prueba activo). Revisá la captura del formulario lleno.")
        return {
            "titulo": titulo_final, "link": link, "tipo": "conferenceObject",
            "estado": f"PRUEBA_{marca.strip('[]')}", "guardado": False,
        }

    # --- Guardar (solo en modo real y si la validación pasó) ---
    guardado_ok = nav.guardar(page)
    if not guardado_ok:
        nav.screenshot(page, f"item_{item_id}_ERROR_GUARDADO")
    estado_final = marca.strip("[]") if guardado_ok else "ERROR_AL_GUARDAR"
    return {
        "titulo": titulo_final, "link": link, "tipo": "conferenceObject",
        "estado": estado_final, "guardado": guardado_ok,
    }


def main():
    pendientes, ws_cola = sc.obtener_pendientes()
    if not pendientes:
        print("No hay items pendientes en la cola.")
        return

    print(f"Procesando {len(pendientes)} item(s)...")

    # Cargar el diccionario de filiaciones una sola vez (se actualiza en memoria
    # a medida que se confirman nuevas).
    try:
        dicc_filiaciones = sc.leer_diccionario_filiaciones()
    except Exception as e:
        print(f"  [WARN] No se pudo cargar el diccionario de filiaciones: {e}")
        dicc_filiaciones = {}

    playwright, browser, page = nav.abrir_navegador()

    try:
        nav.login(page)

        for item in pendientes:
            sc.marcar_estado_cola(ws_cola, item["row_index"], "procesando")
            try:
                resultado = procesar_item(page, item["link"], dicc_filiaciones)
                sc.registrar_item(
                    resultado["titulo"], resultado["link"],
                    resultado["tipo"], resultado["estado"],
                )
                sc.marcar_estado_cola(ws_cola, item["row_index"], resultado["estado"])
            except Exception as e:
                print(f"  [ERROR] Falló el item {item['link']}: {e}")
                traceback.print_exc()
                try:
                    nav.screenshot(page, f"ERROR_{item['link'].split('/')[-2]}")
                except Exception:
                    pass
                sc.marcar_estado_cola(ws_cola, item["row_index"], "ERROR")
                sc.registrar_item("—", item["link"], "—", "ERROR")

    finally:
        nav.cerrar_navegador(playwright, browser)


if __name__ == "__main__":
    main()
