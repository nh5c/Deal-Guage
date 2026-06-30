# DESIGN.md — DealGauge Visual Design System

This document is the single source of truth for DealGauge's look and feel. Any UI work
should follow it so the app stays visually consistent. DealGauge is a professional
commercial real estate underwriting tool — the design should read as a serious fintech
/ analytics product: clean, restrained, trustworthy, and uncluttered. Avoid anything
that looks like a default template or a generic AI app (no emoji as UI icons, no
unstyled default components, no clutter).

## Brand palette

Use these exact values. Do not introduce other accent colors.

- Ink Navy (primary):      #12182B   — primary brand color, headers, dark surfaces
- Navy 2 (primary alt):    #1A2238   — slightly lighter navy for text on light bg, borders
- Brass (accent):          #C2A35E   — the ONLY accent; use sparingly for emphasis,
                                        active states, key figures, the "Gauge" wordmark
- Cream (light surface):   #ECE6D6   — warm off-white for light surfaces / on-dark text
- Paper White:             #FBFAF7   — main app background (warm white, not pure #FFF)
- Ink Text:                #1A2238   — primary body text on light backgrounds
- Muted Text:              #5B6275   — secondary text, labels, captions
- Hairline / border:       #E2DFD6   — subtle dividers and card borders

Semantic colors for verdicts/metrics (use only for status, not decoration):
- Positive / BUY / MEETS:  #2E7D4F   (deep green)
- Negative / PASS / BELOW: #B23A3A   (muted red — NOT bright red)
- Warning / caution:       #B8860B   (dark amber, for the expense-ratio / rent warnings)

Brass is precious — when everything is brass, nothing is. Use it for at most one or two
elements per view (e.g. the key headline metric, the active toggle), not every number.

## Logo usage

Logo files live in `assets/`:
- `assets/dealgauge-logo.svg`      — primary wordmark, for LIGHT backgrounds
- `assets/dealgauge-logo-dark.svg` — wordmark on a navy panel, for dark headers/banners
- `assets/dealgauge-mark.svg`      — compact gauge mark, for favicon and tight spaces

Rules:
- App header: use the wordmark (light version on the paper background, or the dark
  lockup inside a navy header band). Size it ~36–44px tall. Give it clear space — at
  least the height of the gauge mark on all sides; never crowd it with text.
- Favicon / page icon: use `dealgauge-mark.svg`.
- Never recolor, stretch, rotate, or add effects (shadows/glows) to the logo.
- Never place the light wordmark on a busy or mid-tone background where it loses
  contrast; use the dark lockup there instead.
- Streamlit note: `st.image` may not render raw SVG cleanly. Prefer embedding the SVG
  via inline HTML (`st.markdown(svg_html, unsafe_allow_html=True)`), or fall back to a
  high-res PNG export if needed. Set the favicon via `st.set_page_config(page_icon=...)`.

## Typography

- Headings / wordmark feel: a serif for the brand wordmark only (already baked into the
  logo SVG). UI headings use a clean sans-serif.
- UI font: a modern, legible sans-serif. Preferred stack:
  `'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif`.
- Numbers / metrics: use tabular figures where possible so columns of numbers align.
- Scale (rough): page title 28–32px/700; section heading 18–20px/600; body 15–16px/400;
  caption/label 12–13px/500 in Muted Text, often uppercase with slight letter-spacing.
- Don't over-bold. Bold is for headings and the single key metric, not whole paragraphs.

## Layout & spacing

- Background: Paper White (#FBFAF7). Content sits on it in cards/panels.
- Use an 8px spacing rhythm (8 / 16 / 24 / 32). Be generous with whitespace; crowding is
  the main thing that makes a tool feel amateur.
- Group related inputs into labeled cards/panels with a subtle hairline border
  (#E2DFD6), ~12px radius, modest padding (20–24px). Property, Income, Expenses,
  Financing, Hold & Exit should each read as a clean section.
- Results: lead with the verdict and the headline metrics, then supporting detail.
- Max content width should feel like a document, not stretch edge-to-edge on wide
  monitors.

## Components

- Metric tiles: label (Muted Text, small, uppercase) above a large value (Ink Text,
  ~28px/700). The single most important metric per view may use Brass for its value.
- Verdict banner: full-width, rounded. BUY uses the green; PASS uses the muted red.
  Keep the text legible (cream/white on the color). No bright/alarming red.
- Tables (rent roll, pro forma, expenses): light hairline rows, generous cell padding,
  right-align numbers, subtle header row in a faint navy tint. No heavy gridlines.
- Toggles / segmented controls (Simple/Detailed, Manual/Sized): the active option uses
  Brass or Navy; inactive is muted. Make the active state obvious.
- Warnings (expense ratio, current≥market rent): soft amber background, dark amber text,
  small icon — informative, not alarming.
- Buttons: primary action in Ink Navy with cream text; secondary as outline. The main
  CTA ("Run underwriting") should be the clear focal action.

## Tone of the UI copy

- Plain, confident, professional. Short labels. No emoji in the interface.
- Help tooltips explain a term in one sentence a non-expert could follow.

## Do / Don't

Do: lots of whitespace, one accent color used sparingly, consistent spacing, clean
tables, a clear visual hierarchy (verdict → headline metrics → detail).
Don't: emoji icons, multiple accent colors, bright red, heavy gridlines, crowded forms,
drop shadows on the logo, pure-white backgrounds, walls of bold text.
