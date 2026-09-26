from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, evaluate_pairing


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def pairing_review(self, entity_id):
        """Current committee view of a pairing: sides, checks, blockers."""
        pairing = self.repository.get_entity(entity_id)
        if not pairing:
            raise NotFoundError("entity not found: " + entity_id)
        if self.rules.normalize_kind(pairing["kind"]) != "pairing":
            raise NotFoundError("entity is not a pairing: " + entity_id)
        evaluation = evaluate_pairing(
            self._lookup,
            pairing["data"].get("sire_id"),
            pairing["data"].get("dam_id"),
            pairing["data"].get("sire_check_id"),
            pairing["data"].get("dam_check_id"),
        )
        return {
            "id": pairing["id"],
            "status": pairing["status"],
            "version": pairing["version"],
            "proposed_by": pairing["data"].get("proposed_by"),
            "approved_by": pairing["data"].get("approved_by"),
            "can_approve": pairing["status"] == "proposed"
            and not evaluation["approval_issues"],
            "can_register_offspring": pairing["status"] == "approved"
            and not evaluation["blockers"],
            "can_resubmit": pairing["status"] in ("approved", "rejected"),
            "sides": evaluation["sides"],
            "approval_issues": evaluation["approval_issues"],
            "blockers": evaluation["blockers"],
            "inbreeding_coefficient": evaluation["inbreeding_coefficient"],
            "inbreeding_limit": evaluation["inbreeding_limit"],
        }

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
