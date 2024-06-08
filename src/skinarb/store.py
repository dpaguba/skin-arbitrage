"""SQLite backed run state, resumable by design."""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from skinarb.profit import compute_profit
from skinarb.proxies import ProxyStat

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    game_id     TEXT NOT NULL,
    items_total INTEGER
);

CREATE TABLE IF NOT EXISTS items (
    run_id         INTEGER NOT NULL REFERENCES runs(id),
    title          TEXT NOT NULL,
    fee_percent    REAL,
    dmarket_cents  INTEGER,
    steam_cents    INTEGER,
    steam_currency TEXT,
    status         TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, title)
);

CREATE INDEX IF NOT EXISTS items_by_status ON items (run_id, status);

CREATE TABLE IF NOT EXISTS proxy_stats (
    run_id            INTEGER NOT NULL REFERENCES runs(id),
    proxy             TEXT NOT NULL,
    requests          INTEGER NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL DEFAULT 0,
    rate_limited      INTEGER NOT NULL DEFAULT 0,
    errors            INTEGER NOT NULL DEFAULT 0,
    wrong_currency    INTEGER NOT NULL DEFAULT 0,
    median_latency_ms INTEGER,
    state             TEXT NOT NULL,
    PRIMARY KEY (run_id, proxy)
);
"""

UNFINISHED = ("pending", "dmarket_done")


@dataclass(frozen=True)
class ExportResult:
    written: int
    skipped_null_price: int


@dataclass(frozen=True)
class ItemRow:
    title: str
    fee_percent: float | None
    dmarket_cents: int | None
    steam_cents: int | None
    steam_currency: str | None
    status: str
    attempts: int
    last_error: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _failure_cause(message: str) -> str:
    """Collapse a stored `last_error` to its stable, item-independent part.

    A fixed sentence like "status 500" or "rate_limited" already is the
    cause and is kept whole. Others carry a fragment of whatever the item
    or the exception added on, either a quoted excerpt ("no digits in
    '...'") or the text after "ExceptionClassName: ...": grouping on the
    full message would put almost every failure in its own bucket, so the
    text up to the first quote or colon, whichever comes first, is used
    instead.
    """
    indices = [i for i in (message.find(":"), message.find("'")) if i != -1]
    return message if not indices else message[: min(indices)].rstrip()


def _usd(cents: int | None) -> str:
    if cents is None:
        return ""

    sign = "-" if cents < 0 else ""
    abs_cents = abs(cents)

    dollars = abs_cents // 100
    remainder = abs_cents % 100

    return f"{sign}{dollars}.{remainder:02d}"


class Store:
    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def create_run(self, game_id: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs (started_at, game_id) VALUES (?, ?)", (_now(), game_id)
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def add_items(self, run_id: int, items: Iterable[tuple[str, float | None]]) -> int:
        rows = [(run_id, title, fee, "pending", _now()) for title, fee in items]
        self.connection.executemany(
            "INSERT OR IGNORE INTO items (run_id, title, fee_percent, status, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        total = self.connection.execute(
            "SELECT COUNT(*) FROM items WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        self.connection.execute("UPDATE runs SET items_total = ? WHERE id = ?", (total, run_id))
        self.connection.commit()
        return total

    def _select(self, run_id: int, statuses: Sequence[str]) -> list[ItemRow]:
        placeholders = ",".join("?" for _ in statuses)
        rows = self.connection.execute(
            "SELECT title, fee_percent, dmarket_cents, steam_cents, steam_currency,"
            " status, attempts, last_error FROM items"
            f" WHERE run_id = ? AND status IN ({placeholders}) ORDER BY title",
            (run_id, *statuses),
        ).fetchall()
        return [ItemRow(*row) for row in rows]

    def pending(self, run_id: int) -> list[ItemRow]:
        return self._select(run_id, UNFINISHED)

    def needing_steam(self, run_id: int) -> list[ItemRow]:
        return self._select(run_id, ("dmarket_done",))

    def _update(self, run_id: int, title: str, **fields) -> None:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.connection.execute(
            f"UPDATE items SET {assignments}, updated_at = ? WHERE run_id = ? AND title = ?",
            (*fields.values(), _now(), run_id, title),
        )
        self.connection.commit()

    def set_dmarket_price(self, run_id: int, title: str, cents: int) -> None:
        self._update(run_id, title, dmarket_cents=cents, status="dmarket_done")

    def set_steam_price(self, run_id: int, title: str, cents: int, currency: str) -> None:
        self._update(run_id, title, steam_cents=cents, steam_currency=currency, status="priced")

    def mark_skipped(self, run_id: int, title: str) -> None:
        self._update(run_id, title, status="skipped")

    def mark_unlisted(self, run_id: int, title: str) -> None:
        self._update(run_id, title, status="unlisted")

    def mark_failed(self, run_id: int, title: str, error: str) -> None:
        self._update(run_id, title, status="failed", last_error=error)

    def bump_attempt(self, run_id: int, title: str) -> int:
        self.connection.execute(
            "UPDATE items SET attempts = attempts + 1, updated_at = ?"
            " WHERE run_id = ? AND title = ?",
            (_now(), run_id, title),
        )
        self.connection.commit()
        return int(
            self.connection.execute(
                "SELECT attempts FROM items WHERE run_id = ? AND title = ?", (run_id, title)
            ).fetchone()[0]
        )

    def counts(self, run_id: int) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) FROM items WHERE run_id = ? GROUP BY status", (run_id,)
        ).fetchall()
        return {status: count for status, count in rows}

    def failure_breakdown(self, run_id: int, limit: int = 5) -> list[tuple[str, int]]:
        """The `limit` failure causes with the most items, worst first."""
        rows = self.connection.execute(
            "SELECT last_error, COUNT(*) FROM items"
            " WHERE run_id = ? AND status = 'failed' AND last_error IS NOT NULL"
            " GROUP BY last_error",
            (run_id,),
        ).fetchall()
        causes: Counter[str] = Counter()
        for message, count in rows:
            causes[_failure_cause(message)] += count
        return causes.most_common(limit)

    def worst_proxies(self, run_id: int, limit: int = 5) -> list[tuple[str, int]]:
        """The `limit` proxies with the most errors, worst first. None with
        zero errors are ever included, so a clean run reports nothing."""
        rows = self.connection.execute(
            "SELECT proxy, errors FROM proxy_stats"
            " WHERE run_id = ? AND errors > 0"
            " ORDER BY errors DESC, proxy ASC"
            " LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return [(proxy, int(errors)) for proxy, errors in rows]

    def save_proxy_stats(self, run_id: int, stats: Iterable[ProxyStat]) -> None:
        self.connection.executemany(
            "INSERT OR REPLACE INTO proxy_stats (run_id, proxy, requests, ok, rate_limited,"
            " errors, wrong_currency, median_latency_ms, state)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    stat.key,
                    stat.requests,
                    stat.ok,
                    stat.rate_limited,
                    stat.errors,
                    stat.wrong_currency,
                    stat.median_latency_ms,
                    str(stat.state.value),
                )
                for stat in stats
            ],
        )
        self.connection.commit()

    def finish_run(self, run_id: int) -> None:
        self.connection.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (_now(), run_id))
        self.connection.commit()

    def export_csv(
        self, run_id: int, path: str | Path, min_withdrawable_cents: int | None = None
    ) -> ExportResult:
        rows = self._select(run_id, ("priced",))
        written = 0
        skipped_null_price = 0

        with Path(path).open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "title",
                    "dmarket_usd",
                    "steam_usd",
                    "dmarket_fee_pct",
                    "withdrawable_usd",
                    "withdrawable_pct",
                    "wallet_usd",
                    "wallet_pct",
                ]
            )
            for row in rows:
                if row.dmarket_cents is None or row.steam_cents is None:
                    skipped_null_price += 1
                    continue

                profit = compute_profit(
                    row.dmarket_cents, row.steam_cents, dmarket_fee_pct=row.fee_percent
                )
                if min_withdrawable_cents is not None and profit.withdrawable_cents < min_withdrawable_cents:
                    continue
                writer.writerow(
                    [
                        row.title,
                        _usd(row.dmarket_cents),
                        _usd(row.steam_cents),
                        f"{profit.dmarket_fee_pct:.2f}",
                        _usd(profit.withdrawable_cents),
                        f"{profit.withdrawable_pct:.2f}",
                        _usd(profit.wallet_cents),
                        f"{profit.wallet_pct:.2f}",
                    ]
                )
                written += 1

        return ExportResult(written=written, skipped_null_price=skipped_null_price)
