import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from google.api_core.exceptions import BadRequest, GoogleAPIError
from google.cloud import bigquery
from sqlglot import exp, parse
from sqlglot.errors import ErrorLevel, SqlglotError

from .config import Settings

logger = logging.getLogger(__name__)

APPROVED_FUNCTIONS = {
    "SUM", "COUNT", "COUNT_IF", "MIN", "MAX", "AVG", "ROUND",
    "SAFE_DIVIDE", "COALESCE", "NULLIF", "CAST", "TRY_CAST",
    "ABS", "IF", "CASE",
}
APPROVED_SELECT_ARGS = {
    "expressions", "from_", "where", "group",
    "having", "order", "limit", "distinct",
}


class QueryError(ValueError):
    """A query error safe to return to the caller."""


@dataclass(frozen=True)
class QueryResult:
    sql: str
    rows: list[dict]


def compile_query(sql: str, view: str, max_rows: int) -> str:
    if not sql.strip() or len(sql) > 8_000:
        raise QueryError("Provide one SELECT of up to 8,000 characters.")

    try:
        statements = parse(sql, read="bigquery")
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise QueryError("Only one SELECT is permitted.")

        statement = statements[0]
        if any(
            value
            for key, value in statement.args.items()
            if key not in APPROVED_SELECT_ARGS
        ):
            raise QueryError("Use a single SELECT without joins or CTEs.")

        source = statement.args.get("from_")
        table = source.this if source else None
        if (
            not isinstance(table, exp.Table)
            or not isinstance(table.this, exp.Identifier)
            or table.name.lower() != "finance_spend"
            or any(
                value
                for key, value in table.args.items()
                if key not in {"this", "alias"}
            )
        ):
            raise QueryError("Query the unqualified source finance_spend.")

        for node in statement.walk():
            if node is not statement and isinstance(node, exp.Query):
                raise QueryError("Subqueries are disabled.")
            if isinstance(node, (exp.Dot, exp.Window, exp.Parameter, exp.Placeholder)):
                raise QueryError(
                    "Qualified functions, window expressions, and placeholders are disabled."
                )
            # SQLGlot includes AND and OR in its function-node hierarchy.
            if isinstance(node, exp.Func) and not isinstance(node, (exp.And, exp.Or)):
                if isinstance(node, exp.Anonymous):
                    raise QueryError("Unrecognized SQL functions are disabled.")
                if node.sql_name() not in APPROVED_FUNCTIONS:
                    raise QueryError(f"Unsupported SQL function: {node.sql_name()}.")

        if any(candidate is not table for candidate in statement.find_all(exp.Table)):
            raise QueryError("Only the finance_spend source is permitted.")

        limit = statement.args.get("limit")
        if limit is None:
            # Read one extra row to detect an oversized result.
            statement.limit(max_rows + 1, copy=False)
        elif not isinstance(limit.expression, exp.Literal) or not limit.expression.is_int:
            raise QueryError("Use a positive integer LIMIT.")
        elif not 0 < int(limit.expression.this) <= max_rows:
            raise QueryError(f"Use a LIMIT from 1 to {max_rows}.")

        project, dataset, name = view.split(".")
        table.set("catalog", exp.to_identifier(project, quoted=True))
        table.set("db", exp.to_identifier(dataset, quoted=True))
        table.set("this", exp.to_identifier(name, quoted=True))
        return statement.sql(
            dialect="bigquery",
            comments=False,
            unsupported_level=ErrorLevel.RAISE,
        )
    except SqlglotError:
        raise QueryError("The SQL could not be parsed as supported GoogleSQL.") from None


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class FinanceWarehouse:
    def __init__(self, client: bigquery.Client, settings: Settings):
        self.client = client
        self.settings = settings

    def run(self, sql: str) -> QueryResult:
        compiled = compile_query(sql, self.settings.view, self.settings.max_rows)
        timeout = self.settings.query_timeout_seconds
        config = bigquery.QueryJobConfig(
            use_legacy_sql=False,
            maximum_bytes_billed=self.settings.max_bytes_billed,
            job_timeout_ms=timeout * 1_000,
            labels={"app": "finance-copilot"},
        )

        job = None
        try:
            job = self.client.query(
                compiled, job_config=config, timeout=timeout, job_retry=None
            )
            iterator = job.result(
                timeout=timeout, max_results=self.settings.max_rows + 1
            )
            rows = [
                {key: json_value(value) for key, value in row.items()}
                for row in iterator
            ]
        except TimeoutError:
            if job is not None:
                try:
                    job.cancel(timeout=3)
                except (GoogleAPIError, TimeoutError):
                    logger.warning("BigQuery cancellation request failed")
            raise QueryError("Query timed out. Try a narrower date range.") from None
        except BadRequest:
            raise QueryError(
                "BigQuery rejected the query or scan budget. Check the query and simplify it."
            ) from None
        except GoogleAPIError as exc:
            logger.warning("BigQuery request failed: %s", type(exc).__name__)
            raise QueryError(
                "BigQuery could not complete the query. Check backend access."
            ) from None

        if len(rows) > self.settings.max_rows:
            raise QueryError(
                "Too many rows. Aggregate further or use an explicit top-N limit."
            )

        logger.info(
            "BigQuery job=%s rows=%d bytes=%s",
            job.job_id,
            len(rows),
            job.total_bytes_billed,
        )
        # Return the logical table name to keep the project ID out of responses.
        return QueryResult(sql=sql, rows=rows)