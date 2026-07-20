# Guía para poner en marcha el robot — explicada bien fácil

Imaginá que vas a armar un robot que entra solo a la página de la UNC, ordena
los datos de cada trabajo, y anota lo que hizo en una planilla. El robot vive
en internet (en GitHub), así que **no necesitás tener tu compu prendida**.

Para que el robot funcione necesita 3 "llaves" (contraseñas especiales) y que
le digas dónde trabajar. Vamos una por una. Tomate tu tiempo, no hay apuro.

---

## Paso 1 — Crear tu cuenta de robot en GitHub (donde vive el robot)

1. Entrá a **github.com** y creá una cuenta (o entrá si ya tenés, tu usuario es
   `poonkrocker`).
2. Arriba a la derecha, tocá el **+** y elegí **"New repository"** (repositorio
   nuevo = la cajita donde va a vivir el robot).
3. Ponele un nombre, por ejemplo `rdu-agent`.
4. **MUY IMPORTANTE**: elegí la opción **"Private"** (privado). Esto hace que
   nadie más pueda ver tu robot.
5. Tocá **"Create repository"**.

Después vas a subir ahí los archivos del `.zip` que te di (descomprimís el zip
y arrastrás las carpetas `scripts`, `.github` y los archivos sueltos adentro
del repositorio, usando el botón "uploading an existing file").

---

## Paso 2 — Conseguir la llave de DeepSeek (el cerebro barato del robot)

El robot necesita un "cerebro" que piense cómo ordenar los textos. Vamos a
usar DeepSeek porque es muy barato.

1. Entrá a **platform.deepseek.com** y creá una cuenta.
2. Buscá la sección que dice **"API Keys"** (llaves de API).
3. Tocá **"Create new API key"** y se va a generar un texto largo que empieza
   con `sk-...`.
4. **Copialo y guardalo en un lugar seguro** (un bloc de notas), porque
   después no lo vas a poder ver de nuevo.
5. DeepSeek te va a pedir cargar unos pocos dólares de saldo para usarlo
   (con 5 dólares te alcanza para muchísimos trabajos — cada trabajo cuesta
   centavos).

> Esta es la **llave 1**: `DEEPSEEK_API_KEY`

---

## Paso 3 — Conseguir el permiso para escribir en tu Google Sheets

El robot necesita permiso para anotar en tu planilla. Para eso Google usa una
especie de "robot ayudante" con su propio email.

1. Entrá a **console.cloud.google.com** (con tu cuenta de Google).
2. Arriba, creá un **proyecto nuevo** (botón "Select a project" → "New
   project"). Ponele cualquier nombre, por ejemplo `robot-rdu`.
3. Con el proyecto creado, en el buscador de arriba escribí **"Google Sheets
   API"**, entrá y tocá **"Enable"** (habilitar).
4. Ahora en el buscador escribí **"Service Accounts"** (cuentas de servicio) y
   entrá.
5. Tocá **"Create service account"**. Ponele un nombre como `robot-escribe` y
   dale a "Create and continue", después "Done".
6. Vas a ver el robot ayudante en la lista. Tocá sobre él, andá a la pestaña
   **"Keys"** (llaves) → **"Add key"** → **"Create new key"** → elegí
   **JSON** → "Create". Se te va a **descargar un archivo** que termina en
   `.json`. Guardalo bien.
7. **Abrí ese archivo .json con el bloc de notas**: vas a ver un email que
   parece `robot-escribe@robot-rdu.iam.gserviceaccount.com`. **Copiá ese
   email.**
8. Andá a tu **Google Sheets**, tocá "Compartir" (arriba a la derecha), pegá
   ese email y dale permiso de **Editor**. Esto le da permiso al robot para
   escribir.

> Esta es la **llave 2**: `GOOGLE_SERVICE_ACCOUNT_JSON` (es **todo** el
> contenido del archivo .json — lo copiás entero, desde la primera `{` hasta
> la última `}`).
>
> Y guardate también el **número de tu planilla**, que está en el link entre
> `/d/` y `/edit`:
> `1mGP8zORmKlicrbNyFw5xlrDG3ddIn0-Qrcu7dxp9GQw`
> Esa es la **llave 3**: `GOOGLE_SHEET_ID`

---

## Paso 4 — Preparar tu planilla con dos pestañas

En tu Google Sheets:

1. **Pestaña principal** (la que ya tenés): que tenga en la fila 1 estas
   columnas: `Título`, `Link`, `Tipo`, `Estado`, `Fecha`. Acá el robot va a
   ir anotando lo que termina.
2. **Pestaña nueva**: creala abajo con el **+** y llamala exactamente `Cola`.
   En la fila 1 poné dos columnas: `Link` y `Estado`. Acá vas a **pegar los
   links de los trabajos** que querés que el robot haga (uno por fila, en la
   columna Link). La columna Estado la dejás vacía: el robot la completa solo.

---

## Paso 5 — Guardar las llaves dentro de GitHub (la caja fuerte)

Las llaves NO se escriben en el código (sería como dejar la contraseña pegada
en la puerta). GitHub tiene una "caja fuerte" para guardarlas:

1. En tu repositorio de GitHub, andá a **"Settings"** (la rueda dentada,
   arriba).
2. En el menú de la izquierda: **"Secrets and variables"** → **"Actions"**.
3. Tocá **"New repository secret"** y cargá una por una estas cajitas
   (nombre exacto arriba, valor abajo):

   | Nombre (copiá tal cual) | Qué poner adentro |
   |---|---|
   | `DEEPSEEK_API_KEY` | la llave que empieza con `sk-` del Paso 2 |
   | `GOOGLE_SHEET_ID` | `1mGP8zORmKlicrbNyFw5xlrDG3ddIn0-Qrcu7dxp9GQw` |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | todo el contenido del archivo .json del Paso 3 |
   | `RDU_USER` | tu usuario/email para entrar a RDU |
   | `RDU_PASS` | tu contraseña de RDU |

   (Si algún día querés usar Claude en vez de DeepSeek, agregás también
   `ANTHROPIC_API_KEY`. Por ahora no hace falta.)

---

## Paso 6 — Probar el robot a mano (antes de dejarlo solo)

Nunca dejes un robot trabajando solo sin probarlo primero con UN trabajo.

1. Poné **un solo link** de prueba en la pestaña `Cola` de tu planilla.
2. En GitHub, andá a la pestaña **"Actions"** (arriba).
3. Elegí **"Procesar items RDU"** en la lista de la izquierda.
4. A la derecha vas a ver un botón **"Run workflow"** → tocalo → confirmá.
5. Esperá unos minutos. Vas a ver una rueda girando y después un tilde verde
   (salió bien) o una cruz roja (algo falló).
6. Si salió verde: andá a tu planilla y fijate que se haya anotado la fila.
7. Si salió rojo: tocá sobre la corrida para ver el mensaje de error y me lo
   pasás, lo resolvemos juntos.

---

## Paso 7 — Dejar que trabaje solo todos los días

Una vez que la prueba sale bien, el robot ya está programado para correr solo
**todos los días a las 6 de la mañana** (hora Argentina). Vos solo tenés que
ir cargando links en la pestaña `Cola` cuando quieras que procese cosas.

Si querés cambiar el horario, se cambia una línea en el archivo
`.github/workflows/procesar-items.yml` (en el README está explicado cómo).

---

## Lo más importante de recordar

- El robot ordena y anota, pero **no decide solo cuando hay dudas**: si no
  está seguro de una filiación o el PDF no coincide, deja ese trabajo marcado
  como "PENDIENTE_REVISION" para que lo mires vos. Eso es a propósito, para no
  meter datos mal en el repositorio.
- **Vos siempre revisás al final** y ponés tu marca `[REVISADO]` a mano. El
  robot nunca pone esa marca.
- La parte que todavía hay que ajustar juntos son los "selectores" (cómo el
  robot encuentra cada casillero en la página de RDU). Por eso el Paso 6
  (probar con uno) es tan importante: ahí vamos a ver si encuentra bien los
  campos o hay que corregirlos.
