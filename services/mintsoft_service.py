import os
import json
import sys
import socket
import smtplib
import traceback
from email.message import EmailMessage
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from loggers.main_logger import get_logger
from clients.mintsoftClient import MintsoftOrderClient
from mappers.main_mapper import map_return
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


class MintsoftReturnService:
    def __init__(self, logger_name: str = "mintsoft_service", log_file: str = "m_service.log"):
        self.logger = get_logger(logger_name, log_file)
        self.client = MintsoftOrderClient()
        # Estados en los que una orden ya salió del depósito y por lo tanto se
        # le puede crear un return: 4=DESPATCHED, 5=INVOICED, 6=INVOICEFAILED.
        self.returnable_status_ids = {4, 5, 6}

        # ----- Email notification config (read from environment) -----
        self.smtp_host = os.environ.get("SMTP_HOST")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.smtp_user = os.environ.get("SMTP_USER")
        self.smtp_password = os.environ.get("SMTP_PASSWORD")
        self.alert_email_to = "bgallo@the5411.com, jcordero@the5411.com, ngurfinkel@the5411.com, mbivort@the5411.com",
        self.alert_email_from = os.environ.get("ALERT_EMAIL_FROM", self.smtp_user or "")

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
    ) -> None:
        try:
            if not (self.smtp_host and self.smtp_user and self.smtp_password and self.alert_email_to):
                self.logger.warning(
                    "Email alert NOT sent (SMTP credentials missing). "
                    "Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO env vars."
                )
                return

            host = socket.gethostname()
            ts = datetime.utcnow().isoformat() + "Z"
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

    def _get_merchant_name(self, data) -> str:
        """Get merchant name from event_data; supports both merchant_integration and line_items[0].merchant."""
        event_data = data["event_data"]
        try:
            return event_data["merchant_integration"]["merchant"]["name"]
        except (KeyError, TypeError):
            pass
        line_items = event_data.get("line_items") or []
        if line_items:
            merchant = (line_items[0] or {}).get("merchant") or {}
            name = merchant.get("name")
            if name:
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
                    print ("Client not in Mintsoft, return cannot be processed")
                    return None, "No Return Created"

            order_id = self.find_order_id(data)

            if order_id is None: # Si es un external return
                self.logger.info("Order not found in Mintsoft. Creating EXTERNAL return.")

                event_data = data["event_data"]
                line_items = event_data.get("line_items", [])
                return_identifier = self._return_identifier(data)

                external_return_data = {
                    "Reference": return_identifier[:50],
                    "ClientId": client_id,
                    "WarehouseId": warehouse,
                    "ReturnItems": [],
                }
                for item in line_items:
                    sku = item.get("sku")
                    sku, product_id = self.client.get_product_id(sku, client_id, item.get("barcode"))

                    if product_id == None:
                        # Si el item no existe en Mintsoft con ese SKU

                        new_product_data = {
                            "SKU": sku,
                            "Name": (item.get("product_variant") or {}).get("name") or item.get("sku"),
                            "EAN": item.get("barcode"),
                            "ClientId": client_id,
                        }

                        created_product_id = self.client.create_product(new_product_data)
                        
                        # Usamos el ID del item recien creado
                        product_id = created_product_id

                        # Sleep de 3 segundos para no saturar la API
                        time.sleep(3)
                        
                    disposition = item.get("disposition")

                    if disposition == "Return to Stock":
                        return_reason = 1

                    elif disposition == "Missing":
                        print(f"Item {sku} faltante en el return")
                        continue

                    else:
                        return_reason = 2

                    external_return_data["ReturnItems"].append({
                        "SKU": sku,
                        "ProductId": product_id,
                        "Quantity": item.get("quantity"),
                        "Action": "NONE",
                        "ReturnReasonId": return_reason,
                    })

                print(external_return_data)
                external_return_id = self.client.create_external_return(data=external_return_data)

                self.logger.info(f"External return created. ID: {external_return_id}")

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
                f"(merchant {merchant_name!r}, Reference={return_identifier[:50]!r})."
            )
            return_id = self.client.create_return(
                order_id,
                warehouse_id=warehouse,
                reference=return_identifier[:50],
            )

            self.logger.info(f"Created return with ID: {return_id}")
            return return_id, "Internal Return Created"

        except Exception as e:
            self.logger.error(f"Error creating return: {e}", exc_info=True)
            self._send_error_email(
                method="create_return",
                error=e,
                order_reference=self._return_identifier(data),
                context={
                    "merchant_name": merchant_name,
                    "client_id": client_id,
                    "warehouse": warehouse,
                    "order_id": order_id,
                },
            )
            return None, "No Return Created"

    def allocate_external_return_items(self, data, return_id: int):
        merchant_name = self._get_merchant_name(data)
        warehouse = map_warehouse(merchant_name)

        try:
            return_details = self.client.get_return_details(return_id)
            return_items = return_details.get('ReturnItems')

            for item in return_items:
                return_reason = item.get('ReturnReasonId') # 1 es Good Stock, 2 es Quarantine
                print("return_reason",return_reason)
                if return_reason == 1: # Si esta en buena condicion
                    if warehouse == 3:
                        location_id = 4104 # RET Wholesale
                    else:
                        location_id = 4299 # RET E-Commerce

                else: # Si esta en mala condicion
                    if warehouse == 3:
                        location_id = 9 # RET-TEMP Wholesale
                    else:
                        location_id = 4304 # RET-TEMP E-Commerce

                data = {
                    'ReturnItemId': item.get('ID'),
                    'Quantity': item.get('Quantity'),
                    'LocationId': location_id
                }

                response = self.client.allocate_return_item_location(return_id, data)
                self.logger.info(f"Allocated External Return Items to {location_id}: {response}")

            return None

        except Exception as e:
            self.logger.error(f"Error allocating external return items for return {return_id}: {e}", exc_info=True)
            # `data` may have been overwritten in the loop above, so try to pull a
            # reference from it only if it still looks like the original payload.
            ref = None
            if isinstance(data, dict) and "event_data" in data:
                ref = self._safe_get_storefront_order_number(data)
            self._send_error_email(
                method="allocate_external_return_items",
                error=e,
                order_reference=ref,
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
            event_data = data.get("event_data", {})
            line_items = event_data.get("line_items", [])

            if not line_items:
                self.logger.warning("No line items found in return data")
                return None

            # Guardaremos el (ReturnItemId, item, location_id) para allocarlos luego
            items_to_allocate = []

            # Step 1: Add items to the return
            for item in line_items:
                disposition = item.get("disposition")

                if disposition == "Missing":
                    sku_log = (item.get("sku") or "").strip()
                    self.logger.info(f"Item {sku_log} faltante en el return. Saltando.")
                    continue

                sku = (item.get("sku") or "").strip()
                if not sku:
                    self.logger.warning("Skipping line item with missing or empty SKU")
                    continue

                product_id = None
                try:
                    sku, product_id = self.client.get_product_id(sku, client_id, item.get("barcode"))
                except Exception as e:
                    self.logger.error(f"Error al obtener product_id para SKU {sku}: {e}")
                    continue

                if disposition == "Return to Stock":
                    return_reason = 1
                else:
                    return_reason = 2

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

                # Determinar la ubicación de asignación para ESTE ítem específico
                merchant = self._get_merchant_name(data)
                warehouse = map_warehouse(merchant) # 3 si es Wholesale, 5 si es E-Comm

                if disposition == "Return to Stock":
                    returns_location_id = 4104 if warehouse == 3 else 4299
                else:
                    returns_location_id = 9 if warehouse == 3 else 4304

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

            return None

        except Exception as e:
            self.logger.error(f"Error adding items to return {return_id}: {e}", exc_info=True)
            event_data = data.get("event_data", {})
            line_items = event_data.get("line_items", [])
            
            return_identifier = None
            if line_items:
                return_identifier = line_items[0].get("tracking_number")

            if not return_identifier:
                completed_at = event_data.get("completed_at", "")
                customer_email = (event_data.get("customer") or {}).get("email", "")
                return_identifier = f"{completed_at}-{customer_email}"

            self._send_error_email(
                method="add_return_items",
                error=e,
                order_reference=return_identifier, # <--- Corregido
                context={"return_id": return_id},
            )
            return None
    
    def reallocate_return_items(self, data):
        merchant_name = self._get_merchant_name(data)
        client_id = map_client(merchant_name) # Si no encuentra devuelve None
        event_data = data.get("event_data")
        line_items = event_data.get("line_items", [])

        # response se inicializa porque solo se asigna dentro del loop: si no hay
        # line_items (o se saltean todos) el `return response` del final tiraba
        # UnboundLocalError.
        response = None
        # Items sin put_away_bin: no se pueden mover a ninguna caja. Se saltean
        # para no bloquear a los demas, y al final se lanza para que salga el mail.
        sin_caja: List[str] = []

        try:
            for item in line_items:
                sku = item.get("sku")
                sku, product_id = self.client.get_product_id(sku, client_id, item.get("barcode"))
                merchant = self._get_merchant_name(data)
                warehouse = map_warehouse(merchant) # 3 si es Wholesale, 5 si es E-Comm
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
                if disposition == "Return to Stock": # Stock en buenas condiciones

                    reallocation_data = {
                        "SourceWarehouseId": warehouse,
                        "SourceNameOrCode": "RET",
                        "DestinationWarehouseId": warehouse,
                        "DestinationNameOrCode": carton_code,
                        "ProductId": product_id,
                        "Quantity": item.get("quantity"),
                        "Comment": "Return reallocation",
                    }

                    if self.client.check_carton(carton_code) == False: # Check si existe la caja
                        print(f'Carton {carton_code} not in Mintsoft - creating Carton...')
                        client_id = map_client(merchant)

                        if warehouse == 3:
                            returns_location_id = 4104 # RET Wholesale
                        else:
                            returns_location_id = 4299 # RET Ecom

                        carton_data = {
                            "WarehouseId": warehouse,
                            "StorageMediaName": "Stock",
                            "Code": carton_code,
                            "LocationId": returns_location_id
                        }

                        self.client.create_carton(carton_data, client_id)

                    response = self.client.transfer_stock(reallocation_data)
                    print(response)

                else: # Stock a mandar a cuarentena

                    if warehouse == 3:
                        temporary_location_id = 9 # RET-QT Wholesale
                    else:
                        temporary_location_id = 4304 # RET-TEMP E-Comm

                    reallocation_data = {
                        "SourceWarehouseId": warehouse,
                        "SourceNameOrCode": "RET-TEMP",
                        "DestinationWarehouseId": warehouse,
                        "DestinationNameOrCode": carton_code,
                        "ProductId": product_id,
                        "Quantity": item.get("quantity"),
                        "Type": "Quarantine",
                        "Comment": "Return reallocation",
                    }

                    quarantine_data = {
                        "ProductID": product_id,
                        "WarehouseId": warehouse,
                        "LocationId": temporary_location_id,
                        "Quantity": item.get("quantity"),
                        "Comment": "Returned stock sent to Quarantine"
                    }

                    if self.client.check_carton(carton_code) == False: # Check si existe la caja
                        print(f'Carton {carton_code} not in Mintsoft - creating Carton...')
                        client_id = map_client(merchant)

                        carton_data = {
                            "WarehouseId": warehouse,
                            "StorageMediaName": "Stock",
                            "Code": carton_code,
                            "LocationId": temporary_location_id
                        }

                        self.client.create_carton(carton_data, client_id)

                    self.client.quarantine_stock(quarantine_data)
                    self.logger.info(f"{sku} from Return set to Quarantine at Location: {item.get("put_away_bin")}")

                    response = self.client.transfer_stock(reallocation_data)
                    print(response)

            if sin_caja:
                raise RuntimeError(
                    f"Items sin put_away_bin, no se les pudo reubicar el stock: "
                    f"{', '.join(sin_caja)}"
                )

            return response

        except Exception as e:
            self.logger.error(f"Error reallocating return items: {e}", exc_info=True)
            event_data = data["event_data"]
            line_items = event_data.get("line_items", [])
            return_identifier = line_items[0].get("tracking_number") # Si hay, es el tracking number

            if return_identifier is None:
                completed_at = event_data.get("completed_at")
                customer_email = event_data["customer"].get("email")
                new_identifier = f"{completed_at}-{customer_email}"
                return_identifier = new_identifier

            self._send_error_email(
                method="reallocate_return_items",
                error=e,
                order_reference=return_identifier,
                context={
                    "merchant_name": merchant_name,
                    "client_id": client_id,
                },
            )
            raise