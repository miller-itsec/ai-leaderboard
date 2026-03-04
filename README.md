# AI Leaderboard

Standalone CLI tool that fetches developer AI usage data from **Cursor** and **GitHub Copilot** APIs, merges them into a unified per-user dataset, computes behavioral metrics, and outputs CSV + JSON reports.

No web UI, no database, no dependencies beyond `pandas` — just a single Python script you can run on a cron or manually.

## Requirements

- Python 3.9+
- `pandas`

```bash
pip install pandas
```

## Quick Start

```bash
# Both tools
python ai_usage_analytics.py \
    --cursor-key <CURSOR_API_KEY> \
    --github-token <GITHUB_TOKEN> \
    --github-enterprise <ENTERPRISE_SLUG>

# Cursor only
python ai_usage_analytics.py --cursor-key <CURSOR_API_KEY>

# Copilot only
python ai_usage_analytics.py \
    --github-token <GITHUB_TOKEN> \
    --github-enterprise <ENTERPRISE_SLUG>
```

### Options

| Flag | Env var | Description |
|------|---------|-------------|
| `--cursor-key` | `CURSOR_API_KEY` | Cursor Analytics API key |
| `--github-token` | `GITHUB_TOKEN` | GitHub PAT with `manage_billing:copilot` and `read:enterprise` scopes |
| `--github-enterprise` | `GITHUB_ENTERPRISE` | GitHub Enterprise slug (from URL) |
| `--days` | — | Lookback period in days (default: 90) |
| `--output` | — | Output file prefix (default: `ai_usage_report`) |

At least one tool (Cursor or Copilot) must be configured.

## Output Files

### `{output}_users.csv`

Per-user dataset with all computed metrics. One row per developer.

| Column | Description |
|--------|-------------|
| `display_name` | Human-readable name derived from login/email |
| `normalized_name` | Lowercase key used for cross-tool matching |
| `match_status` | `both`, `cursor_only`, `copilot_only`, or `neither` |
| `cursor_chat_tabs_shown` | Tab completion suggestions shown (Cursor) |
| `cursor_tabs_accepted` | Tab completions accepted (Cursor) |
| `cursor_acceptance_rate` | `tabs_accepted / tabs_shown * 100` |
| `cursor_chat_total_applies` | Agent/chat diffs suggested (Cursor) |
| `cursor_chat_total_accepts` | Agent/chat diffs accepted (Cursor) |
| `cursor_chat_quality` | `chat_accepts / chat_applies * 100` |
| `copilot_interactions` | User-initiated interactions (Copilot) |
| `copilot_code_generation` | Code generation events (Copilot) |
| `copilot_code_acceptance` | Code acceptance events (Copilot) |
| `copilot_acceptance_rate` | `code_acceptance / code_generation * 100` |
| `copilot_loc_added` | Total lines of code added (Copilot) |
| `copilot_loc_added_intentional` | LOC from chat/agent features (Copilot) |
| `copilot_loc_added_passive` | LOC from tab completions (Copilot) |
| `intentional_ai_lines` | Combined intentional output (see below) |
| `total_ai_lines` | All AI-generated lines including passive |
| `cursor_tab_lines` | Estimated LOC from Cursor tab completions |
| `best_accept` | Max of cursor/copilot acceptance rates |
| `total_interactions` | Sum of all interactions across tools |
| `behavior_quadrant` | Behavioral classification (see below) |
| `segment` | Output-based tier (see below) |

### `{output}_summary.json`

Team-level KPI aggregates:

```json
{
  "generated_at": "2026-03-04T12:00:00",
  "period_days": 90,
  "total_users": 70,
  "cursor_users": 65,
  "copilot_users": 55,
  "both_users": 50,
  "adoption_pct": 95.7,
  "active_pct": 87.1,
  "intentional_ai_lines": {
    "sum": 125000,
    "avg": 1786,
    "median": 850,
    "p75": 2100,
    "p90": 5200,
    "p95": 12500
  },
  "total_ai_lines": {
    "sum": 152000,
    "avg": 2171,
    "median": 1100
  },
  "quadrant_distribution": {
    "power_user": 18,
    "cautious_adopter": 17,
    "brute_forcer": 17,
    "disengaged": 18
  },
  "segment_distribution": {
    "Champion": 4,
    "Power User": 13,
    "Regular": 18,
    "Casual": 18,
    "Inactive": 17
  }
}
```

---

## Scoring and Algorithms

### Identity Matching

Users exist in Cursor (by email) and Copilot (by GitHub login). The script normalizes both to a common key so the same person's data merges into one row:

- **Cursor emails**: extract the part before `@`, lowercase. `John.Doe@company.com` becomes `john.doe`.
- **Copilot logins**: strip trailing `_suffix` patterns (e.g. `jdoe_company` becomes `jdoe`), replace hyphens with dots. `john-doe` becomes `john.doe`.

Users that normalize to the same key get merged via an outer join. The `match_status` column shows whether each user was found in both tools, one, or neither.

### Intentional vs Passive AI Lines

Not all AI-generated code is equal. The script distinguishes two categories:

**Intentional** — code the developer actively requested via chat, agent mode, or inline edits. This represents deliberate AI collaboration:
- Cursor: accepted agent/chat diffs (`chat_total_accepts`)
- Copilot: LOC from features like `agent_edit`, `chat_panel_agent_mode`, `chat_panel_ask_mode`, `chat_panel_edit_mode`, `chat_inline`, etc.

**Passive** — code auto-suggested without explicit prompting (tab completions):
- Cursor: `tabs_accepted * 3` (estimated at 3 lines per tab completion)
- Copilot: LOC from `code_completion` feature

The primary metric `intentional_ai_lines` only counts intentional output. `total_ai_lines` includes both. This prevents users who simply accept lots of autocomplete from ranking above users who actively use AI for substantial code generation.

### Cursor Lines Per Tab (`CURSOR_LINES_PER_TAB = 3`)

Cursor's tab completion API reports acceptance counts but not lines of code. Based on empirical analysis, each accepted tab completion averages approximately 3 lines of code. This multiplier converts tab acceptance counts to estimated LOC for comparability with Copilot's LOC-based reporting.

### Acceptance Rates

Acceptance rate measures how selective a developer is with AI suggestions — higher means they're getting more relevant suggestions or are better at prompting:

- **Cursor acceptance rate**: `tabs_accepted / tabs_shown * 100` — what percentage of shown tab completions were accepted. Typical range: 15-25%.
- **Cursor chat quality**: `chat_accepts / chat_applies * 100` — what percentage of agent-suggested diffs were accepted. This is a supplementary metric with no Copilot equivalent.
- **Copilot acceptance rate**: `code_acceptance / code_generation * 100` — what percentage of code generation events resulted in acceptance. Typical range: 15-25%.
- **Best acceptance rate**: `max(cursor_rate, copilot_rate)` — used for quadrant assignment so users aren't penalized for only using one tool.

### Behavior Quadrants

Every user is classified into one of four quadrants based on two dimensions:

1. **Interaction volume** (X-axis): `total_interactions` = Cursor chat applies + Copilot interactions
2. **Acceptance quality** (Y-axis): `best_accept` = max of Cursor and Copilot acceptance rates

The thresholds are determined dynamically:

- **10+ active users**: median split — the median of each dimension among users with interactions > 0 defines the boundary.
- **< 10 active users**: fixed thresholds are used instead (20 interactions, 50% acceptance rate) to avoid unstable medians with small samples.

```
                    High Acceptance
                         |
    Cautious Adopter     |     Power User
    (selective but       |     (high volume +
     low volume)         |      high quality)
                         |
  ───────────────────────┼──────────────────
                         |
    Disengaged           |     Brute Forcer
    (low volume +        |     (high volume but
     low quality)        |      low acceptance)
                         |
                    Low Acceptance
         Low Volume                High Volume
```

- **Power User**: high interaction count, high acceptance rate. Getting the most value from AI.
- **Cautious Adopter**: low interaction count, high acceptance rate. Using AI selectively and effectively — may benefit from encouragement to use it more.
- **Brute Forcer**: high interaction count, low acceptance rate. Using AI heavily but not getting good results — may need prompt engineering guidance.
- **Disengaged**: low interaction count, low acceptance rate. Minimal effective AI usage — candidates for training or onboarding support.

Users with zero interactions always default to `disengaged`.

### Segments (Output Tiers)

Users are bucketed into five tiers based on their `intentional_ai_lines` output using percentile thresholds computed from the dataset:

| Segment | Criteria | Description |
|---------|----------|-------------|
| **Champion** | >= P95 | Top 5% of AI output. Leading the organization in AI adoption. |
| **Power User** | >= P75 | Top quartile. Consistent, high-volume AI usage. |
| **Regular** | >= P50 (median) | Above-median output. Solid, steady AI adopters. |
| **Casual** | > 0 | Some AI output, but below median. Room to grow. |
| **Inactive** | = 0 | Zero intentional AI lines. Not using AI for code generation. |

These thresholds are relative to the dataset — a "Regular" user in a highly active organization may produce more absolute output than a "Champion" in a less active one. The segmentation is designed for internal ranking and identifying who needs support, not for cross-organization comparison.

### Summary KPIs

The JSON summary provides organization-level metrics:

- **adoption_pct**: percentage of users found in at least one AI tool (Cursor or Copilot).
- **active_pct**: percentage of users with `intentional_ai_lines > 0` — they're not just licensed, they're producing AI-assisted code.
- **Percentile stats** (P75/P90/P95): identify where the top performers are. A large gap between median and P90 suggests a few power users while most are underutilizing.

## API Details

### Cursor Analytics API

- Base URL: `https://api.cursor.com/analytics`
- Auth: HTTP Basic with the API key as username and empty password
- Endpoints:
  - `/by-user/tabs?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&pageSize=500` — tab completion data per user per day
  - `/by-user/agent-edits?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&pageSize=500` — agent/chat diff data per user per day
- Date range limit: 30 days per request (the script automatically chunks longer ranges)

### GitHub Copilot Metrics API

- Base URL: `https://api.github.com`
- Auth: Bearer token (PAT with `manage_billing:copilot` and `read:enterprise` scopes)
- Endpoint: `/enterprises/{slug}/copilot/metrics/reports/users-1-day?day=YYYY-MM-DD`
- Returns `download_links` array pointing to JSONL files with per-user daily records
- Each JSONL record contains `user_login`, interaction counts, LOC totals, and `totals_by_feature` breakdowns
- Fetched one day at a time (API limitation)

## Cron Example

```bash
# Run weekly on Monday at 6am, 90-day window
0 6 * * 1 CURSOR_API_KEY=xxx GITHUB_TOKEN=xxx GITHUB_ENTERPRISE=myorg \
    python /opt/ai-leaderboard/ai_usage_analytics.py \
    --output /var/reports/ai_usage_$(date +\%Y\%m\%d)
```

## License

MIT
