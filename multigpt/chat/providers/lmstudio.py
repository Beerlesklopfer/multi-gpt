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

from multigpt.chat import capabilities, lmstudio
from multigpt.chat.lmstudio import DEFAULT_ROOT, api_root

from .openai_compat import OpenAICompatAdapter

DEFAULT_BASE_URL = DEFAULT_ROOT


class LMStudioAdapter(OpenAICompatAdapter):
    @property
    def base_url(self) -> str:
        return f"{api_root(self.provider)}/v1"

    def local_report(self, timeout: float = 2):
        """Fähigkeiten und Kontextlängen: bei LM Studio aus ``/api/v0/models``, bei Eliezer
        (kennt ``/api/v0`` nicht) aus ``/api/v1/models`` mit Typ llm, embedding oder image."""
        report = super().local_report(timeout)
        if report[0]:
            return report
        try:
            catalog = lmstudio.list_models(self.provider, timeout)
        except Exception:
            return report
        if lmstudio.server_kind(self.provider) != "eliezer":
            return report
        detected, contexts = {}, {}
        for entry in catalog:
            detected[entry.key] = capabilities.from_catalog(entry)
            for inst in entry.instances:
                if inst.context_length:
                    contexts[entry.key] = inst.context_length
        return detected, contexts
