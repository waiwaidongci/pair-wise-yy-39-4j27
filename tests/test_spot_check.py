import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES

TRANSITIONS = ["inspected", "defect_confirmed", "repair", "verified", "closed"]


class SpotCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _closed_item(self, ref="SP-1", recheck_actor="inspector-a"):
        """创建一个走完流程并关闭的缺陷，复检(verified)与关闭操作人可指定。"""
        item = self.service.create_item(
            {"title": "spot item", "description": "spot check flow",
             "severity": "major", "quantity": 5, "threshold": 10,
             "external_ref": ref}, "creator", "inspector")
        self.service.add_record(
            item["id"], {"kind": "evidence", "detail": "repair evidence",
                         "status": "closed", "external_ref": "EV-" + ref},
            "recorder", "inspector")
        current = item
        actors = {"inspected": "inspector-x", "defect_confirmed": "engineer-y",
                  "repair": "engineer-y", "verified": recheck_actor,
                  "closed": "em-z"}
        for target in TRANSITIONS:
            current = self.service.transition(
                current["id"], target, current["version"],
                actors[target], TRANSITION_ROLES[target][0])
        return current, recheck_actor, "em-z"

    def test_create_spot_check_snapshots_close(self):
        item, recheck_actor, closed_by = self._closed_item()
        check = self.service.create_spot_check(
            item["id"], {"check_no": "SC-001",
                         "description": "汛期抽查复检结论",
                         "reviewer": "spot-reviewer"},
            "em-z", "emergency_manager")
        self.assertEqual(check["status"], "pending")
        self.assertEqual(check["reviewer"], "spot-reviewer")
        self.assertEqual(check["item_version"], item["version"])
        self.assertEqual(check["closed_by"], closed_by)
        self.assertEqual(check["closed_at"], item["updated_at"])
        self.assertEqual(len(self.service.list_spot_checks("viewer")), 1)
        self.assertEqual(
            len(self.service.list_spot_checks("viewer", item["id"])), 1)
        events = [e for e in self.service.audit("viewer", check["id"])
                  if e["entity_type"] == "处置抽检"]
        self.assertEqual(events[0]["action"], "spot_check_create")
        self.assertEqual(events[0]["actor"], "em-z")
        self.assertEqual(events[0]["detail"]["recheck_actor"], recheck_actor)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_only_emergency_manager_can_create(self):
        item, _, _ = self._closed_item("SP-2")
        with self.assertRaises(PermissionDenied):
            self.service.create_spot_check(
                item["id"], {"check_no": "SC-X", "description": "x",
                             "reviewer": "r"}, "viewer", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.create_spot_check(
                item["id"], {"check_no": "SC-X", "description": "x",
                             "reviewer": "r"}, "inspector-a", "inspector")

    def test_cannot_spot_check_unclosed_item(self):
        item = self.service.create_item(
            {"title": "open", "description": "open item", "severity": "major",
             "quantity": 1, "threshold": 1}, "creator", "inspector")
        with self.assertRaises(ConflictError):
            self.service.create_spot_check(
                item["id"], {"check_no": "SC-OPEN", "description": "x",
                             "reviewer": "r"}, "em-z", "emergency_manager")

    def test_only_one_open_spot_check_per_item(self):
        item, _, _ = self._closed_item("SP-3")
        payload = {"check_no": "SC-003", "description": "first",
                   "reviewer": "reviewer-1"}
        self.service.create_spot_check(item["id"], payload, "em-z",
                                       "emergency_manager")
        payload2 = {"check_no": "SC-003B", "description": "second",
                    "reviewer": "reviewer-2"}
        with self.assertRaises(ConflictError):
            self.service.create_spot_check(item["id"], payload2, "em-z",
                                           "emergency_manager")

    def test_duplicate_check_no_rejected_even_across_items(self):
        item1, _, _ = self._closed_item("SP-4A")
        item2, _, _ = self._closed_item("SP-4B")
        payload = {"check_no": "SC-DUP", "description": "same no",
                   "reviewer": "reviewer-1"}
        self.service.create_spot_check(item1["id"], payload, "em-z",
                                       "emergency_manager")
        with self.assertRaises(ConflictError):
            self.service.create_spot_check(item2["id"], dict(payload,
                                            reviewer="reviewer-2"),
                                           "em-z", "emergency_manager")

    def test_recheck_inspector_must_be_excluded(self):
        item, recheck_actor, _ = self._closed_item("SP-5")
        with self.assertRaises(ConflictError):
            self.service.create_spot_check(
                item["id"], {"check_no": "SC-005", "description": "x",
                             "reviewer": recheck_actor},
                "em-z", "emergency_manager")

    def test_pass_keeps_closed(self):
        item, _, _ = self._closed_item("SP-6")
        check = self.service.create_spot_check(
            item["id"], {"check_no": "SC-006", "description": "ok",
                         "reviewer": "spot-r"},
            "em-z", "emergency_manager")
        decided = self.service.decide_spot_check(
            check["id"], {"result": "passed", "expected_version": item["version"]},
            "spot-r", "inspector")
        self.assertEqual(decided["status"], "passed")
        self.assertEqual(decided["version"], 2)
        self.assertEqual(self.service.get_item(item["id"], "viewer")["status"],
                         "closed")
        events = [e for e in self.service.audit("viewer", check["id"])
                  if e["entity_type"] == "处置抽检"]
        self.assertEqual(events[-1]["detail"]["result"], "passed")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_fail_returns_to_repair_restarts_timer_and_preserves_close(self):
        item, _, _ = self._closed_item("SP-7")
        closed_at = item["updated_at"]
        check = self.service.create_spot_check(
            item["id"], {"check_no": "SC-007", "description": "review",
                         "reviewer": "spot-r"},
            "em-z", "emergency_manager")
        decided = self.service.decide_spot_check(
            check["id"], {"result": "failed", "note": "裂缝仍有扩展",
                          "expected_version": item["version"]},
            "spot-r", "dam_engineer")
        self.assertEqual(decided["status"], "failed")
        returned = self.service.get_item(item["id"], "viewer")
        self.assertEqual(returned["status"], "repair")
        self.assertEqual(returned["version"], item["version"] + 1)
        self.assertGreaterEqual(returned["updated_at"], closed_at)
        # 原关闭记录与原因快照仍保留在抽检单上
        stored = self.service.get_spot_check(check["id"], "viewer")
        self.assertEqual(stored["closed_at"], closed_at)
        self.assertEqual(stored["closed_by"], "em-z")
        kinds = [r["kind"] for r in self.service.list_records(item["id"], "viewer")]
        self.assertIn("spot_check_return", kinds)
        events = self.service.audit("viewer", item["id"])
        ret = [e for e in events if e["action"] == "spot_check_return"]
        self.assertEqual(ret[-1]["detail"]["to"], "repair")
        self.assertTrue(ret[-1]["detail"]["restart_timing"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_failed_can_reclose_and_open_new_spot_check(self):
        item, _, _ = self._closed_item("SP-8")
        check = self.service.create_spot_check(
            item["id"], {"check_no": "SC-008", "description": "review",
                         "reviewer": "spot-r"},
            "em-z", "emergency_manager")
        self.service.decide_spot_check(
            check["id"], {"result": "failed", "note": "复检造假",
                          "expected_version": item["version"]},
            "spot-r", "emergency_manager")
        current = self.service.get_item(item["id"], "viewer")
        # 重新复检（原复检人回避规则只针对发起时），再重新关闭
        current = self.service.transition(
            current["id"], "verified", current["version"], "inspector-b",
            TRANSITION_ROLES["verified"][0])
        current = self.service.transition(
            current["id"], "closed", current["version"], "em-z",
            TRANSITION_ROLES["closed"][0])
        second = self.service.create_spot_check(
            item["id"], {"check_no": "SC-008B", "description": "re-review",
                         "reviewer": "spot-r2"},
            "em-z", "emergency_manager")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(
            len(self.service.list_spot_checks("viewer", item["id"])), 2)

    def test_decide_rules(self):
        item, recheck_actor, _ = self._closed_item("SP-9", recheck_actor="insp-9")
        check = self.service.create_spot_check(
            item["id"], {"check_no": "SC-009", "description": "review",
                         "reviewer": "spot-r"},
            "em-z", "emergency_manager")
        base = {"result": "failed", "note": "x",
                "expected_version": item["version"]}
        # 非指定复检人不能判定
        with self.assertRaises(PermissionDenied):
            self.service.decide_spot_check(check["id"], base, "other",
                                           "emergency_manager")
        # viewer 无判定权限
        with self.assertRaises(PermissionDenied):
            self.service.decide_spot_check(check["id"], base, "spot-r", "viewer")
        # 不通过必须填写说明
        with self.assertRaises(ValidationError):
            self.service.decide_spot_check(
                check["id"], {"result": "failed",
                              "expected_version": item["version"]},
                "spot-r", "inspector")
        # 错误的 result
        with self.assertRaises(ValidationError):
            self.service.decide_spot_check(
                check["id"], {"result": "maybe",
                              "expected_version": item["version"]},
                "spot-r", "inspector")
        # 版本冲突
        with self.assertRaises(ConflictError):
            self.service.decide_spot_check(
                check["id"], {**base, "expected_version": 999}, "spot-r",
                "inspector")
        # 重复判定
        self.service.decide_spot_check(
            check["id"], {**base, "expected_version": item["version"]},
            "spot-r", "inspector")
        with self.assertRaises(ConflictError):
            self.service.decide_spot_check(
                check["id"], {"result": "passed",
                              "expected_version": item["version"]},
                "spot-r", "inspector")


if __name__ == "__main__":
    unittest.main()
