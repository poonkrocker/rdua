"""
Transformaciones de formato DETERMINISTICAS (sin IA).

Todo lo que se puede resolver con reglas va aca: es gratis, reproducible y
testeable. La IA queda solo para lo que realmente requiere criterio
(separar autores apelmazados, redactar/limpiar prosa, inferir filiaciones).

Convenciones que aplica, segun el escaneo del formulario de RDU:
  - dc.contributor.author : repetible, un autor por campo, "Apellido, Nombre"
  - dc.subject            : repetible; si el valor trae "::" es del vocabulario
                            srsc y NO se toca; el texto libre va a formato oracion
  - dc.description.fil    : repetible, UNA filiacion por campo
"""
from __future__ import annotations

import re
import unicodedata

# Siglas que se mantienen en mayuscula sostenida dentro de un texto.
SIGLAS = {
    "UNC", "CONICET", "CIECS", "INIMEC", "UNSAM", "UBA", "UNR", "UTN",
    "ADN", "ARN", "TDAH", "TEA", "OMS", "ONU", "TIC", "VIH", "SIDA",
    "CIN", "CIFFyH", "SECyT", "ANPCyT", "PhD", "ONG", "COVID",
}

# Nombres propios que NO deben pasar a minuscula al aplicar formato oracion.
# Ampliar a gusto: es la lista que protege "Psicologia en Cordoba" de quedar
# como "Psicologia en cordoba". Se compara SIN ACENTOS y en minuscula, y
# admite nombres de varias palabras ("buenos aires" se detecta como frase, asi
# que no queda "Buenos aires").
NOMBRES_PROPIOS = {
    # lugares
    "argentina", "cordoba", "buenos aires", "america latina", "america del sur",
    "latinoamerica", "brasil", "chile", "uruguay", "paraguay", "bolivia",
    "peru", "mexico", "espana", "europa", "africa", "asia", "patagonia",
    "rio cuarto", "villa maria", "santa fe", "mendoza", "rosario", "tucuman",
    "salta", "jujuy", "san luis", "entre rios",
    # gentilicios que suelen ir capitalizados en descriptores
    "argentino", "argentinos",
    # autores/escuelas citados como descriptor
    "freud", "lacan", "vigotsky", "piaget", "bourdieu", "foucault",
    "marx", "darwin", "jung", "winnicott",
}

# Particulas que van en minuscula dentro de un apellido/nombre compuesto.
PARTICULAS = {"de", "del", "la", "las", "los", "van", "von", "da", "di", "du", "y", "e"}

# Numeros romanos (para no minusculizar "Congreso XII")
_ROMANO = re.compile(r"^[IVXLCDM]+$")

# Un token que es una inicial: "A." o "A"
_INICIAL = re.compile(r"^[A-ZÁÉÍÓÚÑÜ]\.?$")

# Solo letras, para decidir si un token es una tanda de iniciales pegadas.
_SOLO_LETRAS = re.compile(r"[^A-Za-zÁÉÍÓÚÑÜáéíóúñü]")

# Marcas que ESTE robot pone al principio del resumen. Solo estas se limpian
# antes de volver a marcar; cualquier otro "[...]" inicial se considera parte
# del resumen del autor y NO se toca (ver anteponer_marca).
MARCAS_PROPIAS = {
    "visado",
    "adjunto",
    "revisar autores",
    "fil",
    "visado-imagen escaneada",
    "adjunto no funciona",
}


# ---------------------------------------------------------------- utilidades
def quitar_acentos(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t)
                   if unicodedata.category(c) != "Mn")


def _clave(t: str) -> str:
    """Normaliza para comparar: sin acentos, minusculas, espacios colapsados."""
    return " ".join(quitar_acentos((t or "").strip().lower()).split())


def _es_sigla(token: str) -> bool:
    limpio = token.strip(".,;:()[]")
    if not limpio:
        return False
    if limpio.upper() in SIGLAS:
        return True
    if _ROMANO.match(limpio) and len(limpio) > 1:
        return True
    if any(ch.isdigit() for ch in limpio):
        return True
    return False


def _propios_simples() -> set:
    return {p for p in NOMBRES_PROPIOS if " " not in p}


def _propios_frases() -> list:
    """Nombres propios de varias palabras, de mas largo a mas corto (para que
    'america del sur' gane sobre 'america latina' al matchear)."""
    return sorted((p for p in NOMBRES_PROPIOS if " " in p),
                  key=lambda s: -len(s.split()))


def _es_nombre_propio(token: str) -> bool:
    """True si el token (una sola palabra) es un nombre propio conocido."""
    return _clave(token.strip(".,;:()[]")) in _propios_simples()


def _capitalizar_token(tok: str) -> str:
    return tok[:1].upper() + tok[1:].lower()


# ------------------------------------------------------------------ materias
def es_termino_controlado(termino: str) -> bool:
    """True si el termino viene del vocabulario jerarquico srsc (trae '::')."""
    return "::" in (termino or "")


def formato_oracion(texto: str) -> str:
    """Mayuscula inicial, resto en minuscula, respetando siglas, romanos y
    nombres propios conocidos.

    'PSICOLOGIA EDUCACIONAL'      -> 'Psicologia educacional'
    'trastornos del espectro TEA' -> 'Trastornos del espectro TEA'
    'Salud mental en Cordoba'     -> 'Salud mental en Cordoba'  (no rompe el propio)
    """
    if not texto:
        return texto
    texto = re.sub(r"\s+", " ", texto).strip()

    # Un termino con "::" es del vocabulario controlado: intocable.
    if es_termino_controlado(texto):
        return texto

    tokens = texto.split(" ")
    frases = _propios_frases()
    out = []
    i = 0
    while i < len(tokens):
        # 1) nombre propio de VARIAS palabras ("Buenos Aires", "America Latina")
        largo_frase = 0
        for frase in frases:
            largo = len(frase.split())
            if i + largo <= len(tokens):
                candidato = " ".join(tokens[i:i + largo]).strip(".,;:()[]")
                if _clave(candidato) == frase:
                    largo_frase = largo
                    break
        if largo_frase:
            out.extend(_capitalizar_token(t) for t in tokens[i:i + largo_frase])
            i += largo_frase
            continue

        tok = tokens[i]
        if _es_sigla(tok):
            # sigla conocida -> mayuscula sostenida; romanos/numeros -> tal cual
            if tok.strip(".,;:()[]").upper() in SIGLAS:
                out.append(tok.upper())
            else:
                out.append(tok)
        elif _es_nombre_propio(tok):
            out.append(_capitalizar_token(tok))
        elif i == 0:
            base = tok.lower()
            out.append(base[:1].upper() + base[1:])
        else:
            out.append(tok.lower())
        i += 1
    return " ".join(out)


def formatear_materia(termino: str) -> tuple[str, bool]:
    """Devuelve (termino_formateado, se_modifico).

    Los terminos del vocabulario controlado srsc se devuelven INTACTOS: tocarlos
    rompe el vinculo con el vocabulario.
    """
    t = (termino or "").strip()
    if not t:
        return t, False
    if es_termino_controlado(t):
        return t, False
    nuevo = formato_oracion(t)
    return nuevo, nuevo != t


# ------------------------------------------------------------------ títulos
def dividir_titulo_subtitulo(raw: str) -> tuple[str, str]:
    """Separa título y subtítulo usando los mismos criterios de la herramienta institucional:
    - Separadores con espacio: ' : ', ' – ', ' — ', ' - '.
    - Punto seguido de mayúscula: '. [A-Z]'.
    - Dos puntos: ':'.
    """
    texto = (raw or "").strip()
    separadores = [r"\s+:\s+", r"\s+[–—]\s+", r"\s+-\s+"]
    for patron in separadores:
        m = re.search(patron, texto)
        if m and m.start() > 5:
            return texto[:m.start()].strip(), texto[m.end():].strip()

    m_punto = re.search(r"^(.{6,}?)\.\s+([A-ZÁÉÍÓÚÜÑ].+)$", texto)
    if m_punto:
        return m_punto.group(1).strip(), m_punto.group(2).strip()

    ci = texto.find(":")
    if ci > 5:
        return texto[:ci].strip(), texto[ci + 1:].strip()

    return texto, ""


def estandarizar_titulo(texto: str) -> str:
    """Estandariza un título según el formato institucional de RDU:
    - Mayúscula inicial en el título principal, resto en minúsculas (respetando siglas y nombres propios).
    - Separador estricto ' : ' (espacio dos puntos espacio) entre título y subtítulo.
    - Primera palabra del subtítulo en minúscula (salvo nombre propio o sigla).
    - Sin punto final.
    - Sin espacios múltiples.
    """
    if not texto:
        return ""

    t_limpio = re.sub(r"\s+", " ", texto).strip()
    t_limpio = re.sub(r"\.+$", "", t_limpio).strip()

    titulo, subtitulo = dividir_titulo_subtitulo(t_limpio)
    f_titulo = formato_oracion(titulo)

    if subtitulo:
        f_sub = formato_oracion(subtitulo)
        tokens_sub = f_sub.split(" ")
        if tokens_sub:
            primera = tokens_sub[0]
            if not _es_sigla(primera) and not _es_nombre_propio(primera):
                tokens_sub[0] = primera[:1].lower() + primera[1:]
            f_sub = " ".join(tokens_sub)
        resultado = f"{f_titulo} : {f_sub}"
    else:
        resultado = f_titulo

    return re.sub(r"\.+$", "", resultado).strip()


def son_titulos_equivalentes(titulo_a: str, titulo_b: str) -> bool:
    """Compara si dos títulos son equivalentes bajo las reglas de estandarización de RDU."""
    std_a = estandarizar_titulo(titulo_a)
    std_b = estandarizar_titulo(titulo_b)
    return std_a.lower() == std_b.lower()


# ------------------------------------------------------------- descripciones
def limpiar_descripcion(texto: str) -> str:
    """Limpia el texto de una descripción o resumen según la solapa 'Descripciones' del HTML:
    - Decodifica entidades HTML.
    - Elimina comentarios y etiquetas HTML/Word.
    - Elimina tabulaciones y saltos de línea (unifica en un solo párrafo).
    - Reduce espacios múltiples.
    - Convierte ?texto? a comillas tipográficas “texto”.
    - Corrige espaciado antes y después de signos de puntuación (. , ; : ! ?).
    """
    if not texto:
        return ""

    t = texto

    # Decodificar entidades HTML comunes
    t = (
        t.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
    )

    # Eliminar HTML de Word y etiquetas
    t = re.sub(r"<!--\[if[^\]]*\]>[\s\S]*?<!\[endif\]-->", "", t, flags=re.IGNORECASE)
    t = re.sub(r"<!--[\s\S]*?-->", "", t)
    t = re.sub(r"<style[\s\S]*?</style>", "", t, flags=re.IGNORECASE)
    t = re.sub(r"<script[\s\S]*?</script>", "", t, flags=re.IGNORECASE)
    t = re.sub(r"<xml[\s\S]*?</xml>", "", t, flags=re.IGNORECASE)
    t = re.sub(r"<object[\s\S]*?</object>", "", t, flags=re.IGNORECASE)
    t = re.sub(r"<li[^>]*>([\s\S]*?)</li>", r"\1. ", t, flags=re.IGNORECASE)
    t = re.sub(r"</(p|div|h[1-6]|tr|blockquote|ul|ol)>", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"<br\s*/?>", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)

    # Tabulaciones y saltos de línea -> espacio
    t = t.replace("\t", " ")
    t = t.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")

    # Espacios múltiples
    t = re.sub(r"\s{2,}", " ", t)

    # ?texto? -> “texto”
    t = re.sub(r"\?([^?]{1,80}?)\?", r"“\1”", t)

    # Espacios antes de puntuación
    t = re.sub(r"\s+([.,;:])", r"\1", t)

    # Espacios faltantes tras puntuación
    t = re.sub(r"([.,;:!?])([a-zA-ZáéíóúüñÁÉÍÓÚÜÑ])", r"\1 \2", t)

    return t.strip()


# ------------------------------------------------------------------- autores
def es_run_de_iniciales(token: str) -> bool:
    """True si el token son INICIALES PEGADAS sin puntos: 'Rm', 'MB', 'JCM'.

    En RDU los autores vienen asi todo el tiempo ('Pautassi, Rm' por Ricardo
    Marcos, 'Virgolini, Mb'). Sin esta deteccion, 'Rm' se compara contra
    'Ricardo' como si fuera un nombre de pila de dos letras y nunca matchea.

    El criterio es que un nombre de pila SIEMPRE tiene alguna vocal: 'Ana',
    'Eva' y 'Luz' tienen; 'Rm' y 'Mb' no. Asi se distinguen sin depender de las
    mayusculas (que ya se perdieron al normalizar) ni del largo del token.
    """
    limpio = _SOLO_LETRAS.sub("", token or "")
    if len(limpio) < 2:
        return False
    return not any(quitar_acentos(c).lower() in "aeiou" for c in limpio)


def _capitalizar_parte(parte: str) -> str:
    """Capitaliza una parte de nombre respetando particulas y siglas."""
    tokens = parte.split()
    out = []
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low in PARTICULAS and i > 0:
            out.append(low)
        elif _es_sigla(tok):
            out.append(tok)
        elif "-" in tok:  # apellidos compuestos con guion
            out.append("-".join(p[:1].upper() + p[1:].lower() for p in tok.split("-")))
        else:
            out.append(tok[:1].upper() + tok[1:].lower())
    return " ".join(out)


def normalizar_nombre_autor(nombre: str) -> dict:
    """Normaliza a 'Apellido, Nombre' y elimina iniciales SECUNDARIAS.

    Devuelve:
      {
        "nombre": str,        # el resultado
        "cambio": bool,       # si difiere del original
        "revisar": bool,      # True si NO se pudo resolver con seguridad
        "motivo": str,        # por que hay que revisarlo
      }

    Regla de iniciales (la misma que se uso al curar el diccionario):
      - 'Medrano, Leonardo A.'  -> 'Medrano, Leonardo'   (se descarta la inicial)
      - 'Collino, Cristina M.'  -> 'Collino, Cristina'
      - 'Abate, P.'             -> se DEJA igual y se marca revisar: la inicial ES
                                   el nombre de pila, borrarla dejaria 'Abate,'
    """
    original = (nombre or "").strip()
    if not original:
        return {"nombre": "", "cambio": False, "revisar": True,
                "motivo": "nombre vacio"}

    # separar apellido / nombres
    if "," in original:
        apellido, resto = original.split(",", 1)
    else:
        # sin coma: no se puede saber cual es el apellido con certeza
        return {"nombre": original, "cambio": False, "revisar": True,
                "motivo": "sin coma: no se puede determinar el apellido"}

    apellido = _capitalizar_parte(apellido.strip())
    tokens = resto.strip().split()

    if not tokens:
        return {"nombre": f"{apellido},", "cambio": True, "revisar": True,
                "motivo": "no hay nombre de pila"}

    # El primer token es inicial -> no se puede eliminar (ES el nombre de pila).
    # Vale igual para iniciales pegadas sin puntos ('Pautassi, Rm'), que ademas
    # se dejan en MAYUSCULA: escribir 'Pautassi, Rm' en RDU seria peor que
    # dejar 'Pautassi, RM' si el diccionario no llega a resolverlo.
    if _INICIAL.match(tokens[0]) or es_run_de_iniciales(tokens[0]):
        partes = [t.upper() if es_run_de_iniciales(t) else t for t in tokens]
        rearmado = f"{apellido}, {' '.join(partes)}"
        return {"nombre": rearmado, "cambio": rearmado != original, "revisar": True,
                "motivo": "el nombre de pila esta abreviado (inicial); "
                          "completar a mano o desde el diccionario"}

    # descartar iniciales secundarias (2do nombre abreviado)
    conservados = [tokens[0]]
    descartados = []
    for tok in tokens[1:]:
        if _INICIAL.match(tok):
            descartados.append(tok)
        else:
            conservados.append(tok)

    nombres = _capitalizar_parte(" ".join(conservados))
    resultado = f"{apellido}, {nombres}"
    return {
        "nombre": resultado,
        "cambio": resultado != original,
        "revisar": False,
        "motivo": (f"se descartaron iniciales: {' '.join(descartados)}"
                   if descartados else ""),
    }


def clave_autor(nombre: str) -> str:
    """Clave normalizada para buscar en el diccionario de filiaciones
    (sin acentos, minusculas, espacios colapsados). Igual criterio que
    sheets_client._normalizar_autor, para que las claves coincidan."""
    return _clave(nombre)


def _atomos(tokens: list) -> list:
    """Parte los tokens del nombre de pila en unidades comparables.

    'Ricardo Marcos' -> ['Ricardo', 'Marcos'] | 'R.m.' -> ['R', 'm']
    'M. E.'          -> ['M', 'E']            | 'P.'   -> ['P']
    'Rm'             -> ['R', 'm']            | 'Mb'   -> ['M', 'b']
    """
    out = []
    for tok in tokens:
        for parte in re.split(r"[.\s]+", tok):
            if not parte:
                continue
            if es_run_de_iniciales(parte):
                out.extend(list(parte))  # 'Rm' son DOS iniciales, no un nombre
            else:
                out.append(parte)
    return out


def _desarmar(nombre: str):
    """'Apellido, Resto' -> (apellido_normalizado, [tokens_del_nombre])."""
    if "," not in nombre:
        return None, []
    ap, resto = nombre.split(",", 1)
    ap_norm = clave_autor(ap).replace("-", " ")
    return ap_norm, resto.strip().split()


# --------------------------------------------- coincidencia con el diccionario
# El diccionario de filiaciones es la FUENTE DE VERDAD del nombre. Como en RDU
# los autores vienen abreviados de mil formas ('Abate, P.', 'Pautassi, R.m.',
# 'MEDRANO, LEONARDO A.'), buscar por igualdad exacta encuentra muy poco. Se
# comparan por NIVELES, del mas seguro al menos seguro:
#
#   exacta : el texto normalizado (sin acentos ni mayusculas) es identico.
#   alta   : mismo apellido y todos los nombres de pila COMPLETOS coinciden;
#            las diferencias son solo un segundo nombre de mas/de menos o
#            abreviado ('Medrano, Leonardo' ~ 'Medrano, Leonardo Adrian').
#   media  : mismo apellido y el PRIMER nombre de pila esta abreviado en uno de
#            los dos lados, pero la inicial coincide ('Abate, P.' ~ 'Abate,
#            Paula'). Es el caso "apellido + inicial".
#
# Un nivel se acepta SOLO si en ese nivel hay UN unico nombre candidato. Si el
# apellido+inicial da dos personas distintas ('Abate, Paula' y 'Abate, Pedro'),
# no hay certeza y no se toca nada: se marca el item para revision manual.
NIVELES = {"exacta": 3, "alta": 2, "media": 1}
NIVEL_POR_DEFECTO = "media"


def _atomos_compatibles(a: str, b: str) -> str | None:
    """Compara dos unidades de nombre. Devuelve 'completo', 'inicial' o None."""
    na = quitar_acentos(a).lower().strip("-")
    nb = quitar_acentos(b).lower().strip("-")
    if not na or not nb:
        return None
    if len(na) > 1 and len(nb) > 1:
        return "completo" if na == nb else None
    return "inicial" if na[0] == nb[0] else None


def nivel_coincidencia(nombre_a: str, nombre_b: str) -> str | None:
    """Nivel de coincidencia entre dos nombres ('exacta'/'alta'/'media'/None)."""
    a = (nombre_a or "").strip()
    b = (nombre_b or "").strip()
    if not a or not b:
        return None
    if _clave(a) == _clave(b):
        return "exacta"

    ap_a, tok_a = _desarmar(a)
    ap_b, tok_b = _desarmar(b)
    if not ap_a or not ap_b or ap_a != ap_b:
        return None  # apellido distinto: nunca se asume nada

    atomos_a, atomos_b = _atomos(tok_a), _atomos(tok_b)
    clean_a = [quitar_acentos(x).lower().strip("-.") for x in atomos_a if x]
    clean_b = [quitar_acentos(y).lower().strip("-.") for y in atomos_b if y]
    comp_a = [x for x in clean_a if len(x) > 1]
    comp_b = [x for x in clean_b if len(x) > 1]

    # 1. Ambos tienen nombres de pila completos
    if comp_a and comp_b:
        inter = set(comp_a) & set(comp_b)
        if inter:
            return "alta"
        return None

    # 2. Uno de los dos tiene solo iniciales
    inits_a = [x[0] for x in clean_a if x]
    inits_b = [x[0] for x in clean_b if x]
    if inits_a and inits_b and (inits_a[0] == inits_b[0] or any(ia in inits_b for ia in inits_a)):
        return "media"

    return None


def indice_nombres_diccionario(nombres_originales) -> dict:
    """Indice {apellido_normalizado: [nombre_canonico, ...]} a partir de los
    nombres ORIGINALES del diccionario (columna Autor, tal cual estan escritos).
    Se pasa dentro del dict de filiaciones bajo la clave '__nombres__'."""
    idx = {}
    for nombre in nombres_originales:
        ap_norm, tokens = _desarmar(nombre)
        if not ap_norm or not tokens:
            continue
        lista = idx.setdefault(ap_norm, [])
        if nombre.strip() not in lista:
            lista.append(nombre.strip())
    return idx


def _nombre_canonico(entrada) -> str:
    """Tolera el formato viejo del indice, que guardaba (nombre, iniciales)."""
    if isinstance(entrada, (tuple, list)):
        return str(entrada[0]) if entrada else ""
    return str(entrada or "")


def buscar_en_diccionario(nombre: str, dicc: dict,
                          minimo: str = NIVEL_POR_DEFECTO) -> dict:
    """Busca un autor en el diccionario de filiaciones tolerando abreviaturas.

    Devuelve:
      {
        "nombre":      nombre canonico del diccionario si hubo match, si no el
                       original (se usa TANTO para reescribir el autor en RDU
                       COMO para buscar sus filiaciones),
        "nivel":       'exacta' | 'alta' | 'media' | None,
        "encontrado":  bool,
        "ambiguo":     bool  (hubo varios candidatos igual de buenos),
        "candidatos":  list  (los nombres que empataron, para el log),
        "filiaciones": list  (las filiaciones del nombre canonico),
      }
    """
    original = (nombre or "").strip()
    vacio = {"nombre": original, "nivel": None, "encontrado": False,
             "ambiguo": False, "candidatos": [], "filiaciones": []}
    if not original or not isinstance(dicc, dict):
        return vacio

    idx = dicc.get("__nombres__") or {}
    ap_norm, tokens = _desarmar(original)
    if not ap_norm or not tokens:
        # Sin coma no se puede separar apellido de nombre: solo match exacto.
        filiaciones = dicc.get(clave_autor(original)) or []
        if filiaciones:
            return {**vacio, "nivel": "exacta", "encontrado": True,
                    "candidatos": [original], "filiaciones": list(filiaciones)}
        return vacio

    piso = NIVELES.get((minimo or "").lower(), NIVELES[NIVEL_POR_DEFECTO])

    # Agrupar los candidatos del mismo apellido por nivel de coincidencia.
    por_nivel = {}
    for entrada in idx.get(ap_norm, []):
        canonico = _nombre_canonico(entrada)
        nivel = nivel_coincidencia(original, canonico)
        if nivel and NIVELES[nivel] >= piso:
            lista = por_nivel.setdefault(nivel, [])
            if canonico not in lista:
                lista.append(canonico)

    if not por_nivel:
        return vacio

    # Gana el mejor nivel disponible; dentro de ese nivel exigimos unicidad.
    mejor = max(por_nivel, key=lambda n: NIVELES[n])
    candidatos = por_nivel[mejor]
    if len(candidatos) > 1:
        return {**vacio, "nivel": mejor, "ambiguo": True, "candidatos": candidatos}

    canonico = candidatos[0]
    return {"nombre": canonico, "nivel": mejor, "encontrado": True,
            "ambiguo": False, "candidatos": candidatos,
            "filiaciones": list(dicc.get(clave_autor(canonico)) or [])}


def completar_desde_diccionario(nombre: str, dicc: dict) -> dict:
    """Compatibilidad hacia atras: envoltorio de buscar_en_diccionario()."""
    r = buscar_en_diccionario(nombre, dicc)
    return {"nombre": r["nombre"],
            "completado": r["encontrado"] and r["nombre"] != (nombre or "").strip(),
            "ambiguo": r["ambiguo"], "candidatos": r["candidatos"]}


# ------------------------------------------------------------------- resumen
_RE_MARCA_INICIAL = re.compile(r"^\[([^\]]{1,40})\]\s*")


def limpiar_marcas_propias(resumen: str) -> str:
    """Saca del INICIO del resumen unicamente las marcas que pone este robot.

    IMPORTANTE: antes esto borraba CUALQUIER '[...]' inicial de hasta 40
    caracteres, asi que un resumen que empezaba con '[Trabajo presentado en el
    XII Congreso]' o '[Resumen del autor]' perdia ese texto en cada corrida.
    Ahora solo se limpian las marcas de MARCAS_PROPIAS; cualquier otro
    corchete se considera contenido del autor y se respeta.
    """
    texto = (resumen or "").strip()
    while True:
        m = _RE_MARCA_INICIAL.match(texto)
        if not m:
            break
        if _clave(m.group(1)) not in MARCAS_PROPIAS:
            break  # no es nuestra: es parte del resumen, se deja
        texto = texto[m.end():]
    return texto.strip()


def anteponer_marca(resumen: str, etiquetas: list) -> str:
    """Pone las etiquetas ([VISADO], etc.) AL PRINCIPIO del resumen, evitando
    duplicarlas si el resumen ya venia marcado de una corrida anterior."""
    texto = limpiar_marcas_propias(resumen)
    marcas = " ".join(e for e in etiquetas if e)
    return f"{marcas} {texto}".strip() if marcas else texto


def resumen_sospechoso_de_truncamiento(original: str, limpio: str,
                                       umbral: float = 0.85) -> bool:
    """True si el resumen 'limpio' perdio demasiado texto respecto del original.

    La limpieza de formato NO debe acortar el contenido: como mucho saca
    etiquetas HTML y espacios de mas. Si quedo bastante mas corto, casi seguro
    la IA lo trunco (respuesta cortada por max_tokens) o lo resumio. En ese
    caso conviene conservar el original antes que pisar RDU con texto perdido.
    """
    o = (original or "").strip()
    l = (limpio or "").strip()
    if not o:
        return False
    if not l:
        return True
    return len(l) < len(o) * umbral
