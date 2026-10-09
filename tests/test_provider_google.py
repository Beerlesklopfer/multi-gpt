"""Google-Gemini-Adapter mit gemocktem HTTP (respx, M4-01)."""

import json
import logging

import httpx
import pytest
import respx

from multigpt.chat.models import Provider
from multigpt.chat.providers import (
    ChatMessage,
    Delta,
    Done,
    Error,
    ProviderError,
    ToolCallEvent,
    ToolSpec,
    Usage,
)
from multigpt.chat.providers.google import GoogleAdapter, _ChunkParser
from tests.test_provider_openai_compat import TrackingStream, sse_response

BASE = "http://gemini.test/v1beta"
KEY = "AIza-test-GEHEIM-1234567890"
MODEL = "gemini-test"
URL = f"{BASE}/models/{MODEL}:streamGenerateContent"
MESSAGES = [ChatMessage("user", "Hallo")]


def make_adapter(api_key=KEY, base_url=BASE):
    return GoogleAdapter(Provider(name="Gemini", kind="google", base_url=base_url, api_key=api_key))


def chunk(*texts, finish=None, usage=None, thought=False):
    parts = [{"text": t, **({"thought": True} if thought else {})} for t in texts]
    candidate = {"content": {"role": "model", "parts": parts}, "index": 0}
    if finish:
        candidate["finishReason"] = finish
    data = {"candidates": [candidate], "modelVersion": MODEL}
    if usage:
        data["usageMetadata"] = usage
    return data


USAGE = {"promptTokenCount": 12, "candidatesTokenCount": 7, "totalTokenCount": 19}


def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks).encode()


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


def run(adapter=None, messages=MESSAGES, model=MODEL, **kwargs):
    return list((adapter or make_adapter()).stream(model, messages, **kwargs))


def error_body(code, status, reason=None):
    err = {"code": code, "message": f"API key not valid: {KEY}", "status": status}
    if reason:
        err["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    return {"error": err}


# --- normaler Ablauf ---------------------------------------------------------


def test_normal_stream(mock):
    route = mock.post(URL).mock(
        return_value=sse_response(
            sse(
                chunk("Hal", usage={"promptTokenCount": 12}),
                chunk("lo"),
                chunk(" Welt", finish="STOP", usage=USAGE),
            )
        )
    )
    events = run(system="Sei nett.", temperature=0.5, max_tokens=200)
    assert events == [Delta("Hal"), Delta("lo"), Delta(" Welt"), Usage(12, 7), Done("stop")]

    request = route.calls.last.request
    assert request.url.params["alt"] == "sse"
    assert "key" not in request.url.params
    assert request.headers["x-goog-api-key"] == KEY
    assert "Authorization" not in request.headers
    body = json.loads(request.content)
    assert body["contents"] == [{"role": "user", "parts": [{"text": "Hallo"}]}]
    assert body["systemInstruction"] == {"parts": [{"text": "Sei nett."}]}
    assert body["generationConfig"] == {"temperature": 0.5, "maxOutputTokens": 200}
    assert "tools" not in body and "model" not in body


def test_usage_counts_thoughts_as_output(mock):
    usage = {**USAGE, "thoughtsTokenCount": 30, "toolUsePromptTokenCount": 3}
    mock.post(URL).mock(return_value=sse_response(sse(chunk("x", finish="STOP", usage=usage))))
    assert run()[-2:] == [Usage(15, 37), Done("stop")]


def test_thought_parts_ignored(mock):
    mock.post(URL).mock(
        return_value=sse_response(
            sse(chunk("Überlegung", thought=True), chunk("Antwort", finish="STOP"))
        )
    )
    assert run() == [Delta("Antwort"), Done("stop")]


def test_roles_mapped_and_merged(mock):
    route = mock.post(URL).mock(return_value=sse_response(sse(chunk("x", finish="STOP"))))
    history = [
        ChatMessage("user", "Erste"),
        ChatMessage("assistant", "Antwort"),
        ChatMessage("user", "Zweite"),
        ChatMessage("user", "Nachtrag"),
    ]
    run(messages=history)
    body = json.loads(route.calls.last.request.content)
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "Erste"}]},
        {"role": "model", "parts": [{"text": "Antwort"}]},
        {"role": "user", "parts": [{"text": "Zweite\n\nNachtrag"}]},
    ]
    assert "systemInstruction" not in body


def test_params_mapping(mock):
    route = mock.post(URL).mock(return_value=sse_response(sse(chunk("x", finish="STOP"))))
    tools = [{"name": "t", "description": "d", "parameters": {}}]
    run(
        stop="ENDE",
        top_p=0.9,
        generationConfig={"candidateCount": 1},
        safetySettings=[{"category": "x"}],
        contents=["nicht überschreiben"],
        tools=tools,
        top_k=None,
    )
    body = json.loads(route.calls.last.request.content)
    assert body["generationConfig"] == {"candidateCount": 1, "stopSequences": ["ENDE"], "topP": 0.9}
    assert body["safetySettings"] == [{"category": "x"}]
    assert body["contents"][0]["parts"][0]["text"] == "Hallo"
    assert body["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "t",
                    "description": "d",
                    "parametersJsonSchema": {"type": "object", "properties": {}},
                }
            ]
        }
    ]


def test_default_base_url_and_model_prefix(mock):
    route = mock.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:streamGenerateContent"
    ).mock(return_value=sse_response(sse(chunk("x", finish="STOP"))))
    assert run(make_adapter(base_url=""), model="models/gemini-x")[-1] == Done("stop")
    assert route.called


def test_finish_reason_other_than_stop(mock):
    mock.post(URL).mock(
        return_value=sse_response(sse(chunk("abgeschnitten", finish="MAX_TOKENS", usage=USAGE)))
    )
    assert run() == [Delta("abgeschnitten"), Usage(12, 7), Done("max_tokens")]


# --- Fehler ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "retryable", "fragment"),
    [
        (400, error_body(400, "INVALID_ARGUMENT", "API_KEY_INVALID"), False, "API-Key"),
        (400, error_body(400, "FAILED_PRECONDITION"), False, "HTTP 400"),
        (403, error_body(403, "PERMISSION_DENIED"), False, "API-Key"),
        (404, error_body(404, "NOT_FOUND"), False, "nicht gefunden"),
        (429, error_body(429, "RESOURCE_EXHAUSTED"), True, "zu viele Anfragen"),
        (500, error_body(500, "INTERNAL"), True, "Serverfehler"),
        (503, error_body(503, "UNAVAILABLE"), True, "Serverfehler"),
    ],
)
def test_http_errors(mock, caplog, status, body, retryable, fragment):
    mock.post(URL).mock(return_value=httpx.Response(status, json=body))
    with caplog.at_level(logging.DEBUG):
        events = run()
    assert len(events) == 1
    (error,) = events
    assert isinstance(error, Error)
    assert error.retryable is retryable
    assert fragment in error.message
    assert KEY not in error.message
    assert KEY not in caplog.text
    assert body["error"]["status"] in caplog.text


@pytest.mark.parametrize(
    ("exc", "fragment"),
    [
        (httpx.ConnectError("refused"), "nicht erreichbar"),
        (httpx.ConnectTimeout("timeout"), "nicht erreichbar"),
        (httpx.ReadTimeout("timeout"), "nicht rechtzeitig"),
    ],
)
def test_network_errors(mock, exc, fragment):
    mock.post(URL).mock(side_effect=exc)
    (error,) = run()
    assert isinstance(error, Error)
    assert error.retryable is True
    assert fragment in error.message


@pytest.mark.parametrize(
    ("status", "retryable"), [("UNAVAILABLE", True), ("INVALID_ARGUMENT", False)]
)
def test_error_chunk_mid_stream(mock, caplog, status, retryable):
    err = error_body(503, status)
    mock.post(URL).mock(
        return_value=sse_response(sse(chunk("Teil"), err, chunk("nie", finish="STOP")))
    )
    with caplog.at_level(logging.DEBUG):
        events = run()
    assert events[0] == Delta("Teil")
    assert len(events) == 2
    assert isinstance(events[-1], Error)
    assert events[-1].retryable is retryable
    assert KEY not in events[-1].message and KEY not in caplog.text


def test_prompt_blocked(mock):
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": USAGE}
    mock.post(URL).mock(return_value=sse_response(sse(blocked)))
    (error,) = run()
    assert isinstance(error, Error)
    assert error.retryable is False
    assert "blockiert" in error.message


def test_connection_lost_mid_stream(mock):
    stream = TrackingStream(
        [
            f"data: {json.dumps(chunk('Erster '))}\n\n".encode(),
            f"data: {json.dumps(chunk('Teil'))}\n\n".encode(),
            b"data: never",
        ],
        fail_after=2,
    )
    mock.post(URL).mock(return_value=httpx.Response(200, stream=stream))
    events = run()
    assert events[:2] == [Delta("Erster "), Delta("Teil")]
    assert len(events) == 3
    assert isinstance(events[-1], Error)
    assert events[-1].retryable is True
    assert "unterbrochen" in events[-1].message
    assert stream.closed


def test_stream_ends_without_finish_reason(mock):
    mock.post(URL).mock(return_value=sse_response(sse(chunk("halb"))))
    events = run()
    assert events[0] == Delta("halb")
    assert isinstance(events[-1], Error) and events[-1].retryable


def test_unexpected_exception_becomes_error(mock):
    mock.post(URL).mock(side_effect=RuntimeError(KEY))
    (error,) = run()
    assert isinstance(error, Error)
    assert error.retryable is False
    assert KEY not in error.message


def test_close_closes_connection(mock):
    parts = [f"data: {json.dumps(chunk(str(i)))}\n\n".encode() for i in range(100)]
    stream = TrackingStream(parts)
    mock.post(URL).mock(return_value=httpx.Response(200, stream=stream))
    gen = make_adapter().stream(MODEL, MESSAGES)
    assert next(gen) == Delta("0")
    assert next(gen) == Delta("1")
    assert not stream.closed
    gen.close()
    assert stream.closed


# --- Werkzeuge (M4a) ---------------------------------------------------------


def _function_call_lines():
    call = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"functionCall": {"name": "wetter", "args": {"ort": "Köln"}}}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": USAGE,
    }
    return [f"data: {json.dumps(chunk('Moment'))}", "", f"data: {json.dumps(call)}", ""]


def test_parser_collects_function_calls():
    parser = _ChunkParser(emit_tool_calls=True)
    events = [e for line in _function_call_lines() for e in parser.feed(line)]
    events += list(parser.end_of_stream())
    call = events[1]
    assert call.id.startswith("call_") and call.provider_state == {"id_generated": True}
    assert events == [
        Delta("Moment"),
        ToolCallEvent(
            id=call.id,
            name="wetter",
            arguments={"ort": "Köln"},
            provider_state={"id_generated": True},
        ),
        Usage(12, 7),
        Done("tool_calls"),
    ]


def test_parser_without_tools_emits_no_tool_calls():
    parser = _ChunkParser()
    events = [e for line in _function_call_lines() for e in parser.feed(line)]
    events += list(parser.end_of_stream())
    assert not any(isinstance(e, ToolCallEvent) for e in events)


# --- Modellliste und Online-Status ------------------------------------------


def models_page(names, token=None):
    page = {
        "models": [
            {"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]}
            for n in names
        ]
    }
    if token:
        page["nextPageToken"] = token
    return page


def test_list_models_paginates(mock):
    route = mock.get(f"{BASE}/models").mock(
        side_effect=[
            httpx.Response(200, json=models_page(["gemini-a", "gemini-b"], token="seite2")),
            httpx.Response(200, json=models_page(["text-embedding-x"])),
        ]
    )
    assert make_adapter().list_models() == ["gemini-a", "gemini-b", "text-embedding-x"]
    first, second = route.calls
    assert first.request.headers["x-goog-api-key"] == KEY
    assert "key" not in first.request.url.params
    assert first.request.url.params["pageSize"] == "1000"
    assert second.request.url.params["pageToken"] == "seite2"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json=error_body(400, "INVALID_ARGUMENT", "API_KEY_INVALID")),
        httpx.Response(200, text="kein json"),
        httpx.ConnectError("refused"),
    ],
)
def test_list_models_errors(mock, caplog, response):
    route = mock.get(f"{BASE}/models")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as info:
        make_adapter().list_models()
    assert KEY not in str(info.value)
    assert KEY not in caplog.text


def test_list_models_invalid_key_message(mock):
    mock.get(f"{BASE}/models").mock(
        return_value=httpx.Response(
            400, json=error_body(400, "INVALID_ARGUMENT", "API_KEY_INVALID")
        )
    )
    with pytest.raises(ProviderError, match="API-Key"):
        make_adapter().list_models()


def test_is_online_true(mock):
    mock.get(f"{BASE}/models").mock(return_value=httpx.Response(200, json=models_page(["a"])))
    assert make_adapter().is_online() is True


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(500),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("timeout"),
        RuntimeError("kaputt"),
    ],
)
def test_is_online_false(mock, outcome):
    route = mock.get(f"{BASE}/models")
    if isinstance(outcome, BaseException):
        route.mock(side_effect=outcome)
    else:
        route.mock(return_value=outcome)
    assert make_adapter().is_online(timeout=0.1) is False


WEATHER = ToolSpec(
    name="wetter",
    description="Wetter für einen Ort",
    parameters={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"ort": {"type": "string"}},
        "required": ["ort"],
        "additionalProperties": False,
    },
)


def model_chunk(*parts, finish=None, usage=None):
    candidate = {"content": {"role": "model", "parts": list(parts)}, "index": 0}
    if finish:
        candidate["finishReason"] = finish
    data = {"candidates": [candidate]}
    if usage:
        data["usageMetadata"] = usage
    return data


def fc(name, args, call_id=None, signature=None):
    call = {"name": name, "args": args}
    if call_id:
        call["id"] = call_id
    part = {"functionCall": call}
    if signature:
        part["thoughtSignature"] = signature
    return part


def test_tool_definition_and_config_in_body(mock):
    route = mock.post(URL).mock(return_value=sse_response(sse(chunk("x", finish="STOP"))))
    run(tools=[WEATHER], tool_choice="wetter")
    body = json.loads(route.calls.last.request.content)
    assert body["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "wetter",
                    "description": "Wetter für einen Ort",
                    "parametersJsonSchema": WEATHER.parameters,
                }
            ]
        }
    ]
    assert body["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["wetter"]}
    }


@pytest.mark.parametrize(
    "choice, mode", [("auto", "AUTO"), ("none", "NONE"), ("required", "ANY"), (None, None)]
)
def test_tool_choice_mapping(mock, choice, mode):
    route = mock.post(URL).mock(return_value=sse_response(sse(chunk("x", finish="STOP"))))
    run(tools=[WEATHER], tool_choice=choice)
    config = json.loads(route.calls.last.request.content).get("toolConfig")
    assert (config["functionCallingConfig"]["mode"] if config else None) == mode


def test_streamed_parallel_calls_with_signature_and_ids(mock):
    body = sse(
        model_chunk({"text": "Ich schaue "}),
        model_chunk({"text": "nach."}),
        model_chunk(
            fc("wetter", {"ort": "Köln"}, signature="SIG-1"),
            fc("wetter", {"ort": "Bonn"}, call_id="gem-2"),
        ),
        model_chunk({"text": ""}, finish="STOP", usage=USAGE),
    )
    route = mock.post(URL).mock(return_value=sse_response(body))
    events = run(tools=[WEATHER])
    first, second = events[2], events[3]
    assert events[:2] == [Delta("Ich schaue "), Delta("nach.")]
    assert first.id.startswith("call_") and first.id != second.id
    assert first == ToolCallEvent(
        first.id,
        "wetter",
        {"ort": "Köln"},
        provider_state={"id_generated": True, "thought_signature": "SIG-1"},
    )
    assert second == ToolCallEvent("gem-2", "wetter", {"ort": "Bonn"})
    done = events[-1]
    expected_parts = [
        {"text": "Ich schaue nach."},
        fc("wetter", {"ort": "Köln"}, signature="SIG-1"),
        fc("wetter", {"ort": "Bonn"}, call_id="gem-2"),
        {"text": ""},
    ]
    assert events[4:] == [
        Usage(12, 7),
        Done("tool_calls", provider_state={"parts": expected_parts}),
    ]

    # Rückweg: Modellteile wörtlich, alle Antworten in einem user-Content.
    route.mock(return_value=sse_response(sse(chunk("Sonnig", finish="STOP"))))
    history = [
        *MESSAGES,
        ChatMessage(
            "assistant",
            "Ich schaue nach.",
            tool_calls=[first, second],
            provider_state=done.provider_state,
        ),
        ChatMessage("tool", "Sonne", tool_call_id=first.id, name="wetter"),
        ChatMessage("tool", "kaputt", tool_call_id="gem-2", name="wetter", is_error=True),
    ]
    run(messages=history, tools=[WEATHER])
    contents = json.loads(route.calls.last.request.content)["contents"]
    assert contents[1] == {"role": "model", "parts": expected_parts}
    assert contents[2] == {
        "role": "user",
        "parts": [
            # selbst vergebene ID geht nicht an Gemini zurück
            {"functionResponse": {"name": "wetter", "response": {"result": "Sonne"}}},
            {
                "functionResponse": {
                    "name": "wetter",
                    "response": {"error": "kaputt"},
                    "id": "gem-2",
                }
            },
        ],
    }


def test_history_without_provider_state_uses_call_signature(mock):
    route = mock.post(URL).mock(return_value=sse_response(sse(chunk("x", finish="STOP"))))
    signed = ToolCallEvent("gem-1", "wetter", {"ort": "Köln"}, {"thought_signature": "SIG"})
    unsigned = ToolCallEvent("call_x", "wetter", {"ort": "Bonn"}, {"id_generated": True})
    history = [
        ChatMessage("user", "Frage"),
        ChatMessage("assistant", "", tool_calls=[signed]),
        ChatMessage("tool", "Sonne", tool_call_id="gem-1", name="wetter"),
        ChatMessage("assistant", "", tool_calls=[unsigned]),
        ChatMessage("tool", "Regen", tool_call_id="call_x"),
    ]
    run(messages=history, tools=[WEATHER])
    contents = json.loads(route.calls.last.request.content)["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user", "model", "user"]
    assert contents[1]["parts"] == [
        {
            "functionCall": {"name": "wetter", "args": {"ort": "Köln"}, "id": "gem-1"},
            "thoughtSignature": "SIG",
        }
    ]
    # Signatur verloren: Platzhalter laut Doku; Name aus dem Aufruf ergänzt.
    assert contents[3]["parts"] == [
        {
            "functionCall": {"name": "wetter", "args": {"ort": "Bonn"}},
            "thoughtSignature": "skip_thought_signature_validator",
        }
    ]
    assert contents[4]["parts"] == [
        {"functionResponse": {"name": "wetter", "response": {"result": "Regen"}}}
    ]


@pytest.mark.parametrize(
    "parts, finish, fragment",
    [
        ([{"functionCall": {"name": "wetter", "args": "kaputt"}}], "STOP", "ungültige Argumente"),
        ([], "MALFORMED_FUNCTION_CALL", "ungültigen Werkzeugaufruf"),
        ([], "MISSING_THOUGHT_SIGNATURE", "Signatur"),
    ],
)
def test_invalid_function_calls_become_error(mock, caplog, parts, finish, fragment):
    mock.post(URL).mock(return_value=sse_response(sse(model_chunk(*parts, finish=finish))))
    events = run(tools=[WEATHER])
    assert len(events) == 1 and isinstance(events[0], Error)
    assert fragment in events[0].message
