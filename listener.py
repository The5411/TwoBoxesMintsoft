from flask import Flask, request, jsonify
import os
import traceback
import requests
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor
from services.mintsoft_service import MintsoftReturnService
from urllib3.util.retry import Retry
from dotenv import load_dotenv
load_dotenv()
app = Flask(__name__)
return_service = MintsoftReturnService()

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
GAS_URL = os.environ.get("GAS_URL")
WEBHOOKS_URL = os.environ.get("WEBHOOKS_URL")


executor = ThreadPoolExecutor(max_workers=10)

# Configuración de reintentos
session = requests.Session()
retries = Retry(
    total=5,               # Reintentos
    backoff_factor=0.3,    # Esperar 0.3 mas con cada reintento
    status_forcelist=[502, 503, 504], # Reintentar si el servidor de Google está saturado
    raise_on_status=False
)
session.mount('https://', HTTPAdapter(max_retries=retries))


def enviar_webhook_a_google(datos):
    try:
        print(WEBHOOKS_URL)
        response = requests.post(
            WEBHOOKS_URL,
            json=datos,
            timeout=60,
            allow_redirects=True  # Crucial para seguir el redireccionamiento /echo de Google
        )
        response.raise_for_status()
        print("✅ Respuesta de Google:", response.json())
    except Exception as e:
        print(f"❌ Error al enviar datos: {e}")

def enviar_webhook_por_sku(datos):
    # Corre en un thread del executor: si dejamos escapar una excepción queda
    # atrapada en el Future y no la ve nadie.
    try:
        event_data = datos.get('event_data', {})
        line_items = event_data.get('line_items', [])

        for item in line_items:
            # Copia superficial del payload conservando la misma estructura,
            # pero con un solo line_item (un SKU) por envío.
            payload = dict(datos)
            payload['event_data'] = dict(event_data)
            payload['event_data']['line_items'] = [item]

            print(f"➡️ Enviando SKU: {item.get('sku')}")
            enviar_webhook_a_google(payload)
    except Exception as e:
        print(f"❌ Error en enviar_webhook_por_sku: {e}")
        traceback.print_exc()



def enviar_a_google_async(datos):
    """Archiva el payload crudo en la planilla de Google Apps Script.

    Antes esto era un `session.post(...)` sin mirar la respuesta y un print de
    exito incondicional. Como el `Retry` esta con `raise_on_status=False`, un
    500 / 403 de Apps Script -- o un GAS_URL sin setear -- se logueaba igual
    como "enviado correctamente" y no se subia nada. Una planilla vacia
    parecia entonces "no llego ningun webhook", que es la conclusion
    exactamente opuesta a la verdadera.
    """
    if not GAS_URL:
        print("❌ GAS_URL no esta seteada: el payload NO se archiva")
        return
    try:
        response = session.post(GAS_URL, json=datos, timeout=120, allow_redirects=True)
        response.raise_for_status()
        print(f"✅ Payload archivado en Google Apps Script (HTTP {response.status_code})")
    except Exception as e:
        print(f"❌ Error enviando a Google: {e}")

def _identificar_return(data):
    """Misma lógica de identificación que usa MintsoftReturnService para los mails
    de error: tracking_number si existe, si no completed_at-email del cliente."""
    try:
        event_data = data.get("event_data") or {}
        line_items = event_data.get("line_items") or []
        if line_items:
            tracking = (line_items[0] or {}).get("tracking_number")
            if tracking:
                return tracking
        completed_at = event_data.get("completed_at")
        customer_email = (event_data.get("customer") or {}).get("email")
        return f"{completed_at}-{customer_email}"
    except Exception:
        return None


def procesar_webhook(data):
    try:
        # Crea return interno o externo
        return_id = return_service.create_return(data)
        print(return_id)

        # Pasar items de RET o RET-QT a la caja del return si es External
        if return_id[1] == "External Return Created":
          # Pasar items a RET o RET-QT
          return_service.allocate_external_return_items(data, return_id[0])

          # Pasar items de RET o RET-QT a la caja del return si es External
          return_service.reallocate_return_items(data)

        # Agregar items al return en caso de que sea interno
        if return_id[1] == "Internal Return Created":
          return_service.add_return_items(return_id[0], data)

        
          # Pasar items de RET o RET-QT a la caja del return si es Internal
          return_service.reallocate_return_items(data)

        print("Webhook procesado con exito")

    except Exception as e:
        # Catch-all: cualquier fallo que no haya sido capturado (y notificado) dentro
        # de MintsoftReturnService llega hasta acá. Sin esto el error solo se imprimía
        # en los logs y nadie se enteraba.
        print(f"Error procesando webhook: {e}")
        traceback.print_exc()
        try:
            return_service._send_error_email(
                method="procesar_webhook",
                error=e,
                order_reference=_identificar_return(data),
                context={"origen": "listener.procesar_webhook"},
            )
        except Exception as mail_err:
            print(f"❌ No se pudo enviar el mail de error: {mail_err}")

@app.route("/webhook", methods=["POST"])
def webhook():
    token = request.headers.get("x-two-boxes-authorization")
    if not token or token != WEBHOOK_SECRET:
        print(f"Unauthorized Access Request")
        return jsonify({"error": "Unauthorized"}), 401
    
    raw_data = request.get_json(silent=True)
    if not raw_data:
        return jsonify({"error": "No data"}), 400

    thread_data = raw_data.copy() if isinstance(raw_data, dict) else raw_data

    # Quien postea y que postea. Es la unica fuente de verdad que queda cuando
    # el archivado a GAS falla, y es lo que permite distinguir "Two Boxes manda
    # otro event_type" de "un monitor / un script de retry esta posteando".
    event_type = event_id = n_items = merchant = None
    if isinstance(raw_data, dict):
        event_type = raw_data.get("event_type")
        event_id = raw_data.get("id")
        event_data = raw_data.get("event_data")
        if isinstance(event_data, dict):
            line_items = event_data.get("line_items")
            n_items = len(line_items) if isinstance(line_items, list) else None
            try:
                # Solo para el log. Va en try porque este codigo corre DENTRO del
                # handler: una excepcion aca convertiria el 200 en un 500 y haria
                # que Two Boxes reintente, que es justo lo que no queremos.
                merchant = return_service._get_merchant_name(raw_data) or None
            except Exception as e:
                merchant = f"<error resolviendo merchant: {e}>"
    print(
        f"📥 POST /webhook event_type={event_type!r} id={event_id!r} "
        f"line_items={n_items} merchant={merchant!r} "
        f"remote_addr={request.remote_addr} "
        f"user_agent={request.headers.get('User-Agent')!r}"
    )
    print("tdata", thread_data)

    # Todo se despacha en segundo plano: el handler tiene que devolver 200 en
    # milisegundos. Si algo bloquea acá, gunicorn mata al worker por timeout y se
    # pierde el resto del procesamiento sin dejar rastro.

    # 1. Procesarlo en Mintsoft (la operación de negocio, va primero)
    executor.submit(procesar_webhook, raw_data)

    # 2. Subir JSON al Google Drive
    executor.submit(enviar_a_google_async, thread_data)

    # 3. Notificar a Google un webhook por SKU
    executor.submit(enviar_webhook_por_sku, thread_data)

    return "", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)