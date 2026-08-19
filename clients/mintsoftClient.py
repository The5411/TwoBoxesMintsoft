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
        url = f"{self.BASE_URL}/api/Auth"

        payload = {
            "Username": self.username,
            "Password": self.password,
        }

        r = requests.post(url, json=payload, timeout=120)
        r.raise_for_status()
        print(r.json())
        return r.json()

    def headers(self) -> Dict[str, str]:
        return {
            "ms-apikey": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def get_orders(self, client_id: Optional[int] = None, status_id: Optional[int] = None) -> List[Dict[str, Any]]:
        url = f"{self.BASE_URL}/api/Order/List?clientId={client_id}"

        if status_id is not None:
            url += f"&statusId={status_id}"
            
        r = requests.get(
            url,
            headers=self.headers(),
            timeout=120,
        )

        r.raise_for_status()
        return r.json()
    
    def create_return(self, order_id:int, warehouse_id: Optional[int] = None, client_id: Optional[int] = None):
        url = f"{self.BASE_URL}/api/Return/CreateReturn/{order_id}"

        r = requests.post(
            url, 
            headers=self.headers(),
        )
        
        r.raise_for_status()
        response = r.json()
        return_id = response.get("ID")
        print(response)
        return return_id

    def create_external_return(self, data:Dict[str, Any]):
        print("data", data)
        url = f"{self.BASE_URL}/api/Return/CreateExternalReturn"

        r = requests.post(
            url, 
            headers=self.headers(),
            json=data
        )

        r.raise_for_status()
        response = r.json()
        # Si el response es succes .get("Success") si es false,  llamar a /api/Product/SearchBarcode
        # Cambiar data con lo que llega como productId y volver a llamar a /api/Return/CreateExternalReturn 
        
        print("resp",response)
        
        return_id = response.get("ID")
        
        return return_id
    
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
        url = f"{self.BASE_URL}/api/Warehouse/TransferStock"          

        r = requests.put(
            url,
            json=data,
            headers=self.headers(),
            timeout=120,
        )

        return r.json()

    def quarantine_stock(self, request):
        url = f"{self.BASE_URL}/api/Warehouse/StockMovement?Action=7"

        r = requests.post(
            url=url,
            headers=self.headers(),
            json=request,
            timeout=120
        )

        return r.json()

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
    

    
    def check_carton (self, carton_code):
        url = f'{self.BASE_URL}/api/StorageMedia/ValidateCarton?cartonCode={carton_code}'

        response = requests.get(url, headers=self.headers())

        json = response.json()

        message = json.get("Message")

        if message.startswith("Could not find a Carton with the code"):
            
            return False
        
        else:

            return True

    def create_carton(self, carton_data, client_id):
        url = f'{self.BASE_URL}/api/StorageMedia/CreateCarton?autoGenerateSSCC=false&clientId={client_id}'

        r = requests.post(url, json = carton_data, headers=self.headers())

        return None
    
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