"""
LLM client — wraps the OpenAI-compatible API (vLLM, OpenAI, etc.).

Provides ``chat`` (non-streaming), ``chat_stream`` (SSE streaming),
``health``, and ``warmup`` methods.  All response data is normalised so
callers never deal with provider-specific quirks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import AsyncIterator

import httpx

log = logging.getLogger("llm_client")


class CircuitOpenError(Exception):
    """Raised when the LLM circuit breaker is open (backend unhealthy)."""


class _LLMThrottle:
    """Isolation guard for Mocha's calls to the SHARED opus-trading vLLM.

    Mocha reuses Corvus's inference server, so a Mocha bug (runaway tool loop,
    retry storm) must never be able to flood it and starve live trading. This
    bounds Mocha's worst-case load to a tiny, predictable slice:

      • semaphore   — at most ``max_concurrency`` in-flight requests (default 1,
                      i.e. strictly serial).
      • token bucket — a hard requests/minute ceiling so even fast requests
                      can't burst indefinitely.
      • circuit breaker — after ``fail_threshold`` consecutive backend failures
                      (timeouts / 5xx / connection errors), open for
                      ``cooldown_s`` and fail Mocha's calls fast instead of
                      retry-storming the shared endpoint.
    """

    def __init__(self, *, max_concurrency: int, rate_per_min: float,
                 burst: float, fail_threshold: int, cooldown_s: float) -> None:
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        self._rate = max(0.0, rate_per_min) / 60.0  # tokens/sec
        self._burst = max(1.0, float(burst))
        self._tokens = self._burst
        self._last_refill = time.monotonic()
        self._tb_lock = asyncio.Lock()
        self._fail_threshold = max(1, fail_threshold)
        self._cooldown = max(1.0, cooldown_s)
        self._fails = 0
        self._open = False
        self._opened_at = 0.0

    def _check_breaker(self) -> None:
        if self._open and (time.monotonic() - self._opened_at) < self._cooldown:
            raise CircuitOpenError(
                f"LLM circuit open ({self._fails} consecutive failures); "
                f"backing off ~{self._cooldown:.0f}s to protect shared vLLM"
            )
        # closed, or open-but-cooldown-elapsed (half-open trial) → allow

    async def _take_token(self) -> None:
        if self._rate <= 0:
            return
        async with self._tb_lock:
            now = time.monotonic()
            self._tokens = min(self._burst,
                               self._tokens + (now - self._last_refill) * self._rate)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) / self._rate
        # Pace (not flood). Concurrency is already bounded, so at most a few
        # waiters ever queue here.
        await asyncio.sleep(min(wait, 30.0))
        async with self._tb_lock:
            now = time.monotonic()
            self._tokens = min(self._burst,
                               self._tokens + (now - self._last_refill) * self._rate)
            self._last_refill = now
            self._tokens = max(0.0, self._tokens - 1.0)

    async def acquire(self) -> None:
        """Pre-flight: breaker check → rate token → concurrency slot.

        Raises CircuitOpenError before acquiring anything when the breaker is
        open, so callers can degrade gracefully without holding a slot.
        """
        self._check_breaker()
        await self._take_token()
        await self._sem.acquire()

    def release(self, *, failure: bool) -> None:
        self._sem.release()
        if failure:
            self._fails += 1
            if self._fails >= self._fail_threshold:
                if not self._open:
                    log.warning("LLM circuit OPEN after %d consecutive failures",
                                self._fails)
                self._open = True
                self._opened_at = time.monotonic()
        else:
            if self._open:
                log.info("LLM circuit CLOSED (backend healthy again)")
            self._fails = 0
            self._open = False


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
        # vLLM repetition_penalty (HF-style, prompt-inclusive). >1.0 discourages
        # reusing token sequences ALREADY IN CONTEXT — i.e. recycling her own
        # recent replies, which the fast model does under salience pull now that
        # history numbers/phrases pass through unredacted. ~1.05-1.10 is safe;
        # higher starts to degrade punctuation and Chinese. None → omit.
        _rp = config.get("repetition_penalty")
        self.repetition_penalty: float | None = float(_rp) if _rp else None

        timeout = float(config.get("request_timeout_s", 360))
        # api_key: literal value, or api_key_env: name of an env var holding it
        # (so secrets stay in .env, never in the tracked config.yaml).
        api_key = config.get("api_key", "")
        if not api_key and config.get("api_key_env"):
            api_key = os.environ.get(str(config["api_key_env"]), "")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self._http = httpx.AsyncClient(timeout=timeout, headers=headers)

        # Only Qwen3 chat templates understand the enable_thinking kwarg —
        # sending chat_template_kwargs to other models (deepseek-v3 on the
        # sparks gateway) is at best ignored and at worst a 400. Gate it here
        # so callers don't need to know which lane they're on.
        self._supports_thinking = "qwen" in self.model.lower()

        # Cached health state for cheap lane-selection checks (fast→deep
        # fallback when the local GPU lane is down). TTL-cached so a turn never
        # pays more than one 2s probe per window, and a healthy local vLLM
        # answers in ~1ms.
        self._health_ok: bool = True
        self._health_at: float = 0.0

        # Isolation guard (see _LLMThrottle) — bounds Mocha's load on shared
        # inference (today: the sparks-cluster gateway on the deep lane).
        iso = config.get("isolation", {}) or {}
        self._throttle = _LLMThrottle(
            max_concurrency=int(iso.get("max_concurrency", 1)),
            rate_per_min=float(iso.get("rate_limit_per_min", 30)),
            burst=float(iso.get("burst", 8)),
            fail_threshold=int(iso.get("breaker_fail_threshold", 4)),
            cooldown_s=float(iso.get("breaker_cooldown_s", 20)),
        )

    @property
    def circuit_open(self) -> bool:
        """True while the breaker is tripped (still inside its cooldown)."""
        t = self._throttle
        if not t._open:
            return False
        return (time.monotonic() - t._opened_at) < t._cooldown

    async def healthy_cached(self, ttl_s: float = 30.0) -> bool:
        """Cheap reachability check against /health, cached for ttl_s."""
        now = time.monotonic()
        if self._health_at and (now - self._health_at) < ttl_s:
            return self._health_ok
        try:
            resp = await self._http.get(self._health_url, timeout=2.0)
            self._health_ok = resp.status_code == 200
        except Exception:
            self._health_ok = False
        self._health_at = now
        return self._health_ok

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
        if self.repetition_penalty:
            body["repetition_penalty"] = self.repetition_penalty
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if enable_thinking is not None and self._supports_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": enable_thinking}

        try:
            await self._throttle.acquire()
        except CircuitOpenError as exc:
            log.warning("LLM chat fast-failed: %s", exc)
            return {"content": "", "tool_calls": None, "role": "assistant",
                    "usage": {}, "finish_reason": None, "_error": str(exc)}

        failure = False
        try:
            resp = await self._http.post(self._chat_url, json=body)
            if resp.status_code in (502, 503, 504, 524):
                # Transient edge/tunnel failure (remote gateway behind
                # Cloudflare: busy generation slot → 524 at ~100s). One retry
                # after a short pause — the same policy the desk's spark proxy
                # uses — then give up gracefully.
                log.warning("LLM HTTP %s — one retry in 2s", resp.status_code)
                await asyncio.sleep(2.0)
                resp = await self._http.post(self._chat_url, json=body)
            if resp.status_code != 200:
                failure = True
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
        except asyncio.CancelledError:
            raise  # caller cancellation is not a backend failure
        except Exception:
            failure = True
            raise
        finally:
            self._throttle.release(failure=failure)

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
        """Streaming chat completion via SSE, behind the shared-vLLM isolation
        guard (concurrency cap + rate limit + circuit breaker — see
        ``_LLMThrottle``). When the breaker is open, yields a single graceful
        ``done`` chunk carrying ``_error`` instead of hammering the backend.
        """
        try:
            await self._throttle.acquire()
        except CircuitOpenError as exc:
            log.warning("LLM stream fast-failed: %s", exc)
            yield {"content": "", "done": True, "tool_calls": None,
                   "usage": {}, "finish_reason": None, "_error": str(exc)}
            return

        failure = False
        try:
            async for chunk in self._chat_stream_raw(
                messages, temperature=temperature, max_tokens=max_tokens,
                tools=tools, enable_thinking=enable_thinking,
            ):
                if chunk.get("_error") is not None:
                    failure = True
                yield chunk
        except asyncio.CancelledError:
            raise  # barge-in / caller cancellation is not a backend failure
        except Exception:
            failure = True
            raise
        finally:
            self._throttle.release(failure=failure)

    async def _chat_stream_raw(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        enable_thinking: bool | None = None,
    ) -> AsyncIterator[dict]:
        """Unguarded streaming completion via SSE.

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
        if self.repetition_penalty:
            body["repetition_penalty"] = self.repetition_penalty
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if enable_thinking is not None and self._supports_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": enable_thinking}

        async with self._http.stream("POST", self._chat_url, json=body) as resp:
            if resp.status_code != 200:
                body_text = ""
                async for chunk in resp.aiter_text():
                    body_text += chunk
                    if len(body_text) > 2000:
                        break
                log.error("LLM stream HTTP %s: %s", resp.status_code, body_text[:2000])
                yield {"content": "", "done": True, "tool_calls": None, "usage": {},
                       "finish_reason": None, "_error": body_text[:2000] or f"HTTP {resp.status_code}"}
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
