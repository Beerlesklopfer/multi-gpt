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
    services.append_message(conv, role="user", content="frage")
    services.append_message(conv, role="assistant", content="", status="aborted")
    services.append_message(conv, role="assistant", content="x", status="error")
    Message.objects.create(conversation=conv, role="system", content="sys")
    teil = services.append_message(conv, role="assistant", content="teil", status="aborted")
    history = services.build_history(conv)
    assert [(m.role, m.content) for m in history] == [("user", "frage"), ("assistant", "teil")]
    assert len(services.build_history(conv, exclude_ids=[teil.pk])) == 1


def test_title_truncated():
    title = services._title_from("Erste Zeile " + "x" * 100 + "\nzweite")
    assert len(title) == services.TITLE_LENGTH
    assert title.endswith("…")


def tree_conv(username="a"):
    """Frage q mit zwei Antwortversionen; Folgefrage nur an der zweiten.

    q ─┬─ a_old
       └─ a_new ─ f
    """
    user = User.objects.create_user(username, role=Role.objects.get(key="adult"))
    conv = Conversation.objects.create(user=user)
    q = services.append_message(conv, role="user", content="q")
    a_old = services.append_message(conv, role="assistant", content="alt")
    a_new = services.append_message(conv, parent=q, role="assistant", content="neu")
    Message.objects.create(conversation=conv, role="system", content="sys")
    f = services.append_message(conv, role="user", content="f")
    return conv, q, a_old, a_new, f


def test_visible_messages_is_path_with_siblings():
    conv, q, a_old, a_new, f = tree_conv()
    path = services.visible_messages(conv)
    assert [m.content for m in path] == ["q", "neu", "f"]
    assert [(m.sibling_ids, m.sibling_index, m.sibling_count) for m in path] == [
        ([q.pk], 0, 1),
        ([a_old.pk, a_new.pk], 1, 2),
        ([f.pk], 0, 1),
    ]
    assert path[1].parent_id == q.pk


def test_visible_messages_query_count(django_assert_max_num_queries):
    conv, *_ = tree_conv()
    for _ in range(5):
        services.append_message(conv, role="assistant", content="x")
    with django_assert_max_num_queries(6):
        path = services.visible_messages(conv)
        [list(m.tool_calls.all()) for m in path]


def test_switch_branch_picks_newest_leaf_and_keeps_updated():
    conv, q, a_old, a_new, f = tree_conv()
    conv.refresh_from_db()
    updated = conv.updated
    services.switch_branch(conv, a_old.pk)
    assert [m.content for m in services.visible_messages(conv)] == ["q", "alt"]
    assert [m.content for m in services.build_history(conv)] == ["q", "alt"]
    # Zurück auf die neue Antwort: neuestes Blatt darunter ist die Folgefrage.
    services.switch_branch(conv, a_new.pk)
    conv.refresh_from_db()
    assert conv.current_leaf_id == f.pk and conv.updated == updated
    # Wurzel: neuestes Blatt im ganzen Baum.
    services.switch_branch(conv, q.pk)
    conv.refresh_from_db()
    assert conv.current_leaf_id == f.pk


def test_switch_branch_unknown_message():
    conv, *_ = tree_conv()
    _, foreign_q, *_ = tree_conv("b")
    with pytest.raises(services.TurnError):
        services.switch_branch(conv, foreign_q.pk)  # Nachricht eines anderen Chats
    with pytest.raises(services.TurnError):
        services.switch_branch(conv, "1")


def test_missing_current_leaf_falls_back_to_newest():
    conv, q, a_old, a_new, f = tree_conv()
    Conversation.objects.filter(pk=conv.pk).update(current_leaf=None)
    assert [m.content for m in services.visible_messages(conv)] == ["q", "neu", "f"]
