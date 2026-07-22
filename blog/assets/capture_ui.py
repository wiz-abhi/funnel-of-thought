"""Capture the SigNoz UI screenshots that back the Funnel of Thought blog post.

Headless chromium, 1600px-wide viewport at deviceScaleFactor 2, so every PNG is a
retina-crisp 3200px-wide image. Each shot uses a viewport height chosen to frame
its page without dead space, and an explicit time range chosen so the chart is
never empty.

    python blog/assets/capture_ui.py

Time ranges. The live Gemini batch is 2026-07-22 04:51-05:58 UTC; an earlier
stubbed batch sits at 2026-07-21 19:20 UTC. ``FUNNEL_RANGE = 6h`` covers only the
live batch, which is where the headline 125 / 125 / 80 / 80 comes from. The
span-name shot deliberately uses 1d, because model-name fragmentation only shows
up once both model batches are in view.

Prerequisite for the dashboard and alert shots: ``fot gauges cognition`` must have
run recently. Funnel analytics are not metrics, so the dashboard panels and the
alert rule read the gauges that ``fot gauges`` re-emits; with no fresh gauge
points the panels are empty and the rule sits inactive.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ASSETS = Path(__file__).resolve().parent

BASE = os.environ.get("SIGNOZ_URL", "http://localhost:8080")
EMAIL = os.environ.get("SIGNOZ_EMAIL", "user.abhishek2004@gmail.com")
PASSWORD = os.environ.get("SIGNOZ_PASSWORD", "SigNoz@Warmup2026")

# playwright's bundled chromium; override if the cache lives elsewhere.
CHROMIUM = os.environ.get(
    "FOT_CHROMIUM",
    str(Path.home() / "AppData/Local/ms-playwright/chromium-1155/chrome-win/chrome.exe"),
)

COGNITION_FUNNEL_ID = os.environ.get("FOT_FUNNEL_ID", "019f8813-4152-7d1c-8fa3-80250b4817a8")
DASHBOARD_ID = os.environ.get("FOT_DASHBOARD_ID", "019f8819-3efb-73ec-b741-470840a667bc")
RULE_ID = os.environ.get("FOT_RULE_ID", "019f881b-0f83-7a95-8c7b-4c642cf02614")
# A trace where agent.validate fires BEFORE agent.tool -- a contract violation the
# waterfall makes visible at a glance.
OOO_TRACE = os.environ.get("FOT_TRACE_ID", "d5ba3cc315e495a16446aabd84470361")

FUNNEL_RANGE = "6h"  # live batch only
METRIC_RANGE = "30m"  # dense enough that the gauge series fills the plot
SPAN_RANGE = "1d"  # both model batches, so fragmentation is visible

# The support-chat bubble floats over the bottom-right of every page.
HIDE_CHROME_CSS = """
  #intercom-container, .intercom-lightweight-app, [class*='intercom'],
  [id*='launcher'], .ant-tooltip { display: none !important; }
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def login(page: Page) -> None:
    """SigNoz's two-step login: email, Next, then password."""
    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.fill("input#email", EMAIL)
    page.click("button:has-text('Next')")
    page.wait_for_selector("input[type=password]", timeout=15000)
    page.fill("input[type=password]", PASSWORD)
    page.click("button:has-text('Sign in with Password')")
    page.wait_for_url("**/home**", timeout=30000)
    page.wait_for_timeout(2000)
    log("logged in")


def settle(page: Page, ms: int = 6000) -> None:
    """Wait for network to go quiet, then for chart animations to finish."""
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(ms)
    # Dismiss any onboarding popover that grabbed the viewport.
    for sel in ("button:has-text('Okay')", "button:has-text('Got it')"):
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.click()
            page.wait_for_timeout(600)
    page.add_style_tag(content=HIDE_CHROME_CSS)
    # Park the cursor in the empty strip of the top bar. Anywhere over the left
    # rail expands the sidenav on hover and it covers a third of the shot;
    # anywhere over a chart bakes in a hover tooltip.
    page.mouse.move(1000, 8)
    page.wait_for_timeout(1500)


def shot(page: Page, out: Path, height: int | None = None) -> None:
    """Screenshot the viewport, optionally clipped to ``height`` CSS pixels."""
    clip = {"x": 0, "y": 0, "width": 1600, "height": height} if height else None
    page.screenshot(path=str(out), clip=clip)
    log(f"  wrote {out.name}")


def capture_funnel(page: Page) -> None:
    """04 -- the cognition funnel inside SigNoz's own Trace Funnels product."""
    # Height tuned so the sticky "4 steps - Valid traces found" footer lands on
    # the bottom edge and the empty "Traces with errors" panel stays below it.
    page.set_viewport_size({"width": 1600, "height": 1258})
    page.goto(
        f"{BASE}/traces/funnels/{COGNITION_FUNNEL_ID}?relativeTime={FUNNEL_RANGE}",
        wait_until="networkidle",
    )
    settle(page, 9000)
    body = page.inner_text("body")
    if "Conversion rate" not in body:
        raise SystemExit("funnel page has no conversion rate -- wrong time range?")
    shot(page, ASSETS / "04-signoz-funnel.png")


def capture_dashboard(page: Page) -> None:
    """05 -- the dashboard-as-code that charts the re-emitted funnel gauges."""
    page.set_viewport_size({"width": 1600, "height": 1230})
    page.goto(
        f"{BASE}/dashboard/{DASHBOARD_ID}?relativeTime={METRIC_RANGE}",
        wait_until="networkidle",
    )
    settle(page, 11000)
    shot(page, ASSETS / "05-dashboard.png", height=1195)


def capture_alert_list(page: Page) -> None:
    """06 -- the alert rule in FIRING state on the Alert Rules page."""
    page.set_viewport_size({"width": 1600, "height": 1000})
    page.goto(f"{BASE}/alerts", wait_until="networkidle")
    settle(page, 7000)
    body = page.inner_text("body")
    if "Firing" not in body:
        log("  WARNING: rule is not firing right now (run `fot gauges cognition --since 6h`)")
    shot(page, ASSETS / "06-alert-firing.png", height=310)


def capture_alert_threshold(page: Page) -> None:
    """10 -- the firing rule's own chart: 64% conversion under a 90% threshold."""
    page.set_viewport_size({"width": 1600, "height": 1000})
    page.goto(
        f"{BASE}/alerts/overview?ruleId={RULE_ID}&relativeTime={METRIC_RANGE}",
        wait_until="networkidle",
    )
    settle(page, 10000)
    shot(page, ASSETS / "10-alert-threshold.png", height=895)


def capture_trace(page: Page) -> None:
    """07 -- a single out-of-order trace: plan, validate, tool, respond."""
    page.set_viewport_size({"width": 1600, "height": 810})
    page.goto(f"{BASE}/trace/{OOO_TRACE}", wait_until="networkidle")
    settle(page, 9000)
    body = page.inner_text("body")
    for span in ("agent.plan", "agent.validate", "agent.tool", "agent.respond"):
        if span not in body:
            raise SystemExit(f"trace {OOO_TRACE} is missing {span}")
    shot(page, ASSETS / "07-trace-flamegraph.png", height=750)


def capture_span_names(page: Page) -> None:
    """08 -- OTel GenAI span-name fragmentation, counted in the Traces explorer.

    ``{operation} {model}`` means every model gets its own span name, so a naive
    ``GROUP BY name`` splits one logical LLM call across several rows.
    """
    page.set_viewport_size({"width": 1600, "height": 720})
    page.goto(f"{BASE}/traces-explorer?relativeTime={SPAN_RANGE}", wait_until="networkidle")
    settle(page, 5000)

    page.click("text=Table")
    page.wait_for_timeout(2500)

    # where-clause: only the GenAI chat spans
    page.query_selector_all(".cm-content")[0].click()
    page.keyboard.type("name LIKE 'chat%'")
    page.wait_for_timeout(1500)
    page.keyboard.press("Enter")
    page.wait_for_timeout(1500)

    # group by span name
    page.click("button:has-text('Group By'), div[role='button']:has-text('Group By')")
    page.wait_for_timeout(1200)
    page.query_selector(".ant-select-multiple").click()
    page.wait_for_timeout(800)
    page.keyboard.type("name")
    page.wait_for_timeout(2500)
    for opt in page.query_selector_all(".ant-select-item-option"):
        if opt.inner_text().strip().splitlines()[0].strip() == "name":
            opt.click()
            break
    else:
        raise SystemExit("could not find a 'name' group-by option")
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)
    page.click("button:has-text('Run Query')")
    settle(page, 8000)

    body = page.inner_text("body")
    if "chat gemini-3.1-flash-lite" not in body or "chat gemini-3.1-flash\n" not in body + "\n":
        log("  WARNING: expected two distinct chat span names in the result table")
    shot(page, ASSETS / "08-span-names.png")


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    if not Path(CHROMIUM).exists():
        sys.exit(f"chromium not found at {CHROMIUM}; set FOT_CHROMIUM")

    steps = [
        ("04 trace funnel", capture_funnel),
        ("05 dashboard", capture_dashboard),
        ("06 alert firing", capture_alert_list),
        ("10 alert threshold", capture_alert_threshold),
        ("07 trace waterfall", capture_trace),
        ("08 span names", capture_span_names),
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROMIUM)
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=2,
            color_scheme="dark",
        )
        page = ctx.new_page()
        login(page)
        for label, fn in steps:
            log(label)
            fn(page)
        browser.close()
    log("done")


if __name__ == "__main__":
    main()
