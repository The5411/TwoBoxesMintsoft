"""E-5: los tres payloads reales de models/ como casos base.

Son capturas de produccion (RMA, Work Capture y Return To Sender), asi que
cubren formas que los payloads sinteticos de conftest.py no tienen: barcode en
null con el real en product_variant, put_away_bin ausente, merchant solo en
merchant_integration, y varios line items en un mismo evento.
"""
import json
import os

import pytest

import config
import listener
from conftest import ClienteFalso, payload, procesar_en_background

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FIXTURES = {
    "rma": "tb_rma_model.json",
    "work_capture": "tb_work_capture_model.json",
    "return_to_sender": "tb_return_to_sender_model.json",
}


def cargar(nombre):
    """El evento de un fixture. Los archivos guardan una lista de un elemento."""
    with open(os.path.join(RAIZ, "models", FIXTURES[nombre]), encoding="utf-8") as fh:
        return json.load(fh)[0]


def con_merchant(evento, nombre="bronze snake"):
    """El mismo evento pero atribuido a un merchant que SI esta en la tabla.

    Los tres fixtures vienen de merchants cuyo nombre no coincide con ningun
    `tb_name` ('Kenny Flowers', 'Deiji Studios', 'test client'), asi que sin esto
    todos terminan en el camino de 'cliente no mapeado'. Ese camino se prueba
    aparte, en test_merchant_real_no_mapeado_no_escribe_en_mintsoft.
    """
    evento = json.loads(json.dumps(evento))  # copia profunda
    evento["event_data"]["merchant"] = {"name": nombre}
    for li in evento["event_data"].get("line_items") or []:
        if isinstance(li, dict):
            li["merchant"] = {"name": nombre}
    evento["event_data"].pop("merchant_integration", None)
    return evento


# ----------------------------------------------- los fixtures son lo que decimos
@pytest.mark.parametrize("nombre", sorted(FIXTURES))
def test_el_fixture_tiene_la_forma_de_un_webhook_de_two_boxes(nombre):
    evento = cargar(nombre)
    assert evento["event_type"] == "return-complete"
    assert evento["id"], "sin id no hay clave de idempotencia estable"
    assert isinstance(evento["event_data"]["line_items"], list)
    assert evento["event_data"]["line_items"], "un fixture sin items no prueba nada"


# ------------------------------------------------- extraccion sobre datos reales
@pytest.mark.parametrize("nombre,esperado", [
    ("rma", "test client"),
    ("work_capture", "Deiji Studios"),
    ("return_to_sender", "Kenny Flowers"),
])
def test_el_merchant_se_extrae_de_los_payloads_reales(nombre, esperado):
    """_get_merchant_name mira tres lugares distintos segun el tipo de payload."""
    assert listener.return_service._get_merchant_name(cargar(nombre)) == esperado


@pytest.mark.parametrize("nombre,esperado", [
    ("rma", "1ZEW43670320580052"),
    ("work_capture", "1ZY287C40316293924"),
    ("return_to_sender", "LK246182676AU"),
])
def test_la_reference_es_el_tracking_del_primer_item(nombre, esperado):
    """Es el 'PO reference' con el que se busca el return en Mintsoft."""
    assert listener.return_service._return_identifier(cargar(nombre)) == esperado
    assert len(esperado) <= config.REFERENCE_MAX_LEN


def test_el_barcode_del_RMA_sale_de_product_variant():
    """En los payloads de RMA line_items[].barcode viene null y el real esta
    en line_items[].product_variant.barcode."""
    from services.mintsoft_service import _get_item_barcode

    item = cargar("rma")["event_data"]["line_items"][0]
    assert item.get("barcode") is None, "el fixture dejo de representar este caso"
    # No se exige un valor: se exige que no explote y que no invente uno.
    assert _get_item_barcode(item) in (None, (item.get("product_variant") or {}).get("barcode"))


# --------------------------------------------------- procesamiento de punta a punta
@pytest.mark.parametrize("nombre", sorted(FIXTURES))
def test_un_payload_real_no_rompe_el_endpoint(nombre):
    """Un 5xx haria que Two Boxes reintente; nunca puede pasar."""
    listener.return_service.client = ClienteFalso()
    r = procesar_en_background(cargar(nombre))
    assert r.status_code == 200


def test_work_capture_completo_crea_el_return_y_mueve_el_stock(mails):
    """El fixture con todo en orden: un item, Return to Stock, con put_away_bin."""
    cli = ClienteFalso()
    listener.return_service.client = cli

    procesar_en_background(con_merchant(cargar("work_capture")))

    assert cli.hizo("create_return"), "la orden existe: tiene que ser un return interno"
    assert cli.hizo("confirm_return")
    destinos = [c[1] for c in cli.hizo("transfer_stock")]
    assert destinos == ["DEIJI-RETURNS-118"], "el stock va al put_away_bin del payload"
    assert mails == [], f"no deberia avisar nada: {[m['Subject'] for m in mails]}"


def test_return_to_sender_mueve_los_dos_items_a_sus_cajas(mails):
    """Dos line items, cada uno con su put_away_bin distinto."""
    cli = ClienteFalso()
    listener.return_service.client = cli

    procesar_en_background(con_merchant(cargar("return_to_sender")))

    destinos = sorted(c[1] for c in cli.hizo("transfer_stock"))
    assert destinos == ["KF-RP-3-G", "TB-RP-1-Y"]
    assert mails == []


def test_rma_sin_put_away_bin_avisa_y_no_mueve_stock(mails):
    """Los dos items del fixture de RMA vienen sin put_away_bin (COR-04)."""
    cli = ClienteFalso()
    listener.return_service.client = cli

    procesar_en_background(con_merchant(cargar("rma")))

    assert cli.hizo("transfer_stock") == [], "sin caja destino no se mueve nada"
    assert cli.hizo("create_carton") == [], "no se crea una caja con codigo vacio"
    assert len(mails) == 1
    assert "put_away_bin" in mails[0].get_content()


def test_merchant_real_no_mapeado_no_escribe_en_mintsoft(mails):
    """Los tres fixtures traen merchants que no estan en la tabla del mapper.

    'Deiji Studios' no matchea porque la tabla usa tb_name 'deiji studios
    ecommerce'. El comportamiento correcto es no inventar un ClientId.
    """
    cli = ClienteFalso()
    listener.return_service.client = cli

    procesar_en_background(cargar("work_capture"))

    assert cli.hizo("create_return") == []
    assert cli.hizo("create_external_return") == []
    assert any("no mapeado" in m["Subject"] for m in mails), \
        f"tiene que avisar: {[m['Subject'] for m in mails]}"


def test_el_mismo_payload_real_dos_veces_crea_un_solo_return():
    """Idempotencia sobre un evento real, con su id de Two Boxes (E-1)."""
    cli = ClienteFalso()
    listener.return_service.client = cli
    evento = con_merchant(cargar("work_capture"))

    procesar_en_background(evento)
    procesar_en_background(evento)

    assert len(cli.hizo("create_return")) == 1
