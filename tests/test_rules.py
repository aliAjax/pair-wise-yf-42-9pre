import unittest

from src.rules import inbreeding_coefficient
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertEqual(inbreeding_coefficient({"id": "a"}, {"id": "a"}), 0.5)
        self.assertEqual(inbreeding_coefficient({"id": "a", "sire_id": "b"}, {"id": "b"}), 0.25)
        self.assertEqual(inbreeding_coefficient({"id": "a", "sire_id": "x"}, {"id": "b", "sire_id": "y"}), 0.0)
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.admin, "animals", {"name": "A", "sex": "other"})

    def test_health_exam_requires_existing_animal_and_valid_result(self):
        lookup = lambda kind, field, value: []
        with self.assertRaises(ValidationError):
            self.rules.validate_create(
                Actor("vet-1", "veterinarian"), "health_exam",
                {"animal_id": "ghost", "result": "passed"}, lookup,
            )
        animal = {"id": "a1", "status": "active", "data": {"name": "A"}}
        lookup = lambda kind, field, value: [animal] if kind == "animal" else []
        with self.assertRaises(ValidationError):
            self.rules.validate_create(
                Actor("vet-1", "veterinarian"), "health_exam",
                {"animal_id": "a1", "result": "unknown"}, lookup,
            )
        data = self.rules.validate_create(
            Actor("vet-1", "veterinarian"), "health_exam",
            {"animal_id": "a1", "result": "passed"}, lookup,
        )
        self.assertTrue(data["exam_date"])

    def test_health_exam_create_role_is_veterinarian(self):
        with self.assertRaises(PermissionDenied):
            self.rules.validate_create(
                Actor("coord-1", "coordinator"), "health_exam",
                {"animal_id": "a1", "result": "passed"},
            )


if __name__ == "__main__":
    unittest.main()
