"""Client-IP für die Login-Drosselung (django-axes) hinter einem Reverse Proxy.

django-axes wertet ``AXES_IPWARE_PROXY_COUNT`` nur aus, wenn das Paket
django-ipware installiert ist – das ist es nicht. Ohne diese Funktion sähe axes
hinter nginx für jede Anfrage ``REMOTE_ADDR=127.0.0.1``.

``settings.REVERSE_PROXY_COUNT`` (Umgebung ``AXES_PROXY_COUNT``) = Anzahl der
Reverse Proxys vor gunicorn. Jeder Proxy hängt die Adresse seines Gegenübers an
``X-Forwarded-For`` an (nginx: ``$proxy_add_x_forwarded_for``). Der Eintrag an Position
``-REVERSE_PROXY_COUNT`` stammt damit vom äußersten eigenen Proxy und ist die
echte Client-Adresse; alles davor kann der Client selbst gesetzt haben und wird
ignoriert. Bei 0, fehlendem oder ungültigem Header gilt ``REMOTE_ADDR``.
"""

import ipaddress

from django.conf import settings


def client_ip(request) -> str | None:
    remote = request.META.get("REMOTE_ADDR") or None
    count = int(getattr(settings, "REVERSE_PROXY_COUNT", 0) or 0)
    if count <= 0:
        return remote
    header = request.META.get("HTTP_X_FORWARDED_FOR", "")
    forwarded = [part.strip() for part in header.split(",") if part.strip()]
    if len(forwarded) < count:
        return remote
    candidate = forwarded[-count]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return remote
