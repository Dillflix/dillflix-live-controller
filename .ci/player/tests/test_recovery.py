import json
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from dillflix_prime_player.controller import Controller
from dillflix_prime_player.errors import BusyError, CancelledError, OutcomeUnknownError, StaleResultError, UnavailableError
from dillflix_prime_player.recovery import RuntimeSupervisor
from test_native_adapter import descriptor


BASE = json.loads((Path(__file__).parents[1] / "src/dillflix_prime_player/_runtime/profile.json").read_text())
CAPS = ("core", "search", "broadcasts", "play", "native_inspection", "playback_status")


class Backend:
    def __init__(self, profile, number=1):
        self.profile = profile
        self.session_id = "session-" + str(number)
        self.engine_unavailable = None
        self.failure = None
        self.closed = False
        self.startup_cancelled = threading.Event()
        self.start = Mock()
        self.interrupt_waits = Mock()
        self.fence = Mock(side_effect=lambda epoch: {"epoch": epoch, "acknowledged": True})
        self.adaptation_manifest = Mock(return_value=None)
        self.caps = {name: {"available": True, "reasons": []} for name in CAPS}
        self.restart = False
        self.generation = 1
        self.roles = []
        self.resources = []
        self.close_errors = []
        self.close_calls = 0
        self.page_type = "RUST_PLAYBACK"
        self.page_id = "playing-title"
        self.rows = []
        self.cv = threading.Condition()

    def health(self):
        return {"session_id": self.session_id, "closed": self.closed, "failure": self.failure,
                "state": {"page": {"pageType": self.page_type, "pageId": self.page_id}},
                "compatibility": {"capabilities": deepcopy(self.caps), "generation": self.generation,
                                  "resources": deepcopy(self.resources),
                                  "adaptation": {"restartRequired": self.restart, "roles": deepcopy(self.roles)}}}

    def close(self):
        self.close_calls += 1
        if self.close_errors:
            raise self.close_errors.pop(0)
        self.closed = True

    def compatibility(self):
        return self.health()["compatibility"]

    def diagnostic_snapshot(self):
        return {"session_id": self.session_id, "runtime_records": deepcopy(self.rows)}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.backends = []
        self.configure_backend = None
        self.now = 1000
        self.store = Mock()
        self.store.load_active.return_value = None
        self.store.load.return_value = None
        self.store.retry_after.return_value = 0
        self.build = Mock(side_effect=lambda base, d: {**base, "testRuntime": {**base["testRuntime"], "compressedSha256": d["metadata"]["dataIntegrity"]["sha256"]}})
        self.supervisor = self.create()

    def create(self, base=None):
        def factory(profile):
            backend = Backend(profile, len(self.backends) + 1)
            self.backends.append(backend)
            if self.configure_backend:
                self.configure_backend(backend)
            return backend
        return RuntimeSupervisor(base or BASE, self.directory.name, factory,
                                 clock=lambda: self.now, store=self.store, build_profile=self.build,
                                 serial="192.0.2.1:5555")

    def start(self):
        self.supervisor.step()
        return self.backends[-1]

    def candidate(self, backend):
        backend.caps["native_inspection"]["available"] = False
        value = {"engineBuildId": BASE["engine"]["buildId"], "descriptor": descriptor(b"new image")}
        backend.adaptation_manifest.return_value = value
        return value

    def test_available_playback_is_retained_without_resource_download(self):
        backend = self.start()
        backend.adaptation_manifest.return_value = {"descriptor": descriptor(b"new advertised only")}
        self.supervisor.step()
        self.assertEqual(self.supervisor.adaptation()["state"], "ready")
        self.assertFalse(backend.closed)
        self.build.assert_not_called()
        backend.adaptation_manifest.assert_not_called()

    def test_new_native_identity_is_cached_and_restarted_once(self):
        backend = self.start()
        manifest = self.candidate(backend)
        self.supervisor.step()
        self.assertTrue(backend.closed)
        self.assertEqual(len(self.backends), 2)
        self.assertEqual(self.supervisor.health()["session_id"], "session-2")
        self.store.activate.assert_not_called()
        self.store.begin_trial.assert_called_once()
        self.backends[-1].adaptation_manifest.return_value = manifest
        self.supervisor.step()
        self.store.activate.assert_called_once()
        self.assertEqual(len(self.backends), 2)
        self.assertEqual(self.supervisor.adaptation()["state"], "ready")

    def test_native_validator_failure_reason_survives_health_and_restart(self):
        from dillflix_prime_player.native_adapter import ContractError
        backend = self.start()
        manifest = self.candidate(backend)
        self.build.side_effect = ContractError("native_decompression_failed")
        self.supervisor.step()
        report = self.supervisor.health()["compatibility"]["adaptation"]
        self.assertEqual(report["state"], "backoff")
        self.assertEqual(report["failure_category"], "packaging")
        self.assertEqual(report["reason"], "native_decompression_failed")
        self.assertEqual(report["last_failure"]["reason"], "native_decompression_failed")
        self.assertEqual(self.store.mark_failure.call_args.args, (BASE, manifest["descriptor"], "native_decompression_failed"))
        self.assertFalse(backend.closed)
        self.store.activate.assert_not_called()
        self.assertEqual(self.create().adaptation()["last_failure"]["reason"], "native_decompression_failed")

    def test_native_acquisition_failure_reports_code_without_private_exception_text(self):
        from dillflix_prime_player.native_adapter import NativeAdapterError
        backend = self.start()
        self.candidate(backend)
        self.build.side_effect = NativeAdapterError("native_resource_fetch_timeout")
        self.supervisor.step()
        self.assertEqual(self.supervisor.adaptation()["reason"], "native_resource_fetch_timeout")
        self.assertEqual(self.supervisor.adaptation()["state"], "backoff")
        self.now = self.supervisor.next_retry + 1
        self.build.side_effect = NativeAdapterError("https://example.invalid/private-token")
        self.supervisor.step()
        report = self.supervisor.adaptation()
        self.assertEqual(report["reason"], "native_acquisition_failed")
        self.assertNotIn("private-token", json.dumps(report))
        self.assertNotIn("private-token", (Path(self.directory.name) / "adaptation/last-failure.json").read_text())

    def test_packaging_retries_same_evidence_and_rotated_url_without_restart(self):
        from dillflix_prime_player.native_adapter import ContractError
        backend = self.start()
        manifest = self.candidate(backend)
        self.build.side_effect = ContractError("native_decompression_failed")
        self.supervisor.step()
        self.supervisor.step()
        self.assertEqual(self.build.call_count, 1)
        self.now = self.supervisor.next_retry + 1
        self.supervisor.step()
        self.assertEqual(self.build.call_count, 2)
        manifest["descriptor"]["url"] += "&rotation=private-new-url"
        self.supervisor.step()  # Same compatibility generation, before deadline.
        self.assertEqual(self.build.call_count, 3)
        self.assertFalse(backend.closed)
        self.assertNotIn("private-new-url", json.dumps(self.supervisor.health()))
        self.assertEqual(self.create().adaptation()["failure_category"], "packaging")

    def test_real_abi_failure_remains_contract_incompatible(self):
        from dillflix_prime_player.native_adapter import ContractError
        backend = self.start()
        self.candidate(backend)
        self.build.side_effect = ContractError("native_elf_abi_unsupported")
        self.supervisor.step()
        self.assertEqual(self.supervisor.adaptation()["failure_category"], "engine_contract")
        self.assertEqual(self.supervisor.state, "contract_incompatible")
        self.supervisor.step()
        self.assertEqual(self.build.call_count, 1)

    def test_outer_recovery_handler_does_not_mislabel_packaging_or_cache_errors(self):
        from dillflix_prime_player.native_adapter import ContractError, NativeAdapterError
        for error, category in ((ContractError("native_metadata_invalid"), "packaging"),
                                (NativeAdapterError("native_cache_candidate_invalid"), "acquisition")):
            supervisor = self.create()
            supervisor.step = Mock(side_effect=error)
            supervisor.wake_event = Mock()
            supervisor.wake_event.wait.side_effect = lambda *args: supervisor.stop_event.set()
            supervisor._run()
            self.assertEqual(supervisor.state, "backoff")
            self.assertEqual(supervisor.failure_category, category)

    def test_failed_candidate_startup_never_becomes_active(self):
        backend = self.start()
        self.candidate(backend)
        self.configure_backend = lambda b: setattr(b.start, "side_effect", RuntimeError("startup failed"))
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            self.supervisor.step()
        self.store.activate.assert_not_called()
        self.store.fail_trial.assert_called_with("native_trial_startup_failed")
        self.assertIsNone(self.supervisor.trial_descriptor)

    def test_unverified_loaded_candidate_is_not_promoted(self):
        backend = self.start()
        manifest = self.candidate(backend)
        self.supervisor.step()
        candidate = self.backends[-1]
        candidate.adaptation_manifest.return_value = manifest
        candidate.caps["native_inspection"].update(available=False, reasons=["binary_fingerprint_mismatch"])
        self.supervisor.step()
        self.store.activate.assert_not_called()
        self.now += 121
        self.supervisor.step()
        self.assertTrue(candidate.closed)
        self.store.fail_trial.assert_called_with("native_trial_image_unverified")
        self.assertEqual(self.supervisor.failure_category, "loaded_image")

    def test_manifest_change_during_download_does_not_restart_or_activate(self):
        backend = self.start()
        original = self.candidate(backend)
        backend.adaptation_manifest.side_effect = [original, {**original, "descriptor": descriptor(b"newer image")}]
        self.supervisor.step()
        self.assertFalse(backend.closed)
        self.store.activate.assert_not_called()

    def test_manual_handoff_during_download_prevents_restart(self):
        backend = self.start()
        self.candidate(backend)
        original = self.build.side_effect
        def build(base, desc):
            self.supervisor.manual_hold = True
            return original(base, desc)
        self.build.side_effect = build
        self.supervisor.step()
        self.assertFalse(backend.closed)
        self.assertEqual(len(self.backends), 1)

    def test_loaded_replacement_cancels_old_lease_and_rotates_session(self):
        backend = self.start()
        backend.caps["core"]["available"] = False
        backend.restart = True
        old = self.supervisor.current
        old.ownership.lease = Mock()
        self.supervisor.step()
        old.ownership.lease.cancelled.set.assert_called_once()
        self.assertTrue(backend.closed)
        self.assertIsNone(self.supervisor.current)
        with self.assertRaises(UnavailableError):
            self.supervisor.play("gti")
        self.assertNotEqual(self.supervisor.health()["session_id"], backend.session_id)

    def test_retry_deadline_and_manual_hold_survive_supervisor_restart(self):
        self.supervisor._backoff("test_failure")
        restarted = self.create()
        self.assertEqual(restarted.next_retry, self.supervisor.next_retry)
        restarted.step()
        self.assertEqual(self.backends, [])
        self.supervisor.suspend(True, owner_id="manual-owner")
        restarted = self.create()
        self.assertTrue(restarted.manual_hold)
        self.now += 100
        restarted.step()
        self.assertEqual(self.backends, [])
        receipt = restarted.health()["ownership"]
        restarted.suspend(False, previous=receipt)
        restarted.step()
        self.assertEqual(len(self.backends), 1)

    def test_unknown_engine_never_builds_or_restarts(self):
        backend = self.start()
        backend.caps["core"]["available"] = False
        backend.engine_unavailable = "unknown engine"
        self.supervisor.step()
        self.assertEqual(self.supervisor.adaptation()["state"], "contract_incompatible")
        self.build.assert_not_called()
        self.assertFalse(backend.closed)

    def test_cached_native_is_used_after_process_restart(self):
        cached = {**BASE, "testRuntime": {"cached": True}}
        self.store.load_active.return_value = cached
        backend = self.start()
        self.assertEqual(backend.profile["testRuntime"], {"cached": True})
        self.build.assert_not_called()

    def test_failure_status_stays_available_without_exposing_stale_capabilities(self):
        backend = self.start()
        backend.failure = "lost device"
        health = self.supervisor.health()
        self.assertIsNone(health["failure"])
        self.assertFalse(health["closed"])
        self.assertFalse(health["compatibility"]["capabilities"]["play"]["available"])

    def test_startup_health_retains_device_identity_and_has_no_usable_receipt(self):
        health = self.supervisor.health()
        self.assertEqual(health["serial"], "192.0.2.1:5555")
        self.assertEqual(health["api_version"], 11)
        self.assertIsNone(health["failure"])
        self.assertFalse(health["closed"])
        self.assertFalse(health["ownership"]["acknowledged"])
        self.assertEqual(health["ownership"]["mode"], "blocked")
        self.assertEqual(health["compatibility"]["capabilities"], {})

    def test_three_starts_per_fifteen_minutes_is_persisted(self):
        backend = self.start()
        for expected in (2, 3):
            backend.failure = "transport disconnected"
            self.supervisor.step()
            self.now = self.supervisor.next_retry
            self.supervisor.step()
            self.assertEqual(len(self.backends), expected)
            backend = self.backends[-1]
        backend.failure = "transport disconnected"
        self.supervisor.step()
        self.now = self.supervisor.next_retry
        self.supervisor.step()
        self.assertEqual(self.supervisor.reason, "restart_budget_exhausted")
        self.assertEqual(len(self.backends), 3)
        restarted = self.create()
        restarted.step()
        self.assertEqual(len(self.backends), 3)
        self.assertEqual(len(restarted.restart_times), 3)
        self.now = 1900
        restarted.step()
        self.assertEqual(len(self.backends), 4)

    def test_brief_ready_does_not_clear_the_failure_streak(self):
        self.supervisor._backoff("first_failure")
        self.now = self.supervisor.next_retry
        backend = self.start()
        self.supervisor.step()
        self.assertEqual(self.supervisor.failures, 1)
        self.now += 20
        backend.failure = "crashed after becoming ready"
        self.supervisor.step()
        self.assertEqual(self.supervisor.failures, 2)
        self.assertEqual(self.supervisor.next_retry - self.now, 30)
        self.assertEqual(self.create().failures, 2)

    def test_five_healthy_minutes_in_one_generation_clear_failure_streak(self):
        self.supervisor._backoff("first_failure")
        self.now = self.supervisor.next_retry
        backend = self.start()
        self.supervisor.step()
        self.now += 299
        self.supervisor.step()
        self.assertEqual(self.supervisor.failures, 1)
        backend.generation += 1
        self.supervisor.step()
        self.now += 299
        self.supervisor.step()
        self.assertEqual(self.supervisor.failures, 1)
        self.now += 1
        self.supervisor.step()
        self.assertEqual(self.supervisor.failures, 0)
        self.assertEqual(self.create().failures, 0)

    def test_uncertain_cleanup_prevents_any_replacement_until_it_succeeds(self):
        backend = self.start()
        backend.failure = "lost transport"
        backend.close_errors = [UnavailableError("cleanup uncertain"), UnavailableError("still uncertain")]
        for _ in range(2):
            with self.assertRaises(UnavailableError):
                self.supervisor.step()
            self.assertIs(self.supervisor.retiring_backend, backend)
            self.assertIsNone(self.supervisor.current)
            self.assertEqual(len(self.backends), 1)
        self.supervisor.step()
        self.assertTrue(backend.closed)
        self.assertIsNone(self.supervisor.retiring_backend)
        self.assertEqual(len(self.backends), 2)

    def _blocked_start(self):
        entered, errors = threading.Event(), []
        def configure(backend):
            def start(**kwargs):
                entered.set()
                if not backend.startup_cancelled.wait(3):
                    raise AssertionError("startup cancellation did not arrive")
                raise CancelledError("startup cancelled")
            backend.start.side_effect = start
        self.configure_backend = configure
        def step():
            try:
                self.supervisor.step()
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=step)
        self.supervisor.thread = worker
        worker.start()
        self.assertTrue(entered.wait(2))
        def cleanup():
            for backend in self.backends:
                backend.startup_cancelled.set()
            worker.join(3)
        self.addCleanup(cleanup)
        return worker, errors, self.backends[-1]

    def test_manual_handoff_during_startup_cancels_before_publishing_backend(self):
        worker, errors, backend = self._blocked_start()
        receipt = self.supervisor.suspend(True, owner_id="person")
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], CancelledError)
        self.assertTrue(backend.closed)
        self.assertIsNone(self.supervisor.current)
        self.assertFalse(receipt["acknowledged"])
        self.assertTrue(self.create().manual_hold)
        self.supervisor.step()
        self.assertEqual(len(self.backends), 1)

    def test_shutdown_cancels_and_closes_starting_backend(self):
        worker, errors, backend = self._blocked_start()
        self.supervisor.close()
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], CancelledError)
        self.assertTrue(backend.closed)
        self.assertIsNone(self.supervisor.current)
        self.assertIsNone(self.supervisor.starting_backend)
        self.assertGreaterEqual(backend.close_calls, 1)

    def test_recovery_handoff_rejects_old_epoch_and_foreign_owner(self):
        original = self.supervisor.health()["ownership"]
        receipt = self.supervisor.suspend(True, owner_id="person")
        with self.assertRaises(StaleResultError):
            self.supervisor.suspend(False, previous=original)
        with self.assertRaises(BusyError):
            self.supervisor.suspend(True, owner_id="other-person")
        self.assertEqual(self.supervisor.owner_id, "person")
        self.assertEqual(self.create().owner_id, "person")
        released = self.supervisor.suspend(False, previous=receipt)
        self.assertFalse(self.supervisor.manual_hold)
        self.assertGreater(released["epoch"], receipt["epoch"])
        with self.assertRaises(StaleResultError):
            self.supervisor.suspend(False, previous=receipt)

    def test_persisted_manual_hold_still_rejects_prior_service_receipt(self):
        old = self.supervisor.suspend(True, owner_id="person")
        restarted = self.create()
        with self.assertRaises(StaleResultError):
            restarted.suspend(False, previous=old)
        self.assertTrue(restarted.manual_hold)
        restarted.suspend(False, previous=restarted.health()["ownership"])
        self.assertFalse(restarted.manual_hold)

    def test_default_manual_owner_remains_idempotent(self):
        self.start()
        receipt = self.supervisor.suspend(True)
        self.assertEqual(receipt["owner_id"], "local-client")
        repeated = self.supervisor.suspend(True)
        self.assertEqual(repeated["handoff_id"], receipt["handoff_id"])
        self.assertEqual(self.create().owner_id, "local-client")

    def test_rejected_live_manual_owner_is_not_persisted(self):
        backend = self.start()
        self.supervisor.suspend(True, owner_id="person")
        with self.assertRaises(BusyError):
            self.supervisor.suspend(True, owner_id="other-person")
        self.assertEqual(self.supervisor.owner_id, "person")
        self.assertEqual(self.create().owner_id, "person")
        self.assertEqual(backend.fence.call_count, 1)

    def test_racing_live_handoff_rejection_rolls_back_persisted_intent(self):
        self.start()
        self.supervisor.current.suspend = Mock(side_effect=StaleResultError("epoch changed before dispatch"))
        with self.assertRaises(StaleResultError):
            self.supervisor.suspend(True, owner_id="rejected-person")
        self.assertFalse(self.supervisor.manual_hold)
        self.assertIsNone(self.supervisor.owner_id)
        self.assertFalse(self.create().manual_hold)

    def test_candidate_role_hints_persist_exact_resource_identity(self):
        backend = self.start()
        hints = [{"role": "find_flags", "bindings": [{"moduleId": "16807", "export": "b", "observedExport": "K"}]}]
        backend.roles = hints
        backend.resources = [{"name": BASE["appResource"]["name"], "sha256": "a" * 64}]
        self.supervisor.step()
        saved = json.loads((self.supervisor.path / "role-hints.json").read_text())
        self.assertEqual(saved["roles"], hints)
        self.assertEqual(saved["resource_sha256"], "a" * 64)
        self.assertIn("candidate_locations_only", saved["qualification"])
        restarted = self.create()
        self.assertEqual(restarted.base["adapter"]["roleHints"], hints)
        self.assertEqual(restarted.base["adapter"]["roleHintResourceSha256"], "a" * 64)
        changed = deepcopy(BASE)
        changed["adapter"]["modules"]["find_flags"] = 999
        self.assertNotIn("roleHints", self.create(base=changed).base["adapter"])
        backend.resources.append(deepcopy(backend.resources[0]))
        backend.resources[0]["sha256"] = "b" * 64
        self.supervisor.step()
        self.assertEqual(json.loads((self.supervisor.path / "role-hints.json").read_text()), saved)

    def test_malformed_role_cache_cannot_supply_candidate_bindings(self):
        path = self.supervisor.path / "role-hints.json"
        for payload in ("[1]", "not-json", "x" * 65537,
                        json.dumps({"key": self.supervisor.binding_key, "resource_sha256": "a" * 64,
                                    "roles": [{"role": "find_flags", "bindings": [{"moduleId": "../../file", "export": "b", "observedExport": "K"}]}]})):
            with self.subTest(payload=payload[:60]):
                path.write_text(payload)
                loaded = self.create()
                self.assertFalse(loaded.base["adapter"].get("roleHints"))

    def test_role_cache_without_exact_resource_digest_is_not_used(self):
        path = self.supervisor.path / "role-hints.json"
        roles = [{"role": "find_flags", "bindings": [{"moduleId": "16807", "export": "b", "observedExport": "K"}]}]
        for digest in (None, "invalid", "a" * 63):
            with self.subTest(digest=digest):
                path.write_text(json.dumps({"key": self.supervisor.binding_key, "roles": roles, "resource_sha256": digest}))
                self.assertFalse(self.create().base["adapter"].get("roleHints"))

    def test_contract_failure_is_reported_once_until_generation_changes(self):
        backend = self.start()
        backend.caps["native_inspection"].update(available=False, qualification="contract_failed", reasons=["wrong schema"])
        self.supervisor.step()
        self.assertEqual(self.supervisor.reason, "response_contract_unsupported")
        backend.adaptation_manifest.assert_not_called()
        revision = self.supervisor.revision
        self.supervisor.step()
        self.assertEqual(self.supervisor.revision, revision)
        backend.caps["native_inspection"].update(available=True, qualification="probeable_contract", reasons=[])
        backend.generation += 1
        self.supervisor.step()
        self.assertEqual(self.supervisor.state, "ready")

    def test_missing_initialization_retries_after_two_minutes_with_backoff(self):
        backend = self.start()
        backend.page_type = "RUST_HOME"
        backend.caps["search"].update(available=False, reasons=["module_initializing:find_flags"])
        self.now += 120
        self.supervisor.step()
        self.assertFalse(backend.closed)
        self.now += 1
        self.supervisor.step()
        self.assertTrue(backend.closed)
        self.assertEqual(self.supervisor.reason, "runtime_initialization_incomplete")
        self.assertEqual(self.supervisor.state, "backoff")
        self.assertIsNone(self.supervisor.current)
        self.supervisor.step()
        self.assertEqual(len(self.backends), 1)
        self.now = self.supervisor.next_retry
        self.supervisor.step()
        self.assertEqual(len(self.backends), 2)

    def test_failure_evidence_survives_retirement_and_service_restart(self):
        backend = self.start()
        backend.caps["search"].update(available=False, reasons=["module_not_initialized:find_flags"])
        backend.rows = [{"kind": "private_evidence", "data": {"url": "https://private.example/?token=secret"}}]
        self.now += 121
        self.supervisor.step()
        saved = json.loads((self.supervisor.path / "last-failure.json").read_text())
        self.assertEqual(saved["compatibility"]["capabilities"]["search"]["reasons"], ["module_not_initialized:find_flags"])
        self.assertEqual(saved["snapshot"]["runtime_records"], backend.rows)
        restarted = self.create()
        self.assertEqual(restarted.adaptation()["last_failure"]["reason"], "runtime_initialization_incomplete")
        self.assertNotIn("secret", json.dumps(restarted.health()))

    def test_failed_start_records_evidence_even_if_diagnostic_write_fails(self):
        error = RuntimeError("private startup exception")
        self.configure_backend = lambda b: setattr(b.start, "side_effect", error)
        original = self.supervisor._atomic
        def write(name, value):
            if name == "last-failure.json":
                raise OSError("disk full")
            return original(name, value)
        self.supervisor._atomic = write
        with self.assertRaises(RuntimeError) as caught:
            self.supervisor.step()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.supervisor.adaptation()["last_failure"]["error_type"], "RuntimeError")
        self.assertTrue(self.backends[0].closed)

    def test_large_startup_inventories_do_not_erase_dispatcher_evidence(self):
        backend = self.start()
        state = {"dispatcherReady": False, "page": {"pageType": "UNLOADED"}}
        backend.diagnostic_snapshot = lambda: {
            "seen": ["app_vm_found", "compatibility_degraded"], "latest_state": state,
            "launch_records": [{"kind": "compatibility", "data": "x" * (3 * 1024 * 1024)},
                               {"kind": "app_probe_waiting", "data": {"reason": "modules_not_initialized"}}],
            "runtime_records": [{"kind": "control_state", "data": state}],
        }
        self.supervisor._record_failure(backend, "runtime_initialization_incomplete")
        path = self.supervisor.path / "last-failure.json"
        self.assertLess(path.stat().st_size, 2 * 1024 * 1024)
        saved = json.loads(path.read_text())
        self.assertEqual(saved["snapshot"]["latest_state"], state)
        self.assertEqual(saved["snapshot"]["runtime_records"][0]["data"], state)
        self.assertEqual(saved["snapshot"]["launch_records"][-1]["kind"], "app_probe_waiting")
        self.assertEqual(saved["compatibility"], backend.compatibility())

    def test_pending_start_exposes_compatibility_without_granting_ownership(self):
        backend = Backend(BASE)
        backend.roles = [{"role": "plugin_command", "state": "missing"}]
        self.supervisor.starting_backend = backend
        health = self.supervisor.health()
        self.assertEqual(health["compatibility"]["adaptation"]["roles"], backend.roles)
        self.assertFalse(health["ownership"]["acknowledged"])
        self.assertEqual(health["ownership"]["mode"], "blocked")
        self.assertFalse(health["compatibility"]["capabilities"]["search"]["available"])
        self.assertTrue(health["compatibility"]["startup_capabilities"]["search"]["available"])
        # Publishing the controller makes capability and ownership readiness
        # visible together, even before the supervisor's next phase update.
        self.supervisor.current = Controller(backend)
        self.supervisor.starting_backend = None
        ready = self.supervisor.health()
        self.assertTrue(ready["compatibility"]["capabilities"]["search"]["available"])
        self.assertTrue(ready["ownership"]["acknowledged"])
        self.assertEqual(ready["ownership"]["session_id"], ready["session_id"])

    def test_intentional_shutdown_cancellation_does_not_increment_backoff(self):
        self.supervisor._backoff("prior_failure")
        deadline = self.supervisor.next_retry
        def stopped_step():
            self.supervisor.stop_event.set()
            raise CancelledError("shutdown")
        self.supervisor.step = stopped_step
        self.supervisor._run()
        self.assertEqual(self.supervisor.failures, 1)
        self.assertEqual(self.supervisor.next_retry, deadline)

    def test_optional_initialization_preserves_usable_rust_playback(self):
        backend = self.start()
        backend.rows = [self.playback_event("Playing")]
        backend.caps["search"].update(available=False, reasons=["module_initializing:find_flags"])
        backend.caps["broadcasts"].update(available=False, reasons=["module_not_initialized:find_broadcast_headers"])
        self.now += 121
        self.supervisor.step()
        self.assertEqual(self.supervisor.reason, "partial_runtime_playback_retained")
        self.assertEqual(self.supervisor.state, "observing")
        self.assertIs(self.supervisor.current.backend, backend)
        self.assertFalse(backend.closed)
        self.assertEqual(len(self.backends), 1)

    @staticmethod
    def playback_event(state, name="PLAYBACK_STATE_CHANGE"):
        return {"kind": "control_app_event", "data": {
            "name": name, "playbackEpoch": 3,
            "params": {"value": {"state": state}},
            "state": {"page": {"pageType": "RUST_PLAYBACK", "pageId": "playing-title"}}}}

    def test_ended_playback_page_does_not_block_initialization_recovery(self):
        backend = self.start()
        backend.caps["search"].update(available=False, reasons=["module_initializing:find_flags"])
        backend.rows = [self.playback_event("Playing"), self.playback_event("Ended")]
        self.now += 121
        self.supervisor.step()
        self.assertTrue(backend.closed)
        self.assertEqual(self.supervisor.reason, "runtime_initialization_incomplete")
        self.assertEqual(self.supervisor.state, "backoff")

    def test_partial_runtime_retention_rejects_idle_error_and_new_playback_boundaries(self):
        backend = self.start()
        for state, name in (("Paused", "PLAYBACK_STATE_CHANGE"), ("Stopped", "PLAYBACK_STATE_CHANGE"),
                            (None, "PLAYBACK_FATAL_ERROR"), (None, "PLAYBACK_EXPECTED_FATAL"),
                            (None, "PLAYBACK_UNLOAD"), (None, "NAVIGATE_TO_PAGE"),
                            (None, "PLAYBACK_LOAD"), (None, "PLAYBACK_CONTENT_SWITCH")):
            with self.subTest(state=state, name=name):
                backend.rows = [self.playback_event("Playing"), self.playback_event(state, name)]
                self.assertFalse(self.supervisor._has_retained_playback(backend, backend.health(), backend.caps))
        backend.rows = []
        self.assertFalse(self.supervisor._has_retained_playback(backend, backend.health(), backend.caps))

    def test_existing_status_checkpoint_retains_only_matching_active_playback(self):
        backend = self.start()
        checkpoint = {"page": backend.health()["state"]["page"], "epoch": 3,
                      "event": {"epoch": 3, "pageId": backend.page_id,
                                "name": "PLAYBACK_STATE_CHANGE", "rawState": "Buffering"}}
        backend.rows = [{"kind": "control_action_finished", "data": {
            "op": "playback-status", "result": checkpoint}}]
        self.assertTrue(self.supervisor._has_retained_playback(backend, backend.health(), backend.caps))
        checkpoint["event"]["epoch"] = 2
        self.assertFalse(self.supervisor._has_retained_playback(backend, backend.health(), backend.caps))
        checkpoint["event"]["epoch"] = 3
        checkpoint["event"]["rawState"] = "Ended"
        self.assertFalse(self.supervisor._has_retained_playback(backend, backend.health(), backend.caps))

    def test_input_uncertainty_before_epoch_change_preserves_existing_manual_owner(self):
        self.start()
        receipt = self.supervisor.suspend(True, owner_id="person")
        controller = self.supervisor.current
        def reject(*args, **kwargs):
            controller.ownership.input_uncertain = True
            raise OutcomeUnknownError("manual write became uncertain before transition")
        controller.suspend = Mock(side_effect=reject)
        with self.assertRaises(OutcomeUnknownError):
            self.supervisor.suspend(True, owner_id="unaccepted-person", previous=receipt)
        self.assertEqual(controller.ownership.epoch, receipt["epoch"])
        self.assertEqual(self.supervisor.owner_id, "person")
        self.assertTrue(self.supervisor.manual_hold)
        self.assertEqual(self.create().owner_id, "person")

    def test_uncertain_runtime_barrier_keeps_the_new_manual_hold(self):
        backend = self.start()
        original_epoch = self.supervisor.current.ownership.epoch
        backend.fence.side_effect = OutcomeUnknownError("runtime barrier may have executed")
        with self.assertRaises(OutcomeUnknownError):
            self.supervisor.suspend(True, owner_id="person")
        self.assertGreater(self.supervisor.current.ownership.epoch, original_epoch)
        self.assertTrue(self.supervisor.manual_hold)
        self.assertEqual(self.supervisor.owner_id, "person")
        self.assertTrue(self.create().manual_hold)
