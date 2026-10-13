"""Denktiefe bei LM Studio: Stufen aus der Modellliste, Vorgabe je Modell."""

import pytest

from multigpt.accounts.models import Role, User
from multigpt.chat import capabilities, lmstudio, reasoning
from multigpt.chat.models import AIModel, ChatSettings, Conversation, ModelRuntime, Provider

pytestmark = pytest.mark.django_db


def test_levels_from_lm_studio_options():
    assert capabilities.lmstudio_levels(["off", "low", "medium", "xhigh", "on"]) == (
        "off",
        "low",
        "medium",
        "xhigh",
    )
    assert capabilities.lmstudio_levels(["off", "on"]) == ("off", "medium")  # Gemma 4
    assert capabilities.lmstudio_levels(["low", "medium", "high"]) == ("low", "medium", "high")
    assert capabilities.lmstudio_levels([]) is None
    # „Aus“ geht als none (off/on lehnt LM Studio über /v1 ab).
    assert capabilities.lmstudio_params("off", ("off", "medium")) == {"reasoning_effort": "none"}
    assert capabilities.lmstudio_params("high", ("off", "medium")) == {"reasoning_effort": "medium"}


def test_catalog_parses_reasoning():
    data = {
        "models": [
            {
                "key": "qwen/qwen3.8-27b",
                "capabilities": {
                    "vision": True,
                    "reasoning": {"allowed_options": ["off", "low", "xhigh"], "default": "xhigh"},
                },
            }
        ]
    }
    (entry,) = lmstudio.parse_models(data)
    assert entry.reasoning_options == ("off", "low", "xhigh")
    assert entry.reasoning_default == "xhigh"


@pytest.fixture
def qwen():
    provider = Provider.objects.create(
        name="LM Studio", kind=Provider.Kind.LMSTUDIO, base_url="http://pc:1234"
    )
    model = AIModel.objects.create(
        provider=provider, model_id="qwen/qwen3.8-27b", display_name="Qwen3.8"
    )
    ModelRuntime.objects.create(ai_model=model, reasoning_options=["off", "low", "medium", "xhigh"])
    return model


def test_model_default_applies_without_chat_choice(qwen):
    role = Role.objects.get(key="adult")
    user = User.objects.create_user("anna", password="x-Geheim-123", role=role)
    conversation = Conversation.objects.create(user=user)
    assert reasoning.params_for(qwen, reasoning.effective(conversation, qwen)) == {}
    qwen.default_reasoning_effort = "low"
    qwen.save()
    effort = reasoning.effective(conversation, qwen)
    assert reasoning.params_for(qwen, effort) == {"reasoning_effort": "low"}
    # Vorgabe des Modells vor der allgemeinen Einstellung, Wahl im Chat davor.
    settings = ChatSettings.load()
    settings.default_reasoning_effort = "high"
    settings.save()
    assert reasoning.effective(conversation, qwen) == "low"
    conversation.reasoning_effort = "off"
    assert reasoning.params_for(qwen, reasoning.effective(conversation, qwen)) == {
        "reasoning_effort": "none"
    }
    assert reasoning.levels_for(qwen) == ["off", "low", "medium", "xhigh"]
