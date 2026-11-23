"""End-to-end tests for the record-progression archive."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "records.db"
    monkeypatch.setenv("APP_DB_PATH", str(db_path))

    # Reload the app so the lifespan uses this test's database.
    import app.main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as test_client:
        yield test_client


def supplement(result_id: str, athlete: str, finish_time: str, ms: int) -> dict:
    return {
        "result_id": result_id,
        "athlete": athlete,
        "finish_time": finish_time,
        "duration_ms": ms,
    }


def publish(client, base_version: int, supplements=(), withdrawals=()):
    return client.post(
        "/announcements",
        json={
            "base_version": base_version,
            "supplements": list(supplements),
            "withdrawals": [{"result_id": item} for item in withdrawals],
        },
    )


def kinds(detail):
    return [(item["seq"], item["kind"], item["result_id"]) for item in detail["progression"]]


def test_initial_baseline_exists(client):
    response = client.get("/versions/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["progression"] == []
    assert body["active_results"] == []


def test_first_refresh_and_tie(client):
    response = publish(
        client,
        1,
        supplements=[
            supplement("R-001", "甲", "2026-05-01T10:00:00Z", 250_000),
            supplement("R-002", "乙", "2026-05-02T10:00:00Z", 240_000),
            supplement("R-003", "丙", "2026-05-03T10:00:00Z", 240_000),
            supplement("R-004", "丁", "2026-05-04T10:00:00Z", 245_000),
        ],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"] == 2
    assert body["parent_version"] == 1
    # R-004 is slower than the standing record and is not part of the
    # progression, but it is still an active result.
    assert kinds(body) == [
        (1, "first", "R-001"),
        (2, "refresh", "R-002"),
        (3, "tie", "R-003"),
    ]
    assert len(body["active_results"]) == 4
    assert all(item["result_id"] for item in body["progression"])


def test_supplement_early_time_recomputes_from_that_time(client):
    publish(
        client,
        1,
        supplements=[
            supplement("R-010", "甲", "2026-05-10T10:00:00Z", 240_000),
            supplement("R-020", "乙", "2026-05-20T10:00:00Z", 230_000),
        ],
    )
    # Insert a result whose finish time is between the two existing results
    # and which is faster than the opening record.
    response = publish(
        client,
        2,
        supplements=[supplement("R-015", "丙", "2026-05-15T10:00:00Z", 235_000)],
    )
    assert response.status_code == 201
    body = response.json()
    assert kinds(body) == [
        (1, "first", "R-010"),
        (2, "refresh", "R-015"),
        (3, "refresh", "R-020"),
    ]
    assert body["recompute_from_finish_time"] == "2026-05-15T10:00:00.000000Z"
    assert body["reused_prefix_entries"] == 1
    assert body["recomputed_active_results"] == 2


def test_supplement_before_first_record_changes_first(client):
    publish(
        client,
        1,
        supplements=[supplement("R-100", "甲", "2026-06-01T10:00:00Z", 240_000)],
    )
    response = publish(
        client,
        2,
        supplements=[supplement("R-050", "乙", "2026-05-01T10:00:00Z", 250_000)],
    )
    assert response.status_code == 201
    assert kinds(response.json()) == [
        (1, "first", "R-050"),
        (2, "refresh", "R-100"),
    ]


def test_withdrawal_recomputes_and_old_version_is_unchanged(client):
    v2 = publish(
        client,
        1,
        supplements=[
            supplement("R-201", "甲", "2026-05-01T10:00:00Z", 250_000),
            supplement("R-202", "乙", "2026-05-02T10:00:00Z", 240_000),
            supplement("R-203", "丙", "2026-05-03T10:00:00Z", 245_000),
        ],
    ).json()
    assert kinds(v2) == [
        (1, "first", "R-201"),
        (2, "refresh", "R-202"),
    ]

    withdrawn = publish(client, 2, withdrawals=["R-202"])
    assert withdrawn.status_code == 201
    body = withdrawn.json()
    assert body["version"] == 3
    assert kinds(body) == [
        (1, "first", "R-201"),
        (2, "refresh", "R-203"),
    ]
    assert body["recompute_from_finish_time"] == "2026-05-02T10:00:00.000000Z"

    old = client.get("/versions/2")
    assert old.status_code == 200
    assert kinds(old.json()) == [
        (1, "first", "R-201"),
        (2, "refresh", "R-202"),
    ]
    old_progression = client.get("/versions/2/progression")
    assert old_progression.status_code == 200
    assert [item["result_id"] for item in old_progression.json()] == [
        "R-201",
        "R-202",
    ]


def test_withdrawal_of_only_record_leaves_empty_progression(client):
    publish(
        client,
        1,
        supplements=[supplement("R-300", "甲", "2026-05-01T10:00:00Z", 240_000)],
    )
    body = publish(client, 2, withdrawals=["R-300"]).json()
    assert body["progression"] == []
    assert body["active_results"] == []


def test_stale_base_version_is_rejected_and_current_state_preserved(client):
    first = publish(
        client,
        1,
        supplements=[supplement("R-401", "甲", "2026-05-01T10:00:00Z", 240_000)],
    )
    assert first.status_code == 201

    stale = publish(
        client,
        1,
        supplements=[supplement("R-402", "乙", "2026-05-02T10:00:00Z", 230_000)],
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_base_version"

    latest = client.get("/versions/latest").json()
    assert latest["version"] == 2
    assert [item["result_id"] for item in latest["active_results"]] == ["R-401"]
    versions = client.get("/versions").json()
    assert [item["version"] for item in versions] == [1, 2]


def test_two_announcements_on_same_base_only_one_applies(client):
    first = publish(
        client,
        1,
        supplements=[supplement("R-501", "甲", "2026-05-01T10:00:00Z", 240_000)],
    )
    second = publish(
        client,
        1,
        supplements=[supplement("R-502", "乙", "2026-05-02T10:00:00Z", 230_000)],
    )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "stale_base_version"


def test_invalid_operations_leave_no_partial_version(client):
    publish(
        client,
        1,
        supplements=[
            supplement("R-601", "甲", "2026-05-01T10:00:00Z", 240_000),
            supplement("R-602", "乙", "2026-05-02T10:00:00Z", 230_000),
        ],
    )
    # One valid supplement and one unknown withdrawal: the whole announcement
    # must fail, and no new version may appear.
    response = publish(
        client,
        2,
        supplements=[supplement("R-603", "丙", "2026-05-03T10:00:00Z", 220_000)],
        withdrawals=["R-999"],
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_result"
    latest = client.get("/versions/latest").json()
    assert latest["version"] == 2
    assert [item["result_id"] for item in latest["active_results"]] == [
        "R-601",
        "R-602",
    ]


def test_duplicate_and_retired_ids_are_rejected(client):
    publish(
        client,
        1,
        supplements=[supplement("R-701", "甲", "2026-05-01T10:00:00Z", 240_000)],
    )
    duplicate = publish(
        client,
        2,
        supplements=[supplement("R-701", "乙", "2026-05-02T10:00:00Z", 230_000)],
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "duplicate_result_id"

    publish(client, 2, withdrawals=["R-701"])
    retired = publish(
        client,
        3,
        supplements=[supplement("R-701", "丙", "2026-05-03T10:00:00Z", 220_000)],
    )
    assert retired.status_code == 409
    assert retired.json()["detail"]["code"] == "retired_result_id"


def test_withdrawal_requires_known_active_id(client):
    publish(
        client,
        1,
        supplements=[supplement("R-801", "甲", "2026-05-01T10:00:00Z", 240_000)],
    )
    unknown = publish(client, 2, withdrawals=["R-NOPE"])
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "unknown_result"

    publish(client, 2, withdrawals=["R-801"])
    again = publish(client, 3, withdrawals=["R-801"])
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "already_withdrawn"


def test_announcement_must_have_an_operation(client):
    response = publish(client, 1)
    assert response.status_code == 422


def test_progression_persists_across_restart(tmp_path):
    db_path = tmp_path / "persist.db"

    import app.database as database
    import app.service as service
    from app.schemas import AnnouncementIn

    database.init_db(str(db_path))
    service.ensure_baseline(str(db_path))
    version = service.publish(
        str(db_path),
        AnnouncementIn(
            base_version=1,
            supplements=[
                {
                    "result_id": "R-901",
                    "athlete": "甲",
                    "finish_time": "2026-05-01T10:00:00Z",
                    "duration_ms": 240_000,
                }
            ],
        ),
    )
    detail = service.get_version(str(db_path), version)
    assert [item["result_id"] for item in detail["progression"]] == ["R-901"]
    assert service.latest_version(str(db_path)) == version


def test_write_failure_rolls_back_and_keeps_published_version(tmp_path):
    """A failure while writing the new version must leave no partial state."""

    import sqlite3

    import app.database as database
    import app.service as service
    from app.schemas import AnnouncementIn

    db_path = str(tmp_path / "rollback.db")
    database.init_db(db_path)
    service.ensure_baseline(db_path)

    service.publish(
        db_path,
        AnnouncementIn(
            base_version=1,
            supplements=[
                {
                    "result_id": "R-1001",
                    "athlete": "甲",
                    "finish_time": "2026-05-01T10:00:00Z",
                    "duration_ms": 240_000,
                }
            ],
        ),
    )

    # Force a write failure on the next publish, after several inserts have
    # already run inside the transaction.
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TRIGGER fail_on_operations
        BEFORE INSERT ON version_operations
        WHEN NEW.result_id = 'R-1002'
        BEGIN
            SELECT RAISE(ABORT, 'injected write failure');
        END;
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(sqlite3.IntegrityError):
        service.publish(
            db_path,
            AnnouncementIn(
                base_version=2,
                supplements=[
                    {
                        "result_id": "R-1002",
                        "athlete": "乙",
                        "finish_time": "2026-05-02T10:00:00Z",
                        "duration_ms": 230_000,
                    }
                ],
            ),
        )

    assert service.latest_version(db_path) == 2
    detail = service.get_version(db_path, 2)
    assert [item["result_id"] for item in detail["active_results"]] == ["R-1001"]
    assert service.list_results(db_path, status="all") == [
        {
            "result_id": "R-1001",
            "athlete": "甲",
            "finish_time": "2026-05-01T10:00:00.000000Z",
            "duration_ms": 240_000,
            "status": "active",
            "added_version": 2,
            "withdrawn_version": None,
        }
    ]


def test_withdrawal_of_first_record_promotes_next(client):
    publish(
        client,
        1,
        supplements=[
            supplement("R-A01", "甲", "2026-05-01T10:00:00Z", 250_000),
            supplement("R-A02", "乙", "2026-05-02T10:00:00Z", 245_000),
            supplement("R-A03", "丙", "2026-05-03T10:00:00Z", 240_000),
        ],
    )
    body = publish(client, 2, withdrawals=["R-A01"]).json()
    assert body["recompute_from_finish_time"] == "2026-05-01T10:00:00.000000Z"
    assert body["reused_prefix_entries"] == 0
    assert kinds(body) == [
        (1, "first", "R-A02"),
        (2, "refresh", "R-A03"),
    ]


def test_same_finish_time_orders_by_result_id(client):
    # Same finish instant: ordering falls back to the result id.
    body = publish(
        client,
        1,
        supplements=[
            supplement("R-B02", "乙", "2026-05-01T10:00:00Z", 240_000),
            supplement("R-B01", "甲", "2026-05-01T10:00:00Z", 250_000),
        ],
    ).json()
    assert kinds(body) == [
        (1, "first", "R-B01"),
        (2, "refresh", "R-B02"),
    ]
    assert body["active_results"][0]["result_id"] == "R-B01"


def test_supplement_and_withdrawal_in_one_announcement(client):
    publish(
        client,
        1,
        supplements=[
            supplement("R-C01", "甲", "2026-05-01T10:00:00Z", 250_000),
            supplement("R-C02", "乙", "2026-05-02T10:00:00Z", 240_000),
        ],
    )
    body = publish(
        client,
        2,
        supplements=[supplement("R-C03", "丙", "2026-05-01T09:00:00Z", 260_000)],
        withdrawals=["R-C02"],
    ).json()
    assert kinds(body) == [
        (1, "first", "R-C03"),
        (2, "refresh", "R-C01"),
    ]
    assert body["supplement_count"] == 1
    assert body["withdrawal_count"] == 1


def test_tie_does_not_replace_the_record_holder(client):
    publish(
        client,
        1,
        supplements=[supplement("R-D01", "甲", "2026-05-01T10:00:00Z", 240_000)],
    )
    body = publish(
        client,
        2,
        supplements=[supplement("R-D02", "乙", "2026-05-02T10:00:00Z", 240_000)],
    ).json()
    assert kinds(body) == [
        (1, "first", "R-D01"),
        (2, "tie", "R-D02"),
    ]
    # A later, slower-than-the-record run is not listed even though it is
    # faster than the tie.
    body = publish(
        client,
        3,
        supplements=[supplement("R-D03", "丙", "2026-05-03T10:00:00Z", 245_000)],
    ).json()
    assert kinds(body) == [
        (1, "first", "R-D01"),
        (2, "tie", "R-D02"),
    ]


def test_concurrent_announcements_on_same_base_only_one_wins(tmp_path):
    """Two racing publishes based on the same version: exactly one commits."""

    import threading

    import app.database as database
    import app.service as service
    from app.schemas import AnnouncementIn

    db_path = str(tmp_path / "race.db")
    database.init_db(db_path)
    service.ensure_baseline(db_path)

    barrier = threading.Barrier(2)
    outcomes = []

    def worker(result_id: str, finish_time: str) -> None:
        announcement = AnnouncementIn(
            base_version=1,
            supplements=[
                {
                    "result_id": result_id,
                    "athlete": result_id,
                    "finish_time": finish_time,
                    "duration_ms": 240_000,
                }
            ],
        )
        barrier.wait()
        try:
            outcomes.append(("ok", service.publish(db_path, announcement)))
        except Exception as exc:  # noqa: BLE001 - recorded for assertions
            outcomes.append(("error", getattr(exc, "code", str(exc))))

    threads = [
        threading.Thread(target=worker, args=("R-RACE-1", "2026-05-01T10:00:00Z")),
        threading.Thread(target=worker, args=("R-RACE-2", "2026-05-02T10:00:00Z")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [item for item in outcomes if item[0] == "ok"]
    failures = [item for item in outcomes if item[0] == "error"]
    assert len(successes) == 1, outcomes
    assert len(failures) == 1, outcomes
    assert failures[0][1] == "stale_base_version"
    assert service.latest_version(db_path) == 2
    assert len(service.list_versions(db_path)) == 2
    detail = service.get_version(db_path, 2)
    assert len(detail["active_results"]) == 1


def test_future_base_version_is_rejected(client):
    response = publish(
        client,
        99,
        supplements=[supplement("R-F01", "甲", "2026-05-01T10:00:00Z", 240_000)],
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "base_version_not_published"
    assert client.get("/versions/latest").json()["version"] == 1
