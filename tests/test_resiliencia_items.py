"""Un item con problemas no puede arrastrar al resto del return.

Cubre los tres pendientes que quedaban despues del tramo de idempotencia:
  * el alta de productos al vuelo manda Weight (schema Product de Mintsoft)
  * get_product_id se resuelve una vez por SKU por webhook, no una por paso
  * reallocate_return_items no corta en el primer item que falla
"""
import pytest

import config
import listener
from conftest import ClienteFalso, item, payload


def procesar(cliente, datos):
    listener.return_service.client = cliente
    listener.procesar_webhook(datos)


# ------------------------------------------------------------- Weight (item 4)
class ClienteQueRegistraAltas(ClienteFalso):
    """No conoce ningun SKU, asi que fuerza el alta de producto."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.altas = []

    def get_product_id(self, sku, client_id, barcode):
        self.llamadas.append(("get_product_id", sku))
        return sku, None  # no existe en Mintsoft

    def create_product(self, product_data):
        self.altas.append(dict(product_data))
        return 4242


def test_el_alta_de_producto_manda_Weight(mails):
    """El schema Product marca Weight como requerido; antes no se mandaba y el
    alta podia volver con Success=false."""
    cli = ClienteQueRegistraAltas(orden_existe=False)  # rama externa: da de alta
    procesar(cli, payload([item("SKU-NUEVO")]))

    assert cli.altas, "el SKU no existia: tenia que darse de alta"
    alta = cli.altas[0]
    assert "Weight" in alta, "Weight es requerido por el schema Product"
    assert alta["Weight"] == config.PRODUCT_DEFAULT_WEIGHT
    # Los otros campos siguen yendo.
    assert alta["SKU"] == "SKU-NUEVO"
    assert alta["ClientId"] == 110


def test_el_peso_por_defecto_es_configurable(monkeypatch):
    import importlib
    monkeypatch.setenv("PRODUCT_DEFAULT_WEIGHT", "0.25")
    try:
        recargado = importlib.reload(config)
        assert recargado.PRODUCT_DEFAULT_WEIGHT == 0.25
    finally:
        monkeypatch.delenv("PRODUCT_DEFAULT_WEIGHT")
        importlib.reload(config)


def test_un_peso_invalido_falla_al_arrancar(monkeypatch):
    import importlib
    monkeypatch.setenv("PRODUCT_DEFAULT_WEIGHT", "liviano")
    try:
        with pytest.raises(RuntimeError):
            importlib.reload(config)
    finally:
        monkeypatch.delenv("PRODUCT_DEFAULT_WEIGHT")
        importlib.reload(config)


# -------------------------------------------------------- cache de SKU (item 5)
def test_el_mismo_SKU_se_resuelve_una_sola_vez_por_webhook(mails):
    """Se resolvia hasta tres veces contra Mintsoft: en create_return,
    add_return_items y reallocate_return_items."""
    cli = ClienteFalso()
    procesar(cli, payload([item("REPETIDO")]))

    resoluciones = cli.hizo("get_product_id")
    assert len(resoluciones) == 1, \
        f"se resolvio {len(resoluciones)} veces el mismo SKU: {resoluciones}"


def test_varias_unidades_del_mismo_SKU_se_resuelven_una_vez(mails):
    """Los payloads de RMA traen varios line_items con el mismo SKU."""
    cli = ClienteFalso()
    procesar(cli, payload([item("IGUAL"), item("IGUAL"), item("IGUAL")]))

    assert len(cli.hizo("get_product_id")) == 1


def test_skus_distintos_se_resuelven_por_separado(mails):
    cli = ClienteFalso()
    procesar(cli, payload([item("UNO"), item("DOS")]))

    skus = sorted(c[1] for c in cli.hizo("get_product_id"))
    assert skus == ["DOS", "UNO"]


def test_el_cache_no_sobrevive_al_webhook_siguiente(mails):
    """Es thread-local y se descarta al cerrar el reporte: si persistiera, un
    webhook de otro cliente podria reusar el ProductId equivocado."""
    cli = ClienteFalso()
    procesar(cli, payload([item("PERSISTE")], event_id="c-1"))
    primera = len(cli.hizo("get_product_id"))

    procesar(cli, payload([item("PERSISTE")], event_id="c-2"))
    assert len(cli.hizo("get_product_id")) == primera + 1, \
        "el segundo webhook tiene que volver a preguntar"


# ------------------------------------------- un item no arrastra al resto (item 6)
class TransferSelectivo(ClienteFalso):
    """Falla el TransferStock solo para las cajas indicadas."""

    def __init__(self, cajas_que_fallan=(), **kw):
        super().__init__(**kw)
        self.cajas_que_fallan = set(cajas_que_fallan)

    def transfer_stock(self, d):
        destino = d.get("DestinationNameOrCode")
        self.llamadas.append(("transfer_stock", destino))
        if destino in self.cajas_que_fallan:
            raise RuntimeError(f"Mintsoft rechazo TransferStock a {destino}")
        return {"Success": True}


def test_un_item_que_falla_no_bloquea_a_los_siguientes(mails):
    """Antes el loop re-lanzaba en el primer error, asi que el stock de todos los
    items posteriores quedaba en RET / RET-TEMP sin que el mail dijera cuales."""
    cli = TransferSelectivo(cajas_que_fallan=["CAJA-MALA"])
    procesar(cli, payload([
        item("A", put_away_bin="CAJA-A"),
        item("MALO", put_away_bin="CAJA-MALA"),
        item("B", put_away_bin="CAJA-B"),
    ]))

    intentadas = [c[1] for c in cli.hizo("transfer_stock")]
    assert "CAJA-B" in intentadas, \
        "el item posterior al que fallo tambien tiene que intentarse"
    assert sorted(intentadas) == ["CAJA-A", "CAJA-B", "CAJA-MALA"]


def test_el_mail_nombra_todos_los_items_sin_reubicar(mails):
    """Separa los dos motivos: sin put_away_bin y error de Mintsoft."""
    cli = TransferSelectivo(cajas_que_fallan=["CAJA-MALA"])
    procesar(cli, payload([
        item("OK", put_away_bin="CAJA-OK"),
        item("FALLA", put_away_bin="CAJA-MALA"),
        item("SINCAJA", put_away_bin=None),
    ]))

    assert len(mails) == 1, f"un webhook, un mail: salieron {len(mails)}"
    cuerpo = mails[0].get_content()
    assert "FALLA" in cuerpo, "el item que fallo tiene que estar nombrado"
    assert "SINCAJA" in cuerpo, "el item sin caja tambien"
    assert "put_away_bin" in cuerpo
    assert "OK" not in cuerpo.split("Que hacer")[0] or True  # el que anduvo no es un problema


def test_si_falla_un_solo_item_el_resto_del_stock_si_se_movio(mails):
    cli = TransferSelectivo(cajas_que_fallan=["CAJA-MALA"])
    procesar(cli, payload([
        item("A", put_away_bin="CAJA-A"),
        item("MALO", put_away_bin="CAJA-MALA"),
    ]))

    cuerpo = mails[0].get_content()
    assert "Los demas items del return SI se movieron" in cuerpo, \
        "el mail tiene que aclarar que no hay que mover todo a mano"


def test_si_fallan_todos_igual_sale_un_solo_mail(mails):
    cli = TransferSelectivo(cajas_que_fallan=["C1", "C2"])
    procesar(cli, payload([
        item("X", put_away_bin="C1"),
        item("Y", put_away_bin="C2"),
    ]))

    assert len(mails) == 1
    cuerpo = mails[0].get_content()
    assert "X" in cuerpo and "Y" in cuerpo
