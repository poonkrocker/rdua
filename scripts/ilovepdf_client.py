"""
Conversión de doc/docx a PDF usando la API de iLovePDF.

Flujo de la API (REST):
  1. auth: con la public key se obtiene un token JWT temporal.
  2. start: se pide un servidor de trabajo para la tarea 'officepdf'.
  3. upload: se sube el archivo doc/docx a ese servidor.
  4. process: se ejecuta la conversión.
  5. download: se descarga el PDF resultante (bytes).

Requiere el secret ILOVEPDF_PUBLIC_KEY en GitHub.
Docs: https://developer.ilovepdf.com/docs
"""
import os
import requests

PUBLIC_KEY = os.environ.get("ILOVEPDF_PUBLIC_KEY", "")
BASE = "https://api.ilovepdf.com/v1"


def _auth() -> str | None:
    if not PUBLIC_KEY:
        print("  [WARN] No hay ILOVEPDF_PUBLIC_KEY configurada; no se puede convertir.")
        return None
    try:
        r = requests.post(f"{BASE}/auth", json={"public_key": PUBLIC_KEY}, timeout=30)
        r.raise_for_status()
        return r.json()["token"]
    except Exception as e:
        print(f"  [WARN] Error autenticando con iLovePDF: {e}")
        return None


def convertir_a_pdf(nombre_archivo: str, contenido: bytes) -> bytes | None:
    """Convierte el contenido de un doc/docx (bytes) a PDF (bytes).
    Devuelve None si algo falla."""
    token = _auth()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}

    try:
        # 2. start: pedir servidor para la tarea officepdf.
        r = requests.get(f"{BASE}/start/officepdf", headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        server = data["server"]
        task = data["task"]

        # 3. upload: subir el archivo.
        files = {"file": (nombre_archivo, contenido)}
        r = requests.post(
            f"https://{server}/v1/upload",
            headers=headers,
            data={"task": task},
            files=files,
            timeout=120,
        )
        r.raise_for_status()
        server_filename = r.json()["server_filename"]

        # 4. process: ejecutar la conversión.
        r = requests.post(
            f"https://{server}/v1/process",
            headers=headers,
            json={
                "task": task,
                "tool": "officepdf",
                "files": [{"server_filename": server_filename, "filename": nombre_archivo}],
            },
            timeout=180,
        )
        r.raise_for_status()

        # 5. download: descargar el PDF resultante.
        r = requests.get(f"https://{server}/v1/download/{task}", headers=headers, timeout=120)
        r.raise_for_status()
        print(f"  [OK] iLovePDF convirtió '{nombre_archivo}' a PDF ({len(r.content)} bytes).")
        return r.content

    except Exception as e:
        print(f"  [WARN] Error en la conversión con iLovePDF: {e}")
        return None
