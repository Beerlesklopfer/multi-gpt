"""Hilfsfunktionen des Chat-Ablaufs (services.py)."""

from decimal import Decimal

import pytest

from multigpt.accounts.models import Role, User
from multigpt.chat import services
from multigpt.chat.models import AIModel, Conversation, Message, Provider

pytestmark = pytest.mark.django_db


@pytest.fixture
def provider():
    return Provider.objects.create(name="Cloud", kind=Provider.Kind.OPENAI_COMPAT)


def make_model(provider, **kw):
    return AIModel.objects.create(provider=provider, model_id="m", display_name="M", **kw)


def test_cost_with_prices(provider):
    m = make_model(provider, price_in=Decimal("3"), price_out=Decimal("15"))
    assert services.compute_cost(m, 1_000_000, 0) == Decimal("3")
    assert services.compute_cost(m, 1000, 2000) == Decimal("0.033")


def test_cost_without_prices_is_unknown(provider):
    assert services.compute_cost(make_model(provider), 10, 10) is None


def test_cost_only_one_price(provider):
    m = make_model(provider, price_out=Decimal("1"))
    assert services.compute_cost(m, 500, 1_000_000) == Decimal("1")


def test_cost_local_is_zero():
    local = Provider.objects.create(name="LM", kind="openai_compat", is_local=True)
    m = make_model(local, price_in=Decimal("5"), price_out=Decimal("5"))
    assert services.compute_cost(m, 10**6, 10**6) == Decimal(0)


def test_system_prompt_order():
    role = Role.objects.get(key="teen")
    user = User.objects.create_user("t", role=role)
    conv = Conversation.objects.create(user=user, system_prompt="  Chat  ")
    assert services.build_system_prompt(user, conv) == (
        f"{role.fixed_system_prompt.strip()}\n\nChat"
    )


def test_system_prompt_none_when_empty():
    user = User.objects.create_user("a", role=Role.objects.get(key="adult"))
    conv = Conversation.objects.create(user=user)
    assert services.build_system_prompt(user, conv) is None


def test_history_filters():
    user = User.objects.create_user("a", role=Role.objects.get(key="adult"))
    conv = Conversation.objects.create(user=user)
    keep = Message.objects.create(conversation=conv, role="user", content="frage")
    Message.objects.create(conversation=conv, role="assistant", content="", status="aborted")
    Message.objects.create(conversation=conv, role="assistant", content="x", status="error")
    Message.objects.create(conversation=conv, role="system", content="sys")
    Message.objects.create(conversation=conv, role="assistant", content="alt", status="superseded")
    teil = Message.objects.create(
        conversation=conv, role="assistant", content="teil", status="aborted"
    )
    history = services.build_history(conv)
    assert [(m.role, m.content) for m in history] == [("user", "frage"), ("assistant", "teil")]
    assert len(services.build_history(conv, exclude_ids=[teil.pk])) == 1
    assert keep.pk


def test_title_truncated():
    title = services._title_from("Erste Zeile " + "x" * 100 + "\nzweite")
    assert len(title) == services.TITLE_LENGTH
    assert title.endswith("…")


def test_visible_messages_hide_superseded():
    user = User.objects.create_user("a", role=Role.objects.get(key="adult"))
    conv = Conversation.objects.create(user=user)
    Message.objects.create(conversation=conv, role="user", content="q")
    Message.objects.create(conversation=conv, role="assistant", content="alt", status="superseded")
    Message.objects.create(conversation=conv, role="assistant", content="neu")
    Message.objects.create(conversation=conv, role="system", content="sys")
    assert [m.content for m in services.visible_messages(conv)] == ["q", "neu"]
