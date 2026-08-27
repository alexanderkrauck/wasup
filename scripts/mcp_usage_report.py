#!/usr/bin/env python3
"""Print privacy-safe MCP adoption metrics for the retained window."""

import argparse

from eventindex import config, db
from eventindex.api.mcp_usage import render_usage_report, usage_report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP calls and observed pseudonymous users by client/tool"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        choices=range(1, config.MCP_USAGE_RETENTION_DAYS + 1),
        metavar=f"1-{config.MCP_USAGE_RETENTION_DAYS}",
    )
    args = parser.parse_args()
    with db.connect() as conn:
        report = usage_report(conn, days=args.days)
    print(render_usage_report(report), end="")


if __name__ == "__main__":
    main()
