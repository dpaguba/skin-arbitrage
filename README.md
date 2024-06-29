# skin-arbitrage

Compares CS:GO item prices between DMarket and Steam and reports what a trade would actually leave you, after both platforms take their cut.

It reads DMarket's list of items carrying a reduced selling fee, prices each item on both markets, and writes a CSV. Steam is queried in parallel through a pool of proxies, because Steam rate limits per IP address. Nothing is bought or sold: the tool produces numbers, and every decision stays with you.

Python 3.11 or newer. No services to run, no database to set up.

## The two numbers, and why there are two

Every row carries two profit figures, and they are never added together.

**Withdrawable** is the Steam to DMarket direction. Buy the item on Steam, sell it on DMarket, and the proceeds are money you can take out. This is the figure that matters.

**Wallet** is the DMarket to Steam direction. Buy on DMarket with real money, sell into Steam, and you receive Steam wallet funds, which can only ever be spent inside Steam. A large wallet figure is not income. It is an exchange rate between your money and store credit, and the older version of this tool reported it as profit.

Fees behind those numbers: DMarket takes a percentage from the seller, and the whole point of the reduced-fee list is that this percentage is lower than usual for those items. The export uses each item's own fee when the list reported one and the value looks sane, above zero and no more than one hundred, and falls back to two percent otherwise. That the list reports a percentage rather than a fraction is an assumption that has not been checked against a live response, so every row carries a `dmarket_fee_pct` column showing exactly which figure it used.

On the Steam side, the price shown to a buyer already contains Steam's fee, so a seller keeps roughly `price / 1.15`. That divisor is an estimate until it is measured against a live listing.

## Install

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Two files you create yourself, neither of which is ever committed:

`.env`, with a DMarket API key and secret:

```
DMARKET_API_KEY=...
DMARKET_API_SECRET=...
```

`proxies.txt`, one HTTP proxy per line, `#` starts a comment:

```
1.2.3.4:8080
5.6.7.8:3128:username:password
```

Check the file before a long run. This parses it and reports the count and any duplicate addresses, without making a single request:

```
skinarb proxies check --proxies proxies.txt
```

## Use

```
skinarb run     --db run.sqlite --proxies proxies.txt [--limit 200]
skinarb resume  --db run.sqlite --proxies proxies.txt
skinarb export  --db run.sqlite --csv out.csv [--min-withdrawable 100]
```

A full pass takes hours, so it is built to be interrupted. `Ctrl+C` stops after the requests already in flight come back. Everything priced so far is in the database, the run stays open, and `resume` continues from there. The same is true when the tool stops itself: a DMarket outage, a rate limit that will not clear, or a proxy pool that drops below the floor all end the run without marking it complete.

`resume` takes the game from the run it is resuming, so `--game-id` is ignored there and the tool says so if you pass it.

## Reading the output

At the end of a run you get the item counts by status, the proxy tally, a breakdown of failures by cause, and the addresses that produced the most errors.

| Status | Meaning |
| --- | --- |
| `priced` | both prices known, the row reaches the CSV |
| `skipped` | DMarket holds no offer, so Steam was never asked |
| `unlisted` | DMarket has a price, Steam has no live listing |
| `failed` | three attempts spent without an answer, the reason is stored |

`skipped` and `unlisted` are separate on purpose: a delisted item and an item that Steam simply does not stock are different situations, and merging them hides which one you are looking at.

The CSV columns:

```
title, dmarket_usd, steam_usd, dmarket_fee_pct,
withdrawable_usd, withdrawable_pct, wallet_usd, wallet_pct
```

`steam_usd` is always the lowest live ask. When Steam has sales history but no live listing, the item is reported as `unlisted` rather than priced from the median, because a median is what people paid in the past and not something you can buy at today.

## How it works

Two lanes, because the two APIs are limited in different ways.

DMarket limits per API key, so its lane is a single sequential walk on a token bucket, and it never goes through a proxy: an authenticated key arriving from a thousand countries is how an account gets flagged.

Steam limits per IP, so that side is the one that parallelises. Each address is leased to one request at a time and then rests for a cooldown. An address that hits a rate limit rests four times longer, three failures in a row put it in quarantine, and an answer in a currency other than dollars retires it for the run, since its exit country is wrong for our purpose.

Run state lives in SQLite and is written as each item resolves, which is what makes an interrupted run resumable.

Throughput is bounded by the DMarket lane, not by the pool. At the default of one request per second, ten thousand items take about three hours no matter how many proxies you have; the Steam workers spend most of that time waiting. A batch pricing endpoint on the DMarket side would change that, and whether one exists is still unverified.

## Tuning

| Flag | Default | Notes |
| --- | --- | --- |
| `--concurrency` | 100 | upper bound on parallel Steam requests |
| `--proxy-cooldown` | 15 | seconds an address rests after each request |
| `--dmarket-rps` | 1 | requests per second against the API key |
| `--min-proxies` | 1 | stop the run when fewer are alive, minimum 1 |
| `--game-id` | `a8db` | DMarket's identifier for CS:GO |
| `--limit` | none | stop after this many items, useful for a first run |

## What it will not do

It does not buy, sell, or place an order anywhere. It does not watch prices over time: one run is one snapshot, and prices move while a multi-hour pass is running. It does not verify that an item is tradable, that your account can receive it, or that the offer it priced still exists by the time you look.

Treat the CSV as a shortlist to check by hand, not as instructions.

## Development

```
.venv/bin/pytest
```

160 tests, no network access in any of them. The clients take their request function as an argument, so the entire code path is exercised against canned responses; only the thin `aiohttp` wrapper meets a real server, one started by the test itself.
