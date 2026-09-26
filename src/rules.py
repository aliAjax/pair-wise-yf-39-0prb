from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

NEXT_STATUS_MARKER = "__next_status__"
APPEND_MARKER = "__append__"
BATCH_TERMINAL_STATUSES = ("completed",)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


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


def _number(data, field):
    value = data.get(field)
    if isinstance(value, bool):
        raise ValidationError(field + " must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be a number")


def _validate_transport_batch(actor, data, lookup):
    observation = _find_one(lookup, "observation", "id", data.get("observation_id"))
    if not observation or observation["status"] != "submitted":
        raise ValidationError("transport batch requires a submitted observation")
    _number(data, "temp_limit")
    for batch in lookup("transport_batch", "observation_id", data["observation_id"]) or []:
        if batch["status"] not in BATCH_TERMINAL_STATUSES:
            raise ConflictError("observation already has an open transport batch")


def _batch_depart(actor, entity, data, lookup):
    departure_temp = _number(data, "departure_temp")
    limit = _number(entity["data"], "temp_limit")
    if departure_temp > limit:
        raise InvalidTransition(
            "departure temperature %s exceeds limit %s; release denied"
            % (departure_temp, limit)
        )
    patch = {
        APPEND_MARKER: {
            "temperature_logs": [
                {
                    "stage": "departure",
                    "temperature": departure_temp,
                    "measured_at": data.get("measured_at") or _now(),
                    "by": actor.user_id,
                    "compliant": True,
                }
            ],
            "handover_logs": [
                {
                    "stage": "departure",
                    "from_party": data.get("from_party") or entity["data"].get("origin"),
                    "to_party": data.get("to_party") or entity["data"].get("carrier"),
                    "note": data.get("note", ""),
                    "handover_at": data.get("measured_at") or _now(),
                    "by": actor.user_id,
                }
            ],
        }
    }
    # 首次离场的实际操作人即"原承运人"，之后只有他能补处置说明
    if actor.role == "carrier" and not entity["data"].get("carrier_user_id"):
        patch["carrier_user_id"] = actor.user_id
    return patch


def _batch_handover(actor, entity, data, lookup):
    entry = {
        "stage": "transit",
        "from_party": data.get("from_party", ""),
        "to_party": data["to_party"],
        "note": data.get("note", ""),
        "handover_at": data.get("handover_at") or _now(),
        "by": actor.user_id,
    }
    logs = []
    if data.get("temperature") is not None:
        temperature = _number(data, "temperature")
        limit = _number(entity["data"], "temp_limit")
        logs.append(
            {
                "stage": "transit",
                "temperature": temperature,
                "measured_at": data.get("measured_at") or entry["handover_at"],
                "by": actor.user_id,
                "compliant": temperature <= limit,
            }
        )
    appends = {"handover_logs": [entry]}
    if logs:
        appends["temperature_logs"] = logs
    return {APPEND_MARKER: appends}


def _batch_receive(actor, entity, data, lookup):
    arrival_temp = _number(data, "arrival_temp")
    limit = _number(entity["data"], "temp_limit")
    compliant = arrival_temp <= limit
    measured_at = data.get("measured_at") or _now()
    patch = {
        APPEND_MARKER: {
            "temperature_logs": [
                {
                    "stage": "arrival",
                    "temperature": arrival_temp,
                    "measured_at": measured_at,
                    "by": actor.user_id,
                    "compliant": compliant,
                }
            ],
            "handover_logs": [
                {
                    "stage": "arrival",
                    "from_party": data.get("from_party") or entity["data"].get("carrier"),
                    "to_party": data.get("to_party") or entity["data"].get("destination"),
                    "note": data.get("note", ""),
                    "handover_at": measured_at,
                    "by": actor.user_id,
                }
            ],
        }
    }
    if not compliant:
        patch["submission_suspended"] = True
        patch[NEXT_STATUS_MARKER] = "under_review"
    else:
        patch[NEXT_STATUS_MARKER] = "completed"
    return patch


def _batch_handling_note(actor, entity, data, lookup):
    if actor.user_id != entity["data"].get("carrier_user_id"):
        raise PermissionDenied("only the original carrier can add a handling note")
    return {
        APPEND_MARKER: {
            "handling_notes": [
                {
                    "note": data["note"],
                    "at": data.get("noted_at") or _now(),
                    "by": actor.user_id,
                }
            ]
        }
    }


def _batch_confirm_review(actor, entity, data, lookup):
    if not entity["data"].get("handling_notes"):
        raise ValidationError("carrier handling note is required before review")
    return {
        "reviewed_by": actor.user_id,
        "reviewed_at": data.get("reviewed_at") or _now(),
        "review_note": data.get("note", ""),
        "submission_suspended": False,
    }


def _validate_send_lab(actor, entity, data, lookup):
    batches = lookup("transport_batch", "observation_id", entity["data"].get("observation_id")) or []
    for batch in batches:
        if batch["data"].get("submission_suspended"):
            raise InvalidTransition(
                "sample submission is suspended pending transport review"
            )


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


CUSTOM_CREATE = {
    'observation': _validate_observation,
    'sample': _validate_sample,
    'transport_batch': _validate_transport_batch,
}
CUSTOM_TRANSITIONS = {
    ('sample', 'lab_result'): _validate_lab_result,
    ('sample', 'send_lab'): _validate_send_lab,
    ('transport_batch', 'depart'): _batch_depart,
    ('transport_batch', 'handover'): _batch_handover,
    ('transport_batch', 'receive'): _batch_receive,
    ('transport_batch', 'add_handling_note'): _batch_handling_note,
    ('transport_batch', 'confirm_review'): _batch_confirm_review,
}


class RuleEngine:
    ALIASES = {
        'observations': 'observation',
        'samples': 'sample',
        'clusters': 'cluster',
        'transport_batches': 'transport_batch',
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
            'depart': (('registered',), 'in_transit'),
            'handover': (('in_transit',), 'in_transit'),
            'receive': (('in_transit',), 'completed'),
            'add_handling_note': (('under_review',), 'under_review'),
            'confirm_review': (('under_review',), 'completed'),
        },
    }
    CREATE_REQUIRED = {
        'observation': ('event_id', 'species', 'location', 'observed_at', 'lat', 'lon'),
        'sample': ('observation_id', 'sample_code'),
        'cluster': ('region',),
        'transport_batch': (
            'observation_id',
            'box_code',
            'origin',
            'destination',
            'carrier',
            'temp_limit',
        ),
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
        ('transport_batch', 'depart'): ('departure_temp',),
        ('transport_batch', 'handover'): ('to_party',),
        ('transport_batch', 'receive'): ('arrival_temp',),
        ('transport_batch', 'add_handling_note'): ('note',),
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
        ('transport_batch', 'depart'): ('admin', 'field', 'carrier'),
        ('transport_batch', 'handover'): ('admin', 'field', 'carrier'),
        ('transport_batch', 'receive'): ('admin', 'field', 'lab'),
        ('transport_batch', 'add_handling_note'): ('carrier',),
        ('transport_batch', 'confirm_review'): ('admin', 'reviewer'),
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
        patch = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(patch or {})
        if NEXT_STATUS_MARKER in patch:
            next_status = patch.pop(NEXT_STATUS_MARKER)
        return next_status, patch


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
