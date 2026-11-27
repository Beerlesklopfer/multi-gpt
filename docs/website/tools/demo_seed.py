"""Demodaten für die Bildschirmfotos der Projektseite.

Nur gegen eine eigene, leere Datenbank ausführen, nie gegen die Produktion:

    createdb -T multigpt_template multigpt_demo
    DATABASE_URL=postgres:///multigpt_demo python manage.py migrate
    DATABASE_URL=postgres:///multigpt_demo python manage.py shell < docs/website/tools/demo_seed.py

Legt Familienkonten (Passwort ``demo-demo-demo``), Anbieter ohne Keys, Modelle,
Chats mit fertigen Antworten (keine Modellaufrufe), Sammlungen und Buchungen an.
"""

import datetime
import random
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from multigpt.accounts.models import Role
from multigpt.billing.models import BillingAccount, UsageEntry
from multigpt.chat import services
from multigpt.chat.models import (
    AIModel,
    Collection,
    Conversation,
    Document,
    Project,
    Provider,
    RagSettings,
)

db_name = settings.DATABASES["default"]["NAME"]
if "demo" not in db_name:
    raise SystemExit(f"Abbruch: Datenbank „{db_name}“ ist keine Demo-Datenbank.")

PASSWORD = "demo-demo-demo"
User = get_user_model()


def user(username, role, display):
    obj, _ = User.objects.get_or_create(
        username=username,
        defaults={"role": Role.objects.get(key=role), "display_name": display},
    )
    obj.role = Role.objects.get(key=role)
    obj.display_name = display
    obj.is_staff = obj.role.is_admin
    obj.is_superuser = obj.role.is_admin
    obj.set_password(PASSWORD)
    obj.save()
    return obj


anna = user("anna", "admin", "Anna")
ben = user("ben", "adult", "Ben")
lea = user("lea", "teen", "Lea")

money, _ = BillingAccount.objects.get_or_create(
    name="Anthropic Familie", defaults={"kind": "monetary", "currency": "EUR"}
)
local, _ = BillingAccount.objects.get_or_create(name="Eigene GPU", defaults={"kind": "tokens"})

eliezer, _ = Provider.objects.get_or_create(
    name="Eliezer (eigene GPU)",
    defaults={
        "kind": "openai_compat",
        "base_url": "http://gpu.local:8080/v1",
        "is_local": True,
        "billing_account": local,
    },
)
anthropic, _ = Provider.objects.get_or_create(
    name="Anthropic", defaults={"kind": "anthropic", "billing_account": money}
)
openai, _ = Provider.objects.get_or_create(
    name="OpenAI", defaults={"kind": "openai_compat", "billing_account": money}
)


def model(provider, model_id, name, order, **extra):
    obj, _ = AIModel.objects.update_or_create(
        provider=provider,
        model_id=model_id,
        defaults={"display_name": name, "sort_order": order, "supports_tools": True, **extra},
    )
    return obj


gemma = model(eliezer, "gemma-3-27b", "Gemma 3 27B (lokal)", 1, context_length=32768)
qwen = model(eliezer, "qwen3-coder-30b", "Qwen3 Coder 30B (lokal)", 2, context_length=65536)
sonnet = model(anthropic, "claude-sonnet-5-5", "Claude Sonnet 5.5", 3, supports_vision=True)
opus = model(anthropic, "claude-opus-5-5", "Claude Opus 5.5", 4, supports_vision=True)
gpt = model(openai, "gpt-5", "GPT-5", 5, supports_vision=True)
embed = model(eliezer, "nomic-embed-text", "nomic-embed-text", 9, capability="embedding")

# Jugendliche: nur die lokalen Modelle; Erwachsene alles.
teen = Role.objects.get(key="teen")
teen.allowed_models.set([gemma, qwen])
Role.objects.filter(key="adult").update(all_models=True)
rag = RagSettings.load()
rag.embedding_model = embed
rag.save()

# Sammlungen mit fertigen Dokumenten (nur Metadaten; Text und Vektoren fehlen).
family_docs, _ = Collection.objects.get_or_create(owner=anna, name="Familienordner")
recipes, _ = Collection.objects.get_or_create(owner=anna, name="Rezepte")
school, _ = Collection.objects.get_or_create(owner=lea, name="Schule – Biologie")
for coll, titles in (
    (
        family_docs,
        [
            "Mietvertrag Wohnung 2024.pdf",
            "Kfz-Versicherung Police.pdf",
            "Heizungswartung Protokoll.pdf",
            "Stromabrechnung 2025.pdf",
        ],
    ),
    (recipes, ["Omas Apfelkuchen.md", "Linsensuppe.md", "Brot mit Sauerteig.md"]),
    (school, ["Zellbiologie Skript.pdf", "Fotosynthese Arbeitsblatt.pdf"]),
):
    for title in titles:
        Document.objects.get_or_create(collection=coll, title=title, defaults={"status": "indexed"})

project, _ = Project.objects.get_or_create(owner=anna, name="Haus und Garten")


def chat(owner, title, model, turns, project=None):
    Conversation.objects.filter(user=owner, title=title).delete()
    conv = Conversation.objects.create(
        user=owner, title=title, default_model=model, project=project
    )
    for question, answer, tokens in turns:
        services.append_message(conv, role="user", content=question, author=owner)
        services.append_message(
            conv,
            role="assistant",
            content=answer,
            model=model,
            tokens_in=tokens[0],
            tokens_out=tokens[1],
            status="complete",
        )
    return conv


chat(
    anna,
    "Heizungswartung: was steht an?",
    sonnet,
    [
        (
            "Was hat der Techniker bei der letzten Heizungswartung bemängelt, und was "
            "müssen wir bis zum Winter erledigen?",
            "Laut **Heizungswartung Protokoll.pdf** (Seite 2) wurden zwei Punkte "
            "vermerkt:\n\n"
            "| Punkt | Befund | Bis wann |\n|---|---|---|\n"
            "| Ausdehnungsgefäß | Vordruck 0,6 bar statt 1,0 bar | vor der Heizperiode |\n"
            "| Thermostatventil Bad | schwergängig, Austausch empfohlen | ohne Frist |\n\n"
            "**Vor dem Winter** sollte also das Ausdehnungsgefäß nachgefüllt werden – "
            "das ist eine kurze Arbeit für den Installateur. Das Ventil im Bad kann "
            "warten, bis es ganz klemmt.\n\n"
            "Quellen: [1] Heizungswartung Protokoll.pdf, S. 2",
            (5210, 182),
        ),
        (
            "Schreib mir eine kurze Mail an den Installateur.",
            "Gern, hier ein Entwurf:\n\n"
            "> Guten Tag Herr Keller,\n>\n"
            "> bei der Wartung am 14. Mai wurde ein zu niedriger Vordruck im "
            "Ausdehnungsgefäß festgestellt (0,6 statt 1,0 bar). Können Sie vor Beginn "
            "der Heizperiode einen Termin zum Nachfüllen anbieten? Gern auch das "
            "Thermostatventil im Bad tauschen.\n>\n> Viele Grüße\n> Anna Bernau",
            (5560, 120),
        ),
    ],
    project=project,
)

chat(
    ben,
    "CSV der Stromzähler auswerten",
    qwen,
    [
        (
            "Ich habe die Zählerstände als CSV. Wie berechne ich in Python den Verbrauch je Monat?",
            "So geht es mit der Standardbibliothek, ohne pandas:\n\n"
            "```python\nimport csv\nfrom collections import defaultdict\n\n"
            'readings = []\nwith open("zaehler.csv", newline="") as f:\n'
            '    for row in csv.DictReader(f, delimiter=";"):\n'
            '        readings.append((row["datum"][:7], float(row["stand"])))\n\n'
            "per_month = defaultdict(float)\n"
            "for (_, before), (month, after) in zip(readings, readings[1:]):\n"
            "    per_month[month] += after - before\n\n"
            "for month, kwh in sorted(per_month.items()):\n"
            '    print(f"{month}: {kwh:7.1f} kWh")\n```\n\n'
            "Die Differenz zweier Ablesungen wird dem Monat der späteren Ablesung "
            "zugeschlagen. Mit dem Werkzeug **Python** kann ich das auch direkt mit "
            "deiner Datei ausführen.",
            (1840, 260),
        )
    ],
)

chat(
    lea,
    "Fotosynthese einfach erklärt",
    gemma,
    [
        (
            "Erklär mir die Fotosynthese für den Test morgen, kurz und mit Formel.",
            "Pflanzen bauen mit Licht aus **Kohlendioxid** und **Wasser** Zucker auf; "
            "dabei wird **Sauerstoff** frei.\n\n"
            "6 CO₂ + 6 H₂O → C₆H₁₂O₆ + 6 O₂\n\n"
            "1. **Lichtreaktion** (Thylakoide): Licht spaltet Wasser, es entstehen "
            "ATP, NADPH und Sauerstoff.\n"
            "2. **Calvin-Zyklus** (Stroma): mit ATP und NADPH wird CO₂ zu Zucker.\n\n"
            "Tipp aus deinem *Fotosynthese Arbeitsblatt*: Die Lichtreaktion liefert "
            "die Energie, der Calvin-Zyklus baut.",
            (2400, 210),
        )
    ],
)

chat(anna, "Linsensuppe für sechs Personen", gemma, [])
chat(anna, "Urlaubsplanung Ostsee", opus, [])
chat(anna, "Steuererklärung: Handwerkerkosten", gpt, [])

# Buchungen der letzten 30 Tage für die Verbrauchsseite.
UsageEntry.objects.all().delete()
rng = random.Random(7)
now = timezone.now()
for day in range(30):
    when = now - datetime.timedelta(days=day)
    for who, mdl, account, price in (
        (anna, sonnet, money, Decimal("0.000009")),
        (ben, qwen, local, None),
        (lea, gemma, local, None),
        (anna, gemma, local, None),
    ):
        for _ in range(rng.randint(0, 4)):
            t_in, t_out = rng.randint(800, 9000), rng.randint(80, 900)
            amount = (price * (t_in + 5 * t_out)).quantize(Decimal("0.0001")) if price else None
            UsageEntry.objects.create(
                account=account,
                user=who,
                ai_model=mdl,
                model_name=mdl.model_id,
                created=when,
                tokens_in=t_in,
                tokens_out=t_out,
                requests=1,
                amount=amount,
                currency="EUR" if amount is not None else "",
                amount_eur=amount,
            )

print("Demodaten angelegt.")
