"""The HTTP MCP endpoint every harness uses to reach Khidr's tools."""

import json

from django.test import TestCase

from core.models import Agent, ChatMessage, Project, Run, Task


class McpEndpointTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="p", slug="p")
        self.agent = Agent.objects.create(slug="a", type=Agent.TYPE_CLAUDE)
        self.task = Task.objects.create(project=self.project, title="t", current_agent=self.agent)
        self.run, self.token = Run.start(self.task, self.agent)

    def rpc(self, payload, token=None):
        return self.client.post(
            "/mcp/", data=json.dumps(payload), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token or self.token}",
        )

    def test_initialize(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "1"}}})
        self.assertEqual(r.status_code, 200)
        result = r.json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])

    def test_unknown_protocol_version_gets_the_latest(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(r.json()["result"]["protocolVersion"], "2025-06-18")

    def test_notification_is_accepted_without_body(self):
        r = self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(r.status_code, 202)

    def test_tools_list(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in r.json()["result"]["tools"]}
        self.assertTrue({"note", "set_status", "ask_user", "server_exec", "deploy"} <= names)
        tool = r.json()["result"]["tools"][0]
        self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_tools_call_runs_against_the_runs_task(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "note", "arguments": {"text": "hello"}}})
        self.assertFalse(r.json()["result"]["isError"])
        self.assertTrue(ChatMessage.objects.filter(task=self.task, content__contains="hello").exists())

    def test_tool_errors_are_flagged(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "no_such_tool", "arguments": {}}})
        self.assertTrue(r.json()["result"]["isError"])

    def test_batch(self):
        r = self.rpc([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ])
        self.assertEqual(r.json(), [{"jsonrpc": "2.0", "id": 1, "result": {}}])

    def test_unknown_method(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 5, "method": "sampling/createMessage"})
        self.assertEqual(r.json()["error"]["code"], -32601)

    def test_bad_token_is_rejected(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="nope")
        self.assertEqual(r.status_code, 401)

    def test_token_dies_with_the_run(self):
        self.run.end("finished")
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 401)

    def test_get_is_not_allowed(self):
        r = self.client.get("/mcp/", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(r.status_code, 405)

    def test_path_without_trailing_slash(self):
        r = self.client.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                             content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(r.status_code, 200)
