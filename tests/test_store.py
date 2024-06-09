import csv

import pytest

from skinarb.proxies import ProxyStat, ProxyState
from skinarb.store import Store, _usd


@pytest.fixture()
def store():
    instance = Store(":memory:")
    yield instance
    instance.close()


def test_run_and_items_round_trip(store):
    run_id = store.create_run("a8db")
    added = store.add_items(run_id, [("AK-47 | Redline (Field-Tested)", 5.0), ("Glock | Fade", None)])

    assert added == 2
    assert {row.title for row in store.pending(run_id)} == {
        "AK-47 | Redline (Field-Tested)",
        "Glock | Fade",
    }


def test_adding_the_same_title_twice_does_not_duplicate(store):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("AK-47 | Redline (Field-Tested)", 5.0)])
    store.add_items(run_id, [("AK-47 | Redline (Field-Tested)", 5.0)])

    assert len(store.pending(run_id)) == 1


def test_pricing_moves_an_item_through_the_statuses(store):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("Glock | Fade", None)])

    store.set_dmarket_price(run_id, "Glock | Fade", 1234)
    assert store.pending(run_id)[0].status == "dmarket_done"
    assert [row.title for row in store.needing_steam(run_id)] == ["Glock | Fade"]

    store.set_steam_price(run_id, "Glock | Fade", 2000, "USD")
    assert store.pending(run_id) == []
    assert store.counts(run_id)["priced"] == 1


def test_skipped_and_failed_items_are_not_retried(store):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("A", None), ("B", None)])

    store.mark_skipped(run_id, "A")
    store.mark_failed(run_id, "B", "steam timeout")

    assert store.pending(run_id) == []
    assert store.counts(run_id) == {"skipped": 1, "failed": 1}


def test_unlisted_items_are_not_retried(store):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("A", None)])

    store.mark_unlisted(run_id, "A")

    assert store.pending(run_id) == []
    assert store.counts(run_id) == {"unlisted": 1}


def test_attempts_accumulate(store):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("A", None)])

    assert store.bump_attempt(run_id, "A") == 1
    assert store.bump_attempt(run_id, "A") == 2


def test_resume_sees_only_unfinished_work(tmp_path):
    path = tmp_path / "run.sqlite"
    first = Store(path)
    run_id = first.create_run("a8db")
    first.add_items(run_id, [("A", None), ("B", None)])
    first.set_dmarket_price(run_id, "A", 100)
    first.set_steam_price(run_id, "A", 200, "USD")
    first.close()

    second = Store(path)
    assert [row.title for row in second.pending(run_id)] == ["B"]
    second.close()


def test_export_writes_both_profit_columns(store, tmp_path):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("Cheap on DMarket", None)])
    store.set_dmarket_price(run_id, "Cheap on DMarket", 500)
    store.set_steam_price(run_id, "Cheap on DMarket", 800, "USD")

    out = tmp_path / "out.csv"
    result = store.export_csv(run_id, out)

    assert result.written == 1
    assert result.skipped_null_price == 0
    row = next(iter(csv.DictReader(out.open())))
    assert row["title"] == "Cheap on DMarket"
    assert row["dmarket_usd"] == "5.00"
    assert row["steam_usd"] == "8.00"
    assert row["wallet_usd"] == "1.96"
    assert row["withdrawable_usd"] == "-3.10"


def test_export_skips_a_priced_row_with_a_null_price_instead_of_raising(store, tmp_path):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("Half priced", None), ("Whole", None)])
    store.set_dmarket_price(run_id, "Half priced", 500)
    store.set_steam_price(run_id, "Half priced", 800, "USD")
    store.set_dmarket_price(run_id, "Whole", 500)
    store.set_steam_price(run_id, "Whole", 800, "USD")
    store.connection.execute(
        "UPDATE items SET dmarket_cents = NULL WHERE run_id = ? AND title = ?",
        (run_id, "Half priced"),
    )
    store.connection.commit()

    out = tmp_path / "out.csv"
    result = store.export_csv(run_id, out)

    assert result.written == 1
    assert result.skipped_null_price == 1
    titles = [row["title"] for row in csv.DictReader(out.open())]
    assert titles == ["Whole"]


def test_export_uses_the_items_own_reduced_fee(store, tmp_path):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("Reduced fee item", 1.0)])
    store.set_dmarket_price(run_id, "Reduced fee item", 800)
    store.set_steam_price(run_id, "Reduced fee item", 500, "USD")

    out = tmp_path / "out.csv"
    store.export_csv(run_id, out)

    row = next(iter(csv.DictReader(out.open())))
    assert row["dmarket_fee_pct"] == "1.00"
    assert row["withdrawable_usd"] == "2.92"


def test_export_falls_back_to_the_flat_fee_when_the_stored_fee_is_missing_or_insane(store, tmp_path):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("No fee reported", None), ("Insane fee", 500.0)])
    store.set_dmarket_price(run_id, "No fee reported", 800)
    store.set_steam_price(run_id, "No fee reported", 500, "USD")
    store.set_dmarket_price(run_id, "Insane fee", 800)
    store.set_steam_price(run_id, "Insane fee", 500, "USD")

    out = tmp_path / "out.csv"
    store.export_csv(run_id, out)

    rows = {row["title"]: row for row in csv.DictReader(out.open())}
    assert rows["No fee reported"]["dmarket_fee_pct"] == "2.00"
    assert rows["Insane fee"]["dmarket_fee_pct"] == "2.00"


def test_export_can_filter_by_withdrawable_profit(store, tmp_path):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("Loser", None), ("Winner", None)])
    store.set_dmarket_price(run_id, "Loser", 500)
    store.set_steam_price(run_id, "Loser", 800, "USD")
    store.set_dmarket_price(run_id, "Winner", 800)
    store.set_steam_price(run_id, "Winner", 500, "USD")

    out = tmp_path / "out.csv"
    assert store.export_csv(run_id, out, min_withdrawable_cents=100).written == 1
    assert "Winner" in out.read_text()
    assert "Loser" not in out.read_text()


def test_proxy_stats_are_stored(store):
    run_id = store.create_run("a8db")
    store.save_proxy_stats(
        run_id,
        [ProxyStat("1.2.3.4:8080", 10, 9, 1, 0, 0, 150, ProxyState.COOLDOWN)],
    )

    rows = store.connection.execute("SELECT proxy, ok, state FROM proxy_stats").fetchall()
    assert rows == [("1.2.3.4:8080", 9, "cooldown")]


def test_failure_breakdown_groups_identical_causes(store):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("A", None), ("B", None), ("C", None)])
    store.mark_failed(run_id, "A", "status 500")
    store.mark_failed(run_id, "B", "status 500")
    store.mark_failed(run_id, "C", "rate_limited")

    assert store.failure_breakdown(run_id) == [("status 500", 2), ("rate_limited", 1)]


def test_failure_breakdown_collapses_messages_that_carry_variable_detail(store):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("A", None), ("B", None), ("C", None), ("D", None)])
    store.mark_failed(run_id, "A", "ClientConnectorError: Cannot connect to host x:443")
    store.mark_failed(run_id, "B", "ClientConnectorError: Cannot connect to host y:443")
    store.mark_failed(run_id, "C", "no digits in '93,,84'")
    store.mark_failed(run_id, "D", "no digits in 'oops'")

    assert store.failure_breakdown(run_id) == [("ClientConnectorError", 2), ("no digits in", 2)]


def test_failure_breakdown_ignores_items_that_never_failed(store):
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("A", None), ("B", None)])
    store.mark_skipped(run_id, "A")
    store.mark_unlisted(run_id, "B")

    assert store.failure_breakdown(run_id) == []


def test_failure_breakdown_is_capped_and_sorted_worst_first(store):
    run_id = store.create_run("a8db")
    for cause_index, count in enumerate([6, 5, 4, 3, 2, 1]):
        cause = f"cause {cause_index}"
        titles = [f"{cause} item {n}" for n in range(count)]
        store.add_items(run_id, [(title, None) for title in titles])
        for title in titles:
            store.mark_failed(run_id, title, cause)

    assert store.failure_breakdown(run_id, limit=3) == [
        ("cause 0", 6),
        ("cause 1", 5),
        ("cause 2", 4),
    ]


def test_worst_proxies_sorts_worst_first_and_excludes_clean_ones(store):
    run_id = store.create_run("a8db")
    store.save_proxy_stats(
        run_id,
        [
            ProxyStat("1.1.1.1:1", 10, 5, 0, 5, 0, None, ProxyState.COOLDOWN),
            ProxyStat("2.2.2.2:2", 10, 10, 0, 0, 0, None, ProxyState.ACTIVE),
            ProxyStat("3.3.3.3:3", 10, 1, 0, 9, 0, None, ProxyState.QUARANTINED),
        ],
    )

    assert store.worst_proxies(run_id) == [("3.3.3.3:3", 9), ("1.1.1.1:1", 5)]


def test_worst_proxies_is_capped(store):
    run_id = store.create_run("a8db")
    store.save_proxy_stats(
        run_id,
        [
            ProxyStat(f"1.1.1.{i}:1", 10, 0, 0, errors, 0, None, ProxyState.DEAD)
            for i, errors in enumerate([9, 8, 7, 6, 5, 4], start=1)
        ],
    )

    assert store.worst_proxies(run_id, limit=2) == [("1.1.1.1:1", 9), ("1.1.1.2:1", 8)]


def test_usd_formatting_without_float():
    """Test that _usd formats money without constructing floats."""
    assert _usd(None) == ""
    assert _usd(-310) == "-3.10"
    assert _usd(5) == "0.05"
    assert _usd(500) == "5.00"
    assert _usd(123456789) == "1234567.89"
