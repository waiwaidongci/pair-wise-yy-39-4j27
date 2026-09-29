from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .domain import (ConflictError, NotFoundError, PermissionDenied,
                     ValidationError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    SPOT_CHECK_CREATE_ROLES, SPOT_CHECK_JUDGE_ROLES,
                    SPOT_CHECK_RESULTS, SPOT_CHECK_STATUSES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def create_spot_check(self, item_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SPOT_CHECK_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        check_no = require_text(payload.get("check_no"), "check_no", 100)
        description = require_text(payload.get("description"), "description")
        checker = require_text(payload.get("checker"), "checker", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "closed":
            raise ConflictError("只有已关闭的缺陷才能发起抽检")
        if self.repository.get_open_spot_check(item_id) is not None:
            raise ConflictError("同一缺陷存在未结抽检")
        verifier = self.repository.latest_transition_actor(item_id, "verified")
        if verifier is not None and checker == verifier:
            raise ValidationError("被抽检缺陷的复检人不能参加这次抽检")
        check = self.repository.create_spot_check(
            check_no, item_id, description, checker, item["version"],
            verifier or "", item["version"], item["updated_at"], actor)
        self.repository.append_audit("spot_check_create", "处置抽检", check["id"], actor, {
            "check_no": check_no, "item_id": item_id, "checker": checker,
            "item_version": item["version"], "closed_by": verifier,
        })
        return check

    def judge_spot_check(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SPOT_CHECK_JUDGE_ROLES)
        actor = require_text(actor, "actor", 100)
        check_no = require_text(payload.get("check_no"), "check_no", 100)
        result = payload.get("result")
        if result not in SPOT_CHECK_RESULTS:
            raise ValidationError("result必须是passed或failed")
        note = payload.get("result_note")
        if note is not None:
            note = require_text(note, "result_note")
        if result == "failed":
            note = require_text(note, "退回维修必须填写原因")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        check = self.repository.get_spot_check_by_no(check_no)
        if check is None or check["item_id"] != item_id:
            raise NotFoundError("抽检不存在")
        if check["status"] != "pending":
            raise ConflictError("抽检已判定，不能重复判定")
        if actor != check["checker"]:
            raise PermissionDenied("只有指定复检人才能判定本次抽检")
        updated_check = self.repository.judge_spot_check(
            check["id"], result, note, actor, expected_version)
        detail: Dict[str, Any] = {
            "check_no": check_no, "item_id": item_id, "result": result,
            "from_version": expected_version, "check_version": updated_check["version"],
        }
        if note is not None:
            detail["result_note"] = note
        if result == "failed":
            detail["from"] = item["status"]
            detail["to"] = "repair"
            detail["closed_version"] = check["closed_version"]
        self.repository.append_audit("spot_check_judge", "处置抽检", check["id"], actor, detail)
        if result == "failed":
            self.repository.append_audit("transition", ENTITY, item_id, actor, {
                "from": item["status"], "to": "repair",
                "via_spot_check": check_no,
                "escalation_required": escalation_required(
                    item["severity"], item["quantity"], item["threshold"]),
            })
        return updated_check

    def list_spot_checks(self, role: str, item_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_spot_checks(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        returned_at = item.get("last_returned_at")
        if item["status"] == "repair" and returned_at:
            result["repair_restarted_at"] = returned_at
            result["repair_deadline_at"] = (
                datetime.fromisoformat(returned_at)
                + timedelta(hours=result["deadline_hours"])
            ).isoformat()
        return result
