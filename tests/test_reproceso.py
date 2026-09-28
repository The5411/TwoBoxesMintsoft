"""E-1.4, E-1.5 y E-1.6: saber en que punto quedo un evento y poder rehacerlo.

Antes, un evento que moria a mitad quedaba en 'claimed' para siempre y era
indistinguible de uno que se esta procesando ahora mismo. Estas tres piezas son
las que permiten contestar "que se perdio en el ultimo reinicio".
"""
import json

import pytest

import config
import listener
import reprocesar
from conftest import ClienteFalso, item, payload, procesar_en_background
from storage.event_store import (
    ESTADO_EN_PROCESO,
    ESTADO_FALLADO,
    ESTADO_IGNORADO,
    ESTADO_INTERRUMPIDO,
    ESTADO_PROCESADO,
    PASO_RETURN_CREADO,
    PASO_STOCK_MOVIDO,
)


# ------------------------------------------------------- E-1.4: pasos
def test_un_evento_exitoso_registra_el_ultimo_paso(mails):
    listener.return_service.client = ClienteFalso()
    datos = payload(event_id="paso-ok")
    procesar_en_background(datos)

    reg = listener.event_store.get(listener.event_store.clave_de(datos))
    assert reg["status"] == ESTADO_PROCESADO
    assert reg["step"] == PASO_STOCK_MOVIDO


def test_un_evento_que_fallo_despues_de_crear_el_return_lo_dice(mails):
    """Es la diferencia entre 'reprocesalo' y 'completalo a mano'."""
    listener.return_service.client = ClienteFalso(orden_existe=False, allocate_ok=False)
    datos = payload(event_id="paso-fallo")
    procesar_en_background(datos)

    reg = listener.event_store.get(listener.event_store.clave_de(datos))
    assert reg["status"] == ESTADO_FALLADO
    assert reg["step"] == PASO_RETURN_CREADO, "quedo justo despues de crear el return"
    assert reg["return_id"] == "12772"


# ------------------------------------------------------- E-1.6: barrido
def test_el_barrido_marca_los_claims_viejos_como_interrumpidos(monkeypatch):
    store = listener.event_store
    datos = {"id": "colgado", "event_data": {}}
    v = store.claim(datos, event_id="colgado")
    assert store.get(v.registro["event_key"])["status"] == ESTADO_EN_PROCESO

    # Con la ventana en 0, cualquier claim abierto cuenta como colgado.
    assert store.marcar_interrumpidos(antiguedad_segundos=0) == 1
    assert store.get(v.registro["event_key"])["status"] == ESTADO_INTERRUMPIDO


def test_el_barrido_NO_toca_un_evento_que_se_esta_procesando_ahora():
    """Con varios workers, uno que reinicia no puede pisar el trabajo de otro."""
    store = listener.event_store
    datos = {"id": "en-vuelo", "event_data": {}}
    v = store.claim(datos, event_id="en-vuelo")

    # Ventana normal: un claim de hace un segundo no es un claim colgado.
    assert store.marcar_interrumpidos(antiguedad_segundos=1800) == 0
    assert store.get(v.registro["event_key"])["status"] == ESTADO_EN_PROCESO


def test_un_evento_interrumpido_se_puede_retomar():
    store = listener.event_store
    datos = {"id": "retomable", "event_data": {}}
    store.claim(datos, event_id="retomable")
    store.marcar_interrumpidos(antiguedad_segundos=0)

    v = store.claim(datos, event_id="retomable")
    assert v.otorgado, "un evento interrumpido tiene que poder reintentarse"


def test_el_barrido_no_resucita_un_evento_con_return_creado():
    """Ni siquiera interrumpido justifica crear un segundo return."""
    store = listener.event_store
    datos = {"id": "interrumpido-con-return", "event_data": {}}
    v = store.claim(datos, event_id="interrumpido-con-return")
    store.record_return(v.registro["event_key"], 999, "internal")
    store.marcar_interrumpidos(antiguedad_segundos=0)

    v2 = store.claim(datos, event_id="interrumpido-con-return")
    assert not v2.otorgado
    assert v2.motivo == "return_ya_creado"


# ------------------------------------------------------- E-1.5: el comando
def test_listar_muestra_los_pendientes(mails, capsys):
    listener.return_service.client = ClienteFalso(orden_existe=False, allocate_ok=False)
    datos = payload(event_id="cli-listar")
    procesar_en_background(datos)

    assert reprocesar.main(["listar"]) == 0
    salida = capsys.readouterr().out
    assert "cli-listar" in salida
    assert "a mano" in salida, "tiene que avisar que los que ya crearon return no se reprocesan"


def test_ver_muestra_el_payload_guardado(mails, capsys):
    listener.return_service.client = ClienteFalso()
    datos = payload([item("SKU-VER")], event_id="cli-ver")
    procesar_en_background(datos)

    assert reprocesar.main(["ver", listener.event_store.clave_de(datos)]) == 0
    assert "SKU-VER" in capsys.readouterr().out


def test_correr_se_niega_si_el_return_ya_existe(mails, capsys):
    """La proteccion del caso W836, tambien desde el comando."""
    cli = ClienteFalso(orden_existe=False, allocate_ok=False)
    listener.return_service.client = cli
    datos = payload(event_id="cli-con-return")
    procesar_en_background(datos)

    assert reprocesar.main(["correr", listener.event_store.clave_de(datos)]) == 1
    salida = capsys.readouterr().out
    assert "NO se reprocesa" in salida
    assert "12772" in salida
    assert len(cli.hizo("create_external_return")) == 1, "no puede crear un segundo"


def test_correr_rehace_un_evento_que_no_llego_a_crear_nada(mails, capsys):
    """Si no hubo ninguna escritura, reprocesar es seguro y ademas util."""
    # Primer intento: Mintsoft no responde a la busqueda, no se crea nada.
    listener.return_service.client = ClienteFalso(buscar_orden_falla=True)
    datos = payload(event_id="cli-reintento")
    procesar_en_background(datos)

    clave = listener.event_store.clave_de(datos)
    assert listener.event_store.get(clave)["return_id"] is None

    # Segundo intento: ahora Mintsoft anda.
    cli = ClienteFalso()
    listener.return_service.client = cli
    assert reprocesar.main(["correr", clave]) == 0

    assert cli.hizo("create_return"), "el reproceso tiene que haber creado el return"
    assert listener.event_store.get(clave)["status"] == ESTADO_PROCESADO


def test_correr_no_reprocesa_uno_que_ya_salio_bien(mails, capsys):
    listener.return_service.client = ClienteFalso()
    datos = payload(event_id="cli-ok")
    procesar_en_background(datos)

    assert reprocesar.main(["correr", listener.event_store.clave_de(datos)]) == 1
    assert "ya se proceso con exito" in capsys.readouterr().out


def test_cerrar_saca_el_evento_de_la_lista(mails, capsys):
    listener.return_service.client = ClienteFalso(orden_existe=False, allocate_ok=False)
    datos = payload(event_id="cli-cerrar")
    procesar_en_background(datos)
    clave = listener.event_store.clave_de(datos)

    assert reprocesar.main(["cerrar", clave, "completado", "a", "mano", "en", "Mintsoft"]) == 0
    reg = listener.event_store.get(clave)
    assert reg["status"] == ESTADO_IGNORADO
    assert "completado a mano" in reg["last_error"]

    capsys.readouterr()
    reprocesar.main(["listar"])
    assert clave not in capsys.readouterr().out


def test_una_clave_inexistente_no_rompe(capsys):
    for comando in (["ver", "no-existe"], ["correr", "no-existe"],
                    ["cerrar", "no-existe", "nota"]):
        assert reprocesar.main(comando) == 1
    assert reprocesar.main([]) == 2
