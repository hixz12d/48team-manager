import unittest

from app.domain.identity.policy import (
    normalize_local_purpose,
    normalize_official_plan,
    purpose_from_plan,
    role_from_email,
)


class IdentityInvariantTests(unittest.TestCase):
    def test_gmail_does_not_become_owner(self):
        self.assertIsNone(role_from_email("xiaozhudf2026.21@gmail.com"))
        self.assertIsNone(role_from_email("family.pedro@icloud.com"))

    def test_plan_is_not_purpose(self):
        self.assertEqual(normalize_official_plan("pro"), "pro")
        self.assertIsNone(purpose_from_plan("pro"))
        self.assertIsNone(purpose_from_plan("team"))
        self.assertEqual(normalize_local_purpose("mother"), "mother")
        self.assertEqual(normalize_local_purpose("child"), "child")
        with self.assertRaises(ValueError):
            normalize_local_purpose("gmail")
