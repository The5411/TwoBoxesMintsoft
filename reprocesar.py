#!/usr/bin/env python3
"""Listado y reproceso de webhooks que quedaron a medias (E-1.5).

    python3 reprocesar.py listar                     # que quedo pendiente
    python3 reprocesar.py ver <event_key>            # el detalle de uno
    python3 reprocesar.py correr <event_key>         # reprocesar ese evento
    python3 reprocesar.py cerrar <event_key> "nota"  # darlo por resuelto a mano

Es un comando, no un endpoint: reprocesar escribe en el WMS, y no hace falta
exponer eso en la red para que lo use quien ya tiene acceso al servidor.

**Nunca crea un segundo return.** Si el evento ya tiene `return_id`, el reproceso
se niega y dice que hay que completarlo a mano desde Mintsoft. Es la misma regla
que aplica el listener cuando alguien reenvia un webhook.

No re-archiva en Google Drive: llama directo a procesar_webhook, que es la
operacion de negocio, sin pasar por el handler HTTP.
"""
import json
import sys

import listener
from storage.event_store import (
    ESTADO_FALLADO,
    ESTADO_IGNORADO,
    ESTADO_INTERRUMPIDO,
    ESTADO_PROCESADO,
)

USO = __doc__


def _fmt(reg):
    return (
        f"  {reg.get('event_key'):<48} {str(reg.get('status')):<12} "
        f"paso={str(reg.get('step')):<16} return={str(reg.get('return_id')):<8} "
        f"intentos={reg.get('attempts')}\n"
        f"      merchant={reg.get('merchant')!r} reference={reg.get('reference')!r}\n"
        f"      actualizado={reg.get('updated_at')}\n"
        f"      error={(reg.get('last_error') or '')[:160]}"
    )


def listar(argv):
    estados = argv or [ESTADO_FALLADO, ESTADO_INTERRUMPIDO]
    registros = listener.event_store.listar(estados=estados, limite=100)
    if not registros:
        print(f"Nada pendiente en estado(s) {estados}.")
        return 0
    print(f"{len(registros)} evento(s) en estado(s) {estados}:\n")
    for reg in registros:
        print(_fmt(reg))
        print()
    print(
        "Los que tienen return != None YA crearon el return en Mintsoft: hay que "
        "completarlos a mano y cerrarlos con `cerrar`, no reprocesarlos."
    )
    return 0


def ver(argv):
    if not argv:
        print(USO)
        return 2
    reg = listener.event_store.get(argv[0])
    if not reg:
        print(f"No hay ningun evento con clave {argv[0]!r}.")
        return 1
    for clave, valor in reg.items():
        if clave == "payload":
            continue
        print(f"{clave:>14}: {valor}")
    if reg.get("payload"):
        print("\npayload:")
        try:
            print(json.dumps(json.loads(reg["payload"]), indent=2, ensure_ascii=False))
        except Exception:
            print(reg["payload"])
    return 0


def correr(argv):
    if not argv:
        print(USO)
        return 2
    clave = argv[0]
    reg = listener.event_store.get(clave)

    if not reg:
        print(f"No hay ningun evento con clave {clave!r}.")
        return 1

    # El estado va PRIMERO: un evento que salio bien tambien tiene return_id, y
    # decirle a alguien que "quedo a medias, completalo a mano" cuando en realidad
    # termino perfecto lo manda a buscar un problema que no existe.
    if reg.get("status") == ESTADO_PROCESADO:
        print(
            f"El evento {clave} ya se proceso con exito "
            f"(return {reg.get('return_id')}, paso {reg.get('step')!r}). "
            f"No hay nada que rehacer."
        )
        return 1

    if reg.get("return_id"):
        print(
            f"NO se reprocesa: el evento {clave} ya creo el return "
            f"{reg['return_id']} ({reg.get('return_kind')}) en Mintsoft.\n"
            f"Reprocesarlo crearia un segundo return con el mismo stock.\n\n"
            f"Quedo en el paso {reg.get('step')!r}, asi que probablemente le falten "
            f"items o el movimiento de stock. Completalo a mano en Mintsoft y despues:\n"
            f"    python3 reprocesar.py cerrar {clave} \"lo que hiciste\""
        )
        return 1

    if not reg.get("payload"):
        print(
            f"El evento {clave} no tiene el payload guardado (PERSIST_PAYLOAD estaba "
            f"apagado), asi que no se puede reprocesar desde la base. Hay que "
            f"recuperarlo del archivo en Google Drive y reenviarlo."
        )
        return 1

    datos = json.loads(reg["payload"])

    # El claim vuelve a pedir el evento: si otro worker lo esta procesando ahora
    # mismo, el reproceso NO arranca.
    veredicto = listener.event_store.claim(
        datos,
        event_id=reg.get("event_id"),
        event_type=reg.get("event_type"),
        merchant=reg.get("merchant"),
        reference=reg.get("reference"),
    )
    if not veredicto.otorgado:
        print(
            f"NO se reprocesa: {veredicto.motivo} -- {veredicto.detalle}"
        )
        return 1

    print(f"Reprocesando {clave} (intento {reg.get('attempts')} -> siguiente)...")
    listener.procesar_webhook(datos, clave)

    final = listener.event_store.get(clave)
    print(
        f"\nResultado: status={final.get('status')} paso={final.get('step')} "
        f"return={final.get('return_id')}"
    )
    if final.get("last_error"):
        print(f"Error: {final['last_error']}")
    return 0 if final.get("status") == ESTADO_PROCESADO else 1


def cerrar(argv):
    if len(argv) < 2:
        print(USO)
        return 2
    clave, nota = argv[0], " ".join(argv[1:])
    if not listener.event_store.get(clave):
        print(f"No hay ningun evento con clave {clave!r}.")
        return 1
    # Se cierra como ignorado, no como procesado: el servicio no lo proceso, lo
    # resolvio una persona. La nota queda en last_error, que es el campo que el
    # listado muestra.
    listener.event_store.finish(clave, ESTADO_IGNORADO, f"cerrado a mano: {nota}")
    print(f"Evento {clave} cerrado a mano. No va a volver a aparecer en `listar`.")
    return 0


COMANDOS = {"listar": listar, "ver": ver, "correr": correr, "cerrar": cerrar}


def main(argv):
    if not argv or argv[0] not in COMANDOS:
        print(USO)
        return 2
    if not listener.event_store.disponible:
        print(
            f"El store de eventos no esta disponible: "
            f"{listener.event_store.ultimo_error}"
        )
        return 1
    return COMANDOS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
