import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class BreedingWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.coordinator = Actor("coord-1", "coordinator")
        self.vet = Actor("vet-1", "veterinarian")
        self.other_vet = Actor("vet-2", "veterinarian")
        self.committee = Actor("board-1", "committee")

    def tearDown(self):
        self.tmp.cleanup()

    def _animals(self, sire_data=None, dam_data=None):
        sire = self.service.create(
            self.admin, "animal", {"name": "Sire", "sex": "male", **(sire_data or {})},
        )
        dam = self.service.create(
            self.admin, "animal", {"name": "Dam", "sex": "female", **(dam_data or {})},
        )
        return sire, dam

    def _checks(self, sire, dam, sire_result="passed", dam_result="passed"):
        sire_check = self.service.create(
            self.vet, "health_check", {"animal_id": sire["id"]}
        )
        dam_check = self.service.create(
            self.other_vet, "health_check", {"animal_id": dam["id"]}
        )
        if sire_result:
            sire_check = self.service.transition(
                self.vet, sire_check["id"], "record_result", {"result": sire_result}
            )
        if dam_result:
            dam_check = self.service.transition(
                self.other_vet, dam_check["id"], "record_result", {"result": dam_result}
            )
        return sire_check, dam_check

    def _proposal(self, sire, dam, sire_check, dam_check, actor=None):
        return self.service.create(
            actor or self.coordinator,
            "pairing",
            {
                "proposed_by": (actor or self.coordinator).user_id,
                "sire_id": sire["id"],
                "dam_id": dam["id"],
                "sire_check_id": sire_check["id"],
                "dam_check_id": dam_check["id"],
            },
        )

    def _approved_pairing(self, sire_result="passed", dam_result="passed"):
        sire, dam = self._animals()
        sire_check, dam_check = self._checks(sire, dam, sire_result, dam_result)
        pairing = self._proposal(sire, dam, sire_check, dam_check)
        pairing = self.service.transition(
            self.committee, pairing["id"], "approve", {}
        )
        return sire, dam, sire_check, dam_check, pairing

    def test_vet_records_result_with_own_account(self):
        sire, _ = self._animals()
        check = self.service.create(
            self.vet, "health_check", {"animal_id": sire["id"]}
        )
        self.assertEqual(check["status"], "pending")
        updated = self.service.transition(
            self.vet, check["id"], "record_result", {"result": "passed"}
        )
        self.assertEqual(updated["status"], "passed")
        self.assertEqual(updated["data"]["checked_by"], "vet-1")

    def test_coordinator_and_other_roles_cannot_record_results(self):
        sire, _ = self._animals()
        check = self.service.create(
            self.vet, "health_check", {"animal_id": sire["id"]}
        )
        for actor in (self.coordinator, self.committee, Actor("v", "viewer")):
            with self.assertRaises(PermissionDenied):
                self.service.transition(
                    actor, check["id"], "record_result", {"result": "passed"}
                )

    def test_result_can_only_be_recorded_once(self):
        sire, _ = self._animals()
        check = self.service.create(
            self.vet, "health_check", {"animal_id": sire["id"]}
        )
        self.service.transition(
            self.vet, check["id"], "record_result", {"result": "passed"}
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.other_vet, check["id"], "record_result", {"result": "failed"}
            )

    def test_proposal_requires_check_ids_belonging_to_each_animal(self):
        sire, dam = self._animals()
        sire_check, dam_check = self._checks(sire, dam)
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "pairing",
                {
                    "proposed_by": "coord-1",
                    "sire_id": sire["id"],
                    "dam_id": dam["id"],
                    "sire_check_id": dam_check["id"],
                    "dam_check_id": dam_check["id"],
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "pairing",
                {
                    "proposed_by": "coord-1",
                    "sire_id": sire["id"],
                    "dam_id": dam["id"],
                    "sire_check_id": sire_check["id"],
                },
            )

    def test_failed_check_blocks_approval_and_details_the_check(self):
        sire, dam = self._animals()
        sire_check, dam_check = self._checks(
            sire, dam, sire_result="failed", dam_result="passed"
        )
        pairing = self._proposal(sire, dam, sire_check, dam_check)
        with self.assertRaises(ValidationError) as caught:
            self.service.transition(self.committee, pairing["id"], "approve", {})
        codes = {issue["code"] for issue in caught.exception.details["issues"]}
        self.assertIn("check_not_passed", codes)

    def test_only_committee_or_admin_can_approve(self):
        sire, dam = self._animals()
        sire_check, dam_check = self._checks(sire, dam)
        pairing = self._proposal(sire, dam, sire_check, dam_check)
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.coordinator, pairing["id"], "approve", {})
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.vet, pairing["id"], "approve", {})
        approved = self.service.transition(
            self.committee, pairing["id"], "approve", {}
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["data"]["approved_by"], "board-1")

    def test_inbreeding_above_threshold_blocks_proposal(self):
        # sire is the father of dam -> coefficient 0.25 > 0.125
        sire, _ = self._animals()
        dam = self.service.create(
            self.admin,
            "animal",
            {"name": "Dam", "sex": "female", "sire_id": sire["id"]},
        )
        sire_check, dam_check = self._checks(sire, dam)
        with self.assertRaises(ValidationError):
            self._proposal(sire, dam, sire_check, dam_check)

    def _assert_blocked_complete(self, pairing, animal, status):
        with self.assertRaises(ValidationError) as caught:
            self.service.transition(
                self.coordinator,
                pairing["id"],
                "complete",
                {"offspring_ids": ["baby-1"]},
            )
        blockers = caught.exception.details["blockers"]
        self.assertEqual(len(blockers), 1)
        blocker = blockers[0]
        self.assertEqual(blocker["animal_id"], animal["id"])
        self.assertEqual(blocker["animal_status"], status)
        self.assertIn(animal["id"], blocker["message"])
        self.assertIn(status, blocker["message"])

    def test_quarantine_blocks_offspring_registration_then_recovery_allows_resubmit(self):
        sire, dam, _, _, pairing = self._approved_pairing()

        self.service.transition(
            self.vet, sire["id"], "quarantine_animal", {"reason": "fever"}
        )
        sire = self.service.get(sire["id"])
        review = self.service.pairing_review(pairing["id"])
        self.assertFalse(review["can_register_offspring"])
        self.assertEqual(review["blockers"][0]["animal_id"], sire["id"])
        self.assertEqual(review["blockers"][0]["animal_status"], "quarantined")
        self._assert_blocked_complete(pairing, sire, "quarantined")

        # Animal recovers: the same proposal goes back to committee review.
        self.service.transition(self.vet, sire["id"], "release_quarantine", {})
        pairing = self.service.transition(
            self.coordinator, pairing["id"], "resubmit", {}
        )
        self.assertEqual(pairing["status"], "proposed")
        pairing = self.service.transition(
            self.committee, pairing["id"], "approve", {}
        )
        completed = self.service.transition(
            self.coordinator,
            pairing["id"],
            "complete",
            {"offspring_ids": ["baby-1"]},
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["data"]["offspring_ids"], ["baby-1"])

    def test_departed_and_deceased_block_offspring_registration(self):
        for action, data, status in (
            ("depart_animal", {"reason": "loan ended"}, "departed"),
            ("mark_deceased", {"cause": "illness"}, "deceased"),
        ):
            sire, dam, _, _, pairing = self._approved_pairing()
            self.service.transition(
                self.admin if status == "departed" else self.vet,
                sire["id"],
                action,
                data,
            )
            sire = self.service.get(sire["id"])
            self._assert_blocked_complete(pairing, sire, status)
            # Recovery is impossible: resubmission still cannot be approved.
            pairing = self.service.transition(
                self.coordinator, pairing["id"], "resubmit", {}
            )
            with self.assertRaises(ValidationError):
                self.service.transition(
                    self.committee, pairing["id"], "approve", {}
                )

    def test_rejected_proposal_can_be_resubmitted(self):
        sire, dam = self._animals()
        sire_check, dam_check = self._checks(sire, dam)
        pairing = self._proposal(sire, dam, sire_check, dam_check)
        pairing = self.service.transition(
            self.committee, pairing["id"], "reject", {"reason": "season"}
        )
        self.assertEqual(pairing["status"], "rejected")
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.vet, pairing["id"], "resubmit", {})
        pairing = self.service.transition(
            self.coordinator, pairing["id"], "resubmit", {}
        )
        self.assertEqual(pairing["status"], "proposed")
        pairing = self.service.transition(
            self.committee, pairing["id"], "approve", {}
        )
        self.assertEqual(pairing["status"], "approved")

    def test_review_reports_checks_and_live_animal_status(self):
        sire, dam = self._animals()
        sire_check = self.service.create(
            self.vet, "health_check", {"animal_id": sire["id"]}
        )
        sire_check = self.service.transition(
            self.vet, sire_check["id"], "record_result", {"result": "passed"}
        )
        dam_check = self.service.create(
            self.other_vet, "health_check", {"animal_id": dam["id"]}
        )
        pairing = self._proposal(sire, dam, sire_check, dam_check)
        review = self.service.pairing_review(pairing["id"])
        self.assertFalse(review["can_approve"])
        self.assertEqual(review["sides"]["sire"]["check_status"], "passed")
        self.assertEqual(review["sides"]["dam"]["check_status"], "pending")
        self.assertEqual(
            {issue["code"] for issue in review["approval_issues"]},
            {"check_not_passed"},
        )

    def test_registrar_cannot_approve_pairing(self):
        sire, dam = self._animals()
        sire_check, dam_check = self._checks(sire, dam)
        pairing = self._proposal(sire, dam, sire_check, dam_check)
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("reg-1", "registrar"), pairing["id"], "approve", {}
            )

    def test_rejected_with_failed_check_can_attach_new_check_on_resubmit(self):
        sire, dam = self._animals()
        old_sire_check = self.service.create(
            self.vet, "health_check", {"animal_id": sire["id"]}
        )
        self.service.transition(
            self.vet, old_sire_check["id"], "record_result", {"result": "failed"}
        )
        dam_check = self.service.create(
            self.other_vet, "health_check", {"animal_id": dam["id"]}
        )
        dam_check = self.service.transition(
            self.other_vet, dam_check["id"], "record_result", {"result": "passed"}
        )
        pairing = self._proposal(sire, dam, old_sire_check, dam_check)
        with self.assertRaises(ValidationError):
            self.service.transition(self.committee, pairing["id"], "approve", {})

        # A newer, passing check is attached when resubmitting the same proposal.
        new_sire_check = self.service.create(
            self.vet, "health_check", {"animal_id": sire["id"]}
        )
        new_sire_check = self.service.transition(
            self.vet, new_sire_check["id"], "record_result", {"result": "passed"}
        )
        pairing = self.service.transition(
            self.committee, pairing["id"], "reject", {"reason": "等待复查"}
        )
        pairing = self.service.transition(
            self.coordinator,
            pairing["id"],
            "resubmit",
            {"sire_check_id": new_sire_check["id"]},
        )
        self.assertEqual(pairing["data"]["sire_check_id"], new_sire_check["id"])
        pairing = self.service.transition(
            self.committee, pairing["id"], "approve", {}
        )
        self.assertEqual(pairing["status"], "approved")

    def test_review_unknown_pairing_404(self):
        from src.domain import NotFoundError

        with self.assertRaises(NotFoundError):
            self.service.pairing_review("does-not-exist")


if __name__ == "__main__":
    unittest.main()
