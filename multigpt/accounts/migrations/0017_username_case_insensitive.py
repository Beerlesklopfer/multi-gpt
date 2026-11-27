"""Anmeldenamen ohne Unterschied von Groß- und Kleinschreibung (Nutzer, 2026-11-27).

Bevor ``lower(username)`` eindeutig wird, prüft die Migration, ob es schon Konten gibt, die
sich nur in der Schreibweise unterscheiden. Dann bricht sie mit den Namen ab; ein Verwalter
benennt eines der Konten um (Admin oder ``mgpt-ctl shell``) und migriert erneut.
"""

import django.db.models.functions.text
from django.db import migrations, models

import multigpt.accounts.models


def check_duplicates(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    seen: dict[str, list[str]] = {}
    for name in User.objects.values_list("username", flat=True):
        seen.setdefault(name.lower(), []).append(name)
    clashes = [names for names in seen.values() if len(names) > 1]
    if clashes:
        listed = "; ".join(", ".join(sorted(names)) for names in clashes)
        raise RuntimeError(
            "Anmeldenamen, die sich nur in Groß-/Kleinschreibung unterscheiden: "
            f"{listed}. Bitte eines der Konten umbenennen und erneut migrieren."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0016_role_runner_data"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(check_duplicates, migrations.RunPython.noop),
        migrations.AlterModelManagers(
            name="user",
            managers=[
                ("objects", multigpt.accounts.models.UserManager()),
            ],
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("username"),
                name="accounts_user_username_ci_unique",
                violation_error_message="Ein Konto mit diesem Anmeldenamen gibt es schon "
                "(Groß- und Kleinschreibung zählen nicht).",
            ),
        ),
    ]
