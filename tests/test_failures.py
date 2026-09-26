import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.vet = Actor("vet-1", "veterinarian")
        self.coordinator = Actor("coordinator-1", "coordinator")
        self.committee = Actor("committee-1", "committee")

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, name, sex, extra=None):
        data = {'name': name, 'sex': sex}
        data.update(extra or {})
        return self.service.create(self.admin, 'animal', data)

    def _exam(self, animal_id, result, exam_date='2026-09-01'):
        return self.service.create(
            self.vet, 'health_exam',
            {'animal_id': animal_id, 'result': result, 'exam_date': exam_date},
        )

    def _pairing(self, sire, dam, sire_exam, dam_exam):
        return self.service.create(
            self.coordinator, 'pairing',
            {
                'proposed_by': 'coordinator-1',
                'sire_id': sire['id'],
                'dam_id': dam['id'],
                'sire_exam_id': sire_exam['id'],
                'dam_exam_id': dam_exam['id'],
            },
        )

    def test_permission_denied(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'animal', {'name': 'A', 'sex': 'male'}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                entity["id"],
                'mark_deceased',
                {'cause': 'illness'},
            )

    def test_version_conflict(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'animal', {'name': 'A', 'sex': 'male'}
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                Actor("admin", "admin"),
                entity["id"],
                'mark_deceased',
                {'cause': 'illness'},
                expected_version=999,
            )

    def test_duplicate_idempotency_key_returns_same_entity(self):
        first = self.service.create(
            Actor("admin", "admin"),
            'animal',
            {'name': 'A', 'sex': 'male'},
            idempotency_key="duplicate-check",
        )
        second = self.service.create(
            Actor("admin", "admin"),
            'animal',
            {'name': 'A', 'sex': 'male'},
            idempotency_key="duplicate-check",
        )
        self.assertEqual(first["id"], second["id"])

    def test_pairing_requires_latest_exam_ids(self):
        sire = self._animal('M-1', 'male')
        dam = self._animal('F-1', 'female')
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator, 'pairing',
                {'proposed_by': 'coordinator-1', 'sire_id': sire['id'], 'dam_id': dam['id']},
            )

    def test_pairing_rejects_non_latest_exam(self):
        sire = self._animal('M-1', 'male')
        dam = self._animal('F-1', 'female')
        old_exam = self._exam(sire['id'], 'passed', '2026-08-01')
        self._exam(sire['id'], 'passed', '2026-09-01')
        dam_exam = self._exam(dam['id'], 'passed')
        with self.assertRaises(ValidationError) as caught:
            self._pairing(sire, dam, old_exam, dam_exam)
        self.assertIn("not the latest exam", str(caught.exception))

    def test_approve_blocked_when_exam_failed(self):
        sire = self._animal('M-1', 'male')
        dam = self._animal('F-1', 'female')
        sire_exam = self._exam(sire['id'], 'failed')
        dam_exam = self._exam(dam['id'], 'passed')
        # A coordinator may submit the proposal, but the committee cannot approve it.
        pairing = self._pairing(sire, dam, sire_exam, dam_exam)
        with self.assertRaises(ValidationError) as caught:
            self.service.transition(self.committee, pairing["id"], 'approve', {})
        self.assertIn(sire['id'], str(caught.exception))

    def test_approve_blocked_when_exam_turns_failed_after_proposal(self):
        sire = self._animal('M-1', 'male')
        dam = self._animal('F-1', 'female')
        sire_exam = self._exam(sire['id'], 'passed', '2026-09-01')
        dam_exam = self._exam(dam['id'], 'passed')
        pairing = self._pairing(sire, dam, sire_exam, dam_exam)
        # Vet records a newer failed exam for the sire after the proposal.
        self._exam(sire['id'], 'failed', '2026-09-10')
        with self.assertRaises(ValidationError) as caught:
            self.service.transition(self.committee, pairing["id"], 'approve', {})
        self.assertIn(sire['id'], str(caught.exception))

    def test_approve_requires_committee_role(self):
        sire = self._animal('M-1', 'male')
        dam = self._animal('F-1', 'female')
        pairing = self._pairing(sire, dam, self._exam(sire['id'], 'passed'), self._exam(dam['id'], 'passed'))
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.coordinator, pairing["id"], 'approve', {})

    def test_kinship_above_threshold_rejected(self):
        sire = self._animal('M-1', 'male')
        dam = self._animal('F-1', 'female', {'sire_id': sire['id']})
        sire_exam = self._exam(sire['id'], 'passed')
        dam_exam = self._exam(dam['id'], 'passed')
        with self.assertRaises(ValidationError) as caught:
            self._pairing(sire, dam, sire_exam, dam_exam)
        self.assertIn("kinship", str(caught.exception))

    def test_departed_animal_blocks_offspring_registration(self):
        sire = self._animal('M-1', 'male')
        dam = self._animal('F-1', 'female')
        pairing = self._pairing(sire, dam, self._exam(sire['id'], 'passed'), self._exam(dam['id'], 'passed'))
        self.service.transition(self.committee, pairing["id"], 'approve', {})
        self.service.transition(self.admin, dam["id"], 'depart_animal', {'reason': 'loan to Zoo-B'})
        with self.assertRaises(InvalidTransition) as caught:
            self.service.transition(
                self.coordinator, pairing["id"], 'complete', {'offspring_ids': ['offspring-1']}
            )
        message = str(caught.exception)
        self.assertIn(dam["id"], message)
        self.assertIn("离馆", message)

    def test_deceased_animal_blocks_offspring_registration(self):
        sire = self._animal('M-1', 'male')
        dam = self._animal('F-1', 'female')
        pairing = self._pairing(sire, dam, self._exam(sire['id'], 'passed'), self._exam(dam['id'], 'passed'))
        self.service.transition(self.committee, pairing["id"], 'approve', {})
        self.service.transition(self.vet, sire["id"], 'mark_deceased', {'cause': 'illness'})
        with self.assertRaises(InvalidTransition) as caught:
            self.service.transition(
                self.coordinator, pairing["id"], 'complete', {'offspring_ids': ['offspring-1']}
            )
        self.assertIn("死亡", str(caught.exception))

    def test_resubmit_not_allowed_after_completion(self):
        sire = self._animal('M-1', 'male')
        dam = self._animal('F-1', 'female')
        pairing = self._pairing(sire, dam, self._exam(sire['id'], 'passed'), self._exam(dam['id'], 'passed'))
        self.service.transition(self.committee, pairing["id"], 'approve', {})
        self.service.transition(self.coordinator, pairing["id"], 'complete', {'offspring_ids': ['o-1']})
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.coordinator, pairing["id"], 'resubmit', {})


if __name__ == "__main__":
    unittest.main()
