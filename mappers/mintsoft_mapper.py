import os
import smtplib
from email.message import EmailMessage

clients = [
  { "m_name": "Acler", "m_id": 19, "tb_name": "acler", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Significant Other", "m_id": 33, "tb_name": "significant other", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Brodie", "m_id": 5, "tb_name": "brodie", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Baina", "m_id": 105, "tb_name": "baina", "tb_rma_prov": "Work Capture", "warehouse_id":  3},
  { "m_name": "Bronze Snake", "m_id": 110, "tb_name": "bronze snake", "tb_rma_prov": "Work Capture", "warehouse_id":  5},
  { "m_name": "Deiji Studios", "m_id": 10, "tb_name": "deiji studios ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Emilia Wickstead", "m_id": 20, "tb_name": "emilia wickstead ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Emilia Wickstead", "m_id": 20, "tb_name": "emilia wickstead wholesale", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Emily Lovelock", "m_id": 18, "tb_name": "emily lovelock", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Salt Gypsy", "m_id": 98, "tb_name": "salt gypsy", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Kivari", "m_id": 13, "tb_name": "kivari", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Holiday Company", "m_id": 4, "tb_name": "holiday company", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  # { "m_name": "House of Sunny", "m_id": 12, "tb_name": "house of sunny", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  # { "m_name": "Huishan Zhang", "m_id": 17, "tb_name": "huishan zhang", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  # { "m_name": "Kivari", "m_id": 13, "tb_name": "kivari", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "LF Markey", "m_id": 11, "tb_name": "lf markey", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  # { "m_name": "Lorna Murray", "m_id": 24, "tb_name": "lorna murray", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Lou Swim", "m_id": 25, "tb_name": "lou swim ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Maggie The Label", "m_id": 27, "tb_name": "maggie the label ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Nude Lucy", "m_id": 16, "tb_name": "nude lucy", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Pink City Prints", "m_id": 15, "tb_name": "pink city prints", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  # { "m_name": "Rove", "m_id": 8, "tb_name": "rove", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Sancia", "m_id": 23, "tb_name": "sancia", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Shirty Group", "m_id": 9, "tb_name": "shirty style", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Staple & Hue", "m_id": 21, "tb_name": "staple & hue", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "One Teaspoon", "m_id": 31, "tb_name": "one teaspoon ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  # { "m_name": "Theo", "m_id": 14, "tb_name": "theo", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Third Form", "m_id": 22, "tb_name": "third form ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Third Form", "m_id": 22, "tb_name": "third form - wholesale", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "TT Studios", "m_id": 7, "tb_name": "tt studios", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Sau Lee", "m_id": 107, "tb_name": "sau lee ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Rove", "m_id": 8, "tb_name": "rove ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  # { "m_name": "Ziah", "m_id": 26, "tb_name": "ziah", "tb_rma_prov": "Work Capture", "warehouse_id": 3}
  { "m_name": "Zeynep", "m_id": 87, "tb_name": "zeynep", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Cin Cin", "m_id": 48, "tb_name": "cin cin ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Celia B", "m_id": 49, "tb_name": "celia b", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Clea", "m_id": 45, "tb_name": "clea", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Diarte", "m_id": 74 , "tb_name": "diarte", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "SWF", "m_id": 58, "tb_name": "swf", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Pastiche", "m_id": 51, "tb_name": "pastiche", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Dala", "m_id": 77, "tb_name": "dala", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Lovebirds", "m_id": 80, "tb_name": "lovebirds", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Bernadette", "m_id": 83, "tb_name": "bernadette", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Fori", "m_id": 76, "tb_name": "fori", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Dzo", "m_id": 75, "tb_name": "dzo", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "One of Others", "m_id": 84, "tb_name": "one of others", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "TEST CLIENT", "m_id": 3, "tb_name": "test client wholesale", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "TEST CLIENT", "m_id": 3, "tb_name": "test client ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Seventy Mochi", "m_id": 112, "tb_name": "seventy + mochi ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Lexi", "m_id": 104, "tb_name": "lexi", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Pastiche", "m_id": 51, "tb_name": "pastiche", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Studio 189", "m_id": 113, "tb_name": "studio 189", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Ilio Nema", "m_id": 88, "tb_name": "ilio nema", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "OW Intimates", "m_id": 114, "tb_name": "ow intimates", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Aur Ocea", "m_id": 46, "tb_name": "aur ocea", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Shop Jula", "m_id": 86, "tb_name": "shop jula", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Compania Fantastica", "m_id": 109, "tb_name": "compañía fantástica", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Elce Swim", "m_id": 55, "tb_name": "elce swim", "tb_rma_prov": "Work Capture", "warehouse_id": 3},
  { "m_name": "Pony Rider", "m_id": 29, "tb_name": "pony rider ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Mister Zimi", "m_id": 99, "tb_name": "mister zimi", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Rita Row", "m_id": 73, "tb_name": "rita row ecommerce", "tb_rma_prov": "Work Capture", "warehouse_id": 5},
  { "m_name": "Vero Alfie", "m_id": 59, "tb_name": "vero alfie", "tb_rma_prov": "Work Capture", "warehouse_id": 3}
]

# Clientes que sabemos que no están en `clients` y no queremos que alerten.
IGNORED_CLIENTS = ["posse"]


def _send_unmapped_client_email(tb_name: str) -> None:
    """Avisa por mail que tb_name no está en la lista de clientes. Nunca lanza."""
    try:
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ.get("SMTP_USER")
        smtp_password = os.environ.get("SMTP_PASSWORD")
        email_to = os.environ.get(
            "ALERT_EMAIL_TO",
            "mbivort@the5411.com, jcordero@the5411.com, ngurfinkel@the5411.com, mbivort@the5411.com",
        )

        if not (smtp_host and smtp_user and smtp_password):
            return

        msg = EmailMessage()
        msg["Subject"] = f"[MintsoftReturnService] Cliente no mapeado: {tb_name}"
        msg["From"] = os.environ.get("ALERT_EMAIL_FROM", smtp_user)
        msg["To"] = email_to
        msg.set_content(
            f"El cliente '{tb_name}' no está en la lista de clientes de "
            f"mappers/mintsoft_mapper.py, por lo que map_client() y map_warehouse() "
            f"devolvieron None y la devolución no se pudo mapear."
        )

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except Exception:
                pass
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
    except Exception:
        # Que un fallo de notificación no rompa al caller.
        pass


#
def map_client(tb_name:str):
    for client in clients:
        if client["tb_name"].lower() == tb_name.lower():
            return client["m_id"]
    if tb_name.lower() not in IGNORED_CLIENTS:
        _send_unmapped_client_email(tb_name)
    return None

def map_warehouse(tb_name:str):
    for client in clients:
        if client["tb_name"].lower() == tb_name.lower():
            return client["warehouse_id"]
    return None

# Pavada

#map_client("test-nico")