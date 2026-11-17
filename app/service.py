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


class NoCurrentPublicationError(ServiceError):
    """No non-stale publication exists for the requested station-minute."""

    status_code = 409


class MissingCalibrationError(ServiceError):
    """At least one sample slot has no calibration effective at its time."""

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


def second_key(dt: datetime) -> str:
    """Canonical UTC second string, e.g. ``2026-11-17T05:06:30Z``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_second(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def calibration_hash(zero_count: int, gain: int) -> str:
    payload = json.dumps(
        {"zero_count": zero_count, "gain": gain}, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _wave_hash(wave: list[int]) -> str:
    return hashlib.sha256(
        json.dumps(wave, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _calibration_set_hash(rows: list[sqlite3.Row]) -> str:
    """Stable hash of every calibration of a station, ordered by time."""
    canonical = [
        {
            "effective_at": r["effective_at"],
            "revision": r["revision"],
            "zero_count": r["zero_count"],
            "gain": r["gain"],
            "content_hash": r["content_hash"],
        }
        for r in rows
    ]
    canonical.sort(key=lambda c: c["effective_at"])
    blob = json.dumps(canonical, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _row_to_calibration(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "station": r["station"],
        "effective_at": _parse_second(r["effective_at"]),
        "revision": r["revision"],
        "zero_count": r["zero_count"],
        "gain": r["gain"],
        "content_hash": r["content_hash"],
        "created_at": r["created_at"],
    }


def _station_calibrations(
    conn: sqlite3.Connection, station: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM calibrations WHERE station = ? ORDER BY effective_at",
        (station,),
    ).fetchall()


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
            # Conversions built on those publications are likewise superseded.
            if changed:
                conn.execute(
                    "UPDATE publications SET stale = 1 WHERE station = ? "
                    "AND minute_utc = ? AND stale = 0",
                    (station, minute),
                )
                conn.execute(
                    "UPDATE conversions SET stale = 1 WHERE station = ? "
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


# --------------------------------------------------------------------------- #
# Calibrations
# --------------------------------------------------------------------------- #
def upload_calibration(payload: dict[str, Any]) -> dict[str, Any]:
    station = payload["station"]
    effective = second_key(payload["effective_at"])
    revision = payload["revision"]
    zero_count = payload["zero_count"]
    gain = payload["gain"]
    new_hash = calibration_hash(zero_count, gain)

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM calibrations WHERE station = ? AND effective_at = ?",
                (station, effective),
            ).fetchone()

            changed = False
            if existing is not None:
                if revision < existing["revision"]:
                    raise StaleRevisionError(
                        f"calibration effective_at={effective} already at "
                        f"revision {existing['revision']}; incoming revision "
                        f"{revision} is not higher"
                    )
                if revision == existing["revision"]:
                    # Same version: identical content is an idempotent retry and
                    # returns the original record; different content is refused.
                    if existing["content_hash"] == new_hash:
                        result = _row_to_calibration(existing)
                        result["status"] = "unchanged"
                        conn.commit()
                        return result
                    raise ContentConflictError(
                        f"calibration effective_at={effective} revision="
                        f"{revision} already exists with different content"
                    )
                conn.execute(
                    "UPDATE calibrations SET revision = ?, zero_count = ?, "
                    "gain = ?, content_hash = ? WHERE id = ?",
                    (revision, zero_count, gain, new_hash, existing["id"]),
                )
                changed = True
            else:
                conn.execute(
                    "INSERT INTO calibrations (station, effective_at, revision, "
                    "zero_count, gain, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
                    (station, effective, revision, zero_count, gain, new_hash),
                )
                changed = True

            # A new or revised calibration may change the effective calibration
            # set of earlier conversions: re-evaluate them in the same snapshot
            # and mark exactly the affected ones stale.
            if changed:
                _mark_affected_conversions(conn, station)

            row = conn.execute(
                "SELECT * FROM calibrations WHERE station = ? AND effective_at = ?",
                (station, effective),
            ).fetchone()
            conn.commit()
            result = _row_to_calibration(row)
            result["status"] = "accepted" if existing is None else "superseded"
            return result
        except Exception:
            conn.rollback()
            raise


def list_calibrations(station: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = _station_calibrations(conn, station)
        return [_row_to_calibration(r) for r in rows]


# --------------------------------------------------------------------------- #
# Calibration lookup and conversion math
# --------------------------------------------------------------------------- #
def _effective_calibrations_for_minute(
    cal_rows: list[sqlite3.Row], minute: str
) -> dict[int, sqlite3.Row]:
    """Map every one of the 60 sample slots to its nearest-prior calibration."""
    minute_dt = _parse_minute(minute)
    sample_times = [
        minute_dt.replace(second=pos) for pos in range(SAMPLES_PER_MINUTE)
    ]
    chosen: dict[int, sqlite3.Row] = {}
    for pos, sample_time in enumerate(sample_times):
        sample_key = second_key(sample_time)
        best: sqlite3.Row | None = None
        for row in cal_rows:  # rows are ordered by effective_at
            if row["effective_at"] <= sample_key:
                best = row
            else:
                break
        if best is None:
            raise MissingCalibrationError(
                f"no calibration effective on or before sample time {sample_key}"
            )
        chosen[pos] = best
    return chosen


def _compute_conversion(
    wave: list[int], cal_rows: list[sqlite3.Row], minute: str
) -> list[dict[str, Any]]:
    """Apply (count - zero_count) * gain with each slot's calibration."""
    chosen = _effective_calibrations_for_minute(cal_rows, minute)
    items: list[dict[str, Any]] = []
    for pos, count in enumerate(wave):
        cal = chosen[pos]
        value = (count - cal["zero_count"]) * cal["gain"]
        items.append(
            {
                "sample": pos,
                "effective_at": cal["effective_at"],
                "revision": cal["revision"],
                "zero_count": cal["zero_count"],
                "gain": cal["gain"],
                "value": value,
            }
        )
    return items


# --------------------------------------------------------------------------- #
# Conversion staleness
# --------------------------------------------------------------------------- #
def _conversion_current(
    conn: sqlite3.Connection,
    conv: sqlite3.Row,
    cal_rows: list[sqlite3.Row],
) -> bool:
    """Recompute whether a conversion still matches its committed sources.

    A conversion stays current only while (a) its source publication is the
    latest one and still non-stale, and (b) recomputing from the current
    calibrations yields the same 60 values through the same calibration
    revisions.
    """
    latest_pub = conn.execute(
        "SELECT * FROM publications WHERE station = ? AND minute_utc = ? "
        "ORDER BY id DESC LIMIT 1",
        (conv["station"], conv["minute_utc"]),
    ).fetchone()
    if latest_pub is None or latest_pub["id"] != conv["publication_id"]:
        return False
    if latest_pub["stale"]:
        return False

    pub = conn.execute(
        "SELECT * FROM publications WHERE id = ?", (conv["publication_id"],)
    ).fetchone()
    try:
        items = _compute_conversion(
            load_samples(pub["wave"]), cal_rows, conv["minute_utc"]
        )
    except MissingCalibrationError:
        return False
    stored_values = load_samples(conv["values_json"])
    if [it["value"] for it in items] != stored_values:
        return False
    stored_items = conn.execute(
        "SELECT sample, effective_at, revision FROM conversion_items "
        "WHERE conversion_id = ?",
        (conv["id"],),
    ).fetchall()
    return (
        sorted(
            (r["sample"], r["effective_at"], r["revision"]) for r in stored_items
        )
        == sorted(
            (it["sample"], it["effective_at"], it["revision"]) for it in items
        )
    )


def _mark_affected_conversions(
    conn: sqlite3.Connection, station: str
) -> None:
    """Mark stale exactly the conversions no longer matching current sources."""
    cal_rows = _station_calibrations(conn, station)
    convs = conn.execute(
        "SELECT * FROM conversions WHERE station = ? AND stale = 0", (station,)
    ).fetchall()
    for conv in convs:
        if not _conversion_current(conn, conv, cal_rows):
            conn.execute(
                "UPDATE conversions SET stale = 1 WHERE id = ?", (conv["id"],)
            )


def _refresh_conversion_staleness(
    conn: sqlite3.Connection, conv: sqlite3.Row
) -> sqlite3.Row:
    """Self-heal the stale flag from current sources on read."""
    cal_rows = _station_calibrations(conn, conv["station"])
    if not conv["stale"] and not _conversion_current(conn, conv, cal_rows):
        conn.execute("UPDATE conversions SET stale = 1 WHERE id = ?", (conv["id"],))
        conv = conn.execute(
            "SELECT * FROM conversions WHERE id = ?", (conv["id"],)
        ).fetchone()
    return conv


def _hydrate_conversion(
    conn: sqlite3.Connection, conv: sqlite3.Row
) -> dict[str, Any]:
    items = conn.execute(
        "SELECT * FROM conversion_items WHERE conversion_id = ? ORDER BY sample",
        (conv["id"],),
    ).fetchall()
    return {
        "id": conv["id"],
        "station": conv["station"],
        "minute_utc": _parse_minute(conv["minute_utc"]),
        "publication_id": conv["publication_id"],
        "values": load_samples(conv["values_json"]),
        "stale": bool(conv["stale"]),
        "calibration_set_hash": conv["calibration_set_hash"],
        "publication_wave_hash": conv["publication_wave_hash"],
        "created_at": conv["created_at"],
        "items": [
            {
                "sample": it["sample"],
                "effective_at": _parse_second(it["effective_at"]),
                "revision": it["revision"],
                "zero_count": it["zero_count"],
                "gain": it["gain"],
                "value": it["value"],
            }
            for it in items
        ],
    }


# --------------------------------------------------------------------------- #
# Conversion reads
# --------------------------------------------------------------------------- #
def list_conversions(
    station: str | None, minute_dt: datetime | None
) -> list[dict[str, Any]]:
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
        convs = conn.execute(
            f"SELECT * FROM conversions{where} ORDER BY id", params
        ).fetchall()
        out = []
        for conv in convs:
            conv = _refresh_conversion_staleness(conn, conv)
            out.append(_hydrate_conversion(conn, conv))
        conn.commit()
        return out


def get_conversion(conv_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        conv = conn.execute(
            "SELECT * FROM conversions WHERE id = ?", (conv_id,)
        ).fetchone()
        if conv is None:
            return None
        conv = _refresh_conversion_staleness(conn, conv)
        result = _hydrate_conversion(conn, conv)
        conn.commit()
        return result


def get_latest_conversion(station: str, minute_dt: datetime) -> dict[str, Any] | None:
    minute = minute_key(minute_dt)
    with get_conn() as conn:
        conv = conn.execute(
            "SELECT * FROM conversions WHERE station = ? AND minute_utc = ? "
            "ORDER BY id DESC LIMIT 1",
            (station, minute),
        ).fetchone()
        if conv is None:
            return None
        conv = _refresh_conversion_staleness(conn, conv)
        result = _hydrate_conversion(conn, conv)
        conn.commit()
        return result


# --------------------------------------------------------------------------- #
# Conversion publish
# --------------------------------------------------------------------------- #
def convert_minute(station: str, minute_dt: datetime) -> dict[str, Any]:
    minute = minute_key(minute_dt)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Only the current, non-stale publication of this minute may be
            # converted: a stale source wave would freeze an invalid result.
            pub = conn.execute(
                "SELECT * FROM publications WHERE station = ? AND minute_utc = ? "
                "ORDER BY id DESC LIMIT 1",
                (station, minute),
            ).fetchone()
            if pub is None:
                raise NoCurrentPublicationError(
                    "no publication exists for this station-minute"
                )
            if pub["stale"]:
                raise NoCurrentPublicationError(
                    "the latest publication of this minute is stale; "
                    "re-publish the corrected minute before converting"
                )

            cal_rows = _station_calibrations(conn, station)
            # Reject the whole minute if any sample slot lacks a calibration.
            items = _compute_conversion(
                load_samples(pub["wave"]), cal_rows, minute
            )
            values = [it["value"] for it in items]
            wave_hash = _wave_hash(load_samples(pub["wave"]))
            cal_hash = _calibration_set_hash(cal_rows)

            # Idempotent: an existing fresh conversion of the same publication
            # with the same calibration set is returned instead of duplicated.
            existing = conn.execute(
                "SELECT * FROM conversions WHERE publication_id = ? AND "
                "calibration_set_hash = ? AND stale = 0 ORDER BY id DESC LIMIT 1",
                (pub["id"], cal_hash),
            ).fetchone()
            if existing is not None:
                result = _hydrate_conversion(conn, existing)
                conn.commit()
                return result

            cur = conn.execute(
                "INSERT INTO conversions (station, minute_utc, publication_id, "
                "publication_wave_hash, calibration_set_hash, values_json, stale) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (station, minute, pub["id"], wave_hash, cal_hash,
                 dump_samples(values)),
            )
            conv_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO conversion_items (conversion_id, sample, "
                "effective_at, revision, zero_count, gain, value) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        conv_id,
                        it["sample"],
                        it["effective_at"],
                        it["revision"],
                        it["zero_count"],
                        it["gain"],
                        it["value"],
                    )
                    for it in items
                ],
            )
            conv = conn.execute(
                "SELECT * FROM conversions WHERE id = ?", (conv_id,)
            ).fetchone()
            result = _hydrate_conversion(conn, conv)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
