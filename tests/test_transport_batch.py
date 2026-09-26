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


OBSERVATION = {
    "event_id": "E-1",
    "species": "deer",
    "location": "North",
    "observed_at": "2026-09-20",
    "lat": 40.0,
    "lon": 116.0,
}


class TransportBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.field = Actor("field-1", "field")
        self.carrier = Actor("lenglian-wang", "carrier")
        self.other_carrier = Actor("other-driver", "carrier")
        self.reviewer = Actor("review-1", "reviewer")
        self.observation_id = self._submit_observation()

    def tearDown(self):
        self.tmp.cleanup()

    def _submit_observation(self, event_id="E-1"):
        data = dict(OBSERVATION, event_id=event_id)
        observation = self.service.create(self.field, "observation", data)
        observation = self.service.transition(
            self.field,
            observation["id"],
            "submit",
            {"location": data["location"], "observed_at": data["observed_at"]},
        )
        self.assertEqual(observation["status"], "submitted")
        return observation["id"]

    def _create_batch(self, box="BOX-1", max_temp=8.0, actor=None, observation_id=None):
        return self.service.create(
            actor or self.field,
            "transport_batch",
            {
                "observation_id": observation_id or self.observation_id,
                "box_code": box,
                "origin": "North station",
                "destination": "Provincial lab",
                "carrier": "lenglian-wang",
                "max_temperature": max_temp,
            },
        )

    def test_compliant_journey_flows_to_received(self):
        batch = self._create_batch()
        self.assertEqual(batch["status"], "registered")

        batch = self.service.transition(
            self.field, batch["id"], "release", {"departure_temp": 6.5}
        )
        self.assertEqual(batch["status"], "in_transit")

        batch = self.service.transition(
            self.field, batch["id"], "receive", {"arrival_temp": 7.0}
        )
        self.assertEqual(batch["status"], "received")

        temps = batch["data"]["temperature_records"]
        self.assertEqual([r["stage"] for r in temps], ["departure", "arrival"])
        self.assertTrue(all(r["compliant"] for r in temps))
        self.assertEqual(len(batch["data"]["handovers"]), 2)

    def test_departure_over_limit_blocks_release(self):
        batch = self._create_batch()
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.field, batch["id"], "release", {"departure_temp": 9.0}
            )
        self.assertEqual(self.service.get(batch["id"])["status"], "registered")

        # 降温后仍可放行
        batch = self.service.transition(
            self.field, batch["id"], "release", {"departure_temp": 8.0}
        )
        self.assertEqual(batch["status"], "in_transit")

    def test_arrival_over_limit_goes_pending_review_and_pauses_send_lab(self):
        sample = self.service.create(
            self.field,
            "sample",
            {"observation_id": self.observation_id, "sample_code": "W-1"},
        )
        batch = self._create_batch()
        batch = self.service.transition(
            self.field, batch["id"], "release", {"departure_temp": 6.0}
        )
        batch = self.service.transition(
            self.field, batch["id"], "receive", {"arrival_temp": 10.5}
        )
        self.assertEqual(batch["status"], "pending_review")

        # 暂停送检：样本不能送实验室
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.field, sample["id"], "send_lab", {"lab_id": "LAB-1"}
            )

        # 非原承运人不能补处置说明
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.other_carrier,
                batch["id"],
                "add_handling_note",
                {"handling_note": "ice packs added"},
            )

        # 原承运人补处置说明
        batch = self.service.transition(
            self.carrier,
            batch["id"],
            "add_handling_note",
            {"handling_note": "ice packs added, back to 6C"},
        )
        self.assertEqual(batch["status"], "pending_review")

        # 复核员角色才能确认
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.carrier, batch["id"], "review", {"review_note": "ok"}
            )
        batch = self.service.transition(
            self.reviewer,
            batch["id"],
            "review",
            {"review_note": "handled, resume transport"},
        )
        self.assertEqual(batch["status"], "in_transit")

        # 恢复后重新接收（合规），并恢复送检
        batch = self.service.transition(
            self.field, batch["id"], "receive", {"arrival_temp": 6.0}
        )
        self.assertEqual(batch["status"], "received")
        sample = self.service.transition(
            self.field, sample["id"], "send_lab", {"lab_id": "LAB-1"}
        )
        self.assertEqual(sample["status"], "in_lab")

    def test_review_requires_handling_note(self):
        batch = self._create_batch()
        batch = self.service.transition(
            self.field, batch["id"], "release", {"departure_temp": 6.0}
        )
        batch = self.service.transition(
            self.field, batch["id"], "receive", {"arrival_temp": 11.0}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.reviewer, batch["id"], "review", {"review_note": "skip note"}
            )

    def test_observation_cannot_have_two_open_batches(self):
        self._create_batch(box="BOX-1")
        with self.assertRaises(ConflictError):
            self._create_batch(box="BOX-2")

    def test_same_observation_reusable_after_batch_finished(self):
        batch = self._create_batch(box="BOX-1")
        batch = self.service.transition(
            self.field, batch["id"], "release", {"departure_temp": 6.0}
        )
        batch = self.service.transition(
            self.field, batch["id"], "receive", {"arrival_temp": 6.0}
        )
        self.assertEqual(batch["status"], "received")
        second = self._create_batch(box="BOX-2")
        self.assertEqual(second["status"], "registered")

    def test_duplicate_box_code_returns_first_batch(self):
        first = self._create_batch(box="BOX-DUP")
        duplicate = self._create_batch(box="BOX-DUP")
        self.assertEqual(first["id"], duplicate["id"])

        other_observation = self._submit_observation(event_id="E-2")
        duplicate = self.service.create(
            self.field,
            "transport_batch",
            {
                "observation_id": other_observation,
                "box_code": "BOX-DUP",
                "origin": "South",
                "destination": "Lab",
                "carrier": "someone-else",
                "max_temperature": 4.0,
            },
        )
        self.assertEqual(first["id"], duplicate["id"])

    def test_batch_requires_submitted_observation(self):
        draft = self.service.create(self.field, "observation", dict(OBSERVATION, event_id="E-9"))
        with self.assertRaises(ValidationError):
            self._create_batch(box="BOX-X", observation_id=draft["id"])

    def test_unknown_roles_still_rejected(self):
        from src.domain import Actor as _Actor
        with self.assertRaises(PermissionDenied):
            _Actor.from_headers({"X-Role": "stranger"})

    def test_audit_trail_records_every_handover(self):
        batch = self._create_batch()
        batch = self.service.transition(
            self.field, batch["id"], "release", {"departure_temp": 6.0}
        )
        batch = self.service.transition(
            self.field, batch["id"], "receive", {"arrival_temp": 12.0}
        )
        batch = self.service.transition(
            self.carrier,
            batch["id"],
            "add_handling_note",
            {"handling_note": "re-iced"},
        )
        batch = self.service.transition(
            self.reviewer, batch["id"], "review", {"review_note": "resume"}
        )
        actions = [row["action"] for row in self.service.audit_log(batch["id"])]
        self.assertEqual(
            actions,
            ["create", "release", "receive", "add_handling_note", "review"],
        )


if __name__ == "__main__":
    unittest.main()
