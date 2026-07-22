"""Render the ``fot`` CLI's own output to vector SVG for the blog.

The CLI draws with ``rich``, so instead of screen-capturing a terminal we swap the
module-level console for a *recording* one, run the real commands through the real
Typer app, and export ``save_svg``. The result is true vector text -- crisp at any
zoom, with the exact colours the terminal shows.

Usage (from the repo root, with the repo venv):

    .venv/Scripts/python.exe blog/assets/capture_terminal.py

Environment expected: SIGNOZ_URL / SIGNOZ_EMAIL / SIGNOZ_PASSWORD (or SIGNOZ_JWT).

Window choice matters. The live Gemini batch is 2026-07-22 04:51-05:58 UTC; an
earlier stubbed batch sits at 2026-07-21 19:20 UTC. ``--since 6h`` covers only the
live batch, which is what the headline numbers must come from. The compare shot is
the exception -- the control service has no live traces at all, so it uses a wider
window (see COMPARE_SINCE) and says so in its title.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent
REPO_ROOT = ASSETS.parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Only the live batch (04:51-05:58 UTC on 2026-07-22).
LIVE_SINCE = os.environ.get("FOT_CAPTURE_SINCE", "6h")
# Wide enough to also include the control service's batch (2026-07-21 19:20 UTC).
COMPARE_SINCE = os.environ.get("FOT_CAPTURE_COMPARE_SINCE", "13h")

WIDTH = int(os.environ.get("FOT_CAPTURE_WIDTH", "100"))

os.environ.setdefault("SIGNOZ_URL", "http://localhost:8080")
# Force rich to believe it is on a colour terminal even though stdout is a pipe.
os.environ["FORCE_COLOR"] = "1"
os.environ["TERM"] = "xterm-256color"
os.environ["COLUMNS"] = str(WIDTH)

from rich.console import Console  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

import fot.render as render  # noqa: E402


def recording_console() -> Console:
    """A console that renders to a buffer and remembers every segment."""
    return Console(
        file=io.StringIO(),
        record=True,
        width=WIDTH,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
        highlight=False,
        soft_wrap=False,
    )


def capture(argv: list[str], out: Path, title: str) -> None:
    """Run ``fot <argv>`` through the real CLI and save its render as SVG."""
    rec = recording_console()
    original = render.console
    render.console = rec
    try:
        # Imported late so it picks up the patched console for status lines too.
        import fot.cli as cli

        cli.console = rec
        result = CliRunner().invoke(cli.app, argv, catch_exceptions=False)
    finally:
        render.console = original

    # clear=False is load-bearing: export_text() empties the record buffer by
    # default, which would leave save_svg() with nothing to draw.
    text = rec.export_text(clear=False)
    if result.exit_code != 0 or not text.strip():
        raise SystemExit(
            f"FAILED: fot {' '.join(argv)} -> exit {result.exit_code}\n"
            f"stdout: {result.stdout[:500]}\ncaptured: {text[:500]}"
        )

    rec.save_svg(str(out), title=title)
    # Echo to the real terminal so the operator can eyeball the numbers.
    print(f"\n=== {out.name} :: fot {' '.join(argv)} ===")
    print(text)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)

    capture(
        ["show", "cognition", "--since", LIVE_SINCE],
        ASSETS / "01-funnel-show.svg",
        f"fot show cognition --since {LIVE_SINCE}",
    )
    capture(
        ["counter-proof", "cognition", "--since", LIVE_SINCE],
        ASSETS / "02-counter-proof.svg",
        f"fot counter-proof cognition --since {LIVE_SINCE}",
    )
    capture(
        ["compare", "cognition", "control", "--since", COMPARE_SINCE],
        ASSETS / "03-compare.svg",
        f"fot compare cognition control --since {COMPARE_SINCE}",
    )
    capture(
        ["ls"],
        ASSETS / "09-funnels-ls.svg",
        "fot ls",
    )


if __name__ == "__main__":
    main()
