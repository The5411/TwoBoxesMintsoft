"""Punto 4 del plan: C-2, S-5, S-6, S-7, E-7 y E-8.

Todos se implementaron con el criterio de NO cambiar el comportamiento actual,
asi que varios de estos tests existen para fijar justamente eso: que el default
sigue haciendo lo mismo que antes.
"""
import importlib
import logging

import pytest

import config
import listener
from conftest import (
    ClienteFalso,
    capturar_logs,
    item,
    payload,
    procesar_en_background,
)
from loggers.main_logger import (
    _FiltroCorrelacion,
    get_correlacion,
    limpiar_correlacion,
    set_correlacion,
)

AUTH = {"x-two-boxes-authorization": "test-secret"}


# ------------------------------------------------------------- E-8: rate limit
def test_el_rate_limit_no_molesta_en_volumen_normal(monkeypatch):
    """El default (600/min) tiene que ser invisible para la operacion real."""
    assert config.RATE_LIMIT_PER_MINUTE >= 600
    listener.return_service.client = ClienteFalso()
    http = listener.app.test_client()
    for i in range(20):
        r = http.post("/webhook", json=payload(event_id=f"rl-{i}"), headers=AUTH)
        assert r.status_code == 200
    from conftest import drenar_pools
    drenar_pools()


def test_pasado_el_limite_devuelve_429(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 3)
    listener.return_service.client = ClienteFalso()
    http = listener.app.test_client()

    codigos = [
        http.post("/webhook", json=payload(event_id=f"rl2-{i}"), headers=AUTH).status_code
        for i in range(5)
    ]
    from conftest import drenar_pools
    drenar_pools()

    assert codigos[:3] == [200, 200, 200]
    assert codigos[3:] == [429, 429]


def test_el_429_trae_retry_after(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 1)
    listener.return_service.client = ClienteFalso()
    http = listener.app.test_client()
    http.post("/webhook", json=payload(event_id="rl3-a"), headers=AUTH)
    r = http.post("/webhook", json=payload(event_id="rl3-b"), headers=AUTH)
    from conftest import drenar_pools
    drenar_pools()

    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "60"


def test_en_cero_el_rate_limit_queda_desactivado(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 0)
    listener.return_service.client = ClienteFalso()
    http = listener.app.test_client()
    codigos = [
        http.post("/webhook", json=payload(event_id=f"rl4-{i}"), headers=AUTH).status_code
        for i in range(10)
    ]
    from conftest import drenar_pools
    drenar_pools()
    assert set(codigos) == {200}


def test_el_rate_limit_no_bloquea_sin_token(monkeypatch):
    """El limite se aplica DESPUES de autenticar: un 401 no gasta cupo."""
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 2)
    http = listener.app.test_client()
    for _ in range(5):
        assert http.post("/webhook", json=payload()).status_code == 401
    listener.return_service.client = ClienteFalso()
    assert http.post("/webhook", json=payload(event_id="rl5"), headers=AUTH).status_code == 200
    from conftest import drenar_pools
    drenar_pools()


def test_health_expone_el_rate_limit():
    cuerpo = listener.app.test_client().get("/health").get_json()
    assert cuerpo["config"]["rate_limit_per_minute"] == config.RATE_LIMIT_PER_MINUTE
    assert "rechazados_por_rate_limit" in cuerpo["metricas"]


# --------------------------------------------------- E-7: id de correlacion
def test_el_filtro_agrega_el_id_solo_cuando_hay_uno():
    filtro = _FiltroCorrelacion()
    registro = logging.LogRecord("x", logging.INFO, "f", 1, "hola", None, None)

    limpiar_correlacion()
    filtro.filter(registro)
    assert registro.cid == "", "sin id, la linea queda igual que antes"

    set_correlacion("id:abc")
    filtro.filter(registro)
    assert registro.cid == "[id:abc] "
    limpiar_correlacion()


def test_procesar_webhook_setea_y_limpia_el_id(mails):
    """Los threads del pool se reusan: el id no puede sobrevivir al webhook."""
    listener.return_service.client = ClienteFalso()
    vistos = []

    original = listener.return_service.create_return

    def espia(data):
        vistos.append(get_correlacion())
        return original(data)

    listener.return_service.create_return = espia
    try:
        listener.procesar_webhook(payload(event_id="cid-1"), "id:cid-1")
    finally:
        listener.return_service.create_return = original

    assert vistos == ["id:cid-1"], "durante el procesamiento tiene que estar seteado"
    assert get_correlacion() is None, "al terminar tiene que quedar limpio"


# ------------------------------------------------------------------ C-2
# Los loggers del proyecto tienen propagate=False para que gunicorn no duplique
# cada linea, asi que caplog -- que engancha en el root logger -- no los ve. Se
# lee el stdout capturado, que es adonde escriben de verdad.
class _CajaQueSeCrea(ClienteFalso):
    """El carton no existe, asi que se dispara el create_carton."""

    codigo_devuelto = None  # None = devuelve el mismo que se pidio

    def check_carton(self, code):
        self.llamadas.append(("check_carton", code))
        return False

    def create_carton(self, data, client_id):
        self.llamadas.append(("create_carton", data.get("Code")))
        return {
            "Success": True,
            "ID": 1,
            "Code": self.codigo_devuelto or data.get("Code"),
        }


def test_si_mintsoft_crea_la_caja_con_otro_codigo_se_avisa(mails):
    """Antes la respuesta de create_carton se descartaba, asi que un codigo
    distinto no se veia y el transfer fallaba sin explicar por que."""
    cli = _CajaQueSeCrea()
    cli.codigo_devuelto = "OTRO-CODIGO"
    listener.return_service.client = cli

    with capturar_logs("mintsoft_service") as logs:
        listener.procesar_webhook(payload([item("X", put_away_bin="BOX-1")]))

    assert cli.hizo("create_carton"), "la caja no existia: tenia que crearse"
    assert "OTRO-CODIGO" in logs.texto, "el codigo distinto tiene que quedar registrado"
    assert "puede terminar en otra caja" in logs.texto


def test_si_el_codigo_coincide_no_avisa_nada(mails):
    cli = _CajaQueSeCrea()
    listener.return_service.client = cli

    with capturar_logs("mintsoft_service") as logs:
        listener.procesar_webhook(payload([item("X", put_away_bin="BOX-1")]))

    assert cli.hizo("create_carton")
    assert "distinto del put_away_bin" not in logs.texto


# ------------------------------------------------------------------ S-5
def test_el_sleep_del_alta_de_producto_es_configurable(monkeypatch):
    assert config.PRODUCT_CREATE_SLEEP_SECONDS == 3.0, \
        "el default tiene que ser el comportamiento de antes"

    monkeypatch.setenv("PRODUCT_CREATE_SLEEP_SECONDS", "0")
    try:
        recargado = importlib.reload(config)
        assert recargado.PRODUCT_CREATE_SLEEP_SECONDS == 0.0
    finally:
        monkeypatch.delenv("PRODUCT_CREATE_SLEEP_SECONDS")
        importlib.reload(config)


# ------------------------------------------------------------------ S-6
def test_la_alerta_se_encola_en_vez_de_bloquear(monkeypatch):
    """map_client se llama desde el thread que procesa el return: no puede
    quedarse esperando a un SMTP lento."""
    import mappers.mintsoft_mapper as mm

    encoladas = []

    class EjecutorEspia:
        def submit(self, fn, *a, **k):
            encoladas.append(a[0])

    mm._alert_last_sent.clear()
    monkeypatch.setattr(mm, "_ejecutor_alertas", EjecutorEspia())
    mm._send_alert_email("asunto de prueba", "cuerpo")

    assert encoladas == ["asunto de prueba"], "el envio tiene que delegarse"


def test_el_throttle_se_evalua_antes_de_encolar(monkeypatch):
    """Que el envio sea asincronico no puede cambiar CUANTOS mails salen."""
    import mappers.mintsoft_mapper as mm

    encoladas = []

    class EjecutorEspia:
        def submit(self, fn, *a, **k):
            encoladas.append(a[0])

    mm._alert_last_sent.clear()
    monkeypatch.setattr(mm, "_ejecutor_alertas", EjecutorEspia())
    for _ in range(5):
        mm._send_alert_email("mismo asunto", "cuerpo")

    assert len(encoladas) == 1, "el throttle sigue agrupando los repetidos"


# ------------------------------------------------------------------ S-7
def test_el_fixture_de_locations_documenta_las_cuatro_que_usa_el_codigo():
    import json
    import os

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "models",
                           "mintsoft_warehouse_locations_model.json")) as fh:
        doc = json.load(fh)

    ids = {loc["ID"] for loc in doc["locations"]}
    esperados = set()
    for warehouse in config.RETURN_LOCATIONS:
        esperados.add(config.location_id(warehouse, buen_estado=True))
        esperados.add(config.location_id(warehouse, buen_estado=False))

    assert ids == esperados, \
        "el fixture tiene que documentar exactamente las locations configuradas"
