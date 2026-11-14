import unittest
from types import SimpleNamespace
from unittest import mock

from bdh_sdk import identity as ident

TOKEN = "app-token"
NOW = 1_800_000_000


class SigningTests(unittest.TestCase):
    def test_round_trip(self):
        who = {"u": "steve", "n": "Steve", "staff": True, "mode": "owner", "o": "http://hub"}
        h = ident.signed_headers(TOKEN, "GET", "/x/?a=1", who, now=NOW)
        self.assertEqual(ident.verify(TOKEN, "GET", "/x/?a=1", h, now=NOW + 30), who)

    def test_anonymous_is_empty_dict(self):
        h = ident.signed_headers(TOKEN, "GET", "/", now=NOW)
        self.assertNotIn(ident.HEADER_IDENTITY, h)
        self.assertEqual(ident.verify(TOKEN, "GET", "/", h, now=NOW), {})

    def test_rejects_tampering_and_skew(self):
        h = ident.signed_headers(TOKEN, "POST", "/a/", {"u": "guest"}, now=NOW)
        cases = {
            "other token": ("other", "POST", "/a/", h, NOW),
            "other method": (TOKEN, "GET", "/a/", h, NOW),
            "other path": (TOKEN, "POST", "/b/", h, NOW),
            "too old": (TOKEN, "POST", "/a/", h, NOW + ident.MAX_SKEW + 1),
            "future": (TOKEN, "POST", "/a/", h, NOW - ident.MAX_SKEW - 1),
            "no token": ("", "POST", "/a/", h, NOW),
            "staff added": (TOKEN, "POST", "/a/", {**h, ident.HEADER_IDENTITY:
                            ident.encode_identity({"u": "guest", "staff": True})}, NOW),
            "no time": (TOKEN, "POST", "/a/", {**h, ident.HEADER_TIME: "x"}, NOW),
        }
        for name, args in cases.items():
            with self.subTest(name):
                self.assertIsNone(ident.verify(*args[:4], now=args[4]))

    def test_body_is_signed(self):
        h = ident.signed_headers(TOKEN, "POST", "/_bdh/hook", body=b'{"a":1}', now=NOW)
        self.assertEqual(ident.verify(TOKEN, "POST", "/_bdh/hook", h, body=b'{"a":1}', now=NOW), {})
        self.assertIsNone(ident.verify(TOKEN, "POST", "/_bdh/hook", h, body=b'{"a":2}', now=NOW))
        self.assertIsNone(ident.verify(TOKEN, "POST", "/_bdh/hook", h, now=NOW))

    def test_app_token_is_per_app_and_generation(self):
        a = ident.app_token("secret", "cameras", 1)
        self.assertEqual(a, ident.app_token("secret", "cameras", 1))
        self.assertNotEqual(a, ident.app_token("secret", "cameras", 2))
        self.assertNotEqual(a, ident.app_token("secret", "sonos", 1))
        self.assertNotEqual(a, ident.app_token("other", "cameras", 1))

    def test_user_for(self):
        self.assertIs(ident.user_for({}), ident.ANONYMOUS)
        self.assertFalse(ident.ANONYMOUS.is_authenticated)
        user = ident.user_for({"u": "steve", "staff": True})
        self.assertTrue(user.is_authenticated and user.is_staff and user.has_perm("x"))
        self.assertEqual(user, ident.HubUser("steve"))
        self.assertFalse(ident.user_for({"u": "g"}).has_perm("x"))


def _request(path, headers, method="GET", body=b""):
    return SimpleNamespace(path=path.split("?")[0], method=method, headers=headers, body=body,
                           get_full_path=lambda: path)


@mock.patch.dict("os.environ", {"BDH_APP_TOKEN": TOKEN})
class MiddlewareTests(unittest.TestCase):
    def setUp(self):
        try:
            import django  # noqa: F401
        except ImportError:
            self.skipTest("django not installed")
        from django.conf import settings
        if not settings.configured:
            settings.configure()

    def test_sets_user_and_context(self):
        mw = ident.IdentityMiddleware(lambda r: r)
        who = {"u": "steve", "mode": "owner", "stay": "s1", "o": "http://hub:8000"}
        req = _request("/x/?q=1", ident.signed_headers(TOKEN, "GET", "/x/?q=1", who))
        out = mw(req)
        self.assertIs(out, req)
        self.assertEqual(str(req.user), "steve")
        self.assertEqual((req.bdh_mode, req.bdh_stay_key, req.bdh_hub_origin, req.bdh_internal),
                         ("owner", "s1", "http://hub:8000", False))

    def test_query_string_is_signed(self):
        mw = ident.IdentityMiddleware(lambda r: r)
        req = _request("/x/?q=2", ident.signed_headers(TOKEN, "GET", "/x/?q=1"))
        self.assertEqual(mw(req).status_code, 403)

    def test_internal_paths_need_signed_body(self):
        mw = ident.IdentityMiddleware(lambda r: r)
        unsigned = ident.signed_headers(TOKEN, "POST", "/_bdh/hook")
        self.assertEqual(mw(_request("/_bdh/hook", unsigned, "POST", b"{}")).status_code, 403)
        signed = ident.signed_headers(TOKEN, "POST", "/_bdh/hook", body=b"{}")
        req = _request("/_bdh/hook", signed, "POST", b"{}")
        self.assertIs(mw(req), req)
        self.assertTrue(req.bdh_internal)
        self.assertEqual(req.bdh_mode, "guest")


if __name__ == "__main__":
    unittest.main()
