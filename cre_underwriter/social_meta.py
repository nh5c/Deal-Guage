"""Social-preview (Open Graph / Twitter Card) metadata for DealGauge.

WHY THIS EXISTS
---------------
When someone pastes https://www.dealgaugecre.com into iMessage, Slack, LinkedIn, etc.,
that service fetches the page's raw HTML over plain HTTP and reads the <head> to build a
rich link preview. It never runs JavaScript. So `st.set_page_config(page_title=...,
page_icon=...)` — which sets the browser-tab title and favicon *client-side, after the
React app boots* — is completely invisible to these link-unfurlers.

What they actually see is Streamlit's static index.html, whose <head> ships with
`<title>Streamlit</title>`, Streamlit's own favicon, and NO Open Graph tags. That is why
the preview shows the Streamlit logo and generic text.

Streamlit has no native API for setting <head>/OG tags, so we inject them ourselves into
that static index.html. We do it at startup, idempotently, so the tags are re-applied on
every deploy/boot and automatically survive a Streamlit upgrade (an upgrade would ship a
fresh, unpatched index.html; our startup patch simply re-applies to whatever is installed).

THE PREVIEW IMAGE MUST BE PUBLICLY FETCHABLE
--------------------------------------------
An unfurler is an anonymous bot. The app itself sits behind Google sign-in (st.login), so
we must NOT serve the preview image from the app — the bot would get the login page, not
the image. Instead we point at the copy committed to this PUBLIC GitHub repo, served by
raw.githubusercontent.com, which any bot can reach with no auth. If the repo is ever made
private or renamed, update _RAW below (or host the images somewhere else public).
"""

import re
from html import escape
from pathlib import Path

# --- What the preview should say ---------------------------------------------------------
SITE_URL = "https://www.dealgaugecre.com"
TITLE = "DealGauge"
DESCRIPTION = (
    "Commercial real estate underwriting — turn offering memos into "
    "institutional-grade analysis in minutes."
)

# Public, no-auth image URLs (this repo is public). raw.githubusercontent.com serves the
# committed PNGs with the correct image/* content type, which unfurlers require.
_RAW = "https://raw.githubusercontent.com/nh5c/Deal-Guage/main/assets"
IMAGE_URL = f"{_RAW}/dealgauge-og.png"     # 1200x630 social card
MARK_URL = f"{_RAW}/dealgauge-mark.png"    # square gauge mark, used as the static favicon

# Markers so we can find and update our own block on re-runs without duplicating it.
_MARKER_START = "<!-- dealgauge:social-meta -->"
_MARKER_END = "<!-- /dealgauge:social-meta -->"

# The exact default strings Streamlit's index.html ships with (see streamlit static build).
_DEFAULT_TITLE = "<title>Streamlit</title>"
_DEFAULT_FAVICON = '<link rel="shortcut icon" href="./favicon.png" />'


def _meta_block():
    """The <head> tags to inject: standard meta description, Open Graph (Facebook, iMessage,
    LinkedIn, Slack) and Twitter Card. content= values are HTML-attribute-escaped."""
    title = escape(TITLE, quote=True)
    desc = escape(DESCRIPTION, quote=True)
    image = escape(IMAGE_URL, quote=True)
    url = escape(SITE_URL, quote=True)
    tags = [
        _MARKER_START,
        f'<meta name="description" content="{desc}" />',
        '<meta property="og:type" content="website" />',
        f'<meta property="og:site_name" content="{title}" />',
        f'<meta property="og:title" content="{title}" />',
        f'<meta property="og:description" content="{desc}" />',
        f'<meta property="og:url" content="{url}" />',
        f'<meta property="og:image" content="{image}" />',
        '<meta property="og:image:type" content="image/png" />',
        '<meta property="og:image:width" content="1200" />',
        '<meta property="og:image:height" content="630" />',
        f'<meta property="og:image:alt" content="{title}" />',
        '<meta name="twitter:card" content="summary_large_image" />',
        f'<meta name="twitter:title" content="{title}" />',
        f'<meta name="twitter:description" content="{desc}" />',
        f'<meta name="twitter:image" content="{image}" />',
        _MARKER_END,
    ]
    # Indent to sit tidily inside <head>.
    return "\n    ".join(tags)


def index_html_path():
    """Absolute path to the installed Streamlit's static index.html (the app shell that
    every plain HTTP GET to the site returns)."""
    import streamlit

    return Path(streamlit.__file__).resolve().parent / "static" / "index.html"


def patch_streamlit_index_html():
    """Inject DealGauge's social-preview <head> tags into Streamlit's static index.html,
    and swap the default <title>/favicon so the raw HTML is DealGauge too.

    Idempotent — safe to call on every startup. Never raises: if the file is missing or
    unwritable, the app must still run, so we return False instead of crashing.

    Returns True if the served index.html now carries our tags, False otherwise.
    """
    try:
        path = index_html_path()
        original = path.read_text(encoding="utf-8")
    except OSError:
        return False

    text = original
    block = _meta_block()

    # 1) Insert or refresh our meta block. If a previous block exists (same or older
    #    content), replace it in place; otherwise insert it just before </head>.
    existing = re.compile(
        re.escape(_MARKER_START) + r".*?" + re.escape(_MARKER_END), re.DOTALL
    )
    if existing.search(text):
        text = existing.sub(block, text)
    elif "</head>" in text:
        text = text.replace("</head>", f"    {block}\n  </head>", 1)
    else:
        return False  # unrecognized shell; leave it untouched

    # 2) Make the raw-HTML <title> and favicon DealGauge as well. set_page_config still
    #    owns the live tab once React boots; this covers unfurlers and the pre-React paint.
    text = text.replace(_DEFAULT_TITLE, f"<title>{escape(TITLE)}</title>", 1)
    text = text.replace(_DEFAULT_FAVICON,
                        f'<link rel="shortcut icon" href="{escape(MARK_URL, quote=True)}" />', 1)

    if text == original:
        return True  # already fully patched — nothing to write

    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


if __name__ == "__main__":
    # Runnable at container boot (e.g. prepend to the Render start command:
    #   python -m cre_underwriter.social_meta && streamlit run cre_underwriter/dashboard.py ...)
    # so the very first HTTP GET on a cold container already serves the patched HTML.
    ok = patch_streamlit_index_html()
    print(("patched: " if ok else "could NOT patch: ") + str(index_html_path()))
