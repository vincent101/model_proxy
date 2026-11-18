"""Wine fake-config handler smoke: bad JSON and pre-auth cooldown behavior."""
import io
import json
import sys
import unittest
from pathlib import Path

if __name__ == "__main__":
    if len(sys.argv) != 2 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("requires -I -S -B and a fresh result path")
    result_path = Path(sys.argv[1])
    if result_path.exists():
        raise SystemExit("result exists")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_wine_proxy_integration as fixture

    class BadReload(fixture.WineProxyIntegration):
        __unittest_skip__ = False

        def test_bad_json_keeps_old_config_and_wrong_token_does_not_clear(self):
            store, cooldown = self.proxy.config_store, self.proxy.cooldown_store
            prior = store.get_supply_map()["fake"]["target_model"]
            cooldown.cooldown("fake", 60, "fixture")
            self.config.write_text("{broken", encoding="utf-8")
            status, _, _ = self.request("/model_proxy/reload", {},
                                        {"X-Proxy-Admin-Token": "wrong"})
            self.assertEqual(status, 401)
            self.assertTrue(cooldown.is_cooling("fake"))
            status, _, payload = self.request("/model_proxy/reload", {},
                                              {"X-Proxy-Admin-Token": fixture.ADMIN})
            result = json.loads(payload)
            self.assertNotEqual(status, 200)
            self.assertIs(result["loaded"], False)
            self.assertIs(result["cooldown_cleared"], True)
            self.assertEqual(store.get_supply_map()["fake"]["target_model"], prior)
            self.assertFalse(cooldown.is_cooling("fake"))

    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.TestSuite([BadReload("test_bad_json_keeps_old_config_and_wrong_token_does_not_clear")]))
    result_path.write_text(json.dumps({"tests": result.testsRun, "errors": len(result.errors),
                                      "failures": len(result.failures), "output": output.getvalue()}),
                           encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
