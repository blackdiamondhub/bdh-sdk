import base64
import hashlib
import os
import unittest
from unittest import mock

from bdh_sdk import rpc, shares
from bdh_sdk.shares import Opened, SharesError
from bdh_sdk.testing import FakeBroker

ICS = b"BEGIN:VCALENDAR\r\nIMAGE;VALUE=URI:bdh-share:p1\r\nEND:VCALENDAR\r\n"
JPEG = b"\xff\xd8\xff\xe0 fake jpeg"
PNG = b"\x89PNG\r\n\x1a\n fake png"
EVENTS = [{"name": "events", "type": "text/calendar", "title": "Sun Peaks events"}]


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SharesTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker(app_id="sp", install=True).start()
        self.addCleanup(self.broker.stop)
        self.broker.set_manifest_shares("sp", EVENTS)

    def as_app(self, app_id):
        self.broker.app_id = app_id

    def test_publish_open_round_trip(self):
        rev = shares.publish("events", ICS, attachments={"p1": (JPEG, "image/jpeg"),
                                                         "p2": (PNG, "image/png")})
        self.assertEqual(rev, 1)
        self.broker.grant_share("calendar", "sp", "events")
        self.as_app("calendar")
        opened = shares.open_share("sp", "events")
        self.assertEqual((opened.revision, opened.type, opened.title, opened.data),
                         (1, "text/calendar", "Sun Peaks events", ICS))
        self.assertEqual(opened.attachments, {"p1": f"/apps/_share/{sha(JPEG)}",
                                              "p2": f"/apps/_share/{sha(PNG)}"})
        # The grant came after the first publish, so only "sp" heard of it.
        self.assertEqual([e[0] for e in self.broker.share_events], ["sp"])
        self.as_app("sp")
        self.assertEqual(shares.publish("events", ICS + b"\r\n"), 2)
        self.assertEqual(self.broker.share_events[-1],
                         ("calendar", "_shares/sp/events",
                          {"provider": "sp", "name": "events", "revision": 2}))

    def test_events_go_to_provider_and_granted_consumers(self):
        self.broker.grant_share("calendar", "sp", "events")
        shares.publish("events", ICS)
        payload = {"provider": "sp", "name": "events", "revision": 1}
        self.assertEqual(self.broker.share_events,
                         [("sp", "_shares/sp/events", payload),
                          ("calendar", "_shares/sp/events", payload)])

    def test_if_revision_not_modified(self):
        shares.publish("events", ICS)
        self.assertIsNone(shares.open_share("sp", "events", if_revision=1))
        self.assertEqual(shares.open_share("sp", "events", if_revision=0).revision, 1)

    def test_forbidden_until_granted_and_after_revoke(self):
        shares.publish("events", ICS)
        self.as_app("calendar")
        with self.assertRaises(SharesError) as cm:
            shares.open_share("sp", "events")
        self.assertEqual(cm.exception.code, "forbidden")
        self.broker.grant_share("calendar", "sp", "events")
        self.assertEqual(shares.open_share("sp", "events").data, ICS)
        self.broker.revoke_share("calendar", "sp", "events")
        with self.assertRaises(SharesError) as cm:
            shares.open_share("sp", "events")
        self.assertEqual(cm.exception.code, "forbidden")

    def test_unknown_share_not_found(self):
        with self.assertRaises(SharesError) as cm:
            shares.open_share("sp", "nope")
        self.assertEqual(cm.exception.code, "not_found")

    def test_expected_revision_conflict(self):
        shares.publish("events", ICS)
        self.assertEqual(shares.publish("events", ICS, expected_revision=1), 2)
        with self.assertRaises(SharesError) as cm:
            shares.publish("events", ICS, expected_revision=1)
        self.assertEqual(cm.exception.code, "invalid")
        self.assertEqual(shares.open_share("sp", "events").revision, 2)

    def test_keeps_two_revisions(self):
        for _ in range(4):
            shares.publish("events", ICS)
        self.assertEqual([r["revision"] for r in self.broker.share_revisions[("sp", "events")]],
                         [3, 4])

    def test_bad_attachment_key(self):
        for key in ("-x", "a b", "x" * 65, ""):
            with self.subTest(key=key), self.assertRaises(SharesError) as cm:
                shares.publish("events", ICS, attachments={key: (JPEG, "image/jpeg")})
            self.assertEqual(cm.exception.code, "invalid")
        shares.publish("events", ICS, attachments={"a" + "x" * 63: (JPEG, "image/jpeg")})

    def test_attachment_must_be_image(self):
        with self.assertRaises(SharesError) as cm:
            shares.publish("events", ICS, attachments={"p": (ICS, "text/calendar")})
        self.assertEqual(cm.exception.code, "invalid")

    def test_undeclared_share_rejected(self):
        with self.assertRaises(SharesError) as cm:
            shares.publish("other", ICS)
        self.assertEqual(cm.exception.code, "invalid")
        self.assertEqual(self.broker.ops("shares.publish"), [])
        # The hub refuses it too, whatever the SDK thinks.
        with self.assertRaises(SharesError) as cm:
            shares._call("shares.publish", {"name": "other", "main": sha(ICS), "attachments": {}})
        self.assertEqual(cm.exception.code, "invalid")

    def test_wrong_main_type_rejected(self):
        shares._call("shares.put_blob", {"mime": "image/png", "data_b64": base64.b64encode(PNG).decode()})
        with self.assertRaises(SharesError) as cm:
            shares._call("shares.publish", {"name": "events", "main": sha(PNG), "attachments": {}})
        self.assertEqual(cm.exception.code, "invalid")

    def test_put_blob_rejects_unknown_mime(self):
        with self.assertRaises(SharesError) as cm:
            shares._call("shares.put_blob", {"mime": "application/pdf", "data_b64": ""})
        self.assertEqual(cm.exception.code, "invalid")

    def test_missing_skips_reupload(self):
        shares.publish("events", ICS, attachments={"p1": (JPEG, "image/jpeg")})
        self.assertEqual(len(self.broker.ops("shares.put_blob")), 2)
        shares.publish("events", ICS + b"\r\n", attachments={"p1": (JPEG, "image/jpeg"),
                                                             "again": (JPEG, "image/jpeg")})
        puts = self.broker.ops("shares.put_blob")
        self.assertEqual(len(puts), 3)
        self.assertEqual(base64.b64decode(puts[-1]["data_b64"]), ICS + b"\r\n")
        self.assertEqual(self.broker.ops("shares.publish")[-1]["attachments"],
                         {"p1": sha(JPEG), "again": sha(JPEG)})

    def test_list_shares(self):
        self.broker.set_manifest_shares("other", [{"name": "x", "type": "text/calendar", "title": "X"}])
        self.as_app("calendar")
        self.assertEqual(shares.list_shares(), [])
        self.broker.grant_share("calendar", "sp", "events")
        listed = shares.list_shares()
        self.assertEqual([(s["provider"], s["name"], s["type"], s["title"], s["revision"])
                          for s in listed],
                         [("sp", "events", "text/calendar", "Sun Peaks events", 0)])
        self.as_app("sp")
        shares.publish("events", ICS)
        self.assertEqual([(s["name"], s["revision"]) for s in shares.list_shares()], [("events", 1)])

    def test_resolve(self):
        opened = Opened(revision=1, type="text/calendar", title="t", data=b"",
                        attachments={"p1": "/apps/_share/abc"})
        self.assertEqual(shares.resolve("bdh-share:p1", opened, "https://hub.example:8443/"),
                         "https://hub.example:8443/apps/_share/abc")
        self.assertIsNone(shares.resolve("bdh-share:nope", opened, "https://hub"))
        self.assertIsNone(shares.resolve("https://elsewhere/x.jpg", opened, "https://hub"))
        self.assertIsNone(shares.resolve(None, opened, "https://hub"))


class UnavailableTests(unittest.TestCase):
    def test_no_broker(self):
        rpc.uninstall()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(rpc.BROKER_ENV, None)
            for call in (lambda: shares.list_shares(),
                         lambda: shares.open_share("sp", "events"),
                         lambda: shares.publish("events", ICS)):
                with self.assertRaises(SharesError) as cm:
                    call()
                self.assertEqual(cm.exception.code, "unavailable")


if __name__ == "__main__":
    unittest.main()
