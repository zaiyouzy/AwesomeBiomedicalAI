from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
import unittest

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import curator  # noqa: E402
import discovery  # noqa: E402

API_KEY = "sk-test-key-1234567890"


def make_queue(count: int = 3, **overrides) -> dict:
    queue = []
    for index in range(1, count + 1):
        entry = {
            "candidate_id": f"cand-{index}",
            "doi": f"10.1038/s41467-026-7{index:04d}",
            "doi_url": f"https://doi.org/10.1038/s41467-026-7{index:04d}",
            "title": f"Deep learning for ultrasound imaging study {index}",
            "journal": "Nature Communications",
            "first_online": "202608",
            "date_status": "verified",
            "abstract": "A deep learning system for ultrasound imaging of liver fibrosis.",
            "paper_url": "https://www.nature.com/articles/s41467-026-70001",
        }
        entry.update(overrides)
        queue.append(entry)
    return {"run": {"date": "2026-09-14"}, "queue": queue}


def ai_response(candidate_id: str, decision: str = "include", evidence=None, usage=None) -> dict:
    content = json.dumps({
        "candidate_id": candidate_id,
        "decision": decision,
        "confidence": 0.82,
        "reason": "Central AI imaging contribution in an in-scope modality.",
        "scope_category": "ultrasound",
        "date_status": "verified",
        "evidence_urls": evidence if evidence is not None else [],
        "uncertainties": [],
    })
    return {
        "choices": [{"message": {"content": content}}],
        "usage": usage or {"prompt_tokens": 1200, "completion_tokens": 90, "total_tokens": 1290},
    }


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        parsed = json.loads(body.decode("utf-8"))
        self.calls.append({"url": url, "headers": headers, "body": parsed, "timeout": timeout})
        return self.handler(len(self.calls), self.calls[-1])


def fast_rules(**ai_overrides) -> dict:
    rules = curator.load_rules()
    ai = dict(rules.get("ai") or {})
    ai.update({"retry_seconds": 0, "timeout_seconds": 5})
    ai.update(ai_overrides)
    rules["ai"] = ai
    return rules


class PromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = curator.load_rules()

    def test_prompt_contains_json_contract_and_policy(self) -> None:
        messages, allowed = discovery.build_ai_messages(make_queue(1)["queue"][0], self.rules,
                                                        discovery.ai_config(self.rules))
        system = messages[0]["content"]
        self.assertIn("json", system)
        self.assertIn("candidate_id", system)
        self.assertIn("needs_human_review", system)
        self.assertIn("Nature", system)
        self.assertIn("https://doi.org/10.1038/s41467-026-70001", allowed)

    def test_prompt_never_contains_api_key(self) -> None:
        messages, _ = discovery.build_ai_messages(make_queue(1)["queue"][0], self.rules,
                                                  discovery.ai_config(self.rules))
        joined = json.dumps(messages)
        self.assertNotIn("sk-", joined)

    def test_candidate_input_is_truncated(self) -> None:
        rules = fast_rules(max_abstract_chars=50, max_input_chars_per_candidate=400)
        candidate = make_queue(1, abstract="x" * 5000)["queue"][0]
        messages, _ = discovery.build_ai_messages(candidate, rules, discovery.ai_config(rules))
        self.assertLessEqual(len(messages[1]["content"]), 400)
        self.assertIn("xxxx", messages[1]["content"])


class ParseReplyTests(unittest.TestCase):
    def test_valid_reply_is_normalised(self) -> None:
        text = json.dumps({
            "candidate_id": "cand-1", "decision": "include", "confidence": 1.5,
            "reason": "In scope.", "scope_category": "microscopy", "date_status": "verified",
            "evidence_urls": ["https://doi.org/10.1038/s41467-026-70001",
                              "https://evil.example/hallucinated"],
            "uncertainties": ["small cohort"],
        })
        parsed = discovery.parse_ai_reply(text, "cand-1", ["https://doi.org/10.1038/s41467-026-70001"])
        self.assertEqual("include", parsed["decision"])
        self.assertEqual(1.0, parsed["confidence"])
        self.assertEqual(["https://doi.org/10.1038/s41467-026-70001"], parsed["evidence_urls"])
        self.assertTrue(any("Dropped 1 evidence" in item for item in parsed["uncertainties"]))

    def test_markdown_fences_are_tolerated(self) -> None:
        inner = json.dumps({"candidate_id": "cand-1", "decision": "exclude", "reason": "Out of scope."})
        parsed = discovery.parse_ai_reply(f"```json\n{inner}\n```", "cand-1", [])
        self.assertEqual("exclude", parsed["decision"])

    def test_invalid_payloads_raise(self) -> None:
        cases = [
            ("", "cand-1"),
            ("not json at all", "cand-1"),
            (json.dumps(["a", "list"]), "cand-1"),
            (json.dumps({"candidate_id": "other", "decision": "include", "reason": "x"}), "cand-1"),
            (json.dumps({"candidate_id": "cand-1", "decision": "maybe", "reason": "x"}), "cand-1"),
            (json.dumps({"candidate_id": "cand-1", "decision": "include", "reason": ""}), "cand-1"),
        ]
        for text, candidate_id in cases:
            with self.assertRaises(curator.CuratorError):
                discovery.parse_ai_reply(text, candidate_id, [])


class RunAiReviewTests(unittest.TestCase):
    def run_review(self, queue: dict, transport=None, **kwargs) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = str(root / "weekly")
            payload = discovery.run_ai_review(
                repo_root=root, rules=kwargs.pop("rules", fast_rules()), queue=queue,
                prefix=prefix, transport=transport, run_date=None, **kwargs)
            for key in ("ai_json", "ai_md"):
                self.assertTrue(Path(payload["files"][key]).exists(), key)
            payload["_ai_json"] = json.loads(Path(payload["files"]["ai_json"]).read_text(encoding="utf-8"))
            payload["_ai_md"] = Path(payload["files"]["ai_md"]).read_text(encoding="utf-8")
            return payload

    def test_happy_path_reviews_every_candidate(self) -> None:
        transport = FakeTransport(lambda n, call: ai_response(f"cand-{n}", usage={
            "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}))
        payload = self.run_review(make_queue(3), transport=transport, api_key=API_KEY)
        self.assertEqual("ok", payload["status"])
        self.assertEqual(3, payload["reviewed"])
        self.assertEqual(3, payload["counts"]["include"])
        self.assertEqual(3, payload["usage"]["requests"])
        self.assertEqual(3300, payload["usage"]["total_tokens"])
        self.assertGreater(payload["estimated_cost_usd"], 0)
        self.assertEqual("deepseek-flash", payload["model"])
        # Request contract verified from the DeepSeek docs.
        body = transport.calls[0]["body"]
        self.assertEqual({"type": "json_object"}, body["response_format"])
        self.assertEqual({"type": "disabled"}, body["thinking"])
        self.assertEqual(700, body["max_tokens"])
        self.assertTrue(transport.calls[0]["url"].endswith("/chat/completions"))
        self.assertIn("AI first-pass review", payload["_ai_md"])
        self.assertIn("reviewed-papers.json", payload["_ai_md"])

    def test_api_key_is_never_written_into_reports(self) -> None:
        transport = FakeTransport(lambda n, call: ai_response(f"cand-{n}"))
        payload = self.run_review(make_queue(1), transport=transport, api_key=API_KEY)
        self.assertNotIn(API_KEY, json.dumps(payload["_ai_json"]))
        self.assertNotIn(API_KEY, payload["_ai_md"])

    def test_retry_recovers_from_invalid_json(self) -> None:
        def handler(call_number, call):
            if call_number == 1:
                return {"choices": [{"message": {"content": "sorry, no json"}}]}
            return ai_response("cand-1")

        transport = FakeTransport(handler)
        payload = self.run_review(make_queue(1), transport=transport, api_key=API_KEY)
        self.assertEqual(2, payload["usage"]["requests"])
        self.assertEqual("include", payload["results"][0]["decision"])
        self.assertNotIn("error", payload["results"][0])

    def test_retry_exhausted_records_manual_review(self) -> None:
        transport = FakeTransport(lambda n, call: {"choices": [{"message": {"content": ""}}]})
        payload = self.run_review(make_queue(1), transport=transport, api_key=API_KEY)
        entry = payload["results"][0]
        self.assertTrue(entry["error"])
        self.assertEqual("needs_human_review", entry["decision"])
        self.assertIn("empty response content", entry["reason"])
        self.assertEqual(2, payload["usage"]["requests"])

    def test_api_error_is_scrubbed_and_does_not_crash(self) -> None:
        def handler(call_number, call):
            raise curator.CuratorError(f"DeepSeek API HTTP 401: invalid key {API_KEY}")

        payload = self.run_review(make_queue(1), transport=FakeTransport(handler), api_key=API_KEY)
        entry = payload["results"][0]
        self.assertEqual("needs_human_review", entry["decision"])
        self.assertIn("sk-***", entry["reason"])
        self.assertNotIn(API_KEY, entry["reason"])

    def test_missing_key_skips_without_calls(self) -> None:
        transport = FakeTransport(lambda n, call: ai_response(f"cand-{n}"))
        payload = self.run_review(make_queue(2), transport=transport, api_key="")
        self.assertEqual("skipped_no_api_key", payload["status"])
        self.assertEqual([], transport.calls)
        self.assertEqual(0, payload["usage"]["requests"])
        self.assertEqual(2, payload["counts"]["not_reviewed"])

    def test_dry_run_makes_no_calls(self) -> None:
        transport = FakeTransport(lambda n, call: ai_response(f"cand-{n}"))
        payload = self.run_review(make_queue(2), transport=transport, api_key=API_KEY, dry_run=True)
        self.assertEqual("dry_run", payload["status"])
        self.assertEqual([], transport.calls)
        self.assertIn("prompt_preview", payload["results"][0])

    def test_candidate_cap_and_request_cap(self) -> None:
        transport = FakeTransport(lambda n, call: ai_response(f"cand-{n}"))
        capped = self.run_review(make_queue(5), transport=transport, api_key=API_KEY,
                                 rules=fast_rules(max_candidates_per_run=2))
        self.assertEqual(5, capped["candidates_available"])
        self.assertEqual(2, len(capped["results"]))

        transport2 = FakeTransport(lambda n, call: ai_response(f"cand-{n}"))
        limited = self.run_review(make_queue(3), transport=transport2, api_key=API_KEY,
                                  rules=fast_rules(max_requests_per_run=1))
        self.assertEqual("ok", limited["status"])
        self.assertEqual(1, limited["usage"]["requests"])
        self.assertEqual("include", limited["results"][0]["decision"])
        self.assertEqual("not_reviewed", limited["results"][1]["decision"])
        self.assertIn("request cap", limited["results"][1]["reason"])

    def test_disabled_config_skips(self) -> None:
        payload = self.run_review(make_queue(1), transport=FakeTransport(lambda n, c: {}),
                                  api_key=API_KEY, rules=fast_rules(enabled=False))
        self.assertEqual("disabled", payload["status"])
        self.assertEqual(0, payload["usage"]["requests"])


if __name__ == "__main__":
    unittest.main()
