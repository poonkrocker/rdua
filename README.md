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

## Chequeo de adjuntos rotos a partir de un Excel

Además del flujo de formateo completo de arriba, `scripts/chequear_adjuntos.py`
+ `.github/workflows/chequear-adjuntos.yml` corren un chequeo mucho más
simple, cuyo universo de ítems a revisar **lo definís vos a mano en un
Excel** — no filtra ni recorre el repositorio por su cuenta. No modifica
nada en RDU: es de solo lectura.

### Cómo se usa

1. Abrí (o creá corriendo el workflow una vez) `reportes/entradas_a_revisar.xlsx`.
2. En la columna **Link** pegá, uno por fila, el link o handle de cada ítem
   que querés chequear — cualquiera de estas formas sirve tal cual la
   copiaste:
   - `https://rdu.unc.edu.ar/handle/11086/29993`
   - `https://rdu.unc.edu.ar/items/<uuid>`
   - `11086/29993` (el handle pelado)
3. Subí el Excel al repo (commit + push) y corré el workflow (pestaña
   **Actions** → "Chequear adjuntos rotos RDU" → **Run workflow**, o esperá
   al cron diario).
4. El script completa, en la misma fila, las columnas **Titulo**, **Estado**
   (`OK` o `[ADJUNTO NO FUNCIONA]`), **Detalle** (motivo), **Bytes** (tamaño
   del adjunto encontrado) y **ÚltimoChequeo** (fecha), y commitea el Excel
   actualizado de vuelta al repo.

Un adjunto se marca `[ADJUNTO NO FUNCIONA]` si no tiene ningún archivo, o si
pesa `UMBRAL_BYTES_VACIO` bytes o menos (los adjuntos vacíos observados en
RDU pesan 42 bytes).

### Ítems en revisión (opcional, requiere login)

Aparte del Excel, si hay `RDU_USER`/`RDU_PASS` configurados como secrets
(los mismos que ya usa "Procesar items RDU"), el script también revisa los
ítems que **todavía no son públicos** porque están en el circuito de
revisión de DSpace — esos no tienen un link "normal" para pegar en el
Excel, por eso van aparte, en `reportes/adjuntos_rotos_en_revision.csv`. Es
*best-effort*: si tu cuenta no tiene permisos de revisor/admin en RDU (vas
a ver un `403` en el log) o si la forma de la respuesta necesita un ajuste,
avisa por log sin afectar el chequeo del Excel.

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
