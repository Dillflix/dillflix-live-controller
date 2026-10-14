"""Internal service ownership fence. Receipts are scoped to one service lifetime."""

from contextlib import contextmanager
import threading
import uuid
from .errors import BusyError, CancelledError, StaleResultError, OutcomeUnknownError


class Lease:
    def __init__(self, epoch):
        self.epoch = epoch
        self.cancelled = threading.Event()

    def check(self):
        if self.cancelled.is_set():
            raise CancelledError("operation revoked by ownership transition")


class Ownership:
    def __init__(self, controller):
        self.controller = controller
        self.backend = controller.backend
        self.lock = threading.RLock()
        self.transitions = threading.Lock()
        self.input_lock = threading.Lock()
        self.epoch = 0
        self.mode = "automatic"
        self.owner_id = None
        self.handoff_id = None
        self.lease = None
        self.last_seq = 0
        self.input_uncertain = False

    def receipt(self):
        with self.lock:
            return dict(
                session_id=self.backend.session_id,
                epoch=self.epoch,
                mode=self.mode,
                owner_id=self.owner_id,
                handoff_id=self.handoff_id,
                acknowledged=self.mode in ("automatic", "manual"),
                last_seq=self.last_seq,
            )

    def validate(self, receipt):
        if not isinstance(receipt, dict) or any(
            receipt.get(k) != self.receipt()[k]
            for k in ("session_id", "epoch", "handoff_id")
        ):
            raise StaleResultError(
                "ownership receipt belongs to an older session or handoff"
            )

    @contextmanager
    def operation(self, expected=None, readonly=False):
        with self.lock:
            if expected is not None:
                self.validate(expected)
            if self.mode not in (
                ("automatic", "manual", "blocked") if readonly else ("automatic",)
            ):
                raise BusyError("ownership is " + self.mode)
            if not self.controller.lock.acquire(blocking=False):
                raise BusyError("another device operation is in progress")
            lease = self.lease = Lease(self.epoch)
        scope = getattr(self.backend, "operation_scope", None)
        try:
            if scope:
                with scope(lease):
                    yield lease
                    lease.check()
            else:
                yield lease
                lease.check()
        except OutcomeUnknownError:
            if not readonly:
                with self.lock:
                    if not lease.cancelled.is_set():
                        self.mode = "blocked"
            raise
        finally:
            with self.lock:
                if self.lease is lease:
                    self.lease = None
                self.controller.lock.release()

    def reconcile_unknown(self):
        """Fence an uncertain predecessor before a new automatic mutation."""
        with self.lock:
            lease = self.lease
            if lease is None:
                raise BusyError("no automatic operation owns reconciliation")
            lease.check()
            self.epoch += 1
            lease.epoch = epoch = self.epoch
        try:
            result = self.backend.fence(epoch)
        except Exception as exc:
            lease.check()
            raise OutcomeUnknownError("predecessor cancellation unconfirmed") from exc
        with self.lock:
            lease.check()
            if (
                self.epoch != epoch
                or result.get("epoch") != epoch
                or result.get("acknowledged") is not True
            ):
                self.mode = "blocked"
                raise OutcomeUnknownError("predecessor cancellation unconfirmed")
            for a in self.controller.attempts.values():
                if a["state"] == "unknown":
                    a.update(
                        state="cancelled",
                        reason="uncertain predecessor fenced before new operation",
                    )

    def transition(
        self,
        mode,
        *,
        owner_id=None,
        previous=None,
        session_id=None,
        attempt_id=None,
        cancel_only=False
    ):
        if session_id is not None and session_id != self.backend.session_id:
            raise StaleResultError("service session changed")
        with self.transitions, self.input_lock:
            with self.lock:
                if self.input_uncertain:
                    raise OutcomeUnknownError(
                        "manual input delivery remains uncertain; handoff not acknowledged"
                    )
                if previous is not None:
                    self.validate(previous)
                if cancel_only and self.mode == "manual":
                    return dict(
                        self.receipt(), cancelled=False, reason="manual_owner_active"
                    )
                if mode == "automatic" and self.mode == "manual" and previous is None:
                    raise StaleResultError(
                        "resuming manual control requires its handoff receipt"
                    )
                if mode == "manual" and self.mode == "manual":
                    if owner_id == self.owner_id:
                        return self.receipt()
                    if previous is None:
                        raise BusyError("another manual owner holds this device")
                if (
                    attempt_id is not None
                    and self.controller.current_attempt_id != attempt_id
                    and self.controller.attempts.get(attempt_id, {}).get("state") != "resolving"
                ):
                    return dict(
                        self.receipt(), cancelled=False, reason="attempt_not_current"
                    )
                self.mode = "transitioning"
                self.epoch += 1
                epoch = self.epoch
                if self.lease:
                    self.lease.cancelled.set()
                self.controller.suspended = True
            notify = getattr(self.backend, "interrupt_waits", None)
            if notify:
                notify()
            try:
                fence = getattr(self.backend, "fence", None)
                if fence is None:
                    raise OutcomeUnknownError("backend cannot acknowledge cancellation")
                result = fence(epoch)
                if (
                    result.get("epoch") != epoch
                    or result.get("acknowledged") is not True
                ):
                    raise OutcomeUnknownError(
                        "runtime cancellation barrier unconfirmed"
                    )
                if not self.controller.lock.acquire(timeout=10):
                    raise OutcomeUnknownError(
                        "runtime fenced, but old service operation has not drained"
                    )
                try:
                    with self.lock:
                        for a in self.controller.attempts.values():
                            if a["state"] in (
                                "resolving",
                                "launch_requested",
                                "observing",
                                "unknown",
                            ):
                                a.update(
                                    state="cancelled",
                                    reason="pending work cancelled; existing playback not stopped",
                                )
                        self.controller.handles.clear()
                        if mode == "manual":
                            self.controller.current_attempt_id = None
                        self.mode = mode
                        self.owner_id = owner_id if mode == "manual" else None
                        self.handoff_id = uuid.uuid4().hex if mode == "manual" else None
                        self.last_seq = 0
                        self.controller.suspended = mode == "manual"
                        return dict(
                            self.receipt(), cancelled=True, playback_stopped=False
                        )
                finally:
                    self.controller.lock.release()
            except BaseException:
                with self.lock:
                    self.mode = "blocked"
                raise

    def manual_input(self, receipt, seq, *, key=None, text=None):
        # Transition acknowledgement waits for this entire physical write to finish.
        with self.input_lock:
            with self.lock:
                self.validate(receipt)
                if self.mode != "manual":
                    raise BusyError("manual ownership is not acknowledged")
                if type(seq) is not int or seq <= self.last_seq:
                    raise StaleResultError(
                        "input sequence already consumed; never replay uncertain input"
                    )
                self.last_seq = seq
            try:
                self.backend.manual_input(key=key, text=text)
            except ValueError:
                raise
            except BaseException:
                with self.lock:
                    self.mode = "blocked"
                    self.input_uncertain = True
                raise OutcomeUnknownError(
                    "manual write outcome uncertain; input and ownership release blocked"
                )
            return dict(
                delivered=True,
                seq=seq,
                session_id=self.backend.session_id,
                note="transport delivery, not proof the application handled the input",
            )
