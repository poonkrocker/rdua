# RDU Agent — automatización de carga en Repositorio Digital UNC

Script que corre en GitHub Actions (cron diario, gratis) para procesar items
pendientes en una cola de Google Sheets, aplicando formato estándar y
verificación de PDF, usando la API de Claude + Playwright.

## ⚠️ Estado actual: esqueleto funcional, requiere ajuste de selectores

Los selectores de Playwright en `scripts/rdu_navigation.py` están escritos
según lo que se ve en las capturas compartidas, pero **no están confirmados
contra el HTML real de RDU**. Antes de confiar en una corrida desatendida:

1. Corré el script una vez localmente con `headless=False` (cambiar esa línea
   en `rdu_navigation.py` temporalmente) para ver qué hace el navegador.
2. Si algún campo no se llena bien, inspeccioná el HTML real de esa página
   (botón derecho → Inspeccionar en Chrome) y corregí el selector en
   `rdu_navigation.py`.
3. Recién después de validar con 1-2 items reales, activá el cron.

## ⚠️ Si "Chequear adjuntos rotos RDU" falla con "Process completed with exit code 1"

La causa más común es que al paso final (`git push`, para guardar el reporte
y el checkpoint) le falta permiso de escritura. Por defecto GitHub le da al
token de Actions solo permiso de lectura, y hay que habilitarlo a mano una
vez por repo:

1. En tu repo → **Settings** → **Actions** → **General**.
2. Bajá hasta **"Workflow permissions"**.
3. Elegí **"Read and write permissions"** y guardá.
4. Volvé a correr el workflow (**Actions** → la corrida fallida → **Re-run
   all jobs**, o disparalo de nuevo manualmente).

Si el que falla es el paso "Ejecutar chequeo de adjuntos" (no el del `git
push`), fijate en el log qué línea imprimió el error — el script reintenta
solo los problemas de red pasajeros, así que si igual falla suele ser algo
puntual (endpoint que cambió, credenciales de `RDU_USER`/`RDU_PASS`
incorrectas para el chequeo de ítems en revisión, etc.).

## Setup paso a paso

### 1. Crear el repo en GitHub
Subí esta carpeta completa a un repo **privado** (importante: privado, porque
vas a guardar referencias a tus credenciales — aunque los secrets en sí no
quedan en el código, no conviene exponer la estructura/lógica de login).

### 2. Conseguir tu API key de Anthropic
- Entrá a [console.anthropic.com](https://console.anthropic.com), generá una
  API key.
- **Importante**: esto es una cuenta de facturación *distinta* a tu
  suscripción de Claude Pro — se paga por uso (tokens), no es lo mismo.
  Con 5 items/día y llamadas chicas por paso, el gasto mensual debería ser
  bajo, pero revisá el dashboard de uso los primeros días para calibrar.

### 3. Crear credenciales de Google Sheets (Service Account)
1. Entrá a [Google Cloud Console](https://console.cloud.google.com).
2. Creá un proyecto nuevo (o usá uno existente).
3. Habilitá la **Google Sheets API**.
4. Creá una **Service Account** (IAM & Admin → Service Accounts → Create).
5. Generá una key en formato JSON para esa cuenta — se descarga un archivo.
6. **Compartí tu Google Sheet** (el de
   `1mGP8zORmKlicrbNyFw5xlrDG3ddIn0-Qrcu7dxp9GQw`) con el email de la service
   account (algo como `nombre@proyecto.iam.gserviceaccount.com`), dándole
   permiso de Editor — sin este paso el script no va a poder escribir.

### 4. Preparar la hoja "Cola" en tu Google Sheets
Agregá una pestaña nueva llamada **Cola** con dos columnas en la fila 1:

| Link | Estado |
|---|---|

Ahí vas a ir pegando los links de los items a procesar (uno por fila). Estado
queda vacío hasta que el script lo procese (lo va completando solo con
`procesando`, `VISADO`, `ADJUNTO`, `PENDIENTE_REVISION`, `ERROR`, etc.).

### 5. Configurar los Secrets en GitHub
En tu repo → Settings → Secrets and variables → Actions → New repository
secret. Crear estos 5:

| Nombre | Valor |
|---|---|
| `ANTHROPIC_API_KEY` | tu API key de Anthropic |
| `RDU_USER` | tu usuario/email de RDU |
| `RDU_PASS` | tu contraseña de RDU |
| `GOOGLE_SHEET_ID` | `1mGP8zORmKlicrbNyFw5xlrDG3ddIn0-Qrcu7dxp9GQw` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | el contenido completo del archivo JSON descargado en el paso 3 (pegar tal cual, todo el JSON) |

### 6. Probar manualmente antes de confiar en el cron
En GitHub → pestaña **Actions** → "Procesar items RDU" → **Run workflow**
(botón a la derecha). Esto corre el script una vez, ya, sin esperar al
horario programado — usalo para probar con 1-2 items en la cola antes de
dejarlo desatendido.

### 7. Ajustar el horario
En `.github/workflows/procesar-items.yml`, la línea del cron:
```yaml
- cron: "0 9 * * *"
```
Está en hora UTC. Argentina es UTC-3, así que `0 9 * * *` = 06:00 hora
argentina. Para correrlo, por ejemplo, a las 22:00 hora Argentina, sería
`0 1 * * *` (01:00 UTC del día siguiente).

## Chequeo liviano de adjuntos rotos

Además del flujo de formateo completo de arriba, `scripts/chequear_adjuntos.py`
+ `.github/workflows/chequear-adjuntos.yml` corren un chequeo mucho más
simple: buscan ítems cuyo adjunto está ausente o pesa `UMBRAL_BYTES_VACIO`
bytes o menos (los adjuntos vacíos observados en RDU pesan 42 bytes). No
modifica nada en RDU: solo deja constancia en `reportes/adjuntos_rotos.csv`,
con la marca `[ADJUNTO NO FUNCIONA]`, el link al ítem y el motivo, para
revisar a mano. El script es liviano (sin Playwright, sin IA, sin Google
Sheets — solo librería estándar de Python; no hace falta `pip install`).

Tres chequeos, todos filtrables por tipo de ítem y años (ver más abajo):

1. **Rápido** (siempre completo, cada corrida): ítems **públicos** sin
   ningún archivo, vía un filtro nativo de DSpace. No requiere login.
2. **Gradual** (avanza de a tramos, con checkpoint): recorre los ítems
   **públicos** comparando el tamaño de cada adjunto contra el umbral.
   Pedirle a la API el tamaño de cada adjunto es lento del lado del
   servidor, así que recorrer TODO el repositorio de una sola corrida
   sería demasiado lento — cada corrida avanza `MAX_PAGINAS_POR_CORRIDA`
   páginas (por defecto 150, ~3000 ítems) y guarda dónde quedó en
   `reportes/checkpoint_adjuntos.txt`; la corrida siguiente retoma ahí, y
   al completar una vuelta arranca de nuevo. Con el cron cada 3 horas, el
   repositorio completo (dentro del filtro configurado) queda re-chequeado
   cada uno o dos días, en bucle continuo.
3. **Workflow** (opcional): ítems que **todavía no son públicos** porque
   están en el circuito de revisión de DSpace (enviados, en aprobación).
   Esos no aparecen en la búsqueda anónima, así que este chequeo se
   autentica contra la API con `RDU_USER`/`RDU_PASS` (los mismos secrets que
   ya usa "Procesar items RDU"; si no están cargados en este workflow, el
   chequeo se omite solo, sin marcar error). Es *best-effort*: la forma
   exacta de la respuesta de RDU para ítems en revisión no se pudo probar
   contra un ítem real, así que si falla vas a ver un `[WARN]`/`[ERROR]` en
   el log de esa sección puntual — el resto del chequeo (1 y 2) igual se
   guarda. Si falla, pasame el mensaje del log para ajustar el endpoint.

### Filtrar por tipo y año

Al correrlo manualmente (pestaña **Actions** → "Chequear adjuntos rotos RDU"
→ **Run workflow**) aparecen 4 campos:

| Campo | Ejemplo | Vacío significa |
|---|---|---|
| `tipos` | `doctoralThesis,masterThesis` | todos los tipos |
| `anio_desde` | `2020` | sin mínimo |
| `anio_hasta` | `2023` | sin máximo |
| `max_paginas` | `150` | usa el valor por defecto |

Los valores válidos de `tipos` son los de `dc.type` en RDU (ej.
`conferenceObject`, `bachelorThesis`, `dataSet`, `article`, `bookPart`,
`doctoralThesis`, `masterThesis`, `book`, `workingPaper`, `other`). La
corrida automática por cron usa los valores por defecto (sin filtro, o los
que dejes seteados como `default` en el workflow).

## Límites a tener en cuenta

- **GitHub Actions gratis**: 2000 minutos/mes para repos privados (de sobra
  para esto, cada corrida con 5 items debería tardar pocos minutos).
- **Sin supervisión en tiempo real**: si hay una filiación no confirmable o
  un PDF que no coincide, el script **no fuerza una decisión** — deja el item
  como `PENDIENTE_REVISION` o `ERROR` en la cola y en el registro, para que
  lo resuelvas vos a mano (por ejemplo con Claude in Chrome, en una sesión
  supervisada).
- **Selectores de RDU**: si la universidad actualiza la plataforma DSpace en
  el futuro, los selectores de `rdu_navigation.py` pueden romperse — el punto
  de chequeo es siempre correr una vez en modo visible (`headless=False`)
  si algo falla.
