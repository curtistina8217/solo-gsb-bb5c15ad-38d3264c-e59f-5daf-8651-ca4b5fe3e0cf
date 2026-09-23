"""Concentrate consumption forecasting for a saved recipe version.

Given a recipe version's solve result (which fixes, for the target working
solution volume ``V``, how many mL of each concentrate are needed), a
forecast expands an irrigation schedule into **actual batches** with the
same engine as ``/api/irrigation/plan`` and walks them in chronological
order:

* concentrate ``i`` is drawn proportionally to each batch's water:

      required_i = batch_water_ml * allocation_i / V

  so merged batches are charged once for their summed water, and a
  cross-midnight session is charged exactly once (it is a single batch);
* batches are charged to the **local calendar day on which they start**
  (``local_start``'s date). A batch merged across midnight is therefore
  counted once, on its first day, never split or double-counted;
* stocks are consumed cumulatively. When a bottle cannot cover a batch
  the batch still carries the *full* predicted ``required_ml`` while
  ``served_ml``/``shortfall_ml`` record what the bottle can cover; the
  running stock is clamped at zero — **negative stock never appears**;
* the first local day on which any concentrate runs short is reported as
  ``first_shortage_date`` and the whole forecast is kept, batch by batch.
"""
from __future__ import annotations

from typing import Any, Optional

_EPS = 1e-9


def _r6(v: float) -> float:
    """Round output volumes while normalizing floating-point dust to zero."""
    v = round(float(v), 6)
    return 0.0 if abs(v) < 0.5e-6 else v


def _zeros(names: list[str]) -> dict[str, float]:
    return {n: 0.0 for n in names}


def build_forecast(
    *,
    recipe_id: int,
    version: int,
    version_payload: dict[str, Any],
    stocks: dict[str, float],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Build the consumption-forecast response structure.

    ``stocks`` must already be validated to match the version's
    concentrate names exactly; ``plan`` is a dict produced by
    :func:`app.irrigation.build_plan`.
    """
    result = version_payload["result"]
    mix_total = float(result["total_volume_ml"])
    allocations = result["allocations"]
    names = [str(a["concentrate"]) for a in allocations]
    alloc_ml = [float(a["volume_ml"]) for a in allocations]
    ratios = [x / mix_total for x in alloc_ml]

    dosage = [
        {
            "concentrate": names[i],
            "allocation_ml": _r6(alloc_ml[i]),
            "mix_total_ml": _r6(mix_total),
            "ratio": ratios[i],
            "ml_per_liter_water": _r6(1000.0 * ratios[i]),
        }
        for i in range(len(names))
    ]

    stock = {n: float(stocks[n]) for n in names}
    initial = {n: stock[n] for n in names}

    total_water = 0.0
    total_required = _zeros(names)
    total_served = _zeros(names)
    total_shortfall = _zeros(names)
    first_shortage_date: Optional[str] = None

    batches_out: list[dict[str, Any]] = []
    # Preserve first-seen order; batches arrive sorted by UTC start, which is
    # also chronological local-start order except across offset changes, where
    # attribution by the batch's own local date is still unambiguous.
    day_buckets: dict[str, dict[str, Any]] = {}
    day_order: list[str] = []

    for b in plan["batches"]:
        water = float(b["water_ml"])
        day = str(b["local_start"])[:10]
        required = {names[i]: water * ratios[i] for i in range(len(names))}

        served: dict[str, float] = {}
        shortfall: dict[str, float] = {}
        batch_shortages: list[dict[str, float]] = []
        for n in names:
            need = required[n]
            got = min(need, stock[n])
            miss = need - got
            served[n] = got
            shortfall[n] = miss
            stock[n] = max(0.0, stock[n] - got)
            if miss > _EPS:
                batch_shortages.append(
                    {
                        "concentrate": n,
                        "required_ml": _r6(need),
                        "served_ml": _r6(got),
                        "shortfall_ml": _r6(miss),
                    }
                )

        sufficient = not batch_shortages
        if not sufficient and first_shortage_date is None:
            first_shortage_date = day

        end_stock = {n: stock[n] for n in names}
        batches_out.append(
            {
                "date": day,
                "start_utc": b["start_utc"],
                "local_start": b["local_start"],
                "local_end": b["local_end"],
                "water_ml": _r6(water),
                "merged": bool(b["merged"]),
                "batch_count_before_merge": b["batch_count_before_merge"],
                "crosses_midnight": bool(b["crosses_midnight"]),
                "required_ml": {n: _r6(required[n]) for n in names},
                "served_ml": {n: _r6(served[n]) for n in names},
                "shortfall_ml": {n: _r6(shortfall[n]) for n in names},
                "end_stock_ml": {n: _r6(end_stock[n]) for n in names},
                "stock_sufficient": sufficient,
                "shortages": batch_shortages,
            }
        )

        total_water += water
        for n in names:
            total_required[n] += required[n]
            total_served[n] += served[n]
            total_shortfall[n] += shortfall[n]

        if day not in day_buckets:
            day_order.append(day)
            day_buckets[day] = {
                "date": day,
                "batch_count": 0,
                "water_ml": 0.0,
                "required_ml": _zeros(names),
                "served_ml": _zeros(names),
                "shortfall_ml": _zeros(names),
                "_sufficient": True,
            }
        bucket = day_buckets[day]
        bucket["batch_count"] += 1
        bucket["water_ml"] += water
        for n in names:
            bucket["required_ml"][n] += required[n]
            bucket["served_ml"][n] += served[n]
            bucket["shortfall_ml"][n] += shortfall[n]
        if not sufficient:
            bucket["_sufficient"] = False

    # End-of-day stock = stock after the last batch of that day. Since the
    # running state is already final per batch, snapshot by replaying the
    # batches grouped by day.
    day_last_batch: dict[str, dict[str, Any]] = {}
    for row in batches_out:
        day_last_batch[row["date"]] = row

    days_out: list[dict[str, Any]] = []
    for day in day_order:
        bucket = day_buckets[day]
        end_stock = day_last_batch[day]["end_stock_ml"]
        days_out.append(
            {
                "date": day,
                "batch_count": bucket["batch_count"],
                "water_ml": _r6(bucket["water_ml"]),
                "required_ml": {n: _r6(bucket["required_ml"][n]) for n in names},
                "served_ml": {n: _r6(bucket["served_ml"][n]) for n in names},
                "shortfall_ml": {n: _r6(bucket["shortfall_ml"][n]) for n in names},
                "end_stock_ml": dict(end_stock),
                "stock_sufficient": bucket["_sufficient"],
            }
        )

    return {
        "recipe_id": recipe_id,
        "version": version,
        "recipe_feasible": bool(result.get("feasible", False)),
        "recipe_message": str(result.get("message", "")),
        "timezone": plan["timezone"],
        "start_date": plan["start_date"],
        "end_date": plan["end_date"],
        "mix_total_ml": _r6(mix_total),
        "dosage": dosage,
        "initial_stock_ml": {n: _r6(initial[n]) for n in names},
        "total_water_ml": _r6(total_water),
        "total_required_ml": {n: _r6(total_required[n]) for n in names},
        "total_served_ml": {n: _r6(total_served[n]) for n in names},
        "total_shortfall_ml": {n: _r6(total_shortfall[n]) for n in names},
        "final_stock_ml": {n: _r6(stock[n]) for n in names},
        "stock_sufficient": first_shortage_date is None,
        "first_shortage_date": first_shortage_date,
        "day_results": days_out,
        "batches": batches_out,
    }
