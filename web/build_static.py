"""Render the FastAPI template to the static bundle that ships to the Space.

`static-dist/index.html` is what the hosted demo actually serves. It used to be
produced by hand, which meant `templates/index.html` and the deployed page could
drift apart silently -- and a fix applied to one would simply not exist on the
other. This makes the dist a build artifact of the template rather than a second
copy of it.

    python web/build_static.py            # render, write, report
    python web/build_static.py --check    # exit 1 if the dist is out of date

The snapshot payload is the same `data/snapshot.json` the app serves in snapshot
mode, so the static page shows exactly what a reader would see with no SigNoz
behind it -- badge included, which is what keeps it honest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
SNAPSHOT = HERE / "data" / "snapshot.json"
OUT = HERE / "static-dist" / "index.html"


class _FakeRequest:
    """Jinja only ever needs `request` to exist; nothing in the page reads it.

    Starlette's real Request cannot be constructed outside an ASGI scope, and
    building one just to render a static page would be silly.
    """

    url = ""

    def url_for(self, *_a, **_kw) -> str:  # pragma: no cover - defensive
        return ""


def render() -> str:
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    # Force snapshot mode: the hosted page has no SigNoz behind it, and the
    # badge must say so rather than implying a live read.
    data["mode"] = "snapshot"
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("index.html").render(
        request=_FakeRequest(), d=data, d_json=json.dumps(data)
    )
    # The static host has no API. Leaving a relative /api/funnel link here gives
    # a 404 on the deployed page, so point at the committed data instead.
    return html.replace(
        '<a href="/api/funnel">/api/funnel</a>',
        '<a href="https://github.com/wiz-abhi/funnel-of-thought/blob/main/'
        'web/data/snapshot.json" target="_blank" rel="noopener">snapshot.json</a>',
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify the committed dist matches the template")
    args = ap.parse_args()

    html = render()
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""

    if args.check:
        if current.strip() == html.strip():
            print(f"{OUT.name} is up to date with the template")
            return 0
        print(f"{OUT.name} is STALE -- run: python web/build_static.py", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    verb = "unchanged" if current.strip() == html.strip() else "updated"
    print(f"{verb}: {OUT}  ({len(html):,} bytes)")
    for needle, label in [
        ("youtu.be/N9_sCORyT2E", "demo video link"),
        ("medium.com/@abhiiishek0101", "blog link"),
        ("41", "MCP tool count"),
        ("SNAPSHOT", "honest data badge"),
    ]:
        if needle not in html:
            print(f"  WARNING: {label} missing from the rendered page", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
