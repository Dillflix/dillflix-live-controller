"""Controller adapters project the same durable API records without a loopback HTTP hop."""

import json
from datetime import UTC, datetime

from ..content_status import source_observation
from .models import CancelRequest


class IntegratedPlaybackAdapter:
    def __init__(self, executor):
        self.executor = executor

    @staticmethod
    def project(report):
        return {
            "request_id": report["request_id"],
            "executor_job_id": report["token"],
            "device_id": report["device_id"],
            "intent_version": report["intent_version"],
            "content_id": report["content_id"],
            "state": report["operation"]["state"],
            "observation": report["observation"],
            "finished_at": report["operation"].get("finished_at"),
            "reason": (report["operation"]["error"] or {}).get("message"),
            "device_recovery": bool((report.get("prime_player") or {}).get("device_recovery")),
        }

    def submit(self, request):
        body = dict(request)
        if "deadline_at" not in body:
            with self.executor.db.transaction() as db:
                row = db.execute(
                    "SELECT deadline_at FROM jobs WHERE id=?", (request["request_id"],)
                ).fetchone()
            if not row or row[0] is None:
                raise ValueError("A durable controller navigation deadline is required")
            body["deadline_at"] = datetime.fromtimestamp(row[0], UTC).isoformat()
        report, _ = self.executor.submit(body)
        # The independent executor row persists the token/request association even
        # if controller result acceptance is interrupted or the intent has changed.
        return self.project(report)

    def inspect(self, request_id):
        report = self.executor.store.by_request(request_id)
        return self.project(report) if report else None

    def observe(self, device_id):
        report = self.observe_report(device_id)
        return report["observation"] if report else None

    def observe_report(self, device_id):
        """Read observation and monitoring outcome from the same durable snapshot."""
        if self.executor.stopping or not self.executor.loop:
            raise ConnectionError("Playback executor is not running")
        with self.executor.db.transaction() as db:
            owner = db.execute(
                "SELECT current_token FROM executor_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if not owner or not owner[0]:
                return None
            report = self.executor.store.project(self.executor.store.get_row(db, owner[0]), db)
            error = report["observation_status"].get("error") or {}
            status = (report.get("prime_player") or {}).get("playback_status") or {}
            return {
                "observation": report["observation"],
                "reason": error.get("message"),
                "player_state": status.get("state", "unknown")
                if error.get("code") == "prime_playback_unverified"
                else "unknown",
            }

    def prepare_recovery(self, db, request_id, after):
        return self.executor.prepare_recovery(db, request_id, after)

    def cancel(self, request_id):
        report = self.executor.store.by_request(request_id)
        if report:
            command = CancelRequest(device_id=report["device_id"], token=report["token"])
        else:
            with self.executor.db.transaction() as db:
                row = db.execute("SELECT device_id,intent FROM jobs WHERE id=?", (request_id,)).fetchone()
            if not row:
                return
            command = CancelRequest(device_id=row[0], through_intent_version=row[1])
        self.executor.cancel_sync(command)

    def cancel_device(self, device_id, through_intent_version):
        self.executor.cancel_sync(
            CancelRequest(device_id=device_id, through_intent_version=through_intent_version)
        )


class ExecutorContentStatusAdapter:
    def __init__(self, executor):
        self.executor = executor

    async def lookup(self, request):
        observation = None
        with self.executor.db.transaction() as db:
            # Terminal facts persist after route switches/cancel. Prefer the newest
            # acquired explicit completion. A cached live feed is weaker evidence.
            rows = db.execute(
                "SELECT report FROM executor_jobs WHERE content_id=? AND request IS NOT NULL ORDER BY intent DESC",
                (request["content_id"],),
            ).fetchall()
            for row in rows:
                candidate = json.loads(row[0])["content_status"]["observation"]
                if candidate and candidate["state"] in {"ended", "cancelled"}:
                    observation = candidate
                    break
        feed = source_observation(
            request["content_snapshot"],
            request["catalog_seen_at"],
            datetime.now(UTC),
            datetime.now(UTC),
            "teamarr",
            self.executor.settings.status_ttl,
        )
        if feed:
            feed.update(source="teamarr_feed", simulated=False)
        if observation is None:
            observation = feed
        # Do not replace confirmed completion with a cached feed's live/unknown
        # state just because a periodic catalog fetch refreshed its receipt time.
        return (
            {
                "schema_version": 1,
                "request_id": request["request_id"],
                "content_id": request["content_id"],
                "observation": observation,
            }
            if observation
            else None
        )
