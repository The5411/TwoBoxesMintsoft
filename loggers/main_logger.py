import logging
import os
import sys

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

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
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    logger.addHandler(handler)
    # Sin propagar: el root logger de gunicorn duplicaria cada linea.
    logger.propagate = False

    return logger
