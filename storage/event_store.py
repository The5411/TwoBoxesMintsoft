"""Persistencia e idempotencia de los webhooks (E-1 / OPS-01 / COR-07).

El problema que resuelve
------------------------
Antes la unica proteccion contra el doble procesamiento era un OrderedDict en
memoria del proceso. Eso fallaba en los tres casos que importan:

  * se pierde en cada reinicio y en cada deploy;
  * no se comparte entre los workers de gunicorn (`--workers 2`), asi que el
    mismo evento en el otro worker pasaba de largo;
  * no recordaba que el return ya se habia CREADO, asi que un reproceso a mano
    -- el caso de la orden W836 -- creaba un segundo return en Mintsoft con el
    mismo stock.

Ahora cada evento tiene una fila. El `return_id` se graba en el instante en que
Mintsoft lo crea, antes de seguir con los items y el stock, asi que un reproceso
posterior ve que el return ya existe y no crea otro, incluso si el intento
anterior murio a mitad de camino.

Backends
--------
  * **Postgres** (`DATABASE_URL`): el unico que sobrevive a un deploy y que
    comparten varias instancias. Es el que corresponde en produccion.
  * **SQLite** (`STORE_PATH`, default): comparte estado entre los workers del
    mismo contenedor y sobrevive al reinicio de un worker, pero vive en el disco
    efimero, asi que un deploy lo borra. Sirve para desarrollo y tests, y como
    red de contencion si todavia no hay base provista.

Se eligio guardar los timestamps como texto ISO-8601 en UTC en los dos backends:
se comparan igual como strings y evita divergencias de zona horaria entre uno y
otro.
"""
import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
from loggers.main_logger import get_logger

logger = get_logger("event_store")

ESTADO_EN_PROCESO = "claimed"
ESTADO_PROCESADO = "processed"
ESTADO_FALLADO = "failed"
ESTADO_IGNORADO = "ignored"

_DDL = """
CREATE TABLE IF NOT EXISTS webhook_events (
    event_key    TEXT PRIMARY KEY,
    event_id     TEXT,
    payload_hash TEXT,
    event_type   TEXT,
    merchant     TEXT,
    reference    TEXT,
    status       TEXT NOT NULL,
    return_id    TEXT,
    return_kind  TEXT,
    attempts     INTEGER NOT NULL DEFAULT 1,
    last_error   TEXT,
    payload      TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
)
"""

_INDICES = (
    "CREATE INDEX IF NOT EXISTS ix_webhook_events_hash ON webhook_events (payload_hash)",
    "CREATE INDEX IF NOT EXISTS ix_webhook_events_reference ON webhook_events (reference)",
    "CREATE INDEX IF NOT EXISTS ix_webhook_events_status ON webhook_events (status)",
)

_COLUMNAS = (
    "event_key", "event_id", "payload_hash", "event_type", "merchant",
    "reference", "status", "return_id", "return_kind", "attempts",
    "last_error", "payload", "created_at", "updated_at",
)


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_payload(datos) -> str:
    """Huella estable del payload, para detectar el mismo evento reenviado.

    Con sort_keys el orden de las claves no cambia la huella, asi que un reenvio
    re-serializado por un intermediario sigue dando el mismo hash.
    """
    try:
        canonico = json.dumps(datos, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        canonico = repr(datos)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


class Veredicto:
    """Resultado de pedir el evento para procesarlo."""

    __slots__ = ("otorgado", "motivo", "registro", "detalle")

    def __init__(self, otorgado: bool, motivo: str, registro=None, detalle: str = ""):
        self.otorgado = otorgado
        self.motivo = motivo
        self.registro = registro or {}
        self.detalle = detalle

    @property
    def return_id(self):
        return self.registro.get("return_id")

    def __repr__(self):
        return (
            f"Veredicto(otorgado={self.otorgado}, motivo={self.motivo!r}, "
            f"return_id={self.return_id!r})"
        )


class StoreNoDisponible(RuntimeError):
    """No se pudo abrir ni inicializar la base."""


class EventStore:
    """Registro de webhooks procesados. Thread-safe.

    Se abre una conexion por operacion a proposito: el volumen es de unos pocos
    webhooks por minuto, y una conexion de vida corta evita tener que manejar un
    pool y las conexiones muertas de Postgres.
    """

    def __init__(self, database_url: Optional[str] = None, sqlite_path: Optional[str] = None):
        self.database_url = (
            database_url if database_url is not None else config.DATABASE_URL
        ) or ""
        self.sqlite_path = sqlite_path or config.STORE_PATH
        self.es_postgres = bool(self.database_url)
        self._lock = threading.Lock()
        self._init_ok = False
        self._psycopg = None
        self._ultimo_error: Optional[str] = None
        self.inicializar()

    # ------------------------------------------------------------------ backend
    @property
    def backend(self) -> str:
        return "postgres" if self.es_postgres else "sqlite"

    def _cargar_psycopg(self):
        if self._psycopg is not None:
            return self._psycopg
        try:
            import psycopg  # psycopg 3
            self._psycopg = ("psycopg", psycopg)
        except ImportError:
            try:
                import psycopg2  # fallback
                self._psycopg = ("psycopg2", psycopg2)
            except ImportError as e:
                raise StoreNoDisponible(
                    "DATABASE_URL esta seteada pero no hay driver de Postgres "
                    "instalado. Agregar 'psycopg[binary]' a requirements.txt."
                ) from e
        return self._psycopg

    def _conectar(self):
        if self.es_postgres:
            nombre, modulo = self._cargar_psycopg()
            url = self.database_url
            # Heroku / Railway exponen 'postgres://', que psycopg3 no acepta.
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://"):]
            if nombre == "psycopg":
                return modulo.connect(url, autocommit=False, connect_timeout=10)
            return modulo.connect(url, connect_timeout=10)

        directorio = os.path.dirname(os.path.abspath(self.sqlite_path))
        if directorio:
            os.makedirs(directorio, exist_ok=True)
        con = sqlite3.connect(self.sqlite_path, timeout=15, isolation_level=None)
        # WAL para que los dos workers de gunicorn puedan leer mientras uno
        # escribe; sin esto se pisan con 'database is locked'.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("BEGIN")
        return con

    def _sql(self, sql: str) -> str:
        """Traduce los placeholders '?' a '%s' cuando el backend es Postgres."""
        return sql.replace("?", "%s") if self.es_postgres else sql

    def inicializar(self) -> bool:
        """Crea la tabla y los indices. Devuelve si el store quedo usable."""
        with self._lock:
            try:
                con = self._conectar()
                try:
                    cur = con.cursor()
                    cur.execute(_DDL)
                    for indice in _INDICES:
                        cur.execute(indice)
                    con.commit()
                finally:
                    con.close()
                self._init_ok = True
                self._ultimo_error = None
                logger.info(
                    f"Store de eventos listo (backend={self.backend}"
                    + (f", path={self.sqlite_path}" if not self.es_postgres else "")
                    + ")"
                )
            except Exception as e:
                self._init_ok = False
                self._ultimo_error = f"{type(e).__name__}: {e}"
                logger.error(f"No se pudo inicializar el store de eventos: {e}", exc_info=True)
            return self._init_ok

    @property
    def disponible(self) -> bool:
        return self._init_ok

    @property
    def ultimo_error(self) -> Optional[str]:
        return self._ultimo_error

    # ------------------------------------------------------------------ helpers
    def _fila_a_dict(self, fila) -> Dict[str, Any]:
        if fila is None:
            return {}
        return dict(zip(_COLUMNAS, fila))

    def _select(self, cur, where: str, parametros: Tuple):
        cur.execute(
            self._sql(f"SELECT {', '.join(_COLUMNAS)} FROM webhook_events WHERE {where}"),
            parametros,
        )
        return cur.fetchall()

    # ------------------------------------------------------------------- claves
    @staticmethod
    def clave_de(datos, event_id=None) -> str:
        """Clave de idempotencia del evento.

        Se prefiere el id que manda Two Boxes; si no viene, la huella del
        payload, que es lo que permite frenar un reenvio a mano de un evento sin
        id.
        """
        if event_id is None and isinstance(datos, dict):
            event_id = datos.get("id")
        if event_id is not None and str(event_id).strip():
            return f"id:{str(event_id).strip()}"
        return f"sha256:{_hash_payload(datos)}"

    # -------------------------------------------------------------------- claim
    def claim(self, datos, *, event_id=None, event_type=None, merchant=None,
              reference=None) -> Veredicto:
        """Pide el derecho a procesar este evento.

        Devuelve un Veredicto con otorgado=True solo si nadie lo procesó antes y
        nadie lo esta procesando ahora. Nunca lanza: si la base falla devuelve
        otorgado=False con motivo='store_caido', y el listener decide que hacer
        segun config.REQUIRE_STORE.
        """
        clave = self.clave_de(datos, event_id)
        huella = _hash_payload(datos)
        ahora = _ahora()
        payload_txt = None
        if config.PERSIST_PAYLOAD:
            try:
                payload_txt = json.dumps(datos, default=str)
            except Exception:
                payload_txt = None

        try:
            with self._lock:
                con = self._conectar()
                try:
                    cur = con.cursor()

                    # 1. Intento crear la fila. Si entra, el evento es nuevo y es mio.
                    cur.execute(
                        self._sql(
                            "INSERT INTO webhook_events ("
                            " event_key, event_id, payload_hash, event_type, merchant,"
                            " reference, status, attempts, payload, created_at, updated_at"
                            ") VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) "
                            "ON CONFLICT (event_key) DO NOTHING"
                        ),
                        (clave, str(event_id) if event_id is not None else None, huella,
                         event_type, merchant, reference, ESTADO_EN_PROCESO,
                         payload_txt, ahora, ahora),
                    )
                    if cur.rowcount == 1:
                        con.commit()
                        return Veredicto(True, "nuevo", {"event_key": clave})

                    # 2. Ya existe: hay que mirar en que estado quedo.
                    filas = self._select(cur, "event_key = ?", (clave,))
                    registro = self._fila_a_dict(filas[0] if filas else None)
                    registro.setdefault("event_key", clave)

                    if registro.get("status") == ESTADO_PROCESADO:
                        con.commit()
                        return Veredicto(
                            False, "ya_procesado", registro,
                            detalle=(
                                f"el evento ya se procesó con exito el "
                                f"{registro.get('updated_at')} "
                                f"(return {registro.get('return_id')})"
                            ),
                        )

                    if registro.get("return_id"):
                        # El caso W836: el intento anterior llego a crear el return
                        # y despues fallo. Reprocesarlo crearia un SEGUNDO return
                        # con el mismo stock.
                        con.commit()
                        return Veredicto(
                            False, "return_ya_creado", registro,
                            detalle=(
                                f"el return {registro.get('return_id')} "
                                f"({registro.get('return_kind')}) ya existe en Mintsoft "
                                f"para este evento; hay que completarlo a mano en vez "
                                f"de reprocesar"
                            ),
                        )

                    corte = (
                        datetime.now(timezone.utc)
                        - timedelta(seconds=config.CLAIM_STALE_SECONDS)
                    ).isoformat()
                    actualizado = registro.get("updated_at") or ""

                    if registro.get("status") == ESTADO_EN_PROCESO and actualizado > corte:
                        con.commit()
                        return Veredicto(
                            False, "en_proceso", registro,
                            detalle=(
                                f"otro worker lo tomo el {actualizado} y todavia esta "
                                f"dentro de la ventana de {config.CLAIM_STALE_SECONDS}s"
                            ),
                        )

                    # 3. Quedo fallado, ignorado, o colgado hace rato: se retoma.
                    #    El guard por updated_at hace que si dos workers lo intentan
                    #    a la vez, solo uno gane.
                    cur.execute(
                        self._sql(
                            "UPDATE webhook_events SET status = ?, attempts = attempts + 1, "
                            "updated_at = ?, last_error = NULL WHERE event_key = ? "
                            "AND updated_at = ? AND return_id IS NULL"
                        ),
                        (ESTADO_EN_PROCESO, ahora, clave, registro.get("updated_at")),
                    )
                    gane = cur.rowcount == 1
                    con.commit()

                    if not gane:
                        return Veredicto(
                            False, "carrera_perdida", registro,
                            detalle="otro worker lo retomo primero",
                        )

                    motivo = (
                        "retomado_colgado"
                        if registro.get("status") == ESTADO_EN_PROCESO
                        else "reintento"
                    )
                    return Veredicto(True, motivo, registro)
                finally:
                    con.close()
        except Exception as e:
            self._init_ok = False
            self._ultimo_error = f"{type(e).__name__}: {e}"
            logger.error(f"claim() fallo contra el store: {e}", exc_info=True)
            return Veredicto(False, "store_caido", {"event_key": clave}, detalle=str(e))

    # ------------------------------------------------------- registrar progreso
    def record_return(self, event_key: str, return_id, kind: str) -> bool:
        """Graba el return recien creado. Se llama INMEDIATAMENTE despues de que
        Mintsoft lo crea: es lo que impide que un reproceso cree un segundo.

        Devuelve False si no se pudo grabar, para que el caller lo reporte: en
        ese estado el evento queda sin la marca que lo protege.
        """
        return self._actualizar(
            "UPDATE webhook_events SET return_id = ?, return_kind = ?, updated_at = ? "
            "WHERE event_key = ?",
            (str(return_id) if return_id is not None else None, kind, _ahora(), event_key),
            f"record_return({event_key}, {return_id})",
        )

    def finish(self, event_key: str, status: str, error: Optional[str] = None) -> bool:
        """Cierra el evento como procesado, fallado o ignorado."""
        return self._actualizar(
            "UPDATE webhook_events SET status = ?, last_error = ?, updated_at = ? "
            "WHERE event_key = ?",
            (status, (str(error)[:2000] if error else None), _ahora(), event_key),
            f"finish({event_key}, {status})",
        )

    def registrar_ignorado(self, datos, *, event_id=None, event_type=None,
                           merchant=None, motivo: str = "") -> None:
        """Deja constancia de un evento que se archivo pero no se proceso.

        Sirve para responder 'esto llego?' cuando alguien pregunta por un evento
        de un event_type que el servicio todavia no soporta.
        """
        veredicto = self.claim(
            datos, event_id=event_id, event_type=event_type, merchant=merchant
        )
        if veredicto.otorgado:
            self.finish(veredicto.registro.get("event_key", self.clave_de(datos, event_id)),
                        ESTADO_IGNORADO, motivo)

    def _actualizar(self, sql: str, parametros: Tuple, que: str) -> bool:
        try:
            with self._lock:
                con = self._conectar()
                try:
                    cur = con.cursor()
                    cur.execute(self._sql(sql), parametros)
                    con.commit()
                    return cur.rowcount >= 1
                finally:
                    con.close()
        except Exception as e:
            self._init_ok = False
            self._ultimo_error = f"{type(e).__name__}: {e}"
            logger.error(f"{que} fallo contra el store: {e}", exc_info=True)
            return False

    # --------------------------------------------------------------- consultas
    def returns_por_reference(self, reference, excluir_event_key=None) -> List[Dict[str, Any]]:
        """Eventos distintos de este que ya crearon un return con esa Reference.

        Es la segunda red: el claim frena el MISMO evento, y esto detecta un
        evento nuevo que apunta al mismo return. Nunca lanza.
        """
        if not reference or not str(reference).strip():
            return []
        try:
            with self._lock:
                con = self._conectar()
                try:
                    cur = con.cursor()
                    filas = self._select(
                        cur, "reference = ? AND return_id IS NOT NULL", (str(reference),)
                    )
                    con.commit()
                finally:
                    con.close()
        except Exception as e:
            logger.error(f"returns_por_reference fallo: {e}")
            return []

        registros = [self._fila_a_dict(f) for f in filas]
        if excluir_event_key:
            registros = [r for r in registros if r.get("event_key") != excluir_event_key]
        return registros

    def get(self, event_key: str) -> Dict[str, Any]:
        try:
            with self._lock:
                con = self._conectar()
                try:
                    cur = con.cursor()
                    filas = self._select(cur, "event_key = ?", (event_key,))
                    con.commit()
                    return self._fila_a_dict(filas[0] if filas else None)
                finally:
                    con.close()
        except Exception as e:
            logger.error(f"get({event_key}) fallo: {e}")
            return {}

    def stats(self) -> Dict[str, Any]:
        """Conteo por estado, para /health. Nunca lanza."""
        try:
            with self._lock:
                con = self._conectar()
                try:
                    cur = con.cursor()
                    cur.execute("SELECT status, COUNT(*) FROM webhook_events GROUP BY status")
                    por_estado = {str(k): int(v) for k, v in cur.fetchall()}
                    cur.execute("SELECT COUNT(*) FROM webhook_events WHERE return_id IS NOT NULL")
                    con_return = int(cur.fetchone()[0])
                    con.commit()
                finally:
                    con.close()
            return {
                "backend": self.backend,
                "disponible": True,
                "por_estado": por_estado,
                "eventos_con_return": con_return,
            }
        except Exception as e:
            return {
                "backend": self.backend,
                "disponible": False,
                "error": f"{type(e).__name__}: {e}",
            }
