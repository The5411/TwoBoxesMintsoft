from dotenv import load_dotenv
from datetime import datetime, timezone
from pathlib import Path
import os
import json

from mappers.mintsoft_mapper import map_client, map_warehouse

load_dotenv()

def _event_merchant_name(event_data):
    """Get merchant name from event_data; supports merchant_integration or first line item's merchant."""
    try:
        return event_data["line_items"]["merchant"]["name"]
    except (KeyError, TypeError):
        pass
    line_items = event_data.get("line_items") or []
    if line_items:
        merchant = (line_items[0] or {}).get("merchant") or {}
        if merchant.get("name"):
            return merchant["name"]
    return ""


def map_return(data):
    data = data[0]["event_data"]
    merchant_name = _event_merchant_name(data)
    client_id = map_client(merchant_name)
    warehouse_id = map_warehouse(merchant_name)
    # m_items = []
    # for item in data["line_items"]:
    #     m_items.append({
    #         "SKU": item["sku"],
    #         "Quantity": item["quantity"],
    #         "ReturnReasonId": 1,
    #         "Action": "DoNothing",
    #         "Comments": item["graded_attributes"][0]["merchant_grading_attribute"]["grading_attribute"]["title"]
    #     })

    # Fallback when merchant not in list (e.g. Tecovas, Two Boxes Demo) so API still receives valid ids
    default_client_id = 3
    default_warehouse_id = 3
    m_data = {
        "ClientId": client_id if client_id is not None else default_client_id,
        "WarehouseId": warehouse_id if warehouse_id is not None else default_warehouse_id,
        "Reference": data["line_items"][0]["tracking_number"],
    }
    return m_data