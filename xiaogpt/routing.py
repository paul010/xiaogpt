"""Local model selection and a shared, fail-closed monthly cost ledger."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo

QWEN = "qwen/qwen3-235b-a22b-2507"
SOL = "openai/gpt-6.1-sol"
NANO = 1_000_000_000


def nanos(dollars) -> int:
    value = Decimal(str(dollars))
    if not value.is_finite() or value < 0:
        raise ValueError("Cost must be a finite nonnegative number")
    return int((value * NANO).to_integral_value(rounding=ROUND_CEILING))


def month_key(now: datetime | None = None) -> str:
    return (
        (now or datetime.now(ZoneInfo("Asia/Shanghai")))
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .strftime("%Y-%m")
    )


def native_request(query: str) -> bool:
    q = re.sub(r"^(小爱同学[，, ]*)?(请[你您]?)?", "", query.strip())
    return bool(
        re.match(
            r"(播放|暂停|继续播放|停止播放|下一首|上一首|打开.*灯|关闭.*灯|开灯|关灯|"
            r"把?音量|调[高低大小].*音量|设[置定].*(闹钟|提醒|倒计时)|"
            r"(给我|帮我)?设[置定]?.*(闹钟|提醒|倒计时)|"
            r"(告诉我)?(今天|明天|后天|现在|当地).*(天气|几点|时间))",
            q,
        )
    )


@dataclass(frozen=True)
class Decision:
    model: str
    reason: str
    query: str
    explicit: bool = False


def choose(query: str, previous_query: str = "") -> Decision:
    q = query.strip()
    if re.search(r"省钱模式|用千问|用通义|用Qwen", q, re.I):
        cleaned = re.sub(
            r"(请)?(用千问|用通义|用Qwen|省钱模式)[，,：: ]*", "", q, flags=re.I
        )
        return Decision(QWEN, "explicit_qwen", cleaned or q, True)
    strong = bool(
        re.search(r"用\s*GPT|请\s*GPT|深度分析|认真分析|换.*强.*模型|强一点", q, re.I)
    )
    repeat = bool(re.search(r"重新回答|重答|换.*强|强一点", q))
    cleaned = re.sub(r"(请)?用\s*GPT[，,：: ]*", "", q, flags=re.I).strip()
    if strong:
        if repeat and previous_query:
            cleaned = previous_query
        return Decision(SOL, "explicit_sol", cleaned or q, True)
    if re.search(
        r"分析|比较|对比|权衡|排查|诊断|推导|证明|逻辑推理|"
        r"设计.*方案|制定.*计划|代码.*(错误|报错|调试)",
        q,
    ):
        return Decision(SOL, "complex_task", q)
    return Decision(QWEN, "everyday", q)


class BudgetExceeded(Exception):
    pass


class Ledger:
    """All instances use the same SQLite file. Unknown charges keep their reserve."""

    def __init__(self, path: Path | str, monthly_usd=3):
        self.path = Path(path).expanduser()
        self.limit = nanos(monthly_usd)
        if not self.limit:
            raise ValueError("Monthly budget must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                  id TEXT PRIMARY KEY, month TEXT NOT NULL, model TEXT,
                  speaker TEXT, reason TEXT, amount INTEGER NOT NULL,
                  state TEXT NOT NULL, created TEXT NOT NULL, metadata TEXT);
                CREATE TABLE IF NOT EXISTS blocked_models (
                  model TEXT PRIMARY KEY, reason TEXT NOT NULL);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _total(db, month):
        # Carry unresolved requests over month boundaries, including process crashes.
        return db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM requests "
            "WHERE month=? OR state IN ('reserved','unknown')",
            (month,),
        ).fetchone()[0]

    def total(self, now=None):
        with self.connection() as db:
            return self._total(db, month_key(now))

    def seed(self, dollars, now=None):
        month = month_key(now)
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "seed:" + month,
                    month,
                    "existing_key_usage",
                    "shared",
                    "bootstrap",
                    nanos(dollars),
                    "settled",
                    datetime.now().isoformat(),
                    "{}",
                ),
            )

    def reserve(self, amount: int, model: str, speaker: str, reason: str, now=None):
        if not isinstance(amount, int) or amount <= 0:
            raise ValueError("Reservation must be a positive integer")
        month = month_key(now)
        ident = uuid.uuid4().hex
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._total(db, month) + amount > self.limit:
                raise BudgetExceeded()
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    month,
                    model,
                    speaker,
                    reason,
                    amount,
                    "reserved",
                    datetime.now().isoformat(),
                    "{}",
                ),
            )
        return ident

    def finish(self, ident: str, cost=None, metadata=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM requests WHERE id=?", (ident,)
            ).fetchone()
            if not row or row[0] != "reserved":
                raise ValueError("Unknown or already settled reservation")
            if cost is None:
                db.execute(
                    "UPDATE requests SET state='unknown',metadata=? WHERE id=?",
                    (json.dumps(metadata or {}), ident),
                )
            else:
                db.execute(
                    "UPDATE requests SET state='settled',amount=?,metadata=? WHERE id=?",
                    (nanos(cost), json.dumps(metadata or {}), ident),
                )

    def block(self, model: str, reason: str):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO blocked_models VALUES (?,?)", (model, reason)
            )

    def blocked(self, model: str) -> bool:
        with self.connection() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM blocked_models WHERE model=?", (model,)
                ).fetchone()
            )

    def status(self):
        with self.connection() as db:
            return {
                "month": month_key(),
                "timezone": "Asia/Shanghai",
                "limit_usd": self.limit / NANO,
                "charged_or_reserved_usd": self._total(db, month_key()) / NANO,
                "blocked_models": dict(
                    db.execute("SELECT model,reason FROM blocked_models")
                ),
                "requests_by_state": dict(
                    db.execute("SELECT state,COUNT(*) FROM requests GROUP BY state")
                ),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "seed", "block", "unblock"])
    parser.add_argument(
        "--db", type=Path, default=Path.home() / ".local/state/xiaogpt/budget.sqlite3"
    )
    parser.add_argument("--amount", type=str)
    parser.add_argument("--model", default=SOL)
    args = parser.parse_args()
    ledger = Ledger(args.db)
    if args.command == "seed":
        if args.amount is None:
            parser.error("seed requires --amount")
        ledger.seed(args.amount)
    elif args.command == "block":
        ledger.block(args.model, "manual_or_verified_403")
    elif args.command == "unblock":
        with ledger.connection() as db:
            db.execute("DELETE FROM blocked_models WHERE model=?", (args.model,))
    print(json.dumps(ledger.status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
