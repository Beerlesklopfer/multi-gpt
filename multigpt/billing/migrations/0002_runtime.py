# M14-11: Zeitkontingent „Laufzeit“ (Container-Minuten des Runners).

from django.db import migrations, models

RUNTIME_ACCOUNT_NAME = "Runner-Laufzeit"
# Startwert Erwachsene (Entscheidung 8f): 600 Minuten im Monat; übrige Rollen
# ohne Eintrag = unbegrenzt (ohne Recht „Runner nutzen“ ohnehin gesperrt).
START_MINUTES = {"adult": 600}


def create_runtime_account(apps, schema_editor):
    BillingAccount = apps.get_model("billing", "BillingAccount")
    AccountBudget = apps.get_model("billing", "AccountBudget")
    Role = apps.get_model("accounts", "Role")
    if BillingAccount.objects.filter(kind="runtime").exists():
        return
    name = RUNTIME_ACCOUNT_NAME
    if BillingAccount.objects.filter(name=name).exists():
        name = f"{RUNTIME_ACCOUNT_NAME} (Container)"
    account = BillingAccount.objects.create(
        name=name,
        kind="runtime",
        currency="",
        note="Container-Minuten des Runners (M14). Kontingent je Rolle bzw. Person.",
    )
    for key, minutes in START_MINUTES.items():
        role = Role.objects.filter(key=key).first()
        if role is not None:
            AccountBudget.objects.get_or_create(
                account=account, role=role, defaults={"monthly_minutes": minutes}
            )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_initial_roles_and_family"),
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="accountbudget",
            name="monthly_minutes",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Nur Laufzeitkonten: Container-Minuten des Runners je Kalendermonat. "
                "Leer = unbegrenzt.",
                null=True,
                verbose_name="Monatskontingent (Minuten)",
            ),
        ),
        migrations.AlterField(
            model_name="billingaccount",
            name="kind",
            field=models.CharField(
                choices=[
                    ("monetary", "monetär (Preise je Token)"),
                    ("tokens", "nur Tokens (z. B. lokal)"),
                    ("flat", "Pauschale/Abo (nur gezählt)"),
                    ("runtime", "Laufzeit (Container-Minuten)"),
                ],
                default="monetary",
                max_length=10,
                verbose_name="Art",
            ),
        ),
        migrations.AlterField(
            model_name="modelprice",
            name="unit_prices",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='JSON, z. B. {"web_search": 0.01, "image:high": 0.17}. Schlüssel: '
                "web_search, web_fetch, image, audio_minute, tts_characters, request, "
                "runtime_minute",
                verbose_name="Gebühren je Einheit",
            ),
        ),
        migrations.AlterField(
            model_name="usageentry",
            name="kind",
            field=models.CharField(
                choices=[
                    ("answer", "Antwort"),
                    ("attachment", "Datei/Anhang"),
                    ("tool", "Werkzeuggebühr"),
                    ("runtime", "Container-Laufzeit"),
                ],
                default="answer",
                max_length=12,
                verbose_name="Art",
            ),
        ),
        migrations.RunPython(create_runtime_account, migrations.RunPython.noop),
    ]
