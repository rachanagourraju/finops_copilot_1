import os
import re   
from pathlib import Path
from dotenv import load_dotenv
from google.cloud import bigquery


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    project = os.environ["FINANCE_GCP_PROJECT_ID"].strip()
    raw = os.getenv("FINANCE_BIGQUERY_RAW_DATASET", "finance_raw").strip()
    analytics = os.getenv("FINANCE_BIGQUERY_ANALYTICS_DATASET", "finance_analytics").strip()

    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("Set FINANCE_GCP_PROJECT_ID to your actual Google Cloud project ID.")
    if any(not re.fullmatch(r"[A-Za-z0-9_]{1,1024}", name) for name in (raw, analytics)):
        raise ValueError("Dataset names must contain only letters, digits, or underscores.")

    raw_id = f"{project}.{raw}"
    analytics_id = f"{project}.{analytics}"
    view_id = f"{analytics_id}.fct_monthly_spend"

    with bigquery.Client(project=project) as client:
        source = client.get_dataset(raw_id)
        target = bigquery.Dataset(analytics_id)
        target.location = source.location
        client.create_dataset(target, exists_ok=True)

        destination = client.get_dataset(analytics_id)
        if destination.location != source.location:
            raise ValueError(
                f"{analytics} is in {destination.location}, but {raw} is in {source.location}. "
                "Use an analytics dataset in the same location as the raw dataset."
            )

        create_view_sql = f"""
        CREATE VIEW IF NOT EXISTS {view_id} AS
        SELECT
            m.month_start AS reporting_month,
            b.cost_centre_id,
            c.centre_name AS cost_centre_name,
            b.account_id,
            CAST(a.account_no AS STRING) AS account_no,
            a.account_label,
            CAST(b.actual_amount AS NUMERIC) AS actual_amount,
            CAST(b.budget_amount AS NUMERIC) AS budget_amount,
            CAST(b.actual_amount AS NUMERIC)
                - CAST(b.budget_amount AS NUMERIC) AS variance_amount
        FROM {raw_id}.budget_lines AS b
        LEFT JOIN {raw_id}.months AS m ON b.month_id = m.id
        LEFT JOIN {raw_id}.cost_centres AS c ON b.cost_centre_id = c.id
        LEFT JOIN {raw_id}.accounts AS a ON b.account_id = a.id
        """
        client.query(create_view_sql, location=source.location).result()

        verify_sql = f"""
        SELECT
            COUNT(*) AS spend_rows,
            SUM(actual_amount) AS actual_spend,
            SUM(budget_amount) AS budget,
            SUM(variance_amount) AS variance
        FROM {view_id}
        WHERE reporting_month = DATE '2025-11-01'
        """
        row = next(iter(client.query(verify_sql, location=source.location).result()))
        print(f"View ready: {analytics}.fct_monthly_spend ({source.location})")
        print(f"November rows: {row.spend_rows}")
        if row.spend_rows:
            print(f"Actual spend: ${row.actual_spend:,.2f}")
            print(f"Budget: ${row.budget:,.2f}")
            print(f"Variance: ${row.variance:,.2f}")


if __name__ == "__main__":
    main()