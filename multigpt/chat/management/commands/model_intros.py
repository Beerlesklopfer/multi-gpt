"""Kurzvorstellung lokaler Chat-Modelle von Hugging Face holen und übersetzen.

Wie die Admin-Aktion „Vorstellung von Hugging Face holen“. Bestehende Texte bleiben, außer
mit ``--force``; eine Vorstellung von Hand ist also sicher.
"""

from django.core.management.base import BaseCommand

from multigpt.chat import model_cards
from multigpt.chat.models import AIModel


class Command(BaseCommand):
    help = "Holt die Kurzvorstellung lokaler Chat-Modelle von Hugging Face und übersetzt sie."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Vorhandene Texte ersetzen.")

    def handle(self, *args, **options):
        models = AIModel.objects.select_related("provider").filter(
            active=True, capability=AIModel.Capability.CHAT, provider__is_local=True
        )
        if not options["force"]:
            models = models.filter(intro="")
        for ai_model in models.order_by("provider__name", "model_id"):
            try:
                intro = model_cards.refresh(ai_model)
            except model_cards.CardError as exc:
                self.stderr.write(f"{ai_model.model_id}: {exc}")
            else:
                self.stdout.write(f"{ai_model.model_id}: {intro}")
