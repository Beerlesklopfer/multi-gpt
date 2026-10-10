"""Darstellung im Chat: Schriftgrößen-Knöpfe, ausblendbare Seitenleiste."""

import pytest
from django.urls import reverse

from multigpt.accounts.models import Role, User
from multigpt.chat.models import AIModel, Conversation, Message, Provider

pytestmark = pytest.mark.django_db


@pytest.fixture
def page(client):
    user = User.objects.create_user(
        "jo", password="Geheim-Test-1234", role=Role.objects.get(key="adult")
    )
    client.force_login(user)
    provider = Provider.objects.create(name="LM", kind="openai_compat")
    model = AIModel.objects.create(provider=provider, model_id="m", display_name="M")
    conversation = Conversation.objects.create(user=user, title="T")
    question = Message.objects.create(conversation=conversation, role="user", content="Frage")
    answer = Message.objects.create(
        conversation=conversation,
        role="assistant",
        content="Antwort",
        parent=question,
        model=model,
        status="done",
    )
    Conversation.objects.filter(pk=conversation.pk).update(current_leaf=answer)
    return client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()


def test_font_size_buttons_only_under_answers(page):
    assert page.count('data-font-size="-1"') == 1
    assert page.count('data-font-size="1"') == 1
    assert 'aria-label="Schrift vergrößern"' in page


def test_sidebar_toggle_present(page):
    assert 'id="sidebar-toggle"' in page
    assert 'aria-controls="sidebar"' in page


def _css_rule(css: str, selector: str) -> str:
    """Rumpf der ersten Regel, die genau mit ``selector`` beginnt."""
    start = css.index("\n" + selector + " {")
    return css[start : css.index("}", start)]


def test_chat_history_is_containing_block():
    """Absolut positionierte Elemente im Verlauf (.sr-only, KaTeX-MathML) müssen
    im Scrollbereich des Verlaufs bleiben. Sonst ragen sie unter das Fenster,
    .main-area wird scrollbar und das Eingabefeld rutscht nach oben."""
    from pathlib import Path

    css = (Path(__file__).parents[1] / "multigpt/chat/static/chat/app.css").read_text()
    rule = _css_rule(css, ".chat-history")
    assert "overflow-y: auto" in rule
    assert "position: relative" in rule
