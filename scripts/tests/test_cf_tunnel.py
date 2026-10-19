"""Offline security and behavior tests for cf-tunnel.sh.

Run with: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest scripts.tests.test_cf_tunnel -v
"""
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
import urllib.parse

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cf-tunnel.sh"
API_TOKEN = "cf_test_" + "T" * 32
CONNECTOR_TOKEN = "connector_SECRET_value"


class CfTunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cf-tunnel-test-")
        self.root = pathlib.Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.token_file = self.root / "token"
        self.write_token(API_TOKEN + "\n")
        self.log = self.root / "calls.jsonl"
        self.tmpdirs = self.root / "tmpdirs"
        self.tmpdirs.mkdir()
        self.make_curl()
        self.make_kubectl()

    def tearDown(self):
        self.tmp.cleanup()

    def write_token(self, value, mode=0o600):
        data = value.encode("utf-8") if isinstance(value, str) else value
        self.token_file.write_bytes(data)
        self.token_file.chmod(mode)

    def make_curl(self):
        program = """#!/usr/bin/env python3
import json, os, pathlib, stat, sys

args = sys.argv[1:]
record = {
    "argv": args,
    "first_argument_is_q": bool(args) and args[0] == "-q",
    "has_explicit_config": "--config" in args,
    "noproxy_all": any(args[i:i + 2] == ["--noproxy", "*"] for i in range(len(args))),
}
if record["has_explicit_config"]:
    config = pathlib.Path(args[args.index("--config") + 1])
    config_text = config.read_text(encoding="ascii")
    expected_config = (
        'header = "Authorization: Bearer ' + os.environ["FAKE_API_TOKEN"] + '"\\n'
        'header = "Content-Type: application/json"\\n'
    )
    record["config_private"] = stat.S_IMODE(config.stat().st_mode) == 0o600
    record["config_parent_private"] = not (stat.S_IMODE(config.parent.stat().st_mode) & 0o077)
    record["config_matches_expected"] = config_text == expected_config
else:
    record["config_private"] = False
    record["config_parent_private"] = False
    record["config_matches_expected"] = False

with open(os.environ["FAKE_LOG"], "a") as f:
    print(json.dumps({"curl": record}), file=f)

if not all((record["first_argument_is_q"], record["has_explicit_config"],
            record["noproxy_all"], record["config_private"],
            record["config_parent_private"], record["config_matches_expected"])):
    print("fake curl isolation failure: " + json.dumps({
        key: value for key, value in record.items() if key != "argv"
    }), file=sys.stderr)
    raise SystemExit(70)

if os.environ.get("FAKE_CURL_FAIL") == "1":
    print("fake curl failure", file=sys.stderr)
    raise SystemExit(22)

url = next((a for a in args if a.startswith("https://api.cloudflare.com/")), "")
method = args[args.index("-X") + 1] if "-X" in args else "GET"
if "/cfd_tunnel?name=" in url and "is_deleted=false" in url:
    result = [{"id": "tid-1"}] if os.environ.get("FAKE_TUNNEL_EXISTS") == "1" else []
    print(json.dumps({"success": True, "result": result}))
elif url.endswith("/cfd_tunnel/tid-1/token"):
    print(json.dumps({"success": True, "result": os.environ["FAKE_CONNECTOR_TOKEN"]}))
elif url.endswith("/cfd_tunnel/tid-1/configurations"):
    body = json.loads(args[args.index("--data") + 1])
    with open(os.environ["FAKE_LOG"], "a") as f:
        print(json.dumps({"body": body}), file=f)
    print(json.dumps({"success": True, "result": {}}))
elif url.endswith("/cfd_tunnel/tid-1"):
    print(json.dumps({"success": True, "result": {
        "id": "tid-1", "name": "quoted", "status": "healthy", "connections": [{}]
    }}))
elif "/dns_records?name=" in url:
    print(json.dumps({"success": True, "result": []}))
elif "/dns_records" in url and method in ("POST", "PUT"):
    body = json.loads(args[args.index("--data") + 1])
    with open(os.environ["FAKE_LOG"], "a") as f:
        print(json.dumps({"body": body}), file=f)
    print(json.dumps({"success": True, "result": {}}))
elif method == "POST" and url.endswith("/cfd_tunnel"):
    body = json.loads(args[args.index("--data") + 1])
    with open(os.environ["FAKE_LOG"], "a") as f:
        print(json.dumps({"body": body}), file=f)
    print(json.dumps({"success": True, "result": {"id": "tid-1"}}))
else:
    print(json.dumps({"success": True, "result": []}))
"""
        path = self.bin / "curl"
        path.write_text(program)
        path.chmod(0o700)

    def make_kubectl(self):
        program = """#!/usr/bin/env python3
import base64, json, os, pathlib, re, sys

args = sys.argv[1:]
expected = os.environ["FAKE_CONNECTOR_TOKEN"].encode("ascii")

def log(**extra):
    payload = {"kubectl": args}
    payload.update(extra)
    with open(os.environ["FAKE_LOG"], "a") as f:
        print(json.dumps(payload), file=f)

if "apply" in args:
    log(apply_attempt=True)
    print("apply forbidden", file=sys.stderr)
    raise SystemExit(2)

if "get" in args:
    log(get_attempt=True)
    raise SystemExit(0 if os.environ.get("FAKE_SECRET_EXISTS") == "1" else 1)

if "create" in args and "--dry-run=client" in args:
    secret_input = sys.stdin.buffer.read()
    input_matches = secret_input == expected
    log(secret_input_matches=input_matches)
    if not input_matches:
        print("unexpected secret input", file=sys.stderr)
        raise SystemExit(2)
    name = args[args.index("generic") + 1]
    namespace = args[args.index("-n") + 1]
    encoded = base64.b64encode(expected).decode("ascii")
    sys.stdout.write(
        "apiVersion: v1\\nkind: Secret\\nmetadata:\\n"
        f"  name: {name}\\n  namespace: {namespace}\\n"
        "type: Opaque\\ndata:\\n"
        f"  token: {encoded}\\n"
    )
    raise SystemExit(0)

if "-f" in args and ("create" in args or "replace" in args):
    manifest = pathlib.Path(args[args.index("-f") + 1]).read_text(encoding="utf-8")
    match = re.fullmatch(
        r"apiVersion: v1\\nkind: Secret\\nmetadata:\\n  name: [^\\n]+\\n"
        r"  namespace: [^\\n]+\\ntype: Opaque\\ndata:\\n  token: ([A-Za-z0-9+/=]+)\\n",
        manifest,
    )
    try:
        decoded = base64.b64decode(match.group(1), validate=True) if match else b""
    except ValueError:
        decoded = b""
    encoded = base64.b64encode(expected).decode("ascii")
    has_expected_connector = decoded == expected
    only_expected_connector = bool(match) and manifest.count(encoded) == 1 and expected.decode("ascii") not in manifest
    log(
        manifest_read=True,
        manifest_is_secret=bool(match),
        manifest_has_expected_connector=has_expected_connector,
        manifest_only_expected_connector=only_expected_connector,
    )
    if not all((match, has_expected_connector, only_expected_connector)):
        print("unexpected secret manifest", file=sys.stderr)
        raise SystemExit(2)
    if os.environ.get("FAKE_KUBECTL_APPLY_FAIL") == "1":
        print("fake kubectl apply-stage failure", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0)

log(unexpected_call=True)
print("unexpected kubectl invocation", file=sys.stderr)
raise SystemExit(2)
"""
        path = self.bin / "kubectl"
        path.write_text(program)
        path.chmod(0o700)

    def run_script(self, cmd, **extra):
        curl_home = self.root / "curl-home"
        curl_home.mkdir(exist_ok=True)
        (curl_home / ".curlrc").write_text("proxy = http://ignored.invalid:1\n")
        env = os.environ.copy()
        env.update({
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "HOME": str(curl_home),
            "CURL_HOME": str(curl_home),
            "CF_API_TOKEN_FILE": str(self.token_file),
            "FAKE_API_TOKEN": API_TOKEN,
            "FAKE_CONNECTOR_TOKEN": CONNECTOR_TOKEN,
            "FAKE_LOG": str(self.log),
            "TMPDIR": str(self.tmpdirs),
            "HTTP_PROXY": "http://proxy.invalid:8080",
            "HTTPS_PROXY": "http://proxy.invalid:8080",
            "ALL_PROXY": "http://proxy.invalid:8080",
            "NO_PROXY": "",
            "FAKE_TUNNEL_EXISTS": "1" if cmd == "show" else "0",
        })
        env.update(extra)
        return subprocess.run([str(SCRIPT), cmd], env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def records(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_cleaned(self):
        self.assertEqual(list(self.tmpdirs.iterdir()), [])

    def assert_no_secrets_in_process_io(self, proc):
        output = proc.stdout + proc.stderr
        if self.log.exists():
            output += self.log.read_text()
        self.assertNotIn(API_TOKEN, output)
        self.assertNotIn(CONNECTOR_TOKEN, output)

    def assert_curl_isolated(self, records):
        curl_records = [record["curl"] for record in records if "curl" in record]
        self.assertTrue(curl_records)
        for record in curl_records:
            self.assertEqual(record["argv"][0], "-q")
            self.assertTrue(record["first_argument_is_q"])
            self.assertTrue(record["has_explicit_config"])
            self.assertTrue(record["noproxy_all"])
            self.assertTrue(record["config_private"])
            self.assertTrue(record["config_parent_private"])
            self.assertTrue(record["config_matches_expected"])
            self.assertNotIn(API_TOKEN, " ".join(record["argv"]))

    def assert_secret_manifest(self, records, final_verb):
        dry_run = [record for record in records if record.get("secret_input_matches") is not None]
        manifests = [record for record in records if record.get("manifest_read")]
        self.assertEqual(len(dry_run), 1)
        self.assertTrue(dry_run[0]["secret_input_matches"])
        self.assertEqual(len(manifests), 1)
        manifest = manifests[0]
        self.assertIn(final_verb, manifest["kubectl"])
        self.assertTrue(manifest["manifest_is_secret"])
        self.assertTrue(manifest["manifest_has_expected_connector"])
        self.assertTrue(manifest["manifest_only_expected_connector"])
        self.assertFalse(any("apply" in record.get("kubectl", []) for record in records))

    def assert_query_encoded(self, records, marker, value):
        urls = [
            arg
            for record in records if "curl" in record
            for arg in record["curl"]["argv"]
            if arg.startswith("https://api.cloudflare.com/") and marker in arg
        ]
        self.assertEqual(len(urls), 1)
        parsed = urllib.parse.urlsplit(urls[0])
        self.assertIn("name=" + urllib.parse.quote(value, safe=""), parsed.query)
        self.assertEqual(urllib.parse.parse_qs(parsed.query)["name"], [value])

    def test_token_helper_is_single_open_read_and_trap_precedes_mktemp(self):
        source = SCRIPT.read_text()
        self.assertNotIn("validate_token", source)
        self.assertNotIn("json_quote", source)
        self.assertIn("O_NOFOLLOW", source)
        self.assertEqual(source.count("os.open(src, read_flags)"), 1)
        self.assertEqual(source.count("os.read(fd,"), 1)
        self.assertLess(source.index("trap cleanup EXIT"), source.index("tmp_dir=$(mktemp"))

    def test_rejects_insecure_or_malicious_token_files_without_leak(self):
        cases = (
            ("group-readable", API_TOKEN + "\n", 0o640),
            ("empty", b"", 0o600),
            ("second line", (API_TOKEN + "\nheader = injected\n").encode(), 0o600),
            ("carriage return", (API_TOKEN + "\r").encode(), 0o600),
            ("nul", API_TOKEN.encode() + b"\x00", 0o600),
            ("escape", API_TOKEN.encode() + b"\x1b", 0o600),
            ("too long", b"A" * 513, 0o600),
            ("option injection", b"A" * 20 + b";--config=/tmp/x", 0o600),
        )
        for label, value, mode in cases:
            with self.subTest(label=label):
                self.log.unlink(missing_ok=True)
                self.write_token(value, mode)
                proc = self.run_script("show")
                self.assertNotEqual(proc.returncode, 0)
                self.assertNotIn(API_TOKEN, proc.stdout + proc.stderr)
                self.assertEqual(self.records(), [])
                self.assert_cleaned()

    def test_rejects_non_regular_and_symlink_token_files(self):
        self.token_file.unlink()
        self.token_file.mkdir()
        proc = self.run_script("show")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.records(), [])
        self.assert_no_secrets_in_process_io(proc)
        self.assert_cleaned()

        self.token_file.rmdir()
        target = self.root / "real-token"
        target.write_text(API_TOKEN + "\n")
        target.chmod(0o600)
        self.token_file.symlink_to(target)
        proc = self.run_script("show")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.records(), [])
        self.assert_no_secrets_in_process_io(proc)
        self.assert_cleaned()

    def test_show_isolates_curl_configuration_and_proxy_environment(self):
        proc = self.run_script("show")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("tid-1 quoted healthy connections: 1", proc.stdout)
        records = self.records()
        self.assert_curl_isolated(records)
        self.assert_no_secrets_in_process_io(proc)
        self.assert_cleaned()

    def test_tunnel_and_dns_mock_paths_preserve_json_and_encoded_queries(self):
        name = 'tunnel &?/#=%+ "quoted"'
        host = 'host &?/#=%+ "quoted"'
        service = 'http://svc/"\\n'
        namespace = 'ns"\\n'
        proc = self.run_script(
            "tunnel",
            TUNNEL_NAME=name,
            HOSTNAME_FQDN=host,
            SERVICE_URL=service,
            NAMESPACE=namespace,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = self.records()
        bodies = [record["body"] for record in records if "body" in record]
        self.assertTrue(any(body.get("name") == name for body in bodies))
        ingress = next(body["config"]["ingress"] for body in bodies if "config" in body)
        self.assertEqual(ingress[0]["hostname"], host)
        self.assertEqual(ingress[0]["service"], service)
        self.assert_query_encoded(records, "/cfd_tunnel?name=", name)
        self.assert_secret_manifest(records, "create")
        self.assert_curl_isolated(records)
        self.assert_no_secrets_in_process_io(proc)
        self.assert_cleaned()

        self.log.unlink()
        proc = self.run_script(
            "dns",
            FAKE_TUNNEL_EXISTS="1",
            HOSTNAME_FQDN=host,
            NAMESPACE=namespace,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = self.records()
        body = next(record["body"] for record in records if "body" in record)
        self.assertEqual(body["name"], host)
        self.assertTrue(body["proxied"])
        self.assert_query_encoded(records, "/dns_records?name=", host)
        self.assert_curl_isolated(records)
        self.assert_no_secrets_in_process_io(proc)
        self.assert_cleaned()

    def test_existing_secret_uses_replace_without_apply(self):
        proc = self.run_script("tunnel", FAKE_SECRET_EXISTS="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = self.records()
        self.assert_secret_manifest(records, "replace")
        self.assert_curl_isolated(records)
        self.assert_no_secrets_in_process_io(proc)
        self.assert_cleaned()

    def test_cleanup_after_manifest_exists_then_create_or_replace_fails(self):
        for exists, verb in (("0", "create"), ("1", "replace")):
            with self.subTest(verb=verb):
                self.log.unlink(missing_ok=True)
                proc = self.run_script(
                    "tunnel",
                    FAKE_SECRET_EXISTS=exists,
                    FAKE_KUBECTL_APPLY_FAIL="1",
                )
                self.assertNotEqual(proc.returncode, 0)
                records = self.records()
                self.assert_secret_manifest(records, verb)
                self.assert_curl_isolated(records)
                self.assert_no_secrets_in_process_io(proc)
                self.assert_cleaned()

    def test_cleanup_after_curl_failure(self):
        proc = self.run_script("tunnel", FAKE_CURL_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assert_curl_isolated(self.records())
        self.assert_no_secrets_in_process_io(proc)
        self.assert_cleaned()


if __name__ == "__main__":
    unittest.main()
