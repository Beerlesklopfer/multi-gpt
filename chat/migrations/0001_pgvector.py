"""Aktiviert pgvector (CREATE EXTENSION IF NOT EXISTS vector).

Die Extension ist nicht "trusted": Ohne Superuser-Rechte gelingt das nur, wenn
sie schon existiert (dann No-op). Für die Entwicklung legt `make db-create` sie
in der DB und in template1 an.
"""

from django.db import migrations
from pgvector.django import VectorExtension


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [VectorExtension()]
