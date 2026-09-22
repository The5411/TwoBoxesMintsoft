"""Endpoint HTTP que recibe los webhooks de Two Boxes.

Responsabilidades, en orden de importancia:

  1. Autenticar y devolver 200 en milisegundos. Un 5xx hace que Two Boxes
     reintente, y un handler lento hace que gunicorn mate al worker por timeout.
  2. Garantizar que cada evento se procese UNA sola vez (storage/event_store.py).
  3. Despachar el procesamiento en Mintsoft y el archivado a Google en pools
     separados, para que un Google Apps Script lento no demore el WMS.
"""
import hmac
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

import config
from loggers.main_logger import get_logger
from mappers.mintsoft_mapper import _send_alert_email
from services.mintsoft_service import MintsoftReturnService
from storage.event_store import (
    ESTADO_FALLADO,
    ESTADO_IGNORADO,
    ESTADO_PROCESADO,
    EventStore,
)

logger = get_logger("listener")

app = Flask(__name__)
return_service = MintsoftReturnService()

# Store de eventos: persistencia e idempotencia (E-1 / OPS-01 / COR-07).
# Se construye al importar para que un problema de configuración se vea en el
# arranque, pero su constructor nunca lanza: si la base no está, queda
# `disponible = False` y el handler lo trata como tal.
event_store = EventStore()
return_service.store = event_store

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
GAS_URL = os.environ.get("GAS_URL")
WEBHOOKS_URL = os.environ.get("WEBHOOKS_URL")

# Tipos de evento que este servicio sabe procesar. Two Boxes manda todo a la misma
# URL y antes no se miraba event_type en ningun lado, asi que cualquier tipo con
# otra forma entraba igual a procesar_webhook.
EVENT_TYPES_SOPORTADOS = config.EVENT_TYPES_SOPORTADOS

# Rechazar cuerpos gigantes antes de parsearlos.
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH

# Contadores para /health.
_metricas = {
    "recibidos": 0,
    "procesados": 0,
    "fallados": 0,
    "duplicados": 0,
    "ignorados": 0,
    "sin_store": 0,
}
_metricas_lock = Lock()


def _contar(clave):
    with _metricas_lock:
        _metricas[clave] = _metricas.get(clave, 0) + 1


# --- Pools ---------------------------------------------------------------------
# Dos pools separados a propósito (S-7). Antes los tres trabajos compartían un
# único ThreadPoolExecutor(10): el archivado a Google tiene timeouts de 60 y 120
# segundos, así que unos pocos eventos con Apps Script lento se quedaban con
# todos los threads y el procesamiento en Mintsoft -- lo único que no se puede
# perder ni reintentar sin riesgo -- quedaba encolado detrás del archivado.
executor = ThreadPoolExecutor(
    max_workers=config.WORKERS_MINTSOFT, thread_name_prefix="mintsoft"
)
archivo_executor = ThreadPoolExecutor(
    max_workers=config.WORKERS_ARCHIVO, thread_name_prefix="archivo"
)

# Configuración de reintentos para el archivado a Google.
session = requests.Session()
retries = Retry(
    total=5,               # Reintentos
    backoff_factor=0.3,    # Esperar 0.3 mas con cada reintento
    status_forcelist=[502, 503, 504], # Reintentar si el servidor de Google está saturado
    raise_on_status=False
)
session.mount('https://', HTTPAdapter(max_retries=retries))


def enviar_webhook_a_google(datos):
    if not WEBHOOKS_URL:
        logger.debug("WEBHOOKS_URL no configurada: no se notifica por SKU")
        return
    try:
        response = requests.post(
            WEBHOOKS_URL,
            json=datos,
            timeout=60,
            allow_redirects=True  # Crucial para seguir el redireccionamiento /echo de Google
        )
        response.raise_for_status()
        logger.info("Notificacion a Google OK")
    except Exception as e:
        logger.error(f"Error notificando a Google: {e}")


def enviar_webhook_por_sku(datos):
    # Corre en un thread del pool de archivado: si dejamos escapar una excepción
    # queda atrapada en el Future y no la ve nadie.
    try:
        event_data = datos.get('event_data', {})
        line_items = event_data.get('line_items', [])

        for item in line_items:
            # Copia superficial del payload conservando la misma estructura,
            # pero con un solo line_item (un SKU) por envío.
            payload = dict(datos)
            payload['event_data'] = dict(event_data)
            payload['event_data']['line_items'] = [item]

            logger.info(f"Notificando SKU {(item or {}).get('sku')!r} a Google")
            enviar_webhook_a_google(payload)
    except Exception as e:
        logger.error(f"Error en enviar_webhook_por_sku: {e}", exc_info=True)


def enviar_a_google_async(datos):
    """Archiva el JSON en Google Drive. Corre en el pool de archivado."""
    if not GAS_URL:
        logger.debug("GAS_URL no configurada: no se archiva el payload")
        return
    try:
        session.post(GAS_URL, json=datos, timeout=120)
        logger.info("Payload archivado en Google Apps Script")
    except Exception as e:
        logger.error(f"Error archivando en Google: {e}")


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


def _avisar_duplicado_por_reference(reference, previos):
    """Registra que otro evento ya creó un return con esta misma Reference.

    Es la segunda red de idempotencia: el claim del store frena el MISMO evento,
    y esto detecta un evento DISTINTO que apunta al mismo return (por ejemplo un
    payload reeditado a mano, o Two Boxes reemitiendo con otro id).

    **Con `warn` no manda mail, solo loguea.** Una misma orden puede tener dos
    devoluciones legítimas en momentos distintos -- y ahí la Reference cae al
    número de orden --, así que este aviso daba muchos falsos positivos y el mail
    no aportaba sobre la línea de log.

    Con `block` sí manda mail, y eso no es negociable: ahí el return NO se crea,
    y un return que no se crea sin que nadie se entere es una devolución perdida.
    """
    detalle = ", ".join(
        f"return {p.get('return_id')} ({p.get('return_kind')}) del evento "
        f"{p.get('event_key')} el {p.get('created_at')}"
        for p in previos
    )
    bloquear = config.DUPLICATE_REFERENCE_ACTION == "block"
    logger.warning(
        f"Reference {reference!r} ya tiene return(s) creado(s): {detalle}. "
        f"DUPLICATE_REFERENCE_ACTION={config.DUPLICATE_REFERENCE_ACTION!r} -> "
        f"{'NO se procesa' if bloquear else 'se procesa igual'}."
    )

    if not bloquear:
        # Solo log: ver el docstring.
        return True

    _send_alert_email(
        subject=f"[Mintsoft] Return NO creado, Reference duplicada - PO {reference}",
        body=(
            f"Llego un webhook NUEVO cuya Reference ({reference}) ya tiene un return "
            f"creado en Mintsoft:\n\n  {detalle}\n\n"
            f"NO se proceso en Mintsoft (DUPLICATE_REFERENCE_ACTION=block), asi que "
            f"no se creo un segundo return.\n\n"
            f"Si esta devolucion es legitima y distinta de la anterior, hay que "
            f"cargarla a mano: una misma orden puede tener dos devoluciones en "
            f"momentos distintos, y en ese caso la Reference es la misma.\n\n"
            f"Para que estos casos se procesen igual (y revisarlos despues en "
            f"Mintsoft), poner DUPLICATE_REFERENCE_ACTION=warn."
        ),
    )
    return False


def procesar_webhook(data, event_key=None):
    # Un webhook = un mail. El reporte junta los problemas de todas las capas y se
    # manda una sola vez en el finally, en vez de un mail por capa que fallaba.
    return_service.begin_webhook_report(data, event_key=event_key)
    estado_final = ESTADO_FALLADO
    error_final = None
    try:
        # Crea return interno o externo
        return_id = return_service.create_return(data)
        logger.info(f"create_return -> {return_id}")

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
                logger.error(
                    f"El armado del return {return_id[0]} fallo: NO se reubica el "
                    f"stock. Queda en RET / RET-TEMP esperando intervencion."
                )
            else:
                # Pasar items de RET o RET-QT a la caja del return si es Internal
                return_service.reallocate_return_items(data)

        # No afirmar exito cuando no se creo nada: "Webhook procesado con exito"
        # se imprimia igual con (None, "No Return Created"), que es justo el caso
        # que hay que revisar.
        if return_id and return_id[1] == "No Return Created":
            logger.warning(f"Webhook NO produjo return en Mintsoft ({return_id[1]})")
            # No es un fallo del servicio: puede ser un merchant no mapeado o un
            # return con todos los items Missing. Queda marcado como ignorado para
            # que un reproceso no lo confunda con un evento nunca visto.
            estado_final = ESTADO_IGNORADO
            error_final = return_id[1]
        else:
            _contar("procesados")
            estado_final = ESTADO_PROCESADO
            logger.info("Webhook procesado con exito")

    except Exception as e:
        # Catch-all: cualquier fallo que no haya sido capturado (y notificado) dentro
        # de MintsoftReturnService llega hasta acá. Sin esto el error solo se imprimía
        # en los logs y nadie se enteraba.
        _contar("fallados")
        estado_final = ESTADO_FALLADO
        error_final = f"{type(e).__name__}: {e}"
        logger.error(f"Error procesando webhook: {e}", exc_info=True)
        try:
            # Si la capa de abajo ya reporto esta misma excepcion, el reporte la
            # deduplica y esto no agrega nada. Queda para los fallos que no paso
            # ninguna capa (por ejemplo un payload con una forma inesperada).
            return_service._send_error_email(
                method="procesar_webhook",
                error=e,
                order_reference=_identificar_return(data),
                context={"origen": "listener.procesar_webhook", "event_key": event_key},
                que_falta="El webhook no se pudo procesar",
                accion=(
                    "Revisar el payload y reprocesarlo. El reproceso NO duplica el "
                    "return: si el return ya se habia creado, el servicio lo detecta "
                    "y avisa en vez de crear otro."
                ),
            )
        except Exception as mail_err:
            logger.error(f"No se pudo registrar el error: {mail_err}")
    finally:
        # Cerrar el evento en el store ANTES del reporte: si el mail falla, el
        # estado del evento tiene que quedar igual grabado.
        if event_key:
            try:
                event_store.finish(event_key, estado_final, error_final)
            except Exception as store_err:
                logger.error(f"No se pudo cerrar el evento en el store: {store_err}")
        # Siempre, incluso si todo salio bien (ahi no manda nada).
        try:
            return_service.flush_webhook_report()
        except Exception as flush_err:
            logger.error(f"No se pudo enviar el reporte del webhook: {flush_err}")


@app.route("/webhook", methods=["POST"])
def webhook():
    # hmac.compare_digest en vez de !=: la comparacion de strings corta en el
    # primer byte distinto, y ese tiempo distinto filtra el secreto de a un
    # caracter por vez.
    token = request.headers.get("x-two-boxes-authorization")
    if not WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET no esta seteada: se rechaza todo")
        return jsonify({"error": "Unauthorized"}), 401
    if not token or not hmac.compare_digest(str(token), str(WEBHOOK_SECRET)):
        logger.warning(f"Unauthorized Access Request desde {request.remote_addr}")
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
    reference = None
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
        try:
            reference = return_service._return_identifier(raw_data)
        except Exception:
            reference = None

    logger.info(
        f"POST /webhook event_type={event_type!r} id={event_id!r} "
        f"line_items={n_items} merchant={merchant!r} reference={reference!r} "
        f"remote_addr={request.remote_addr} "
        f"user_agent={request.headers.get('User-Agent')!r}"
    )
    # El payload completo traia customer.full_name, customer.email y la direccion
    # del RMA al log agregado de la plataforma. Se redactan esos campos y se deja
    # el resto: el tracking y los SKUs son lo que se necesita para operar.
    logger.info(f"payload: {_redactar_pii(thread_data)}")

    # COR-26 -- solo los tipos soportados llegan a Mintsoft. Two Boxes manda todo
    # a la misma URL y antes no se miraba event_type en ningun lado, asi que un
    # tipo con otra forma entraba igual a procesar_webhook y fallaba adentro. Se
    # archiva igual (abajo) para no perder el evento, pero no se escribe en el WMS.
    procesable = event_type in EVENT_TYPES_SOPORTADOS
    if not procesable:
        _contar("ignorados")
        logger.warning(
            f"event_type={event_type!r} no soportado "
            f"(soportados: {sorted(EVENT_TYPES_SOPORTADOS)}). "
            f"Se archiva pero NO se procesa en Mintsoft."
        )
        # Queda registrado igual, para poder responder "esto llego?" despues.
        try:
            event_store.registrar_ignorado(
                raw_data, event_id=event_id, event_type=event_type,
                merchant=merchant, motivo="event_type no soportado",
            )
        except Exception as e:
            logger.error(f"No se pudo registrar el evento ignorado: {e}")
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

    # --- Idempotencia (E-1 / OPS-01 / COR-07) ---------------------------------
    # El claim es atómico y persistente, así que cubre los tres casos que el
    # OrderedDict en memoria no cubría: el reinicio, el otro worker de gunicorn,
    # y el reproceso a mano de un evento cuyo return YA se creó.
    event_key = None
    if procesable:
        veredicto = event_store.claim(
            raw_data, event_id=event_id, event_type=event_type,
            merchant=merchant, reference=reference,
        )
        if veredicto.otorgado:
            event_key = veredicto.registro.get("event_key") or event_store.clave_de(
                raw_data, event_id
            )
            if veredicto.motivo != "nuevo":
                logger.warning(
                    f"Evento {event_key} retomado ({veredicto.motivo}): "
                    f"intento numero {int(veredicto.registro.get('attempts') or 1) + 1}"
                )
        elif veredicto.motivo == "store_caido":
            # No se puede garantizar que no sea un duplicado. Se archiva y se avisa,
            # pero NO se escribe en el WMS: duplicar stock es peor que demorar el
            # return. Con REQUIRE_STORE=false se procesa igual, a riesgo.
            _contar("sin_store")
            procesable = not config.REQUIRE_STORE
            logger.error(
                f"El store de eventos no responde ({veredicto.detalle}). "
                f"REQUIRE_STORE={config.REQUIRE_STORE} -> "
                f"{'se procesa a riesgo de duplicar' if procesable else 'NO se procesa'}."
            )
            _send_alert_email(
                subject="[Mintsoft] Base de idempotencia caida",
                body=(
                    f"No se pudo consultar la base que evita procesar dos veces el "
                    f"mismo webhook.\n\nError: {veredicto.detalle}\n\n"
                    + (
                        "El webhook SE PROCESO igual porque REQUIRE_STORE=false: si "
                        "Two Boxes reintenta, puede quedar un return duplicado.\n\n"
                        if procesable else
                        "El webhook NO se proceso en Mintsoft. Se archivo, asi que no "
                        "se perdio: una vez arreglada la base hay que reenviarlo.\n\n"
                    )
                    + f"backend: {event_store.backend}\n"
                      f"event id: {event_id}\nreference: {reference}\n"
                ),
            )
        else:
            procesable = False
            _contar("duplicados")
            logger.warning(
                f"Evento no procesado ({veredicto.motivo}): {veredicto.detalle}. "
                f"Se archiva igual."
            )
            if veredicto.motivo == "return_ya_creado":
                # Este es el caso de la orden W836: alguien reprocesa un webhook cuyo
                # return ya existe. Antes se creaba un segundo return con el mismo
                # stock; ahora se avisa para que lo completen a mano.
                _send_alert_email(
                    subject=f"[Mintsoft] Reproceso frenado, el return ya existe - PO {reference}",
                    body=(
                        f"Se reprocesó un webhook cuyo return YA se había creado en "
                        f"Mintsoft, asi que NO se creo un segundo.\n\n"
                        f"{veredicto.detalle}\n\n"
                        f"El intento anterior creó el return pero no llegó a "
                        f"terminarlo, asi que probablemente le falten items o el "
                        f"movimiento de stock. Hay que completarlo a mano desde "
                        f"Mintsoft, no reenviando el webhook.\n\n"
                        f"return_id: {veredicto.return_id}\n"
                        f"reference: {reference}\n"
                        f"event id:  {event_id}\n"
                    ),
                )

    # Segunda red: un evento NUEVO cuya Reference ya tiene un return creado.
    if procesable and event_key and config.DUPLICATE_REFERENCE_ACTION != "off":
        previos = event_store.returns_por_reference(reference, excluir_event_key=event_key)
        if previos:
            procesable = _avisar_duplicado_por_reference(reference, previos)
            if not procesable:
                _contar("duplicados")
                event_store.finish(event_key, ESTADO_IGNORADO, "reference duplicada")

    # Todo se despacha en segundo plano: el handler tiene que devolver 200 en
    # milisegundos. Si algo bloquea acá, gunicorn mata al worker por timeout y se
    # pierde el resto del procesamiento sin dejar rastro.

    # 1. Procesarlo en Mintsoft (la operación de negocio, va en su propio pool)
    if procesable:
        executor.submit(procesar_webhook, raw_data, event_key)

    # 2. Subir JSON al Google Drive (pool de archivado)
    archivo_executor.submit(enviar_a_google_async, thread_data)

    # 3. Notificar a Google un webhook por SKU (pool de archivado)
    archivo_executor.submit(enviar_webhook_por_sku, thread_data)

    return "", 200


@app.route("/health", methods=["GET"])
def health():
    """Estado del servicio y contadores. No toca Mintsoft: tiene que responder
    aunque el WMS este caido, que es justo cuando el health check importa.

    Sí consulta el store, porque sin él el servicio no puede garantizar que no
    duplique returns, y eso es parte de estar sano.
    """
    with _metricas_lock:
        metricas = dict(_metricas)

    store = event_store.stats()
    ok = store.get("disponible") or not config.REQUIRE_STORE

    return jsonify({
        "status": "ok" if ok else "degraded",
        "event_types_soportados": sorted(EVENT_TYPES_SOPORTADOS),
        "config": {
            "webhook_secret": bool(WEBHOOK_SECRET),
            "gas_url": bool(GAS_URL),
            "webhooks_url": bool(WEBHOOKS_URL),
            "require_store": config.REQUIRE_STORE,
            "duplicate_reference_action": config.DUPLICATE_REFERENCE_ACTION,
            "returnable_order_status_ids": sorted(config.RETURNABLE_ORDER_STATUS_IDS),
            "return_locations": {str(k): v for k, v in config.RETURN_LOCATIONS.items()},
        },
        "store": store,
        "metricas": metricas,
    }), 200 if ok else 503


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
