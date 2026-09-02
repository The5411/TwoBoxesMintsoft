import os
import requests
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
import json
load_dotenv()



class MintsoftOrderClient:
    BASE_URL = "https://api.mintsoft.co.uk"

    def __init__(self):
        self.username = os.getenv("MINTSOFT_USERNAME")
        self.password = os.getenv("MINTSOFT_PASSWORD")

        if not all([self.username, self.password]):
            raise RuntimeError(
                "Missing Mintsoft credentials "
                "(MINTSOFT_USERNAME / MINTSOFT_PASSWORD)"
            )

        self.api_key = self._authenticate()

    def _authenticate(self) -> str:
        """Pide una ms-apikey a Mintsoft.

        NO imprimir ni loguear la key: es una credencial. Antes se hacía
        print(r.json()) acá, así que la key quedaba en texto plano en los logs
        de producción en cada arranque de worker.
        """
        url = f"{self.BASE_URL}/api/Auth"

        payload = {
            "Username": self.username,
            "Password": self.password,
        }

        r = requests.post(url, json=payload, timeout=120)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _toolkit_result(r, what: str) -> Dict[str, Any]:
        """Valida una respuesta de Mintsoft que devuelve un ToolkitResult.

        Mintsoft contesta 200 con {"Success": false, "Message": "..."} cuando
        rechaza la operacion, asi que mirar solo el status code no alcanza:
        hay que mirar Success. Sin esto un rechazo pasaba desapercibido.
        """
        r.raise_for_status()
        try:
            body = r.json()
        except ValueError as e:
            raise RuntimeError(
                f"{what}: Mintsoft devolvio {r.status_code} con un cuerpo que no es JSON: "
                f"{(r.text or '')[:300]!r}"
            ) from e
        if not isinstance(body, dict):
            raise RuntimeError(f"{what}: se esperaba un objeto, llego {type(body).__name__}: {body!r}")
        return body

    def headers(self) -> Dict[str, str]:
        return {
            "ms-apikey": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def get_orders(self, client_id: Optional[int] = None, status_id: Optional[int] = None,
                   page_no: int = 1, limit: int = 100) -> List[Dict[str, Any]]:
        """Lista ordenes paginadas.

        Ojo: el parametro de status se llama OrderStatusId, NO statusId. Con
        'statusId' la API no bindea nada y devuelve TODAS las ordenes sin
        filtrar (probado: statusId=4 y statusId=5 devolvian exactamente las
        mismas 100 filas, con OrderStatusIds 1, 5 y 17 mezclados).
        Para encontrar UNA orden puntual usar search_orders(), que no pagina.
        """
        url = f"{self.BASE_URL}/api/Order/List"

        params: Dict[str, Any] = {
            "ClientId": client_id,
            "PageNo": page_no,
            "Limit": limit,
        }
        if status_id is not None:
            params["OrderStatusId"] = status_id

        r = requests.get(
            url,
            headers=self.headers(),
            params=params,
            timeout=120,
        )

        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def search_orders(self, order_number: str, exact_match: bool = True,
                      include_order_items: bool = False) -> List[Dict[str, Any]]:
        """Busca ordenes por OrderNumber, TrackingNumber o ExternalOrderReference.

        Devuelve hasta 10 ordenes, la mas reciente primero. No filtra por
        cliente, asi que el caller tiene que chequear ClientId.
        """
        url = f"{self.BASE_URL}/api/Order/Search"

        r = requests.get(
            url,
            headers=self.headers(),
            params={
                "OrderNumber": order_number,
                "exactMatch": "true" if exact_match else "false",
                "includeOrderItems": "true" if include_order_items else "false",
            },
            timeout=120,
        )

        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return [data] if data else []
    
    def create_return(self, order_id: int, warehouse_id: Optional[int] = None,
                      reference: Optional[str] = None):
        """Crea un return interno contra una orden.

        WarehouseId y Reference van como query params. Antes se recibia
        warehouse_id y no se mandaba a ningun lado, asi que Mintsoft creaba el
        return en el warehouse de la orden mientras el log decia otra cosa.
        Si warehouse_id es None se mantiene ese comportamiento (lo elige Mintsoft).
        """
        url = f"{self.BASE_URL}/api/Return/CreateReturn/{order_id}"

        params: Dict[str, Any] = {}
        if warehouse_id is not None:
            params["WarehouseId"] = warehouse_id
        if reference:
            params["Reference"] = reference

        r = requests.post(
            url,
            headers=self.headers(),
            params=params,
            timeout=120,
        )

        response = self._toolkit_result(r, f"Return/CreateReturn/{order_id}")
        if not response.get("Success"):
            raise RuntimeError(
                f"Mintsoft rechazo CreateReturn para la orden {order_id}: "
                f"{response.get('Message')!r}"
            )
        return response.get("ID")

    def create_external_return(self, data:Dict[str, Any]):
        print("data", data)
        url = f"{self.BASE_URL}/api/Return/CreateExternalReturn"

        r = requests.post(
            url, 
            headers=self.headers(),
            json=data
        )

        response = self._toolkit_result(r, "Return/CreateExternalReturn")
        print("resp", response)

        # Mintsoft contesta 200 con Success=false cuando rechaza el return (por
        # ejemplo un ProductId que no existe). Antes se devolvia response["ID"]
        # sin mirar Success, asi que el ID falso seguia camino y los pasos
        # siguientes allocaban contra un return que no existia.
        if not response.get("Success"):
            raise RuntimeError(
                f"Mintsoft rechazo CreateExternalReturn: {response.get('Message')!r} "
                f"(ClientId={data.get('ClientId')}, WarehouseId={data.get('WarehouseId')}, "
                f"Reference={data.get('Reference')!r})"
            )

        return response.get("ID")
    
    def add_return_item(self, return_id: int, item_data: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.BASE_URL}/api/Return/{return_id}/AddItem"
        
        r = requests.post(
            url,
            headers=self.headers(),
            json=item_data,
            timeout=120
        )
        r.raise_for_status()
        return r.json()
    
    def allocate_return_item_location(self, return_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        item_id = data.get("ReturnItemId")
        quantity = data.get("Quantity")
        location_id = data.get("LocationId")
        
        url = f"{self.BASE_URL}/api/Return/{return_id}/AllocateItemLocation?ReturnitemId={item_id}&Quantity={quantity}&LocationId={location_id}" 
        
        r = requests.post(
            url,
            headers=self.headers(),
            timeout=120
        )
        r.raise_for_status()
        return r.json()
    
    def confirm_return(self, return_id: int) -> Dict[str, Any]:
        url = f"{self.BASE_URL}/api/Return/{return_id}/Confirm"
        
        r = requests.post(
            url,
            headers=self.headers(),
            timeout=120
        )
        r.raise_for_status()
        return r.json()
    
    def get_warehouse_locations(self, warehouse_id:int):
        url = f"{self.BASE_URL}/api/Warehouse/{warehouse_id}/Location/All"

        r = requests.get(
            url,
            headers=self.headers(),
            timeout=120,
        )

        r.raise_for_status()
        data = r.json()
        with open('mintsoft_warehouse_locations_model.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return data
    
    def transfer_stock(self, data: Dict):
        """Mueve stock entre ubicaciones/cajas. Lanza si Mintsoft lo rechaza.

        Antes no habia raise_for_status() ni chequeo de Success, asi que un
        rechazo se devolvia como un dict cualquiera y el caller lo imprimia
        como si hubiera funcionado.
        """
        url = f"{self.BASE_URL}/api/Warehouse/TransferStock"

        r = requests.put(
            url,
            json=data,
            headers=self.headers(),
            timeout=120,
        )

        response = self._toolkit_result(r, "Warehouse/TransferStock")
        if not response.get("Success"):
            raise RuntimeError(
                f"Mintsoft rechazo TransferStock ({data.get('SourceNameOrCode')!r} -> "
                f"{data.get('DestinationNameOrCode')!r}, ProductId={data.get('ProductId')}, "
                f"Quantity={data.get('Quantity')}): {response.get('Message')!r}"
            )
        return response

    def quarantine_stock(self, request):
        """Manda stock a cuarentena (StockMovement Action=7). Lanza si falla.

        NO usar en el flujo de returns: Mintsoft ya cuarentena al confirmar, por el
        StockAction del motivo (ReturnReasonId=2), y conservando la location.
        Llamarla después falla con "Unable to Quarantine stock as not enough could
        be found in the selected location!", porque en esa location ya no hay stock
        sin cuarentenar. Queda para cuarentenar stock a mano, fuera de un return.
        """
        url = f"{self.BASE_URL}/api/Warehouse/StockMovement?Action=7"

        r = requests.post(
            url=url,
            headers=self.headers(),
            json=request,
            timeout=120
        )

        response = self._toolkit_result(r, "Warehouse/StockMovement?Action=7")
        if not response.get("Success"):
            raise RuntimeError(
                f"Mintsoft rechazo StockMovement/Quarantine "
                f"(ProductId={request.get('ProductID') or request.get('ProductId')}, "
                f"LocationId={request.get('LocationId')}, Quantity={request.get('Quantity')}): "
                f"{response.get('Message')!r}"
            )
        return response

    def get_currencies(self):
        url = f"{self.BASE_URL}/api/RefData/Currencies"

        r = requests.get(
            url,
            headers=self.headers(),
            timeout=120,
        )

        r.raise_for_status()
        data = r.json()
        print(data)
        with open('mintsoft_currency_model.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return data
    
    def get_products_in_locations(self, warehouse_id:int, client_id:int):
        url = f"{self.BASE_URL}/api/Reports/ProductsInLocationReport?warehouseId={warehouse_id}&clientId={client_id}"

        r = requests.get(
            url,
            headers=self.headers(),
            timeout=120,
        )

        r.raise_for_status()
        data = r.json()
        print(data)
        with open('mintsoft_products_in_locations_model.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return data
    
    def get_return_reasons(self):
        url = f"{self.BASE_URL}/api/Return/Reasons"

        r = requests.get(
            url,
            headers=self.headers(),
            timeout=120,
        )

        r.raise_for_status()
        data = r.json()
        print(data)
        return data

    def get_return_details(self, return_id):
        url = f"{self.BASE_URL}/api/Return/{return_id}"

        r = requests.get(
            url,
            headers=self.headers(),
            timeout=120
        )
        r.raise_for_status()
        return r.json() 
    
    def get_sku_dado_barcode(self, barcode):
        url = f"{self.BASE_URL}/api/Product/SearchBarcode"

        r = requests.get(
            url,
            headers=self.headers(),
            params={
                "Barcode": barcode
            },
            timeout=120,
        )

        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            sku = data.get("SKU")
            
            if sku is not None:
                return sku

        return "null"
    
    
    def get_product_id(self, sku: str, client_id: int, barcode):
        url = f"{self.BASE_URL}//api/Product/Search?Search={sku}"

        r = requests.get(
            url,
            headers=self.headers(),
            timeout=120,
        )

        r.raise_for_status()
        data = r.json()

        product_id = next(
            (item["ID"] for item in data if item.get("ClientId") == client_id and item.get("SKU") == sku),
            None,
        )

        print(f"Product ID for SKU {sku} (ClientId {client_id}): {product_id}")

        # barcode puede venir None (los payloads de RMA no traen line_items[].barcode),
        # asi que se normaliza antes de medirlo: antes esto reventaba con
        # "TypeError: object of type 'NoneType' has no len()" y el item se perdia.
        barcode = str(barcode).strip() if barcode is not None else ""

        if product_id == None and len(barcode) > 7:
            sku_rety = self.get_sku_dado_barcode(barcode)
            # Sku ret
            if sku_rety == "null":
                return sku, None

            url = f"{self.BASE_URL}//api/Product/Search?Search={sku_rety}"

            r = requests.get(
                url,
                headers=self.headers(),
                timeout=120,
            )

            r.raise_for_status()
            data = r.json()
            print(data, "barcode buscado")
            try: 
                product_id = data[0]["ID"]
                print(product_id, "producto change")
                sku = sku_rety
                
            except:
                product_id = None
        
        print(sku, product_id, "producto final")
            
        return sku, product_id
    

    
    CARTON_NOT_FOUND_PREFIX = "Could not find a Carton with the code"

    def check_carton (self, carton_code):
        # Mintsoft no expone un "existe / no existe" limpio: ValidateCarton devuelve
        # un objeto Result y el unico caso que comunica explicitamente es el de "no
        # existe", por el texto de Message. En el caso de exito Message viene null,
        # asi que la ausencia de mensaje se interpreta como "la caja existe".
        if carton_code is None or not str(carton_code).strip():
            raise ValueError(
                "check_carton: el line item no trae put_away_bin, "
                "no se puede validar ni crear la caja"
            )

        carton_code = str(carton_code).strip()

        url = f'{self.BASE_URL}/api/StorageMedia/ValidateCarton'

        # params= en vez de interpolar: los codigos de caja escaneados pueden traer
        # caracteres que rompen el querystring (#, &, %, espacios).
        response = requests.get(
            url,
            headers=self.headers(),
            params={"cartonCode": carton_code},
            timeout=120,
        )

        body = self._toolkit_result(response, f"StorageMedia/ValidateCarton({carton_code})")
        message = body.get("Message") or ""

        return not message.startswith(self.CARTON_NOT_FOUND_PREFIX)

    def create_carton(self, carton_data, client_id):
        """Crea una caja. Lanza si la request falla; si Mintsoft la rechaza, loguea.

        No lanzamos con Success=false a proposito: un rechazo benigno (por
        ejemplo que la caja ya exista) no deberia tumbar el return entero, y el
        TransferStock que viene despues ahora si falla ruidosamente si la caja
        realmente no se puede usar.
        """
        url = f'{self.BASE_URL}/api/StorageMedia/CreateCarton'

        r = requests.post(
            url,
            json=carton_data,
            headers=self.headers(),
            params={"autoGenerateSSCC": "false", "clientId": client_id},
            timeout=120,
        )

        response = self._toolkit_result(r, "StorageMedia/CreateCarton")
        if not response.get("Success"):
            print(f"⚠️ Mintsoft rechazó CreateCarton {carton_data.get('Code')!r}: "
                  f"{response.get('Message')!r}")
        return response
    
    def create_product(self, product_data):
        url = f'{self.BASE_URL}/api/Product'

        r = requests.put(url, json = product_data, headers = self.headers())

        if r.status_code == 200:
            body = r.json()
            product_id = body.get("ProductId")
            success = body.get("Success", False)

            if success:
                print(f"Se ha creado exitosamente el SKU {product_data['SKU']}")
                return product_id
            else:
                # Mintsoft can return 200 with Success=false and an error in Message
                print(f"Mintsoft rechazó el SKU {product_data['SKU']}: {body.get('Message')}")
                return None

        print(f"Error {r.status_code} creando SKU {product_data['SKU']}: {r.text}")
        return None