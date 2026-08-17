#!/usr/bin/env python3
"""Tests for agentbox. Run: python3 -m unittest discover -s tests

The valuable tests here are the DB ones. They run against a fixture built
with opencode 1.18.18's exact schema, so an opencode upgrade that changes
the schema fails these instead of silently reporting wrong token counts.
"""

import json
import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agentbox  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture.db")

SCHEMA = """
CREATE TABLE `session` (`id` text PRIMARY KEY,`project_id` text NOT NULL,`workspace_id` text,
 `parent_id` text,`slug` text NOT NULL,`directory` text NOT NULL,`path` text,`title` text NOT NULL,
 `version` text NOT NULL,`share_url` text,`summary_additions` integer,`summary_deletions` integer,
 `summary_files` integer,`summary_diffs` text,`metadata` text,`cost` real DEFAULT 0 NOT NULL,
 `tokens_input` integer DEFAULT 0 NOT NULL,`tokens_output` integer DEFAULT 0 NOT NULL,
 `tokens_reasoning` integer DEFAULT 0 NOT NULL,`tokens_cache_read` integer DEFAULT 0 NOT NULL,
 `tokens_cache_write` integer DEFAULT 0 NOT NULL,`revert` text,`permission` text,`agent` text,
 `model` text,`time_created` integer NOT NULL,`time_updated` integer NOT NULL,
 `time_compacting` integer,`time_archived` integer);
CREATE TABLE `message` (`id` text PRIMARY KEY,`session_id` text NOT NULL,
 `time_created` integer NOT NULL,`time_updated` integer NOT NULL,`data` text NOT NULL);
CREATE TABLE `part` (`id` text PRIMARY KEY,`message_id` text NOT NULL,`session_id` text NOT NULL,
 `time_created` integer NOT NULL,`time_updated` integer NOT NULL,`data` text NOT NULL);
CREATE TABLE `todo` (`session_id` text NOT NULL,`content` text NOT NULL,`status` text NOT NULL,
 `priority` text NOT NULL,`position` integer NOT NULL,`time_created` integer NOT NULL,
 `time_updated` integer NOT NULL, PRIMARY KEY(`session_id`,`position`));
"""

# (session, input, output, reasoning, cache_read, cache_write)
TURNS = [("ses_A", 5000, 800, 0, 12000, 400),
         ("ses_A", 3000, 500, 120, 9000, 0),
         ("ses_A", 108, 35, 59, 8320, 0),
         ("ses_B", 2000, 300, 0, 1000, 0)]

SUM_IN = sum(t[1] for t in TURNS)
SUM_OUT = sum(t[2] for t in TURNS)


def build_fixture(path=FIXTURE):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    now = int(time.time() * 1000)
    model = json.dumps({"id": "big-pickle", "providerID": "opencode"})
    cols = "?," * 28 + "?"

    con.execute(f"INSERT INTO session VALUES({cols})",
                ("ses_A", "global", None, None, "shiny-meadow",
                 "/home/francesco/proj", "home/francesco/proj",
                 "Web UI inaccessible via Tailscale", "1.18.18", None, 0, 0, 0,
                 None, None, 0.0,
                 # stored cols hold ONLY the last turn - the real 1.18.18 behaviour
                 108, 35, 59, 8320, 0,
                 None, None, "build", model, now - 3600_000, now - 120_000, None, None))
    con.execute(f"INSERT INTO session VALUES({cols})",
                ("ses_B", "global", None, "ses_A", "brisk-hill",
                 "/home/francesco/proj", "home/francesco/proj",
                 "grep the config tree", "1.18.18", None, 0, 0, 0, None, None, 0.0,
                 50, 10, 0, 1000, 0,
                 None, None, "general", model, now - 1800_000, now - 900_000, None, None))

    for i, (sid, i_, o_, r_, cr, cw) in enumerate(TURNS):
        mid = f"msg_{i}"
        con.execute("INSERT INTO message VALUES(?,?,?,?,?)",
                    (mid, sid, now - 1000 * i, now - 1000 * i,
                     json.dumps({"role": "assistant", "modelID": "big-pickle",
                                 "providerID": "opencode"})))
        con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                    (f"prt_{i}", mid, sid, now - 1000 * i, now - 1000 * i,
                     json.dumps({"type": "step-finish", "reason": "unknown", "cost": 0,
                                 "tokens": {"input": i_, "output": o_, "reasoning": r_,
                                            "cache": {"read": cr, "write": cw}}})))
        # a non-step-finish part that must be ignored
        con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                    (f"prtx_{i}", mid, sid, now - 1000 * i, now - 1000 * i,
                     json.dumps({"type": "text", "text": "noise"})))

    con.execute("INSERT INTO todo VALUES(?,?,?,?,?,?,?)",
                ("ses_A", "Wire agentbox into AGENTS.md", "in_progress", "high", 0, now, now))
    con.execute("INSERT INTO todo VALUES(?,?,?,?,?,?,?)",
                ("ses_A", "Ship the README", "pending", "medium", 1, now, now))
    con.execute("INSERT INTO todo VALUES(?,?,?,?,?,?,?)",
                ("ses_A", "already done", "completed", "low", 2, now, now))
    con.commit()
    con.close()
    return path


class TestFormatters(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(agentbox.human_bytes(0), "0B")
        self.assertEqual(agentbox.human_bytes(1536), "1.5K")
        self.assertEqual(agentbox.human_bytes(3 * 1024 ** 3), "3.0G")

    def test_human_count(self):
        self.assertEqual(agentbox.human_count(999), "999")
        self.assertEqual(agentbox.human_count(1500), "1.5k")
        self.assertEqual(agentbox.human_count(2_500_000), "2.50M")

    def test_human_delta(self):
        self.assertEqual(agentbox.human_delta(45), "45s")
        self.assertEqual(agentbox.human_delta(3600), "1h00m")
        self.assertEqual(agentbox.human_delta(90000), "1d1h")

    def test_bar_clamps(self):
        self.assertEqual(agentbox.bar(-10, 4), "[....]")
        self.assertEqual(agentbox.bar(999, 4), "[####]")

    def test_command_output_replaces_invalid_utf8(self):
        output, error = agentbox.run_result([
            sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"
        ])
        self.assertEqual(output, "\ufffd")
        self.assertIsNone(error)


class TestModelLabel(unittest.TestCase):
    def test_bare_session_model_object(self):
        # session.model column shape
        self.assertEqual(
            agentbox._model_label({"id": "big-pickle", "providerID": "opencode"}),
            "opencode/big-pickle")

    def test_flat_message_keys(self):
        self.assertEqual(
            agentbox._model_label({"modelID": "gpt-5", "providerID": "openai"}),
            "openai/gpt-5")

    def test_falls_through_sources(self):
        self.assertEqual(agentbox._model_label({}, {"modelID": "x"}), "?/x")

    def test_none_when_unknown(self):
        self.assertIsNone(agentbox._model_label({}, None, "junk"))


class TestPartTokens(unittest.TestCase):
    def test_nested_cache(self):
        got = agentbox._part_tokens(
            {"tokens": {"input": 1, "output": 2, "reasoning": 3,
                        "cache": {"read": 4, "write": 5}}})
        self.assertEqual(got, {"input": 1, "output": 2, "reasoning": 3,
                               "cache_read": 4, "cache_write": 5})

    def test_missing_tokens(self):
        self.assertIsNone(agentbox._part_tokens({"type": "text"}))

    def test_malformed_tokens(self):
        self.assertIsNone(agentbox._part_tokens({"tokens": {"input": "nope"}}))


class TestCLI(unittest.TestCase):
    def test_positive_numeric_options(self):
        args = agentbox.build_parser().parse_args(["--days", "1", "--watch", "2"])
        self.assertEqual((args.days, args.watch), (1, 2))

    def test_rejects_non_positive_numeric_options(self):
        for argv in (["--days", "0"], ["--days", "-1"],
                     ["--watch", "0"], ["--watch", "-1"]):
            with self.subTest(argv=argv), mock.patch.object(sys, "stderr", io.StringIO()), \
                    self.assertRaises(SystemExit):
                agentbox.build_parser().parse_args(argv)

    def test_redact_aliases(self):
        for flag in ("--redact", "--no-titles"):
            with self.subTest(flag=flag):
                self.assertTrue(agentbox.build_parser().parse_args([flag]).redact)

    def test_jsonl_is_one_parseable_line(self):
        snap = {"hostname": "box", "timestamp": "now", "status": "OK", "warnings": []}
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["agentbox", "--jsonl", "cpu"]), \
                mock.patch.object(agentbox, "snapshot", return_value=snap), \
                mock.patch.object(sys, "stdout", stdout):
            self.assertEqual(agentbox.main(), 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), snap)

    def test_watch_non_tty_has_no_ansi(self):
        long_message = "x" * 200
        snap = {"hostname": "box", "timestamp": "now", "status": "WARNING",
                "warnings": [{"code": "test", "message": long_message}]}
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["agentbox", "--watch", "1", "cpu"]), \
                mock.patch.object(agentbox, "snapshot", return_value=snap), \
                mock.patch.object(agentbox.time, "sleep", side_effect=KeyboardInterrupt), \
                mock.patch.object(sys, "stdout", stdout):
            self.assertEqual(agentbox.main(), 0)
        self.assertNotIn("\x1b", stdout.getvalue())
        self.assertIn(long_message, stdout.getvalue())

    def test_formatted_json_watch_is_rejected(self):
        with mock.patch.object(sys, "argv", ["agentbox", "--json", "--watch", "1"]), \
                mock.patch.object(sys, "stderr", io.StringIO()), \
                self.assertRaises(SystemExit):
            agentbox.main()

    def test_closed_pipe_exits_cleanly(self):
        class ClosedPipe(io.StringIO):
            def write(self, value):
                raise BrokenPipeError

            def close(self):
                pass

        stdout = ClosedPipe()
        snap = {"hostname": "box", "timestamp": "now", "status": "OK", "warnings": []}
        with mock.patch.object(sys, "argv", ["agentbox", "--jsonl", "cpu"]), \
                mock.patch.object(agentbox, "snapshot", return_value=snap), \
                mock.patch.object(sys, "stdout", stdout):
            self.assertEqual(agentbox.main(), 0)

    def test_closed_pipe_preserves_check_warning_exit(self):
        class ClosedPipe(io.StringIO):
            def write(self, value):
                raise BrokenPipeError

            def close(self):
                pass

        snap = {"hostname": "box", "timestamp": "now", "status": "WARNING",
                "warnings": [{"code": "test", "message": "warning"}]}
        with mock.patch.object(sys, "argv", ["agentbox", "--check", "--jsonl"]), \
                mock.patch.object(agentbox, "snapshot", return_value=snap), \
                mock.patch.object(sys, "stdout", ClosedPipe()):
            self.assertEqual(agentbox.main(), 1)

    def test_check_exit_codes(self):
        stdout = io.StringIO()
        warning = {"hostname": "box", "timestamp": "now", "status": "WARNING",
                   "warnings": [{"code": "test", "message": "warning"}]}
        with mock.patch.object(sys, "argv", ["agentbox", "--check", "cpu"]), \
                mock.patch.object(agentbox, "snapshot", return_value=warning), \
                mock.patch.object(sys, "stdout", stdout):
            self.assertEqual(agentbox.main(), 1)

        healthy = dict(warning, status="OK", warnings=[])
        with mock.patch.object(sys, "argv", ["agentbox", "--check", "cpu"]), \
                mock.patch.object(agentbox, "snapshot", return_value=healthy), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(agentbox.main(), 0)

    def test_check_watch_is_rejected(self):
        with mock.patch.object(sys, "argv", ["agentbox", "--check", "--watch", "1"]), \
                mock.patch.object(sys, "stderr", io.StringIO()), \
                self.assertRaises(SystemExit):
            agentbox.main()


class TestReleaseOneCollectors(unittest.TestCase):
    PRESSURE = ("some avg10=1.25 avg60=2.50 avg300=3.75 total=123\n"
                "full avg10=0.10 avg60=0.20 avg300=0.30 total=45\n")

    def test_parse_pressure(self):
        parsed = agentbox._parse_pressure(self.PRESSURE)
        self.assertEqual(parsed["some"]["avg10"], 1.25)
        self.assertEqual(parsed["full"]["total_us"], 45)

    def test_parse_pressure_requires_complete_some_line(self):
        self.assertIsNone(agentbox._parse_pressure("some avg10=1 total=2"))

    def test_parse_mountinfo_and_select_longest_mount(self):
        text = (
            "44 1 259:2 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
            "45 44 259:3 / /home/data\\040set ro,relatime - ext4 /dev/data ro\n"
        )
        mounts = agentbox._parse_mountinfo(text)
        self.assertEqual(mounts[1]["mount_point"], "/home/data set")
        self.assertEqual(agentbox._mount_for_path("/home/data set/file", mounts)["device"],
                         "259:3")

    def test_listener_endpoint_scopes(self):
        cases = {
            "0.0.0.0:22": ("0.0.0.0", "22", "wildcard"),
            "127.0.0.1:631": ("127.0.0.1", "631", "loopback"),
            "100.105.6.51:80": ("100.105.6.51", "80", "tailscale"),
            "192.168.1.2:80": ("192.168.1.2", "80", "lan"),
            "[::1]:631": ("::1", "631", "loopback"),
            "[2001:4860:4860::8888]:53": ("2001:4860:4860::8888", "53", "external"),
        }
        for endpoint, expected in cases.items():
            with self.subTest(endpoint=endpoint):
                host, port = agentbox._split_endpoint(endpoint)
                tailscale = {"100.105.6.51"}
                self.assertEqual((host, port, agentbox._listener_scope(host, tailscale)),
                                 expected)

    def test_agent_listener_is_structured(self):
        output = ('LISTEN 0 4096 0.0.0.0:11434 0.0.0.0:* '
                  'users:(("ollama",pid=42,fd=3))\n')
        with mock.patch.object(agentbox, "run_result", return_value=(output, None)), \
                mock.patch.object(agentbox, "_tailscale_addresses", return_value=set()):
            listeners, error = agentbox.collect_listeners()
        self.assertIsNone(error)
        self.assertEqual(listeners[0]["agent_kind"], "ollama")
        self.assertEqual(listeners[0]["scope"], "wildcard")
        self.assertEqual(listeners[0]["pid"], 42)

    def test_claude_process_detection_is_exact(self):
        with mock.patch.object(agentbox, "read", return_value="claude\n"):
            self.assertEqual(agentbox._is_agent_proc(1, "/home/user/.local/bin/claude"),
                             "claude")
        self.assertIsNone(agentbox._agent_kind_from_names(["claude-code-url-handler"]))
        with mock.patch.object(agentbox, "read", return_value="node\n"):
            command = "node /opt/node_modules/@anthropic-ai/claude-code/cli.js"
            self.assertEqual(agentbox._is_agent_proc(1, command), "claude")
        self.assertEqual(agentbox._agent_kind_from_names(["opencode-deskto"]), "opencode")

    def test_agents_counts_providers_without_cmdline(self):
        agents = agentbox.collect_agents([
            {"kind": "claude", "pid": 1, "cmdline": "secret", "rss_bytes": 10,
             "tty": None, "age_seconds": 2},
            {"kind": "opencode", "pid": 2, "cmdline": "secret", "rss_bytes": 20,
             "tty": None, "age_seconds": 3},
        ])
        self.assertEqual(agents["counts"]["claude"], 1)
        self.assertNotIn("cmdline", agents["processes"][0])

    def test_new_warning_codes(self):
        snap = {
            "disk": [{"path": "/", "used_pct": 10, "inode_used_pct": 90,
                      "read_only": True}],
            "listening": [{"agent_kind": "ollama", "scope": "wildcard",
                           "address": "0.0.0.0:11434"}],
            "pressure": {"cpu": {"some": {"avg10": 51}},
                         "memory": {"full": {"avg10": 6}},
                         "io": {"full": {"avg10": 11}}},
        }
        codes = {warning["code"] for warning in agentbox.collect_warnings(snap)}
        self.assertEqual(codes, {"inode_high", "disk_read_only", "agent_server_exposed",
                                 "cpu_pressure_high", "memory_pressure_high",
                                 "io_pressure_high"})

    def test_unavailable_pressure_is_warning(self):
        warnings = agentbox.collect_warnings({
            "pressure": {"available": False, "reason": "not mounted",
                         "cpu": None, "memory": None, "io": None},
        })
        self.assertEqual(warnings[0]["code"], "pressure_unavailable")

    def test_redaction_hides_mount_source_and_warning_address(self):
        snap = {
            "hostname": "box",
            "disk": [{"mount_source": "10.0.0.2:/home/private", "used_pct": 10}],
            "listening": [{"address": "192.168.1.2:11434", "host": "192.168.1.2",
                           "port": "11434", "scope": "lan", "process": "ollama(1)",
                           "process_name": "ollama", "pid": 1, "agent_kind": "ollama"}],
        }
        agentbox.redact_snapshot(snap)
        warnings = agentbox.collect_warnings(snap)
        blob = json.dumps({"snapshot": snap, "warnings": warnings})
        self.assertNotIn("10.0.0.2", blob)
        self.assertNotIn("192.168.1.2", blob)


class TestReleaseTwoCollectors(unittest.TestCase):
    def test_claude_usage_is_normalized_without_content(self):
        record = {
            "timestamp": "2026-08-17T12:00:00Z",
            "message": {"usage": {
                "input_tokens": 10, "output_tokens": 4,
                "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2,
            }, "content": "secret prompt"},
        }
        with tempfile.TemporaryDirectory() as root:
            project = os.path.join(root, "private-project")
            os.makedirs(project)
            path = os.path.join(project, "session.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            with mock.patch.object(agentbox.os.path, "expanduser", return_value=root), \
                    mock.patch.object(agentbox, "run", return_value="Claude 2.1.233"):
                result = agentbox.collect_claude(days=30, scrub=True)
        self.assertTrue(result["available"])
        self.assertEqual(result["totals"]["input"], 10)
        self.assertEqual(result["totals"]["cache_read"], 3)
        self.assertNotIn("secret prompt", json.dumps(result))
        self.assertTrue(result["projects"][0]["project_hash"].startswith("sha256:"))

    def test_claude_unknown_schema_is_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "project"))
            with open(os.path.join(root, "project", "session.jsonl"), "w") as fh:
                fh.write('{"type":"system","content":"secret"}\n')
            with mock.patch.object(agentbox.os.path, "expanduser", return_value=root):
                result = agentbox.collect_claude()
        self.assertFalse(result["available"])
        self.assertIn("unsupported", result["reason"])

    def test_ollama_list_and_redaction(self):
        listing = ("NAME ID SIZE MODIFIED\n"
                   "model:latest abc123 1.5 GB 2 hours ago\n")
        running = "NAME ID SIZE PROCESSOR UNTIL\nmodel:latest abc123 1.5 GB 100% CPU 4 minutes\n"
        def result(cmd, timeout=5):
            if cmd[-1] == "list":
                return listing, None
            if cmd[-1] == "ps":
                return running, None
            return "", None
        with mock.patch.object(agentbox, "run_result", side_effect=result), \
                mock.patch.object(agentbox, "run", return_value="ollama version is 1"):
            out = agentbox.collect_ollama(scrub=True)
        self.assertTrue(out["available"])
        self.assertEqual(out["models"][0]["size_bytes"], int(1.5 * 1024 ** 3))
        self.assertTrue(out["running"][0]["name"].startswith("sha256:"))
        self.assertIsNone(out["running"][0]["raw"])

    def test_changes_uses_metadata_only(self):
        def result(cmd, timeout=5):
            if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return "/tmp/repo\n", None
            if cmd[1:3] == ["log", "-1"]:
                return "a" * 40 + "\0subject\017123456789\n", None
            if cmd[1] == "status":
                return " M secret.py\n?? new.txt\n", None
            if cmd[1:3] == ["diff", "HEAD"]:
                return "2\t1\tsecret.py\n", None
            return "", "unexpected"
        with mock.patch.object(agentbox, "run_result", side_effect=result):
            out = agentbox.collect_changes(scrub=True)
        self.assertTrue(out["available"])
        self.assertTrue(out["dirty"])
        self.assertEqual(out["counts"]["modified"], 1)
        self.assertIsNone(out["root"])
        self.assertNotIn("secret.py", json.dumps(out))


class TestStatusAndRendering(unittest.TestCase):
    def test_warning_summary(self):
        snap = {
            "disk": [{"path": "/", "used_pct": 91.0}],
            "memory": {"total_bytes": 100, "available_bytes": 5},
        }
        warnings = agentbox.collect_warnings(snap)
        self.assertEqual({w["code"] for w in warnings}, {"disk_high", "memory_low"})

    def test_redact_removes_identifiers(self):
        snap = {
            "hostname": "private-host",
            "cpu": {"top": [{"pid": 123, "name": "python", "cmdline": "/secret/x"}]},
            "listening": [{"address": "10.0.0.1:8080", "port": "8080",
                           "process": "server(123)"}],
            "gpu": {"processes": [{"pid": 456, "name": "secret-worker"}]},
            "opencode": {
                "db_path": "/home/user/opencode.db",
                "live_processes": [{"pid": 123, "tty": "pts/1", "cmdline": "/secret"}],
                "sessions": [{"id": "ses_secret", "parent_id": "ses_parent",
                              "title": "secret title", "directory": "/secret/project"}],
                "todos": [{"session_id": "ses_secret", "content": "secret todo"}],
            },
        }
        agentbox.redact_snapshot(snap)
        blob = json.dumps(snap)
        for secret in ("private-host", "10.0.0.1", "ses_secret", "ses_parent",
                       "/home/user", "pts/1", "/secret", "secret title", "secret todo",
                       "secret-worker", "server(123)", "python"):
            self.assertNotIn(secret, blob)
        self.assertEqual(snap["listening"][0]["address"], "*:8080")

    def test_listener_ownership_warning(self):
        snap = {"services": {
            "available": True, "failed": [], "watched": [],
            "listeners_available": True, "listener_owners_available": False,
        }}
        self.assertEqual(agentbox.collect_warnings(snap)[0]["code"],
                         "listener_owners_unavailable")

    def test_unavailable_reason_is_clipped(self):
        snap = {
            "hostname": "box", "timestamp": "now", "status": "WARNING", "warnings": [],
            "opencode": {"window_days": 7, "version": None, "tested_version": "1",
                         "live_processes": [], "available": False, "reason": "x" * 200},
        }
        self.assertTrue(all(len(line) <= 40 for line in agentbox.render(snap, width=40).splitlines()))

    def test_clip_handles_tiny_widths(self):
        self.assertEqual([agentbox.clip("long", width) for width in range(4)],
                         ["", ".", "..", "..."])

    def test_plain_gpu_output_is_ascii(self):
        snap = {
            "hostname": "box", "timestamp": "now", "status": "OK", "warnings": [],
            "gpu": {"available": True, "gpus": [{
                "index": 0, "name": "gpu", "util_pct": 1, "mem_used_bytes": 1,
                "mem_total_bytes": 2, "temp_c": 40, "power_w": None,
                "power_limit_w": None,
            }], "processes": [{"name": "worker", "pid": 1, "mem_bytes": 1}]},
        }
        self.assertTrue(agentbox.render(snap, plain=True).isascii())

    def test_session_count_matches_rendered_rows(self):
        sessions = [{
            "age_seconds": n + 1, "summed": {"input": 1, "output": 1},
            "model": "provider/model", "is_subagent": False, "agent": "build",
            "title": f"session-{n}", "id": f"ses_{n}",
        } for n in range(10)]
        snap = {
            "hostname": "box", "timestamp": "now", "status": "OK", "warnings": [],
            "opencode": {
                "window_days": 7, "version": None, "tested_version": "1",
                "live_processes": [], "available": True,
                "tokens_total": dict(agentbox.ZERO_TOKENS, turns=0), "cost_usd": 0,
                "tokens_by_model": {}, "tokens_by_day": {}, "sessions": sessions,
                "session_count": 10, "todos": [], "stored_matches_summed": None,
            },
        }
        text = agentbox.render(snap)
        self.assertIn("showing 8", text)
        self.assertIn("session-7", text)
        self.assertNotIn("session-8", text)

    def test_collect_services_keeps_full_watchlist(self):
        def fake_result(cmd, timeout=5):
            if "--state=running" in cmd:
                return "ssh.service loaded active running SSH\n", None
            return "", None

        def fake_run(cmd, timeout=5):
            unit = cmd[-1]
            if unit == "ssh" and "--user" not in cmd:
                return "active\n"
            if unit == "ollama" and "--user" in cmd:
                return "active\n"
            return "inactive\n"

        with mock.patch.object(agentbox, "run_result", side_effect=fake_result), \
                mock.patch.object(agentbox, "run", side_effect=fake_run):
            services = agentbox.collect_services()
        self.assertEqual([w["unit"] for w in services["watched"]], agentbox.WATCHED_UNITS)
        ollama = next(w for w in services["watched"] if w["unit"] == "ollama")
        self.assertEqual((ollama["state"], ollama["scope"]), ("active", "user"))


class TestCollector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_fixture()
        os.environ["AGENTBOX_OPENCODE_DB"] = FIXTURE
        cls.oc = agentbox.collect_opencode(days=30)

    def test_version_read_from_db_not_subprocess(self):
        self.assertEqual(self.oc["version"], "1.18.18")

    def test_available(self):
        self.assertTrue(self.oc["available"], self.oc.get("reason"))

    def test_sums_only_step_finish_parts(self):
        # 'text' parts carry no tokens and must not inflate the count
        self.assertEqual(self.oc["tokens_total"]["turns"], len(TURNS))
        self.assertEqual(self.oc["tokens_total"]["input"], SUM_IN)
        self.assertEqual(self.oc["tokens_total"]["output"], SUM_OUT)

    def test_detects_stored_is_not_lifetime_sum(self):
        # the whole reason we sum from `part` instead of trusting session.*
        self.assertIs(self.oc["stored_matches_summed"], False)

    def test_session_summed_beats_stored(self):
        a = next(s for s in self.oc["sessions"] if s["id"] == "ses_A")
        self.assertEqual(a["summed"]["input"], 8108)
        self.assertEqual(a["stored"]["input"], 108)

    def test_subagent_flagged(self):
        b = next(s for s in self.oc["sessions"] if s["id"] == "ses_B")
        self.assertTrue(b["is_subagent"])
        self.assertEqual(b["agent"], "general")

    def test_model_resolved(self):
        self.assertIn("opencode/big-pickle", self.oc["tokens_by_model"])

    def test_open_todos_only(self):
        contents = [t["content"] for t in self.oc["todos"]]
        self.assertIn("Wire agentbox into AGENTS.md", contents)
        self.assertNotIn("already done", contents)

    def test_window_excludes_old(self):
        narrow = agentbox.collect_opencode(days=0)
        self.assertEqual(narrow["tokens_total"]["turns"], 0)
        self.assertTrue(narrow["available"])

    def test_scrub_removes_identifying_text(self):
        scrubbed = agentbox.collect_opencode(days=30, scrub=True)
        blob = json.dumps(scrubbed)
        self.assertNotIn("Tailscale", blob)
        self.assertNotIn("/home/francesco", blob)
        self.assertNotIn("Wire agentbox", blob)
        self.assertNotIn(os.path.expanduser("~"), blob)   # db_path leaks username
        # but the numbers survive
        self.assertEqual(scrubbed["tokens_total"]["input"], SUM_IN)


class TestDegradation(unittest.TestCase):
    """The snapshot must never crash just because opencode is absent/changed."""

    def test_missing_db(self):
        """An explicit override must NOT silently fall back to a real DB."""
        os.environ["AGENTBOX_OPENCODE_DB"] = "/nonexistent/nope.db"
        oc = agentbox.collect_opencode()
        os.environ["AGENTBOX_OPENCODE_DB"] = FIXTURE
        self.assertFalse(oc["available"])
        self.assertIsNotNone(oc["reason"])

    def test_schema_mismatch(self):
        path = FIXTURE + ".empty"
        sqlite3.connect(path).close()
        os.environ["AGENTBOX_OPENCODE_DB"] = path
        oc = agentbox.collect_opencode()
        os.environ["AGENTBOX_OPENCODE_DB"] = FIXTURE
        os.remove(path)
        self.assertFalse(oc["available"])
        self.assertIn("schema mismatch", oc["reason"])

    def test_render_never_raises(self):
        snap = agentbox.snapshot(days=30)
        self.assertIsInstance(agentbox.render(snap), str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
