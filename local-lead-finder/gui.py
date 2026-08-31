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
from tkinter import messagebox, ttk
from typing import Optional

import enrich
import osm_source
import scoring
from cache import Cache
from osm_source import ALL_CATEGORY_KEYS, AreaCandidate, Business
from scoring import ScoreResult

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
}

COLUMNS = ("score", "tier", "name", "category", "phone", "web", "address")
COLUMN_HEADINGS = {
    "score": "Score",
    "tier": "Tier",
    "name": "Name",
    "category": "Category",
    "phone": "Phone",
    "web": "Website / FB",
    "address": "Address",
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
# STEP 6 — MAIN APPLICATION WINDOW
# ============================================================

class App(ttk.Frame):
    def __init__(self, root: tk.Tk, cache: Cache):
        super().__init__(root)
        self.root = root
        self.cache = cache
        self.pack(fill="both", expand=True)

        # osm_id -> (Business, ScoreResult), so a Treeview selection can
        # look up everything the detail panel needs to show.
        self.rows: dict[str, tuple[Business, ScoreResult]] = {}
        self._sort_reverse: dict[str, bool] = {}

        self.result_queue: "queue.Queue" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()

        self._build_top_bar()
        self._build_main_area()
        self._build_status_bar()

        self.root.after(100, self._poll_queue)

    # -- layout -------------------------------------------------

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
            bar, text="Coordinates + radius", variable=self.area_mode_var, value="radius",
            command=self._on_area_mode_changed,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))

        # -- Mode A widgets: a single place-name entry --
        self.named_frame = ttk.Frame(bar)
        ttk.Label(self.named_frame, text="Place name:").pack(side="left")
        self.area_name_var = tk.StringVar()
        ttk.Entry(self.named_frame, textvariable=self.area_name_var, width=30).pack(
            side="left", padx=(4, 0)
        )

        # -- Mode B widgets: lat/lon entries + a miles radius slider --
        self.radius_frame = ttk.Frame(bar)
        ttk.Label(self.radius_frame, text="Lat:").pack(side="left")
        self.lat_var = tk.StringVar()
        ttk.Entry(self.radius_frame, textvariable=self.lat_var, width=10).pack(
            side="left", padx=(4, 8)
        )
        ttk.Label(self.radius_frame, text="Lon:").pack(side="left")
        self.lon_var = tk.StringVar()
        ttk.Entry(self.radius_frame, textvariable=self.lon_var, width=10).pack(
            side="left", padx=(4, 8)
        )
        ttk.Label(self.radius_frame, text="Radius (mi):").pack(side="left")
        self.radius_var = tk.DoubleVar(value=5.0)
        self.radius_label_var = tk.StringVar(value="5")
        ttk.Scale(
            self.radius_frame, from_=1, to=25, orient="horizontal", variable=self.radius_var,
            command=self._on_radius_changed, length=140,
        ).pack(side="left", padx=(4, 4))
        ttk.Label(self.radius_frame, textvariable=self.radius_label_var, width=3).pack(side="left")

        self.named_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # -- category toggles --
        category_frame = ttk.Frame(bar)
        category_frame.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(category_frame, text="Categories:").pack(side="left")
        self.category_vars: dict[str, tk.BooleanVar] = {}
        for key in ALL_CATEGORY_KEYS:
            var = tk.BooleanVar(value=True)
            self.category_vars[key] = var
            ttk.Checkbutton(category_frame, text=key, variable=var).pack(side="left", padx=4)

        # -- action buttons --
        button_frame = ttk.Frame(bar)
        button_frame.grid(row=0, column=4, rowspan=3, sticky="e", padx=(20, 0))
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
            width = 70 if col == "score" else 130 if col in ("tier", "phone") else 200
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

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 4))
        bar.grid(row=2, column=0, sticky="ew")

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)

        self.cache_hit_var = tk.StringVar(value="Cache hits: 0")
        ttk.Label(bar, textvariable=self.cache_hit_var, anchor="e").pack(side="right")
        self._cache_hits = 0

    # -- top-bar interaction -------------------------------------

    def _on_area_mode_changed(self) -> None:
        if self.area_mode_var.get() == "named":
            self.radius_frame.grid_remove()
            self.named_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        else:
            self.named_frame.grid_remove()
            self.radius_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

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
        return (
            result.score,
            result.tier,
            business.name,
            f"{business.category_key}={business.category_value}",
            business.phone or "—",
            web_or_fb,
            business.address or "—",
        )

    def _insert_or_update_row(self, business: Business, result: ScoreResult) -> None:
        iid = str(business.osm_id)
        self.rows[iid] = (business, result)
        values = self._row_values(business, result)
        if self.tree.exists(iid):
            self.tree.item(iid, values=values, tags=(result.tier,))
        else:
            self.tree.insert("", "end", iid=iid, values=values, tags=(result.tier,))

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
        self.detail_reasons_text.configure(state="disabled")

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

        if self.area_mode_var.get() == "named":
            area_name = self.area_name_var.get().strip()
            if not area_name:
                messagebox.showerror("Local Lead Finder", "Enter a place name.")
                return
            search_params = {"mode": "named", "area_name": area_name}
        else:
            try:
                lat = float(self.lat_var.get())
                lon = float(self.lon_var.get())
            except ValueError:
                messagebox.showerror("Local Lead Finder", "Latitude and longitude must be numbers.")
                return
            search_params = {"mode": "radius", "lat": lat, "lon": lon, "radius": self.radius_var.get()}

        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
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
                    params["area_name"], self.cache, picker=self._gui_picker, status_cb=self._status_cb
                )
                if area is None:
                    self.result_queue.put(("error", f'No match found for "{params["area_name"]}".'))
                    return
            else:
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
            initial_rows = [(business, scoring.score_business(business, None)) for business in businesses]
            self.result_queue.put(("initial_results", initial_rows))

            if self.cancel_event.is_set():
                self.result_queue.put(("cancelled", None))
                return

            def on_enriched(business: Business, enrichment) -> None:
                result = scoring.score_business(business, enrichment)
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
            if selection and selection[0] == str(business.osm_id):
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
