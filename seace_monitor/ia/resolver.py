"""Resolución dinámica de configuración IA — espejo de IA-003 (``src/ia/resolver.ts``).

Lee ``ia_endpoints`` (con ``ia_modelos`` + ``ia_proveedores`` embebidos por
PostgREST) e ``ia_meta`` con la service key; descifra ``clave_cifrada`` con
``IA_MASTER_KEY`` en memoria (nunca a logs ni a caché compartida).

Caché: snapshot completo con TTL ~60 s (PLAN-001). ``version_config`` viaja en
cada entrada resuelta para trazabilidad. Cuando la tabla no existe o la lectura
falla, ``resolver`` devuelve ``None``: el caller conserva el camino por env.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

from seace_monitor.config import obtener_strip
from seace_monitor.ia.contracts import ModeloCfg
from seace_monitor.ia.crypto import descifrar_clave

TTL_S = 60.0
CORPUS_ENV = "gemini-emb001-1536"


class ConfigEmbeddingInsegura(RuntimeError):
    """No se puede demostrar compatibilidad del proveedor con el corpus."""

SELECT_ENDPOINTS = (
    "endpoint,hereda,config,activo,"
    "ia_modelos(modelo,tipo,dimensiones,espacio_vectorial,timeout_ms,"
    "capacidades,params,precio,activo,"
    "ia_proveedores(id,tipo_api,base_url,clave_cifrada,activo))"
)


def _get_http(url: str, headers: dict, timeout: float):
    """GET por defecto; inyectable en pruebas."""
    return httpx.get(url, headers=headers, timeout=timeout)


class ResolverCfg:
    """Resuelve la configuración efectiva de endpoints IA desde ``ia_*``.

    ``None`` = sin config dinámica (tabla ausente, fila inactiva o lectura
    fallida): el caller sigue por env como antes de IA-005.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        key: str | None = None,
        master_key: str | None = None,
        get: Callable | None = None,
        now: Callable[[], float] = time.monotonic,
        descifrar: Callable[[str, str], str] = descifrar_clave,
        ttl_s: float = TTL_S,
    ) -> None:
        self._url = (url if url is not None else obtener_strip("SUPABASE_URL")).rstrip("/")
        self._key = key if key is not None else obtener_strip("SUPABASE_SERVICE_KEY")
        self._master = (
            master_key if master_key is not None else obtener_strip("IA_MASTER_KEY")
        )
        self._get = get or _get_http
        self._now = now
        self._descifrar = descifrar or descifrar_clave
        self._ttl = ttl_s
        self._snapshot: dict | None = None
        self._avisado_fallo = False

    @staticmethod
    def _log(entrada: dict) -> None:
        print(json.dumps({"ia_cfg": 1, **entrada}, ensure_ascii=False), flush=True)

    def _fetch_json(self, path: str):
        if not self._key or not self._url:
            return None
        response = self._get(
            f"{self._url}/rest/v1/{path}",
            headers={
                "apikey": self._key,
                "Authorization": f"Bearer {self._key}",
            },
            timeout=15.0,
        )
        status = getattr(response, "status_code", 0) or 0
        if status < 200 or status >= 300:
            # Tabla aún no migrada / relación inexistente → camino por env.
            raise ErrorLectura(status, "lectura rechazada")
        try:
            return response.json()
        except ValueError:
            raise ErrorLectura(status, "cuerpo no-JSON") from None

    def _cargar(self) -> dict:
        mapa: dict[str, ModeloCfg] = {}
        if not self._key or not self._url:
            return {"map": mapa, "version": 0, "fetched": self._now()}
        meta = self._fetch_json(
            "ia_meta?clave=eq.version_config&select=valor"
        ) or []
        filas = self._fetch_json(
            f"ia_endpoints?select={SELECT_ENDPOINTS}&activo=eq.true"
        ) or []
        try:
            version = int((meta[0] or {}).get("valor") or 0)
        except (IndexError, TypeError, ValueError):
            version = 0
        try:
            meta_corpus = self._fetch_json(
                "ia_meta?clave=eq.corpus_embeddings&select=valor"
            )
        except ErrorLectura:
            meta_corpus = None
        corpus = ""
        if meta_corpus:
            valor = (meta_corpus[0] or {}).get("valor")
            corpus = valor if isinstance(valor, str) else ""
        if self._fetch_json("ia_meta?clave=eq.version_config&select=valor") != meta:
            raise ErrorLectura(409, "configuración cambió durante lectura")

        claves: dict[str, str | None] = {}
        por_endpoint = {
            f["endpoint"]: f for f in filas
            if isinstance(f, dict) and f.get("endpoint") and f.get("activo") is True
        }
        for fila in por_endpoint.values():
            modelo = fila.get("ia_modelos")
            proveedor = (modelo or {}).get("ia_proveedores")
            if not modelo or not proveedor:
                continue
            if not modelo.get("activo") or not proveedor.get("activo"):
                continue
            # Defensa en profundidad: el corpus activo fija el espacio de
            # embeddings (la regla anti-mezcla no depende solo del guardrail SQL).
            espacio = modelo.get("espacio_vectorial")
            if (
                fila["endpoint"] == "embeddings"
                and (not corpus or not espacio or espacio != corpus
                     or modelo.get("tipo") != "embedding"
                     or modelo.get("dimensiones") != 1536)
            ):
                self._log({
                    "aviso": "embeddings_fuera_de_espacio",
                    "espacio": espacio,
                    "corpus": corpus,
                })
                continue
            pid = proveedor.get("id") or ""
            if pid not in claves:
                blob = proveedor.get("clave_cifrada")
                if blob and self._master:
                    try:
                        claves[pid] = self._descifrar(blob, self._master)
                    except Exception:
                        self._log({"aviso": "clave_descifrado_fallo", "proveedor": pid})
                        claves[pid] = None
                else:
                    claves[pid] = None
            mapa[fila["endpoint"]] = ModeloCfg(
                endpoint=fila["endpoint"],
                proveedor=pid,
                tipo_api=proveedor.get("tipo_api") or "",
                modelo=modelo.get("modelo") or "",
                base_url=proveedor.get("base_url"),
                api_key=claves[pid],
                timeout_ms=int(modelo.get("timeout_ms") or 60_000),
                capacidades=modelo.get("capacidades") or {},
                params=modelo.get("params") or {},
                dimensiones=modelo.get("dimensiones"),
                espacio_vectorial=espacio,
                precio=modelo.get("precio") or {},
                version_config=version,
            )

        # hereda: query_rewrite usa el modelo de chat cuando no tiene asignación.
        for fila in por_endpoint.values():
            endpoint = fila["endpoint"]
            if endpoint in mapa or not fila.get("hereda") or not fila.get("activo"):
                continue
            heredada = mapa.get(fila["hereda"])
            if heredada is not None:
                mapa[endpoint] = ModeloCfg(
                    **{**heredada.__dict__, "endpoint": endpoint}
                )

        return {"map": mapa, "version": version, "fetched": self._now(),
                "corpus": corpus, "embedding_activo": "embeddings" in por_endpoint}

    def resolver(self, endpoint: str) -> ModeloCfg | None:
        try:
            if (
                self._snapshot is None
                or self._now() - self._snapshot["fetched"] >= self._ttl
            ):
                self._snapshot = self._cargar()
            cfg = self._snapshot["map"].get(endpoint)
            if endpoint == "embeddings" and self._url and self._key:
                if self._snapshot.get("error"):
                    raise ConfigEmbeddingInsegura("No se pudo verificar el corpus; embeddings detenido")
                if self._snapshot.get("embedding_activo"):
                    if (cfg is None or cfg.tipo_api not in ("gemini", "openai")
                            or not clave_efectiva(cfg)):
                        raise ConfigEmbeddingInsegura("Configuración activa de embeddings no utilizable")
                elif self._snapshot.get("corpus") != CORPUS_ENV:
                    raise ConfigEmbeddingInsegura("Fallback env incompatible o corpus desconocido")
            return cfg
        except ConfigEmbeddingInsegura:
            raise
        except Exception as error:
            if not self._avisado_fallo:
                self._avisado_fallo = True
                status = getattr(error, "status", None)
                self._log({
                    "aviso": "resolver_fallo",
                    "status": status,
                    "tipo": type(error).__name__,
                })
            # Snapshot vacío dentro del TTL: un consumidor por página/ítem no
            # debe reintentar la red en cada llamada ante un fallo sostenido.
            self._snapshot = {
                "map": {}, "version": 0, "fetched": self._now(), "error": True,
            }
            if endpoint == "embeddings":
                raise ConfigEmbeddingInsegura("No se pudo verificar el corpus; embeddings detenido") from None
            return None

    def version_vista(self) -> int:
        """Versión de configuración vista (0 si aún no se cargó)."""
        return int((self._snapshot or {}).get("version") or 0)

    def invalidar(self) -> None:
        """Fuerza recarga en el siguiente ``resolver`` (tests/admin)."""
        self._snapshot = None


class ErrorLectura(Exception):
    """Lectura PostgREST fallida; lleva el status para observabilidad."""

    def __init__(self, status: int, detalle: str) -> None:
        super().__init__(f"ia_cfg {status}: {detalle}")
        self.status = status


def clave_efectiva(cfg: ModeloCfg) -> str | None:
    """Clave utilizable: la descifrada de la fila, o la de env para 'gemini'.

    El shim del proxy (IA-004) hace lo mismo: la fila gana si tiene clave
    cifrada; si no, la del env. Proveedores OpenAI sin clave devuelven None:
    nunca se llama a un proveedor sin credencial.
    """
    if cfg.api_key:
        return cfg.api_key
    if cfg.proveedor == "gemini":
        return obtener_strip("GEMINI_API_KEY") or None
    return None


_resolver_compartido: ResolverCfg | None = None


def resolver_compartido() -> ResolverCfg:
    """Un resolver por proceso: el TTL de 60 s agrupa lecturas ``ia_*``."""
    global _resolver_compartido
    if _resolver_compartido is None:
        _resolver_compartido = ResolverCfg()
    return _resolver_compartido


def reset_resolver_compartido() -> None:
    """Solo para tests/admin."""
    global _resolver_compartido
    _resolver_compartido = None
