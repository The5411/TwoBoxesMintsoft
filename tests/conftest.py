"""Arnes de tests. No toca la red: Mintsoft, SMTP y Google estan stubeados."""
import os
import sys
import smtplib

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

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
def sin_red(monkeypatch):
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

    import listener
    respuesta_ok = type("R", (), {
        "status_code": 200,
        "raise_for_status": lambda self: None,
        "json": lambda self: {},
    })
    monkeypatch.setattr(listener.session, "post", lambda *a, **k: respuesta_ok())
    monkeypatch.setattr(listener.requests, "post", lambda *a, **k: respuesta_ok())
    listener.MAILS = enviados
    yield enviados


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
                 buscar_orden_falla=False, producto_id=1):
        self.orden_existe = orden_existe
        self.add_item_ok = add_item_ok
        self.transfer_ok = transfer_ok
        self.buscar_orden_falla = buscar_orden_falla
        self.producto_id = producto_id
        self.llamadas = []

    def search_orders(self, termino, **k):
        self.llamadas.append(("search_orders", termino))
        if self.buscar_orden_falla:
            raise RuntimeError("Order/Search: timeout contra Mintsoft")
        if not self.orden_existe:
            return []
        return [{"ID": 77, "ClientId": 110, "OrderNumber": "A1",
                 "ExternalOrderReference": "#A1", "OrderStatusId": 4}]

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
        self.llamadas.append(("allocate", d.get("LocationId"))); return {"Success": True}

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
