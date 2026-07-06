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
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")  # Programmable Search Engine id
# dedicated search key if set, else the Places key (same project, API enabled)
GOOGLE_SEARCH_API_KEY = (
    os.environ.get("GOOGLE_SEARCH_API_KEY", "")
    or os.environ.get("GOOGLE_CSE_API_KEY", "")
    or os.environ.get("GOOGLE_PLACES_API_KEY", "")
)

# LLM (DECISIONS.md: one provider = OpenRouter; model names live here)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL_MINI = "openai/gpt-5-mini"
MODEL_MID = "anthropic/claude-sonnet-4.5"
MODEL_FRONTIER = "anthropic/claude-opus-4.5"
LLM_MAX_OUTPUT_TOKENS = 16000  # event-list pages produce long array outputs
USD_TO_EUR = 0.90  # OpenRouter reports cost in USD credits

# Budgets (DECISIONS.md: enforced in code from day one)
GLOBAL_DAILY_LLM_CAP_EUR = 5.0
MONTHLY_BUDGET_EUR_BY_TIER = {1: 2.0, 2: 1.0, 3: 1.0, 4: 3.0}
# Fallback when OpenRouter omits cost in the response: deliberately pessimistic.
FALLBACK_EUR_PER_1K_TOKENS = 0.005

# Onboarding agent (§5b / §harness): budget enforced by the loop, not the model
ONBOARD_MAX_TURNS = 25
ONBOARD_SESSION_CAP_EUR = 0.60  # H3.5: one-time per source; hard sites cost more
ONBOARD_WALL_CLOCK_S = 600
TRAJECTORY_DIR = VAR_DIR / "trajectories"

# Worker
JOB_MAX_ATTEMPTS = 3
JOB_RETRY_BACKOFF_S = 60  # attempt n retries after 60 * 5^(n-1) seconds
JOB_STALE_RUNNING_S = 3600  # running jobs older than this are requeued at startup
WORKER_IDLE_POLL_S = 5

# Crawl politeness
USER_AGENT = "EventIndexBot/0.1 (+alexander.krauck@gmail.com)"
CRAWL_DELAY_S = 2.0  # DECISIONS.md: per-domain rate limit >= 2s

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
