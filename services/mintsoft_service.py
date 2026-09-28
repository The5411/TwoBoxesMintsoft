import os
import json
import sys
import socket
import threading
import smtplib
import traceback
from email.message import EmailMessage
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
from loggers.main_logger import get_logger
from clients.mintsoftClient import MintsoftOrderClient
from mappers.mintsoft_mapper import map_client, map_warehouse


def _normalize_order_number(value) -> str:
    """Normaliza un número de orden para poder compararlo.

    Two Boxes manda el storefront_order_number con '#' y no siempre adelante:
    ROVE usa 'US#12901' (numeral en el medio) y en Mintsoft esa orden es
    OrderNumber='US12901'. El .lstrip('#') anterior solo sacaba el numeral
    inicial, así que 'US#12901' quedaba igual y nunca matcheaba: TODOS los
    returns de ROVE terminaban creados como externos. Ahora sacamos el '#'
    esté donde esté.
    """
    return str(value or "").replace("#", "").strip().upper()


def _order_number_variants(value) -> List[str]:
    """Términos de búsqueda a probar en Order/Search, sin repetir.

    Primero el valor crudo: Order/Search matchea ExternalOrderReference, que en
    Mintsoft guarda el número tal como vino de la tienda ('US#12901'), así que
    el crudo suele entrar de una. Después la versión sin '#' por si la orden
    quedó cargada solo como OrderNumber.
    """
    raw = str(value or "").strip()
    variants = []
    for candidate in (raw, raw.replace("#", "").strip()):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _is_missing(item) -> bool:
    """True si el item nunca llegó físicamente al depósito. Ver config.es_missing."""
    return config.es_missing(item)


def _get_item_barcode(item) -> Optional[str]:
    """Barcode de un line item de Two Boxes.

    Los payloads de RMA traen line_items[].barcode en null y el barcode real vive en
    line_items[].product_variant.barcode (ver models/tb_rma_model.json), asi que hay
    que mirar los dos lugares. Devuelve None si no hay ninguno.
    """
    item = item or {}
    for candidate in (item.get("barcode"), (item.get("product_variant") or {}).get("barcode")):
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return None


class MintsoftReturnService:
    def __init__(self, logger_name: str = "mintsoft_service", log_file: str = "m_service.log"):
        self.logger = get_logger(logger_name, log_file)
        self.client = MintsoftOrderClient()
        # Estados que habilitan un return interno (4=DESPATCHED, 5=INVOICED,
        # 6=INVOICEFAILED). Vive en config.py con el resto de los ids de Mintsoft.
        self.returnable_status_ids = config.RETURNABLE_ORDER_STATUS_IDS

        # Store de eventos. Lo inyecta el listener; si queda en None el service
        # funciona igual pero sin la proteccion contra reprocesos (E-1).
        self.store = None

        # ----- Email notification config (read from environment) -----
        self.smtp_host = os.environ.get("SMTP_HOST")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.smtp_user = os.environ.get("SMTP_USER")
        self.smtp_password = os.environ.get("SMTP_PASSWORD")
        # Sin la coma final: con ella esto era una TUPLA de un elemento, no un
        # string. Y se lee de ALERT_EMAIL_TO para no tener dos listas hardcodeadas
        # en dos archivos distintos (la otra esta en mappers/mintsoft_mapper.py).
        self.alert_email_to = os.environ.get(
            "ALERT_EMAIL_TO",
            "bgallo@the5411.com, jcordero@the5411.com, ngurfinkel@the5411.com, mbivort@the5411.com",
        )
        self.alert_email_from = os.environ.get("ALERT_EMAIL_FROM", self.smtp_user or "")

        # Reporte de problemas del webhook que se esta procesando. Es thread-local
        # porque el service es una instancia unica compartida por los 10 threads del
        # executor del listener, pero procesar_webhook y todo lo que llama corren en
        # el MISMO thread, asi que cada webhook ve solo sus propios problemas.
        self._reporte = threading.local()

    # -------------------------------------------------------------
    # Un webhook = un mail. Antes cada capa mandaba el suyo: un return externo
    # que fallaba generaba el de allocate_external_return_items MAS el del
    # catch-all de listener.procesar_webhook, y uno interno podia llegar a tres
    # o cuatro (add_return_items por items caidos, add_return_items por la
    # excepcion, reallocate_return_items, y el catch-all). Ahora los problemas se
    # acumulan durante el procesamiento y se manda uno solo al final.
    # -------------------------------------------------------------
    def begin_webhook_report(self, data, event_key: Optional[str] = None) -> None:
        """Abre el reporte del webhook. Idempotente si ya hay uno abierto.

        `event_key` es la clave de idempotencia del evento en el store. Se guarda
        acá para que create_return pueda grabar el return_id en cuanto Mintsoft lo
        devuelve, sin tener que pasarlo por la firma de cada método.
        """
        self._reporte.problemas = []
        self._reporte.event_key = event_key
        # Cache de productos con alcance de este webhook. Ver _cache_productos.
        self._reporte.productos = {}
        try:
            self._reporte.referencia = self._return_identifier(data)
        except Exception:
            self._reporte.referencia = None
        try:
            event_data = (data or {}).get("event_data") or {}
            self._reporte.event_type = (data or {}).get("event_type")
            self._reporte.merchant = self._get_merchant_name(data) or None
            line_items = event_data.get("line_items")
            self._reporte.total_items = (
                len(line_items) if isinstance(line_items, list) else None
            )
        except Exception:
            self._reporte.event_type = None
            self._reporte.merchant = None
            self._reporte.total_items = None

    def _reporte_activo(self) -> bool:
        return isinstance(getattr(self._reporte, "problemas", None), list)

    def flush_webhook_report(self) -> None:
        """Manda UN mail con todos los problemas del webhook, y cierra el reporte."""
        problemas = getattr(self._reporte, "problemas", None)
        self._reporte.problemas = None
        # El cache de productos muere con el webhook: no puede sobrevivir al
        # siguiente, que puede ser de otro cliente.
        self._reporte.productos = None
        if not problemas:
            return
        try:
            self._enviar_reporte(problemas)
        except Exception as e:
            self.logger.error(f"No se pudo enviar el reporte del webhook: {e}", exc_info=True)

    def _enviar_reporte(self, problemas: List[Dict[str, Any]]) -> None:
        ref = getattr(self._reporte, "referencia", None) or "UNKNOWN"
        merchant = getattr(self._reporte, "merchant", None) or "?"
        event_type = getattr(self._reporte, "event_type", None) or "?"
        total_items = getattr(self._reporte, "total_items", None)

        if not (self.smtp_host and self.smtp_user and self.smtp_password and self.alert_email_to):
            self.logger.warning(
                f"Reporte NO enviado (faltan credenciales SMTP). "
                f"{len(problemas)} problema(s) en POReference={ref}."
            )
            return

        # El asunto dice QUE falta, no solo donde fallo: es lo que se lee primero.
        titulares = []
        for pr in problemas:
            if pr.get("que_falta"):
                titulares.append(pr["que_falta"])
        resumen = titulares[0] if titulares else problemas[0].get("paso", "error")
        if len(problemas) > 1:
            resumen = f"{resumen} (+{len(problemas) - 1} mas)"
        subject = f"[Mintsoft] {merchant} - {resumen} - PO {ref}"

        lineas = [
            f"Return {ref} de {merchant} -- {len(problemas)} problema(s) sin resolver.",
            "",
            f"POReference:  {ref}",
            f"Merchant:     {merchant}",
            f"event_type:   {event_type}",
            f"Items:        {total_items if total_items is not None else '?'}",
            f"Hora (UTC):   {datetime.now(timezone.utc).isoformat()}Z",
            f"Host:         {socket.gethostname()}",
            "",
            "=" * 70,
        ]

        for n, pr in enumerate(problemas, 1):
            lineas.append("")
            lineas.append(f"[{n}/{len(problemas)}] {pr.get('que_falta') or pr.get('paso')}")
            if pr.get("sku"):
                lineas.append(f"    SKU:          {pr['sku']}")
            lineas.append(f"    Paso:         {pr.get('paso')}")
            if pr.get("accion"):
                lineas.append(f"    Que hacer:    {pr['accion']}")
            lineas.append(f"    Error:        {pr.get('error_tipo')}: {pr.get('error_msg')}")
            if pr.get("context"):
                try:
                    ctx = json.dumps(pr["context"], indent=6, default=str)
                except Exception:
                    ctx = str(pr["context"])
                lineas.append(f"    Contexto:     {ctx}")

        # Los tracebacks al final y una sola vez: son lo mas largo y lo que menos
        # se necesita para arreglar el return a mano.
        vistos = set()
        tbs = []
        for pr in problemas:
            tb = pr.get("traceback")
            if tb and tb not in vistos:
                vistos.add(tb)
                tbs.append(f"--- {pr.get('paso')} ---\n{tb}")
        if tbs:
            lineas += ["", "=" * 70, "", "Tracebacks:", ""] + tbs

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.alert_email_from
        msg["To"] = self.alert_email_to
        msg.set_content("\n".join(lineas))

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except Exception:
                pass
            server.login(self.smtp_user, self.smtp_password)
            server.send_message(msg)

        self.logger.info(
            f"Reporte enviado a {self.alert_email_to}: {len(problemas)} problema(s), "
            f"POReference={ref}"
        )

    # -------------------------------------------------------------
    # Internal: send an error notification email. Never raises.
    # order_reference (storefront_order_number / POReference) is highlighted
    # in the subject and body when provided.
    # -------------------------------------------------------------
    def _send_error_email(
        self,
        method: str,
        error: BaseException,
        order_reference: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        sku: Optional[str] = None,
        que_falta: Optional[str] = None,
        accion: Optional[str] = None,
    ) -> None:
        """Reporta un problema. Si hay un reporte de webhook abierto, lo acumula
        para que salga UN solo mail al final; si no, manda el mail suelto.

        `que_falta` y `accion` son lo importante: describen en una linea que quedo
        sin hacer y que hay que corregir a mano. El tipo de excepcion y el
        traceback dicen donde se rompio el codigo, no que le falta al return.
        """
        if self._reporte_activo():
            error_msg = str(error)
            # Dedupe: allocate_external_return_items y reallocate_return_items
            # reportan y ademas re-lanzan, asi que el catch-all del listener ve la
            # MISMA excepcion. Sin esto, cada fallo entraba dos veces al reporte.
            for previo in self._reporte.problemas:
                if previo.get("error_msg") == error_msg and previo.get("sku") == sku:
                    # Si el segundo reporte trae mejor descripcion, se queda con esa.
                    if que_falta and not previo.get("que_falta"):
                        previo["que_falta"] = que_falta
                    if accion and not previo.get("accion"):
                        previo["accion"] = accion
                    return
            try:
                tb = "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
            except Exception:
                tb = None
            self._reporte.problemas.append({
                "paso": method,
                "sku": sku,
                "que_falta": que_falta,
                "accion": accion,
                "error_tipo": type(error).__name__,
                "error_msg": error_msg,
                "context": context,
                "traceback": tb,
            })
            if order_reference and not getattr(self._reporte, "referencia", None):
                self._reporte.referencia = order_reference
            self.logger.error(
                f"[reporte] {method}: {que_falta or error_msg}"
                + (f" (SKU {sku})" if sku else "")
            )
            return

        try:
            if not (self.smtp_host and self.smtp_user and self.smtp_password and self.alert_email_to):
                self.logger.warning(
                    "Email alert NOT sent (SMTP credentials missing). "
                    "Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO env vars."
                )
                return

            host = socket.gethostname()
            ts = datetime.now(timezone.utc).isoformat() + "Z"
            tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))

            ref_label = str(order_reference) if order_reference else "UNKNOWN"
            subject = f"[MintsoftReturnService] API error in {method} - POReference: {ref_label}"

            body_lines = [
                f"An error occurred in MintsoftReturnService.{method}",
                "",
                f"POReference: {ref_label}",
                f"Time (UTC):  {ts}",
                f"Host:        {host}",
                f"Error type:  {type(error).__name__}",
                f"Error:       {error}",
                "",
            ]
            if context:
                body_lines.append("Context:")
                try:
                    body_lines.append(json.dumps(context, indent=2, default=str))
                except Exception:
                    body_lines.append(str(context))
                body_lines.append("")
            body_lines.append("Traceback:")
            body_lines.append(tb)

            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self.alert_email_from
            msg["To"] = self.alert_email_to
            msg.set_content("\n".join(body_lines))

            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                server.ehlo()
                try:
                    server.starttls()
                    server.ehlo()
                except Exception:
                    # Server may not support STARTTLS (e.g. local relay) -- continue.
                    pass
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)

            self.logger.info(
                f"Error alert email sent to {self.alert_email_to} for {method} (POReference={ref_label})"
            )
        except Exception as mail_err:
            # Never let notification failures take down the caller.
            self.logger.error(f"Failed to send error alert email: {mail_err}", exc_info=True)

    # Mintsoft usa este texto cuando no encuentra stock movible en el origen. Es el
    # UNICO error de TransferStock que se reintenta, y se puede reintentar porque
    # significa que el transfer no se ejecuto: no hay riesgo de mover dos veces.
    _TRANSFER_NOT_FOUND = "could not find any of product"
    _TRANSFER_INTENTOS = 3
    _TRANSFER_ESPERA_BASE = 2  # segundos; backoff 2s, 4s -> 6s como maximo por item

    def _stock_en_caja(self, warehouse_id, client_id, sku, carton_code, stock_cache):
        """(cantidad, [tipos]) del SKU dentro de la caja, o (None, None) si no se sabe.

        El reporte se baja UNA vez por llamada a reallocate_return_items y se cachea
        en `stock_cache`: antes se consultaba por item, y con un return de varios
        items eso eran varias descargas de un reporte de miles de filas.
        """
        key = (warehouse_id, client_id)
        if key not in stock_cache:
            stock_cache[key] = self.client.fetch_products_in_locations(
                warehouse_id, client_id
            )
        rows = stock_cache[key]
        if rows is None:
            return None, None

        sku_b = str(sku or "").strip().upper()
        caja_b = str(carton_code or "").strip().upper()
        cantidad, tipos = 0, []
        for x in rows:
            if str(x.get("ProductSKU") or "").strip().upper() != sku_b:
                continue
            if str(x.get("CartonCode") or "").strip().upper() != caja_b:
                continue
            try:
                cantidad += int(x.get("Quantity") or 0)
            except (TypeError, ValueError):
                pass
            tipo = str(x.get("Type") or "").strip()
            if tipo and tipo not in tipos:
                tipos.append(tipo)
        return cantidad, tipos

    def _transfer_stock_resiliente(self, reallocation_data, sku, client_id, stock_cache):
        """TransferStock con reintentos acotados y tolerancia al no-op.

        Mintsoft rechaza el transfer con "Could not find any of product ID: X in
        <origen>!" en dos situaciones que no son lo mismo:

          1. El stock todavia no aterrizo en el origen. Es transitorio: el confirm
             del return puede tardar en reflejarse. Aca reintentar SI sirve, y por
             eso hay un backoff corto en vez de un time.sleep fijo antes de cada
             transfer -- el sleep se paga solo cuando hace falta, no siempre.

          2. La unidad ya esta DENTRO de la caja destino. Pasa cuando la caja vive
             en la misma location que el origen (BS-DAMAGED-50 esta en RET-TEMP):
             ahi el confirm consolida la unidad adentro de la caja y no queda nada
             suelto para mover. Reintentar no sirve, va a fallar siempre. Pero el
             stock YA esta en el put_away_bin, que es el destino que queriamos: el
             transfer era un no-op.

        Se reintenta SOLO con ese mensaje. Cualquier otro error -- un ReadTimeout
        incluido -- se relanza de una: si el request se corto sin respuesta no se
        sabe si Mintsoft lo aplico, y reintentar a ciegas podria mover dos veces.
        """
        ultimo = None
        for intento in range(1, self._TRANSFER_INTENTOS + 1):
            try:
                return self.client.transfer_stock(reallocation_data)
            except Exception as e:
                if self._TRANSFER_NOT_FOUND not in str(e).lower():
                    raise
                ultimo = e
                if intento < self._TRANSFER_INTENTOS:
                    espera = self._TRANSFER_ESPERA_BASE * intento
                    self.logger.warning(
                        f"{sku}: TransferStock no encontro stock en "
                        f"{reallocation_data.get('SourceNameOrCode')!r} "
                        f"(intento {intento}/{self._TRANSFER_INTENTOS}). "
                        f"Reintentando en {espera}s."
                    )
                    time.sleep(espera)

        # Agotados los reintentos: ver si en realidad ya esta en el destino.
        carton_code = reallocation_data.get("DestinationNameOrCode")
        cantidad, tipos = self._stock_en_caja(
            reallocation_data.get("DestinationWarehouseId"),
            client_id, sku, carton_code, stock_cache,
        )

        if cantidad is None:
            self.logger.error(
                f"{sku}: TransferStock fallo y NO se pudo verificar si el stock ya "
                f"esta en {carton_code!r}. Se reporta el error original."
            )
            raise ultimo
        if cantidad <= 0:
            self.logger.error(
                f"{sku}: TransferStock fallo y el stock tampoco esta en "
                f"{carton_code!r}. Es un faltante real."
            )
            raise ultimo

        self.logger.info(
            f"{sku}: no habia stock suelto en "
            f"{reallocation_data.get('SourceNameOrCode')!r}, pero ya hay {cantidad} "
            f"unidad(es) dentro de {carton_code!r} (Type={tipos}). El confirm las "
            f"consolido en la caja: el stock ya esta en su destino y el transfer "
            f"era un no-op."
        )
        if not any(t.strip().upper() == "QUARANTINE" for t in (tipos or [])):
            self.logger.warning(
                f"{sku}: las {cantidad} unidad(es) en {carton_code!r} NO estan en "
                f"cuarentena (Type={tipos}). Hay que cuarentenarlas a mano."
            )
        return {
            "Success": True,
            "Message": f"No-op: el stock ya estaba en {carton_code}",
            "NoOp": True,
        }

    # -------------------------------------------------------------
    # Helpers compartidos por los dos caminos (interno y externo). Antes cada
    # rama resolvía por su cuenta la location, el return reason y el ProductId,
    # con los ids escritos a mano en cinco bloques distintos.
    # -------------------------------------------------------------
    def _location_de(self, warehouse, item) -> int:
        """LocationId de returns para este item: RET si vuelve vendible, RET-TEMP
        si va a cuarentena. Lanza config.LocationDesconocida si el warehouse no
        está configurado, en vez de caer en silencio a las locations de E-Commerce.
        """
        return config.location_id(
            warehouse, buen_estado=config.es_buen_estado((item or {}).get("disposition"))
        )

    def _event_key(self) -> Optional[str]:
        return getattr(self._reporte, "event_key", None)

    def _registrar_return(self, return_id, kind: str) -> None:
        """Graba en el store el return que Mintsoft acaba de crear.

        Se llama ANTES de agregar items o mover stock: es la marca que impide que
        un reproceso posterior cree un segundo return con el mismo stock (el caso
        de la orden W836). Si no se puede grabar, el webhook sigue -- el return ya
        existe -- pero queda un problema en el reporte, porque el evento perdió la
        protección.
        """
        event_key = self._event_key()
        if not (self.store and event_key and return_id is not None):
            return
        if self.store.record_return(event_key, return_id, kind):
            self.logger.info(
                f"Return {return_id} ({kind}) registrado en el store para "
                f"event_key={event_key}"
            )
            return
        self.logger.error(
            f"No se pudo registrar el return {return_id} en el store: el evento "
            f"{event_key} quedó sin proteccion contra reprocesos."
        )
        self._send_error_email(
            method="_registrar_return",
            error=RuntimeError(
                f"El return {return_id} se creo en Mintsoft pero no se pudo grabar "
                f"en la base de idempotencia"
            ),
            que_falta=(
                f"El return {return_id} existe en Mintsoft pero NO quedó registrado "
                f"en la base"
            ),
            accion=(
                "NO reprocesar este webhook: al no estar registrado, un reproceso "
                f"crearía un segundo return. Completar el return {return_id} a mano."
            ),
            context={"return_id": return_id, "kind": kind, "event_key": event_key},
        )

    def _cache_productos(self) -> Dict[Any, Any]:
        """Cache de SKU -> (sku, product_id) con alcance de UN webhook.

        Vive en el thread-local del reporte, no en la instancia: el service es
        único y lo comparten los threads del pool del listener, así que un cache
        de instancia mezclaría clientes distintos y quedaría desactualizado entre
        webhooks. Al ser thread-local, cada webhook ve solo el suyo y se descarta
        al terminar.
        """
        cache = getattr(self._reporte, "productos", None)
        if cache is None:
            cache = {}
            self._reporte.productos = cache
        return cache

    def _resolver_product_id(self, item, client_id, *, crear_si_falta: bool):
        """(sku, product_id) para un line item, dando de alta el SKU si hace falta.

        Unifica lo que las dos ramas hacían por separado: la externa creaba el
        producto y la interna se limitaba a descartar el item. Devuelve
        product_id=None si no se pudo resolver y `crear_si_falta` es False.

        El resultado se cachea por webhook: el mismo SKU se resolvía hasta tres
        veces contra Mintsoft (create_return, add_return_items y
        reallocate_return_items), y cada resolución son una o dos llamadas HTTP
        -- dos si hay que caer al fallback por barcode. Un return de RMA con
        varias unidades del mismo SKU las multiplicaba.
        """
        sku_original = (item or {}).get("sku")
        if isinstance(sku_original, str):
            sku_original = sku_original.strip()
        cache = self._cache_productos()
        clave = (client_id, str(sku_original))

        # Solo se cachean las resoluciones exitosas. Un None puede venir de una
        # llamada con crear_si_falta=False, y en ese caso una llamada posterior
        # con crear_si_falta=True todavía tiene que poder dar de alta el SKU.
        if clave in cache:
            return cache[clave]

        barcode = _get_item_barcode(item)
        sku, product_id = self.client.get_product_id(sku_original, client_id, barcode)

        if product_id is not None:
            cache[clave] = (sku, product_id)
            return sku, product_id

        if not crear_si_falta:
            return sku, product_id

        # El SKU no existe en Mintsoft: se da de alta.
        nuevo = {
            "SKU": sku,
            "Name": ((item or {}).get("product_variant") or {}).get("name") or (item or {}).get("sku"),
            "EAN": barcode,
            "ClientId": client_id,
            # Weight es requerido por el schema Product y antes no se mandaba, asi
            # que el alta podia volver con Success=false. El valor es un
            # placeholder: el payload de Two Boxes no trae el peso. Ver
            # config.PRODUCT_DEFAULT_WEIGHT.
            "Weight": config.PRODUCT_DEFAULT_WEIGHT,
        }
        product_id = self.client.create_product(nuevo)
        if product_id is not None:
            self.logger.warning(
                f"SKU {sku!r} dado de alta al vuelo en Mintsoft (ProductId "
                f"{product_id}) con Weight={config.PRODUCT_DEFAULT_WEIGHT} como "
                f"placeholder: el payload de Two Boxes no trae el peso. Hay que "
                f"completar la ficha del producto a mano."
            )
        if product_id is None:
            # Mintsoft rechaza el CreateExternalReturn entero si un item viene con
            # ProductId null, pero el mensaje que devuelve no dice cual fue. Fallar
            # acá nombra el SKU: mismo resultado, diagnóstico útil.
            raise RuntimeError(
                f"No se pudo crear el producto {sku!r} en Mintsoft, asi que el item "
                f"no tiene ProductId. El return no se crea: hay que dar de alta el "
                f"SKU a mano."
            )
        # Pausa para no saturar la API despues de un alta. Sale de configuracion
        # (default 3s, igual que antes) porque se paga dentro del thread que
        # procesa el return: son 3 segundos por SKU nuevo.
        if config.PRODUCT_CREATE_SLEEP_SECONDS > 0:
            time.sleep(config.PRODUCT_CREATE_SLEEP_SECONDS)
        cache[clave] = (sku, product_id)
        return sku, product_id

    def _asegurar_caja(self, carton_code, warehouse, client_id) -> None:
        """Crea la caja destino si Mintsoft no la conoce.

        La caja se crea SIEMPRE en RET, nunca en RET-TEMP, por dos razones:

          1. Es donde tiene que quedar la mercadería: RET-TEMP es la location
             transitoria de aislamiento, no un destino.
          2. Si la caja vive en RET-TEMP -- la misma location a la que el confirm
             alloca el item -- y ya contiene ese SKU, Mintsoft consolida la unidad
             nueva adentro de la caja y no queda nada suelto. Después el
             TransferStock falla con "Could not find any of product ID: X in
             RET-TEMP!". Con la caja en RET eso no pasa.

        El transfer sigue saliendo de RET-TEMP cuando corresponde: el destino es el
        código de caja, y Mintsoft arrastra la unidad a la location de la caja
        conservando Type='Quarantine'.
        """
        if self.client.check_carton(carton_code):
            return
        self.logger.info(f"Caja {carton_code!r} no esta en Mintsoft: se crea.")
        respuesta = self.client.create_carton(
            {
                "WarehouseId": warehouse,
                "StorageMediaName": "Stock",
                "Code": carton_code,
                "LocationId": config.location_id(warehouse, buen_estado=True),
            },
            client_id,
        ) or {}

        # C-2 -- la respuesta se miraba y se tiraba. El transfer sigue usando el
        # put_away_bin del payload (es el codigo que el operario escaneo y pego en
        # la caja fisica), pero si Mintsoft dice haber creado OTRO codigo, eso hay
        # que verlo: significa que el destino del transfer y la caja real no son
        # la misma, y el transfer va a fallar sin explicar por que.
        creado = respuesta.get("Code") or respuesta.get("CartonCode")
        if creado and str(creado).strip().upper() != carton_code.strip().upper():
            self.logger.error(
                f"Mintsoft creo la caja con el codigo {creado!r}, distinto del "
                f"put_away_bin {carton_code!r} que se va a usar como destino del "
                f"TransferStock. Revisar: el stock puede terminar en otra caja."
            )
        elif respuesta.get("ID"):
            self.logger.info(
                f"Caja {carton_code!r} creada (ID {respuesta.get('ID')})."
            )

    def _get_merchant_name(self, data) -> str:
        """Nombre del merchant, buscándolo en los tres lugares donde puede venir.

        Two Boxes lo manda tanto en event_data['merchant'] como en
        event_data['line_items'][0]['merchant']. Antes solo se miraba el del
        line item (y un event_data['merchant_integration'] que no existe en
        estos payloads), así que cualquier evento sin line_items -- o con un
        line item sin merchant -- devolvía "" y disparaba el mail de "cliente
        no mapeado" con el nombre vacío, aunque el merchant estuviera en el
        payload al lado.
        """
        event_data = (data or {}).get("event_data") or {}

        try:
            name = event_data["merchant_integration"]["merchant"]["name"]
            if name and name.strip():
                return name.strip()
        except (KeyError, TypeError):
            pass

        line_items = event_data.get("line_items") or []
        if line_items:
            merchant = (line_items[0] or {}).get("merchant") or {}
            name = merchant.get("name")
            if name and name.strip():
                return name.strip()

        # El merchant del return, presente incluso cuando no hay line_items.
        merchant = event_data.get("merchant") or {}
        name = merchant.get("name")
        if name and name.strip():
            return name.strip()

        return ""

    def _get_storefront_order_number(self, data) -> str:
        return data["event_data"]["line_items"][0]["storefront_order_number"]

    def _safe_get_storefront_order_number(self, data) -> Optional[str]:
        """Same as _get_storefront_order_number but returns None instead of raising
        when data is malformed -- so it can be used inside error-handling code."""
        try:
            return self._get_storefront_order_number(data)
        except Exception:
            return None

    def find_order_id(self, data) -> Optional[int]:
        """Busca en Mintsoft la orden a la que corresponde este return.

        Usa /api/Order/Search en vez de listar todas las órdenes del cliente.
        Antes esto hacía Order/List una vez por cada status (4, 5, 6) y buscaba
        el número a mano sobre el resultado, lo cual fallaba por dos motivos a
        la vez:

          1. Order/List recibe OrderStatusId, no statusId, así que el filtro se
             ignoraba y las tres llamadas devolvían las MISMAS 100 filas.
          2. Sin PageNo/Limit solo se veían las 100 órdenes más recientes del
             cliente, así que cualquier orden de más de unos días no aparecía.

        Resultado: la orden existía en Mintsoft pero no se encontraba, y el
        return se creaba como externo. Order/Search resuelve ambas cosas con una
        sola llamada y matchea también ExternalOrderReference.

        Devuelve el OrderId, o None si la orden no está (ahí sí corresponde un
        return externo). Si la búsqueda falla contra la API, propaga la
        excepción: no podemos asumir "no existe" y crear un externo de más.
        """
        order_number = self._safe_get_storefront_order_number(data)
        if not order_number:
            self.logger.warning("Payload sin storefront_order_number, no se puede buscar la orden")
            return None

        merchant_name = self._get_merchant_name(data)
        client_id = map_client(merchant_name)
        target = _normalize_order_number(order_number)

        for term in _order_number_variants(order_number):
            candidates = self.client.search_orders(term)
            self.logger.info(
                f"Order/Search({term!r}) devolvió {len(candidates)} orden(es)"
            )

            matches = []
            for order in candidates:
                if client_id is not None and order.get("ClientId") != client_id:
                    continue
                if target in (
                    _normalize_order_number(order.get("OrderNumber")),
                    _normalize_order_number(order.get("ExternalOrderReference")),
                ):
                    matches.append(order)

            if not matches:
                continue

            returnable = [
                o for o in matches
                if o.get("OrderStatusId") in self.returnable_status_ids
            ]

            if not returnable:
                # La orden existe pero todavía no salió del depósito (o está
                # cancelada). No le creamos un return interno.
                estados = [o.get("OrderStatusId") for o in matches]
                self.logger.warning(
                    f"Orden {order_number} encontrada en Mintsoft pero en estado(s) "
                    f"{estados}, fuera de {sorted(self.returnable_status_ids)}. "
                    f"No se crea return interno."
                )
                return None

            if len(returnable) > 1:
                self.logger.warning(
                    f"Orden {order_number} matcheó {len(returnable)} órdenes de Mintsoft "
                    f"({[o.get('ID') for o in returnable]}); uso la primera."
                )

            order = returnable[0]
            self.logger.info(
                f"Orden {order_number} -> Mintsoft OrderId={order.get('ID')} "
                f"(OrderNumber={order.get('OrderNumber')!r}, "
                f"ExternalOrderReference={order.get('ExternalOrderReference')!r}, "
                f"OrderStatusId={order.get('OrderStatusId')})"
            )
            return order.get("ID")

        self.logger.info(
            f"Orden {order_number} no encontrada en Mintsoft para ClientId {client_id}"
        )
        return None

    def _return_identifier(self, data) -> str:
        """Reference con el que se guarda el return en Mintsoft.

        TODO return, externo o interno, tiene que llevar Reference: es lo que
        después se busca como "PO reference" en Mintsoft. Prioridad:

          1. tracking_number del primer line item
          2. si viene vacío, el storefront_order_number

        Si no hubiera ninguno de los dos se cae a completed_at + mail del
        cliente, para que al menos el mail de error identifique el return.
        """
        event_data = data.get("event_data") or {}
        line_items = event_data.get("line_items") or []

        if line_items:
            tracking = str((line_items[0] or {}).get("tracking_number") or "").strip()
            if tracking:
                return tracking

        order_number = str(self._safe_get_storefront_order_number(data) or "").strip()
        if order_number:
            return order_number

        completed_at = event_data.get("completed_at")
        customer_email = (event_data.get("customer") or {}).get("email")
        return f"{completed_at}-{customer_email}"

    def create_return(self, data) -> Optional[int]:
        # Se inicializan acá porque el except de abajo los mete en el context del
        # mail: si algo falla antes de asignarlos, el propio handler tiraba NameError.
        merchant_name = None
        client_id = None
        warehouse = None
        order_id = None

        try:
            merchant_name = self._get_merchant_name(data)
            client_id = map_client(merchant_name) # Si no encuentra devuelve None
            warehouse = map_warehouse(merchant_name)

            if client_id is None:
                self.logger.error(
                    f"Merchant {merchant_name!r} no esta en la tabla de clientes: "
                    f"no se puede procesar el return. (map_client ya avisó por mail.)"
                )
                return None, "No Return Created"

            # Un return en el que NINGUNA unidad llegó no tiene nada que registrar.
            # Antes se creaba igual: la rama externa armaba ReturnItems=[] (porque
            # los Missing se saltean) y la interna creaba un return vacío que
            # después se confirmaba sin items.
            todos_los_items = (data.get("event_data") or {}).get("line_items") or []
            if todos_los_items and all(_is_missing(li) for li in todos_los_items):
                self.logger.info(
                    f"Los {len(todos_los_items)} item(s) del return están Missing: no "
                    f"llegó ninguna unidad al depósito, no se crea return en Mintsoft."
                )
                return None, "No Return Created"

            order_id = self.find_order_id(data)

            if order_id is None: # Si es un external return
                self.logger.info("Order not found in Mintsoft. Creating EXTERNAL return.")

                event_data = data["event_data"]
                line_items = event_data.get("line_items", [])
                return_identifier = self._return_identifier(data)

                if len(return_identifier) > config.REFERENCE_MAX_LEN:
                    # Mintsoft corta la Reference: si se trunca, el PO reference
                    # que se busca despues no es el que se ve en el mail.
                    self.logger.warning(
                        f"Reference truncada a {config.REFERENCE_MAX_LEN} caracteres: "
                        f"{return_identifier!r} -> "
                        f"{return_identifier[:config.REFERENCE_MAX_LEN]!r}"
                    )
                external_return_data = {
                    "Reference": return_identifier[:config.REFERENCE_MAX_LEN],
                    "ClientId": client_id,
                    "WarehouseId": warehouse,
                    "ReturnItems": [],
                }
                # Tracking a nivel return: el primero que traiga alguno de los items.
                # Se usaba como fallback (`or return_tracking_number`) sin estar
                # definido, así que la rama externa tiraba NameError.
                # Ojo: no se reusa return_identifier porque ese cae al número de
                # orden cuando no hay tracking, y acá queremos un tracking o nada.
                return_tracking_number = next(
                    (
                        str((li or {}).get("tracking_number") or "").strip()
                        for li in line_items
                        if str((li or {}).get("tracking_number") or "").strip()
                    ),
                    "",
                )
                # Trackings ya escritos en Comments, para no repetirlos en cada item
                commented_tracking_numbers = set()

                for item in line_items:
                    item = item or {}  # un line_item null rompia el loop entero

                    # Missing se chequea ANTES de tocar Mintsoft: la unidad no llegó,
                    # no hay nada que dar de alta ni que ubicar, y así no se gasta un
                    # get_product_id (ni un alta de SKU) para un item que se descarta.
                    if _is_missing(item):
                        self.logger.info(
                            f"Item {item.get('sku')} con disposition='Missing': no "
                            f"llegó, no se agrega al return externo."
                        )
                        continue

                    sku, product_id = self._resolver_product_id(
                        item, client_id, crear_si_falta=True
                    )
                    return_reason = config.return_reason_id(item.get("disposition"))

                    return_item_data = {
                        "SKU": sku,
                        "ProductId": product_id,
                        "Quantity": item.get("quantity"),
                        "Action": "NONE",
                        "ReturnReasonId": return_reason,
                    }

                    # Tracking del item, y si no tiene usamos el del return.
                    # Solo se escribe una vez en Comments, sin importar la cantidad de items.
                    tracking_number = (item.get("tracking_number") or "").strip() or return_tracking_number
                    if tracking_number and tracking_number not in commented_tracking_numbers:
                        return_item_data["Comments"] = tracking_number
                        commented_tracking_numbers.add(tracking_number)

                    external_return_data["ReturnItems"].append(return_item_data)

                # Sin volcar el payload: trae SKUs y el tracking. El resumen
                # alcanza para saber que se mando.
                self.logger.info(
                    f"CreateExternalReturn: ClientId={client_id} WarehouseId={warehouse} "
                    f"items={len(external_return_data['ReturnItems'])} "
                    f"Reference={external_return_data['Reference']!r}"
                )
                external_return_id = self.client.create_external_return(data=external_return_data)

                self.logger.info(f"External return created. ID: {external_return_id}")
                # Se graba ANTES de ubicar items y mover stock: si algo de eso falla,
                # un reproceso tiene que ver que el return ya existe.
                self._registrar_return(external_return_id, "external")

                return external_return_id, "External Return Created" # Crea Return Externa (con el Order ID)

            # Si es un Internal Return
            # El warehouse sale del mapeo del cliente (3 = Wholesale, 5 = E-Comm),
            # no de un 3 fijo. Antes se pasaba warehouse_id=3 y encima el cliente
            # no lo mandaba en la request, asi que Mintsoft usaba el warehouse de
            # la orden mientras el log decia "Warehouse ID = 3".
            #
            # El Reference va también acá: la rama externa siempre lo seteaba y la
            # interna no, así que un return interno quedaba sin PO reference y no se
            # podía encontrar en Mintsoft. Antes no se notaba porque la búsqueda de
            # orden estaba rota y TODOS los returns salían externos.
            return_identifier = self._return_identifier(data)
            self.logger.info(
                f"Order found (ID={order_id}). Creating standard return on WarehouseId={warehouse} "
                f"(merchant {merchant_name!r}, "
                f"Reference={return_identifier[:config.REFERENCE_MAX_LEN]!r})."
            )
            return_id = self.client.create_return(
                order_id,
                warehouse_id=warehouse,
                reference=return_identifier[:config.REFERENCE_MAX_LEN],
            )

            self.logger.info(f"Created return with ID: {return_id}")
            self._registrar_return(return_id, "internal")
            return return_id, "Internal Return Created"

        except Exception as e:
            self.logger.error(f"Error creating return: {e}", exc_info=True)
            self._send_error_email(
                method="create_return",
                error=e,
                order_reference=self._return_identifier(data),
                que_falta="El return NO se creo en Mintsoft",
                accion=(
                    "Crear el return a mano en Mintsoft y ubicar los items: el stock "
                    "devuelto no quedo registrado en ninguna parte."
                ),
                context={
                    "merchant_name": merchant_name,
                    "client_id": client_id,
                    "warehouse": warehouse,
                    "order_id": order_id,
                },
            )
            return None, "No Return Created"

    def allocate_external_return_items(self, data, return_id: int):
        # La EXTRACCION del payload va dentro del try: si el evento tiene una forma
        # inesperada, antes lanzaba aca afuera y se salteaba el handler, asi que no
        # se reportaba nada y el error escapaba al catch-all del listener.
        merchant_name = None
        warehouse = None

        try:
            merchant_name = self._get_merchant_name(data)
            warehouse = map_warehouse(merchant_name)
            return_details = self.client.get_return_details(return_id)
            return_items = return_details.get('ReturnItems')

            for item in return_items:
                # 1 = Good Stock -> RET;  2 = Quarantine -> RET-TEMP.
                # Los ids de location salen de config.RETURN_LOCATIONS: antes estaban
                # escritos a mano acá y en otros cuatro bloques.
                return_reason = item.get('ReturnReasonId')
                buen_estado = return_reason == config.RETURN_REASON_GOOD
                location_id = config.location_id(warehouse, buen_estado=buen_estado)
                self.logger.info(
                    f"ReturnItem {item.get('ID')}: ReturnReasonId={return_reason} "
                    f"-> LocationId={location_id}"
                )

                # Ojo con el nombre: esto se llamaba `data` y pisaba el parametro con
                # el payload del webhook, asi que el handler de abajo no podia sacar la
                # referencia y TODOS estos mails salian con "POReference: UNKNOWN".
                allocation_data = {
                    'ReturnItemId': item.get('ID'),
                    'Quantity': item.get('Quantity'),
                    'LocationId': location_id
                }

                response = self.client.allocate_return_item_location(return_id, allocation_data)
                self.logger.info(f"Allocated External Return Items to {location_id}: {response}")


            response = self.client.confirm_return(return_id)
            self.logger.info(f"Confirmed return {return_id}: {response}")

            return None

        except Exception as e:
            self.logger.error(f"Error allocating external return items for return {return_id}: {e}", exc_info=True)
            self._send_error_email(
                method="allocate_external_return_items",
                error=e,
                order_reference=self._return_identifier(data),
                que_falta=(
                    f"El return externo {return_id} quedo con items sin ubicar y SIN "
                    f"confirmar"
                ),
                accion=(
                    f"Abrir el return {return_id} en Mintsoft, ubicar los items que "
                    f"falten en RET / RET-TEMP y confirmarlo. El loop corta en el primer "
                    f"item que falla, asi que los siguientes tampoco se ubicaron."
                ),
                context={
                    "merchant_name": merchant_name,
                    "warehouse": warehouse,
                    "return_id": return_id,
                },
            )
            raise


    def add_return_items(self, return_id: int, data: Dict) -> Optional[Dict[str, Any]]:

        self.logger.info(f"Starting to add items to return {return_id}")

        try:
            merchant_name = self._get_merchant_name(data)
            client_id = map_client(merchant_name) # Si no encuentra devuelve None
            warehouse = map_warehouse(merchant_name) # 3 si es Wholesale, 5 si es E-Comm
            event_data = data.get("event_data", {})
            line_items = event_data.get("line_items", [])

            if not line_items:
                self.logger.warning("No line items found in return data")
                return True

            # Guardaremos el (ReturnItemId, item, location_id) para allocarlos luego
            items_to_allocate = []

            # Items que se caen del return sin abortarlo. Se reportan al final: un return
            # con menos unidades de las que devolvio el cliente tiene que ser visible.
            dropped_items = []

            # Step 1: Add items to the return
            for item in line_items:
                item = item or {}  # un line_item null rompia el loop entero
                disposition = item.get("disposition")

                if _is_missing(item):
                    sku_log = (item.get("sku") or "").strip()
                    self.logger.info(
                        f"Item {sku_log} con disposition='Missing': no llegó, no se "
                        f"agrega al return."
                    )
                    continue

                sku = (item.get("sku") or "").strip()
                if not sku:
                    self.logger.warning("Skipping line item with missing or empty SKU")
                    dropped_items.append({"sku": None, "motivo": "line item sin SKU"})
                    continue

                product_id = None
                try:
                    # Por el helper, no por el cliente directo: asi comparte el
                    # cache de SKUs con create_return y reallocate_return_items.
                    sku, product_id = self._resolver_product_id(
                        item, client_id, crear_si_falta=False
                    )
                except Exception as e:
                    self.logger.error(f"Error al obtener product_id para SKU {sku}: {e}", exc_info=True)
                    dropped_items.append({"sku": sku, "motivo": f"{type(e).__name__}: {e}"})
                    continue

                return_reason = config.return_reason_id(disposition)

                graded_attributes = item.get("graded_attributes") or []
                return_photos = item.get("photo_urls", [])

                quantity = item.get("quantity")
                try:
                    quantity = max(1, int(quantity)) if quantity is not None else 1
                except (TypeError, ValueError):
                    quantity = 1

                item_data = {
                    "Quantity": quantity,
                    "ReturnReasonId": return_reason,
                    "ProductId": product_id,
                    "Action": "NONE",
                    "ReturnPhotos": return_photos
                }

                if graded_attributes:
                    ga = graded_attributes[0] or {}
                    mg = (ga.get("merchant_grading_attribute") or {}).get("grading_attribute") or {}
                    grading_title = (mg.get("title") or "").strip()
                    if grading_title:
                        item_data["Comments"] = grading_title

                response = self.client.add_return_item(return_id, item_data)
                self.logger.info(f"Added item {sku} to return {return_id}: {response}")

                if not response or not response.get("Success"):
                    msg = response.get("Message") if response else "Unknown error"
                    self.logger.error(f"Mintsoft AddItem failed for SKU {sku}: {msg}")
                    raise RuntimeError(f"Mintsoft AddItem failed: {msg}")

                return_item_id = response.get("ID")

                # Ubicación de asignación de ESTE ítem. El merchant ya se resolvió
                # arriba del loop: se llamaba a _get_merchant_name() una vez por item
                # para recalcular siempre lo mismo.
                returns_location_id = self._location_de(warehouse, item)

                # Guardamos la referencia directa del ID de la devolución que nos devolvió Mintsoft
                items_to_allocate.append({
                    "ReturnItemId": return_item_id,
                    "LocationId": returns_location_id,
                    "Quantity": quantity,
                    "ProductId": product_id
                })

            # Step 2: Allocate locations for items
            for alloc in items_to_allocate:
                allocation_data = {
                    "ReturnItemId": alloc["ReturnItemId"],
                    "LocationId": alloc["LocationId"],
                    "Quantity": alloc["Quantity"],
                }

                response = self.client.allocate_return_item_location(return_id, allocation_data)
                self.logger.info(f"Allocated location {alloc['LocationId']} for ReturnItemId {alloc['ReturnItemId']}: {response}")

            # Step 3: Confirm the return
            self.logger.info(f"Confirming return {return_id}")
            response = self.client.confirm_return(return_id)
            self.logger.info(f"Confirmed return {return_id}: {response}")

            # El return quedo confirmado igual, pero si se cayeron items quedo corto
            # (o vacio, si se cayeron todos). Hay que avisar para corregirlo a mano.
            if dropped_items:
                detalle = ", ".join(
                    f"{d['sku']} ({d['motivo']})" for d in dropped_items
                )
                self.logger.error(
                    f"Return {return_id} confirmado con {len(items_to_allocate)} de "
                    f"{len(line_items)} items. Items caidos: {detalle}"
                )
                self._send_error_email(
                    method="add_return_items",
                    error=RuntimeError(
                        f"Return {return_id} confirmado incompleto: "
                        f"{len(items_to_allocate)} de {len(line_items)} items"
                    ),
                    order_reference=self._return_identifier(data),
                    sku=", ".join(str(d.get("sku")) for d in dropped_items),
                    que_falta=(
                        f"Faltan {len(dropped_items)} de {len(line_items)} items en el "
                        f"return {return_id}, que quedo confirmado y corto"
                    ),
                    accion=(
                        f"Agregar a mano al return {return_id}: {detalle}. Ya esta "
                        f"confirmado, asi que hay menos unidades registradas de las que "
                        f"devolvio el cliente."
                    ),
                    context={
                        "return_id": return_id,
                        "items_agregados": len(items_to_allocate),
                        "items_en_el_payload": len(line_items),
                        "items_caidos": dropped_items,
                    },
                )

            return True

        except Exception as e:
            # Se devuelve False (no None) para que el listener sepa que el return no
            # quedo armado y NO mueva stock. Antes esto absorbia el error y devolvia
            # None, indistinguible del camino exitoso.
            self.logger.error(f"Error adding items to return {return_id}: {e}", exc_info=True)
            self._send_error_email(
                method="add_return_items",
                error=e,
                order_reference=self._return_identifier(data),
                context={"return_id": return_id},
                que_falta=f"El return interno {return_id} quedo sin items y SIN confirmar",
                accion=(
                    f"Abrir el return {return_id} en Mintsoft, agregar los items, "
                    f"ubicarlos en RET / RET-TEMP y confirmarlo."
                ),
            )
            return False
    
    def reallocate_return_items(self, data):
        # Igual que en allocate_external_return_items: la extraccion del payload va
        # adentro del try, para que una forma inesperada se reporte y no escape.
        merchant_name = None
        client_id = None
        line_items: List[Any] = []

        # Se acumula un resultado por item reasignado. Hay que inicializarla acá:
        # responses.append() y `return responses` se usaban sin que la lista
        # existiera, así que reallocate_return_items() tiraba NameError en cuanto
        # llegaba al primer transfer_stock().
        responses: List[Any] = []
        # Items sin put_away_bin: no se pueden mover a ninguna caja. Se saltean
        # para no bloquear a los demas, y al final se lanza para que salga el mail.
        sin_caja: List[str] = []
        # Items que nunca llegaron (disposition='Missing'): no hay stock que mover.
        # Se cuentan aparte para no mezclarlos con los que SI deberian tener caja.
        faltantes: List[str] = []
        # Items cuya reubicacion fallo contra Mintsoft. Se juntan y se reportan
        # todos juntos al final, en vez de cortar en el primero.
        fallados: List[Dict[str, str]] = []
        # El reporte de stock se baja una sola vez por llamada, no por item.
        stock_cache: Dict[Any, Any] = {}

        try:
            merchant_name = self._get_merchant_name(data)
            client_id = map_client(merchant_name)  # Si no encuentra devuelve None
            # Una sola vez, no una por item: antes se llamaba a _get_merchant_name()
            # y map_warehouse() dentro del loop para recalcular siempre lo mismo.
            warehouse = map_warehouse(merchant_name) # 3 si es Wholesale, 5 si es E-Comm
            event_data = data.get("event_data") or {}
            line_items = event_data.get("line_items", []) or []
            for item in line_items:
                item = item or {}  # un line_item null rompia el loop entero
                sku = item.get("sku")

                # Missing se chequea PRIMERO: antes del get_product_id y antes del
                # chequeo de caja. Estos items no traen put_away_bin -- y esta bien
                # que no lo traigan, porque no hay unidad que guardar -- pero como
                # `disposition` se leia DESPUES del chequeo de caja, caian en
                # `sin_caja` y disparaban el mail "Items sin put_away_bin" por lo que
                # es el comportamiento correcto. Ademas se les gastaba un
                # get_product_id contra Mintsoft para nada.
                if _is_missing(item):
                    self.logger.info(
                        f"Item {sku} con disposition='Missing': no llego al deposito, "
                        f"no hay stock que reubicar."
                    )
                    faltantes.append(str(sku))
                    continue

                # Un item que falla NO puede abortar la reubicacion de los
                # demas. Antes este loop re-lanzaba en el primer error, asi que
                # el stock de todos los items siguientes quedaba en RET /
                # RET-TEMP sin que el mail dijera cuales. add_return_items ya
                # juntaba sus items caidos; esto lo hace simetrico.
                try:
                    sku, product_id = self._resolver_product_id(
                        item, client_id, crear_si_falta=False
                    )
                    carton_code = (item.get("put_away_bin") or "").strip()

                    if not carton_code:
                        # Sin caja destino, el TransferStock iria a DestinationNameOrCode=""
                        # y antes ademas check_carton devolvia True para el codigo vacio.
                        self.logger.error(
                            f"Item {sku} sin put_away_bin: no hay caja destino, se saltea la "
                            f"reubicacion de stock."
                        )
                        sin_caja.append(str(sku))
                        continue

                    disposition = item.get("disposition")
                    if config.es_buen_estado(disposition): # Stock en buenas condiciones

                        reallocation_data = {
                            "SourceWarehouseId": warehouse,
                            "SourceNameOrCode": config.SOURCE_GOOD,
                            "DestinationWarehouseId": warehouse,
                            "DestinationNameOrCode": carton_code,
                            "ProductId": product_id,
                            "Quantity": item.get("quantity"),
                            "Comment": "Return reallocation",
                        }

                        self._asegurar_caja(carton_code, warehouse, client_id)

                        response = self._transfer_stock_resiliente(
                            reallocation_data, sku, client_id, stock_cache
                        )
                        responses.append(response)
                        self.logger.info(f"{sku}: transfer a {carton_code!r} -> {response}")

                    else: # Stock a mandar a cuarentena

                        # El TransferStock de abajo lleva Type='Quarantine', asi que solo
                        # mueve stock YA cuarentenado. Que la unidad llegue cuarentenada a
                        # RET-TEMP depende de la rama, y no es igual en las dos:
                        #
                        #  - Return INTERNO: add_return_items alloca el item a RET-TEMP y
                        #    DESPUES confirma, asi que el StockAction='Quarantine' de
                        #    ReturnReasonId=2 cae sobre la unidad ya ubicada y la deja en
                        #    RET-TEMP con Type='Quarantine'. Acá el transfer funciona.
                        #
                        #  - Return EXTERNO: CreateExternalReturn crea el return YA
                        #    confirmado, o sea que la cuarentena se aplica ANTES de que el
                        #    item tenga ubicacion. Recien despues AllocateItemLocation lo
                        #    deja en RET-TEMP, y lo deja como Type='Allocation'. El segundo
                        #    confirm_return no re-aplica nada porque ya estaba confirmado.
                        #    Resultado: no hay stock cuarentenado en RET-TEMP y el transfer
                        #    falla con "Could not find any of product ID: X in RET-TEMP!".
                        #
                        # Verificado con el return de tracking 9434636208303429481960
                        # (SKU W836-2-Chocolate-6, disposition 'Exception', Bronze Snake):
                        # quedo en RET-TEMP con Type='Allocation' y sin caja asociada.
                        #
                        # Por eso la cuarentena se pide explicitamente y el fallo NO aborta:
                        # en la rama interna la unidad ya esta cuarentenada y Mintsoft va a
                        # rechazar el movimiento, y ahi el transfer de abajo funciona igual.

                        temporary_location_id = config.location_id(
                            warehouse, buen_estado=False
                        )

                        reallocation_data = {
                            "SourceWarehouseId": warehouse,
                            "SourceNameOrCode": config.SOURCE_QUARANTINE,
                            "DestinationWarehouseId": warehouse,
                            "DestinationNameOrCode": carton_code,
                            "ProductId": product_id,
                            "Quantity": item.get("quantity"),
                            "Type": "Quarantine",
                            "Comment": "Return reallocation",
                        }

                        # La caja se crea en RET, no en RET-TEMP. El por qué está en
                        # _asegurar_caja.
                        self._asegurar_caja(carton_code, warehouse, client_id)

                        try:
                            self.client.quarantine_stock({
                                # "ProductId", no "ProductID": es la grafia que usan
                                # TransferStock, AddItem y CreateExternalReturn en toda la
                                # API. Con "ProductID" el producto no bindeaba y Mintsoft
                                # contestaba "Unable to Quarantine stock as not enough could
                                # be found in the selected location!", que se leyo como "ya
                                # estaba cuarentenado" cuando era un nombre de campo mal
                                # escrito.
                                "ProductId": product_id,
                                "WarehouseId": warehouse,
                                "LocationId": temporary_location_id,
                                "Quantity": item.get("quantity"),
                                "Comment": "Returned stock sent to Quarantine",
                            }, timeout=25)
                            self.logger.info(
                                f"{sku}: cuarentenado en RET-TEMP. Reubicando a la caja {carton_code}."
                            )
                        except Exception as qt_err:
                            # No aborta: en la rama interna la unidad ya viene cuarentenada
                            # por el confirm y este movimiento se rechaza, pero el transfer
                            # de abajo si funciona. Si el transfer tambien falla, ese si
                            # lanza y se reporta con el mail de error.
                            self.logger.warning(
                                f"{sku}: no se pudo cuarentenar en RET-TEMP ({qt_err}). "
                                f"Si el return es interno la unidad ya estaba cuarentenada; "
                                f"se intenta el transfer a {carton_code} igual."
                            )

                        response = self._transfer_stock_resiliente(
                            reallocation_data, sku, client_id, stock_cache
                        )
                        responses.append(response)
                        self.logger.info(f"{sku}: transfer a {carton_code!r} -> {response}")
                except Exception as item_err:
                    self.logger.error(
                        f"{sku}: fallo la reubicacion de stock ({item_err}). Se "
                        f"sigue con los demas items.",
                        exc_info=True,
                    )
                    fallados.append({
                        "sku": str(sku),
                        "error": f"{type(item_err).__name__}: {item_err}",
                    })
                    continue

            # Se lanza DESPUES de recorrer todos los items, no en el primer
            # problema: asi el mail lista todo lo que quedo sin reubicar y el
            # resto del return si se procesa.
            if sin_caja or fallados:
                partes = []
                if sin_caja:
                    partes.append(
                        f"sin put_away_bin ({', '.join(sin_caja)})"
                    )
                if fallados:
                    partes.append(
                        "con error ("
                        + "; ".join(f"{f['sku']}: {f['error']}" for f in fallados)
                        + ")"
                    )
                raise RuntimeError(
                    "Items cuyo stock NO se reubico -- " + " | ".join(partes)
                )

            if faltantes:
                self.logger.info(
                    f"{len(faltantes)} item(s) salteados por estar Missing: "
                    f"{', '.join(faltantes)}"
                )

            # Se cuenta sobre los reubicables, no sobre len(line_items): con items
            # Missing en el payload el mensaje anterior exageraba el faltante.
            reubicables = len(line_items) - len(faltantes)
            if reubicables and not responses:
                self.logger.warning(
                    f"No se reasigno ningun item de los {reubicables} reubicables del "
                    f"payload (ninguno traia put_away_bin): el stock quedo en RET / RET-TEMP"
                )

            return responses

        except Exception as e:
            self.logger.error(f"Error reallocating return items: {e}", exc_info=True)
            # Nada de recalcular el identificador a mano acá: line_items[0] con lista
            # vacía o un customer ausente lanzaban DENTRO del handler y el error real
            # quedaba tapado por el del propio handler.
            # El mail nombra TODOS los items que quedaron sin reubicar, separando
            # los dos motivos: sin put_away_bin (no hay caja destino) y los que
            # fallaron contra Mintsoft. Antes solo podia nombrar los primeros,
            # porque el loop cortaba en el primer error y no sabia que mas faltaba.
            pendientes = list(sin_caja) + [f["sku"] for f in fallados]
            detalles = []
            if sin_caja:
                detalles.append(
                    f"{len(sin_caja)} sin put_away_bin ({', '.join(sin_caja)})"
                )
            if fallados:
                detalles.append(f"{len(fallados)} con error de Mintsoft")

            self._send_error_email(
                method="reallocate_return_items",
                error=e,
                order_reference=self._return_identifier(data),
                sku=", ".join(pendientes) if pendientes else None,
                que_falta=(
                    f"{len(pendientes)} item(s) con el stock sin mover "
                    f"({'; '.join(detalles)}): quedo en RET / RET-TEMP"
                    if pendientes else
                    "El stock quedo en RET / RET-TEMP, sin mover a la caja del operario"
                ),
                accion=(
                    f"Mover a mano el stock de {', '.join(pendientes)} desde "
                    f"RET / RET-TEMP a la caja fisica que corresponda. El return SI "
                    f"esta creado y confirmado; lo que falta es el movimiento de "
                    f"stock. Los demas items del return SI se movieron."
                    if pendientes else
                    "Mover a mano el stock desde RET / RET-TEMP a la caja del "
                    "put_away_bin. El return SI esta creado y confirmado; lo que falta "
                    "es el movimiento de stock."
                ),
                context={
                    "merchant_name": merchant_name,
                    "client_id": client_id,
                    "items_reasignados": len(responses),
                    "items_en_el_payload": len(line_items),
                    "items_sin_caja": sin_caja,
                    "items_fallados": fallados,
                },
            )
            raise