"""Seite „Mein Verbrauch“ und ``GET /api/usage/`` (M6-02, M6-03).

Beide zeigen nur den Verbrauch des angemeldeten Kontos – es gibt keinen
Parameter für ein anderes Konto. Den Verbrauch aller sieht nur die Seite
„Familie“ (VIEW_USAGE_ALL).
"""

from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from . import usage


def _money(value) -> str | None:
    """Decimal als String für JSON (exakt, ohne Float-Rundung)."""
    return None if value is None else str(value)


@require_GET
@login_required
def usage_page(request):
    user = request.user
    state = usage.budget_state(user)
    start, end = usage.month_bounds()
    # Je Konto und Modell: wählbarer Monat (?monat=JJJJ-MM, letzte 12 Monate).
    options, selected = usage.month_choice(request.GET.get("monat", ""))
    sel_start, sel_end = usage.month_bounds(selected)
    models = usage.usage_by_model(user, sel_start, sel_end)
    months = usage.monthly_totals(user, 12)
    max_month = max((m["cost"] for m in months), default=0)
    total_tokens_in = sum(r["tokens_in"] for r in models)
    total_tokens_out = sum(r["tokens_out"] for r in models)
    return render(
        request,
        "accounts/usage.html",
        {
            "state": state,
            "warning_text": usage.warning_text(state),
            "month_label": usage.month_label(start),
            "selected_label": usage.month_label(sel_start),
            "month_options": [(f"{o:%Y-%m}", usage.month_label(o)) for o in options],
            "selected": f"{selected:%Y-%m}",
            "account_states": usage.account_states(user),
            "account_rows": usage.usage_by_account(user, sel_start, sel_end),
            "model_rows": models,
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "total_cost": sum((r["cost"] for r in models), Decimal(0)),
            "months": months,
            "max_month": max_month,
        },
    )


@require_GET
def usage_api(request):
    """Eigener Verbrauch im laufenden Monat als JSON. Beträge als Strings (Euro)."""
    user = request.user
    if not user.is_authenticated or not user.is_active:
        return JsonResponse({"error": "Nicht angemeldet."}, status=403)
    return JsonResponse(usage_payload(user))


def usage_payload(user) -> dict:
    """Eigener Verbrauch im laufenden Monat (auch für das MCP-Werkzeug ``usage``)."""
    state = usage.budget_state(user)
    start, end = usage.month_bounds()
    return {
        "month": start.strftime("%Y-%m"),
        "month_label": usage.month_label(start),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "spent": _money(state.spent),
        "budget": _money(state.budget),
        "remaining": _money(state.remaining),
        "percent": state.percent,
        "level": state.level,
        "message": usage.warning_text(state),
        # Budgets je Abrechnungskonto (EUR bzw. Tokens).
        "accounts": [
            {
                "account": a.name,
                "unit": a.unit,
                "spent": _money(a.spent),
                "limit": _money(a.limit),
                "percent": a.percent,
                "level": a.level,
                "message": a.warning_text(),
            }
            for a in usage.account_states(user)
        ],
        "models": [
            {
                "model_id": row["model_id"],
                "model": row["model"],
                "provider": row["provider"],
                "account": row["account"],
                "is_local": row["is_local"],
                "answers": row["answers"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "files": row["files"],
                "cost": _money(row["cost"]),
            }
            for row in usage.usage_by_model(user, start, end)
        ],
    }
