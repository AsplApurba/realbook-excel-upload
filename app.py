"""Flask web app for the RealBooks job lookup — Flask port of gui.py.

Logic is unchanged: this delegates to NEWFILE.api_fetch_job / fetch_menu_list /
add_menu / edit_menu / delete_menu. Only the surface (Tk -> Flask + HTML/JS) changed.
"""
from __future__ import annotations

import csv
import fnmatch
import io
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from contextlib import contextmanager
from datetime import date, datetime, time as dtime

from flask import Flask, jsonify, render_template, request, url_for

# Allow imports from the parent dir where NEWFILE.py lives.
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from NEWFILE import (
    api_fetch_job,
    api_post_comment,
    fetch_menu_list,
    add_menu,
    edit_menu,
    delete_menu,
    fetch_ledger_groups,
    fetch_item_groups,
    DEFAULT_LEDGER_GROUPS,
    LEDGER_COLUMNS,
    ITEM_COLUMNS,
    RB_FETCH_RETRIES,
    RB_FETCH_TIMEOUT,
    _ledger_url,
    _item_url,
    _rlb_box_id,
    _clean_group_list,
    _nextgen_cookie,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, ".web_settings.json")
LOGS_DIR = APP_DIR
# Browser-uploaded CSV/Excel files land here so the viewer can re-read them when
# the operator flips sheet / header row without re-uploading. Cleaned after a day.
UPLOADS_DIR = os.path.join(APP_DIR, ".uploads")
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build", ".ipynb_checkpoints"}
DEFAULT_REALBOOKS_ROOT = "/home/adansa/Desktop/Apurba/Realbooks/NoteBookWorking/RealBooks"
# The root is format-aware: new format browses RealBooks/, old format browses dms-excel-uploads/.
DEFAULT_REALBOOKS_ROOT_OLD = "/home/adansa/Desktop/Apurba/Realbooks/NoteBookWorking/dms-excel-uploads"

app = Flask(__name__)
# Don't let the browser hold onto stale static assets — we also version each URL
# with its mtime below, but this keeps conditional revalidation honest.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# Only the CSV/Excel viewer accepts a body this big (browser-side file upload).
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024


@app.context_processor
def inject_static_url() -> dict:
    """Expose ``static_url(filename)`` to templates — same as ``url_for('static', ...)``
    but appends ``?v=<mtime>`` so edited JS/CSS is re-fetched on the next reload
    instead of served from the browser cache."""
    def static_url(filename: str) -> str:
        try:
            ver = int(os.stat(os.path.join(APP_DIR, "static", filename)).st_mtime)
        except OSError:
            ver = 0
        return url_for("static", filename=filename, v=ver)
    return {"static_url": static_url}


# ---------------------------------------------------------------- settings

def _load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_settings(settings: dict) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
    except OSError:
        pass


def _dir_has_entries(path: str | None) -> bool:
    """True if ``path`` is a directory holding at least one entry."""
    if not path or not os.path.isdir(path):
        return False
    try:
        with os.scandir(path) as it:
            return any(True for _ in it)
    except OSError:
        return False


def _resolve_root(saved: str | None, default: str) -> str:
    return saved if (saved and os.path.isdir(saved)) else default


def _realbooks_root() -> str:
    """Root for the file browser / domain lookup — different per menu_list_format.
    new -> realbooks_root (RealBooks/);  old -> realbooks_root_old (dms-excel-uploads/).

    Old format falls back to the new root when its own root is missing or empty:
    on many machines the old-format .py upload scripts live under RealBooks/ too,
    and an empty dms-excel-uploads/ would otherwise make Browse… a dead end."""
    settings = _load_settings()
    is_old = str(settings.get("menu_list_format", "new")).strip().lower() == "old"
    new_root = _resolve_root(settings.get("realbooks_root"), DEFAULT_REALBOOKS_ROOT)
    if not is_old:
        return new_root
    old_root = _resolve_root(settings.get("realbooks_root_old"), DEFAULT_REALBOOKS_ROOT_OLD)
    if _dir_has_entries(old_root):
        return old_root
    return new_root if os.path.isdir(new_root) else old_root


# ---------------------------------------------------------------- helpers (ported from gui.py)

def _sanitize(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_")
    return name or "untitled"


def _find_domain_folder(domain_alias: str) -> str | None:
    root = _realbooks_root()
    if not domain_alias or not os.path.isdir(root):
        return None
    target_raw = domain_alias.strip().lower()
    target = re.sub(r"\s+", "", target_raw)
    exact = None
    loose = None
    for entry in os.listdir(root):
        full = os.path.join(root, entry)
        if not os.path.isdir(full):
            continue
        name_raw = entry.strip().lower()
        name = re.sub(r"\s+", "", name_raw)
        if name == target:
            exact = full
            break
        if target and (target in name or name in target):
            loose = loose or full
    return exact or loose


def _parse_description(text: str) -> dict:
    if not text:
        return {}
    side_pat = re.compile(
        r"\bdeploy\s+(from|to)\s+domain\b\s*[-:=]\s*(.+?)\s*$",
        flags=re.I | re.M,
    )
    side_hits: dict = {}
    for m in side_pat.finditer(text):
        key = f"deploy_{m.group(1).lower()}_domain"
        side_hits.setdefault(key, m.group(2).strip())
    mapping = {
        "domain": "domain",
        "box": "box",
        "company": "company",
        "cid": "cid",
        "c id": "cid",
        "c name": "cid",
        "segment": "segment",
        "seg id": "segid",
        "segid": "segid",
        "menu name": "menu_name",
        "manu name": "menu_name",
        "menu": "menu_name",
        "gstin": "gstin",
    }
    plain_patterns = []
    for k in sorted(mapping.keys(), key=len, reverse=True):
        key_re = r"\s+".join(re.escape(t) for t in k.split())
        plain_patterns.append(
            (mapping[k], re.compile(rf"^\s*{key_re}\s+(.+?)\s*$", flags=re.I))
        )
    result: dict = {}
    for line in text.splitlines():
        m = re.match(
            r"\s*([A-Za-z][A-Za-z ]*?)\s*(?:[-:=]|\bis\b)\s*(.+?)\s*$",
            line, flags=re.I,
        )
        if m:
            key = re.sub(r"\s+", " ", m.group(1).strip().lower())
            val = m.group(2).strip()
            if not val:
                continue
            target = mapping.get(key)
            if target and target not in result:
                result[target] = val
            continue
        for target, pat in plain_patterns:
            mm = pat.match(line)
            if mm:
                val = mm.group(1).strip()
                if val and target not in result:
                    result[target] = val
                break
    for k, v in side_hits.items():
        result.setdefault(k, v)
    return result


def _dedupe_menu_rows(rows: list[dict]) -> list[dict]:
    extra_keys = (
        "menu_name", "domain_alias",
        "gstin", "rlb_module_type", "file_ext_type",
        "is_ledger_creation", "is_item_creation",
        "is_cc_creation", "is_tagg_creation",
    )
    grouped: dict[str, dict] = {}
    output: list[dict] = []
    for index, row in enumerate(rows):
        py_file_path = str(row.get("py_file_path") or "").strip()
        template_file_path = str(row.get("template_file_path") or "").strip()
        segid = str(row.get("segid") or "").strip()

        key = py_file_path.lower() if py_file_path else f"_empty_{index}"
        if key not in grouped:
            entry = {
                "segids": [],
                "py_file_path": py_file_path,
                "template_file_path": template_file_path,
            }
            for k in extra_keys:
                entry[k] = str(row.get(k) or "").strip()
            grouped[key] = entry
            output.append(entry)
        if segid and segid not in grouped[key]["segids"]:
            grouped[key]["segids"].append(segid)

    return [
        {
            "segid": ", ".join(row["segids"]) if row["segids"] else "",
            "py_file_path": row["py_file_path"],
            "template_file_path": row["template_file_path"],
            **{k: row[k] for k in extra_keys},
        }
        for row in output
    ]


def _search_py_files(pattern: str, root: str | None = None) -> list[dict]:
    matches: list[dict] = []
    search_root = root or _realbooks_root()
    if not os.path.isdir(search_root):
        return matches
    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in fnmatch.filter(filenames, pattern):
            full_path = os.path.join(dirpath, filename)
            try:
                stat = os.stat(full_path)
                matches.append({
                    "path": full_path,
                    "size_kb": round(stat.st_size / 1024, 2),
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
            except OSError:
                continue
    matches.sort(key=lambda x: x["modified"], reverse=True)
    return matches


def _clone_py_file(original_path: str, new_name: str, target_folder: str | None = None) -> tuple[str, bool]:
    folder = target_folder or os.path.dirname(original_path)
    if "." not in new_name:
        _, ext = os.path.splitext(original_path)
        new_name = new_name + ext
    clone_path = os.path.join(folder, new_name)
    renamed = False
    if os.path.exists(clone_path):
        base, ext = os.path.splitext(new_name)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        clone_path = os.path.join(folder, f"{base}_{timestamp}{ext}")
        renamed = True
    shutil.copy2(original_path, clone_path)
    return clone_path, renamed


def _menu_list_format() -> str:
    """new | beta | nextgen | old. 'beta' behaves like 'new' everywhere except the
    host (beta-custom-pyexcel.realbooks.in). 'nextgen' is the exvspy service on the
    production host: same forms and roots, but it authenticates with the operator's
    session cookie instead of the date password (see _nextgen_cookie_setting), and
    its EditMenu wants a menu_name. Only 'old' changes payload shape, parsing,
    roots and the Add form."""
    fmt = str(_load_settings().get("menu_list_format", "new")).strip().lower()
    return fmt if fmt in ("old", "beta", "nextgen") else "new"


def _nextgen_cookie_setting() -> str:
    """The pasted Cookie header for the nextgen endpoints — the setting wins, then
    REALBOOKS_NEXTGEN_COOKIE (NEWFILE applies that fallback and normalises)."""
    return _nextgen_cookie(_load_settings().get("nextgen_cookie") or "")


def _remote_kwargs() -> dict:
    """The two settings every NEWFILE call needs: which host, and — for nextgen —
    the cookie that authenticates against it."""
    return {"format_type": _menu_list_format(), "cookie": _nextgen_cookie_setting()}


def _nextgen_cookie_error():
    """A (response, 400) when the format is nextgen and no cookie is set, else None.
    Checked before touching the request so an Add/Edit isn't half-validated and
    then refused for a reason the operator can fix in the sidebar."""
    if _menu_list_format() == "nextgen" and not _nextgen_cookie_setting():
        return jsonify(
            error="Nextgen cookie is not set — paste the Cookie header from a "
                  "logged-in RealBooks tab in the sidebar (Nextgen cookie → Save)"
        ), 400
    return None


def _enrich_job(data: dict) -> dict:
    """Replicates the post-fetch enrichment from gui.py._worker."""
    parsed_desc = _parse_description(data.get("description") or "")
    if parsed_desc:
        data["desc_parsed"] = parsed_desc
        for side_key in ("deploy_from_domain", "deploy_to_domain"):
            if parsed_desc.get(side_key) and not data.get(side_key):
                data[side_key] = parsed_desc[side_key]

    def _inputs(side: str) -> tuple[str, list[str], str, str]:
        cid = data.get(f"deploy_{side}_cid") or ""
        segids = data.get(f"deploy_{side}_segids") or []
        box_id = data.get(f"deploy_{side}_box") or data.get("box_id") or ""
        menu_name = data.get(f"deploy_{side}_menu") or data.get("menu_name") or ""
        if parsed_desc:
            if not cid:
                cid = parsed_desc.get("cid", "")
            if not segids and parsed_desc.get("segid"):
                segids = re.findall(r"\d+", parsed_desc["segid"])
            if not segids and parsed_desc.get("cid"):
                segids = [parsed_desc["cid"]]
            if not box_id:
                box_id = parsed_desc.get("box", "")
            if not menu_name:
                menu_name = parsed_desc.get("menu_name", "")
        return cid, segids, box_id, menu_name

    for side in ("from", "to"):
        cid, segids, box_id_lookup, menu_name_lookup = _inputs(side)
        if cid and not data.get(f"deploy_{side}_cid"):
            data[f"deploy_{side}_cid"] = cid
        if segids and not data.get(f"deploy_{side}_segids"):
            data[f"deploy_{side}_segids"] = segids
        if box_id_lookup and not data.get(f"deploy_{side}_box"):
            data[f"deploy_{side}_box"] = box_id_lookup
        if menu_name_lookup and not data.get(f"deploy_{side}_menu"):
            data[f"deploy_{side}_menu"] = menu_name_lookup
        if cid and segids and box_id_lookup:
            try:
                rows = fetch_menu_list(
                    cid, segids, box_id_lookup, menu_name_lookup,
                    return_all=True,
                    **_remote_kwargs(),
                )
            except (ValueError, PermissionError) as e:
                # No nextgen cookie, or an expired one: keep the scraped ticket and
                # show why the menu list is empty instead of failing the fetch.
                print(f"menu list ({side}) skipped: {e}")
                data[f"menu_list_{side}_error"] = str(e)
                data[f"menu_list_{side}"] = []
                continue
            side_menu = (
                data.get(f"deploy_{side}_menu")
                or data.get("menu_name")
                or menu_name_lookup
                or ""
            )
            side_domain = (
                data.get(f"deploy_{side}_domain")
                or data.get("domain_alias")
                or ""
            )
            for row in rows:
                if not row.get("menu_name") and side_menu:
                    row["menu_name"] = side_menu
                if not row.get("domain_alias") and side_domain:
                    row["domain_alias"] = side_domain
            data[f"menu_list_{side}"] = rows
        else:
            data[f"menu_list_{side}"] = []

    data["menu_list"] = data.get("menu_list_from") or data.get("menu_list_to") or []
    return data


def _record_recent_job(job: str) -> None:
    job = (job or "").strip()
    if not job:
        return
    settings = _load_settings()
    recent = list(settings.get("recent_jobs") or [])
    recent = [j for j in recent if j != job]
    recent.insert(0, job)
    recent = recent[:10]
    settings["recent_jobs"] = recent
    _save_settings(settings)


def _cleanup_old_logs() -> None:
    """Delete job log files whose YYYYMMDD timestamp is before today."""
    today = date.today().strftime("%Y%m%d")
    pat = re.compile(r"_(\d{8})_\d{6}\.txt$")
    try:
        entries = os.listdir(LOGS_DIR)
    except OSError:
        return
    for name in entries:
        m = pat.search(name)
        if not m or m.group(1) >= today:
            continue
        try:
            os.remove(os.path.join(LOGS_DIR, name))
        except OSError:
            pass


def _write_job_log(data: dict) -> str | None:
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        _cleanup_old_logs()
        job = str(data.get("job") or "unknown").strip() or "unknown"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(LOGS_DIR, f"{job}_{ts}.txt")
        lines: list[str] = []
        lines.append(f"=== Job {job} @ {ts} ===")
        lines.append(f"Title : {data.get('title','')}")
        lines.append(f"URL   : {data.get('url','')}")
        lines.append("")
        lines.append("--- Description ---")
        lines.append(data.get("description") or "(empty)")
        lines.append("")
        lines.append("--- Summary ---")
        for key in ("box_id", "menu_name", "gstin", "domain_alias",
                    "deploy_from_cid", "deploy_from_segids",
                    "deploy_to_cid", "deploy_to_segids"):
            value = data.get(key, "")
            if isinstance(value, list):
                value = ", ".join(value)
            lines.append(f"{key}: {value}")
        lines.append("")
        lines.append("--- Menu list ---")
        for row in data.get("menu_list") or []:
            lines.append(json.dumps(row, ensure_ascii=False))
        lines.append("")
        lines.append("--- Raw JSON ---")
        lines.append(json.dumps(data, indent=2, ensure_ascii=False))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return path
    except OSError:
        return None


# ---------------------------------------------------------------- CSV / Excel reader
# Backs the "CSV / Excel" screen. Everything is returned as *strings* — the point
# of the screen is to see the sheet exactly as the upload scripts see it (leading
# zeros in codes, GSTINs, item numbers), so no type coercion happens anywhere here.

CSV_EXTS = {".csv", ".tsv", ".txt"}
XLSX_EXTS = {".xlsx", ".xlsm"}
XLS_EXTS = {".xls"}
TABLE_EXTS = CSV_EXTS | XLSX_EXTS | XLS_EXTS
DEFAULT_TABLE_ROWS = 1000
MAX_TABLE_ROWS = 50000
MAX_TABLE_BYTES = 200 * 1024 * 1024


def _int_or(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _fmt_cell(value) -> str:
    """One sheet cell -> display string. Dates/times get a stable ISO-ish shape and
    whole floats lose their ``.0`` (openpyxl hands back 5.0 for a cell showing 5)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        if value.hour or value.minute or value.second:
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dtime):
        return value.strftime("%H:%M:%S")
    if isinstance(value, float):
        if value != value:  # NaN (also catches pandas NaT)
            return ""
        return str(int(value)) if value.is_integer() else str(value)
    try:
        if value != value:  # pandas NA / NaT sentinels
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _csv_grid(path: str) -> tuple[list[list[str]], str, bool, str]:
    """Rows plus delimiter, BOM flag and line terminator, so a save round-trips the
    file's shape — csv.writer would otherwise turn every LF file into CRLF and show
    up as a whole-file diff instead of the one cell that actually changed."""
    with open(path, "rb") as fh:
        raw = fh.read()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    nl = raw.find(b"\n")
    lineterm = "\r\n" if nl > 0 and raw[nl - 1:nl] == b"\r" else "\n"
    text = _decode_bytes(raw)
    sample = text[:8192]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Sniffer gives up on single-column / ragged files — guess by frequency.
        delimiter = max(",;\t|", key=sample.count)
        if not sample.count(delimiter):
            delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [[_fmt_cell(c) for c in row] for row in reader], delimiter, has_bom, lineterm


def _csv_rows(path: str) -> list[list[str]]:
    return _csv_grid(path)[0]


def _xlsx_rows(path: str, sheet: str) -> tuple[list[list[str]], list[str], str]:
    from openpyxl import load_workbook  # lazy: the rest of the app runs without it

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = list(wb.sheetnames)
        name = sheet if sheet in sheets else (sheets[0] if sheets else "")
        if not name:
            return [], sheets, ""
        ws = wb[name]
        rows = [[_fmt_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
        return rows, sheets, name
    finally:
        wb.close()


def _xls_rows(path: str, sheet: str) -> tuple[list[list[str]], list[str], str]:
    """Legacy .xls — openpyxl can't read it, so lean on pandas (which needs xlrd)."""
    try:
        import pandas as pd
    except ImportError:
        raise RuntimeError(".xls needs pandas + xlrd installed — or save the file as .xlsx / .csv")
    try:
        xl = pd.ExcelFile(path)
        sheets = [str(s) for s in xl.sheet_names]
        name = sheet if sheet in sheets else (sheets[0] if sheets else "")
        df = xl.parse(sheet_name=name, header=None)
    except ImportError:
        raise RuntimeError(".xls needs the xlrd package (pip install xlrd) — or save as .xlsx / .csv")
    rows = [[_fmt_cell(v) for v in rec] for rec in df.itertuples(index=False, name=None)]
    return rows, sheets, name


# ---------------------------------------------------------------- Google Sheets
# The viewer reads a Google Sheet the same way it reads a workbook: paste the
# sheet's URL (or bare id) where a file path would go. Everything still comes back
# as strings, so a sheet behaves exactly like a .xlsx once it is on screen.
#
# Auth is a Google credentials JSON — a service account is what this machine has,
# and the sheet must be shared with its client_email as an Editor: edits made in the
# viewer are written straight back to the live tab, so there is no local copy to
# reconcile and everyone else sees the change immediately. Google's own version
# history is the undo.

GSHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_GSHEET_CREDENTIALS = "/home/adansa/Downloads/excelupload-494105-36a0ffee8634.json"
# https://docs.google.com/spreadsheets/d/<id>/edit?gid=<gid>#gid=<gid>
_GSHEET_URL_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)")
_GSHEET_GID_RE = re.compile(r"[#?&]gid=(\d+)")
# A bare id pasted on its own — Google ids are long and have no dots or slashes.
_GSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{30,}$")


def _is_gsheet_ref(value: str) -> bool:
    value = str(value or "").strip()
    return bool(_GSHEET_URL_RE.search(value) or _GSHEET_ID_RE.match(value))


def _parse_gsheet_ref(value: str) -> tuple[str, str]:
    """'…/d/<id>/edit#gid=735709360' -> ('<id>', '735709360'). The gid is '' when the
    link doesn't name a tab, in which case the first tab is used."""
    value = str(value or "").strip()
    m = _GSHEET_URL_RE.search(value)
    sheet_id = m.group(1) if m else value
    gid = _GSHEET_GID_RE.search(value)
    return sheet_id, (gid.group(1) if gid else "")


def _gsheet_client():
    """Authorise against the credentials JSON named in settings.

    Service account and authorized-user (gcloud ADC) JSONs are both accepted; the
    interactive OAuth-client flow is not, because there is no console to complete it
    on when this is running behind Flask.
    """
    try:
        import gspread
    except ImportError:
        raise RuntimeError(
            "Google Sheets support needs gspread — pip install gspread google-auth")

    cred_path = str(_load_settings().get("gsheet_credentials")
                    or DEFAULT_GSHEET_CREDENTIALS).strip()
    if not os.path.isfile(cred_path):
        raise RuntimeError(f"Google credentials JSON not found: {cred_path}")
    with open(cred_path, "r", encoding="utf-8") as fh:
        info = json.load(fh)

    kind = info.get("type")
    if kind == "service_account":
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(info, scopes=GSHEET_SCOPES)
    elif kind == "authorized_user":
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_info(info, scopes=GSHEET_SCOPES)
    else:
        raise RuntimeError(
            f"Unusable credentials JSON (type={kind!r}) — needs a service account or "
            "an authorized_user (gcloud ADC) file")
    return gspread.authorize(creds)


def _gsheet_account() -> str:
    """The client_email / account the credentials JSON authenticates as — the address
    a sheet has to be shared with. Best effort: it only ever decorates an error."""
    try:
        cred_path = str(_load_settings().get("gsheet_credentials")
                        or DEFAULT_GSHEET_CREDENTIALS).strip()
        with open(cred_path, "r", encoding="utf-8") as fh:
            info = json.load(fh)
        return info.get("client_email") or info.get("account") or "the configured account"
    except (OSError, ValueError):
        return "the configured account"


def _gsheet_open(ref: str, sheet: str):
    """Resolve a ref + tab selector to ``(worksheet, tab names, spreadsheet title)``.

    ``sheet`` selects the tab by title (the dropdown's value); the gid from the URL
    picks it on the first read, before any title is known. The worksheet is None only
    for a spreadsheet with no tabs at all.
    """
    import gspread

    sheet_id, gid = _parse_gsheet_ref(ref)
    gc = _gsheet_client()
    try:
        sh = gc.open_by_key(sheet_id)
    except gspread.exceptions.GSpreadException as e:
        # gspread turns a 404 into SpreadsheetNotFound, which is a sibling of APIError
        # rather than a subclass — catch the common base so both land here. To this
        # service account "not shared" and "does not exist" look identical, so the
        # message has to cover both.
        status = getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(e, gspread.exceptions.SpreadsheetNotFound) or status in (403, 404):
            raise RuntimeError(
                f"Google can't see that sheet as this service account ({_gsheet_account()}). "
                f"Either the id is wrong, or the sheet isn't shared — open it in Google "
                f"Sheets, press Share, and add that address as a Viewer.")
        raise

    tabs = sh.worksheets()
    names = [ws.title for ws in tabs]
    ws = None
    if sheet:
        ws = next((w for w in tabs if w.title == sheet), None)
    if ws is None and gid:
        ws = next((w for w in tabs if str(w.id) == gid), None)
    if ws is None:
        ws = tabs[0] if tabs else None
    return ws, names, sh.title


def _gsheet_grid(ws) -> list[list[str]]:
    """get_values pads ragged rows itself and hands back display strings, which is
    exactly the contract the rest of the viewer works to."""
    return [[_fmt_cell(v) for v in row] for row in ws.get_values()]


def _gsheet_rows(ref: str, sheet: str) -> tuple[list[list[str]], list[str], str, str]:
    """Read one tab. Returns ``(grid, tab names, active tab, spreadsheet title)``."""
    ws, names, title = _gsheet_open(ref, sheet)
    if ws is None:
        return [], names, "", title
    return _gsheet_grid(ws), names, ws.title, title


def _gsheet_write_edits(ref: str, sheet: str, header_row: int, edits: list[dict]) -> int:
    """Push the viewer's cell edits into the live tab; returns how many cells moved.

    The tab is re-read here before the edits are mapped, exactly as the file writers
    re-read their file — if someone else changed the sheet since it was loaded,
    ``_resolve_edits`` raises rather than writing into a row that has shifted.

    Values go up as RAW, so what the operator typed is what the cell holds: leading
    zeros on codes and GSTINs survive, where USER_ENTERED would quietly turn '0091'
    into the number 91. Only the edited cells are sent, so formatting, formulas and
    every other row on the tab are left alone.
    """
    import gspread

    ws, _names, _title = _gsheet_open(ref, sheet)
    if ws is None:
        raise ValueError("That spreadsheet has no tabs to write to")

    resolved = _resolve_edits(_gsheet_grid(ws), header_row, edits)
    if not resolved:
        return 0
    cells = [gspread.Cell(row + 1, col + 1, value)       # the Sheets API is 1-based
             for (row, col), value in sorted(resolved.items())]
    ws.update_cells(cells, value_input_option="RAW")
    return len(cells)


def _data_rows(grid: list[list[str]], header_row: int) -> list[tuple[int, list[str]]]:
    """The rows the viewer shows as data, as ``(physical_index, cells)``.

    Blank rows are skipped, so the "#" the operator sees is *not* the sheet row —
    the write path replays this same walk to map an edit back to its real row."""
    out = []
    for idx, cells in enumerate(grid):
        if header_row and idx + 1 <= header_row:
            continue
        if not any(str(c).strip() for c in cells):
            continue
        out.append((idx, cells))
    return out


def _build_table(grid: list[list[str]], header_row: int, max_rows: int) -> dict:
    """Split a raw grid into ``columns`` + ``rows``.

    ``header_row`` is 1-based against the sheet; 0 means "no header row, generate
    Column 1..N". Fully blank rows are dropped (Excel loves trailing ones) and
    trailing all-blank columns are trimmed off the right edge."""
    header_cells: list[str] = []
    if header_row and header_row <= len(grid):
        header_cells = list(grid[header_row - 1])

    data = _data_rows(grid, header_row)
    total = len(data)
    body = [list(cells) for _, cells in data[:max_rows]]

    width = max([len(header_cells)] + [len(r) for r in body])

    def _col_blank(i: int) -> bool:
        if i < len(header_cells) and str(header_cells[i]).strip():
            return False
        return all(not (i < len(r) and str(r[i]).strip()) for r in body)

    while width and _col_blank(width - 1):
        width -= 1

    columns = []
    for i in range(width):
        name = str(header_cells[i]).strip() if i < len(header_cells) else ""
        columns.append(name or f"Column {i + 1}")
    rows = [[(r[i] if i < len(r) else "") for i in range(width)] for r in body]

    return {
        "columns": columns,
        "rows": rows,
        "total_rows": total,
        "returned_rows": len(rows),
        "truncated": total > len(rows),
    }


def _read_table_file(path: str, sheet: str, header_row: int, max_rows: int) -> dict:
    if not path:
        raise ValueError("path is required")

    # A Google Sheets URL (or bare id) stands in for a file path here.
    if _is_gsheet_ref(path):
        try:
            grid, sheets, active_sheet, title = _gsheet_rows(path, sheet)
        except RuntimeError as e:
            raise ValueError(str(e))
        table = _build_table(grid, header_row, max_rows)
        table.update({
            "path": path,
            "name": title,
            "ext": ".gsheet",
            "sheets": sheets,
            "sheet": active_sheet,
            "header_row": header_row,
            "max_rows": max_rows,
        })
        return table

    if not os.path.isfile(path):
        raise ValueError(f"Not a file: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext not in TABLE_EXTS:
        raise ValueError(f"Unsupported file type '{ext or path}' — expected one of: "
                         + ", ".join(sorted(TABLE_EXTS)))
    if os.path.getsize(path) > MAX_TABLE_BYTES:
        raise ValueError(f"File is larger than {MAX_TABLE_BYTES // (1024 * 1024)} MB")

    sheets: list[str] = []
    active_sheet = ""
    if ext in CSV_EXTS:
        grid = _csv_rows(path)
    elif ext in XLSX_EXTS:
        grid, sheets, active_sheet = _xlsx_rows(path, sheet)
    else:
        grid, sheets, active_sheet = _xls_rows(path, sheet)

    table = _build_table(grid, header_row, max_rows)
    table.update({
        "path": path,
        "name": os.path.basename(path),
        "ext": ext,
        "sheets": sheets,
        "sheet": active_sheet,
        "header_row": header_row,
        "max_rows": max_rows,
    })
    return table


@contextmanager
def _atomic_write(path: str):
    """Yield a scratch path beside ``path``, then swap it in once the write finished.

    Saves land in the operator's own file, so a write that dies half way through would
    otherwise leave a truncated CSV where their data used to be. ``os.replace`` is
    atomic within a filesystem — the file ends up either untouched or fully rewritten,
    never a mix — which is why the scratch file is created in the same directory.
    """
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".rb_save_",
                               suffix=os.path.splitext(path)[1])
    os.close(fd)
    try:
        yield tmp
        try:
            shutil.copymode(path, tmp)   # keep the original's permission bits
        except OSError:
            # Brand new file (the ledger export): mkstemp made it 0600, which is not
            # what a file the operator just created should look like. Fall back to
            # whatever their umask says a new file gets.
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _resolve_edits(grid: list[list[str]], header_row: int, edits: list[dict]) -> dict[tuple[int, int], str]:
    """Map the viewer's ``{row: <the # column>, col: <column index>}`` onto physical
    ``(grid row, column)`` pairs. Out-of-range edits are an error, not a silent skip —
    a mismatch means the file changed under the operator and we must not guess."""
    data = _data_rows(grid, header_row)
    resolved: dict[tuple[int, int], str] = {}
    for edit in edits:
        row_no = _int_or(edit.get("row"), 0)
        col = _int_or(edit.get("col"), -1)
        if row_no < 1 or row_no > len(data):
            raise ValueError(f"Row {row_no} is no longer in the file — re-read it and redo that edit")
        if col < 0:
            raise ValueError(f"Bad column index {col}")
        resolved[(data[row_no - 1][0], col)] = str(edit.get("value") or "")
    return resolved


def _write_csv_edits(path: str, header_row: int, edits: list[dict]) -> int:
    grid, delimiter, has_bom, lineterm = _csv_grid(path)
    resolved = _resolve_edits(grid, header_row, edits)
    for (row_idx, col), value in resolved.items():
        row = grid[row_idx]
        if col >= len(row):                      # ragged row — pad out to the column
            row.extend([""] * (col + 1 - len(row)))
        row[col] = value
    encoding = "utf-8-sig" if has_bom else "utf-8"
    with _atomic_write(path) as tmp:
        with open(tmp, "w", newline="", encoding=encoding) as fh:
            csv.writer(fh, delimiter=delimiter, lineterminator=lineterm).writerows(grid)
    return len(resolved)


def _write_xlsx_edits(path: str, sheet: str, header_row: int, edits: list[dict]) -> int:
    """Edit cells in place and re-save the workbook, so every other sheet, formula
    and bit of formatting survives — we only touch the cells the operator typed in."""
    from openpyxl import load_workbook

    grid, sheets, name = _xlsx_rows(path, sheet)
    resolved = _resolve_edits(grid, header_row, edits)

    wb = load_workbook(path, keep_vba=path.lower().endswith(".xlsm"))
    try:
        if name not in wb.sheetnames:
            raise ValueError(f"Sheet '{name}' is not in the workbook")
        ws = wb[name]
        for (row_idx, col), value in resolved.items():
            ws.cell(row=row_idx + 1, column=col + 1).value = value
        # The workbook is fully in memory by now, so writing to the scratch file and
        # swapping is safe even though the source is the file we're replacing.
        with _atomic_write(path) as tmp:
            wb.save(tmp)
    finally:
        wb.close()
    return len(resolved)


def _cleanup_old_uploads() -> None:
    """Drop viewer uploads older than a day — same spirit as _cleanup_old_logs."""
    cutoff = datetime.now().timestamp() - 24 * 3600
    try:
        entries = os.listdir(UPLOADS_DIR)
    except OSError:
        return
    for name in entries:
        full = os.path.join(UPLOADS_DIR, name)
        try:
            if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                os.remove(full)
        except OSError:
            pass


def _save_upload(filename: str, raw: bytes) -> str:
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    _cleanup_old_uploads()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(filename or "")).strip("_")
    base, ext = os.path.splitext(safe or "upload")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(UPLOADS_DIR, f"{base or 'upload'}_{ts}{ext.lower()}")
    with open(path, "wb") as fh:
        fh.write(raw)
    return path


# ------------------------------------------------------- ledger / item export
# Backs the "Ledger export" and "Item export" screens — a port of the
# RB_Ledger_Export notebook, extended to the matching item endpoint. Both pull a
# set of groups off the mapping service and drop them into one timestamped
# workbook: a SUMMARY sheet, a combined ALL_* sheet, then one sheet per group.
# The notebook used pandas + xlsxwriter; this writes with openpyxl, which the app
# already depends on, so no new requirement appears.

EXCEL_MAX_ROWS = 1_048_575            # Excel's per-sheet limit, minus the header row
GROUP_PREVIEW_ROWS = 200              # rows per group handed back for the on-screen table
INVALID_SHEET_CHARS = set(r"[]:*?/\\")

# Everything that differs between the two screens. `split_commas` is the real
# distinction: ledger group names are one per request, while the item endpoint
# takes a comma list in a single call ("GRP1,GRP2"), so an item line is passed
# through whole and only newlines start a new request.
GROUP_EXPORTS = {
    "ledger": {
        "label": "Ledger",
        "fetch": lambda *a, **kw: fetch_ledger_groups(*a, **kw),
        "url": lambda fmt: _ledger_url(fmt),
        "known": LEDGER_COLUMNS,
        "tag": "ledger_group",
        "file_prefix": "RB_Ledger_Export",
        "all_sheet": "ALL_LEDGERS",
        "defaults": list(DEFAULT_LEDGER_GROUPS),
        "split_commas": True,
    },
    "item": {
        "label": "Item",
        "fetch": lambda *a, **kw: fetch_item_groups(*a, **kw),
        "url": lambda fmt: _item_url(fmt),
        "known": ITEM_COLUMNS,
        "tag": "item_group",
        "file_prefix": "RB_Item_Export",
        "all_sheet": "ALL_ITEMS",
        "defaults": [],
        "split_commas": False,
    },
}


def _safe_sheet_name(name: str, used: set[str]) -> str:
    """Excel sheet names: <= 31 chars, none of []:*?/\\ , and unique in the book.
    A collision gets a ``_1`` suffix that eats into the name rather than overflowing."""
    clean = "".join("_" if ch in INVALID_SHEET_CHARS else ch for ch in str(name)).strip() or "Sheet"
    clean = clean[:31]
    base, n = clean, 1
    while clean.lower() in used:
        suffix = f"_{n}"
        clean = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(clean.lower())
    return clean


def _autosize(worksheet, columns: list[str], rows: list[list[str]]) -> None:
    """Best-effort column widths — the first 2000 rows decide, capped at 60 chars."""
    from openpyxl.utils import get_column_letter

    for idx, col in enumerate(columns):
        longest = len(str(col))
        for row in rows[:2000]:
            if idx < len(row):
                longest = max(longest, len(str(row[idx])))
        worksheet.column_dimensions[get_column_letter(idx + 1)].width = min(longest + 2, 60)


def _group_columns(frames: dict[str, list[dict]], known: tuple[str, ...]) -> list[str]:
    """The known columns first, then anything extra the API sent, in first-seen
    order — so a new field upstream lands in the workbook instead of being dropped."""
    columns = list(known)
    for rows in frames.values():
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
    return columns


def _group_grid(rows: list[dict], columns: list[str]) -> list[list[str]]:
    return [[str(row.get(c, "")) for c in columns] for row in rows]


def _write_sheet(wb, title: str, columns: list[str], grid: list[list[str]]):
    """One header row + the grid, header frozen, columns fitted."""
    ws = wb.create_sheet(title)
    ws.freeze_panes = "A2"
    _autosize(ws, columns, grid)
    ws.append(columns)
    for row in grid:
        ws.append(row)
    return ws


def _write_group_workbook(
    path: str,
    meta: list[tuple[str, str]],
    frames: dict[str, list[dict]],
    summary: list[dict],
    columns: list[str],
    all_sheet: str,
    group_label: str,
) -> list[dict]:
    """Write the workbook and return one ``{sheet, rows}`` entry per sheet written.

    Write-only mode keeps a six-figure dump off the heap; the trade-off is that
    every sheet has to be finished before the next one starts, which is why the
    grids are built up front.
    """
    from openpyxl import Workbook

    all_grid: list[list[str]] = []
    for rows in frames.values():
        all_grid.extend(_group_grid(rows, columns))

    wb = Workbook(write_only=True)
    used: set[str] = set()
    written: list[dict] = []

    # --- SUMMARY: the run's parameters, then the per-group log ------------------
    sname = _safe_sheet_name("SUMMARY", used)
    ws = wb.create_sheet(sname)
    sum_cols = ["sl_no", group_label, "row_count", "seconds", "api_response"]
    sum_keys = ["sl_no", "group", "row_count", "seconds", "api_response"]
    sum_grid = [[str(s.get(k, "")) for k in sum_keys] for s in summary]
    _autosize(ws, sum_cols, sum_grid + [[k, v, "", "", ""] for k, v in meta])
    ws.append(["parameter", "value"])
    for key, value in meta:
        ws.append([key, value])
    ws.append([])
    ws.append([])
    ws.append(sum_cols)
    for row in sum_grid:
        ws.append(row)
    written.append({"sheet": sname, "rows": len(summary)})

    # --- the combined sheet, split further if it outgrows one ------------------
    if all_grid:
        if len(all_grid) > EXCEL_MAX_ROWS:
            for part, start in enumerate(range(0, len(all_grid), EXCEL_MAX_ROWS), start=1):
                chunk = all_grid[start:start + EXCEL_MAX_ROWS]
                cname = _safe_sheet_name(f"{all_sheet}_{part}", used)
                _write_sheet(wb, cname, columns, chunk)
                written.append({"sheet": cname, "rows": len(chunk)})
        else:
            sname = _safe_sheet_name(all_sheet, used)
            _write_sheet(wb, sname, columns, all_grid)
            written.append({"sheet": sname, "rows": len(all_grid)})

    # --- one sheet per group, empty groups included so the gap is visible -------
    for grp, rows in frames.items():
        sname = _safe_sheet_name(grp, used)
        grid = _group_grid(rows, columns)[:EXCEL_MAX_ROWS]
        _write_sheet(wb, sname, columns, grid)
        entry = {"sheet": sname, "rows": len(grid)}
        if len(rows) > EXCEL_MAX_ROWS:
            entry["truncated_from"] = len(rows)
        written.append(entry)

    with _atomic_write(path) as tmp:
        wb.save(tmp)
    return written


def _group_export_dir(settings: dict, kind: str) -> str:
    saved = str(settings.get(f"{kind}_export_dir") or "").strip()
    return saved if os.path.isdir(saved) else APP_DIR


def _parse_group_lines(raw, split_commas: bool) -> list[str]:
    """Accepts a list, or newline separated text. Group names contain spaces, so
    whitespace never splits. Commas split only for ledgers — an item line keeps its
    commas, because that is how the item endpoint batches several groups per call.
    """
    if isinstance(raw, list):
        items = [str(g) for g in raw]
    else:
        items = re.split(r"[\n,]+" if split_commas else r"[\n]+", str(raw or ""))
    out: list[str] = []
    for item in items:
        # A line kept whole still gets its comma list tidied — a stray trailing comma
        # is a 404 from the item service, and cleaning it here keeps the sheet name and
        # the summary row showing exactly what was sent.
        item = item.strip() if split_commas else _clean_group_list(item)
        if item and item not in out:
            out.append(item)
    return out


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    settings = _load_settings()
    new_root_cfg = _resolve_root(settings.get("realbooks_root"), DEFAULT_REALBOOKS_ROOT)
    old_root_cfg = _resolve_root(settings.get("realbooks_root_old"), DEFAULT_REALBOOKS_ROOT_OLD)
    return render_template(
        "index.html",
        recent_jobs=settings.get("recent_jobs") or [],
        defaults={
            # Per-format roots shown side by side in the sidebar (configured values,
            # not the fallback-resolved one). old_root_fallback flags when old format
            # would borrow the new root because its own folder is empty/missing.
            "realbooks_root_new": new_root_cfg,
            "realbooks_root_old": old_root_cfg,
            "old_root_fallback": not _dir_has_entries(old_root_cfg),
            "rlb_module_type": settings.get("rlb_module_type", "inventory"),
            "file_ext_type": settings.get("file_ext_type", "xlsx,xls"),
            "uid_create": settings.get("uid_create", "1111"),
            "uid_update": settings.get("uid_update", "1111"),
            "is_ledger_creation": settings.get("is_ledger_creation", "1"),
            "is_item_creation": settings.get("is_item_creation", "1"),
            "is_cc_creation": settings.get("is_cc_creation", "0"),
            "is_tagg_creation": settings.get("is_tagg_creation", "0"),
            "menu_list_format": settings.get("menu_list_format", "new"),
            # Shown verbatim so the operator can see what is set and replace it;
            # the env fallback is not echoed, only flagged via nextgen_cookie_set.
            "nextgen_cookie": settings.get("nextgen_cookie", ""),
            "nextgen_cookie_set": bool(_nextgen_cookie_setting()),
            "edit_uid_update": settings.get("edit_uid_update", "1111"),
            "edit_is_ledger_creation": settings.get("edit_is_ledger_creation", "0"),
            "edit_is_item_creation": settings.get("edit_is_item_creation", "1"),
            "edit_is_cc_creation": settings.get("edit_is_cc_creation", "0"),
            "edit_is_tagg_creation": settings.get("edit_is_tagg_creation", "0"),
            "delete_uid_update": settings.get("delete_uid_update", "1111"),
            "realbooks_root": _realbooks_root(),
            "viewer_path": settings.get("viewer_path", ""),
            "viewer_header_row": settings.get("viewer_header_row", "1"),
            "viewer_max_rows": str(settings.get("viewer_max_rows", DEFAULT_TABLE_ROWS)),
            "ledger_cid": settings.get("ledger_cid", ""),
            "ledger_box_id": settings.get("ledger_box_id", ""),
            "ledger_groups": "\n".join(_parse_group_lines(
                settings.get("ledger_groups") or GROUP_EXPORTS["ledger"]["defaults"], True)),
            "ledger_export_dir": _group_export_dir(settings, "ledger"),
            "item_cid": settings.get("item_cid", ""),
            "item_box_id": settings.get("item_box_id", ""),
            "item_groups": "\n".join(_parse_group_lines(
                settings.get("item_groups") or GROUP_EXPORTS["item"]["defaults"], False)),
            "item_export_dir": _group_export_dir(settings, "item"),
        },
    )


@app.route("/api/fetch-job", methods=["POST"])
def api_fetch():
    body = request.get_json(silent=True) or {}
    job = (body.get("job") or "").strip()
    if not job:
        return jsonify(error="job is required"), 400
    try:
        data = api_fetch_job(job)
        data = _enrich_job(data)
        _record_recent_job(str(data.get("job") or job))
        log_path = _write_job_log(data)
        if log_path:
            data["_log_path"] = log_path
        return jsonify(ok=True, data=data)
    except SystemExit as e:
        traceback.print_exc()
        return jsonify(error=str(e), trace=traceback.format_exc()), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500


@app.route("/api/post-comment", methods=["POST"])
def api_post_comment_route():
    # Posts a comment (default "Deployed") on a job ticket at tasks.realbooks.in.
    # The front-end fires this automatically once AddMenu returns
    # "MenuName Successfully Created", mirroring the manual deploy sign-off.
    body = request.get_json(silent=True) or {}
    job = (body.get("job") or "").strip()
    text = (str(body.get("text") or "").strip()) or "Deployed"
    if not job:
        return jsonify(error="job is required"), 400
    try:
        result = api_post_comment(job, text=text)
        return jsonify(ok=True, result=result)
    except SystemExit as e:
        traceback.print_exc()
        return jsonify(error=str(e), trace=traceback.format_exc()), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500


@app.route("/api/menu-search", methods=["POST"])
def api_menu_search():
    body = request.get_json(silent=True) or {}
    cid = (body.get("cid") or "").strip()
    box_id = (body.get("box_id") or "").strip()
    segid_raw = (body.get("segids") or "").strip()
    segids = [s for s in re.split(r"[\s,]+", segid_raw) if s]
    menu_name = (body.get("menu_name") or "").strip()
    domain_alias = (body.get("domain_alias") or "").strip()
    if not cid or not segids or not box_id:
        return jsonify(error="cid, segids, box_id are required"), 400
    err = _nextgen_cookie_error()
    if err:
        return err
    try:
        rows = fetch_menu_list(
            cid, segids, box_id, menu_name,
            return_all=True,
            **_remote_kwargs(),
        )
        for row in rows:
            if not row.get("menu_name") and menu_name:
                row["menu_name"] = menu_name
            if not row.get("domain_alias") and domain_alias:
                row["domain_alias"] = domain_alias
        return jsonify(ok=True, rows=rows, deduped=_dedupe_menu_rows(rows))
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500


@app.route("/api/add-menu", methods=["POST"])
def api_add_menu():
    body = request.get_json(silent=True) or {}
    is_old = _menu_list_format() == "old"
    required = ["cid", "segids", "box_id", "menu_name", "domain_alias", "py_file"]
    # New format always needs a template; old format only when "Is template file" is ticked.
    if not is_old:
        required.append("template_file")
    missing = [k for k in required if not body.get(k)]
    if missing:
        return jsonify(error="Missing: " + ", ".join(missing)), 400
    err = _nextgen_cookie_error()
    if err:
        return err

    segids_raw = body.get("segids")
    if isinstance(segids_raw, str):
        segids = [s for s in re.split(r"[\s,]+", segids_raw) if s]
    else:
        segids = list(segids_raw or [])

    # Only forward fields the client actually sent; empties fall back to add_menu's
    # defaults (the old-format form omits the uid/creation-flag fields entirely).
    kwargs = {k: str(body.get(k, "")).strip() for k in (
        "rlb_module_type", "file_ext_type", "uid_create", "uid_update",
        "is_ledger_creation", "is_item_creation", "is_cc_creation", "is_tagg_creation",
    ) if str(body.get(k, "")).strip()}

    settings = _load_settings()
    for k, v in kwargs.items():
        if v:
            settings[k] = v
    _save_settings(settings)

    # Old format also needs a fixed Db Connection File (.txt); take it from the
    # request, else fall back to the saved setting (NEWFILE also honors the env var).
    db_connection_file = str(
        body.get("db_connection_file") or settings.get("old_db_connection_file") or ""
    ).strip()

    try:
        result = add_menu(
            cid=str(body["cid"]).strip(),
            segids=segids,
            box_id=str(body["box_id"]).strip(),
            menu_name=str(body["menu_name"]).strip(),
            gstin=str(body.get("gstin") or "").strip(),
            domain_alias=str(body["domain_alias"]).strip(),
            py_file_path=str(body["py_file"]).strip(),
            template_file_path=str(body.get("template_file") or "").strip(),
            db_connection_file=db_connection_file,
            mpau=str(body.get("mpau") or "0").strip(),
            api_file_path=str(body.get("api_file") or "").strip(),
            **kwargs,
            **_remote_kwargs(),
        )
        return jsonify(ok=True, result=result)
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500


@app.route("/api/edit-menu", methods=["POST"])
def api_edit_menu():
    body = request.get_json(silent=True) or {}
    required = ["cid", "box_id", "py_file", "template_file"]
    is_nextgen = _menu_list_format() == "nextgen"
    # The exvspy EditMenu addresses the menu by name; manualmapping never took one.
    if is_nextgen:
        required.append("menu_name")
    missing = [k for k in required if not body.get(k)]
    if missing:
        return jsonify(error="Missing: " + ", ".join(missing)), 400
    err = _nextgen_cookie_error()
    if err:
        return err

    kwargs = {
        "gstin": str(body.get("gstin") or "").strip(),
        "uid_update": str(body.get("uid_update") or "1111").strip(),
        "is_ledger_creation": str(body.get("is_ledger_creation") or "0").strip(),
        "is_item_creation": str(body.get("is_item_creation") or "1").strip(),
        "is_cc_creation": str(body.get("is_cc_creation") or "0").strip(),
        "is_tagg_creation": str(body.get("is_tagg_creation") or "0").strip(),
    }

    settings = _load_settings()
    for k, v in kwargs.items():
        settings[f"edit_{k}"] = v
    _save_settings(settings)

    try:
        result = edit_menu(
            cid=str(body["cid"]).strip(),
            box_id=str(body["box_id"]).strip(),
            py_file_path=str(body["py_file"]).strip(),
            template_file_path=str(body["template_file"]).strip(),
            menu_name=str(body.get("menu_name") or "").strip(),
            **kwargs,
            **_remote_kwargs(),
        )
        return jsonify(ok=True, result=result)
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500


@app.route("/api/delete-menu", methods=["POST"])
def api_delete_menu():
    body = request.get_json(silent=True) or {}
    raw = body.get("ids") or ""
    confirm = (body.get("confirm") or "").strip()
    uid_update = (body.get("uid_update") or "1111").strip()

    if isinstance(raw, list):
        tokens = raw
    else:
        tokens = re.split(r"[\s,]+", str(raw))
    seen: set[str] = set()
    ids: list[str] = []
    for tok in tokens:
        tok = str(tok).strip().strip('"').strip("'").rstrip(",")
        if tok and tok not in seen:
            seen.add(tok)
            ids.append(tok)
    if not ids:
        return jsonify(error="No IDs provided"), 400

    expected = f"realbooks@@adansa{date.today().strftime('%d%m')}"
    if confirm != expected:
        return jsonify(error="Confirmation password did not match"), 403
    err = _nextgen_cookie_error()
    if err:
        return err

    settings = _load_settings()
    settings["delete_uid_update"] = uid_update
    _save_settings(settings)

    try:
        results = delete_menu(ids, uid_update=uid_update, **_remote_kwargs())
        return jsonify(ok=True, results=results, count=len(results))
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500


@app.route("/api/find-domain", methods=["POST"])
def api_find_domain():
    body = request.get_json(silent=True) or {}
    domain_alias = (body.get("domain_alias") or "").strip()
    folder = _find_domain_folder(domain_alias)
    return jsonify(ok=True, folder=folder, root=_realbooks_root())


@app.route("/api/search-files", methods=["POST"])
def api_search_files():
    body = request.get_json(silent=True) or {}
    domain_alias = (body.get("domain_alias") or "").strip()
    term = (body.get("term") or "").strip()
    file_kind = (body.get("kind") or "py").strip()  # "py" or "template"
    if not domain_alias:
        return jsonify(error="domain_alias is required"), 400
    if not term:
        return jsonify(error="term is required"), 400

    search_root = _find_domain_folder(domain_alias)
    if not search_root:
        return jsonify(error=f"No folder matching '{domain_alias}' under {_realbooks_root()}"), 404

    default_ext = ".py" if file_kind == "py" else ".xls*"
    if "*" not in term and "?" not in term:
        pattern = f"*{term}*{default_ext}" if "." not in term else f"*{term}*"
    else:
        pattern = term
    return jsonify(ok=True, pattern=pattern, results=_search_py_files(pattern, root=search_root))


@app.route("/api/clone-file", methods=["POST"])
def api_clone_file():
    body = request.get_json(silent=True) or {}
    src = (body.get("src") or "").strip()
    new_name = (body.get("new_name") or "").strip()
    deploy_to_domain = (body.get("deploy_to_domain") or "").strip()
    if not src or not new_name or not deploy_to_domain:
        return jsonify(error="src, new_name, deploy_to_domain are required"), 400
    if not os.path.isfile(src):
        return jsonify(error=f"src not found: {src}"), 404
    target = _find_domain_folder(deploy_to_domain)
    if not target:
        return jsonify(error=f"No folder matching '{deploy_to_domain}' under {_realbooks_root()}"), 404
    try:
        clone_path, renamed = _clone_py_file(src, new_name, target_folder=target)
        return jsonify(ok=True, clone_path=clone_path, renamed=renamed)
    except OSError as e:
        return jsonify(error=str(e)), 500


@app.route("/api/create-py", methods=["POST"])
def api_create_py():
    body = request.get_json(silent=True) or {}
    job = (body.get("job") or "").strip()
    title = (body.get("title") or "").strip()
    domain_alias = (body.get("domain_alias") or "").strip()
    src = (body.get("src") or "").strip()
    overwrite = bool(body.get("overwrite") or False)

    missing = [n for n, v in [("job", job), ("title", title), ("domain_alias", domain_alias)] if not v]
    if missing:
        return jsonify(error="Missing: " + ", ".join(missing)), 400

    folder = _find_domain_folder(domain_alias)
    if not folder:
        return jsonify(error=f"No folder matching '{domain_alias}' under {_realbooks_root()}"), 404

    if not src or not os.path.isfile(src):
        return jsonify(error="Provide an existing source py file path"), 400

    filename = f"{_sanitize(job)}_{_sanitize(title)}.py"
    dest = os.path.join(folder, filename)
    if os.path.exists(dest) and not overwrite:
        return jsonify(error="exists", dest=dest), 409
    try:
        shutil.copyfile(src, dest)
    except OSError as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True, dest=dest)


@app.route("/api/list-dir", methods=["POST"])
def api_list_dir():
    body = request.get_json(silent=True) or {}
    path = (body.get("path") or "").strip()
    term = (body.get("term") or "").strip().lower()
    dirs_only = bool(body.get("dirs_only") or False)
    raw_exts = body.get("extensions") or []
    if isinstance(raw_exts, str):
        raw_exts = [raw_exts]
    extensions = []
    for ext in raw_exts:
        ext = str(ext).strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        extensions.append(ext)

    if not path or not os.path.isdir(path):
        path = _realbooks_root() if os.path.isdir(_realbooks_root()) else os.path.expanduser("~")

    parent = os.path.dirname(path.rstrip(os.sep))
    if parent == path or not os.path.isdir(parent):
        parent = None

    dirs: list[dict] = []
    files: list[dict] = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if os.path.isdir(full):
                if term and term not in name.lower():
                    continue
                dirs.append({
                    "name": name,
                    "path": full,
                    "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
                continue
            if dirs_only:
                continue
            if not os.path.isfile(full):
                continue
            if extensions and not any(name.lower().endswith(ext) for ext in extensions):
                continue
            if term and term not in name.lower():
                continue
            files.append({
                "name": name,
                "path": full,
                "size_kb": round(st.st_size / 1024, 2),
                "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "_mtime": st.st_mtime,
            })
    except OSError as e:
        return jsonify(error=str(e)), 400

    files.sort(key=lambda x: x["_mtime"], reverse=True)
    for f in files:
        f.pop("_mtime", None)

    return jsonify(ok=True, path=path, parent=parent, dirs=dirs, files=files)


@app.errorhandler(413)
def _too_large(_e):
    # Flask aborts oversized uploads before the route runs; keep the response JSON
    # so the viewer's toast shows something useful instead of "HTTP 413".
    limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify(error=f"Upload is larger than {limit} MB — read it by server path instead"), 413


@app.route("/api/read-table", methods=["POST"])
def api_read_table():
    """Read a CSV/Excel file into columns + rows for the CSV / Excel screen.

    Two shapes of request:
      * multipart with ``file`` — a browser-side upload; the bytes are kept under
        ``.uploads/`` and the saved path comes back so sheet/header changes can
        re-read without another upload.
      * JSON with ``path`` — a server-side file, same as every other picker here.
    Both also take ``sheet``, ``header_row`` (1-based, 0 = no header) and ``max_rows``.
    """
    upload = request.files.get("file")
    src = request.form if upload is not None else (request.get_json(silent=True) or {})

    sheet = str(src.get("sheet") or "").strip()
    header_row = max(0, _int_or(src.get("header_row"), 1))
    max_rows = _int_or(src.get("max_rows"), DEFAULT_TABLE_ROWS)
    max_rows = max(1, min(max_rows, MAX_TABLE_ROWS))

    try:
        if upload is not None:
            filename = upload.filename or ""
            ext = os.path.splitext(filename)[1].lower()
            if ext not in TABLE_EXTS:
                return jsonify(error=f"Unsupported file type '{ext or filename}' — expected one of: "
                                     + ", ".join(sorted(TABLE_EXTS))), 400
            path = _save_upload(filename, upload.read())
        else:
            path = str(src.get("path") or "").strip()

        table = _read_table_file(path, sheet, header_row, max_rows)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500

    # Remember the last file the operator looked at, like every other screen does.
    settings = _load_settings()
    settings["viewer_path"] = table["path"]
    settings["viewer_header_row"] = str(header_row)
    settings["viewer_max_rows"] = str(max_rows)
    _save_settings(settings)

    return jsonify(ok=True, **table)


@app.route("/api/write-table", methods=["POST"])
def api_write_table():
    """Apply the viewer's cell edits back to the file they came from.

    Only the edited cells are touched — the file is re-read here and each edit is
    mapped onto its physical row/column, so rows past ``max_rows``, blank rows and
    trimmed columns are all left exactly as they were. The save goes into the original
    file (atomically, see ``_atomic_write``); no copy is left beside it.
    """
    body = request.get_json(silent=True) or {}
    path = str(body.get("path") or "").strip()
    sheet = str(body.get("sheet") or "").strip()
    header_row = max(0, _int_or(body.get("header_row"), 1))
    edits = body.get("edits") or []

    # A Google Sheet is written in place — there is no local file, and the change is
    # live for everyone on the document the moment it lands.
    if _is_gsheet_ref(path):
        if not isinstance(edits, list) or not edits:
            return jsonify(error="No edits to save"), 400
        try:
            count = _gsheet_write_edits(path, sheet, header_row, edits)
        except (ValueError, RuntimeError) as e:
            return jsonify(error=str(e)), 400
        except Exception as e:
            traceback.print_exc()
            return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500
        return jsonify(ok=True, path=path, name=sheet or "Google Sheet", saved=count)

    if not path:
        return jsonify(error="path is required"), 400
    if not os.path.isfile(path):
        return jsonify(error=f"Not a file: {path}"), 404
    if not isinstance(edits, list) or not edits:
        return jsonify(error="No edits to save"), 400

    ext = os.path.splitext(path)[1].lower()
    if ext in XLS_EXTS:
        return jsonify(error="Saving .xls isn't supported — re-save the file as .xlsx first"), 400
    if ext not in CSV_EXTS | XLSX_EXTS:
        return jsonify(error=f"Cannot write '{ext}' files"), 400
    if not os.access(os.path.dirname(path) or ".", os.W_OK):
        return jsonify(error=f"No write permission in {os.path.dirname(path)}"), 403

    if not os.access(path, os.W_OK):
        return jsonify(error=f"File is read-only: {path}"), 403

    try:
        if ext in CSV_EXTS:
            count = _write_csv_edits(path, header_row, edits)
        else:
            count = _write_xlsx_edits(path, sheet, header_row, edits)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500

    return jsonify(ok=True, path=path, name=os.path.basename(path), saved=count)


def _run_group_export(kind: str):
    """Shared body of /api/ledger-export and /api/item-export.

    Fetch and write happen in one call on purpose: these responses run to hundreds of
    thousands of rows, so round-tripping them through the browser just to write a file
    would double the transfer. What comes back is the per-group summary, the path of
    the workbook, and a capped preview — the whole file is meant to be opened on the
    CSV / Excel screen, which already reads server-side paths.
    """
    spec = GROUP_EXPORTS[kind]
    body = request.get_json(silent=True) or {}
    cid = str(body.get("cid") or "").strip()
    box_id = str(body.get("box_id") or "").strip()
    groups = _parse_group_lines(body.get("groups"), spec["split_commas"])
    write_file = body.get("write_file", True)

    missing = [k for k, v in (("cid", cid), ("box_id", box_id), ("groups", groups)) if not v]
    if missing:
        return jsonify(error="Missing: " + ", ".join(missing)), 400

    settings = _load_settings()
    out_dir = str(body.get("out_dir") or "").strip() or _group_export_dir(settings, kind)
    if write_file and not os.path.isdir(out_dir):
        return jsonify(error=f"Output folder does not exist: {out_dir}"), 400

    settings[f"{kind}_cid"] = cid
    settings[f"{kind}_box_id"] = box_id
    settings[f"{kind}_groups"] = groups
    if write_file:
        settings[f"{kind}_export_dir"] = out_dir
    _save_settings(settings)

    fmt = _menu_list_format()
    err = _nextgen_cookie_error()
    if err:
        return err
    try:
        frames, summary = spec["fetch"](
            cid, box_id, groups, format_type=fmt, cookie=_nextgen_cookie_setting(),
            retries=_int_or(body.get("retries"), RB_FETCH_RETRIES),
            timeout=_int_or(body.get("timeout"), RB_FETCH_TIMEOUT),
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500

    columns = _group_columns(frames, spec["known"])
    preview: list[list[str]] = []
    for rows in frames.values():
        preview.extend(_group_grid(rows[:GROUP_PREVIEW_ROWS], columns))
    total_rows = sum(len(rows) for rows in frames.values())
    url = spec["url"](fmt)

    result = {
        "ok": True,
        "kind": kind,
        "cid": cid,
        "box_id": _rlb_box_id(box_id),
        "format": fmt,
        "url": url,
        "summary": summary,
        "columns": columns,
        "preview": preview,
        "preview_capped": len(preview) < total_rows,
        "total_rows": total_rows,
    }

    if not write_file:
        return jsonify(**result)

    run_ts = datetime.now()
    name = (f"{spec['file_prefix']}_{_sanitize(cid)}_{_sanitize(_rlb_box_id(box_id))}"
            f"_{run_ts.strftime('%Y-%m-%d_%H-%M-%S')}.xlsx")
    path = os.path.join(out_dir, name)
    meta = [
        ("Generated On", run_ts.strftime("%Y-%m-%d %H:%M:%S")),
        ("CID", cid),
        ("Box ID", _rlb_box_id(box_id)),
        ("API URL", url),
        ("Groups Requested", str(len(summary))),
        ("Groups With Data", str(sum(1 for s in summary if s["row_count"] > 0))),
        ("Total Rows", str(total_rows)),
    ]
    try:
        sheets = _write_group_workbook(
            path, meta, frames, summary, columns, spec["all_sheet"], spec["tag"],
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500

    result.update({
        "path": path,
        "name": name,
        "size_mb": round(os.path.getsize(path) / (1024 * 1024), 2),
        "sheets": sheets,
    })
    return jsonify(**result)


@app.route("/api/ledger-export", methods=["POST"])
def api_ledger_export():
    return _run_group_export("ledger")


@app.route("/api/item-export", methods=["POST"])
def api_item_export():
    return _run_group_export("item")



@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(_load_settings())
    body = request.get_json(silent=True) or {}
    settings = _load_settings()
    for k, v in body.items():
        if v in ("", None):
            settings.pop(k, None)
        else:
            settings[k] = v
    _save_settings(settings)
    # Echo the effective (format-aware) root so the UI can refresh its label after a format toggle.
    return jsonify(ok=True, settings=settings, realbooks_root=_realbooks_root())


_cleanup_old_logs()
_cleanup_old_uploads()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
