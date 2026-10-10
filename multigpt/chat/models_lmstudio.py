"""Laufzeitzustand der Modellverwaltung für LM Studio (``chat/model_manager.py``).

Eigenes Modul, damit ``models.py`` schlank bleibt; eingebunden am Ende von
``chat/models.py``. Beide Tabellen sind Betriebsdaten (keine Inhalte):

- ``ModelRuntime``: je Modell zuletzt genutzt, Ladezustand, Fehlversuche mit
  Backoff und die zuletzt von LM Studio gemeldeten Eckdaten (Dateigröße,
  maximale Kontextlänge, Architektur) für die Speicherschätzung im Admin, auch
  wenn LM Studio gerade nicht erreichbar ist.
- ``ModelLease``: laufende Anfragen je Modell, prozessübergreifend (gunicorn,
  Worker). Ein Modell mit gültigem Eintrag wird nie entladen. Jeder Eintrag
  läuft ab (``expires``), damit ein Absturz keine Modelle für immer sperrt.
"""

from django.db import models
from django.utils import timezone


class ModelRuntime(models.Model):
    ai_model = models.OneToOneField(
        "chat.AIModel",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="runtime",
        verbose_name="Modell",
    )
    last_used = models.DateTimeField("zuletzt genutzt", null=True, blank=True)
    loading_since = models.DateTimeField("wird geladen seit", null=True, blank=True)
    loaded_at = models.DateTimeField("zuletzt geladen", null=True, blank=True)
    load_failures = models.PositiveSmallIntegerField("Fehlversuche beim Laden", default=0)
    retry_after = models.DateTimeField("nächster Ladeversuch ab", null=True, blank=True)
    last_error = models.CharField("letzter Ladefehler", max_length=500, blank=True)
    # Zuletzt gemeldete Eckdaten aus GET /api/v1/models.
    size_bytes = models.BigIntegerField("Dateigröße (Byte)", null=True, blank=True)
    max_context_length = models.PositiveIntegerField("maximale Kontextlänge", null=True, blank=True)
    architecture = models.CharField("Architektur", max_length=100, blank=True)
    params = models.CharField("Parameter", max_length=50, blank=True)
    model_type = models.CharField("Art (LM Studio)", max_length=20, blank=True)
    vision = models.BooleanField("Bild-Eingabe", default=False)
    catalog_seen = models.DateTimeField("zuletzt gemeldet", null=True, blank=True)

    class Meta:
        verbose_name = "Modellzustand (LM Studio)"
        verbose_name_plural = "Modellzustände (LM Studio)"

    def __str__(self):
        return f"Zustand von Modell {self.ai_model_id}"


class ModelLease(models.Model):
    ai_model = models.ForeignKey(
        "chat.AIModel", on_delete=models.CASCADE, related_name="leases", verbose_name="Modell"
    )
    created = models.DateTimeField("seit", default=timezone.now)
    expires = models.DateTimeField("läuft ab")

    class Meta:
        verbose_name = "laufende Anfrage"
        verbose_name_plural = "laufende Anfragen"
        indexes = [models.Index(fields=["ai_model", "expires"], name="chat_modellease_model_exp")]

    def __str__(self):
        return f"Anfrage an Modell {self.ai_model_id}"
