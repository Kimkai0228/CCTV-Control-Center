"""Dashboard report widgets shared by every user.

The layout is stored in the ``dashboard.reports`` setting so all logged-in
users see the same charts. Only superadmins/admins can change it.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Any

from sqlalchemy.orm import Session

from ..settings_service import get_setting

SETTINGS_KEY = "dashboard.reports"
MAX_REPORTS = 10
ALLOWED_DAYS = {7, 14, 30, 90}
MAX_CUSTOM_DAYS = 366
PIE_METRICS = {"source", "campus", "status", "agent"}
AREA_METRICS = {"total", "source", "campus", "status", "agent"}

DEFAULT_REPORTS: list[dict[str, Any]] = [
    {
        "id": "sources-pie",
        "type": "pie",
        "metric": "source",
        "title": "Jobs by source",
        "days": 30,
        "span": 1,
        "range": "days",
        "start_date": None,
        "end_date": None,
    },
    {
        "id": "activity-area",
        "type": "area",
        "metric": "source",
        "title": "Requests over time",
        "days": 14,
        "span": 1,
        "range": "days",
        "start_date": None,
        "end_date": None,
    },
]

_TITLES = {
    "source": "Jobs by source",
    "campus": "Jobs by campus",
    "status": "Jobs by status",
    "agent": "Local vs campus agent",
    "total": "Requests over time",
}


def default_reports() -> list[dict[str, Any]]:
    return copy.deepcopy(DEFAULT_REPORTS)


def normalize_reports(raw: Any) -> list[dict[str, Any]]:
    """Validate a client-supplied layout and fill safe defaults."""
    if not isinstance(raw, list):
        raise ValueError("Reports must be a list")
    if len(raw) > MAX_REPORTS:
        raise ValueError(f"At most {MAX_REPORTS} charts are allowed")

    cleaned: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Chart {index + 1} is invalid")
        chart_type = str(item.get("type") or "pie").strip().lower()
        if chart_type not in {"pie", "area"}:
            raise ValueError(f"Chart {index + 1}: type must be pie or area")
        metric = str(item.get("metric") or "").strip().lower()
        allowed = PIE_METRICS if chart_type == "pie" else AREA_METRICS
        if not metric:
            metric = "source" if chart_type == "pie" else "total"
        if metric not in allowed:
            raise ValueError(
                f"Chart {index + 1}: {metric} is not available for a {chart_type} chart"
            )
        chart_range = str(item.get("range") or "days").strip().lower()
        if chart_range not in {"days", "custom"}:
            chart_range = "days"
        start_date: str | None = None
        end_date: str | None = None
        if chart_range == "custom":
            raw_start = str(item.get("start_date") or "").strip()
            raw_end = str(item.get("end_date") or "").strip()
            try:
                start_obj = dt.date.fromisoformat(raw_start)
                end_obj = dt.date.fromisoformat(raw_end)
            except ValueError:
                raise ValueError(f"Chart {index + 1}: choose a valid start and end date")
            if start_obj > end_obj:
                raise ValueError(
                    f"Chart {index + 1}: the end date must be on or after the start date"
                )
            span_days = (end_obj - start_obj).days + 1
            if span_days > MAX_CUSTOM_DAYS:
                raise ValueError(
                    f"Chart {index + 1}: custom range cannot exceed {MAX_CUSTOM_DAYS} days"
                )
            days = span_days
            start_date = start_obj.isoformat()
            end_date = end_obj.isoformat()
        else:
            chart_range = "days"
            try:
                days = int(item.get("days") or 30)
            except (TypeError, ValueError):
                raise ValueError(f"Chart {index + 1}: invalid time range")
            if days not in ALLOWED_DAYS:
                raise ValueError(
                    f"Chart {index + 1}: time range must be 7, 14, 30 or 90 days"
                )
        try:
            span = int(item.get("span") or 1)
        except (TypeError, ValueError):
            span = 1
        if span not in {1, 2}:
            span = 1

        title = str(item.get("title") or "").strip()[:80]
        if not title:
            title = _TITLES.get(metric, "Report")

        chart_id = str(item.get("id") or "").strip()[:60]
        if not chart_id or chart_id in used_ids:
            chart_id = f"chart-{index + 1}"
            suffix = 2
            while chart_id in used_ids:
                chart_id = f"chart-{index + 1}-{suffix}"
                suffix += 1
        used_ids.add(chart_id)

        cleaned.append(
            {
                "id": chart_id,
                "type": chart_type,
                "metric": metric,
                "title": title,
                "days": days,
                "span": span,
                "range": chart_range,
                "start_date": start_date,
                "end_date": end_date,
            }
        )
    return cleaned


def get_reports(db: Session) -> list[dict[str, Any]]:
    raw = get_setting(db, SETTINGS_KEY, "")
    if not raw:
        return default_reports()
    try:
        return normalize_reports(json.loads(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default_reports()
