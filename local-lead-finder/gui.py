# ============================================================
# STEP 1 — SCAFFOLD: Tkinter GUI module (placeholder)
# WHY: reserving the file now fixes the module boundary — all
#      Tkinter widget code lives here and nowhere else. The
#      real window (search bar, results table, detail panel,
#      status bar, background-thread wiring) is built in STEP 6,
#      after osm_source.py, scoring.py and cache.py exist for it
#      to call into.
# ============================================================

# TODO (STEP 6): class App(tk.Frame) with the full layout —
# top search bar, ttk.Treeview results table, right-hand detail
# panel, bottom status bar — plus a queue.Queue bridge so
# background network threads never touch Tk widgets directly.
