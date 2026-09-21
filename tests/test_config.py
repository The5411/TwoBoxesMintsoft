"""S-1: los ids de Mintsoft salen de config.py, no de numeros escritos a mano."""
import importlib

import pytest

import config


# --------------------------------------------------- locations por warehouse
@pytest.mark.parametrize("warehouse,buen_estado,esperado", [
    (3, True, 4104),   # RET Wholesale
    (3, False, 9),     # RET-TEMP Wholesale
    (5, True, 4299),   # RET E-Commerce
    (5, False, 4304),  # RET-TEMP E-Commerce
])
def test_las_locations_por_defecto_son_las_de_produccion(warehouse, buen_estado, esperado):
    """Los defaults tienen que ser identicos a los ids que estaban hardcodeados."""
    assert config.location_id(warehouse, buen_estado=buen_estado) == esperado


@pytest.mark.parametrize("warehouse", [None, 7, 0, "", "tres"])
def test_un_warehouse_desconocido_lanza_en_vez_de_adivinar(warehouse):
    """Antes el `else` mandaba cualquier warehouse a las locations de E-Commerce.

    Un warehouse=None (merchant no mapeado) terminaba ubicando stock en 4299 /
    4304, que son locations de otro deposito. Fallar es lo correcto.
    """
    with pytest.raises(config.LocationDesconocida):
        config.location_id(warehouse, buen_estado=True)


def test_las_locations_se_pueden_cambiar_sin_deploy(monkeypatch):
    monkeypatch.setenv(
        "RETURN_LOCATIONS",
        '{"3":{"good":111,"quarantine":222},"9":{"good":333,"quarantine":444}}',
    )
    recargado = importlib.reload(config)
    try:
        assert recargado.location_id(3, buen_estado=True) == 111
        assert recargado.location_id(3, buen_estado=False) == 222
        # Un warehouse nuevo se habilita solo con la variable de entorno.
        assert recargado.location_id(9, buen_estado=True) == 333
        # Los que no se tocaron siguen con el default.
        assert recargado.location_id(5, buen_estado=True) == 4299
    finally:
        monkeypatch.delenv("RETURN_LOCATIONS")
        importlib.reload(config)


@pytest.mark.parametrize("valor", ['no es json', '[]', '{"3":{"good":1}}'])
def test_una_configuracion_de_locations_invalida_falla_al_arrancar(monkeypatch, valor):
    """Mejor no levantar que levantar con locations a medio definir."""
    monkeypatch.setenv("RETURN_LOCATIONS", valor)
    try:
        with pytest.raises(RuntimeError):
            importlib.reload(config)
    finally:
        monkeypatch.delenv("RETURN_LOCATIONS")
        importlib.reload(config)


# ------------------------------------------------------ dispositions y reasons
def test_return_to_stock_es_el_unico_que_vuelve_a_stock_vendible():
    assert config.es_buen_estado("Return to Stock") is True
    for otra in ("Exception", "Damaged", "Quarantine", "", None, "return to stock "):
        assert config.es_buen_estado(otra) is False, otra


def test_el_return_reason_sale_de_la_disposition():
    assert config.return_reason_id("Return to Stock") == config.RETURN_REASON_GOOD == 1
    assert config.return_reason_id("Exception") == config.RETURN_REASON_QUARANTINE == 2


def test_es_missing_reconoce_el_item_que_nunca_llego():
    assert config.es_missing({"disposition": "Missing"}) is True
    assert config.es_missing({"disposition": " Missing "}) is True
    assert config.es_missing({"disposition": "Return to Stock"}) is False
    assert config.es_missing({}) is False
    assert config.es_missing(None) is False


def test_los_estados_returnables_son_4_5_6():
    assert config.RETURNABLE_ORDER_STATUS_IDS == {4, 5, 6}


def test_una_variable_entera_invalida_falla_al_arrancar(monkeypatch):
    monkeypatch.setenv("MAX_CONTENT_LENGTH", "cinco megas")
    try:
        with pytest.raises(RuntimeError):
            importlib.reload(config)
    finally:
        monkeypatch.delenv("MAX_CONTENT_LENGTH")
        importlib.reload(config)
