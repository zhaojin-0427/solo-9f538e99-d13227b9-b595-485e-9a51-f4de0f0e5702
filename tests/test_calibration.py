"""End-to-end tests for calibration records and count->microgal conversions.

Mirrors the isolation pattern of test_api.py: every test gets its own SQLite
file and reloaded application modules.
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="cal-test-")
    db_path = Path(tmpdir) / "test.db"
    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))

    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    os.rmdir(tmpdir)


MIN = "2026-11-17T05:06:00Z"
CAL_T0 = "2026-11-17T05:05:00Z"
CAL_MID = "2026-11-17T05:06:30Z"


def frag(no, rev, start, samples, station="STA1", minute=MIN):
    return {
        "station": station,
        "minute_utc": minute,
        "fragment_no": no,
        "revision": rev,
        "start_sample": start,
        "samples": samples,
    }


def cal(effective_at, rev, zero, gain, station="STA1"):
    return {
        "station": station,
        "effective_at": effective_at,
        "revision": rev,
        "zero_count": zero,
        "gain": gain,
    }


def publish_complete_minute(client, station="STA1", wave=None):
    if wave is None:
        wave = list(range(60))
    r = client.post("/fragments", json=frag(1, 1, 0, wave[:30], station=station))
    assert r.status_code == 201
    r = client.post("/fragments", json=frag(2, 1, 30, wave[30:], station=station))
    assert r.status_code == 201
    r = client.post(
        "/publications", params={"station": station, "minute_utc": MIN}
    )
    assert r.status_code == 201
    return r.json()


# --------------------------------------------------------------------------- #
# Calibration validation and revision semantics
# --------------------------------------------------------------------------- #
def test_calibration_validation(client):
    r = client.post("/calibrations", json=cal(CAL_T0, 1, 10, 0))
    assert r.status_code == 422
    r = client.post("/calibrations", json=cal(CAL_T0, 0, 10, 2))
    assert r.status_code == 422
    r = client.post(
        "/calibrations", json=cal("2026-11-17T05:05:00.500000Z", 1, 10, 2)
    )
    assert r.status_code == 422
    r = client.post(
        "/calibrations",
        json={**cal(CAL_T0, 1, 10, 2), "station": "   "},
    )
    assert r.status_code == 422


def test_calibration_upload_and_list(client):
    r = client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "accepted"
    assert body["revision"] == 1
    assert len(body["content_hash"]) == 64

    rows = client.get("/calibrations", params={"station": "STA1"}).json()
    assert len(rows) == 1
    assert rows[0]["effective_at"].endswith("Z")
    assert rows[0]["gain"] == 2


def test_calibration_same_revision_idempotent(client):
    r1 = client.post("/calibrations", json=cal(CAL_T0, 2, 10, 2))
    r2 = client.post("/calibrations", json=cal(CAL_T0, 2, 10, 2))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r2.json()["status"] == "unchanged"
    assert r1.json()["id"] == r2.json()["id"]
    rows = client.get("/calibrations", params={"station": "STA1"}).json()
    assert len(rows) == 1


def test_calibration_same_revision_different_content_rejected(client):
    client.post("/calibrations", json=cal(CAL_T0, 2, 10, 2))
    r = client.post("/calibrations", json=cal(CAL_T0, 2, 11, 2))
    assert r.status_code == 409
    r = client.post("/calibrations", json=cal(CAL_T0, 2, 10, 3))
    assert r.status_code == 409
    rows = client.get("/calibrations", params={"station": "STA1"}).json()
    assert rows[0]["zero_count"] == 10 and rows[0]["gain"] == 2


def test_calibration_lower_revision_rejected(client):
    client.post("/calibrations", json=cal(CAL_T0, 3, 10, 2))
    r = client.post("/calibrations", json=cal(CAL_T0, 2, 10, 2))
    assert r.status_code == 409
    assert "not higher" in r.json()["detail"]
    rows = client.get("/calibrations", params={"station": "STA1"}).json()
    assert rows[0]["revision"] == 3


def test_calibration_higher_revision_supersedes(client):
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    r = client.post("/calibrations", json=cal(CAL_T0, 2, 5, -3))
    assert r.status_code == 201
    assert r.json()["status"] == "superseded"
    rows = client.get("/calibrations", params={"station": "STA1"}).json()
    assert len(rows) == 1
    assert rows[0]["revision"] == 2
    assert rows[0]["gain"] == -3  # negative gains are legal, only zero is banned


def test_calibration_station_isolation(client):
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2, station="STA1"))
    client.post("/calibrations", json=cal(CAL_T0, 1, 20, 4, station="STA2"))
    s1 = client.get("/calibrations", params={"station": "STA1"}).json()
    s2 = client.get("/calibrations", params={"station": "STA2"}).json()
    assert s1[0]["zero_count"] == 10 and s2[0]["zero_count"] == 20


# --------------------------------------------------------------------------- #
# Conversion preconditions
# --------------------------------------------------------------------------- #
def test_convert_without_publication_rejected(client):
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    r = client.post("/conversions", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert "no publication" in r.json()["detail"]
    assert client.get("/conversions").json() == []


def test_convert_without_calibration_rejects_whole_minute(client):
    publish_complete_minute(client)
    r = client.post("/conversions", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert "no calibration" in r.json()["detail"]
    # failure leaves no half-written record behind
    assert client.get("/conversions").json() == []


def test_convert_refuses_stale_publication(client):
    pub = publish_complete_minute(client)
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    # correction makes the publication stale
    client.post(
        "/fragments", json=frag(1, 2, 0, [99] + list(range(1, 30)))
    )
    r = client.post("/conversions", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert "stale" in r.json()["detail"]
    assert client.get("/conversions").json() == []

    # after re-publishing, conversion works and pins the new publication
    pub2 = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    r = client.post("/conversions", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 201
    assert r.json()["publication_id"] == pub2["id"]
    assert r.json()["publication_id"] != pub["id"]


# --------------------------------------------------------------------------- #
# Conversion math and provenance
# --------------------------------------------------------------------------- #
def test_basic_conversion_math_and_reads(client):
    pub = publish_complete_minute(client, wave=list(range(60)))
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    r = client.post("/conversions", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 201
    conv = r.json()
    assert conv["values"] == [(i - 10) * 2 for i in range(60)]
    assert len(conv["items"]) == 60
    assert all(it["revision"] == 1 for it in conv["items"])
    assert all(it["effective_at"] == CAL_T0 for it in conv["items"])
    assert conv["publication_id"] == pub["id"]
    assert conv["stale"] is False
    assert len(conv["calibration_set_hash"]) == 64

    # readable by id, via latest, and in the listing
    cid = conv["id"]
    assert client.get(f"/conversions/{cid}").json()["values"] == conv["values"]
    assert (
        client.get(
            "/conversions/latest",
            params={"station": "STA1", "minute_utc": MIN},
        ).json()["id"]
        == cid
    )
    assert [c["id"] for c in client.get("/conversions").json()] == [cid]


def test_conversion_idempotent_for_same_sources(client):
    publish_complete_minute(client)
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    r1 = client.post("/conversions", params={"station": "STA1", "minute_utc": MIN})
    r2 = client.post("/conversions", params={"station": "STA1", "minute_utc": MIN})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    assert len(client.get("/conversions").json()) == 1


def test_nearest_effective_calibration_per_slot(client):
    wave = list(range(60))
    publish_complete_minute(client, wave=wave)
    # calibration effective at second 30 of the minute: slots 0..29 keep T0,
    # slots 30..59 take the nearer, later record
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    client.post("/calibrations", json=cal(CAL_MID, 1, 10, 4))
    conv = client.post(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    expected = [(i - 10) * 2 for i in range(30)] + [
        (i - 10) * 4 for i in range(30, 60)
    ]
    assert conv["values"] == expected
    assert all(it["effective_at"] == CAL_T0 for it in conv["items"][:30])
    assert all(it["effective_at"] == CAL_MID for it in conv["items"][30:])


def test_calibration_starting_inside_minute_still_covers_all_slots(client):
    # No calibration before the minute; one effective at second 0 exactly must
    # cover slot 0 (<= semantics), while a later one alone must fail slot 0.
    wave = [5] * 60
    publish_complete_minute(client, wave=wave)
    client.post("/calibrations", json=cal(MIN, 1, 1, 1))  # effective :05:06:00
    conv = client.post(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert conv["values"] == [4] * 60


def test_missing_calibration_for_early_slots(client):
    publish_complete_minute(client)
    # effective only at second 30 -> slots 0..29 uncovered -> whole reject
    client.post("/calibrations", json=cal(CAL_MID, 1, 10, 2))
    r = client.post("/conversions", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert "2026-11-17T05:06:00Z" in r.json()["detail"]
    assert client.get("/conversions").json() == []


# --------------------------------------------------------------------------- #
# Staleness: fragment corrections and calibration changes
# --------------------------------------------------------------------------- #
def test_calibration_revision_marks_conversion_stale(client):
    publish_complete_minute(client)
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    conv = client.post(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    client.post("/calibrations", json=cal(CAL_T0, 2, 10, 3))
    old = client.get(f"/conversions/{conv['id']}").json()
    assert old["stale"] is True
    assert old["values"] == conv["values"]  # frozen result still readable

    new = client.post(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert new["id"] != conv["id"] and new["stale"] is False
    assert new["values"] == [(i - 10) * 3 for i in range(60)]

    listing = client.get(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert [c["stale"] for c in listing] == [True, False]


def test_calibration_backfill_marks_affected_conversion_stale(client):
    publish_complete_minute(client)
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    conv = client.post(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    # backfill inside the minute changes provenance for slots 30..59
    client.post("/calibrations", json=cal(CAL_MID, 1, 10, 2))
    assert client.get(f"/conversions/{conv['id']}").json()["stale"] is True
    new = client.post(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert new["id"] != conv["id"]
    assert {it["effective_at"] for it in new["items"][30:]} == {CAL_MID}


def test_fragment_correction_marks_conversion_stale(client):
    publish_complete_minute(client)
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    conv = client.post(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    client.post("/fragments", json=frag(1, 2, 0, [42] + list(range(1, 30))))
    body = client.get(f"/conversions/{conv['id']}").json()
    assert body["stale"] is True
    assert body["values"] == conv["values"]  # frozen


def test_idempotent_calibration_retry_keeps_conversion_fresh(client):
    publish_complete_minute(client)
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    conv = client.post(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    # identical retransmission changes nothing
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    assert client.get(f"/conversions/{conv['id']}").json()["stale"] is False
    again = client.post(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert again["id"] == conv["id"]


def test_conversion_404s(client):
    assert client.get("/conversions/123").status_code == 404
    r = client.get(
        "/conversions/latest",
        params={"station": "NOPE", "minute_utc": MIN},
    )
    assert r.status_code == 404


def test_conversion_survives_restart(client, monkeypatch):
    publish_complete_minute(client)
    client.post("/calibrations", json=cal(CAL_T0, 1, 10, 2))
    conv = client.post(
        "/conversions", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    with TestClient(main.app) as restarted:
        r = restarted.get(f"/conversions/{conv['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["values"] == [(i - 10) * 2 for i in range(60)]
        assert body["stale"] is False
        assert len(body["items"]) == 60


# --------------------------------------------------------------------------- #
# Concurrency: interleaved calibration writes and conversions
# --------------------------------------------------------------------------- #
def test_concurrent_calibrations_and_conversions(monkeypatch):
    """Conversions must never pin an invalidated source and failures must be
    clean (no half-written rows) when writes interleave with conversions."""
    import socket
    import time

    import httpx
    import uvicorn

    tmpdir = tempfile.mkdtemp(prefix="cal-live-")
    db_path = Path(tmpdir) / "live.db"
    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))

    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:  # pragma: no cover
        raise RuntimeError("test server did not start")

    with httpx.Client(base_url=base, timeout=15) as http:
        http.post("/fragments", json=frag(1, 1, 0, list(range(30))))
        http.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
        r = http.post(
            "/publications", params={"station": "STA1", "minute_utc": MIN}
        )
        assert r.status_code == 201
        # baseline calibration so every conversion has complete coverage
        http.post("/calibrations", json=cal(CAL_T0, 1, 0, 1))

    errors: list[Exception] = []
    barrier = threading.Barrier(8)
    # Unique revisions per writer; arrival order is nondeterministic so a
    # lower revision may legitimately lose the race with 409.
    submitted_revisions = [2 + idx for idx in range(0, 8, 2)]

    def worker(idx: int) -> None:
        try:
            barrier.wait()
            with httpx.Client(base_url=base, timeout=15) as http:
                if idx % 2 == 0:
                    # push ever-higher revisions of the same calibration
                    rev = 2 + idx
                    r = http.post(
                        "/calibrations",
                        json=cal(CAL_T0, rev, 0, 1 + idx),
                    )
                    assert r.status_code in (201, 409), r.text
                else:
                    r = http.post(
                        "/conversions",
                        params={"station": "STA1", "minute_utc": MIN},
                    )
                    assert r.status_code in (201, 409), r.text
                    if r.status_code == 201:
                        body = r.json()
                        assert len(body["values"]) == 60
                        assert len(body["items"]) == 60
                        assert body["publication_id"] >= 1
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    with httpx.Client(base_url=base, timeout=15) as http:
        convs = http.get("/conversions").json()
        for conv in convs:
            # every stored conversion is internally complete and consistent
            assert len(conv["values"]) == 60
            assert len(conv["items"]) == 60
            assert [it["value"] for it in conv["items"]] == conv["values"]
        # all but the highest revision have been superseded; exactly one
        # calibration row remains, carrying the highest submitted revision
        cals = http.get("/calibrations", params={"station": "STA1"}).json()
        assert len(cals) == 1
        assert cals[0]["revision"] == max(submitted_revisions)
        # a final conversion against the settled calibration set must succeed
        r = http.post(
            "/conversions", params={"station": "STA1", "minute_utc": MIN}
        )
        assert r.status_code == 201
        final = r.json()
        gain = cals[0]["gain"]
        assert final["values"] == [i * gain for i in range(60)]
        assert all(it["revision"] == cals[0]["revision"] for it in final["items"])

    server.should_exit = True
    thread.join(timeout=10)
