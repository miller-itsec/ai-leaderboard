#!/usr/bin/env python3
"""Standalone AI Usage Analytics — fetch Cursor + Copilot data via API,
merge into a unified dataset, and output CSV + JSON.

Usage:
    python ai_usage_analytics.py \
        --cursor-key <CURSOR_API_KEY> \
        --github-token <GITHUB_TOKEN> \
        --github-enterprise <ENTERPRISE_SLUG> \
        [--days 90] \
        [--output ai_usage_report]

Credentials can also be set via env vars:
    CURSOR_API_KEY, GITHUB_TOKEN, GITHUB_ENTERPRISE
"""

import argparse
import base64
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CURSOR_API_BASE = "https://api.cursor.com/analytics"
GITHUB_API_BASE = "https://api.github.com"
CURSOR_LINES_PER_TAB = 3

INTENTIONAL_FEATURES = {
    "agent_edit", "chat_panel_agent_mode", "chat_panel_ask_mode",
    "chat_panel_edit_mode", "chat_panel_custom_mode", "chat_inline",
    "chat_panel_unknown_mode",
}
PASSIVE_FEATURES = {"code_completion"}

# Behavior quadrant thresholds for small datasets (< 10 active users)
SMALL_DATASET_MIN_USERS = 10
SMALL_DATASET_INTERACTION_THRESHOLD = 20
SMALL_DATASET_ACCEPT_THRESHOLD = 50.0

# ---------------------------------------------------------------------------
# HTTP + Date helpers
# ---------------------------------------------------------------------------


def _api_get(url, headers, label="", raw=False):
    """HTTP GET with error handling."""
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=120) as resp:
            data = resp.read()
            if raw:
                return data
            content_type = resp.headers.get("Content-Type", "")
            if "json" in content_type or data[:1] in (b"{", b"["):
                return json.loads(data)
            return data
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"API error [{label}]: HTTP {e.code} — {body}") from e


def _date_chunks(start, end, max_days=30):
    """Split a date range into chunks of max_days."""
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    chunks = []
    while s < e:
        chunk_end = min(s + timedelta(days=max_days), e)
        chunks.append((s.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        s = chunk_end + timedelta(days=1)
    return chunks


def _merge_by_user_chunks(chunks_data):
    """Merge multiple by-user API responses (each keyed by email -> daily list)."""
    merged = {"data": {}}
    for chunk in chunks_data:
        for email, days in chunk.get("data", {}).items():
            if email not in merged["data"]:
                merged["data"][email] = []
            merged["data"][email].extend(days)
    return merged


# ---------------------------------------------------------------------------
# Identity normalization
# ---------------------------------------------------------------------------


def _normalize_email(email):
    """Extract username from email and lowercase it."""
    if not isinstance(email, str):
        return ""
    return email.split("@")[0].strip().lower()


def _normalize_copilot_login(login):
    """Normalize GitHub login: strip _suffix, replace hyphens with dots."""
    if not isinstance(login, str):
        return ""
    name = re.sub(r"_[a-z]+$", "", login.strip().lower())
    return name.replace("-", ".")


def _display_name(normalized):
    """Convert 'john.doe' to 'John Doe'."""
    return " ".join(part.capitalize() for part in normalized.split("."))


# ---------------------------------------------------------------------------
# Cursor API fetch
# ---------------------------------------------------------------------------


def fetch_cursor(api_key, days=90):
    """Fetch Cursor Analytics API and return aggregated DataFrame."""
    headers = {
        "Authorization": f"Basic {base64.b64encode(f'{api_key}:'.encode()).decode()}",
    }

    end = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    chunks = _date_chunks(start, end, max_days=30)

    total_steps = len(chunks) * 2
    step = 0

    # Fetch tabs
    tab_chunks = []
    for cs, ce in chunks:
        url = f"{CURSOR_API_BASE}/by-user/tabs?startDate={cs}&endDate={ce}&pageSize=500"
        tab_chunks.append(_api_get(url, headers, f"tabs {cs}->{ce}"))
        step += 1
        print(f"  [{step}/{total_steps}] Cursor tabs {cs} -> {ce}")
    tabs_data = _merge_by_user_chunks(tab_chunks)

    # Fetch agent edits
    agent_chunks = []
    for cs, ce in chunks:
        url = f"{CURSOR_API_BASE}/by-user/agent-edits?startDate={cs}&endDate={ce}&pageSize=500"
        agent_chunks.append(_api_get(url, headers, f"agent {cs}->{ce}"))
        step += 1
        print(f"  [{step}/{total_steps}] Cursor agent edits {cs} -> {ce}")
    agent_data = _merge_by_user_chunks(agent_chunks)

    all_emails = set(tabs_data["data"].keys()) | set(agent_data["data"].keys())

    rows = []
    for email in sorted(all_emails):
        tab_days = tabs_data["data"].get(email, [])
        agent_days = agent_data["data"].get(email, [])
        rows.append({
            "email": email,
            "normalized_name": _normalize_email(email),
            "chat_tabs_shown": sum(d.get("total_suggestions", 0) for d in tab_days),
            "chat_total_applies": sum(d.get("total_suggested_diffs", 0) for d in agent_days),
            "chat_total_accepts": sum(d.get("total_accepted_diffs", 0) for d in agent_days),
            "tabs_accepted": sum(d.get("total_accepts", 0) for d in tab_days),
            "agent_lines_accepted": sum(d.get("total_lines_accepted", 0) for d in agent_days),
            "tab_lines_accepted": sum(d.get("total_lines_accepted", 0) for d in tab_days),
        })

    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame(columns=[
        "email", "normalized_name", "chat_tabs_shown",
        "chat_total_applies", "chat_total_accepts", "tabs_accepted",
        "agent_lines_accepted", "tab_lines_accepted",
    ])


# ---------------------------------------------------------------------------
# Copilot API fetch
# ---------------------------------------------------------------------------


def fetch_copilot(github_token, enterprise, days=90):
    """Fetch Copilot Usage Metrics API and return aggregated DataFrame."""
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    end = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    current = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    total_days = (end_dt - current).days + 1

    all_records = []
    failed_days = []
    day_idx = 0

    while current <= end_dt:
        day = current.strftime("%Y-%m-%d")
        try:
            url = (f"{GITHUB_API_BASE}/enterprises/{enterprise}"
                   f"/copilot/metrics/reports/users-1-day?day={day}")
            meta = _api_get(url, headers, f"copilot-{day}")
            for link in meta.get("download_links", []):
                raw = _api_get(link, {}, f"download-{day}", raw=True)
                for line in raw.decode("utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line:
                        all_records.append(json.loads(line))
        except Exception:
            failed_days.append(day)

        day_idx += 1
        if day_idx % 10 == 0 or day_idx == total_days:
            print(f"  [{day_idx}/{total_days}] Copilot {day}")
        current += timedelta(days=1)

    if failed_days:
        print(f"  Warning: {len(failed_days)} days failed: "
              f"{', '.join(failed_days[:5])}{'...' if len(failed_days) > 5 else ''}")

    # Aggregate per user
    user_data = defaultdict(lambda: {
        "interactions": 0, "code_generation": 0, "code_acceptance": 0,
        "loc_added": 0, "loc_added_intentional": 0, "loc_added_passive": 0,
    })

    for rec in all_records:
        login = rec.get("user_login", "")
        if not login:
            continue
        ud = user_data[login]
        ud["interactions"] += rec.get("user_initiated_interaction_count", 0)
        ud["code_generation"] += rec.get("code_generation_activity_count", 0)
        ud["code_acceptance"] += rec.get("code_acceptance_activity_count", 0)
        ud["loc_added"] += rec.get("loc_added_sum", 0)

        for ft in rec.get("totals_by_feature", []):
            feature = ft.get("feature", "")
            loc = ft.get("loc_added_sum", 0)
            if feature in PASSIVE_FEATURES:
                ud["loc_added_passive"] += loc
            else:
                ud["loc_added_intentional"] += loc

    rows = []
    for login in sorted(user_data):
        ud = user_data[login]
        rows.append({
            "user_login": login,
            "normalized_name": _normalize_copilot_login(login),
            **ud,
        })

    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame(columns=[
        "user_login", "normalized_name", "interactions", "code_generation",
        "code_acceptance", "loc_added", "loc_added_intentional", "loc_added_passive",
    ])


# ---------------------------------------------------------------------------
# Build unified dataset
# ---------------------------------------------------------------------------


def build_unified(cursor_df, copilot_df):
    """Merge Cursor + Copilot data into a unified per-user DataFrame."""
    if cursor_df is None and copilot_df is None:
        return pd.DataFrame()

    # Prepare copies with prefixed columns
    if cursor_df is not None and not cursor_df.empty:
        c = cursor_df.copy()
        c = c.rename(columns={col: f"cursor_{col}" for col in c.columns
                               if col != "normalized_name"})
    else:
        c = None

    if copilot_df is not None and not copilot_df.empty:
        p = copilot_df.copy()
        p = p.rename(columns={col: f"copilot_{col}" for col in p.columns
                               if col != "normalized_name"})
    else:
        p = None

    # Merge
    if c is not None and p is not None:
        df = pd.merge(c, p, on="normalized_name", how="outer")
    elif c is not None:
        df = c.copy()
    elif p is not None:
        df = p.copy()
    else:
        return pd.DataFrame()

    for col in df.select_dtypes(include="number").columns:
        df[col] = df[col].fillna(0).astype(int)

    df["display_name"] = df["normalized_name"].apply(_display_name)

    # Tool presence — require actual activity, not just an account existing
    if "cursor_email" in df.columns:
        _has_login = df["cursor_email"].notna() & (df["cursor_email"] != "")
        _cursor_lines = pd.Series(0, index=df.index)
        for _cc in ("cursor_tabs_accepted", "cursor_chat_total_accepts"):
            if _cc in df.columns:
                _cursor_lines = _cursor_lines + df[_cc].fillna(0)
        has_cursor = _has_login & (_cursor_lines > 0)
    else:
        has_cursor = pd.Series(False, index=df.index)

    if "copilot_user_login" in df.columns:
        _has_login = df["copilot_user_login"].notna() & (df["copilot_user_login"] != "")
        _copilot_lines = pd.Series(0, index=df.index)
        for _cc in ("copilot_loc_added", "copilot_loc_added_intentional",
                     "copilot_loc_added_passive"):
            if _cc in df.columns:
                _copilot_lines = _copilot_lines + df[_cc].fillna(0)
        _copilot_interact = (df["copilot_interactions"].fillna(0)
                             if "copilot_interactions" in df.columns
                             else pd.Series(0, index=df.index))
        has_copilot = _has_login & ((_copilot_lines > 0) | (_copilot_interact > 0))
    else:
        has_copilot = pd.Series(False, index=df.index)

    df["has_cursor"] = has_cursor
    df["has_copilot"] = has_copilot
    df["match_status"] = "neither"
    df.loc[has_cursor & has_copilot, "match_status"] = "both"
    df.loc[has_cursor & ~has_copilot, "match_status"] = "cursor_only"
    df.loc[~has_cursor & has_copilot, "match_status"] = "copilot_only"

    # AI lines — intentional (chat/agent) vs passive (tab completions)
    # Prefer actual LOC from API; fall back to diff count
    df["cursor_chat_lines"] = 0
    df["cursor_tab_lines"] = 0
    if "cursor_agent_lines_accepted" in df.columns:
        df["cursor_chat_lines"] = df["cursor_agent_lines_accepted"]
    elif "cursor_chat_total_accepts" in df.columns:
        df["cursor_chat_lines"] = df["cursor_chat_total_accepts"]
    if "cursor_tab_lines_accepted" in df.columns:
        df["cursor_tab_lines"] = df["cursor_tab_lines_accepted"]
    elif "cursor_tabs_accepted" in df.columns:
        df["cursor_tab_lines"] = df["cursor_tabs_accepted"] * CURSOR_LINES_PER_TAB
    df["cursor_ai_lines"] = df["cursor_chat_lines"] + df["cursor_tab_lines"]
    df["copilot_ai_lines"] = df.get("copilot_loc_added", pd.Series(0, index=df.index))

    # Intentional = agent/chat only (excludes passive tab completions)
    if "copilot_loc_added_intentional" in df.columns:
        copilot_intentional = df["copilot_loc_added_intentional"]
        copilot_passive = df.get("copilot_loc_added_passive",
                                 pd.Series(0, index=df.index))
    else:
        copilot_intentional = df["copilot_ai_lines"]
        copilot_passive = pd.Series(0, index=df.index)

    df["copilot_intentional_lines"] = copilot_intentional
    df["copilot_passive_lines"] = copilot_passive
    df["intentional_ai_lines"] = df["cursor_chat_lines"] + copilot_intentional
    df["total_ai_lines"] = df["cursor_ai_lines"] + df["copilot_ai_lines"]

    # Acceptance rates
    if "cursor_tabs_accepted" in df.columns and "cursor_chat_tabs_shown" in df.columns:
        df["cursor_acceptance_rate"] = (
            df["cursor_tabs_accepted"]
            / df["cursor_chat_tabs_shown"].replace(0, float("nan")) * 100
        ).fillna(0)
    else:
        df["cursor_acceptance_rate"] = 0

    # Cursor chat quality (accepts / applies)
    if "cursor_chat_total_accepts" in df.columns and "cursor_chat_total_applies" in df.columns:
        df["cursor_chat_quality"] = (
            df["cursor_chat_total_accepts"]
            / df["cursor_chat_total_applies"].replace(0, float("nan")) * 100
        ).fillna(0)
    else:
        df["cursor_chat_quality"] = 0

    if "copilot_code_acceptance" in df.columns and "copilot_code_generation" in df.columns:
        df["copilot_acceptance_rate"] = (
            df["copilot_code_acceptance"]
            / df["copilot_code_generation"].replace(0, float("nan")) * 100
        ).fillna(0)
    else:
        df["copilot_acceptance_rate"] = 0

    # Interactions
    cursor_applies = df.get("cursor_chat_total_applies", pd.Series(0, index=df.index))
    copilot_interactions = df.get("copilot_interactions", pd.Series(0, index=df.index))
    if isinstance(cursor_applies, int):
        cursor_applies = pd.Series(cursor_applies, index=df.index)
    if isinstance(copilot_interactions, int):
        copilot_interactions = pd.Series(copilot_interactions, index=df.index)
    df["total_interactions"] = cursor_applies + copilot_interactions

    # Best acceptance rate (max of cursor / copilot)
    df["best_accept"] = df[["cursor_acceptance_rate", "copilot_acceptance_rate"]].max(axis=1)

    # Behavior quadrant assignment
    df = _assign_behavior_quadrant(df)

    # Segmentation (based on intentional output)
    if len(df) > 0 and df["intentional_ai_lines"].max() > 0:
        q50 = df["intentional_ai_lines"].quantile(0.50)
        q75 = df["intentional_ai_lines"].quantile(0.75)
        q95 = df["intentional_ai_lines"].quantile(0.95)
        df["segment"] = "Inactive"
        df.loc[df["intentional_ai_lines"] > 0, "segment"] = "Casual"
        df.loc[df["intentional_ai_lines"] >= q50, "segment"] = "Regular"
        df.loc[df["intentional_ai_lines"] >= q75, "segment"] = "Power User"
        df.loc[df["intentional_ai_lines"] >= q95, "segment"] = "Champion"
    else:
        df["segment"] = "Inactive"

    # Any AI activity (interactions OR passive LOC) should not be Inactive
    df.loc[
        (df["segment"] == "Inactive")
        & ((df["total_interactions"] > 0) | (df["total_ai_lines"] > 0)),
        "segment",
    ] = "Casual"

    df = df.sort_values("intentional_ai_lines", ascending=False).reset_index(drop=True)
    return df


def _assign_behavior_quadrant(df):
    """Assign behavior_quadrant based on interactions and acceptance rate."""
    df["behavior_quadrant"] = "disengaged"
    if "total_interactions" not in df.columns or "best_accept" not in df.columns:
        return df

    active = df[df["total_interactions"] > 0]
    if active.empty:
        return df

    if len(active) < SMALL_DATASET_MIN_USERS:
        interaction_thresh = SMALL_DATASET_INTERACTION_THRESHOLD
        accept_thresh = SMALL_DATASET_ACCEPT_THRESHOLD
    else:
        interaction_thresh = active["total_interactions"].median()
        accept_thresh = active["best_accept"].median()

    high_interact = df["total_interactions"] >= interaction_thresh
    high_accept = df["best_accept"] >= accept_thresh
    has_interactions = df["total_interactions"] > 0

    df.loc[has_interactions & high_interact & high_accept, "behavior_quadrant"] = "power_user"
    df.loc[has_interactions & ~high_interact & high_accept, "behavior_quadrant"] = "cautious_adopter"
    df.loc[has_interactions & high_interact & ~high_accept, "behavior_quadrant"] = "brute_forcer"
    df.loc[has_interactions & ~high_interact & ~high_accept, "behavior_quadrant"] = "disengaged"

    return df


# ---------------------------------------------------------------------------
# Summary computation
# ---------------------------------------------------------------------------


def compute_summary(df, days):
    """Compute team-level KPI aggregates from unified DataFrame."""
    if df.empty:
        return {"generated_at": datetime.now().isoformat(), "period_days": days,
                "total_users": 0}

    total = len(df)
    any_tool = int((df["match_status"] != "neither").sum())
    active = int((df["intentional_ai_lines"] > 0).sum())

    intentional = df["intentional_ai_lines"]
    total_lines = df["total_ai_lines"]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period_days": days,
        "total_users": total,
        "cursor_users": int(df["has_cursor"].sum()),
        "copilot_users": int(df["has_copilot"].sum()),
        "both_users": int((df["match_status"] == "both").sum()),
        "adoption_pct": round(any_tool / total * 100, 1) if total else 0,
        "active_pct": round(active / total * 100, 1) if total else 0,
        "intentional_ai_lines": {
            "sum": int(intentional.sum()),
            "avg": int(intentional.mean()),
            "median": int(intentional.median()),
            "p75": int(intentional.quantile(0.75)),
            "p90": int(intentional.quantile(0.90)),
            "p95": int(intentional.quantile(0.95)),
        },
        "total_ai_lines": {
            "sum": int(total_lines.sum()),
            "avg": int(total_lines.mean()),
            "median": int(total_lines.median()),
        },
        "quadrant_distribution": df["behavior_quadrant"].value_counts().to_dict(),
        "segment_distribution": df["segment"].value_counts().to_dict(),
    }


# ---------------------------------------------------------------------------
# CSV output columns
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "display_name", "normalized_name", "match_status",
    "cursor_chat_tabs_shown", "cursor_tabs_accepted", "cursor_acceptance_rate",
    "cursor_chat_total_applies", "cursor_chat_total_accepts", "cursor_chat_quality",
    "copilot_interactions", "copilot_code_generation", "copilot_code_acceptance",
    "copilot_acceptance_rate",
    "copilot_loc_added", "copilot_loc_added_intentional", "copilot_loc_added_passive",
    "intentional_ai_lines", "total_ai_lines", "cursor_tab_lines",
    "best_accept", "total_interactions",
    "behavior_quadrant", "segment",
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Standalone AI Usage Analytics — Cursor + Copilot API -> CSV/JSON")
    parser.add_argument("--cursor-key", default=os.environ.get("CURSOR_API_KEY", ""),
                        help="Cursor Analytics API key (or CURSOR_API_KEY env var)")
    parser.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN", ""),
                        help="GitHub PAT with copilot scope (or GITHUB_TOKEN env var)")
    parser.add_argument("--github-enterprise", default=os.environ.get("GITHUB_ENTERPRISE", ""),
                        help="GitHub Enterprise slug (or GITHUB_ENTERPRISE env var)")
    parser.add_argument("--days", type=int, default=90,
                        help="Number of days to fetch (default: 90)")
    parser.add_argument("--output", default="ai_usage_report",
                        help="Output file prefix (default: ai_usage_report)")
    args = parser.parse_args()

    has_cursor = bool(args.cursor_key)
    has_copilot = bool(args.github_token and args.github_enterprise)

    if not has_cursor and not has_copilot:
        print("Error: provide at least --cursor-key or --github-token + --github-enterprise",
              file=sys.stderr)
        sys.exit(1)

    # Fetch Cursor
    cursor_df = None
    if has_cursor:
        print(f"Fetching Cursor data ({args.days} days)...")
        cursor_df = fetch_cursor(args.cursor_key, args.days)
        print(f"  -> {len(cursor_df)} Cursor users")

    # Fetch Copilot
    copilot_df = None
    if has_copilot:
        print(f"Fetching Copilot data ({args.days} days)...")
        copilot_df = fetch_copilot(args.github_token, args.github_enterprise, args.days)
        print(f"  -> {len(copilot_df)} Copilot users")

    # Build unified dataset
    print("Building unified dataset...")
    df = build_unified(cursor_df, copilot_df)
    print(f"  -> {len(df)} total users")

    # Compute summary
    summary = compute_summary(df, args.days)

    # Write CSV — only include columns that exist
    csv_path = f"{args.output}_users.csv"
    out_cols = [c for c in CSV_COLUMNS if c in df.columns]
    df[out_cols].to_csv(csv_path, index=False)
    print(f"Wrote {csv_path} ({len(df)} rows, {len(out_cols)} columns)")

    # Write JSON summary
    json_path = f"{args.output}_summary.json"
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Wrote {json_path}")

    # Console summary
    print("\n" + "=" * 60)
    print("AI USAGE SUMMARY")
    print("=" * 60)
    print(f"Period:            {args.days} days")
    print(f"Total users:       {summary['total_users']}")
    print(f"Cursor users:      {summary.get('cursor_users', 0)}")
    print(f"Copilot users:     {summary.get('copilot_users', 0)}")
    print(f"Both tools:        {summary.get('both_users', 0)}")
    print(f"Adoption:          {summary.get('adoption_pct', 0)}%")
    print(f"Active:            {summary.get('active_pct', 0)}%")
    ial = summary.get("intentional_ai_lines", {})
    if isinstance(ial, dict):
        print(f"\nIntentional AI lines:")
        print(f"  Total:           {ial.get('sum', 0):,}")
        print(f"  Average:         {ial.get('avg', 0):,}")
        print(f"  Median:          {ial.get('median', 0):,}")
        print(f"  P75/P90/P95:     {ial.get('p75', 0):,} / {ial.get('p90', 0):,} / {ial.get('p95', 0):,}")
    qd = summary.get("quadrant_distribution", {})
    if qd:
        print(f"\nBehavior quadrants:")
        for q, n in sorted(qd.items()):
            print(f"  {q:20s} {n}")
    sd = summary.get("segment_distribution", {})
    if sd:
        print(f"\nSegments:")
        for s, n in sorted(sd.items()):
            print(f"  {s:20s} {n}")
    print("=" * 60)


if __name__ == "__main__":
    main()
