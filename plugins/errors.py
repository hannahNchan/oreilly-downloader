"""Errores que el orquestador necesita distinguir del resto."""


class SessionExpired(Exception):
    """La sesion de O'Reilly ya no sirve: hacen falta cookies nuevas.

    Es un tipo propio y no un RuntimeError con cierto texto porque la cola tiene
    que reaccionar distinto a esto que a cualquier otro fallo: en vez de dar el
    trabajo por perdido, lo pausa y espera a que el usuario pegue cookies.
    Distinguirlo por el mensaje seria fragil — en este mismo proyecto ya habia
    un `if "cancelled" in str(e).lower()` y es justo lo que no quiero repetir.
    """
