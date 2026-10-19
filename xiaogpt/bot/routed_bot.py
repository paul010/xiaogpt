"""OpenRouter voice bot with per-request model choice and cross-process budgeting."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import os
import time

import httpx

from xiaogpt.bot.chatgptapi_bot import ChatGPTBot
from xiaogpt.routing import BudgetExceeded, Ledger, QWEN, SOL, choose, nanos
from xiaogpt.utils import split_sentences


class RoutedBot(ChatGPTBot):
    name = "AI"

    @classmethod
    def from_config(cls, config):
        if (config.api_base or "").rstrip("/") != "https://openrouter.ai/api/v1":
            raise ValueError("Budgeted routing requires the OpenRouter HTTPS endpoint")
        bot = cls(
            config.openai_key, config.api_base, config.proxy, config.deployment_id
        )
        bot.settings = dict(config.routing)
        bot.ledger = Ledger(
            bot.settings.get("ledger_path", "~/.local/state/xiaogpt/budget.sqlite3"),
            bot.settings.get("monthly_budget_usd", 3),
        )
        bot.prompt = config.prompt
        bot.speaker = os.getenv("XIAOGPT_SPEAKER", "speaker")
        return bot

    def change_prompt(self, prompt):
        self.prompt = prompt

    def messages(self, query):
        system = (
            self.prompt
            + "。面向音箱，用80到150字先给结论，不使用Markdown或链接。没有实时数据时不要声称已联网查询。"
        )
        history = self.history[-3:]
        while True:
            messages = [{"role": "system", "content": system}]
            for q, answer in history:
                messages.extend(
                    [
                        {"role": "user", "content": q},
                        {"role": "assistant", "content": answer},
                    ]
                )
            messages.append({"role": "user", "content": query})
            # UTF-8 bytes plus generous framing overhead is a conservative bound
            # for these byte-level text tokenizers; never estimate from char count.
            bound = (
                len(json.dumps(messages, ensure_ascii=False).encode())
                + 64 * len(messages)
                + 256
            )
            if bound <= 4096:
                return messages, bound
            if not history:
                raise ValueError("question_too_long")
            history = history[1:]

    @staticmethod
    def limits(model):
        # Reserve cache-write headroom for Sol, in addition to the provider price cap.
        return (
            (1536, Decimal("2.5"), Decimal("10"))
            if model == SOL
            else (512, Decimal("0.10"), Decimal("0.60"))
        )

    async def request(self, model, messages, token_bound, reason, state):
        import openai

        max_tokens, input_price, output_price = self.limits(model)
        reserve = nanos(
            (token_bound * input_price + max_tokens * output_price) / 1_000_000
        )
        ticket = self.ledger.reserve(reserve, model, self.speaker, reason)
        started = time.monotonic()
        kwargs = {"trust_env": True}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        usage = None
        try:
            async with httpx.AsyncClient(**kwargs) as session:
                async with openai.AsyncOpenAI(
                    api_key=self.openai_key,
                    base_url=self.api_base,
                    http_client=session,
                    max_retries=0,
                    timeout=20,
                ) as client:
                    extra = {
                        "provider": {
                            "max_price": {
                                "prompt": 2.0 if model == SOL else float(input_price),
                                "completion": float(output_price),
                                "request": 0,
                            },
                            "require_parameters": True,
                        }
                    }
                    if model == SOL:
                        extra["reasoning"] = {"effort": "low", "exclude": True}
                        extra["prompt_cache_options"] = {"mode": "explicit"}
                    stream = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=model,
                            messages=messages,
                            max_tokens=max_tokens,
                            stream=True,
                            extra_body=extra,
                        ),
                        timeout=8,
                    )
                    seen_text = False
                    iterator = stream.__aiter__()
                    try:
                        while True:
                            remaining = (
                                max(0.01, 8 - (time.monotonic() - started))
                                if not seen_text
                                else 20
                            )
                            try:
                                event = await asyncio.wait_for(
                                    iterator.__anext__(), remaining
                                )
                            except StopAsyncIteration:
                                break
                            if event.usage is not None:
                                usage = event.usage.model_dump()
                            state["generation_id"] = event.id
                            state["model"] = event.model
                            if getattr(event, "error", None):
                                raise RuntimeError("stream_error")
                            if not event.choices:
                                continue
                            finish = getattr(event.choices[0], "finish_reason", None)
                            if finish:
                                state["finish_reason"] = finish
                            if finish == "error":
                                raise RuntimeError("stream_error")
                            text = event.choices[0].delta.content
                            if text:
                                if not seen_text:
                                    state["first_token_seconds"] = round(
                                        time.monotonic() - started, 3
                                    )
                                seen_text = True
                                state["reply"] += text
                                yield text
                    finally:
                        await stream.close()
                    if not seen_text:
                        raise ValueError("empty_completion")
        except BaseException as error:
            state["error"] = type(error).__name__
            state["status"] = getattr(error, "status_code", None)
            body = getattr(error, "body", None)
            if isinstance(body, dict):
                body = body.get("error", body)
                metadata = body.get("metadata", {}) if isinstance(body, dict) else {}
                if isinstance(metadata, dict) and metadata.get("limit_source") in {
                    "openrouter_guardrail_budget",
                    "openrouter_workspace_budget",
                    "openrouter_key_limit",
                }:
                    state["budget_block"] = True
            # Budget and prompt moderation 403s must not disable a model forever.
            if state["status"] == 403 and "terms of service" in str(error).lower():
                self.ledger.block(model, "provider_terms_403")
            raise
        finally:
            cost = usage.get("cost") if usage else None
            if cost is not None:
                try:
                    nanos(cost)
                except (ValueError, ArithmeticError):
                    cost = None
            details = {k: v for k, v in state.items() if k != "reply"}
            details["usage"] = usage
            self.ledger.finish(ticket, cost, details)

    async def answer(self, query):
        previous = self.history[-1][0] if self.history else ""
        decision = choose(query, previous)
        model, reason = decision.model, decision.reason
        try:
            if model == SOL and (
                self.ledger.blocked(SOL)
                or self.ledger.total() >= self.ledger.limit * 0.8
            ):
                model, reason = QWEN, "strong_blocked_or_budget"
                if decision.explicit:
                    yield "GPT暂时不可用或预算不足，我先用千问回答。"
            if self.ledger.blocked(model):
                yield "模型服务暂时不可用，请稍后再试。"
                return
            messages, bound = self.messages(decision.query)
            for attempt in range(2):
                state = {"reply": "", "selected_model": model, "reason": reason}
                try:
                    async for text in self.request(
                        model, messages, bound, reason, state
                    ):
                        yield text
                except BudgetExceeded:
                    yield "两台音箱本月的三美元AI预算已用完或余额不足，本次不再付费调用。"
                    return
                except Exception:
                    # Only Sol may fall back, before any answer text was handed to TTS.
                    status = state.get("status")
                    if state.get("budget_block"):
                        yield "OpenRouter费用额度已用完，本次停止付费调用。"
                        return
                    if (
                        model == SOL
                        and attempt == 0
                        and not state["reply"]
                        and status not in (401, 402)
                    ):
                        model, reason = QWEN, "strong_failed_fallback"
                        if self.ledger.blocked(QWEN):
                            break
                        continue
                    yield "本次AI回答没有完成，请稍后再试。"
                    return
                else:
                    self.history.append((decision.query, state["reply"]))
                    self.history = self.history[-3:]
                    return
        except ValueError:
            yield "问题或上下文太长，请把问题说简短一些。"
        except Exception:
            # Database/permission failure must never bypass budget enforcement.
            yield "费用控制暂时不可用，本次停止付费调用。"

    async def ask_stream(self, query, **options):
        async for sentence in split_sentences(self.answer(query)):
            yield sentence

    async def ask(self, query, **options):
        return "".join([text async for text in self.ask_stream(query, **options)])
