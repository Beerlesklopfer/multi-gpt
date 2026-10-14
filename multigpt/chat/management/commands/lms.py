"""``mgpt-ctl lms …`` – Modellverwaltung für LM Studio auf der Konsole (M7-09, 0.3.2).

Unterbefehle::

    lms ps [--provider ID]               geladene Modelle wie ``lms ps`` (mit Schätzung)
    lms budget [--provider ID]           Budget, Reserve, belegt, frei; Modelle mit Profil
    lms load <modell> [--context N]      laden (wie im Chat: Budget, ggf. andere entladen)
    lms unload <modell|instanz> [--provider ID]

``<modell>`` ist die ID eines KI-Modells oder seine Modell-ID (z. B.
``allenai/olmocr-2-7b``). Dünne Hülle über ``chat/model_manager.py``; der Betrieb
(root bzw. Dienstnutzer) handelt wie ein Verwalter.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from multigpt.chat import lmstudio, model_manager, vram
from multigpt.chat.models import AIModel, ModelRuntime, Provider


def _num(value) -> str:
    return f"{value:,}".replace(",", " ") if value else "–"


class Command(BaseCommand):
    help = "Modelle in LM Studio anzeigen, laden und entladen (Modellverwaltung)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", metavar="UNTERBEFEHL")
        sub.required = True
        for name, text in (
            ("ps", "Geladene Modelle anzeigen."),
            ("budget", "Speicherbudget und Lade-Profile anzeigen."),
        ):
            cmd = sub.add_parser(name, help=text)
            cmd.add_argument("--provider", type=int, help="Anbieter-ID (sonst alle lokalen).")
        load = sub.add_parser("load", help="Modell laden (hält sich an das Budget).")
        load.add_argument("model", help="ID oder Modell-ID des KI-Modells.")
        load.add_argument(
            "--context",
            type=int,
            choices=vram.CONTEXT_STAGES,
            help="Kontextfenster speichern und damit laden.",
        )
        unload = sub.add_parser("unload", help="Modell bzw. Instanz entladen.")
        unload.add_argument("model", help="ID bzw. Modell-ID des KI-Modells oder Instanz-ID.")
        unload.add_argument("--provider", type=int, help="Anbieter-ID (bei fremder Instanz).")

    def handle(self, *args, action, **options):
        getattr(self, f"_{action}")(**options)

    # --- Hilfen ---

    def _providers(self, provider_id: int | None) -> list[Provider]:
        providers = model_manager.local_providers()
        if provider_id is not None:
            providers = [p for p in providers if p.pk == provider_id]
            if not providers:
                raise CommandError(f"Kein lokaler Anbieter mit ID {provider_id}.")
        if not providers:
            raise CommandError("Kein lokaler Anbieter (LM Studio) eingerichtet.")
        return providers

    def _model(self, ref: str) -> AIModel:
        qs = AIModel.objects.select_related("provider").filter(provider__is_local=True)
        found = qs.filter(pk=int(ref)).first() if ref.isdigit() else None
        if found is None:
            matches = list(qs.filter(model_id=ref))
            if len(matches) > 1:
                raise CommandError(f"„{ref}“ ist mehrdeutig – bitte die ID angeben.")
            found = matches[0] if matches else None
        if found is None:
            raise CommandError(f"Kein lokales KI-Modell „{ref}“.")
        return found

    def _snapshot(self, provider: Provider):
        try:
            return model_manager.snapshot(provider)
        except lmstudio.LmsError as exc:
            self.stderr.write(f"{provider.name}: {exc.message}")
            return None

    # --- Unterbefehle ---

    def _ps(self, provider=None, **_):
        now = timezone.now()
        for p in self._providers(provider):
            snap = self._snapshot(p)
            if snap is None:
                continue
            state = "verwaltet" if model_manager.managed(p) else "nicht verwaltet"
            self.stdout.write(f"{p.name} (#{p.pk}, {state})")
            if not snap.slots:
                self.stdout.write("  keine Modelle geladen")
            self.stdout.write(f"  {'MODELL':<40} {'KONTEXT':>8} {'SPEICHER':>9}  STATUS")
            for slot in snap.slots:
                flags = []
                if slot.pinned:
                    flags.append("angeheftet")
                if slot.busy:
                    flags.append("in Benutzung")
                if slot.indexing:
                    flags.append("Indexierung")
                if slot.ai_model is None:
                    flags.append("nicht angelegt")
                if slot.last_used:
                    minutes = int((now - slot.last_used).total_seconds() // 60)
                    flags.append(f"genutzt vor {minutes} min")
                self.stdout.write(
                    f"  {slot.label:<40} {_num(slot.context):>8} {slot.size_text:>9}  "
                    + ", ".join(flags)
                )
            if snap.budget:
                self.stdout.write(
                    f"  belegt {vram.gb_text(snap.used)} von {vram.gb_text(snap.usable)} "
                    f"(Budget {vram.gb_text(snap.budget)} − Reserve {vram.gb_text(snap.reserve)}),"
                    f" frei {vram.gb_text(max(0, snap.free))}"
                )

    def _budget(self, provider=None, **_):
        for p in self._providers(provider):
            snap = self._snapshot(p)
            self.stdout.write(f"{p.name} (#{p.pk})")
            if not p.vram_budget_gb:
                self.stdout.write("  kein Budget eingestellt (Admin: Anbieter, Modellverwaltung)")
            elif snap is not None:
                self.stdout.write(
                    f"  Budget {vram.gb_text(snap.budget)}, Reserve {vram.gb_text(snap.reserve)},"
                    f" nutzbar {vram.gb_text(snap.usable)}, belegt {vram.gb_text(snap.used)},"
                    f" frei {vram.gb_text(max(0, snap.free))}"
                )
            catalog = snap.catalog if snap is not None else []
            runtimes = {
                rt.ai_model_id: rt for rt in ModelRuntime.objects.filter(ai_model__provider=p)
            }
            for ai_model in p.ai_models.order_by("model_id"):
                rt = runtimes.get(ai_model.pk)
                entry, _inst = model_manager.match(ai_model, catalog)
                max_context = (
                    entry.max_context_length if entry else (rt.max_context_length if rt else None)
                )
                context = model_manager.profile_context(ai_model, max_context)
                est = model_manager.estimate_model(
                    ai_model, entry, context, model_manager.load_parallel(ai_model), rt
                )
                note = ""
                if snap is not None and snap.budget and est.total > snap.usable:
                    note = "  GRÖSSER ALS DAS BUDGET"
                self.stdout.write(
                    f"  {ai_model.model_id:<40} Kontext {_num(context):>7}  "
                    f"≈ {vram.gb_text(est.total):>8} ({est.source}), Priorität "
                    f"{ai_model.load_priority}{', angeheftet' if ai_model.pinned else ''}{note}"
                )

    def _load(self, model, context=None, **_):
        ai_model = self._model(model)
        if not model_manager.managed(ai_model.provider):
            raise CommandError(
                f"„{ai_model.provider.name}“: „Modelle verwalten“ ist aus bzw. kein Budget gesetzt."
            )
        if context:
            ai_model.load_context_length = context
            ai_model.save(update_fields=["load_context_length"])
        try:
            loaded = model_manager.load_now(ai_model)
        except model_manager.ModelUnavailable as exc:
            raise CommandError(exc.message) from None
        state = "geladen" if loaded else "war schon geladen"
        self.stdout.write(f"„{ai_model.model_id}“ {state}.")

    def _unload(self, model, provider=None, **_):
        instance = model
        target = None
        if provider is None:
            ai_model = self._model(model)
            target, instance = ai_model.provider, ai_model.model_id
            snap = self._snapshot(target)
            if snap is not None:
                for slot in snap.slots:
                    if slot.ai_model is not None and slot.ai_model.pk == ai_model.pk:
                        instance = slot.instance_id
                        break
                else:
                    raise CommandError(f"„{ai_model.model_id}“ ist nicht geladen.")
        else:
            target = self._providers(provider)[0]
        try:
            model_manager.unload_now(target, instance)
        except (model_manager.ModelUnavailable, lmstudio.LmsError) as exc:
            raise CommandError(f"Entladen nicht möglich: {exc.message}") from None
        self.stdout.write(f"„{instance}“ entladen.")
