from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from xiaogpt.bot.routed_bot import RoutedBot
from xiaogpt.config import Config
from xiaogpt.routing import QWEN, SOL, nanos


class Stream:
    def __init__(self, events):
        self.events = iter(events)
        self.close = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration


def event(text=None, usage=None):
    return SimpleNamespace(
        id="fake-generation",
        model=QWEN,
        usage=usage,
        choices=(
            [SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=None)]
            if text
            else []
        ),
    )


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(
            openai_key="unit-test-placeholder",
            api_base="https://openrouter.ai/api/v1",
            routing={
                "enabled": True,
                "ledger_path": str(Path(self.temp.name) / "budget.db"),
                "monthly_budget_usd": 3,
            },
        )
        self.bot = RoutedBot.from_config(self.config)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_blocked_strong_model_never_gets_called(self):
        self.bot.ledger.block(SOL, "verified_403")
        calls = []

        async def request(model, messages, bound, reason, state):
            calls.append(model)
            state["reply"] = "正常回答。"
            yield state["reply"]

        with patch.object(self.bot, "request", request):
            answer = await self.bot.ask("用GPT重新回答")
        self.assertEqual(calls, [QWEN])
        self.assertIn("GPT暂时不可用", answer)
        self.assertIn("正常回答", answer)

    async def test_first_text_failure_falls_back_but_partial_answer_does_not(self):
        for partial in [False, True]:
            calls = []

            async def request(model, messages, bound, reason, state):
                calls.append(model)
                if model == SOL:
                    if partial:
                        state["reply"] = "部分回答。"
                        yield state["reply"]
                    raise TimeoutError()
                state["reply"] = "回退成功。"
                yield state["reply"]

            with patch.object(self.bot, "request", request):
                await self.bot.ask("认真分析装修方案")
            self.assertEqual(calls, [SOL] if partial else [SOL, QWEN])

    async def test_stream_tail_usage_is_settled_without_choices(self):
        usage = SimpleNamespace(
            model_dump=lambda: {
                "cost": 0.0001,
                "prompt_tokens": 20,
                "completion_tokens": 5,
            }
        )
        stream = Stream([event("测试成功。"), event(usage=usage)])
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(return_value=stream))
            )
        )

        class ClientContext:
            async def __aenter__(self):
                return client

            async def __aexit__(self, *args):
                pass

        with patch("openai.AsyncOpenAI", return_value=ClientContext()):
            answer = await self.bot.ask("介绍光合作用")
        self.assertEqual(answer, "测试成功。")
        self.assertEqual(self.bot.ledger.total(), nanos("0.0001"))
        self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], QWEN)
        self.assertIn(
            "max_price",
            client.chat.completions.create.call_args.kwargs["extra_body"]["provider"],
        )
        stream.close.assert_awaited_once()

    async def test_budget_or_storage_failure_prevents_network_call(self):
        self.bot.ledger.seed(3)
        with patch("openai.AsyncOpenAI") as client:
            answer = await self.bot.ask("讲一个故事")
            client.assert_not_called()
        self.assertIn("预算", answer)
        with patch.object(
            self.bot.ledger, "blocked", side_effect=OSError("db unavailable")
        ):
            answer = await self.bot.ask("讲一个故事")
        self.assertIn("停止付费调用", answer)

    async def test_real_403_failure_blocks_sol_and_keeps_unknown_cost(self):
        import httpx
        import openai

        denied = openai.PermissionDeniedError(
            "The request is prohibited due to a violation of provider Terms Of Service.",
            response=httpx.Response(
                403,
                request=httpx.Request(
                    "POST", "https://openrouter.ai/api/v1/chat/completions"
                ),
            ),
            body=None,
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(side_effect=denied))
            )
        )

        class ClientContext:
            async def __aenter__(self):
                return client

            async def __aexit__(self, *args):
                pass

        state = {"reply": ""}
        with patch("openai.AsyncOpenAI", return_value=ClientContext()):
            with self.assertRaises(openai.PermissionDeniedError):
                async for _ in self.bot.request(
                    SOL, [{"role": "user", "content": "test"}], 100, "test", state
                ):
                    pass
        self.assertEqual(state["status"], 403)
        self.assertTrue(self.bot.ledger.blocked(SOL))
        self.assertEqual(self.bot.ledger.status()["requests_by_state"], {"unknown": 1})
        self.assertGreater(self.bot.ledger.total(), 0)

    async def test_budget_403_does_not_permanently_block_or_try_another_model(self):
        import httpx
        import openai

        denied = openai.PermissionDeniedError(
            "Budget limit exceeded",
            response=httpx.Response(
                403,
                request=httpx.Request(
                    "POST", "https://openrouter.ai/api/v1/chat/completions"
                ),
            ),
            body={"metadata": {"limit_source": "openrouter_key_limit"}},
        )
        create = AsyncMock(side_effect=denied)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        class ClientContext:
            async def __aenter__(self):
                return client

            async def __aexit__(self, *args):
                pass

        with patch("openai.AsyncOpenAI", return_value=ClientContext()):
            answer = await self.bot.ask("认真分析装修方案")
        self.assertIn("费用额度已用完", answer)
        self.assertFalse(self.bot.ledger.blocked(SOL))
        create.assert_awaited_once()

    async def test_large_question_fails_without_api_call_and_history_is_trimmed(self):
        self.bot.history = [("旧问题" * 200, "旧回答" * 200)] * 3
        messages, bound = self.bot.messages("新问题")
        self.assertLessEqual(bound, 4096)
        self.assertEqual(messages[-1]["content"], "新问题")
        with patch("openai.AsyncOpenAI") as client:
            answer = await self.bot.ask("问题" * 5000)
            client.assert_not_called()
        self.assertIn("太长", answer)
