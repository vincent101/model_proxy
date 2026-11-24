"""install 候选筛选必须认得 provider supply 的线协议。

回归（2026-11-24）：`candidate_tokens` 直接拿 supply["protocol"] 去问 reasoning
注册表，而注册表只认 anthropic/chat/responses；catpaw-cli / catpaw-ide 会被判
「非法 protocol」，于是**只有 catpaw 供给的配置筛不出任何候选**，`install` 对
Claude 直接跳过并提示「无协议匹配」。provider 的线协议恒为 anthropic，筛选必须
按它判断。
"""
import unittest

from core.cli._install_ops import candidate_tokens


def _cfg(supplies, tiers=None):
    return {
        "supplies": supplies,
        "routes": [{"id": "r", "tiers": tiers or {"sonnet": [supplies[0]["id"]]}}],
        "strategies": [{"client_token": "tok", "route_id": "r"}],
    }


class CandidateProtocolTests(unittest.TestCase):
    def test_catpaw_cli_counts_as_anthropic(self):
        cfg = _cfg([{"id": "p", "protocol": "catpaw-cli", "target_model": "m"}])
        self.assertEqual([c["client_token"] for c in candidate_tokens(cfg, "anthropic")], ["tok"])
        self.assertEqual(candidate_tokens(cfg, "responses"), [])

    def test_catpaw_ide_counts_as_anthropic(self):
        cfg = _cfg([{"id": "p", "protocol": "catpaw-ide", "target_model": "m"}])
        self.assertEqual([c["client_token"] for c in candidate_tokens(cfg, "anthropic")], ["tok"])

    def test_legacy_responses_still_answered_by_url_inference(self):
        cfg = _cfg([{"id": "r1", "url": "https://x.example.com/v1/responses", "target_model": "m"}])
        self.assertEqual([c["client_token"] for c in candidate_tokens(cfg, "responses")], ["tok"])

    def test_unknown_protocol_still_reported_as_no_candidate(self):
        cfg = _cfg([{"id": "bad", "protocol": "nonsense", "target_model": "m"}])
        self.assertEqual(candidate_tokens(cfg, "anthropic"), [])


if __name__ == "__main__":
    unittest.main()
