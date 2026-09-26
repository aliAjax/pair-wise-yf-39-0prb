from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _as_temperature(value, field):
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be a number")
    if temperature != temperature:  # NaN
        raise ValidationError(field + " must be a number")
    return temperature


def _validate_observation(actor, data, lookup):
    rows = lookup("observation", "event_id", data.get("event_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("observed_at") == data.get("observed_at"):
            raise ConflictError("duplicate observation event")
    if not data.get("species"):
        raise ValidationError("species is required")


def _validate_sample(actor, data, lookup):
    observation = _find_one(lookup, "observation", "id", data.get("observation_id"))
    if not observation or observation["status"] not in ("submitted", "sampled"):
        raise ValidationError("sample requires a submitted observation")


def _validate_lab_result(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "negative"):
        raise ValidationError("lab result must be positive or negative")


def _ensure_not_in_pending_batch(observation_id, lookup):
    """暂停送检：关联观察仍有温度超限、待复核批次时，样本不得送实验室。"""
    for batch in lookup("transport_batch", "observation_id", observation_id) or []:
        if batch["status"] == "pending_review":
            raise InvalidTransition(
                "sample send paused: transport batch %s is pending review" % batch["id"]
            )


def _validate_send_lab(actor, entity, data, lookup):
    _ensure_not_in_pending_batch(entity["data"].get("observation_id"), lookup)
    return {}


def _validate_transport_batch(actor, data, lookup):
    observation = _find_one(lookup, "observation", "id", data.get("observation_id"))
    if not observation or observation["status"] != "submitted":
        raise ValidationError("transport batch requires a submitted observation")
    open_batches = [
        row
        for row in (lookup("transport_batch", "observation_id", observation["id"]) or [])
        if row["status"] in TRANSPORT_OPEN_STATUSES
    ]
    if open_batches:
        raise ConflictError("observation already has an open transport batch")
    upper = _as_temperature(data.get("max_temperature"), "max_temperature")
    if not data.get("origin") or not data.get("destination") or not data.get("carrier"):
        raise ValidationError("origin, destination and carrier are required")
    return {"max_temperature": upper}


def _ensure_carrier(actor, entity):
    """处置说明只能由登记批次的原承运人补。"""
    if actor.role != "admin" and actor.user_id != entity["data"].get("carrier"):
        raise PermissionDenied("only the original carrier can add a handling note")


def _validate_release(actor, entity, data, lookup):
    temperature = _as_temperature(data.get("departure_temp"), "departure_temp")
    limit = entity["data"]["max_temperature"]
    if temperature > limit:
        raise ValidationError(
            "departure temperature %.1f exceeds limit %.1f, batch cannot be released"
            % (temperature, limit)
        )
    return {
        "departure_temp": temperature,
        "released_at": _now(),
        "temperature_records": entity["data"].get("temperature_records", []) + [
            {
                "stage": "departure",
                "temperature": temperature,
                "limit": limit,
                "compliant": True,
                "at": _now(),
                "by": actor.user_id,
            }
        ],
        "handovers": entity["data"].get("handovers", []) + [
            {
                "stage": "departure",
                "from_party": entity["data"]["origin"],
                "to_party": entity["data"]["carrier"],
                "temperature": temperature,
                "at": _now(),
                "by": actor.user_id,
            }
        ],
    }


def _validate_receive(actor, entity, data, lookup):
    temperature = _as_temperature(data.get("arrival_temp"), "arrival_temp")
    limit = entity["data"]["max_temperature"]
    compliant = temperature <= limit
    now = _now()
    patch = {
        "arrival_temp": temperature,
        "received_at": now,
        "temperature_records": entity["data"].get("temperature_records", []) + [
            {
                "stage": "arrival",
                "temperature": temperature,
                "limit": limit,
                "compliant": compliant,
                "at": now,
                "by": actor.user_id,
            }
        ],
        "handovers": entity["data"].get("handovers", []) + [
            {
                "stage": "arrival",
                "from_party": entity["data"]["carrier"],
                "to_party": entity["data"]["destination"],
                "temperature": temperature,
                "at": now,
                "by": actor.user_id,
            }
        ],
    }
    if not compliant:
        # 接收实测超限：转待复核并暂停送检
        patch["handovers"][-1]["flag"] = "over_limit"
        return "pending_review", patch
    return "received", patch


def _validate_handling_note(actor, entity, data, lookup):
    _ensure_carrier(actor, entity)
    now = _now()
    return {
        "handling_note": data.get("handling_note"),
        "handling_note_at": now,
        "handovers": entity["data"].get("handovers", []) + [
            {
                "stage": "handling_note",
                "from_party": entity["data"]["carrier"],
                "to_party": "reviewer",
                "note": data.get("handling_note"),
                "at": now,
                "by": actor.user_id,
            }
        ],
    }


def _validate_review(actor, entity, data, lookup):
    if not entity["data"].get("handling_note"):
        raise ValidationError("carrier handling note is required before review")
    now = _now()
    patch = {
        "review_note": data.get("review_note", ""),
        "reviewed_by": actor.user_id,
        "reviewed_at": now,
        "handovers": entity["data"].get("handovers", []) + [
            {
                "stage": "review",
                "from_party": "reviewer",
                "to_party": entity["data"]["carrier"],
                "note": data.get("review_note", ""),
                "resume": True,
                "at": now,
                "by": actor.user_id,
            }
        ],
    }
    return "in_transit", patch


def _haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def is_cluster(observations, max_days=14, radius_km=10):
    if len(observations) < 3:
        return False
    points = observations[:3]
    same_window = all(
        abs(_date_ordinal(points[0].get("observed_at")) - _date_ordinal(item.get("observed_at"))) <= max_days
        for item in points[1:]
    )
    close = all(
        _haversine_km(points[0]["lat"], points[0]["lon"], item["lat"], item["lon"]) <= radius_km
        for item in points[1:]
    )
    return same_window and close


TRANSPORT_OPEN_STATUSES = ("registered", "in_transit", "pending_review")

CUSTOM_CREATE = {
    'observation': _validate_observation,
    'sample': _validate_sample,
    'transport_batch': _validate_transport_batch,
}
CUSTOM_TRANSITIONS = {
    ('sample', 'lab_result'): _validate_lab_result,
    ('sample', 'send_lab'): _validate_send_lab,
    ('transport_batch', 'release'): _validate_release,
    ('transport_batch', 'receive'): _validate_receive,
    ('transport_batch', 'add_handling_note'): _validate_handling_note,
    ('transport_batch', 'review'): _validate_review,
}


class RuleEngine:
    ALIASES = {
        'observations': 'observation',
        'samples': 'sample',
        'clusters': 'cluster',
        'transport_batches': 'transport_batch',
        'batches': 'transport_batch',
    }
    INITIAL_STATUS = {
        'observation': 'captured',
        'sample': 'collected',
        'cluster': 'draft',
        'transport_batch': 'registered',
    }
    TRANSITIONS = {
        'observation': {
            'submit': (('captured',), 'submitted'),
            'reject': (('submitted',), 'rejected'),
            'link_sample': (('submitted',), 'sampled'),
        },
        'sample': {
            'send_lab': (('collected',), 'in_lab'),
            'lab_result': (('in_lab',), 'resulted'),
            'retest': (('resulted',), 'in_lab'),
            'close': (('resulted',), 'closed'),
        },
        'cluster': {
            'confirm_cluster': (('draft',), 'confirmed'),
            'dismiss': (('draft',), 'dismissed'),
        },
        'transport_batch': {
            'release': (('registered',), 'in_transit'),
            'receive': (('in_transit',), 'received'),
            'add_handling_note': (('pending_review',), 'pending_review'),
            'review': (('pending_review',), 'in_transit'),
        },
    }
    CREATE_REQUIRED = {
        'observation': ('event_id', 'species', 'location', 'observed_at', 'lat', 'lon'),
        'sample': ('observation_id', 'sample_code'),
        'cluster': ('region',),
        'transport_batch': ('observation_id', 'box_code', 'origin', 'destination', 'carrier', 'max_temperature'),
    }
    ACTION_REQUIRED = {
        ('observation', 'submit'): ('location', 'observed_at'),
        ('observation', 'reject'): ('reason',),
        ('observation', 'link_sample'): ('sample_id',),
        ('sample', 'send_lab'): ('lab_id',),
        ('sample', 'lab_result'): ('result', 'result_at'),
        ('sample', 'retest'): ('reason',),
        ('sample', 'close'): ('outcome',),
        ('cluster', 'confirm_cluster'): ('observation_ids', 'centroid'),
        ('cluster', 'dismiss'): ('reason',),
        ('transport_batch', 'release'): ('departure_temp',),
        ('transport_batch', 'receive'): ('arrival_temp',),
        ('transport_batch', 'add_handling_note'): ('handling_note',),
    }
    CREATE_ROLES = {
        'observation': ('admin', 'field'),
        'sample': ('admin', 'field'),
        'cluster': ('admin', 'epidemiologist'),
        'transport_batch': ('admin', 'field'),
    }
    ROLE_ACTIONS = {
        'submit': ('admin', 'field'),
        'reject': ('admin', 'epidemiologist'),
        'link_sample': ('admin', 'field'),
        'send_lab': ('admin', 'field'),
        'lab_result': ('admin', 'lab'),
        'retest': ('admin', 'lab'),
        'close': ('admin', 'epidemiologist'),
        'confirm_cluster': ('admin', 'epidemiologist'),
        'dismiss': ('admin', 'epidemiologist'),
        ('transport_batch', 'release'): ('admin', 'field'),
        ('transport_batch', 'receive'): ('admin', 'field', 'lab'),
        ('transport_batch', 'add_handling_note'): ('admin', 'carrier'),
        ('transport_batch', 'review'): ('admin', 'reviewer'),
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
            extra = custom(actor, data, lookup) or {}
            merged = dict(data)
            merged.update(extra)
            data = merged
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
        if custom:
            result = custom(actor, entity, data, lookup)
            if isinstance(result, tuple):
                next_status, extra = result
            else:
                extra = result or {}
        else:
            extra = {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
