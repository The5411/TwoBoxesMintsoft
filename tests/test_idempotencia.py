"""E-1 / OPS-01 / COR-07: persistencia e idempotencia.

Estos son los casos que el OrderedDict en memoria NO cubria y que ahora cubre
storage/event_store.py. El mas importante es
test_W836_reproceso_no_crea_un_segundo_return.
"""
import os
import tempfile

import pytest

import config
import listener
from conftest import ClienteFalso, item, limpiar_store, payload, procesar_en_background
from storage.event_store import ESTADO_FALLADO, ESTADO_PROCESADO, EventStore

AUTH = {"x-two-boxes-authorization": "test-secret"}


def _store_nuevo():
    """Store en un archivo propio, para los tests que no pasan por el listener."""
    return EventStore(database_url="", sqlite_path=os.path.join(
        tempfile.mkdtemp(prefix="tb-idem-"), "eventos.db"))


# ------------------------------------------------------- el caso de la orden W836
def test_W836_reproceso_no_crea_un_segundo_return(mails):
    """El return se crea, el paso siguiente falla, y alguien reprocesa el webhook.

    Antes eso creaba un SEGUNDO return en Mintsoft con el mismo stock. Ahora el
    return_id quedo grabado en el instante en que Mintsoft lo devolvio, asi que
    el reproceso se frena y avisa.
    """
    # orden_existe=False -> return externo; allocate_ok=False -> falla despues de crearlo
    cli = ClienteFalso(orden_existe=False, allocate_ok=False)
    listener.return_service.client = cli

    datos = payload(event_id="w836")
    procesar_en_background(datos)

    creados = cli.hizo("create_external_return")
    assert len(creados) == 1, "el primer intento tiene que crear el return"

    # El return quedo registrado aunque el webhook fallo despues.
    registro = listener.event_store.get(listener.event_store.clave_de(datos))
    assert registro["return_id"] == "12772"
    assert registro["status"] == ESTADO_FALLADO

    # Reproceso: el mismo payload, reenviado a mano.
    mails.clear()
    procesar_en_background(datos)

    assert len(cli.hizo("create_external_return")) == 1, \
        "el reproceso NO puede crear un segundo return"
    assert len(mails) == 1, "tiene que avisar que el return ya existe"
    cuerpo = mails[0].get_content()
    assert "12772" in cuerpo
    assert "a mano" in cuerpo


def test_reproceso_avisa_con_el_return_id_en_el_asunto(mails):
    cli = ClienteFalso(orden_existe=False, allocate_ok=False)
    listener.return_service.client = cli
    datos = payload(event_id="w836-b")
    procesar_en_background(datos)
    mails.clear()
    procesar_en_background(datos)
    assert "ya existe" in mails[0]["Subject"]


# --------------------------------------------------------- sobrevive al reinicio
def test_el_registro_sobrevive_al_reinicio_del_proceso():
    """Un OrderedDict en memoria se perdia en cada deploy; la base no."""
    store = _store_nuevo()
    datos = {"id": "e-reinicio", "event_data": {}}

    v = store.claim(datos, event_id="e-reinicio", reference="TRK-1")
    assert v.otorgado
    store.finish(v.registro["event_key"], ESTADO_PROCESADO)

    # Un proceso nuevo, apuntando al mismo archivo: es el reinicio.
    otro = EventStore(database_url="", sqlite_path=store.sqlite_path)
    v2 = otro.claim(datos, event_id="e-reinicio")
    assert not v2.otorgado
    assert v2.motivo == "ya_procesado"


def test_dos_workers_de_gunicorn_comparten_el_registro():
    """El OrderedDict era por proceso: el mismo evento en el otro worker pasaba."""
    store = _store_nuevo()
    worker_a = store
    worker_b = EventStore(database_url="", sqlite_path=store.sqlite_path)

    datos = {"id": "e-dos-workers", "event_data": {}}
    otorgados = [
        worker_a.claim(datos, event_id="e-dos-workers").otorgado,
        worker_b.claim(datos, event_id="e-dos-workers").otorgado,
    ]
    assert otorgados == [True, False], "solo un worker puede procesarlo"


def test_payload_reenviado_sin_id_se_detecta_por_hash():
    """Si el reenvio viene sin el campo `id`, la clave es la huella del payload."""
    store = _store_nuevo()
    datos = {"event_data": {"line_items": [{"sku": "A"}]}}

    assert store.claim(datos).otorgado
    assert not store.claim(datos).otorgado, \
        "el mismo payload sin id no puede procesarse dos veces"

    # Un payload distinto si tiene que pasar.
    assert store.claim({"event_data": {"line_items": [{"sku": "B"}]}}).otorgado


def test_el_orden_de_las_claves_no_cambia_la_huella():
    store = _store_nuevo()
    assert store.claim({"a": 1, "b": 2}).otorgado
    assert not store.claim({"b": 2, "a": 1}).otorgado


# ------------------------------------------------------------- reintentos validos
def test_un_evento_que_fallo_sin_crear_return_si_se_reintenta():
    """Un fallo ANTES de crear el return se puede reintentar: no hay nada que duplicar."""
    store = _store_nuevo()
    datos = {"id": "e-fallo", "event_data": {}}

    v = store.claim(datos, event_id="e-fallo")
    store.finish(v.registro["event_key"], ESTADO_FALLADO, "Mintsoft caida")

    v2 = store.claim(datos, event_id="e-fallo")
    assert v2.otorgado, "sin return creado, el reintento tiene que poder correr"
    assert v2.motivo == "reintento"


def test_un_claim_colgado_se_retoma_pasada_la_ventana(monkeypatch):
    """Si el worker muere a mitad de camino, el evento no puede quedar trabado."""
    store = _store_nuevo()
    datos = {"id": "e-colgado", "event_data": {}}

    assert store.claim(datos, event_id="e-colgado").otorgado
    # Mientras esta dentro de la ventana, nadie mas lo toca.
    assert store.claim(datos, event_id="e-colgado").motivo == "en_proceso"

    # Pasada la ventana se considera colgado.
    monkeypatch.setattr(config, "CLAIM_STALE_SECONDS", -1)
    v = store.claim(datos, event_id="e-colgado")
    assert v.otorgado
    assert v.motivo == "retomado_colgado"


def test_un_claim_colgado_con_return_creado_NO_se_retoma(monkeypatch):
    """Ni siquiera un claim colgado justifica crear un segundo return."""
    store = _store_nuevo()
    datos = {"id": "e-colgado-con-return", "event_data": {}}
    v = store.claim(datos, event_id="e-colgado-con-return")
    store.record_return(v.registro["event_key"], 999, "internal")

    monkeypatch.setattr(config, "CLAIM_STALE_SECONDS", -1)
    v2 = store.claim(datos, event_id="e-colgado-con-return")
    assert not v2.otorgado
    assert v2.motivo == "return_ya_creado"
    assert v2.return_id == "999"


# ------------------------------------------------------------ la base no responde
def test_si_la_base_no_responde_no_se_escribe_en_mintsoft(mails, monkeypatch):
    """Duplicar stock es peor que demorar un return: se archiva y se avisa."""
    cli = ClienteFalso()
    listener.return_service.client = cli
    monkeypatch.setattr(config, "REQUIRE_STORE", True)
    monkeypatch.setattr(
        listener.event_store, "_conectar",
        lambda: (_ for _ in ()).throw(RuntimeError("connection refused")),
    )

    r = procesar_en_background(payload(event_id="e-sin-base"))

    assert r.status_code == 200, "Two Boxes no tiene que reintentar por esto"
    assert cli.llamadas == [], "sin idempotencia no se escribe en el WMS"
    assert len(mails) == 1
    assert "idempotencia" in mails[0]["Subject"].lower()


def test_con_REQUIRE_STORE_false_se_procesa_a_riesgo(mails, monkeypatch):
    cli = ClienteFalso()
    listener.return_service.client = cli
    monkeypatch.setattr(config, "REQUIRE_STORE", False)
    monkeypatch.setattr(
        listener.event_store, "_conectar",
        lambda: (_ for _ in ()).throw(RuntimeError("connection refused")),
    )

    procesar_en_background(payload(event_id="e-sin-base-2"))

    assert cli.hizo("create_return"), "con REQUIRE_STORE=false se procesa igual"
    assert any("duplicad" in m.get_content() for m in mails), \
        "el mail tiene que advertir el riesgo de duplicar"


# ------------------------------------------- segunda red: misma Reference, otro id
def test_con_warn_se_procesa_y_NO_sale_mail(mails, monkeypatch):
    """Dos eventos distintos apuntando al mismo return.

    Con 'warn' (el default) se procesa igual y queda SOLO en el log: una misma
    orden puede tener dos devoluciones legitimas en momentos distintos, asi que
    este aviso daba muchos falsos positivos y el mail no agregaba nada sobre la
    linea de log.
    """
    monkeypatch.setattr(config, "DUPLICATE_REFERENCE_ACTION", "warn")
    cli = ClienteFalso()
    listener.return_service.client = cli

    procesar_en_background(payload(event_id="ref-1"))
    mails.clear()
    procesar_en_background(payload(event_id="ref-2"))  # misma TRK-999

    assert len(cli.hizo("create_return")) == 2, "con 'warn' se procesa igual"
    assert mails == [], \
        f"con 'warn' no tiene que salir mail: {[m['Subject'] for m in mails]}"


def test_con_block_no_se_crea_el_segundo_return(mails, monkeypatch):
    monkeypatch.setattr(config, "DUPLICATE_REFERENCE_ACTION", "block")
    cli = ClienteFalso()
    listener.return_service.client = cli

    procesar_en_background(payload(event_id="ref-3"))
    procesar_en_background(payload(event_id="ref-4"))  # misma TRK-999

    assert len(cli.hizo("create_return")) == 1, \
        "con 'block' el segundo evento no crea otro return"


def test_con_block_SI_sale_mail(mails, monkeypatch):
    """Con 'block' el return no se crea, y eso no puede pasar en silencio: un
    return que no se crea sin que nadie se entere es una devolucion perdida."""
    monkeypatch.setattr(config, "DUPLICATE_REFERENCE_ACTION", "block")
    cli = ClienteFalso()
    listener.return_service.client = cli

    procesar_en_background(payload(event_id="ref-5"))
    mails.clear()
    procesar_en_background(payload(event_id="ref-6"))  # misma TRK-999

    assert len(mails) == 1, "el operador tiene que enterarse de que no se creo"
    assert "NO creado" in mails[0]["Subject"]
    assert "a mano" in mails[0].get_content()


def test_con_off_ni_se_consulta(mails, monkeypatch):
    monkeypatch.setattr(config, "DUPLICATE_REFERENCE_ACTION", "off")
    cli = ClienteFalso()
    listener.return_service.client = cli

    procesar_en_background(payload(event_id="ref-7"))
    procesar_en_background(payload(event_id="ref-8"))  # misma TRK-999

    assert len(cli.hizo("create_return")) == 2
    assert mails == []


# -------------------------------------------------------------------- /health
def test_health_expone_el_estado_del_store():
    http = listener.app.test_client()
    cuerpo = http.get("/health").get_json()
    assert cuerpo["store"]["backend"] == "sqlite"
    assert cuerpo["store"]["disponible"] is True
    assert cuerpo["config"]["require_store"] is config.REQUIRE_STORE


def test_health_degradado_si_el_store_esta_caido(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_STORE", True)
    monkeypatch.setattr(
        listener.event_store, "_conectar",
        lambda: (_ for _ in ()).throw(RuntimeError("connection refused")),
    )
    r = listener.app.test_client().get("/health")
    assert r.status_code == 503
    assert r.get_json()["status"] == "degraded"
