"""Streamlit dashboard for the CRE Underwriter (Phase 4).

This is the presentation layer only. It does not do any underwriting math or any
SQL itself — it calls the existing engine and database modules:

    - all metrics + the buy/pass decision come from engine.py
    - all storage goes through database.py (save_deal, load_deal, list_deals)

Keeping that boundary strict means the day we swap SQLite for a hosted database,
only database.py changes — this file never touches a connection.

Run it with:  streamlit run cre_underwriter/dashboard.py
"""

import base64
import re
from pathlib import Path

import pandas as pd
import streamlit as st

# Import the engine and storage layers. The try/except lets this run both under
# `streamlit run cre_underwriter/dashboard.py` (script folder on the path) and as
# part of the package.
try:
    from cre_underwriter import engine, database, rentcast, extraction, deal_memo
except ModuleNotFoundError:
    import engine
    import database
    import rentcast
    import extraction
    import deal_memo


# -----------------------------------------------------------------------------
# Brand assets (DealGauge logo + mark). See DESIGN.md. SVGs are embedded inline via
# HTML (st.image doesn't render raw SVG cleanly), and the favicon is passed to
# set_page_config as a data URI built from the gauge mark.
# -----------------------------------------------------------------------------
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


def _asset_text(name):
    """Read an SVG (or text) asset; return '' if it's missing so the app still runs."""
    try:
        return (ASSETS_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _favicon():
    """The page icon: the gauge mark as an SVG data URI, or a neutral fallback."""
    svg = _asset_text("dealgauge-mark.svg")
    if not svg:
        return ":bar_chart:"   # harmless fallback if the asset is missing
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


# st.set_page_config must be the first Streamlit call.
st.set_page_config(page_title="DealGauge", page_icon=_favicon(), layout="centered")

# Make sure the table exists before we read or write. This is idempotent.
database.initialize_database()


# -----------------------------------------------------------------------------
# Design system (DESIGN.md): palette, typography, cards, metric tiles, tables.
# Native theming lives in .streamlit/config.toml; this CSS layers the rest on top.
# Brass is the single accent and is used sparingly (the logo + one header hairline).
# -----------------------------------------------------------------------------
DESIGN_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
  --dg-navy:#12182B; --dg-navy2:#1A2238; --dg-brass:#C2A35E; --dg-cream:#ECE6D6;
  --dg-paper:#FBFAF7; --dg-ink:#1A2238; --dg-muted:#5B6275; --dg-hair:#E2DFD6;
  --dg-card:#FCFBF8; --dg-tint:#F1ECDE;
  --dg-green:#2E7D4F; --dg-red:#B23A3A; --dg-amber:#B8860B;
}

/* Base typography + background */
html, body, [class*="css"], .stApp,
[data-testid="stMarkdownContainer"], [data-testid="stWidgetLabel"] {
  font-family: 'Inter','Segoe UI', system-ui, -apple-system, sans-serif;
}
.stApp { background: var(--dg-paper); color: var(--dg-ink); }
[data-testid="stHeader"] { background: transparent; }
footer { visibility: hidden; }

/* Document-width content, generous top space */
.block-container, [data-testid="stMainBlockContainer"] {
  max-width: 880px; padding-top: 2.2rem; padding-bottom: 4rem;
}

/* Headings */
h1, h2, h3, h4, h5 { color: var(--dg-navy); letter-spacing: -0.01em; }
h1 { font-weight: 700; }
h2, h3, h4 { font-weight: 600; }

/* Brand header: logo with clear space + a single precious brass hairline */
.dg-header { padding: 4px 0 18px; border-bottom: 2px solid var(--dg-brass); margin: 0 0 8px; }
.dg-logo svg { height: 60px; width: auto; display: block; }

/* Section card titles (inside bordered cards) */
.dg-card-title {
  font-size: 1.02rem; font-weight: 600; color: var(--dg-navy);
  margin: 2px 0 12px; padding: 0;
}

/* Bordered containers -> design cards (sections, results panels, memo) */
[data-testid="stVerticalBlockBorderWrapper"] {
  border: 1px solid var(--dg-hair) !important; border-radius: 12px;
  background: var(--dg-card); padding: 18px 22px; margin-bottom: 16px;
}

/* Metric tiles: small uppercase label over a tabular value. The value is sized to
   fit multi-million-dollar figures in a 4-across tile row (e.g. the Financing section)
   without truncating — tabular figures keep columns aligned. */
[data-testid="stMetric"] {
  border: 1px solid var(--dg-hair); border-radius: 10px;
  background: var(--dg-card); padding: 12px 14px;
}
[data-testid="stMetricLabel"] p {
  text-transform: uppercase; font-size: 0.68rem; letter-spacing: 0.06em;
  font-weight: 600; color: var(--dg-muted);
}
[data-testid="stMetricValue"] {
  color: var(--dg-navy); font-weight: 700; font-variant-numeric: tabular-nums;
  font-size: 1.35rem; line-height: 1.2; white-space: nowrap;
}
[data-testid="stMetricValue"] > div { overflow: visible; }

/* Hairline tables (markdown tables: rent roll / pro forma / expenses / memo) */
[data-testid="stMarkdownContainer"] table {
  border-collapse: collapse; width: 100%; margin: 4px 0 10px;
  font-variant-numeric: tabular-nums;
}
[data-testid="stMarkdownContainer"] thead th {
  background: var(--dg-tint); color: var(--dg-navy2); text-align: left;
  text-transform: uppercase; font-size: 0.70rem; letter-spacing: 0.05em;
  font-weight: 600; padding: 9px 12px; border: none; border-bottom: 1px solid var(--dg-hair);
}
[data-testid="stMarkdownContainer"] tbody td {
  padding: 8px 12px; border: none; border-bottom: 1px solid #ECE8DD; font-size: 0.92rem;
}
[data-testid="stMarkdownContainer"] tbody tr:last-child td { border-bottom: none; }

/* Buttons: primary = Ink Navy with cream text; secondary = navy outline */
.stButton button, .stDownloadButton button { border-radius: 8px; font-weight: 600; }
.stButton button[kind="primary"], [data-testid="stBaseButton-primary"] {
  background: var(--dg-navy); color: var(--dg-cream); border: none;
}
.stButton button[kind="primary"]:hover, [data-testid="stBaseButton-primary"]:hover {
  background: #0d1320; color: #ffffff;
}
.stButton button[kind="secondary"], [data-testid="stBaseButton-secondary"] {
  background: transparent; color: var(--dg-navy2); border: 1px solid var(--dg-navy2);
}

/* Dividers, expanders, captions */
hr { border-color: var(--dg-hair); }
[data-testid="stExpander"] { border: 1px solid var(--dg-hair); border-radius: 12px; }
[data-testid="stExpander"] summary:hover { color: var(--dg-navy); }
[data-testid="stCaptionContainer"] { color: var(--dg-muted); }
</style>
"""
st.markdown(DESIGN_CSS, unsafe_allow_html=True)


def _render_brand_header():
    """The DealGauge wordmark (light version on the paper background) + a short tagline.
    Inline SVG so it renders crisply; falls back to a styled text wordmark if missing."""
    logo = _asset_text("dealgauge-logo.svg")
    if logo:
        st.markdown(f'<div class="dg-header"><span class="dg-logo">{logo}</span></div>',
                    unsafe_allow_html=True)
    else:
        st.markdown('<div class="dg-header"><h1 style="margin:0">DealGauge</h1></div>',
                    unsafe_allow_html=True)
    st.caption("Commercial real estate underwriting — enter a deal, get the metrics and a "
               "buy/pass read.")


# -----------------------------------------------------------------------------
# Authentication + marketing landing page (Streamlit native OIDC).
# Auth is configured in .streamlit/secrets.toml ([auth] with Google OIDC). No secret
# ever lives in code. A signed-out visitor sees the full landing page below (the dark
# "front door"); signing in with Google reveals the app. The app and engine are unchanged.
#
# DESIGN.md inverted for the landing: navy background, cream text, brass as the single
# accent. The hero/section photos are downscaled web images in assets/web/, embedded as
# base64 data URIs so the CSS gradient scrim + fade work cleanly (st.image can't do that).
# -----------------------------------------------------------------------------
GITHUB_URL = "https://github.com/your-org/dealgauge"   # placeholder — edit to the real repo
SUPPORT_EMAIL = "nic.a.hornung@gmail.com"


@st.cache_data(show_spinner=False)
def _web_image_data_uri(filename):
    """Base64 data URI for a downscaled web image in assets/web/ (encoded once, cached)."""
    try:
        raw = (ASSETS_DIR / "web" / filename).read_bytes()
    except OSError:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def _landing_css():
    """The dark landing-page stylesheet, with the hero + Seattle + ending photos embedded."""
    hero = _web_image_data_uri("hero.jpg")
    seattle = _web_image_data_uri("seattle.jpg")
    ending = _web_image_data_uri("ending.jpg")
    return f"""
<style>
:root {{
  --navy:#12182B; --navy2:#1A2238; --cream:#ECE6D6; --paperish:#FBFAF7;
  --brass:#C2A35E; --muted:rgba(236,230,214,0.72); --hair:rgba(236,230,214,0.14);
}}
/* Full-bleed dark landing; hide the app chrome and remove container padding */
.stApp {{ background: var(--navy); }}
[data-testid="stHeader"] {{ display: none; }}
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {{ display: none; }}
footer {{ display: none; }}
[data-testid="stMainBlockContainer"], .block-container {{ max-width: 100% !important; padding: 0 !important; }}
[data-testid="stMain"] {{ background: var(--navy); }}
[data-testid="stVerticalBlock"] {{ gap: 0 !important; }}
.stApp, .stApp p, .stApp h1, .stApp h2, .stApp h3 {{
  font-family: 'Inter','Segoe UI', system-ui, -apple-system, sans-serif;
}}

/* Section scaffold (8px rhythm, document-width inner wrap, generous whitespace) */
.dg-wrap {{ max-width: 1080px; margin: 0 auto; }}
.dg-sec {{ padding: 88px 28px; }}
.dg-alt {{ background: #10152a; }}
.dg-eyebrow {{ color: var(--brass); text-transform: uppercase; letter-spacing: 0.16em;
  font-size: 0.76rem; font-weight: 700; margin: 0; }}
/* Section headings: force bright white (!important beats Streamlit's themed heading
   color, which otherwise renders these dark and they blend into the navy background) */
.dg-h2 {{ color: #FFFFFF !important; font-size: clamp(1.6rem, 3vw, 2.15rem); font-weight: 700;
  letter-spacing: -0.01em; margin: 10px 0 0; }}
.dg-lead {{ color: var(--muted); font-size: 1.08rem; line-height: 1.6; margin: 16px 0 0; max-width: 640px; }}

/* HERO over the Chicago photo: navy scrim for legibility + a fade into the page below */
.dg-hero {{
  position: relative; min-height: clamp(560px, 76vh, 780px);
  display: flex; align-items: center; justify-content: center; text-align: center;
  padding: 80px 28px 64px;
  background:
    linear-gradient(180deg, rgba(18,24,43,0.60) 0%, rgba(18,24,43,0.54) 36%,
                    rgba(18,24,43,0.84) 76%, var(--navy) 100%),
    url("{hero}");
  background-size: cover; background-position: center 26%;
}}
.dg-hero-inner {{ max-width: 920px; }}
.dg-hero-logo svg {{ height: 56px; width: auto; margin-bottom: 30px; }}
.dg-hero h1 {{ color: var(--paperish); font-size: clamp(2.1rem, 4.6vw, 3.4rem); line-height: 1.07;
  font-weight: 700; letter-spacing: -0.025em; margin: 0 auto; max-width: 17ch; }}
.dg-hero .sub {{ color: rgba(236,230,214,0.88); font-size: clamp(1.05rem, 1.7vw, 1.28rem);
  line-height: 1.5; margin: 24px auto 0; max-width: 660px; }}

/* CTA buttons (native st.button, keyed, centered via a 3-column row): brass on navy */
.st-key-login_hero {{ margin-top: -18px; }}
.st-key-login_final {{ margin-top: 14px; }}
.st-key-login_hero button, .st-key-login_final button {{
  background: var(--brass) !important; color: var(--navy) !important; border: none !important;
  font-weight: 700; font-size: 1.04rem; padding: 0.8rem 2rem; border-radius: 11px;
  max-width: 360px; margin: 0 auto; display: block;
  box-shadow: 0 8px 22px rgba(0,0,0,0.30);
}}
.st-key-login_hero button:hover, .st-key-login_final button:hover {{
  background: #cdb274 !important; color: var(--navy) !important;
}}

/* WHAT IT DOES — feature cards */
.dg-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(232px, 1fr));
  gap: 20px; margin-top: 38px; }}
.dg-card {{ background: var(--navy2); border: 1px solid var(--hair); border-radius: 14px; padding: 26px 24px; }}
.dg-card .dg-dot {{ width: 30px; height: 30px; border-radius: 8px;
  background: rgba(194,163,94,0.16); border: 1px solid rgba(194,163,94,0.55); margin-bottom: 16px; }}
.dg-card h3 {{ color: var(--cream); font-size: 1.06rem; font-weight: 600; margin: 0 0 8px; }}
.dg-card p {{ color: var(--muted); font-size: 0.95rem; line-height: 1.55; margin: 0; }}

/* HOW IT WORKS — numbered steps */
.dg-steps {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 28px; margin-top: 38px; }}
.dg-step .dg-num {{ display: inline-flex; align-items: center; justify-content: center;
  width: 40px; height: 40px; border-radius: 50%; border: 1.5px solid var(--brass);
  color: var(--brass); font-weight: 700; font-size: 1.1rem; margin-bottom: 14px; }}
.dg-step h3 {{ color: var(--cream); font-size: 1.05rem; font-weight: 600; margin: 0 0 6px; }}
.dg-step p {{ color: var(--muted); font-size: 0.95rem; line-height: 1.5; margin: 0; }}

/* WHO IT'S FOR — split with the businessmen accent image (tonal treatment) */
.dg-split {{ display: grid; grid-template-columns: 1.05fr 1fr; gap: 46px; align-items: center; }}
.dg-accent-frame {{ position: relative; border-radius: 16px; overflow: hidden; border: 1px solid var(--hair); }}
.dg-accent-frame img {{ display: block; width: 100%; height: 100%; object-fit: cover;
  filter: saturate(0.82) brightness(0.82) contrast(1.04); }}
.dg-accent-frame::after {{ content: ""; position: absolute; inset: 0;
  background: linear-gradient(180deg, rgba(18,24,43,0.08) 0%, rgba(18,24,43,0.44) 100%); }}

/* DIFFERENTIATOR — band over the Seattle night photo */
.dg-band {{
  position: relative; padding: 108px 28px; text-align: center;
  background:
    linear-gradient(180deg, var(--navy) 0%, rgba(18,24,43,0.80) 24%,
                    rgba(18,24,43,0.84) 76%, var(--navy) 100%),
    url("{seattle}");
  background-size: cover; background-position: center 42%;
}}
.dg-band .line {{ max-width: 880px; margin: 0 auto; color: var(--paperish);
  font-size: clamp(1.3rem, 2.7vw, 1.95rem); line-height: 1.42; font-weight: 600; }}
.dg-band .line b {{ color: var(--brass); font-weight: 700; }}

/* FINAL CTA — centered in the navy gap between the Seattle band and the ending photo */
.dg-final {{ text-align: center; padding: 96px 28px 26px; }}
.dg-final .dg-h2, .dg-final .dg-lead {{ margin-left: auto; margin-right: auto; }}

/* ENDING — the closing photo, faded right out, with the support email */
.dg-end {{
  position: relative; text-align: center; padding: 176px 28px 104px;
  /* Hold navy under the button, then let the photo emerge gradually (no hard cut) and
     stay softly visible toward the bottom where the email sits. */
  background:
    linear-gradient(180deg, var(--navy) 0%, var(--navy) 20%,
                    rgba(18,24,43,0.85) 52%, rgba(18,24,43,0.70) 100%),
    url("{ending}");
  background-size: cover; background-position: center 55%;
}}
.dg-end-kicker {{ color: var(--brass); text-transform: uppercase; letter-spacing: 0.18em;
  font-size: 0.72rem; font-weight: 700; margin: 0 0 14px; }}
.dg-end-email {{ margin: 0; }}
.dg-end-email a {{ color: var(--cream); text-decoration: none; font-size: 1.0rem;
  letter-spacing: 0.03em; border-bottom: 1px solid rgba(194,163,94,0.55); padding-bottom: 3px; }}
.dg-end-email a:hover {{ color: var(--brass); border-color: var(--brass); }}

/* FOOTER */
.dg-footer {{ border-top: 1px solid var(--hair); padding: 26px 28px 44px; }}
.dg-foot-row {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }}
.dg-foot-brand {{ color: var(--cream); font-weight: 700; letter-spacing: 0.02em; }}
.dg-foot-links a {{ color: var(--brass); text-decoration: none; margin-left: 22px; font-size: 0.92rem; }}
.dg-foot-links a:hover {{ text-decoration: underline; }}
.dg-foot-fine {{ color: rgba(236,230,214,0.5); font-size: 0.82rem; margin: 14px 0 0; }}

@media (max-width: 760px) {{
  .dg-sec {{ padding: 60px 22px; }}
  .dg-split, .dg-steps {{ grid-template-columns: 1fr; }}
  .dg-foot-row {{ flex-direction: column; align-items: flex-start; }}
  .dg-foot-links a {{ margin: 0 22px 0 0; }}
}}
</style>
"""


def _login_cta(key):
    """A centered brass 'Sign in with Google' button that starts the Google OIDC flow.
    Centered with a 3-column row (reliable) and width-capped via CSS."""
    middle = st.columns([1, 1, 1])[1]
    if middle.button("Sign in with Google", key=key, type="primary", use_container_width=True):
        st.login()   # uses the [auth] config in secrets.toml; redirects to Google


def _render_landing():
    """The signed-out marketing landing page (the dark front door). Sections render as
    HTML; the two CTAs are native buttons so they can call st.login()."""
    st.markdown(_landing_css(), unsafe_allow_html=True)
    # Transparent wordmark (no navy plaque) so the logo floats cleanly over the photo.
    logo = (_asset_text("dealgauge-logo-transparent.svg")
            or _asset_text("dealgauge-logo-dark-cropped.svg")
            or _asset_text("dealgauge-logo-dark.svg"))

    # 1. HERO
    st.markdown(f"""
<section class="dg-hero"><div class="dg-hero-inner">
  <div class="dg-hero-logo">{logo}</div>
  <h1>Underwrite multifamily deals in minutes, not spreadsheets.</h1>
  <p class="sub">DealGauge turns offering memos and rent rolls into institutional-grade
  analysis, so you know whether a deal pencils before you ever build a model.</p>
</div></section>
""", unsafe_allow_html=True)
    _login_cta("login_hero")

    businessmen = _web_image_data_uri("businessmen.jpg")
    # 2-5. WHAT IT DOES / HOW IT WORKS / WHO IT'S FOR / DIFFERENTIATOR + final-CTA heading
    st.markdown(f"""
<section class="dg-sec"><div class="dg-wrap">
  <p class="dg-eyebrow">What it does</p>
  <h2 class="dg-h2">Everything you need to call a deal.</h2>
  <div class="dg-cards">
    <div class="dg-card"><div class="dg-dot"></div>
      <h3>Institutional-grade underwriting</h3>
      <p>NOI, cap rate, DSCR, cash-on-cash, IRR, and a clear buy/pass verdict. Every
      number traces back to your inputs.</p></div>
    <div class="dg-card"><div class="dg-dot"></div>
      <h3>AI document extraction</h3>
      <p>Drop in an offering memorandum or rent roll and DealGauge reads it into a
      populated model for you to review.</p></div>
    <div class="dg-card"><div class="dg-dot"></div>
      <h3>Value-add modeling</h3>
      <p>Project renovations, rent ramps, and the forced appreciation that drives
      multifamily returns.</p></div>
    <div class="dg-card"><div class="dg-dot"></div>
      <h3>One-click deal memos</h3>
      <p>Export a clean, professional PDF you can share with partners or lenders.</p></div>
  </div>
</div></section>

<section class="dg-sec dg-alt"><div class="dg-wrap">
  <p class="dg-eyebrow">How it works</p>
  <h2 class="dg-h2">Three steps to a defensible answer.</h2>
  <div class="dg-steps">
    <div class="dg-step"><span class="dg-num">1</span>
      <h3>Enter or import a deal</h3>
      <p>Type the numbers, or drop in an offering memo or rent roll.</p></div>
    <div class="dg-step"><span class="dg-num">2</span>
      <h3>Review the numbers and assumptions</h3>
      <p>Every field is editable, so you stay in control of the model.</p></div>
    <div class="dg-step"><span class="dg-num">3</span>
      <h3>Get your verdict and memo</h3>
      <p>A clear buy/pass read and an exportable PDF.</p></div>
  </div>
</div></section>

<section class="dg-sec"><div class="dg-wrap dg-split">
  <div class="dg-accent-frame"><img src="{businessmen}" alt="" /></div>
  <div>
    <p class="dg-eyebrow">Who it's for</p>
    <h2 class="dg-h2">Built for people who move on deals.</h2>
    <p class="dg-lead">Real estate investors, acquisition analysts, and anyone evaluating
    multifamily deals who wants fast, defensible numbers without wrestling a spreadsheet.</p>
  </div>
</div></section>

<section class="dg-band"><div class="dg-wrap">
  <p class="line">AI reads your documents, but the math is <b>deterministic and
  transparent</b>. Every result traces back to your inputs, <b>nothing is a black box</b>.</p>
</div></section>

<section class="dg-sec dg-final"><div class="dg-wrap">
  <h2 class="dg-h2">See whether your next deal pencils.</h2>
  <p class="dg-lead">Sign in with Google to start underwriting in minutes.</p>
</div></section>
""", unsafe_allow_html=True)
    _login_cta("login_final")

    # 6b. ENDING photo (faded) with the support email, then 7. FOOTER
    st.markdown(f"""
<section class="dg-end"><div class="dg-wrap">
  <p class="dg-end-kicker">Get in touch</p>
  <p class="dg-end-email"><a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a></p>
</div></section>
<footer class="dg-footer"><div class="dg-wrap">
  <div class="dg-foot-row">
    <span class="dg-foot-brand">DealGauge</span>
    <span class="dg-foot-links">
      <a href="{GITHUB_URL}" target="_blank" rel="noopener">GitHub</a>
    </span>
  </div>
  <p class="dg-foot-fine">&copy; 2026 DealGauge &nbsp;&middot;&nbsp; DealGauge provides
  analysis tools, not investment advice.</p>
</div></footer>
""", unsafe_allow_html=True)
    st.stop()   # nothing below renders until the visitor is signed in


def current_user_email():
    """The signed-in user's email (the per-user deal owner), or None if unavailable."""
    return getattr(st.user, "email", None)


# -----------------------------------------------------------------------------
# Small formatting + computation helpers (presentation only)
# -----------------------------------------------------------------------------
def _money(amount):
    """Format a dollar amount like $69,600 or -$3,480."""
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.0f}"


def _money_or_dash(value):
    """Format a dollar amount, or an em dash when it's None (for review tables)."""
    return "—" if value is None else _money(value)


def _format_metric(value, display):
    """Format a metric the way the engine labels it: 'percent' or 'ratio'."""
    if display == "percent":
        return f"{value * 100:.2f}%"
    return f"{value:.2f}x"


def _markdown_table(headers, rows):
    """Build a clean markdown table (no index column) from headers + row lists."""
    head = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join([head, separator, body])


def _value_or(value, default):
    """Return value, or the default when value is None (keeps a legitimate 0)."""
    return default if value is None else value


def _heat_color(value, vmin, vmax):
    """CSS background for a heatmap cell: red (low) -> yellow -> green (high).

    Pure-Python interpolation so we don't pull in matplotlib for a colormap.
    Returns "" for NaN/empty cells so they stay uncolored.
    """
    if pd.isna(value) or vmax == vmin:
        return ""
    t = (value - vmin) / (vmax - vmin)            # 0.0 = lowest IRR, 1.0 = highest
    if t < 0.5:                                   # red -> yellow
        f = t / 0.5
        r, g, b = 220 + (245 - 220) * f, 70 + (215 - 70) * f, 70 + (90 - 70) * f
    else:                                         # yellow -> green
        f = (t - 0.5) / 0.5
        r, g, b = 245 + (75 - 245) * f, 215 + (170 - 215) * f, 90 + (95 - 90) * f
    return f"background-color: rgb({int(r)}, {int(g)}, {int(b)}); color: #111;"


def compute_metrics(deal):
    """Run a deal (in engine units: rates as fractions) through every engine
    function and return all the numbers the UI needs. No math lives here — this
    just orchestrates calls into engine.py."""
    gross_rental_income = engine.resolve_gross_rental_income(deal)
    vacancy_rate = deal["vacancy_rate"]
    purchase_price = deal["purchase_price"]
    annual_interest_rate = deal["annual_interest_rate"]
    amortization_years = deal["amortization_years"]

    # Resolve operating expenses via the engine (structured line items or a single
    # total). No expense math is done in the dashboard.
    effective_gross_income = gross_rental_income * (1 - vacancy_rate)
    expense_result = engine.build_operating_expenses(deal, effective_gross_income)
    operating_expenses = expense_result["total"]

    noi = engine.calculate_noi(gross_rental_income, vacancy_rate, operating_expenses)
    cap_rate = engine.calculate_cap_rate(noi, purchase_price)
    # Resolve financing (Phase B): manual loan/down, or sized from LTV/DSCR using NOI.
    financing = engine.resolve_financing(deal, noi)
    loan_amount = financing["loan_amount"]
    down_payment = financing["down_payment"]
    annual_debt_service = engine.calculate_annual_debt_service(
        loan_amount, annual_interest_rate, amortization_years
    )
    dscr = engine.calculate_dscr(noi, annual_debt_service)
    cash_on_cash = engine.calculate_cash_on_cash_return(
        noi, annual_debt_service, down_payment
    )
    evaluation = engine.evaluate_deal(cap_rate, dscr, cash_on_cash)

    return {
        "noi": noi,
        "cap_rate": cap_rate,
        "annual_debt_service": annual_debt_service,
        "dscr": dscr,
        "cash_on_cash": cash_on_cash,
        "evaluation": evaluation,
        "operating_expenses": operating_expenses,
        "expense_result": expense_result,
        "loan_amount": loan_amount,
        "down_payment": down_payment,
        "sizing": financing["sizing"],
        # Intermediates for the breakdown tables.
        "vacancy_loss": gross_rental_income * vacancy_rate,
        "effective_gross_income": effective_gross_income,
        "monthly_payment": annual_debt_service / 12,
        "annual_cash_flow": noi - annual_debt_service,
    }


def _assumption_defaults():
    """Hold & exit assumption fields, defaulted from the engine's sample deal.

    Used to fill these in for deals that don't carry them — e.g. one loaded from
    the database, whose schema (unchanged) stores only the year-one inputs.
    """
    deal = engine.sample_deal
    return {
        "hold_period_years": deal["hold_period_years"],
        "rent_growth": deal["rent_growth"],
        "expense_growth": deal["expense_growth"],
        "exit_cap_rate": deal["exit_cap_rate"],
        "selling_cost_pct": deal["selling_cost_pct"],
    }


# -----------------------------------------------------------------------------
# RentCast lookups (Phase 5)
#
# RentCast is called ONLY on the "Look up property" button press (in the sidebar),
# never on an ordinary rerun. We also wrap the client in st.cache_data keyed by
# address, so looking up the same address twice costs nothing. The free plan is 50
# calls a month and each lookup is two calls (value + rent), so this matters.
#
# Successful results are cached. Failures are re-raised inside the cached function
# so st.cache_data does NOT store them — that way a transient error (rate limit,
# network blip) can be retried instead of being stuck in the cache.
# -----------------------------------------------------------------------------
class _LookupFailed(Exception):
    """Carries a rentcast error-result dict so we can avoid caching failures."""

    def __init__(self, result):
        super().__init__(result.get("error", "lookup failed"))
        self.result = result


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def _cached_value_estimate(address):
    result = rentcast.get_value_estimate(address)
    if not result["ok"]:
        raise _LookupFailed(result)  # don't cache failures
    return result


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def _cached_rent_estimate(address):
    result = rentcast.get_rent_estimate(address)
    if not result["ok"]:
        raise _LookupFailed(result)  # don't cache failures
    return result


def value_estimate(address):
    """Cached value lookup that always returns a result dict (never raises).

    The API key is checked LIVE here, BEFORE the cache. With no key we return the
    missing-key result without ever touching st.cache_data — so a no-key state is
    never cached, and a key set later takes effect on the very next lookup.
    """
    if not rentcast.has_api_key():
        return rentcast.missing_key_error()
    try:
        return _cached_value_estimate(address)
    except _LookupFailed as failure:
        return failure.result


def rent_estimate(address):
    """Cached rent lookup that always returns a result dict (never raises).

    Same as value_estimate: the key is checked live, before the cache.
    """
    if not rentcast.has_api_key():
        return rentcast.missing_key_error()
    try:
        return _cached_rent_estimate(address)
    except _LookupFailed as failure:
        return failure.result


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def _cached_market_trends(zip_code):
    result = rentcast.get_market_trends(zip_code)
    if not result["ok"]:
        raise _LookupFailed(result)  # don't cache failures
    return result


def market_trends(zip_code):
    """Cached market-trends lookup (1 API call per new zip). Never raises.

    The key is checked live before the cache, like the value/rent lookups, so a
    no-key state is never cached. Repeat views of the same zip cost nothing.
    """
    if not rentcast.has_api_key():
        return rentcast.missing_key_error()
    try:
        return _cached_market_trends(zip_code)
    except _LookupFailed as failure:
        return failure.result


def _zip_from_address(address):
    """Pull the last 5-digit group out of an address string, or '' if none."""
    matches = re.findall(r"\b(\d{5})\b", address or "")
    return matches[-1] if matches else ""


def _render_market_trends():
    """Show the most recent market-trends result: zip history vs the user's input."""
    trends = st.session_state.get("_trends")
    if trends is None:
        return
    if not trends.get("ok"):
        st.warning(trends["error"])
        return

    zip_code = st.session_state.get("_trends_zip", "")
    user_growth_pct = st.session_state["rent_growth_pct"]     # already a percent
    history_growth = trends.get("annualized_rent_growth")     # fraction or None

    span_months = trends.get("rent_history_months") or 0
    left, right = st.columns(2)
    left.metric("Your rent-growth assumption", f"{user_growth_pct:.1f}%/yr")
    if history_growth is not None:
        right.metric(f"Zip {zip_code} rent growth (history)", f"{history_growth * 100:.1f}%/yr")
        st.caption(f"Annualized over the **{span_months} months** of history RentCast "
                   f"returned for zip {zip_code}. Informational only — your assumption is "
                   "left unchanged; adjust it yourself if the history suggests you should.")
    else:
        right.metric(f"Zip {zip_code} rent growth (history)", "n/a")
        st.caption("RentCast returned too little rent history for this zip to derive a "
                   "trend. Your assumption is left unchanged.")

    rent_history = trends.get("rent_history") or []
    if len(rent_history) >= 2:
        st.line_chart(
            {"Month": [m for m, _ in rent_history], "Avg rent": [v for _, v in rent_history]},
            x="Month", y="Avg rent",
        )


def _comp_cell(value, kind="plain"):
    """Format one comparable's cell, showing an em dash for missing values."""
    if value is None:
        return "—"
    if kind == "money":
        return _money(value)
    if kind == "sqft":
        return f"{value:,.0f}"
    if kind == "miles":
        return f"{value:.2f}"
    if kind == "pct":
        return f"{value * 100:.0f}%"
    if kind == "num":
        return f"{value:g}"
    return str(value)


def _comps_rows(comps, price_label):
    """Turn comp dicts into rows for st.dataframe (all strings, pre-formatted)."""
    rows = []
    for comp in comps:
        rows.append({
            "Address": comp.get("address") or "—",
            price_label: _comp_cell(comp.get("price"), "money"),
            "Bd": _comp_cell(comp.get("bedrooms"), "num"),
            "Ba": _comp_cell(comp.get("bathrooms"), "num"),
            "SqFt": _comp_cell(comp.get("squareFootage"), "sqft"),
            "Dist mi": _comp_cell(comp.get("distance"), "miles"),
            "Days old": _comp_cell(comp.get("daysOld"), "num"),
            "Match": _comp_cell(comp.get("correlation"), "pct"),
        })
    return rows


def _render_comp_check(label, kind, check, per_unit):
    """Render one market-validation line (rent or price) from compare_to_comps."""
    if not check.get("ok"):
        st.caption(f"{label}: not enough comps to compare.")
        return
    gap = check["gap_pct"]
    count = check["comp_count"]
    basis = "per-unit " if per_unit else ""
    median_text = _money(check["comp_median"]) + ("/mo" if kind == "rent" else "")
    if check["materially_above"]:
        tail = "possibly optimistic" if kind == "rent" else "you may be paying above market"
        st.warning(f"⚠️ Your {basis}{label.lower()} is ~{gap * 100:.0f}% **above** the "
                   f"median of {count} comps ({median_text}) — {tail}.")
    elif check["materially_below"]:
        tail = "conservative vs comps" if kind == "rent" else "below comparable sales"
        st.info(f"Your {basis}{label.lower()} is ~{abs(gap) * 100:.0f}% **below** the "
                f"median of {count} comps ({median_text}) — {tail}.")
    else:
        st.success(f"Your {basis}{label.lower()} is in line with comps "
                   f"({gap * 100:+.0f}% vs the median of {count}, {median_text}).")


def render_rentcast_panel():
    """Show the most recent RentCast lookup: estimates, apply buttons, and comps."""
    value_result = st.session_state.get("_rc_value")
    rent_result = st.session_state.get("_rc_rent")
    if value_result is None and rent_result is None:
        return  # nothing looked up yet this session

    address_used = st.session_state.get("_rc_address_used", "")
    with st.container(border=True):
        st.markdown(f"**RentCast lookup — {address_used}**")
        st.caption("Starting estimates to validate your inputs — not final truth. Eyeball the comps.")

        value_col, rent_col = st.columns(2)

        with value_col:
            st.markdown("**Value estimate**")
            if value_result and value_result.get("ok") and value_result.get("estimate") is not None:
                estimate = value_result["estimate"]
                st.metric("Estimated value", _money(estimate), label_visibility="collapsed")
                low, high = value_result.get("range_low"), value_result.get("range_high")
                if low is not None and high is not None:
                    st.caption(f"Range {_money(low)} – {_money(high)}")
                if st.button("→ Use as purchase price", key="rc_apply_price", use_container_width=True):
                    st.session_state["_pending_prefill"] = {
                        "fields": {"purchase_price": float(round(estimate))},
                        "message": f"Set purchase price to {_money(estimate)} from the RentCast value estimate.",
                    }
                    st.rerun()
            elif value_result and not value_result.get("ok"):
                st.warning(value_result["error"])
            else:
                st.caption("No value estimate returned.")

        with rent_col:
            st.markdown("**Rent estimate**")
            if rent_result and rent_result.get("ok") and rent_result.get("estimate") is not None:
                monthly_rent = rent_result["estimate"]
                annual_rent = monthly_rent * 12
                st.metric("Estimated rent", f"{_money(monthly_rent)}/mo", label_visibility="collapsed")
                low, high = rent_result.get("range_low"), rent_result.get("range_high")
                if low is not None and high is not None:
                    st.caption(f"Range {_money(low)} – {_money(high)}/mo · ≈ {_money(annual_rent)}/yr")
                else:
                    st.caption(f"≈ {_money(annual_rent)}/yr")
                if st.button("→ Use as gross rent (×12)", key="rc_apply_rent", use_container_width=True):
                    st.session_state["_pending_prefill"] = {
                        "fields": {"gross_rental_income": float(round(annual_rent))},
                        "message": (f"Set gross rental income to {_money(annual_rent)}/yr "
                                    f"({_money(monthly_rent)}/mo × 12) from RentCast."),
                    }
                    st.rerun()
            elif rent_result and not rent_result.get("ok"):
                st.warning(rent_result["error"])
            else:
                st.caption("No rent estimate returned.")

        if value_result and value_result.get("ok") and value_result.get("comps"):
            st.markdown("**Nearby sales (value comps)**")
            st.dataframe(_comps_rows(value_result["comps"], "Sale price"),
                         hide_index=True, use_container_width=True)
        if rent_result and rent_result.get("ok") and rent_result.get("comps"):
            st.markdown("**Nearby rentals (rent comps)**")
            st.dataframe(_comps_rows(rent_result["comps"], "Rent/mo"),
                         hide_index=True, use_container_width=True)

        # ---- Market validation: the user's numbers vs the comps (NO new API call) ----
        units = max(int(st.session_state.get("number_of_units", 1) or 1), 1)
        rent_has_comps = rent_result and rent_result.get("ok") and rent_result.get("comps")
        value_has_comps = value_result and value_result.get("ok") and value_result.get("comps")
        if rent_has_comps or value_has_comps:
            st.markdown("**Market validation** — a directional sanity check")
            if rent_has_comps:
                user_rent_per_unit = st.session_state["gross_rental_income"] / units / 12
                rent_check = engine.compare_to_comps(
                    user_rent_per_unit, [c["price"] for c in rent_result["comps"]])
                _render_comp_check("Gross rent", "rent", rent_check, per_unit=units > 1)
            if value_has_comps:
                user_price_per_unit = st.session_state["purchase_price"] / units
                price_check = engine.compare_to_comps(
                    user_price_per_unit, [c["price"] for c in value_result["comps"]])
                _render_comp_check("Purchase price", "price", price_check, per_unit=units > 1)
            st.caption("Approximate: RentCast comps skew toward single units, so for "
                       "multi-unit properties read this as a rough, directional check only.")


# -----------------------------------------------------------------------------
# Import from documents (AI) — Phase D
#
# Upload a rent roll and/or operating statement (T-12); Claude reads them into the
# existing rent-roll and expense inputs for the user to REVIEW and edit. The API is
# called ONLY on the "Extract" button press (never on an ordinary rerun). Extraction
# only POPULATES the editable form — the deterministic engine still does every metric
# and the buy/pass decision, and only when the user clicks Run underwriting.
#
# The actual form pre-fill happens in the apply-before-widgets step below (via the
# _pending_extract flag), exactly like a saved-deal load — you can't change a widget's
# value after it's been created.
# -----------------------------------------------------------------------------
EXPENSE_LINE_LABELS = {
    "taxes": "Property taxes",
    "insurance": "Insurance",
    "management": "Management",
    "repairs": "Repairs & maintenance",
    "utilities": "Utilities (owner-paid)",
    "reserves": "Replacement reserves",
    "hoa": "HOA fees",
}


def _extraction_has_data(result):
    """True if an extraction result carries anything that PRE-FILLS a form field —
    a rent roll, an expense, the offering price, or a stated vacancy rate. (The stated
    NOIs are display-only cross-checks, so they don't count toward pre-filling.)"""
    if not result or not result.get("ok"):
        return False
    summary = result.get("summary") or {}
    has_rent_roll = bool(result.get("rent_roll"))
    has_expense = any(v is not None for v in (result.get("operating_expenses") or {}).values())
    has_price = summary.get("offering_price") is not None
    has_vacancy = summary.get("vacancy_rate_pct") is not None
    return has_rent_roll or has_expense or has_price or has_vacancy


def _render_extraction_review():
    """Show what the most recent extraction pulled, so the user can eyeball it before
    editing the pre-filled fields. Reads the stored result; makes no API call."""
    result = st.session_state.get("_extraction_result")
    if result is None:
        return

    if not result.get("ok"):
        # The missing-key case is already warned about by the uploader section above.
        if result.get("error_type") != "missing_key":
            st.warning(result.get("error", "Extraction failed."))
        return

    summary = result.get("summary") or {}
    rent_roll = result.get("rent_roll") or []
    expenses = result.get("operating_expenses") or {}
    any_expense = any(v is not None for v in expenses.values())
    any_summary = any(summary.get(key) is not None for key in extraction.SUMMARY_KEYS)

    if not rent_roll and not any_expense and not any_summary:
        st.warning("Claude didn't find a price, rent roll, or expense figures in the "
                   "document(s). Your manual inputs are unchanged — enter the numbers yourself.")
        for note in result.get("notes") or []:
            st.caption(f"• {note}")
        return

    st.success(f"Extracted with `{result.get('model')}` — these are AI estimates. Review "
               "them here, then check and edit the pre-filled fields below before underwriting.")

    # Property summary: the offering price + vacancy that pre-fill the inputs.
    summary_rows = []
    if summary.get("offering_price") is not None:
        summary_rows.append(["Offering / asking price → purchase price", _money(summary["offering_price"])])
    if summary.get("vacancy_rate_pct") is not None:
        summary_rows.append(["Vacancy rate → vacancy input", f"{summary['vacancy_rate_pct']:.1f}%"])
    if summary_rows:
        st.markdown("**Property summary** (pre-filled)")
        st.markdown(_markdown_table(["Field", "Value"], summary_rows))

    if rent_roll:
        st.markdown(f"**Rent roll** — {len(rent_roll)} unit(s) pulled")
        preview = [{
            "Unit": unit.get("label"),
            "Type": unit.get("unit_type") or "—",
            "SqFt": "—" if unit.get("square_footage") is None else f"{unit['square_footage']:,.0f}",
            "Rent/mo": _money_or_dash(unit.get("monthly_rent")),
            "Market/mo": _money_or_dash(unit.get("market_rent")),
            "Occupied": "Yes" if unit.get("occupied", True) else "No",
        } for unit in rent_roll]
        st.dataframe(preview, hide_index=True, use_container_width=True)

    if any_expense:
        st.markdown("**Operating expenses** (annual, as found)")
        rows = [[EXPENSE_LINE_LABELS[key], _money_or_dash(expenses.get(key))]
                for key in extraction.EXPENSE_KEYS]
        total = sum(v for v in expenses.values() if v is not None)
        rows.append(["**Total loaded**", f"**{_money(total)}**"])
        st.markdown(_markdown_table(["Line item", "Annual amount"], rows))
        st.caption("Loaded as a single operating-expense **total** (the sum above) in Simple "
                   "mode — a statement reports actual dollars. Switch to detailed line items "
                   "below if you'd rather model taxes/management/repairs as percentages.")

    # Stated NOI: a cross-check the user reads against the tool's own computed NOI —
    # deliberately NOT fed into the engine.
    current_noi = summary.get("current_noi")
    pro_forma_noi = summary.get("pro_forma_noi")
    if current_noi is not None or pro_forma_noi is not None:
        st.markdown("**Stated NOI — cross-check only (not used by the engine)**")
        noi_rows = []
        if current_noi is not None:
            noi_rows.append(["Current NOI (stated in OM)", _money(current_noi)])
        if pro_forma_noi is not None:
            noi_rows.append(["Pro forma NOI (stated in OM)", _money(pro_forma_noi)])
        st.markdown(_markdown_table(["Figure", "Annual amount"], noi_rows))
        st.caption("Shown only to compare against the NOI the tool computes from your reviewed "
                   "inputs. These never feed the engine — the deal's NOI is always recomputed.")

    for note in result.get("notes") or []:
        st.caption(f"• {note}")

    # Short note: what was imported vs. what the user still has to enter (financing).
    imported = []
    if summary.get("offering_price") is not None:
        imported.append("purchase price")
    if rent_roll:
        imported.append(f"rent roll ({len(rent_roll)} units)")
    if any_expense:
        imported.append("operating expenses")
    if summary.get("vacancy_rate_pct") is not None:
        imported.append("vacancy rate")
    imported_text = ", ".join(imported) if imported else "nothing pre-fillable"
    st.info(
        f"**Imported:** {imported_text}.\n\n"
        "**You still enter:** financing — loan amount / down payment (or LTV & DSCR to size "
        "the loan), interest rate, and amortization — plus the hold & exit assumptions. An "
        "offering memorandum doesn't contain a buyer's financing terms, so the tool never "
        "extracts them."
    )

    st.caption("⚠️ AI-extracted estimates — double-check every value before you run the numbers.")


def _render_document_import():
    """The 'Import from documents (AI)' section: a file uploader plus an Extract button
    that makes ONE API call on press, then a review panel. Pre-fills happen via the
    _pending_extract flag (applied before the input widgets are created)."""
    with st.expander("Import from documents (AI)", expanded=False):
        st.caption(
            "Upload a rent roll and/or operating statement (T-12) — PDF, image, CSV, or "
            "Excel — and Claude reads it into the rent-roll and expense inputs below. "
            "**These are AI-extracted estimates to review, not final.** You can edit every "
            "value; the buy/pass math is still the deterministic engine, run only when you "
            "click **Run underwriting**."
        )
        if not extraction.has_api_key():
            st.warning(extraction.MISSING_KEY_MESSAGE)

        st.file_uploader(
            "Document(s)",
            type=["pdf", "png", "jpg", "jpeg", "gif", "webp", "csv", "xlsx", "xls", "txt"],
            accept_multiple_files=True,
            key="_doc_uploads",
            help="Upload the rent roll, the operating statement, or both.",
        )
        st.caption("One **Extract** = one Claude API call. The cost is tiny — a fraction of "
                   "a cent — but it does call the API, so it only runs on the button press.")

        if st.button("Extract", key="extract_btn", use_container_width=True):
            uploaded = st.session_state.get("_doc_uploads") or []
            if not uploaded:
                st.warning("Upload at least one document before extracting.")
            else:
                files = [(item.name, item.getvalue()) for item in uploaded]
                with st.spinner("Reading your document(s) with Claude…"):
                    result = extraction.extract_from_documents(files)
                st.session_state["_extraction_result"] = result
                # Only pre-fill the form when something usable came back; otherwise the
                # manual inputs are left exactly as they were.
                if _extraction_has_data(result):
                    st.session_state["_pending_extract"] = result
                    st.session_state["_flash"] = (
                        "Pre-filled the form from your document(s). Review and edit the "
                        "values below, then click Run underwriting."
                    )
                st.rerun()

        _render_extraction_review()


# -----------------------------------------------------------------------------
# Deal memo (Phase E)
#
# A one-page summary of a completed underwrite: the engine's numbers + verdict, plus a
# plain-English write-up. The AI write-up is generated ONLY on the "Generate memo"
# button press (one API call), never on rerun — and falls back to a template summary
# with no API call when there's no key. The numbers shown and put in the PDF come
# straight from the engine's results via deal_memo.build_memo_data; the AI only writes
# the prose and never makes the buy/pass call.
# -----------------------------------------------------------------------------
def _memo_filename(memo_data):
    """A tidy PDF filename from the deal name, e.g. deal-memo-maple-street.pdf."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", memo_data["property"]["name"]).strip("-").lower()
    return f"deal-memo-{slug or 'deal'}.pdf"


def _render_deal_memo(memo_data):
    """Render the Deal memo section: a Generate button, the on-screen memo, and a PDF
    download. Reads/writes st.session_state['_memo_narrative']; the API call happens only
    on the button press."""
    st.divider()
    st.subheader("Deal memo")
    st.caption("A one-page summary of this underwrite — the engine's numbers and verdict "
               "with a plain-English write-up. Generate the write-up, then download the PDF.")

    if st.button("Generate memo", key="memo_btn", use_container_width=True):
        with st.spinner("Writing the memo…"):
            st.session_state["_memo_narrative"] = deal_memo.generate_narrative(memo_data)
        st.rerun()
    st.caption("One **Generate memo** = one Claude API call (a fraction of a cent). Without "
               "an API key it still produces a template summary — no call made.")

    narrative = st.session_state.get("_memo_narrative")
    if not narrative:
        return

    if narrative["source"] == "ai":
        st.success("Write-up by Claude from the engine's numbers — it reports the model's "
                   "verdict, it doesn't make it. Review before sharing.")
    else:
        st.info(narrative.get("error") or "Template summary (no AI used).")

    # Compact on-screen rendering of the same memo (mirrors the PDF layout).
    prop, met, asm = memo_data["property"], memo_data["metrics"], memo_data["assumptions"]
    verdict = memo_data["verdict"]["verdict"]
    with st.container(border=True):
        st.markdown(f"#### {prop['name']}")
        st.caption(f"{prop['property_type']} · {prop['number_of_units']} units · "
                   f"{_money(prop['purchase_price'])} ({_money(prop['price_per_unit'])}/unit)")
        st.markdown(f"**Model verdict: {verdict}** "
                    f"({'clears every threshold' if verdict == 'BUY' else 'below one or more thresholds'})")

        irr_text = f"{met['irr'] * 100:.1f}%" if met["irr"] is not None else "n/a"
        em_text = f"{met['equity_multiple']:.2f}x" if met["equity_multiple"] is not None else "n/a"
        st.markdown("**Key metrics**")
        st.markdown(_markdown_table(
            ["NOI", "Cap rate", "DSCR", "Cash-on-cash", "IRR", "Equity mult."],
            [[_money(met["noi"]), f"{met['cap_rate'] * 100:.2f}%", f"{met['dscr']:.2f}x",
              f"{met['cash_on_cash'] * 100:.2f}%", irr_text, em_text]],
        ))

        st.markdown("**Key assumptions**")
        st.markdown(_markdown_table(
            ["Vacancy", "Rate / amort", "Hold", "Rent gr.", "Exp. gr.", "Exit cap"],
            [[f"{asm['vacancy_rate'] * 100:.1f}%",
              f"{asm['interest_rate'] * 100:.2f}% / {asm['amortization_years']}y",
              f"{asm['hold_period_years']}y", f"{asm['rent_growth'] * 100:.1f}%",
              f"{asm['expense_growth'] * 100:.1f}%", f"{asm['exit_cap_rate'] * 100:.2f}%"]],
        ))

        if memo_data["value_add"]:
            va = memo_data["value_add"]
            st.markdown("**Value-add**")
            st.markdown(_markdown_table(
                ["Upside units", "NOI lift", "Value gain", "Reno cost", "Yrs to stab."],
                [[str(va["num_upside_units"]), _money(va["noi_lift"]), _money(va["value_gain"]),
                  _money(va["total_renovation_cost"]), str(va["years_to_stabilize"])]],
            ))

        st.markdown("**Summary**")
        st.markdown(narrative["text"])

    # PDF download — rendered locally from the same memo data (no API call).
    try:
        pdf_bytes = deal_memo.render_pdf(memo_data, narrative["text"])
        st.download_button(
            "Download PDF", data=pdf_bytes, file_name=_memo_filename(memo_data),
            mime="application/pdf", use_container_width=True, key="memo_pdf_btn",
        )
    except Exception as exc:   # never let a render hiccup break the page
        st.warning(f"Couldn't render the PDF ({exc}). The on-screen memo above is still complete.")


# -----------------------------------------------------------------------------
# Form state: seed defaults from the engine's sample deal, and load saved deals.
#
# Streamlit reruns this whole script on every interaction. We keep each input in
# st.session_state so values survive reruns and so "Load" can drop a saved deal
# back into the form. Vacancy and interest are kept in the UI as PERCENT (5.0,
# 6.5); we convert to fractions (0.05, 0.065) before calling the engine/database.
# -----------------------------------------------------------------------------
RENT_ROLL_COLUMNS = ["label", "unit_type", "square_footage", "monthly_rent", "market_rent", "occupied"]


def _na(value):
    """None for NaN/None, else the value (cleans st.data_editor blanks)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def _clean_rent_roll(edited_df):
    """Turn the edited rent-roll dataframe into a clean list of unit dicts."""
    roll = []
    for i, rec in enumerate(edited_df.to_dict("records")):
        label = _na(rec.get("label"))
        unit_type = _na(rec.get("unit_type"))
        square_footage = _na(rec.get("square_footage"))
        monthly_rent = _na(rec.get("monthly_rent"))
        market_rent = _na(rec.get("market_rent"))
        occupied = _na(rec.get("occupied"))
        roll.append({
            "label": str(label).strip() if label is not None else f"Unit {i + 1}",
            "unit_type": str(unit_type) if unit_type is not None else "",
            "square_footage": float(square_footage) if square_footage is not None else None,
            "monthly_rent": float(monthly_rent) if monthly_rent is not None else 0.0,
            "market_rent": float(market_rent) if market_rent is not None else None,
            "occupied": bool(occupied) if occupied is not None else False,
        })
    return roll


def _apply_expense_template(property_type):
    """Load a property type's expense-template defaults into the form (overwriting
    the current line-item values). Called when the user changes the property type."""
    template = engine.EXPENSE_TEMPLATES.get(property_type, engine.EXPENSE_TEMPLATES[engine.DEFAULT_PROPERTY_TYPE])
    st.session_state["property_tax_pct"] = template["property_tax_rate"] * 100.0
    st.session_state["insurance_annual"] = float(template["insurance_annual"])
    st.session_state["hoa_annual"] = float(template["hoa_annual"])
    st.session_state["management_pct_ui"] = template["management_pct"] * 100.0
    st.session_state["repairs_pct_ui"] = template["repairs_pct"] * 100.0
    st.session_state["utilities_annual"] = float(template["utilities_annual"])
    st.session_state["reserves_per_unit"] = float(template["reserves_per_unit"])


def _seed_form_defaults():
    """Seed a BLANK form into session_state once, as the starting values on launch.

    The app starts empty — it does NOT auto-load the sample deal. Deal-specific inputs
    (name, purchase price, income / rent roll, loan, down payment, renovation) start at
    0 / blank, and the generic assumptions not tied to a property (vacancy, interest,
    growth, selling cost) start at 0. What's KEPT is everything associated with the
    PROPERTY TYPE: the expense-template line items (taxes %, insurance, management %,
    repairs %, utilities, reserves, HOA) plus the income/expense/financing mode toggles.
    A handful of fields whose widget requires a non-zero value (unit count, amortization,
    hold, exit cap, LTV/DSCR) keep their conventional defaults — they can't be left at 0.

    The sample deal still exists in engine.py and still backs "Load into form" for saved
    deals and the assumption fallbacks; it just no longer pre-fills the blank launch form.
    """
    property_type = engine.DEFAULT_PROPERTY_TYPE
    template = engine.EXPENSE_TEMPLATES[property_type]

    # ---- Deal-specific inputs: start blank ----
    st.session_state.setdefault("name", "")
    st.session_state.setdefault("purchase_price", 0.0)
    st.session_state.setdefault("gross_rental_income", 0.0)
    st.session_state.setdefault("operating_expenses", 0.0)        # simple-mode total
    st.session_state.setdefault("loan_amount", 0.0)
    st.session_state.setdefault("down_payment", 0.0)
    st.session_state.setdefault("renovation_cost_per_unit_input", 0.0)
    st.session_state.setdefault("renovation_pace_input", 0.0)
    # One empty rent-roll row, so the detailed table shows its structure to fill in.
    st.session_state.setdefault(
        "rent_roll",
        [{"label": "Unit 1", "unit_type": "", "square_footage": None,
          "monthly_rent": 0.0, "market_rent": None, "occupied": True}],
    )

    # ---- Generic assumptions (not tied to a property type): start at 0 ----
    st.session_state.setdefault("vacancy_pct", 0.0)
    st.session_state.setdefault("interest_pct", 0.0)
    st.session_state.setdefault("rent_growth_pct", 0.0)
    st.session_state.setdefault("expense_growth_pct", 0.0)
    st.session_state.setdefault("sell_cost_pct", 0.0)

    # ---- Property type + its expense TEMPLATE: kept (these are the defaults that ARE
    #      associated with the property type; changing the type below reloads them) ----
    st.session_state.setdefault("property_type", property_type)
    st.session_state.setdefault("_applied_property_type", st.session_state["property_type"])
    st.session_state.setdefault("property_tax_pct", template["property_tax_rate"] * 100.0)
    st.session_state.setdefault("insurance_annual", float(template["insurance_annual"]))
    st.session_state.setdefault("hoa_annual", float(template["hoa_annual"]))
    st.session_state.setdefault("management_pct_ui", template["management_pct"] * 100.0)
    st.session_state.setdefault("repairs_pct_ui", template["repairs_pct"] * 100.0)
    st.session_state.setdefault("utilities_annual", float(template["utilities_annual"]))
    st.session_state.setdefault("reserves_per_unit", float(template["reserves_per_unit"]))

    # ---- Selector / mode toggles: keep their property-type-driven defaults ----
    st.session_state.setdefault(
        "income_mode_label",
        "Detailed (rent roll)" if engine.default_income_mode(property_type) == "detailed"
        else "Simple (single total)",
    )
    st.session_state.setdefault("expense_mode_label", "Detailed (line items)")
    st.session_state.setdefault("financing_mode_label", "Manual (enter loan / down payment)")

    # ---- Fields whose widget forbids 0: conventional defaults (not deal-identifying) ----
    st.session_state.setdefault("number_of_units", 1)
    st.session_state.setdefault("amortization_years", 30)
    st.session_state.setdefault("ltv_max_pct", engine.DEFAULT_LTV_MAX * 100.0)   # 75%
    st.session_state.setdefault("dscr_min_input", engine.DEFAULT_DSCR_MIN)        # 1.25x
    st.session_state.setdefault("hold_period_years", 5)
    st.session_state.setdefault("exit_cap_pct", 6.5)


def _apply_deal_to_form(deal):
    """Copy a deal dict (engine units) into the form's session_state fields."""
    st.session_state["name"] = deal["name"]
    st.session_state["purchase_price"] = float(deal["purchase_price"])
    st.session_state["gross_rental_income"] = float(deal["gross_rental_income"])
    st.session_state["vacancy_pct"] = float(deal["vacancy_rate"]) * 100.0
    # Operating expenses (Phase 9/12). A loaded deal carries these columns (NULL when a
    # simple-mode deal omitted line items); fall back to the property type's template on
    # None. Set _applied_property_type so the load isn't overwritten by the template.
    sample = engine.sample_deal
    property_type = deal.get("property_type") or engine.DEFAULT_PROPERTY_TYPE
    template = engine.expense_template_for(deal)
    st.session_state["property_type"] = property_type
    st.session_state["_applied_property_type"] = property_type
    st.session_state["number_of_units"] = int(_value_or(deal.get("number_of_units"), sample["number_of_units"]))
    # Income method (Phase A): from the loaded deal, defaulting by type if absent. Reset
    # the rent roll to the loaded one and clear the editor's edits from the prior deal.
    loaded_income_mode = deal.get("income_mode") or engine.default_income_mode(property_type)
    st.session_state["income_mode_label"] = (
        "Detailed (rent roll)" if loaded_income_mode == "detailed" else "Simple (single total)"
    )
    loaded_roll = deal.get("rent_roll")
    st.session_state["rent_roll"] = (
        [dict(u) for u in loaded_roll] if loaded_roll
        else engine.default_rent_roll(
            _value_or(deal.get("number_of_units"), sample["number_of_units"]),
            _value_or(deal.get("gross_rental_income"), sample["gross_rental_income"]))
    )
    st.session_state.pop("rent_roll_editor", None)
    st.session_state["expense_mode_label"] = (
        "Detailed (line items)" if deal.get("expense_mode") == "detailed" else "Simple (single total)"
    )
    st.session_state["operating_expenses"] = float(_value_or(deal.get("operating_expenses"), sample["operating_expenses"]))
    st.session_state["property_tax_pct"] = float(_value_or(deal.get("property_tax_rate"), template["property_tax_rate"])) * 100.0
    st.session_state["insurance_annual"] = float(_value_or(deal.get("insurance_annual"), template["insurance_annual"]))
    st.session_state["hoa_annual"] = float(_value_or(deal.get("hoa_annual"), template["hoa_annual"]))
    st.session_state["management_pct_ui"] = float(_value_or(deal.get("management_pct"), template["management_pct"])) * 100.0
    st.session_state["repairs_pct_ui"] = float(_value_or(deal.get("repairs_pct"), template["repairs_pct"])) * 100.0
    st.session_state["utilities_annual"] = float(_value_or(deal.get("utilities_annual"), template["utilities_annual"]))
    st.session_state["reserves_per_unit"] = float(_value_or(deal.get("reserves_per_unit"), template["reserves_per_unit"]))
    # Value-add (Phase C): renovation inputs from the loaded deal.
    st.session_state["renovation_cost_per_unit_input"] = float(_value_or(deal.get("renovation_cost_per_unit"), 0.0))
    st.session_state["renovation_pace_input"] = float(_value_or(deal.get("renovation_pace"), 0.0))
    st.session_state["loan_amount"] = float(deal["loan_amount"])
    st.session_state["interest_pct"] = float(deal["annual_interest_rate"]) * 100.0
    st.session_state["amortization_years"] = int(deal["amortization_years"])
    st.session_state["down_payment"] = float(deal["down_payment"])
    # Financing (Phase B): from the loaded deal, defaulting if absent.
    st.session_state["financing_mode_label"] = (
        "Size the loan (commercial)" if deal.get("financing_mode") == "sized"
        else "Manual (enter loan / down payment)"
    )
    st.session_state["ltv_max_pct"] = float(_value_or(deal.get("ltv_max"), engine.DEFAULT_LTV_MAX)) * 100.0
    st.session_state["dscr_min_input"] = float(_value_or(deal.get("dscr_min"), engine.DEFAULT_DSCR_MIN))
    # Hold & exit assumptions. A deal loaded from the database won't have these
    # (schema unchanged), so fall back to the engine's sample-deal defaults.
    defaults = engine.sample_deal
    st.session_state["hold_period_years"] = int(deal.get("hold_period_years", defaults["hold_period_years"]))
    st.session_state["rent_growth_pct"] = float(deal.get("rent_growth", defaults["rent_growth"])) * 100.0
    st.session_state["expense_growth_pct"] = float(deal.get("expense_growth", defaults["expense_growth"])) * 100.0
    st.session_state["exit_cap_pct"] = float(deal.get("exit_cap_rate", defaults["exit_cap_rate"])) * 100.0
    st.session_state["sell_cost_pct"] = float(deal.get("selling_cost_pct", defaults["selling_cost_pct"])) * 100.0


def _property_type_from_rent_roll(rent_roll, expenses):
    """Derive the property type from the rent roll's unit-ROW count — exactly how the
    types are defined. The row count is authoritative (use it, not any "number of units"
    figure stated elsewhere in the document, in case they disagree):

        1 unit  -> condo_townhome if the document shows HOA dues, else single_family_rental
        2-4     -> small_multifamily
        5+      -> larger_multifamily

    Returns None when the count can't be determined (empty roll), so the caller leaves
    the currently selected property type unchanged rather than guessing. Only the expense
    TEMPLATE differs by type; the NOI / cap / DSCR / IRR math is identical across them."""
    unit_count = len(rent_roll)
    if unit_count <= 0:
        return None
    if unit_count == 1:
        hoa = (expenses or {}).get("hoa")
        has_hoa = hoa is not None and hoa > 0   # HOA dues in the doc -> a condo/townhome
        return "condo_townhome" if has_hoa else "single_family_rental"
    if unit_count <= 4:
        return "small_multifamily"
    return "larger_multifamily"


def _apply_extraction_to_form(result):
    """Pre-fill the form's inputs from an AI extraction result.

    Only POPULATES the editable form fields (same session-state writes _apply_deal_to_form
    makes, so it must run before the input widgets are created). The user then reviews and
    edits every value; the engine still does all the underwriting. Financing is never
    touched — loan terms aren't in an OM and stay the user's to enter.

    - Offering price -> purchase price (the most important field; without it the rent roll
      gets underwritten against a stale price).
    - Stated vacancy rate -> vacancy input (only when the OM stated one; else left as is).
    - Rent roll -> detailed Income mode + the unit table, and the property type is derived
      from the unit-row count (1 / 2-4 / 5+; a 1-unit doc with HOA dues -> condo), which
      also loads that type's expense template. An empty roll leaves the type unchanged.
    - Expenses -> a single operating-expense TOTAL in Simple mode (a statement reports
      actual annual dollars; detailed mode would need %-of-price / %-of-income bases the
      statement doesn't give us).
    - Stated NOI is shown for cross-check only and is deliberately NOT applied.
    """
    summary = result.get("summary") or {}
    expenses = result.get("operating_expenses") or {}

    if summary.get("offering_price") is not None:
        st.session_state["purchase_price"] = float(summary["offering_price"])
    if summary.get("vacancy_rate_pct") is not None:
        st.session_state["vacancy_pct"] = float(summary["vacancy_rate_pct"])

    rent_roll = result.get("rent_roll") or []
    if rent_roll:
        st.session_state["rent_roll"] = [
            {
                "label": str(unit.get("label") or f"Unit {index + 1}"),
                "unit_type": str(unit.get("unit_type") or ""),
                "square_footage": unit.get("square_footage"),
                "monthly_rent": float(unit.get("monthly_rent") or 0.0),
                "market_rent": unit.get("market_rent"),
                "occupied": bool(unit.get("occupied", True)),
            }
            for index, unit in enumerate(rent_roll)
        ]
        st.session_state.pop("rent_roll_editor", None)   # drop the editor's stale edits
        st.session_state["income_mode_label"] = "Detailed (rent roll)"
        st.session_state["number_of_units"] = max(len(rent_roll), 1)

        # Derive the property type from the unit-row count (after the roll is parsed, so
        # the count is known). Load that type's expense template directly and mark it
        # applied, so the property-type selectbox below doesn't re-fire and reset the
        # income mode we just set to detailed (single-family / condo default to simple).
        derived_type = _property_type_from_rent_roll(rent_roll, expenses)
        if derived_type is not None:
            st.session_state["property_type"] = derived_type
            _apply_expense_template(derived_type)
            st.session_state["_applied_property_type"] = derived_type

    found = {key: value for key, value in expenses.items() if value is not None}
    if found:
        st.session_state["operating_expenses"] = float(sum(found.values()))
        st.session_state["expense_mode_label"] = "Simple (single total)"


# Authentication gate: a signed-out visitor sees only the marketing landing page (which
# calls st.stop()), so none of the app below renders until they're signed in with Google.
if not st.user.is_logged_in:
    _render_landing()

# From here on the visitor is signed in. Their email owns the deals they save/load.
USER_EMAIL = current_user_email()

# Seed defaults first (only fills keys that don't exist yet).
_seed_form_defaults()

# Apply a pending load BEFORE any input widget is created this run. The "Load"
# button (below) just records an id and reruns; we do the actual load here so we
# never modify a widget's state after it has been instantiated. Scoped to the signed-in
# user, so a deal id from another user (or a legacy deal) won't load.
_pending_load_id = st.session_state.pop("_pending_load_id", None)
if _pending_load_id is not None:
    loaded_deal = database.load_deal(_pending_load_id, owner=USER_EMAIL)
    if loaded_deal is not None:
        _apply_deal_to_form(loaded_deal)
        # Show the loaded deal's results immediately.
        st.session_state["_results_deal"] = loaded_deal
        st.session_state["_show_results"] = True
        st.session_state.pop("_memo_narrative", None)   # a different deal -> stale memo
        st.session_state["_flash"] = f"Loaded “{loaded_deal['name']}” (deal #{_pending_load_id})."

# Apply a pending RentCast pre-fill (set by an "Apply" button) BEFORE the input
# widgets are created — same reason as the load above: you can't change a widget's
# value after it has been instantiated.
_pending_prefill = st.session_state.pop("_pending_prefill", None)
if _pending_prefill is not None:
    for field, value in _pending_prefill["fields"].items():
        st.session_state[field] = value
    st.session_state["_flash"] = _pending_prefill["message"]

# Apply a pending document extraction (set by the "Extract" button) BEFORE the input
# widgets are created — same rule as the load and RentCast pre-fill above. The flash
# message is set by the button handler; here we only populate the fields.
_pending_extract = st.session_state.pop("_pending_extract", None)
if _pending_extract is not None:
    _apply_extraction_to_form(_pending_extract)


# -----------------------------------------------------------------------------
# Header + one-time flash message (e.g. "Saved", "Loaded")
# -----------------------------------------------------------------------------
_render_brand_header()

_flash = st.session_state.pop("_flash", None)
if _flash:
    st.success(_flash)


# -----------------------------------------------------------------------------
# Sidebar: load a previously saved deal
# -----------------------------------------------------------------------------
with st.sidebar:
    # Signed-in identity + log out, unobtrusively at the very top.
    _display_name = (getattr(st.user, "name", None) or USER_EMAIL or "Signed in")
    st.caption(f"Signed in as **{_display_name}**")
    if st.button("Log out", key="logout_btn", use_container_width=True):
        st.logout()
    st.divider()

    st.header("Saved deals")
    saved_deals = database.list_deals(owner=USER_EMAIL)  # only this user's deals

    if saved_deals:
        # Map a readable label -> deal id for the dropdown.
        options = {
            f"#{deal['id']} · {deal['name']} · {deal['created_at'][:10]}": deal["id"]
            for deal in saved_deals
        }
        chosen_label = st.selectbox("Pick a deal", list(options.keys()), key="_deal_choice")
        if st.button("Load into form", key="load_btn", use_container_width=True):
            st.session_state["_pending_load_id"] = options[chosen_label]
            st.rerun()
    else:
        st.caption("No saved deals yet. Fill the form and click **Save deal**.")

    st.caption(f"{len(saved_deals)} saved")

    st.divider()
    st.header("RentCast lookup")
    st.caption("Look up an address to pull an estimated property value and market rent, "
               "each with a low–high range and nearby comparable sales and rentals — a "
               "quick way to sanity-check your purchase price and rent assumptions.")
    lookup_address = st.text_input(
        "Property address", key="_rc_address",
        placeholder="Street, City, State, Zip",
        help="Full address. One lookup = a value estimate + a rent estimate + comps.",
    )
    if st.button("Look up property", key="rc_lookup_btn", use_container_width=True):
        if not lookup_address.strip():
            st.warning("Enter an address before looking up.")
        else:
            # RentCast is called HERE ONLY (this button press). Results are cached
            # by address, so reruns and repeat lookups don't spend more calls.
            with st.spinner("Asking RentCast…"):
                st.session_state["_rc_value"] = value_estimate(lookup_address.strip())
                st.session_state["_rc_rent"] = rent_estimate(lookup_address.strip())
            st.session_state["_rc_address_used"] = lookup_address.strip()
    st.caption("Estimates validate your inputs — they aren't final truth.")

    with st.expander("Buy thresholds"):
        thresholds = engine.DEFAULT_THRESHOLDS
        st.write(f"Cap rate ≥ {thresholds['min_cap_rate'] * 100:.2f}%")
        st.write(f"DSCR ≥ {thresholds['min_dscr']:.2f}x")
        st.write(f"Cash-on-cash ≥ {thresholds['min_cash_on_cash'] * 100:.2f}%")
        st.caption("A deal earns a BUY only if it clears all three.")


# -----------------------------------------------------------------------------
# RentCast lookup results (rendered in the main area; populated by the sidebar)
# -----------------------------------------------------------------------------
render_rentcast_panel()


# -----------------------------------------------------------------------------
# The deal input form
# -----------------------------------------------------------------------------
st.subheader("Deal inputs")
st.text_input("Deal name", key="name")

# Import from documents (AI) — pre-fills the rent roll and expenses below for review.
_render_document_import()

# Property type sets the expense template (Phase 12). Changing it loads that type's
# expense defaults; it changes nothing else in the underwriting math.
property_type = st.selectbox(
    "Property type",
    options=list(engine.PROPERTY_TYPE_LABELS.keys()),
    format_func=lambda t: engine.PROPERTY_TYPE_LABELS[t],
    key="property_type",
    help="Residential only for now. Configures which expense line items show and their "
         "default values — the NOI / cap / DSCR / IRR math is identical for every type.",
)
# When the type changes, load its template defaults BEFORE the expense widgets are
# created (so they pick up the new values). Manual edits afterward still win.
if st.session_state.get("_applied_property_type") != property_type:
    _apply_expense_template(property_type)
    # Income method follows the new type's smart default (the user can re-toggle).
    st.session_state["income_mode_label"] = (
        "Detailed (rent roll)" if engine.default_income_mode(property_type) == "detailed"
        else "Simple (single total)"
    )
    st.session_state["_applied_property_type"] = property_type

# Property inputs. The loan terms (interest rate, amortization) used to sit beside
# these; they now live together with the rest of the loan in the Financing section
# below, so the whole loan is described in one place.
with st.container(border=True):
    st.markdown('<p class="dg-card-title">Property</p>', unsafe_allow_html=True)
    prop_col1, prop_col2 = st.columns(2)
    purchase_price = prop_col1.number_input(
        "Purchase price ($)", key="purchase_price",
        min_value=0.0, step=5000.0, format="%.0f",
        help="The price you pay for the property.",
    )
    vacancy_pct = prop_col2.number_input(
        "Vacancy rate (%)", key="vacancy_pct",
        min_value=0.0, max_value=100.0, step=0.5, format="%.2f",
        help="Expected ongoing vacancy assumption — separate from any units currently "
             "marked vacant in the rent roll.",
    )

# -----------------------------------------------------------------------------
# Income (Phase A): a single gross total, or a detailed per-unit rent roll.
# -----------------------------------------------------------------------------
with st.container(border=True):
    st.markdown('<p class="dg-card-title">Income</p>', unsafe_allow_html=True)
    income_mode_label = st.radio(
        "Income input mode", ["Detailed (rent roll)", "Simple (single total)"],
        key="income_mode_label", horizontal=True, label_visibility="collapsed",
        help="Rent roll sums per-unit rents (a vacant unit counts as $0); Simple takes one "
             "annual gross number. Defaults to rent roll for multifamily.",
    )
    income_detailed = income_mode_label.startswith("Detailed")

    if income_detailed:
        st.caption("Add or remove unit rows. A vacant unit stays listed but contributes $0 "
                   "to current income. Set a unit's **market rent** above its current rent to "
                   "model renovation upside.")
        roll_df = pd.DataFrame(st.session_state["rent_roll"], columns=RENT_ROLL_COLUMNS)
        edited_roll = st.data_editor(
            roll_df, key="rent_roll_editor", num_rows="dynamic", hide_index=True,
            use_container_width=True,
            column_config={
                "label": st.column_config.TextColumn("Unit"),
                "unit_type": st.column_config.TextColumn("Type (bd/ba)"),
                "square_footage": st.column_config.NumberColumn("SqFt", min_value=0, step=10),
                "monthly_rent": st.column_config.NumberColumn("Rent/mo", min_value=0, step=25, format="$%d"),
                "market_rent": st.column_config.NumberColumn(
                    "Market rent/mo", min_value=0, step=25, format="$%d",
                    help="Achievable rent after renovation; leave blank if already at market."),
                "occupied": st.column_config.CheckboxColumn("Occupied"),
            },
        )
        rent_roll = _clean_rent_roll(edited_roll)
        roll_summary = engine.summarize_rent_roll(rent_roll)
        gross_rental_income = roll_summary["annual_gross_rental_income"]
        number_of_units = roll_summary["unit_count"]
        # Reconcile: in detailed mode the rent roll defines the unit count (used by per-unit
        # reserves), so derive number_of_units from it rather than a separate field.
        st.session_state["number_of_units"] = max(number_of_units, 1)

        # Split across two rows so the dollar figures get enough width to show in full
        # (a single 5-column row truncated "Annual gross" to "$102,...").
        count_totals = st.columns(3)
        count_totals[0].metric("Units", roll_summary["unit_count"])
        count_totals[1].metric("Occupied", f"{roll_summary['occupied_count']}/{roll_summary['unit_count']}")
        count_totals[2].metric("Occupancy", f"{roll_summary['physical_occupancy'] * 100:.0f}%")
        rent_totals = st.columns(2)
        rent_totals[0].metric("Monthly rent (occupied)", _money(roll_summary["occupied_monthly_rent"]))
        rent_totals[1].metric("Annual gross", _money(gross_rental_income))

        st.markdown("**Value-add** (renovate units toward market rent)")
        va_col1, va_col2 = st.columns(2)
        renovation_cost_per_unit = va_col1.number_input(
            "Renovation cost ($/unit)", key="renovation_cost_per_unit_input",
            min_value=0.0, step=1000.0, format="%.0f",
            help="Capex per unit to renovate it to market rent.",
        )
        renovation_pace = va_col2.number_input(
            "Renovation pace (units/yr)", key="renovation_pace_input",
            min_value=0.0, max_value=100.0, step=1.0, format="%.1f",
            help="Units renovated per year, e.g. as leases turn. 0 = no renovation plan.",
        )

        # Warn when an occupied unit's current rent is already at/above its market rent (a
        # market rent IS set, but there's no upside to capture) — otherwise the flat pro
        # forma looks like a bug. Blank/zero market rent = no upside modeled, so no warning.
        no_upside_units = [
            unit for unit in rent_roll
            if unit.get("occupied", True)
            and unit.get("market_rent")  # market rent set and non-zero
            and (unit.get("monthly_rent") or 0.0) >= unit["market_rent"]
        ]
        if no_upside_units:
            shown = ", ".join(str(unit.get("label") or "?") for unit in no_upside_units[:6])
            more = f" and {len(no_upside_units) - 6} more" if len(no_upside_units) > 6 else ""
            st.warning(
                f"⚠️ {len(no_upside_units)} occupied unit(s) ({shown}{more}) have a current rent "
                "**at or above** their market rent, so there's no renovation upside to capture — "
                "the value-add ramp won't do anything for them. Double-check that the current and "
                "market rents were entered correctly."
            )
    else:
        income_col1, income_col2 = st.columns(2)
        gross_rental_income = income_col1.number_input(
            "Gross rental income ($/yr)", key="gross_rental_income",
            min_value=0.0, step=1000.0, format="%.0f",
            help="Total annual rent if every unit is occupied.",
        )
        number_of_units = income_col2.number_input(
            "Number of units", key="number_of_units",
            min_value=1, max_value=50, step=1,
            help="Unit count — used for per-unit replacement reserves.",
        )
        rent_roll = None
        # Value-add needs a rent roll; in simple mode carry the stored values (unused).
        renovation_cost_per_unit = st.session_state.get("renovation_cost_per_unit_input", 0.0)
        renovation_pace = st.session_state.get("renovation_pace_input", 0.0)

with st.container(border=True):
    st.markdown('<p class="dg-card-title">Operating expenses</p>', unsafe_allow_html=True)
    expense_mode_label = st.radio(
        "Expense input mode", ["Detailed (line items)", "Simple (single total)"],
        key="expense_mode_label", horizontal=True, label_visibility="collapsed",
        help="Detailed builds the total from standard line items; Simple takes one number "
             "so a quick look doesn't need six fields.",
    )
    expense_detailed = expense_mode_label.startswith("Detailed")

    if expense_detailed:
        ecol1, ecol2, ecol3 = st.columns(3)
        ecol1.number_input(
            "Property taxes (% of price)", key="property_tax_pct",
            min_value=0.0, max_value=10.0, step=0.05, format="%.2f",
        )
        ecol2.number_input(
            "Management (% of income)", key="management_pct_ui",
            min_value=0.0, max_value=30.0, step=0.5, format="%.1f",
        )
        ecol3.number_input(
            "Repairs (% of income)", key="repairs_pct_ui",
            min_value=0.0, max_value=30.0, step=0.5, format="%.1f",
        )
        ecol4, ecol5, ecol6 = st.columns(3)
        ecol4.number_input(
            "Insurance ($/yr)", key="insurance_annual",
            min_value=0.0, step=250.0, format="%.0f",
        )
        ecol5.number_input(
            "Utilities, owner-paid ($/yr)", key="utilities_annual",
            min_value=0.0, step=250.0, format="%.0f",
        )
        ecol6.number_input(
            "Reserves ($/unit/yr)", key="reserves_per_unit",
            min_value=0.0, step=50.0, format="%.0f",
            help="Multiplied by the number of units above.",
        )
        if property_type == "condo_townhome":
            st.number_input(
                "HOA fees ($/yr)", key="hoa_annual",
                min_value=0.0, step=300.0, format="%.0f",
                help="Condo/townhome HOA dues — the major line item for this type.",
            )
            st.caption("Condo defaults assume the HOA covers the structure, so owner-paid "
                       "**insurance and maintenance are intentionally low** to avoid double-counting "
                       "(the HOA fee already includes master insurance and exterior upkeep).")
        st.caption("The total operating expenses and the expense ratio appear in the results below.")
    else:
        st.number_input(
            "Operating expenses, total ($/yr)", key="operating_expenses",
            min_value=0.0, step=1000.0, format="%.0f",
            help="A single all-in operating-expense number for a quick look.",
        )

# Assemble the deal so far (income + expenses). Financing (loan terms + amount) and
# the hold & exit assumptions are added below. NOI must be known before the loan can
# be sized, and NOI doesn't depend on the loan, so the loan terms are set in the
# Financing section rather than here.
current_deal = {
    "name": st.session_state["name"],
    "property_type": property_type,
    "purchase_price": purchase_price,
    "number_of_units": int(number_of_units),
    "income_mode": "detailed" if income_detailed else "simple",
    "gross_rental_income": gross_rental_income,
    "rent_roll": rent_roll,
    "vacancy_rate": vacancy_pct / 100.0,
    "expense_mode": "detailed" if expense_detailed else "simple",
    "operating_expenses": st.session_state["operating_expenses"],
    "property_tax_rate": st.session_state["property_tax_pct"] / 100.0,
    "insurance_annual": st.session_state["insurance_annual"],
    "hoa_annual": st.session_state["hoa_annual"],
    "management_pct": st.session_state["management_pct_ui"] / 100.0,
    "repairs_pct": st.session_state["repairs_pct_ui"] / 100.0,
    "utilities_annual": st.session_state["utilities_annual"],
    "reserves_per_unit": st.session_state["reserves_per_unit"],
    # Value-add (Phase C): renovate units toward market rent (market rents live in the
    # rent roll). No upside / no pace -> behaves like the flat-growth pro forma.
    "renovation_cost_per_unit": renovation_cost_per_unit,
    "renovation_pace": renovation_pace,
}
# NOI now, so the loan can be sized against it (Phase B). All math via the engine.
_gross_now = engine.resolve_gross_rental_income(current_deal)
noi_now = engine.calculate_noi(
    _gross_now, current_deal["vacancy_rate"],
    engine.build_operating_expenses(
        current_deal, _gross_now * (1 - current_deal["vacancy_rate"]))["total"],
)

# -----------------------------------------------------------------------------
# Financing (Phase B): the whole loan in one place — the manual-vs-sized mode, the
# interest rate and amortization, and either the loan/down payment or the LTV/DSCR
# limits to size it from. (Interest rate and amortization used to sit in a separate
# "Loan terms" box by Property; they're consolidated here.)
# -----------------------------------------------------------------------------
with st.container(border=True):
    st.markdown('<p class="dg-card-title">Financing</p>', unsafe_allow_html=True)
    financing_mode_label = st.radio(
        "Financing mode",
        ["Manual (enter loan / down payment)", "Size the loan (commercial)"],
        key="financing_mode_label", horizontal=True, label_visibility="collapsed",
        help="Manual: type the loan and down payment. Sized: the loan is the smaller of "
             "the max-LTV and min-DSCR limits, using this deal's NOI.",
    )
    financing_sized = financing_mode_label.startswith("Size")

    # Loan terms apply to both modes (and the sizing below needs them), so they come first.
    rate_col, amort_col = st.columns(2)
    interest_pct = rate_col.number_input(
        "Interest rate (%)", key="interest_pct",
        min_value=0.0, max_value=30.0, step=0.125, format="%.3f",
        help="Annual fixed mortgage rate.",
    )
    amortization_years = amort_col.number_input(
        "Amortization (years)", key="amortization_years",
        min_value=1, max_value=40, step=1,
        help="Years to fully pay the loan off.",
    )
    current_deal["annual_interest_rate"] = interest_pct / 100.0
    current_deal["amortization_years"] = int(amortization_years)

    if financing_sized:
        fin_col1, fin_col2 = st.columns(2)
        ltv_max = fin_col1.number_input(
            "Max LTV (%)", key="ltv_max_pct",
            min_value=1.0, max_value=100.0, step=1.0, format="%.1f",
            help="Loan can't exceed this share of the purchase price.",
        ) / 100.0
        dscr_min = fin_col2.number_input(
            "Min DSCR (x)", key="dscr_min_input",
            min_value=1.0, max_value=2.0, step=0.05, format="%.2f",
            help="NOI must cover annual debt service at least this many times.",
        )
        sizing = engine.size_loan(
            purchase_price, noi_now, current_deal["annual_interest_rate"],
            current_deal["amortization_years"], ltv_max, dscr_min,
        )
        loan_amount = sizing["sized_loan"]
        down_payment = sizing["down_payment"]
        st.caption(f"NOI used for sizing: **{_money(noi_now)}**  ·  binding constraint: "
                   f"**{sizing['binding_constraint']}** (the loan is the smaller of the two limits).")
        size_cols = st.columns(4)
        size_cols[0].metric("LTV-constrained loan", _money(sizing["ltv_loan"]))
        size_cols[1].metric("DSCR-constrained loan", _money(sizing["dscr_loan"]))
        size_cols[2].metric("Sized loan", _money(sizing["sized_loan"]))
        size_cols[3].metric("Down payment", _money(sizing["down_payment"]))
        result_cols = st.columns(2)
        result_cols[0].metric(
            "Resulting DSCR",
            f"{sizing['resulting_dscr']:.2f}x" if sizing["resulting_dscr"] is not None else "n/a",
        )
        result_cols[1].metric("Resulting LTV", f"{sizing['resulting_ltv'] * 100:.1f}%")
    else:
        fin_col1, fin_col2 = st.columns(2)
        loan_amount = fin_col1.number_input(
            "Loan amount ($)", key="loan_amount",
            min_value=0.0, step=5000.0, format="%.0f",
            help="Amount borrowed (principal).",
        )
        down_payment = fin_col2.number_input(
            "Down payment ($)", key="down_payment",
            min_value=0.0, step=5000.0, format="%.0f",
            help="Your out-of-pocket cash going in.",
        )
        ltv_max = st.session_state.get("ltv_max_pct", 75.0) / 100.0
        dscr_min = st.session_state.get("dscr_min_input", 1.25)

    current_deal["financing_mode"] = "sized" if financing_sized else "manual"
    current_deal["loan_amount"] = loan_amount
    current_deal["down_payment"] = down_payment
    current_deal["ltv_max"] = ltv_max
    current_deal["dscr_min"] = dscr_min

with st.container(border=True):
    st.markdown('<p class="dg-card-title">Hold &amp; exit assumptions</p>', unsafe_allow_html=True)
    hold_col, rentg_col, expg_col, exitcap_col, sellcost_col = st.columns(5)
    hold_period_years = hold_col.number_input(
        "Hold (yrs)", key="hold_period_years",
        min_value=1, max_value=40, step=1,
        help="How many years you plan to own before selling.",
    )
    rent_growth_pct = rentg_col.number_input(
        "Rent growth (%/yr)", key="rent_growth_pct",
        min_value=-20.0, max_value=20.0, step=0.25, format="%.2f",
        help="Annual growth in gross rent.",
    )
    expense_growth_pct = expg_col.number_input(
        "Expense growth (%/yr)", key="expense_growth_pct",
        min_value=-20.0, max_value=20.0, step=0.25, format="%.2f",
        help="Annual growth in operating expenses.",
    )
    exit_cap_pct = exitcap_col.number_input(
        "Exit cap rate (%)", key="exit_cap_pct",
        min_value=0.10, max_value=30.0, step=0.10, format="%.2f",
        help="Cap rate a buyer pays at sale: sale price = final-year NOI / exit cap rate.",
    )
    sell_cost_pct = sellcost_col.number_input(
        "Selling costs (%)", key="sell_cost_pct",
        min_value=0.0, max_value=20.0, step=0.5, format="%.2f",
        help="Broker + closing costs at sale, as a % of the sale price.",
    )

    # Add the hold & exit assumptions to the deal (Phase 7-8): percent inputs -> fractions.
    current_deal["hold_period_years"] = int(hold_period_years)
    current_deal["rent_growth"] = rent_growth_pct / 100.0
    current_deal["expense_growth"] = expense_growth_pct / 100.0
    current_deal["exit_cap_rate"] = exit_cap_pct / 100.0
    current_deal["selling_cost_pct"] = sell_cost_pct / 100.0

    # Market trends (Phase 11): OPT-IN, a single extra API call, cached by zip. Placed
    # next to the rent-growth input so real history sits beside the user's assumption.
    with st.expander("Market trends from RentCast (uses 1 API call)"):
        st.caption("Optional: compare your rent-growth assumption to the zip's recent "
                   "history. Separate from the address lookup, cached per zip, never automatic.")
        st.session_state.setdefault("_trend_zip", _zip_from_address(st.session_state.get("_rc_address", "")))
        zip_col, btn_col = st.columns([2, 1])
        trend_zip = zip_col.text_input("Zip code", key="_trend_zip", max_chars=5,
                                       placeholder="5-digit zip")
        fetch_trends = btn_col.button("Get market trends", key="trends_btn", use_container_width=True)
        if fetch_trends:
            cleaned_zip = (trend_zip or "").strip()
            if not (cleaned_zip.isdigit() and len(cleaned_zip) == 5):
                st.warning("Enter a 5-digit zip code.")
            else:
                with st.spinner("Asking RentCast for market history…"):
                    st.session_state["_trends"] = market_trends(cleaned_zip)  # 1 call, cached by zip
                st.session_state["_trends_zip"] = cleaned_zip
        _render_market_trends()


# -----------------------------------------------------------------------------
# Action buttons: run the underwriting, or save the deal
# -----------------------------------------------------------------------------
run_col, save_col = st.columns(2)
run_clicked = run_col.button(
    "Run underwriting", key="run_btn", type="primary", use_container_width=True
)
save_clicked = save_col.button("Save deal", key="save_btn", use_container_width=True)

if run_clicked:
    # Snapshot the inputs so the results stay put until the next run.
    st.session_state["_results_deal"] = current_deal
    st.session_state["_show_results"] = True
    st.session_state.pop("_memo_narrative", None)   # new underwrite -> any prior memo is stale

if save_clicked:
    if not current_deal["name"].strip():
        st.error("Give the deal a name before saving.")
    else:
        new_id = database.save_deal(current_deal, owner=USER_EMAIL)  # tagged to this user
        st.session_state["_flash"] = f"Saved “{current_deal['name']}” as deal #{new_id}."
        st.rerun()


# -----------------------------------------------------------------------------
# Results
# -----------------------------------------------------------------------------
def _verdict_banner(text, color, subtitle):
    """Full-width, rounded BUY/PASS banner in the design's semantic green/red with
    legible cream text (per DESIGN.md — never a bright/alarming red)."""
    st.markdown(
        f"<div style='padding:0.95rem 1.25rem;border-radius:12px;background:{color};"
        f"color:#FBFAF7;text-align:center;font-size:1.6rem;font-weight:700;"
        f"letter-spacing:0.14em'>{text}</div>",
        unsafe_allow_html=True,
    )
    st.caption(subtitle)


def render_results(deal):
    """Show the metrics, the buy/pass verdict, the checks, and the breakdowns."""
    # Fill in hold/exit assumptions if missing (e.g. a deal loaded from the database,
    # whose schema doesn't store them) so the multi-year engine calls always work.
    deal = {**_assumption_defaults(), **deal}

    try:
        results = compute_metrics(deal)
    except ZeroDivisionError:
        st.error(
            "Can't compute the ratios when purchase price, loan amount, or down "
            "payment is zero. Adjust those inputs and run again."
        )
        return

    evaluation = results["evaluation"]
    verdict = evaluation["verdict"]

    # Full-hold projection via the engine (never re-implemented here). Guarded so a
    # pathological input can't crash the page — the year-one results still render.
    try:
        pro_forma = engine.build_pro_forma(deal)
        exit_result = engine.calculate_exit(deal)
        multi_year_ok = True
    except (ZeroDivisionError, ValueError):
        pro_forma, exit_result, multi_year_ok = None, None, False

    # Value-add summary (Phase C) — computed once, reused by the deal memo and the
    # value-add section below. Guarded like the projection above.
    try:
        value_add = engine.value_add_summary(deal)
    except (ZeroDivisionError, ValueError):
        value_add = None

    st.divider()
    st.subheader(f"Results — {deal['name']}")

    # Overall verdict: the ONLY place the words BUY / PASS appear. Design semantic
    # colors — deep green for BUY, muted red for PASS (never a bright/alarming red).
    if verdict == "BUY":
        _verdict_banner("BUY", "#2E7D4F", "Clears every threshold below.")
    else:
        _verdict_banner("PASS", "#B23A3A", "Below one or more thresholds — see the checks below.")

    # Year-one headline metrics as labeled numbers.
    metric_cols = st.columns(4)
    metric_cols[0].metric("NOI (annual)", _money(results["noi"]))
    metric_cols[1].metric("Cap rate", f"{results['cap_rate'] * 100:.2f}%")
    metric_cols[2].metric("DSCR", f"{results['dscr']:.2f}x")
    metric_cols[3].metric("Cash-on-cash", f"{results['cash_on_cash'] * 100:.2f}%")

    # Full-hold return metrics, right below the year-one numbers (still up top).
    hold_years = int(deal["hold_period_years"])
    st.caption(f"Full-hold returns ({hold_years}-year)")
    return_cols = st.columns(2)
    if multi_year_ok and exit_result["irr_ok"]:
        return_cols[0].metric("IRR (annualized)", f"{exit_result['irr'] * 100:.1f}%")
    else:
        return_cols[0].metric("IRR (annualized)", "n/a")
    if multi_year_ok:
        return_cols[1].metric("Equity multiple", f"{exit_result['equity_multiple']:.2f}x")
    else:
        return_cols[1].metric("Equity multiple", "n/a")
    # Plain-English note in place of the IRR number when there's no real IRR.
    if multi_year_ok and not exit_result["irr_ok"]:
        st.caption(f"No real IRR — {exit_result['irr_reason']}")
    elif not multi_year_ok:
        st.caption("Couldn't project the hold from these inputs — check the hold/exit assumptions.")

    # Per-threshold checks. Each reads MEETS / BELOW — never "PASS", so the word
    # PASS only ever means "decline the deal" (the verdict above).
    st.markdown("**Threshold checks**")
    check_rows = []
    for check in evaluation["checks"]:
        value = _format_metric(check["value"], check["display"])
        minimum = _format_metric(check["minimum"], check["display"])
        result = "MEETS" if check["passed"] else "BELOW"
        check_rows.append([check["label"], value, f"min {minimum}", result])
    st.markdown(_markdown_table(["Check", "Value", "Threshold", "Result"], check_rows))

    # Income and financing breakdowns, side by side.
    income_col, financing_col = st.columns(2)

    with income_col:
        st.markdown("**Income**")
        gross = deal["gross_rental_income"]
        vacancy_pct_display = deal["vacancy_rate"] * 100
        st.markdown(_markdown_table(["Item", "Amount"], [
            ["Gross rental income (100% occ.)", _money(gross)],
            [f"Less vacancy ({vacancy_pct_display:.1f}%)", _money(-results["vacancy_loss"])],
            ["Effective gross income", _money(results["effective_gross_income"])],
            ["Less operating expenses", _money(-results["operating_expenses"])],
            ["Net operating income (NOI)", _money(results["noi"])],
        ]))

    with financing_col:
        st.markdown("**Financing**")
        loan_label = "Loan amount"
        if results.get("sizing"):
            loan_label = f"Loan amount (sized, {results['sizing']['binding_constraint']}-bound)"
        st.markdown(_markdown_table(["Item", "Amount"], [
            ["Purchase price", _money(deal["purchase_price"])],
            [loan_label, _money(results["loan_amount"])],
            ["Down payment (cash in)", _money(results["down_payment"])],
            ["Interest / amortization",
             f"{deal['annual_interest_rate'] * 100:.3f}% / {int(deal['amortization_years'])} yrs"],
            ["Monthly payment", _money(results["monthly_payment"])],
            ["Annual debt service", _money(results["annual_debt_service"])],
            ["Annual cash flow (NOI − debt)", _money(results["annual_cash_flow"])],
        ]))

    # Operating-expense breakdown + expense-ratio sanity flag (Phase 9).
    expense_result = results["expense_result"]
    st.markdown("**Operating expenses**")
    expense_rows = [[line["name"], line["basis"], _money(line["amount"])]
                    for line in expense_result["lines"]]
    expense_rows.append(["**Total**", "", f"**{_money(expense_result['total'])}**"])
    st.markdown(_markdown_table(["Line item", "Basis", "Amount"], expense_rows))

    ratio = expense_result["expense_ratio"]
    ratio_text = f"{ratio * 100:.1f}%"
    if ratio < engine.IMPLAUSIBLY_LOW_EXPENSE_RATIO:
        st.warning(
            f"⚠️ Expense ratio **{ratio_text}** looks implausibly low. Small multifamily "
            "usually runs ~35-50% of effective gross income — double-check the expense inputs."
        )
    elif ratio < engine.TYPICAL_EXPENSE_RATIO_LOW:
        st.caption(f"Expense ratio {ratio_text} — a touch below the typical 35-50% band.")
    else:
        st.caption(f"Expense ratio {ratio_text} — typical small multifamily runs ~35-50%.")

    # ----- Deal memo (Phase E) — built from the engine's numbers, AI writes the prose -----
    # Placed before the multi-year early-return so the memo is available even when the
    # hold projection can't be computed (the memo handles a missing exit gracefully).
    memo_data = deal_memo.build_memo_data(
        deal, results, exit_result if multi_year_ok else None, value_add
    )
    _render_deal_memo(memo_data)

    # ----- Multi-year detail (Phase 7 pro forma + Phase 8 exit), below year-one -----
    if not multi_year_ok:
        return

    st.divider()
    st.markdown(f"### Full-hold detail ({hold_years}-year)")

    # Exit breakdown: sale price, selling costs, loan payoff, net proceeds.
    st.markdown("**Exit — sale at end of hold**")
    exit_cap_display = deal["exit_cap_rate"] * 100
    selling_cost_display = deal["selling_cost_pct"] * 100
    st.markdown(_markdown_table(["Item", "Amount"], [
        [f"Sale price (yr {hold_years} NOI / {exit_cap_display:.2f}% cap)",
         _money(exit_result["sale_price"])],
        [f"Less selling costs ({selling_cost_display:.1f}%)",
         _money(-exit_result["selling_costs"])],
        ["Less loan payoff", _money(-exit_result["ending_loan_balance"])],
        ["Net sale proceeds", _money(exit_result["net_sale_proceeds"])],
    ]))

    # Year-by-year pro forma table.
    st.markdown(f"**{hold_years}-year pro forma**")
    pro_forma_rows = []
    for row in pro_forma:
        pro_forma_rows.append({
            "Year": row["year"],
            "Gross rent": _money(row["gross_rental_income"]),
            "NOI": _money(row["noi"]),
            "Debt service": _money(row["annual_debt_service"]),
            "Cash flow": _money(row["annual_cash_flow"]),
            "End loan bal": _money(row["ending_loan_balance"]),
        })
    st.dataframe(pro_forma_rows, hide_index=True, use_container_width=True)

    # Annual cash flow chart — years 1..N, with the year-N sale spike included.
    st.markdown("**Annual cash flow by year**")
    stream = exit_result["cash_flow_stream"]  # [year0 = -down payment, cf1, ..., cfN + sale]
    chart_data = {
        "Year": list(range(1, len(stream))),
        "Cash flow": stream[1:],
    }
    st.bar_chart(chart_data, x="Year", y="Cash flow")
    st.caption(f"Year {hold_years} spikes because the net sale proceeds "
               f"({_money(exit_result['net_sale_proceeds'])}) land on top of that year's "
               "operating cash flow.")

    # ----- Value-add (Phase C): rent ramp + value creation, only if there's upside -----
    # (value_add was already computed above and reused by the deal memo.)
    if value_add and value_add["has_value_add"]:
        st.divider()
        st.markdown("### Value-add — forcing NOI up forces value up")
        va_cols = st.columns(4)
        va_cols[0].metric("Going-in NOI", _money(value_add["going_in_noi"]))
        va_cols[1].metric("Stabilized NOI", _money(value_add["stabilized_noi"]),
                          delta=_money(value_add["noi_lift"]))
        va_cols[2].metric("Implied value gain", _money(value_add["value_gain"]))
        va_cols[3].metric("Renovation cost", _money(value_add["total_renovation_cost"]))
        st.caption(
            f"{value_add['num_upside_units']} units with upside · stabilizes in year "
            f"{value_add['years_to_stabilize']} · value {_money(value_add['going_in_value'])} → "
            f"{_money(value_add['stabilized_value'])} at the {deal['exit_cap_rate'] * 100:.2f}% exit "
            "cap (stabilized NOI ÷ exit cap, in today's dollars)."
        )
        st.markdown("**Rent ramp**")
        ramp_rows = [{
            "Year": row["year"],
            "Renovated": f"{row['units_renovated_cumulative']}/{row['num_upside_units']}",
            "+ this yr": row["units_renovated_this_year"],
            "Gross rent": _money(row["gross_rental_income"]),
            "Reno capex": _money(row["renovation_capex"]),
            "NOI": _money(row["noi"]),
            "Cash flow": _money(row["annual_cash_flow"]),
        } for row in pro_forma]
        st.dataframe(ramp_rows, hide_index=True, use_container_width=True)

    # ----- Sensitivity: IRR across exit cap rate (cols) and rent growth (rows) -----
    st.divider()
    st.markdown("### Sensitivity — IRR")
    st.caption("Each cell is the IRR if those two assumptions held — exit cap rate "
               "across the columns, rent growth down the rows. Greener = higher IRR; "
               "a cell with no real IRR shows n/a.")

    sensitivity = engine.run_sensitivity(deal)  # default axes: exit cap vs rent growth
    col_labels = [f"{v * 100:.2f}%" for v in sensitivity["col_values"]]   # exit cap rate
    row_labels = [f"{v * 100:.1f}%" for v in sensitivity["row_values"]]   # rent growth
    # IRR as percent; None -> NaN so the gradient skips it and we render it as "n/a".
    numeric_grid = [[float("nan") if irr is None else irr * 100 for irr in row]
                    for row in sensitivity["irr_grid"]]
    grid_df = pd.DataFrame(numeric_grid, index=row_labels, columns=col_labels)
    grid_df.index.name = "rent growth ↓ / exit cap →"

    flat = [value for row in numeric_grid for value in row if not pd.isna(value)]
    vmin, vmax = (min(flat), max(flat)) if flat else (0.0, 0.0)
    styled = (grid_df.style
              .format(lambda v: "n/a" if pd.isna(v) else f"{v:.1f}%")
              .map(lambda v: _heat_color(v, vmin, vmax)))
    st.dataframe(styled, use_container_width=True)


if st.session_state.get("_show_results") and "_results_deal" in st.session_state:
    render_results(st.session_state["_results_deal"])
else:
    st.info("Fill in the deal and click **Run underwriting** to see the metrics and verdict.")
