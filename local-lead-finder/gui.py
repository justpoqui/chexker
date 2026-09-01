# ============================================================
# STEP 1 — SCAFFOLD: module purpose
# WHY: all Tkinter widget code lives here and nowhere else, so
#      the network/scoring modules never need to know Tkinter
#      exists. Everything network-related runs on a background
#      thread and talks back to the widgets only through a
#      queue.Queue the main loop polls — the Tk main loop itself
#      must never block, or the whole window freezes.
# ============================================================

import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import enrich
import export
import osm_source
import scoring
from cache import Cache
from osm_source import ALL_CATEGORY_KEYS, US_STATES, AreaCandidate, Business

STATE_NAME_TO_ISO = dict(US_STATES)
ANY_STATE_LABEL = "Any state"
from scoring import ScoreResult

CALL_SHEET_LIMIT = 20

WINDOW_SIZE = "1100x700"

# Warm-to-cool background per tier, matching the scoring order: the
# hottest lead (NO_PRESENCE) is reddest, the coldest (HEALTHY) is green.
TIER_COLORS = {
    "NO_PRESENCE": "#ffb3b3",
    "FACEBOOK_ONLY": "#ffcc99",
    "SOCIAL_ONLY": "#ffe6a8",
    "SITE_NO_SOCIAL": "#fff6b0",
    "WEAK_SITE": "#dce8ff",
    "HEALTHY": "#c8f0c8",
    "CHAIN": "#d9d9d9",  # deliberately outside the warm-to-cool gradient — not a lead at all
}

COLUMNS = ("score", "tier", "new", "status", "name", "category", "phone", "web", "address", "edited")
COLUMN_HEADINGS = {
    "score": "Score",
    "tier": "Tier",
    "new": "New?",
    "status": "Status",
    "name": "Name",
    "category": "Category",
    "phone": "Phone",
    "web": "Website / FB",
    "address": "Address",
    "edited": "Last Edited",
}

# ============================================================
# STEP 10 — LEAD STATE TRACKING: statuses a lead can be in
# WHY: without this the app has no memory between runs — the same
#      hottest leads keep resurfacing at the top even after
#      they've already been called. "new" is the only status that
#      counts as untouched; everything else means some action was
#      taken, and is what "Hide already contacted" filters out.
# ============================================================

LEAD_STATUSES = ("new", "contacted", "callback", "not_interested", "won")
LEAD_STATUS_LABELS = {
    "new": "New",
    "contacted": "Contacted",
    "callback": "Callback",
    "not_interested": "Not Interested",
    "won": "Won",
}


# ============================================================
# STEP 6 — AREA PICKER DIALOG
# WHY: this is the real implementation of the `picker` hook
#      osm_source.resolve_named_area() has expected since STEP 2.
#      It runs modally on the *main* thread; the background search
#      thread blocks on a threading.Event until this dialog closes
#      (see App._handle_pick_area below), which is how a Tk dialog
#      can be driven from a callback invoked off the network thread.
# ============================================================

class _AreaPickerDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, candidates: list[AreaCandidate]):
        super().__init__(parent)
        self.title("Multiple matches found")
        self.resizable(False, False)
        self.choice: Optional[AreaCandidate] = None
        self._candidates = candidates

        ttk.Label(self, text="Several places match that name — pick one:").pack(
            padx=12, pady=(12, 6)
        )

        self.listbox = tk.Listbox(self, width=64, height=min(10, len(candidates)))
        for candidate in candidates:
            self.listbox.insert("end", str(candidate))
        self.listbox.selection_set(0)
        self.listbox.pack(padx=12, pady=6, fill="both", expand=True)
        self.listbox.bind("<Double-Button-1>", lambda event: self._on_ok())

        button_row = ttk.Frame(self)
        button_row.pack(pady=(0, 12))
        ttk.Button(button_row, text="OK", command=self._on_ok).pack(side="left", padx=4)
        ttk.Button(button_row, text="Cancel", command=self._on_cancel).pack(side="left", padx=4)

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.transient(parent)
        self.grab_set()

    def _on_ok(self) -> None:
        selection = self.listbox.curselection()
        if selection:
            self.choice = self._candidates[selection[0]]
        self.destroy()

    def _on_cancel(self) -> None:
        self.choice = None
        self.destroy()


# ============================================================
# STEP 8 — ABOUT BOX
# WHY: the OSM/ODbL license requires attribution wherever this
#      data is displayed, and honesty about the app's limits (OSM's
#      uneven coverage, never judging a Facebook page's activity)
#      belongs somewhere a user will actually see it — not just
#      buried in the README.
# ============================================================

def _make_link_label(parent: tk.Widget, text: str, url: str) -> tk.Label:
    label = tk.Label(parent, text=text, fg="blue", cursor="hand2", wraplength=380, justify="left")
    label.bind("<Button-1>", lambda event: webbrowser.open(url))
    return label


class _AboutDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk):
        super().__init__(parent)
        self.title("About Local Lead Finder")
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Local Lead Finder", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Finds businesses with weak or missing web presence, so you can "
                 "pitch them web/marketing work.",
            wraplength=380, justify="left",
        ).pack(anchor="w", pady=(2, 12))

        ttk.Label(
            frame,
            text="Business data © OpenStreetMap contributors, licensed under the "
                 "Open Database License (ODbL):",
            wraplength=380, justify="left",
        ).pack(anchor="w")
        _make_link_label(
            frame, "https://www.openstreetmap.org/copyright", "https://www.openstreetmap.org/copyright"
        ).pack(anchor="w", pady=(0, 12))

        ttk.Label(
            frame,
            text='OSM coverage is uneven. A "no website" result is a hypothesis to '
                 "verify by hand, not a fact.",
            wraplength=380, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        ttk.Label(
            frame,
            text="This app never determines whether a Facebook page is active or "
                 "abandoned — that judgment call is yours, made by clicking the link.",
            wraplength=380, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        ttk.Button(frame, text="OK", command=self.destroy).pack(anchor="e")

        self.transient(parent)
        self.grab_set()


# ============================================================
# STEP 6 — MAIN APPLICATION WINDOW
# ============================================================

class App(ttk.Frame):
    def __init__(self, root: tk.Tk, cache: Cache):
        super().__init__(root)
        self.root = root
        self.cache = cache
        self.pack(fill="both", expand=True)

        # osm_key -> (Business, ScoreResult), so a Treeview selection can
        # look up everything the detail panel needs to show.
        self.rows: dict[str, tuple[Business, ScoreResult]] = {}
        self._sort_reverse: dict[str, bool] = {}
        self._selected_osm_key: Optional[str] = None

        # Loaded once at startup; every later read/write goes through this
        # in-memory copy, kept in sync with cache.py's `leads` table by
        # _on_save_lead_clicked() so the DB is never re-queried per row.
        self.lead_info: dict[str, dict] = self.cache.get_all_leads()
        self.hide_contacted_var = tk.BooleanVar(value=False)

        self.result_queue: "queue.Queue" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()

        self._build_menu_bar()
        self._build_top_bar()
        self._build_main_area()
        self._build_export_bar()
        self._build_status_bar()

        self.root.after(100, self._poll_queue)

    # -- layout -------------------------------------------------

    def _build_menu_bar(self) -> None:
        menu_bar = tk.Menu(self.root)
        help_menu = tk.Menu(menu_bar, tearoff=0)
        help_menu.add_command(label="About Local Lead Finder", command=lambda: _AboutDialog(self.root))
        menu_bar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menu_bar)

    def _build_top_bar(self) -> None:
        bar = ttk.Frame(self, padding=8)
        bar.grid(row=0, column=0, sticky="ew")
        self.grid_columnconfigure(0, weight=1)

        self.area_mode_var = tk.StringVar(value="named")
        ttk.Radiobutton(
            bar, text="Named area", variable=self.area_mode_var, value="named",
            command=self._on_area_mode_changed,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            bar, text="ZIP / postal code", variable=self.area_mode_var, value="zip",
            command=self._on_area_mode_changed,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Radiobutton(
            bar, text="Coordinates + radius", variable=self.area_mode_var, value="radius",
            command=self._on_area_mode_changed,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        # -- Mode A widgets: a place-name entry, plus an optional state
        #    filter that turns an ambiguous name ("Brandon" exists in a
        #    dozen states) into a single match with no picker at all --
        #    see osm_source.find_named_area_candidates()'s state_iso param.
        self.named_frame = ttk.Frame(bar)
        ttk.Label(self.named_frame, text="Place name:").pack(side="left")
        self.area_name_var = tk.StringVar()
        ttk.Entry(self.named_frame, textvariable=self.area_name_var, width=30).pack(
            side="left", padx=(4, 0)
        )
        ttk.Label(self.named_frame, text="State:").pack(side="left", padx=(12, 0))
        self.state_var = tk.StringVar(value=ANY_STATE_LABEL)
        ttk.Combobox(
            self.named_frame, textvariable=self.state_var, state="readonly", width=18,
            values=[ANY_STATE_LABEL] + [name for name, _ in US_STATES],
        ).pack(side="left", padx=(4, 0))

        # -- Mode "zip" widgets: a ZIP/postal code entry (radius slider is
        #    shared with Mode B via radius_only_frame below) --
        self.zip_frame = ttk.Frame(bar)
        ttk.Label(self.zip_frame, text="ZIP / postal code:").pack(side="left")
        self.zip_var = tk.StringVar()
        ttk.Entry(self.zip_frame, textvariable=self.zip_var, width=12).pack(side="left", padx=(4, 0))

        # -- Mode B widgets: lat/lon entries only --
        self.radius_frame = ttk.Frame(bar)
        ttk.Label(self.radius_frame, text="Lat:").pack(side="left")
        self.lat_var = tk.StringVar()
        ttk.Entry(self.radius_frame, textvariable=self.lat_var, width=10).pack(
            side="left", padx=(4, 8)
        )
        ttk.Label(self.radius_frame, text="Lon:").pack(side="left")
        self.lon_var = tk.StringVar()
        ttk.Entry(self.radius_frame, textvariable=self.lon_var, width=10).pack(
            side="left", padx=(4, 0)
        )

        # -- the miles radius slider, shared by "zip" and "radius" modes --
        self.radius_only_frame = ttk.Frame(bar)
        ttk.Label(self.radius_only_frame, text="Radius (mi):").pack(side="left")
        self.radius_var = tk.DoubleVar(value=5.0)
        self.radius_label_var = tk.StringVar(value="5")
        ttk.Scale(
            self.radius_only_frame, from_=1, to=25, orient="horizontal", variable=self.radius_var,
            command=self._on_radius_changed, length=140,
        ).pack(side="left", padx=(4, 4))
        ttk.Label(self.radius_only_frame, textvariable=self.radius_label_var, width=3).pack(side="left")

        self.named_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # -- category toggles --
        category_frame = ttk.Frame(bar)
        category_frame.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(category_frame, text="Categories:").pack(side="left")
        self.category_vars: dict[str, tk.BooleanVar] = {}
        for key in ALL_CATEGORY_KEYS:
            var = tk.BooleanVar(value=True)
            self.category_vars[key] = var
            ttk.Checkbutton(category_frame, text=key, variable=var).pack(side="left", padx=4)

        # -- action buttons --
        button_frame = ttk.Frame(bar)
        button_frame.grid(row=0, column=4, rowspan=4, sticky="e", padx=(20, 0))
        bar.grid_columnconfigure(4, weight=1)
        self.search_button = ttk.Button(button_frame, text="Search", command=self._on_search_clicked)
        self.search_button.pack(side="left", padx=4)
        self.cancel_button = ttk.Button(
            button_frame, text="Cancel", command=self._on_cancel_clicked, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=4)

    def _build_main_area(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        main = ttk.Frame(self)
        main.grid(row=1, column=0, sticky="nsew")
        main.grid_columnconfigure(0, weight=3)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        # -- results table --
        table_frame = ttk.Frame(main)
        table_frame.grid(row=0, column=0, sticky="nsew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(table_frame, columns=COLUMNS, show="headings")
        for col in COLUMNS:
            self.tree.heading(col, text=COLUMN_HEADINGS[col], command=lambda c=col: self._sort_by(c))
            width = 55 if col == "new" else 70 if col == "score" else 130 if col in ("tier", "phone", "status", "edited") else 200
            self.tree.column(col, width=width, anchor="w")
        for tier, color in TIER_COLORS.items():
            self.tree.tag_configure(tier, background=color)
        self.tree.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.tree.bind("<<TreeviewSelect>>", self._on_row_selected)

        # -- detail panel --
        detail = ttk.LabelFrame(main, text="Details", padding=8)
        detail.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        detail.grid_columnconfigure(1, weight=1)

        self.detail_name_var = tk.StringVar(value="—")
        self.detail_category_var = tk.StringVar(value="—")
        self.detail_phone_var = tk.StringVar(value="—")
        self.detail_address_var = tk.StringVar(value="—")
        self.detail_hours_var = tk.StringVar(value="—")

        ttk.Label(detail, text="Name:", font=("", 10, "bold")).grid(row=0, column=0, sticky="nw")
        ttk.Label(detail, textvariable=self.detail_name_var, wraplength=220).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(detail, text="Category:").grid(row=1, column=0, sticky="nw", pady=(6, 0))
        ttk.Label(detail, textvariable=self.detail_category_var).grid(
            row=1, column=1, sticky="w", pady=(6, 0)
        )
        ttk.Label(detail, text="Phone:").grid(row=2, column=0, sticky="nw", pady=(6, 0))
        ttk.Label(detail, textvariable=self.detail_phone_var).grid(
            row=2, column=1, sticky="w", pady=(6, 0)
        )
        ttk.Label(detail, text="Address:").grid(row=3, column=0, sticky="nw", pady=(6, 0))
        ttk.Label(detail, textvariable=self.detail_address_var, wraplength=220).grid(
            row=3, column=1, sticky="w", pady=(6, 0)
        )
        ttk.Label(detail, text="Hours:").grid(row=4, column=0, sticky="nw", pady=(6, 0))
        ttk.Label(detail, textvariable=self.detail_hours_var, wraplength=220).grid(
            row=4, column=1, sticky="w", pady=(6, 0)
        )

        ttk.Label(detail, text="Website:").grid(row=5, column=0, sticky="nw", pady=(6, 0))
        self.detail_website_link = ttk.Label(detail, text="—", wraplength=220)
        self.detail_website_link.grid(row=5, column=1, sticky="w", pady=(6, 0))

        ttk.Label(detail, text="Facebook:").grid(row=6, column=0, sticky="nw", pady=(6, 0))
        self.detail_facebook_link = ttk.Label(detail, text="—", wraplength=220)
        self.detail_facebook_link.grid(row=6, column=1, sticky="w", pady=(6, 0))

        ttk.Label(detail, text="Instagram:").grid(row=7, column=0, sticky="nw", pady=(6, 0))
        self.detail_instagram_link = ttk.Label(detail, text="—", wraplength=220)
        self.detail_instagram_link.grid(row=7, column=1, sticky="w", pady=(6, 0))

        ttk.Label(detail, text="Why this tier:").grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.detail_reasons_text = tk.Text(detail, height=10, width=30, wrap="word", state="disabled")
        self.detail_reasons_text.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
        detail.grid_rowconfigure(9, weight=1)

        # -- STEP 10: lead status tracking --
        ttk.Separator(detail, orient="horizontal").grid(
            row=10, column=0, columnspan=2, sticky="ew", pady=(10, 6)
        )
        ttk.Label(detail, text="Lead status:").grid(row=11, column=0, sticky="w")
        self.detail_status_var = tk.StringVar(value=LEAD_STATUS_LABELS["new"])
        status_combo = ttk.Combobox(
            detail, textvariable=self.detail_status_var, state="readonly",
            values=[LEAD_STATUS_LABELS[s] for s in LEAD_STATUSES], width=16,
        )
        status_combo.grid(row=11, column=1, sticky="w")

        ttk.Label(detail, text="Notes:").grid(row=12, column=0, sticky="nw", pady=(6, 0))
        self.detail_notes_text = tk.Text(detail, height=3, width=24, wrap="word")
        self.detail_notes_text.grid(row=12, column=1, sticky="ew", pady=(6, 0))

        self.save_lead_button = ttk.Button(
            detail, text="Save Lead Info", command=self._on_save_lead_clicked, state="disabled"
        )
        self.save_lead_button.grid(row=13, column=0, columnspan=2, sticky="e", pady=(6, 0))

    def _build_export_bar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 4))
        bar.grid(row=2, column=0, sticky="ew")
        ttk.Button(bar, text="Export CSV", command=self._on_export_csv_clicked).pack(side="left")
        ttk.Button(
            bar, text="Copy as Call Sheet", command=self._on_copy_call_sheet_clicked
        ).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(
            bar, text="Hide already contacted", variable=self.hide_contacted_var,
            command=self._refresh_table_visibility,
        ).pack(side="left", padx=(20, 0))

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 4))
        bar.grid(row=3, column=0, sticky="ew")

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)

        self.cache_hit_var = tk.StringVar(value="Cache hits: 0")
        ttk.Label(bar, textvariable=self.cache_hit_var, anchor="e").pack(side="right")
        self._cache_hits = 0

    # -- top-bar interaction -------------------------------------

    def _on_area_mode_changed(self) -> None:
        mode = self.area_mode_var.get()

        self.named_frame.grid_remove()
        self.zip_frame.grid_remove()
        self.radius_frame.grid_remove()
        self.radius_only_frame.grid_remove()

        if mode == "named":
            self.named_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        elif mode == "zip":
            self.zip_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
            self.radius_only_frame.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        else:  # "radius"
            self.radius_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
            self.radius_only_frame.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

    def _on_radius_changed(self, value: str) -> None:
        self.radius_label_var.set(str(round(float(value))))

    # -- clickable links -------------------------------------------

    def _set_link(self, label: ttk.Label, url: Optional[str]) -> None:
        label.unbind("<Button-1>")
        if url:
            label.configure(text=url, foreground="blue", cursor="hand2")
            label.bind("<Button-1>", lambda event, u=url: webbrowser.open(u))
        else:
            label.configure(text="—", foreground="black", cursor="")

    # -- results table --------------------------------------------

    def _row_values(self, business: Business, result: ScoreResult) -> tuple:
        web_or_fb = business.website or business.facebook or business.instagram or "—"
        status = self._lead_status(business.osm_key)
        edited = business.osm_timestamp[:10] if business.osm_timestamp else "—"
        return (
            result.score,
            result.tier,
            "★ New" if result.is_new else "—",
            LEAD_STATUS_LABELS[status],
            business.name,
            f"{business.category_key}={business.category_value}",
            business.phone or "—",
            web_or_fb,
            business.address or "—",
            edited,
        )

    def _lead_status(self, osm_key: str) -> str:
        return self.lead_info.get(osm_key, {}).get("status", "new")

    def _insert_or_update_row(self, business: Business, result: ScoreResult) -> None:
        """Update the data model, then reflect it in the tree — but only insert
        a tree row if it isn't currently hidden by the "Hide already
        contacted" filter."""
        osm_key = business.osm_key
        self.rows[osm_key] = (business, result)

        hidden = self.hide_contacted_var.get() and self._lead_status(osm_key) != "new"
        if hidden:
            if self.tree.exists(osm_key):
                self.tree.delete(osm_key)
            return

        values = self._row_values(business, result)
        if self.tree.exists(osm_key):
            self.tree.item(osm_key, values=values, tags=(result.tier,))
        else:
            self.tree.insert("", "end", iid=osm_key, values=values, tags=(result.tier,))

    def _refresh_table_visibility(self) -> None:
        """Re-derive which rows the tree shows from self.rows + the current
        filter state — called when the filter checkbox is toggled, or after
        a lead's status changes."""
        for business, result in list(self.rows.values()):
            self._insert_or_update_row(business, result)

    def _sort_by(self, column: str) -> None:
        reverse = self._sort_reverse.get(column, False)
        items = list(self.tree.get_children(""))

        def sort_key(iid: str):
            value = self.tree.set(iid, column)
            if column == "score":
                try:
                    return float(value)
                except ValueError:
                    return 0.0
            return value.lower()

        items.sort(key=sort_key, reverse=reverse)
        for index, iid in enumerate(items):
            self.tree.move(iid, "", index)
        self._sort_reverse[column] = not reverse

    def _on_row_selected(self, event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        business, result = self.rows[selection[0]]
        self._show_detail(business, result)

    def _show_detail(self, business: Business, result: ScoreResult) -> None:
        self._selected_osm_key = business.osm_key
        lead = self.lead_info.get(business.osm_key, {})
        self.detail_status_var.set(LEAD_STATUS_LABELS[lead.get("status", "new")])
        self.detail_notes_text.delete("1.0", "end")
        self.detail_notes_text.insert("end", lead.get("notes", ""))
        self.save_lead_button.configure(state="normal")

        self.detail_name_var.set(business.name)
        self.detail_category_var.set(f"{business.category_key} = {business.category_value}")
        self.detail_phone_var.set(business.phone or "—")
        self.detail_address_var.set(business.address or "—")
        self.detail_hours_var.set(business.opening_hours or "—")
        self._set_link(self.detail_website_link, business.website)
        self._set_link(self.detail_facebook_link, business.facebook)
        self._set_link(self.detail_instagram_link, business.instagram)

        self.detail_reasons_text.configure(state="normal")
        self.detail_reasons_text.delete("1.0", "end")
        self.detail_reasons_text.insert("end", f"{result.tier}  (score {result.score})\n\n")
        for reason in result.reasons:
            self.detail_reasons_text.insert("end", f"• {reason}\n")
        if result.flags:
            self.detail_reasons_text.insert("end", "\nNotes (not a judgment, just a signal):\n")
            for flag in result.flags:
                self.detail_reasons_text.insert("end", f"• {flag}\n")
        self.detail_reasons_text.configure(state="disabled")

    def _on_save_lead_clicked(self) -> None:
        if self._selected_osm_key is None:
            return
        label_to_status = {label: status for status, label in LEAD_STATUS_LABELS.items()}
        status = label_to_status[self.detail_status_var.get()]
        notes = self.detail_notes_text.get("1.0", "end").rstrip("\n")

        self.cache.set_lead(self._selected_osm_key, status, notes)
        self.lead_info[self._selected_osm_key] = {
            "status": status, "notes": notes, "last_contacted": None,
        }
        self._refresh_table_visibility()
        self.status_var.set(f"Saved lead status: {LEAD_STATUS_LABELS[status]}.")

    # -- export -------------------------------------------------

    def _ordered_rows(self) -> list[tuple[Business, ScoreResult]]:
        """Rows in whatever order the table is currently sorted/displayed in."""
        return [self.rows[iid] for iid in self.tree.get_children("")]

    def _on_export_csv_clicked(self) -> None:
        rows = self._ordered_rows()
        if not rows:
            messagebox.showinfo("Local Lead Finder", "Run a search first — there's nothing to export yet.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile="leads.csv",
        )
        if not path:
            return

        count = export.write_csv(path, rows)
        self.status_var.set(f"Exported {count} leads to {path}")

    def _on_copy_call_sheet_clicked(self) -> None:
        rows = self._ordered_rows()
        if not rows:
            messagebox.showinfo("Local Lead Finder", "Run a search first — there's nothing to copy yet.")
            return

        call_sheet = export.build_call_sheet(rows, limit=CALL_SHEET_LIMIT)
        self.root.clipboard_clear()
        self.root.clipboard_append(call_sheet)
        self.root.update()  # required for the clipboard to actually retain the text
        self.status_var.set(f"Copied top {CALL_SHEET_LIMIT} callable leads to the clipboard.")

    # -- search lifecycle -------------------------------------------

    def _set_running(self, running: bool) -> None:
        self.search_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _update_status(self, message: str) -> None:
        self.status_var.set(message)
        if "loaded from cache" in message.lower():
            self._cache_hits += 1
            self.cache_hit_var.set(f"Cache hits: {self._cache_hits}")

    def _on_search_clicked(self) -> None:
        category_keys = [key for key, var in self.category_vars.items() if var.get()]
        if not category_keys:
            messagebox.showerror("Local Lead Finder", "Select at least one category.")
            return

        mode = self.area_mode_var.get()
        if mode == "named":
            area_name = self.area_name_var.get().strip()
            if not area_name:
                messagebox.showerror("Local Lead Finder", "Enter a place name.")
                return
            state_iso = STATE_NAME_TO_ISO.get(self.state_var.get())
            search_params = {"mode": "named", "area_name": area_name, "state_iso": state_iso}
        elif mode == "zip":
            zip_code = self.zip_var.get().strip()
            if not zip_code:
                messagebox.showerror("Local Lead Finder", "Enter a ZIP / postal code.")
                return
            search_params = {"mode": "zip", "zip_code": zip_code, "radius": self.radius_var.get()}
        else:  # "radius"
            try:
                lat = float(self.lat_var.get())
                lon = float(self.lon_var.get())
            except ValueError:
                messagebox.showerror("Local Lead Finder", "Latitude and longitude must be numbers.")
                return
            search_params = {"mode": "radius", "lat": lat, "lon": lon, "radius": self.radius_var.get()}

        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self._selected_osm_key = None
        self.save_lead_button.configure(state="disabled")
        self._cache_hits = 0
        self.cache_hit_var.set("Cache hits: 0")
        self.cancel_event.clear()
        self._set_running(True)

        self.worker_thread = threading.Thread(
            target=self._run_search, args=(search_params, category_keys), daemon=True
        )
        self.worker_thread.start()

    def _on_cancel_clicked(self) -> None:
        self.cancel_event.set()
        self.status_var.set("Cancelling...")

    # -- background thread (NEVER touch widgets directly from here) --

    def _status_cb(self, message: str) -> None:
        self.result_queue.put(("status", message))

    def _gui_picker(self, candidates: list[AreaCandidate]) -> Optional[AreaCandidate]:
        """Runs on the background thread; blocks it until the main thread's
        _handle_pick_area() has shown the dialog and recorded a choice."""
        result_holder: dict = {}
        done_event = threading.Event()
        self.result_queue.put(("pick_area", candidates, result_holder, done_event))
        done_event.wait()
        return result_holder.get("choice")

    def _run_search(self, params: dict, category_keys: list[str]) -> None:
        try:
            if params["mode"] == "named":
                area = osm_source.resolve_named_area(
                    params["area_name"], self.cache, picker=self._gui_picker,
                    status_cb=self._status_cb, state_iso=params.get("state_iso"),
                )
                if area is None:
                    self.result_queue.put(("error", f'No match found for "{params["area_name"]}".'))
                    return
            elif params["mode"] == "zip":
                self._status_cb(f"Looking up ZIP/postal code {params['zip_code']}...")
                center = osm_source.geocode_postal_code(params["zip_code"], self.cache)
                if center is None:
                    self.result_queue.put(
                        ("error", f"Couldn't find ZIP/postal code \"{params['zip_code']}\".")
                    )
                    return
                lat, lon = center
                label = f"{params['radius']:g} mi around ZIP {params['zip_code']}"
                area = osm_source.make_radius_area(lat, lon, params["radius"], label=label)
            else:  # "radius"
                area = osm_source.make_radius_area(params["lat"], params["lon"], params["radius"])

            if self.cancel_event.is_set():
                self.result_queue.put(("cancelled", None))
                return

            businesses = osm_source.search_businesses(
                area, category_keys, self.cache, status_cb=self._status_cb
            )

            if self.cancel_event.is_set():
                self.result_queue.put(("cancelled", None))
                return

            self._status_cb(f"{len(businesses)} businesses found. Checking websites...")

            # Chain/franchise detection runs once, up front, over the whole
            # batch — a name's frequency can't be known from one Business in
            # isolation. The resulting reason (or None) is threaded into
            # every score_business() call below, including the ones
            # enrichment triggers later, so a chain doesn't un-demote itself
            # the moment its website finishes checking.
            name_frequency_chains = scoring.detect_name_frequency_chains(businesses)
            chain_reasons = {
                business.osm_key: scoring.chain_reason_for(business, name_frequency_chains)
                for business in businesses
            }

            # Diff mode (STEP 15): "this exact search" is identified by the
            # resolved area's label plus which categories were searched, so
            # re-running the same city with the same category toggles diffs
            # against last time, but broadening the categories doesn't
            # falsely flag everything the wider search adds as "new".
            snapshot_key = f"{area.label}|{','.join(sorted(category_keys))}"
            previous_snapshot = self.cache.get_snapshot(snapshot_key)
            current_keys = {business.osm_key for business in businesses}
            self.cache.set_snapshot(snapshot_key, current_keys)
            is_new_map = {
                business.osm_key: previous_snapshot is not None and business.osm_key not in previous_snapshot
                for business in businesses
            }
            if previous_snapshot is not None:
                new_count = sum(is_new_map.values())
                self._status_cb(f"{new_count} business(es) new since your last search here.")

            initial_rows = [
                (
                    business,
                    scoring.score_business(
                        business, None, chain_reasons[business.osm_key],
                        is_new_since_last_run=is_new_map[business.osm_key],
                    ),
                )
                for business in businesses
            ]
            self.result_queue.put(("initial_results", initial_rows))

            if self.cancel_event.is_set():
                self.result_queue.put(("cancelled", None))
                return

            # Pre-call DNS verification (STEP 13) only concerns businesses
            # with no website tag at all, so it can never disagree with
            # enrichment (which only ever looks at businesses that DO have
            # one) — the two update disjoint sets of rows.
            precall_hits = enrich.precall_check_businesses(
                businesses, self.cache, status_cb=self._status_cb
            )
            for business in businesses:
                hits = precall_hits.get(business.osm_key)
                if hits:
                    result = scoring.score_business(
                        business, None, chain_reasons[business.osm_key], hits,
                        is_new_since_last_run=is_new_map[business.osm_key],
                    )
                    self.result_queue.put(("row_update", business, result))

            if self.cancel_event.is_set():
                self.result_queue.put(("cancelled", None))
                return

            def on_enriched(business: Business, enrichment) -> None:
                result = scoring.score_business(
                    business, enrichment, chain_reasons[business.osm_key],
                    precall_hits.get(business.osm_key),
                    is_new_since_last_run=is_new_map[business.osm_key],
                )
                self.result_queue.put(("row_update", business, result))

            enrich.enrich_businesses(
                businesses, self.cache, status_cb=self._status_cb, on_result=on_enriched
            )

            self.result_queue.put(("search_complete", None))
        except Exception as exc:  # noqa: BLE001 - surface any failure to the status bar
            self.result_queue.put(("error", str(exc)))

    # -- main-thread queue polling ------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                message = self.result_queue.get_nowait()
                self._handle_message(message)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_message(self, message: tuple) -> None:
        kind = message[0]

        if kind == "status":
            self._update_status(message[1])

        elif kind == "pick_area":
            _, candidates, result_holder, done_event = message
            dialog = _AreaPickerDialog(self.root, candidates)
            self.root.wait_window(dialog)
            result_holder["choice"] = dialog.choice
            done_event.set()

        elif kind == "initial_results":
            _, rows = message
            for business, result in rows:
                self._insert_or_update_row(business, result)

        elif kind == "row_update":
            _, business, result = message
            self._insert_or_update_row(business, result)
            selection = self.tree.selection()
            if selection and selection[0] == business.osm_key:
                self._show_detail(business, result)

        elif kind == "error":
            self._set_running(False)
            self.status_var.set(f"Error: {message[1]}")
            messagebox.showerror("Local Lead Finder", message[1])

        elif kind == "cancelled":
            self._set_running(False)
            self.status_var.set("Cancelled.")

        elif kind == "search_complete":
            self._set_running(False)
            self.status_var.set(f"Done. {len(self.rows)} leads found.")


# ============================================================
# STEP 6 — entry point used by main.py
# ============================================================

def run_app(cache: Optional[Cache] = None) -> None:
    owns_cache = cache is None
    cache = cache or Cache()
    root = tk.Tk()
    root.title("Local Lead Finder")
    root.geometry(WINDOW_SIZE)
    App(root, cache)
    try:
        root.mainloop()
    finally:
        if owns_cache:
            cache.close()


if __name__ == "__main__":
    run_app()
