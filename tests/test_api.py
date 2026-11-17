"""End-to-end tests for the waveform assembly API.

Each test gets an isolated SQLite file so runs never touch real data.
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
    tmpdir = tempfile.mkdtemp(prefix="wf-test-")
    db_path = Path(tmpdir) / "test.db"
    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))

    # Import after the env var is set so config picks up the test database.
    import importlib

    from app import config, database, main, service

    # Reload in dependency order so every module re-reads the test DB path.
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


def frag(no, rev, start, samples, station="STA1", minute=MIN):
    return {
        "station": station,
        "minute_utc": minute,
        "fragment_no": no,
        "revision": rev,
        "start_sample": start,
        "samples": samples,
    }


# --------------------------------------------------------------------------- #
# Health & validation
# --------------------------------------------------------------------------- #
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_boundary_rejected(client):
    # start 55 + 6 samples reaches slot 61 -> out of bounds
    r = client.post("/fragments", json=frag(1, 1, 55, [1, 2, 3, 4, 5, 6]))
    assert r.status_code == 422
    assert "boundary" in r.json()["detail"]


def test_non_whole_minute_rejected(client):
    payload = frag(1, 1, 0, [1])
    payload["minute_utc"] = "2026-11-17T05:06:30Z"
    r = client.post("/fragments", json=payload)
    assert r.status_code == 422


def test_revision_must_be_positive(client):
    r = client.post("/fragments", json=frag(1, 0, 0, [1]))
    assert r.status_code == 422


def test_empty_samples_rejected(client):
    r = client.post("/fragments", json=frag(1, 1, 0, []))
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Revision semantics
# --------------------------------------------------------------------------- #
def test_upload_and_retrieve(client):
    r = client.post("/fragments", json=frag(1, 1, 0, [10, 20]))
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "accepted"
    assert body["revision"] == 1
    assert len(body["content_hash"]) == 64

    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_lower_revision_rejected(client):
    client.post("/fragments", json=frag(1, 3, 0, [10, 20]))
    r = client.post("/fragments", json=frag(1, 2, 0, [99]))
    assert r.status_code == 409
    assert "not higher" in r.json()["detail"]
    # stored fragment untouched
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()[0]["revision"] == 3


def test_same_revision_same_content_idempotent(client):
    r1 = client.post("/fragments", json=frag(1, 2, 0, [10, 20]))
    r2 = client.post("/fragments", json=frag(1, 2, 0, [10, 20]))
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r2.json()["status"] == "unchanged"
    assert r1.json()["content_hash"] == r2.json()["content_hash"]
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert len(r.json()) == 1


def test_same_revision_different_content_rejected(client):
    client.post("/fragments", json=frag(1, 2, 0, [10, 20]))
    r = client.post("/fragments", json=frag(1, 2, 0, [10, 21]))
    assert r.status_code == 409
    assert "different content" in r.json()["detail"]
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()[0]["samples"] == [10, 20]


def test_higher_revision_supersedes(client):
    client.post("/fragments", json=frag(1, 1, 0, [10, 20]))
    r = client.post("/fragments", json=frag(1, 2, 0, [11, 21]))
    assert r.status_code == 201
    assert r.json()["status"] == "superseded"
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()[0]["revision"] == 2
    assert r.json()[0]["samples"] == [11, 21]


# --------------------------------------------------------------------------- #
# Assembly: gaps and conflicts
# --------------------------------------------------------------------------- #
def test_assembly_gaps(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["complete"] is False
    assert body["coverage"] == 3
    assert body["gaps"] == list(range(3, 60))
    assert body["conflicts"] == []


def test_assembly_conflict_reports_position_and_fragments(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    client.post("/fragments", json=frag(2, 1, 2, [9, 4]))  # overlap at slot 2
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["complete"] is False
    assert body["conflicts"] == [
        {"position": 2, "fragment_nos": [1, 2], "values": [3, 9]}
    ]
    assert body["gaps"] == list(range(4, 60))


def test_overlap_same_value_is_not_conflict(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    client.post("/fragments", json=frag(2, 1, 2, [3, 4]))  # slot 2 agrees
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["conflicts"] == []
    assert body["coverage"] == 4
    assert body["gaps"] == list(range(4, 60))


def test_complete_minute_assembly(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["complete"] is True
    assert body["coverage"] == 60
    assert body["gaps"] == []
    assert body["conflicts"] == []


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #
def test_publish_fails_with_gaps(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2]))
    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert "gaps" in r.json()["detail"]


def test_publish_fails_with_conflict(client):
    client.post("/fragments", json=frag(1, 1, 0, [1] + [0] * 29))
    client.post("/fragments", json=frag(2, 1, 0, [9] + [0] * 59))
    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert "conflicts" in r.json()["detail"]
    # failed publish leaves nothing behind
    r = client.get("/publications", params={"station": "STA1"})
    assert r.json() == []


def test_full_publish_lifecycle(client):
    wave_a = list(range(60))
    client.post("/fragments", json=frag(1, 1, 0, wave_a[:30]))
    client.post("/fragments", json=frag(2, 1, 30, wave_a[30:]))

    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 201
    pub = r.json()
    pub_id = pub["id"]
    assert pub["wave"] == wave_a
    assert pub["stale"] is False
    assert sorted(it["fragment_no"] for it in pub["items"]) == [1, 2]
    assert {it["revision"] for it in pub["items"]} == {1}
    # revisions and per-fragment waveforms are pinned in the record
    pinned = {it["fragment_no"]: it["samples"] for it in pub["items"]}
    assert pinned[1] == wave_a[:30]
    assert pinned[2] == wave_a[30:]

    # publishing the same fragment set again is idempotent
    r2 = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r2.json()["id"] == pub_id

    # a later correction changes current assembly but must not mutate the record
    client.post("/fragments", json=frag(1, 2, 0, [100] + wave_a[1:30]))
    r = client.get(f"/publications/{pub_id}")
    old = r.json()
    assert old["wave"] == wave_a  # frozen waveform
    assert old["stale"] is True
    assert old["items"][0]["revision"] == 1  # frozen revisions

    # the new fragment set can itself be published
    new_wave = [100] + wave_a[1:]
    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 201
    pub2 = r.json()
    assert pub2["id"] != pub_id
    assert pub2["wave"] == new_wave
    assert pub2["stale"] is False

    # both records remain readable; latest endpoint returns the new one
    r = client.get(
        "/publications/latest",
        params={"station": "STA1", "minute_utc": MIN},
    )
    assert r.json()["id"] == pub2["id"]
    r = client.get("/publications", params={"station": "STA1", "minute_utc": MIN})
    ids = [p["id"] for p in r.json()]
    assert ids == [pub_id, pub2["id"]]
    assert [p["stale"] for p in r.json()] == [True, False]


def test_supersede_marks_previous_publication_stale(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert pub["stale"] is False

    # replacement fragment at same number, higher revision, different span
    client.post("/fragments", json=frag(2, 2, 29, [77] + list(range(30, 60))))
    r = client.get(f"/publications/{pub['id']}")
    assert r.json()["stale"] is True


def test_idempotent_retry_does_not_stale_publication(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    # same version, same content retransmission: fragment set unchanged
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    r = client.get(f"/publications/{pub['id']}")
    assert r.json()["stale"] is False
    # no duplicate publication either
    again = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert again["id"] == pub["id"]


# --------------------------------------------------------------------------- #
# Restart durability
# --------------------------------------------------------------------------- #
def test_data_survives_restart(client, monkeypatch, tmp_path):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    # Simulate a process restart: rebuild app objects against the same file.
    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    with TestClient(main.app) as restarted:
        r = restarted.get(f"/publications/{pub['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["wave"] == list(range(60))
        assert body["stale"] is False
        r = restarted.get(
            "/assembly", params={"station": "STA1", "minute_utc": MIN}
        )
        assert r.json()["complete"] is True


# --------------------------------------------------------------------------- #
# Concurrency: interleaved uploads and publishes serialize on one snapshot
# --------------------------------------------------------------------------- #
def test_concurrent_uploads_and_publishes(monkeypatch):
    """Hit a real uvicorn server with parallel uploads/publishes.

    Every publication must come from one committed fragment set and failed
    publishes must never leave partial rows behind.
    """
    import socket
    import time

    import httpx
    import uvicorn

    tmpdir = tempfile.mkdtemp(prefix="wf-live-")
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

    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def worker(idx: int) -> None:
        try:
            no = idx + 1
            barrier.wait()
            r = httpx.post(
                base + "/fragments",
                json=frag(no, 1, (no - 1) * 10, [no * 100] * 10),
                timeout=10,
            )
            assert r.status_code == 201, r.text
            rp = httpx.post(
                base + "/publications",
                params={"station": "STA1", "minute_utc": MIN},
                timeout=10,
            )
            # incomplete snapshots fail cleanly; the complete one succeeds —
            # a 500/half-written row would be a correctness bug
            assert rp.status_code in (201, 409), rp.text
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    with httpx.Client(base_url=base, timeout=10) as http:
        r = http.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
        assert len(r.json()) == 6
        pubs = http.post(
            "/publications", params={"station": "STA1", "minute_utc": MIN}
        ).json()
        assert pubs["wave"] == [
            v for no in range(1, 7) for v in [no * 100] * 10
        ]
        listing = http.get(
            "/publications", params={"station": "STA1", "minute_utc": MIN}
        ).json()
        # only one publication row for a single fragment set
        assert len(listing) == 1

    server.should_exit = True
    thread.join(timeout=10)


def test_station_isolation(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3], station="STA1"))
    client.post("/fragments", json=frag(1, 1, 0, [4, 5, 6], station="STA2"))
    r = client.get("/fragments", params={"station": "STA2", "minute_utc": MIN})
    assert r.json()[0]["samples"] == [4, 5, 6]
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()["coverage"] == 3
