"""Contrato del endpoint: auth, filtrado por tipo, duplicados y PII."""
import pytest

from conftest import ClienteFalso, item, payload
import listener


@pytest.fixture
def cliente_http():
    listener.return_service.client = ClienteFalso()
    return listener.app.test_client()


AUTH = {"x-two-boxes-authorization": "test-secret"}


def test_sin_token_rechaza(cliente_http):
    assert cliente_http.post("/webhook", json=payload()).status_code == 401


def test_token_incorrecto_rechaza(cliente_http):
    r = cliente_http.post("/webhook", json=payload(),
                          headers={"x-two-boxes-authorization": "otra-cosa"})
    assert r.status_code == 401


def test_token_correcto_acepta(cliente_http):
    assert cliente_http.post("/webhook", json=payload(), headers=AUTH).status_code == 200


@pytest.mark.parametrize("cuerpo", [
    {"event_type": "x", "event_data": {"line_items": ["no es un dict"]}},
    {"event_type": "x", "event_data": [{"forma": "inesperada"}]},
    [{"event_type": "x", "event_data": {}}],
    {"event_type": "return-complete"},
])
def test_payload_deforme_sigue_devolviendo_200(cliente_http, cuerpo):
    """Un 5xx haria que Two Boxes reintente, que es justo lo que no queremos."""
    assert cliente_http.post("/webhook", json=cuerpo, headers=AUTH).status_code == 200


# ---------------------------------------------------------------- COR-26
def test_event_type_no_soportado_no_llega_a_mintsoft(cliente_http):
    cli = ClienteFalso()
    listener.return_service.client = cli
    cliente_http.post("/webhook", json=payload(event_type="return-created"), headers=AUTH)
    listener.executor.shutdown(wait=True)
    import concurrent.futures
    listener.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    assert cli.llamadas == [], "un tipo no soportado no puede escribir en el WMS"


# ---------------------------------------------------------------- COR-07 (parcial)
def test_mismo_event_id_dos_veces_procesa_una_sola(cliente_http):
    cli = ClienteFalso()
    listener.return_service.client = cli
    for _ in range(2):
        cliente_http.post("/webhook", json=payload(event_id="evt-repetido"), headers=AUTH)
    listener.executor.shutdown(wait=True)
    import concurrent.futures
    listener.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    assert len(cli.hizo("create_return")) == 1, "el segundo envio no debe crear otro return"


# ---------------------------------------------------------------- SEC-02
def test_el_log_no_expone_datos_del_comprador():
    red = listener._redactar_pii(payload())
    assert red["event_data"]["customer"] == "<redactado>"
    assert red["event_data"]["rma_address"] == "<redactado>"
    assert red["event_data"]["line_items"][0]["tracking_number"] == "TRK-999"


def test_health_responde_sin_tocar_mintsoft(cliente_http):
    r = cliente_http.get("/health")
    assert r.status_code == 200
    cuerpo = r.get_json()
    assert cuerpo["status"] == "ok"
    assert "metricas" in cuerpo
