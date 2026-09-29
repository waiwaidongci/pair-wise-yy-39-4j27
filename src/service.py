from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    SPOT_CREATE_ROLES, SPOT_DECIDE_ROLES, SPOT_ENTITY, TITLE,
                    VIEW_ROLES, completion_blockers, ensure_reviewer_independent,
                    escalation_required, priority_score, response_deadline_hours,
                    role_for_transition, spot_check_can_open, validate_transition)


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
        ensure_role(role, SPOT_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        check_no = require_text(payload.get("check_no"), "check_no", 100)
        description = require_text(payload.get("description"), "description")
        reviewer = require_text(payload.get("reviewer"), "reviewer", 100)
        item = self.repository.get_item(item_id)
        if not spot_check_can_open(item["status"]):
            raise ConflictError("只能对已关闭的缺陷发起处置抽检")
        # 当时的复检结论由 repair→verified 的操作人负责，其本人不得参加本次抽检
        recheck_actor = self.repository.latest_transition_actor(item_id, "verified")
        ensure_reviewer_independent(reviewer, recheck_actor)
        close_actor = self.repository.latest_transition_actor(item_id, "closed")
        close_reason = self._latest_close_reason(item_id)
        check = self.repository.create_spot_check(
            check_no, item_id, description, reviewer, item["version"],
            item["updated_at"], close_actor, close_reason, actor)
        self.repository.append_audit("spot_check_create", SPOT_ENTITY, check["id"], actor, {
            "check_no": check_no, "item_id": item_id,
            "item_version": item["version"], "reviewer": reviewer,
            "recheck_actor": recheck_actor, "closed_by": close_actor,
        })
        return check

    def decide_spot_check(self, check_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SPOT_DECIDE_ROLES)
        actor = require_text(actor, "actor", 100)
        result = payload.get("result")
        if result not in ("passed", "failed"):
            from .domain import ValidationError
            raise ValidationError("result必须是passed或failed")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note")
        if result == "failed" and not note:
            from .domain import ValidationError
            raise ValidationError("抽检不通过必须填写判定说明")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        check = self.repository.get_spot_check(check_id)
        if check["status"] != "pending":
            raise ConflictError("抽检已判定，不能重复判定")
        if check["reviewer"] != actor:
            from .domain import PermissionDenied
            raise PermissionDenied("只有指定的复检人能判定本次抽检")
        return_record = None
        if result == "failed":
            # 退回维修并重新计时；原关闭记录与关闭原因已快照保留在抽检单上
            return_record = {
                "kind": "spot_check_return",
                "detail": "抽检不通过退回维修：" + note,
                "external_ref": "SPOT-" + str(check_id),
            }
        outcome = self.repository.decide_spot_check(
            check_id, result, note, actor, expected_version, return_record)
        decided = outcome["spot_check"]
        self.repository.append_audit("spot_check_decide", SPOT_ENTITY, check_id, actor, {
            "check_no": check["check_no"], "item_id": check["item_id"],
            "result": result, "version": decided["version"],
            "item_version_expected": expected_version,
            "item_status": outcome["item_status"],
        })
        if result == "failed":
            self.repository.append_audit("spot_check_return", ENTITY, check["item_id"], actor, {
                "from": "closed", "to": "repair", "check_no": check["check_no"],
                "reason": note, "restart_timing": True,
            })
        return decided

    def get_spot_check(self, check_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_spot_check(check_id)

    def list_spot_checks(self, role: str, item_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_spot_checks(item_id)

    def _latest_close_reason(self, item_id: int) -> Optional[str]:
        events = self.repository.list_audit(item_id)
        for event in reversed(events):
            if event["action"] == "transition" and event["detail"].get("to") == "closed":
                return event["detail"].get("reason")
        return None

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
