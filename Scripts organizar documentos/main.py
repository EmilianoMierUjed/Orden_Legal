"""
main.py
=====================
Organizador Jurídico Definitivo con Gemini Batch API.
Versión unificada y refactorizada para producción.

Proceso en 3 fases:
  Fase 1: Escanea la carpeta de entrada, sube archivos a Gemini Files API
           y genera el lote JSONL de solicitudes.
  Fase 2: Crea el trabajo Batch en la nube y lo monitorea.
  Fase 3: Descarga resultados, clasifica y mueve archivos físicamente.

Uso:
    pip install google-genai python-dotenv
    python main.py                        # Ejecuta todas las fases
    python main.py --fase 1               # Solo preparar
    python main.py --fase 2               # Solo monitorear el batch
    python main.py --fase 3               # Solo procesar resultados
    python main.py --entrada /ruta/docs   # Especificar carpeta de entrada
"""

import csv
import json
import os
import re
import shutil
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

from google import genai
from google.genai import types

from dotenv import load_dotenv

# ============================================================
# ██  CONFIGURACIÓN  ██
# ============================================================

load_dotenv()

# --- Seguridad: API Key desde .env ---
API_KEY = os.getenv("GEMINI_API_KEY", "")

# --- Rutas dinámicas (NUNCA hardcodeadas) ---
SCRIPTS_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.getenv("BASE_DIR", str(SCRIPTS_DIR.parent)))
CARPETA_ENTRADA = Path(os.getenv("CARPETA_ENTRADA", str(BASE_DIR / "DataRaw")))
CARPETA_DESTINO = Path(os.getenv("CARPETA_DESTINO", str(BASE_DIR / "Organizado")))

# --- Archivos de estado (siempre junto al script) ---
INVENTARIO_CSV = SCRIPTS_DIR / "inventario.csv"
BATCH_MAP_FILE = SCRIPTS_DIR / "batch_map.json"
BATCH_JOB_FILE = SCRIPTS_DIR / "batch_job.json"
BATCH_JSONL_FILE = SCRIPTS_DIR / "batch_requests.jsonl"
PROGRESO_FILE = SCRIPTS_DIR / "batch_progreso.json"

# --- Modelo de IA ---
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# --- Extensiones soportadas ---
EXTENSIONES_GEMINI = {".pdf", ".docx"}
EXTENSIONES_TEXTO = {".doc"}
EXTENSIONES_INTERES = EXTENSIONES_GEMINI | EXTENSIONES_TEXTO

# ============================================================
# ██  PROMPT PARA GEMINI  ██
# ============================================================

SYSTEM_PROMPT = """\
Eres un asistente jurídico experto en derecho mexicano. Tu tarea es clasificar documentos legales.

Instrucciones ESTRICTAS:
1. Analiza el documento y devuelve ÚNICAMENTE un objeto JSON válido, sin markdown, sin explicaciones, sin texto adicional.
2. El JSON debe tener exactamente estos 4 campos:

{
  "fecha": "YYYY-MM-DD",
  "tipo": "...",
  "materia": "...",
  "descripcion": "..."
}

Reglas para cada campo:
- fecha: La fecha más relevante del documento en formato YYYY-MM-DD. Si no hay fecha clara, usa "sin_fecha".
- tipo: SOLO una de estas opciones exactas: Demandas, Contestaciones, Sentencias, Acuerdos, Contratos, Otros
- materia: SOLO una de estas opciones exactas: Civil, Mercantil, Amparo, Administrativo, Penal, Sin_materia
- descripcion: Máximo 5 palabras que resuman el documento, usando guiones_bajos en lugar de espacios.

RESPONDE SOLO CON EL JSON. Nada más.
"""

TIPOS_VALIDOS = {"Demandas", "Contestaciones", "Sentencias", "Acuerdos", "Contratos", "Otros"}
MATERIAS_VALIDAS = {"Civil", "Mercantil", "Amparo", "Administrativo", "Penal", "Sin_materia"}


# ============================================================
# ██  UTILIDADES  ██
# ============================================================

def guardar_json(path: Path, data: Any) -> None:
    """Guarda datos como JSON de forma atómica."""
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def cargar_json(path: Path) -> Optional[dict]:
    """Carga datos desde JSON de forma segura."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [WARN] Error leyendo {path.name}: {e}")
        return None


def formato_tiempo(segundos: float) -> str:
    """Formatea segundos en formato legible."""
    if segundos < 60:
        return f"{int(segundos)}s"
    elif segundos < 3600:
        m, s = divmod(int(segundos), 60)
        return f"{m}m {s}s"
    else:
        h, resto = divmod(int(segundos), 3600)
        m = resto // 60
        return f"{h}h {m}m"


def extraer_texto_doc(ruta: Path) -> str:
    """Extrae texto de archivos .doc/.docx usando python-docx (fallback para .doc)."""
    try:
        from docx import Document
        doc = Document(str(ruta))
        textos = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        return "\n".join(textos)[:8000]
    except Exception as e:
        print(f"  [WARN] No se pudo leer {ruta.name}: {e}")
        return ""


def extraer_json_respuesta(respuesta_texto: str) -> Optional[dict]:
    """Intenta extraer un JSON válido de la respuesta del modelo."""
    texto = respuesta_texto.strip()
    texto = re.sub(r"^```(?:json)?\s*", "", texto)
    texto = re.sub(r"\s*```$", "", texto)
    texto = texto.strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", texto, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


def validar_clasificacion(data: dict) -> dict:
    """Valida y normaliza los campos del JSON devuelto por Gemini."""
    fecha = str(data.get("fecha", "sin_fecha"))
    tipo = data.get("tipo", "Otros")
    materia = data.get("materia", "Sin_materia")
    descripcion = str(data.get("descripcion", "documento"))

    if fecha != "sin_fecha" and not re.match(r"^\d{4}-\d{2}-\d{2}$", fecha):
        fecha = "sin_fecha"
    if tipo not in TIPOS_VALIDOS:
        tipo = "Otros"
    if materia not in MATERIAS_VALIDAS:
        materia = "Sin_materia"

    descripcion = re.sub(r"[^a-záéíóúñA-ZÁÉÍÓÚÑ0-9_\s]", "", descripcion)
    palabras = descripcion.split()[:5]
    descripcion = "_".join(palabras).lower() if palabras else "documento"

    return {"fecha": fecha, "tipo": tipo, "materia": materia, "descripcion": descripcion}


def extraer_texto_respuesta_batch(result: Any) -> tuple[str, str]:
    """
    Extrae de forma SEGURA el key y el texto de respuesta de un resultado batch.
    Maneja tanto formato dict (descargado de archivo) como objetos inline.
    Retorna (key, response_text).
    """
    key = ""
    response_text = ""

    if isinstance(result, dict):
        key = result.get("key", "")
        response_data = result.get("response", {})
        if not isinstance(response_data, dict):
            return key, ""

        # Navegación segura: response -> candidates[0] -> content -> parts[0] -> text
        candidates = response_data.get("candidates")
        if isinstance(candidates, list) and len(candidates) > 0:
            candidate = candidates[0]
            if isinstance(candidate, dict):
                content = candidate.get("content")
                if isinstance(content, dict):
                    parts = content.get("parts")
                    if isinstance(parts, list):
                        for part in parts:
                            if isinstance(part, dict) and "text" in part:
                                response_text += str(part["text"])
    else:
        # Resultado inline (objeto SDK)
        key = getattr(result, "key", "")
        resp = getattr(result, "response", None)
        if resp is not None:
            response_text = getattr(resp, "text", "")

    return key, response_text


# ============================================================
# ██  INVENTARIO INTEGRADO  ██
# ============================================================

def crear_inventario(carpeta_entrada: Path) -> list[dict]:
    """
    Escanea la carpeta de entrada y genera automáticamente el inventario CSV.
    Reemplaza por completo al antiguo inventario.py.
    """
    print(f"\n  Escaneando carpeta: {carpeta_entrada}")

    if not carpeta_entrada.exists():
        print(f"  [ERROR] La carpeta de entrada no existe: {carpeta_entrada}")
        print(f"  Configura CARPETA_ENTRADA en el archivo .env o usa --entrada")
        return []

    registros: list[dict] = []
    contador = 0

    for root, _dirs, files in os.walk(carpeta_entrada):
        for name in files:
            ruta_completa = Path(root) / name
            extension = ruta_completa.suffix.lower()

            if extension not in EXTENSIONES_INTERES:
                continue

            contador += 1
            registros.append({
                "id": str(contador),
                "file_name": name,
                "file_path": str(ruta_completa),
                "extension": extension,
                "es_interes": "True",
            })

    # Guardar CSV
    with open(INVENTARIO_CSV, "w", newline="", encoding="utf-8") as f:
        campos = ["id", "file_name", "file_path", "extension", "es_interes"]
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        writer.writerows(registros)

    print(f"  Archivos encontrados: {len(registros)}")
    print(f"  Inventario guardado:  {INVENTARIO_CSV.name}")
    return registros


def leer_inventario() -> list[dict]:
    """Lee el inventario CSV y retorna los registros de interés."""
    registros: list[dict] = []
    if not INVENTARIO_CSV.exists():
        return registros
    try:
        with open(INVENTARIO_CSV, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("es_interes", "").strip().lower() == "true":
                    ext = row.get("extension", "").lower()
                    if ext in EXTENSIONES_INTERES:
                        registros.append(row)
    except (OSError, csv.Error) as e:
        print(f"  [ERROR] No se pudo leer inventario: {e}")
    return registros


# ============================================================
# ██  FASE 1: PREPARAR SOLICITUDES  ██
# ============================================================

def _construir_jsonl_file(request_key: str, uri: str, mime: str) -> str:
    """Construye una línea JSONL para un archivo subido a Gemini."""
    return json.dumps({
        "key": request_key,
        "request": {
            "contents": [{
                "parts": [
                    {"text": "Clasifica el siguiente documento jurídico:"},
                    {"fileData": {"fileUri": uri, "mimeType": mime}}
                ]
            }],
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]}
        }
    }, ensure_ascii=False)


def _construir_jsonl_texto(request_key: str, texto: str) -> str:
    """Construye una línea JSONL para un documento de texto."""
    return json.dumps({
        "key": request_key,
        "request": {
            "contents": [{
                "parts": [{"text": f"Clasifica el siguiente documento jurídico:\n\n{texto}"}]
            }],
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]}
        }
    }, ensure_ascii=False)


def fase1_preparar(client: genai.Client, carpeta_entrada: Path) -> bool:
    """
    Escanea carpeta, sube archivos a Gemini Files API y genera JSONL.
    Incluye reanudación automática si se interrumpe.
    """
    print("\n" + "=" * 65)
    print("  FASE 1: PREPARAR SOLICITUDES BATCH")
    print("=" * 65)

    # Generar inventario automáticamente
    registros = crear_inventario(carpeta_entrada)
    if not registros:
        # Intentar leer inventario existente como fallback
        registros = leer_inventario()
        if not registros:
            print("  [ERROR] No se encontraron archivos para procesar.")
            return False

    # Cargar progreso previo (para reanudación)
    progreso = cargar_json(PROGRESO_FILE) or {"fase": 0, "archivos_subidos": {}, "errores_fase1": []}
    archivos_subidos: dict = progreso.get("archivos_subidos", {})

    total = len(registros)
    pendientes = total - len(archivos_subidos)
    print(f"\n  Archivos en inventario: {total}")
    print(f"  Ya subidos:            {len(archivos_subidos)}")
    print(f"  Pendientes:            {pendientes}")

    batch_map: dict[str, str] = {}
    jsonl_lines: list[str] = []
    errores: list[dict] = progreso.get("errores_fase1", [])

    for i, row in enumerate(registros, start=1):
        file_id = row["id"]
        file_path = Path(row["file_path"])
        ext = row.get("extension", "").lower()
        request_key = f"req-{file_id}"

        # Reanudación: si ya fue subido, reconstruir JSONL de los datos guardados
        if request_key in archivos_subidos:
            info = archivos_subidos[request_key]
            batch_map[request_key] = row["file_path"]

            if info.get("tipo") == "file":
                jsonl_lines.append(_construir_jsonl_file(request_key, info["uri"], info["mime"]))
            else:
                jsonl_lines.append(_construir_jsonl_texto(request_key, info.get("texto", "")))
            continue

        # Verificar que el archivo existe
        if not file_path.exists():
            print(f"  [{i}/{total}] ✗ No existe: {file_path.name}")
            errores.append({"id": file_id, "file": str(file_path), "error": "Archivo no existe"})
            continue

        print(f"  [{i}/{total}] Subiendo: {file_path.name}...", end=" ", flush=True)

        try:
            if ext in EXTENSIONES_GEMINI:
                mime_type = "application/pdf" if ext == ".pdf" else \
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

                uploaded = client.files.upload(
                    file=str(file_path),
                    config=types.UploadFileConfig(
                        display_name=f"doc-{file_id}",
                        mime_type=mime_type
                    )
                )

                archivos_subidos[request_key] = {
                    "tipo": "file",
                    "uri": uploaded.uri,
                    "mime": mime_type,
                    "name": uploaded.name
                }
                batch_map[request_key] = row["file_path"]
                jsonl_lines.append(_construir_jsonl_file(request_key, uploaded.uri, mime_type))
                print("✓")

            elif ext in EXTENSIONES_TEXTO:
                texto = extraer_texto_doc(file_path)
                if not texto.strip():
                    print("✗ (vacío)")
                    errores.append({"id": file_id, "file": str(file_path), "error": "Texto vacío"})
                    continue

                texto_truncado = texto[:4000]
                archivos_subidos[request_key] = {"tipo": "texto", "texto": texto_truncado}
                batch_map[request_key] = row["file_path"]
                jsonl_lines.append(_construir_jsonl_texto(request_key, texto))
                print("✓ (texto extraído)")

        except Exception as e:
            err_msg = str(e)[:200]
            print(f"✗ Error: {err_msg}")
            errores.append({"id": file_id, "file": str(file_path), "error": err_msg})

            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg.upper():
                print("  [429] Esperando 60s por cuota de uploads...")
                time.sleep(60)

        # Guardar progreso cada 50 archivos (para reanudación)
        if i % 50 == 0:
            progreso["archivos_subidos"] = archivos_subidos
            progreso["errores_fase1"] = errores
            guardar_json(PROGRESO_FILE, progreso)
            print(f"  --- Progreso guardado: {len(archivos_subidos)}/{total} ---")

    # Guardar JSONL
    with open(BATCH_JSONL_FILE, "w", encoding="utf-8") as f:
        for line in jsonl_lines:
            f.write(line + "\n")

    # Guardar mapa de solicitudes
    guardar_json(BATCH_MAP_FILE, batch_map)

    # Guardar progreso final
    progreso.update({
        "fase": 1,
        "archivos_subidos": archivos_subidos,
        "errores_fase1": errores,
        "total_solicitudes": len(jsonl_lines),
        "timestamp_fase1": datetime.now().isoformat()
    })
    guardar_json(PROGRESO_FILE, progreso)

    # Resumen
    print(f"\n{'=' * 65}")
    print(f"  FASE 1 COMPLETADA")
    print(f"  Solicitudes generadas: {len(jsonl_lines)}")
    print(f"  Errores:               {len(errores)}")
    print(f"  Archivo JSONL:         {BATCH_JSONL_FILE.name}")

    # Estimación de costo
    tokens_estimados = len(jsonl_lines) * 2000
    costo_input = (tokens_estimados / 1_000_000) * 0.15
    costo_output = (len(jsonl_lines) * 100 / 1_000_000) * 1.25
    costo_total = costo_input + costo_output
    print(f"\n  💰 Costo estimado: ~${costo_total:.4f} USD (Batch API, 50% descuento)")
    print(f"{'=' * 65}")

    return len(jsonl_lines) > 0


# ============================================================
# ██  FASE 2: ENVIAR BATCH  ██
# ============================================================

def fase2_enviar_batch(client: genai.Client) -> bool:
    """Sube JSONL y crea el trabajo batch. Monitorea hasta que termine."""
    print("\n" + "=" * 65)
    print("  FASE 2: ENVIAR Y MONITOREAR BATCH")
    print("=" * 65)

    # Verificar si ya hay un trabajo en curso (reanudación)
    job_info = cargar_json(BATCH_JOB_FILE)
    if job_info and job_info.get("name"):
        print(f"\n  Trabajo batch existente: {job_info['name']}")
        print("  Verificando estado...")

        try:
            batch_job = client.batches.get(name=job_info["name"])
            state = _obtener_estado(batch_job)
            print(f"  Estado actual: {state}")

            if state == "JOB_STATE_SUCCEEDED":
                print("  ✓ El trabajo ya terminó exitosamente!")
                job_info["state"] = state
                dest_file = _obtener_archivo_resultado(batch_job)
                if dest_file:
                    job_info["result_file"] = dest_file
                guardar_json(BATCH_JOB_FILE, job_info)
                return True
            elif state in ("JOB_STATE_RUNNING", "JOB_STATE_PENDING"):
                print("  El trabajo sigue en progreso. Monitoreando...")
                return _monitorear_batch(client, job_info["name"])
            else:
                print(f"  ✗ El trabajo anterior falló/expiró: {state}")
                print("  Creando nuevo trabajo...")
        except Exception as e:
            print(f"  ✗ Error verificando trabajo: {e}")
            print("  Creando nuevo trabajo...")

    # Verificar JSONL
    if not BATCH_JSONL_FILE.exists():
        print(f"\n  [ERROR] No se encontró {BATCH_JSONL_FILE.name}")
        print("  Ejecuta primero la Fase 1.")
        return False

    # Contar líneas
    with open(BATCH_JSONL_FILE, "r", encoding="utf-8") as f:
        num_requests = sum(1 for _ in f)
    print(f"\n  Solicitudes en JSONL: {num_requests}")

    # Subir JSONL a Gemini Files API
    print("  Subiendo archivo JSONL a Gemini Files API...", end=" ", flush=True)
    try:
        uploaded_jsonl = client.files.upload(
            file=str(BATCH_JSONL_FILE),
            config=types.UploadFileConfig(
                display_name="batch-clasificador-juridico",
                mime_type="jsonl"
            )
        )
        print("✓")
        print(f"  Archivo: {uploaded_jsonl.name}")
    except Exception as e:
        print(f"✗\n  Error subiendo JSONL: {e}")
        return False

    # Crear trabajo batch
    print(f"  Creando trabajo batch con modelo {GEMINI_MODEL}...", end=" ", flush=True)
    try:
        batch_job = client.batches.create(
            model=GEMINI_MODEL,
            src=uploaded_jsonl.name,
            config={
                "display_name": f"clasificador-juridico-{datetime.now().strftime('%Y%m%d-%H%M')}",
            },
        )
        print("✓")
        print(f"  Trabajo: {batch_job.name}")
    except Exception as e:
        print(f"✗\n  Error creando batch: {e}")
        return False

    # Guardar info del trabajo (reanudación)
    job_info = {
        "name": batch_job.name,
        "created": datetime.now().isoformat(),
        "num_requests": num_requests,
        "model": GEMINI_MODEL,
    }
    guardar_json(BATCH_JOB_FILE, job_info)

    return _monitorear_batch(client, batch_job.name)


def _obtener_estado(batch_job: Any) -> str:
    """Extrae el estado del batch de forma segura."""
    state = getattr(batch_job, "state", None)
    if state is None:
        return "UNKNOWN"
    return state.name if hasattr(state, "name") else str(state)


def _obtener_archivo_resultado(batch_job: Any) -> Optional[str]:
    """Extrae el nombre del archivo de resultados de forma segura."""
    dest = getattr(batch_job, "dest", None)
    if dest is None:
        return None
    return getattr(dest, "file_name", None)


def _monitorear_batch(client: genai.Client, job_name: str) -> bool:
    """Monitorea el estado del batch hasta que termine."""
    print(f"\n  Monitoreando trabajo: {job_name}")
    print("  (El batch puede tardar hasta 24h, pero usualmente es mucho más rápido)")
    print("  Puedes cerrar este script y ejecutar 'python main.py --fase 2' para verificar")
    print("-" * 65)

    completed_states = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
    poll_interval = 30
    inicio = time.time()

    while True:
        try:
            batch_job = client.batches.get(name=job_name)
            state = _obtener_estado(batch_job)
            elapsed = formato_tiempo(time.time() - inicio)

            print(f"  [{elapsed}] Estado: {state}", end="")

            stats = getattr(batch_job, "batch_stats", None)
            if stats is not None:
                total_req = getattr(stats, "total_request_count", "?")
                success = getattr(stats, "success_request_count", "?")
                failed = getattr(stats, "failed_request_count", "?")
                print(f" | ✓{success} ✗{failed} / {total_req}", end="")

            print()

            if state in completed_states:
                job_info = cargar_json(BATCH_JOB_FILE) or {}
                job_info["state"] = state
                job_info["finished"] = datetime.now().isoformat()

                if state == "JOB_STATE_SUCCEEDED":
                    dest_file = _obtener_archivo_resultado(batch_job)
                    if dest_file:
                        job_info["result_file"] = dest_file
                    print("\n  ✓ ¡BATCH COMPLETADO EXITOSAMENTE!")
                else:
                    print(f"\n  ✗ Batch terminó con estado: {state}")
                    error = getattr(batch_job, "error", None)
                    if error:
                        print(f"  Error: {error}")
                        job_info["error"] = str(error)

                guardar_json(BATCH_JOB_FILE, job_info)
                return state == "JOB_STATE_SUCCEEDED"

            time.sleep(poll_interval)

        except KeyboardInterrupt:
            print("\n\n  ⚠ Monitoreo interrumpido. El batch sigue corriendo en la nube.")
            print("  Ejecuta 'python main.py --fase 2' para verificar el estado.")
            return False

        except Exception as e:
            print(f"  [WARN] Error de polling: {e}")
            time.sleep(poll_interval)


# ============================================================
# ██  FASE 3: PROCESAR RESULTADOS  ██
# ============================================================

def fase3_procesar_resultados(client: genai.Client) -> bool:
    """Descarga resultados del batch, clasifica y mueve archivos originales."""
    print("\n" + "=" * 65)
    print("  FASE 3: PROCESAR RESULTADOS Y MOVER ARCHIVOS")
    print("=" * 65)

    # Cargar info del trabajo
    job_info = cargar_json(BATCH_JOB_FILE)
    if not job_info or job_info.get("state") != "JOB_STATE_SUCCEEDED":
        print("\n  [ERROR] No hay un trabajo batch completado exitosamente.")
        print("  Ejecuta primero las Fases 1 y 2.")
        return False

    # Cargar mapa de archivos
    batch_map = cargar_json(BATCH_MAP_FILE)
    if not batch_map:
        print(f"\n  [ERROR] No se encontró {BATCH_MAP_FILE.name}")
        return False

    # Descargar resultados
    result_file_name = job_info.get("result_file")
    results: list[Any] = []

    if result_file_name:
        print(f"\n  Descargando resultados desde: {result_file_name}...")
        try:
            result_bytes = client.files.download(file=result_file_name)
            result_text = result_bytes.decode("utf-8")
            for line in result_text.splitlines():
                line = line.strip()
                if line:
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        print(f"  [WARN] Línea JSONL inválida ignorada")
        except Exception as e:
            print(f"  Error descargando resultados: {e}")
            return False
    else:
        # Intentar resultados inline
        try:
            batch_job = client.batches.get(name=job_info["name"])
            dest = getattr(batch_job, "dest", None)
            if dest is not None:
                inlined = getattr(dest, "inlined_responses", None)
                if inlined:
                    print("\n  Procesando resultados inline...")
                    results = list(inlined)
        except Exception as e:
            print(f"  Error obteniendo resultados inline: {e}")
            return False

    print(f"  Resultados obtenidos: {len(results)}")

    # Procesar cada resultado
    CARPETA_DESTINO.mkdir(parents=True, exist_ok=True)

    exitos = 0
    errores = 0
    log_resultados: list[dict] = []
    total_results = len(results)

    for i, result in enumerate(results, start=1):
        try:
            # Extraer key y texto de forma SEGURA
            key, response_text = extraer_texto_respuesta_batch(result)

            # Buscar archivo original
            file_path_str = batch_map.get(key, "")
            if not file_path_str:
                errores += 1
                log_resultados.append({"key": key, "status": "key_no_encontrada"})
                continue

            file_path = Path(file_path_str)

            # Parsear clasificación
            if not response_text:
                print(f"  [{i}/{total_results}] ✗ {file_path.name} → Sin respuesta")
                errores += 1
                log_resultados.append({"archivo": str(file_path), "status": "sin_respuesta"})
                continue

            clasificacion_raw = extraer_json_respuesta(response_text)
            if not clasificacion_raw:
                print(f"  [{i}/{total_results}] ✗ {file_path.name} → JSON inválido")
                errores += 1
                log_resultados.append({
                    "archivo": str(file_path),
                    "status": "json_invalido",
                    "respuesta": response_text[:200]
                })
                continue

            clasificacion = validar_clasificacion(clasificacion_raw)

            # Verificar que el archivo físico exista
            if not file_path.exists():
                print(f"  [{i}/{total_results}] ✗ {file_path.name} → No existe")
                errores += 1
                continue

            # Mover archivo a la carpeta organizada
            tipo = clasificacion["tipo"]
            materia = clasificacion["materia"]
            fecha = clasificacion["fecha"]
            descripcion = clasificacion["descripcion"]
            ext = file_path.suffix

            carpeta = CARPETA_DESTINO / tipo / materia
            carpeta.mkdir(parents=True, exist_ok=True)

            nombre_nuevo = f"{fecha}_{tipo}_{materia}_{descripcion}{ext}"
            ruta_destino = carpeta / nombre_nuevo

            # Evitar duplicados
            contador = 2
            while ruta_destino.exists():
                base = f"{fecha}_{tipo}_{materia}_{descripcion}_{contador}{ext}"
                ruta_destino = carpeta / base
                contador += 1

            shutil.move(str(file_path), str(ruta_destino))

            ruta_relativa = f"{tipo}/{materia}/{ruta_destino.name}"
            print(f"  [{i}/{total_results}] ✓ {file_path.name} → {ruta_relativa}")
            exitos += 1

            log_resultados.append({
                "archivo_original": str(file_path),
                "archivo_nuevo": str(ruta_destino),
                "clasificacion": clasificacion,
                "status": "ok"
            })

        except Exception as e:
            errores += 1
            nombre = file_path.name if "file_path" in dir() else "?"
            print(f"  [{i}/{total_results}] ✗ Error: {str(e)[:100]}")
            log_resultados.append({"archivo": nombre, "status": "error", "error": str(e)[:200]})

    # Guardar log de resultados
    log_path = CARPETA_DESTINO / "clasificacion_log.json"
    guardar_json(log_path, {
        "timestamp": datetime.now().isoformat(),
        "total_resultados": total_results,
        "exitos": exitos,
        "errores": errores,
        "resultados": log_resultados
    })

    # Resumen
    print(f"\n{'=' * 65}")
    print(f"  FASE 3 COMPLETADA")
    print(f"  Archivos movidos:  {exitos}")
    print(f"  Errores:           {errores}")
    print(f"  Log guardado en:   {log_path}")
    print(f"  Carpeta destino:   {CARPETA_DESTINO}")
    print(f"{'=' * 65}")

    # Actualizar progreso
    progreso = cargar_json(PROGRESO_FILE) or {}
    progreso.update({
        "fase": 3,
        "exitos": exitos,
        "errores": errores,
        "timestamp_fase3": datetime.now().isoformat()
    })
    guardar_json(PROGRESO_FILE, progreso)

    return True


# ============================================================
# ██  MAIN  ██
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Organizador Jurídico con Gemini Batch API"
    )
    parser.add_argument(
        "--fase", type=int, choices=[1, 2, 3], default=None,
        help="Ejecutar solo una fase específica (1=preparar, 2=enviar, 3=procesar)"
    )
    parser.add_argument(
        "--entrada", type=str, default=None,
        help="Ruta a la carpeta con los documentos a procesar"
    )
    args = parser.parse_args()

    # Determinar carpeta de entrada
    carpeta_entrada = Path(args.entrada) if args.entrada else CARPETA_ENTRADA

    print("=" * 65)
    print("  ORGANIZADOR JURÍDICO — GEMINI BATCH API")
    print(f"  Modelo:   {GEMINI_MODEL}")
    print(f"  Entrada:  {carpeta_entrada}")
    print(f"  Destino:  {CARPETA_DESTINO}")
    print("  50% descuento en costo vs API normal")
    print("=" * 65)

    # Validar API Key
    if not API_KEY or API_KEY == "PEGA_TU_API_KEY_AQUI":
        print("\n  [ERROR] Configura tu GEMINI_API_KEY en el archivo .env")
        print(f"  Archivo .env esperado en: {SCRIPTS_DIR / '.env'}")
        return

    # Crear cliente
    client = genai.Client(api_key=API_KEY)

    try:
        if args.fase == 1:
            fase1_preparar(client, carpeta_entrada)
        elif args.fase == 2:
            fase2_enviar_batch(client)
        elif args.fase == 3:
            fase3_procesar_resultados(client)
        else:
            # Ejecutar todas las fases con reanudación automática
            progreso = cargar_json(PROGRESO_FILE)
            fase_actual = progreso.get("fase", 0) if progreso else 0

            if fase_actual < 1:
                ok = fase1_preparar(client, carpeta_entrada)
                if not ok:
                    return

            if fase_actual < 2:
                ok = fase2_enviar_batch(client)
                if not ok:
                    return

            if fase_actual < 3:
                fase3_procesar_resultados(client)

    except KeyboardInterrupt:
        print("\n\n  ⚠ Interrumpido por el usuario.")
        print("  El progreso se guardó automáticamente.")
        print("  Vuelve a ejecutar para continuar desde donde se quedó.")

    except Exception as e:
        print(f"\n  [ERROR FATAL] {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
