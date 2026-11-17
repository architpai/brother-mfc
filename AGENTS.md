# Use brother-mfc to scan and print

These instructions are for any agent that can run shell commands on a Mac. They do not need a specific model, provider, or agent framework. See [README.md](README.md) for installation, configuration, and more examples.

Only one model was tested on hardware: the Brother MFC-J7500CDW. On that model, the single-sided A4 feeder scan at 300 dpi and printing a PDF were verified. Do not say that other scan modes or other models are verified. Other eSCL scanners and CUPS queues may work, but the user must check the result.

## Find the command

Use `brother-mfc`. If it is not in `PATH`, run `python3 brother_mfc.py` from this directory. Keep `pdf.swift` in the same directory as `brother_mfc.py`.

## Scan documents for the user

1. Run `brother-mfc status`. This reads the scanner and does not move paper.
   - If the command says that no scanner was found or that it found several scanners, run `brother-mfc discover`. Then ask the user which scanner to use, or ask for its address. Use that address with `--host`.
   - Do not select a scanner yourself when there is more than one.
2. Use the page count, paper size, sidedness, and readiness that the user already gave. If you do not know whether the pages are single-sided or double-sided, ask. If the feeder is empty, tell the user to load the pages face up, top edge first, and to adjust the guides. Wait until the user says that the pages are ready.
3. Run the scan. For eight single-sided pages:

   ```sh
   brother-mfc scan --pages 8
   ```

4. Let the command finish. It prints progress to stderr and one absolute PDF path to stdout. Each page can take tens of seconds. If the command runs in a background session, wait for it and keep the user informed.
5. Check the exit code. If the scan succeeded, give the user a link to the PDF path from stdout. Tell the user the page count and the settings that they asked for. The command checks the page count and the saved rotation. It does not check what is on the pages. If the user asks you to check the content, look at the PDF.

The defaults are the document feeder, single-sided, A4, color, 300 dpi, and the rotation from the config file (0 if there is no config file). The PDF goes into `~/Downloads`. Keep these defaults unless the user asks for a change.

`--pages` counts PDF pages, so duplex counts both sides. For eight double-sided sheets, use `brother-mfc scan --duplex --pages 16`. If you do not know the page count, leave out `--pages`. Use `--rotate 0` to keep the orientation from the scanner. Use `--source flatbed --pages 1` to scan from the glass.

If the user asks for a scan and the pages are ready, run the scan. Do not ask for confirmation again. Do not start a physical scan only to test a change to the code or the documents.

## Fix orientation without scanning again

Make a rotated copy of the PDF:

```sh
brother-mfc rotate /absolute/path/input.pdf --degrees 180 -o /absolute/path/corrected.pdf
```

Use a new output file name. The command does not overwrite files. The rotation is clockwise from the current orientation and applies to every page.

If all the scans from this scanner are upside down, tell the user that they can set `"rotate": 180` in the config file. The config file path is shown at the end of `brother-mfc --help`. Do not change the config file unless the user asks you to.

## Print documents for the user

```sh
brother-mfc print /absolute/path/document.pdf
```

The defaults are one copy, single-sided, and A4. Use `--copies 2`, `--duplex`, and `--paper letter` when the user asks. Duplex uses long-edge binding. Add `--short-edge` for short-edge binding. Printing keeps the orientation of the document.

The printer queue comes from `--printer`, then `BROTHER_PRINTER`, then the config file, then the macOS default printer. If the command says that no queue is selected, run `lpstat -p` and ask the user which queue to use. The scanner `--host` option does not select the printer.

To check a print command without printing, add `--dry-run`. This checks the document and the queue and prints the planned `lp` command. Use it to test code changes. Do not send a real print job only to test code or documents.

If the user asks you to print and gives the document and the settings, send the job. Do not ask for confirmation again. Tell the user the CUPS receipt from stdout and say that the job is in the queue. Do not say that the paper has printed. To see waiting jobs, run `lpstat -o QUEUE_NAME`.

Do not repeat a print command that failed or was interrupted. Check the queue first, because the printer may have accepted the first job.

## Recover from an error

Before the scan starts, the command prints the session directory. The pages and `session.json` stay in `~/Downloads/.brother-mfc/session-*`.

If only the PDF step failed, rebuild the PDF without moving paper:

```sh
brother-mfc assemble /absolute/path/to/session -o /absolute/path/rebuilt.pdf
```

The rebuild uses the saved page count and rotation. Add `--pages` or `--rotate` only when the change is correct. Do not lower the page count to hide missing pages. Tell the user which pages were captured before you arrange another scan.

Do not restart a failed scan automatically. Do not repeat a failed document download. A second request can move the next sheet through the feeder or duplicate pages. Before a physical retry, find out which pages are missing and ask the user to load them again. Keep the captured files until the user no longer needs them.

If the scanner is busy, wait for the current operation to finish. If its address changed, use `--host NEW_ADDRESS` or run `brother-mfc discover`. Do not replace the command with your own eSCL request. The script contains the exact XML and HTTP headers that work, and it checks the JPEG density to find ignored settings.

## Verify code changes

Run `python3 -m unittest -v` from this directory. The tests use generated images, a fake scanner, and fake `dns-sd`, `lp`, and `lpstat` commands. Use `brother-mfc status` for a read-only scanner check, and `brother-mfc print DOCUMENT --dry-run` to check a print command. Tell the user whether you verified with real hardware, saved pages, or test fixtures.

A successful PDF command exits with 0 and prints one absolute path on stdout. A successful print command prints a CUPS receipt. A dry run prints the planned command. Exit code 1 means the operation failed, 2 means the arguments are not correct, and 130 means the command was interrupted.
