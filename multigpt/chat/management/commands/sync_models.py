"""Modelllisten der Anbieter abrufen (`make sync-models`, M4-02).

Für jeden aktiven, nicht lokalen Anbieter wird ``list_models()`` abgefragt.
Fehlende Modelle werden als ``AIModel`` mit ``active=False`` angelegt, damit
der Verwalter sie bewusst freischaltet (Preise, Anzeigename, Werkzeuge).
Vorhandene Einträge bleiben unverändert. Lokale Anbieter (LM Studio)
überspringt das Kommando: Deren Modelle gleicht die Statusprüfung ab.

Die Fähigkeit wird nur bei eindeutigen ID-Mustern abweichend von ``chat``
gesetzt; im Zweifel ``chat``, der Verwalter korrigiert im Admin.
"""

import re

from django.core.management.base import BaseCommand, CommandError

from multigpt.chat.models import AIModel, Provider
from multigpt.chat.providers import ProviderError, get_adapter

# Reihenfolge zählt: erstes passendes Muster gewinnt.
_CAPABILITY_PATTERNS = [
    (re.compile(r"embed"), AIModel.Capability.EMBEDDING),
    (re.compile(r"(^|[-_./])tts([-_.]|$)"), AIModel.Capability.TTS),
    (re.compile(r"whisper|transcribe"), AIModel.Capability.STT),
    (re.compile(r"dall-e|gpt-image|(^|/)imagen"), AIModel.Capability.IMAGE),
]


def guess_capability(model_id: str) -> str:
    """Fähigkeit aus eindeutigen ID-Mustern, sonst ``chat``."""
    lowered = model_id.lower()
    for pattern, capability in _CAPABILITY_PATTERNS:
        if pattern.search(lowered):
            return capability
    return AIModel.Capability.CHAT


class Command(BaseCommand):
    help = (
        "Ruft die Modelllisten der aktiven Anbieter ab und legt fehlende Modelle "
        "inaktiv an. Vorhandene Modelle bleiben unverändert, lokale Anbieter werden "
        "übersprungen."
    )

    def add_arguments(self, parser):
        parser.add_argument("--provider", help="Nur diesen Anbieter abgleichen (Name oder ID).")
        parser.add_argument(
            "--dry-run", action="store_true", help="Nur anzeigen, nichts speichern."
        )

    def handle(self, *args, **options):
        providers = self._providers(options["provider"])
        failed = 0
        for provider in providers:
            if provider.is_local:
                self.stdout.write(
                    f"{provider.name}: übersprungen (lokal, Abgleich über die Statusprüfung)."
                )
                continue
            try:
                model_ids = get_adapter(provider).list_models()
            except ProviderError as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f"{provider.name}: {exc}"))
                continue
            self._sync(provider, model_ids, options["dry_run"])
        if failed:
            raise CommandError(f"{failed} Anbieter konnten nicht abgefragt werden.")

    def _providers(self, selector):
        qs = Provider.objects.order_by("name")
        if not selector:
            providers = list(qs.filter(active=True))
            if not providers:
                self.stdout.write("Keine aktiven Anbieter vorhanden.")
            return providers
        lookup = {"pk": int(selector)} if selector.isdigit() else {"name": selector}
        provider = qs.filter(**lookup).first()
        if provider is None:
            raise CommandError(f"Anbieter „{selector}“ nicht gefunden.")
        if not provider.active:
            raise CommandError(f"Anbieter „{provider.name}“ ist nicht aktiv.")
        return [provider]

    def _sync(self, provider, model_ids, dry_run):
        max_len = AIModel._meta.get_field("model_id").max_length
        existing = set(provider.ai_models.values_list("model_id", flat=True))
        seen = set()
        new = []
        too_long = 0
        for model_id in model_ids:
            if model_id in existing or model_id in seen:
                continue
            seen.add(model_id)
            if len(model_id) > max_len:
                too_long += 1
                continue
            new.append(
                AIModel(
                    provider=provider,
                    model_id=model_id,
                    display_name=model_id,
                    capability=guess_capability(model_id),
                    active=False,
                )
            )
        if new and not dry_run:
            # ignore_conflicts: Ein paralleler Abgleich darf nicht stören.
            AIModel.objects.bulk_create(new, ignore_conflicts=True)

        verb = "würden angelegt" if dry_run else "neu (inaktiv)"
        self.stdout.write(
            self.style.SUCCESS(
                f"{provider.name}: {len(model_ids)} gemeldet, {len(new)} {verb}, "
                f"{len(existing & set(model_ids))} bereits vorhanden."
            )
        )
        for model in new:
            label = AIModel.Capability(model.capability).label
            self.stdout.write(f"  + {model.model_id} ({label})")
        if too_long:
            self.stdout.write(f"  {too_long} Modell-ID(s) zu lang, ausgelassen.")
