"""
LLM client — wraps the OpenAI-compatible API (vLLM, OpenAI, etc.).

Provides ``chat`` (non-streaming), ``chat_stream`` (SSE streaming),
``health``, and ``warmup`` methods.  All response data is normalised so
callers never deal with provider-specific quirks.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

log = logging.getLogger("llm_client")


class LLMClient:
    """Async client for an OpenAI-compatible chat API (vLLM, OpenAI, etc.)."""

    def __init__(self, config: dict) -> None:
        base = (config.get("base_url") or "http://127.0.0.1:8800/v1").rstrip("/")
        # Ensure base ends with /v1
        if not base.endswith("/v1"):
            base = base.rstrip("/") + "/v1"
        self._base_url = base
        self._chat_url = f"{self._base_url}/chat/completions"
        self._health_url = base.rsplit("/v1", 1)[0] + "/health"
        self._models_url = f"{self._base_url}/models"

        self.model: str = config.get("model", "meta-llama/Llama-3.3-70B-Instruct")
        self.default_temperature: float = float(config.get("temperature", 0.8))
        self.default_max_tokens: int = int(config.get("max_tokens", 4096))

        timeout = float(config.get("request_timeout_s", 360))
        api_key = config.get("api_key", "")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self._http = httpx.AsyncClient(timeout=timeout, headers=headers)

    # ------------------------------------------------------------------
    #  Non-streaming chat
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        enable_thinking: bool | None = None,
    ) -> dict:
        """Non-streaming chat completion.

        Returns a normalised dict::

            {"content": str, "tool_calls": list[dict] | None, "role": "assistant"}

        Tool call arguments are always parsed to ``dict`` (OpenAI returns them
        as JSON strings).

        enable_thinking: Qwen3 hybrid thinking control.
            True  — force ``<think>`` block (deep reasoning).
            False — suppress thinking (fast path).
            None  — let the model decide (default).
        """
        body: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature if temperature is not None else self.default_temperature,
            "max_tokens": max_tokens or self.default_max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if enable_thinking is not None:
            body["chat_template_kwargs"] = {"enable_thinking": enable_thinking}

        resp = await self._http.post(self._chat_url, json=body)
        if resp.status_code != 200:
            log.error("LLM HTTP %s: %s", resp.status_code, resp.text[:2000])
            return {"content": "", "tool_calls": None, "role": "assistant",
                    "usage": {}, "finish_reason": None, "_error": resp.text[:2000]}

        data = resp.json()
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        return {
            "content": msg.get("content") or "",
            "tool_calls": self._normalize_tool_calls(msg.get("tool_calls")),
            "role": "assistant",
            "usage": data.get("usage", {}),
            "finish_reason": choice.get("finish_reason"),
        }

    # ------------------------------------------------------------------
    #  Streaming chat
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        enable_thinking: bool | None = None,
    ) -> AsyncIterator[dict]:
        """Streaming chat completion via SSE.

        Yields normalised chunks::

            {"content": str, "done": bool, "tool_calls": list[dict] | None}

        The caller feeds ``content`` into the ``_StreamingSegmentParser``
        exactly as it did with raw Ollama token strings.

        enable_thinking: Qwen3 hybrid thinking control (see ``chat()``).
        """
        body: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature if temperature is not None else self.default_temperature,
            "max_tokens": max_tokens or self.default_max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if enable_thinking is not None:
            body["chat_template_kwargs"] = {"enable_thinking": enable_thinking}

        async with self._http.stream("POST", self._chat_url, json=body) as resp:
            if resp.status_code != 200:
                body_text = ""
                async for chunk in resp.aiter_text():
                    body_text += chunk
                    if len(body_text) > 2000:
                        break
                log.error("LLM stream HTTP %s: %s", resp.status_code, body_text[:2000])
                yield {"content": "", "done": True, "tool_calls": None, "usage": {}, "finish_reason": None}
                return

            # Accumulate tool call deltas and usage across chunks
            tool_call_parts: dict[int, dict] = {}
            _usage: dict = {}

            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue

                # SSE sentinel — stream is done
                if line == "data: [DONE]":
                    tc = self._assemble_tool_calls(tool_call_parts) if tool_call_parts else None
                    yield {"content": "", "done": True, "tool_calls": tc, "usage": _usage, "finish_reason": None}
                    return

                if not line.startswith("data: "):
                    continue

                try:
                    chunk_data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                # Capture usage from the final chunk (vLLM stream_options)
                if chunk_data.get("usage"):
                    _usage = chunk_data["usage"]

                choice = (chunk_data.get("choices") or [{}])[0]
                delta = choice.get("delta", {})
                finish = choice.get("finish_reason")

                # Accumulate streamed tool call fragments
                if delta.get("tool_calls"):
                    for tc_delta in delta["tool_calls"]:
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_call_parts:
                            tool_call_parts[idx] = {
                                "id": tc_delta.get("id", f"call_{idx}"),
                                "type": "function",
                                "function": {
                                    "name": "",
                                    "arguments": "",
                                },
                            }
                        if tc_delta.get("id"):
                            tool_call_parts[idx]["id"] = tc_delta["id"]
                        fn = tc_delta.get("function", {})
                        if fn.get("name"):
                            tool_call_parts[idx]["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            tool_call_parts[idx]["function"]["arguments"] += fn["arguments"]

                content = delta.get("content") or ""
                if content:
                    yield {"content": content, "done": False, "tool_calls": None}

                if finish:
                    tc = self._assemble_tool_calls(tool_call_parts) if tool_call_parts else None
                    yield {"content": "", "done": True, "tool_calls": tc, "usage": _usage, "finish_reason": finish}
                    return

        # If we exit the context manager without a done/finish, signal done
        tc = self._assemble_tool_calls(tool_call_parts) if tool_call_parts else None
        yield {"content": "", "done": True, "tool_calls": tc, "usage": _usage, "finish_reason": None}

    # ------------------------------------------------------------------
    #  Health & warmup
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        """Return True if the LLM backend is reachable."""
        try:
            r = await self._http.get(self._health_url, timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    async def warmup(self) -> None:
        """Send a minimal request to pre-load model weights."""
        try:
            await self.chat(
                [{"role": "user", "content": "hi"}],
                temperature=0,
                max_tokens=1,
            )
            log.info("LLM warmup OK (model: %s)", self.model)
        except Exception as exc:
            log.warning("LLM warmup failed: %s", exc)

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    #  Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_tool_calls(raw: list[dict] | None) -> list[dict] | None:
        """Normalise OpenAI tool_calls: parse ``arguments`` JSON strings to dicts."""
        if not raw:
            return None
        result = []
        for i, tc in enumerate(raw):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {}
            result.append({
                "id": tc.get("id", f"call_{i}"),
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": args},
            })
        return result or None

    @staticmethod
    def _assemble_tool_calls(parts: dict[int, dict]) -> list[dict] | None:
        """Assemble streamed tool call fragments into complete tool calls."""
        if not parts:
            return None
        result = []
        for idx in sorted(parts):
            tc = parts[idx]
            fn = tc.get("function", {})
            args = fn.get("arguments", "")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {}
            result.append({
                "id": tc.get("id", f"call_{idx}"),
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": args},
            })
        return result or None
