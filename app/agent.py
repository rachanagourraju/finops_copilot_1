import json
import logging
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

import google.auth
from google.cloud import bigquery
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain.tools import ToolRuntime, tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel

from .charts import ChartType, make_chart
from .config import Settings
from .query import FinanceWarehouse, QueryError, QueryResult

logger = logging.getLogger(__name__)

INSTRUCTIONS = """
You are the Finance Operations Copilot. Use the finance tools for data-backed answers.

The query tool accepts a single GoogleSQL SELECT against the logical table
finance_spend. These are the available columns:
- reporting_month DATE, the first day of the reporting month
- cost_centre_id INT64, cost_centre_name STRING
- account_id INT64, account_no STRING, account_label STRING
- actual_amount NUMERIC, budget_amount NUMERIC, variance_amount NUMERIC

Each row represents one month, one cost centre, and one expense account.
The sample data covers January through December 2025.

Determine whether the question asks for a financial metric or a reference
attribute. For reference lookups, such as listing cost centres, use the
available columns and ask for a date only if the user requests a historical
or as-of answer. If a requested field such as budget_holder is absent from
the schema above, explain that the copilot has no access to that field.
Stop without calling a tool or asking for a date.

For period-dependent metrics, ask "Which month or date range should I use?"
if the user gives no period. Ask before calling any tool. Treat "all available
data" as a period. If the user names a month without a year, use 2025 and state
that year in the answer. For example, "in the month of August" means August
2025. Respect an explicitly requested year.

Use DATE literals and direct comparisons on reporting_month. Use the exact
stored labels for cost-centre filters. If needed, look up the label using
SELECT DISTINCT cost_centre_name FROM finance_spend ORDER BY cost_centre_name.
Avoid EXTRACT, DATE_TRUNC, FORMAT_DATE, LOWER, and UPPER. Joins, CTEs,
subqueries, and window functions are disabled.

Actual spend is SUM(actual_amount). Budget is SUM(budget_amount).
Expense variance is actual spend minus budget. Positive means overspend.
Calculate variance percentage after aggregation:
ROUND(100 * SAFE_DIVIDE(SUM(actual_amount) - SUM(budget_amount),
                       SUM(budget_amount)), 2).
A zero budget gives an undefined percentage.

For a single number, use an ungrouped aggregate with COUNT(*) AS matched_rows
and a clear alias for the requested metric. Here is the pattern for actual
spend for one cost centre in one month. Adapt the label and date to the question:

SELECT COUNT(*) AS matched_rows, SUM(actual_amount) AS actual_spend
FROM finance_spend
WHERE cost_centre_name = 'Customer support'
  AND reporting_month = DATE '2025-08-01'

If matched_rows is zero, report that no data matched. After getting a valid
single-number result, report the amount and period directly. A single-number
answer needs no chart unless the user asks for one and the data supports it.

For questions about budget variance drivers or actual-versus-budget
breakdowns, fetch a separate overall total using the same reporting scope
before ranking or applying a top-N limit. Use these exact totals aliases:

COUNT(*) AS matched_rows
SUM(actual_amount) AS actual_spend
SUM(budget_amount) AS budget
SUM(actual_amount) - SUM(budget_amount) AS variance_amount
ROUND(100 * SAFE_DIVIDE(SUM(actual_amount) - SUM(budget_amount),
                       SUM(budget_amount)), 2) AS variance_pct

For multi-row financial comparisons, use create_finance_chart with the actual
result ID and column names. Use horizontal_bar for rankings, line for trends,
and bar for other comparisons. Return reporting_month as a DATE for time
charts. Keep overall totals out of the breakdown chart. A plain reference
lookup needs only a text answer.

If the query tool reports an unsupported function, rewrite the filter using
direct comparisons and DATE literals. Make at most one corrected attempt
for a failed query. After a second failure, explain the problem and stop.
If a chart fails, explain the available figures and say the chart could
not be generated.

Base factual claims on successful tool results. Describe observed drivers
without inventing causes. For a breakdown, give up to five plain-text
takeaways, each on its own line starting with "- ". Use a plain sentence
for single numbers, clarifications, unavailable fields, and errors.
"""


@dataclass
class FinanceRun:
    warehouse: FinanceWarehouse
    results: dict[str, QueryResult] = field(default_factory=dict)
    charts: list[dict] = field(default_factory=list)
    query_attempted: bool = False


class QueryOutput(BaseModel):
    result_id: str
    sql: str
    rows: list[dict]


class ChartOutput(BaseModel):
    result_id: str
    figure: dict


class FinanceResponse(BaseModel):
    status: Literal["answered", "no_data", "clarification", "query_failed", "chart_failed"]
    explanation: str
    queries: list[QueryOutput]
    charts: list[ChartOutput]


@tool
def run_finance_query(sql: str, runtime: ToolRuntime[FinanceRun]) -> str:
    """Run one GoogleSQL SELECT against the logical finance_spend table."""
    run = runtime.context
    run.query_attempted = True
    try:
        result = run.warehouse.run(sql)
    except QueryError as exc:
        logger.warning("Finance query rejected: %s", exc)
        return json.dumps({
            "error": str(exc),
            "hint": (
                "Use direct comparisons and DATE literals for filters. "
                "Check the schema and make one corrected attempt."
            ),
        })

    result_id = uuid4().hex
    run.results[result_id] = result
    return json.dumps({"result_id": result_id, "rows": result.rows})


@tool
def create_finance_chart(
    result_id: str,
    chart_type: ChartType,
    category: str,
    value: str,
    runtime: ToolRuntime[FinanceRun],
) -> str:
    """Build a chart from a successful query result and two of its columns."""
    run = runtime.context
    result = run.results.get(result_id)
    if result is None:
        return json.dumps({"error": "Use a result_id returned in this request."})

    try:
        figure = make_chart(result.rows, chart_type, category, value)
    except ValueError as exc:
        logger.warning("Finance chart rejected: %s", exc)
        return json.dumps({"error": str(exc)})

    run.charts.append({"result_id": result_id, "figure": figure})
    return json.dumps({"chart_created": True, "result_id": result_id})


class FinanceCopilot:
    def __init__(self, warehouse: FinanceWarehouse, model):
        self.warehouse = warehouse
        self.agent = create_agent(
            model=model,
            tools=[run_finance_query, create_finance_chart],
            system_prompt=INSTRUCTIONS,
            context_schema=FinanceRun,
            middleware=[
                ModelCallLimitMiddleware(run_limit=8, exit_behavior="end"),
                ToolCallLimitMiddleware(
                    tool_name="run_finance_query", run_limit=4, exit_behavior="error"
                ),
                ToolCallLimitMiddleware(
                    tool_name="create_finance_chart", run_limit=2, exit_behavior="error"
                ),
            ],
        )

    def ask(self, question: str) -> FinanceResponse:
        run = FinanceRun(warehouse=self.warehouse)
        try:
            result = self.agent.invoke(
                {"messages": [{"role": "user", "content": question}]},
                context=run,
                config={"recursion_limit": 64},
            )
            explanation = result["messages"][-1].text.strip()
        except (GraphRecursionError, ToolCallLimitExceededError) as exc:
            logger.warning("Finance copilot reached a run limit: %s", type(exc).__name__)
            status = "query_failed"
            explanation = "The analysis stopped after reaching its call limit."
        else:
            if not run.results:
                status = "query_failed" if run.query_attempted else "clarification"
            elif not any(
                row.get("matched_rows") != 0
                for query in run.results.values()
                for row in query.rows
            ):
                status = "no_data"
                explanation = "No finance rows matched the requested filters."
            else:
                status = "answered"

        return FinanceResponse(
            status=status,
            explanation=explanation or "See the query results for the available figures.",
            queries=[
                QueryOutput(result_id=result_id, sql=query.sql, rows=query.rows)
                for result_id, query in run.results.items()
            ],
            charts=[ChartOutput(**chart) for chart in run.charts],
        )


def create_copilot(settings: Settings, client: bigquery.Client) -> FinanceCopilot:
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    model = ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        vertexai=True,
        credentials=credentials,
        project=settings.gcp_project_id,
        location=settings.vertex_location,
        temperature=0,
        max_output_tokens=2_048,
        timeout=25,
        max_retries=1,
    )
    return FinanceCopilot(FinanceWarehouse(client, settings), model)