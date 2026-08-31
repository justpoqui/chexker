# ============================================================
# STEP 6 — application entry point
# WHY: main.py's only job is to start the GUI. All real wiring
#      (building the Cache, constructing the Tk window, starting
#      the event loop) lives in gui.run_app() so that gui.py stays
#      independently runnable and testable on its own.
# ============================================================

from gui import run_app


def main() -> None:
    run_app()


if __name__ == "__main__":
    main()
