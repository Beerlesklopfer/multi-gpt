import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from konten.models import Gruppe, User


def test_eigenes_user_modell_aktiv():
    assert get_user_model() is User


@pytest.mark.django_db
def test_gruppe_erweitert_django_group():
    gruppe = Gruppe.objects.create(name="Familie")
    konto = User.objects.create_user("anna", password="geheim-123-x")
    konto.groups.add(gruppe)

    django_gruppe = konto.groups.get()
    assert isinstance(django_gruppe, Group)
    assert django_gruppe.gruppe == gruppe
