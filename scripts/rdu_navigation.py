"""
Navegación headless de RDU con Playwright.
Selectores confirmados contra el HTML real de DSpace 7.6.5 (RDU UNC).

NOTA SOBRE MATERIAS: el campo dc.subject es un vocabulario controlado con
readonly + árbol modal. Por decisión de diseño, el robot NO toca Materias:
las dejás vos a mano en tu revisión. El robot hace todo el resto.
"""
import os
from playwright.sync_api import sync_playwright

RDU_USER = os.environ["RDU_USER"]
RDU_PASS = os.environ["RDU_PASS"]
RDU_LOGIN_URL = "https://rdu.unc.edu.ar/login"


def login(page):
    page.goto(RDU_LOGIN_URL)
    page.wait_for_load_state("networkidle")

    # Usar click() + type() en lugar de fill() para que Angular detecte los
    # eventos de input correctamente y habilite el botón de submit.
    # fill() inyecta el valor directamente sin disparar los eventos que Angular
    # escucha para validar el formulario reactivo.
    campo_email = page.locator("input[type='email']")
    campo_email.click()
    campo_email.type(RDU_USER, delay=50)

    campo_pass = page.locator("input[type='password']")
    campo_pass.click()
    campo_pass.type(RDU_PASS, delay=50)

    # Esperar a que Angular valide el formulario y HABILITE el botón.
    # El botón arranca con aria-disabled="true" hasta que el form es válido.
    try:
        page.wait_for_function(
            """() => {
                const b = document.querySelector("button[data-test='login-button']");
                return b && b.getAttribute('aria-disabled') !== 'true' && !b.disabled;
            }""",
            timeout=8000,
        )
        print("  [DEBUG] Botón de login habilitado.")
    except Exception:
        print("  [WARN] El botón sigue deshabilitado tras llenar credenciales. "
              "Esto suele indicar que el email o la contraseña no pasaron la "
              "validación del formulario (revisar espacios/saltos en los secrets).")

    try:
        page.click("button[data-test='login-button']", timeout=8000)
    except Exception:
        try:
            page.click("button[type='submit']", timeout=5000)
        except Exception:
            campo_pass.press("Enter")

    # CLAVE: esperar a que la redirección post-login OCURRA antes de juzgar.
    # La autenticación es asíncrona: el clic dispara la petición, y recién
    # cuando vuelve el token DSpace saca al usuario de /login. Si chequeamos
    # la URL de inmediato, vemos /login todavía y daríamos un falso "fallido".
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
        print(f"  [DEBUG] Redirección OK, URL ahora: {page.url}")
    except Exception:
        # Tras 20s todavía en /login => login realmente falló.
        raise RuntimeError(
            f"Login fallido: seguimos en {page.url} tras 20s. "
            "Verificá que RDU_USER y RDU_PASS en los secrets de GitHub estén "
            "exactos, sin espacios ni saltos de línea al final. Probá copiarlos "
            "de nuevo (al pegar en GitHub a veces se cuela un salto de línea)."
        )

    # DSpace 7 guarda el JWT en localStorage. Esperamos a que Angular termine
    # de procesar la autenticación antes de navegar a cualquier otra URL.
    try:
        page.wait_for_function(
            "() => !!localStorage.getItem('dsAuthInfo')",
            timeout=15000,
        )
        print("  [DEBUG] Login OK — JWT presente en localStorage.")
    except Exception:
        # Algunos deployments de DSpace usan otra clave; si no encontró dsAuthInfo
        # pero ya salimos del /login, probablemente igual estamos autenticados.
        print("  [WARN] No se encontró 'dsAuthInfo' en localStorage, "
              "pero la URL ya no es /login. Continuando...")


def abrir_item(page, link_edit: str, max_intentos: int = 3):
    """Abre el formulario de edición con retry automático via refresh.

    DSpace 7 / Angular a veces no termina de hidratar el formulario en el
    primer intento (el mismo problema que se resuelve con F5 manualmente).
    Reintentamos hasta `max_intentos` veces antes de levantar excepción.
    """
    for intento in range(1, max_intentos + 1):
        if intento == 1:
            page.goto(link_edit)
        else:
            print(f"  [RETRY] Formulario no cargó, refresh (intento {intento}/{max_intentos})...")
            page.reload()

        page.wait_for_load_state("networkidle")

        # DEBUG: permite diagnosticar redirecciones al login o páginas de error.
        print(f"  [DEBUG] URL actual (intento {intento}): {page.url}")
        print(f"  [DEBUG] Título de página: {page.title()}")

        try:
            page.wait_for_selector("#dc_title", timeout=20000)
            # CLAVE: esperar a que el campo título tenga CONTENIDO real, no solo
            # que exista. Angular crea el campo vacío y lo rellena un instante
            # después con los datos del servidor. Si leemos en ese hueco, sale
            # todo vacío/a medias (título vacío, autores duplicados, etc.).
            page.wait_for_function(
                """() => {
                    const autores = Array.from(document.querySelectorAll("input[placeholder='Autores']"));
                    // La señal confiable de que DSpace terminó de hidratar es que
                    // haya autores y NINGUNO esté vacío. (El título puede estar
                    // legítimamente vacío en ítems incompletos, así que no lo
                    // exigimos como condición para no colgarnos esperándolo.)
                    if (autores.length === 0) return false;
                    return autores.every(a => a.value && a.value.trim().length > 0);
                }""",
                timeout=25000,
            )
            # Verificación de ESTABILIDAD: esperar a que el número de autores
            # llenos no cambie durante ~2s. Si sigue cambiando, DSpace todavía
            # está agregando autores y no hay que leer aún.
            conteo_previo = -1
            for _ in range(6):
                page.wait_for_timeout(500)
                conteo = page.evaluate(
                    """() => Array.from(document.querySelectorAll("input[placeholder='Autores']"))
                        .filter(a => a.value && a.value.trim()).length"""
                )
                if conteo == conteo_previo:
                    break  # estable
                conteo_previo = conteo
            page.wait_for_timeout(1000)
            print(f"  [DEBUG] Formulario cargado con datos en intento {intento}.")
            return  # formulario listo CON datos, salimos
        except Exception:
            snippet = page.locator("body").inner_text()[:300].replace("\n", " ")
            print(f"  [DEBUG] Formulario sin datos completos en intento {intento}. "
                  f"Texto visible: {snippet}")
            if intento == max_intentos:
                raise RuntimeError(
                    f"El formulario no terminó de cargar datos tras {max_intentos} intentos: {link_edit}"
                )


# ---------- HELPER DE ESCRITURA ----------
def _set_valor(page, locator, valor: str, cerrar_dropdown: bool = False):
    """Escribe en un campo de forma ATÓMICA y segura.

    - fill() reemplaza el valor de un solo golpe: NUNCA deja el campo a medias
      ni vacío (a diferencia de borrar-y-teclear, que si falla el segundo paso
      deja el campo borrado).
    - Luego disparamos los eventos 'input' y 'change' POR CÓDIGO para que
      Angular registre el cambio (sin teclear, así no se activa el
      autocompletado que duplicaba autores).
    """
    locator.fill(valor)
    locator.evaluate(
        """el => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }"""
    )
    if cerrar_dropdown:
        page.keyboard.press("Escape")  # cierra el dropdown visual sin aceptar nada
    page.wait_for_timeout(150)


# ---------- TÍTULO ----------
def leer_titulo(page, esperar_hidratacion: bool = False) -> str:
    """Lee el título. Si esperar_hidratacion=True, reintenta hasta ~8s esperando
    que DSpace lo cargue (los autores a veces hidratan antes que el título, y si
    leemos demasiado pronto sale vacío aunque el ítem SÍ tenga título)."""
    if not esperar_hidratacion:
        return page.input_value("#dc_title")
    # Reintentar lectura por si el título todavía no hidrató.
    for _ in range(16):  # 16 * 500ms = 8s máximo
        valor = page.input_value("#dc_title").strip()
        if valor:
            return valor
        page.wait_for_timeout(500)
    return ""  # tras 8s sigue vacío => el ítem genuinamente no tiene título


def escribir_titulo(page, nuevo_titulo: str):
    campo = page.locator("#dc_title")
    _set_valor(page, campo, nuevo_titulo)


# ---------- AUTORES ----------
# DSpace repite el MISMO id="dc_contributor_author" en todos los campos de autor
# (HTML inválido pero es lo que hay). Por eso NO se puede seleccionar por id:
# Playwright no resuelve bien .nth() sobre ids duplicados. Usamos el placeholder
# "Autores", que también está en todos, + el índice de posición.
_SEL_AUTORES = "input[placeholder='Autores']"


def leer_autores(page) -> list[str]:
    campos = page.locator(_SEL_AUTORES)
    return [campos.nth(i).input_value() for i in range(campos.count())]


def _asegurar_cantidad_campos(page, selector_campo: str, cantidad_necesaria: int, nombre_log: str):
    """Genérico: agrega campos con el botón 'Añadir más' correspondiente hasta
    tener `cantidad_necesaria` campos que matcheen `selector_campo`.

    BLINDADO: verifica que CADA clic agregue exactamente un campo del tipo
    esperado. Si un clic no aumenta esos campos (tocó el botón equivocado, ej.
    el de 'Otros títulos' en vez del de Autores/Filiación), ABORTA de inmediato
    para no crear cascadas de campos basura."""
    faltan = cantidad_necesaria - page.locator(selector_campo).count()
    if faltan <= 0:
        return

    for _ in range(faltan + 2):  # margen mínimo
        antes = page.locator(selector_campo).count()
        if antes >= cantidad_necesaria:
            return

        # El botón "Añadir más" tiene class="ds-form-add-more". Hay varios en el
        # form (uno por sección), todos iguales. Ubicamos el correcto buscando,
        # entre todos los 'ds-form-add-more', el que está inmediatamente
        # después del último campo del tipo buscado, en el orden del DOM.
        boton = page.evaluate_handle(
            """(sel) => {
                const campos = Array.from(document.querySelectorAll(sel));
                if (!campos.length) return null;
                const ultimo = campos[campos.length - 1];
                const botones = Array.from(document.querySelectorAll('button.ds-form-add-more'));
                for (const b of botones) {
                    if (ultimo.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) {
                        return b;
                    }
                }
                return null;
            }""",
            selector_campo,
        )
        if not boton or not boton.as_element():
            print(f"  [WARN] No se encontró el botón 'Añadir más' de {nombre_log}. "
                  f"Los campos extra quedan para revisión manual.")
            return
        try:
            boton.as_element().click(timeout=4000)
            page.wait_for_timeout(700)
        except Exception as e:
            print(f"  [WARN] No se pudo clickear 'Añadir más' de {nombre_log}: {e}")
            return

        despues = page.locator(selector_campo).count()
        # SALVAGUARDA: si el clic NO agregó un campo del tipo esperado, abortamos.
        if despues <= antes:
            print(f"  [ALERTA] El clic en 'Añadir más' no agregó un campo de {nombre_log} "
                  f"(antes={antes}, después={despues}). Aborto para no crear basura.")
            return


def asegurar_cantidad_campos_autor(page, cantidad_necesaria: int):
    """Agrega campos de Autor con 'Añadir más' hasta tener `cantidad_necesaria`."""
    _asegurar_cantidad_campos(page, _SEL_AUTORES, cantidad_necesaria, "AUTORES")


def asegurar_cantidad_campos_filiacion(page, cantidad_necesaria: int):
    """Agrega campos de Filiación con 'Añadir más' hasta tener `cantidad_necesaria`."""
    _asegurar_cantidad_campos(page, "input[name='dc.description.fil']",
                               cantidad_necesaria, "FILIACIÓN")


def limpiar_campos_autor_sobrantes(page, cantidad_usada: int):
    """Vacía los campos de autor que sobran (índices >= cantidad_usada).
    Útil cuando el original tenía varios autores en un campo y ahora usamos
    menos campos que los que existían."""
    campos = page.locator(_SEL_AUTORES)
    total = campos.count()
    for i in range(cantidad_usada, total):
        try:
            campo = campos.nth(i)
            campo.fill("")
            campo.evaluate(
                """el => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.blur();
                }"""
            )
            page.wait_for_timeout(150)
        except Exception:
            pass


def escribir_autor(page, index: int, nuevo_valor: str):
    """Escribe un autor lidiando con el autocompletado agresivo de DSpace.

    El combobox de autores abre un dropdown con sugerencias del índice Solr que
    puede pisar el valor. Estrategia:
      1. fill() para poner el valor (atómico, no teclea → menos sugerencias).
      2. blur() por código para QUITAR EL FOCO: esto cierra el dropdown SIN
         aceptar ninguna sugerencia (más confiable que Escape).
      3. Verificar el valor final y reintentar hasta 3 veces si quedó mal.
    """
    campo = page.locator(_SEL_AUTORES).nth(index)

    for intento in range(1, 4):
        # Poner el valor.
        campo.fill(nuevo_valor)
        # Disparar input/change para que Angular lo registre.
        campo.evaluate(
            """el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        # CLAVE: quitar el foco por código cierra el dropdown sin aceptar nada.
        campo.evaluate("el => el.blur()")
        page.wait_for_timeout(400)

        actual = campo.input_value().strip()
        if actual == nuevo_valor.strip():
            return  # quedó perfecto
        print(f"  [WARN] Autor {index}: quedó '{actual}' en intento {intento}, "
              f"esperaba '{nuevo_valor}'. Reintentando.")

    print(f"  [ALERTA] No se pudo escribir limpio el autor {index} tras 3 intentos. "
          f"Quedó: '{campo.input_value().strip()}'.")


# ---------- MATERIAS: el robot NO las toca (decisión de diseño) ----------


# ---------- RESUMEN ----------
def leer_resumen(page) -> str:
    return page.input_value("textarea[name='dc.description.abstract']")


def escribir_resumen(page, texto_final: str):
    campo = page.locator("textarea[name='dc.description.abstract']")
    campo.fill(texto_final)  # fill es rápido para textos largos
    # Disparar eventos para que Angular registre el cambio (el textarea no tiene
    # dropdown, así que no hay riesgo de que blur/change borre nada).
    campo.evaluate(
        """el => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        }"""
    )


# ---------- FILIACIÓN ----------
# Campos id 25_array-0-dc_description_fil, -1-, etc. (name="dc.description.fil").
# También es un combobox con autocomplete; usamos blur() como en autores.
def leer_filiaciones(page) -> list:
    """Lee el texto actual de todos los campos de Filiación del ítem (sin
    modificar nada). Usado por el scraper de filiaciones existentes."""
    campos = page.locator("input[name='dc.description.fil']")
    return [campos.nth(i).input_value().strip() for i in range(campos.count())]


def escribir_filiacion(page, index: int, texto_filiacion: str) -> bool:
    campos = page.locator("input[name='dc.description.fil']")
    cantidad = campos.count()
    print(f"  [DEBUG] Campos de filiación disponibles: {cantidad} (escribiendo en índice {index}).")
    if index < cantidad:
        campo = campos.nth(index)
        campo.fill(texto_filiacion)
        campo.evaluate(
            """el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.blur();
            }"""
        )
        page.wait_for_timeout(300)
        return True
    else:
        print(f"  [WARN] No existe campo de filiación en índice {index}. "
              "Puede que el ítem no tenga campos de filiación creados "
              "(habría que tocar 'Añadir más'). Queda para revisión manual.")
        return False


# ---------- PDF ----------
def obtener_nombre_archivo(page) -> str | None:
    """Busca el NOMBRE VISIBLE del archivo adjunto (con extensión real), no la
    URL de descarga. El link de descarga en RDU es un UUID sin extensión
    (/bitstreams/<uuid>/download), así que la única forma confiable de saber
    si el adjunto es .pdf, .doc o .docx es leer el nombre que se muestra en la
    sección 'Upload files' (ej. 'trabajo_final.doc')."""
    import re
    # El nombre de archivo aparece como texto visible con extensión conocida.
    loc = page.get_by_text(re.compile(r"\.(pdf|docx?|PDF|DOCX?)\b")).first
    if loc.count() == 0:
        return None
    texto = loc.inner_text().strip()
    return texto


def obtener_link_pdf(page) -> str | None:
    """Busca el link de descarga del adjunto. Confirmado contra el HTML real de
    RDU: el patrón es /bitstreams/<uuid>/download (plural 'bitstreams', termina
    en '/download', NO en '/content' ni '.pdf'). Se agregan variantes como
    respaldo por si algún deployment usa otro patrón."""
    candidatos = [
        "a[href*='/bitstreams/'][href*='/download']",  # patrón real confirmado
        "a[aria-label^='Descargar']",                   # aria-label empieza con "Descargar"
        "a[href*='/bitstreams/'][href*='/content']",
        "a[href*='/bitstream/']",
        "a[href$='.pdf']",
        "a[href*='.pdf']",
    ]
    for sel in candidatos:
        loc = page.locator(sel).first
        if loc.count() > 0:
            href = loc.get_attribute("href")
            if href:
                print(f"  [DEBUG] Link de adjunto encontrado con selector '{sel}': {href[:80]}...")
                # El href puede ser relativo (ej. "/bitreams/uuid/download");
                # lo resolvemos a absoluto contra la URL actual de la página.
                if href.startswith("/"):
                    from urllib.parse import urljoin
                    href = urljoin(page.url, href)
                return href
    print("  [WARN] No se encontró ningún link de descarga de adjunto en el ítem.")
    return None


# ---------- SUBIR PDF CONVERTIDO ----------
def subir_pdf_al_item(page, nombre_archivo: str, contenido_pdf: bytes) -> bool:
    """Sube un PDF al ítem usando el input de archivos de la página de edición.

    ATENCIÓN: esta función MODIFICA el adjunto del ítem en RDU. Es la operación
    más delicada del robot. En MODO_PRUEBA no debería llamarse (lo controla
    main.py). DSpace tiene una zona 'Suelte archivos para adjuntarlos al ítem'
    con un <input type=file> asociado; le pasamos los bytes directamente.
    """
    import tempfile, os as _os
    try:
        # Playwright necesita un archivo en disco o un dict con buffer.
        # Usamos set_input_files con el buffer en memoria.
        input_file = page.locator("input[type='file']").first
        if input_file.count() == 0:
            print("  [WARN] No se encontró el input de archivos para subir el PDF.")
            return False

        input_file.set_input_files(files=[{
            "name": nombre_archivo,
            "mimeType": "application/pdf",
            "buffer": contenido_pdf,
        }])
        # Esperar a que DSpace procese la subida (aparece el archivo en la lista).
        page.wait_for_timeout(4000)
        print(f"  [OK] PDF '{nombre_archivo}' enviado al ítem (verificar en captura).")
        return True
    except Exception as e:
        print(f"  [WARN] Error subiendo el PDF al ítem: {e}")
        return False


def descargar_pdf_bytes(page, url_pdf: str = None) -> bytes | None:
    """Descarga el archivo adjunto dejando que EL NAVEGADOR maneje la descarga,
    igual que haría una persona.

    IMPORTANTE — por qué NO usamos page.request.get() a la URL directamente:
    en esta versión de DSpace, la URL de descarga (/bitstreams/<id>/download)
    es una ruta de Angular que primero muestra una pantalla intermedia
    ("Descargando X.pdf... Atrás") y RECIÉN DESPUÉS, vía JavaScript, dispara la
    descarga real del archivo. Un HTTP request pelado solo trae esa pantalla
    HTML, nunca el archivo. Por eso simulamos el clic real y esperamos a que
    el navegador (que sí ejecuta el JS, como Chromium real) dispare la
    descarga efectiva.
    """
    link = page.locator("a[href*='/bitstreams/'][href*='/download']").first
    if link.count() == 0:
        link = page.locator("a[aria-label^='Descargar']").first
    if link.count() == 0:
        print("  [WARN] No se encontró el link de descarga para clickear.")
        return None

    href_directo = link.get_attribute("href")

    # Intento 1: clic normal + esperar el evento de descarga (más tiempo, ya
    # que Angular muestra una pantalla intermedia antes de disparar la
    # descarga real, y eso puede tardar unos segundos).
    try:
        with page.context.expect_event("download", timeout=45000) as descarga_info:
            link.click()
        descarga = descarga_info.value
        ruta = descarga.path()
        if ruta:
            with open(ruta, "rb") as f:
                contenido = f.read()
            print(f"  [DEBUG] Adjunto descargado vía navegador (clic): {len(contenido)} bytes.")
            return contenido
    except Exception as e:
        print(f"  [WARN] El clic no disparó una descarga en 45s ({e}). Probando goto directo...")

    # Intento 2 (respaldo): abrir una PESTAÑA NUEVA y navegar ahí a la URL de
    # descarga. Usamos una pestaña separada (no la `page` principal) porque
    # esta función se llama en medio del formulario de edición, con título y
    # autores ya escritos pero SIN GUARDAR — navegar la página principal
    # perdería esos cambios. Si el servidor responde con Content-Disposition
    # de archivo, Playwright convierte la navegación en descarga.
    if href_directo:
        pagina_extra = None
        try:
            from urllib.parse import urljoin
            url_abs = urljoin(page.url, href_directo) if href_directo.startswith("/") else href_directo
            pagina_extra = page.context.new_page()
            with pagina_extra.expect_download(timeout=20000) as descarga_info2:
                try:
                    pagina_extra.goto(url_abs)
                except Exception:
                    pass  # puede "fallar" justo porque se convirtió en descarga; es esperable
            descarga = descarga_info2.value
            ruta = descarga.path()
            if ruta:
                with open(ruta, "rb") as f:
                    contenido = f.read()
                print(f"  [DEBUG] Adjunto descargado vía pestaña nueva: {len(contenido)} bytes.")
                return contenido
        except Exception as e2:
            print(f"  [WARN] Tampoco funcionó la pestaña nueva: {e2}")
        finally:
            if pagina_extra:
                try:
                    pagina_extra.close()
                except Exception:
                    pass

    print("  [ALERTA] No se pudo descargar el adjunto por ningún método.")
    return None


# ---------- GUARDAR ----------
def guardar(page) -> bool:
    # Dar un momento a Angular para registrar los cambios y habilitar "Guardar".
    page.wait_for_timeout(1000)

    btn_guardar = page.locator("button[data-test='save']")
    btn_later = page.locator("button[data-test='save-for-later']")

    def _habilitado(loc) -> bool:
        if loc.count() == 0:
            return False
        aria = loc.first.get_attribute("aria-disabled")
        clase = loc.first.get_attribute("class") or ""
        return aria != "true" and "disabled" not in clase

    try:
        if _habilitado(btn_guardar):
            print("  [INFO] Guardando con botón 'Guardar'.")
            btn_guardar.first.click(timeout=8000)
        elif _habilitado(btn_later):
            print("  [INFO] 'Guardar' deshabilitado; uso 'Guardar para más adelante'.")
            btn_later.first.click(timeout=8000)
        else:
            print("  [WARN] Ningún botón de guardado está habilitado. "
                  "Probablemente Angular no detectó cambios en el formulario.")
            return False
    except Exception as e:
        print(f"  [WARN] Error al hacer clic en guardar: {e}")
        return False

    # Confirmar guardado. El mensaje puede variar entre 'Guardar' y 'Guardar
    # para más adelante', así que aceptamos varias formas.
    try:
        page.wait_for_selector(
            "text=/guardado con éxito|guardado correctamente|guardad|éxito/i",
            timeout=10000,
        )
        print("  [DEBUG] Mensaje de guardado detectado.")
        return True
    except Exception:
        print("  [WARN] No apareció un mensaje de éxito explícito. "
              "Verificá manualmente que el ítem haya quedado guardado.")
        return True  # asumimos OK si el clic no lanzó error


# ---------- SCREENSHOTS ----------
import os as _os
_DIR_CAPTURAS = "capturas"


def screenshot(page, nombre: str):
    """Guarda una captura de pantalla en la carpeta capturas/ para diagnóstico.
    El workflow de GitHub la sube como artefacto descargable."""
    try:
        _os.makedirs(_DIR_CAPTURAS, exist_ok=True)
        # Limpiar el nombre para que sea un archivo válido.
        seguro = "".join(c if c.isalnum() or c in "-_." else "_" for c in nombre)
        ruta = f"{_DIR_CAPTURAS}/{seguro}.png"
        page.screenshot(path=ruta, full_page=True)
        print(f"  [CAPTURA] Guardada: {ruta}")
    except Exception as e:
        print(f"  [WARN] No se pudo guardar la captura '{nombre}': {e}")


# ---------- NAVEGADOR ----------
def abrir_navegador():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    # accept_downloads=True es necesario para que expect_download() funcione
    # (usado al descargar el adjunto real, ver descargar_pdf_bytes).
    page = browser.new_page(accept_downloads=True)
    return playwright, browser, page


def cerrar_navegador(playwright, browser):
    browser.close()
    playwright.stop()
