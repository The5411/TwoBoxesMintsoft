import logging
import os
import sys
import threading

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- Id de correlacion (E-7) --------------------------------------------------
# Todas las lineas de un mismo webhook llevan el mismo id, asi que se puede
# seguir un return por los logs sin cruzar timestamps a ojo. Es thread-local
# porque cada webhook se procesa entero en un thread del pool.
#
# Se agrega como prefijo SOLO cuando hay uno seteado, asi que las lineas de
# arranque y las de /health siguen exactamente igual que antes.
_contexto = threading.local()


def set_correlacion(valor) -> None:
    _contexto.cid = valor


def limpiar_correlacion() -> None:
    """Obligatorio al terminar: los threads del pool se reusan, y sin esto el
    id del webhook anterior se pegaria a las lineas del siguiente."""
    _contexto.cid = None


def get_correlacion():
    return getattr(_contexto, "cid", None)


class _FiltroCorrelacion(logging.Filter):
    def filter(self, record):
        cid = get_correlacion()
        record.cid = f"[{cid}] " if cid else ""
        return True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def get_logger(name: str, filename: str = None) -> logging.Logger:
    """Logger a stdout.

    Antes esto escribia ademas a logs/<filename> con un RotatingFileHandler de
    10 MB x 5. En un PaaS ese archivo vive en el disco efimero del contenedor:
    se pierde en cada deploy y nadie lo lee nunca, mientras que stdout si va al
    log agregado de la plataforma. El parametro `filename` se mantiene por
    compatibilidad con los callers pero se ignora.
    """
    logger = logging.getLogger(name)
    logger.setLevel(LOG_LEVEL)

    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_FiltroCorrelacion())
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(cid)s%(message)s")
    )
    logger.addHandler(handler)
    # Sin propagar: el root logger de gunicorn duplicaria cada linea.
    logger.propagate = False

    return logger
