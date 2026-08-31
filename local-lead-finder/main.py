# ============================================================
# STEP 1 — SCAFFOLD: application entry point
# WHY: a single, tiny entry point that just launches the GUI
#      keeps startup logic in one obvious place. Real wiring
#      (constructing the GUI, passing it a cache handle, etc.)
#      lands in a later step once gui.py and cache.py exist.
# ============================================================

def main() -> None:
    # STEP 6 will replace this with: build a Cache, build the
    # Tkinter root window, hand both to gui.App, call mainloop().
    print("Local Lead Finder — scaffold only. GUI not wired up yet.")


if __name__ == "__main__":
    main()
