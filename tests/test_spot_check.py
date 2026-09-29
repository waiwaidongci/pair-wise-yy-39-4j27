import tempfile, unittest
from datetime import datetime
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES

VERIFIER = "verifier_zhang"
CHECKER = "checker_li"
MANAGER = "manager_wang"


class SpotCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _closed_item(self, ref="SC-1"):
        item = self.service.create_item({
            "title": "spot item", "description": "spot check target",
            "severity": "major", "quantity": 12, "threshold": 6,
            "external_ref": ref,
        }, "creator", "inspector")
        self.service.add_record(item["id"], {
            "kind": "evidence", "detail": "repair evidence",
            "status": "closed", "external_ref": ref + "-EV",
        }, "recorder", "inspector")
        current = item
        for target in STATES[1:]:
            if target == "verified":
                actor = VERIFIER
            elif target == "closed":
                actor = MANAGER
            else:
                actor = "actor_x"
            current = self.service.transition(
                current["id"], target, current["version"], actor,
                TRANSITION_ROLES[target][0])
        return current

    def _start_check(self, item, check_no="JC-1", checker=CHECKER):
        return self.service.create_spot_check(item["id"], {
            "check_no": check_no, "description": "汛期抽查复检结论",
            "checker": checker,
        }, MANAGER, "emergency_manager")

    def test_pass_keeps_closed_and_preserves_history(self):
        item = self._closed_item()
        closed_version = item["version"]
        check = self._start_check(item)
        self.assertEqual(check["status"], "pending")
        self.assertEqual(check["closed_by"], VERIFIER)
        self.assertEqual(check["item_version"], closed_version)

        judged = self.service.judge_spot_check(item["id"], {
            "check_no": "JC-1", "result": "passed",
            "expected_version": closed_version,
        }, CHECKER, "inspector")
        self.assertEqual(judged["status"], "passed")
        self.assertEqual(self.service.get_item(item["id"], "viewer")["status"], "closed")
        self.assertEqual(self.service.get_item(item["id"], "viewer")["version"], closed_version)

        events = self.service.audit("viewer", item["id"])
        actions = [e["action"] for e in events]
        self.assertIn("transition", actions)
        close_event = [e for e in events if e["action"] == "transition"
                       and e["detail"].get("to") == "closed"][-1]
        self.assertEqual(close_event["actor"], MANAGER)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_fail_returns_to_repair_restarts_timer_and_keeps_close_record(self):
        item = self._closed_item()
        closed_version = item["version"]
        self._start_check(item)
        judged = self.service.judge_spot_check(item["id"], {
            "check_no": "JC-1", "result": "failed",
            "result_note": "复检结论与现场不符，重新维修",
            "expected_version": closed_version,
        }, CHECKER, "dam_engineer")
        self.assertEqual(judged["status"], "failed")

        refreshed = self.service.get_item(item["id"], "viewer")
        self.assertEqual(refreshed["status"], "repair")
        self.assertEqual(refreshed["version"], closed_version + 1)
        self.assertIsNotNone(refreshed["last_returned_at"])
        self.assertIn("repair_deadline_at", refreshed)
        deadline = datetime.fromisoformat(refreshed["repair_deadline_at"])
        restarted = datetime.fromisoformat(refreshed["repair_restarted_at"])
        self.assertGreater(deadline, restarted)

        events = self.service.audit("viewer", item["id"])
        self.assertTrue(any(e["action"] == "transition"
                            and e["detail"].get("to") == "closed" for e in events))
        judge_event = [e for e in events if e["action"] == "spot_check_judge"][-1]
        self.assertEqual(judge_event["actor"], CHECKER)
        self.assertEqual(judge_event["detail"]["from"], "closed")
        self.assertEqual(judge_event["detail"]["closed_version"], closed_version)
        self.assertTrue(self.repo.verify_audit_chain())

        current = self.service.transition(item["id"], "verified", refreshed["version"],
                                          "another_inspector", "inspector")
        current = self.service.transition(item["id"], "closed", current["version"],
                                          MANAGER, "emergency_manager")
        second = self._start_check(current, "JC-2", "third_inspector")
        self.assertEqual(second["status"], "pending")

    def test_only_manager_can_start_only_checker_can_judge(self):
        item = self._closed_item()
        with self.assertRaises(PermissionDenied):
            self.service.create_spot_check(item["id"], {
                "check_no": "JC-X", "description": "x", "checker": CHECKER,
            }, "inspector_1", "inspector")
        with self.assertRaises(PermissionDenied):
            self.service.judge_spot_check(item["id"], {
                "check_no": "JC-NEW", "result": "passed",
                "expected_version": item["version"],
            }, MANAGER, "emergency_manager")

        self._start_check(item)
        with self.assertRaises(PermissionDenied):
            self.service.judge_spot_check(item["id"], {
                "check_no": "JC-1", "result": "passed",
                "expected_version": item["version"],
            }, "someone_else", "inspector")
        with self.assertRaises(PermissionDenied):
            self.service.judge_spot_check(item["id"], {
                "check_no": "JC-1", "result": "passed",
                "expected_version": item["version"],
            }, "viewer_x", "viewer")

    def test_verifier_cannot_be_checker(self):
        item = self._closed_item()
        with self.assertRaises(ValidationError):
            self._start_check(item, checker=VERIFIER)

    def test_open_check_unique_per_item_and_check_no_unique(self):
        item = self._closed_item()
        self._start_check(item)
        with self.assertRaises(ConflictError):
            self._start_check(item, check_no="JC-OTHER")
        other = self._closed_item("SC-2")
        with self.assertRaises(ConflictError):
            self._start_check(other, check_no="JC-1")

    def test_cannot_start_on_unclosed_item(self):
        item = self.service.create_item({
            "title": "open item", "description": "still open",
            "severity": "minor", "quantity": 1, "threshold": 5,
            "external_ref": "SC-OPEN",
        }, "creator", "inspector")
        with self.assertRaises(ConflictError):
            self._start_check(item)

    def test_judge_guards_version_reason_and_double_judge(self):
        item = self._closed_item()
        self._start_check(item)
        with self.assertRaises(ConflictError):
            self.service.judge_spot_check(item["id"], {
                "check_no": "JC-1", "result": "passed",
                "expected_version": item["version"] + 1,
            }, CHECKER, "inspector")
        with self.assertRaises(ValidationError):
            self.service.judge_spot_check(item["id"], {
                "check_no": "JC-1", "result": "failed",
                "expected_version": item["version"],
            }, CHECKER, "inspector")
        with self.assertRaises(ValidationError):
            self.service.judge_spot_check(item["id"], {
                "check_no": "JC-1", "result": "unknown",
                "expected_version": item["version"],
            }, CHECKER, "inspector")

        self.service.judge_spot_check(item["id"], {
            "check_no": "JC-1", "result": "passed",
            "expected_version": item["version"],
        }, CHECKER, "inspector")
        with self.assertRaises(ConflictError):
            self.service.judge_spot_check(item["id"], {
                "check_no": "JC-1", "result": "failed",
                "result_note": "retry", "expected_version": item["version"],
            }, CHECKER, "inspector")

    def test_listing_and_audit_responsibility_trace(self):
        item = self._closed_item()
        self._start_check(item)
        listing = self.service.list_spot_checks("viewer", item["id"])
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["created_by"], MANAGER)
        self.assertEqual(listing[0]["checker"], CHECKER)
        self.assertEqual(len(self.service.list_spot_checks("viewer")), 1)
        events = self.service.audit("viewer")
        created = [e for e in events if e["action"] == "spot_check_create"][-1]
        self.assertEqual(created["actor"], MANAGER)
        self.assertEqual(created["detail"]["check_no"], "JC-1")
        self.assertEqual(created["detail"]["item_version"], item["version"])


if __name__ == "__main__":
    unittest.main()
