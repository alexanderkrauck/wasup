"""All knobs and constants in one place (CLAUDE.md: no config sprawl).

Secrets come from .env; everything else is a constant here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
VAR_DIR = ROOT / "var"
MIGRATIONS_DIR = ROOT / "db" / "migrations"

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://eventindex:eventindex@localhost:5432/eventindex"
)
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

# LLM (DECISIONS.md: one provider = OpenRouter; model names live here)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# swapped to open-weight models 2026-07-07 (Alexander): ~4x cheaper per day
# at yesterday's volume; the validation nets (pydantic schemas, recipe
# self-validation, verify-calls, gold set) are what guarantee quality, not
# model brand (ARCHITECTURE §model-routing).
# Two tiers suffice (Alexander 2026-07-07); a frontier tier had zero call
# sites and was deleted - re-add if tier-D crawls ever unfence.
# When the PDF/flyer trigger fires: kimi (mid) already sees images;
# qwen/qwen3.6-flash ($0.19/$1.13, multimodal) is the vision-mini candidate.
# 2026-07-08 re-check: mini is the consensus cheapest-capable model, keep.
# Mid swapped kimi-k2.7-code ($0.74/$3.50) -> minimax-m3 ($0.30/$1.20):
# ~2.5-3x cheaper on the dominant onboarding spend, strong agentic scores,
# multimodal (keeps the PDF/vision option). Kimi is the fallback if the
# recipe-success rate in the digest degrades.
MODEL_MINI = "deepseek/deepseek-v4-flash"   # $0.09/$0.18 per M, 1M ctx, text-only
MODEL_MID = "minimax/minimax-m3"            # $0.30/$1.20, 1M ctx, text+image+video
# frontier re-added 2026-07-11 (was dropped 2026-07-07 as unused): the final
# onboarding attempt on gate-heavy sites needs it - mid wall-clocked 4x on a
# row-capped JSF portal while satisfying coverage+horizon+window constraints
MODEL_FRONTIER = "z-ai/glm-5.2"
LLM_MAX_OUTPUT_TOKENS = 16000  # event-list pages produce long array outputs
USD_TO_EUR = 0.90  # OpenRouter reports cost in USD credits

# Budgets (DECISIONS.md: enforced in code from day one)
GLOBAL_DAILY_LLM_CAP_EUR = 15.0  # 5->10 (2026-07-06), 10->15 (2026-07-08): onboarding backlog
MONTHLY_BUDGET_EUR_BY_TIER = {1: 2.0, 2: 1.0, 3: 1.0, 4: 3.0}
# Fallback when OpenRouter omits cost in the response: deliberately pessimistic.
FALLBACK_EUR_PER_1K_TOKENS = 0.005

# Completeness contract (Alexander 2026-07-07: if events are findable
# without a login wall, we get them - incomplete feeds get an agent thrown
# at the site): productive sources whose yield horizon stays short are
# flagged and escalated once.
COMPLETENESS_MIN_YIELD = 10       # only productive sources can be "capped"
HORIZON_CAPPED_DAYS = 10          # yield never reaching past this = suspicious
RECIPE_MIN_HORIZON_DAYS = 21      # escalated recipes must reach at least this

# Onboarding agent (§5b / §harness): budget enforced by the loop, not the model.
# Base rings; when a session approaches one, a value checkpoint (Alexander
# 2026-07-08) asks the agent for its expected yield IN the cached conversation
# and a deterministic gate scales the rings - worth is expected_events x
# EUR_PER_EXPECTED_EVENT, clamped to the hard rings. The model provides
# evidence, the code decides; a lying model still can't pass the hard rings.
ONBOARD_MAX_TURNS = 25
ONBOARD_SESSION_CAP_EUR = 0.60  # H3.5: one-time per source; hard sites cost more
ONBOARD_WALL_CLOCK_S = 1500  # 600->1000 (2026-07-08); ->1500: gate validations run inside turns (2026-07-11)
ONBOARD_EUR_PER_EXPECTED_EVENT = 0.03  # one-time spend justified per expected event/crawl
ONBOARD_HARD_CAP_EUR = 2.50
ONBOARD_HARD_MAX_TURNS = 60
# 3600: validation got heavier (headless trimmed runs + deep probes eat wall
# clock inside agent turns); 1800 killed a converging session 2026-07-11
ONBOARD_HARD_WALL_CLOCK_S = 3600
TRAJECTORY_DIR = VAR_DIR / "trajectories"

# Worker
JOB_MAX_ATTEMPTS = 3
JOB_RETRY_BACKOFF_S = 60  # attempt n retries after 60 * 5^(n-1) seconds
# must exceed the worst-case legitimate job (a 60-page + 60-detail recipe
# crawl at 2s politeness plus LLM extraction runs well past an hour) -
# requeueing a LIVE job double-runs it: double spend, interleaved claims
JOB_STALE_RUNNING_S = 4 * 3600
WORKER_IDLE_POLL_S = 5

# Crawl politeness
USER_AGENT = "EventIndexBot/0.1 (+alexander.krauck@gmail.com)"
CRAWL_DELAY_S = 2.0  # DECISIONS.md: per-domain rate limit >= 2s

# QA loop (§12: nightly random re-verification feeding source trust)
QA_NIGHTLY_SAMPLE = 20
QA_TRUST_ALPHA = 0.1  # trust <- (1-a)*trust + a*accuracy per check

# Digest
DIGEST_DIR = VAR_DIR / "digests"
DEAD_MAN_HOURS = 48

TIMEZONE = "Europe/Vienna"

# Top-level taxonomy seed (§8: two-level, ~15 top; sub-categories come with
# the enrichment pass in phase 4)
CATEGORIES = [
    "music", "nightlife", "theatre", "film", "art", "culture", "sport",
    "community", "learning", "family", "market", "food_drink", "tech",
    "religion", "other",
]
