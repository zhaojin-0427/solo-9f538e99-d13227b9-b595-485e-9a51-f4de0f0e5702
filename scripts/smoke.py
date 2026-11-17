#!/usr/bin/env python3
"""End-to-end smoke test against a running server.

Usage: python scripts/smoke.py [base_url]
Exits non-zero on the first failed expectation.
"""
from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
MIN = "2026-11-17T05:06:00Z"
ST = "SMOKE"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


def main() -> int:
    http = httpx.Client(base_url=BASE, timeout=10)

    def frag(no, rev, start, samples, station=ST, minute=MIN):
        return {
            "station": station,
            "minute_utc": minute,
            "fragment_no": no,
            "revision": rev,
            "start_sample": start,
            "samples": samples,
        }

    # 1. Out-of-order arrival: fragment 2 lands before fragment 1.
    r = http.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    check("upload fragment 2 first", r.status_code == 201, r.json().get("status", ""))
    r = http.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    check("upload fragment 1", r.status_code == 201)

    # 2. Assembly is complete and conflict free.
    body = http.get("/assembly", params={"station": ST, "minute_utc": MIN}).json()
    check("assembly complete", body["complete"] is True, f"coverage={body['coverage']}")
    check("no gaps", body["gaps"] == [])
    check("no conflicts", body["conflicts"] == [])

    # 3. Boundary rejection.
    r = http.post("/fragments", json=frag(9, 1, 58, [1, 2, 3]))
    check("out-of-bounds rejected", r.status_code == 422, r.json()["detail"])

    # 4. Idempotent retry: same version + same content -> unchanged, no dup row.
    r = http.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    check("same-version same-content retry idempotent",
          r.status_code == 201 and r.json()["status"] == "unchanged")
    rows = http.get("/fragments", params={"station": ST, "minute_utc": MIN}).json()
    check("no duplicate fragment row", len(rows) == 2)

    # 5. Same version, different content -> rejected.
    r = http.post("/fragments", json=frag(1, 1, 0, [999] + list(range(1, 30))))
    check("same-version different-content rejected", r.status_code == 409)

    # 6. Lower revision -> rejected.
    r = http.post("/fragments", json=frag(1, 0, 0, [1]))
    check("revision zero rejected at validation", r.status_code == 422)

    # 7. Publish the complete minute.
    r = http.post("/publications", params={"station": ST, "minute_utc": MIN})
    check("publish succeeds", r.status_code == 201)
    pub1 = r.json()
    check("wave is 60 samples", pub1["wave"] == list(range(60)))
    check("published as fresh", pub1["stale"] is False)
    check("pins two fragment revisions",
          sorted((i["fragment_no"], i["revision"]) for i in pub1["items"])
          == [(1, 1), (2, 1)])

    # Re-publishing the identical set returns the same record.
    r = http.post("/publications", params={"station": ST, "minute_utc": MIN})
    check("republish identical set is idempotent",
          r.status_code == 201 and r.json()["id"] == pub1["id"])

    # 8. Correction arrives: higher revision changes current assembly.
    r = http.post("/fragments", json=frag(1, 2, 0, [42] + list(range(1, 30))))
    check("higher revision accepted", r.status_code == 201
          and r.json()["status"] == "superseded")

    # 9. Old record is still readable, frozen, and marked stale.
    old = http.get(f"/publications/{pub1['id']}").json()
    check("old record still readable", old["id"] == pub1["id"])
    check("old wave frozen", old["wave"] == list(range(60)))
    check("old revisions frozen", old["items"][0]["revision"] == 1)
    check("old record marked stale", old["stale"] is True)

    # 10. Publish the new fragment set; latest points to it.
    r = http.post("/publications", params={"station": ST, "minute_utc": MIN})
    check("republish after correction", r.status_code == 201)
    pub2 = r.json()
    check("new record id differs", pub2["id"] != pub1["id"])
    check("new wave contains correction", pub2["wave"][0] == 42
          and pub2["wave"][1:] == list(range(1, 60)))
    check("new record fresh", pub2["stale"] is False)
    latest = http.get("/publications/latest",
                      params={"station": ST, "minute_utc": MIN}).json()
    check("latest endpoint returns new record", latest["id"] == pub2["id"])
    listing = http.get("/publications",
                       params={"station": ST, "minute_utc": MIN}).json()
    check("both records retained", [p["id"] for p in listing]
          == [pub1["id"], pub2["id"]])
    check("staleness flags", [p["stale"] for p in listing] == [True, False])

    # 11. Conflict reporting on a second station-minute.
    http.post("/fragments", json=frag(1, 1, 0, [7, 8, 9], station="BAD"))
    http.post("/fragments", json=frag(2, 1, 2, [1, 2], station="BAD"))
    body = http.get("/assembly", params={"station": "BAD", "minute_utc": MIN}).json()
    check("conflict located with fragment numbers",
          body["conflicts"] == [{"position": 2, "fragment_nos": [1, 2],
                                 "values": [9, 1]}])
    check("gaps listed", body["gaps"] == list(range(4, 60)))
    r = http.post("/publications", params={"station": "BAD", "minute_utc": MIN})
    check("conflicted minute cannot publish", r.status_code == 409)
    r = http.get("/publications", params={"station": "BAD"})
    check("failed publish left no rows", r.json() == [])

    # 12. Missing minute publication -> 404.
    r = http.get("/publications/latest",
                 params={"station": "NOPE", "minute_utc": MIN})
    check("unknown publication 404", r.status_code == 404)

    # 13. Calibration validation: zero gain and sub-second times are rejected.
    r = http.post("/calibrations", json={"station": ST,
                  "effective_at": "2026-11-17T05:05:00Z",
                  "revision": 1, "zero_count": 10, "gain": 0})
    check("zero gain rejected", r.status_code == 422)
    r = http.post("/calibrations", json={"station": ST,
                  "effective_at": "2026-11-17T05:05:00.5Z",
                  "revision": 1, "zero_count": 10, "gain": 2})
    check("sub-second effective_at rejected", r.status_code == 422)

    # 14. Calibration revision semantics mirror fragments.
    r = http.post("/calibrations", json={"station": ST,
                  "effective_at": "2026-11-17T05:05:00Z",
                  "revision": 1, "zero_count": 10, "gain": 2})
    check("calibration accepted", r.status_code == 201
          and r.json()["status"] == "accepted")
    r = http.post("/calibrations", json={"station": ST,
                  "effective_at": "2026-11-17T05:05:00Z",
                  "revision": 1, "zero_count": 10, "gain": 2})
    check("calibration idempotent retry", r.status_code == 201
          and r.json()["status"] == "unchanged")
    r = http.post("/calibrations", json={"station": ST,
                  "effective_at": "2026-11-17T05:05:00Z",
                  "revision": 1, "zero_count": 11, "gain": 2})
    check("calibration same-revision different-content rejected",
          r.status_code == 409)
    r = http.post("/calibrations", json={"station": ST,
                  "effective_at": "2026-11-17T05:05:00Z",
                  "revision": 2, "zero_count": 10, "gain": 3})
    check("calibration higher revision supersedes", r.status_code == 201
          and r.json()["status"] == "superseded")
    rows = http.get("/calibrations", params={"station": ST}).json()
    check("single calibration row per effective time", len(rows) == 1
          and rows[0]["revision"] == 2)

    # 15. Convert the current (corrected, re-published) minute to microgals.
    r = http.post("/conversions", params={"station": ST, "minute_utc": MIN})
    check("conversion succeeds", r.status_code == 201)
    conv1 = r.json()
    expected_wave = [42] + list(range(1, 60))
    check("microgal values match (count-zero)*gain",
          conv1["values"] == [(v - 10) * 3 for v in expected_wave])
    check("60 per-slot items with pinned calibration revision",
          len(conv1["items"]) == 60
          and all(it["revision"] == 2 for it in conv1["items"]))
    check("conversion pins source publication",
          conv1["publication_id"] == pub2["id"])

    # 16. Re-conversion of identical sources is idempotent; reads by id/latest.
    r = http.post("/conversions", params={"station": ST, "minute_utc": MIN})
    check("re-conversion idempotent", r.status_code == 201
          and r.json()["id"] == conv1["id"])
    check("conversion readable by id",
          http.get(f"/conversions/{conv1['id']}").json()["values"]
          == conv1["values"])
    check("conversion latest endpoint",
          http.get("/conversions/latest",
                   params={"station": ST, "minute_utc": MIN}).json()["id"]
          == conv1["id"])

    # 17. Backfill a later calibration: slots >=30 switch to it; old record
    # stays readable but is marked stale, new conversion carries provenance.
    r = http.post("/calibrations", json={"station": ST,
                  "effective_at": "2026-11-17T05:06:30Z",
                  "revision": 1, "zero_count": 0, "gain": 1})
    check("later calibration accepted", r.status_code == 201)
    check("old conversion marked stale after backfill",
          http.get(f"/conversions/{conv1['id']}").json()["stale"] is True)
    conv2 = http.post(
        "/conversions", params={"station": ST, "minute_utc": MIN}).json()
    check("new conversion uses both calibrations",
          {it["effective_at"] for it in conv2["items"][:30]}
          == {"2026-11-17T05:05:00Z"}
          and {it["effective_at"] for it in conv2["items"][30:]}
          == {"2026-11-17T05:06:30Z"})
    check("new conversion values per slot",
          conv2["values"][:30] == [(v - 10) * 3 for v in expected_wave[:30]]
          and conv2["values"][30:] == expected_wave[30:])
    listing = http.get(
        "/conversions", params={"station": ST, "minute_utc": MIN}).json()
    check("conversion history retained with stale flags",
          [c["id"] for c in listing] == [conv1["id"], conv2["id"]]
          and [c["stale"] for c in listing] == [True, False])

    # 18. Missing calibration for any slot rejects the whole minute cleanly.
    http.post("/fragments", json=frag(1, 1, 0, list(range(30)), station="NOCAL"))
    http.post("/fragments", json=frag(2, 1, 30, list(range(30, 60)), station="NOCAL"))
    http.post("/publications", params={"station": "NOCAL", "minute_utc": MIN})
    r = http.post("/conversions", params={"station": "NOCAL", "minute_utc": MIN})
    check("minute without calibration rejected", r.status_code == 409)
    check("failed conversion leaves no rows",
          http.get("/conversions", params={"station": "NOCAL"}).json() == [])

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
