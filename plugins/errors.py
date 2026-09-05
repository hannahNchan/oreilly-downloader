"""Errores que el orquestador necesita distinguir del resto."""


class PreviewOnly(Exception):
    """O'Reilly solo sirve un avance de este capitulo, y no es culpa de la sesion.

    Existe para romper un bucle real: el mismo sintoma -- un capitulo corto
    acabado en puntos suspensivos -- lo produce una sesion caida Y un libro que
    no esta completo en la cuenta. Tratando siempre el primer caso, pegabas
    cookies nuevas, la cola reanudaba, el mismo capitulo volvia a llegar corto y
    volvia a pedir cookies. Para siempre.

    NO hereda de SessionExpired a proposito: la cola tiene que darlo por fallo
    del trabajo, no pausar la cola entera esperando algo que no va a ayudar.
    """

    def __init__(self, message: str, html: str = ""):
        super().__init__(message)
        # Lo que SI llego. Quien llama decide si le sirve: para una pagina de
        # relleno legitimamente corta si, y en cualquier caso perderla es mucho
        # menos que perder el libro entero.
        self.html = html


class SessionExpired(Exception):
    """La sesion de O'Reilly ya no sirve: hacen falta cookies nuevas.

    Es un tipo propio y no un RuntimeError con cierto texto porque la cola tiene
    que reaccionar distinto a esto que a cualquier otro fallo: en vez de dar el
    trabajo por perdido, lo pausa y espera a que el usuario pegue cookies.
    Distinguirlo por el mensaje seria fragil — en este mismo proyecto ya habia
    un `if "cancelled" in str(e).lower()` y es justo lo que no quiero repetir.
    """
