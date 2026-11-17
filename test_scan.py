import contextlib
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import shlex
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import brother_scan as scanner


CAPABILITIES = f'''<scan:ScannerCapabilities xmlns:scan="{scanner.NS['scan']}" xmlns:pwg="{scanner.NS['pwg']}">
<pwg:MakeAndModel>Test Brother</pwg:MakeAndModel>
<scan:Adf><scan:AdfSimplexInputCaps>
<scan:MaxWidth>3507</scan:MaxWidth><scan:MaxHeight>5100</scan:MaxHeight>
<scan:SettingProfiles><scan:SettingProfile>
<scan:ColorModes><scan:ColorMode>RGB24</scan:ColorMode></scan:ColorModes>
<scan:SupportedResolutions><scan:DiscreteResolutions><scan:DiscreteResolution>
<scan:XResolution>300</scan:XResolution><scan:YResolution>300</scan:YResolution>
</scan:DiscreteResolution></scan:DiscreteResolutions></scan:SupportedResolutions>
</scan:SettingProfile></scan:SettingProfiles>
</scan:AdfSimplexInputCaps></scan:Adf></scan:ScannerCapabilities>'''.encode()


def isolate_environment(test, folder, **values):
    environment = patch.dict(os.environ, {"BROTHER_SCAN_CONFIG": str(folder / "config.json"), **values})
    environment.start()
    test.addCleanup(environment.stop)
    for name in ("BROTHER_SCANNER_HOST", "BROTHER_PRINTER", "XDG_CONFIG_HOME"):
        if name not in values:
            os.environ.pop(name, None)


FAKE_DNS_SD = '''import json, os, sys, time
arguments = sys.argv[1:]
with open(os.environ["BROTHER_TEST_CALLS"], "a") as output:
    output.write(json.dumps({"program": "dns-sd", "arguments": arguments}) + "\\n")
scanners = json.loads(os.environ["BROTHER_TEST_SCANNERS"])
if arguments[0] == "-B":
    print("Browsing for _uscan._tcp.local.")
    print("14:19:39.564  ...STARTING...")
    print("Timestamp     A/R    Flags  if Domain               Service Type         Instance Name")
    for name in scanners:
        for interface in (14, 15):  # The same scanner can appear on several interfaces.
            print(f"14:19:39.700  Add        2  {interface} local.               _uscan._tcp.         {name}")
else:
    host, port, resource = scanners[arguments[1]]
    escaped = arguments[1].replace(" ", "\\\\032")
    print(f"Lookup {arguments[1]}._uscan._tcp.local.")
    print(f"14:20:00.100  {escaped}._uscan._tcp.local. can be reached at {host}.:{port} (interface 14)")
    print(f" txtvers=1 ty=Test\\\\ Scanner rs={resource} vers=2.63")
sys.stdout.flush()
time.sleep(60)  # Like dns-sd, never exit without being stopped.
'''


def install_fakes(folder, programs):
    executables = folder / "bin"
    executables.mkdir(exist_ok=True)
    for name, program in programs.items():
        executable = executables / name
        executable.write_text(f"#!{sys.executable}\n" + program)
        executable.chmod(0o755)
    return str(executables) + os.pathsep + os.environ.get("PATH", "")


@contextlib.contextmanager
def fake_scanner(images, *, fail_at=None, loaded=True):
    calls = []
    remaining = iter(images)
    downloads = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body=b"", **headers):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            nonlocal downloads
            calls.append(("GET", self.path))
            if self.path == "/eSCL/ScannerCapabilities":
                self.reply(200, CAPABILITIES)
            elif self.path == "/eSCL/ScannerStatus":
                feeder = "ScannerAdfLoaded" if loaded else "ScannerAdfEmpty"
                self.reply(200, f'<scan:ScannerStatus xmlns:scan="{scanner.NS["scan"]}" '
                           f'xmlns:pwg="{scanner.NS["pwg"]}"><pwg:State>Idle</pwg:State>'
                           f'<scan:AdfState>{feeder}</scan:AdfState></scan:ScannerStatus>'.encode())
            elif self.path == "/eSCL/ScanJobs/test/NextDocument":
                downloads += 1
                if downloads == fail_at:
                    self.reply(500)
                else:
                    image = next(remaining, None)
                    self.reply(200, image, **{"Content-Type": "image/jpeg"}) if image else self.reply(404)
            else:
                self.reply(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(("POST", self.path, dict(self.headers), body))
            self.reply(201, Location="/eSCL/ScanJobs/test")

        def do_DELETE(self):
            calls.append(("DELETE", self.path))
            self.reply(200)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@unittest.skipUnless(sys.platform == "darwin", "PDFKit requires macOS")
class ScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory(prefix="brother-test-fixtures-")
        folder = Path(cls.fixtures.name)
        ppm = folder / "sample.ppm"
        # Asymmetric page dimensions and colors make image conversion observable.
        ppm.write_bytes(b"P6\n20 30\n255\n" + b"\xff\x00\x00" * 200 + b"\xff\xff\xff" * 400)
        cls.images = {}
        for dpi in (200, 300):
            jpg = folder / f"sample-{dpi}.jpg"
            subprocess.run(["sips", "-s", "format", "jpeg", "-s", "dpiWidth", str(dpi),
                            "-s", "dpiHeight", str(dpi), str(ppm), "--out", str(jpg)],
                           check=True, capture_output=True)
            data = bytearray(jpg.read_bytes())
            # sips sets EXIF DPI but leaves JFIF units at 0. The Brother supplies
            # JFIF units 1 (dots per inch), so use that same wire format here.
            assert data[6:11] == b"JFIF\x00"
            data[13] = 1
            cls.images[dpi] = bytes(data)
        cls.inspector = folder / "inspect.swift"
        cls.inspector.write_text('''import Foundation
import PDFKit
let pdf = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1]))!
let pages = (0..<pdf.pageCount).map { pdf.page(at: $0)! }
let result: [String: Any] = ["pages": pdf.pageCount,
    "rotations": pages.map { $0.rotation },
    "widths": pages.map { $0.bounds(for: .mediaBox).width },
    "heights": pages.map { $0.bounds(for: .mediaBox).height }]
print(String(data: try JSONSerialization.data(withJSONObject: result), encoding: .utf8)!)
''')

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="brother-test-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.output = self.folder / "result.pdf"
        isolate_environment(self, self.folder)

    def run_cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["brother-scan", *map(str, arguments)]), \
                patch.object(Path, "home", return_value=self.folder), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = scanner.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def inspect(self, path):
        result = subprocess.run(["swift", str(self.inspector), str(path)],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def sessions(self):
        return list((self.folder / "Downloads" / ".brother-scan").glob("session-*"))

    def test_status_never_creates_a_job(self):
        with fake_scanner([]) as (host, calls):
            code, stdout, _ = self.run_cli("status", "--host", host)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["feeder"], "ScannerAdfLoaded")
        self.assertTrue(all(call[0] == "GET" for call in calls))

    def test_scan_collects_pages_with_exact_request_format(self):
        with fake_scanner([self.images[300]] * 2) as (host, calls):
            code, stdout, _ = self.run_cli("scan", "--host", host, "--pages", 2, "-o", self.output)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), str(self.output.resolve()))
        info = self.inspect(self.output)
        self.assertEqual(info["pages"], 2)
        self.assertEqual(info["rotations"], [0, 0])
        # 20 pixels at 300 dpi should remain 4.8 PDF points wide.
        self.assertAlmostEqual(info["widths"][0], 4.8, places=2)
        post = [call for call in calls if call[0] == "POST"]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0][2]["Content-Type"], "text/xml; charset=utf-8")
        self.assertEqual(post[0][2]["User-Agent"], "AirScan")
        self.assertNotIn(b"\n", post[0][3])
        self.assertNotIn(b"<?xml", post[0][3])
        self.assertIn(b"<pwg:InputSource>Feeder</pwg:InputSource>", post[0][3])
        self.assertIn(b"<scan:XResolution>300</scan:XResolution>", post[0][3])
        self.assertIn(b"<scan:Duplex>false</scan:Duplex>", post[0][3])

    def test_rotation_comes_from_config_unless_the_command_overrides_it(self):
        (self.folder / "config.json").write_text(json.dumps({"rotate": 180}))
        with fake_scanner([self.images[300]] * 2) as (host, _):
            code, _, _ = self.run_cli("scan", "--host", host, "--pages", 2, "-o", self.output)
        self.assertEqual(code, 0)
        self.assertEqual(self.inspect(self.output)["rotations"], [180, 180])
        session, = self.sessions()
        self.assertEqual(json.loads((session / "session.json").read_text())["rotation"], 180)
        override = self.folder / "override.pdf"
        with fake_scanner([self.images[300]]) as (host, _):
            code, _, _ = self.run_cli("scan", "--host", host, "--rotate", 90, "-o", override)
        self.assertEqual(code, 0)
        self.assertEqual(self.inspect(override)["rotations"], [90])

    def test_count_mismatch_preserves_pages_for_offline_assembly(self):
        with fake_scanner([self.images[300]] * 2) as (host, _):
            code, _, _ = self.run_cli("scan", "--host", host, "--pages", 3, "-o", self.output)
        self.assertEqual(code, 1)
        self.assertFalse(self.output.exists())
        session, = self.sessions()
        self.assertEqual(len(list(session.glob("*.jpg"))), 2)
        code, _, _ = self.run_cli("assemble", session, "--pages", 2, "-o", self.output)
        self.assertEqual(code, 0)
        self.assertEqual(self.inspect(self.output)["pages"], 2)
        rotated = self.folder / "rotated.pdf"
        code, _, _ = self.run_cli("rotate", self.output, "--degrees", 180, "-o", rotated)
        self.assertEqual(code, 0)
        self.assertEqual(self.inspect(rotated)["rotations"], [180, 180])
        self.assertEqual(self.inspect(self.output)["rotations"], [0, 0])

    def test_ignored_resolution_stops_after_first_page_and_keeps_it(self):
        with fake_scanner([self.images[200]] * 2) as (host, calls):
            code, _, stderr = self.run_cli("scan", "--host", host, "-o", self.output)
        self.assertEqual(code, 1)
        self.assertIn("settings were ignored", stderr)
        self.assertFalse(self.output.exists())
        session, = self.sessions()
        self.assertEqual(len(list(session.glob("*.jpg"))), 1)
        self.assertEqual(sum(call[1].endswith("/NextDocument") for call in calls), 1)
        self.assertEqual(sum(call[0] == "DELETE" for call in calls), 1)

    def test_download_error_keeps_completed_pages_and_does_not_retry(self):
        with fake_scanner([self.images[300]] * 2, fail_at=2) as (host, calls):
            code, _, _ = self.run_cli("scan", "--host", host, "-o", self.output)
        self.assertEqual(code, 1)
        self.assertFalse(self.output.exists())
        session, = self.sessions()
        self.assertEqual(len(list(session.glob("*.jpg"))), 1)
        self.assertEqual(sum(call[0] == "POST" for call in calls), 1)
        self.assertEqual(sum(call[1].endswith("/NextDocument") for call in calls), 2)

    def test_existing_output_and_empty_feeder_do_not_start_scans(self):
        self.output.write_bytes(b"keep this file")
        with fake_scanner([]) as (host, calls):
            code, _, _ = self.run_cli("scan", "--host", host, "-o", self.output)
        self.assertEqual(code, 1)
        self.assertEqual(self.output.read_bytes(), b"keep this file")
        self.assertEqual(calls, [])
        with fake_scanner([], loaded=False) as (host, calls):
            code, _, _ = self.run_cli("scan", "--host", host, "-o", self.folder / "new.pdf")
        self.assertEqual(code, 1)
        self.assertFalse(any(call[0] == "POST" for call in calls))


class PrinterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="brother-print-test-")
        self.folder = Path(self.temporary.name)
        self.document = self.folder / "invoice's copy.pdf"
        self.document.write_bytes(b"%PDF-1.4\n%%EOF\n")
        self.call_log = self.folder / "calls.jsonl"
        program = '''import json, os, sys
from pathlib import Path
program = Path(sys.argv[0]).name
arguments = sys.argv[1:]
with open(os.environ["BROTHER_TEST_CALLS"], "a") as output:
    output.write(json.dumps({"program": program, "arguments": arguments}) + "\\n")
if program == "lpstat" and arguments == ["-d"]:
    default = os.environ.get("BROTHER_TEST_DEFAULT_PRINTER")
    print(f"system default destination: {default}" if default else "no system default destination")
elif program == "lpstat":
    queue = arguments[arguments.index("-p") + 1]
    if queue == "Missing":
        print("lpstat: Unknown destination Missing", file=sys.stderr)
        sys.exit(1)
    print(f"printer {queue} is idle")
else:
    if os.environ.get("BROTHER_TEST_PRINT_FAIL"):
        print("lp: The printer is not accepting jobs", file=sys.stderr)
        sys.exit(1)
    queue = arguments[arguments.index("-d") + 1]
    print(f"request id is {queue}-42 (1 file(s))")
'''
        self.addCleanup(self.temporary.cleanup)
        isolate_environment(self, self.folder,
                            PATH=install_fakes(self.folder, {"lp": program, "lpstat": program}),
                            BROTHER_TEST_CALLS=str(self.call_log), BROTHER_TEST_PRINT_FAIL="",
                            BROTHER_TEST_DEFAULT_PRINTER="OfficeDefault")

    def run_cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["brother-scan", *map(str, arguments)]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = scanner.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def calls(self, program=None):
        calls = [json.loads(line) for line in self.call_log.read_text().splitlines()] if self.call_log.exists() else []
        return [call for call in calls if not program or call["program"] == program]

    def test_print_submits_one_job_with_requested_settings_and_intact_filename(self):
        code, stdout, _ = self.run_cli("print", self.document, "--printer", "TestBrother",
                                     "--copies", 2, "--duplex", "--paper", "letter")
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "request id is TestBrother-42 (1 file(s))")
        submission, = self.calls("lp")
        arguments = submission["arguments"]
        self.assertEqual(arguments[arguments.index("-d") + 1], "TestBrother")
        self.assertEqual(arguments[arguments.index("-n") + 1], "2")
        self.assertIn("media=Letter", arguments)
        self.assertIn("sides=two-sided-long-edge", arguments)
        self.assertEqual(arguments[-1], str(self.document.resolve()))

    def test_defaults_and_short_edge_binding(self):
        code, _, _ = self.run_cli("print", self.document)
        self.assertEqual(code, 0)
        submission, = self.calls("lp")
        arguments = submission["arguments"]
        self.assertEqual(arguments[arguments.index("-d") + 1], "OfficeDefault")
        self.assertEqual(arguments[arguments.index("-n") + 1], "1")
        self.assertIn("media=A4", arguments)
        self.assertIn("sides=one-sided", arguments)
        with patch.dict(os.environ, {"BROTHER_PRINTER": "EnvQueue"}):
            code, stdout, _ = self.run_cli("print", self.document, "--duplex", "--short-edge", "--paper", "a3")
        self.assertEqual(code, 0)
        self.assertIn("EnvQueue-42", stdout)
        self.assertIn("sides=two-sided-short-edge", self.calls("lp")[-1]["arguments"])
        self.assertIn("media=A3", self.calls("lp")[-1]["arguments"])

    def queue_used(self, *arguments):
        code, stdout, stderr = self.run_cli("print", self.document, "--dry-run", *arguments)
        self.assertEqual(code, 0, stderr)
        command = shlex.split(stdout)
        return command[command.index("-d") + 1]

    def test_queue_precedence_is_option_environment_config_then_system_default(self):
        self.assertEqual(self.queue_used(), "OfficeDefault")
        (self.folder / "config.json").write_text(json.dumps({"printer": "ConfigQueue"}))
        self.assertEqual(self.queue_used(), "ConfigQueue")
        with patch.dict(os.environ, {"BROTHER_PRINTER": "EnvQueue"}):
            self.assertEqual(self.queue_used(), "EnvQueue")
            self.assertEqual(self.queue_used("--printer", "OptionQueue"), "OptionQueue")
        self.assertEqual(self.calls("lp"), [])

    def test_missing_system_default_stops_before_submission(self):
        with patch.dict(os.environ, {"BROTHER_TEST_DEFAULT_PRINTER": ""}):
            code, stdout, stderr = self.run_cli("print", self.document)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("no default printer", stderr)
        self.assertIn("lpstat -p", stderr)
        self.assertEqual(self.calls("lp"), [])

    def test_dry_run_checks_queue_and_does_not_submit(self):
        code, stdout, _ = self.run_cli("print", self.document, "--copies", 3, "--duplex", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual([call["arguments"][0] for call in self.calls("lpstat")], ["-d", "-p"])
        self.assertEqual(self.calls("lp"), [])
        arguments = shlex.split(stdout)
        self.assertEqual(Path(arguments[0]).name, "lp")
        self.assertEqual(arguments[-1], str(self.document.resolve()))
        self.assertIn("sides=two-sided-long-edge", arguments)

    def test_invalid_requests_do_not_submit(self):
        for arguments, message in [
            (["print", self.folder / "missing.pdf"], "Document does not exist"),
            (["print", self.folder], "not a file"),
            (["print", self.document, "--short-edge"], "requires --duplex"),
            (["print", self.document, "--printer", "Missing"], "Unknown destination"),
        ]:
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.run_cli(*arguments)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn(message, stderr)
                self.assertEqual(self.calls("lp"), [])
        with self.assertRaises(SystemExit) as error:
            self.run_cli("print", self.document, "--copies", 0)
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.calls("lp"), [])

    def test_submission_error_is_reported_without_retry(self):
        with patch.dict(os.environ, {"BROTHER_TEST_PRINT_FAIL": "1"}):
            code, stdout, stderr = self.run_cli("print", self.document)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("The printer is not accepting jobs", stderr)
        self.assertEqual(len(self.calls("lp")), 1)


class DiscoveryAndConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="brother-discovery-test-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.call_log = self.folder / "calls.jsonl"
        isolate_environment(self, self.folder, PATH=install_fakes(self.folder, {"dns-sd": FAKE_DNS_SD}),
                            BROTHER_TEST_CALLS=str(self.call_log), BROTHER_TEST_SCANNERS="{}")
        timing = patch.object(scanner, "DISCOVERY_SECONDS", 1.0)
        timing.start()
        self.addCleanup(timing.stop)

    def advertise(self, scanners):
        os.environ["BROTHER_TEST_SCANNERS"] = json.dumps(scanners)

    def run_cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["brother-scan", *map(str, arguments)]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = scanner.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def lookups(self):
        lines = self.call_log.read_text().splitlines() if self.call_log.exists() else []
        return [json.loads(line)["arguments"] for line in lines]

    def test_one_advertised_scanner_is_used_and_listed(self):
        with fake_scanner([]) as (host, calls):
            port = host.rsplit(":", 1)[1]
            self.advertise({"Brother Test": ["localhost", port, "eSCL"]})
            code, stdout, stderr = self.run_cli("status")
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["model"], "Test Brother")
            self.assertIn("Using Brother Test", stderr)
            code, stdout, _ = self.run_cli("discover")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), [{"name": "Brother Test", "url": f"http://localhost:{port}/eSCL/"}])
        # Duplicate interface records are looked up only once.
        self.assertEqual(sum(arguments[0] == "-L" for arguments in self.lookups()), 2)
        self.assertTrue(all(call[0] == "GET" for call in calls))

    def test_no_scanner_or_several_scanners_stop_without_a_request(self):
        code, _, stderr = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("No eSCL scanner was found", stderr)
        self.advertise({"Upstairs": ["upstairs.example", "80", "eSCL"],
                        "Downstairs": ["downstairs.example", "8080", "/escl"]})
        code, _, stderr = self.run_cli("scan", "--pages", 1, "-o", self.folder / "unused.pdf")
        self.assertEqual(code, 1)
        self.assertIn("Found 2 scanners", stderr)
        self.assertIn("Upstairs: http://upstairs.example:80/eSCL/", stderr)
        self.assertIn("Downstairs: http://downstairs.example:8080/escl/", stderr)

    def test_explicit_host_from_option_environment_or_config_skips_discovery(self):
        with fake_scanner([]) as (host, _):
            self.assertEqual(self.run_cli("status", "--host", host)[0], 0)
            with patch.dict(os.environ, {"BROTHER_SCANNER_HOST": host}):
                self.assertEqual(self.run_cli("status")[0], 0)
            (self.folder / "config.json").write_text(json.dumps({"host": host}))
            self.assertEqual(self.run_cli("status")[0], 0)
        self.assertEqual(self.lookups(), [])

    def test_invalid_config_is_rejected(self):
        config = self.folder / "config.json"
        for content, message in [("{not json", "Invalid config file"),
                                 ('{"hots": "10.0.0.2"}', "Unknown config key"),
                                 ('{"rotate": 45}', "must be one of"),
                                 ('{"rotate": true}', "must be a non-empty int"),
                                 ('["host"]', "JSON object")]:
            with self.subTest(content=content):
                config.write_text(content)
                code, _, stderr = self.run_cli("status", "--host", "127.0.0.1:9")
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)

    def test_scanner_url_keeps_explicit_paths(self):
        self.assertEqual(scanner.scanner_url("10.0.0.5"), "http://10.0.0.5/eSCL/")
        self.assertEqual(scanner.scanner_url("http://10.0.0.5:8080/eSCL"), "http://10.0.0.5:8080/eSCL/")
        self.assertEqual(scanner.scanner_url("https://scanner.local/escl/"), "https://scanner.local/escl/")
        with self.assertRaises(ValueError):
            scanner.scanner_url("http://user:secret@10.0.0.5")


if __name__ == "__main__":
    unittest.main()
