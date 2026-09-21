"""Configuracion centralizada del servicio.

Todo lo que antes era un numero magico repetido en varios lugares vive aca, con
un default igual al comportamiento actual y una variable de entorno para
cambiarlo sin deploy.

Los ids de location eran el caso mas grave: 4104 / 4299 / 9 / 4304 aparecian
hardcodeados en cinco bloques distintos de services/mintsoft_service.py, y en
todos con la forma `if warehouse == 3: <wholesale> else: <ecommerce>`. Ese `else`
significaba que un warehouse nuevo (o warehouse=None, cuando el merchant no esta
mapeado) recibia en silencio las locations de E-Commerce. Ahora un warehouse
desconocido lanza LocationDesconocida en vez de mandar stock a la location de
otro deposito.
"""
import json
import os
from typing import Any, Dict, Optional


def _env_int(nombre: str, default: int) -> int:
    crudo = os.environ.get(nombre)
    if crudo is None or not str(crudo).strip():
        return default
    try:
        return int(str(crudo).strip())
    except ValueError:
        raise RuntimeError(
            f"{nombre}={crudo!r} no es un entero valido"
        ) from None


def _env_bool(nombre: str, default: bool) -> bool:
    crudo = os.environ.get(nombre)
    if crudo is None or not str(crudo).strip():
        return default
    return str(crudo).strip().lower() in ("1", "true", "yes", "y", "on", "si")


def _env_set(nombre: str, default: str):
    crudo = os.environ.get(nombre, default)
    return {x.strip() for x in str(crudo).split(",") if x.strip()}


def _env_int_set(nombre: str, default: str):
    return {int(x) for x in _env_set(nombre, default)}


# --- Locations de returns por warehouse ---------------------------------------
# 3 = Wholesale, 5 = E-Commerce.
#   good       -> RET      (stock que vuelve en condiciones de vender)
#   quarantine -> RET-TEMP (stock que va a cuarentena)
_LOCATIONS_DEFAULT: Dict[int, Dict[str, int]] = {
    3: {"good": 4104, "quarantine": 9},
    5: {"good": 4299, "quarantine": 4304},
}


class LocationDesconocida(RuntimeError):
    """No hay locations configuradas para ese warehouse."""


def _cargar_locations() -> Dict[int, Dict[str, int]]:
    """Locations por warehouse, con override por RETURN_LOCATIONS (JSON).

    Ejemplo:
        RETURN_LOCATIONS={"3":{"good":4104,"quarantine":9},"7":{"good":1,"quarantine":2}}

    El override reemplaza el warehouse entero, no lo mergea: si se define el 3
    hay que dar sus dos locations.
    """
    locations = {w: dict(v) for w, v in _LOCATIONS_DEFAULT.items()}

    crudo = os.environ.get("RETURN_LOCATIONS")
    if not crudo or not crudo.strip():
        return locations

    try:
        override = json.loads(crudo)
    except ValueError as e:
        raise RuntimeError(f"RETURN_LOCATIONS no es JSON valido: {e}") from e
    if not isinstance(override, dict):
        raise RuntimeError("RETURN_LOCATIONS tiene que ser un objeto JSON")

    for warehouse, valores in override.items():
        if not isinstance(valores, dict):
            raise RuntimeError(
                f"RETURN_LOCATIONS[{warehouse!r}] tiene que ser un objeto con "
                f"'good' y 'quarantine'"
            )
        faltantes = {"good", "quarantine"} - set(valores)
        if faltantes:
            raise RuntimeError(
                f"RETURN_LOCATIONS[{warehouse!r}] no define {sorted(faltantes)}"
            )
        locations[int(warehouse)] = {
            "good": int(valores["good"]),
            "quarantine": int(valores["quarantine"]),
        }

    return locations


RETURN_LOCATIONS = _cargar_locations()


def location_id(warehouse_id: Optional[int], *, buen_estado: bool) -> int:
    """Location de returns para ese warehouse.

    `buen_estado=True` devuelve RET; False devuelve RET-TEMP (cuarentena).
    Lanza LocationDesconocida si el warehouse no esta configurado: mandar el
    stock a la location de otro deposito es peor que fallar y avisar.
    """
    try:
        clave = int(warehouse_id)
    except (TypeError, ValueError):
        raise LocationDesconocida(
            f"WarehouseId={warehouse_id!r} no es un id valido: no se puede "
            f"resolver la location de returns. Suele venir de un merchant que no "
            f"esta en la tabla de mappers/mintsoft_mapper.py."
        ) from None

    if clave not in RETURN_LOCATIONS:
        raise LocationDesconocida(
            f"WarehouseId={clave} no tiene locations de returns configuradas "
            f"(configurados: {sorted(RETURN_LOCATIONS)}). Agregarlo a "
            f"RETURN_LOCATIONS para habilitarlo."
        )

    return RETURN_LOCATIONS[clave]["good" if buen_estado else "quarantine"]


# Nombres de las locations de origen del TransferStock. Mintsoft los recibe como
# texto (SourceNameOrCode), no como id.
SOURCE_GOOD = os.environ.get("SOURCE_LOCATION_GOOD", "RET")
SOURCE_QUARANTINE = os.environ.get("SOURCE_LOCATION_QUARANTINE", "RET-TEMP")


# --- Dispositions y return reasons --------------------------------------------
# Two Boxes manda `disposition` por line item. 'Return to Stock' es la unica que
# vuelve a stock vendible; el resto ('Exception', 'Damaged', ...) va a cuarentena.
GOOD_STOCK_DISPOSITIONS = _env_set("GOOD_STOCK_DISPOSITIONS", "Return to Stock")
# 'Missing' es un caso aparte: la unidad nunca llego al deposito.
MISSING_DISPOSITION = os.environ.get("MISSING_DISPOSITION", "Missing")

# ReturnReasonId de Mintsoft. El 2 lleva StockAction='Quarantine', que es lo que
# cuarentena la unidad al confirmar el return.
RETURN_REASON_GOOD = _env_int("RETURN_REASON_GOOD", 1)
RETURN_REASON_QUARANTINE = _env_int("RETURN_REASON_QUARANTINE", 2)


def return_reason_id(disposition) -> int:
    return (
        RETURN_REASON_GOOD
        if es_buen_estado(disposition)
        else RETURN_REASON_QUARANTINE
    )


def es_buen_estado(disposition) -> bool:
    return str(disposition or "").strip() in GOOD_STOCK_DISPOSITIONS


def es_missing(item: Optional[Dict[str, Any]]) -> bool:
    """True si el item nunca llego fisicamente al deposito.

    Two Boxes lo marca con disposition='Missing': el cliente declaro la
    devolucion pero la unidad no aparecio. No hay nada que dar de alta en
    Mintsoft, nada que ubicar, y -- importante -- no corresponde reclamarle
    `put_away_bin`, porque no hay unidad que guardar en ninguna caja.
    """
    return str((item or {}).get("disposition") or "").strip() == MISSING_DISPOSITION


# --- Ordenes ------------------------------------------------------------------
# Estados en los que una orden ya salio del deposito y por lo tanto se le puede
# crear un return: 4=DESPATCHED, 5=INVOICED, 6=INVOICEFAILED.
RETURNABLE_ORDER_STATUS_IDS = _env_int_set("RETURNABLE_ORDER_STATUS_IDS", "4,5,6")

# Mintsoft corta la Reference en 50 caracteres.
REFERENCE_MAX_LEN = _env_int("REFERENCE_MAX_LEN", 50)


# --- Webhook ------------------------------------------------------------------
EVENT_TYPES_SOPORTADOS = _env_set("EVENT_TYPES", "return-complete")
MAX_CONTENT_LENGTH = _env_int("MAX_CONTENT_LENGTH", 5 * 1024 * 1024)

# Threads para la operacion de negocio (Mintsoft) y, aparte, para el archivado a
# Google. Antes compartian un solo pool de 10: un Google Apps Script lento (los
# timeouts son de 60 y 120 segundos) se quedaba con los threads y demoraba el
# procesamiento en el WMS, que es lo unico que no se puede perder.
WORKERS_MINTSOFT = _env_int("WORKERS_MINTSOFT", 10)
WORKERS_ARCHIVO = _env_int("WORKERS_ARCHIVO", 4)


# --- Persistencia / idempotencia (E-1, OPS-01, COR-07) ------------------------
# DATABASE_URL (postgres://...) si esta seteada; si no, SQLite en STORE_PATH.
# Ver storage/event_store.py para las implicancias de cada backend.
DATABASE_URL = os.environ.get("DATABASE_URL") or ""
STORE_PATH = os.environ.get("STORE_PATH", "webhook_events.db")

# Si no se puede abrir el store NO se escribe en Mintsoft: sin idempotencia un
# reproceso duplica el return, y eso es peor que un return demorado. El evento se
# archiva igual y sale un mail.
REQUIRE_STORE = _env_bool("REQUIRE_STORE", True)

# Guardar el payload completo habilita reprocesar desde la base sin depender del
# archivado a Google Drive. Incluye los datos del comprador.
PERSIST_PAYLOAD = _env_bool("PERSIST_PAYLOAD", True)

# Un claim que quedo abierto mas de esto se considera colgado (el worker murio) y
# se puede retomar. Tiene que ser mucho mas largo que un webhook normal.
CLAIM_STALE_SECONDS = _env_int("CLAIM_STALE_SECONDS", 1800)

# Que hacer cuando llega un evento NUEVO cuya Reference ya tiene un return creado.
#   warn  -> se procesa, pero avisa por mail (default)
#   block -> no se crea un segundo return
#   off   -> no se chequea
# No es 'block' por default porque una misma orden puede tener dos devoluciones
# legitimas en momentos distintos, y ahi la Reference cae al numero de orden.
DUPLICATE_REFERENCE_ACTION = os.environ.get(
    "DUPLICATE_REFERENCE_ACTION", "warn"
).strip().lower()
