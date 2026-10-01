# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Flask console that wraps the manual steps of deploying a customer's Excel-upload
integration at RealBooks: pull a job ticket off the task board, read the deployment
details out of the ticket description, look up the customer's existing upload menus,
add / edit / delete those menus, and inspect the customer's workbook — one page instead
of three websites and a text editor. Ported from an older Tk app (`gui.py`, one level up
in `EXCEL_UPLOAD_TEST/`); the surface changed, the logic did not.

`README.md` is a long-form guide to the same code. It is accurate except where noted
under **README corrections** below.

## Running

```bash
pip install -r requirements.txt
pip install requests selenium        # see below — NOT in requirements.txt
python3 run.py                       # http://127.0.0.1:5000
PORT=8000 DEBUG=0 python3 run.py
```

`requirements.txt` lists only `Flask` and `openpyxl`, but `app.py` imports `NEWFILE.py`
at startup and that module imports `requests` and `selenium` at the top — a clean install
from `requirements.txt` alone fails with `ModuleNotFoundError`. `pandas` + `xlrd` are
optional (legacy `.xls` in the viewer only). Chrome + chromedriver are needed for the
job-fetch and post-comment paths only; every other screen is plain HTTP.

Env vars: `HOST` (`0.0.0.0`), `PORT` (`5000`), `DEBUG` (`1`),
`REALBOOKS_USERNAME` / `REALBOOKS_PASSWORD`, `OLD_DB_CONNECTION_FILE`,
`REALBOOKS_NEXTGEN_COOKIE` (fallback for the `nextgen_cookie` setting).

**There is no test suite and no linter — do not invent test commands.** Verify a change
by fetching a real job and walking the affected tab. This directory is not a git repo
and has no parent repo; it is a dated working snapshot under `EXCEL_UPLOAD_TEST/`.

`debug=True` and `HOST=0.0.0.0` are the defaults in both `run.py` and `app.py`'s
`__main__`, and `/api/list-dir` browses **any** absolute path on the host with no root
restriction. Keep it on localhost, or set `DEBUG=0` and bind `127.0.0.1`.

## Architecture

```
run.py            entry point — HOST/PORT/DEBUG, app.run()
  └── app.py      Flask: 17 routes, settings, file browser, CSV/Excel viewer  (~1.8k lines)
        └── NEWFILE.py  transport layer — Selenium for the task board,
                        requests for the menu-mapping services               (~1.3k lines)
              ├── tasks.realbooks.in           job ticket + comments   (Selenium)
              ├── custom-pyexcel…/manualmapping  menu list/add/edit/delete  (new)
              ├── beta-custom-pyexcel…           same endpoints, staging    (beta)
              ├── custom-pyexcel…/exvspy         same, cookie-authenticated (nextgen)
              └── xlconverter.realbooks.in       Python_Upload list + save  (old)
```

`NEWFILE.py` owns every browser session and every outbound RealBooks call
(`api_fetch_job`, `api_post_comment`, `fetch_menu_list`, `add_menu`, `edit_menu`,
`delete_menu`) and is also a standalone CLI (`NEWFILE.main()`: `python3 NEWFILE.py 160911
--headless`, `--comment "text"`). `app.py` is a thin JSON-in/JSON-out wrapper plus the
local-filesystem features (file browser, `.py` cloning, workbook viewer). Adding a
workflow usually means touching `app.py` **and** `NEWFILE.py`.

UI is a single page: `templates/index.html` + `static/js/main.js` (one IIFE) +
`static/css/style.css`. Ten tabs — summary / description / menu / add / edit / delete /
viewer / ledger / item / json — swapped by a generic `#tabs .tab` handler.

### Fetch flow

`POST /api/fetch-job` → `NEWFILE.api_fetch_job` (Selenium login + scrape) →
`app._enrich_job` → `_write_job_log` → `_record_recent_job`.

`_enrich_job` re-parses the description with `_parse_description`, fills
`deploy_from_*` / `deploy_to_*` **only where the scrape left a field blank** (the
ticket's own structured fields win), then calls `fetch_menu_list` for the `from` and `to`
sides independently into `menu_list_from` / `menu_list_to`. `menu_list` is whichever came
back non-empty, `from` first.

## new / beta / nextgen / old — the main axis

`menu_list_format` in `.web_settings.json` (global Format dropdown, read by
`_menu_list_format()`, passed as `format_type=` into every NEWFILE call) is the single
most load-bearing setting in the app:

| format | host | shape |
|---|---|---|
| `new` | `custom-pyexcel.realbooks.in` | JSON responses |
| `beta` | `beta-custom-pyexcel.realbooks.in` | identical payloads and parsing — only the URL differs |
| `nextgen` | `custom-pyexcel.realbooks.in/exvspy/` | same payloads **minus the `password` field** — authenticated by a session **Cookie header** on every call; EditMenu also takes `menu_name` |
| `old` | `xlconverter.realbooks.in` | returns **HTML**, scraped by `_parse_old_menu_html`; no beta or nextgen equivalent |

All three JSON variants resolve through one table — `NEWFILE._PYEXCEL_URLS` +
`_pyexcel_url(endpoint, format_type)`. Add a host there, not a ternary at each call
site; an unrecognised `format_type` (including `old`) falls through to production,
which is what the ledger/item borrow below relies on.

**`nextgen` is not a host, it is an auth scheme.** There is no
`nextgen-custom-pyexcel.realbooks.in` (the name does not resolve). The exvspy
service lives on the production host and trusts only the operator's RealBooks browser
session: the full `Cookie:` header value (`RLBMAIN=…; rlb_api=…; boxid_ngnx_mbox=…;
…`) copied out of a logged-in tab, stored in the `nextgen_cookie` setting (sidebar
block, visible only when Format = nextgen; `REALBOOKS_NEXTGEN_COOKIE` is the env
fallback). `_pyexcel_auth(format_type, cookie)` is the single place that decides
between the `password` form field and the `Cookie` header; `app._remote_kwargs()`
threads both settings into every NEWFILE call. Rules that follow:

- The cookie **expires with the browser session**. A `401 {"type":"error","msg":
  "Unauthorized"}` means missing *or* expired — the two are indistinguishable, so
  `_check_nextgen_auth` raises `PermissionError` naming the cookie either way.
- A nextgen call with no cookie is **refused before any request goes out**
  (`ValueError` in NEWFILE, `400` from `_nextgen_cookie_error()` in every route).
  `_enrich_job` catches that so a fetched ticket still renders with an empty menu
  list and `menu_list_<side>_error` instead of failing the whole fetch.
- The nextgen EditMenu addresses the menu by **`menu_name`** and takes a narrower
  field set (no `gstin`, no cc/tagg flags). The Edit form's "Menu name" input is
  `data-fmt-only="nextgen"`; new/beta never send it.
- Only MenuList / AddMenu / EditMenu are from captured curls. `exvspy/DeleteMenu`
  and the ledger/item endpoints are **assumed** to sit beside them and are unverified.

The format also picks the file-browser root (`_realbooks_root()`): new/beta browse
`realbooks_root`, old browses `realbooks_root_old` — and old **falls back to the new
root** when its own is missing or empty, because on most machines the old-format scripts
live under `RealBooks/` too.

Old format diverges further on Add: it posts multipart to `/converter/RLB_XC_Py_upload`,
one POST per segid, and needs two files the new flow doesn't (`apiFileUpload`, and a
required `dbConnectionFile` `.txt` from `OLD_DB_CONNECTION_FILE` / `old_db_connection_file`
/ the request). Module and extension values are mapped to old codes by `_old_module_type`
/ `_old_file_ext_type`. The Add screen therefore has two forms, `#add-new` and `#add-old`,
swapped with the Format dropdown.

**`old` is the format worth branching on for shape; `nextgen` only for auth.** `new`
and `beta` differ by hostname alone, and `nextgen` adds only the cookie and the Edit
`menu_name`, so every shape check in the app is `== "old"` / `!= "old"` —
`_realbooks_root()`, `applyAddFormat()`, `updateActiveRootRow()`, the Add required-field
list — and the nextgen checks are confined to `_pyexcel_auth`, `_check_nextgen_auth`,
`edit_menu`, `_nextgen_cookie_error()` and `[data-fmt-only="nextgen"]` in the DOM.
Adding another JSON host needs a `_PYEXCEL_URLS` row and nothing else.

## Conventions that will bite

- **Three unrelated date-derived passwords**, all regenerated per request from
  `date.today()`. A skewed clock looks exactly like an auth failure; a stale session never
  is the problem — **except on nextgen**, where a stale session is the *only* problem:
  it sends no password at all, just the pasted `Cookie` header (see the format section).
  - new/beta — `RLB1234<YYYYMMDD>`, a POST field named `password`
  - old — `adansa@@realbooks<day><month>` (no zero-padding), a **cookie** scoped to `/converter/`
  - delete confirm — `realbooks@@adansa<DDMM>`, checked in `api_delete_menu`; a mismatch
    is a **403**, and the frontend must send the same string
- **Never `requests.post` the live Save endpoints to "test."** `RLB_XC_Py_upload` (old
  add) and `AddMenu` / `EditMenu` / `DeleteMenu` create and destroy real production menus.
  Validate request assembly by intercepting `requests.post`, not by hitting it.
- **Box ids:** routes pass the raw value; NEWFILE zero-pads to two digits (`_pad_box`,
  `5` → `05`) and prefixes `RLBMBOX1`. Never pre-format a box id at the route layer.
  The menu calls build it inline; `_rlb_box_id()` does the same but passes an
  already-prefixed value through, which is what the ledger screen needs.
- **`segid` goes out as a JSON array of ints** — a non-numeric segid raises before the
  request is made.
- **Fuzzy menu matching:** `fetch_menu_list` tries exact, then token-stem, then a
  `difflib` ratio ≥ 0.8 fallback that takes only the top scorer. A renamed menu can match
  the wrong row — test against real `JOB-*.txt` samples before tightening it.
  `_find_domain_folder` matches folders the same loose way (exact wins, else the first
  substring hit in **either** direction), so a short alias can land on a longer folder.
- **One displayed menu row is not one service row.** `_dedupe_menu_rows` collapses the
  service's rows by `py_file_path` (lowercased; blank paths stay separate) and joins that
  file's segids into a single `"1, 2, 3"` string, keeping the first row's other fields.
  Everything downstream — the menu table, the Add prefill, the logs — sees the collapsed
  shape, so count segids, not rows, when checking a deployment against the ticket.
- **Settings are the source of truth for form defaults.** Always round-trip through
  `_load_settings` / `_save_settings`; never touch `.web_settings.json` directly.
  `POST /api/settings` **merges**: a key with a value is set, a key sent as `""` or `null`
  is **deleted**. It echoes back the effective `realbooks_root`, which changes when
  `menu_list_format` is toggled.
- **Self-cleaning, date-keyed state.** `_cleanup_old_logs` deletes `JOB-*.txt` from before
  today and `_cleanup_old_uploads` clears `.uploads/` after 24 h — both at import time and
  again before each write, so they run even if nobody opens the screen. Don't park sample
  logs here. The logs contain customer names, CIDs, GSTINs and box ids.
- **`grep` reports `static/js/main.js` as binary** — there is a deliberate NUL byte in a
  JS string literal (`values.join("\0")`, ~line 1066). Use `grep -a` on that file.
- Static assets are cache-busted by mtime via `static_url()` in the template context, and
  `SEND_FILE_MAX_AGE_DEFAULT` is `0` — edited JS/CSS shows up on a plain reload.

## CSV / Excel viewer

Everything in this path is **strings**, deliberately — the screen exists to show the sheet
exactly as the upload scripts see it, so leading zeros in part codes, GSTINs and item
numbers survive. No type coercion anywhere; don't add any.

- `.csv`/`.tsv`/`.txt` (delimiter sniffed, encoding auto-detected), `.xlsx`/`.xlsm`
  (openpyxl read-only), `.xls` (needs pandas + xlrd)
- header row is 1-based; `0` means no header, raw grid
- caps: `DEFAULT_TABLE_ROWS` 1000, `MAX_TABLE_ROWS` 50 000, `MAX_TABLE_BYTES` 200 MB,
  `MAX_CONTENT_LENGTH` 64 MB per request body (413 is caught and returned as JSON)
- writes are surgical: the file is re-read, only the resolved cells are replaced, and the
  swap goes through `_atomic_write` (scratch file in the same directory + `os.replace`), so
  a failed write leaves the original intact rather than truncated. CSV saves round-trip the
  file's *shape* too — sniffed delimiter, BOM flag, original line terminator — so one edited
  cell is a one-line diff, not whole-file CRLF churn.
- `_resolve_edits` raises on an out-of-range row instead of skipping it: a mismatch means
  the file changed under the operator, and guessing would corrupt data.

The screen is not read-only, and it feeds the Add tab:

- **Exactly one column is editable at a time** — `fv.editCol`, auto-picked by
  `fvFindEditCol` (`File_Status` / `Status`, else the first header containing "status")
  and overridable from the `#fv-edit-col` dropdown. Cells are set to
  `contentEditable = "plaintext-only"` (with a `"true"` fallback for older Firefox) so
  pasted rich text can't smuggle markup into a cell, and they are driven by **one
  delegated `input` listener** on the tbody, because a 2000-row redraw would otherwise
  wire up thousands. Edits live in a `row:col → {value, orig}` map, go
  back to `orig` on Revert, and **Save writes into the file that was read** — no copy.
- **`#fv-to-add` ("Send cid + segids → Add Menu") is a real cross-tab dependency.**
  `fvDeriveCtx` finds the cid and segid columns by normalized header name over the rows
  the **filters currently select**, splits multi-segid cells on `[\s,;|]+`, flattens
  them into one comma list, fills **both** Add forms (`#add-cid`/`#add-segids` and
  `#add-old-cid`/`#add-old-segid`, since the visible one depends on the Format toggle)
  and switches tabs. Multiple cids in the selection is not an error: it takes the first
  and warns. Renaming an Add-form input id silently breaks this path — nothing throws.

## Google Sheets in the viewer

The CSV / Excel screen accepts a Google Sheets URL (or a bare spreadsheet id) wherever a
file path goes — `_is_gsheet_ref` routes it to `_gsheet_rows` instead of the file
readers, and everything downstream is unchanged because it all comes back as strings.

- **Auth is a credentials JSON**, path in the `gsheet_credentials` setting, defaulting to
  `/home/adansa/Downloads/excelupload-494105-36a0ffee8634.json` (a service account,
  `excelupload@excelupload-494105.iam.gserviceaccount.com`). Service-account and
  `authorized_user` files work; the interactive OAuth-client flow does not, because
  there is no console to complete it on behind Flask.
- **A sheet must be shared with that client_email**, or Google returns 404 — a service
  account sees nothing by default. "Not shared" and "no such id" are indistinguishable
  from the client, so the error message names the account and covers both.
- **gspread raises `SpreadsheetNotFound`, which is a *sibling* of `APIError`**, not a
  subclass — catch `GSpreadException` or the friendly message gets bypassed.
- **The gid in the URL picks the tab on the first read**; after that the sheet dropdown
  passes a tab title, so `_gsheet_rows` matches on title first and gid second.
- **Writes go straight into the live tab.** `_gsheet_write_edits` re-reads the tab,
  maps the edits through the same `_resolve_edits` the file writers use (so a row that
  shifted underneath raises instead of being guessed at), and sends only the edited
  cells via `update_cells`. There is no local copy and no staging — everyone on the
  document sees it at once, so the viewer's confirm dialog says so and names Google's
  version history as the undo. The scope is the full `spreadsheets` one and the sheet
  must be shared as **Editor**, not Viewer.
- **`value_input_option="RAW"` is load-bearing.** USER_ENTERED would parse `0091` into
  the number 91 and eat the leading zero the whole viewer exists to preserve.
- Needs `gspread` and `google-auth`. Both are installed in the Anaconda env, which is
  what plain `python3` on PATH already resolves to here
  (`/home/adansa/anaconda3/bin/python3`, 3.11) — so `python3 run.py` picks them up with
  no activation step. The system `python3.8` has its own copy for
  `Googel_Sheet2/reorder.py`; they are unrelated installs.

## Ledger / item export

`POST /api/ledger-export` and `POST /api/item-export` — the "Ledger export" and "Item
export" tabs. A port of the `RB_Ledger_Export.ipynb` notebook, extended to the matching
item endpoint. Each pulls a set of groups from `manualmapping/GetRbLedgerByLedgerGrp` /
`GetRbItemByItemGrp` and writes a timestamped
`RB_{Ledger,Item}_Export_<cid>_<box>_<YYYY-MM-DD_HH-MM-SS>.xlsx`: a SUMMARY sheet (run
parameters, then the per-group log), a combined ALL_LEDGERS / ALL_ITEMS sheet, then one
sheet per group.

Both share one core — `NEWFILE._fetch_rb_group` / `_fetch_rb_groups` and app.py's
`_run_group_export` + `GROUP_EXPORTS` table. Adding a third such endpoint means one
entry in `GROUP_EXPORTS`, one `fetch_*_groups` wrapper, one route, one panel and one
`makeGroupExport({...})` call — not another copy of the screen.

- **The two differ in how the group textarea splits, and that is deliberate.** Ledger
  groups go one per request, so commas split. The item endpoint takes a comma list in a
  single call (`"itemgrp": "GRP1,GRP2"`), so an item line is sent **whole** and only a
  newline starts a new request — that is how the operator asks for a batched pull.
  `_parse_group_lines(raw, split_commas)` is the only place this lives.
- **An empty segment in `itemgrp` 404s the whole request.** The service splits the
  comma list and looks every piece up, so `"Trading Goods,"`, `",Reckitt"` and
  `"A,,B"` all fail — while `"Reckitt , Trading Goods"` is fine, spaces and all.
  Writing a group list one per line with trailing commas is the obvious way to type it,
  so `_clean_group_list` drops the empties before the request goes out; it is applied
  both at the parse boundary and in `fetch_item_group`. Verified live: `"Reckitt"` 254
  rows, `"Reckitt,"` 404.
- **`type: "sucess"` does not mean success.** Both services answer
  `{"msg": "… Fetched Successfully", "type": "sucess"}` while putting a Spring error
  object *inside* `data` — the 404 above arrives as though it were a row.
  `_is_error_block` catches those before they reach the workbook; the group lands in
  SUMMARY as `FAILED …` instead. Never trust the envelope alone here.
- **Fetch and write happen in one call.** These responses run to six figures of rows
  (a real ledger pull for cid 5770 / RLBMBOX118 is ~10 MB of JSON), so the browser never
  receives them in full — it gets the summary, a `GROUP_PREVIEW_ROWS` preview and the
  workbook's path, and opens the real thing through the CSV / Excel screen.
  `write_file: false` does the fetch without writing.
- **These are the one path that differs from the rest of the format axis.** `beta` hits
  its own host as usual, but xlconverter has no ledger or item endpoint, so `old` borrows
  the production URL (`_ledger_url` / `_item_url`) instead of failing the screen.
  `nextgen` goes to `exvspy/GetRb…` with the cookie header — assumed to mirror the
  menu endpoints and **not** confirmed against live data the way the new-format
  columns were; verify before relying on an export there.
- **The endpoints are JSON, not form-encoded, and carry no password field** — the payload
  is `{"cid", "boxid", "<ledgergrp|itemgrp>"}` with `Content-Type: application/json`.
  Don't add the `RLB1234<date>` field here; it isn't what these services expect. The
  nextgen cookie is the only auth that applies (`_rb_group_headers`).
- **Both column sets are confirmed against live data**, not guessed: ledgers are
  `ledger_name` / `ledger_code` / `gstin_no` (cid 5770 / RLBMBOX118), items are
  `item_code` / `item_id` / `item_name` (cid 13799 / RLBMBOX101, 4 371 rows). Note items
  carry **no gstin and no hsn**. Anything else the service sends is still kept, appended
  after the known columns. Don't add a column on a hunch — a forced column that never
  arrives shows up empty in every row.
- **One dead group never costs the operator the other twenty.** `_fetch_rb_group`
  retries `RB_FETCH_RETRIES` (3) times with a `2 * attempt` back-off against a
  `RB_FETCH_TIMEOUT` of **300 s** per request, then returns `([], "FAILED …")` rather
  than raising. A big multi-group export can therefore sit for minutes with no output;
  that is the timeout, not a hang.
- Values are stringified in `_flatten_rb_rows` for the same reason the viewer does it —
  ledger codes, item codes and GSTINs carry leading zeros.
- Writing uses **openpyxl in write-only mode**, not pandas/xlsxwriter as the notebook
  did, so the app keeps openpyxl as its only spreadsheet dependency. Write-only means a
  sheet must be finished before the next one starts — hence the grids are built up front.

## Adding things

- **Endpoint:** parse `request.get_json(silent=True) or {}`, validate required keys and
  return `jsonify(error="Missing: …"), 400`, call into `NEWFILE.py`, return
  `jsonify(ok=True, …)`; wrap remote calls in `try/except` returning
  `jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500`.
- **Tab:** `<button class="tab" data-tab="x">` + `<div class="card panel hidden"
  data-panel="x">` + a renderer in `main.js` under its own `// ----` banner (the generic
  `#tabs` handler wires the switching).
- **Persisted default:** add the key to the `defaults` dict in the `index` route, read it
  in the template as `{{ defaults.key }}`, and write it back in the route that owns it.

## README corrections

The committed `README.md` is stale on two points:

- It says `app.py` imports `NEWFILE` from its **parent** directory and that the parent copy
  shadows the local one. `app.py` does prepend `PARENT_DIR` to `sys.path`, but
  `EXCEL_UPLOAD_TEST/` holds no `NEWFILE.py` — **the local copy is what runs here.** Still
  check for a parent copy before concluding an edit had no effect.
- Its API table calls `/api/add-menu` and `/api/edit-menu` multipart (`py_file`,
  `template_file`). They are **JSON with server-side absolute paths** — the whole app reads
  files off the server filesystem through the Browse… picker. `/api/read-table` is the only
  route that accepts a browser upload (multipart `file`, stashed under `.uploads/`).

## Credentials

`NEWFILE.py` reads `REALBOOKS_USERNAME` / `REALBOOKS_PASSWORD` from the environment but
falls back to a **real account hardcoded in the source**; the old-format cookie prefix is
hardcoded next to it. Set the env vars, blank the inline fallbacks, and rotate that account.
