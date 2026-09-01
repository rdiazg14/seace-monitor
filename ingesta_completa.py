#!/usr/bin/env python3
"""
Ingesta completa del corpus de contrataciones menores del SEACE.

Modos:
  COMPLETA     Primera corrida o --forzar-completa. Descarga los ~76k registros.
  INCREMENTAL  Corridas siguientes. Detecta MAX(id) en Supabase (o parquet local)
               y solo baja lo publicado después de ese punto.

Salidas:
  Supabase  tabla 'contratos' via UPSERT — si SUPABASE_URL y
            SUPABASE_SERVICE_KEY están en el entorno.
  data/seace_menores_completo.parquet   backup local (snappy)
  data/seace_menores_completo.csv       utf-8-sig; si >100 MB solo últimas 1000 filas
  data/ultima_ingesta.txt               timestamp + estadísticas
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

# ── Configuración ──────────────────────────────────────────────────────────
URL_SPA  = "https://prod6.seace.gob.pe/buscador-publico/contrataciones"
API_BASE = (
    "https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico"
    "/contrataciones/buscador"
)
ANIO        = datetime.now().year
PAGE_SIZE   = 100
BATCH_SIZE  = 500                   # registros por lote de upsert a Supabase
CSV_LIMITE  = 100 * 1024 * 1024    # 100 MiB
CSV_MUESTRA = 1_000

OUT_PARQUET = "data/seace_menores_completo.parquet"
OUT_CSV     = "data/seace_menores_completo.csv"
OUT_LOG     = "data/ultima_ingesta.txt"

# Credenciales (GitHub Secrets en Actions; .env local en desarrollo)
SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# ── Clasificación: categoría IT ────────────────────────────────────────────
# Primera coincidencia en la lista gana (orden = prioridad).
IT_CATS: list[tuple[str, list[str]]] = [
    ("Firma digital", [
        "firma digital", "certificado digital", "certificado electronico",
        "token criptografico",
    ]),
    ("IA/analytics", [
        "inteligencia artificial", "machine learning", "ia generativa",
        "chatbot", "asistente virtual", "llm", "gpt", "copilot",
        "gemini", "claude", "openai", "azure openai",
        "analytics", "business intelligence", "ciencia de datos", "big data",
        "procesamiento de lenguaje", "red neuronal", "deep learning",
        "tokens de procesamiento",
    ]),
    ("Ciberseguridad", [
        "ciberseguridad", "seguridad informatica", "seguridad de la informacion",
        "firewall", "pentest", "ethical hacking",
    ]),
    ("Cloud/hosting", [
        "nube publica", "cloud computing", "hosting", "servidor virtual",
        " aws ", "google cloud",
    ]),
    ("Microsoft", [
        "microsoft", "office 365", "microsoft 365",
        "sharepoint", "exchange", "windows server",
    ]),
    ("Oracle", ["oracle database", "oracle ebs", "peoplesoft"]),
    ("Base de datos/ERP", [
        "base de datos", "sql server", "postgresql", "mysql", "mongodb",
        "data warehouse", " sap ", " erp ",
    ]),
    ("Desarrollo software", [
        "desarrollo de software", "desarrollo de sistema",
        "sistema de informacion", "aplicativo", "software a medida",
        "plataforma web", "portal web", "sistema web",
        "sistema administrativo", "aplicacion movil", "app movil",
        "implementacion de software",
    ]),
    ("Licencias", [
        "licencia de software", "licenciamiento", "suscripcion de software",
    ]),
    ("Soporte tecnico", [
        "soporte tecnico", "mantenimiento de software",
        "mantenimiento de sistema", "mesa de ayuda", "helpdesk", "help desk",
    ]),
    ("Redes/cableado", [
        "red de datos", "cableado estructurado", " switch ", "router",
        "fibra optica", " wifi", "wireless", "access point", "punto de acceso",
    ]),
    ("Correo electronico", [
        "correo electronico", "mensajeria electronica",
    ]),
    ("Hardware", [
        "computadora", "laptop", "impresora", " monitor ", "disco duro",
        "memoria ram", " ups ", "proyector", " tablet ",
        "equipos informaticos", "equipos de computo", "scanner", "escaner",
    ]),
]

# Relevancia IA
KW_ALTA = [
    "token", "azure openai", "openai", "gpt", "llm",
    "claude", "copilot", "gemini",
]
KW_GENERICOS = [
    "inteligencia artificial", "ia generativa", "chatbot", "asistente virtual",
    "machine learning", "aprendizaje automatico", "procesamiento de lenguaje",
    "vision computacional", "deep learning", "red neuronal",
    "modelo de lenguaje", "ciencia de datos", "big data",
]
_KW_LIMITE_PALABRA: set[str] = {"ia"}


def _norm(texto: str) -> str:
    """Minúsculas y sin tildes."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower()


def _contiene(texto_norm: str, kw: str) -> bool:
    kn = _norm(kw)
    if kn in _KW_LIMITE_PALABRA:
        return bool(re.search(r"\b" + re.escape(kn) + r"\b", texto_norm))
    return kn in texto_norm


def _texto_contrato(r: dict) -> str:
    """Concatena campos de texto de un registro API para clasificación."""
    return " " + " ".join(
        _norm(str(r.get(k, "")))
        for k in ("desObjetoContrato", "desContratacion",
                  "nomObjetoContrato", "nomEntidad")
    ) + " "


def clasificar_categoria_it(r: dict) -> str | None:
    t = _texto_contrato(r)
    for cat, kws in IT_CATS:
        if any(_contiene(t, kw) for kw in kws):
            return cat
    return None


def clasificar_relevancia_ia(r: dict) -> str | None:
    t = _texto_contrato(r)
    if any(_contiene(t, kw) for kw in KW_ALTA):
        return "ALTA"
    gen = [kw for kw in KW_GENERICOS if _contiene(t, kw)]
    if len(gen) >= 2:
        return "MEDIA"
    if len(gen) == 1:
        return "BAJA"
    return None


_FMT_SEACE = "%d/%m/%Y %H:%M:%S"


def parsear_fecha(s: str | None) -> str | None:
    """'dd/mm/yyyy HH:MM:SS' → ISO 8601 con offset UTC para PostgreSQL."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), _FMT_SEACE).isoformat() + "+00:00"
    except Exception:
        return None


class RegistroSeace(BaseModel):
    """Campos que la ingesta necesita. Extra se permite (la API manda más)."""
    model_config = ConfigDict(extra="allow")

    idContrato: int
    nroContratacion: int | str | None = None
    desContratacion: str | None = None
    nomObjetoContrato: str | None = None
    desObjetoContrato: str | None = None
    nomEntidad: str | None = None
    nomEstadoContrato: str | None = None
    fecPublica: str | None = None
    fecIniCotizacion: str | None = None
    fecFinCotizacion: str | None = None
    idTipoCotizacion: int | str | None = None
    cotizar: bool | None = None

    @field_validator("idContrato")
    @classmethod
    def id_positivo(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("idContrato debe ser > 0")
        return v

    @field_validator(
        "desContratacion", "nomObjetoContrato", "desObjetoContrato",
        "nomEntidad", "nomEstadoContrato",
        "fecPublica", "fecIniCotizacion", "fecFinCotizacion",
        mode="before",
    )
    @classmethod
    def vacio_a_none(cls, v):
        if v is None:
            return None
        if isinstance(v, (dict, list)):
            raise ValueError("se esperaba texto, llegó estructura")
        s = str(v).strip()
        return s or None


def _id_contrato_de(payload: dict) -> int | None:
    try:
        n = int(payload.get("idContrato"))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


# Nunca persistir contexto de sesión Playwright / HTTP.
_KEYS_SESION = {
    "cookie", "cookies", "authorization", "token", "access_token",
    "refresh_token", "set-cookie", "headers", "header", "csrf",
    "x-csrf-token", "api_key", "apikey", "session", "playwright",
    "request", "response",
}


def payload_solo_datos(registro: dict) -> dict:
    """Copia el registro de la API. Sin cookies, headers ni tokens."""
    out: dict = {}
    for k, v in registro.items():
        lk = str(k).lower()
        if lk in _KEYS_SESION or "cookie" in lk or "token" in lk:
            continue
        if lk.startswith("authorization") or lk.startswith("x-"):
            continue
        out[k] = v
    return out


def registrar_rechazo(
    client,
    payload: dict,
    motivo: str,
    origen: str = "ingesta",
) -> None:
    if client is None:
        print(f"  [rechazo] (sin supabase) {motivo[:180]}", flush=True)
        return
    datos = payload_solo_datos(payload) if isinstance(payload, dict) else {"_raw": str(payload)[:2000]}
    fila = {
        "id_contrato": _id_contrato_de(datos),
        "origen": origen,
        "motivo": (motivo or "invalido")[:2000],
        "payload": datos,
        "resuelto": False,
    }
    try:
        client.table("ingesta_rechazados").insert(fila).execute()
    except Exception as e:
        print(
            f"  [rechazo] no persistido ({e}). "
            f"El registro NO entra a contratos. "
            f"Si falta la tabla, ejecuta ingesta_rechazados.sql",
            flush=True,
        )


def filtrar_validos(raw: list[dict], client) -> tuple[list[dict], int]:
    """Devuelve (aceptados, n_rechazados). Los inválidos van a ingesta_rechazados."""
    ok: list[dict] = []
    n_rech = 0
    for r in raw:
        if not isinstance(r, dict):
            n_rech += 1
            registrar_rechazo(client, {"_raw": r}, "registro no es un objeto JSON")
            continue
        try:
            RegistroSeace.model_validate(r)
            ok.append(r)
        except ValidationError as e:
            n_rech += 1
            registrar_rechazo(client, r, str(e))
    return ok, n_rech


def preparar_fila_db(r: dict) -> dict:
    """Convierte un registro de la API SEACE al esquema de la tabla contratos."""
    return {
        "id":                   r["idContrato"],
        "nro_contratacion":     str(r.get("nroContratacion", "")),
        "descripcion_contrato": r.get("desContratacion"),
        "objeto":               r.get("nomObjetoContrato"),
        "descripcion":          r.get("desObjetoContrato"),
        "entidad":              r.get("nomEntidad"),
        "estado":               r.get("nomEstadoContrato"),
        "fecha_publica":        parsear_fecha(r.get("fecPublica")),
        "fecha_ini_cotizacion": parsear_fecha(r.get("fecIniCotizacion")),
        "fecha_fin_cotizacion": parsear_fecha(r.get("fecFinCotizacion")),
        "tipo_cotizacion":      str(r.get("idTipoCotizacion", "")),
        "cotizar":              bool(r.get("cotizar", False)),
        "categoria_it":         clasificar_categoria_it(r),
        "relevancia_ia":        clasificar_relevancia_ia(r),
    }


# ── Supabase ────────────────────────────────────────────────────────────────

def init_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[supabase] variables de entorno no configuradas — solo CSV/parquet.")
        return None
    try:
        from supabase import create_client
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        print("[supabase] cliente inicializado OK")
        return client
    except ImportError:
        print("[aviso] paquete 'supabase' no instalado.")
        return None
    except Exception as e:
        print(f"[aviso] error al conectar Supabase: {e}")
        return None


def get_max_id_supabase(client) -> int:
    try:
        res = (
            client.table("contratos")
            .select("id")
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            return int(res.data[0]["id"])
    except Exception as e:
        print(f"  [supabase] no se pudo obtener MAX(id): {e}")
    return 0


def _upsert_lote(client, lote: list[dict], reintentos: int = 3):
    for i in range(reintentos):
        try:
            client.table("contratos").upsert(lote, on_conflict="id").execute()
            return
        except Exception as e:
            if i < reintentos - 1:
                espera = 2 ** (i + 1)
                print(f"  [retry {i+1}/{reintentos-1}] {e} — espero {espera}s")
                time.sleep(espera)
            else:
                raise


def upsert_supabase(client, filas: list[dict]) -> int:
    total   = len(filas)
    n_lotes = -(-total // BATCH_SIZE)
    errores = 0
    print(f"[supabase] UPSERT {total:,} registros en {n_lotes} lotes de {BATCH_SIZE}...")
    t0 = time.time()
    for i in range(0, total, BATCH_SIZE):
        lote = filas[i: i + BATCH_SIZE]
        num  = i // BATCH_SIZE + 1
        try:
            _upsert_lote(client, lote)
            elapsed = time.time() - t0
            eta = (n_lotes - num) * (elapsed / num)
            print(f"  lote {num}/{n_lotes} ({len(lote)} filas) OK "
                  f"[{elapsed:.0f}s ~{eta:.0f}s restantes]")
        except Exception as e:
            print(f"  lote {num}/{n_lotes} ERROR: {e}")
            errores += 1
    print(f"[supabase] completado en {time.time()-t0:.0f}s — "
          f"errores: {errores}/{n_lotes} lotes")
    return errores


# ── Descarga desde API SEACE ─────────────────────────────────────────────────

def _api_call(page, page_num: int) -> tuple[int, list[dict], int]:
    r = page.request.get(
        API_BASE,
        params={
            "anio": ANIO, "palabra_clave": "",
            "orden": 2, "page": page_num, "page_size": PAGE_SIZE,
        },
        timeout=60_000,
    )
    if r.status != 200:
        return 0, [], r.status
    j = r.json()
    return (
        j.get("pageable", {}).get("totalElements", 0),
        j.get("data", []) or [],
        200,
    )


# ── Parquet / CSV ────────────────────────────────────────────────────────────

def _leer_parquet() -> pd.DataFrame:
    if os.path.exists(OUT_PARQUET):
        try:
            df = pd.read_parquet(OUT_PARQUET, engine="pyarrow")
            print(f"[parquet] existente: {len(df):,} filas, "
                  f"max idContrato={df['idContrato'].max()}")
            return df
        except Exception as e:
            print(f"[aviso] parquet no legible ({e})")
    return pd.DataFrame()


def _guardar_csv(df: pd.DataFrame):
    est = df.memory_usage(deep=True).sum()
    if est > CSV_LIMITE:
        sub = df.sort_values("idContrato", ascending=False).head(CSV_MUESTRA)
        sub.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
        sz = os.path.getsize(OUT_CSV) / 1024
        print(f"[csv] corpus > 100 MiB → muestra de {CSV_MUESTRA} filas "
              f"({sz:.0f} KiB)")
    else:
        df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
        sz = os.path.getsize(OUT_CSV) / 1024 / 1024
        print(f"[csv] {OUT_CSV} → {sz:.1f} MiB ({len(df):,} filas)")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Ingesta corpus SEACE → Supabase + parquet + CSV"
    )
    ap.add_argument("--forzar-completa", action="store_true",
                    help="ignora max_id y re-descarga todo el corpus")
    ap.add_argument("--headed", action="store_true",
                    help="navegador visible (útil para depurar)")
    ap.add_argument("--simular-rechazo", action="store_true",
                    help="G2: inserta un payload inválido en ingesta_rechazados y sale")
    args = ap.parse_args()
    os.makedirs("data", exist_ok=True)

    # ── 1. Iniciar Supabase ────────────────────────────────────────────
    supa = init_supabase()

    if args.simular_rechazo:
        fake = {
            "desObjetoContrato": "SIMULACION G2 — sin idContrato",
            "nomEntidad": "ENTIDAD DE PRUEBA",
            "nomEstadoContrato": {"inesperado": True},
        }
        print("G2 --simular-rechazo: payload inválido a propósito", flush=True)
        print(json.dumps(fake, ensure_ascii=False), flush=True)
        try:
            RegistroSeace.model_validate(fake)
            raise SystemExit("ERROR: el payload simulado no debería pasar el esquema")
        except ValidationError as e:
            motivo = str(e)
            print(f"  ValidationError:\n{motivo}", flush=True)
            registrar_rechazo(supa, fake, motivo, origen="ingesta")
        if not supa:
            raise SystemExit("ERROR: sin Supabase; el rechazo no se persistió")
        try:
            res = (
                supa.table("ingesta_rechazados")
                .select("id,id_contrato,origen,motivo,payload,resuelto,created_at")
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as e:
            raise SystemExit(
                f"ERROR: no pude leer ingesta_rechazados ({e}). "
                "Pega ingesta_rechazados.sql en Supabase → SQL Editor y reintenta."
            )
        row = (res.data or [None])[0]
        if not row:
            raise SystemExit(
                "ERROR: no hay fila en ingesta_rechazados. "
                "Pega ingesta_rechazados.sql en Supabase → SQL Editor y reintenta."
            )
        print("\nFila persistida:", flush=True)
        print(json.dumps(row, ensure_ascii=False, indent=2, default=str), flush=True)
        return

    # ── 2. Determinar punto de partida (incremental vs completo) ───────
    max_id = 0
    if not args.forzar_completa:
        if supa:
            max_id = get_max_id_supabase(supa)
            print(f"[incremental] MAX id en Supabase: {max_id:,}")
        else:
            df_local = _leer_parquet()
            if not df_local.empty:
                max_id = int(df_local["idContrato"].max())
                print(f"[incremental] MAX id en parquet local: {max_id:,}")

    modo = "INCREMENTAL" if max_id > 0 else "COMPLETA"
    print(f"[modo] {modo}" + (f" — solo id > {max_id:,}" if max_id else ""))

    # ── 3. Descarga desde el SEACE ────────────────────────────────────
    t0 = time.time()
    nuevas_raw: list[dict] = []
    alerta_anomala = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_context(ignore_https_errors=True).new_page()
        print("Iniciando SPA...")
        page.goto(URL_SPA, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(2_000)

        total_api  = 0
        total_pags = 1
        pagina     = 1
        stop       = False

        while not stop:
            total_api, lote, status = _api_call(page, pagina)
            if status != 200:
                print(f"[error] pagina {pagina}: HTTP {status}")
                alerta_anomala = 1
                break
            if pagina == 1:
                total_pags = -(-total_api // PAGE_SIZE) if total_api else 0
                print(f"totalElements={total_api:,}  paginas={total_pags}")
                if total_api == 0:
                    alerta_anomala = 1

            if max_id > 0:
                nuevos = [r for r in lote if r.get("idContrato", 0) > max_id]
                if len(nuevos) < len(lote):
                    nuevas_raw.extend(nuevos)
                    print(f"  p{pagina}: {len(nuevos)} nuevos "
                          f"({len(lote)-len(nuevos)} ya conocidos) → STOP")
                    stop = True
                    break
                nuevas_raw.extend(nuevos)
            else:
                nuevas_raw.extend(lote)

            elapsed = time.time() - t0
            rate = pagina / max(elapsed, 1)
            eta  = (total_pags - pagina) / rate
            print(f"  p{pagina}/{total_pags} +{len(lote)} "
                  f"acum={len(nuevas_raw):,} {elapsed:.0f}s ~{eta:.0f}s")

            if not lote or pagina >= total_pags:
                break
            pagina += 1

        browser.close()

    print(f"\nDescarga: {len(nuevas_raw):,} registros en {time.time()-t0:.0f}s")

    if not nuevas_raw and max_id == 0:
        alerta_anomala = 1
        print("ERROR: sin registros.", file=sys.stderr)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(OUT_LOG, "w", encoding="utf-8") as fh:
            fh.write(
                f"{ts}\n"
                f"total_registros=0\n"
                f"nuevos_esta_corrida=0\n"
                f"rechazados_esta_corrida=0\n"
                f"alerta_anomala=1\n"
            )
        sys.exit(1)

    # ── 4. Validar (G2) + clasificar ───────────────────────────────────
    n_rech = 0
    if nuevas_raw:
        print("Validando esquema (G2)...", flush=True)
        nuevas_raw, n_rech = filtrar_validos(nuevas_raw, supa)
        print(f"  aceptados={len(nuevas_raw):,}  rechazados={n_rech:,}", flush=True)
        print("Clasificando registros...")
        filas_db: list[dict] = []
        for r in nuevas_raw:
            try:
                filas_db.append(preparar_fila_db(r))
            except Exception as e:
                n_rech += 1
                registrar_rechazo(supa, r, f"preparar_fila_db: {e}")
        n_it = sum(1 for f in filas_db if f["categoria_it"])
        n_ia = sum(1 for f in filas_db if f["relevancia_ia"])
        print(f"  categoria_it asignada: {n_it:,}  |  relevancia_ia: {n_ia:,}")
    else:
        filas_db = []
        print("Sin registros nuevos. Corpus al día.")

    # ── 5. Upsert a Supabase ───────────────────────────────────────────
    if supa and filas_db:
        upsert_supabase(supa, filas_db)

    # ── 6. Guardar parquet + CSV (backup local) ────────────────────────
    df_nuevo = pd.DataFrame(nuevas_raw) if nuevas_raw else pd.DataFrame()
    df_exist = _leer_parquet()

    if not df_nuevo.empty:
        if not df_exist.empty:
            df_final = pd.concat([df_exist, df_nuevo], ignore_index=True)
            df_final = df_final.drop_duplicates("idContrato")
            print(f"[merge] total parquet tras merge: {len(df_final):,} filas")
        else:
            df_final = df_nuevo
    else:
        df_final = df_exist

    if not df_final.empty:
        df_final.to_parquet(OUT_PARQUET, engine="pyarrow",
                            compression="snappy", index=False)
        pq_sz = os.path.getsize(OUT_PARQUET)
        print(f"[parquet] {OUT_PARQUET} → {pq_sz/1024/1024:.2f} MiB "
              f"({len(df_final):,} filas)")
        _guardar_csv(df_final)

    # ── 7. Log ────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    n_total = len(df_final) if not df_final.empty else 0
    with open(OUT_LOG, "w", encoding="utf-8") as fh:
        fh.write(
            f"{ts}\n"
            f"total_registros={n_total:,}\n"
            f"nuevos_esta_corrida={len(nuevas_raw):,}\n"
            f"rechazados_esta_corrida={n_rech}\n"
            f"alerta_anomala={alerta_anomala}\n"
        )
    print(f"[log] {ts}")

    print("\n===== RESUMEN FINAL =====")
    print(f"Total corpus local: {n_total:,}")
    print(f"Nuevos esta corrida: {len(nuevas_raw):,}")
    print(f"Rechazados (G2): {n_rech:,}")
    print(f"Supabase: {'✓ upsert completado' if supa and filas_db else '✗ no configurado o sin datos nuevos'}")
    print("=========================")


if __name__ == "__main__":
    main()
