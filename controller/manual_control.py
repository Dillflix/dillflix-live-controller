"""Durable manual ownership. Input transport lives in device_input, not the planner."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from .planner import parse_time


class ManualControl:
    @staticmethod
    def owner_key(device_id):
        return f"manual-owner:{device_id}"

    def check_manual_owner(self, db, device, session_id, owner_token):
        session = device.get("manual_control")
        expected = self.db.meta(db, self.owner_key(device["id"]), "") or ""
        if (
            not session
            or session["session_id"] != session_id
            or parse_time(session["expires_at"]) <= datetime.now(UTC)
            or not hmac.compare_digest(expected, hashlib.sha256(owner_token.encode()).hexdigest())
        ):
            raise HTTPException(
                409, "Manual control ended or belongs to another browser. Refresh the screen."
            )
        return session

    def manual_authorized(self, device_id, session_id, owner_token):
        with self.db.transaction() as db:
            device = self.db.device(db, device_id)
            session = self.check_manual_owner(db, device, session_id, owner_token)
            if device.get("input_handoff") or not session.get("input_ready", False):
                raise HTTPException(409, "Waiting for playback cancellation. Reconnect the remote to retry.")
            return session

    def manual_expired(self):
        with self.db.transaction() as db:
            session = self.db.device(db).get("manual_control")
            return bool(session and parse_time(session["expires_at"]) <= datetime.now(UTC))

    def prepare_manual_input(self, device_id, session_id, owner_token):
        with self.playback_lock:
            with self.db.transaction() as db:
                self.check_manual_owner(db, self.db.device(db, device_id), session_id, owner_token)
            self.reconcile_input_handoff(device_id)

    def reconcile_input_handoff(self, device_id="living-room"):
        """Caller holds playback_lock. No DB transaction spans executor I/O.

        The fence survives session expiry/release and blocks new playback until
        acknowledged. A failed cancellation never authorizes manual input.
        """
        with self.db.transaction() as db:
            device = self.db.device(db, device_id)
            handoff = device.get("input_handoff")
            session = device.get("manual_control")
            if not handoff and session and not session.get("input_ready", False):
                # Upgrade a retained session from before cancellation barriers.
                handoff = {"through_intent_version": device["intent_version"]}
                device["input_handoff"] = handoff
                self.db.save_device(db, device)
        if not handoff:
            return
        try:
            self.playback.cancel_device(device_id, handoff["through_intent_version"])
        except Exception as exc:
            detail = str(exc)[:500] or type(exc).__name__
            with self.db.transaction() as db:
                device = self.db.device(db, device_id)
                current = device.get("input_handoff")
                if current and current["through_intent_version"] == handoff["through_intent_version"]:
                    if current.get("error") != detail:
                        self.db.log(
                            db,
                            datetime.now(UTC).isoformat(),
                            "Manual handoff blocked",
                            detail,
                            "manual_control",
                            device_id,
                        )
                    current.update(error=detail, last_attempt_at=datetime.now(UTC).isoformat())
                    self.db.save_device(db, device)
            raise
        with self.db.transaction() as db:
            device = self.db.device(db, device_id)
            if (
                not device.get("input_handoff")
                or device["input_handoff"]["through_intent_version"] != handoff["through_intent_version"]
            ):
                return  # Another takeover established a newer barrier during I/O.
            device["input_handoff"] = None
            if device.get("manual_control"):
                device["manual_control"]["input_ready"] = True
            self.db.save_device(db, device)

    def clear_playback_for_manual(self, db, device):
        device["intent_version"] += 1
        db.execute("UPDATE jobs SET state='cancelled' WHERE device_id=? AND state='pending'", (device["id"],))
        device.update(
            desired=None,
            observed=None,
            playback_state="waiting",
            started_at=None,
            last_switch_at=None,
            next_candidate=None,
            recovery=None,
            retry_playback=None,
            force_switch=True,
        )

    def finish_manual(self, db, device, mode, reason):
        self.clear_playback_for_manual(db, device)
        device.update(manual_control=None, automation=mode, reason=reason)
        self.db.set_meta(db, self.owner_key(device["id"]), None)
        self.db.log(
            db, datetime.now(UTC).isoformat(), reason, "Watch plan retained", "manual_control", device["id"]
        )

    def expire_manual(self):
        with self.db.transaction() as db:
            device = self.db.device(db)
            session = device.get("manual_control")
            if session and parse_time(session["expires_at"]) <= datetime.now(UTC):
                self.finish_manual(db, device, session["return_mode"], "Manual control expired")
                device["revision"] += 1
                self.db.save_device(db, device)
                return True
        return False

    def manual_command(self, device_id, command):
        if not self.settings.screen_adb_serial and command.action == "take":
            raise HTTPException(409, "Configure SCREEN_ADB_SERIAL before taking control.")

        def apply(db, device):
            current = device.get("manual_control")
            if command.action == "take":
                if current and not command.takeover:
                    raise HTTPException(
                        409, "Another browser has manual control. Choose Take over explicitly."
                    )
                if current and current["session_id"] == command.session_id:
                    raise HTTPException(409, "A takeover requires a new session ID.")
                return_mode = current["return_mode"] if current else device["automation"]
                self.clear_playback_for_manual(db, device)
                device.update(
                    automation="paused",
                    reason="Manual device control; watch plan retained.",
                    input_handoff={"through_intent_version": device["intent_version"]},
                    manual_control={
                        "session_id": command.session_id,
                        "started_at": datetime.now(UTC).isoformat(),
                        "expires_at": (datetime.now(UTC) + timedelta(minutes=command.minutes)).isoformat(),
                        "return_mode": return_mode,
                        "input_ready": False,
                    },
                )
                self.db.set_meta(
                    db, self.owner_key(device_id), hashlib.sha256(command.owner_token.encode()).hexdigest()
                )
                message = "Manual control taken over" if current else "Manual control started"
            else:
                session = self.check_manual_owner(db, device, command.session_id, command.owner_token)
                if command.action == "release":
                    self.finish_manual(db, device, command.release_mode, "Manual control ended")
                    return
                # Extend from now, bounded to 24 hours; repeated receipts never extend twice.
                session["expires_at"] = (datetime.now(UTC) + timedelta(minutes=command.minutes)).isoformat()
                message = "Manual control extended"
            self.db.log(
                db, datetime.now(UTC).isoformat(), message, "Watch plan retained", "manual_control", device_id
            )

        return self.mutate(
            device_id, command.command_id, command.expected_revision, command.model_dump(), apply
        )
