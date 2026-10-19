from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import multiprocessing
import tempfile
import unittest

from xiaogpt.routing import (
    BudgetExceeded,
    Ledger,
    QWEN,
    SOL,
    choose,
    month_key,
    native_request,
    nanos,
)


def reserve_worker(path):
    ledger = Ledger(path, monthly_usd="0.01")
    try:
        ledger.reserve(nanos("0.002"), QWEN, "worker", "test")
        return 1
    except BudgetExceeded:
        return 0


class RoutingTests(unittest.TestCase):
    def test_question_selection_and_manual_override(self):
        cases = [
            ("解释光合作用", QWEN),
            ("翻译这句话", QWEN),
            ("比较两个装修方案", SOL),
            ("认真分析我的预算", SOL),
            ("用千问比较方案", QWEN),
            ("用GPT解释光合作用", SOL),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(choose(question).model, expected)
        self.assertEqual(choose("请用GPT重新回答", "之前的问题").query, "之前的问题")

    def test_native_commands_do_not_steal_ai_explanations(self):
        for query in [
            "请播放音乐",
            "请设明天七点的闹钟",
            "请给我设置一个闹钟",
            "请打开客厅的灯",
            "现在几点",
        ]:
            self.assertTrue(native_request(query), query)
        for query in ["请解释闹钟为什么响", "请分析音量变化的原因", "请介绍音乐的历史"]:
            self.assertFalse(native_request(query), query)

    def test_nonfinite_costs_rejected(self):
        for value in ["NaN", "Infinity", "-1"]:
            with self.assertRaises(ValueError):
                nanos(value)

    def test_beijing_month_boundary(self):
        self.assertEqual(
            month_key(datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)), "2026-11"
        )


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "budget.sqlite3"
        self.ledger = Ledger(self.path, monthly_usd=3)

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_cost_refunds_reservation_and_bootstrap_is_once(self):
        self.ledger.seed("0.10")
        self.ledger.seed("1.00")
        ticket = self.ledger.reserve(nanos("0.5"), QWEN, "bedroom", "test")
        self.ledger.finish(ticket, "0.01", {"tokens": 2})
        self.assertEqual(self.ledger.total(), nanos("0.11"))
        with self.assertRaises(ValueError):
            self.ledger.finish(ticket, "0")

    def test_restart_and_missing_usage_do_not_release_reserves(self):
        ticket = self.ledger.reserve(nanos("2.99"), SOL, "bedroom", "test")
        self.ledger.finish(ticket, None)
        restarted = Ledger(self.path, monthly_usd=3)
        with self.assertRaises(BudgetExceeded):
            restarted.reserve(nanos("0.02"), QWEN, "livingroom", "test")

    def test_unresolved_request_survives_month_rollover(self):
        october = datetime(2026, 10, 30, tzinfo=timezone.utc)
        november = datetime(2026, 11, 2, tzinfo=timezone.utc)
        self.ledger.reserve(nanos("2"), SOL, "bedroom", "test", now=october)
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve(nanos("2"), QWEN, "livingroom", "test", now=november)

    def test_two_processes_cannot_spend_the_same_remaining_budget(self):
        with ProcessPoolExecutor(
            max_workers=2, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            accepted = sum(pool.map(reserve_worker, [str(self.path)] * 12))
        self.assertEqual(accepted, 5)
        self.assertEqual(self.ledger.total(), nanos("0.01"))

    def test_provider_403_block_is_shared(self):
        self.ledger.block(SOL, "provider_403")
        self.assertTrue(Ledger(self.path).blocked(SOL))
        self.assertFalse(self.ledger.blocked(QWEN))
