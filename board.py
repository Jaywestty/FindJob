"""JobTool board (Streamlit) — all data flows through the FastAPI layer.

Run:  uv run streamlit run board.py      (API must be up: uvicorn api:app)
This app never touches jobs.db directly, so board and API run independently.
Theme lives in .streamlit/config.toml (dark + mint accent); the Dashboard
tab matches its plotly charts to the same values (ACCENT below).
"""
from __future__ import annotations

import html as _html

import requests
import streamlit as st

API = "http://127.0.0.1:8000"
# Must match primaryColor in .streamlit/config.toml.
ACCENT = "#00E5A0"

# Fixed categorical palette (dark-theme readable, hues kept apart —
# no two near-identical shades, which was the old all-greens bug).
PALETTE = {
    "mint": "#00E5A0",    # accent: progress / success-in-motion
    "amber": "#F5A524",   # attention: scheduled / mid scores
    "coral": "#FF6B6B",   # bad news: rejected
    "blue": "#4AA8FF",    # waiting: pending / kept pool
    "purple": "#B388FF",  # faded out: no response
    "gray": "#8A94A6",    # neutral base (score-gradient low end)
}
# Statuses needing hues outside the core six get their own fixed hexes:
# PINK celebrates the offer state; BLUE_GRAY carries the not_applied bulk
# (muted, recedes); CHARTREUSE marks the transient no_status sliver with
# maximum pop against its common companions. An earlier gray/white pairing
# here read as identical — both achromatic — so both are real hues now.
_OFFER_PINK = "#F472B6"
_NOT_APPLIED_BLUE_GRAY = "#546E8A"
_NO_STATUS_CHARTREUSE = "#A3E635"

# One fixed color per interview status, stable regardless of which
# statuses are present or their order. Every possible interview_breakdown
# key has an explicit entry — nothing falls back to a default. Reused for
# status pills in the Jobs tab so a status means the same color everywhere.
STATUS_COLORS = {
    "not_applied": _NOT_APPLIED_BLUE_GRAY,
    "pending": PALETTE["blue"],
    "no_response": PALETTE["purple"],
    "rejected": PALETTE["coral"],
    "interview_scheduled": PALETTE["amber"],
    "interview_completed": PALETTE["mint"],
    "offer": _OFFER_PINK,
    "no_status": _NO_STATUS_CHARTREUSE,
}

# Funnel stages in fixed progress order (mirrors the status semantics:
# inert -> pool -> action -> progress -> goal).
FUNNEL_STAGES = ["Fetched", "Kept", "Applied", "Interview", "Offer"]
FUNNEL_COLORS = [PALETTE["gray"], PALETTE["blue"], PALETTE["amber"],
                 PALETTE["mint"], _OFFER_PINK]


def _lerp_hex(c1: str, c2: str, t: float) -> str:
    """Linear RGB blend between two hex colors."""
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02X}{:02X}{:02X}".format(
        *(round(x + (y - x) * t) for x, y in zip(a, b)))


def score_bin_colors(n: int) -> list[str]:
    """Gradient from lowest-emphasis (gray) to highest-emphasis (mint)."""
    if n <= 1:
        return [PALETTE["mint"]]
    return [_lerp_hex(PALETTE["gray"], PALETTE["mint"], i / (n - 1))
            for i in range(n)]

st.set_page_config(page_title="JobTool board", layout="wide")
st.title("JobTool board")


def api_get(path: str, params: dict | None = None) -> dict | list:
    r = requests.get(f"{API}{path}", params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def api_post(path: str, params: dict | None = None) -> dict:
    r = requests.post(f"{API}{path}", params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def api_patch(path: str, body: dict) -> dict:
    r = requests.patch(f"{API}{path}", json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def _flash_error(job_id: int, exc: Exception) -> None:
    st.session_state["flash"] = f"Job {job_id} update failed: {exc}"


def _on_applied(job_id: int) -> None:
    try:
        api_patch(f"/jobs/{job_id}",
                  {"applied": bool(st.session_state[f"applied_{job_id}"])})
    except requests.RequestException as exc:
        _flash_error(job_id, exc)


def _on_status(job_id: int) -> None:
    try:
        api_patch(f"/jobs/{job_id}",
                  {"interview_status": st.session_state[f"status_{job_id}"]})
    except requests.RequestException as exc:
        _flash_error(job_id, exc)


def _on_notes(job_id: int) -> None:
    try:
        api_patch(f"/jobs/{job_id}",
                  {"notes": st.session_state[f"notes_{job_id}"]})
    except requests.RequestException as exc:
        _flash_error(job_id, exc)


def tracking_controls(job: dict, statuses: list) -> None:
    """One job's tracking row: Applied checkbox, plus status dropdown and
    notes field once applied. Widget changes PATCH the API (applied_date is
    stamped server-side); the auto-rerun then shows fresh state."""
    jid = job["id"]
    head, box = st.columns([5, 1])
    head.write(f"**{job['match_percentage']}%** · {job['title']} @ "
               f"{job['company']} · {job['location']} ({job['source']})")
    box.checkbox("Applied", value=bool(job["applied"]), key=f"applied_{jid}",
                 on_change=_on_applied, kwargs={"job_id": jid})
    if job["applied"]:
        status_notes_controls(job, statuses)


def status_notes_controls(job: dict, statuses: list) -> None:
    """Status dropdown + notes field for an applied job (shared by the
    card layout and the tracker view; same widget keys/state)."""
    jid = job["id"]
    s_col, n_col = st.columns(2)
    cur = job["interview_status"]
    s_col.selectbox("Interview status", statuses,
                    index=statuses.index(cur) if cur in statuses else 0,
                    key=f"status_{jid}",
                    on_change=_on_status, kwargs={"job_id": jid})
    n_col.text_input("Notes", value=job["notes"] or "",
                     key=f"notes_{jid}",
                     on_change=_on_notes, kwargs={"job_id": jid},
                     help="Saves on Enter / blur")


def match_badge(score) -> str:
    """Colored match % badge HTML: mint accent 80+, amber 60-79, gray below."""
    if score is None:
        return ('<span style="background-color:#2A3140;color:#C7CDD8;'
                'padding:2px 10px;border-radius:999px;font-size:0.85em;'
                'font-weight:700">—</span>')
    if score >= 80:
        bg, fg = ACCENT, "#04120C"
    elif score >= 60:
        bg, fg = "#F5A524", "#1A1206"
    else:
        bg, fg = "#2A3140", "#C7CDD8"
    return (f'<span style="background-color:{bg};color:{fg};padding:2px 10px;'
            f'border-radius:999px;font-size:0.85em;font-weight:700">'
            f'{score:.1f}%</span>')


def status_pill(status: str | None) -> str:
    """Small status pill using the fixed STATUS_COLORS mapping (same colors
    as the Dashboard donut, so a status reads identically everywhere)."""
    if not status:
        return ""
    bg = STATUS_COLORS.get(status, PALETTE["gray"])
    label = _html.escape(status.replace("_", " "))
    return (f' <span style="background-color:{bg};color:#0B0E14;'
            f'padding:2px 10px;border-radius:999px;font-size:0.8em;'
            f'font-weight:700">{label}</span>')


def scope_tag(scope: str | None) -> str:
    """Small remote-eligibility tag (worldwide mint, restricted coral,
    unknown gray; empty when the job has no scope)."""
    if not scope:
        return ""
    colors = {"worldwide": ACCENT, "restricted": PALETTE["coral"]}
    bg = colors.get(str(scope), PALETTE["gray"])
    label = _html.escape(str(scope))
    return (f' <span style="background-color:{bg};color:#0B0E14;'
            f'padding:2px 10px;border-radius:999px;font-size:0.8em;'
            f'font-weight:700">{label}</span>')


def job_card(job: dict, statuses: list, descriptions: dict) -> None:
    """One compact job card: header + badge, metadata, actions, description."""
    jid = job["id"]
    title = _html.escape(str(job["title"] or "Untitled"))
    company = _html.escape(str(job["company"] or "?"))
    with st.container(border=True):
        st.markdown(f"#### {title} @ {company} {match_badge(job['match_percentage'])}"
                    f"{status_pill(job.get('interview_status')) if job.get('applied') else ''}"
                    f"{scope_tag(job.get('remote_scope'))}",
                    unsafe_allow_html=True)
        st.caption(f"{job['source']} · {job['region']} · "
                   f"{job['posted_date'] or 'date unknown'}")
        act, check = st.columns([1, 1])
        act.link_button("View posting", str(job["url"] or "#"))
        check.checkbox("Applied", value=bool(job["applied"]),
                       key=f"applied_{jid}",
                       on_change=_on_applied, kwargs={"job_id": jid})
        if job["applied"]:
            status_notes_controls(job, statuses)
        with st.expander("Description"):
            st.write(descriptions.get(jid)
                     or "_No description available for this posting_")


def _dark(fig):
    """Match a plotly figure to the .streamlit dark theme + mint accent."""
    fig.update_layout(template="plotly_dark",
                      paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color="#FAFAFA"),
                      margin=dict(l=10, r=10, t=40, b=10))
    return fig


# --- health / task state (top, always visible) ---
try:
    health = api_get("/health")
    st.caption(f"DB rows: {health['rows']} | scored: {health['scored_rows']} | "
               f"last ingested: {health['last_ingested']}")
    for name, t in health["tasks"].items():
        if t.get("state") == "running":
            st.info(f"{name} is running…")
        elif t.get("state") == "error":
            st.error(f"Last {name} failed: {t.get('detail', '')}")
except requests.RequestException as exc:
    st.error(f"API unreachable at {API} — start it first: "
             "`uv run uvicorn api:app --host 127.0.0.1 --port 8000` ({exc})")
    st.stop()

flash = st.session_state.pop("flash", None)
if flash:
    st.warning(flash)

# --- sidebar control panel: view, filters, then actions ---
view = st.sidebar.radio("View", ["Browse matches", "Applied tracker"])
st.sidebar.divider()
st.sidebar.header("Filters")
min_score = st.sidebar.slider("Minimum match %", 0.0, 100.0, 0.0, 1.0)
try:
    opts = api_get("/filter-options")
except requests.RequestException as exc:
    st.error(f"Could not load filter options: {exc}")
    st.stop()
sources = st.sidebar.multiselect("Sources", opts["sources"], default=opts["sources"])
regions = st.sidebar.multiselect("Regions", opts["regions"], default=opts["regions"])
statuses = opts.get("interview_statuses") or [
    "pending", "no_response", "rejected", "interview_scheduled",
    "interview_completed", "offer"]
dates = st.sidebar.date_input("Posted date range", value=())
limit = st.sidebar.number_input("Max rows", 1, 2000, 200)

params: dict = {"min_score": min_score, "limit": int(limit), "offset": 0}
if len(dates) == 2:
    params["date_from"] = dates[0].isoformat()
    params["date_to"] = dates[1].isoformat()

# --- actions (non-blocking: API returns 202 immediately) ---
st.sidebar.divider()
st.sidebar.header("Actions")
if st.sidebar.button("Run ingestion"):
    with st.spinner("Starting ingest…"):
        try:
            res = api_post("/ingest")
            st.sidebar.success(res["status"])
        except requests.RequestException as exc:
            st.sidebar.error(f"Ingest trigger failed: {exc}")
only_unscored = st.sidebar.checkbox("Rescore only unscored jobs", value=True)
if st.sidebar.button("Rescore matches"):
    with st.spinner("Starting rescore…"):
        try:
            res = api_post("/rescore", {"only_unscored": str(only_unscored).lower()})
            st.sidebar.success(res["status"])
        except requests.RequestException as exc:
            st.sidebar.error(f"Rescore trigger failed: {exc}")

tab_jobs, tab_dash = st.tabs(["Jobs", "Dashboard"])

with tab_jobs:
    # --- job list: every sidebar filter is passed to the API as query params.
    # Single-value API params (source/region) are fanned out per selection and
    # merged, so filtering always happens server-side.
    src_sel = sources if set(sources) != set(opts["sources"]) else [None]
    reg_sel = regions if set(regions) != set(opts["regions"]) else [None]
    jobs: list = []
    total = 0
    try:
        for src in src_sel:
            for reg in reg_sel:
                q = dict(params)
                if src:
                    q["source"] = src
                if reg:
                    q["region"] = reg
                part = api_get("/jobs", q)
                total += part["total"]
                jobs.extend(part["jobs"])
    except requests.RequestException as exc:
        st.error(f"Could not fetch jobs: {exc}")
        st.stop()
    jobs.sort(key=lambda j: (j["match_percentage"] is None,
                             -(j["match_percentage"] or 0)))

    if view == "Applied tracker":
        # Lightweight application tracker, separate from match-scored browsing.
        st.subheader("Applied tracker")
        try:
            applied_jobs = api_get("/jobs", {"applied": True, "limit": 2000,
                                             "offset": 0})["jobs"]
        except requests.RequestException as exc:
            st.error(f"Could not fetch applied jobs: {exc}")
            st.stop()
        if not applied_jobs:
            st.info("Nothing marked applied yet — tick Applied on a job in Browse view.")
        else:
            st.dataframe(
                applied_jobs,
                column_order=["title", "company", "applied_date",
                              "interview_status", "notes", "url"],
                column_config={"url": st.column_config.LinkColumn("link")},
                hide_index=True,
                use_container_width=True,
            )
            st.divider()
            for j in applied_jobs:
                tracking_controls(j, statuses)
        st.stop()

    st.caption(f"Showing **{len(jobs)}** of **{total}** matching jobs "
               f"(sorted by match % desc)")
    if not jobs:
        st.info("No jobs match the current filters.")
    else:
        # Descriptions are immutable once ingested (INSERT OR IGNORE never
        # updates rows), so cache them per id and fetch only what is missing.
        desc_cache = st.session_state.setdefault("desc_cache", {})
        missing = [j["id"] for j in jobs if j["id"] not in desc_cache]
        if missing:
            with st.spinner(f"Loading descriptions for {len(missing)} jobs…"):
                for jid in missing:
                    try:
                        desc_cache[jid] = api_get(f"/jobs/{jid}").get("description")
                    except requests.RequestException:
                        desc_cache[jid] = None
        for j in jobs:
            job_card(j, statuses, desc_cache)

    # --- detail view via GET /jobs/{id} ---
    labels = {f"{j['title']} @ {j['company']} ({j['match_percentage']}%)": j["id"]
              for j in jobs}
    choice = st.selectbox("Job detail", ["—"] + sorted(labels))
    if choice != "—":
        try:
            d = api_get(f"/jobs/{labels[choice]}")
            st.subheader(d["title"])
            st.write(f"**{d['company']}** · {d['location']} · {d['region']} · "
                     f"{d['source']} · {d['posted_date']} · match {d['match_percentage']}%")
            st.write(f"[Open posting]({d['url']})")
            with st.expander("Description", expanded=True):
                st.write(d.get("description") or "_No description_")
        except requests.RequestException as exc:
            st.error(f"Could not load detail: {exc}")

with tab_dash:
    st.subheader("Tracking dashboard")
    if st.button("Refresh dashboard"):
        pass  # the rerun refetches fresh stats below; no full page reload
    try:
        with st.spinner("Loading stats…"):
            stats = api_get("/stats/summary")
    except requests.RequestException as exc:
        st.error(f"Could not load stats: {exc}")
        st.stop()

    f = stats["funnel"]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Kept jobs", f["total_kept"])
    k2.metric("Applied", f["applied_count"])
    k3.metric("Response rate", f"{stats['response_rate']}%")
    days = stats["days_since_last_application"]
    k4.metric("Days since last application", days if days is not None else "—")

    c1, c2 = st.columns(2)
    with c1:
        import plotly.graph_objects as go
        funnel = go.Figure(go.Funnel(
            y=FUNNEL_STAGES,
            x=[f["total_fetched"], f["total_kept"], f["applied_count"],
               f["interview_count"], f["offer_count"]],
            marker=dict(color=FUNNEL_COLORS),
            textinfo="value",
        ))
        funnel.update_layout(title="Pipeline funnel")
        st.plotly_chart(_dark(funnel), use_container_width=True)
    with c2:
        bd = stats["interview_breakdown"]
        # Fixed color per status (not positional): stable no matter which
        # statuses are present or their order. Label+percent with auto
        # placement plus a full legend keeps even a 1% slice legible.
        donut = go.Figure(go.Pie(
            labels=list(bd.keys()), values=list(bd.values()), hole=0.5,
            marker=dict(colors=[STATUS_COLORS.get(k, PALETTE["gray"])
                                for k in bd.keys()]),
            textinfo="label+percent", textposition="auto",
            showlegend=True,
        ))
        donut.update_layout(title="Applications by status")
        st.plotly_chart(_dark(donut), use_container_width=True)

    c3, c4 = st.columns(2)
    with c3:
        tl = stats["applications_timeline"]
        line = go.Figure(go.Scatter(
            x=[p["date"] for p in tl], y=[p["count"] for p in tl],
            mode="lines+markers", line=dict(color=ACCENT, width=2.5),
            marker=dict(color=ACCENT, size=7),
        ))
        line.update_layout(title="Applications — last 30 days",
                           xaxis_title="Date", yaxis_title="Applied")
        st.plotly_chart(_dark(line), use_container_width=True)
    with c4:
        sd = stats["score_distribution"]
        bars = go.Figure(go.Bar(
            x=[b["bin"] for b in sd], y=[b["count"] for b in sd],
            marker_color=score_bin_colors(len(sd)),
        ))
        bars.update_layout(title="Match-score distribution",
                           xaxis_title="Score bin", yaxis_title="Jobs")
        st.plotly_chart(_dark(bars), use_container_width=True)
