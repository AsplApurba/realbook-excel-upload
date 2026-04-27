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
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


LOGIN_URL = "https://tasks.realbooks.in/login"
JOB_URL = "https://tasks.realbooks.in/jobs/view/job-{num}"
MENULIST_URL = "https://custom-pyexcel.realbooks.in/manualmapping/MenuList"
ADDMENU_URL = "https://custom-pyexcel.realbooks.in/manualmapping/AddMenu"
EDITMENU_URL = "https://custom-pyexcel.realbooks.in/manualmapping/EditMenu"
DELETEMENU_URL = "https://custom-pyexcel.realbooks.in/manualmapping/DeleteMenu"

# Credentials must come from the environment, especially for hosted deploys.
USERNAME = os.environ.get("REALBOOKS_USERNAME", "").strip()
PASSWORD = os.environ.get("REALBOOKS_PASSWORD", "").strip()

RLB_BOX_PREFIX = "RLBMBOX1"
RLB_PASSWORD_PREFIX = "RLB1234"


def _pad_box(box_id: str) -> str:
    """Normalize box_id to 2-digit zero-padded form (e.g. '5' -> '05', '29' -> '29').
    Non-numeric values are returned unchanged."""
    s = str(box_id).strip()
    return f"{int(s):02d}" if s.isdigit() else s

FIELD_KEYS = ("Assigned to", "Owned by", "Priority", "Deadline",
              "Original Deadline", "Process")


def build_driver(headless: bool):
    options = Options()
    chrome_bin = os.environ.get("CHROME_BIN")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chrome_bin:
        options.binary_location = chrome_bin
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--remote-debugging-port=9222")
        options.add_argument("--window-size=1400,1000")
    service = Service(chromedriver_path) if chromedriver_path else None
    return webdriver.Chrome(service=service, options=options)


def login(driver, wait):
    if not USERNAME or not PASSWORD:
        sys.exit("Missing REALBOOKS_USERNAME or REALBOOKS_PASSWORD environment variable.")
    driver.get(LOGIN_URL)
    wait.until(EC.visibility_of_element_located((By.ID, "userName"))).send_keys(USERNAME)
    driver.find_element(By.ID, "password").send_keys(PASSWORD)
    driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

    try:
        wait.until(lambda d: "/login" not in d.current_url)
    except TimeoutException:
        msg = ""
        title = ""
        body_excerpt = ""
        try:
            msg = driver.find_element(
                By.CSS_SELECTOR, ".error, .alert, .invalid-feedback, [role='alert']"
            ).text.strip()
        except NoSuchElementException:
            pass
        try:
            title = (driver.title or "").strip()
        except Exception:
            pass
        try:
            body_excerpt = (driver.find_element(By.TAG_NAME, "body").text or "").strip()
            body_excerpt = re.sub(r"\s+", " ", body_excerpt)[:500]
        except Exception:
            pass
        details = "; ".join(
            part for part in [
                f"current_url={driver.current_url!r}",
                f"title={title!r}" if title else "",
                f"page_text={body_excerpt!r}" if body_excerpt else "",
                msg,
            ] if part
        )
        sys.exit(f"Login failed. {details}".strip())


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

    def details_grab(block: str, label_regex: str) -> str:
        m_g = re.search(rf"^\s*{label_regex}\s*[:=\-]\s*(.+)$",
                        block, flags=re.I | re.M)
        return m_g.group(1).strip() if m_g else ""

    def details_grab_int(block: str, label_regex: str) -> str:
        m_g = re.search(rf"^\s*{label_regex}\s*[:=\-]\s*(\d+)",
                        block, flags=re.I | re.M)
        return m_g.group(1) if m_g else ""

    def details_grab_segids(block: str) -> list[str]:
        m_g = re.search(r"^\s*SEG\s*ID\s*[:=\-]\s*([\d,\s]+)",
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

    # Quaternary format: 'From' / 'To' alone on a line (optionally followed by
    # a 'Deployment Details' line) as section headers, with plain 'Label <sep>
    # Value' lines inside. Example: "From\nDeployment Details\nMenu Name - ..."
    def extract_fmt4_block(side: str) -> str:
        pattern = (
            rf"^\s*{side}\s*\n"
            r"(?:^\s*Deployment\s+Details\s*[:\-]?\s*\n)?"
            r"(.*?)"
            r"(?=^\s*(?:From|To)\s*(?:\n\s*Deployment\s+Details)?\s*[:\-]?\s*$"
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


def fetch_menu_list(cid: str, segids: list[str], box_id: str, menu_name: str) -> list[dict]:
    results: list[dict] = []
    for segid in segids:
        data = {
            "cid": cid,
            "segid": segid,
            "rlb_box_id": f"{RLB_BOX_PREFIX}{_pad_box(box_id)}",
            "password": f"{RLB_PASSWORD_PREFIX}{date.today().strftime('%Y%m%d')}",
            "file_name": "",
        }
        print(data)
        response = requests.post(MENULIST_URL, data=data).text
        try:
            items = json.loads(response).get("data") or []
        except ValueError:
            items = []
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
        py_file_path = str(match.get("py_file_path", "")).split("#@#", 1)[-1]
        template_file_path = str(match.get("temp_file_path", "")).split("#@#", 1)[-1]
        results.append({
            "id": str(match.get("id") or match.get("menu_id") or match.get("_id") or ""),
            "segid": segid,
            "py_file_path": py_file_path,
            "template_file_path": template_file_path,
            "gstin": str(match.get("gstin", "")),
            "rlb_module_type": str(match.get("rlb_module_type", "")),
            "file_ext_type": str(match.get("file_ext_type", "")),
            "is_ledger_creation": str(match.get("is_ledger_creation", "")),
            "is_item_creation": str(match.get("is_item_creation", "")),
            "is_cc_creation": str(match.get("is_cc_creation", "")),
            "is_tagg_creation": str(match.get("is_tagg_creation", "")),
        })
    return results


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
) -> dict:
    data = {
        "rlb_box_id": f"{RLB_BOX_PREFIX}{_pad_box(box_id)}",
        "cid": cid,
        "gstin": gstin,
        "domain_alias": domain_alias,
        "rlb_module_type": rlb_module_type,
        "menu_name": menu_name,
        "segid": json.dumps([int(s) for s in segids]),
        "password": f"{RLB_PASSWORD_PREFIX}{date.today().strftime('%Y%m%d')}",
        "uid_create": uid_create,
        "uid_update": uid_update,
        "file_ext_type": file_ext_type,
        "is_ledger_creation": is_ledger_creation,
        "is_item_creation": is_item_creation,
        "is_cc_creation": is_cc_creation,
        "is_tagg_creation": is_tagg_creation,
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
        response = requests.post(ADDMENU_URL, data=data, files=files)
    print(f"AddMenu response [{response.status_code}]: {response.text}")
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
) -> dict:
    data = {
        "rlb_box_id": f"{RLB_BOX_PREFIX}{_pad_box(box_id)}",
        "cid": cid,
        "gstin": gstin,
        "password": f"{RLB_PASSWORD_PREFIX}{date.today().strftime('%Y%m%d')}",
        "uid_update": uid_update,
        "is_ledger_creation": is_ledger_creation,
        "is_item_creation": is_item_creation,
        "is_cc_creation": is_cc_creation,
        "is_tagg_creation": is_tagg_creation,
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
        response = requests.post(EDITMENU_URL, data=data, files=files)
    print(f"EditMenu response [{response.status_code}]: {response.text}")
    try:
        return response.json()
    except ValueError:
        return {"status_code": response.status_code, "text": response.text}


def delete_menu(ids: list[str], uid_update: str = "1111") -> list[dict]:
    password = f"{RLB_PASSWORD_PREFIX}{date.today().strftime('%Y%m%d')}"
    results: list[dict] = []
    for menu_id in ids:
        menu_id = (menu_id or "").strip()
        if not menu_id:
            continue
        payload = {"id": menu_id, "password": password, "uid_update": uid_update}
        print(f"DeleteMenu request: {payload}")
        try:
            response = requests.post(DELETEMENU_URL, data=payload)
            try:
                body = response.json()
            except ValueError:
                body = {"text": response.text}
            entry = {"id": menu_id, "status_code": response.status_code, "response": body}
        except Exception as e:
            entry = {"id": menu_id, "error": f"{type(e).__name__}: {e}"}
        print(f"DeleteMenu result: {entry}")
        results.append(entry)
    return results


def main():
    parser = argparse.ArgumentParser(description="Fetch a RealBooks job ticket by number.")
    parser.add_argument("job_number", nargs="?", help="Job number, e.g. 160911 or job-160911")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless (recommended on Ubuntu servers).")
    parser.add_argument("--keep-open", action="store_true", help="Pause before closing the browser (headed mode only).")
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
