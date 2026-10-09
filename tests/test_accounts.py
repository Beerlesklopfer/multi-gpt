import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from multigpt.accounts.models import User, UserGroup


def test_custom_user_model_active():
    assert get_user_model() is User


@pytest.mark.django_db
def test_user_group_extends_django_group():
    group = UserGroup.objects.create(name="Familie")
    user = User.objects.create_user("anna", password="geheim-123-x")
    user.groups.add(group)

    django_group = user.groups.get()
    assert isinstance(django_group, Group)
    assert django_group.usergroup == group
