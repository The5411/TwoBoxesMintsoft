"""El store se comporta igual en SQLite y en Postgres.

Los tests de tests/test_idempotencia.py corren solo contra SQLite, que es lo que
hay en CI por defecto. Pero produccion usa Postgres, y las dos implementaciones
comparten la SQL con una sola diferencia: el placeholder ('?' vs '%s'). Este
archivo corre el MISMO contrato contra los dos backends, asi que una divergencia
se ve.

Postgres se ejercita solo si TEST_DATABASE_URL apunta a una base descartable; si
no, esos casos quedan skipped. El workflow de CI levanta un servicio de Postgres
y la setea, asi que en CI corren los dos.
"""
import os
import tempfile

import pytest

import config
from storage.event_store import (
    ESTADO_FALLADO,
    ESTADO_IGNORADO,
    ESTADO_PROCESADO,
    EventStore,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def _sqlite():
    return EventStore(database_url="", sqlite_path=os.path.join(
        tempfile.mkdtemp(prefix="tb-backend-"), "eventos.db"))


def _postgres():
    store = EventStore(database_url=TEST_DATABASE_URL)
    if not store.disponible:
        pytest.fail(
            f"TEST_DATABASE_URL esta seteada pero el store no abrio: "
            f"{store.ultimo_error}"
        )
    # Base compartida entre tests: se limpia al empezar.
    con = store._conectar()
    try:
        con.cursor().execute("DELETE FROM webhook_events")
        con.commit()
    finally:
        con.close()
    return store


@pytest.fixture(params=["sqlite", "postgres"])
def store(request):
    """Un store por backend. Postgres se saltea si no hay TEST_DATABASE_URL."""
    if request.param == "postgres":
        if not TEST_DATABASE_URL:
            pytest.skip("sin TEST_DATABASE_URL: no se ejercita Postgres")
        return _postgres()
    return _sqlite()


# ------------------------------------------------------------------- contrato
def test_el_backend_es_el_esperado(store):
    assert store.backend in ("sqlite", "postgres")
    assert store.disponible is True


def test_un_evento_nuevo_se_otorga_una_sola_vez(store):
    datos = {"id": "b-1", "event_data": {}}
    assert store.claim(datos, event_id="b-1").motivo == "nuevo"
    assert store.claim(datos, event_id="b-1").motivo == "en_proceso"


def test_el_insert_on_conflict_no_duplica_filas(store):
    """Es la primitiva sobre la que se apoya toda la idempotencia."""
    datos = {"id": "b-2", "event_data": {}}
    for _ in range(5):
        store.claim(datos, event_id="b-2")

    por_estado = store.stats()["por_estado"]
    assert sum(por_estado.values()) == 1, "cinco claims, una sola fila"


def test_record_return_y_finish_persisten(store):
    datos = {"id": "b-3", "event_data": {}}
    v = store.claim(datos, event_id="b-3", reference="TRK-B3", merchant="Bronze Snake")
    clave = v.registro["event_key"]

    assert store.record_return(clave, 4242, "external") is True
    assert store.finish(clave, ESTADO_PROCESADO) is True

    registro = store.get(clave)
    assert registro["return_id"] == "4242"
    assert registro["return_kind"] == "external"
    assert registro["status"] == ESTADO_PROCESADO
    assert registro["reference"] == "TRK-B3"
    assert registro["merchant"] == "Bronze Snake"
    assert registro["created_at"] and registro["updated_at"]


def test_un_return_ya_creado_frena_el_reproceso(store):
    """El caso W836, contra los dos backends."""
    datos = {"id": "b-4", "event_data": {}}
    v = store.claim(datos, event_id="b-4")
    store.record_return(v.registro["event_key"], 12772, "external")
    store.finish(v.registro["event_key"], ESTADO_FALLADO, "fallo despues de crearlo")

    v2 = store.claim(datos, event_id="b-4")
    assert not v2.otorgado
    assert v2.motivo == "return_ya_creado"
    assert v2.return_id == "12772"


def test_el_reintento_se_permite_si_no_hay_return(store):
    datos = {"id": "b-5", "event_data": {}}
    v = store.claim(datos, event_id="b-5")
    store.finish(v.registro["event_key"], ESTADO_FALLADO, "Mintsoft caida")
    assert store.claim(datos, event_id="b-5").motivo == "reintento"


def test_el_contador_de_intentos_sube(store):
    datos = {"id": "b-6", "event_data": {}}
    v = store.claim(datos, event_id="b-6")
    store.finish(v.registro["event_key"], ESTADO_FALLADO)
    store.claim(datos, event_id="b-6")
    assert int(store.get(v.registro["event_key"])["attempts"]) == 2


def test_busqueda_por_reference(store):
    a = store.claim({"id": "b-7a"}, event_id="b-7a", reference="TRK-MISMA")
    b = store.claim({"id": "b-7b"}, event_id="b-7b", reference="TRK-MISMA")
    store.record_return(a.registro["event_key"], 111, "internal")

    previos = store.returns_por_reference("TRK-MISMA",
                                          excluir_event_key=b.registro["event_key"])
    assert [p["return_id"] for p in previos] == ["111"], \
        "solo los eventos con return creado, y no el que pregunta"

    # Sin return creado todavia, no cuenta.
    assert store.returns_por_reference("TRK-INEXISTENTE") == []
    assert store.returns_por_reference(None) == []
    assert store.returns_por_reference("") == []


def test_un_claim_colgado_se_retoma(store, monkeypatch):
    datos = {"id": "b-8", "event_data": {}}
    assert store.claim(datos, event_id="b-8").otorgado
    monkeypatch.setattr(config, "CLAIM_STALE_SECONDS", -1)
    assert store.claim(datos, event_id="b-8").motivo == "retomado_colgado"


def test_registrar_ignorado_deja_constancia(store):
    datos = {"id": "b-9", "event_data": {}}
    store.registrar_ignorado(datos, event_id="b-9", event_type="return-created",
                             motivo="event_type no soportado")
    registro = store.get(store.clave_de(datos, "b-9"))
    assert registro["status"] == ESTADO_IGNORADO
    assert registro["event_type"] == "return-created"


def test_stats_cuenta_por_estado(store):
    v1 = store.claim({"id": "b-10a"}, event_id="b-10a")
    v2 = store.claim({"id": "b-10b"}, event_id="b-10b")
    store.finish(v1.registro["event_key"], ESTADO_PROCESADO)
    store.record_return(v2.registro["event_key"], 7, "internal")
    store.finish(v2.registro["event_key"], ESTADO_FALLADO)

    s = store.stats()
    assert s["disponible"] is True
    assert s["por_estado"][ESTADO_PROCESADO] == 1
    assert s["por_estado"][ESTADO_FALLADO] == 1
    assert s["eventos_con_return"] == 1


def test_el_payload_se_guarda_cuando_PERSIST_PAYLOAD(store, monkeypatch):
    monkeypatch.setattr(config, "PERSIST_PAYLOAD", True)
    datos = {"id": "b-11", "event_data": {"line_items": [{"sku": "ABC"}]}}
    v = store.claim(datos, event_id="b-11")
    assert "ABC" in (store.get(v.registro["event_key"])["payload"] or "")


def test_sin_PERSIST_PAYLOAD_no_se_guarda(store, monkeypatch):
    monkeypatch.setattr(config, "PERSIST_PAYLOAD", False)
    datos = {"id": "b-12", "event_data": {"line_items": [{"sku": "SECRETO"}]}}
    v = store.claim(datos, event_id="b-12")
    assert not store.get(v.registro["event_key"])["payload"]


def test_una_clave_muy_larga_no_rompe(store):
    """Los event_id de Two Boxes son UUIDs, pero la clave por hash es mas larga."""
    datos = {"event_data": {"x": "y" * 5000}}  # sin id -> clave por sha256
    v = store.claim(datos)
    assert v.otorgado
    assert v.registro["event_key"].startswith("sha256:")


# --------------------------------------------- traduccion de placeholders (S-x)
def test_los_placeholders_se_traducen_segun_el_backend():
    """La unica diferencia de SQL entre los dos backends."""
    sqlite = EventStore.__new__(EventStore)
    sqlite.es_postgres = False
    postgres = EventStore.__new__(EventStore)
    postgres.es_postgres = True

    sql = "SELECT a FROM t WHERE b = ? AND c = ?"
    assert sqlite._sql(sql) == sql
    assert postgres._sql(sql) == "SELECT a FROM t WHERE b = %s AND c = %s"


@pytest.mark.parametrize("url,esperado", [
    ("postgres://u:p@h/db", "postgresql://u:p@h/db"),
    ("postgresql://u:p@h/db", "postgresql://u:p@h/db"),
])
def test_se_normaliza_el_esquema_postgres_de_los_PaaS(url, esperado, monkeypatch):
    """Heroku y Railway exponen 'postgres://', que psycopg3 no acepta."""
    store = EventStore.__new__(EventStore)
    store.es_postgres = True
    store.database_url = url
    store._psycopg = None
    capturado = {}

    class ModuloFalso:
        @staticmethod
        def connect(u, **k):
            capturado["url"] = u
            raise RuntimeError("no conectamos de verdad")

    store._cargar_psycopg = lambda: ("psycopg", ModuloFalso)
    with pytest.raises(RuntimeError):
        store._conectar()
    assert capturado["url"] == esperado
