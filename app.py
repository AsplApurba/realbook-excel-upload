"""Flask web wrapper around NEWFILE.py — exposes job lookup + menu add/edit/delete over HTTP."""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
from datetime import date, datetime

import sys

import requests
from flask import Flask, g, render_template, request, jsonify, send_from_directory
from selenium.webdriver.support.ui import WebDriverWait

from NEWFILE import (
    MENULIST_URL,
    RLB_BOX_PREFIX,
    RLB_PASSWORD_PREFIX,
    _pad_box,
    build_driver,
    login,
    fetch_job,
    fetch_menu_list,
    add_menu,
    edit_menu,
    delete_menu,
)

app = Flask(__name__)
# Reread templates on each request — saves a restart on every HTML edit.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# ---------------- logging ----------------
# When frozen by PyInstaller, __file__ lives inside a temp extraction dir;
# we want logs next to the actual executable instead.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)


def _cleanup_old_logs() -> None:
    """Delete every log file whose date stamp isn't today. Mirrors gui._cleanup_old_logs."""
    today = datetime.now().strftime("%Y%m%d")
    pattern = re.compile(r"^web_(\d{8})\.log$")
    try:
        for name in os.listdir(LOGS_DIR):
            m = pattern.match(name)
            if not m or m.group(1) == today:
                continue
            try:
                os.remove(os.path.join(LOGS_DIR, name))
            except OSError:
                pass
    except OSError:
        pass


_cleanup_old_logs()
LOG_PATH = os.path.join(LOGS_DIR, f"web_{datetime.now().strftime('%Y%m%d')}.log")

logger = logging.getLogger("realbook_web")
logger.setLevel(logging.INFO)
if not logger.handlers:
    h = logging.FileHandler(LOG_PATH, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(h)


@app.before_request
def _log_request_start():
    g._t0 = time.time()
    if request.path.startswith("/api/"):
        try:
            body_summary = ""
            if request.is_json:
                body_summary = json.dumps(request.get_json(silent=True) or {}, ensure_ascii=False)[:500]
            elif request.form:
                # Don't log file bytes; just field names and short values.
                body_summary = "; ".join(
                    f"{k}={v[:100]}" for k, v in request.form.items()
                )[:500]
                if request.files:
                    body_summary += " | files=" + ",".join(
                        f"{k}:{f.filename}" for k, f in request.files.items() if f.filename
                    )
            logger.info(f"-> {request.method} {request.path} {body_summary}")
        except Exception as e:
            logger.info(f"-> {request.method} {request.path} (body-log-error: {e})")


@app.after_request
def _log_request_end(response):
    if request.path.startswith("/api/"):
        dt = (time.time() - getattr(g, "_t0", time.time())) * 1000
        logger.info(f"<- {request.method} {request.path} {response.status_code} ({dt:.0f}ms)")
    return response

REALBOOKS_ROOT = os.environ.get(
    "REALBOOKS_ROOT", "/home/adansa/Desktop/Apurba/Realbooks/NoteBookWorking/RealBooks"
)
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build", ".ipynb_checkpoints"}

# Extra directories the basename walk also searches when a file isn't found
# under REALBOOKS_ROOT — covers the common case where the user just
# downloaded the template into Downloads/. Override with EXTRA_FILE_ROOTS
# (colon-separated paths).
EXTRA_FILE_ROOTS = [
    p.strip() for p in os.environ.get(
        "EXTRA_FILE_ROOTS",
        f"{os.path.expanduser('~/Downloads')}:{os.path.expanduser('~/Desktop')}:{os.path.dirname(os.path.abspath(__file__))}",
    ).split(":") if p.strip()
]

# Selenium drivers are NOT thread-safe and Chrome is heavy to spin up per request.
_driver_lock = threading.Lock()
_driver = None
_wait = None


def _get_driver():
    global _driver, _wait
    if _driver is None:
        _driver = build_driver(headless=True)
        _wait = WebDriverWait(_driver, 25)
        login(_driver, _wait)
    return _driver, _wait


def _sanitize(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_")
    return name or "untitled"


def _find_domain_folder(domain_alias: str) -> str | None:
    if not domain_alias or not os.path.isdir(REALBOOKS_ROOT):
        return None
    target = domain_alias.strip().lower()
    exact = None
    loose = None
    for entry in os.listdir(REALBOOKS_ROOT):
        full = os.path.join(REALBOOKS_ROOT, entry)
        if not os.path.isdir(full):
            continue
        name = entry.strip().lower()
        if name == target:
            exact = full
            break
        if target in name or name in target:
            loose = loose or full
    return exact or loose


def _search_files(pattern: str) -> list[dict]:
    matches: list[dict] = []
    if not os.path.isdir(REALBOOKS_ROOT):
        return matches
    for dirpath, dirnames, filenames in os.walk(REALBOOKS_ROOT):
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


def _clone_file(original_path: str, new_name: str, target_folder: str) -> tuple[str, bool]:
    if "." not in new_name:
        _, ext = os.path.splitext(original_path)
        new_name = new_name + ext
    clone_path = os.path.join(target_folder, new_name)
    renamed = False
    if os.path.exists(clone_path):
        base, ext = os.path.splitext(new_name)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        clone_path = os.path.join(target_folder, f"{base}_{timestamp}{ext}")
        renamed = True
    shutil.copy2(original_path, clone_path)
    return clone_path, renamed


def _find_source_from_menu(menu_list: list[dict]) -> str:
    """Walk REALBOOKS_ROOT to find the first existing py file referenced by menu_list."""
    for row in menu_list or []:
        path = str(row.get("py_file_path") or "").strip()
        if path and os.path.isfile(path):
            return path
    wanted = {os.path.basename(str(r.get("py_file_path") or "").strip()) for r in (menu_list or [])}
    wanted.discard("")
    if not wanted or not os.path.isdir(REALBOOKS_ROOT):
        return ""
    for dirpath, dirs, files in os.walk(REALBOOKS_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name in wanted:
                return os.path.join(dirpath, name)
    return ""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/media/<path:filename>")
def media(filename):
    """Serve static media (DOG.mp4 etc.) from APP_DIR."""
    return send_from_directory(APP_DIR, filename)


_FLAT_FIELD_MAP = {
    "domain": "domain_alias",
    "box": "box_id",
    "box id": "box_id",
    "box no": "box_id",
    "cid": "cid",
    "c id": "cid",
    "seg id": "segid",
    "segid": "segid",
    "menu": "menu_name",
    "menu name": "menu_name",
    "gstin": "gstin",
}


def _flat_parse_description(description: str) -> dict:
    """Parse 'Key - Value' lines (e.g. 'Cid - 12996', 'Menu - AMEX', 'Seg id - 13009')."""
    out: dict = {}
    if not description:
        return out
    for line in description.splitlines():
        m = re.match(r"\s*([A-Za-z][A-Za-z ]*?)\s*[-:=]\s*(.+?)\s*$", line)
        if not m:
            continue
        key = re.sub(r"\s+", " ", m.group(1).strip().lower())
        val = m.group(2).strip()
        if not val:
            continue
        target = _FLAT_FIELD_MAP.get(key)
        if target and target not in out:
            out[target] = val
    return out


def _fill_from_flat(data: dict) -> None:
    """If fetch_job left key fields empty, recover them from the flat 'Key - Value' format.

    Mutates `data` in place. Populates both the top-level fields and the
    'deploy_from_*' / 'deploy_to_*' mirrors so downstream menu lookups work.
    """
    flat = _flat_parse_description(data.get("description", ""))
    if not flat:
        return
    if not data.get("box_id") and flat.get("box_id"):
        data["box_id"] = flat["box_id"]
    if not data.get("menu_name") and flat.get("menu_name"):
        data["menu_name"] = flat["menu_name"]
    if not data.get("gstin") and flat.get("gstin"):
        data["gstin"] = flat["gstin"]
    if not data.get("domain_alias") and flat.get("domain_alias"):
        data["domain_alias"] = flat["domain_alias"]

    cid = flat.get("cid", "")
    segid = flat.get("segid", "")
    box = flat.get("box_id", "") or data.get("box_id", "")
    menu = flat.get("menu_name", "") or data.get("menu_name", "")
    gstin = flat.get("gstin", "") or data.get("gstin", "")
    domain = flat.get("domain_alias", "") or data.get("domain_alias", "")
    segids = re.findall(r"\d+", segid) if segid else []

    for side in ("from", "to"):
        if cid and not data.get(f"deploy_{side}_cid"):
            data[f"deploy_{side}_cid"] = cid
        if segids and not data.get(f"deploy_{side}_segids"):
            data[f"deploy_{side}_segids"] = segids
        if box and not data.get(f"deploy_{side}_box"):
            data[f"deploy_{side}_box"] = box
        if menu and not data.get(f"deploy_{side}_menu"):
            data[f"deploy_{side}_menu"] = menu
        if gstin and not data.get(f"deploy_{side}_gstin"):
            data[f"deploy_{side}_gstin"] = gstin
        if domain and not data.get(f"deploy_{side}_domain"):
            data[f"deploy_{side}_domain"] = domain


def _fetch_menu_list_raw(cid: str, segids: list[str], box_id: str, side: str = "") -> list[dict]:
    """Return EVERY menu the server has for each (cid, segid, box). No menu_name filtering.

    Each item is the raw dict the server returned, with `segid` and `side`
    injected so the front-end can group/label the rows. Used to populate the
    full menu list — fetch_menu_list() only returns the single best match per segid.
    """
    out: list[dict] = []
    password = f"{RLB_PASSWORD_PREFIX}{date.today().strftime('%Y%m%d')}"
    for segid in segids:
        payload = {
            "cid": cid,
            "segid": segid,
            "rlb_box_id": f"{RLB_BOX_PREFIX}{_pad_box(box_id)}",
            "password": password,
            "file_name": "",
        }
        try:
            resp = requests.post(MENULIST_URL, data=payload, timeout=30).text
            items = json.loads(resp).get("data") or []
        except (requests.RequestException, ValueError):
            items = []
        for it in items:
            row = dict(it)
            row["segid"] = segid
            row["side"] = side
            row["py_file_path"] = str(it.get("py_file_path", "")).split("#@#", 1)[-1]
            row["template_file_path"] = str(it.get("temp_file_path", "")).split("#@#", 1)[-1]
            out.append(row)
    return out


@app.post("/api/job")
def api_job():
    job_number = (request.json or {}).get("job_number", "").strip()
    if not job_number:
        return jsonify({"error": "job_number required"}), 400
    try:
        with _driver_lock:
            driver, wait = _get_driver()
            data = fetch_job(driver, wait, job_number)
        _fill_from_flat(data)
        box_id = data["box_id"]
        # Query both Deploy From (the source — where py/template files already
        # live) and Deploy To (the destination — where we'll add/edit). The
        # match the user wants is usually on Deploy From; defaulting to only
        # Deploy To hides it.
        sides = []
        if data.get("deploy_from_cid") and data.get("deploy_from_segids"):
            sides.append(("from", data["deploy_from_cid"], data["deploy_from_segids"]))
        if data.get("deploy_to_cid") and data.get("deploy_to_segids"):
            sides.append(("to", data["deploy_to_cid"], data["deploy_to_segids"]))

        merged_match: list[dict] = []
        merged_all: list[dict] = []
        seen_ids = set()
        for side, cid, segids in sides:
            if not (cid and segids and box_id):
                continue
            for row in fetch_menu_list(cid, segids, box_id, menu_name=data["menu_name"]):
                row = dict(row); row["side"] = side
                if row.get("id") and row["id"] in seen_ids:
                    continue
                if row.get("id"): seen_ids.add(row["id"])
                merged_match.append(row)
            merged_all.extend(_fetch_menu_list_raw(cid, segids, box_id, side=side))

        # Dedupe the raw list by id too so a menu that appears on both sides
        # only renders once.
        deduped_all: list[dict] = []
        seen_all = set()
        for row in merged_all:
            rid = str(row.get("id") or "")
            if rid and rid in seen_all:
                continue
            if rid: seen_all.add(rid)
            deduped_all.append(row)

        if sides:
            data["menu_list"] = merged_match
            data["menu_list_all"] = deduped_all
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.post("/api/search")
def api_search():
    body = request.json or {}
    term = (body.get("term") or "").strip()
    kind = body.get("kind", "py")  # "py" or "template"
    if not term:
        return jsonify({"error": "term required"}), 400
    default_ext = ".py" if kind == "py" else ".xls*"
    if "*" in term or "?" in term:
        pattern = term
    elif "." in term:
        pattern = f"*{term}*"
    else:
        pattern = f"*{term}*{default_ext}"
    return jsonify({"pattern": pattern, "matches": _search_files(pattern)})


@app.post("/api/clone")
def api_clone():
    body = request.json or {}
    src = (body.get("src") or "").strip()
    new_name = (body.get("new_name") or "").strip()
    domain = (body.get("domain_alias") or "").strip()
    if not src or not os.path.isfile(src):
        return jsonify({"error": "src must be an existing file"}), 400
    if not new_name:
        return jsonify({"error": "new_name required"}), 400
    folder = _find_domain_folder(domain)
    if not folder:
        return jsonify({"error": f"No folder matching '{domain}' under {REALBOOKS_ROOT}"}), 404
    try:
        clone_path, renamed = _clone_file(src, new_name, folder)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"clone_path": clone_path, "renamed": renamed, "target_folder": folder})


@app.post("/api/create_py")
def api_create_py():
    """Auto-create a py file in the Deploy-TO domain folder, named {job}_{title}.py.

    Source: first existing py from menu_list, or `src` from the request body.
    """
    body = request.json or {}
    job = (body.get("job") or "").strip()
    title = (body.get("title") or "").strip()
    domain = (body.get("domain_alias") or "").strip()
    menu_list = body.get("menu_list") or []
    src = (body.get("src") or "").strip()
    overwrite = bool(body.get("overwrite"))

    missing = [k for k, v in [("job", job), ("title", title), ("domain_alias", domain)] if not v]
    if missing:
        return jsonify({"error": "Missing: " + ", ".join(missing)}), 400

    if not src or not os.path.isfile(src):
        src = _find_source_from_menu(menu_list)
    if not src or not os.path.isfile(src):
        return jsonify({"error": "Source py file not found — pass `src` explicitly"}), 404

    folder = _find_domain_folder(domain)
    if not folder:
        return jsonify({"error": f"No folder matching '{domain}' under {REALBOOKS_ROOT}"}), 404

    filename = f"{_sanitize(job)}_{_sanitize(title)}.py"
    dest = os.path.join(folder, filename)
    if os.path.exists(dest) and not overwrite:
        return jsonify({"error": "exists", "dest": dest, "needs_overwrite": True}), 409
    try:
        shutil.copyfile(src, dest)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"dest": dest, "src": src})


def _save_upload(field: str) -> str | None:
    f = request.files.get(field)
    if not f or not f.filename:
        return None
    fd, path = tempfile.mkstemp(suffix="_" + os.path.basename(f.filename))
    os.close(fd)
    f.save(path)
    return path


def _norm_name(s: str) -> str:
    """Strip non-alphanumerics and lowercase. Used for fuzzy basename matching.

    Same file is sometimes saved locally with extra punctuation — e.g. the
    server returns `JOB-159863160688.xlsx` but the local copy is
    `JOB-159863(160688).xlsx`. Normalizing both sides catches this.
    """
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _find_under_root(name: str) -> str | None:
    """Search REALBOOKS_ROOT and EXTRA_FILE_ROOTS for a file matching `name`.

    Tries an exact basename match first, then a normalized match
    (alphanumerics-only, case-insensitive) on the same extension so a slightly
    differently-named file (parentheses, spaces, etc.) still resolves.
    """
    if not name:
        return None
    target = os.path.basename(name)
    target_norm = _norm_name(target)
    target_ext = os.path.splitext(target)[1].lower()
    roots = [REALBOOKS_ROOT, *EXTRA_FILE_ROOTS]
    fuzzy_hit = None
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            if target in files:
                return os.path.join(dirpath, target)
            if not fuzzy_hit:
                for f in files:
                    if os.path.splitext(f)[1].lower() == target_ext and _norm_name(f) == target_norm:
                        fuzzy_hit = os.path.join(dirpath, f)
                        break
    return fuzzy_hit


def _resolve_file_input(field: str) -> tuple[str | None, bool, str]:
    """Return (path, is_temp, reason). Prefers uploaded file; falls back to '<field>_path' form value;
    finally searches REALBOOKS_ROOT by basename when the given path isn't found on disk.
    `reason` is empty on success and a human-readable explanation on failure.
    """
    up = _save_upload(field)
    if up:
        return up, True, ""
    p = (request.form.get(f"{field}_path") or "").strip()
    if not p:
        return None, False, f"{field}: no upload and no {field}_path provided"
    if os.path.isfile(p):
        return p, False, ""
    found = _find_under_root(p)
    if found:
        return found, False, ""
    roots_searched = ", ".join([REALBOOKS_ROOT, *EXTRA_FILE_ROOTS])
    return None, False, (
        f"{field}: '{p}' not found on disk and no file named "
        f"'{os.path.basename(p)}' found under any of: {roots_searched}"
    )


@app.post("/api/add_menu")
def api_add_menu():
    py_path, py_temp, py_err = _resolve_file_input("py_file")
    tpl_path, tpl_temp, tpl_err = _resolve_file_input("template_file")
    errs = [e for e in (py_err, tpl_err) if e]
    if errs:
        return jsonify({"error": " / ".join(errs)}), 400
    form = request.form
    try:
        result = add_menu(
            cid=form["cid"],
            segids=form.getlist("segids"),
            box_id=form["box_id"],
            menu_name=form["menu_name"],
            gstin=form.get("gstin", ""),
            domain_alias=form.get("domain_alias", ""),
            py_file_path=py_path,
            template_file_path=tpl_path,
            rlb_module_type=form.get("rlb_module_type", "inventory"),
            file_ext_type=form.get("file_ext_type", "xlsx,xls"),
            uid_create=form.get("uid_create", "1111"),
            uid_update=form.get("uid_update", "1111"),
            is_ledger_creation=form.get("is_ledger_creation", "1"),
            is_item_creation=form.get("is_item_creation", "1"),
            is_cc_creation=form.get("is_cc_creation", "0"),
            is_tagg_creation=form.get("is_tagg_creation", "0"),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
    finally:
        if py_temp and py_path and os.path.exists(py_path):
            os.unlink(py_path)
        if tpl_temp and tpl_path and os.path.exists(tpl_path):
            os.unlink(tpl_path)


@app.post("/api/edit_menu")
def api_edit_menu():
    py_path, py_temp, py_err = _resolve_file_input("py_file")
    tpl_path, tpl_temp, tpl_err = _resolve_file_input("template_file")
    errs = [e for e in (py_err, tpl_err) if e]
    if errs:
        return jsonify({"error": " / ".join(errs)}), 400
    form = request.form
    try:
        result = edit_menu(
            cid=form["cid"],
            box_id=form["box_id"],
            py_file_path=py_path,
            template_file_path=tpl_path,
            gstin=form.get("gstin", ""),
            uid_update=form.get("uid_update", "1111"),
            is_ledger_creation=form.get("is_ledger_creation", "0"),
            is_item_creation=form.get("is_item_creation", "1"),
            is_cc_creation=form.get("is_cc_creation", "0"),
            is_tagg_creation=form.get("is_tagg_creation", "0"),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
    finally:
        if py_temp and py_path and os.path.exists(py_path):
            os.unlink(py_path)
        if tpl_temp and tpl_path and os.path.exists(tpl_path):
            os.unlink(tpl_path)


@app.post("/api/delete_menu")
def api_delete_menu():
    ids = (request.json or {}).get("ids") or []
    if not ids:
        return jsonify({"error": "ids required"}), 400
    try:
        return jsonify(delete_menu(ids))
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    # Production: gunicorn -w 1 -b 0.0.0.0:8000 app:app
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
