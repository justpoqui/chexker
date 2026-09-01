# Building a standalone Local Lead Finder executable

Two ways to get a runnable binary instead of `python main.py`. Both use the
same checked-in `LocalLeadFinder.spec` (a [PyInstaller](https://pyinstaller.org/)
build config), so they always produce the same thing.

## Option A — download the prebuilt Windows .exe

Every push to this branch that touches `local-lead-finder/` triggers
[`.github/workflows/build-windows-exe.yml`](../.github/workflows/build-windows-exe.yml),
which builds `LocalLeadFinder.exe` on a real Windows runner (PyInstaller
can't cross-compile — a Windows build has to actually run on Windows) and
uploads it as a workflow artifact.

To get it:
1. Open this repository's **Actions** tab on GitHub.
2. Click the latest **Build Windows exe** run (or trigger one yourself with
   the "Run workflow" button — the workflow also runs on-demand).
3. Download the **LocalLeadFinder-windows-exe** artifact and unzip it.
4. Run `LocalLeadFinder.exe`. No Python install needed on the machine that
   runs it.

## Option B — build it yourself

Works on Windows, macOS, or Linux — PyInstaller bundles a native binary for
whichever OS you run it on (so building on macOS gives you a macOS app, not
a `.exe`).

```bash
cd local-lead-finder
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller
pyinstaller LocalLeadFinder.spec
```

The finished binary lands in `local-lead-finder/dist/` (`LocalLeadFinder.exe`
on Windows, `LocalLeadFinder` on macOS/Linux). `build/` and `dist/` are
gitignored — they're build output, not source.

## Where the packaged app stores its data

Running as a plain Python script, the SQLite cache/lead-tracking database
lives right next to `cache.py`, same as always. A packaged build is
different: PyInstaller's `--onefile` mode unpacks the whole app to a
temporary directory on every launch and deletes it on exit, so anything
written there — the cache, your lead statuses, everything — would vanish
the moment you closed the app. `cache.py` detects this (`sys.frozen`) and
instead stores the database in a real per-user data directory:

- Windows: `%APPDATA%\LocalLeadFinder\lead_cache.sqlite3`
- macOS/Linux: `~/LocalLeadFinder/lead_cache.sqlite3`

That's what makes lead statuses, cached Overpass results, and diff-mode
snapshots survive between runs of the packaged app.
