"""Business logic: fragment uploads, minute assembly and publications.

All mutating operations run inside a single ``BEGIN IMMEDIATE`` SQLite
transaction so that uploads and publications serialize on one consistent
snapshot of the fragment set.  Any error rolls the whole transaction back —
no half-written publications are ever left behind.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import SAMPLES_PER_MINUTE
from .database import get_conn


# --------------------------------------------------------------------------- #
# Errors (mapped to HTTP status codes in the router)
# --------------------------------------------------------------------------- #
class ServiceError(Exception):
    status_code = 400


class BoundaryError(ServiceError):
    status_code = 422


class StaleRevisionError(ServiceError):
    """Incoming revision is not higher than the one already stored."""

    status_code = 409


class ContentConflictError(ServiceError):
    """Same fragment_no + revision but different payload."""

    status_code = 409


class NotPublishableError(ServiceError):
    status_code = 409


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def minute_key(dt: datetime) -> str:
    """Canonical UTC minute string, e.g. ``2026-11-17T05:06:00Z``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:00Z")


def dump_samples(samples: list[int]) -> str:
    return json.dumps(samples, separators=(",", ":"))


def load_samples(raw: str) -> list[int]:
    return json.loads(raw)


def content_hash(start_sample: int, samples: list[int]) -> str:
    payload = json.dumps(
        {"start_sample": start_sample, "samples": samples}, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fragment_set_hash(rows: list[sqlite3.Row]) -> str:
    """Stable hash of the *effective* fragment set of a station-minute."""
    canonical = [
        {
            "fragment_no": r["fragment_no"],
            "revision": r["revision"],
            "start_sample": r["start_sample"],
            "samples": load_samples(r["samples"]),
            "content_hash": r["content_hash"],
        }
        for r in rows
    ]
    canonical.sort(key=lambda f: f["fragment_no"])
    blob = json.dumps(canonical, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _parse_minute(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _row_to_fragment(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "station": r["station"],
        "minute_utc": _parse_minute(r["minute_utc"]),
        "fragment_no": r["fragment_no"],
        "revision": r["revision"],
        "start_sample": r["start_sample"],
        "samples": load_samples(r["samples"]),
        "content_hash": r["content_hash"],
    }


def _get_fragments(
    conn: sqlite3.Connection, station: str, minute: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fragments WHERE station = ? AND minute_utc = ? "
        "ORDER BY fragment_no",
        (station, minute),
    ).fetchall()


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def compute_assembly(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Compute coverage, gaps and value conflicts for one station-minute."""
    # position -> list of (fragment_no, value)
    covered: dict[int, list[tuple[int, int]]] = {p: [] for p in range(SAMPLES_PER_MINUTE)}
    for r in rows:
        start = r["start_sample"]
        for offset, value in enumerate(load_samples(r["samples"])):
            covered[start + offset].append((r["fragment_no"], value))

    gaps: list[int] = []
    conflicts: list[dict[str, Any]] = []
    coverage = 0
    for pos in range(SAMPLES_PER_MINUTE):
        entries = covered[pos]
        if not entries:
            gaps.append(pos)
            continue
        coverage += 1
        distinct_values = {value for _, value in entries}
        if len(distinct_values) > 1:
            conflicts.append(
                {
                    "position": pos,
                    "fragment_nos": [frag_no for frag_no, _ in entries],
                    "values": [value for _, value in entries],
                }
            )
    return {
        "complete": coverage == SAMPLES_PER_MINUTE and not conflicts,
        "coverage": coverage,
        "gaps": gaps,
        "conflicts": conflicts,
    }


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #
def upload_fragment(payload: dict[str, Any]) -> dict[str, Any]:
    station = payload["station"]
    minute = minute_key(payload["minute_utc"])
    fragment_no = payload["fragment_no"]
    revision = payload["revision"]
    start_sample = payload["start_sample"]
    samples = payload["samples"]

    # A fragment must stay inside the 60 one-hertz sample slots.
    if start_sample + len(samples) > SAMPLES_PER_MINUTE:
        raise BoundaryError(
            f"fragment exceeds minute boundary: start_sample={start_sample} "
            f"length={len(samples)} would reach slot {start_sample + len(samples)} "
            f"(limit {SAMPLES_PER_MINUTE})"
        )

    new_hash = content_hash(start_sample, samples)
    samples_json = dump_samples(samples)

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM fragments WHERE station = ? AND minute_utc = ? "
                "AND fragment_no = ?",
                (station, minute, fragment_no),
            ).fetchone()

            changed = False
            if existing is not None:
                if revision < existing["revision"]:
                    raise StaleRevisionError(
                        f"fragment_no={fragment_no} already at revision "
                        f"{existing['revision']}; incoming revision {revision} "
                        "is not higher"
                    )
                if revision == existing["revision"]:
                    # Same version: identical content is an idempotent retry and
                    # must return the original result; different content is refused.
                    if existing["content_hash"] == new_hash:
                        result = _row_to_fragment(existing)
                        result["status"] = "unchanged"
                        conn.commit()
                        return result
                    raise ContentConflictError(
                        f"fragment_no={fragment_no} revision={revision} already "
                        "exists with different content"
                    )
                # Higher revision accepted.
                conn.execute(
                    "UPDATE fragments SET revision = ?, start_sample = ?, "
                    "samples = ?, content_hash = ? WHERE id = ?",
                    (revision, start_sample, samples_json, new_hash, existing["id"]),
                )
                changed = True
            else:
                conn.execute(
                    "INSERT INTO fragments (station, minute_utc, fragment_no, "
                    "revision, start_sample, samples, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        station,
                        minute,
                        fragment_no,
                        revision,
                        start_sample,
                        samples_json,
                        new_hash,
                    ),
                )
                changed = True

            # The effective fragment set changed: every earlier publication of
            # this minute is now superseded and marked stale in the same txn.
            if changed:
                conn.execute(
                    "UPDATE publications SET stale = 1 WHERE station = ? "
                    "AND minute_utc = ? AND stale = 0",
                    (station, minute),
                )

            row = conn.execute(
                "SELECT * FROM fragments WHERE station = ? AND minute_utc = ? "
                "AND fragment_no = ?",
                (station, minute, fragment_no),
            ).fetchone()
            conn.commit()
            result = _row_to_fragment(row)
            result["status"] = "accepted" if existing is None else "superseded"
            return result
        except Exception:
            conn.rollback()
            raise


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get_assembly(station: str, minute_dt: datetime) -> dict[str, Any]:
    minute = minute_key(minute_dt)
    with get_conn() as conn:
        rows = _get_fragments(conn, station, minute)
        fragments = [_row_to_fragment(r) for r in rows]
        assembly = compute_assembly(rows)
    return {
        "station": station,
        "minute_utc": _parse_minute(minute),
        **assembly,
        "fragments": fragments,
    }


def list_fragments(station: str, minute_dt: datetime) -> list[dict[str, Any]]:
    minute = minute_key(minute_dt)
    with get_conn() as conn:
        rows = _get_fragments(conn, station, minute)
        return [_row_to_fragment(r) for r in rows]


def _refresh_staleness(conn: sqlite3.Connection, pub: sqlite3.Row) -> sqlite3.Row:
    """Self-heal the stale flag from the current fragment set on read."""
    rows = _get_fragments(conn, pub["station"], pub["minute_utc"])
    current_hash = _fragment_set_hash(rows)
    if not pub["stale"] and current_hash != pub["fragment_set_hash"]:
        conn.execute("UPDATE publications SET stale = 1 WHERE id = ?", (pub["id"],))
        pub = conn.execute(
            "SELECT * FROM publications WHERE id = ?", (pub["id"],)
        ).fetchone()
    return pub


def _hydrate_publication(conn: sqlite3.Row, pub: sqlite3.Row) -> dict[str, Any]:
    items = conn.execute(
        "SELECT * FROM publication_items WHERE publication_id = ? ORDER BY fragment_no",
        (pub["id"],),
    ).fetchall()
    return {
        "id": pub["id"],
        "station": pub["station"],
        "minute_utc": _parse_minute(pub["minute_utc"]),
        "wave": load_samples(pub["wave"]),
        "stale": bool(pub["stale"]),
        "fragment_set_hash": pub["fragment_set_hash"],
        "created_at": pub["created_at"],
        "items": [
            {
                "fragment_no": it["fragment_no"],
                "revision": it["revision"],
                "start_sample": it["start_sample"],
                "samples": load_samples(it["samples"]),
                "content_hash": it["content_hash"],
            }
            for it in items
        ],
    }


def list_publications(station: str | None, minute_dt: datetime | None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if station is not None:
        clauses.append("station = ?")
        params.append(station)
    if minute_dt is not None:
        clauses.append("minute_utc = ?")
        params.append(minute_key(minute_dt))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        pubs = conn.execute(
            f"SELECT * FROM publications{where} ORDER BY id", params
        ).fetchall()
        out = []
        for pub in pubs:
            pub = _refresh_staleness(conn, pub)
            out.append(_hydrate_publication(conn, pub))
        conn.commit()
        return out


def get_publication(pub_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        pub = conn.execute(
            "SELECT * FROM publications WHERE id = ?", (pub_id,)
        ).fetchone()
        if pub is None:
            return None
        pub = _refresh_staleness(conn, pub)
        result = _hydrate_publication(conn, pub)
        conn.commit()
        return result


def get_latest_publication(station: str, minute_dt: datetime) -> dict[str, Any] | None:
    minute = minute_key(minute_dt)
    with get_conn() as conn:
        pub = conn.execute(
            "SELECT * FROM publications WHERE station = ? AND minute_utc = ? "
            "ORDER BY id DESC LIMIT 1",
            (station, minute),
        ).fetchone()
        if pub is None:
            return None
        pub = _refresh_staleness(conn, pub)
        result = _hydrate_publication(conn, pub)
        conn.commit()
        return result


# --------------------------------------------------------------------------- #
# Publish
# --------------------------------------------------------------------------- #
def publish_minute(station: str, minute_dt: datetime) -> dict[str, Any]:
    minute = minute_key(minute_dt)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = _get_fragments(conn, station, minute)
            assembly = compute_assembly(rows)

            if not rows:
                raise NotPublishableError("no fragments for this station-minute")
            if assembly["gaps"]:
                raise NotPublishableError(
                    f"minute incomplete, gaps at positions: "
                    f"{','.join(map(str, assembly['gaps']))}"
                )
            if assembly["conflicts"]:
                detail = "; ".join(
                    f"position {c['position']} fragments {c['fragment_nos']} "
                    f"values {c['values']}"
                    for c in assembly["conflicts"]
                )
                raise NotPublishableError(f"unresolved conflicts: {detail}")

            set_hash = _fragment_set_hash(rows)

            # A fresh publication for this exact fragment set already exists:
            # return it instead of creating a duplicate.
            existing = conn.execute(
                "SELECT * FROM publications WHERE station = ? AND minute_utc = ? "
                "AND fragment_set_hash = ? AND stale = 0 ORDER BY id DESC LIMIT 1",
                (station, minute, set_hash),
            ).fetchone()
            if existing is not None:
                result = _hydrate_publication(conn, existing)
                conn.commit()
                return result

            # Build the 60-sample wave from the committed fragment set.
            wave = [0] * SAMPLES_PER_MINUTE
            for r in rows:
                start = r["start_sample"]
                for offset, value in enumerate(load_samples(r["samples"])):
                    wave[start + offset] = value

            cur = conn.execute(
                "INSERT INTO publications (station, minute_utc, wave, "
                "fragment_set_hash, stale) VALUES (?, ?, ?, ?, 0)",
                (station, minute, dump_samples(wave), set_hash),
            )
            pub_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO publication_items (publication_id, fragment_no, "
                "revision, start_sample, samples, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        pub_id,
                        r["fragment_no"],
                        r["revision"],
                        r["start_sample"],
                        r["samples"],
                        r["content_hash"],
                    )
                    for r in rows
                ],
            )
            pub = conn.execute(
                "SELECT * FROM publications WHERE id = ?", (pub_id,)
            ).fetchone()
            result = _hydrate_publication(conn, pub)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
