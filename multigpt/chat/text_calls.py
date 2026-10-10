"""Werkzeugaufruf als Text erkennen (Antwort besteht nur aus dem Aufruf).

Manche Modelle schreiben einen Aufruf als Antworttext, statt ihn als
``tool_call`` zu liefern, z. B. ``generate_image("Bundesadler …")``, als JSON
``{"name": "generate_image", "arguments": {...}}`` oder in ``<tool_call>``-Tags
(Qwen/Hermes), wenn LM Studio das Format nicht erkennt oder das Modell gar
keine Werkzeuge kann (olmOCR). Die Oberfläche zeigt dann einen Hinweis mit dem
Knopf „Ausführen“, der den Aufruf **nur vorbelegt** (Modus „Bild“ bzw.
Websuche mit Text im Eingabefeld). Gesendet wird erst, wenn der Nutzer sendet.

Sicherheit: Der Text stammt vom Modell und ist nicht vertrauenswürdig. Deshalb
werden nur die eingebauten Werkzeuge in ``TOOLS`` erkannt, nie MCP-Werkzeuge
(Prompt-Injection könnte sonst Aktionen auslösen), und nie automatisch
ausgeführt. Erkannt wird nur, was im Wesentlichen *allein* in der Antwort steht
(höchstens Codeblock- bzw. ``<tool_call>``-Hülle); ein Aufruf mitten im Text
bleibt Text.
"""

from __future__ import annotations

import ast
import json
import re

# Eingebautes Werkzeug -> Name des Text-Arguments.
TOOLS = {"generate_image": "prompt", "web_search": "query"}
MAX_TEXT = 4000

_FENCE = re.compile(r"^```[\w-]*[ \t]*\n(?P<body>.*?)\n?```$", re.DOTALL)
_TAG = re.compile(r"^<(?P<tag>tool_call|function_call)>(?P<body>.*?)</(?P=tag)>$", re.DOTALL)
_CALL = re.compile(
    r"^(?P<name>[a-z_]+)\s*\(\s*(?:(?P<key>[a-z_]+)\s*=\s*)?"
    r"(?P<literal>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')\s*\)\s*;?$",
    re.DOTALL,
)


def _unwrap(text: str) -> str:
    """Codeblock- und ``<tool_call>``-Hüllen entfernen (auch verschachtelt)."""
    for _ in range(3):
        stripped = text.strip()
        match = _FENCE.match(stripped) or _TAG.match(stripped)
        if not match:
            return stripped
        text = match.group("body")
    return text.strip()


def _result(name, argument) -> dict | None:
    if name not in TOOLS or not isinstance(argument, str):
        return None
    argument = argument.strip()
    if not argument or len(argument) > MAX_TEXT:
        return None
    return {"tool": name, "argument": argument}


def _from_call(text: str) -> dict | None:
    match = _CALL.match(text)
    if not match:
        return None
    name, key = match.group("name"), match.group("key")
    if key is not None and key != TOOLS.get(name):
        return None
    try:
        value = ast.literal_eval(match.group("literal"))  # nur ein String-Literal
    except (ValueError, SyntaxError):
        return None
    return _result(name, value)


def _from_json(text: str) -> dict | None:
    if not text.startswith(("{", "[")):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("function"), dict):  # Form der OpenAI-API
        data = data["function"]
    name = data.get("name")
    args = data.get("arguments", data.get("parameters"))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict) or name not in TOOLS:
        return None
    return _result(name, args.get(TOOLS[name]))


def detect(content: str) -> dict | None:
    """``{"tool": …, "argument": …}``, wenn ``content`` nur ein Textaufruf eines
    eingebauten Werkzeugs ist, sonst ``None``."""
    if not content or len(content) > MAX_TEXT + 500:
        return None
    text = _unwrap(content)
    return _from_call(text) or _from_json(text)
