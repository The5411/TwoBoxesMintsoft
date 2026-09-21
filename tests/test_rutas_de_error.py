"""Los casos que la auditoria marco como base (E-5, seccion 5.3 del plan).

Cada test nombra el hallazgo que cubre.
"""
import json
import os

import pytest

from conftest import ClienteFalso, item, payload
import listener


def procesar(cliente, datos):
    listener.return_service.client = cliente
    listener.procesar_webhook(datos)


# ---------------------------------------------------------------- COR-03
def test_item_missing_no_mueve_stock(mails):
    """Un item que nunca llego no se cuarentena ni se transfiere."""
    cli = ClienteFalso()
    procesar(cli, payload([item("BUENO"), item("FALTA", "Missing", put_away_bin=None)]))

    skus_movidos = [c[1] for c in cli.hizo("transfer_stock")]
    assert cli.hizo("transfer_stock"), "el item bueno si se tiene que mover"
    assert not any("FALTA" in str(c) for c in cli.llamadas), \
        "el item Missing no puede aparecer en ningun movimiento"
    assert mails == [], "un item Missing sin caja no es un error"


def test_todos_missing_no_crea_return(mails):
    """Si ninguna unidad llego, no se registra nada en Mintsoft."""
    cli = ClienteFalso()
    procesar(cli, payload([item("FALTA", "Missing", put_away_bin=None)]))

    assert cli.hizo("create_return") == []
    assert cli.hizo("create_external_return") == []
    assert mails == []


# ---------------------------------------------------------------- COR-04
def test_sin_put_away_bin_no_crea_caja_y_avisa(mails):
    """Sin caja destino no se transfiere stock, y sale un mail."""
    cli = ClienteFalso()
    procesar(cli, payload([item("SINCAJA", put_away_bin=None)]))

    assert cli.hizo("create_carton") == [], "no se puede crear una caja con codigo None"
    assert cli.hizo("transfer_stock") == []
    assert len(mails) == 1
    assert "put_away_bin" in mails[0].get_content()


# ---------------------------------------------------------------- COR-08
def test_barcode_nulo_no_rompe(mails):
    """Los payloads de RMA traen barcode en null: no debe ser un TypeError."""
    cli = ClienteFalso()
    procesar(cli, payload([item("SINBARCODE", barcode=None)]))

    assert cli.hizo("transfer_stock"), "el item se tiene que procesar igual"
    assert mails == []


# ---------------------------------------------------------------- COR-10, COR-14, COR-15
def test_line_items_vacio_no_rompe_el_handler(mails):
    """Un evento sin items se reporta, no explota dentro del manejador."""
    cli = ClienteFalso()
    procesar(cli, payload([]))

    assert all("Traceback" not in m.get_content() or "AttributeError" not in m.get_content()
               for m in mails)


# ---------------------------------------------------------------- COR-01
def test_fallo_al_buscar_la_orden_no_crea_return_externo(mails):
    """'No pude preguntar' no puede degradar a return externo: duplicaria la recepcion."""
    cli = ClienteFalso(buscar_orden_falla=True)
    procesar(cli, payload())

    assert cli.hizo("create_external_return") == [], \
        "un timeout de Order/Search no puede terminar en un return externo"
    assert len(mails) == 1


# ---------------------------------------------------------------- COR-06
def test_si_falla_el_armado_no_se_mueve_stock(mails):
    """Si AddItem devuelve Success: false, no se reubica el stock."""
    cli = ClienteFalso(add_item_ok=False)
    procesar(cli, payload())

    assert cli.hizo("transfer_stock") == [], \
        "no se puede mover stock de un return que quedo sin items"
    assert len(mails) == 1


# ---------------------------------------------------------------- COR-05
def test_transferstock_rechazado_llega_al_mail(mails):
    """Un rechazo de TransferStock tiene que producir un mail, no pasar en silencio."""
    cli = ClienteFalso(transfer_ok=False)
    procesar(cli, payload())

    assert len(mails) == 1
    assert "RET" in mails[0].get_content()


# ---------------------------------------------------------------- COR-16 / COR-28
def test_merchant_no_mapeado_no_crea_nada(mails):
    """Un merchant que no esta en la tabla no puede terminar en un ClientId inventado."""
    cli = ClienteFalso()
    procesar(cli, payload(merchant="Merchant Inexistente SRL"))

    assert cli.hizo("create_return") == []
    assert cli.hizo("create_external_return") == []


# ---------------------------------------------------------------- un webhook = un mail
def test_un_solo_mail_por_webhook(mails):
    """Varias capas pueden fallar; el operador recibe un unico reporte."""
    cli = ClienteFalso(transfer_ok=False)
    procesar(cli, payload([item("A"), item("B", put_away_bin=None)]))

    assert len(mails) == 1, f"se esperaba 1 mail, salieron {len(mails)}"
    cuerpo = mails[0].get_content()
    assert "problema(s)" in cuerpo


# ---------------------------------------------------------------- E-4
def test_una_orden_que_no_salio_del_deposito_no_lleva_return_interno(mails):
    """Los estados que habilitan un return interno son configurables (4, 5, 6).

    Una orden en estado 1 (PENDING) existe en Mintsoft pero todavia no se
    despacho: no corresponde crearle un return interno.
    """
    cli = ClienteFalso(order_status_id=1)
    procesar(cli, payload())

    assert cli.hizo("create_return") == [], \
        "una orden sin despachar no puede recibir un return interno"
    assert cli.hizo("create_external_return"), \
        "se crea como externo, que es el camino correcto"


def test_se_puede_habilitar_un_estado_nuevo_sin_deploy(mails, monkeypatch):
    cli = ClienteFalso(order_status_id=17)
    monkeypatch.setattr(
        listener.return_service, "returnable_status_ids", {4, 5, 6, 17}
    )
    procesar(cli, payload())

    assert cli.hizo("create_return"), "con el estado habilitado va por interno"


# ---------------------------------------------------------------- S-1
def test_la_caja_de_un_item_en_cuarentena_se_crea_en_RET_no_en_RET_TEMP(mails):
    """Si la caja se crea en RET-TEMP, Mintsoft consolida la unidad adentro y el
    TransferStock posterior falla con 'Could not find any of product ID: X in
    RET-TEMP!'. Por eso la caja va siempre a RET."""
    import config

    class SinCaja(ClienteFalso):
        def check_carton(self, code):
            self.llamadas.append(("check_carton", code))
            return False  # la caja no existe: hay que crearla

        def create_carton(self, data, client_id):
            self.llamadas.append(("create_carton", data.get("LocationId")))
            return {"Success": True}

    cli = SinCaja()
    # disposition 'Exception' -> cuarentena, warehouse 5 (bronze snake)
    procesar(cli, payload([item("QT", "Exception")]))

    creadas = [c[1] for c in cli.hizo("create_carton")]
    assert creadas == [config.location_id(5, buen_estado=True)] == [4299], \
        "la caja tiene que crearse en RET (4299), no en RET-TEMP (4304)"
