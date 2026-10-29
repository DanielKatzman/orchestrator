import json
import sqlite3
import time
from contextlib import contextmanager

from .common import now

TERMINAL = {"completed", "failed", "cancelled"}


class Store:
    def __init__(self, data):
        data.mkdir(parents=True, exist_ok=True)
        self.path = data / "state.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL,
                    outcome TEXT, phase TEXT, detail TEXT, created TEXT, updated TEXT,
                    request TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
                    cancelled INTEGER NOT NULL DEFAULT 0, request_key TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    at TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS job_events ON events(job_id,id);
                CREATE TABLE IF NOT EXISTS stage_runs (
                    job_id TEXT, attempt INTEGER, phase TEXT, status TEXT,
                    receipt TEXT NOT NULL, PRIMARY KEY(job_id,attempt,phase));
                CREATE TABLE IF NOT EXISTS system (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=20000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        value = dict(row)
        value["request"] = json.loads(value["request"])
        return value

    def get(self, job_id):
        with self.connect() as db:
            return self.decode(
                db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            )

    def by_key(self, key):
        with self.connect() as db:
            return self.decode(
                db.execute("SELECT * FROM jobs WHERE request_key=?", (key,)).fetchone()
            )

    def list(self):
        with self.connect() as db:
            return [
                self.decode(r)
                for r in db.execute(
                    "SELECT * FROM jobs ORDER BY created DESC LIMIT 200"
                )
            ]

    def create(self, job_id, name, request, key=None):
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,name,state,created,updated,request,request_key) VALUES(?,?,?,?,?,?,?)",
                (job_id, name, "queued", now(), now(), json.dumps(request), key),
            )
            self._event(db, job_id, "state", {"state": "queued"})
        return self.get(job_id)

    def _event(self, db, job_id, kind, data):
        db.execute(
            "INSERT INTO events(job_id,at,kind,data) VALUES(?,?,?,?)",
            (job_id, now(), kind, json.dumps(data, allow_nan=False)),
        )

    def event(self, job_id, kind, data):
        with self.connect() as db:
            self._event(db, job_id, kind, data)

    def events(self, job_id, after=0):
        with self.connect() as db:
            return [
                {**dict(r), "data": json.loads(r["data"])}
                for r in db.execute(
                    "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT 500",
                    (job_id, after),
                )
            ]

    def update(self, job_id, **fields):
        if not fields or set(fields) - {
            "state",
            "outcome",
            "phase",
            "detail",
            "cancelled",
            "attempt",
        }:
            raise ValueError("Invalid job update")
        fields["updated"] = now()
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                (*fields.values(), job_id),
            )
            self._event(db, job_id, "state", fields)

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET state='running',updated=? WHERE id=?",
                (now(), row["id"]),
            )
            self._event(db, row["id"], "state", {"state": "running"})
            value = self.decode(row)
            value["state"] = "running"
            return value

    def finish(self, job_id, state, *, outcome=None, phase=None, detail=None):
        """Serialize finalization with cancellation so an accepted cancel wins."""
        if state not in {
            "completed",
            "review_required",
            "failed",
            "interrupted",
            "cancelled",
        }:
            raise ValueError("Invalid worker outcome")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,cancelled,phase FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["state"] not in {"running", "cancelling"}:
                raise ValueError("Worker no longer owns an active job")
            if row["cancelled"]:
                state, outcome, phase = "cancelled", None, row["phase"]
                detail = "Cancelled by user; committed artifacts were retained"
            fields = {
                "state": state,
                "outcome": outcome,
                "phase": phase or row["phase"],
                "detail": detail,
                "updated": now(),
            }
            db.execute(
                "UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                (*fields.values(), job_id),
            )
            self._event(db, job_id, "state", fields)

    def transition(self, job_id, expected, state, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row[0] not in expected:
                raise ValueError("Job changed state; refresh before retrying")
            assignments = {"state": state, "updated": now(), **fields}
            if set(assignments) - {
                "state",
                "updated",
                "attempt",
                "cancelled",
                "outcome",
                "detail",
                "phase",
            }:
                raise ValueError("Invalid transition fields")
            db.execute(
                "UPDATE jobs SET "
                + ",".join(k + "=?" for k in assignments)
                + " WHERE id=?",
                (*assignments.values(), job_id),
            )
            self._event(db, job_id, "state", assignments)

    def receipt(self, job, phase, status, receipt):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO stage_runs VALUES(?,?,?,?,?)",
                (job["id"], job["attempt"], phase, status, json.dumps(receipt)),
            )

    def stages(self, job):
        with self.connect() as db:
            return [
                {**dict(r), "receipt": json.loads(r["receipt"])}
                for r in db.execute(
                    "SELECT * FROM stage_runs WHERE job_id=? AND attempt=?",
                    (job["id"], job["attempt"]),
                )
            ]

    def heartbeat(self):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO system VALUES('heartbeat',?)",
                (str(time.time()),),
            )

    def ready(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM system WHERE key='heartbeat'"
            ).fetchone()
            return bool(row and time.time() - float(row[0]) < 15)
