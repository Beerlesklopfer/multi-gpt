"""Preise der Modelle (``mgpt-ctl prices``).

    mgpt-ctl prices list [--all]                 aktuelle Preise der Cloud-Modelle
    mgpt-ctl prices suggest [MODELL …] [--apply] Vorschläge aus der LiteLLM-Preisliste

``suggest`` zeigt ohne ``--apply`` nur die Vorschau (wie die Admin-Aktion „Preise aus der
Preisliste vorschlagen“). Mit ``--apply`` entsteht je geändertem Modell eine neue
Preisversion ab jetzt; frühere Buchungen bleiben unverändert.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from multigpt.billing import pricing, provider_costs
from multigpt.chat.models import AIModel
from multigpt.core.cli import table


def _models(names, include_local: bool):
    qs = AIModel.objects.select_related("provider__billing_account").filter(active=True)
    if not include_local:
        qs = qs.filter(provider__is_local=False)
    if names:
        qs = qs.filter(model_id__in=names)
        missing = set(names) - set(qs.values_list("model_id", flat=True))
        if missing:
            raise CommandError(f"Unbekannte Modelle: {', '.join(sorted(missing))}.")
    return qs.order_by("provider__name", "model_id")


class Command(BaseCommand):
    help = "Preise der Modelle anzeigen bzw. aus der LiteLLM-Preisliste übernehmen."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        listing = sub.add_parser("list", help="Aktuelle Preise")
        listing.add_argument("--all", action="store_true", help="auch lokale Modelle")
        suggest = sub.add_parser("suggest", help="Vorschläge aus der LiteLLM-Preisliste")
        suggest.add_argument("models", nargs="*", help="Modell-IDs (Standard: alle Cloud-Modelle)")
        suggest.add_argument("--apply", action="store_true", help="übernehmen (sonst Vorschau)")

    def handle(self, *args, **options):
        if options["action"] == "list":
            self._list(options["all"])
        else:
            self._suggest(options["models"], options["apply"])

    def _list(self, include_local: bool):
        rows = []
        for m in _models([], include_local):
            price = pricing.price_at(m)
            rows.append(
                (
                    m.provider.name,
                    m.model_id,
                    pricing.describe(price, m.provider.billing_account) if price else "kein Preis",
                    timezone.localtime(price.valid_from).strftime("%d.%m.%Y") if price else "",
                )
            )
        table(self.stdout, ("Anbieter", "Modell", "Preis", "gilt ab"), rows, "Keine Modelle.")

    def _suggest(self, names, apply: bool):
        try:
            catalog = provider_costs.fetch_catalog()
        except provider_costs.CostError as exc:
            raise CommandError(str(exc)) from exc
        note = f"LiteLLM-Preisliste, {timezone.localdate():%d.%m.%Y}"
        rows, changed = [], 0
        for m in _models(names, include_local=False):
            s = provider_costs.suggest(m, catalog)
            if s is None:
                rows.append((m.provider.name, m.model_id, "nicht in der Preisliste – von Hand"))
            elif not s.changed:
                rows.append((m.provider.name, m.model_id, "unverändert"))
            else:
                changed += 1
                rows.append((m.provider.name, m.model_id, f"neu: {s.describe()}"))
                if apply:
                    s.apply(note)
        table(self.stdout, ("Anbieter", "Modell", "Vorschlag"), rows, "Keine Cloud-Modelle.")
        if apply:
            self.stdout.write(self.style.SUCCESS(f"{changed} neue Preisversion(en) gespeichert."))
        elif changed:
            self.stdout.write(f"Vorschau: {changed} Änderung(en). Übernehmen mit --apply.")
