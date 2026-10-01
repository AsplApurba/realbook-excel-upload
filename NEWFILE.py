from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from datetime import date

import requests
from selenium import webdriver
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


LOGIN_URL = "https://tasks.realbooks.in/login"
JOB_URL = "https://tasks.realbooks.in/jobs/view/job-{num}"
MENULIST_URL = "https://custom-pyexcel.realbooks.in/manualmapping/MenuList"
# Python_Upload_List.jsp is only the HTML shell; the rows load via this AJAX endpoint (returns HTML, not JSON).
MENULIST_OLD_URL = "https://xlconverter.realbooks.in/converter/JSP/Python_Upload_List_Ajax.jsp"
ADDMENU_URL = "https://custom-pyexcel.realbooks.in/manualmapping/AddMenu"
EDITMENU_URL = "https://custom-pyexcel.realbooks.in/manualmapping/EditMenu"
DELETEMENU_URL = "https://custom-pyexcel.realbooks.in/manualmapping/DeleteMenu"
# Beta mirrors of the four new-format endpoints: same payloads, staging host.
# format_type="beta" picks these; there is no beta equivalent of the old xlconverter URLs.
BETA_MENULIST_URL = "https://beta-custom-pyexcel.realbooks.in/manualmapping/MenuList"
BETA_ADDMENU_URL = "https://beta-custom-pyexcel.realbooks.in/manualmapping/AddMenu"
BETA_EDITMENU_URL = "https://beta-custom-pyexcel.realbooks.in/manualmapping/EditMenu"
BETA_DELETEMENU_URL = "https://beta-custom-pyexcel.realbooks.in/manualmapping/DeleteMenu"
# Old-format add/edit: Python_Upload.jsp is the UI shell; its Save button posts (multipart) here.
GETLEDGER_URL = "https://custom-pyexcel.realbooks.in/manualmapping/GetRbLedgerByLedgerGrp"
BETA_GETLEDGER_URL = "https://beta-custom-pyexcel.realbooks.in/manualmapping/GetRbLedgerByLedgerGrp"
GETITEM_URL = "https://custom-pyexcel.realbooks.in/manualmapping/GetRbItemByItemGrp"
BETA_GETITEM_URL = "https://beta-custom-pyexcel.realbooks.in/manualmapping/GetRbItemByItemGrp"
# Nextgen: the exvspy service on the production host (there is no separate
# nextgen-custom-pyexcel host — that name does not resolve). Same multipart
# payloads as manualmapping minus the date password: every call is authenticated
# by the operator's RealBooks browser session, sent verbatim as a Cookie header
# (see _pyexcel_auth). There is no nextgen equivalent of the old xlconverter URLs.
# MenuList / AddMenu / EditMenu come from captured curls; DeleteMenu and the
# ledger/item endpoints are assumed to sit next to them and are unconfirmed.
NEXTGEN_BASE_URL = "https://custom-pyexcel.realbooks.in/exvspy"
NEXTGEN_MENULIST_URL = f"{NEXTGEN_BASE_URL}/MenuList"
NEXTGEN_ADDMENU_URL = f"{NEXTGEN_BASE_URL}/AddMenu"
NEXTGEN_EDITMENU_URL = f"{NEXTGEN_BASE_URL}/EditMenu"
NEXTGEN_DELETEMENU_URL = f"{NEXTGEN_BASE_URL}/DeleteMenu"
NEXTGEN_GETLEDGER_URL = f"{NEXTGEN_BASE_URL}/GetRbLedgerByLedgerGrp"
NEXTGEN_GETITEM_URL = f"{NEXTGEN_BASE_URL}/GetRbItemByItemGrp"

RLB_XC_UPLOAD_URL = "https://xlconverter.realbooks.in/converter/RLB_XC_Py_upload"

# Credentials: prefer env vars, fall back to inline for quick testing.
USERNAME = os.environ.get("REALBOOKS_USERNAME", "sahaapurba1994@gmail.com")
PASSWORD = os.environ.get("REALBOOKS_PASSWORD", "Ams@1234")
# Fixed .txt the old Add form requires as "Db Connection File" (override per call or via this env var).
OLD_DB_CONNECTION_FILE = os.environ.get("OLD_DB_CONNECTION_FILE", "")
# Nextgen (exvspy) session cookie — the full "Cookie:" header value copied out of a
# logged-in RealBooks browser tab (RLBMAIN=…; rlb_api=…; boxid_ngnx_mbox=…; …).
# It expires with that browser session, so the app takes it per call from its
# settings and only falls back to this env var.
NEXTGEN_COOKIE = os.environ.get("REALBOOKS_NEXTGEN_COOKIE", "")

RLB_BOX_PREFIX = "RLBMBOX1"
RLB_PASSWORD_PREFIX = "RLB1234"
OLD_PASSWORD_PREFIX = "adansa@@realbooks"

# The old xlconverter pages gate access with a date-based password held in a cookie
# scoped to /converter/ (see onLoadPageSecurity): adansa@@realbooks<day><month>.
_OLD_MODULE_TYPES = {
    "acc": "acc", "account": "acc", "accounts": "acc",
    "inv": "inv", "inventory": "inv",
    "custsaler": "custsaleR", "custsaleretr": "custsaleRetR", "acc_ledger": "acc_ledger",
}


# The JSON manualmapping endpoints live on three hosts that take identical payloads
# and parse identically, so the host choice sits here instead of growing a ternary
# at every call site.
_PYEXCEL_URLS = {
    "MenuList": {"new": MENULIST_URL, "beta": BETA_MENULIST_URL, "nextgen": NEXTGEN_MENULIST_URL},
    "AddMenu": {"new": ADDMENU_URL, "beta": BETA_ADDMENU_URL, "nextgen": NEXTGEN_ADDMENU_URL},
    "EditMenu": {"new": EDITMENU_URL, "beta": BETA_EDITMENU_URL, "nextgen": NEXTGEN_EDITMENU_URL},
    "DeleteMenu": {"new": DELETEMENU_URL, "beta": BETA_DELETEMENU_URL, "nextgen": NEXTGEN_DELETEMENU_URL},
    "GetRbLedgerByLedgerGrp": {
        "new": GETLEDGER_URL, "beta": BETA_GETLEDGER_URL, "nextgen": NEXTGEN_GETLEDGER_URL,
    },
    "GetRbItemByItemGrp": {
        "new": GETITEM_URL, "beta": BETA_GETITEM_URL, "nextgen": NEXTGEN_GETITEM_URL,
    },
}


def _pyexcel_url(endpoint: str, format_type: str) -> str:
    """Host for one of the JSON manualmapping endpoints.

    'beta' is the new format pointed at the staging host; 'nextgen' is the exvspy
    service on production — same payloads and parsing, only the URL differs (and
    nextgen authenticates by cookie, see _pyexcel_auth). Anything unrecognised resolves to production,
    which is also how 'old' lands here: the four menu calls branch on 'old' before
    they reach this, but the ledger and item calls have nowhere else to go because
    xlconverter has no such endpoint."""
    hosts = _PYEXCEL_URLS[endpoint]
    return hosts.get(str(format_type or "").strip().lower(), hosts["new"])


def _old_password() -> str:
    today = date.today()
    return f"{OLD_PASSWORD_PREFIX}{today.day}{today.month}"


def _old_cookies() -> dict:
    return {"password": _old_password()}


def _is_nextgen(format_type: str) -> bool:
    return str(format_type or "").strip().lower() == "nextgen"


def _nextgen_cookie(cookie: str = "") -> str:
    """Normalise a pasted cookie: drop a leading 'Cookie:' label and any line breaks,
    fall back to the env var. Returns '' when nothing usable is set."""
    raw = str(cookie or "").strip() or NEXTGEN_COOKIE
    raw = re.sub(r"^\s*cookie\s*:\s*", "", raw, flags=re.IGNORECASE)
    return " ".join(raw.split()).strip()


def _pyexcel_auth(format_type: str, cookie: str = "") -> tuple[dict, dict]:
    """(extra form fields, request headers) that authenticate one JSON-endpoint call.

    new/beta: the date password goes in the form as ``password``; no headers.
    nextgen: no password field at all — the exvspy service trusts only the
    operator's RealBooks session, sent verbatim as a Cookie header on every call.
    A nextgen call without a cookie is refused here rather than sent, because the
    service would just answer 401 and the failure is clearer before the upload."""
    if not _is_nextgen(format_type):
        return {"password": f"{RLB_PASSWORD_PREFIX}{date.today().strftime('%Y%m%d')}"}, {}
    value = _nextgen_cookie(cookie)
    if not value:
        raise ValueError(
            "nextgen cookie is not set — paste the Cookie header from a logged-in "
            "RealBooks tab (sidebar → Nextgen cookie, or REALBOOKS_NEXTGEN_COOKIE)"
        )
    return {}, {"Cookie": value}


def _check_nextgen_auth(response, format_type: str, label: str) -> None:
    """The exvspy endpoints answer 401 {"type": "error", "msg": "Unauthorized"} for a
    missing *or expired* cookie alike; name the cookie in the error so the operator
    knows to paste a fresh one instead of hunting through the payload."""
    if _is_nextgen(format_type) and response.status_code == 401:
        raise PermissionError(
            f"{label}: 401 Unauthorized from the nextgen service — the session cookie "
            "is missing or has expired; copy a fresh Cookie header from a logged-in "
            "RealBooks tab and save it in the sidebar"
        )


def _old_module_type(value: str) -> str:
    return _OLD_MODULE_TYPES.get(str(value or "").strip().lower(), value or "inv")


def _old_file_ext_type(value: str) -> str:
    parts = {p.strip().lower() for p in str(value or "").split(",") if p.strip()}
    if parts == {"csv"}:
        return "csv"
    if parts == {"xml"}:
        return "xml"
    if parts & {"xls", "xlsx"}:
        return "xls,xlsx"
    return value or "xls,xlsx"


def _pad_box(box_id: str) -> str:
    """Normalize box_id to 2-digit zero-padded form (e.g. '5' -> '05', '29' -> '29').
    Non-numeric values are returned unchanged."""
    s = str(box_id).strip()
    return f"{int(s):02d}" if s.isdigit() else s


def _rlb_box_id(box_id: str) -> str:
    """'5' -> 'RLBMBOX105'. A value that already carries the prefix is passed through,
    because the ledger screen lets the operator paste a whole box id off a ticket."""
    s = str(box_id or "").strip()
    if not s:
        return ""
    if s.upper().startswith(RLB_BOX_PREFIX):
        return s.upper()
    return f"{RLB_BOX_PREFIX}{_pad_box(s)}"

FIELD_KEYS = ("Assigned to", "Owned by", "Priority", "Deadline",
              "Original Deadline", "Process")


def build_driver(headless: bool):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1400,1000")
    return webdriver.Chrome(options=options)


def login(driver, wait):
    driver.get(LOGIN_URL)
    wait.until(EC.visibility_of_element_located((By.ID, "userName"))).send_keys(USERNAME)
    driver.find_element(By.ID, "password").send_keys(PASSWORD)
    driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

    try:
        wait.until(lambda d: "/login" not in d.current_url)
    except TimeoutException:
        msg = ""
        try:
            msg = driver.find_element(
                By.CSS_SELECTOR, ".error, .alert, .invalid-feedback, [role='alert']"
            ).text.strip()
        except NoSuchElementException:
            pass
        sys.exit(f"Login failed. {msg}".strip())


def fetch_job(driver, wait, job_number: str) -> dict:
    # Strip a leading "job-" if the user typed it.
    num = re.sub(r"^job-", "", job_number, flags=re.I)
    driver.get(JOB_URL.format(num=num))

    # Wait for the page to load — look for the "Describe the Task / Issue:" label
    # in the body, or the page title to show JOB-{num}.
    try:
        wait.until(
            lambda d: "Describe the Task" in d.find_element(By.TAG_NAME, "body").text
            or f"JOB-{num}" in (d.title or "").upper()
        )
    except TimeoutException:
        sys.exit(f"Job JOB-{num} did not load. Page title: {driver.title!r}")

    # Give React a moment to fill in the side-panel fields, then try to wait
    # until the Deployment Details block is actually in the DOM. The 5s baseline
    # is not always enough on slower connections; poll for up to 15s more.
    time.sleep(5)
    deadline = time.time() + 15
    while time.time() < deadline:
        txt = driver.find_element(By.TAG_NAME, "body").text
        if re.search(r"Deployment\s+Details", txt, flags=re.I) or \
           re.search(r"Deploy\s+(?:From|To)\b", txt, flags=re.I):
            break
        time.sleep(0.5)

    # Try to extract descriptive title from any heading that mentions JOB-{num}.
    title = ""
    for h in driver.find_elements(By.CSS_SELECTOR, "h1, h2, h3"):
        text = h.text.strip()
        m = re.search(rf"JOB-{num}\s*-\s*(.+)", text, flags=re.I)
        if m:
            title = m.group(1).strip()
            break

    # Prefer document.innerText (captures more than body.text for some React apps),
    # fall back to body.text if that fails.
    body_text = ""
    try:
        body_text = driver.execute_script(
            "return document.documentElement.innerText || document.body.innerText || '';"
        ) or ""
    except Exception:
        pass
    if not body_text:
        body_text = driver.find_element(By.TAG_NAME, "body").text

    # Diagnostic dump — helps when extraction misses fields. Overwrites each run.
    try:
        with open("/tmp/realbooks_last_body.txt", "w", encoding="utf-8") as _fh:
            _fh.write(body_text)
    except OSError:
        pass

    # Fallback: first non-empty line after "Describe the Task / Issue:".
    if not title:
        m = re.search(r"Describe the Task\s*/\s*Issue:[ \t]*\n(.+)", body_text)
        if m:
            title = m.group(1).strip()

    # Description: everything between "Describe the Task / Issue:" and the next section.
    description = ""
    m = re.search(
        r"Describe the Task\s*/\s*Issue:[ \t]*\n(.*?)(?=\n(?:No To-Dos Set|Update To Dos|Tasks\n|Attachments\n|Details\n))",
        body_text,
        flags=re.DOTALL,
    )
    if m:
        description = m.group(1).strip()

    # "Key: value" extraction — use [ \t]* so empty values don't swallow the next line.
    fields = {}
    for key in FIELD_KEYS:
        m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", body_text, flags=re.MULTILINE)
        if m:
            fields[key] = m.group(1).strip()

    def extract_side_field(side: str, label_regex: str, value_regex: str = r"([^\n]+)") -> str:
        pattern = rf"Deploy\s+{side}\s+{label_regex}\s*[-=:]*\s*{value_regex}"
        m_field = re.search(pattern, description, flags=re.I)
        return m_field.group(1).strip() if m_field else ""

    def extract_side_segids(side: str) -> list[str]:
        pattern = rf"Deploy\s+{side}\s+Seg\s*id\s*[-=:]*\s*([\d,\s]+)"
        m_seg = re.search(pattern, description, flags=re.I)
        return re.findall(r"\d+", m_seg.group(1)) if m_seg else []

    deploy_from_cid = extract_side_field("From", r"C\s*Id", r"(\d+)")
    deploy_to_cid = extract_side_field("To", r"C\s*Id", r"(\d+)")
    deploy_from_segids = extract_side_segids("From")
    deploy_to_segids = extract_side_segids("To")

    deploy_from_box = extract_side_field("From", r"Box\s*(?:id|no|#)?", r"(\d+)")
    deploy_to_box = extract_side_field("To", r"Box\s*(?:id|no|#)?", r"(\d+)")
    deploy_from_menu = extract_side_field("From", r"Menu\s*Name")
    deploy_to_menu = extract_side_field("To", r"Menu\s*Name")
    deploy_from_gstin = extract_side_field("From", r"GSTIN")
    deploy_to_gstin = extract_side_field("To", r"GSTIN")
    deploy_from_domain = extract_side_field("From", r"Domain")
    deploy_to_domain = extract_side_field("To", r"Domain")

    # Side blocks for display: collect every line that starts with "Deploy From"
    # or "Deploy To" — robust to jobs where the "Deploy To" section uses "To" alone
    # as a header or mixes casing on subsequent lines.
    from_lines: list[str] = []
    to_lines: list[str] = []
    for line in description.splitlines():
        if re.match(r"\s*Deploy\s+From\b", line, flags=re.I):
            from_lines.append(line.rstrip())
        elif re.match(r"\s*Deploy\s+To\b", line, flags=re.I):
            to_lines.append(line.rstrip())
    from_block = "\n".join(from_lines).strip()
    to_block = "\n".join(to_lines).strip()

    # Secondary format: "From Deployment Details :" / "To Deployment Details :"
    # section headers with plain "Label: Value" lines inside. Used as a fallback
    # when the primary "Deploy From <field>" style extraction finds nothing.
    def extract_details_block(side: str) -> str:
        pattern = (
            rf"^\s*{side}\s+Deployment\s+Details\s*[:\-]*\s*\n"
            r"(.*?)"
            r"(?=^\s*(?:From|To)\s+Deployment\s+Details\b"
            r"|^\s*File\s+Attached\b"
            r"|^\s*(?:To\s*Do|No\s+To-Dos)\b"
            r"|\Z)"
        )
        m_blk = re.search(pattern, description, flags=re.I | re.M | re.S)
        return m_blk.group(1).strip() if m_blk else ""

    # Detail lines sometimes repeat the side as a label prefix
    # ("From Cid: 7968", "Deploy to Segid: 8376, 8377"). Each block is already
    # side-scoped, so allow (and ignore) an optional leading From/To qualifier;
    # without this the "From <field>:" style matched neither the primary
    # "Deploy From <field>" patterns nor these line-anchored grabs.
    side_label = r"(?:(?:deploy\s+)?(?:from|to)\s+)?"

    def details_grab(block: str, label_regex: str) -> str:
        m_g = re.search(rf"^\s*{side_label}{label_regex}\s*[:=\-]\s*(.+)$",
                        block, flags=re.I | re.M)
        return m_g.group(1).strip() if m_g else ""

    def details_grab_int(block: str, label_regex: str) -> str:
        m_g = re.search(rf"^\s*{side_label}{label_regex}\s*[:=\-]\s*(\d+)",
                        block, flags=re.I | re.M)
        return m_g.group(1) if m_g else ""

    def details_grab_segids(block: str) -> list[str]:
        m_g = re.search(rf"^\s*{side_label}SEG\s*ID\s*[:=\-]\s*([\d,\s]+)",
                        block, flags=re.I | re.M)
        return re.findall(r"\d+", m_g.group(1)) if m_g else []

    from_details = extract_details_block("From")
    to_details = extract_details_block("To")

    deploy_from_cid = deploy_from_cid or details_grab_int(from_details, r"C\s*I\s*D")
    deploy_to_cid = deploy_to_cid or details_grab_int(to_details, r"C\s*I\s*D")
    deploy_from_segids = deploy_from_segids or details_grab_segids(from_details)
    deploy_to_segids = deploy_to_segids or details_grab_segids(to_details)
    deploy_from_box = deploy_from_box or details_grab_int(from_details, r"Box(?:\s*id|\s*no|\s*#)?")
    deploy_to_box = deploy_to_box or details_grab_int(to_details, r"Box(?:\s*id|\s*no|\s*#)?")
    deploy_from_menu = deploy_from_menu or details_grab(from_details, r"Menu\s*Name")
    deploy_to_menu = deploy_to_menu or details_grab(to_details, r"Menu\s*Name")
    deploy_from_gstin = deploy_from_gstin or details_grab(from_details, r"GSTIN")
    deploy_to_gstin = deploy_to_gstin or details_grab(to_details, r"GSTIN")
    deploy_from_domain = deploy_from_domain or details_grab(from_details, r"Domain")
    deploy_to_domain = deploy_to_domain or details_grab(to_details, r"Domain")

    if not from_block:
        from_block = from_details
    if not to_block:
        to_block = to_details

    # Tertiary format: standalone "Deploy From" / "Deploy To" header line followed
    # by plain "Label <sep> Value" lines (no per-line 'Deploy ...' prefix).
    def extract_deploy_section_block(side: str) -> str:
        pattern = (
            rf"^\s*Deploy\s+{side}\s*\n"
            r"(.*?)"
            r"(?=^\s*Deploy\s+(?:From|To)\b"
            r"|^\s*(?:From|To)\s+Deployment\s+Details\b"
            r"|^\s*File\s+Attached\b"
            r"|^\s*(?:To\s*Do|No\s+To-Dos|Attachments)\b"
            r"|\Z)"
        )
        m_blk = re.search(pattern, description, flags=re.I | re.M | re.S)
        return m_blk.group(1).strip() if m_blk else ""

    from_section = extract_deploy_section_block("From")
    to_section = extract_deploy_section_block("To")

    deploy_from_cid = deploy_from_cid or details_grab_int(from_section, r"C\s*I\s*D")
    deploy_to_cid = deploy_to_cid or details_grab_int(to_section, r"C\s*I\s*D")
    deploy_from_segids = deploy_from_segids or details_grab_segids(from_section)
    deploy_to_segids = deploy_to_segids or details_grab_segids(to_section)
    deploy_from_box = deploy_from_box or details_grab_int(from_section, r"Box(?:\s*id|\s*no|\s*#)?")
    deploy_to_box = deploy_to_box or details_grab_int(to_section, r"Box(?:\s*id|\s*no|\s*#)?")
    deploy_from_menu = deploy_from_menu or details_grab(from_section, r"Menu\s*Name")
    deploy_to_menu = deploy_to_menu or details_grab(to_section, r"Menu\s*Name")
    deploy_from_gstin = deploy_from_gstin or details_grab(from_section, r"GSTIN")
    deploy_to_gstin = deploy_to_gstin or details_grab(to_section, r"GSTIN")
    deploy_from_domain = deploy_from_domain or details_grab(from_section, r"Domain")
    deploy_to_domain = deploy_to_domain or details_grab(to_section, r"Domain")

    if not from_block:
        from_block = from_section
    if not to_block:
        to_block = to_section

    # Quaternary format: 'From' / 'To' (optionally with a trailing 'Domain', e.g.
    # 'From Domain' / 'To Domain') alone on a line, optionally followed by a
    # 'Deployment Details' line, as section headers with plain 'Label <sep> Value'
    # lines inside. Examples: "From\nDeployment Details\nMenu Name - ..." and
    # "From Domain\n\nDomain: Cipltd\nCid: 7968\n...".
    def extract_fmt4_block(side: str) -> str:
        pattern = (
            rf"^\s*{side}(?:\s+Domain)?\s*\n"
            r"(?:^\s*Deployment\s+Details\s*[:\-]?\s*\n)?"
            r"(.*?)"
            r"(?=^\s*(?:From|To)(?:\s+Domain)?\s*(?:\n\s*Deployment\s+Details)?\s*[:\-]?\s*$"
            r"|^\s*Deploy\s+(?:From|To)\b"
            r"|^\s*(?:From|To)\s+Deployment\s+Details\b"
            r"|^\s*File\s+Attached\b"
            r"|^\s*(?:To\s*Do|No\s+To-Dos|Attachments)\b"
            r"|\Z)"
        )
        m_blk = re.search(pattern, description, flags=re.I | re.M | re.S)
        return m_blk.group(1).strip() if m_blk else ""

    from_fmt4 = extract_fmt4_block("From")
    to_fmt4 = extract_fmt4_block("To")

    deploy_from_cid = deploy_from_cid or details_grab_int(from_fmt4, r"C\s*I\s*D")
    deploy_to_cid = deploy_to_cid or details_grab_int(to_fmt4, r"C\s*I\s*D")
    deploy_from_segids = deploy_from_segids or details_grab_segids(from_fmt4)
    deploy_to_segids = deploy_to_segids or details_grab_segids(to_fmt4)
    deploy_from_box = deploy_from_box or details_grab_int(from_fmt4, r"Box(?:\s*id|\s*no|\s*#)?")
    deploy_to_box = deploy_to_box or details_grab_int(to_fmt4, r"Box(?:\s*id|\s*no|\s*#)?")
    deploy_from_menu = deploy_from_menu or details_grab(from_fmt4, r"Menu\s*Name")
    deploy_to_menu = deploy_to_menu or details_grab(to_fmt4, r"Menu\s*Name")
    deploy_from_gstin = deploy_from_gstin or details_grab(from_fmt4, r"GSTIN")
    deploy_to_gstin = deploy_to_gstin or details_grab(to_fmt4, r"GSTIN")
    deploy_from_domain = deploy_from_domain or details_grab(from_fmt4, r"Domain")
    deploy_to_domain = deploy_to_domain or details_grab(to_fmt4, r"Domain")

    if not from_block:
        from_block = from_fmt4
    if not to_block:
        to_block = to_fmt4

    # Quinary format: a single-side 'Deployment Details:' block with no
    # From/To qualifier at all. Jobs in this shape describe one target; we
    # populate both FROM and TO with the same values so downstream menu-list
    # lookups and edit/add uploads continue to work unchanged.
    def extract_single_details_block() -> str:
        pattern = (
            r"^\s*Deployment\s+Details\s*[:\-]?\s*\n"
            r"(.*?)"
            r"(?=^\s*(?:From|To)\s+Deployment\s+Details\b"
            r"|^\s*Deploy\s+(?:From|To)\b"
            r"|^\s*File\s+Attached\b"
            r"|^\s*(?:To\s*Do|No\s+To-Dos|Attachments)\b"
            r"|\Z)"
        )
        for m_blk in re.finditer(pattern, description, flags=re.I | re.M | re.S):
            preceding_lines = description[:m_blk.start()].rstrip("\n").splitlines()
            if preceding_lines and re.fullmatch(
                r"\s*(?:From|To)\s*", preceding_lines[-1], flags=re.I
            ):
                continue
            return m_blk.group(1).strip()
        return ""

    single_details = extract_single_details_block()
    if single_details:
        single_cid = details_grab_int(single_details, r"C\s*I\s*D")
        single_segids = details_grab_segids(single_details)
        single_box = details_grab_int(single_details, r"Box(?:\s*id|\s*no|\s*#)?")
        single_menu = details_grab(single_details, r"Menu\s*Name")
        single_gstin = details_grab(single_details, r"GSTIN")
        single_domain = details_grab(single_details, r"Domain")

        deploy_from_cid = deploy_from_cid or single_cid
        deploy_to_cid = deploy_to_cid or single_cid
        deploy_from_segids = deploy_from_segids or single_segids
        deploy_to_segids = deploy_to_segids or single_segids
        deploy_from_box = deploy_from_box or single_box
        deploy_to_box = deploy_to_box or single_box
        deploy_from_menu = deploy_from_menu or single_menu
        deploy_to_menu = deploy_to_menu or single_menu
        deploy_from_gstin = deploy_from_gstin or single_gstin
        deploy_to_gstin = deploy_to_gstin or single_gstin
        deploy_from_domain = deploy_from_domain or single_domain
        deploy_to_domain = deploy_to_domain or single_domain

        if not from_block:
            from_block = single_details
        if not to_block:
            to_block = single_details

    # Top-level convenience fields — prefer TO (the upload target), fall back to FROM,
    # then to any first occurrence in the description for legacy formats.
    box_id = deploy_to_box or deploy_from_box
    if not box_id:
        m = re.search(r"Box\s*(?:id|no|#)?\s*[-=:]?\s*(\d+)", description, flags=re.I)
        if m:
            box_id = m.group(1)
    menu_name = deploy_to_menu or deploy_from_menu
    if not menu_name:
        m = re.search(r"Menu\s*Name\s*[-:=]?[ \t]*(.+)", description, flags=re.I)
        if not m:
            m = re.search(r"(?:Deploy(?:e)?ment\s+)?(?:File|Menu)\s*Name\s*[-:=]?[ \t]*(.+)",
                          description, flags=re.I)
        if m:
            menu_name = m.group(1).strip()
    gstin = deploy_to_gstin or deploy_from_gstin
    domain_alias = deploy_to_domain or deploy_from_domain

    return {
        "job": f"JOB-{num}",
        "title": title,
        "description": description,
        "box_id": box_id,
        "menu_name": menu_name,
        "deploy_from_cid": deploy_from_cid,
        "deploy_from_segids": deploy_from_segids,
        "deploy_from_block": from_block,
        "deploy_from_box": deploy_from_box,
        "deploy_from_menu": deploy_from_menu,
        "deploy_from_gstin": deploy_from_gstin,
        "deploy_from_domain": deploy_from_domain,
        "deploy_to_cid": deploy_to_cid,
        "deploy_to_segids": deploy_to_segids,
        "deploy_to_block": to_block,
        "deploy_to_box": deploy_to_box,
        "deploy_to_menu": deploy_to_menu,
        "deploy_to_gstin": deploy_to_gstin,
        "deploy_to_domain": deploy_to_domain,
        "gstin": gstin,
        "domain_alias": domain_alias,
        "fields": fields,
        "url": driver.current_url,
    }


def api_fetch_job(job_number: str, headless: bool = True) -> dict:
    driver = build_driver(headless=headless)
    wait = WebDriverWait(driver, 25)
    try:
        login(driver, wait)
        return fetch_job(driver, wait, job_number)
    finally:
        driver.quit()


# --- Posting a comment on a job ticket -------------------------------------
# tasks.realbooks.in is a React app and the comment box is a rich-text widget,
# so these selectors are deliberately defensive: we try the tab/editor/button by
# several common shapes rather than pinning one brittle class that a redeploy of
# the ticket UI could rename.
_COMMENTS_TAB_XPATH = (
    "//*[self::a or self::button or self::li or self::div or self::span]"
    "[normalize-space(text())='Comments']"
)
_POST_BTN_XPATH = "//button[normalize-space(.)='Post']"
_COMMENT_EDITOR_SELECTORS = (
    "[placeholder='Type your text here']",
    "textarea[placeholder*='Type your text']",
    "div[contenteditable='true']",
    "[contenteditable='true']",
    ".ql-editor",                      # Quill
    ".public-DraftEditor-content",     # Draft.js
    ".note-editable",                  # Summernote
    ".fr-element",                     # Froala
    "textarea",
)


def _first_displayed(driver, by, selectors):
    """Return the first visible element matching any selector in `selectors`."""
    for sel in selectors:
        try:
            els = driver.find_elements(by, sel)
        except Exception:
            els = []
        for el in els:
            try:
                if el.is_displayed():
                    return el
            except StaleElementReferenceException:
                continue
    return None


def _activity_count(driver) -> int:
    """Count entries in the job's activity/comment thread.

    Each row (comment, file upload, status change) carries a "<when> ago : <time>"
    timestamp, so the number of "ago :" markers is a cheap, layout-independent
    proxy for "how many entries are in the thread" — used to confirm a new
    comment actually landed.
    """
    try:
        return driver.find_element(By.TAG_NAME, "body").text.count("ago :")
    except Exception:
        return -1


def post_comment(driver, wait, job_number: str, text: str = "Deployed") -> dict:
    """Open a job ticket's Comments tab and post `text` as a comment.

    The driver must already be logged in (call login() first, or use
    api_post_comment which owns the whole lifecycle). Returns a small status
    dict; raises RuntimeError with a clear message if a step can't be located.
    """
    num = re.sub(r"^job-", "", job_number, flags=re.I)
    driver.get(JOB_URL.format(num=num))

    # Same load gate fetch_job uses.
    try:
        wait.until(
            lambda d: "Describe the Task" in d.find_element(By.TAG_NAME, "body").text
            or f"JOB-{num}" in (d.title or "").upper()
        )
    except TimeoutException:
        raise RuntimeError(f"Job JOB-{num} did not load (page title: {driver.title!r}).")

    # Switch to the Comments tab (JS click sidesteps overlay/intercept issues).
    try:
        tab = wait.until(EC.element_to_be_clickable((By.XPATH, _COMMENTS_TAB_XPATH)))
        driver.execute_script("arguments[0].click();", tab)
    except TimeoutException:
        raise RuntimeError("Could not find the 'Comments' tab on the job page.")

    # The editor mounts after the tab switch; poll briefly for it.
    editor = None
    deadline = time.time() + 15
    while time.time() < deadline:
        editor = _first_displayed(driver, By.CSS_SELECTOR, _COMMENT_EDITOR_SELECTORS)
        if editor is not None:
            break
        time.sleep(0.5)
    if editor is None:
        raise RuntimeError("Comment editor not found on the Comments tab.")

    # Snapshot the thread length so we can confirm the post actually landed.
    before = _activity_count(driver)

    # Type the comment, then pause: the editor is Quill behind a React wrapper,
    # and clicking Post in the same instant posts an *empty* comment because the
    # component hasn't absorbed the keystrokes yet. The settle is what makes it
    # commit.
    editor.click()
    editor.send_keys(text)
    time.sleep(1)
    if text not in (editor.text or ""):
        # Fallback: write into Quill's contenteditable directly and fire an
        # input event so the React wrapper reads the value.
        driver.execute_script(
            "arguments[0].classList.remove('ql-blank');"
            "arguments[0].innerHTML = '<p></p>';"
            "arguments[0].firstChild.textContent = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
            editor, text,
        )
        time.sleep(1)

    # Click Post — native click triggers the form/React handler reliably; fall
    # back to a JS click only if something intercepts it.
    try:
        post_btn = wait.until(EC.element_to_be_clickable((By.XPATH, _POST_BTN_XPATH)))
    except TimeoutException:
        raise RuntimeError("Could not find the 'Post' button on the Comments tab.")
    try:
        post_btn.click()
    except Exception:
        driver.execute_script("arguments[0].click();", post_btn)

    # Confirm it committed: a new thread entry appears, or the exact comment text
    # shows up as its own line. (An "editor cleared" check is unreliable — the
    # box can blank out without the comment posting.)
    posted = False
    deadline = time.time() + 15
    while time.time() < deadline:
        body = driver.find_element(By.TAG_NAME, "body").text
        if body.count("ago :") > before or any(
            line.strip() == text for line in body.splitlines()
        ):
            posted = True
            break
        time.sleep(0.5)
    if not posted:
        raise RuntimeError(
            f"Clicked Post but no new comment appeared on JOB-{num} "
            "(the comment may not have saved)."
        )

    return {"job": f"JOB-{num}", "text": text, "posted": posted}


def api_post_comment(job_number: str, text: str = "Deployed", headless: bool = True) -> dict:
    driver = build_driver(headless=headless)
    wait = WebDriverWait(driver, 25)
    try:
        login(driver, wait)
        return post_comment(driver, wait, job_number, text=text)
    finally:
        driver.quit()


def _pick(item: dict, *keys: str) -> str:
    """Return the first non-empty value among `keys` (handles snake_case/camelCase variants)."""
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _normalize_menu_row(item: dict, segid: str) -> dict:
    py_file_path = _pick(item, "py_file_path", "pyFilePath", "py_file", "pyFile").split("#@#", 1)[-1]
    template_file_path = _pick(
        item, "temp_file_path", "tempFilePath", "template_file_path", "templateFilePath",
    ).split("#@#", 1)[-1]
    return {
        "id": _pick(item, "id", "menu_id", "menuId", "_id"),
        "segid": _pick(item, "segid", "branchid", "branchId") or segid,
        "menu_name": _pick(item, "menu_name", "menuName"),
        "domain_alias": _pick(item, "domain_alias", "domainAlias"),
        "py_file_path": py_file_path,
        "template_file_path": template_file_path,
        "gstin": _pick(item, "gstin", "GSTIN"),
        "rlb_module_type": _pick(item, "rlb_module_type", "rlbModuleType", "module_type"),
        "file_ext_type": _pick(item, "file_ext_type", "fileExtType"),
        "is_ledger_creation": _pick(item, "is_ledger_creation", "isLedgerCreation"),
        "is_item_creation": _pick(item, "is_item_creation", "isItemCreation"),
        "is_cc_creation": _pick(item, "is_cc_creation", "isCcCreation"),
        "is_tagg_creation": _pick(item, "is_tagg_creation", "isTaggCreation"),
    }


def _parse_old_menu_html(html: str) -> list[dict]:
    """Parse the old-format (xlconverter .jsp) HTML response into menu dicts.

    Each row embeds a hidden <form class="form_edit"> whose inputs carry the
    clean field values; we read those rather than scraping the visible cells.
    Returns dicts keyed to match what _normalize_menu_row / the matcher expect.
    """
    items: list[dict] = []
    for form in re.findall(
        r'<form[^>]*class="[^"]*form_edit[^"]*"[^>]*>(.*?)</form>', html, re.S
    ):
        fields: dict[str, str] = {}
        for tag in re.findall(r"<input[^>]*>", form):
            attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', tag))
            if attrs.get("name"):
                fields[attrs["name"]] = attrs.get("value", "")
        if not fields.get("menuName"):
            continue
        items.append({
            "id": fields.get("savedid", ""),
            "menu_name": fields.get("menuName", ""),
            "segid": fields.get("branchid", ""),
            "domain_alias": fields.get("domainAlias", ""),
            "py_file_path": fields.get("fileName", ""),
            "template_file_path": fields.get("tempFileName", ""),
            "gstin": fields.get("GSTIN", ""),
            "rlb_module_type": fields.get("moduletype", ""),
            "file_ext_type": fields.get("fileExttype", ""),
        })
    return items


def fetch_menu_list(
    cid: str,
    segids: list[str],
    box_id: str,
    menu_name: str = "",
    format_type: str = "new",
    return_all: bool = False,
    cookie: str = "",
) -> list[dict]:
    use_old = str(format_type).strip().lower() == "old"
    results: list[dict] = []
    rlb_box_id = f"{RLB_BOX_PREFIX}{_pad_box(box_id)}"
    headers: dict = {}
    if not use_old:
        # Raises before any request goes out when nextgen has no cookie.
        auth_data, headers = _pyexcel_auth(format_type, cookie)
    for segid in segids:
        if use_old:
            url = MENULIST_OLD_URL
            data = {"cid": cid, "branchid": segid, "boxId": rlb_box_id}
        else:
            url = _pyexcel_url("MenuList", format_type)
            data = {
                "cid": cid,
                "segid": segid,
                "rlb_box_id": rlb_box_id,
                "file_name": "",
                **auth_data,
            }
        print(f"MenuList request -> {url}: {data}")
        cookies = _old_cookies() if use_old else None
        resp = requests.post(url, data=data, cookies=cookies, headers=headers or None)
        _check_nextgen_auth(resp, format_type, "MenuList")
        response = resp.text
        print(f"MenuList response: {response[:1500]}")
        if use_old:
            # Old format returns an HTML table, not JSON.
            items = _parse_old_menu_html(response)
        else:
            try:
                parsed = json.loads(response)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
            elif isinstance(parsed, dict):
                items = parsed.get("data") or parsed.get("list") or parsed.get("rows") or []
            else:
                items = []

        if return_all:
            results.extend(_normalize_menu_row(item, segid) for item in items)
            continue

        target = menu_name.strip().lower()

        def _norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

        def _stem_tokens(s: str) -> set[str]:
            return {t.rstrip("s") for t in _norm(s).split() if t}

        target_norm = _norm(target)
        target_tokens = _stem_tokens(target)

        matches = [i for i in items if str(i.get("menu_name", "")).strip().lower() == target]
        if not matches:
            matches = [i for i in items if _stem_tokens(str(i.get("menu_name", ""))) == target_tokens]
        if not matches and target_norm:
            scored = [
                (difflib.SequenceMatcher(None, _norm(str(i.get("menu_name", ""))), target_norm).ratio(), i)
                for i in items
            ]
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored and scored[0][0] >= 0.8:
                matches = [scored[0][1]]
        match = matches[0] if matches else {}
        results.append(_normalize_menu_row(match, segid))
    return results


def _add_menu_old(
    cid: str,
    segids: list[str],
    box_id: str,
    menu_name: str,
    gstin: str,
    domain_alias: str,
    py_file_path: str,
    template_file_path: str,
    rlb_module_type: str,
    file_ext_type: str,
    uid: str,
    db_connection_file: str,
    mpau: str,
    api_file_path: str = "",
) -> dict:
    """Add a menu via the old xlconverter form (multipart POST to RLB_XC_Py_upload).

    The old form needs two files the new flow doesn't: 'apiFileUpload' (its own .py,
    or the main .py if not given) and 'dbConnectionFile' (a .txt). One POST per segid.
    """
    db_path = db_connection_file or OLD_DB_CONNECTION_FILE
    if not db_path:
        raise ValueError(
            "Old-format add requires a Db Connection File (.txt). "
            "Set the OLD_DB_CONNECTION_FILE env var or pass db_connection_file."
        )
    api_path = api_file_path or py_file_path  # reuse main .py when no separate api file
    rlb_box_id = f"{RLB_BOX_PREFIX}{_pad_box(box_id)}"
    has_template = bool(template_file_path)
    mpau_on = str(mpau).strip().lower() in ("1", "on", "true", "yes")
    results: list[dict] = []
    for segid in segids:
        data = {
            "cid": cid,
            "segid": segid,
            "menuName": menu_name,
            "moduletype": _old_module_type(rlb_module_type),
            "GSTIN": gstin,
            "domainAlias": domain_alias,
            "fileExttype": _old_file_ext_type(file_ext_type),
            "boxId": rlb_box_id,
            "uId": uid,
            "savedid": "0",  # 0 = new record
        }
        # Checkboxes are sent (with empty value) only when ticked, omitted otherwise.
        if mpau_on:
            data["mpau"] = ""
        if has_template:
            data["istempFile"] = ""

        handles = []
        try:
            py_fh = open(py_file_path, "rb"); handles.append(py_fh)
            api_fh = open(api_path, "rb"); handles.append(api_fh)
            db_fh = open(db_path, "rb"); handles.append(db_fh)
            files = {
                "fileName": (os.path.basename(py_file_path), py_fh, "text/x-python"),
                "apiFileUpload": (os.path.basename(api_path), api_fh, "text/x-python"),
                "dbConnectionFile": (os.path.basename(db_path), db_fh, "text/plain"),
            }
            if has_template:
                tpl_fh = open(template_file_path, "rb"); handles.append(tpl_fh)
                files["tempFileName"] = (
                    os.path.basename(template_file_path),
                    tpl_fh,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            print(f"AddMenu(old) request -> {RLB_XC_UPLOAD_URL}: {data}")
            print("  py_file:", py_file_path, "| db_file:", db_path,
                  "| template:", template_file_path or "(none)")
            response = requests.post(
                RLB_XC_UPLOAD_URL, data=data, files=files, cookies=_old_cookies()
            )
        finally:
            for fh in handles:
                fh.close()
        print(f"AddMenu(old) response [{response.status_code}] segid {segid}: {response.text[:1500]}")
        results.append({
            "segid": segid,
            "status_code": response.status_code,
            "text": response.text[:2000],
        })
    return {"format": "old", "results": results}


def add_menu(
    cid: str,
    segids: list[str],
    box_id: str,
    menu_name: str,
    gstin: str,
    domain_alias: str,
    py_file_path: str,
    template_file_path: str,
    rlb_module_type: str = "inventory",
    file_ext_type: str = "xlsx,xls",
    uid_create: str = "1111",
    uid_update: str = "1111",
    is_ledger_creation: str = "1",
    is_item_creation: str = "1",
    is_cc_creation: str = "0",
    is_tagg_creation: str = "0",
    format_type: str = "new",
    db_connection_file: str = "",
    mpau: str = "0",
    api_file_path: str = "",
    cookie: str = "",
) -> dict:
    if str(format_type).strip().lower() == "old":
        return _add_menu_old(
            cid=cid,
            segids=segids,
            box_id=box_id,
            menu_name=menu_name,
            gstin=gstin,
            domain_alias=domain_alias,
            py_file_path=py_file_path,
            template_file_path=template_file_path,
            rlb_module_type=rlb_module_type,
            file_ext_type=file_ext_type,
            uid=uid_create,
            db_connection_file=db_connection_file,
            mpau=mpau,
            api_file_path=api_file_path,
        )
    auth_data, headers = _pyexcel_auth(format_type, cookie)
    data = {
        "rlb_box_id": f"{RLB_BOX_PREFIX}{_pad_box(box_id)}",
        "cid": cid,
        "gstin": gstin,
        "domain_alias": domain_alias,
        "rlb_module_type": rlb_module_type,
        "menu_name": menu_name,
        "segid": json.dumps([int(s) for s in segids]),
        "uid_create": uid_create,
        "uid_update": uid_update,
        "file_ext_type": file_ext_type,
        "is_ledger_creation": is_ledger_creation,
        "is_item_creation": is_item_creation,
        "is_cc_creation": is_cc_creation,
        "is_tagg_creation": is_tagg_creation,
        **auth_data,
    }
    print("AddMenu request:", data)
    print("  py_file:", py_file_path)
    print("  template_file:", template_file_path)
    with open(py_file_path, "rb") as py_fh, open(template_file_path, "rb") as tpl_fh:
        files = {
            "py_file": (os.path.basename(py_file_path), py_fh, "text/x-python"),
            "template_file": (
                os.path.basename(template_file_path),
                tpl_fh,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        }
        url = _pyexcel_url("AddMenu", format_type)
        print(f"AddMenu request -> {url}")
        response = requests.post(url, data=data, files=files, headers=headers or None)
    print(f"AddMenu response [{response.status_code}]: {response.text}")
    _check_nextgen_auth(response, format_type, "AddMenu")
    try:
        return response.json()
    except ValueError:
        return {"status_code": response.status_code, "text": response.text}


def edit_menu(
    cid: str,
    box_id: str,
    py_file_path: str,
    template_file_path: str,
    gstin: str = "",
    uid_update: str = "1111",
    is_ledger_creation: str = "0",
    is_item_creation: str = "1",
    is_cc_creation: str = "0",
    is_tagg_creation: str = "0",
    format_type: str = "new",
    menu_name: str = "",
    cookie: str = "",
) -> dict:
    auth_data, headers = _pyexcel_auth(format_type, cookie)
    if _is_nextgen(format_type):
        # The exvspy EditMenu addresses the menu by name and takes a narrower field
        # set (captured curl): no gstin, no cc/tagg flags, no password.
        if not str(menu_name or "").strip():
            raise ValueError("menu_name is required for a nextgen EditMenu")
        data = {
            "rlb_box_id": f"{RLB_BOX_PREFIX}{_pad_box(box_id)}",
            "cid": cid,
            "uid_update": uid_update,
            "is_ledger_creation": is_ledger_creation,
            "is_item_creation": is_item_creation,
            "menu_name": menu_name,
        }
    else:
        data = {
            "rlb_box_id": f"{RLB_BOX_PREFIX}{_pad_box(box_id)}",
            "cid": cid,
            "gstin": gstin,
            "uid_update": uid_update,
            "is_ledger_creation": is_ledger_creation,
            "is_item_creation": is_item_creation,
            "is_cc_creation": is_cc_creation,
            "is_tagg_creation": is_tagg_creation,
            **auth_data,
        }
    print("EditMenu request:", data)
    print("  py_file:", py_file_path)
    print("  template_file:", template_file_path)
    with open(py_file_path, "rb") as py_fh, open(template_file_path, "rb") as tpl_fh:
        files = {
            "py_file": (os.path.basename(py_file_path), py_fh, "text/x-python"),
            "template_file": (
                os.path.basename(template_file_path),
                tpl_fh,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        }
        url = _pyexcel_url("EditMenu", format_type)
        print(f"EditMenu request -> {url}")
        response = requests.post(url, data=data, files=files, headers=headers or None)
    print(f"EditMenu response [{response.status_code}]: {response.text}")
    _check_nextgen_auth(response, format_type, "EditMenu")
    try:
        return response.json()
    except ValueError:
        return {"status_code": response.status_code, "text": response.text}


def delete_menu(
    ids: list[str], uid_update: str = "1111", format_type: str = "new", cookie: str = ""
) -> list[dict]:
    auth_data, headers = _pyexcel_auth(format_type, cookie)
    url = _pyexcel_url("DeleteMenu", format_type)
    results: list[dict] = []
    for menu_id in ids:
        menu_id = (menu_id or "").strip()
        if not menu_id:
            continue
        payload = {"id": menu_id, "uid_update": uid_update, **auth_data}
        print(f"DeleteMenu request -> {url}: {payload}")
        try:
            response = requests.post(url, data=payload, headers=headers or None)
            _check_nextgen_auth(response, format_type, "DeleteMenu")
            try:
                body = response.json()
            except ValueError:
                body = {"text": response.text}
            entry = {"id": menu_id, "status_code": response.status_code, "response": body}
        except PermissionError as e:
            # An expired nextgen cookie fails every id identically — stop after the first.
            entry = {"id": menu_id, "error": f"{type(e).__name__}: {e}"}
            print(f"DeleteMenu result: {entry}")
            results.append(entry)
            break
        except Exception as e:
            entry = {"id": menu_id, "error": f"{type(e).__name__}: {e}"}
        print(f"DeleteMenu result: {entry}")
        results.append(entry)
    return results


# --- Ledger / item export --------------------------------------------------
# Ported from the RB_Ledger_Export notebook and extended to the matching item
# endpoint. Both services take the same JSON envelope and answer in the same
# shape, so one core does the work and the two public pairs only differ in the
# URL, the payload key and which columns are forced to the front.
#
# The pandas DataFrames of the notebook became plain lists of dicts, so the Flask
# app keeps openpyxl as its only spreadsheet dependency.

LEDGER_COLUMNS = ("ledger_group", "ledger_name", "ledger_code", "gstin_no")
# Confirmed against live data (cid 13799 / RLBMBOX101, 4 371 rows). Anything the
# service adds beyond these is still kept, appended in the order it arrives.
ITEM_COLUMNS = ("item_group", "item_code", "item_id", "item_name")
DEFAULT_LEDGER_GROUPS = (
    "Current Assets",
    "Current Liabilities",
    "Sundry Debtors",
    "Sundry Creditors",
)
RB_FETCH_TIMEOUT = 300        # seconds per request — these payloads get large
RB_FETCH_RETRIES = 3          # attempts per request


def _ledger_url(format_type: str) -> str:
    """new and old both resolve to the production host — xlconverter has no ledger
    endpoint, so 'old' borrows the new one rather than failing the whole screen.
    beta and nextgen each have their own."""
    return _pyexcel_url("GetRbLedgerByLedgerGrp", format_type)


def _item_url(format_type: str) -> str:
    """Same fallback as _ledger_url — there is no old-format item endpoint."""
    return _pyexcel_url("GetRbItemByItemGrp", format_type)


def _clean_group_list(value: str) -> str:
    """'Trading Goods,' -> 'Trading Goods'.

    The item service splits ``itemgrp`` on commas and looks every piece up, so an
    empty piece — a trailing comma, a leading one, a doubled one — makes it answer
    404 NOT_FOUND for the *whole* request, not just that piece. Writing a group list
    down one per line with trailing commas is the obvious way to type it, so the
    empties are dropped here rather than left to fail. Spaces around a comma are
    fine either way; the service trims them itself.
    """
    parts = [p.strip() for p in str(value or "").split(",")]
    return ",".join(p for p in parts if p)


def _is_error_block(item: dict) -> bool:
    """The services answer 'type': 'sucess' with 'msg': '… Fetched Successfully'
    even when ``data`` carries a Spring error object instead of rows — an empty
    itemgrp comes back as {'error': 'Not Found', 'status': 404, 'type': 'error'}.
    Rows like that must not reach the workbook as data."""
    if str(item.get("type", "")).strip().lower() == "error":
        return True
    return "status" in item and ("error" in item or "message" in item)


def _flatten_rb_rows(data, tag_key: str, tag_value: str, known: tuple[str, ...]) -> list[dict]:
    """``data`` comes back as a list of blocks (list-of-lists of dicts). Flatten it,
    tag every row with the group it was asked for, and put the known columns first.

    Every value is stringified for the same reason the CSV/Excel viewer does it:
    ledger codes, item codes and GSTINs carry leading zeros that int coercion eats.
    """
    raw: list[dict] = []
    for block in data or []:
        if isinstance(block, list):
            raw.extend(r for r in block if isinstance(r, dict))
        elif isinstance(block, dict):
            raw.append(block)

    rows: list[dict] = []
    for item in raw:
        row = {tag_key: tag_value}
        for col in known:
            if col != tag_key:
                row[col] = "" if item.get(col) is None else str(item.get(col))
        for key, value in item.items():
            if key not in row:
                row[key] = "" if value is None else str(value)
        rows.append(row)
    return rows


def _fetch_rb_group(
    url: str,
    payload_key: str,
    group: str,
    cid: str,
    box_id: str,
    tag_key: str,
    known: tuple[str, ...],
    retries: int,
    timeout: int,
    label: str,
    headers: dict | None = None,
) -> tuple[list[dict], str]:
    """Pull one group and return ``(rows, api_message)``.

    Response shape:
        {"data": [[{…}, …]], "msg": "… Fetched Successfully", "type": "sucess"}

    A group that fails every attempt returns ``([], "FAILED …")`` instead of raising,
    so one dead group never costs the operator the other twenty.
    """
    payload = {"cid": cid, "boxid": _rlb_box_id(box_id), payload_key: group}

    body = None
    last_err: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            print(f"{label} request -> {url}: {payload} (attempt {attempt})")
            resp = requests.post(
                url, headers={"Content-Type": "application/json", **(headers or {})},
                json=payload, timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            break
        except Exception as exc:               # network / timeout / bad json
            last_err = exc
            if attempt >= max(1, retries):
                return [], f"FAILED after {retries} attempt(s): {type(exc).__name__}: {exc}"
            time.sleep(2 * attempt)            # simple back-off
    if body is None:
        return [], f"FAILED: {last_err}"

    msg = f"{body.get('type', '')} | {body.get('msg', '')}".strip(" |")

    # An error object inside data outranks the envelope's cheerful 'sucess'.
    for block in body.get("data") or []:
        for entry in (block if isinstance(block, list) else [block]):
            if isinstance(entry, dict) and _is_error_block(entry):
                detail = " ".join(str(entry.get(k)) for k in ("error", "message")
                                  if entry.get(k)) or str(entry)
                print(f"{label} error block: {entry}")
                return [], f"FAILED: {detail}"

    rows = _flatten_rb_rows(body.get("data"), tag_key, group, known)
    print(f"{label} response: {msg} | {len(rows)} row(s)")
    return rows, msg


def _fetch_rb_groups(groups: list[str], fetch_one) -> tuple[dict[str, list[dict]], list[dict]]:
    """Run ``fetch_one`` over each group in order, keeping a per-group log.

    Returns ``(rows_by_group, summary)`` where summary is one dict per group —
    sl_no / ledger_group|item_group / row_count / seconds / api_response.
    """
    frames: dict[str, list[dict]] = {}
    summary: list[dict] = []
    for grp in groups:
        grp = str(grp).strip()
        if not grp or grp in frames:
            continue
        t0 = time.time()
        rows, msg = fetch_one(grp)
        frames[grp] = rows
        summary.append({
            "sl_no": len(summary) + 1,
            "group": grp,
            "row_count": len(rows),
            "seconds": round(time.time() - t0, 2),
            "api_response": msg,
        })
    return frames, summary


def _rb_group_headers(format_type: str, cookie: str) -> dict:
    """These endpoints are JSON and carry no password field, so the only auth that
    applies is the nextgen cookie; new/beta/old send nothing extra."""
    return _pyexcel_auth(format_type, cookie)[1] if _is_nextgen(format_type) else {}


def fetch_ledger_group(
    ledger_grp: str,
    cid: str,
    box_id: str,
    format_type: str = "new",
    retries: int = RB_FETCH_RETRIES,
    timeout: int = RB_FETCH_TIMEOUT,
    cookie: str = "",
) -> tuple[list[dict], str]:
    return _fetch_rb_group(
        _ledger_url(format_type), "ledgergrp", ledger_grp, cid, box_id,
        "ledger_group", LEDGER_COLUMNS, retries, timeout, "GetRbLedger",
        headers=_rb_group_headers(format_type, cookie),
    )


def fetch_ledger_groups(
    cid: str,
    box_id: str,
    groups: list[str],
    format_type: str = "new",
    retries: int = RB_FETCH_RETRIES,
    timeout: int = RB_FETCH_TIMEOUT,
    cookie: str = "",
) -> tuple[dict[str, list[dict]], list[dict]]:
    return _fetch_rb_groups(groups, lambda g: fetch_ledger_group(
        g, cid, box_id, format_type=format_type, retries=retries, timeout=timeout,
        cookie=cookie))


def fetch_item_group(
    item_grp: str,
    cid: str,
    box_id: str,
    format_type: str = "new",
    retries: int = RB_FETCH_RETRIES,
    timeout: int = RB_FETCH_TIMEOUT,
    cookie: str = "",
) -> tuple[list[dict], str]:
    """A caller may batch several groups into one request the way the service allows —
    "GRP1,GRP2". Empty segments are stripped first; see _clean_group_list."""
    return _fetch_rb_group(
        _item_url(format_type), "itemgrp", _clean_group_list(item_grp), cid, box_id,
        "item_group", ITEM_COLUMNS, retries, timeout, "GetRbItem",
        headers=_rb_group_headers(format_type, cookie),
    )


def fetch_item_groups(
    cid: str,
    box_id: str,
    groups: list[str],
    format_type: str = "new",
    retries: int = RB_FETCH_RETRIES,
    timeout: int = RB_FETCH_TIMEOUT,
    cookie: str = "",
) -> tuple[dict[str, list[dict]], list[dict]]:
    """One request per entry in ``groups``. An entry that already holds a comma list
    is passed through whole, which is how the operator asks for a batched pull."""
    return _fetch_rb_groups(groups, lambda g: fetch_item_group(
        g, cid, box_id, format_type=format_type, retries=retries, timeout=timeout,
        cookie=cookie))



def main():
    parser = argparse.ArgumentParser(description="Fetch a RealBooks job ticket by number.")
    parser.add_argument("job_number", nargs="?", help="Job number, e.g. 160911 or job-160911")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless (recommended on Ubuntu servers).")
    parser.add_argument("--keep-open", action="store_true", help="Pause before closing the browser (headed mode only).")
    parser.add_argument(
        "--comment", metavar="TEXT", nargs="?", const="Deployed",
        help="Instead of fetching, post TEXT as a comment on the job ticket "
             "(defaults to 'Deployed' when given with no value).",
    )
    args = parser.parse_args()

    job_number = args.job_number
    if not job_number:
        job_number = input("Enter job number: ").strip()
        if not job_number:
            sys.exit("No job number provided.")

    driver = build_driver(headless=args.headless)
    wait = WebDriverWait(driver, 25)
    try:
        login(driver, wait)
        if args.comment is not None:
            result = post_comment(driver, wait, job_number, text=args.comment or "Deployed")
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if args.keep_open and not args.headless:
                input("Press Enter to close the browser...")
            return
        data = fetch_job(driver, wait, job_number)
        cid = data["deploy_to_cid"] or data["deploy_from_cid"]
        segids = data["deploy_to_segids"] or data["deploy_from_segids"]
        if cid and segids and data["box_id"]:
            data["menu_list"] = fetch_menu_list(
                cid,
                segids,
                data["box_id"],
                menu_name=data["menu_name"],
            )
        print(json.dumps(data, indent=2, ensure_ascii=False))
        if args.keep_open and not args.headless:
            input("Press Enter to close the browser...")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()