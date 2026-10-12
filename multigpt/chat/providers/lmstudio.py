"""Schnittstelle „LM Studio“: ein Anbieter, eine Adresse (Server-Wurzel).

Daraus abgeleitet: Chats über ``<Wurzel>/v1`` (OpenAI-kompatibel), Laden,
Entladen, Modellliste und Herunterladen über ``<Wurzel>/api/v1``
(``chat/lmstudio.py``, ``chat/model_manager.py``). Die eigene Chat-API von LM
Studio (``/api/v1/chat``) taugt nicht für MultiGPT – geprüft 2026-10-13 gegen
LM Studio 0.4: ``tools`` wird abgelehnt („Unrecognized key(s) in object:
'tools'“), ``input`` nimmt keinen Verlauf mit Rollen (nur ``text``/``image``,
Verlauf nur über von LM Studio gespeicherte Antworten). Ohne beides fielen
Werkzeuge, Rückfragen und der von MultiGPT geführte Verlauf weg.
"""

from __future__ import annotations

from multigpt.chat.lmstudio import DEFAULT_ROOT, api_root

from .openai_compat import OpenAICompatAdapter

DEFAULT_BASE_URL = DEFAULT_ROOT


class LMStudioAdapter(OpenAICompatAdapter):
    @property
    def base_url(self) -> str:
        return f"{api_root(self.provider)}/v1"
