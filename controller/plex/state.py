"""Durable configuration, independent command receipts and default images."""

import hashlib
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from ..database import encode
from .client import Asset, PlexError


def configuration(db, device):
    row = db.execute("SELECT revision,payload FROM plex_settings WHERE device_id=?", (device,)).fetchone()
    return (
        {**json.loads(row["payload"]), "revision": row["revision"]}
        if row
        else {
            "revision": 0,
            "enabled": False,
            "base_url": "",
            "rating_key": "8",
            "machine_id": None,
            "target": None,
            "credential_ref": None,
            "defaults": {},
            "enabled_at": None,
            "one_shot": False,
        }
    )


def runtime(db, device):
    row = db.execute("SELECT payload FROM plex_runtime WHERE device_id=?", (device,)).fetchone()
    return (
        json.loads(row[0])
        if row
        else {
            "generation": 0,
            "desired": None,
            "last_confirmed_at": None,
            "fallback_at": None,
            "applied": {},
            "applied_title": None,
            "pending": False,
            "blocked": None,
            "retry_at": None,
            "failures": 0,
            "error": None,
            "in_flight": None,
            "hold": False,
        }
    )


def save_runtime(db, device, value):
    db.execute(
        "INSERT INTO plex_runtime VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET payload=excluded.payload",
        (device, encode(value)),
    )


def save_configuration(db, device, value):
    db.execute(
        "INSERT INTO plex_settings VALUES (?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
        "revision=excluded.revision,payload=excluded.payload",
        (device, value["revision"], encode({k: v for k, v in value.items() if k != "revision"})),
    )


def save_asset(db, asset):
    db.execute(
        "INSERT OR IGNORE INTO plex_assets VALUES (?,?,?,?,?)",
        (asset.digest, asset.data, asset.mime, asset.width, asset.height),
    )
    return asset.digest


def asset_by_id(db, digest):
    row = db.execute("SELECT * FROM plex_assets WHERE digest=?", (digest,)).fetchone()
    if not row:
        raise PlexError("A saved default image is missing", permanent=True)
    return Asset(row["data"], row["digest"], row["mime"], row["width"], row["height"])


def prune_assets(db):
    keep = set()
    for row in db.execute("SELECT payload FROM plex_settings"):
        keep.update(json.loads(row[0]).get("defaults", {}).values())
    for row in db.execute("SELECT payload FROM plex_runtime"):
        state = json.loads(row[0])
        if state.get("in_flight"):
            keep.add(state["in_flight"].get("digest"))
        keep.update(v.get("digest") for v in state.get("applied", {}).values())
        for source in (state.get("desired") or {}).get("sources", {}).values():
            if source:
                keep.add(source.get("asset"))
    for row in db.execute("SELECT digest FROM plex_assets").fetchall():
        if row[0] not in keep:
            db.execute("DELETE FROM plex_assets WHERE digest=?", (row[0],))


class Credentials:
    def __init__(self, database):
        self.directory = Path(str(database) + ".plex-secrets")
        self.staged = set()
        self.lock = threading.RLock()

    def write(self, token):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        ref = uuid4().hex
        with self.lock:
            self.staged.add(ref)
            fd = os.open(self.directory / ref, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
        return ref

    def release(self, ref):
        with self.lock:
            self.staged.discard(ref)

    def read(self, ref):
        if not ref or len(ref) != 32 or any(c not in "0123456789abcdef" for c in ref):
            raise PlexError("Enter a Plex token in settings", permanent=True)
        try:
            return (self.directory / ref).read_text()
        except OSError:
            raise PlexError("Plex credential is unavailable; enter the token again", permanent=True) from None

    def prune(self, references):
        # Only called by the serial worker after its previous client has closed.
        with self.lock:
            if self.directory.exists():
                for path in self.directory.iterdir():
                    if len(path.name) == 32 and path.name not in references | self.staged:
                        path.unlink(missing_ok=True)


def receipt(db, device, command_id, revision, payload):
    digest = hashlib.sha256(encode(payload).encode()).hexdigest()
    row = db.execute(
        "SELECT * FROM plex_commands WHERE device_id=? AND id=?", (device, command_id)
    ).fetchone()
    if row:
        if row["body_hash"] != digest:
            raise HTTPException(409, "Command ID was already used with a different payload")
        return digest, json.loads(row["result"])
    if configuration(db, device)["revision"] != revision:
        raise HTTPException(409, "Plex settings changed. Reload and review your changes.")
    return digest, None


def record_receipt(db, device, command_id, digest, revision):
    result = {"accepted": True, "revision": revision, "command_id": command_id}
    db.execute("INSERT INTO plex_commands VALUES (?,?,?,?)", (device, command_id, digest, encode(result)))
    db.execute(
        "DELETE FROM plex_commands WHERE device_id=? AND rowid NOT IN "
        "(SELECT rowid FROM plex_commands WHERE device_id=? ORDER BY rowid DESC LIMIT 1000)",
        (device, device),
    )
    return result
