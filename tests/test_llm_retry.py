import unittest

from app.services.llm_retry import strip_json_fence


class LlmRetryTests(unittest.TestCase):
    def test_strip_json_fence(self) -> None:
        raw = "```json\n{\"score_suggestion\": 1, \"comment_text\": \"ok\"}\n```"
        cleaned = strip_json_fence(raw)
        self.assertTrue(cleaned.startswith("{"))


if __name__ == "__main__":
    unittest.main()
