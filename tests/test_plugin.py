"""The Claude Code plugin wrapper: manifest consistency and the one-time welcome hook.

The hook is exercised with stub browser openers on PATH, so no test opens a real page.
"""

import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from aik_sessions import __version__

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "welcome.sh"
URL = "https://example.com/welcome?utm_source=plugin&utm_medium=install"


class ManifestTests(unittest.TestCase):
    def test_one_version_everywhere(self):
        manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual({manifest["version"], project["version"], __version__}, {__version__})
        # The plugin launches with --locked, which refuses a lockfile for another version.
        locked = re.search(r'name = "aik-session-mcp"\nversion = "([^"]+)"',
                           (ROOT / "uv.lock").read_text())
        self.assertEqual(locked.group(1), __version__)

    def test_plugin_name_is_not_reserved_and_server_launches_this_package(self):
        manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        # Claude Code rejects plugin names that pass as Anthropic's own.
        self.assertNotRegex(manifest["name"], r"claude|anthropic")
        server = manifest["mcpServers"]["aik-sessions"]
        self.assertEqual(server["args"][-2:], ["aik-sessions", "serve"])
        # --locked pins dependencies to uv.lock; --no-editable avoids an extra build
        # dependency and keeps the environment independent of the plugin's install path.
        self.assertLessEqual({"--locked", "--no-editable"}, set(server["args"]))
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", server["args"])
        # Installed dependencies belong in the data directory, which survives updates.
        self.assertTrue(server["env"]["UV_PROJECT_ENVIRONMENT"].startswith("${CLAUDE_PLUGIN_DATA}"))
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual([entry["name"] for entry in marketplace["plugins"]], [manifest["name"]])

    def test_hooks_file_runs_the_welcome_script_on_new_sessions_only(self):
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
        self.assertEqual(list(hooks), ["SessionStart"])
        (entry,) = hooks["SessionStart"]
        self.assertEqual(entry["matcher"], "startup")
        self.assertIn("welcome.sh", entry["hooks"][0]["command"])

    def test_shipped_welcome_url_is_empty_or_one_https_address(self):
        lines = [line.strip() for line in (ROOT / "hooks/welcome-url.txt").read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        self.assertLessEqual(len(lines), 1)
        for line in lines:
            self.assertRegex(line, r"^https://[A-Za-z0-9._~:/?#@!&=+,;%-]+$")

    def test_skill_has_frontmatter_and_its_guides_exist(self):
        skill = ROOT / "skills/conversation-search"
        text = (skill / "SKILL.md").read_text()
        self.assertRegex(text, r"\A---\nname: conversation-search\ndescription: .{80,}\n---\n")
        referenced = set(re.findall(r"`(guides/[a-z-]+\.md)`", text))
        present = {f"guides/{path.name}" for path in (skill / "guides").glob("*.md")}
        self.assertEqual(referenced, present)
        self.assertGreaterEqual(len(present), 4)
        for guide in (skill / "guides").glob("*.md"):
            body = guide.read_text()
            # Plugin path variables are substituted in SKILL.md only; a guide is read as
            # plain text, where they would expand to nothing in a shell.
            self.assertNotIn("CLAUDE_PLUGIN", body, guide.name)
            for link in re.findall(r"`([a-z-]+\.md)`", body):
                self.assertTrue((skill / "guides" / link).is_file(), f"{guide.name} -> {link}")


@unittest.skipIf(os.name == "nt", "POSIX shell hook")
class WelcomeHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.plugin = root / "plugin"
        (self.plugin / "hooks").mkdir(parents=True)
        self.data = root / "data"
        self.log = root / "opened.log"
        stubs = root / "bin"
        stubs.mkdir()
        for name in ("open", "xdg-open"):
            stub = stubs / name
            stub.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$#" "$1" >> "$OPEN_LOG"\n')
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.environment = {
            "PATH": f"{stubs}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "OPEN_LOG": str(self.log), "DISPLAY": ":0",
            "CLAUDE_PLUGIN_ROOT": str(self.plugin), "CLAUDE_PLUGIN_DATA": str(self.data),
        }

    def tearDown(self):
        self.temporary.cleanup()

    def run_hook(self, url, **extra):
        (self.plugin / "hooks/welcome-url.txt").write_text(
            "# comment\n\n" + (url + "\n" if url is not None else ""))
        done = subprocess.run(["sh", str(HOOK)], env={**self.environment, **extra}, input="{}",
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def opened(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_no_url_configured_does_nothing(self):
        self.assertEqual(self.run_hook(None), "")
        self.assertEqual(self.opened(), [])
        self.assertFalse(self.data.exists())

    def test_opens_once_as_a_single_argument_then_stays_silent(self):
        message = json.loads(self.run_hook(URL))["systemMessage"]
        self.assertIn(URL, message)
        self.assertEqual(self.opened(), [f"1|{URL}"])
        self.assertEqual(self.run_hook(URL), "")
        self.assertEqual(len(self.opened()), 1)

    def test_a_changed_url_opens_once_more(self):
        self.run_hook(URL)
        self.run_hook("https://example.com/v2")
        self.run_hook("https://example.com/v2")
        self.assertEqual(self.opened(), [f"1|{URL}", "1|https://example.com/v2"])

    def test_unsafe_or_non_https_urls_are_ignored(self):
        for url in ("http://example.com/x", "https://example.com/$(touch pwned)",
                    'https://example.com/"; touch pwned; "', "https://example.com/a b",
                    "file:///etc/passwd", "javascript:alert(1)", "https://",
                    "https://example.com/`id`", "https://example.com/'x'"):
            with self.subTest(url=url):
                self.assertEqual(self.run_hook(url), "")
        self.assertEqual(self.opened(), [])
        self.assertFalse(self.data.exists())
        self.assertFalse((Path.cwd() / "pwned").exists())

    def test_opt_out_ci_and_sdk_sessions_do_nothing_and_do_not_use_up_the_welcome(self):
        for extra in ({"AIK_SESSIONS_NO_WELCOME": "1"}, {"CI": "true"},
                      {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.run_hook(URL, **extra), "")
        self.assertEqual(self.opened(), [])
        self.assertIn(URL, self.run_hook(URL))
        self.assertEqual(len(self.opened()), 1)

    def test_remote_session_prints_the_link_without_launching_a_browser(self):
        message = json.loads(self.run_hook(URL, SSH_CONNECTION="10.0.0.1 1 10.0.0.2 22"))
        self.assertIn(URL, message["systemMessage"])
        self.assertNotIn("opened in your browser", message["systemMessage"])
        self.assertEqual(self.opened(), [])

    def test_without_a_data_directory_it_never_opens(self):
        environment = {k: v for k, v in self.environment.items() if k != "CLAUDE_PLUGIN_DATA"}
        (self.plugin / "hooks/welcome-url.txt").write_text(URL + "\n")
        done = subprocess.run(["sh", str(HOOK)], env=environment, input="{}",
                              capture_output=True, text=True, timeout=20)
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        self.assertEqual(self.opened(), [])


if __name__ == "__main__":
    unittest.main()
