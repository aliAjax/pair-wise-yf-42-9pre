import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.registrar = Actor("registrar-1", "registrar")
        self.vet_a = Actor("vet-a", "veterinarian")
        self.vet_b = Actor("vet-b", "veterinarian")
        self.coordinator = Actor("coordinator-1", "coordinator")
        self.committee = Actor("committee-1", "committee")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_pair_with_exams(self):
        sire = self.service.create(self.registrar, 'animal', {'name': 'M-1', 'sex': 'male'})
        dam = self.service.create(self.registrar, 'animal', {'name': 'F-1', 'sex': 'female'})
        sire_exam = self.service.create(
            self.vet_a, 'health_exam',
            {'animal_id': sire['id'], 'result': 'passed', 'exam_date': '2026-09-01'},
        )
        dam_exam = self.service.create(
            self.vet_b, 'health_exam',
            {'animal_id': dam['id'], 'result': 'passed', 'exam_date': '2026-09-02'},
        )
        pairing = self.service.create(
            self.coordinator, 'pairing',
            {
                'proposed_by': 'coordinator-1',
                'sire_id': sire['id'],
                'dam_id': dam['id'],
                'sire_exam_id': sire_exam['id'],
                'dam_exam_id': dam_exam['id'],
            },
        )
        return sire, dam, sire_exam, dam_exam, pairing

    def test_full_workflow(self):
        sire, dam, sire_exam, dam_exam, pairing = self._create_pair_with_exams()
        self.assertEqual(pairing["status"], "proposed")
        self.assertEqual(sire_exam["created_by"], "vet-a")
        self.assertEqual(dam_exam["created_by"], "vet-b")

        approved = self.service.transition(self.committee, pairing["id"], 'approve', {})
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["data"]["approved_by"], "committee-1")
        self.assertEqual(approved["data"]["kinship_coefficient"], 0.0)

        completed = self.service.transition(
            self.coordinator, pairing["id"], 'complete', {'offspring_ids': ['offspring-1']}
        )
        self.assertEqual(completed["status"], "completed")

        transfer = self.service.create(
            self.registrar, 'transfer',
            {'animal_id': sire['id'], 'from_institution': 'Zoo-A', 'to_institution': 'Zoo-B'},
        )
        steps = [
            ('authorize', {'permit_id': 'P-1'}, 'authorized'),
            ('ship', {'transport_id': 'T-1'}, 'in_transit'),
            ('arrive', {'arrival_date': '2026-05-01'}, 'completed'),
        ]
        for action, data, expect in steps:
            entity = self.service.transition(self.registrar, transfer["id"], action, data)
            self.assertEqual(entity["status"], expect)

    def test_quarantine_blocks_offspring_then_recover_and_resubmit(self):
        sire, dam, sire_exam, dam_exam, pairing = self._create_pair_with_exams()
        self.service.transition(self.committee, pairing["id"], 'approve', {})

        # Sire falls ill and is quarantined: offspring registration is blocked.
        self.service.transition(self.vet_a, sire["id"], 'quarantine_animal', {'reason': 'flu'})
        with self.assertRaises(InvalidTransition) as caught:
            self.service.transition(
                self.coordinator, pairing["id"], 'complete', {'offspring_ids': ['offspring-1']}
            )
        message = str(caught.exception)
        self.assertIn(sire["id"], message)
        self.assertIn("隔离", message)

        # The pairing view names the blocking animal and its current status.
        view = self.service.get(pairing["id"])
        self.assertFalse(view["can_register_offspring"])
        self.assertEqual(view["blockers"][0]["animal_id"], sire["id"])
        self.assertEqual(view["blockers"][0]["status"], "quarantined")
        self.assertEqual(view["blockers"][0]["status_label"], "隔离")

        # The animal recovers; the same proposal goes back for review.
        self.service.transition(self.vet_a, sire["id"], 'release_quarantine', {})
        resubmitted = self.service.transition(self.coordinator, pairing["id"], 'resubmit', {})
        self.assertEqual(resubmitted["status"], "proposed")

        reapproved = self.service.transition(self.committee, pairing["id"], 'approve', {})
        self.assertEqual(reapproved["status"], "approved")
        completed = self.service.transition(
            self.coordinator, pairing["id"], 'complete', {'offspring_ids': ['offspring-1']}
        )
        self.assertEqual(completed["status"], "completed")

    def test_rejected_proposal_can_be_resubmitted(self):
        sire, dam, sire_exam, dam_exam, pairing = self._create_pair_with_exams()
        rejected = self.service.transition(
            self.committee, pairing["id"], 'reject', {'reason': 'dam exam stale'}
        )
        self.assertEqual(rejected["status"], "rejected")
        resubmitted = self.service.transition(self.coordinator, pairing["id"], 'resubmit', {})
        self.assertEqual(resubmitted["status"], "proposed")
        approved = self.service.transition(self.committee, pairing["id"], 'approve', {})
        self.assertEqual(approved["status"], "approved")


if __name__ == "__main__":
    unittest.main()
