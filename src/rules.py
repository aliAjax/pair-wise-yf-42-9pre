from .domain import (
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# Pairing may only proceed when the inbreeding risk is at or below this value.
MAX_INBREEDING = 0.125
# Animal statuses in which it cannot take part in breeding.
UNAVAILABLE_ANIMAL_STATUS = ("quarantined", "departed", "deceased")


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def _find_one(lookup, kind, entity_id):
    if lookup is None or entity_id is None:
        return None
    rows = lookup(kind, "id", entity_id) or []
    return rows[0] if rows else None


def _pedigree(animal):
    """Coefficient calculation needs the entity id beside stored data."""
    if not animal:
        return None
    pedigree = dict(animal["data"])
    pedigree["id"] = animal["id"]
    return pedigree


def _animal_label(animal):
    return "%s(%s)" % (animal["data"].get("name", "未命名"), animal["id"])


def _check_side(lookup, side, animal_id, check_id):
    """Return (animal, check, issues) for one pairing participant."""
    issues = []
    animal = _find_one(lookup, "animal", animal_id)
    check = _find_one(lookup, "health_check", check_id)
    if not animal:
        issues.append({"side": side, "code": "animal_missing",
                       "message": "%s方动物不存在" % side_label(side)})
    if not check:
        issues.append({"side": side, "code": "check_missing",
                       "message": "%s方健康检查不存在: %s" % (side_label(side), check_id)})
    elif check["data"].get("animal_id") != animal_id:
        issues.append({
            "side": side,
            "code": "check_other_animal",
            "message": "%s方检查 %s 不属于动物 %s" % (side_label(side), check_id, animal_id),
        })
    if animal and animal["status"] != "active":
        issues.append({
            "side": side,
            "code": "animal_unavailable",
            "animal_id": animal_id,
            "animal_name": animal["data"].get("name"),
            "animal_status": animal["status"],
            "message": "%s方动物 %s 当前状态为 %s，不参与繁育"
                       % (side_label(side), _animal_label(animal), animal["status"]),
        })
    if check and check["status"] != "passed":
        issues.append({
            "side": side,
            "code": "check_not_passed",
            "check_id": check_id,
            "check_status": check["status"],
            "message": "%s方健康检查 %s 结果为 %s"
                       % (side_label(side), check_id, check["status"]),
        })
    return animal, check, issues


def side_label(side):
    return "父本" if side == "sire" else "母本"


def evaluate_pairing(lookup, sire_id, dam_id, sire_check_id, dam_check_id):
    """Evaluate health approval rules and post-approval registration blockers.

    Returns a dict with:
      approval_issues: reasons the committee cannot approve the pairing now;
      blockers: animals currently unable to register offspring (after approval);
      animals/checks: current snapshots of both sides.
    """
    sire, sire_check, sire_issues = _check_side(lookup, "sire", sire_id, sire_check_id)
    dam, dam_check, dam_issues = _check_side(lookup, "dam", dam_id, dam_check_id)

    if sire_id is not None and dam_id is not None and sire_id == dam_id:
        sire_issues.append({"side": "sire", "code": "same_animal",
                            "message": "父本与母本不能是同一只动物"})

    coefficient = inbreeding_coefficient(_pedigree(sire), _pedigree(dam))
    inbreeding_ok = coefficient <= MAX_INBREEDING
    if not inbreeding_ok:
        sire_issues.append({
            "side": "sire",
            "code": "inbreeding_too_high",
            "coefficient": coefficient,
            "message": "亲缘风险系数 %s 高于上限 %s" % (coefficient, MAX_INBREEDING),
        })

    blockers = []
    for side, animal in (("sire", sire), ("dam", dam)):
        if animal and animal["status"] in UNAVAILABLE_ANIMAL_STATUS:
            blockers.append({
                "side": side,
                "animal_id": animal["id"],
                "animal_name": animal["data"].get("name"),
                "animal_status": animal["status"],
                "message": "%s方动物 %s 当前状态为 %s，配对不得登记产仔"
                           % (side_label(side), _animal_label(animal), animal["status"]),
            })

    def snapshot(animal, check, side):
        return {
            "side": side,
            "animal_id": animal["id"] if animal else None,
            "animal_name": animal["data"].get("name") if animal else None,
            "animal_status": animal["status"] if animal else None,
            "check_id": check["id"] if check else None,
            "check_status": check["status"] if check else None,
            "checked_by": check["data"].get("checked_by") if check else None,
        }

    return {
        "approval_issues": sire_issues + dam_issues,
        "blockers": blockers,
        "inbreeding_coefficient": coefficient,
        "inbreeding_limit": MAX_INBREEDING,
        "sides": {
            "sire": snapshot(sire, sire_check, "sire"),
            "dam": snapshot(dam, dam_check, "dam"),
        },
    }


def _validate_pairing_create(actor, data, lookup):
    sire_id = data.get("sire_id")
    dam_id = data.get("dam_id")
    sire = _find_one(lookup, "animal", sire_id)
    dam = _find_one(lookup, "animal", dam_id)
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire_id == dam_id:
        raise ValidationError("sire and dam must be different animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active when proposed")
    for side, animal_id, check_id in (
        ("sire", sire_id, data.get("sire_check_id")),
        ("dam", dam_id, data.get("dam_check_id")),
    ):
        check = _find_one(lookup, "health_check", check_id)
        if not check:
            raise ValidationError(
                "%s side health check not found: %s" % (side, check_id)
            )
        if check["data"].get("animal_id") != animal_id:
            raise ValidationError(
                "%s side check %s does not belong to animal %s"
                % (side, check_id, animal_id)
            )
    # The committee can only approve while risk stays at or below the limit.
    if inbreeding_coefficient(_pedigree(sire), _pedigree(dam)) > MAX_INBREEDING:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {}


def _record_check_result(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("result must be passed or failed")
    return {"checked_by": actor.user_id}


def _approve_pairing(actor, entity, data, lookup):
    evaluation = evaluate_pairing(
        lookup,
        entity["data"].get("sire_id"),
        entity["data"].get("dam_id"),
        entity["data"].get("sire_check_id"),
        entity["data"].get("dam_check_id"),
    )
    issues = evaluation["approval_issues"]
    if issues:
        raise ValidationError(
            "pairing cannot be approved: " + "; ".join(i["message"] for i in issues),
            details={"issues": issues, "evaluation": evaluation},
        )
    return {"approved_by": actor.user_id}


def _complete_pairing(actor, entity, data, lookup):
    evaluation = evaluate_pairing(
        lookup,
        entity["data"].get("sire_id"),
        entity["data"].get("dam_id"),
        entity["data"].get("sire_check_id"),
        entity["data"].get("dam_check_id"),
    )
    blockers = evaluation["blockers"]
    if blockers:
        raise ValidationError(
            "pairing cannot register offspring: "
            + "; ".join(b["message"] for b in blockers),
            details={"blockers": blockers, "evaluation": evaluation},
        )
    return {}


CUSTOM_CREATE = {
    'animal': _validate_animal,
    'pairing': _validate_pairing_create,
}
CUSTOM_TRANSITIONS = {
    ('health_check', 'record_result'): _record_check_result,
    ('pairing', 'approve'): _approve_pairing,
    ('pairing', 'complete'): _complete_pairing,
}


class RuleEngine:
    ALIASES = {
        'animals': 'animal',
        'pairings': 'pairing',
        'transfers': 'transfer',
        'health_checks': 'health_check',
    }
    INITIAL_STATUS = {
        'animal': 'active',
        'pairing': 'proposed',
        'transfer': 'planned',
        'health_check': 'pending',
    }
    TRANSITIONS = {
        'animal': {
            'mark_deceased': (('active', 'quarantined'), 'deceased'),
            'quarantine_animal': (('active',), 'quarantined'),
            'release_quarantine': (('quarantined',), 'active'),
            'depart_animal': (('active', 'quarantined'), 'departed'),
        },
        'pairing': {
            'approve': (('proposed',), 'approved'),
            'reject': (('proposed',), 'rejected'),
            'resubmit': (('approved', 'rejected'), 'proposed'),
            'complete': (('approved',), 'completed'),
        },
        'transfer': {
            'authorize': (('planned',), 'authorized'),
            'ship': (('authorized',), 'in_transit'),
            'arrive': (('in_transit',), 'completed'),
        },
        'health_check': {
            'record_result': (('pending',), None),
        },
    }
    CREATE_REQUIRED = {
        'animal': ('name', 'sex'),
        'pairing': ('proposed_by', 'sire_id', 'dam_id', 'sire_check_id', 'dam_check_id'),
        'transfer': ('animal_id', 'from_institution', 'to_institution'),
        'health_check': ('animal_id',),
    }
    ACTION_REQUIRED = {
        ('animal', 'mark_deceased'): ('cause',),
        ('animal', 'quarantine_animal'): ('reason',),
        ('animal', 'depart_animal'): ('reason',),
        ('pairing', 'reject'): ('reason',),
        ('pairing', 'resubmit'): (),
        ('pairing', 'complete'): ('offspring_ids',),
        ('transfer', 'authorize'): ('permit_id',),
        ('transfer', 'ship'): ('transport_id',),
        ('transfer', 'arrive'): ('arrival_date',),
        ('health_check', 'record_result'): ('result',),
    }
    CREATE_ROLES = {
        'animal': ('admin', 'registrar'),
        'pairing': ('admin', 'coordinator'),
        'transfer': ('admin', 'registrar'),
        'health_check': ('admin', 'veterinarian'),
    }
    ROLE_ACTIONS = {
        'mark_deceased': ('admin', 'veterinarian'),
        'quarantine_animal': ('admin', 'veterinarian'),
        'release_quarantine': ('admin', 'veterinarian'),
        'depart_animal': ('admin', 'registrar'),
        'approve': ('admin', 'committee'),
        'reject': ('admin', 'committee'),
        'resubmit': ('admin', 'coordinator'),
        'complete': ('admin', 'coordinator'),
        'authorize': ('admin', 'registrar'),
        'ship': ('admin', 'registrar'),
        'arrive': ('admin', 'registrar'),
        ('health_check', 'record_result'): ('admin', 'veterinarian'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        # A transition may choose its next status dynamically
        # (e.g. health_check.record_result -> passed/failed).
        if next_status is None:
            next_status = data["result"]
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
