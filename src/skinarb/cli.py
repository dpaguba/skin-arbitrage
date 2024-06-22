"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

import aiohttp
from dotenv import load_dotenv

from skinarb.dmarket import DMarketClient, DMarketError, DMarketRateLimited
from skinarb.proxies import Proxy, ProxyPool, load_proxies
from skinarb.runner import NoProxiesLeft, RunConfig, Runner
from skinarb.steam import SteamClient
from skinarb.store import Store
from skinarb.transport import aiohttp_requester


def _proxy_summary(proxies: list[Proxy]) -> str:
    keys = [proxy.key for proxy in proxies]
    duplicates = len(keys) - len(set(keys))
    return f"{len(proxies)} parsed, {duplicates} duplicate addresses"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skinarb")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_run_arguments(sub: argparse.ArgumentParser, *, resume: bool = False) -> None:
        sub.add_argument("--db", required=True)
        sub.add_argument("--proxies", required=True)
        sub.add_argument("--game-id", default=None if resume else "a8db")
        sub.add_argument("--limit", type=int)
        sub.add_argument("--concurrency", type=int, default=100)
        sub.add_argument("--proxy-cooldown", type=float, default=15.0)
        sub.add_argument("--dmarket-rps", type=float, default=1.0)
        sub.add_argument("--min-proxies", type=int, default=1)

    add_run_arguments(commands.add_parser("run"))
    add_run_arguments(commands.add_parser("resume"), resume=True)

    export = commands.add_parser("export")
    export.add_argument("--db", required=True)
    export.add_argument("--csv", required=True)
    export.add_argument("--min-withdrawable", type=int, help="cents")

    proxies = commands.add_parser("proxies").add_subparsers(dest="proxy_command", required=True)
    check = proxies.add_parser("check")
    check.add_argument("--proxies", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    if args.command == "export":
        return _export(args)
    if args.command == "proxies":
        return _check_proxies(args)
    return asyncio.run(_run(args, resume=args.command == "resume"))


def _export(args) -> int:
    store = Store(args.db)
    try:
        row = store.connection.execute(
            "SELECT id, finished_at FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print("no finished run in this database")
            return 1
        run_id, finished_at = row
        result = store.export_csv(run_id, args.csv, args.min_withdrawable)
        print(f"{result.written} rows written to {args.csv}")
        if result.skipped_null_price:
            print(f"{result.skipped_null_price} rows skipped: priced but missing a price")
        if finished_at is None:
            print(f"run {run_id} has not finished; this export reflects a partial run")
        return 0
    finally:
        store.close()


def _check_proxies(args) -> int:
    try:
        proxies = load_proxies(args.proxies)
    except (OSError, ValueError) as error:
        print(f"cannot read the proxy file: {error}")
        return 1

    print(_proxy_summary(proxies))
    return 0


async def _run(args, resume: bool) -> int:
    key = os.getenv("DMARKET_API_KEY")
    secret = os.getenv("DMARKET_API_SECRET")
    if not key or not secret:
        print("DMARKET_API_KEY and DMARKET_API_SECRET must be set in the environment or .env")
        return 1

    try:
        proxies = load_proxies(args.proxies)
    except (OSError, ValueError) as error:
        print(f"cannot read the proxy file: {error}")
        return 1

    if not proxies:
        print(f"no proxies found in {args.proxies}")
        return 1

    print(_proxy_summary(proxies))

    store = Store(args.db)
    pool = ProxyPool(proxies, cooldown=args.proxy_cooldown)

    if resume:
        target = _resume_target(store)
        if target is None:
            print("nothing to resume in this database")
            store.close()
            return 1
        run_id, game_id = target
        if args.game_id is not None:
            print(f"--game-id is ignored on resume: continuing with {game_id!r}")
    else:
        run_id = store.create_run(args.game_id)
        game_id = args.game_id

    try:
        config = RunConfig(
            game_id=game_id,
            limit=args.limit,
            concurrency=args.concurrency,
            min_proxies=args.min_proxies,
        )
    except ValueError as error:
        print(str(error))
        store.close()
        return 1

    async with aiohttp.ClientSession() as session:
        request = aiohttp_requester(session)
        runner = Runner(
            store,
            DMarketClient(request, key, secret, rps=args.dmarket_rps),
            SteamClient(request, pool),
            pool,
            config,
        )

        loop = asyncio.get_running_loop()
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(name, runner.request_stop)

        try:
            if not resume:
                total = await runner.collect(run_id)
                print(f"{total} items to price")
            summary = await runner.execute(run_id)
            by_cause = store.failure_breakdown(summary.run_id)
            worst_proxies = store.worst_proxies(summary.run_id)
        except NoProxiesLeft as error:
            print(f"stopped: {error}")
            return 1
        except DMarketRateLimited as error:
            print(f"stopped: {error}. Lower --dmarket-rps and resume.")
            return 1
        except DMarketError as error:
            print(f"stopped: {error}. Everything priced so far is kept, resume when it is back.")
            return 1
        finally:
            store.close()

    print(f"run {summary.run_id}: {summary.counts}")
    print(f"proxies alive {summary.alive_proxies}, dead {summary.dead_proxies}")

    if by_cause:
        print("failures by cause:")
        for cause, count in by_cause:
            print(f"  {count:>5}  {cause}")

    if worst_proxies:
        print("proxies with the most errors:")
        for proxy, errors in worst_proxies:
            print(f"  {errors:>5}  {proxy}")

    if summary.stopped:
        print("stopped early, nothing priced is lost: run resume to continue")

    return 0


def _resume_target(store: Store) -> tuple[int, str] | None:
    row = store.connection.execute(
        "SELECT id, game_id FROM runs WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else (int(row[0]), str(row[1]))


if __name__ == "__main__":
    sys.exit(main())
