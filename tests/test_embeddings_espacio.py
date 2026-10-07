"""IA-007: escritura dual embedding_v2 (gemini) / embedding_v3 (qwen-tev4).

Cubre la validación de columna, las funciones genéricas del repositorio, el
parámetro ``columna`` de ``run_gemini`` y el cableado del CLI ``--espacio``.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Callable

import pytest

import generar_embeddings as ge
from seace_monitor.embeddings import repository, service
from seace_monitor.embeddings.preparation import EMBED_STATS, reset_embed_stats
from seace_monitor.ia.errores import ErrorProveedor


class FakeQuery:
    def __init__(self, client: "FakeSupabase", table: str):
        self.client = client
        self.table_name = table
        self.operations: list[tuple] = []

    def _record(self, name: str, *args, **kwargs):
        self.operations.append((name, args, kwargs))
        return self

    @property
    def not_(self):
        return self._record("not")

    def select(self, *args, **kwargs):
        return self._record("select", *args, **kwargs)

    def insert(self, *args, **kwargs):
        return self._record("insert", *args, **kwargs)

    def upsert(self, *args, **kwargs):
        return self._record("upsert", *args, **kwargs)

    def eq(self, *args, **kwargs):
        return self._record("eq", *args, **kwargs)

    def in_(self, *args, **kwargs):
        return self._record("in", *args, **kwargs)

    def is_(self, *args, **kwargs):
        return self._record("is", *args, **kwargs)

    def order(self, *args, **kwargs):
        return self._record("order", *args, **kwargs)

    def range(self, *args, **kwargs):
        return self._record("range", *args, **kwargs)

    def limit(self, *args, **kwargs):
        return self._record("limit", *args, **kwargs)

    def execute(self):
        self.client.executed.append(self)
        return self.client.handler(self)


class FakeSupabase:
    def __init__(self, handler: Callable[[FakeQuery], SimpleNamespace] | None = None):
        self.handler = handler or (lambda query: response([]))
        self.executed: list[FakeQuery] = []

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)


def operation(query: FakeQuery, name: str) -> tuple:
    return next(item for item in query.operations if item[0] == name)


def response(data=None, count=None) -> SimpleNamespace:
    return SimpleNamespace(data=data, count=count)


class FakeHttpContext:
    def __init__(self, client: object):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, exc_type, exc, traceback):
        return False


def fila_chunk() -> dict:
    return {
        "id": 7,
        "contrato_id": 42,
        "chunk_index": 0,
        "tipo": "tdr_pdf",
        "texto": "[ABC | 1]\ncontenido",
        "fuente": "pdf",
        "chunk_embed_text": "contenido",
    }


# ── repository: validación y genericidad por columna ─────────────────────────

@pytest.mark.parametrize("bad", ["embedding_v1", "embedding", "", "v2", "x; drop"])
def test_col_rechaza_columnas_desconocidas(bad: str) -> None:
    with pytest.raises(ValueError, match="columna de embedding no soportada"):
        repository._col(bad)
    client = FakeSupabase()
    with pytest.raises(ValueError, match="columna de embedding no soportada"):
        repository.chunks_sin_por_fuente(client, bad, "pdf", 5)
    assert client.executed == []


def test_funciones_genericas_filtran_la_columna_pedida() -> None:
    client = FakeSupabase(lambda query: response([{"id": 1}]))

    repository.chunks_sin_por_fuente(client, "embedding_v3", "pdf", 1)
    assert operation(client.executed[0], "is")[1] == ("embedding_v3", "null")

    repository.chunks_sin_embedding(client, "embedding_v3", [10], 1)
    assert operation(client.executed[1], "is")[1] == ("embedding_v3", "null")

    repository.contar_embeddings(client, "embedding_v3", [10], "pdf")
    assert operation(client.executed[2], "is")[1] == ("embedding_v3", "null")
    assert any(item[0] == "not" for item in client.executed[2].operations)


def test_guardar_embeddings_escribe_en_la_columna_pedida() -> None:
    client = FakeSupabase()
    rows = [fila_chunk()]

    repository.guardar_embeddings(client, "embedding_v3", rows, [[1.0, -0.5]])

    upserts = operation(client.executed[0], "upsert")[1][0]
    assert upserts[0]["embedding_v3"] == "[1.00000000,-0.50000000]"
    assert "embedding_v2" not in upserts[0]


# ── FIX-013: escritura por RPC con sub-lotes y fallback a upsert ─────────────

class FakeSupabaseRpc(FakeSupabase):
    def __init__(self, handler=None, rpc_error: Exception | None = None):
        super().__init__(handler)
        self.rpc_error = rpc_error
        self.rpc_calls: list[tuple] = []

    def rpc(self, name: str, params: dict):
        self.rpc_calls.append((name, params))
        error = self.rpc_error
        return SimpleNamespace(
            execute=lambda: (_ for _ in ()).throw(error) if error else response()
        )


def _pgrst202() -> Exception:
    exc = Exception("Could not find the function")
    exc.code = "PGRST202"  # type: ignore[attr-defined]
    return exc


def test_guardar_embeddings_usa_rpc_en_sublotes() -> None:
    client = FakeSupabaseRpc()
    rows = [
        {**fila_chunk(), "id": i} for i in range(repository.SUBLOTE_GUARDAR * 2 + 3)
    ]
    vectors = [[float(i), 0.0] for i in range(len(rows))]

    repository.guardar_embeddings(client, "embedding_v2", rows, vectors)

    assert len(client.rpc_calls) == 3  # 8 + 8 + 3
    assert client.executed == []  # nunca upsert
    name, params = client.rpc_calls[0]
    assert name == "guardar_embeddings_v2"
    assert params["ids"] == list(range(8))
    assert params["vectores"][0] == "[0.00000000,0.00000000]"
    assert len(params["vectores"]) == 8
    assert client.rpc_calls[2][1]["ids"] == [16, 17, 18]


def test_guardar_embeddings_cae_a_upsert_si_la_rpc_no_existe() -> None:
    client = FakeSupabaseRpc(rpc_error=_pgrst202())
    rows = [fila_chunk(), {**fila_chunk(), "id": 8}]

    repository.guardar_embeddings(client, "embedding_v3", rows, [[1.0], [2.0]])

    assert client.rpc_calls == [("guardar_embeddings_v3", {"ids": [7, 8],
                                                           "vectores": ["[1.00000000]",
                                                                        "[2.00000000]"]})]
    upserts = operation(client.executed[0], "upsert")[1][0]
    assert upserts[0]["embedding_v3"] == "[1.00000000]"
    assert upserts[1]["id"] == 8


def test_guardar_embeddings_propaga_error_real_de_rpc() -> None:
    timeout = Exception("canceling statement due to statement timeout")
    client = FakeSupabaseRpc(rpc_error=timeout)

    with pytest.raises(Exception, match="statement timeout"):
        repository.guardar_embeddings(
            client, "embedding_v2", [fila_chunk()], [[1.0]])

    assert client.executed == []  # sin fallback ante error real


def test_cobertura_columna_devuelve_claves_genericas(monkeypatch) -> None:
    monkeypatch.setattr(repository, "paginar_ids_vigentes", lambda supa: [1])
    counts = iter([10, 7])
    client = FakeSupabase(lambda query: response([], next(counts)))

    cov = repository.cobertura_columna(client, "embedding_v3")

    assert cov == {
        "vigentes": 1,
        "chunks_vigentes": 10,
        "chunks_col": 7,
        "chunks_col_null": 3,
        "col": "embedding_v3",
    }
    assert operation(client.executed[1], "is")[1] == ("embedding_v3", "null")


def test_wrappers_v2_conservan_la_columna_historica() -> None:
    client = FakeSupabase(lambda query: response([{"id": 1}]))

    repository.chunks_sin_embedding_v2(client, [10], 1, fuente="pdf")
    repository.guardar_embeddings_v2(client, [fila_chunk()], [[1.0]])
    repository.contar_embeddings_v2(client, [10])

    assert operation(client.executed[0], "is")[1] == ("embedding_v2", "null")
    upserts = operation(client.executed[1], "upsert")[1][0]
    assert "embedding_v2" in upserts[0]
    assert operation(client.executed[2], "is")[1] == ("embedding_v2", "null")


def test_cobertura_vigentes_conserva_sus_claves(monkeypatch) -> None:
    monkeypatch.setattr(repository, "paginar_ids_vigentes", lambda supa: [1])
    counts = iter([10, 7])
    client = FakeSupabase(lambda query: response([], next(counts)))

    assert repository.cobertura_vigentes(client) == {
        "vigentes": 1,
        "chunks_vigentes": 10,
        "chunks_v2": 7,
        "chunks_v2_null": 3,
    }


def test_entrypoint_reexporta_cobertura_columna() -> None:
    assert ge.cobertura_columna is repository.cobertura_columna


# ── service.run_gemini(columna=...) ──────────────────────────────────────────

def test_run_gemini_v3_escribe_y_mide_en_embedding_v3(monkeypatch, capsys) -> None:
    reset_embed_stats()
    EMBED_STATS["tokens_api"] = 5  # fuerza la fila de traza uso_ia
    row = fila_chunk()
    supa = FakeSupabase()
    captured: dict = {}

    monkeypatch.setattr(
        service,
        "chunks_sin_por_fuente",
        lambda supa_, col, fuente, limit: [row] if col == "embedding_v3" else [],
    )
    monkeypatch.setattr(
        service,
        "guardar_embeddings",
        lambda supa_, col, rows, vectors: captured.update(
            {"col": col, "rows": rows, "vectors": vectors}
        ),
    )
    conteos: list[str] = []
    monkeypatch.setattr(
        service,
        "contar_embeddings",
        lambda supa_, col, ids, fuente=None: conteos.append(col) or 1,
    )
    runs: list[tuple] = []
    monkeypatch.setattr(
        service,
        "registrar_run",
        lambda *args, **kwargs: runs.append((args, kwargs)),
    )
    monkeypatch.setattr(
        service,
        "registrar_evento",
        lambda *args, **kwargs: None,
    )

    result = service.run_gemini(
        supa,
        0,
        fuente="pdf",
        ids=[42],
        delay=0,
        fail_fast=True,
        api_key="test-key",
        solicitar=lambda http, texts, key, fail_fast=False: [[1.0, 0.0]],
        http_client_factory=lambda: FakeHttpContext(object()),
        sleep=lambda s: None,
        columna="embedding_v3",
    )

    assert result == {"ok": 1, "err": 0, "total": 1, "pendientes": 0}
    assert captured["col"] == "embedding_v3"
    assert conteos == ["embedding_v3"]

    # Traza uso_ia y registrar_run llevan columna + espacio del escritor v3.
    uso = next(q for q in supa.executed if q.table_name == "uso_ia")
    fila_uso = operation(uso, "insert")[1][0]
    assert fila_uso["detalle"]["columna"] == "embedding_v3"
    assert fila_uso["detalle"]["espacio"] == "qwen-tev4-1536"
    assert runs[0][0][2]["columna"] == "embedding_v3"
    assert runs[0][0][2]["espacio"] == "qwen-tev4-1536"

    out = capsys.readouterr().out
    assert "WHERE embedding_v3 IS NULL" in out
    assert "-> embedding_v3" in out


def test_run_gemini_v3_sin_pendientes_mide_cobertura_v3(monkeypatch, capsys) -> None:
    reset_embed_stats()
    vistos: list[str] = []
    monkeypatch.setattr(
        service,
        "chunks_sin_embedding",
        lambda supa_, col, ids, limit, fuente=None: [],
    )
    monkeypatch.setattr(
        service,
        "cobertura_columna",
        lambda supa_, col: vistos.append(col) or {
            "vigentes": 1,
            "chunks_vigentes": 4,
            "chunks_col": 3,
            "chunks_col_null": 1,
            "col": col,
        },
    )
    monkeypatch.setattr(service, "paginar_ids_vigentes", lambda supa_: [1])

    result = service.run_gemini(object(), 0, api_key="k", columna="embedding_v3")

    assert result == {"ok": 0, "err": 0, "total": 0, "pendientes": 0}
    assert vistos == ["embedding_v3"]
    out = capsys.readouterr().out
    assert "embedding_v3 NOT NULL" in out
    assert "embedding_v3 NULL" in out


def test_run_gemini_columna_desconocida_aborta() -> None:
    with pytest.raises(ValueError, match="columna de embedding no soportada"):
        service.run_gemini(object(), 0, api_key="k", columna="embedding_v1")


# ── CLI --espacio ─────────────────────────────────────────────────────────────

def test_espacio_qwen_cablea_openai_y_dashscope(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")
    calls: list[dict] = []
    monkeypatch.setattr(
        ge, "_run_gemini", lambda supa, limit, **kw: calls.append(kw) or {})
    # El escritor v3 no consulta la config del corpus activo.
    monkeypatch.setattr(
        ge, "cfg_resuelta", lambda e: pytest.fail("cfg_resuelta llamado"))
    monkeypatch.setattr(
        ge, "extras_embeddings", lambda c: pytest.fail("extras_embeddings llamado"))

    ge.run_gemini(object(), 7, fuente="pdf", espacio="qwen")

    kw = calls[0]
    assert kw["api_key"] == "dash-key"
    assert kw["columna"] == "embedding_v3"
    assert kw["modelo"] == "text-embedding-v4"
    assert kw["precio_in"] == ge.QWEN_EMBED_USD_PER_M == 0.07
    assert kw["batch"] == 10
    assert kw["fuente"] == "pdf"

    seen: dict = {}
    monkeypatch.setattr(
        ge,
        "solicitar_embeddings_openai",
        lambda http, texts, key, fail_fast=False, **kw2: seen.update(
            {"texts": texts, "key": key, "fail_fast": fail_fast, **kw2}
        ) or [[0.0]],
    )
    out = kw["solicitar"](object(), ["hola"], "dash-key", fail_fast=True)
    assert out == [[0.0]]
    assert seen["key"] == "dash-key"
    assert seen["fail_fast"] is True
    assert seen["url"] == ge.QWEN_EMBED_URL
    assert seen["modelo"] == "text-embedding-v4"
    assert seen["dimensiones"] == 1536
    assert seen["batch_max"] == 10
    assert seen["proveedor"] == "qwen"


def test_espacio_qwen_sin_clave_aborta_antes_de_resolver(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(
        ge, "cfg_resuelta", lambda e: pytest.fail("cfg_resuelta llamado"))

    with pytest.raises(SystemExit, match="DASHSCOPE_API_KEY"):
        ge.run_gemini(object(), 0, espacio="qwen")


def test_espacio_gemini_conserva_el_camino_por_cfg(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(ge, "GEMINI_API_KEY", "env-key")
    monkeypatch.setattr(ge, "cfg_resuelta", lambda e: None)
    monkeypatch.setattr(
        ge, "_run_gemini", lambda supa, limit, **kw: calls.append(kw) or {})

    ge.run_gemini(object(), 0)

    assert calls[0]["api_key"] == "env-key"
    assert calls[0]["columna"] == "embedding_v2"
    assert "solicitar" not in calls[0]


def test_auth_check_qwen_reporta_estado_sin_mostrar_clave(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-secret")
    llamadas: list[tuple] = []
    monkeypatch.setattr(
        ge,
        "solicitar_embeddings_openai",
        lambda http, texts, key, fail_fast=False, **kw: llamadas.append(
            (texts, key, fail_fast, kw)
        ) or [[0.0]],
    )

    assert ge.auth_check_qwen() == 0

    out = capsys.readouterr().out
    assert "qwen_auth HTTP=200 auth_fail=False auth_ok=True" in out
    assert "dash-secret" not in out
    texts, key, fail_fast, kw = llamadas[0]
    assert texts == ["ping auth"]
    assert key == "dash-secret"
    assert fail_fast is True
    assert kw["url"] == ge.QWEN_EMBED_URL
    assert kw["modelo"] == "text-embedding-v4"
    assert kw["proveedor"] == "qwen"


def test_auth_check_qwen_falla_con_estado_http(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-secret")

    def boom(*args, **kwargs):
        raise ErrorProveedor(
            "credencial", "qwen HTTP 401", proveedor="qwen",
            modelo="text-embedding-v4", status=401, retriable=False,
        )

    monkeypatch.setattr(ge, "solicitar_embeddings_openai", boom)

    assert ge.auth_check_qwen() == 1
    out = capsys.readouterr().out
    assert "qwen_auth HTTP=401 auth_fail=True auth_ok=False" in out
    assert "dash-secret" not in out


def test_auth_check_qwen_sin_clave(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    assert ge.auth_check_qwen() == 2
    assert "qwen_auth HTTP=missing auth_ok=false" in capsys.readouterr().out


def test_main_espacio_qwen_enruta_ping_y_flags(monkeypatch) -> None:
    pings: list[str] = []
    monkeypatch.setattr(
        ge, "auth_check_qwen", lambda: pings.append("qwen") or 0)
    monkeypatch.setattr(
        ge, "auth_check_gemini", lambda: pings.append("gemini") or 0)
    monkeypatch.setattr(
        sys, "argv", ["generar_embeddings.py", "--espacio", "qwen", "--auth-check"])

    with pytest.raises(SystemExit) as exc:
        ge.main()
    assert exc.value.code == 0
    assert pings == ["qwen"]


def test_main_espacio_qwen_pasa_flags_al_run(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")
    monkeypatch.setattr(ge, "SUPABASE_URL", "https://sb")
    monkeypatch.setattr(ge, "SUPABASE_KEY", "svc")
    monkeypatch.setattr(ge, "crear_cliente", lambda: object())
    calls: list[tuple] = []
    monkeypatch.setattr(
        ge, "run_gemini", lambda supa, limit, **kw: calls.append((limit, kw)))
    monkeypatch.setattr(
        sys,
        "argv",
        ["generar_embeddings.py", "--espacio", "qwen", "--limit", "5",
         "--fuente", "pdf", "--ids", "42"],
    )

    ge.main()

    limit, kw = calls[0]
    assert limit == 5
    assert kw["espacio"] == "qwen"
    assert kw["fuente"] == "pdf"
    assert kw["ids"] == [42]
