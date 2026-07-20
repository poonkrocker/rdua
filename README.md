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

## Chequeo liviano de adjuntos rotos (sin credenciales)

Además del flujo de formateo completo de arriba, `scripts/chequear_adjuntos.py`
+ `.github/workflows/chequear-adjuntos.yml` corren un chequeo mucho más
simple: recorren la API pública de RDU (sin login, sin Playwright, sin IA,
sin Google Sheets — solo librería estándar de Python) buscando ítems cuyo
adjunto está ausente o pesa `UMBRAL_BYTES_VACIO` bytes o menos (los adjuntos
vacíos observados en RDU pesan 42 bytes). No modifica nada en RDU: solo dejar
constancia en `reportes/adjuntos_rotos.csv`, con la marca `[ADJUNTO NO
FUNCIONA]`, el link al ítem y el motivo, para revisar a mano.

- **No requiere secrets**: usa solo la API pública, así que no necesita
  `RDU_USER`/`RDU_PASS` ni ninguna otra credencial.
- **Corre en tramos**: recorrer los ~35.000 ítems del repositorio pidiéndole
  a la API el tamaño de cada adjunto es lento del lado del servidor, así que
  cada corrida avanza `MAX_PAGINAS_POR_CORRIDA` páginas (por defecto 150,
  ~3000 ítems) y guarda dónde quedó en `reportes/checkpoint_adjuntos.txt`.
  Con el cron cada 3 horas, el repositorio completo queda re-chequeado cada
  uno o dos días, en bucle continuo. Los ítems sin ningún adjunto se detectan
  aparte, en cada corrida, con un filtro nativo de DSpace (rápido, sin
  recorrer nada).
- **Se puede correr manualmente**: pestaña **Actions** → "Chequear adjuntos
  rotos RDU" → **Run workflow**.
- El reporte y el checkpoint se commitean solos al repo en cada corrida.

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
