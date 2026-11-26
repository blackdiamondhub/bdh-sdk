import re
import unittest

from bdh_sdk import ALL_MODES, DASHBOARD, GUEST, OWNER, OWNER_ONLY, PluginSpec, Stay, grants, hooks, rpc, tools
from bdh_sdk.testing import FakeBroker
from bdh_sdk.tools import InvalidArgs, Tool, ToolError, check_params, tool_name, validate_args

HERE = __name__

TEMP = {"type": "object", "required": ["celsius"], "properties": {
    "celsius": {"type": "integer", "minimum": 30, "maximum": 40},
    "zone": {"type": "string", "enum": ["tub", "pool"]},
    "boost": {"type": "boolean"},
    "note": {"type": "string", "maxLength": 5},
    "ratio": {"type": "number"},
}}

seen = []


def demo_tools(mode=None):
    seen.append(("tools", mode))
    return [Tool("look", "Look", "Look around.", readonly=True, modes=ALL_MODES),
            Tool("look", "Dup", "Duplicate id."),
            Tool("heat", "Heat", "Heat the tub.", params=TEMP)]


def demo_run(tool_id, args, mode=None):
    seen.append((tool_id, args, mode))
    if tool_id == "fail":
        raise ToolError("tub is offline")
    if tool_id == "object":
        return object()
    return {"ok": tool_id, "args": args}


def spec(**kw):
    return PluginSpec(id="demo", app="demo", **kw)


class ToolTests(unittest.TestCase):
    def test_defaults_are_owner_only_and_normalised(self):
        t = Tool("look", "Look", "Look around.")
        self.assertEqual(t.modes, OWNER_ONLY)
        self.assertEqual(t.params, {"type": "object", "properties": {}, "required": [],
                                    "additionalProperties": False})
        self.assertFalse(t.confirm or t.readonly)
        self.assertEqual(Tool("x", "X", "x", modes=[GUEST]).modes, frozenset({GUEST}))

    def test_rejects_bad_fields(self):
        for kw in ({"id": "Bad"}, {"id": "a__b"}, {"id": "_a"}, {"id": "a" * 41}, {"id": ""},
                   {"title": ""}, {"title": "x" * 61}, {"description": " "},
                   {"description": "x" * 301}, {"modes": frozenset()}, {"modes": {"root"}},
                   {"modes": "guest"}, {"params": []}):
            args = {"id": "ok", "title": "T", "description": "D", **kw}
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                Tool(**args)

    def test_params_must_be_a_flat_simple_schema(self):
        bad = [
            {"type": "array"},
            {"properties": {"x": {"type": "object"}}},
            {"properties": {"x": {"$ref": "#/defs/x"}}},
            {"properties": {"x": {"type": "string", "items": {}}}},
            {"properties": {"x": {"type": "string"}}, "required": ["y"]},
            {"properties": {"x": {"type": "string"}}, "required": ["x", "x"]},
            {"properties": {"x": {"type": "string", "minimum": 1}}},
            {"properties": {"x": {"type": "integer", "maxLength": 1}}},
            {"properties": {"x": {"type": "integer", "enum": ["a"]}}},
            {"properties": {"x": {"type": "string", "enum": []}}},
            {"properties": {"bad-name": {"type": "string"}}},
            {"properties": {"x": {"type": "string"}}, "additionalProperties": True},
            {"$defs": {}},
            {"properties": {f"p{i}": {"type": "string"} for i in range(21)}},
        ]
        for params in bad:
            with self.subTest(params=params), self.assertRaises(ValueError):
                check_params(params)
        self.assertEqual(check_params(TEMP)["required"], ["celsius"])


class ValidateArgsTests(unittest.TestCase):
    tool = Tool("heat", "Heat", "Heat the tub.", params=TEMP)

    def test_cleans_good_arguments(self):
        self.assertEqual(validate_args(self.tool, {"celsius": 38.0, "zone": "tub", "boost": True,
                                                   "ratio": 1, "note": None}),
                         {"celsius": 38, "zone": "tub", "boost": True, "ratio": 1})
        self.assertIsInstance(validate_args(self.tool, {"celsius": 38.0})["celsius"], int)
        self.assertEqual(validate_args(TEMP, {"celsius": 30}), {"celsius": 30})
        self.assertEqual(validate_args(Tool("x", "X", "x"), None), {})

    def test_refuses_bad_arguments(self):
        bad = [
            ({}, "missing required"),
            ({"celsius": None}, "missing required"),
            ({"celsius": "38"}, "must be an integer"),
            ({"celsius": True}, "must be an integer"),
            ({"celsius": 38.5}, "must be an integer"),
            ({"celsius": 41}, "at most 40"),
            ({"celsius": 29}, "at least 30"),
            ({"celsius": 38, "zone": "sauna"}, "one of tub, pool"),
            ({"celsius": 38, "boost": "yes"}, "must be a boolean"),
            ({"celsius": 38, "ratio": False}, "must be a number"),
            ({"celsius": 38, "note": "toolong"}, "at most 5"),
            ({"celsius": 38, "rm": "-rf"}, "unknown argument 'rm'"),
            ([38], "must be an object"),
        ]
        for args, why in bad:
            with self.subTest(args=args):
                with self.assertRaisesRegex(InvalidArgs, why):
                    validate_args(self.tool, args)


class ToolNameTests(unittest.TestCase):
    def test_plain_names_are_kept(self):
        self.assertEqual(tool_name("hottub", "heat"), "hottub__heat")
        self.assertEqual(tool_name("my-app", "shortcut", "Movie_1"), "my-app__shortcut__Movie_1")

    def test_unsafe_or_long_names_are_mapped_and_unique(self):
        names = {tool_name("a.b", "x"), tool_name("a b", "x"), tool_name("a_b", "x"),
                 tool_name("x" * 60, "y"), tool_name("x" * 60, "z")}
        self.assertEqual(len(names), 5)
        for name in names:
            self.assertRegex(name, r"^[a-zA-Z0-9_-]{1,64}$")
        self.assertEqual(tool_name("a.b", "x"), tool_name("a.b", "x"))


class SpecAndHookTests(unittest.TestCase):
    def setUp(self):
        seen.clear()

    def test_tools_and_run_tool_come_together(self):
        with self.assertRaises(ValueError):
            spec(tools=f"{HERE}.demo_tools")
        with self.assertRaises(ValueError):
            spec(run_tool=f"{HERE}.demo_run")
        spec(tools=f"{HERE}.demo_tools", run_tool=f"{HERE}.demo_run")

    def test_assistant_opt_out(self):
        self.assertTrue(spec().assistant)
        self.assertTrue(spec().manifest()["assistant"])
        off = spec(assistant=False)
        self.assertIs(off.manifest()["assistant"], False)
        with self.assertRaisesRegex(ValueError, "opts out"):
            spec(assistant=False, tools=f"{HERE}.demo_tools", run_tool=f"{HERE}.demo_run")
        with self.assertRaises(ValueError):
            spec(assistant=0)

    def test_tools_permission(self):
        from bdh_sdk.plugins import valid_permission
        self.assertTrue(valid_permission("tools"))
        self.assertEqual(grants.TOOLS, "tools")

    def test_hooks(self):
        s = spec(tools=f"{HERE}.demo_tools", run_tool=f"{HERE}.demo_run")
        out = hooks.run(s, "tools", mode="guest")
        self.assertEqual([t["id"] for t in out], ["look", "heat"])
        self.assertEqual(out[0]["modes"], sorted(ALL_MODES))
        self.assertEqual(out[1]["params"]["required"], ["celsius"])
        self.assertEqual(hooks.run(s, "run_tool", tool_id="heat", args={"celsius": 38}, mode="owner"),
                         {"ok": "heat", "args": {"celsius": 38}})
        self.assertIn(("tools", "guest"), seen)
        self.assertIn(("heat", {"celsius": 38}, "owner"), seen)
        with self.assertRaises(ToolError):
            hooks.run(s, "run_tool", tool_id="fail")
        with self.assertRaises(TypeError):
            hooks.run(s, "run_tool", tool_id="object")
        with self.assertRaises(hooks.HookNotFound):
            hooks.run(spec(), "run_tool", tool_id="heat")

    def test_hub_topics_are_never_publishable(self):
        self.assertFalse(grants.can_publish(["events.publish:#"], "voice", "assistant/s1/call"))
        self.assertFalse(grants.can_publish([], "assistant", "assistant/s1/call"))
        self.assertTrue(grants.can_publish([], "voice", "voice/x"))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker(install=True, permissions=["tools"], app_id="voice").start()
        self.addCleanup(self.broker.stop)
        self.calls = []
        self.broker.add_tool(Tool("look", "Look", "Look.", readonly=True, modes=ALL_MODES),
                             lambda a: "all quiet", name="cams__look")
        self.broker.add_tool(Tool("heat", "Heat", "Heat.", params=TEMP, modes={GUEST, DASHBOARD}),
                             self._heat, name="tub__heat")
        self.broker.add_tool(Tool("unlock", "Unlock", "Unlock the door."), lambda a: "open")

    def _heat(self, args):
        self.calls.append(args)
        if args["celsius"] == 40:
            raise ToolError("too hot for guests")
        if args["celsius"] == 39:
            raise RuntimeError("boom")
        return {"target": args["celsius"]}

    def names(self, token):
        return [t["name"] for t in tools.list_tools(token)]

    def test_session_follows_stay_and_never_owner(self):
        token = tools.open_session("voice", display="10.0.0.5")
        self.assertEqual(self.names(token), ["cams__look", "tub__heat"])
        self.assertEqual(self.broker.ops("tools.open")[0], {"kind": "voice", "display": "10.0.0.5"})
        self.broker.stay = Stay("Smiths")
        self.assertEqual(self.broker.session_mode(), GUEST)
        self.assertNotIn("unlock", self.names(token))
        self.assertEqual(tools.call_tool(token, "unlock"), {"ok": False, "error": "no tool 'unlock' here"})
        with self.assertRaises(rpc.BrokerError):
            tools.open_session(OWNER)

    def test_call_validates_and_reports(self):
        token = tools.open_session()
        self.assertEqual(tools.call_tool(token, "tub__heat", {"celsius": 38}),
                         {"ok": True, "result": {"target": 38}})
        bad = tools.call_tool(token, "tub__heat", {"celsius": 50})
        self.assertFalse(bad["ok"])
        self.assertIn("at most 40", bad["error"])
        self.assertEqual(tools.call_tool(token, "tub__heat", {"celsius": 40}),
                         {"ok": False, "error": "too hot for guests"})
        self.assertEqual(tools.call_tool(token, "tub__heat", {"celsius": 39}),
                         {"ok": False, "error": "the tool failed"})
        self.assertEqual(self.calls, [{"celsius": 38}, {"celsius": 40}, {"celsius": 39}])
        self.assertEqual([c["ok"] for c in self.broker.tool_calls], [True, False, False, False])
        self.assertEqual(self.broker.tool_calls[0]["mode"], DASHBOARD)

    def test_dashboard_control_off_leaves_readonly_tools(self):
        self.broker.assistant_dashboard_control = False
        token = tools.open_session()
        self.assertEqual(self.names(token), ["cams__look"])
        self.broker.stay = Stay("Smiths")
        self.assertEqual(self.names(token), ["cams__look", "tub__heat"])

    def test_bad_tokens_and_missing_grant(self):
        with self.assertRaises(rpc.BrokerError) as ctx:
            tools.list_tools("forged")
        self.assertEqual(ctx.exception.code, tools.BAD_SESSION)
        token = tools.open_session()
        self.broker.app_id = "other"
        with self.assertRaises(rpc.BrokerError) as ctx:
            tools.call_tool(token, "cams__look")
        self.assertEqual(ctx.exception.code, tools.BAD_SESSION)
        self.broker.permissions = ["stays.read"]
        with self.assertRaises(rpc.PermissionDenied):
            tools.open_session()

    def test_list_is_a_retry_safe_read(self):
        self.assertIn("tools.list", rpc._READ_OPS)
        self.assertNotIn("tools.call", rpc._READ_OPS)
        self.assertTrue(re.fullmatch(r"[a-z_]+", tools.BAD_SESSION))


if __name__ == "__main__":
    unittest.main()
