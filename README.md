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
| `line_items[].barcode` | `EAN` al crear el producto; recuperación barcode→SKU |
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
storefront_order_number se compara contra las órdenes de Mintsoft
con StatusId ∈ {4, 5, 6}  (fetch_mintsoft_orders → match_rma_order)
   │
   ├── coincidencia ──►  INTERNO:  POST /api/Return/CreateReturn/{orderId}
   │                               luego AddItem → AllocateItemLocation → Confirm
   │
   └── sin coincidencia ─►  EXTERNO:  POST /api/Return/CreateExternalReturn
                                      (los items van en la misma llamada de creación)
                                      luego AllocateItemLocation
```

Los status ids `4, 5, 6` provienen de `models/mintsoft_order_status_model.json` — los estados
de despachado / completado, es decir solo las órdenes que plausiblemente podrían devolverse.

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
| `RET` — staging de stock bueno | `4104` | `4299` |
| `RET-TEMP` — staging de cuarentena | `9` | `4304` |

El stock dañado además pasa por
`POST /api/Warehouse/StockMovement?Action=7` (cuarentena) antes de la transferencia, y la
transferencia misma lleva `"Type": "Quarantine"`.

### Manejo de cajas (put-away bin)

`reallocate_return_items` mueve el stock desde la ubicación de staging a la caja física que
escaneó el operario (`put_away_bin`):

1. Si el item no trae `put_away_bin`, se **saltea** con un warning: sin código de caja no hay
   destino para el transfer, y el stock queda en la ubicación de staging. Los payloads de RMA
   pueden traer `put_away_bin: null` (ver `models/tb_rma_model.json`).
2. `GET /api/StorageMedia/ValidateCarton?cartonCode=…` — ver [`check_carton`](#check_carton-y-la-detección-de-cajas-existentes) abajo.
3. Si no existe → `POST /api/StorageMedia/CreateCarton` (`StorageMediaName: "Stock"`, ubicada en
   la ubicación `RET` / `RET-TEMP` correspondiente, `autoGenerateSSCC=false`).
4. `PUT /api/Warehouse/TransferStock` desde `RET`/`RET-TEMP` → código de la caja.

#### `check_carton` y la detección de cajas existentes

`ValidateCarton` no devuelve un "existe / no existe" limpio: devuelve un objeto `Result` y el
único caso que comunica de forma explícita es el de **no existe**, por el texto del campo
`Message` (`"Could not find a Carton with the code …"`). En el caso de éxito `Message` viene
`null`. `check_carton` (`clients/mintsoftClient.py:302`) resuelve en este orden:

| Respuesta | Resultado |
|---|---|
| `Message` empieza con `"Could not find a Carton with the code"` | `False` — hay que crear la caja |
| No-2xx, o `WasSuccessful: false` con otro mensaje | **Excepción** con el status y el body crudo logueados |
| 2xx sin mensaje de "no existe" (`Message: null`) | `True` — la caja existe |
| 2xx con un mensaje inesperado | `True`, y se loguea el mensaje |
| Body no-JSON o payload que no es un objeto | **Excepción** con el body crudo logueado |
| `put_away_bin` vacío o `None` | `ValueError` (el service saltea el item antes de llegar acá) |

La versión anterior hacía `json.get("Message").startswith(...)` directo, así que **cualquier
respuesta con `Message: null` — es decir el caso normal de caja ya existente — rompía con
`AttributeError: 'NoneType' object has no attribute 'startswith'`** y abortaba el return
completo, dejando el stock en `RET` / `RET-TEMP`.

Para los casos ambiguos se elige deliberadamente **cortar con una excepción** en vez de adivinar:
un `False` incorrecto haría que `CreateCarton` corra sobre un código ya existente y podría
reubicar una caja que está en otro lado; un `True` incorrecto hace fallar el `TransferStock`
después, que es un error recuperable. El status y el body crudo se imprimen siempre en esos
casos, así que la próxima ocurrencia dice exactamente qué devolvió Mintsoft.

El `cartonCode` se manda por `params=` y no interpolado en la URL: los códigos escaneados pueden
traer `#`, `&`, `%` o espacios, que romperían el querystring.

### Fallback Barcode → SKU y la limitación de `SearchBarcode`

Two Boxes a veces manda un `sku` que en Mintsoft no existe con ese valor exacto (el SKU real
está cargado distinto, o el operario escaneó una variante). Para esos casos
`MintsoftOrderClient.get_product_id` (`clients/mintsoftClient.py:252`) tiene un fallback: si la
búsqueda por SKU no encontró producto, reintenta resolviendo el **barcode** a un SKU vía
`GET /api/Product/SearchBarcode`, y si eso devuelve algo usa ese SKU para volver a buscar el
producto.

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

> ⚠️ El `len(barcode)` explota con `TypeError` si el line item no trae `barcode` (`None`).
> En `add_return_items` la excepción se captura y el item se saltea; en `create_return` sube y
> hace fallar el return completo.

---

## Endpoints de la API de Mintsoft utilizados

URL base `https://api.mintsoft.co.uk`. Auth: `POST /api/Auth` al construir el cliente; la
API key devuelta se envía en el header `ms-apikey` en cada llamada. Todos los timeouts son de 120 s.

| Método | Endpoint | Método del cliente |
|---|---|---|
| `POST` | `/api/Auth` | `_authenticate` |
| `GET` | `/api/Order/List?clientId=&statusId=` | `get_orders` |
| `POST` | `/api/Return/CreateReturn/{orderId}` | `create_return` |
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
| `POST` | `/api/Warehouse/StockMovement?Action=7` | `quarantine_stock` |
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

Cada vez que se reporta un error, el return se identifica con
`line_items[0].tracking_number`, o — si no está — con `"{completed_at}-{customer.email}"`.
Para returns externos ese mismo valor se convierte en el `Reference` de Mintsoft (truncado a
50 caracteres).

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
2. **Los mails de alerta no se envían realmente** — `login`/`send_message` están comentados
   (`services/mintsoft_service.py`, marcados con `CAMBIAR`), y aun así se loguea una línea de
   éxito.
3. **`alert_email_to` tiene una coma final**, lo que la convierte en una tupla en lugar de un
   string, y está hardcodeada en vez de leerse de `ALERT_EMAIL_TO`.
4. **Los returns internos ignoran el warehouse mapeado**: `create_return` llama a
   `self.client.create_return(order_id, warehouse_id=3)`, y `MintsoftOrderClient.create_return`
   no le envía `warehouse_id` a Mintsoft en absoluto. La asignación posterior *sí* respeta el
   warehouse mapeado.
5. **El `9` está etiquetado de forma inconsistente** — `RET-TEMP Wholesale` en un lugar y
   `RET-QT Wholesale` en otro. Es el mismo id de ubicación en ambos casos.
6. **En `add_return_items`, `returns_location_id` se asigna en el paso 1 y se recalcula en el
   paso 2**; solo se usa el valor del paso 2 (el que considera el warehouse) para la asignación.
   La asignación del paso 1, incluida la constante `2363` (`RET-QT`), es código muerto.
7. **`get_product_id` se llama repetidamente** para el mismo SKU en `create_return`,
   `add_return_items` (dos veces) y `reallocate_return_items` — sin ningún cacheo.
8. **`get_product_id` arma una URL con doble slash** (`…co.uk//api/Product/Search`), que
   Mintsoft acepta igual.
9. **`reallocate_return_items` devuelve el último `response`**, por lo que lanza
   `UnboundLocalError` si `line_items` está vacío (o si todos los items se saltean por no traer
   `put_away_bin`); además re-lanza la excepción, mientras que `add_return_items` la absorbe.
   Como re-lanza, cualquier fallo en un item **aborta la reasignación de los items siguientes** y
   ese stock queda en `RET` / `RET-TEMP`.
10. **`allocate_external_return_items` sobreescribe el parámetro `data`** dentro de su loop; el
    manejador de errores se protege de eso con un chequeo de `isinstance` / `"event_data" in data`.
11. **El manejador de errores de `add_return_items` referencia `new_identifier`**, que queda sin
    definir cuando *sí* había tracking number — convirtiendo ese camino en un `NameError`.
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

### Agregar un merchant

1. Agregar una fila a `clients` en `mappers/mintsoft_mapper.py` con el nombre exacto del
   merchant de Two Boxes en `tb_name` (en minúsculas), el client id de Mintsoft en `m_id`, y
   `warehouse_id` `3` (Wholesale) o `5` (E-Commerce).
2. Si el merchant vende por los dos canales, agregar **dos** filas con `tb_name` distintos.
3. Verificar que las ubicaciones `RET` / `RET-TEMP` del merchant coincidan con los ids
   hardcodeados para ese depósito; si no, hay que parametrizar las constantes de ubicación.
