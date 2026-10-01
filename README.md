# Excel_Upload — RealBooks job & menu console

A small Flask app that wraps the manual steps of deploying a customer's Excel-upload
integration. It pulls a job ticket off the RealBooks task board, reads the deployment
details out of the ticket description, looks up the customer's existing upload menus,
and lets you add / edit / delete those menus and inspect the customer's workbook —
all from one page instead of three websites and a text editor.

It is a Flask port of an older Tk app (`gui.py`); the surface changed, the logic did not.

---

## Quick start

```bash
pip install -r requirements.txt
pip install requests selenium          # see "Dependencies" — not in requirements.txt
python3 run.py                         # http://127.0.0.1:5000
```

| env var | default | effect |
|---|---|---|
| `HOST` | `0.0.0.0` | bind address |
| `PORT` | `5000` | port |
| `DEBUG` | `1` | Flask debug/reloader; set `0` to disable |
| `REALBOOKS_USERNAME` / `REALBOOKS_PASSWORD` | *inline fallback* | task-board login (see **Credentials**) |
| `OLD_DB_CONNECTION_FILE` | `""` | fixed `.txt` the old Add form requires |

Fetching a job drives a real Chrome session through Selenium, so a working
**Chrome + chromedriver** is required on the host. Everything else (menu list,
add/edit/delete, the CSV/Excel viewer) is plain HTTP and works without it.

---

## How it fits together

```
run.py                 entry point — reads HOST/PORT/DEBUG, calls app.run()
  └── app.py           Flask: 18 routes, settings, file browser, CSV/Excel viewer
        └── NEWFILE.py transport layer — Selenium for the task board,
                       requests for the menu-mapping services
              ├── tasks.realbooks.in            job ticket + comments (Selenium)
              ├── custom-pyexcel.realbooks.in   menu list / add / edit / delete  (new)
              ├── beta-custom-pyexcel...        same four endpoints, staging      (beta)
              └── xlconverter.realbooks.in      Python_Upload list + save         (old)
```

`custom-pyexcel` also serves `GetRbLedgerByLedgerGrp` and `GetRbItemByItemGrp`,
behind the Ledger export and Item export tabs.

`app.py` imports `NEWFILE.py` from its **parent** directory — `PARENT_DIR` is prepended
to `sys.path` at import time. A copy also sits next to `app.py`; the parent one wins.

### The three menu formats

`menu_list_format` in settings switches which backend the menu screens talk to. This is
the single most load-bearing setting in the app:

| format | list / add / edit / delete host | notes |
|---|---|---|
| `new` | `custom-pyexcel.realbooks.in` | JSON payloads, multipart file upload |
| `beta` | `beta-custom-pyexcel.realbooks.in` | same payloads, staging host |
| `old` | `xlconverter.realbooks.in` | returns **HTML**, not JSON — parsed by `_parse_old_menu_html`; no beta equivalent |

The format also picks the **file-browser root**: `new`/`beta` browse `realbooks_root`
(`…/RealBooks`), `old` browses `realbooks_root_old`. Old format falls back to the new
root when its own root is missing or empty, because on most machines the old-format
scripts live under `RealBooks/` too.

Both remote families are gated by a **date-derived password**, regenerated per request:

- new/beta — `RLB1234<YYYYMMDD>` sent as the `password` field
- old — `adansa@@realbooks<day><month>` set as a cookie scoped to `/converter/`

So a stale session is never the problem; a wrong system clock is.

---

## The screens

Ten tabs, all served from the single `/` page (`templates/index.html`, driven by
`static/js/main.js`).

| tab | what it does |
|---|---|
| **Summary** | Enter a job number → box id, menu name, GSTIN, domain alias, and the from/to CID + segids pulled off the ticket |
| **Description** | The raw ticket description, plus the key/value pairs parsed out of it |
| **Menu list** | The customer's existing upload menus for that CID / segid / box |
| **Add Menu** | Upload a `.py` converter + template workbook and register a new menu |
| **Edit Menu** | Replace the `.py` / template on an existing menu |
| **Delete Menu** | Remove a menu |
| **CSV / Excel** | Open a workbook — server-side path, browser upload, or a Google Sheets link — pick sheet and header row, edit cells in place (a sheet saves back to Google) |
| **Ledger export** | Pull a list of ledger groups for a CID / box and write them to one timestamped workbook |
| **Item export** | The same, against the item endpoint — a line may carry a comma list to batch groups into one request |
| **Raw JSON** | The whole fetched job object |

### Description parsing

`_parse_description` reads the ticket body line by line and recognises `Key - value`,
`Key: value`, `Key = value` and `Key is value`, case-insensitively. Recognised keys:

```
domain    box    company    cid / c id / c name    segment
segid / seg id    menu name / manu name / menu    gstin
```

plus the directional form `deploy from domain - X` / `deploy to domain - Y`.
Longer keys are matched first, so `seg id` never gets eaten by `segment`. Parsed
values only fill fields the scrape left blank — the ticket's own structured fields win.

`_enrich_job` then runs the menu lookup for the `from` and `to` sides independently,
and `menu_list` is set to whichever side came back non-empty (`from` first).

### CSV / Excel viewer

Everything is returned as **strings**. That is deliberate: the point of the screen is to
see the sheet exactly as the upload scripts see it — leading zeros in part codes, GSTINs
and item numbers survive. No type coercion happens anywhere in that path.

- a **Google Sheets URL or id** in place of a path — tab picked by the `gid` in the link,
  then by name from the dropdown; edits save straight back (see below)
- `.csv` / `.tsv` / `.txt` — delimiter sniffed, encoding auto-detected
- `.xlsx` / `.xlsm` — `openpyxl`, read-only
- `.xls` — needs `pandas` + `xlrd`; error message says so if they're missing
- header row is 1-based; `0` means "no header, show raw grid"
- caps: 1 000 rows by default, 50 000 max, 200 MB per file, 64 MB per request body

Google Sheets are **editable in place**: type in a cell, press Save changes, and the
value is written to the live tab — only the cells you touched, sent as RAW so leading
zeros survive. Everyone on the document sees it immediately, so the confirm dialog says
that outright; Google's version history is the undo. Auth is the credentials JSON named
by the `gsheet_credentials` setting (a service account by default) — **the sheet must be
shared with that account's `client_email`** (Viewer to read, Editor to save), or Google
reports it as missing. Needs `pip install gspread google-auth`.

Writes are surgical — the file is re-read and only the edited cells are replaced, via
`_atomic_write` (scratch file in the same directory, then `os.replace`), so a failed
write leaves the original untouched rather than truncated. CSV saves also round-trip the
file's *shape*: the sniffed delimiter, the BOM flag and the original line terminator are
all preserved, so changing one cell shows up as a one-line diff instead of a whole-file
CRLF churn.

### Ledger export / Item export

A port of the `RB_Ledger_Export.ipynb` notebook, extended to the matching item endpoint.
The two screens are the same form against `manualmapping/GetRbLedgerByLedgerGrp` and
`manualmapping/GetRbItemByItemGrp`, and write
`RB_Ledger_Export_<cid>_<box>_<YYYY-MM-DD_HH-MM-SS>.xlsx` /
`RB_Item_Export_…xlsx`:

- **SUMMARY** — the run's parameters (generated-on, cid, box, URL, group counts, total
  rows), then the per-group log: row count, seconds, and the API's own message
- **ALL_LEDGERS** / **ALL_ITEMS** — every group concatenated, split across `…_1`, `_2`…
  if it outgrows Excel's 1 048 575-row sheet limit
- one sheet per group, empty ones included so a group that returned nothing is visible

**The two screens split their group box differently.** Ledger groups go one per request,
so commas split. The item endpoint batches a comma list in a single call, so an item line
is sent whole and only a newline starts a new request:

```
GRP1,GRP2      -> one request for both groups
GRP3           -> a second request
```

Trailing commas are safe to type — `Trading Goods,` on its own line is sent as
`Trading Goods`. That matters because an **empty segment in the comma list makes the
item service answer 404 for the whole request**: `"Reckitt"` returns 254 rows,
`"Reckitt,"` returns Not Found. Spaces around a comma are fine.

The fetch and the write are one request. These responses reach six figures of rows (a
real ledger pull is ~10 MB of JSON), so the browser only ever receives the summary, a
200-row-per-group preview and the file's path — "Open in CSV / Excel" hands that path to
the viewer. "Fetch only" runs the same lookup without writing anything.

Three things differ from the rest of the app. The endpoints take **JSON with no password
field** (`{"cid", "boxid", "ledgergrp"|"itemgrp"}`); `old` format has no xlconverter
equivalent, so it borrows the production URL rather than failing the screen; and **the
envelope lies** — both services answer `"type": "sucess"` with `"… Fetched Successfully"`
while putting a `404 NOT_FOUND` object inside `data`. Those are caught and reported as a
failed group rather than written as rows. A group that fails all its retries lands in
SUMMARY as `FAILED …` with an empty sheet instead of killing the run. Values are strings
for the same reason the viewer's are — ledger and item codes and GSTINs carry leading
zeros.

---

## HTTP API

All `POST` bodies are JSON unless noted.

| method | route | purpose |
|---|---|---|
| GET | `/` | the single-page UI |
| POST | `/api/fetch-job` | scrape a job ticket, enrich it, write a log |
| POST | `/api/post-comment` | post a comment on the ticket (default text `Deployed`) |
| POST | `/api/menu-search` | look up menus for a CID / segid / box |
| POST | `/api/add-menu` | register a new menu (multipart: `py_file`, `template_file`) |
| POST | `/api/edit-menu` | replace files on an existing menu |
| POST | `/api/delete-menu` | remove a menu |
| POST | `/api/find-domain` | resolve a domain alias to a folder under the root |
| POST | `/api/search-files` | glob for `.py` or template files inside a domain folder |
| POST | `/api/clone-file` | copy a `.py` into the deploy-to domain folder |
| POST | `/api/create-py` | copy a source `.py` to `<job>_<title>.py`; `409` if it exists and `overwrite` is false |
| POST | `/api/list-dir` | directory listing for the file picker |
| POST | `/api/read-table` | read a workbook — JSON `path` (a file path or a Google Sheets link), or multipart `file` |
| POST | `/api/write-table` | apply cell edits back to the file |
| POST | `/api/ledger-export` | fetch ledger groups and write the timestamped workbook |
| POST | `/api/item-export` | the same for item groups |
| GET/POST | `/api/settings` | read / merge `.web_settings.json` |

`POST /api/settings` **merges**: keys with a value are set, keys sent as `""` or `null`
are deleted. It echoes back the effective `realbooks_root`, which changes when
`menu_list_format` is toggled.

---

## Configuration — `.web_settings.json`

Written next to `app.py`, updated whenever a screen remembers something. Not secret,
but machine-specific — it holds absolute paths.

| key | meaning |
|---|---|
| `recent_jobs` | last 10 job numbers, newest first |
| `menu_list_format` | `new` \| `beta` \| `old` — see above |
| `realbooks_root` / `realbooks_root_old` | file-browser roots per format |
| `rlb_module_type` | `inventory` / `acc` / … |
| `file_ext_type` | extensions the menu accepts, e.g. `xlsx,xls` |
| `uid_create`, `uid_update` | operator ids sent with add/edit |
| `is_ledger_creation`, `is_item_creation`, `is_cc_creation`, `is_tagg_creation` | `"1"` / `"0"` master-creation flags |
| `edit_*` | the same flags remembered separately for the Edit screen |
| `viewer_path`, `viewer_header_row`, `viewer_max_rows` | last file the viewer opened |
| `gsheet_credentials` | Google credentials JSON for the Sheets reader |
| `ledger_cid`, `ledger_box_id`, `ledger_groups`, `ledger_export_dir` | last Ledger export run |
| `item_cid`, `item_box_id`, `item_groups`, `item_export_dir` | last Item export run |

---

## Files on disk

| path | what | lifetime |
|---|---|---|
| `JOB-*_YYYYMMDD_HHMMSS.txt` | one log per fetch — title, URL, description, summary fields, menu rows, raw JSON | deleted on next run once the datestamp is older than today |
| `.uploads/` | workbooks uploaded through the viewer, kept so sheet/header changes re-read without re-uploading | deleted after 24 h |
| `RB_Ledger_Export_*.xlsx` / `RB_Item_Export_*.xlsx` | one workbook per export run — SUMMARY, ALL_LEDGERS / ALL_ITEMS, one sheet per group | never cleaned |
| `.web_settings.json` | see above | persistent |
| `__pycache__/` | bytecode | — |

Both cleanups run at import time and again before each new write, so they happen even
if nobody touches the relevant screen.

There is **no `.gitignore`** in this directory. The job logs happen to be untracked, but
`.web_settings.json` and the whole `__pycache__/` directory (six `.pyc` files, three
Python versions) *are* committed. The settings file holds absolute paths from one
developer's machine, so it conflicts on every pull. The logs are worth ignoring
explicitly too — they contain customer names, CIDs, GSTINs and box ids, and only
today's survive, so a stray `git add -A` on the wrong day commits live customer data.

```gitignore
JOB-*.txt
.uploads/
.web_settings.json
__pycache__/
```

---

## Dependencies

`requirements.txt` lists only `Flask` and `openpyxl`, but `app.py` imports `NEWFILE.py`
unconditionally at startup, and that module imports `requests` and `selenium` at the top.
A fresh `pip install -r requirements.txt && python3 run.py` therefore fails with
`ModuleNotFoundError`. The full set:

```
Flask>=2.3          required
openpyxl>=3.1       required — .xlsx/.xlsm reading
requests            required — all menu-mapping calls
selenium            required — task-board scrape (import happens even if unused)
pandas + xlrd       optional — only for legacy .xls in the viewer
gspread + google-auth  optional — only to open a Google Sheet in the viewer
```

Chrome + chromedriver are needed for the job-fetch and post-comment paths only.

---

## Credentials

`NEWFILE.py` reads `REALBOOKS_USERNAME` / `REALBOOKS_PASSWORD` from the environment but
**falls back to a real account hardcoded in the source**, which is committed to git.
The old-format cookie password prefix is hardcoded next to it.

Set the env vars and replace the inline fallbacks with empty strings, and rotate the
account that is currently in the file — the history will still hold it.

---

## Gotchas

- **`sys.path` injection.** `app.py` prepends its parent directory and imports
  `NEWFILE` from there. Two copies of `NEWFILE.py` exist; the parent shadows the local
  one. Edit the wrong copy and nothing changes.
- **Old format returns HTML.** `Python_Upload_List_Ajax.jsp` is scraped, not parsed as
  JSON. A markup change upstream breaks the Menu list screen silently — it returns zero
  rows rather than an error.
- **Date-derived passwords.** Both backends derive the password from *today's* date. A
  skewed clock looks exactly like an auth failure.
- **`segid` is sent as a JSON array of ints.** Non-numeric segids raise before the
  request is made.
- **Box id is zero-padded to two digits** by `_pad_box` (`5` -> `05`) and prefixed
  with `RLBMBOX1`. Non-numeric ids pass through untouched.
- **Static assets are cache-busted** by mtime (`static_url()` in the template context),
  and `SEND_FILE_MAX_AGE_DEFAULT` is `0` — edited JS/CSS shows up on reload.
- **`debug=True` by default** in both `run.py` and `app.py`'s `__main__`. Combined with
  `HOST=0.0.0.0` that exposes the Werkzeug debugger on the network. Set `DEBUG=0` for
  anything but localhost.
