"""Desktop GUI for the RealBooks job lookup (Tkinter, native on Ubuntu)."""
from __future__ import annotations
 
import fnmatch
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from datetime import date, datetime
from tkinter import ttk, messagebox, scrolledtext, filedialog, simpledialog

import ttkbootstrap as tb

from selenium.webdriver.support.ui import WebDriverWait

from NEWFILE import build_driver, login, fetch_job, fetch_menu_list, add_menu, edit_menu, delete_menu


REALBOOKS_ROOT = "/home/adansa/Desktop/Apurba/Realbooks/NoteBookWorking/RealBooks"
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, ".gui_settings.json")
LOGS_DIR = APP_DIR
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build", ".ipynb_checkpoints"}
 
BG = "#f8fafc"
PANEL = "#ffffff"
PANEL2 = "#f1f5f9"
TEXT = "#0f172a"
MUTED = "#64748b"
ACCENT = "#0284c7"
BORDER = "#cbd5e1"
FIELD_BG = "#ffffff"
 
 
class JobLookupApp(tb.Window):
    def __init__(self) -> None:
        super().__init__(themename="cosmo")
        self.title("Realbook Excel upload")
        self.configure(bg=BG)
        self.minsize(720, 520)
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{sw}x{sh}+0+0")
        try:
            self.attributes("-zoomed", True)
        except tk.TclError:
            try:
                self.state("zoomed")
            except tk.TclError:
                pass
 
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._settings: dict = self._load_settings()
        self._current_log_path: str | None = None

        saved_root = self._settings.get("realbooks_root")
        if saved_root and os.path.isdir(saved_root):
            global REALBOOKS_ROOT
            REALBOOKS_ROOT = saved_root
 
        self._cleanup_old_logs()
        self._build_styles()
        self._build_widgets()
        self.after(100, self._poll_queue)
        if not os.path.isdir(REALBOOKS_ROOT):
            self.after(250, self._prompt_missing_realbooks_root)

    def _prompt_missing_realbooks_root(self) -> None:
        self.status_var.set(f"Realbooks folder not found: {REALBOOKS_ROOT}")
        if messagebox.askyesno(
            "Realbooks folder not found",
            f"The configured Realbooks folder does not exist:\n\n{REALBOOKS_ROOT}\n\n"
            "Pick a new one now?",
        ):
            self._set_realbooks_folder()
 
    # ------------------------------------------------------------------ settings
 
    @staticmethod
    def _load_settings() -> dict:
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
 
    def _save_settings(self) -> None:
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump(self._settings, fh, indent=2)
        except OSError:
            pass
 
    # ------------------------------------------------------------------ styling
 
    def _build_styles(self) -> None:
        style = tb.Style.get_instance() or ttk.Style(self)
        colors = getattr(style, "colors", None)
        primary = getattr(colors, "primary", ACCENT) if colors else ACCENT
        bg = getattr(colors, "bg", BG) if colors else BG
        fg = getattr(colors, "fg", TEXT) if colors else TEXT
        secondary = getattr(colors, "secondary", MUTED) if colors else MUTED
        light = getattr(colors, "light", PANEL2) if colors else PANEL2
        selectfg = getattr(colors, "selectfg", "#ffffff") if colors else "#ffffff"
        active = getattr(colors, "active", "#0369a1") if colors else "#0369a1"

        style.configure("Panel.TFrame", background=bg)
        style.configure("HeaderBar.TFrame", background=primary)
        style.configure("Header.TLabel", background=primary, foreground=selectfg, font=("DejaVu Sans", 17, "bold"))
        style.configure("Section.TLabel", background=bg, foreground=primary, font=("DejaVu Sans", 11, "bold"))
        style.configure("Muted.TLabel", background=bg, foreground=secondary, font=("DejaVu Sans", 9))
        style.configure("Value.TLabel", background=bg, foreground=fg, font=("DejaVu Sans", 10))
        style.configure("Accent.TButton",
                        background=primary, foreground=selectfg,
                        font=("DejaVu Sans", 10, "bold"), padding=(14, 8), borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", active), ("disabled", light)])
        style.configure("Treeview", rowheight=26, font=("DejaVu Sans", 10))
        style.configure("Treeview.Heading", font=("DejaVu Sans", 9, "bold"))
        style.configure("TNotebook.Tab", padding=(14, 8), font=("DejaVu Sans", 10))
 
    # ------------------------------------------------------------------ layout
 
    def _build_widgets(self) -> None:
        header = ttk.Frame(self, padding=(20, 16), style="HeaderBar.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="Realbook Excel upload", style="Header.TLabel", anchor="center").pack(fill="x", expand=True)
 
        form = ttk.Frame(self, padding=(20, 0, 20, 12))
        form.pack(fill="x")
 
        ttk.Label(form, text="Job number:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.job_var = tk.StringVar()
        recent = self._settings.get("recent_jobs") or []
        self.job_entry = ttk.Combobox(
            form, textvariable=self.job_var, width=32, values=recent,
        )
        self.job_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.job_entry.bind("<Return>", lambda _e: self._on_fetch())
        self.job_entry.bind("<<ComboboxSelected>>", lambda _e: self._on_fetch())

        self.headless_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Headless", variable=self.headless_var).grid(row=0, column=2, padx=(0, 12))

        self.fetch_btn = tb.Button(
            form, text="Fetch job", bootstyle="primary", command=self._on_fetch,
        )
        self.fetch_btn.grid(row=0, column=3, padx=(0, 8))

        self.logs_btn = tb.Button(
            form, text="Open logs", bootstyle="secondary-outline",
            command=self._open_logs_folder,
        )
        self.logs_btn.grid(row=0, column=4, padx=(0, 8))

        self.folder_btn = tb.Button(
            form, text="Realbooks folder", bootstyle="secondary-outline",
            command=self._set_realbooks_folder,
        )
        self.folder_btn.grid(row=0, column=5)

        form.columnconfigure(1, weight=1)
 
        self.status_var = tk.StringVar(value="Ready.")
        status = ttk.Frame(self, padding=(20, 0, 20, 8))
        status.pack(fill="x")
        self.status_lbl = ttk.Label(status, textvariable=self.status_var, foreground=MUTED, background=BG)
        self.status_lbl.pack(side="left")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=160)
 
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=20, pady=(0, 16))
 
        self._last_data: dict | None = None
 
        self._build_summary_tab()
        self._build_description_tab()
        self._build_menu_tab()
        self._build_upload_tab()
        self._build_edit_tab()
        self._build_delete_tab()
        self._build_json_tab()

        self.job_entry.focus_set()
 
    def _build_summary_tab(self) -> None:
        frame = ttk.Frame(self.notebook, style="Panel.TFrame", padding=16)
        self.notebook.add(frame, text="Summary")
        self.summary_title = ttk.Label(frame, text="—", style="Section.TLabel")
        self.summary_title.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
 
        self.summary_vars: dict[str, tk.StringVar] = {}
        labels = [
            ("Job", "job"),
            ("Box ID", "box_id"),
            ("Menu name", "menu_name"),
            ("GSTIN", "gstin"),
            ("Domain", "domain_alias"),
            ("Deploy From CID", "deploy_from_cid"),
            ("Deploy From Seg IDs", "deploy_from_segids"),
            ("Deploy To CID", "deploy_to_cid"),
            ("Deploy To Seg IDs", "deploy_to_segids"),
            ("URL", "url"),
        ]
        for i, (label, key) in enumerate(labels, start=1):
            ttk.Label(frame, text=label, style="Muted.TLabel").grid(row=i, column=0, sticky="nw", pady=4, padx=(0, 16))
            var = tk.StringVar(value="—")
            self.summary_vars[key] = var
            ttk.Label(frame, textvariable=var, style="Value.TLabel", wraplength=680, justify="left").grid(
                row=i, column=1, sticky="w", pady=4
            )
            tb.Button(
                frame, text="Copy", bootstyle="secondary-outline", width=6,
                command=lambda k=key: self._copy_summary_value(k),
            ).grid(row=i, column=2, sticky="w", padx=(10, 0), pady=4)
        frame.columnconfigure(1, weight=1)

    def _copy_summary_value(self, key: str) -> None:
        value = self.summary_vars.get(key)
        text = (value.get() if value else "") or ""
        if not text or text == "—":
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.status_var.set(f"Copied {key}: {text[:60]}")
 
    def _build_description_tab(self) -> None:
        frame = ttk.Frame(self.notebook, style="Panel.TFrame", padding=16)
        self.notebook.add(frame, text="Description")
        self.desc_text = scrolledtext.ScrolledText(
            frame, wrap="word", bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", borderwidth=1, font=("DejaVu Sans Mono", 10),
        )
        self.desc_text.pack(fill="both", expand=True)
        self._make_readonly(self.desc_text)
 
    def _build_menu_tab(self) -> None:
        frame = ttk.Frame(self.notebook, style="Panel.TFrame", padding=16)
        self.notebook.add(frame, text="Menu list")

        source_row = ttk.Frame(frame, style="Panel.TFrame")
        source_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(source_row, text="Source:", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.menu_source_var = tk.StringVar(value="from")
        ttk.Radiobutton(
            source_row, text="Deploy From", value="from",
            variable=self.menu_source_var, command=self._apply_menu_source,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            source_row, text="Deploy To", value="to",
            variable=self.menu_source_var, command=self._apply_menu_source,
        ).pack(side="left")
        self.menu_source_count = ttk.Label(source_row, text="", style="Muted.TLabel")
        self.menu_source_count.pack(side="left", padx=(12, 0))

        filter_row = ttk.Frame(frame, style="Panel.TFrame")
        filter_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(filter_row, text="Filter:", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.menu_filter_var = tk.StringVar()
        self.menu_filter_var.trace_add("write", lambda *_a: self._apply_menu_filter())
        filter_entry = ttk.Entry(filter_row, textvariable=self.menu_filter_var, width=40)
        filter_entry.pack(side="left", fill="x", expand=True)
        tb.Button(
            filter_row, text="Clear", bootstyle="secondary-outline",
            command=lambda: self.menu_filter_var.set(""),
        ).pack(side="left", padx=(6, 0))

        columns = (
            "segid", "py_file_path", "template_file_path",
            "gstin", "rlb_module_type", "file_ext_type",
            "is_ledger_creation", "is_item_creation",
            "is_cc_creation", "is_tagg_creation",
        )
        self.menu_tree = ttk.Treeview(
            frame, columns=columns, show="headings", height=10,
        )
        self._menu_rows_cache: list[tuple] = []
        headings = {
            "segid": ("Seg ID", 100),
            "py_file_path": ("Py file path", 300),
            "template_file_path": ("Template", 200),
            "gstin": ("GSTIN", 140),
            "rlb_module_type": ("Module", 90),
            "file_ext_type": ("Ext", 80),
            "is_ledger_creation": ("Ledger", 60),
            "is_item_creation": ("Item", 50),
            "is_cc_creation": ("CC", 50),
            "is_tagg_creation": ("Tagg", 50),
        }
        for col, (text, width) in headings.items():
            self.menu_tree.heading(col, text=text)
            self.menu_tree.column(col, width=width, anchor="w")
        vscroll = ttk.Scrollbar(frame, orient="vertical", command=self.menu_tree.yview)
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.menu_tree.xview)
        self.menu_tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        self.menu_tree.grid(row=2, column=0, sticky="nsew")
        vscroll.grid(row=2, column=1, sticky="ns")
        hscroll.grid(row=3, column=0, sticky="ew")
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)
        self._bind_tree_copy(self.menu_tree)
 
    def _build_upload_tab(self) -> None:
        outer = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.notebook.add(outer, text="Add_Menu")
 
        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0, borderwidth=0)
        vscroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")
 
        frame = ttk.Frame(canvas, style="Panel.TFrame", padding=16)
        window_id = canvas.create_window((0, 0), window=frame, anchor="nw")
 
        def _on_frame_configure(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
 
        def _on_canvas_configure(event):
            canvas.itemconfigure(window_id, width=event.width)
 
        frame.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
 
        def _on_mousewheel(event):
            if event.num == 4:
                canvas.yview_scroll(-3, "units")
            elif event.num == 5:
                canvas.yview_scroll(3, "units")
            else:
                canvas.yview_scroll(int(-1 * (event.delta / 120)) * 3, "units")
 
        def _bind_wheel(_e=None):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_mousewheel)
            canvas.bind_all("<Button-5>", _on_mousewheel)
 
        def _unbind_wheel(_e=None):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")
 
        outer.bind("<Enter>", _bind_wheel)
        outer.bind("<Leave>", _unbind_wheel)
 
        saved = self._settings
        defaults = {
            "py_file": "",
            "template_file": "",
            "rlb_module_type": "inventory",
            "file_ext_type": "xlsx,xls",
            "uid_create": "1111",
            "uid_update": "1111",
            "is_ledger_creation": "1",
            "is_item_creation": "1",
            "is_cc_creation": "0",
            "is_tagg_creation": "0",
        }
        self.upload_vars: dict[str, tk.StringVar] = {
            k: tk.StringVar(value=saved.get(k, v)) for k, v in defaults.items()
        }
        for key in self.upload_vars:
            self.upload_vars[key].trace_add(
                "write", lambda *_a, k=key: self._persist_setting(k)
            )
        self.upload_ctx_vars: dict[str, tk.StringVar] = {
            "cid": tk.StringVar(value="—"),
            "segids": tk.StringVar(value="—"),
            "box_id": tk.StringVar(value="—"),
            "menu_name": tk.StringVar(value="—"),
            "gstin": tk.StringVar(value="—"),
            "domain_alias": tk.StringVar(value="—"),
        }
        self.upload_source_var = tk.StringVar(value="to")
 
        row = 0
        for label, key, filetypes in [
            ("Python file", "py_file", [("Python", "*.py"), ("All", "*.*")]),
            ("Template (xlsx)", "template_file", [("Excel", "*.xlsx *.xls"), ("All", "*.*")]),
        ]:
            ttk.Label(frame, text=label, style="Section.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            ttk.Entry(frame, textvariable=self.upload_vars[key], width=60).grid(row=row, column=1, sticky="ew", pady=4)
            ttk.Button(
                frame, text="Browse…",
                command=lambda k=key, ft=filetypes: self._pick_file(k, ft),
            ).grid(row=row, column=2, padx=(8, 0), pady=4)
            row += 1
 
        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(10, 10)
        )
        row += 1
 
        ttk.Label(frame, text="Source (prefill):", style="Section.TLabel").grid(
            row=row, column=0, sticky="w", pady=(0, 6)
        )
        src_frame = ttk.Frame(frame, style="Panel.TFrame")
        src_frame.grid(row=row, column=1, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Radiobutton(
            src_frame, text="Deploy To", value="to",
            variable=self.upload_source_var, command=self._refresh_upload_ctx,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            src_frame, text="Deploy From", value="from",
            variable=self.upload_source_var, command=self._refresh_upload_ctx,
        ).pack(side="left")
        row += 1
        for label, key in [
            ("CID", "cid"),
            ("Seg IDs (comma-sep)", "segids"),
            ("Box ID", "box_id"),
            ("Menu name", "menu_name"),
            ("GSTIN", "gstin"),
            ("Domain", "domain_alias"),
        ]:
            ttk.Label(frame, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=2)
            ttk.Entry(
                frame, textvariable=self.upload_ctx_vars[key], width=60,
            ).grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
            row += 1
 
        ttk.Label(frame, text="Source details (raw):", style="Muted.TLabel").grid(
            row=row, column=0, sticky="nw", padx=(0, 12), pady=(6, 2)
        )
        self.upload_block_text = scrolledtext.ScrolledText(
            frame, wrap="word", height=8, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", borderwidth=1, font=("DejaVu Sans Mono", 10),
        )
        self.upload_block_text.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(6, 2))
        self._make_readonly(self.upload_block_text)
        row += 1
 
        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(10, 10)
        )
        row += 1
 
        module_choices = ("acc_jrnal", "inventory", "ibt_acc", "inv_price_diff")
        for label, key in [
            ("Module type", "rlb_module_type"),
            ("File ext", "file_ext_type"),
            ("uid_create", "uid_create"),
            ("uid_update", "uid_update"),
            ("is_ledger_creation", "is_ledger_creation"),
            ("is_item_creation", "is_item_creation"),
            ("is_cc_creation", "is_cc_creation"),
            ("is_tagg_creation", "is_tagg_creation"),
        ]:
            ttk.Label(frame, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            if key == "rlb_module_type":
                ttk.Combobox(
                    frame, textvariable=self.upload_vars[key], width=28,
                    values=module_choices,
                ).grid(row=row, column=1, sticky="w", pady=4)
            else:
                ttk.Entry(frame, textvariable=self.upload_vars[key], width=30).grid(row=row, column=1, sticky="w", pady=4)
            row += 1
 
        btn_row = ttk.Frame(frame, style="Panel.TFrame")
        btn_row.grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 8))
        self.create_py_btn = tb.Button(
            btn_row, text="Create py file in domain folder",
            bootstyle="info", command=self._on_create_py,
        )
        self.create_py_btn.pack(side="left", padx=(0, 8))
        self.search_clone_btn = tb.Button(
            btn_row, text="Search & clone py file",
            bootstyle="info-outline", command=self._on_search_clone,
        )
        self.search_clone_btn.pack(side="left", padx=(0, 8))
        self.search_clone_tpl_btn = tb.Button(
            btn_row, text="Search & clone template file",
            bootstyle="info-outline", command=self._on_search_clone_template,
        )
        self.search_clone_tpl_btn.pack(side="left", padx=(0, 8))
        self.upload_btn = tb.Button(
            btn_row, text="Upload to AddMenu", bootstyle="success", command=self._on_upload,
        )
        self.upload_btn.pack(side="left")
        row += 1
 
        self.upload_result = scrolledtext.ScrolledText(
            frame, wrap="word", height=10, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", borderwidth=1, font=("DejaVu Sans Mono", 10),
        )
        self.upload_result.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        self._make_readonly(self.upload_result)
 
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(row, weight=1)

    def _build_edit_tab(self) -> None:
        outer = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.notebook.add(outer, text="Edit_Menu")

        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0, borderwidth=0)
        vscroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

        frame = ttk.Frame(canvas, style="Panel.TFrame", padding=16)
        window_id = canvas.create_window((0, 0), window=frame, anchor="nw")

        frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))

        def _on_mousewheel(event):
            if event.num == 4:
                canvas.yview_scroll(-3, "units")
            elif event.num == 5:
                canvas.yview_scroll(3, "units")
            else:
                canvas.yview_scroll(int(-1 * (event.delta / 120)) * 3, "units")

        def _bind_wheel(_e=None):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_mousewheel)
            canvas.bind_all("<Button-5>", _on_mousewheel)

        def _unbind_wheel(_e=None):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        outer.bind("<Enter>", _bind_wheel)
        outer.bind("<Leave>", _unbind_wheel)

        saved = self._settings
        defaults = {
            "edit_py_file": "",
            "edit_template_file": "",
            "edit_uid_update": "1111",
            "edit_is_ledger_creation": "0",
            "edit_is_item_creation": "1",
            "edit_is_cc_creation": "0",
            "edit_is_tagg_creation": "0",
        }
        self.edit_vars: dict[str, tk.StringVar] = {
            k: tk.StringVar(value=saved.get(k, v)) for k, v in defaults.items()
        }
        for key in self.edit_vars:
            self.edit_vars[key].trace_add(
                "write", lambda *_a, k=key: self._persist_edit_setting(k)
            )

        self.edit_ctx_vars: dict[str, tk.StringVar] = {
            "cid": tk.StringVar(value="—"),
            "box_id": tk.StringVar(value="—"),
            "gstin": tk.StringVar(value="—"),
            "domain_alias": tk.StringVar(value="—"),
        }
        self.edit_source_var = tk.StringVar(value="to")

        row = 0
        for label, key, filetypes in [
            ("Python file", "edit_py_file", [("Python", "*.py"), ("All", "*.*")]),
            ("Template (xlsx)", "edit_template_file", [("Excel", "*.xlsx *.xls"), ("All", "*.*")]),
        ]:
            ttk.Label(frame, text=label, style="Section.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            ttk.Entry(frame, textvariable=self.edit_vars[key], width=60).grid(row=row, column=1, sticky="ew", pady=4)
            ttk.Button(
                frame, text="Browse…",
                command=lambda k=key, ft=filetypes: self._pick_edit_file(k, ft),
            ).grid(row=row, column=2, padx=(8, 0), pady=4)
            row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(10, 10)
        )
        row += 1

        ttk.Label(frame, text="Source (prefill):", style="Section.TLabel").grid(
            row=row, column=0, sticky="w", pady=(0, 6)
        )
        src_frame = ttk.Frame(frame, style="Panel.TFrame")
        src_frame.grid(row=row, column=1, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Radiobutton(
            src_frame, text="Deploy To", value="to",
            variable=self.edit_source_var, command=self._refresh_edit_ctx,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            src_frame, text="Deploy From", value="from",
            variable=self.edit_source_var, command=self._refresh_edit_ctx,
        ).pack(side="left")
        row += 1
        for label, key in [
            ("CID", "cid"),
            ("Box ID", "box_id"),
            ("GSTIN", "gstin"),
            ("Domain", "domain_alias"),
        ]:
            ttk.Label(frame, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=2)
            ttk.Entry(
                frame, textvariable=self.edit_ctx_vars[key], width=60,
            ).grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
            row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(10, 10)
        )
        row += 1

        for label, key in [
            ("uid_update", "edit_uid_update"),
            ("is_ledger_creation", "edit_is_ledger_creation"),
            ("is_item_creation", "edit_is_item_creation"),
            ("is_cc_creation", "edit_is_cc_creation"),
            ("is_tagg_creation", "edit_is_tagg_creation"),
        ]:
            ttk.Label(frame, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            ttk.Entry(frame, textvariable=self.edit_vars[key], width=30).grid(row=row, column=1, sticky="w", pady=4)
            row += 1

        btn_row = ttk.Frame(frame, style="Panel.TFrame")
        btn_row.grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 8))
        self.edit_btn = tb.Button(
            btn_row, text="Upload to EditMenu", bootstyle="warning", command=self._on_edit_upload,
        )
        self.edit_btn.pack(side="left")
        row += 1

        self.edit_result = scrolledtext.ScrolledText(
            frame, wrap="word", height=10, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", borderwidth=1, font=("DejaVu Sans Mono", 10),
        )
        self.edit_result.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        self._make_readonly(self.edit_result)

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(row, weight=1)

    def _persist_edit_setting(self, key: str) -> None:
        value = self.edit_vars[key].get().strip()
        if value:
            self._settings[key] = value
        else:
            self._settings.pop(key, None)
        self._save_settings()

    def _refresh_edit_ctx(self) -> None:
        data = self._last_data or {}
        src = self.edit_source_var.get()
        prefix = "deploy_from_" if src == "from" else "deploy_to_"
        cid = data.get(f"{prefix}cid", "")
        box_id = data.get(f"{prefix}box") or data.get("box_id") or ""
        gstin = data.get(f"{prefix}gstin") or data.get("gstin") or ""
        domain_alias = data.get(f"{prefix}domain") or data.get("domain_alias") or ""
        parsed = data.get("desc_parsed") or {}
        if not cid:
            cid = parsed.get("cid", "")
        if not box_id:
            box_id = parsed.get("box", "")
        if not gstin:
            gstin = parsed.get("gstin", "")
        if not domain_alias:
            domain_alias = parsed.get("domain", "")
        self.edit_ctx_vars["cid"].set(cid or "—")
        self.edit_ctx_vars["box_id"].set(box_id or "—")
        self.edit_ctx_vars["gstin"].set(gstin or "—")
        self.edit_ctx_vars["domain_alias"].set(domain_alias or "—")

    def _pick_edit_file(self, key: str, filetypes: list[tuple[str, str]]) -> None:
        current = self.edit_vars[key].get().strip()
        initialdir = None
        if current and os.path.isdir(os.path.dirname(current)):
            initialdir = os.path.dirname(current)
        else:
            domain_alias = self.edit_ctx_vars["domain_alias"].get().strip()
            if domain_alias and domain_alias != "—":
                folder = self._find_domain_folder(domain_alias)
                if folder:
                    initialdir = folder
            if not initialdir:
                initialdir = REALBOOKS_ROOT
        extensions = self._filetypes_to_extensions(filetypes)
        path = self._browse_file_dialog(
            title=f"Select {key}", initialdir=initialdir, extensions=extensions,
        )
        if path:
            self.edit_vars[key].set(path)

    def _on_edit_upload(self) -> None:
        if self._busy:
            return

        def _clean(value: str) -> str:
            v = value.strip()
            return "" if v == "—" else v

        cid = _clean(self.edit_ctx_vars["cid"].get())
        box_id = _clean(self.edit_ctx_vars["box_id"].get())
        gstin = _clean(self.edit_ctx_vars["gstin"].get())
        py_file = self.edit_vars["edit_py_file"].get().strip()
        template_file = self.edit_vars["edit_template_file"].get().strip()

        missing = [
            name for name, val in [
                ("cid", cid), ("box_id", box_id),
                ("py_file", py_file), ("template_file", template_file),
            ] if not val
        ]
        if missing:
            messagebox.showerror("Missing values", "Required: " + ", ".join(missing))
            return

        kwargs = {
            "gstin": gstin,
            "uid_update": self.edit_vars["edit_uid_update"].get().strip(),
            "is_ledger_creation": self.edit_vars["edit_is_ledger_creation"].get().strip(),
            "is_item_creation": self.edit_vars["edit_is_item_creation"].get().strip(),
            "is_cc_creation": self.edit_vars["edit_is_cc_creation"].get().strip(),
            "is_tagg_creation": self.edit_vars["edit_is_tagg_creation"].get().strip(),
        }

        self._busy = True
        self.edit_btn.configure(state="disabled")
        self.status_var.set("Uploading to EditMenu…")
        self.progress.pack(side="right")
        self.progress.start(12)

        threading.Thread(
            target=self._edit_worker,
            args=(cid, box_id, py_file, template_file, kwargs),
            daemon=True,
        ).start()

    def _edit_worker(self, cid, box_id, py_file, template_file, kwargs) -> None:
        try:
            result = edit_menu(
                cid=cid, box_id=box_id,
                py_file_path=py_file, template_file_path=template_file,
                **kwargs,
            )
            self._queue.put(("edit_ok", result))
        except Exception as e:
            self._queue.put(("edit_err", f"{type(e).__name__}: {e}"))

    def _build_delete_tab(self) -> None:
        frame = ttk.Frame(self.notebook, style="Panel.TFrame", padding=16)
        self.notebook.add(frame, text="Delete Menu")

        ttk.Label(
            frame,
            text="Menu IDs (one per line, commas or spaces also accepted):",
            style="Section.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        self.delete_ids_text = scrolledtext.ScrolledText(
            frame, wrap="word", height=10, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", borderwidth=1, font=("DejaVu Sans Mono", 10),
        )
        self.delete_ids_text.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(0, 8))

        saved = self._settings
        self.delete_uid_var = tk.StringVar(value=saved.get("delete_uid_update", "1111"))
        self.delete_uid_var.trace_add("write", lambda *_a: self._persist_delete_setting())

        ttk.Label(frame, text="uid_update", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=4
        )
        ttk.Entry(frame, textvariable=self.delete_uid_var, width=30).grid(
            row=2, column=1, sticky="w", pady=4
        )

        load_row = ttk.Frame(frame, style="Panel.TFrame")
        load_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(load_row, text="Load IDs from current job:", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        ttk.Button(
            load_row, text="Deploy From",
            command=lambda: self._load_delete_ids("from"),
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            load_row, text="Deploy To",
            command=lambda: self._load_delete_ids("to"),
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            load_row, text="Both",
            command=lambda: self._load_delete_ids("both"),
        ).pack(side="left")

        btn_row = ttk.Frame(frame, style="Panel.TFrame")
        btn_row.grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 8))
        self.delete_btn = tb.Button(
            btn_row, text="Delete Menus", bootstyle="danger", command=self._on_delete_run,
        )
        self.delete_btn.pack(side="left")
        ttk.Button(
            btn_row, text="Clear",
            command=lambda: self.delete_ids_text.delete("1.0", "end"),
        ).pack(side="left", padx=(8, 0))

        self.delete_result = scrolledtext.ScrolledText(
            frame, wrap="word", height=12, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", borderwidth=1, font=("DejaVu Sans Mono", 10),
        )
        self.delete_result.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        self._make_readonly(self.delete_result)

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.rowconfigure(5, weight=2)

    def _persist_delete_setting(self) -> None:
        value = self.delete_uid_var.get().strip()
        if value:
            self._settings["delete_uid_update"] = value
        else:
            self._settings.pop("delete_uid_update", None)
        self._save_settings()

    def _load_delete_ids(self, side: str) -> None:
        data = self._last_data or {}
        if not data:
            messagebox.showinfo("No job loaded", "Fetch a job first, then load IDs from its menu list.")
            return
        sides = ("from", "to") if side == "both" else (side,)
        ids: list[str] = []
        seen: set[str] = set()
        for s in sides:
            for row in (data.get(f"menu_list_{s}") or []):
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or "").strip()
                if rid and rid not in seen:
                    seen.add(rid)
                    ids.append(rid)
        if not ids:
            messagebox.showinfo(
                "No IDs found",
                f"No 'id' values were present in menu_list_{side}.\n"
                "Make sure the job's menu list returned matches with id fields.",
            )
            return
        existing = self.delete_ids_text.get("1.0", "end").strip()
        prefix = (existing + "\n") if existing else ""
        self.delete_ids_text.delete("1.0", "end")
        self.delete_ids_text.insert("1.0", prefix + "\n".join(ids))
        self.status_var.set(f"Loaded {len(ids)} id(s) from menu_list_{side}.")

    @staticmethod
    def _parse_delete_ids(raw: str) -> list[str]:
        tokens = re.split(r"[\s,]+", raw or "")
        seen: set[str] = set()
        ids: list[str] = []
        for tok in tokens:
            tok = tok.strip().strip('"').strip("'").rstrip(",")
            if tok and tok not in seen:
                seen.add(tok)
                ids.append(tok)
        return ids

    def _on_delete_run(self) -> None:
        if self._busy:
            return

        raw = self.delete_ids_text.get("1.0", "end")
        ids = self._parse_delete_ids(raw)
        if not ids:
            messagebox.showerror("No IDs", "Paste at least one menu id to delete.")
            return

        uid_update = self.delete_uid_var.get().strip() or "1111"

        preview = "\n".join(ids[:10])
        if len(ids) > 10:
            preview += f"\n… and {len(ids) - 10} more"
        if not messagebox.askyesno(
            "Confirm delete",
            f"You are about to DELETE {len(ids)} menu(s):\n\n{preview}\n\nProceed?",
        ):
            return

        expected = f"realbooks@@adansa{date.today().strftime('%d%m')}"
        typed = simpledialog.askstring(
            "Security check",
            f"This is irreversible. Enter the delete password to confirm "
            f"deleting {len(ids)} menu(s):",
            parent=self, show="*",
        )
        if typed is None:
            return
        if typed.strip() != expected:
            messagebox.showwarning("Cancelled", "Confirmation password did not match. No menus were deleted.")
            return

        self._busy = True
        self.delete_btn.configure(state="disabled")
        self.status_var.set(f"Deleting {len(ids)} menu(s)…")
        self.progress.pack(side="right")
        self.progress.start(12)

        threading.Thread(
            target=self._delete_worker, args=(ids, uid_update), daemon=True,
        ).start()

    def _delete_worker(self, ids: list[str], uid_update: str) -> None:
        try:
            results = delete_menu(ids, uid_update=uid_update)
            self._queue.put(("delete_ok", results))
        except Exception as e:
            self._queue.put(("delete_err", f"{type(e).__name__}: {e}"))

    def _build_json_tab(self) -> None:
        frame = ttk.Frame(self.notebook, style="Panel.TFrame", padding=16)
        self.notebook.add(frame, text="Raw JSON")
        self.json_text = scrolledtext.ScrolledText(
            frame, wrap="none", bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", borderwidth=1, font=("DejaVu Sans Mono", 10),
        )
        self.json_text.pack(fill="both", expand=True)
        self._make_readonly(self.json_text)
 
    # ------------------------------------------------------------------ upload helpers
 
    def _persist_setting(self, key: str) -> None:
        value = self.upload_vars[key].get().strip()
        if value:
            self._settings[key] = value
        else:
            self._settings.pop(key, None)
        self._save_settings()
 
    def _refresh_upload_ctx(self) -> None:
        data = self._last_data or {}
        src = self.upload_source_var.get()
        prefix = "deploy_from_" if src == "from" else "deploy_to_"
        cid = data.get(f"{prefix}cid", "")
        segids = data.get(f"{prefix}segids") or []
        block = data.get(f"{prefix}block") or ""
        box_id = data.get(f"{prefix}box") or data.get("box_id") or ""
        menu_name = data.get(f"{prefix}menu") or data.get("menu_name") or ""
        gstin = data.get(f"{prefix}gstin") or data.get("gstin") or ""
        domain_alias = data.get(f"{prefix}domain") or data.get("domain_alias") or ""
        parsed = data.get("desc_parsed") or {}
        if not cid:
            cid = parsed.get("cid", "")
        if not segids and parsed.get("segid"):
            segids = [parsed["segid"]]
        if not box_id:
            box_id = parsed.get("box", "")
        if not menu_name:
            menu_name = parsed.get("menu_name", "")
        if not gstin:
            gstin = parsed.get("gstin", "")
        if not domain_alias:
            domain_alias = parsed.get("domain", "")
        self.upload_ctx_vars["cid"].set(cid or "—")
        self.upload_ctx_vars["segids"].set(", ".join(segids) if segids else "—")
        self.upload_ctx_vars["box_id"].set(box_id or "—")
        self.upload_ctx_vars["menu_name"].set(menu_name or "—")
        self.upload_ctx_vars["gstin"].set(gstin or "—")
        self.upload_ctx_vars["domain_alias"].set(domain_alias or "—")
        self._set_text(self.upload_block_text, block or f"(no deploy-{src} section detected)")
 
    def _pick_file(self, key: str, filetypes: list[tuple[str, str]]) -> None:
        current = self.upload_vars[key].get().strip()
        initialdir = None
        if current and os.path.isdir(os.path.dirname(current)):
            initialdir = os.path.dirname(current)
        elif key == "py_file":
            initialdir = REALBOOKS_ROOT
        extensions = self._filetypes_to_extensions(filetypes)
        path = self._browse_file_dialog(
            title=f"Select {key}", initialdir=initialdir, extensions=extensions,
        )
        if path:
            self.upload_vars[key].set(path)

    @staticmethod
    def _filetypes_to_extensions(filetypes: list[tuple[str, str]]) -> list[str]:
        for _label, pattern in filetypes:
            exts: list[str] = []
            for part in pattern.split():
                part = part.strip()
                if part in ("*", "*.*", ""):
                    return []
                if part.startswith("*"):
                    part = part[1:]
                if part and part != ".":
                    exts.append(part.lower())
            if exts:
                return exts
        return []

    def _browse_file_dialog(self, title: str, initialdir: str | None,
                             extensions: list[str]) -> str | None:
        start = initialdir or REALBOOKS_ROOT
        if not start or not os.path.isdir(start):
            start = os.path.expanduser("~")

        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("960x560")

        top = ttk.Frame(dlg, padding=(8, 8, 8, 4))
        top.pack(fill="x")
        dir_var = tk.StringVar(value=start)

        def _go_back() -> None:
            folder = dir_var.get().strip().rstrip(os.sep)
            parent = os.path.dirname(folder)
            if parent and parent != folder and os.path.isdir(parent):
                dir_var.set(parent)
                filter_var.set("")
                _refresh()

        ttk.Button(top, text="← Back", command=_go_back).pack(side="left", padx=(0, 6))
        ttk.Label(top, text="Directory:").pack(side="left", padx=(0, 6))
        dir_entry = ttk.Entry(top, textvariable=dir_var)
        dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        search_row = ttk.Frame(dlg, padding=(8, 4))
        search_row.pack(fill="x")
        ttk.Label(search_row, text="Search:").pack(side="left", padx=(0, 6))
        filter_var = tk.StringVar()
        search_entry = ttk.Entry(search_row, textvariable=filter_var)
        search_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(search_row, text="Clear",
                   command=lambda: filter_var.set("")).pack(side="left", padx=(6, 0))

        body = ttk.Frame(dlg, padding=(8, 4))
        body.pack(fill="both", expand=True)
        cols = ("name", "size", "modified")
        tree = ttk.Treeview(body, columns=cols, show="headings", height=18)
        tree.heading("name", text="Name")
        tree.heading("size", text="Size (KB)")
        tree.heading("modified", text="Modified")
        tree.column("name", width=520, anchor="w")
        tree.column("size", width=100, anchor="e")
        tree.column("modified", width=180, anchor="w")
        vscroll = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vscroll.set)
        tree.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

        selected: dict = {"path": None}

        def _refresh(*_a) -> None:
            folder = dir_var.get().strip()
            tree.delete(*tree.get_children())
            if not folder or not os.path.isdir(folder):
                return
            term = filter_var.get().strip().lower()

            parent = os.path.dirname(folder.rstrip(os.sep))
            if parent and parent != folder and os.path.isdir(parent):
                tree.insert("", "end", iid=parent, values=("[..]", "", ""))

            try:
                entries = sorted(os.listdir(folder))
            except OSError:
                return

            dirs: list[tuple[str, str, float]] = []
            files: list[tuple[str, str, int, float]] = []
            for name in entries:
                full = os.path.join(folder, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if os.path.isdir(full):
                    if term and term not in name.lower():
                        continue
                    dirs.append((name, full, st.st_mtime))
                    continue
                if not os.path.isfile(full):
                    continue
                if extensions and not any(name.lower().endswith(ext) for ext in extensions):
                    continue
                if term and term not in name.lower():
                    continue
                files.append((name, full, st.st_size, st.st_mtime))

            for name, full, mtime in dirs:
                ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                tree.insert("", "end", iid=full, values=(f"[{name}]", "", ts))

            files.sort(key=lambda x: x[3], reverse=True)
            for name, full, size, mtime in files:
                size_kb = round(size / 1024, 2)
                ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                tree.insert("", "end", iid=full, values=(name, f"{size_kb}", ts))

        def _confirm(_e=None) -> None:
            sel = tree.selection()
            if not sel:
                return
            path = sel[0]
            if os.path.isdir(path):
                dir_var.set(path)
                filter_var.set("")
                _refresh()
                return
            if os.path.isfile(path):
                selected["path"] = path
                dlg.destroy()

        def _cancel() -> None:
            dlg.destroy()

        dir_entry.bind("<Return>", lambda _e: _refresh())
        filter_var.trace_add("write", _refresh)
        tree.bind("<Double-Button-1>", _confirm)
        tree.bind("<Return>", _confirm)

        btn_row = ttk.Frame(dlg, padding=(8, 4, 8, 8))
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Cancel", command=_cancel).pack(side="right")
        ttk.Button(btn_row, text="Open", command=_confirm).pack(side="right", padx=(0, 8))

        _refresh()
        search_entry.focus_set()
        dlg.wait_window()
        return selected["path"]
 
    @staticmethod
    def _sanitize(name: str) -> str:
        name = re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_")
        return name or "untitled"
 
    @staticmethod
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
 
    @staticmethod
    def _parse_description(text: str) -> dict:
        if not text:
            return {}
        mapping = {
            "domain": "domain",
            "box": "box",
            "company": "company",
            "cid": "cid",
            "c id": "cid",
            "segment": "segment",
            "seg id": "segid",
            "segid": "segid",
            "menu name": "menu_name",
            "gstin": "gstin",
        }
        # Whitespace-only fallback patterns (e.g. "New Deployment Details" format:
        # "CID 5342", "SEG ID 5353"). Longer keys first so "seg id" beats "seg".
        plain_patterns = []
        for k in sorted(mapping.keys(), key=len, reverse=True):
            key_re = r"\s+".join(re.escape(t) for t in k.split())
            plain_patterns.append(
                (mapping[k], re.compile(rf"^\s*{key_re}\s+(.+?)\s*$", flags=re.I))
            )
        result: dict = {}
        for line in text.splitlines():
            m = re.match(r"\s*([A-Za-z][A-Za-z ]*?)\s*[-:=]\s*(.+?)\s*$", line)
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
        return result

    @staticmethod
    def _find_source_from_menu(data: dict) -> str:
        """Return the first existing py file path referenced by the menu list.
 
        Strategy:
            1. Direct-hit pass: any path that already resolves on disk.
            2. Basename pass: walk REALBOOKS_ROOT once, collecting first match
               for any basename we still need.
        """
        rows = data.get("menu_list") or []
        for row in rows:
            path = str(row.get("py_file_path") or "").strip()
            if path and os.path.isfile(path):
                return path
 
        wanted = {
            os.path.basename(str(r.get("py_file_path") or "").strip())
            for r in rows
        }
        wanted.discard("")
        if not wanted or not os.path.isdir(REALBOOKS_ROOT):
            return ""
 
        for dirpath, _dirs, files in os.walk(REALBOOKS_ROOT):
            for name in files:
                if name in wanted:
                    return os.path.join(dirpath, name)
        return ""
 
    # ------------------------------------------------------------------ actions
 
    def _on_create_py(self) -> None:
        data = self._last_data or {}
        job = (data.get("job") or "").strip()
        title = (data.get("title") or "").strip()
        domain_alias = (data.get("domain_alias") or "").strip()
 
        missing = [
            n for n, v in [
                ("job", job), ("title", title),
                ("domain_alias", domain_alias),
            ] if not v
        ]
        if missing:
            messagebox.showerror("Missing values", "Required: " + ", ".join(missing))
            return
 
        src = self._find_source_from_menu(data)
        if not src or not os.path.isfile(src):
            picked = self.upload_vars["py_file"].get().strip()
            if picked and os.path.isfile(picked):
                src = picked
        if not src or not os.path.isfile(src):
            initialdir = self._settings.get("last_src_dir") or REALBOOKS_ROOT
            src = filedialog.askopenfilename(
                title="Source py file not found in menu — pick one",
                filetypes=[("Python", "*.py"), ("All", "*.*")],
                initialdir=initialdir,
            )
            if not src:
                return
            self._settings["last_src_dir"] = os.path.dirname(src)
            self._save_settings()
 
        folder = self._find_domain_folder(domain_alias)
        if not folder:
            messagebox.showerror(
                "Domain folder not found",
                f"No folder matching '{domain_alias}' under {REALBOOKS_ROOT}",
            )
            return
 
        filename = f"{self._sanitize(job)}_{self._sanitize(title)}.py"
        dest = os.path.join(folder, filename)
        if os.path.exists(dest):
            if not messagebox.askyesno("Overwrite?", f"{dest}\n\nFile exists. Overwrite?"):
                return
        try:
            shutil.copyfile(src, dest)
        except OSError as e:
            messagebox.showerror("Create failed", str(e))
            return
 
        self.upload_vars["py_file"].set(dest)
        self.status_var.set(f"Created {dest}")
        messagebox.showinfo("Created", dest)

    @staticmethod
    def _search_py_files(pattern: str) -> list[dict]:
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
        return matches

    def _pick_search_result(self, results: list[dict]) -> dict | None:
        if len(results) == 1:
            return results[0]
        results.sort(key=lambda x: x["modified"], reverse=True)
        dlg = tk.Toplevel(self)
        dlg.title(f"Select file ({len(results)} matches)")
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("980x420")

        lb = tk.Listbox(dlg, font=("DejaVu Sans Mono", 10), activestyle="dotbox")
        for r in results:
            lb.insert("end", f"[{r['modified']}]  {r['size_kb']:>8} KB  {r['path']}")
        if results:
            lb.selection_set(0)
        lb.pack(fill="both", expand=True, padx=8, pady=8)

        selected: dict = {"value": None}

        def _confirm(_e=None):
            sel = lb.curselection()
            if sel:
                selected["value"] = results[sel[0]]
                dlg.destroy()

        def _cancel():
            dlg.destroy()

        lb.bind("<Double-Button-1>", _confirm)
        lb.bind("<Return>", _confirm)

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="Cancel", command=_cancel).pack(side="right")
        ttk.Button(btns, text="Select", command=_confirm).pack(side="right", padx=(0, 8))

        dlg.wait_window()
        return selected["value"]

    @staticmethod
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

    def _on_search_clone(self) -> None:
        self._run_search_and_clone(
            file_type_label="py file",
            default_ext_pattern=".py",
            upload_var_key="py_file",
        )

    def _on_search_clone_template(self) -> None:
        self._run_search_and_clone(
            file_type_label="template file",
            default_ext_pattern=".xls*",
            upload_var_key="template_file",
        )

    def _run_search_and_clone(self, file_type_label: str,
                              default_ext_pattern: str, upload_var_key: str) -> None:
        term = simpledialog.askstring(
            f"Search {file_type_label}", "Enter file name to search:", parent=self
        )
        if term is None:
            return
        term = term.strip()
        if not term:
            messagebox.showwarning("Missing input", "No filename entered.")
            return

        if "*" not in term and "?" not in term:
            pattern = f"*{term}*{default_ext_pattern}" if "." not in term else f"*{term}*"
        else:
            pattern = term

        results = self._search_py_files(pattern)
        if not results:
            messagebox.showinfo(
                "No matches",
                f"No files matching '{pattern}' under {REALBOOKS_ROOT}",
            )
            return

        selected = self._pick_search_result(results)
        if not selected:
            return

        data = self._last_data or {}
        deploy_to_domain = (data.get("deploy_to_domain") or data.get("domain_alias") or "").strip()
        if not deploy_to_domain:
            messagebox.showerror(
                "Missing Deploy TO domain",
                "Fetch a job first — the clone needs a Deploy TO domain "
                "to pick the destination folder.",
            )
            return
        target_folder = self._find_domain_folder(deploy_to_domain)
        if not target_folder:
            messagebox.showerror(
                "Domain folder not found",
                f"No folder matching '{deploy_to_domain}' under {REALBOOKS_ROOT}",
            )
            return

        new_name = simpledialog.askstring(
            "Clone as",
            f"Enter new file name for clone:\n"
            f"(source: {os.path.basename(selected['path'])})\n"
            f"(destination: {target_folder})",
            parent=self,
        )
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name:
            messagebox.showwarning("Missing input", "No filename entered.")
            return

        try:
            clone_path, renamed = self._clone_py_file(
                selected["path"], new_name, target_folder=target_folder
            )
        except OSError as e:
            messagebox.showerror("Clone failed", str(e))
            return

        self.upload_vars[upload_var_key].set(clone_path)
        note = " (existing file — saved with timestamp suffix)" if renamed else ""
        self.status_var.set(f"Cloned → {clone_path}{note}")
        messagebox.showinfo("Cloned", f"{clone_path}{note}")

    def _on_upload(self) -> None:
        if self._busy:
            return
 
        def _clean(value: str) -> str:
            v = value.strip()
            return "" if v == "—" else v
 
        cid = _clean(self.upload_ctx_vars["cid"].get())
        segids_raw = _clean(self.upload_ctx_vars["segids"].get())
        segids = [s for s in re.split(r"[\s,]+", segids_raw) if s]
        box_id = _clean(self.upload_ctx_vars["box_id"].get())
        menu_name = _clean(self.upload_ctx_vars["menu_name"].get())
        gstin = _clean(self.upload_ctx_vars["gstin"].get())
        domain_alias = _clean(self.upload_ctx_vars["domain_alias"].get())
        py_file = self.upload_vars["py_file"].get().strip()
        template_file = self.upload_vars["template_file"].get().strip()
 
        missing = [
            name for name, val in [
                ("cid", cid), ("segids", segids), ("box_id", box_id),
                ("menu_name", menu_name), ("domain_alias", domain_alias),
                ("py_file", py_file), ("template_file", template_file),
            ] if not val
        ]
        if missing:
            messagebox.showerror("Missing values", "Required: " + ", ".join(missing))
            return
 
        kwargs = {k: self.upload_vars[k].get().strip() for k in (
            "rlb_module_type", "file_ext_type", "uid_create", "uid_update",
            "is_ledger_creation", "is_item_creation", "is_cc_creation", "is_tagg_creation",
        )}
 
        self._busy = True
        self.upload_btn.configure(state="disabled")
        self.status_var.set("Uploading to AddMenu…")
        self.progress.pack(side="right")
        self.progress.start(12)
 
        threading.Thread(
            target=self._upload_worker,
            args=(cid, segids, box_id, menu_name, gstin, domain_alias, py_file, template_file, kwargs),
            daemon=True,
        ).start()
 
    def _upload_worker(self, cid, segids, box_id, menu_name, gstin, domain_alias,
                       py_file, template_file, kwargs) -> None:
        try:
            result = add_menu(
                cid=cid, segids=segids, box_id=box_id, menu_name=menu_name,
                gstin=gstin, domain_alias=domain_alias,
                py_file_path=py_file, template_file_path=template_file,
                **kwargs,
            )
            self._queue.put(("upload_ok", result))
        except Exception as e:
            self._queue.put(("upload_err", f"{type(e).__name__}: {e}"))
 
    def _on_fetch(self) -> None:
        if self._busy:
            return
        job_number = self.job_var.get().strip()
        if not job_number:
            messagebox.showwarning("Missing input", "Please enter a job number.")
            return
        self._busy = True
        self.fetch_btn.configure(state="disabled")
        self.status_var.set(f"Fetching {job_number}… (this can take 30–60s)")
        self.progress.pack(side="right")
        self.progress.start(12)
 
        headless = self.headless_var.get()
        threading.Thread(
            target=self._worker, args=(job_number, headless), daemon=True
        ).start()
 
    def _worker(self, job_number: str, headless: bool) -> None:
        try:
            driver = build_driver(headless=headless)
            wait = WebDriverWait(driver, 25)
            try:
                login(driver, wait)
                data = fetch_job(driver, wait, job_number)
                parsed_desc = self._parse_description(data.get("description") or "")
                if parsed_desc:
                    data["desc_parsed"] = parsed_desc

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
                    if cid and segids and box_id_lookup:
                        data[f"menu_list_{side}"] = fetch_menu_list(
                            cid, segids, box_id_lookup, menu_name=menu_name_lookup
                        )
                    else:
                        data[f"menu_list_{side}"] = []

                # Default menu_list (used by other tabs / JSON view): prefer FROM, fall back to TO.
                data["menu_list"] = data["menu_list_from"] or data["menu_list_to"]
                self._queue.put(("ok", data))
            finally:
                driver.quit()
        except SystemExit as e:
            self._queue.put(("err", str(e)))
        except Exception as e:
            self._queue.put(("err", f"{type(e).__name__}: {e}"))
 
    # ------------------------------------------------------------------ queue + render
 
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self._busy = False
                self.fetch_btn.configure(state="normal")
                self.upload_btn.configure(state="normal")
                self.edit_btn.configure(state="normal")
                self.delete_btn.configure(state="normal")
                self.progress.stop()
                self.progress.pack_forget()
                if kind == "ok":
                    self._render(payload)
                    self.status_var.set(f"Loaded {payload.get('job', '')}.")
                elif kind == "upload_ok":
                    self._set_text(self.upload_result, json.dumps(payload, indent=2, ensure_ascii=False))
                    self._append_job_log("UPLOAD OK", payload)
                    self.status_var.set("Upload complete.")
                elif kind == "upload_err":
                    self._set_text(self.upload_result, payload)
                    self._append_job_log("UPLOAD ERROR", payload)
                    self.status_var.set("Upload error.")
                    messagebox.showerror("Upload failed", payload)
                elif kind == "edit_ok":
                    self._set_text(self.edit_result, json.dumps(payload, indent=2, ensure_ascii=False))
                    self._append_job_log("EDIT OK", payload)
                    self.status_var.set("Edit upload complete.")
                elif kind == "edit_err":
                    self._set_text(self.edit_result, payload)
                    self._append_job_log("EDIT ERROR", payload)
                    self.status_var.set("Edit upload error.")
                    messagebox.showerror("Edit upload failed", payload)
                elif kind == "delete_ok":
                    self._set_text(self.delete_result, json.dumps(payload, indent=2, ensure_ascii=False))
                    self._append_job_log("DELETE OK", payload)
                    self.status_var.set(f"Delete complete ({len(payload)} processed).")
                elif kind == "delete_err":
                    self._set_text(self.delete_result, payload)
                    self._append_job_log("DELETE ERROR", payload)
                    self.status_var.set("Delete error.")
                    messagebox.showerror("Delete failed", payload)
                else:
                    self.status_var.set("Error.")
                    messagebox.showerror("Lookup failed", payload)
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)
 
    def _render(self, data: dict) -> None:
        self._last_data = data
        self._record_recent_job(str(data.get("job") or self.job_var.get()))
        self._start_job_log(data)
        self._refresh_upload_ctx()
        self._refresh_edit_ctx()
 
        title = data.get("title") or "(no title)"
        self.summary_title.configure(text=f"{data.get('job', '')} — {title}")
        for key, var in self.summary_vars.items():
            value = data.get(key, "")
            if isinstance(value, list):
                value = ", ".join(value) if value else "—"
            var.set(str(value) if value not in (None, "") else "—")
 
        self._set_text(self.desc_text, data.get("description") or "(empty)")

        # Pick a sensible default source for the Menu list radio.
        if data.get("menu_list_from"):
            self.menu_source_var.set("from")
        elif data.get("menu_list_to"):
            self.menu_source_var.set("to")
        self._apply_menu_source()

        for upload_key in ("is_ledger_creation", "is_item_creation", "rlb_module_type"):
            for row in (data.get("menu_list") or []):
                value = str(row.get(upload_key) or "").strip()
                if value:
                    self.upload_vars[upload_key].set(value)
                    break
 
        self._set_text(self.json_text, json.dumps(data, indent=2, ensure_ascii=False))
 
    def _apply_menu_source(self) -> None:
        data = self._last_data or {}
        side = self.menu_source_var.get()
        key = f"menu_list_{side}"
        if key in data:
            menu_rows = data.get(key) or []
        else:
            menu_rows = data.get("menu_list") or []
        self._menu_rows_cache = []
        for row in self._dedupe_menu_rows(menu_rows):
            template_raw = row.get("template_file_path") or ""
            template_name = os.path.basename(template_raw) if template_raw else ""
            self._menu_rows_cache.append((
                row.get("segid", ""),
                row.get("py_file_path") or "—",
                template_name or "—",
                row.get("gstin") or "—",
                row.get("rlb_module_type") or "—",
                row.get("file_ext_type") or "—",
                row.get("is_ledger_creation") or "—",
                row.get("is_item_creation") or "—",
                row.get("is_cc_creation") or "—",
                row.get("is_tagg_creation") or "—",
            ))
        label = "Deploy From" if side == "from" else "Deploy To"
        self.menu_source_count.configure(text=f"({label}: {len(menu_rows)} row(s))")
        self._apply_menu_filter()

    @staticmethod
    def _dedupe_menu_rows(rows: list[dict]) -> list[dict]:
        extra_keys = (
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
 
    @staticmethod
    def _set_text(widget: scrolledtext.ScrolledText, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)

    def _apply_menu_filter(self) -> None:
        term = (self.menu_filter_var.get() or "").strip().lower()
        self.menu_tree.delete(*self.menu_tree.get_children())
        for values in self._menu_rows_cache:
            if not term or any(term in str(v).lower() for v in values):
                self.menu_tree.insert("", "end", values=values)

    def _set_realbooks_folder(self) -> None:
        global REALBOOKS_ROOT
        start = REALBOOKS_ROOT if os.path.isdir(REALBOOKS_ROOT) else os.path.expanduser("~")
        folder = filedialog.askdirectory(title="Select Realbooks folder", initialdir=start)
        if not folder:
            return
        REALBOOKS_ROOT = folder
        self._settings["realbooks_root"] = folder
        self._save_settings()
        self.status_var.set(f"Realbooks folder: {folder}")

    def _cleanup_old_logs(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        pattern = re.compile(r"^.+_(\d{8})_\d{6}\.txt$")
        try:
            names = os.listdir(LOGS_DIR)
        except OSError:
            return
        for name in names:
            m = pattern.match(name)
            if not m or m.group(1) == today:
                continue
            try:
                os.remove(os.path.join(LOGS_DIR, name))
            except OSError:
                pass

    def _open_logs_folder(self) -> None:
        os.makedirs(LOGS_DIR, exist_ok=True)
        try:
            if sys.platform.startswith("linux"):
                subprocess.Popen(["xdg-open", LOGS_DIR])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", LOGS_DIR])
            elif sys.platform.startswith("win"):
                os.startfile(LOGS_DIR)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("Cannot open", f"{LOGS_DIR}\n\n{exc}")

    def _record_recent_job(self, job: str) -> None:
        job = (job or "").strip()
        if not job:
            return
        recent = list(self._settings.get("recent_jobs") or [])
        recent = [j for j in recent if j != job]
        recent.insert(0, job)
        recent = recent[:10]
        self._settings["recent_jobs"] = recent
        self._save_settings()
        try:
            self.job_entry.configure(values=recent)
        except tk.TclError:
            pass

    def _start_job_log(self, data: dict) -> None:
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            job = str(data.get("job") or self.job_var.get() or "unknown").strip() or "unknown"
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
            menu_rows = data.get("menu_list") or []
            if menu_rows:
                for row in menu_rows:
                    lines.append(json.dumps(row, ensure_ascii=False))
            else:
                lines.append("(empty)")
            lines.append("")
            lines.append("--- Raw JSON ---")
            lines.append(json.dumps(data, indent=2, ensure_ascii=False))

            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            self._current_log_path = path
        except OSError:
            self._current_log_path = None

    def _append_job_log(self, label: str, payload) -> None:
        try:
            if not self._current_log_path:
                os.makedirs(LOGS_DIR, exist_ok=True)
                job = (self.job_var.get() or "unknown").strip() or "unknown"
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                self._current_log_path = os.path.join(LOGS_DIR, f"{job}_{ts}.txt")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self._current_log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n\n=== {label} @ {ts} ===\n")
                if isinstance(payload, (dict, list)):
                    fh.write(json.dumps(payload, indent=2, ensure_ascii=False))
                else:
                    fh.write(str(payload))
        except OSError:
            pass

    @staticmethod
    def _bind_tree_copy(tree: ttk.Treeview) -> None:
        cols = tree["columns"]
        last_rclick: dict = {"event": None}

        def _rows_to_text(items) -> str:
            headers = [tree.heading(c)["text"] for c in cols]
            lines = ["\t".join(headers)]
            for item in items:
                values = tree.item(item, "values")
                lines.append("\t".join(str(v) for v in values))
            return "\n".join(lines)

        def _copy_selected(_event=None):
            items = tree.selection()
            if not items:
                items = tree.get_children()
            if not items:
                return "break"
            text = _rows_to_text(items)
            tree.clipboard_clear()
            tree.clipboard_append(text)
            tree.update_idletasks()
            return "break"

        def _select_all(_event=None):
            tree.selection_set(tree.get_children())
            return "break"

        def _copy_all(_event=None):
            _select_all()
            return _copy_selected()

        def _cell_value_at(event) -> str | None:
            row = tree.identify_row(event.y)
            col = tree.identify_column(event.x)
            if not row or not col:
                return None
            try:
                col_idx = int(col.lstrip("#")) - 1
            except ValueError:
                return None
            if col_idx < 0 or col_idx >= len(cols):
                return None
            values = tree.item(row, "values")
            if col_idx >= len(values):
                return None
            return str(values[col_idx])

        def _copy_cell_at(event):
            value = _cell_value_at(event)
            if not value:
                return "break"
            tree.clipboard_clear()
            tree.clipboard_append(value)
            tree.update_idletasks()
            return "break"

        def _copy_cell_from_popup():
            event = last_rclick["event"]
            if event is not None:
                _copy_cell_at(event)

        tree.bind("<Control-c>", _copy_selected)
        tree.bind("<Control-C>", _copy_selected)
        tree.bind("<Control-Insert>", _copy_selected)
        tree.bind("<Control-a>", _select_all)
        tree.bind("<Control-A>", _select_all)
        tree.bind("<Double-Button-1>", _copy_cell_at)

        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="Copy this cell", command=_copy_cell_from_popup)
        menu.add_command(label="Copy selected row(s)", command=_copy_selected)
        menu.add_command(label="Copy all rows", command=_copy_all)
        menu.add_separator()
        menu.add_command(label="Select all", command=_select_all)

        def _popup(event):
            last_rclick["event"] = event
            iid = tree.identify_row(event.y)
            if iid and iid not in tree.selection():
                tree.selection_set(iid)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        tree.bind("<Button-3>", _popup)

    @staticmethod
    def _make_readonly(widget: tk.Text) -> None:
        def _copy(_event=None):
            try:
                selected = widget.get("sel.first", "sel.last")
            except tk.TclError:
                return "break"
            if selected:
                widget.clipboard_clear()
                widget.clipboard_append(selected)
                widget.update_idletasks()
            return "break"

        def _select_all(_event=None):
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "1.0")
            widget.see("insert")
            return "break"

        def _on_key(event):
            if event.state & 0x4:  # Control held — allow copy / select-all
                if event.keysym.lower() in ("c", "a", "insert"):
                    return None
            if event.keysym in (
                "Left", "Right", "Up", "Down", "Home", "End",
                "Prior", "Next",
                "Shift_L", "Shift_R", "Control_L", "Control_R",
                "Alt_L", "Alt_R",
            ):
                return None
            return "break"

        widget.bind("<Key>", _on_key)
        widget.bind("<Control-c>", _copy)
        widget.bind("<Control-C>", _copy)
        widget.bind("<Control-Insert>", _copy)
        widget.bind("<Control-a>", _select_all)
        widget.bind("<Control-A>", _select_all)
        widget.bind("<<Paste>>", lambda _e: "break")
        widget.bind("<<Cut>>", lambda _e: "break")
        widget.bind("<Button-2>", lambda _e: "break")

        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Copy", command=_copy)
        menu.add_command(label="Select All", command=_select_all)

        def _popup(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        widget.bind("<Button-3>", _popup)
 
 
if __name__ == "__main__":
    JobLookupApp().mainloop()