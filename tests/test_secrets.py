import os
import unittest
from unittest import mock

from bdh_sdk import plugins, rpc, secrets
from bdh_sdk.testing import FakeBroker


class MemoryBackendTests(unittest.TestCase):
    def setUp(self):
        secrets.set_backend(None)
        self.addCleanup(secrets.set_backend, None)

    def test_round_trip(self):
        self.assertIsNone(secrets.get("token", app="demo"))
        secrets.set("token", "s3cret", app="demo")
        self.assertEqual(secrets.get("token", app="demo"), "s3cret")
        secrets.delete("token", app="demo")
        self.assertIsNone(secrets.get("token", app="demo"))
        secrets.delete("token", app="demo")  # deleting twice is fine

    def test_apps_are_separate(self):
        secrets.set("token", "a", app="app_a")
        self.assertIsNone(secrets.get("token", app="app_b"))

    def test_app_id_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"BDH_APP_ID": "demo"}):
            secrets.set("token", "x")
            self.assertEqual(secrets.get("token"), "x")
        self.assertEqual(secrets.get("token", app="demo"), "x")

    def test_app_id_is_required(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            secrets.get("token")

    def test_bad_names_and_values(self):
        for name in ("", "Upper", "a-b", "../x", "x" * 65, None):
            with self.assertRaises(ValueError):
                secrets.get(name, app="demo")
        with self.assertRaises(ValueError):
            secrets.set("token", "", app="demo")
        with self.assertRaises(TypeError):
            secrets.set("token", 123, app="demo")
        with self.assertRaises(ValueError):
            secrets.set("token", "x" * (secrets.MAX_LENGTH + 1), app="demo")

    def test_environment_fallback(self):
        env = {"BDH_DEMO_TOKEN": "from-env", "OLD_TOKEN": "legacy"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(secrets.get("token", app="demo"), "from-env")
            self.assertEqual(secrets.get("other", app="demo", legacy=("OLD_TOKEN",)), "legacy")
            secrets.set("token", "stored", app="demo")
            self.assertEqual(secrets.get("token", app="demo"), "stored")
        self.assertEqual(secrets.get("missing", "dflt", app="demo"), "dflt")

    def test_custom_backend(self):
        calls = []

        class Backend:
            def get(self, app_id, name):
                calls.append(("get", app_id, name))
                raise KeyError(name)

            def set(self, app_id, name, value):
                calls.append(("set", app_id, name, value))

            def delete(self, app_id, name):
                calls.append(("delete", app_id, name))

        secrets.set_backend(Backend())
        secrets.set("k", "v", app="demo")
        self.assertIsNone(secrets.get("k", app="demo"))
        secrets.delete("k", app="demo")
        self.assertEqual(calls, [("set", "demo", "k", "v"), ("get", "demo", "k"),
                                 ("delete", "demo", "k")])


class RpcSecretsTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker(install=True, app_id="cameras").start()
        self.addCleanup(self.broker.stop)

    def test_round_trip_over_the_broker(self):
        secrets.set("protect_password_1", "hunter2")
        self.assertEqual(self.broker.secrets, {("cameras", "protect_password_1"): "hunter2"})
        rpc.client()  # still installed
        backend = secrets._backend
        backend.clear()
        self.assertEqual(secrets.get("protect_password_1"), "hunter2")
        secrets.delete("protect_password_1")
        self.assertEqual(self.broker.secrets, {})
        self.assertIsNone(secrets.get("protect_password_1"))

    def test_app_is_never_sent(self):
        secrets.set("token", "x", app="other")
        secrets._backend.clear()
        secrets.get("token", app="other")
        secrets.delete("token", app="other")
        for op, payload in self.broker.calls:
            self.assertNotIn("app", payload, op)
        # The broker keys secrets by its own app, whatever the caller asked for.
        self.assertEqual(self.broker.secrets, {})
        secrets.set("token", "y", app="other")
        self.assertEqual(list(self.broker.secrets), [("cameras", "token")])

    def test_reads_are_cached_in_memory(self):
        self.broker.secrets[("cameras", "token")] = "abc"
        self.assertEqual(secrets.get("token"), "abc")
        self.assertEqual(secrets.get("token"), "abc")
        self.assertEqual(len(self.broker.ops("secrets.get")), 1)

    def test_ungranted_secrets_raise(self):
        self.broker.grants = ["settings.*"]
        with self.assertRaises(rpc.PermissionDenied):
            secrets.get("token")

    def test_uninstall_restores_memory(self):
        rpc.uninstall()
        self.assertIsInstance(secrets._backend, secrets.MemorySecretBackend)


class SecretSettingTests(unittest.TestCase):
    def test_secret_setting_keeps_text_as_typed(self):
        s = plugins.Setting("api_key", "API key", type=plugins.SECRET)
        self.assertEqual(s.parse("  spaced  "), "  spaced  ")

    def test_secret_setting_has_no_default(self):
        with self.assertRaises(ValueError):
            plugins.Setting("api_key", "API key", type=plugins.SECRET, default="x")


if __name__ == "__main__":
    unittest.main()
