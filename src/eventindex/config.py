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
MCP_USAGE_HMAC_KEY = os.environ.get("MCP_USAGE_HMAC_KEY", "")

# LLM (DECISIONS.md: one provider = OpenRouter; model names live here)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_RESUME_MIN_USD = 0.25
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
MODEL_MINI = "deepseek/deepseek-v4-flash"   # $0.14/$0.28 per M, 1M ctx, text-only
MODEL_MID = "minimax/minimax-m3"            # $0.30/$1.20, 1M ctx, text+image+video
# frontier re-added 2026-07-11 (was dropped 2026-07-07 as unused): the final
# onboarding attempt on gate-heavy sites needs it - mid wall-clocked 4x on a
# row-capped JSF portal while satisfying coverage+horizon+window constraints
MODEL_FRONTIER = "z-ai/glm-5.2"
# vision/PDF fence fired 2026-07-20 (Alexander: human-parity extraction is
# the requirement): mid is multimodal, so posters/screenshots ride on it.
MODEL_VISION = MODEL_MID
LLM_MAX_OUTPUT_TOKENS = 16000  # event-list pages produce long array outputs
USD_TO_EUR = 0.90  # OpenRouter reports cost in USD credits

# Publication-gating audience essentials are intentionally tiny and batched.
# Worst-case reviewed request: 20 x (title + 600 description chars + metadata)
# is under ~8k input tokens and 4k output tokens. At MODEL_MINI's reviewed
# ceiling that is < EUR 0.003; EUR 0.01 leaves >3x headroom without making a
# cheap mandatory call reserve the generic EUR 0.20 maximum.
AUDIENCE_ESSENTIALS_BATCH_SIZE = 20
AUDIENCE_ESSENTIALS_DESCRIPTION_CHARS = 600
AUDIENCE_ESSENTIALS_MAX_OUTPUT_TOKENS = 4000
AUDIENCE_ESSENTIALS_RESERVATION_EUR = 0.01

# Paid-provider budgets (Alexander 2026-08-05: hard ceiling below $3/day).
# EUR 2.40 is about USD 2.67 at the ledger FX and deliberately leaves room
# below $3 for FX drift and OpenRouter's credit-purchase fee.  Google Places
# shadow charges share this same envelope.
GLOBAL_DAILY_PAID_CAP_EUR = 2.40
# Bulk fact/backfill queues may use at most this much of the total. They cannot
# consume the other EUR 1.50; routine work and interactive search share that.
RECOVERY_DAILY_PAID_CAP_EUR = 0.90
# Natural-language /v1/search is a paid convenience; /v1/query is free.
INTERACTIVE_DAILY_PAID_CAP_EUR = 0.40
# Conservative maximum reservation per request.  Each exceeds the theoretical
# full-context + 16k-output cost of its configured model at the prices above.
LLM_RESERVATION_EUR_BY_MODEL = {
    MODEL_MINI: 0.20,
    MODEL_MID: 0.40,
    MODEL_FRONTIER: 0.80,
}
# An unreviewed model must reserve the entire day; known models above have
# tighter ceilings backed by their maximum context/output prices.
LLM_UNKNOWN_MODEL_RESERVATION_EUR = GLOBAL_DAILY_PAID_CAP_EUR
# Provider routing rejects an endpoint whose advertised token price exceeds
# these reviewed maxima (USD per million tokens). A price change fails closed.
LLM_MAX_PRICE_USD_PER_M_BY_MODEL = {
    MODEL_MINI: {"prompt": 0.14, "completion": 0.28},
    MODEL_MID: {"prompt": 0.30, "completion": 1.20},
    MODEL_FRONTIER: {"prompt": 0.76, "completion": 2.42},
}
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
USER_AGENT = "EventIndexBot/0.1 (+alexander@business.goedly.com)"
CRAWL_DELAY_S = 2.0  # DECISIONS.md: per-domain rate limit >= 2s

# Discovery: sweeps skip domains rejected by a probe within this window
# (directly enqueued probe jobs still run, so a re-probe can be forced;
# after a classifier fix the window expiring re-heals wrong rejections)
PROBE_REJECT_TTL_DAYS = 90

# QA loop (§12: nightly random re-verification feeding source trust)
QA_NIGHTLY_SAMPLE = 20
QA_TRUST_ALPHA = 0.1  # trust <- (1-a)*trust + a*accuracy per check

# Human-parity audit (2026-07-20): weekly agent re-extraction of sampled
# recipe sources; the requirement is a watched number, not a one-time claim
PARITY_SAMPLE = 3           # sources per weekly audit (~EUR 1/week ceiling)
PARITY_MIN_COVERAGE = 0.7   # below this, misses feed the source's notes

# Digest
DIGEST_DIR = VAR_DIR / "digests"
DEAD_MAN_HOURS = 48
CREDITS_WARN_USD = 3.0  # less than one maximum-cost day remains
MCP_USAGE_RETENTION_DAYS = 30

TIMEZONE = "Europe/Vienna"

# Top-level taxonomy seed (§8: two-level, ~15 top; sub-categories come with
# the enrichment pass in phase 4)
CATEGORIES = [
    "music", "nightlife", "theatre", "film", "art", "culture", "sport",
    "community", "learning", "family", "market", "food_drink", "tech",
    "religion", "other",
]
