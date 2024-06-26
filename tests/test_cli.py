from skinarb.cli import main
from skinarb.store import Store
from skinarb.transport import HttpResponse


def test_export_writes_a_file(tmp_path, capsys):
    db = tmp_path / "run.sqlite"
    store = Store(db)
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("A", None)])
    store.set_dmarket_price(run_id, "A", 800)
    store.set_steam_price(run_id, "A", 500, "USD")
    store.close()

    out = tmp_path / "out.csv"
    assert main(["export", "--db", str(db), "--csv", str(out)]) == 0
    assert out.exists()
    assert "1 rows" in capsys.readouterr().out


def test_export_reports_rows_skipped_for_a_null_price(tmp_path, capsys):
    db = tmp_path / "run.sqlite"
    store = Store(db)
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("Half priced", None)])
    store.set_dmarket_price(run_id, "Half priced", 800)
    store.set_steam_price(run_id, "Half priced", 500, "USD")
    store.connection.execute(
        "UPDATE items SET dmarket_cents = NULL WHERE run_id = ? AND title = ?",
        (run_id, "Half priced"),
    )
    store.connection.commit()
    store.close()

    out = tmp_path / "out.csv"
    assert main(["export", "--db", str(db), "--csv", str(out)]) == 0
    output = capsys.readouterr().out
    assert "0 rows written" in output
    assert "1 rows skipped" in output


def test_export_reports_an_empty_database(tmp_path, capsys):
    db = tmp_path / "run.sqlite"
    Store(db).close()

    assert main(["export", "--db", str(db), "--csv", str(tmp_path / "out.csv")]) == 1
    assert "no finished run" in capsys.readouterr().out


def test_export_notes_when_the_newest_run_has_not_finished(tmp_path, capsys):
    db = tmp_path / "run.sqlite"
    store = Store(db)
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("A", None)])
    store.set_dmarket_price(run_id, "A", 800)
    store.set_steam_price(run_id, "A", 500, "USD")
    store.close()

    out = tmp_path / "out.csv"
    assert main(["export", "--db", str(db), "--csv", str(out)]) == 0
    output = capsys.readouterr().out
    assert "1 rows written" in output
    assert "has not finished" in output
    assert str(run_id) in output


def test_export_says_nothing_extra_about_a_finished_run(tmp_path, capsys):
    db = tmp_path / "run.sqlite"
    store = Store(db)
    run_id = store.create_run("a8db")
    store.add_items(run_id, [("A", None)])
    store.set_dmarket_price(run_id, "A", 800)
    store.set_steam_price(run_id, "A", 500, "USD")
    store.finish_run(run_id)
    store.close()

    out = tmp_path / "out.csv"
    assert main(["export", "--db", str(db), "--csv", str(out)]) == 0
    assert "has not finished" not in capsys.readouterr().out


def test_proxies_check_counts_and_reports_duplicates(tmp_path, capsys):
    path = tmp_path / "proxies.txt"
    path.write_text("1.2.3.4:8080\n# note\n1.2.3.4:8080\n5.6.7.8:3128:bob:secret\n")

    assert main(["proxies", "check", "--proxies", str(path)]) == 0
    out = capsys.readouterr().out
    assert "3 parsed" in out
    assert "1 duplicate" in out
    assert "secret" not in out


def test_proxies_check_points_at_the_broken_line(tmp_path, capsys):
    path = tmp_path / "proxies.txt"
    path.write_text("1.2.3.4:8080\nbroken\n")

    assert main(["proxies", "check", "--proxies", str(path)]) == 1
    assert "line 2" in capsys.readouterr().out


def test_a_malformed_proxy_line_never_prints_its_password(tmp_path, capsys):
    path = tmp_path / "proxies.txt"
    path.write_text("1.2.3.4:8080\n5.6.7.8:notaport:bob:zqx9-super-secret-leak\n")

    assert main(["proxies", "check", "--proxies", str(path)]) == 1
    out = capsys.readouterr().out
    assert "zqx9-super-secret-leak" not in out
    assert "bob" not in out
    assert "line 2" in out


def test_a_missing_proxy_file_is_reported_not_raised(tmp_path, capsys):
    missing = tmp_path / "not-there.txt"

    assert main(["proxies", "check", "--proxies", str(missing)]) == 1
    assert "cannot read the proxy file" in capsys.readouterr().out


def test_resume_refuses_a_database_whose_runs_all_finished(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DMARKET_API_KEY", "k")
    monkeypatch.setenv("DMARKET_API_SECRET", "ab" * 32)
    db = tmp_path / "run.sqlite"
    store = Store(db)
    run_id = store.create_run("a8db")
    store.finish_run(run_id)
    store.close()

    proxies = tmp_path / "proxies.txt"
    proxies.write_text("1.2.3.4:8080\n")

    code = main(["resume", "--db", str(db), "--proxies", str(proxies)])

    assert code == 1
    assert "nothing to resume" in capsys.readouterr().out


def test_run_without_credentials_fails_clearly(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("DMARKET_API_KEY", raising=False)
    monkeypatch.delenv("DMARKET_API_SECRET", raising=False)
    proxies = tmp_path / "proxies.txt"
    proxies.write_text("1.2.3.4:8080\n")

    code = main(["run", "--db", str(tmp_path / "r.sqlite"), "--proxies", str(proxies)])

    assert code == 1
    assert "DMARKET_API_KEY" in capsys.readouterr().out


def test_run_rejects_min_proxies_below_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DMARKET_API_KEY", "k")
    monkeypatch.setenv("DMARKET_API_SECRET", "ab" * 32)
    proxies = tmp_path / "proxies.txt"
    proxies.write_text("1.2.3.4:8080\n")

    code = main(
        [
            "run",
            "--db",
            str(tmp_path / "r.sqlite"),
            "--proxies",
            str(proxies),
            "--min-proxies",
            "0",
        ]
    )

    assert code == 1
    assert "at least 1" in capsys.readouterr().out


def test_resume_uses_the_run_stored_game_id_not_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("DMARKET_API_KEY", "k")
    monkeypatch.setenv("DMARKET_API_SECRET", "ab" * 32)

    db = tmp_path / "run.sqlite"
    store = Store(db)
    run_id = store.create_run("csgo")
    store.add_items(run_id, [("A", None)])
    store.close()

    proxies = tmp_path / "proxies.txt"
    proxies.write_text("1.2.3.4:8080\n")

    seen_game_ids = []

    def fake_requester(session):
        async def request(method, url, *, params=None, headers=None, proxy=None, timeout=15.0):
            if params and "gameId" in params:
                seen_game_ids.append(params["gameId"])
            return HttpResponse(200, {"objects": []})

        return request

    monkeypatch.setattr("skinarb.cli.aiohttp_requester", fake_requester)

    code = main(
        ["resume", "--db", str(db), "--proxies", str(proxies), "--game-id", "wrong-game"]
    )

    assert code == 0
    assert seen_game_ids == ["csgo"]


def test_resume_reports_that_a_given_game_id_is_ignored(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DMARKET_API_KEY", "k")
    monkeypatch.setenv("DMARKET_API_SECRET", "ab" * 32)

    db = tmp_path / "run.sqlite"
    store = Store(db)
    run_id = store.create_run("csgo")
    store.add_items(run_id, [("A", None)])
    store.close()

    proxies = tmp_path / "proxies.txt"
    proxies.write_text("1.2.3.4:8080\n")

    def fake_requester(session):
        async def request(method, url, *, params=None, headers=None, proxy=None, timeout=15.0):
            return HttpResponse(200, {"objects": []})

        return request

    monkeypatch.setattr("skinarb.cli.aiohttp_requester", fake_requester)

    code = main(
        ["resume", "--db", str(db), "--proxies", str(proxies), "--game-id", "wrong-game"]
    )

    assert code == 0
    assert "--game-id is ignored" in capsys.readouterr().out


def test_resume_says_nothing_about_game_id_when_none_was_given(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DMARKET_API_KEY", "k")
    monkeypatch.setenv("DMARKET_API_SECRET", "ab" * 32)

    db = tmp_path / "run.sqlite"
    store = Store(db)
    run_id = store.create_run("csgo")
    store.add_items(run_id, [("A", None)])
    store.close()

    proxies = tmp_path / "proxies.txt"
    proxies.write_text("1.2.3.4:8080\n")

    def fake_requester(session):
        async def request(method, url, *, params=None, headers=None, proxy=None, timeout=15.0):
            return HttpResponse(200, {"objects": []})

        return request

    monkeypatch.setattr("skinarb.cli.aiohttp_requester", fake_requester)

    code = main(["resume", "--db", str(db), "--proxies", str(proxies)])

    assert code == 0
    assert "--game-id" not in capsys.readouterr().out


def test_run_and_resume_report_duplicate_proxy_addresses(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DMARKET_API_KEY", "k")
    monkeypatch.setenv("DMARKET_API_SECRET", "ab" * 32)
    db = tmp_path / "run.sqlite"
    store = Store(db)
    run_id = store.create_run("a8db")
    store.finish_run(run_id)
    store.close()

    proxies = tmp_path / "proxies.txt"
    proxies.write_text("1.2.3.4:8080\n1.2.3.4:8080\n5.6.7.8:3128\n")

    code = main(["resume", "--db", str(db), "--proxies", str(proxies)])

    assert code == 1
    out = capsys.readouterr().out
    assert "3 parsed" in out
    assert "1 duplicate" in out


def test_run_report_shows_failure_causes_and_worst_proxies(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DMARKET_API_KEY", "k")
    monkeypatch.setenv("DMARKET_API_SECRET", "ab" * 32)

    proxies = tmp_path / "proxies.txt"
    proxies.write_text("1.2.3.4:8080\n")

    def fake_requester(session):
        async def request(method, url, *, params=None, headers=None, proxy=None, timeout=15.0):
            if "customized-fees" in url:
                return HttpResponse(200, {"reducedFees": [{"title": "A", "fee": 2.0}]})
            if "market/items" in url:
                return HttpResponse(200, {"objects": [{"price": {"USD": "500"}}]})
            if "priceoverview" in url:
                return HttpResponse(500, None)
            raise AssertionError(url)

        return request

    monkeypatch.setattr("skinarb.cli.aiohttp_requester", fake_requester)

    code = main(
        [
            "run",
            "--db",
            str(tmp_path / "r.sqlite"),
            "--proxies",
            str(proxies),
            "--proxy-cooldown",
            "0",
            "--dmarket-rps",
            "1000",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "failures by cause:" in out
    assert "status 500" in out
    assert "proxies with the most errors:" in out
    assert "1.2.3.4:8080" in out


def test_run_report_says_nothing_extra_when_nothing_failed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DMARKET_API_KEY", "k")
    monkeypatch.setenv("DMARKET_API_SECRET", "ab" * 32)

    proxies = tmp_path / "proxies.txt"
    proxies.write_text("1.2.3.4:8080\n")

    def fake_requester(session):
        async def request(method, url, *, params=None, headers=None, proxy=None, timeout=15.0):
            if "customized-fees" in url:
                return HttpResponse(200, {"reducedFees": [{"title": "A", "fee": 2.0}]})
            if "market/items" in url:
                return HttpResponse(200, {"objects": [{"price": {"USD": "500"}}]})
            if "priceoverview" in url:
                return HttpResponse(200, {"success": True, "lowest_price": "$8.00"})
            raise AssertionError(url)

        return request

    monkeypatch.setattr("skinarb.cli.aiohttp_requester", fake_requester)

    code = main(
        [
            "run",
            "--db",
            str(tmp_path / "r.sqlite"),
            "--proxies",
            str(proxies),
            "--proxy-cooldown",
            "0",
            "--dmarket-rps",
            "1000",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "failures by cause:" not in out
    assert "proxies with the most errors:" not in out
