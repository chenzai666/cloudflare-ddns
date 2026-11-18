import argparse
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, mock_open, patch
from urllib.parse import parse_qs, urlsplit

import cloudflare_ddns


def record(identifier, name, content="192.0.2.1", kind="A", proxied=False):
    return dict(id=identifier, name=name + ".example.org", content=content,
                type=kind, proxied=proxied, ttl=120)


class RecordCommandsTest(unittest.TestCase):
    def setUp(self):
        self.parser = argparse.ArgumentParser()
        cloudflare_ddns.register_commands(self.parser.add_subparsers(dest="command"))
        self.config = {"BASE_DOMAIN": "example.org", "RECORD_NAME": "home.example.org",
                       "RECORD_TYPE": "A", "DEFAULT_TTL": "auto"}
        self.get_ip = Mock(return_value="192.0.2.9")

    def command(self, argv, api):
        self.output = io.StringIO()
        with redirect_stdout(self.output), redirect_stderr(self.output):
            return cloudflare_ddns.run(self.parser.parse_args(argv), self.config, api, self.get_ip)

    def test_sync_preview_apply_and_retry(self):
        source = record("s", "home", "192.0.2.9")
        rows = [source, record("a", "one"), record("p", "proxied", proxied=True),
                record("ok", "match", "192.0.2.9"), record("b", "two")]
        writes = []
        fail = True

        def api(method, endpoint, data=None):
            if method == "GET":
                query = parse_qs(urlsplit(endpoint).query)
                self.assertEqual(query["type"], ["A"])
                if "name" in query:
                    self.assertEqual(query["name"], ["home.example.org"])
                    return {"success": True, "result": [source]}
                page = int(query["page"][0])
                return {"success": True, "result": {1: rows[:3], 2: rows[3:]}[page],
                        "result_info": {"page": page, "total_pages": 2}}
            self.assertEqual(method, "PATCH")
            self.assertEqual(data, {"content": source["content"]})
            target = endpoint.split("/")[-1]
            writes.append(target)
            if fail and target == "a":
                return {"success": False, "errors": [{"message": "denied"}]}
            next(row for row in rows if row["id"] == target).update(data)
            return {"success": True}

        self.assertTrue(self.command(["sync", "--dry-run"], api))
        self.assertEqual(writes, [])
        self.assertIn("同步源：home.example.org (A) = 192.0.2.9", self.output.getvalue())
        for name in ("one.example.org", "two.example.org"):
            self.assertIn(name, self.output.getvalue())
        self.assertFalse(self.command(["sync"], api))
        self.assertEqual(writes, ["a", "b"])
        self.assertIn("成功 1，失败 1", self.output.getvalue())
        fail = False
        self.assertTrue(self.command(["sync"], api))
        self.assertTrue(self.command(["sync"], api))
        self.assertEqual(writes, ["a", "b", "a"])
        self.get_ip.assert_not_called()

    def test_confirm_uses_displayed_plan_and_does_not_ask_on_no_changes(self):
        source = record("s", "home", "192.0.2.9")
        target = record("a", "one")
        api = Mock(side_effect=[
            {"success": True, "result": [source]},
            {"success": True, "result": [source, target]},
            {"success": True},
            {"success": True, "result": [source]},
            {"success": True, "result": [source]},
        ])
        with patch("builtins.input", return_value="y") as confirm:
            self.assertTrue(self.command(["sync", "--confirm"], api))
            confirm.assert_called_once()
            self.assertTrue(self.command(["sync", "--confirm"], api))
            confirm.assert_called_once()
        self.assertEqual([call.args[0] for call in api.call_args_list], ["GET", "GET", "PATCH", "GET", "GET"])
        self.assertIn("无需同步", self.output.getvalue())

    def test_later_page_failure_aborts_before_first_write(self):
        api = Mock(side_effect=[
            {"success": True, "result": [record("s", "home", "192.0.2.9")]},
            {"success": True, "result": [record("a", "one")], "result_info": {"total_pages": 2}},
            {"success": False, "errors": [{"message": "read failed"}]},
        ])
        self.assertFalse(self.command(["sync"], api))
        self.assertEqual([call.args[0] for call in api.call_args_list], ["GET"] * 3)

    def test_ambiguous_delete_and_sync_fail_without_writing(self):
        for argv in (["delete", "home", "--type", "A"], ["sync"]):
            with self.subTest(argv=argv):
                api = Mock(return_value={"success": True, "result": [
                    record("a", "home"), record("b", "home", "192.0.2.2")]})
                self.assertFalse(self.command(argv, api))
                api.assert_called_once()
                self.assertEqual(api.call_args.args[0], "GET")

    def test_create_apex_uses_selected_address_family_and_ttl(self):
        api = Mock(side_effect=[{"success": True, "result": []}, {"success": True}])
        self.get_ip.return_value = "2001:db8::1"
        self.assertTrue(self.command(["add", "@", "--type", "AAAA", "--proxied"], api))
        self.get_ip.assert_called_once_with("AAAA")
        self.assertEqual(api.call_args.args, ("POST", "dns_records", {
            "name": "example.org", "type": "AAAA", "content": "2001:db8::1",
            "ttl": 1, "proxied": True}))

    def test_update_and_delete_only_address_the_selected_record(self):
        found = {"success": True, "result": [record("b", "home", proxied=True)]}
        for argv, expected in (
                (["update", "home", "--ttl", "240"], ("PATCH", "dns_records/b", {"ttl": 240})),
                (["delete", "home", "--type", "A"], ("DELETE", "dns_records/b", None))):
            with self.subTest(argv=argv):
                api = Mock(side_effect=[found, {"success": True}])
                self.assertTrue(self.command(argv, api))
                self.assertEqual(api.call_args.args, expected)


class UnifiedEntryTest(unittest.TestCase):
    def test_interactive_start_uses_menu_and_cron_keeps_ddns(self):
        with patch.object(cloudflare_ddns.sys.stdin, "isatty", return_value=True), \
                patch.object(cloudflare_ddns.sys.stdout, "isatty", return_value=True), \
                patch.object(cloudflare_ddns, "menu", return_value=0) as menu:
            self.assertEqual(cloudflare_ddns.main([]), 0)
            menu.assert_called_once()

    def test_cron_schedule_replaces_only_python_entry(self):
        original = (
            "15 4 * * * /usr/local/bin/backup\n"
            "*/5 * * * * /bin/bash /opt/ddns.sh ddns # cloudflare-ddns:sh\n"
            "*/10 * * * * /usr/bin/python3 /old/ddns.py ddns # cloudflare-ddns:python\n"
            "0 3 * * * /usr/bin/python3 /old/ddns.py clean-logs # cloudflare-ddns:python-logs\n"
        )
        with patch.object(subprocess, "run") as invoke, redirect_stdout(io.StringIO()), \
                patch.object(cloudflare_ddns, "LOG_FILE", PurePosixPath("/home/cron test%/.cloudflare_ddns/cloudflare_ddns.log")):
            invoke.return_value = subprocess.CompletedProcess([], 0, stdout=original, stderr="")
            cloudflare_ddns._cron_schedule("60", ["/usr/bin/python3", "/opt/A B%/ddns.py"])
            command, write = invoke.call_args
            self.assertEqual(command[0], ["crontab", "-"])
            self.assertEqual(write["input"], original[:original.rindex("*/10")] +
                             "0 * * * * umask 077; /usr/bin/python3 '/opt/A B\\%/ddns.py' ddns "
                             ">> '/home/cron test\\%/.cloudflare_ddns/cloudflare_ddns.log' 2>&1 # cloudflare-ddns:python\n"
                             "0 3 * * * /usr/bin/python3 '/opt/A B\\%/ddns.py' clean-logs # cloudflare-ddns:python-logs\n")
            cloudflare_ddns._cron_schedule("5", ["/usr/bin/python3", "/opt/ddns.py"], sync_others=True)
            self.assertIn("ddns --sync-others >> ", invoke.call_args.kwargs["input"])
            cloudflare_ddns._cron_schedule("off", [])
            self.assertEqual(invoke.call_args.kwargs["input"], original[:original.rindex("*/10")])

    def test_cron_read_failure_leaves_current_tasks_alone(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, stdout="", stderr="permission denied")) as invoke:
            with self.assertRaisesRegex(ValueError, "permission denied"):
                cloudflare_ddns._cron_schedule("5", ["/usr/bin/python3", "/opt/ddns.py"])
            invoke.assert_called_once()

    def test_log_cleanup_only_removes_old_owned_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            config = folder / "config.json"
            log = folder / "ddns.log"
            legacy_log = folder / "legacy.log"
            old = folder / "ddns.log.ddns-python-20200101-030000-000000"
            recent = folder / "ddns.log.ddns-python-20200102-030000-000000"
            unrelated = folder / "ddns.log.ddns-sh-20200101-030000"
            config.write_text(json.dumps({"LOG_FILE": str(legacy_log)}), encoding="utf-8")
            for path in (log, legacy_log, old, recent, unrelated):
                path.write_text(path.name, encoding="utf-8")
            stale = time.time() - 8 * 24 * 60 * 60
            os.utime(old, (stale, stale))
            os.utime(unrelated, (stale, stale))
            with patch.object(cloudflare_ddns, "CFG_FILE", config), patch.object(cloudflare_ddns, "LOG_FILE", log):
                cloudflare_ddns.clean_logs()
            self.assertFalse(log.exists())
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(legacy_log.exists())
            archived = set(folder.glob("ddns.log.ddns-python-*")) - {recent}
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived.pop().read_text(encoding="utf-8"), log.name)

    def test_windows_schedules_two_distinct_operations(self):
        with patch.object(cloudflare_ddns.sys, "platform", "win32"), \
                patch.object(Path, "exists", return_value=True), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as invoke:
            cloudflare_ddns.schedule("5", sync_others=True)
        update, cleanup = [item.args[0] for item in invoke.call_args_list]
        self.assertIn("MINUTE", update)
        self.assertIn("DAILY", cleanup)
        task = update[update.index("/TR") + 1]
        self.assertIn(" ddns --log-file ", task)
        self.assertIn("cloudflare_ddns.log", task)
        self.assertTrue(task.endswith(" --sync-others"))
        self.assertTrue(cleanup[cleanup.index("/TR") + 1].endswith(" clean-logs"))

    def test_legacy_log_setting_does_not_create_a_file(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()) as output:
            legacy_log = Path(directory) / "legacy.log"
            client = cloudflare_ddns.CloudflareDDNS({"LOG_FILE": str(legacy_log)})
            client.logger.info("手动运行")
            self.assertIn("手动运行", output.getvalue())
            self.assertFalse(legacy_log.exists())

    def test_explicit_log_file_collects_output(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cloudflare_ddns, "_update_ddns") as update:
            log = Path(directory) / "cloudflare_ddns.log"
            update.side_effect = lambda _scope: print("纪录一次更新") or True
            self.assertEqual(cloudflare_ddns.main(["ddns", "--log-file", str(log)]), 0)
            self.assertIn("纪录一次更新", log.read_text(encoding="utf-8"))

    def test_ddns_sync_reuses_same_client_and_aborts_on_source_failure(self):
        with patch.object(cloudflare_ddns, "CloudflareDDNS") as factory, \
                patch.object(cloudflare_ddns, "_sync", return_value=True) as sync:
            client = factory.return_value
            client.update_dns.side_effect = [False, True]
            client.current_ip = "192.0.2.9"
            self.assertEqual(cloudflare_ddns.main(["ddns", "--sync-others"]), 1)
            sync.assert_not_called()
            self.assertEqual(cloudflare_ddns.main(["ddns", "--sync-others"]), 0)
            sync.assert_called_once_with(client.config, client.cf_api_request, source_ip="192.0.2.9")

    def test_ddns_corrects_remote_change_and_preserves_proxy(self):
        config = {"RECORD_TYPE": "A", "RECORD_NAME": "home.example.org", "TTL": 60}
        remote = record("home", "home", "192.0.2.1", proxied=True)
        client = cloudflare_ddns.CloudflareDDNS.__new__(cloudflare_ddns.CloudflareDDNS)
        client.config = config
        client.logger = Mock()
        client.refresh_symbol = client.success_symbol = ""
        client.get_public_ip = Mock(return_value="192.0.2.9")
        client.cf_api_request = Mock(side_effect=[
            {"success": True, "result": [remote]},
            {"success": True},
            {"success": True, "result": [dict(remote, content="192.0.2.9")]},
        ])
        self.assertTrue(client.update_dns())
        self.assertTrue(client.update_dns())
        writes = [call.args for call in client.cf_api_request.call_args_list if call.args[0] != "GET"]
        self.assertEqual(writes, [("PATCH", "dns_records/home", {"content": "192.0.2.9"})])

    def test_old_cron_runs_only_ddns(self):
        with patch.object(cloudflare_ddns, "CloudflareDDNS") as client, \
                patch.object(cloudflare_ddns, "load_manager_config") as config:
            self.assertEqual(cloudflare_ddns.main([]), 0)
            client.assert_called_once_with()
            client.return_value.update_dns.assert_called_once_with()
            config.assert_not_called()

    def test_manager_account_wins_even_when_ddns_config_exists(self):
        data = '{"API_TOKEN":"dummy","ZONE_ID":"manager","DDNS_RECORD":"ddns.example.org"}'
        opener = mock_open(read_data=data)
        with patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "open", autospec=True, side_effect=opener):
            config = cloudflare_ddns.load_manager_config()
        self.assertEqual(opener.call_args.args[0], cloudflare_ddns.MANAGER_FILE)
        self.assertEqual(config["RECORD_NAME"], "ddns.example.org")
        self.assertEqual(config["DEFAULT_TTL"], "auto")


if __name__ == "__main__":
    unittest.main()
