# Integración de Returns Two Boxes → Mintsoft

Servicio de webhook que recibe eventos `return-complete` de **Two Boxes** y crea el
**return** correspondiente en **Mintsoft**, asignando el stock devuelto a la ubicación
correcta del depósito y moviéndolo a la caja física que usó el operario.

- Lenguaje: Python 3 / Flask
- Deploy: `gunicorn` (`Procfile`, estilo PaaS — Railway / Heroku / Cloud Run)
- Sistemas externos: Two Boxes (webhook entrante), API de Mintsoft, Google Apps Script (registro de payloads y errores), SMTP (mails de alerta)

---

## Índice

- [Arquitectura](#arquitectura)
- [Flujo del request](#flujo-del-request)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Variables de entorno](#variables-de-entorno)
- [Correr localmente](#correr-localmente)
- [Contrato del webhook](#contrato-del-webhook)
- [Reglas de negocio](#reglas-de-negocio)
  - [Mapeo Merchant → Client / Warehouse](#mapeo-merchant--client--warehouse)
  - [Return interno vs externo](#return-interno-vs-externo)
  - [Disposition → return reason → ubicación](#disposition--return-reason--ubicación)
  - [Manejo de cajas (put-away bin)](#manejo-de-cajas-put-away-bin)
  - [Fallback Barcode → SKU y la limitación de `SearchBarcode`](#fallback-barcode--sku-y-la-limitación-de-searchbarcode)
- [Endpoints de la API de Mintsoft utilizados](#endpoints-de-la-api-de-mintsoft-utilizados)
- [Manejo de errores](#manejo-de-errores)
- [Logging](#logging)
- [Datos de referencia (`models/`)](#datos-de-referencia-models)
- [Notas operativas y comportamientos conocidos](#notas-operativas-y-comportamientos-conocidos)

---

## Arquitectura

```
                  ┌──────────────────────────────────────────────┐
  Two Boxes  ───► │  POST /webhook   (listener.py, Flask)        │
  return-complete │  auth: header x-two-boxes-authorization      │
                  └───────────────┬──────────────────────────────┘
                                  │ responde 200 inmediatamente
                    ┌─────────────┴──────────────┐
                    ▼                            ▼
      ThreadPoolExecutor (10)         ThreadPoolExecutor (10)
      enviar_a_google_async           procesar_webhook
              │                                │
              ▼                                ▼
      Google Apps Script          MintsoftReturnService
        (GAS_URL — archivo        (services/mintsoft_service.py)
         del payload crudo)                    │
                                    ┌──────────┴───────────┐
                                    ▼                      ▼
                          MintsoftOrderClient       canales de error
                          (clients/…Client.py)      ├─ ERRORES_URL (GAS)
                                    │               └─ mail de alerta SMTP
                                    ▼
                            api.mintsoft.co.uk
```

El endpoint es **fire-and-forget**: valida el secreto compartido, entrega el payload a dos
threads en segundo plano y responde `200` con body vacío. Por lo tanto Two Boxes nunca ve
los fallos de procesamiento — esos se reportan por fuera del request (Google Sheet + mail),
ver [Manejo de errores](#manejo-de-errores).

---

## Flujo del request

`procesar_webhook(data)` en `listener.py:41`:

1. **`create_return(data)`** → devuelve una tupla `(return_id, status)` donde `status` es uno de
   `"Internal Return Created"`, `"External Return Created"`, `"No Return Created"`.
2. Si es **External**:
   - `allocate_external_return_items(data, return_id)` — asigna cada item del return a la
     ubicación de staging `RET` / `RET-TEMP`.
   - `reallocate_return_items(data)` — transfiere el stock desde la ubicación de staging a la
     caja del operario (`put_away_bin`), poniéndolo antes en cuarentena cuando corresponde.
3. Si es **Internal**:
   - `add_return_items(return_id, data)` — agrega los items, asigna ubicaciones y **confirma** el return.
   - `reallocate_return_items(data)` — mismo paso de transferencia/cuarentena que arriba.
4. Si es **`No Return Created`**, no pasa nada más; el error ya fue reportado.

> Nota: solo el flujo interno llama a `confirm_return`. Los returns externos se crean ya
> confirmados por `CreateExternalReturn`.

---

## Estructura del repositorio

```
listener.py                       App Flask, auth del webhook, dispatch de threads
Procfile                          Entrypoint de gunicorn
requirements.txt

clients/
  mintsoftClient.py               MintsoftOrderClient — wrapper HTTP sobre la API de Mintsoft
services/
  mintsoft_service.py             MintsoftReturnService — toda la lógica de negocio
mappers/
  mintsoft_mapper.py              Tabla nombre de merchant → ClientId / WarehouseId de Mintsoft
  error_mapper.py                 ERROR_CODES (E-01 … E-07)
  main_mapper.py                  Helper legacy/sin uso (map_return)
  return_reason_mapper.py         Placeholder vacío
loggers/
  main_logger.py                  Factory de logger con archivo rotativo + stream
models/                           Payloads de ejemplo capturados y datos de referencia de Mintsoft (ver abajo)
logs/                             Salida rotativa de logs (m_service.log)
```

---

## Variables de entorno

| Variable | Requerida | Usada por | Propósito |
|---|---|---|---|
| `WEBHOOK_SECRET` | ✅ | `listener.py` | Valor esperado del header `x-two-boxes-authorization`. Si no coincide → `401`. |
| `MINTSOFT_USERNAME` | ✅ | `clients/mintsoftClient.py` | Usuario para `POST /api/Auth` de Mintsoft. Si falta → `RuntimeError` al importar. |
| `MINTSOFT_PASSWORD` | ✅ | `clients/mintsoftClient.py` | Contraseña de Mintsoft. |
| `GAS_URL` | ✅ | `listener.py` | Endpoint de Google Apps Script que archiva cada payload crudo del webhook. |
| `ERRORES_URL` | ✅ | `services/mintsoft_service.py` | Endpoint de Google Apps Script que recibe las filas de error por item. |
| `PORT` | – | `listener.py` | Puerto del servidor de desarrollo / gunicorn. Default `8080`. |
| `SMTP_HOST` | – | service | Servidor SMTP para los mails de alerta. Si falta alguna variable SMTP, las alertas se saltean con un warning. |
| `SMTP_PORT` | – | service | Default `587`. |
| `SMTP_USER` | – | service | Login SMTP; también el `From` por defecto. |
| `SMTP_PASSWORD` | – | service | Contraseña SMTP. |
| `ALERT_EMAIL_FROM` | – | service | Sobreescribe la dirección `From`. |
| `LOG_DIR` | – | `loggers/main_logger.py` | Default `logs`. |
| `LOG_LEVEL` | – | logger | Default `INFO`. |
| `LOG_MAX_BYTES` | – | logger | Default `10485760` (10 MB). |
| `LOG_BACKUP_COUNT` | – | logger | Default `5`. |

`clients/mintsoftClient.py` llama a `load_dotenv()`, así que un archivo `.env` local funciona.

> ⚠️ `ALERT_EMAIL_TO` aparece documentada en un mensaje de warning pero **no** se lee del
> entorno — la lista de destinatarios está hardcodeada en `MintsoftReturnService.__init__`.
> Ver [comportamientos conocidos](#notas-operativas-y-comportamientos-conocidos).

---

## Correr localmente

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cat > .env <<'ENV'
WEBHOOK_SECRET=...
MINTSOFT_USERNAME=...
MINTSOFT_PASSWORD=...
GAS_URL=https://script.google.com/macros/s/.../exec
ERRORES_URL=https://script.google.com/macros/s/.../exec
ENV

python listener.py            # servidor de desarrollo en :8080
# o, como en producción:
gunicorn listener:app --bind 0.0.0.0:8080
```

Reproducir un payload capturado:

```bash
python3 - <<'PY'
import json, requests, os
payload = json.load(open("models/tb_work_capture_model.json"))[0]
r = requests.post("http://localhost:8080/webhook", json=payload,
                  headers={"x-two-boxes-authorization": os.environ["WEBHOOK_SECRET"]})
print(r.status_code)
PY
```

Como el procesamiento es asincrónico, hay que mirar `logs/m_service.log` (y stdout) para ver
el resultado.

---

## Contrato del webhook

### `POST /webhook`

| | |
|---|---|
| Header | `x-two-boxes-authorization: <WEBHOOK_SECRET>` |
| Body | Evento `return-complete` de Two Boxes (objeto JSON) |
| `200` | Aceptado (body vacío). **No significa que Mintsoft haya funcionado.** |
| `400` | Body ausente o JSON no parseable |
| `401` | Header de auth ausente o incorrecto |

### Campos del payload que realmente se consumen

Se soportan dos formas de payload. Los ejemplos completos están en `models/`
(`tb_rma_model.json`, `tb_work_capture_model.json`, `tb_return_to_sender_model.json`).

| Path | Usado para |
|---|---|
| `event_data.merchant_integration.merchant.name` | Nombre del merchant (fuente preferida) |
| `event_data.line_items[0].merchant.name` | Fallback del nombre del merchant (payloads de Work Capture) |
| `event_data.line_items[0].storefront_order_number` | Se compara contra el `OrderNumber` de Mintsoft |
| `event_data.line_items[0].tracking_number` | Referencia principal del return / `Reference` (truncado a 50 caracteres) |
| `event_data.completed_at` + `event_data.customer.email` | Referencia de fallback `"{completed_at}-{email}"` cuando no hay tracking number |
| `line_items[].sku` | Búsqueda del producto en Mintsoft |
| `line_items[].barcode` | `EAN` al crear el producto; fallback barcode→SKU |
| `line_items[].product_variant.barcode` | Fallback del barcode — en los payloads de RMA `line_items[].barcode` viene `null` y el barcode real está acá (`_get_item_barcode`) |
| `line_items[].product_variant.name` | `Name` del producto cuando se crea uno inexistente |
| `line_items[].quantity` | Cantidad del item del return |
| `line_items[].disposition` | Determina return reason, ubicación y cuarentena (ver abajo) |
| `line_items[].put_away_bin` | Código de la caja de destino |
| `line_items[].photo_urls` | Se envía como `ReturnPhotos` (solo returns internos) |
| `line_items[].graded_attributes[0].merchant_grading_attribute.grading_attribute.title` | Se copia en el campo `Comments` del item (solo returns internos) |

---

## Reglas de negocio

### Mapeo Merchant → Client / Warehouse

`mappers/mintsoft_mapper.py` contiene una única tabla. Cada fila mapea un **nombre de merchant
de Two Boxes** (`tb_name`) a un **client id de Mintsoft** (`m_id`) y un **warehouse id**
(`warehouse_id`):

```python
{ "m_name": "Third Form", "m_id": 22, "tb_name": "third form ecommerce",   "warehouse_id": 5 },
{ "m_name": "Third Form", "m_id": 22, "tb_name": "third form - wholesale", "warehouse_id": 3 },
```

- La búsqueda es por **coincidencia exacta, insensible a mayúsculas**, sobre `tb_name`
  (`map_client`, `map_warehouse`).
- El mismo cliente de Mintsoft puede aparecer dos veces — una por canal de venta — y así es
  como un merchant se rutea a Wholesale o a E-Commerce.
- Warehouse ids: **`3` = General / Wholesale**, **`5` = E-Commerce**.
- Las filas comentadas son merchants desactivados a propósito.
- Un merchant no mapeado → error **E-07**, no se crea ningún return. Agregar un merchant
  significa agregar una fila acá.

### Return interno vs externo

```
storefront_order_number se busca con GET /api/Order/Search  (find_order_id)
   │
   ├── encontrada, OrderStatusId ∈ {4, 5, 6}
   │        └──►  INTERNO:  POST /api/Return/CreateReturn/{orderId}?WarehouseId=&Reference=
   │                        luego AddItem → AllocateItemLocation → Confirm
   │
   └── no encontrada ──►  EXTERNO:  POST /api/Return/CreateExternalReturn
                                    (los items van en la misma llamada de creación)
                                    luego AllocateItemLocation
```

`find_order_id` prueba dos términos de búsqueda: primero el valor **crudo** del payload
(`US#12901`), que entra por `ExternalOrderReference` — Mintsoft guarda ahí el número tal como
vino de la tienda —, y si no hay match reintenta sin el `#` (`US12901`, que es el `OrderNumber`).
Se filtra por `ClientId` a mano, porque `Order/Search` no lo acepta como parámetro, y se
compara contra `OrderNumber` **y** `ExternalOrderReference`.

Los status ids `4, 5, 6` (`DESPATCHED`, `INVOICED`, `INVOICEFAILED`) son los estados en los que
la orden ya salió del depósito, es decir las únicas que plausiblemente podrían devolverse. Si la
orden existe pero está en otro estado, se loguea el estado y **no** se crea un return interno.

Si `Order/Search` falla contra la API, la excepción se propaga en vez de devolver "no
encontrada": asumir que la orden no existe crearía un return externo de más.

> **Antes** esto listaba todas las órdenes del cliente con `GET /api/Order/List` una vez por cada
> status y buscaba el número a mano sobre el resultado. Fallaba por dos motivos a la vez: el
> parámetro de status es `OrderStatusId` y se enviaba `statusId`, así que el filtro se ignoraba y
> las tres llamadas devolvían las mismas 100 filas; y sin `PageNo`/`Limit` solo se veían las 100
> órdenes más recientes. Sumado a que `_normalize_order_number` hacía `lstrip("#")` — que solo
> saca el numeral inicial, no el del medio de `US#12901` — la orden **nunca** se encontraba: sobre
> 1500 returns de la cuenta, 1474 estaban creados como externos y 26 como internos.

Para returns externos, cuando un SKU no existe para ese cliente, el producto se **crea** al
vuelo (`PUT /api/Product`) usando SKU, nombre de la variante y barcode, con sleeps de 3
segundos alrededor de la llamada para darle tiempo a Mintsoft a indexarlo.

### Disposition → return reason → ubicación

`disposition` es el resultado del grading que hace el operario en Two Boxes.

| `disposition` | `ReturnReasonId` de Mintsoft | Significado | Ubicación de staging |
|---|---|---|---|
| `Return to Stock` | `1` — *Unwanted - Good Stock* (`DoNothing`) | revendible | `RET` |
| `Missing` | — | el item nunca llegó → **se saltea por completo** | — |
| cualquier otro valor | `2` — *Faulty or Damaged - Quarantine Stock* (`Quarantine`) | dañado | `RET-TEMP` |

Los ids de ubicación están hardcodeados por depósito:

| Propósito | Wholesale (wh 3) | E-Commerce (wh 5) |
|---|---|---|
| `RET` — staging de stock bueno, y destino de **todas** las cajas | `4104` | `4299` |
| `RET-TEMP` — staging de cuarentena | `9` | `4304` |

**La cuarentena la aplica Mintsoft solo, al confirmar el return.** El motivo
`ReturnReasonId=2` tiene `StockAction='Quarantine'` (ver `GET /api/Return/Reasons`), y el
`Confirm` lo ejecuta: la unidad queda en `RET-TEMP` con `Type='Quarantine'` y suma a
`InQuarantine`. El código **no** pide la cuarentena.

> **Antes** se llamaba además a `POST /api/Warehouse/StockMovement?Action=7`. Esa llamada
> fallaba **siempre** con `"Unable to Quarantine stock as not enough could be found in the
> selected location!"`, porque para cuando corre ya no queda stock sin cuarentenar en esa
> ubicación — la unidad ya está cuarentenada. Por eso el comment
> `'Returned stock sent to Quarantine'` no aparece en ningún movimiento histórico de la cuenta:
> nunca funcionó. `quarantine_stock` sigue en el cliente, documentada, para cuarentenar stock a
> mano fuera de un return.

La transferencia a la caja sí es necesaria —el `Confirm` deja la unidad **suelta**, con
`Carton=None`— y lleva `"Type": "Quarantine"`, que es lo que le permite mover stock ya
cuarentenado conservando el estado.

### Manejo de cajas (put-away bin)

`reallocate_return_items` mueve el stock desde la ubicación de staging a la caja física que
escaneó el operario (`put_away_bin`):

1. Si el item no trae `put_away_bin`, se **saltea** con un warning: sin código de caja no hay
   destino para el transfer, y el stock queda en la ubicación de staging. Los payloads de RMA
   pueden traer `put_away_bin: null` (ver `models/tb_rma_model.json`).
2. `GET /api/StorageMedia/ValidateCarton?cartonCode=…` — ver [`check_carton`](#check_carton-y-la-detección-de-cajas-existentes) abajo.
3. Si no existe → `POST /api/StorageMedia/CreateCarton` (`StorageMediaName: "Stock"`,
   `autoGenerateSSCC=false`) ubicada **siempre en `RET`**, también para los items en
   cuarentena.
4. `PUT /api/Warehouse/TransferStock` desde `RET` / `RET-TEMP` → código de la caja. El destino
   es el código de caja, así que Mintsoft arrastra la unidad a la ubicación **de la caja**: un
   item que salió de `RET-TEMP` termina en `RET`, conservando `Type='Quarantine'`.

> **La caja de cuarentena se crea en `RET`, no en `RET-TEMP`**, por dos razones. Primero,
> `RET-TEMP` es la ubicación transitoria de aislamiento, no un destino. Segundo, si la caja vive
> en `RET-TEMP` —la misma ubicación a la que el `Confirm` asigna el item— y ya contiene ese SKU,
> Mintsoft consolida la unidad nueva dentro de la caja y no queda nada suelto; después el
> `TransferStock` falla con `"Could not find any of product ID: X in RET-TEMP!"`. Con la caja en
> `RET` eso no puede pasar.
>
> Un mismo SKU puede acumularse legítimamente en una caja desde returns distintos (uno el lunes,
> otro hoy), así que el transfer **siempre** se ejecuta: no se saltea por "ese SKU ya está en la
> caja". Verificado: dos returns del mismo SKU a la misma caja dejan `Qty=2` e `InQuarantine=2`.

#### `check_carton` y la detección de cajas existentes

`ValidateCarton` no devuelve un "existe / no existe" limpio. Devuelve un `ToolkitResult`, y en
esta cuenta **devuelve `Success: false` para todas las cajas** — incluso para las que existen y
tienen stock. Medido:

| `cartonCode` | `Success` | `Message` | `check_carton` |
|---|---|---|---|
| una caja existente (`RV-RETURNS-169`, `*RV-2505-1`, …) | `false` | `"The retrieved Carton does not have a valid prefix and code! …"` | `True` ✅ |
| un código inexistente | `false` | `"Could not find a Carton with the code …"` | `False` ✅ |
| `""` | `false` | `"Carton code was not provided."` | `ValueError` |

El mensaje de las cajas existentes dice *"The **retrieved** Carton…"*: Mintsoft **sí** la
encuentra y después la rechaza por no tener un SSCC válido, probablemente porque `create_carton`
las crea con `autoGenerateSSCC=false`. Y ese rechazo no impide nada — `TransferStock` funciona
igual contra ellas.

Por eso `check_carton` mira **únicamente** si Mintsoft dice explícitamente que no la encontró:

```python
body = self._toolkit_result(response, ...)          # raise_for_status + body no-JSON
message = body.get("Message") or ""
return not message.startswith(self.CARTON_NOT_FOUND_PREFIX)
```

⚠️ **No cambiar esto por `return body.get("Success")`.** Daría `False` para toda caja y el código
recrearía cajas existentes en cada return.

`_toolkit_result` sí corta con excepción ante un no-2xx o un body que no es JSON, y un
`put_away_bin` vacío o `None` levanta `ValueError` — aunque el service saltea esos items antes de
llegar acá.

El `cartonCode` se manda por `params=` y no interpolado en la URL: los códigos escaneados pueden
traer `#`, `&`, `%` o espacios, que romperían el querystring.

### Fallback Barcode → SKU y la limitación de `SearchBarcode`

Two Boxes a veces manda un `sku` que en Mintsoft no existe con ese valor exacto (el SKU real
está cargado distinto, o el operario escaneó una variante). Para esos casos
`MintsoftOrderClient.get_product_id` (`clients/mintsoftClient.py:252`) tiene un fallback: si la
búsqueda por SKU no encontró producto, reintenta resolviendo el **barcode** a un SKU vía
`GET /api/Product/SearchBarcode`, y si eso devuelve algo usa ese SKU para volver a buscar el
producto.

El barcode se saca con `_get_item_barcode` (`services/mintsoft_service.py`), que mira
`line_items[].barcode` y, si viene `null`, `line_items[].product_variant.barcode`: **en los
payloads de RMA el barcode de nivel item viene siempre `null`** y el real está en el
`product_variant` (ver `models/tb_rma_model.json`).

```python
if product_id == None and len(barcode) > 7:
    sku_rety = self.get_sku_dado_barcode(barcode)
    if sku_rety == "null":
        return sku, None
```

**Por qué el `len(barcode) > 7`.** Es una limitación del endpoint `SearchBarcode` de Mintsoft:
con barcodes cortos (7 caracteres o menos) el endpoint no se comporta como una búsqueda exacta
y devuelve matches por prefijo/parcial, es decir el SKU de **otro** producto. Un SKU equivocado
es peor que ningún SKU: el return se crearía contra un producto real distinto y el stock
devuelto quedaría sumado en el lugar equivocado, sin ningún error visible. Por eso el guard
corta antes de llamar al endpoint y el fallback solo se intenta cuando el barcode es lo bastante
largo para que la búsqueda sea confiable (EAN-13, UPC-A, etc.).

**Qué pasa con los casos que caen acá.** Si el barcode tiene 7 caracteres o menos, `get_product_id`
devuelve `(sku, None)` sin intentar el fallback, y el flujo sigue con `product_id = None`:

- **Return externo** (`create_return`, `services/mintsoft_service.py:247`): se crea un producto
  nuevo al vuelo con `PUT /api/Product`, así que en Mintsoft aparece un producto duplicado con el
  SKU que mandó Two Boxes.
- **Return interno** (`add_return_items`): el item se agrega con `ProductId: None`.

En ninguno de los dos casos el proceso se detiene ni se reporta un error de SKU específico
(no hay un código tipo `SKU_NOT_RESOLVABLE`). **Estos casos se corrigen a mano en Mintsoft**: nos
llegan por mail (aviso del merchant / del equipo de depósito), se identifica el SKU real y se
arregla el return y el stock manualmente. Es un volumen bajo y asumido — la alternativa sería
mandar el barcode corto a `SearchBarcode` y arriesgarse a imputar el return al producto
equivocado, que es un error mucho más difícil de detectar después.

> El `barcode` se normaliza a string antes de medirlo. Cuando venía `None`, `len(barcode)`
> lanzaba `TypeError: object of type 'NoneType' has no len()`; en `add_return_items` esa
> excepción se capturaba y **el item se caía del return en silencio** (return corto o vacío), y
> en `create_return` hacía fallar el return completo.

---

## Endpoints de la API de Mintsoft utilizados

URL base `https://api.mintsoft.co.uk`. Auth: `POST /api/Auth` al construir el cliente; la
API key devuelta se envía en el header `ms-apikey` en cada llamada. Todos los timeouts son de 120 s.

| Método | Endpoint | Método del cliente |
|---|---|---|
| `POST` | `/api/Auth` | `_authenticate` |
| `GET` | `/api/Order/Search?OrderNumber=&exactMatch=true` | `search_orders` |
| `GET` | `/api/Order/List?ClientId=&OrderStatusId=&PageNo=&Limit=` | `get_orders` *(sin uso en el flujo)* |
| `POST` | `/api/Return/CreateReturn/{orderId}?WarehouseId=&Reference=` | `create_return` |
| `POST` | `/api/Return/CreateExternalReturn` | `create_external_return` |
| `GET` | `/api/Return/{id}` | `get_return_details` |
| `POST` | `/api/Return/{id}/AddItem` | `add_return_item` |
| `POST` | `/api/Return/{id}/AllocateItemLocation?ReturnitemId=&Quantity=&LocationId=` | `allocate_return_item_location` |
| `POST` | `/api/Return/{id}/Confirm` | `confirm_return` |
| `GET` | `/api/Return/Reasons` | `get_return_reasons` |
| `GET` | `/api/Product/Search?Search={sku}` | `get_product_id` |
| `GET` | `/api/Product/SearchBarcode?Barcode=` | `get_sku_dado_barcode` |
| `PUT` | `/api/Product` | `create_product` |
| `PUT` | `/api/Warehouse/TransferStock` | `transfer_stock` |
| `POST` | `/api/Warehouse/StockMovement?Action=7` | `quarantine_stock` *(sin uso en el flujo — ver [Disposition](#disposition--return-reason--ubicación))* |
| `GET` | `/api/Warehouse/{id}/Location/All` | `get_warehouse_locations` *(dump de referencia)* |
| `GET` | `/api/StorageMedia/ValidateCarton?cartonCode=` | `check_carton` |
| `POST` | `/api/StorageMedia/CreateCarton?autoGenerateSSCC=false&clientId=` | `create_carton` |
| `GET` | `/api/Reports/ProductsInLocationReport` | `get_products_in_locations` *(dump de referencia)* |
| `GET` | `/api/RefData/Currencies` | `get_currencies` *(dump de referencia)* |

`get_product_id` exige coincidencia **exacta** de SKU *y* que coincida el `ClientId` — la
búsqueda de Mintsoft es difusa, así que un match por substring de otro cliente se ignora
deliberadamente.

`SearchBarcode` solo se llama con barcodes de **más de 7 caracteres**: con barcodes cortos el
endpoint matchea parcialmente y devuelve el SKU de otro producto. Ver
[Fallback Barcode → SKU](#fallback-barcode--sku-y-la-limitación-de-searchbarcode).

Los helpers `get_warehouse_locations` / `get_products_in_locations` / `get_currencies` escriben
la respuesta en un archivo JSON en el CWD del proceso; son herramientas de desarrollo para
refrescar `models/`, no forman parte del flujo del request.

---

## Manejo de errores

Dos canales independientes, ambos best-effort y no fatales.

### 1. Códigos de error → Google Sheet

`mappers/error_mapper.py`:

| Código | Clave | Cuándo se genera |
|---|---|---|
| `E-01` | `FETCH_ORDERS_FAILED` | falló `/api/Order/List` |
| `E-02` | `SKU_NOT_RESOLVABLE` | no se pudo resolver el barcode a un SKU |
| `E-03` | `CREATE_RETURN_FAILED` | falla genérica al crear el return |
| `E-04` | `ALLOCATE_ITEMS_FAILED` | falló la asignación de items en un return **externo** |
| `E-05` | `ADD_ITEMS_FAILED` | falló el agregado de items a un return **interno** |
| `E-06` | `REALLOCATE_ITEMS_FAILED` | falló el paso de transferencia/cuarentena |
| `E-07` | `CLIENT_NOT_MAPPED` | merchant ausente en `mappers/mintsoft_mapper.py` |

`_log_failed_items` postea **una fila por line item fallido** a `ERRORES_URL`: una copia del
payload original con `event_data.line_items` reducido a ese único item, más `error_code` y
`error_description`. Esto hace que cada fila se pueda reprocesar individualmente.

### 2. Mail de alerta

`_send_error_email` arma un mensaje con asunto
`[MintsoftReturnService] {code} - {description} | POReference: {reference}` y un cuerpo que
contiene la referencia, el timestamp UTC, el host, el tipo de excepción, el contexto en JSON y
el traceback completo. Nunca lanza excepciones — los problemas de SMTP se loguean y se
absorben.

> ⚠️ **El envío está actualmente desactivado**: las líneas `server.login(...)` y
> `server.send_message(...)` están comentadas (marcadas con `CAMBIAR`), así que la conexión SMTP
> se abre y se cierra sin entregar nada, y el log igual reporta "Error alert email sent".

### Referencia del return usada en los reportes

`_return_identifier(data)` es el único lugar donde se calcula, con esta prioridad:

1. `line_items[0].tracking_number`
2. si viene vacío (`null`, `""` o solo espacios), `storefront_order_number`
3. como último recurso, `"{completed_at}-{customer.email}"` — para que el `POREference` del mail
   de error nunca quede en `UNKNOWN`

Ese valor va como `Reference` de Mintsoft (truncado a 50 caracteres) en **las dos** ramas,
interna y externa, y es el mismo con el que se identifica el return en los reportes de error.

> **Antes** solo la rama externa seteaba `Reference`; la interna no. No se notaba porque casi
> todos los returns salían externos, pero al arreglar la búsqueda de órdenes los internos
> quedaban sin `Reference` y no se podían encontrar por PO reference en Mintsoft.

### Política de reintentos

Se usa un `requests.Session` con `Retry` de `urllib3` (`total=5`, `backoff_factor=0.3`,
`status_forcelist=[502, 503, 504]`, `raise_on_status=False`) **únicamente** para las llamadas a
Google Apps Script (Google devuelve 5xx con frecuencia cuando está saturado). Las llamadas a
Mintsoft son de un solo intento.

---

## Logging

`loggers/main_logger.py` construye un logger con un `RotatingFileHandler`
(`logs/m_service.log`, 10 MB × 5 backups) y un stream handler, con el formato:

```
%(asctime)s | %(levelname)s | %(name)s | %(message)s
```

El logger del servicio se llama `mintsoft_service`. Tener en cuenta que buena parte del
diagnóstico también sale por `print()` sueltos, tanto en el cliente como en el listener, así
que el stdout del contenedor es la vista más completa.

---

## Datos de referencia (`models/`)

Fixtures capturados, útiles para reproducir casos y para buscar los ids hardcodeados.

| Archivo | Contenido |
|---|---|
| `tb_rma_model.json` | Payload completo `return-complete` de RMA de Two Boxes (con `merchant_integration`) |
| `tb_work_capture_model.json` | Payload de Work Capture — más reducido, el merchant solo aparece en `line_items[].merchant` |
| `tb_return_to_sender_model.json` | Payload de return-to-sender |
| `mintsoft_orders_model.json` | Respuesta de ejemplo de `/api/Order/List` |
| `mintsoft_order_status_model.json` | Ids de estado de orden (origen del filtro `[4, 5, 6]`) |
| `mintsoft_return_reasons_model.json` | Ids de return reason (origen de las razones `1` y `2`) |
| `mintsoft_warehouse_locations_model.json` | Todas las ubicaciones de depósito — de acá salen `4104` / `4299` / `9` / `4304` |
| `mintsoft_products_in_locations_model.json` | Ejemplo del reporte de productos por ubicación |

---

## Notas operativas y comportamientos conocidos

Documentados tal cual están; cada punto es un comportamiento real del código actual.

1. **El webhook siempre responde `200`.** Las fallas solo se ven en la planilla de errores, en
   el mail de alerta (hoy desactivado) y en los logs. Two Boxes nunca va a reintentar.
2. **Los mails de alerta sí se envían.** Ya no hay nada comentado en `_send_error_email`. Como
   `map_client` se invoca en varios puntos del flujo, un mismo payload malo puede generar varias
   alertas idénticas; no hay deduplicación.
3. **`alert_email_to` tiene una coma final**, lo que la convierte en una tupla en lugar de un
   string, y está hardcodeada en vez de leerse de `ALERT_EMAIL_TO`.
4. **Los returns internos ya respetan el warehouse mapeado.** `create_return` pasa
   `map_warehouse(merchant)` en vez de un `3` fijo, y el cliente lo envía como query param
   `WarehouseId`. Antes recibía el argumento y no lo mandaba, así que Mintsoft usaba el warehouse
   de la orden mientras el log decía otra cosa.
5. **La ubicación `9` es `RET-TEMP` en wholesale**, no `RET-QT`. El comentario que decía
   `RET-QT Wholesale` estaba mal: `RET-QT` es otra ubicación (id `2363`, `LocationTypeId=5` =
   `GOODS IN`, solo en wh 3) que el código no usa. Verificado por API.
6. **Mintsoft no tiene un tipo de ubicación de cuarentena.** Los tipos son `PICK`, `ALLOCATE`,
   `BINS`, `NO PICK`, `BULK`, `Wholesale`, `REPLEN`, `OFFHAND`, `GOODS IN`, `REPLEN TROLLEY`,
   `PICKING TOTE`, `CROSSDOCK`, `PACKING`. La cuarentena es un **estado del stock**
   (`Type='Quarantine'` / `InQuarantine`), y convive con ubicación y caja: una unidad puede estar
   en cuarentena dentro de una caja en `RET`.
7. **`get_product_id` se llama repetidamente** para el mismo SKU en `create_return`,
   `add_return_items` (dos veces) y `reallocate_return_items` — sin ningún cacheo.
8. **`get_product_id` arma una URL con doble slash** (`…co.uk//api/Product/Search`), que
   Mintsoft acepta igual.
9. **`reallocate_return_items` acumula las respuestas en una lista** y la devuelve. Antes
   devolvía la variable `response` del último item del loop, así que lanzaba
   `UnboundLocalError` cuando no se procesaba ningún item (`line_items` vacío, o todos salteados
   por no traer `put_away_bin`). Sigue re-lanzando las excepciones, mientras que
   `add_return_items` las absorbe — o sea que un fallo en un item **aborta la reasignación de los
   items siguientes** y ese stock queda en `RET` / `RET-TEMP`.
10. **`allocate_external_return_items` sobreescribe el parámetro `data`** dentro de su loop; el
    manejador de errores se protege de eso con un chequeo de `isinstance` / `"event_data" in data`.
11. **Los manejadores de error ya no recalculan el identificador a mano.** Los cuatro usan
    `_return_identifier(data)`, que es defensivo. Antes hacían `line_items[0].get(...)` y
    `event_data["customer"]` dentro del propio `except`, así que con una lista vacía o un
    `customer` ausente **lanzaban dentro del handler y tapaban el error real** — que es
    exactamente cómo se perdió la causa de una falla de producción.
12. **Dos `sleep` de 3 segundos** rodean la creación de productos al vuelo, ocupando un thread
    del pool de 10 slots por más de 6 segundos por cada SKU nuevo.
13. **`mappers/main_mapper.py` (`map_return`) no se usa** en el flujo del request, y
    `mappers/return_reason_mapper.py` está vacío.
14. **Los ids de ubicación, de warehouse y de return reason están hardcodeados.** Si se
    renombran o recrean ubicaciones en Mintsoft, hay que editar `services/mintsoft_service.py`.
15. **Los items con barcode de 7 caracteres o menos y SKU no encontrado se arreglan a mano.** El
    fallback `SearchBarcode` está deshabilitado a propósito para esos casos por una limitación del
    endpoint de Mintsoft (matchea parcial y devuelve el producto equivocado). El return se crea
    igual — con un producto duplicado si es externo, o con `ProductId: None` si es interno — y la
    corrección se hace manualmente en Mintsoft a partir del aviso que nos llega por mail. Ver
    [Fallback Barcode → SKU](#fallback-barcode--sku-y-la-limitación-de-searchbarcode).
16. **`add_return_items` confirma el return incluso si se cayeron items.** Un item se cae si no
    tiene SKU o si falla la búsqueda del producto. Antes eso era un `continue` silencioso: el
    return quedaba con menos unidades de las que devolvió el cliente — o **vacío**, si se caían
    todas. Ahora los items caídos se juntan y, al final, se loguea un error y se manda un mail de
    alerta con el detalle (`items_agregados` / `items_en_el_payload` / `items_caidos`). El return
    igual queda confirmado y corto: la corrección es manual.
17. **No hay idempotencia.** `create_return` nunca chequea si ya existe un return en Mintsoft para
    esa orden o ese tracking number. Si Two Boxes manda dos eventos `return-complete` para el
    mismo return físico — o reintenta una entrega — se crean **dos returns separados** en Mintsoft.
    Como los payloads de RMA traen varios `line_items` con el mismo `tracking_number` y los de
    Work Capture traen uno solo, dos eventos para el mismo return físico es un escenario real.

18. **La `ms-apikey` vence a las 24 horas y no se renueva.** El spec de `POST /api/Auth` lo dice
    explícitamente. `MintsoftOrderClient` la pide una sola vez en el constructor y `listener.py`
    instancia el service a nivel de módulo, así que cualquier worker con más de un día de uptime
    empieza a devolver `401` en todas las llamadas hasta que se redespliegue. **Pendiente.**
19. **`create_product` no envía `Weight`**, que el schema `Product` marca como requerido junto
    con `SKU`. Se mandan solo SKU, Name, EAN y ClientId, así que crear un SKU al vuelo durante un
    return externo puede volver con `Success: false`. **Pendiente.**
20. **El merchant se busca en tres lugares.** `event_data['merchant_integration']['merchant']`
    (que no existe en estos payloads), `event_data['line_items'][0]['merchant']` y
    `event_data['merchant']`. Antes solo se miraban los dos primeros, así que cualquier evento
    sin `line_items` devolvía `""` y disparaba la alerta de "cliente no mapeado" con el nombre
    vacío, aunque el merchant estuviera en el payload al lado. `map_client` ahora distingue los
    dos casos: con nombre vacío manda "Payload sin merchant".
21. **`listener.py` no mira `event_type`.** Todo `POST` a `/webhook` va a `procesar_webhook`, sea
    `return-complete` o cualquier otro tipo que Two Boxes mande, y los otros tipos pueden tener
    otra forma de payload.

### Agregar un merchant

1. Agregar una fila a `clients` en `mappers/mintsoft_mapper.py` con el nombre exacto del
   merchant de Two Boxes en `tb_name` (en minúsculas), el client id de Mintsoft en `m_id`, y
   `warehouse_id` `3` (Wholesale) o `5` (E-Commerce).
2. Si el merchant vende por los dos canales, agregar **dos** filas con `tb_name` distintos.
3. Verificar que las ubicaciones `RET` / `RET-TEMP` del merchant coincidan con los ids
   hardcodeados para ese depósito; si no, hay que parametrizar las constantes de ubicación.
