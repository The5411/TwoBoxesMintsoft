"""Arnes de tests.

No toca la red, y esta hecho cumplir: `red_bloqueada` hace fallar cualquier
conexion que no sea loopback, asi que un camino nuevo sin stub es un error
ruidoso en vez de una llamada real a produccion. Mintsoft, SMTP y Google estan
stubeados; el Postgres de CI corre en localhost, que si esta permitido.
"""
import os
import sys
import smtplib
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

# El store de eventos va a una base temporal, NO a webhook_events.db del repo:
# los tests no pueden ensuciar (ni depender de) el estado de desarrollo.
# Se setea antes de importar cualquier modulo del proyecto, porque config.py lee
# el entorno al importarse.
_DIR_STORE = tempfile.mkdtemp(prefix="tb-store-")
os.environ["STORE_PATH"] = os.path.join(_DIR_STORE, "eventos.db")
os.environ.pop("DATABASE_URL", None)

os.environ.setdefault("MINTSOFT_USERNAME", "test")
os.environ.setdefault("MINTSOFT_PASSWORD", "test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("SMTP_HOST", "smtp.test")
os.environ.setdefault("SMTP_USER", "test@test")
os.environ.setdefault("SMTP_PASSWORD", "test")
for var in ("GAS_URL", "WEBHOOKS_URL"):
    os.environ.pop(var, None)

import pytest


@pytest.fixture(autouse=True)
def red_bloqueada(monkeypatch):
    """Hace fallar cualquier conexion de red real, en vez de dejarla colgar.

    Los stubs de `sin_red` cubren los caminos conocidos, pero un camino nuevo --
    o una tarea que sobreviva al teardown -- salia a internet de verdad: la suite
    llego a colgarse 30 segundos posteando a la GAS_URL de produccion con los
    retries y el timeout de 120s. Con esto, eso es un error inmediato y ruidoso.

    Va en su propio fixture, y antes de `sin_red` en el orden de resolucion, para
    que tambien cubra el armado de los stubs.
    """
    import socket

    # Loopback si: es donde corre el Postgres de CI, que los tests de
    # tests/test_store_backends.py necesitan de verdad. Todo lo demas -- Mintsoft,
    # Google, SMTP -- tiene que estar stubeado.
    LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
    connect_real = socket.socket.connect
    connect_ex_real = socket.socket.connect_ex

    def es_loopback(direccion):
        if isinstance(direccion, (tuple, list)) and direccion:
            return str(direccion[0]) in LOOPBACK
        # Los sockets unix (AF_UNIX) son una ruta local: tampoco salen a la red.
        return isinstance(direccion, (str, bytes))

    def guardia(real, nombre):
        def envoltorio(self, direccion, *a, **k):
            if es_loopback(direccion):
                return real(self, direccion, *a, **k)
            raise AssertionError(
                f"Los tests no pueden salir a la red ({nombre} a {direccion!r}). "
                f"Si un camino nuevo necesita HTTP, stubealo en conftest.py."
            )
        return envoltorio

    monkeypatch.setattr(socket.socket, "connect", guardia(connect_real, "connect"))
    monkeypatch.setattr(
        socket.socket, "connect_ex", guardia(connect_ex_real, "connect_ex")
    )
    yield


@pytest.fixture(autouse=True)
def sin_red(monkeypatch, red_bloqueada):
    """Corta toda salida a la red y captura los mails."""
    enviados = []

    class SMTPFalso:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): enviados.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", SMTPFalso)

    import clients.mintsoftClient as mc
    monkeypatch.setattr(mc.MintsoftOrderClient, "_authenticate", lambda self: "key-de-test")

    import services.mintsoft_service as ms
    monkeypatch.setattr(ms.time, "sleep", lambda *_: None)

    # El throttle de alertas es estado de modulo: sin limpiarlo, el primer test
    # que manda una alerta suprime la del siguiente durante 30 minutos.
    import mappers.mintsoft_mapper as mm
    mm._alert_last_sent.clear()

    import listener
    # Cada test arranca con el store vacio: si no, el claim del segundo test ve
    # el evento del primero y no procesa nada.
    limpiar_store(listener.event_store)
    respuesta_ok = type("R", (), {
        "status_code": 200,
        "raise_for_status": lambda self: None,
        "json": lambda self: {},
    })
    monkeypatch.setattr(listener.session, "post", lambda *a, **k: respuesta_ok())
    monkeypatch.setattr(listener.requests, "post", lambda *a, **k: respuesta_ok())
    # Las URLs de Google se neutralizan a nivel de modulo: conftest las saca del
    # entorno, pero listener.py llama a load_dotenv() al importarse y las vuelve a
    # cargar desde .env -- que en una maquina de desarrollo tiene las de
    # PRODUCCION. Con esto, incluso una llamada que se escape no sale a la red.
    monkeypatch.setattr(listener, "GAS_URL", None)
    monkeypatch.setattr(listener, "WEBHOOKS_URL", None)

    yield enviados

    # Drenar los DOS pools antes de que monkeypatch deshaga los stubs.
    #
    # Sin esto, un test que solo esperaba `executor` dejaba tareas de archivado
    # encoladas en `archivo_executor`. Esas tareas corrian DESPUES del teardown,
    # ya sin los stubs, asi que llamaban al requests.post de verdad contra la
    # GAS_URL real: la suite hacia red y se colgaba hasta 30 segundos por los
    # retries y el timeout de 120s. monkeypatch se deshace despues de este
    # fixture (lo pide `sin_red`), asi que aca los stubs todavia estan puestos.
    drenar_pools()


def limpiar_store(store):
    """Borra todas las filas del store. Los tests comparten el archivo.

    Toma el mismo lock que usa el store: sin eso, si quedara una conexion
    abierta de un thread anterior, esta esperaria el busy_timeout de 15s de
    SQLite antes de fallar.
    """
    with store._lock:
        con = store._conectar()
        try:
            con.cursor().execute("DELETE FROM webhook_events")
            con.commit()
        finally:
            con.close()


def drenar_pools():
    """Espera a que terminen los dos pools y los reemplaza.

    Hay que reemplazarlos porque un ThreadPoolExecutor apagado no acepta mas
    trabajo. Es lo unico que garantiza que no quede una tarea corriendo despues
    del test que la encolo.
    """
    import concurrent.futures
    import listener

    for nombre in ("executor", "archivo_executor"):
        getattr(listener, nombre).shutdown(wait=True)
        setattr(listener, nombre, concurrent.futures.ThreadPoolExecutor(max_workers=4))


def procesar_en_background(cuerpo, headers=None):
    """Postea a /webhook y espera a que los dos pools terminen.

    Devuelve la respuesta HTTP.
    """
    import listener

    http = listener.app.test_client()
    r = http.post("/webhook", json=cuerpo,
                  headers=headers or {"x-two-boxes-authorization": "test-secret"})
    drenar_pools()
    return r


@pytest.fixture
def mails(sin_red):
    return sin_red


def item(sku="SKU-1", disposition="Exception", put_away_bin="BOX-1", barcode="123456789"):
    d = {
        "sku": sku,
        "disposition": disposition,
        "quantity": 1,
        "tracking_number": "TRK-999",
        "storefront_order_number": "#A1",
        "merchant": {"name": "Bronze Snake"},
        "product_variant": {"barcode": barcode, "sku": sku},
    }
    if put_away_bin:
        d["put_away_bin"] = put_away_bin
    if barcode is not None:
        d["barcode"] = barcode
    return d


def payload(items=None, event_type="return-complete", merchant="Bronze Snake", event_id="evt-1"):
    items = [dict(i) for i in (items if items is not None else [item()])]
    # El merchant tiene que ir tambien en cada line item: _get_merchant_name mira
    # line_items[0].merchant ANTES que event_data.merchant, asi que dejarlo solo
    # arriba hacia que el payload no representara lo que el test dice representar.
    for i in items:
        i["merchant"] = {"name": merchant} if merchant else None
    return {
        "id": event_id,
        "event_type": event_type,
        "event_data": {
            "completed_at": "2026-09-11T12:00:00Z",
            "customer": {"email": "a@b.com", "full_name": "Test Cliente"},
            "rma_address": {"address_1": "Calle 1", "zip": "1000"},
            "merchant": {"name": merchant} if merchant else None,
            "line_items": items,
        },
    }


class ClienteFalso:
    """Mintsoft simulado. Los flags cambian el camino que se quiere ejercitar."""

    def __init__(self, orden_existe=True, add_item_ok=True, transfer_ok=True,
                 buscar_orden_falla=False, producto_id=1, allocate_ok=True,
                 order_status_id=4):
        self.orden_existe = orden_existe
        self.add_item_ok = add_item_ok
        self.transfer_ok = transfer_ok
        self.buscar_orden_falla = buscar_orden_falla
        self.producto_id = producto_id
        # allocate_ok=False simula el caso W836: el return SI se crea en Mintsoft
        # y el paso siguiente falla, dejando el return a medio armar.
        self.allocate_ok = allocate_ok
        # 4 = DESPATCHED. Un estado fuera de RETURNABLE_ORDER_STATUS_IDS hace que
        # la orden exista pero no habilite un return interno.
        self.order_status_id = order_status_id
        self.llamadas = []

    def search_orders(self, termino, **k):
        self.llamadas.append(("search_orders", termino))
        if self.buscar_orden_falla:
            raise RuntimeError("Order/Search: timeout contra Mintsoft")
        if not self.orden_existe:
            return []
        # Devuelve una orden que matchea el termino buscado, como hace Mintsoft.
        # Antes devolvia siempre 'A1', asi que cualquier payload con otro numero
        # de orden -- los fixtures reales de models/, por ejemplo -- caia en el
        # camino de return externo sin que el test lo dijera.
        return [{"ID": 77, "ClientId": 110, "OrderNumber": termino,
                 "ExternalOrderReference": termino,
                 "OrderStatusId": self.order_status_id}]

    def get_product_id(self, sku, client_id, barcode):
        self.llamadas.append(("get_product_id", sku))
        return sku, self.producto_id

    def create_return(self, order_id, **k):
        self.llamadas.append(("create_return", order_id)); return 555

    def create_external_return(self, data):
        self.llamadas.append(("create_external_return", len(data["ReturnItems"]))); return 12772

    def get_return_details(self, rid):
        return {"ReturnItems": [{"ID": 1, "Quantity": 1, "ReturnReasonId": 2}]}

    def add_return_item(self, rid, d):
        self.llamadas.append(("add_return_item", d.get("ProductId")))
        if not self.add_item_ok:
            return {"Success": False, "Message": "AddItem rechazado por Mintsoft"}
        return {"Success": True, "ID": 9}

    def allocate_return_item_location(self, rid, d):
        self.llamadas.append(("allocate", d.get("LocationId")))
        if not self.allocate_ok:
            raise RuntimeError("Mintsoft rechazo AllocateItemLocation")
        return {"Success": True}

    def confirm_return(self, rid):
        self.llamadas.append(("confirm_return", rid)); return {"Success": True}

    def quarantine_stock(self, req, timeout=120):
        self.llamadas.append(("quarantine_stock", req.get("ProductId"))); return {"Success": True}

    def check_carton(self, code):
        self.llamadas.append(("check_carton", code)); return True

    def create_carton(self, data, client_id):
        self.llamadas.append(("create_carton", data.get("Code"))); return {"Success": True}

    def transfer_stock(self, d):
        self.llamadas.append(("transfer_stock", d.get("DestinationNameOrCode")))
        if not self.transfer_ok:
            raise RuntimeError(
                f"Mintsoft rechazo TransferStock ('{d['SourceNameOrCode']}' -> "
                f"'{d['DestinationNameOrCode']}'): 'Could not find any of product ID: 1 "
                f"in {d['SourceNameOrCode']}!'")
        return {"Success": True}

    def fetch_products_in_locations(self, *a, **k):
        return []

    def hizo(self, nombre):
        return [c for c in self.llamadas if c[0] == nombre]
