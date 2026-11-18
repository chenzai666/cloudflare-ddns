import argparse
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, mock_open, patch
from urllib.parse import parse_qs, urlsplit

import cf_records
import cloudflare_ddns


def record(identifier, name, content="192.0.2.1", kind="A", proxied=False):
    return dict(id=identifier, name=name + ".example.org", content=content,
                type=kind, proxied=proxied, ttl=120)


class RecordCommandsTest(unittest.TestCase):
    def setUp(self):
        self.parser = argparse.ArgumentParser()
        cf_records.register_commands(self.parser.add_subparsers(dest="command"))
        self.config = {"BASE_DOMAIN": "example.org", "RECORD_NAME": "home.example.org",
                       "RECORD_TYPE": "A", "DEFAULT_TTL": "auto"}
        self.get_ip = Mock(return_value="192.0.2.9")

    def command(self, argv, api):
        self.output = io.StringIO()
        with redirect_stdout(self.output), redirect_stderr(self.output):
            return cf_records.run(self.parser.parse_args(argv), self.config, api, self.get_ip)

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
        for name in ("one.example.org", "two.example.org"):
            self.assertIn(name, self.output.getvalue())
        self.assertFalse(self.command(["sync"], api))
        self.assertEqual(writes, ["a", "b"])
        fail = False
        self.assertTrue(self.command(["sync"], api))
        self.assertTrue(self.command(["sync"], api))
        self.assertEqual(writes, ["a", "b", "a"])
        self.get_ip.assert_not_called()

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
