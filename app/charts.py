import json
from math import isfinite
from typing import Literal

import plotly.graph_objects as go

ChartType = Literal["horizontal_bar", "bar", "line"]


def make_chart(
    rows: list[dict],
    chart_type: ChartType,
    category: str,
    value: str,
) -> dict:
    if not rows or category == value:
        raise ValueError("Choose two distinct fields from a query with rows.")

    if any(
        category not in row or value not in row or row[category] is None
        for row in rows
    ):
        raise ValueError(
            "Choose an existing category without nulls and an existing value field."
        )

    labels = [str(row[category]) for row in rows]
    if len(set(labels)) != len(labels):
        raise ValueError(
            "Chart categories must be unique. Group the query by the chart category."
        )

    try:
        values = [None if row[value] is None else float(row[value]) for row in rows]
    except (TypeError, ValueError, OverflowError):
        raise ValueError("The value field must be numeric.") from None

    if not any(number is not None for number in values) or any(
        number is not None and not isfinite(number) for number in values
    ):
        raise ValueError("The value field must contain finite numbers.")

    category_title = category.replace("_", " ").capitalize()
    value_title = value.replace("_", " ").capitalize()

    if chart_type == "horizontal_bar":
        ordered = sorted(
            zip(labels, values),
            key=lambda point: (point[1] is None, -(point[1] or 0)),
        )
        labels, values = map(list, zip(*ordered))
        figure = go.Figure(go.Bar(x=values, y=labels, orientation="h"))
        figure.update_layout(xaxis_title=value_title, yaxis_title=category_title)
        figure.update_yaxes(autorange="reversed")
    elif chart_type == "line":
        ordered = sorted(zip(labels, values), key=lambda point: point[0])
        labels, values = map(list, zip(*ordered))
        figure = go.Figure(go.Scatter(x=labels, y=values, mode="lines+markers"))
        figure.update_layout(xaxis_title=category_title, yaxis_title=value_title)
    else:
        figure = go.Figure(go.Bar(x=labels, y=values))
        figure.update_layout(xaxis_title=category_title, yaxis_title=value_title)

    figure.update_layout(
        title=f"{value_title} by {category_title}",
        template="plotly_white",
    )
    return json.loads(figure.to_json())