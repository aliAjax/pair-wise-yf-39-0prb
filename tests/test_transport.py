import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def submitted_observation(service, event_id="E-T"):
    return service.transition(
        Actor("admin", "admin"),
        service.create(
            Actor("field-1", "field"),
            "observation",
            {
                "event_id": event_id,
                "species": "deer",
                "location": "North",
                "observed_at": "2026-05-01",
                "lat": 40.0,
                "lon": 116.0,
            },
        )["id"],
        "submit",
        {"location": "North", "observed_at": "2026-05-01"},
    )


class TransportBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.field = Actor("field-1", "field")
        self.carrier = Actor("driver-chen", "carrier")
        self.reviewer = Actor("reviewer-li", "reviewer")
        self.lab = Actor("lab-1", "lab")

    def tearDown(self):
        self.tmp.cleanup()

    def _register(self, observation_id=None, box_code="BOX-1", actor=None, extra=None):
        observation_id = observation_id or submitted_observation(self.service)["id"]
        payload = {
            "observation_id": observation_id,
            "box_code": box_code,
            "origin": "North station",
            "destination": "Provincial lab",
            "carrier": "Chen Logistics",
            "temp_limit": 8.0,
        }
        if extra:
            payload.update(extra)
        return self.service.create(actor or self.field, "transport_batch", payload)

    def test_happy_path_appends_temperature_and_handover_logs(self):
        batch = self._register()
        self.assertEqual(batch["status"], "registered")
        self.assertEqual(batch["data"]["temperature_logs"], [])

        batch = self.service.transition(
            self.carrier, batch["id"], "depart", {"departure_temp": 6.5}
        )
        self.assertEqual(batch["status"], "in_transit")
        self.assertEqual(batch["data"]["temperature_logs"][0]["stage"], "departure")

        batch = self.service.transition(
            self.carrier,
            batch["id"],
            "handover",
            {"from_party": "Chen Logistics", "to_party": "Relay Zhao", "temperature": 7.2},
        )
        self.assertEqual(batch["status"], "in_transit")
        self.assertEqual(len(batch["data"]["handover_logs"]), 2)
        self.assertEqual(len(batch["data"]["temperature_logs"]), 2)

        batch = self.service.transition(
            self.lab, batch["id"], "receive", {"arrival_temp": 7.9}
        )
        self.assertEqual(batch["status"], "completed")
        self.assertFalse(batch["data"].get("submission_suspended"))
        self.assertEqual(len(batch["data"]["temperature_logs"]), 3)
        stages = [entry["stage"] for entry in batch["data"]["handover_logs"]]
        self.assertEqual(stages, ["departure", "transit", "arrival"])

    def test_departure_over_limit_is_rejected(self):
        batch = self._register()
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.carrier, batch["id"], "depart", {"departure_temp": 9.0}
            )
        self.assertEqual(self.service.get(batch["id"])["status"], "registered")

    def test_arrival_over_limit_goes_under_review_and_suspends_submission(self):
        observation = submitted_observation(self.service, event_id="E-S")
        batch = self._register(observation_id=observation["id"], box_code="BOX-S")
        sample = self.service.create(
            self.field,
            "sample",
            {"observation_id": observation["id"], "sample_code": "W-S"},
        )

        batch = self.service.transition(
            self.carrier, batch["id"], "depart", {"departure_temp": 5.0}
        )
        batch = self.service.transition(
            self.lab, batch["id"], "receive", {"arrival_temp": 10.5}
        )
        self.assertEqual(batch["status"], "under_review")
        self.assertTrue(batch["data"]["submission_suspended"])
        self.assertFalse(batch["data"]["temperature_logs"][-1]["compliant"])

        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.field, sample["id"], "send_lab", {"lab_id": "LAB-1"}
            )

        # 原承运人补处置说明
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("other-driver", "carrier"),
                batch["id"],
                "add_handling_note",
                {"note": "I opened the cooler"},
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.reviewer,
                batch["id"],
                "add_handling_note",
                {"note": "reviewer cannot note"},
            )
        batch = self.service.transition(
            self.carrier,
            batch["id"],
            "add_handling_note",
            {"note": "Cooler lid was loose; repacked with ice packs."},
        )
        self.assertEqual(batch["status"], "under_review")
        self.assertEqual(len(batch["data"]["handling_notes"]), 1)

        # 复核员确认后恢复
        batch = self.service.transition(
            self.reviewer, batch["id"], "confirm_review", {"note": "Accepted"}
        )
        self.assertEqual(batch["status"], "completed")
        self.assertFalse(batch["data"]["submission_suspended"])
        self.assertEqual(batch["data"]["reviewed_by"], "reviewer-li")

        sample = self.service.transition(
            self.field, sample["id"], "send_lab", {"lab_id": "LAB-1"}
        )
        self.assertEqual(sample["status"], "in_lab")

    def test_review_requires_handling_note(self):
        batch = self._register(box_code="BOX-R")
        batch = self.service.transition(
            self.carrier, batch["id"], "depart", {"departure_temp": 4.0}
        )
        batch = self.service.transition(
            self.lab, batch["id"], "receive", {"arrival_temp": 12.0}
        )
        self.assertEqual(batch["status"], "under_review")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.reviewer, batch["id"], "confirm_review", {"note": "no note yet"}
            )

    def test_one_observation_cannot_have_two_open_batches(self):
        observation = submitted_observation(self.service, event_id="E-D")
        first = self._register(observation_id=observation["id"], box_code="BOX-D1")
        with self.assertRaises(ConflictError):
            self._register(observation_id=observation["id"], box_code="BOX-D2")

        # 批次结束后同一观察可以再开一批
        first = self.service.transition(
            self.carrier, first["id"], "depart", {"departure_temp": 4.0}
        )
        first = self.service.transition(
            self.lab, first["id"], "receive", {"arrival_temp": 4.5}
        )
        self.assertEqual(first["status"], "completed")
        second = self._register(observation_id=observation["id"], box_code="BOX-D2")
        self.assertNotEqual(first["id"], second["id"])

    def test_duplicate_box_code_returns_first_open_batch(self):
        first = self._register(box_code="BOX-DUP")
        other_observation = submitted_observation(self.service, event_id="E-OTHER")
        replay = self._register(
            observation_id=other_observation["id"], box_code="BOX-DUP"
        )
        self.assertEqual(first["id"], replay["id"])
        # 被拒绝方的观察没有被绑定到任何批次
        bound = [
            item
            for item in self.service.list("transport_batch")
            if item["data"]["observation_id"] == other_observation["id"]
        ]
        self.assertEqual(bound, [])

    def test_register_requires_submitted_observation(self):
        captured = self.service.create(
            self.field,
            "observation",
            {
                "event_id": "E-C",
                "species": "deer",
                "location": "North",
                "observed_at": "2026-05-02",
                "lat": 40.0,
                "lon": 116.0,
            },
        )
        with self.assertRaises(ValidationError):
            self._register(observation_id=captured["id"], box_code="BOX-X")

    def test_viewer_cannot_register(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("v", "viewer"),
                "transport_batch",
                {
                    "observation_id": submitted_observation(self.service, "E-V")["id"],
                    "box_code": "BOX-V",
                    "origin": "a",
                    "destination": "b",
                    "carrier": "c",
                    "temp_limit": 8,
                },
            )

    def test_audit_timeline_records_transport_actions(self):
        batch = self._register(box_code="BOX-A")
        self.service.transition(
            self.carrier, batch["id"], "depart", {"departure_temp": 5.0}
        )
        actions = [row["action"] for row in self.service.audit_log(batch["id"])]
        self.assertEqual(actions, ["create", "depart"])
        depart_row = [row for row in self.service.audit_log(batch["id"]) if row["action"] == "depart"][0]
        self.assertEqual(depart_row["from_status"], "registered")
        self.assertEqual(depart_row["to_status"], "in_transit")


if __name__ == "__main__":
    unittest.main()
