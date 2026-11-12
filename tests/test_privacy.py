import unittest

from aik_sessions.models import Message, Session
from aik_sessions.privacy import redact_session, redact_text


class PrivacyTests(unittest.TestCase):
    def test_common_secrets_are_masked(self):
        text = "Key sk-proj-" + "a" * 40 + " Authorization: Bearer " + "b" * 40
        text += " password='synthetic-password' postgres://user:secret@host/db"
        masked = redact_text(text)
        self.assertNotIn("a" * 40, masked)
        self.assertNotIn("b" * 40, masked)
        self.assertNotIn("synthetic-password", masked)
        self.assertNotIn("user:secret", masked)
        self.assertIn("[REDACTED]", masked)

    def test_private_keys_are_masked(self):
        self.assertEqual(redact_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"),
                         "[REDACTED]")

    def test_regular_text_and_source_model_remain_intact(self):
        original = Session(id="codex:test", source="codex", title="Test", path="/fixture.jsonl",
                           messages=[Message(id="1", role="user", text="password=test")])
        cleaned = redact_session(original)
        self.assertEqual(original.messages[0].text, "password=test")
        self.assertEqual(cleaned.messages[0].text, "[REDACTED]")
        self.assertEqual(redact_text("Deploy the webhook handler at 12:00."),
                         "Deploy the webhook handler at 12:00.")


if __name__ == "__main__":
    unittest.main()
