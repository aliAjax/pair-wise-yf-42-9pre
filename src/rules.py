from datetime import date

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# Animal statuses that block pairing approval and offspring registration.
UNAVAILABLE_ANIMAL_STATUSES = ("quarantined", "departed", "deceased")
STATUS_LABELS = {
    "active": "在馆健康",
    "quarantined": "隔离",
    "departed": "离馆",
    "deceased": "死亡",
}

KINSHIP_LIMIT = 0.125


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
    if sire.get("dam_id") == dam_id or dam.get("dam_id") == sire_id:
        return 0.25
    return 0.0


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _latest_exam(lookup, animal_id):
    """Most recent recorded health exam for an animal, newest first."""
    exams = (lookup or (lambda *args: []))("health_exam", "animal_id", animal_id) or []
    if not exams:
        return None

    def sort_key(exam):
        raw = str(exam["data"].get("exam_date") or exam.get("created_at") or "")[:10]
        try:
            return date.fromisoformat(raw).toordinal()
        except ValueError:
            return 0

    ordered = sorted(
        exams,
        key=lambda exam: (sort_key(exam), exam.get("created_at") or ""),
        reverse=True,
    )
    return ordered[0]


def _animal_data(entity):
    """Animal payload with the entity id injected for kinship calculation."""
    data = dict(entity["data"])
    data.setdefault("id", entity["id"])
    return data


def _validate_health_exam(actor, data, lookup):
    result = data.get("result")
    if result not in ("passed", "failed"):
        raise ValidationError("result must be passed or failed")
    animal = _find_one(lookup, "animal", "id", data.get("animal_id"))
    if not animal:
        raise ValidationError("health exam requires an existing animal")
    if animal["status"] == "deceased":
        raise ValidationError("cannot record health exam for a deceased animal")
    if not data.get("exam_date"):
        data["exam_date"] = date.today().isoformat()


def _resolve_pair_animals(data):
    sire_id = data.get("sire_id")
    dam_id = data.get("dam_id")
    if not sire_id or not dam_id:
        raise ValidationError("pairing requires sire_id and dam_id")
    return sire_id, dam_id


def _validate_pairing_create(actor, data, lookup):
    sire_id, dam_id = _resolve_pair_animals(data)
    if sire_id == dam_id:
        raise ValidationError("pairing requires two different animals")
    sire = _find_one(lookup, "animal", "id", sire_id)
    dam = _find_one(lookup, "animal", "id", dam_id)
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    for animal in (sire, dam):
        if animal["status"] != "active":
            raise ValidationError(
                "animal %s is %s, pairing animals must be active"
                % (animal["id"], animal["status"])
            )
    _require_latest_exam(lookup, sire, data.get("sire_exam_id"))
    _require_latest_exam(lookup, dam, data.get("dam_exam_id"))
    if inbreeding_coefficient(_animal_data(sire), _animal_data(dam)) > KINSHIP_LIMIT:
        raise ValidationError("pairing exceeds kinship risk threshold 0.125")
    return {"proposed_by": actor.user_id, "proposed_by_role": actor.role}

def _require_latest_exam(lookup, animal, exam_id):
    if not exam_id:
        raise ValidationError("latest health exam id is required for animal " + animal["id"])
    exam = _find_one(lookup, "health_exam", "id", exam_id)
    if not exam:
        raise ValidationError("health exam %s does not exist" % exam_id)
    if exam["data"].get("animal_id") != animal["id"]:
        raise ValidationError(
            "health exam %s does not belong to animal %s" % (exam_id, animal["id"])
        )
    latest = _latest_exam(lookup, animal["id"])
    if not latest or latest["id"] != exam_id:
        raise ValidationError(
            "health exam %s is not the latest exam for animal %s (latest is %s)"
            % (exam_id, animal["id"], latest["id"] if latest else "none")
        )
    return exam


def _evaluate_pair(lookup, data):
    """Shared gate used at approval and resubmission time."""
    sire_id, dam_id = _resolve_pair_animals(data)
    sire = _find_one(lookup, "animal", "id", sire_id)
    dam = _find_one(lookup, "animal", "id", dam_id)
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")

    issues = []
    for animal, exam_id in ((sire, data.get("sire_exam_id")), (dam, data.get("dam_exam_id"))):
        if animal["status"] in UNAVAILABLE_ANIMAL_STATUSES:
            issues.append(
                "animal %s is %s" % (animal["id"], animal["status"])
            )
        else:
            exam = _require_latest_exam(lookup, animal, exam_id)
            if exam["data"].get("result") != "passed":
                issues.append(
                    "latest health exam %s for animal %s is %s"
                    % (exam["id"], animal["id"], exam["data"].get("result"))
                )

    coefficient = inbreeding_coefficient(_animal_data(sire), _animal_data(dam))
    if coefficient > KINSHIP_LIMIT:
        issues.append("kinship risk %.3f exceeds threshold 0.125" % coefficient)
    if issues:
        raise ValidationError("pairing cannot be approved: " + "; ".join(issues))
    return sire, dam, coefficient


def _validate_pairing_approve(actor, entity, data, lookup):
    # The proposal carries sire/dam and the referenced latest exam ids;
    # the committee may also override them in the action payload.
    proposal = dict(entity["data"])
    proposal.update({key: value for key, value in data.items() if value})
    sire, dam, coefficient = _evaluate_pair(lookup, proposal)
    return {
        "sire_id": sire["id"],
        "dam_id": dam["id"],
        "sire_exam_id": proposal.get("sire_exam_id"),
        "dam_exam_id": proposal.get("dam_exam_id"),
        "kinship_coefficient": coefficient,
        "approved_by": actor.user_id,
        "approved_by_role": actor.role,
    }


def _validate_pairing_resubmit(actor, entity, data, lookup):
    proposal = dict(entity["data"])
    for field in ("sire_exam_id", "dam_exam_id"):
        if data.get(field):
            proposal[field] = data[field]
    _evaluate_pair(lookup, proposal)
    return {
        "sire_exam_id": proposal.get("sire_exam_id"),
        "dam_exam_id": proposal.get("dam_exam_id"),
        "resubmitted_by": actor.user_id,
    }


def _validate_pairing_complete(actor, entity, data, lookup):
    proposal = entity["data"]
    sire = _find_one(lookup, "animal", "id", proposal.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", proposal.get("dam_id"))
    blockers = []
    for animal in (sire, dam):
        if animal and animal["status"] in UNAVAILABLE_ANIMAL_STATUSES:
            blockers.append(
                {
                    "animal_id": animal["id"],
                    "status": animal["status"],
                    "status_label": STATUS_LABELS.get(animal["status"], animal["status"]),
                }
            )
    if blockers:
        raise InvalidTransition(
            "offspring cannot be registered: "
            + "; ".join(
                "animal %s is %s" % (item["animal_id"], item["status_label"])
                for item in blockers
            )
        )


CUSTOM_CREATE = {
    'animal': _validate_animal,
    'health_exam': _validate_health_exam,
    'pairing': _validate_pairing_create,
}
CUSTOM_TRANSITIONS = {
    ('pairing', 'approve'): _validate_pairing_approve,
    ('pairing', 'resubmit'): _validate_pairing_resubmit,
    ('pairing', 'complete'): _validate_pairing_complete,
}


class RuleEngine:
    ALIASES = {
        'animals': 'animal',
        'pairings': 'pairing',
        'transfers': 'transfer',
        'health_exams': 'health_exam',
        'exams': 'health_exam',
    }
    INITIAL_STATUS = {
        'animal': 'active',
        'pairing': 'proposed',
        'transfer': 'planned',
        'health_exam': 'recorded',
    }
    TRANSITIONS = {
        'animal': {
            'mark_deceased': (('active', 'quarantined'), 'deceased'),
            'quarantine_animal': (('active',), 'quarantined'),
            'release_quarantine': (('quarantined',), 'active'),
            'depart_animal': (('active', 'quarantined'), 'departed'),
            'return_animal': (('departed',), 'active'),
        },
        'pairing': {
            'approve': (('proposed',), 'approved'),
            'reject': (('proposed',), 'rejected'),
            'resubmit': (('rejected', 'approved'), 'proposed'),
            'complete': (('approved',), 'completed'),
        },
        'transfer': {
            'authorize': (('planned',), 'authorized'),
            'ship': (('authorized',), 'in_transit'),
            'arrive': (('in_transit',), 'completed'),
        },
    }
    CREATE_REQUIRED = {
        'animal': ('name', 'sex'),
        'pairing': ('sire_id', 'dam_id', 'sire_exam_id', 'dam_exam_id'),
        'transfer': ('animal_id', 'from_institution', 'to_institution'),
        'health_exam': ('animal_id', 'result'),
    }
    ACTION_REQUIRED = {
        ('animal', 'mark_deceased'): ('cause',),
        ('animal', 'quarantine_animal'): ('reason',),
        ('animal', 'depart_animal'): ('reason',),
        ('pairing', 'approve'): (),
        ('pairing', 'reject'): ('reason',),
        ('pairing', 'complete'): ('offspring_ids',),
        ('transfer', 'authorize'): ('permit_id',),
        ('transfer', 'ship'): ('transport_id',),
        ('transfer', 'arrive'): ('arrival_date',),
    }
    CREATE_ROLES = {
        'animal': ('admin', 'registrar'),
        'pairing': ('admin', 'coordinator'),
        'transfer': ('admin', 'registrar'),
        'health_exam': ('admin', 'veterinarian'),
    }
    ROLE_ACTIONS = {
        'mark_deceased': ('admin', 'veterinarian'),
        'quarantine_animal': ('admin', 'veterinarian'),
        'release_quarantine': ('admin', 'veterinarian'),
        'depart_animal': ('admin', 'registrar'),
        'return_animal': ('admin', 'registrar'),
        'approve': ('admin', 'committee'),
        'reject': ('admin', 'committee'),
        'resubmit': ('admin', 'coordinator'),
        'complete': ('admin', 'coordinator'),
        'authorize': ('admin', 'registrar'),
        'ship': ('admin', 'registrar'),
        'arrive': ('admin', 'registrar'),
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
        extra = custom(actor, data, lookup) if custom else {}
        payload = dict(data)
        if extra:
            payload.update(extra)
        return payload

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
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def pairing_view(rules, lookup, pairing):
    """Enrich a pairing with animal/exam snapshot and committee decision aid."""
    view = dict(pairing)
    data = pairing.get("data", {})
    sire_id = data.get("sire_id")
    dam_id = data.get("dam_id")
    sire = _find_one(lookup, "animal", "id", sire_id) if sire_id else None
    dam = _find_one(lookup, "animal", "id", dam_id) if dam_id else None

    animals = []
    blockers = []
    issues = []
    for role_name, animal, ref_exam_id in (
        ("sire", sire, data.get("sire_exam_id")),
        ("dam", dam, data.get("dam_exam_id")),
    ):
        if not animal:
            issues.append("%s animal %s is missing" % (role_name, animal_id_of(role_name, data)))
            continue
        latest = _latest_exam(lookup, animal["id"])
        ref = _find_one(lookup, "health_exam", "id", ref_exam_id) if ref_exam_id else None
        entry = {
            "role": role_name,
            "animal_id": animal["id"],
            "animal_name": animal["data"].get("name"),
            "status": animal["status"],
            "status_label": STATUS_LABELS.get(animal["status"], animal["status"]),
            "referenced_exam_id": ref_exam_id,
            "referenced_exam_result": ref["data"].get("result") if ref else None,
            "latest_exam_id": latest["id"] if latest else None,
            "latest_exam_result": latest["data"].get("result") if latest else None,
            "latest_exam_date": latest["data"].get("exam_date") if latest else None,
        }
        animals.append(entry)
        if animal["status"] in UNAVAILABLE_ANIMAL_STATUSES:
            message = "动物 %s（%s）当前状态：%s" % (
                animal["id"],
                animal["data"].get("name", ""),
                entry["status_label"],
            )
            blockers.append({"animal_id": animal["id"], "status": animal["status"],
                             "status_label": entry["status_label"], "message": message})
            issues.append("animal %s is %s" % (animal["id"], animal["status"]))
        elif not latest:
            issues.append("animal %s has no health exam on record" % animal["id"])
        elif latest["data"].get("result") != "passed":
            issues.append(
                "latest health exam %s for animal %s is failed" % (latest["id"], animal["id"])
            )
        elif ref_exam_id != latest["id"]:
            issues.append(
                "referenced exam %s for animal %s is stale (latest %s)"
                % (ref_exam_id, animal["id"], latest["id"])
            )

    coefficient = None
    if sire and dam:
        coefficient = inbreeding_coefficient(_animal_data(sire), _animal_data(dam))
        if coefficient > KINSHIP_LIMIT:
            issues.append("kinship risk %.3f exceeds threshold 0.125" % coefficient)

    view["sire"] = next((item for item in animals if item["role"] == "sire"), None)
    view["dam"] = next((item for item in animals if item["role"] == "dam"), None)
    view["kinship_coefficient"] = coefficient
    view["kinship_within_limit"] = coefficient is not None and coefficient <= KINSHIP_LIMIT
    view["blockers"] = blockers
    view["can_register_offspring"] = pairing["status"] == "approved" and not blockers
    view["approvable"] = pairing["status"] == "proposed" and not issues
    view["approval_issues"] = issues
    return view


def animal_id_of(role_name, data):
    return data.get(role_name + "_id")
