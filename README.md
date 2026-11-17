# brother-scan

A small command-line tool for macOS that scans a stack of pages from a network scanner into one PDF. It can also rotate PDFs and send documents to a printer.

It uses the standard eSCL (AirScan) protocol, macOS PDFKit, and CUPS. It has no Python package dependencies, needs no API key, and does not use a driver from the printer maker.

The tool works well from a terminal or from a coding agent. Each successful command prints one result on stdout. Progress goes to stderr. The tool never retries a request that can move paper. It keeps every scanned page so that you can rebuild a PDF without scanning again. See [AGENTS.md](AGENTS.md) for the agent instructions.

## Why this exists

macOS can scan with Image Capture and Preview, but you cannot easily use them from a script. This tool gives you one command:

```sh
brother-scan scan --pages 8
```

It also contains a fix for a Brother problem. Some Brother models ignore eSCL scan settings and send a default 200 dpi image. They do this unless the request uses compact XML with no line breaks, a `text/xml; charset=utf-8` content type, and an `AirScan` user agent. This tool sends that exact request. It also reads the JPEG density of each page and stops if the scanner ignored the requested resolution.

## Tested hardware

| Model | Scan | Print | Notes |
| --- | --- | --- | --- |
| Brother MFC-J7500CDW | Single-sided A4 feeder, color, 300 dpi: verified on hardware | Queue check and `--dry-run` only | Feeder pages arrive upside down. Set `"rotate": 180` in the config file. |

Tested on an Apple Silicon Mac with macOS 26.

Duplex scanning, flatbed scanning, other paper sizes, and other resolutions are available. They were not tested on hardware. Other Brother models and other eSCL scanners will probably work, but they are not tested. If you try a different model, please open an issue or a pull request with the results. Include the model, the commands, and what worked.

## Requirements

- macOS
- Python 3.10 or later
- Swift command line tools (`xcode-select --install`)
- A scanner that supports eSCL (AirScan) on the local network
- For printing: a printer queue that is installed in macOS

## Install

```sh
git clone https://github.com/architpai/brother-scan.git
cd brother-scan
chmod +x brother_scan.py
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/brother_scan.py" "$HOME/.local/bin/brother-scan"
```

Make sure that `~/.local/bin` is in your `PATH`. Keep `brother_scan.py` and `pdf.swift` in the same directory. You can also run `python3 /path/to/brother_scan.py` directly.

## Find your scanner

```sh
brother-scan discover
brother-scan status
```

`discover` uses Bonjour to list the eSCL scanners on your network. If you do not set a host, `status` and `scan` use discovery automatically. If discovery finds more than one scanner, the command stops and lists them. It never selects one for you.

`status` prints the model, the scanner state, and the feeder state. It does not start a scan.

To skip discovery, set the scanner address in one of these places. The first one found is used:

1. The `--host` option, for example `--host 192.168.1.40`
2. The `BROTHER_SCANNER_HOST` environment variable
3. `"host"` in the config file
4. Bonjour discovery

## Config file

The tool reads `~/.config/brother-scan/config.json`. If `XDG_CONFIG_HOME` is set, it reads `$XDG_CONFIG_HOME/brother-scan/config.json`. The `BROTHER_SCAN_CONFIG` variable can give a different path. All keys are optional:

```json
{
  "host": "192.168.1.40",
  "printer": "Office_Printer",
  "rotate": 180
}
```

- `host`: the scanner IP address, hostname, or eSCL URL
- `printer`: the CUPS queue name. Run `lpstat -p` to list the queues.
- `rotate`: the default clockwise rotation for scanned pages: 0, 90, 180, or 270

Command options always override the config file. An unknown key or a bad value stops the command with an error.

## Scan a stack into one PDF

1. Put the pages in the document feeder, face up, top edge first.
2. Adjust the paper guides.
3. Run the scan with the number of PDF pages that you expect:

```sh
brother-scan scan --pages 8
```

The defaults are the document feeder, single-sided, A4, color, 300 dpi, and no rotation. The PDF goes into `~/Downloads`. The command prints the full path of the PDF on stdout.

`--pages` counts PDF pages, so duplex counts both sides. To scan eight double-sided sheets:

```sh
brother-scan scan --duplex --pages 16
```

Other options:

```sh
brother-scan scan --pages 8 --rotate 180
brother-scan scan --pages 8 --paper letter --dpi 600 --color Grayscale8 -o ~/Documents/contract.pdf
brother-scan scan --source flatbed --pages 1
```

If you do not know the page count, leave out `--pages`. The tool then scans the whole stack. Run `brother-scan scan --help` for all options.

## Rotate an existing PDF

```sh
brother-scan rotate ~/Downloads/scan.pdf --degrees 180 -o ~/Downloads/scan-fixed.pdf
```

This makes a new copy. The tool never overwrites an existing file.

## Print a document

```sh
brother-scan print ~/Downloads/scan.pdf
brother-scan print ~/Downloads/scan.pdf --copies 2 --duplex --paper letter
brother-scan print ~/Downloads/scan.pdf --duplex --dry-run
```

The tool selects the printer queue from these places, in this order: `--printer`, `BROTHER_PRINTER`, `"printer"` in the config file, and then the macOS default printer. If none of these gives a queue, the command stops and tells you how to select one.

The defaults are one copy, single-sided, and A4. Duplex uses long-edge binding. Add `--short-edge` for short-edge binding. `--dry-run` checks the file and the queue and prints the `lp` command, but it does not print.

A successful command prints the CUPS job receipt. This means that the job is in the queue. It does not mean that the paper has printed. If a print command fails, check the queue with `lpstat -o` before you try again. The first job may have been accepted.

## Recover a scan without scanning again

Each scan keeps its pages and a `session.json` file in `~/Downloads/.brother-scan/session-*`. The tool prints the session path before it starts. These folders contain your documents. Delete them when you no longer need them.

If the page count is wrong or the PDF step fails, the command exits with an error and keeps the pages. To rebuild the PDF:

```sh
brother-scan assemble ~/Downloads/.brother-scan/session-EXAMPLE -o ~/Downloads/rebuilt.pdf
```

The rebuild uses the saved page count and rotation. Add `--pages` or `--rotate` to change them. The tool never retries a failed request automatically, because a second request can move the next sheet through the feeder.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | The operation failed |
| 2 | The arguments are not correct |
| 130 | Interrupted |

## Development

```sh
python3 -m unittest -v
```

The tests use a fake eSCL scanner, fake `dns-sd`, `lp`, and `lpstat` commands, and generated images. They do not connect to a real scanner or printer.

## License

MIT. See [LICENSE](LICENSE).

Brother is a trademark of Brother Industries, Ltd. This project is not affiliated with Brother.
