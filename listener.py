from flask import Flask, request, jsonify
import hmac
import os
import threading
import traceback
from collections import OrderedDict
import requests
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor
from services.mintsoft_service import MintsoftReturnService
from mappers.mintsoft_mapper import _send_alert_email
from urllib3.util.retry import Retry
from dotenv import load_dotenv
load_dotenv()
app = Flask(__name__)
return_service = MintsoftReturnService()

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
GAS_URL = os.environ.get("GAS_URL")
WEBHOOKS_URL = os.environ.get("WEBHOOKS_URL")

# Tipos de evento que este servicio sabe procesar. Two Boxes manda todo a la misma
# URL y antes no se miraba event_type en ningun lado, asi que cualquier tipo con
# otra forma entraba igual a procesar_webhook.
EVENT_TYPES_SOPORTADOS = {
    t.strip() for t in os.environ.get("EVENT_TYPES", "return-complete").split(",") if t.strip()
}

# Rechazar cuerpos gigantes antes de parsearlos.
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", 5 * 1024 * 1024))

# --- Guarda de reentrega (parcial de COR-07) --------------------------------------
# NO es idempotencia de verdad: vive en memoria, se pierde en cada reinicio y no se
# comparte entre los workers de gunicorn. Solo frena la reentrega del MISMO event_id
# al mismo worker, que es el caso comun cuando Two Boxes reintenta. La regla
# operativa sigue en pie: no reenviar webhooks a mano hasta que exista la
# persistencia (E-1), porque un reenvio que caiga en el otro worker SI duplica.
_EVENTOS_VISTOS_MAX = 2000
_eventos_vistos = OrderedDict()
_eventos_lock = threading.Lock()

# Contadores para /health.
_metricas = {"recibidos": 0, "procesados": 0, "fallados": 0, "duplicados": 0, "ignorados": 0}
_metricas_lock = threading.Lock()


def _contar(clave):
    with _metricas_lock:
        _metricas[clave] = _metricas.get(clave, 0) + 1


def _ya_procesado(event_id) -> bool:
    """True si este worker ya vio ese event_id. Lo registra si es nuevo."""
    if not event_id:
        return False
    with _eventos_lock:
        if event_id in _eventos_vistos:
            return True
        _eventos_vistos[event_id] = True
        while len(_eventos_vistos) > _EVENTOS_VISTOS_MAX:
            _eventos_vistos.popitem(last=False)
    return False


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
    """Función para enviar datos en segundo plano"""
    try:
        session.post(GAS_URL, json=datos, timeout=120)
        print("✅ Enviado a Google Apps Script correctamente")
    except Exception as e:
        print(f"❌ Error enviando a Google: {e}")

PII_KEYS = ("customer", "rma_address")


def _redactar_pii(datos):
    """Copia del payload sin los campos con datos personales del comprador.

    Se saca customer (nombre, mail) y rma_address (domicilio). Se deja el resto,
    tracking_number incluido: es el PO reference con el que se busca el return en
    Mintsoft, asi que sin el los logs no sirven para operar.
    """
    try:
        if not isinstance(datos, dict):
            return datos
        copia = dict(datos)
        event_data = copia.get("event_data")
        if isinstance(event_data, dict):
            event_data = dict(event_data)
            for k in PII_KEYS:
                if k in event_data:
                    event_data[k] = "<redactado>"
            copia["event_data"] = event_data
        return copia
    except Exception:
        # Nunca romper el handler por el logging.
        return "<no se pudo redactar el payload>"


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
    # Un webhook = un mail. El reporte junta los problemas de todas las capas y se
    # manda una sola vez en el finally, en vez de un mail por capa que fallaba.
    return_service.begin_webhook_report(data)
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
            # add_return_items devuelve False si el return no quedo armado. Antes
            # absorbia la excepcion y devolvia None, y el listener llamaba igual a
            # reallocate_return_items: el stock se movia FISICAMENTE de RET/RET-TEMP
            # a la caja del operario para un return que habia quedado sin items y
            # sin confirmar. Quedaba mercaderia en una caja sin ningun return que la
            # respalde. Ahora, si el armado fallo, no se toca el stock: queda en el
            # staging, con el reporte diciendo que hay que completarlo a mano.
            armado_ok = return_service.add_return_items(return_id[0], data)

            if armado_ok is False:
                print(
                    f"⚠️ El armado del return {return_id[0]} fallo: NO se reubica el "
                    f"stock. Queda en RET / RET-TEMP esperando intervencion."
                )
            else:
                # Pasar items de RET o RET-QT a la caja del return si es Internal
                return_service.reallocate_return_items(data)

        # No afirmar exito cuando no se creo nada: "Webhook procesado con exito"
        # se imprimia igual con (None, "No Return Created"), que es justo el caso
        # que hay que revisar.
        if return_id and return_id[1] == "No Return Created":
            print(f"⚠️ Webhook NO produjo return en Mintsoft ({return_id[1]})")
        else:
            _contar("procesados")
            print("Webhook procesado con exito")

    except Exception as e:
        # Catch-all: cualquier fallo que no haya sido capturado (y notificado) dentro
        # de MintsoftReturnService llega hasta acá. Sin esto el error solo se imprimía
        # en los logs y nadie se enteraba.
        _contar("fallados")
        print(f"Error procesando webhook: {e}")
        traceback.print_exc()
        try:
            # Si la capa de abajo ya reporto esta misma excepcion, el reporte la
            # deduplica y esto no agrega nada. Queda para los fallos que no paso
            # ninguna capa (por ejemplo un payload con una forma inesperada).
            return_service._send_error_email(
                method="procesar_webhook",
                error=e,
                order_reference=_identificar_return(data),
                context={"origen": "listener.procesar_webhook"},
                que_falta="El webhook no se pudo procesar",
                accion=(
                    "Revisar el payload y reprocesarlo. Ojo: reprocesar crea un return "
                    "nuevo en Mintsoft, no actualiza el anterior (no hay idempotencia)."
                ),
            )
        except Exception as mail_err:
            print(f"❌ No se pudo registrar el error: {mail_err}")
    finally:
        # Siempre, incluso si todo salio bien (ahi no manda nada).
        try:
            return_service.flush_webhook_report()
        except Exception as flush_err:
            print(f"❌ No se pudo enviar el reporte del webhook: {flush_err}")

@app.route("/webhook", methods=["POST"])
def webhook():
    # hmac.compare_digest en vez de !=: la comparacion de strings corta en el
    # primer byte distinto, y ese tiempo distinto filtra el secreto de a un
    # caracter por vez.
    token = request.headers.get("x-two-boxes-authorization")
    if not WEBHOOK_SECRET:
        print("❌ WEBHOOK_SECRET no esta seteada: se rechaza todo")
        return jsonify({"error": "Unauthorized"}), 401
    if not token or not hmac.compare_digest(str(token), str(WEBHOOK_SECRET)):
        print(f"Unauthorized Access Request desde {request.remote_addr}")
        return jsonify({"error": "Unauthorized"}), 401

    _contar("recibidos")
    
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
    # El payload completo traia customer.full_name, customer.email y la direccion
    # del RMA al log agregado de la plataforma. Se redactan esos campos y se deja
    # el resto: el tracking y los SKUs son lo que se necesita para operar.
    print("payload:", _redactar_pii(thread_data))

    # COR-26 -- solo los tipos soportados llegan a Mintsoft. Two Boxes manda todo
    # a la misma URL y antes no se miraba event_type en ningun lado, asi que un
    # tipo con otra forma entraba igual a procesar_webhook y fallaba adentro. Se
    # archiva igual (abajo) para no perder el evento, pero no se escribe en el WMS.
    procesable = event_type in EVENT_TYPES_SOPORTADOS
    if not procesable:
        _contar("ignorados")
        print(
            f"⏭️ event_type={event_type!r} no soportado "
            f"(soportados: {sorted(EVENT_TYPES_SOPORTADOS)}). "
            f"Se archiva pero NO se procesa en Mintsoft."
        )
        # Avisar por mail, no solo loguear: si Two Boxes empieza a mandar un tipo
        # nuevo que SI habria que procesar, una linea en el log no lo hace visible.
        # Va con el throttle del mapper (ALERT_THROTTLE_SECONDS, default 30 min) y
        # el asunto lleva el event_type, asi que un tipo de alto volumen manda un
        # mail por ventana y no uno por evento.
        _send_alert_email(
            subject=f"[Mintsoft] event_type no soportado: {event_type!r}",
            body=(
                f"Llego un webhook con event_type={event_type!r}, que no esta en la "
                f"lista de tipos soportados ({sorted(EVENT_TYPES_SOPORTADOS)}).\n\n"
                f"El payload se archivo igual, pero NO se escribio nada en Mintsoft: "
                f"no se creo el return ni se movio stock.\n\n"
                f"Si este tipo SI hay que procesarlo, agregarlo a la variable de "
                f"entorno EVENT_TYPES (separada por comas). No requiere cambio de "
                f"codigo, pero si reiniciar el servicio.\n\n"
                f"event id:   {event_id}\n"
                f"merchant:   {merchant}\n"
                f"line_items: {n_items}\n"
                f"origen:     {request.remote_addr} / {request.headers.get('User-Agent')!r}"
            ),
        )

    # Guarda de reentrega: parcial, por proceso. Ver el comentario de _ya_procesado.
    if procesable and _ya_procesado(event_id):
        procesable = False
        _contar("duplicados")
        print(
            f"⏭️ event_id={event_id!r} ya fue procesado por este worker: se saltea "
            f"para no crear un segundo return. Se archiva igual."
        )

    # Todo se despacha en segundo plano: el handler tiene que devolver 200 en
    # milisegundos. Si algo bloquea acá, gunicorn mata al worker por timeout y se
    # pierde el resto del procesamiento sin dejar rastro.

    # 1. Procesarlo en Mintsoft (la operación de negocio, va primero)
    if procesable:
        executor.submit(procesar_webhook, raw_data)

    # 2. Subir JSON al Google Drive
    executor.submit(enviar_a_google_async, thread_data)

    # 3. Notificar a Google un webhook por SKU
    executor.submit(enviar_webhook_por_sku, thread_data)

    return "", 200

@app.route("/health", methods=["GET"])
def health():
    """Estado del servicio y contadores. No toca Mintsoft: tiene que responder
    aunque el WMS este caido, que es justo cuando el health check importa."""
    with _metricas_lock:
        metricas = dict(_metricas)
    return jsonify({
        "status": "ok",
        "event_types_soportados": sorted(EVENT_TYPES_SOPORTADOS),
        "config": {
            "webhook_secret": bool(WEBHOOK_SECRET),
            "gas_url": bool(GAS_URL),
            "webhooks_url": bool(WEBHOOKS_URL),
        },
        "metricas": metricas,
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)