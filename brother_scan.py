#!/usr/bin/env python3
"""Scan, print, and manage documents with a Brother or other eSCL multifunction printer."""

import argparse
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import shlex
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent
NS = {"pwg": "http://www.pwg.org/schemas/2010/12/sm",
      "scan": "http://schemas.hp.com/imaging/escl/2011/05/03"}
PAPERS = {"a4": (2480, 3508), "a3": (3507, 4961),
          "letter": (2550, 3300), "legal": (2550, 4200)}
ROTATIONS = [0, 90, 180, 270]
DISCOVERY_SECONDS = 3.0
CONFIG_KEYS = {"host": str, "printer": str, "rotate": int}


def log(message):
    print(message, file=sys.stderr, flush=True)


def config_path():
    if os.environ.get("BROTHER_SCAN_CONFIG"):
        return Path(os.environ["BROTHER_SCAN_CONFIG"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base).expanduser() / "brother-scan" / "config.json"


def load_config():
    path = config_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise ValueError(f"Invalid config file {path}: {error}") from None
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a JSON object")
    for key, value in data.items():
        if key not in CONFIG_KEYS:
            raise ValueError(f"Unknown config key {key!r} in {path}; use {', '.join(CONFIG_KEYS)}")
        if not isinstance(value, CONFIG_KEYS[key]) or isinstance(value, bool) or value in ("", None):
            raise ValueError(f"Config key {key!r} in {path} must be a non-empty {CONFIG_KEYS[key].__name__}")
    if data.get("rotate", 0) not in ROTATIONS:
        raise ValueError(f"Config key 'rotate' in {path} must be one of {ROTATIONS}")
    return data


def scanner_url(host):
    url = host if "://" in host else "http://" + host
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("--host must be an IP address or an HTTP(S) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("--host cannot include credentials, a query, or a fragment")
    # A bare address uses the standard eSCL path. An explicit path is kept as given.
    return url.rstrip("/") + ("/" if parts.path.strip("/") else "/eSCL/")


def run_dns_sd(arguments, seconds, done=None):
    """Run dns-sd, which never exits by itself, and return its output lines."""
    tool = shutil.which("dns-sd")
    if not tool:
        raise ValueError("Scanner discovery requires the macOS dns-sd command; use --host instead")
    # dns-sd buffers output that goes to a pipe, so give it a terminal.
    controller, terminal = pty.openpty()
    try:
        process = subprocess.Popen([tool, *arguments], stdin=subprocess.DEVNULL,
                                   stdout=terminal, stderr=subprocess.DEVNULL)
    finally:
        os.close(terminal)
    output = b""
    deadline = time.monotonic() + seconds
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            if not select.select([controller], [], [], remaining)[0]:
                break
            try:
                chunk = os.read(controller, 4096)
            except OSError:  # The terminal closes when dns-sd exits.
                break
            if not chunk:
                break
            output += chunk
            if done and done(output.decode(errors="replace")):
                break
    finally:
        process.kill()
        process.wait()
        os.close(controller)
    return output.decode(errors="replace").splitlines()


def discover(seconds=None):
    """Find eSCL scanners that advertise _uscan._tcp through Bonjour."""
    seconds = DISCOVERY_SECONDS if seconds is None else seconds
    names = []
    for line in run_dns_sd(["-B", "_uscan._tcp", "local."], seconds):
        match = re.search(r"\sAdd\s+\d+\s+\d+\s+\S+\s+_uscan\._tcp\.\s+(.+?)\s*$", line)
        if match and match[1] not in names:
            names.append(match[1])
    scanners = []
    for name in names:
        text = "\n".join(run_dns_sd(["-L", name, "_uscan._tcp", "local."], seconds,
                                    done=lambda out: re.search(r"can be reached at[^\n]*\n[^\n]*\n", out)))
        reached = re.search(r"can be reached at (\S+?)\.?:(\d+)", text)
        if not reached:
            log(f"Could not resolve the address of {name}")
            continue
        resource = re.search(r"(?:^|\s)rs=(\S*)", text, re.M)
        path = (resource[1].strip("/") if resource else "") or "eSCL"
        scanners.append({"name": name, "url": f"http://{reached[1]}:{reached[2]}/{path}/"})
    return scanners


def resolve_scanner(args, config):
    host = args.host or os.environ.get("BROTHER_SCANNER_HOST") or config.get("host")
    if host:
        return scanner_url(host)
    log("No scanner host is set; searching the local network")
    scanners = discover()
    if len(scanners) == 1:
        log(f'Using {scanners[0]["name"]} at {scanners[0]["url"]}')
        return scanner_url(scanners[0]["url"])
    found = "".join(f'\n  {item["name"]}: {item["url"]}' for item in scanners)
    if not scanners:
        raise ValueError("No eSCL scanner was found on the local network. Use --host ADDRESS, "
                         f"set BROTHER_SCANNER_HOST, or add \"host\" to {config_path()}")
    raise ValueError(f"Found {len(scanners)} scanners. Select one with --host:{found}")


def request(url, *, data=None, method=None, timeout=120):
    headers = {"User-Agent": "AirScan"}
    if data is not None:
        headers["Content-Type"] = "text/xml; charset=utf-8"
    return urllib.request.urlopen(
        urllib.request.Request(url, data=data, method=method, headers=headers),
        timeout=timeout,
    )


def read_device(base, timeout):
    with request(base + "ScannerCapabilities", timeout=timeout) as response:
        caps = ET.fromstring(response.read())
    with request(base + "ScannerStatus", timeout=timeout) as response:
        status = ET.fromstring(response.read())
    summary = {"url": base, "model": caps.findtext("pwg:MakeAndModel", namespaces=NS),
               "state": status.findtext("pwg:State", namespaces=NS),
               "feeder": status.findtext("scan:AdfState", namespaces=NS)}
    return caps, summary


def scan_settings(args):
    width, height = PAPERS[args.paper]
    source = "Feeder" if args.source == "feeder" else "Platen"
    # Keep the exact compact XML and HTTP headers that worked on this Brother.
    # Earlier multiline requests returned a default 200 dpi scan instead.
    return (f'<scan:ScanSettings xmlns:scan="{NS["scan"]}" xmlns:pwg="{NS["pwg"]}">'
            '<pwg:Version>2.0</pwg:Version><scan:Intent>Document</scan:Intent>'
            '<pwg:ScanRegions><pwg:ScanRegion>'
            '<pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>'
            '<pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>'
            f'<pwg:Width>{width}</pwg:Width><pwg:Height>{height}</pwg:Height>'
            '</pwg:ScanRegion></pwg:ScanRegions>'
            f'<pwg:InputSource>{source}</pwg:InputSource>'
            '<scan:CompressionFactor>1</scan:CompressionFactor>'
            f'<scan:ColorMode>{args.color}</scan:ColorMode>'
            '<pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>'
            '<scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>'
            f'<scan:XResolution>{args.dpi}</scan:XResolution>'
            f'<scan:YResolution>{args.dpi}</scan:YResolution>'
            f'<scan:Duplex>{str(args.duplex).lower()}</scan:Duplex>'
            '</scan:ScanSettings>').encode()


def validate_device(caps, status, args):
    if status["state"] != "Idle":
        raise ValueError(f'Scanner is {status["state"]}; wait until it is idle')
    if args.source == "feeder":
        if status["feeder"] != "ScannerAdfLoaded":
            raise ValueError(f'Load the document feeder first (state: {status["feeder"]})')
        kind = "AdfDuplexInputCaps" if args.duplex else "AdfSimplexInputCaps"
        source = caps.find(f"scan:Adf/scan:{kind}", NS)
    else:
        source = caps.find("scan:Platen/scan:PlatenInputCaps", NS)
    if source is None:
        raise ValueError("The scanner does not advertise the selected input mode")
    resolutions = source.findall(".//scan:DiscreteResolution", NS)
    if resolutions and not any(
        r.findtext("scan:XResolution", namespaces=NS) == str(args.dpi)
        and r.findtext("scan:YResolution", namespaces=NS) == str(args.dpi)
        for r in resolutions
    ):
        raise ValueError(f"The selected input does not support {args.dpi} dpi")
    modes = [node.text for node in source.findall(".//scan:ColorMode", NS)]
    if args.color not in modes:
        raise ValueError("The selected input does not support this color mode")
    width, height = PAPERS[args.paper]
    if width > int(source.findtext("scan:MaxWidth", namespaces=NS)) or height > int(
        source.findtext("scan:MaxHeight", namespaces=NS)
    ):
        raise ValueError("The selected paper size exceeds this input's scan area")


def jpeg_dpi(path):
    with path.open("rb") as image:
        if image.read(2) != b"\xff\xd8":
            return None
        while True:
            marker = image.read(2)
            if len(marker) != 2 or marker[0] != 255 or marker[1] in (0xDA, 0xD9):
                return None
            size_bytes = image.read(2)
            if len(size_bytes) != 2:
                return None
            size = struct.unpack(">H", size_bytes)[0]
            if size < 2:
                return None
            segment = image.read(size - 2)
            if marker == b"\xff\xe0" and segment.startswith(b"JFIF\x00") and len(segment) >= 12:
                units = segment[7]
                x, y = struct.unpack(">HH", segment[8:12])
                factor = {1: 1, 2: 2.54}.get(units)
                return (x * factor, y * factor) if factor else None


def write_manifest(session, manifest):
    temporary = session / "session.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(session / "session.json")


def default_output(label="scan"):
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
    return Path.home() / "Downloads" / f"Brother-{label}-{stamp}.pdf"


def prepare_output(value, label="scan"):
    output = Path(value).expanduser().resolve() if value else default_output(label)
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def pdf_tool(*arguments):
    if sys.platform != "darwin" or not shutil.which("swift"):
        raise ValueError("PDF processing requires macOS and Swift command line tools")
    subprocess.run(["swift", str(ROOT / "pdf.swift"), *map(str, arguments)], check=True)


def publish_pdf(output, *arguments):
    with tempfile.TemporaryDirectory(prefix=".brother-pdf-", dir=output.parent) as directory:
        temporary = Path(directory) / "output.pdf"
        pdf_tool(*arguments, temporary)
        # link() publishes the finished PDF atomically and refuses to overwrite.
        os.link(temporary, output)
    print(output, flush=True)


def assemble(session, output, expected=None, rotation=None):
    manifest_path = session / "session.json"
    manifest = json.loads(manifest_path.read_text())
    files = [session / name for name in manifest["files"]]
    if not files:
        raise ValueError("No captured pages are available")
    expected = expected if expected is not None else manifest["expected_pages"]
    rotation = rotation if rotation is not None else manifest["rotation"]
    with tempfile.TemporaryDirectory(prefix="brother-assemble-") as directory:
        inputs = Path(directory) / "inputs.json"
        inputs.write_text(json.dumps({"files": [str(path) for path in files],
                                     "expected_pages": expected, "rotation": rotation}))
        publish_pdf(output, "assemble", inputs)


def capture(args, config):
    if args.duplex and args.source != "feeder":
        raise ValueError("--duplex requires --source feeder")
    if args.rotate is None:
        args.rotate = config.get("rotate", 0)
    output = prepare_output(args.output)
    pdf_tool("check")
    base = resolve_scanner(args, config)
    caps, status = read_device(base, args.timeout)
    validate_device(caps, status, args)
    archive = Path.home() / "Downloads" / ".brother-scan"
    archive.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="session-", dir=archive))
    settings = scan_settings(args)
    (session / "settings.xml").write_bytes(settings)
    manifest = {"url": base, "expected_pages": args.pages, "rotation": args.rotate,
                "dpi": args.dpi, "source": args.source, "duplex": args.duplex,
                "paper": args.paper, "files": []}
    write_manifest(session, manifest)
    log(f"Captured pages will be kept in {session}")
    job = None
    try:
        # Never retry job creation or document downloads: a retry can skip or rescan a page.
        with request(base + "ScanJobs", data=settings, timeout=args.timeout) as response:
            location = response.headers.get("Location")
            if response.status != 201 or not location:
                raise ValueError("Scanner did not return a scan job location")
        candidate = urljoin(base + "ScanJobs", location)
        if urlsplit(candidate).netloc != urlsplit(base).netloc:
            raise ValueError("Scanner returned a job on a different host")
        job = candidate
        manifest["job"] = job
        write_manifest(session, manifest)
        for index in range(1, 501):
            raw = session / f"page-{index:04d}.raw"
            try:
                with request(job.rstrip("/") + "/NextDocument", timeout=args.timeout) as response:
                    with raw.open("xb") as destination:
                        shutil.copyfileobj(response, destination)
            except urllib.error.HTTPError as error:
                if error.code in (404, 410):
                    break
                raise
            with raw.open("rb") as source:
                signature = source.read(1024)
            if signature.startswith(b"\xff\xd8"):
                page = raw.rename(raw.with_suffix(".jpg"))
            elif b"%PDF-" in signature:
                page = raw.rename(raw.with_suffix(".pdf"))
            else:
                raise ValueError(f"Unexpected scan response; retained at {raw}")
            manifest["files"].append(page.name)
            write_manifest(session, manifest)
            if page.suffix == ".jpg":
                density = jpeg_dpi(page)
                if density and any(abs(value - args.dpi) > 1 for value in density):
                    raise ValueError(f"Scanner returned {density} dpi instead of {args.dpi}; settings were ignored")
            log(f"Saved document {index}" + (f" (expected {args.pages} pages)" if args.pages else ""))
        else:
            raise ValueError("Stopped at the limit of 500 documents")
        assemble(session, output)
    except BaseException:
        if job:
            try:
                with request(job, method="DELETE", timeout=10):
                    pass
            except (OSError, ValueError, http.client.HTTPException) as error:
                log(f"Could not cancel scan job: {error}")
        log(f"Captured files are retained. Rebuild without rescanning:\n"
            f"  brother-scan assemble {session}")
        raise


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def resolve_printer(args, config, lpstat):
    queue = args.printer or os.environ.get("BROTHER_PRINTER") or config.get("printer")
    if queue:
        return queue
    default = subprocess.run([lpstat, "-d"], capture_output=True, text=True)
    match = re.search(r"system default destination:\s*(\S+)", default.stdout)
    if match:
        return match[1]
    raise ValueError("No printer queue is selected and the system has no default printer. "
                     f"Use --printer QUEUE_NAME, set BROTHER_PRINTER, or add \"printer\" to {config_path()}. "
                     "List queues with lpstat -p")


def print_document(args, config):
    document = args.input.expanduser().resolve()
    if not document.is_file():
        raise ValueError(f"Document does not exist or is not a file: {document}")
    if args.short_edge and not args.duplex:
        raise ValueError("--short-edge requires --duplex")
    lp = shutil.which("lp")
    lpstat = shutil.which("lpstat")
    if not lp or not lpstat:
        raise ValueError("Printing requires the CUPS lp and lpstat commands")
    printer = resolve_printer(args, config, lpstat)
    queue = subprocess.run([lpstat, "-p", printer], capture_output=True, text=True)
    if queue.returncode:
        raise ValueError(queue.stderr.strip() or f"Printer queue is unavailable: {printer}")
    sides = "two-sided-short-edge" if args.short_edge else "two-sided-long-edge"
    if not args.duplex:
        sides = "one-sided"
    command = [lp, "-d", printer, "-n", str(args.copies),
               "-o", f"media={args.paper.capitalize()}", "-o", f"sides={sides}", str(document)]
    if args.dry_run:
        print(shlex.join(command), flush=True)
        return
    # A failed submission may already have created a job. Do not retry it.
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Print submission failed; check the queue before retrying")
    print(result.stdout.strip(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=f"Agent instructions: {ROOT / 'AGENTS.md'}\n"
                                            f"Config file: {config_path()}")
    sub = parser.add_subparsers(dest="command", required=True)
    network = argparse.ArgumentParser(add_help=False)
    network.add_argument("--host", help="scanner IP or URL (env: BROTHER_SCANNER_HOST; "
                                         "default: config file, then Bonjour discovery)")
    network.add_argument("--timeout", type=positive_int, default=120, help="socket timeout in seconds (default: 120)")
    sub.add_parser("discover", help="list eSCL scanners on the local network")
    sub.add_parser("status", parents=[network], help="read scanner status without scanning")
    scan = sub.add_parser("scan", parents=[network], help="scan loaded pages into one PDF")
    scan.add_argument("--pages", type=positive_int, help="expected PDF pages; duplex counts both sides")
    scan.add_argument("--source", choices=["feeder", "flatbed"], default="feeder")
    scan.add_argument("--duplex", action="store_true", help="scan both sides of each feeder sheet")
    scan.add_argument("--paper", choices=PAPERS, default="a4")
    scan.add_argument("--dpi", type=int, choices=[100, 200, 300, 600], default=300)
    scan.add_argument("--color", choices=["RGB24", "Grayscale8", "BlackAndWhite1"], default="RGB24")
    scan.add_argument("--rotate", type=int, choices=ROTATIONS,
                      help="clockwise correction for every page (default: config file, then 0)")
    scan.add_argument("-o", "--output", help="PDF path (default: timestamped PDF in Downloads)")
    rebuild = sub.add_parser("assemble", help="rebuild a saved session without using the scanner")
    rebuild.add_argument("session", type=Path)
    rebuild.add_argument("--pages", type=positive_int, help="override expected page count")
    rebuild.add_argument("--rotate", type=int, choices=ROTATIONS, help="override saved rotation")
    rebuild.add_argument("-o", "--output")
    rotate = sub.add_parser("rotate", help="rotate an existing PDF into a new copy")
    rotate.add_argument("input", type=Path)
    rotate.add_argument("--degrees", type=int, choices=ROTATIONS, required=True, help="clockwise rotation")
    rotate.add_argument("-o", "--output")
    printing = sub.add_parser("print", help="submit a document to a configured printer queue")
    printing.add_argument("input", type=Path, help="document path, such as a PDF or image")
    printing.add_argument("--printer", help="CUPS queue name (env: BROTHER_PRINTER; "
                                            "default: config file, then the system default printer)")
    printing.add_argument("--copies", type=positive_int, default=1)
    printing.add_argument("--duplex", action="store_true", help="print both sides, with long-edge binding")
    printing.add_argument("--short-edge", action="store_true", help="use short-edge binding with --duplex")
    printing.add_argument("--paper", choices=PAPERS, default="a4")
    printing.add_argument("--dry-run", action="store_true", help="check the file and queue, then show the command without printing")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = load_config()
        if args.command == "discover":
            print(json.dumps(discover(), indent=2))
        elif args.command == "status":
            _, status = read_device(resolve_scanner(args, config), args.timeout)
            print(json.dumps(status, indent=2))
        elif args.command == "scan":
            capture(args, config)
        elif args.command == "assemble":
            assemble(args.session.expanduser().resolve(), prepare_output(args.output), args.pages, args.rotate)
        elif args.command == "print":
            print_document(args, config)
        else:
            publish_pdf(prepare_output(args.output, "rotated"), "rotate", args.input.expanduser().resolve(), args.degrees)
    except KeyboardInterrupt:
        log("Interrupted. No operation was restarted. Check the printer queue before repeating a print submission.")
        return 130
    except (OSError, ValueError, KeyError, ET.ParseError, http.client.HTTPException,
            subprocess.CalledProcessError) as error:
        log(f"Error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
