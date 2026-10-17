"""Water meter dashboard (Streamlit) fed by a Google Sheet."""


from __future__ import annotations

import io
import math
import re
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.colors import sample_colorscale

DATE_COL = "date_obj"
DATE_STR_COL = "date_str"
INFLOW_PREFIX = "daily_inflow"
ALL = "All"


# ----------------------------------------------------------------- config --
def get_config() -> tuple[str | None, int, int]:
    try:
        url = st.secrets["sheet"]["url_read"]
    except Exception:
        url = None
    try:
        g = st.secrets["gauge"]
        gmax, gstep = int(g.get("max", 1600)), int(g.get("step", 100))
    except Exception:
        gmax, gstep = 1600, 100
    return url, gmax, max(gstep, 1)


# ------------------------------------------------------------------- data --
@st.cache_data(ttl=900, show_spinner="Reading Google Sheet…")
def fetch_workbook(url: str) -> bytes:
    """Download the whole workbook (all month tabs) as xlsx bytes."""
    m = re.search(r"/d/([\w-]+)", url)
    if not m:
        raise ValueError("Could not find a spreadsheet id in the sheet URL.")
    export = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
    r = requests.get(export, timeout=30)
    r.raise_for_status()
    if "html" in r.headers.get("content-type", "").lower():
        raise PermissionError(
            "Google returned a login page. Share the sheet as 'Anyone with the link: Viewer'."
        )
    return r.content


def month_tabs(xls: pd.ExcelFile) -> list[tuple[str, datetime]]:
    """Worksheets named like 'Oct-2026', sorted chronologically."""
    tabs = []
    for name in xls.sheet_names:
        try:
            tabs.append((name, datetime.strptime(name.strip(), "%b-%Y")))
        except ValueError:
            continue
    return sorted(tabs, key=lambda t: t[1])


def _norm(c: object) -> str:
    return re.sub(r"\s+", "_", str(c).strip().lower())


def _to_dates(s: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    return pd.to_datetime(s, dayfirst=True, errors="coerce", format="mixed")


def load_daily(xls: pd.ExcelFile, sheet: str) -> pd.DataFrame:
    """Return one row per date: columns [date, inflow] (same-day rows summed)."""
    df = xls.parse(sheet)
    df.columns = [_norm(c) for c in df.columns]
    inflow_col = next((c for c in df.columns if c.startswith(INFLOW_PREFIX)), None)
    if inflow_col is None or (DATE_COL not in df.columns and DATE_STR_COL not in df.columns):
        return pd.DataFrame(columns=["date", "inflow"])

    dates = _to_dates(df[DATE_COL]) if DATE_COL in df.columns else pd.Series(pd.NaT, index=df.index)
    if DATE_STR_COL in df.columns:
        dates = dates.fillna(_to_dates(df[DATE_STR_COL]))
    out = pd.DataFrame(
        {"date": dates.dt.normalize(), "inflow": pd.to_numeric(df[inflow_col], errors="coerce")}
    ).dropna()
    return out.groupby("date", as_index=False)["inflow"].sum().sort_values("date")


# ----------------------------------------------------------------- charts --
def make_bar_or_line(x, y, kind: str, categorical: bool) -> go.Figure:
    if kind == "Bar":
        trace = go.Bar(
            x=x, y=y,
            marker=dict(color=y, colorscale=[[0, "#cfe3f1"], [1, "#4a86b0"]], line=dict(color="white", width=1)),
            hovertemplate="%{x}<br><b>%{y:.0f} m³</b><extra></extra>",
        )
    else:
        trace = go.Scatter(
            x=x, y=y, mode="lines+markers", fill="tozeroy",
            line=dict(color="#ff2d95", width=4, shape="spline"),
            marker=dict(size=9, color="#00c2ff", line=dict(color="white", width=2)),
            fillcolor="rgba(255,45,149,0.18)",
            hovertemplate="%{x}<br><b>%{y:.0f} m³</b><extra></extra>",
        )
    fig = go.Figure(trace)
    fig.update_layout(
        height=360, margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(255,255,255,0.65)",
        yaxis=dict(title="Inflow (m³)", gridcolor="rgba(0,0,0,0.08)"),
        xaxis=dict(title="Month" if categorical else "Date",
                   type="category" if categorical else "date",
                   tickformat=None if categorical else "%d %b"),
        dragmode=False,
    )
    return fig


def make_gauge(total: float, max_val: int, tick: int) -> go.Figure:
    """Half-circle dial with coloured segments and a real needle."""
    n = max(math.ceil(max_val / tick), 1)
    seg = 180 / n
    colors = sample_colorscale("Turbo", [i / max(n - 1, 1) for i in range(n)])
    angle = 180 * min(max(total / max_val, 0), 1)

    fig = go.Figure()
    fig.add_trace(go.Barpolar(
        theta=[seg * (i + 0.5) for i in range(n)], width=[seg] * n,
        r=[0.3] * n, base=[0.7] * n,
        marker=dict(color=colors, line=dict(color="white", width=2)),
        hoverinfo="skip",
    ))
    fig.add_trace(go.Scatterpolar(
        theta=[angle, angle], r=[0, 0.9], mode="lines",
        line=dict(color="#1b1b2f", width=6), hoverinfo="skip",
    ))
    fig.add_trace(go.Scatterpolar(
        theta=[0], r=[0], mode="markers",
        marker=dict(size=22, color="#1b1b2f", line=dict(color="white", width=3)),
        hoverinfo="skip",
    ))
    tickvals = [min(i * tick / max_val * 180, 180) for i in range(n + 1)]
    ticktext = [str(min(i * tick, max_val)) for i in range(n + 1)]
    fig.update_layout(
        showlegend=False, height=270, margin=dict(l=35, r=35, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        polar=dict(
            sector=[0, 180], bgcolor="rgba(0,0,0,0)",
            radialaxis=dict(visible=False, range=[0, 1]),
            angularaxis=dict(rotation=180, direction="clockwise", tickmode="array",
                             tickvals=tickvals, ticktext=ticktext,
                             tickfont=dict(size=11), showline=False, ticks="outside"),
        ),
    )
    return fig


# --------------------------------------------------------------------- ui --
CSS = """
<style>
.stApp {background: linear-gradient(160deg,#e0f7ff 0%,#fff0f8 55%,#fffbe0 100%);}
.hero {background: linear-gradient(90deg,#00c2ff,#7b2ff7,#ff2d95); color:white;
       padding:14px 18px; border-radius:16px; margin-bottom:12px;}
.hero h1 {font-size:1.5rem; margin:0; color:white;}
.hero p {margin:2px 0 0 0; opacity:.9; font-size:.9rem;}
.total {background: linear-gradient(135deg,#00e0a4,#00a2ff); color:white; text-align:center;
        padding:14px; border-radius:16px; box-shadow:0 4px 14px rgba(0,120,255,.3);}
.total .v {font-size:2.4rem; font-weight:800; line-height:1.1;}
.total .l {font-size:.95rem; opacity:.95;}
</style>
"""


def main() -> None:
    st.set_page_config(page_title="Water Meter Dashboard", page_icon="💧", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown('<div class="hero"><h1>💧 Water Meter Dashboard</h1>'
                "<p>Daily inflows</p></div>", unsafe_allow_html=True)

    url, gauge_max, gauge_step = get_config()
    if not url:
        st.error("Add `[sheet] url = ...` to `.streamlit/secrets.toml`.")
        st.stop()

    if st.button("🔄 Update", type="primary"):
        st.cache_data.clear()
        st.rerun()

    try:
        raw = fetch_workbook(url)
        xls = pd.ExcelFile(io.BytesIO(raw))
    except PermissionError as e:  # our own message, contains no URL
        st.error(str(e))
        st.stop()
    except Exception as e:  # network / format problems; never echo the URL
        st.error(f"Could not read the Google Sheet ({type(e).__name__}). Check the sheet's sharing settings and your network.")
        st.stop()

    tabs = month_tabs(xls)
    if not tabs:
        st.warning("No worksheets named like 'Oct-2026' were found.")
        st.stop()

    names = [n for n, _ in tabs]
    with st.sidebar:
        st.header("⚙️ Options")
        choice = st.selectbox("Month", names + [ALL], index=len(names) - 1)
        kind = st.radio("Graph type", ["Bar", "Line"], horizontal=True)

    if choice == ALL:
        parts = []
        for name, _ in tabs:
            d = load_daily(xls, name)
            if not d.empty:
                parts.append((name, float(d["inflow"].sum())))
        x = [p[0] for p in parts]
        y = [p[1] for p in parts]
        categorical, label = True, "all months"
        st.subheader("Month-wise inflow")
    else:
        d = load_daily(xls, choice)
        x, y = list(d["date"]), list(d["inflow"])
        categorical, label = False, choice
        st.subheader(f"Daily inflow – {choice}")

    if not y:
        st.info(f"No inflow data found for {label} yet.")
        st.stop()

    st.plotly_chart(make_bar_or_line(x, y, kind, categorical), width="stretch",
                    config={"displayModeBar": False})

    total = float(sum(y))
    max_val = max(gauge_max, math.ceil(total / gauge_step) * gauge_step)
    tick = gauge_step * max(1, math.ceil((max_val / gauge_step) / 16))  # keep dial readable
    st.markdown(f'<div class="total"><div class="l">Total water received ({label})</div>'
                f'<div class="v">{total:,.0f} m³</div></div>', unsafe_allow_html=True)
    st.plotly_chart(make_gauge(total, max_val, tick), width="stretch",
                    config={"displayModeBar": False, "staticPlot": True})

    st.download_button("⬇️ Download xls", data=raw, file_name="water_meter.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.caption(f"Loaded {datetime.now():%d %b %Y %H:%M}. Press Update to re-read the sheet.")


if __name__ == "__main__":
    main()