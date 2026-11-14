import argparse
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import plotly.io as pio
from google.cloud import bigquery

from .agent import FinanceCopilot, create_copilot
from .config import load_settings

DEFAULT_QUESTION = "Which cost centres drove the November 2025 overspend?"


@contextmanager
def open_copilot() -> Iterator[FinanceCopilot]:
    settings = load_settings()
    with bigquery.Client(
        project=settings.gcp_project_id,
        location=settings.bigquery_location,
    ) as client:
        yield create_copilot(settings, client)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask the Finance Copilot a question.")
    parser.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    parser.add_argument("--open", action="store_true", help="Open the generated charts.")
    args = parser.parse_args()

    question = args.question.strip()
    if not 1 <= len(question) <= 2_000:
        parser.error("Enter a question between 1 and 2,000 characters.")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    with open_copilot() as copilot:
        result = copilot.ask(question)

    print(result.model_dump_json(indent=2, exclude={"charts"}))
    for index, chart in enumerate(result.charts, start=1):
        output = Path(f"finance_chart_{index}.html").resolve()
        pio.write_html(chart.figure, str(output), auto_open=args.open)
        print(f"Chart: {output}")

    return int(result.status in {"query_failed", "chart_failed"})


if __name__ == "__main__":
    raise SystemExit(main())