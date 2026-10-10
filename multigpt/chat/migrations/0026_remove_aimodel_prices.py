"""Alte Preisfelder entfallen; Preise stehen jetzt in ``billing.ModelPrice``
(übernommen in 0025_provider_billing_account). Eigene Migration, damit das
Entfernen der Spalten nicht in derselben Transaktion wie die Datenmigration läuft."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0025_provider_billing_account"),
    ]

    operations = [
        migrations.RemoveField(model_name="aimodel", name="price_in"),
        migrations.RemoveField(model_name="aimodel", name="price_out"),
    ]
