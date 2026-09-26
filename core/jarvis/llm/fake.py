"""A deterministic stand-in model for tests, CI and offline development.

It never calls the network. What it does:
  * `/tool NAME {json args}` in the latest user message makes it call that
    tool, then answer with a short summary of the tool's result.
  * When the agent expects structured output, it returns the smallest object
    that satisfies the output schema.
  * Anything else gets a polite streamed echo.

It is only available when JARVIS_ALLOW_FAKE_LLM=1, and never in production.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel


def _last_user_text(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, UserPromptPart):
                    content = part.content
                    return content if isinstance(content, str) else str(content)
    return ""


def _pending_tool_return(messages: list[ModelMessage]) -> ToolReturnPart | None:
    if not messages:
        return None
    last = messages[-1]
    if isinstance(last, ModelRequest):
        for part in last.parts:
            if isinstance(part, ToolReturnPart):
                return part
    return None


def _minimal(schema: dict[str, Any], defs: dict[str, Any]) -> Any:
    if "$ref" in schema:
        return _minimal(defs[schema["$ref"].split("/")[-1]], defs)
    if "anyOf" in schema:
        options = [o for o in schema["anyOf"] if o.get("type") != "null"]
        return _minimal(options[0], defs) if options else None
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):  # a choice: the first allowed value
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        return {k: _minimal(v, defs) for k, v in props.items() if k in schema.get("required", [])}
    return {"array": [], "string": "", "integer": 0, "number": 0, "boolean": False}.get(str(kind))


def _pending_retry(messages: list[ModelMessage]) -> RetryPromptPart | None:
    if messages and isinstance(messages[-1], ModelRequest):
        for part in messages[-1].parts:
            if isinstance(part, RetryPromptPart):
                return part
    return None


def _decide(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, str, dict[str, Any]]:
    """Return ("text", text, {}) or ("tool", name, args)."""
    retry = _pending_retry(messages)
    if retry is not None and info.allow_text_output:
        detail = retry.content if isinstance(retry.content, str) else "invalid arguments"
        return "text", f"I couldn't do that: {detail}"[:300], {}
    tool_return = _pending_tool_return(messages)
    if tool_return is not None:
        if info.output_tools and not info.allow_text_output:
            out = info.output_tools[0]
            schema = out.parameters_json_schema
            return "tool", out.name, _minimal(schema, schema.get("$defs", {}))
        content = tool_return.content
        text = content if isinstance(content, str) else json.dumps(content, default=str)
        return "text", f"Done. ({tool_return.tool_name}: {text[:200]})", {}
    user = _last_user_text(messages).strip()
    if user.startswith("/tool "):
        name, _, raw = user[len("/tool ") :].partition(" ")
        known = {t.name for t in info.function_tools}
        if name in known:
            return "tool", name, json.loads(raw) if raw.strip() else {}
    if info.output_tools and not info.allow_text_output:
        out = info.output_tools[0]
        schema = out.parameters_json_schema
        return "tool", out.name, _minimal(schema, schema.get("$defs", {}))
    preview = user[:280] if user else "(nothing)"
    return "text", f"Understood. You said: {preview}", {}


def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    mode, value, args = _decide(messages, info)
    if mode == "tool":
        return ModelResponse(parts=[ToolCallPart(tool_name=value, args=args)])
    return ModelResponse(parts=[TextPart(value)])


async def _stream(
    messages: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str | DeltaToolCalls]:
    mode, value, args = _decide(messages, info)
    if mode == "tool":
        yield {0: DeltaToolCall(name=value, json_args=json.dumps(args))}
        return
    for word in value.split(" "):
        yield word + " "


def fake_model(model_name: str = "fake") -> FunctionModel:
    return FunctionModel(_respond, stream_function=_stream, model_name=model_name)
