"""Que casos avisan por mail, cuales no, y que el throttle no inunde."""
import pytest

from conftest import ClienteFalso, item, payload, procesar_en_background
import listener

AUTH = {"x-two-boxes-authorization": "test-secret"}

# Drena los DOS pools. Antes esta funcion apagaba solo `executor` y dejaba las
# tareas de archivado corriendo despues del test.
postear = procesar_en_background


def test_COR26_event_type_no_soportado_avisa_y_no_escribe(mails):
    cli = ClienteFalso(); listener.return_service.client = cli
    r = postear(payload(event_type="return-created", event_id="e-26"))
    assert r.status_code == 200
    assert cli.llamadas == [], "no puede escribir en Mintsoft"
    assert len(mails) == 1, "tiene que avisar"
    asunto = mails[0]["Subject"]
    cuerpo = mails[0].get_content()
    print(f"\n  COR-26  MAILS={len(mails)}  {asunto}")
    assert "return-created" in asunto
    assert "EVENT_TYPES" in cuerpo, "el mail tiene que decir como habilitarlo"


def test_COR26_throttle_no_inunda(mails):
    """20 eventos del mismo tipo no soportado -> un solo mail."""
    cli = ClienteFalso(); listener.return_service.client = cli
    for i in range(20):
        postear(payload(event_type="return-created", event_id=f"e-{i}"))
    print(f"  COR-26  20 eventos -> MAILS={len(mails)}")
    assert len(mails) == 1


def test_COR06_falla_el_armado_avisa_y_no_mueve_stock(mails):
    cli = ClienteFalso(add_item_ok=False); listener.return_service.client = cli
    listener.procesar_webhook(payload(event_id="e-06"))
    print(f"  COR-06  MAILS={len(mails)}  {mails[0]['Subject'] if mails else '-'}")
    assert cli.hizo("transfer_stock") == []
    assert len(mails) == 1


def test_COR07_duplicado_no_avisa_pero_queda_en_health(mails):
    """Un duplicado es benigno (Two Boxes reintenta): no vale un mail, si un contador."""
    cli = ClienteFalso(); listener.return_service.client = cli
    for _ in range(2):
        postear(payload(event_id="e-dup"))
    http = listener.app.test_client()
    m = http.get("/health").get_json()["metricas"]
    print(f"  COR-07  MAILS={len(mails)}  /health duplicados={m['duplicados']}")
    assert len(cli.hizo("create_return")) == 1
    assert mails == []
    assert m["duplicados"] >= 1
