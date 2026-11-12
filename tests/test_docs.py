"""Doc-to-code sync: the published tool reference must match the registered tools."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from aik_sessions.index import SessionIndex
from aik_sessions.models import Message, Session
from aik_sessions.server import create_server
from aik_sessions.service import Settings

ROOT = Path(__file__).resolve().parents[1]


class ToolReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(data_dir=root / "state", home=root / "home")
        server, _ = create_server(self.settings)
        self.tools = [tool.model_dump(by_alias=True) for tool in await server.list_tools()]
        self.reference = (ROOT / "docs/tools.md").read_text()

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def section(self, name):
        match = re.search(rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)", self.reference,
                          re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, f"docs/tools.md has no section for {name}")
        return match.group(1)

    async def test_every_tool_and_parameter_is_documented_and_nothing_extra(self):
        registered = {tool["name"] for tool in self.tools}
        documented = set(re.findall(r"^## (sessions_\w+)$", self.reference, re.MULTILINE))
        self.assertEqual(documented, registered)
        for tool in self.tools:
            section = self.section(tool["name"])
            rows = dict(re.findall(r"^\| `(\w+)` \| (.*)$", section, re.MULTILINE))
            properties = tool["inputSchema"].get("properties", {})
            self.assertEqual(set(rows), set(properties), tool["name"])
            required = set(tool["inputSchema"].get("required", []))
            for name, schema in properties.items():
                row = rows[name]
                self.assertIn("| yes |" if name in required else "| no |", row,
                              f"{tool['name']}.{name}")
                options = [schema] + schema.get("anyOf", [])
                for keyword in ("minimum", "maximum", "minLength", "maxLength"):
                    for option in options:
                        if keyword in option:
                            self.assertIn(str(option[keyword]), row,
                                          f"{tool['name']}.{name} {keyword}")
                if isinstance(schema.get("default"), int) and schema["default"]:
                    self.assertIn(f"default {schema['default']}", row, f"{tool['name']}.{name}")
                for option in options:
                    for value in option.get("enum", []):
                        self.assertIn(f"`{value}`", row, f"{tool['name']}.{name}")

    async def test_annotation_table_matches_registered_annotations(self):
        for tool in self.tools:
            row = re.search(rf"^\| `{tool['name']}` \| (\w+) \| (\w+) \| (\w+) \| (\w+) \|$",
                            self.reference, re.MULTILINE)
            self.assertIsNotNone(row, tool["name"])
            hints = tool["annotations"]
            expected = [str(hints[key]).lower() for key in (
                "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")]
            self.assertEqual(list(row.groups()), expected, tool["name"])

    async def test_readme_lists_every_tool(self):
        readme = (ROOT / "README.md").read_text()
        for tool in self.tools:
            self.assertIn(f"`{tool['name']}`", readme)

    async def test_every_documented_error_message_exists_in_the_source(self):
        source = "".join(path.read_text() for path in (ROOT / "src/aik_sessions").glob("*.py"))
        flat = re.sub(r'"\s*\n\s*f?"', "", source)
        table = re.search(r"^## Errors\n(.*?)(?=^## )", self.reference, re.MULTILINE | re.DOTALL)
        rows = re.findall(r"^\| (`.*?) \| .* \| .* \|$", table.group(1), re.MULTILINE)
        self.assertGreaterEqual(len(rows), 9)
        # "validation error" is produced by the SDK's schema check, not by this package.
        produced_elsewhere = {"validation error"}
        for cell in rows:
            for fragment in re.findall(r"`([^`]+)`", cell):
                fragment = fragment.split(" <", 1)[0].rstrip(":")
                if fragment not in produced_elsewhere:
                    self.assertIn(fragment, flat, f"docs/tools.md error row: {fragment}")

    async def test_example_shapes_match_real_output(self):
        index = SessionIndex(self.settings.database)
        index.upsert(Session(
            id="codex:doc", source="codex", title="Doc sync", path="/synthetic/doc.jsonl",
            project="/synthetic", started_at="2026-11-11T12:00:00Z",
            updated_at="2026-11-11T12:00:00Z",
            messages=[Message(id="m1", role="user", text="lighthouse keeper",
                              timestamp="2026-11-11T12:00:00Z")]))

        def example(tool):
            block = re.search(r"```json\n(.*?)\n```", self.section(tool), re.DOTALL)
            return json.loads(block.group(1))

        real = index.search("lighthouse")
        shown = example("sessions_search")
        self.assertEqual(set(shown), set(real))
        self.assertEqual(set(shown["results"][0]), set(real["results"][0]))
        exported = index.export_batch()
        shown = example("sessions_export_batch")
        self.assertEqual(set(shown), set(exported))
        self.assertEqual(set(shown["documents"][0]), set(exported["documents"][0]))


if __name__ == "__main__":
    unittest.main()
