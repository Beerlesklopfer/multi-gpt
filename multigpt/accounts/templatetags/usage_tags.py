"""Verbrauch in Templates (M6): Euro-Format, Budgetbalken, Hinweis in der Kopfzeile.

``{% budget_banner %}`` fragt den Verbrauch nur ab, wenn das Konto ein Budget
hat (zwei Aggregat-Abfragen je Seite); ohne Budget keine Abfrage.
"""

from django import template

from .. import usage

register = template.Library()


@register.filter
def eur(value) -> str:
    """Decimal -> „1,23 €“ (bei < 0,01 € vier Nachkommastellen)."""
    return usage.format_eur(value)


@register.filter
def tokens(value) -> str:
    """Ganzzahl mit Tausenderpunkten: 12345 -> „12.345“."""
    try:
        return f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


@register.inclusion_tag("accounts/_budget_banner.html", takes_context=True)
def budget_banner(context):
    """Hinweis ab 80 % des Monatsbudgets (gelb) bzw. bei ausgeschöpftem Budget."""
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or usage.budget_for(user) is None:
        return {"budget_level": usage.LEVEL_OK}
    state = usage.budget_state(user)
    return {
        "budget_level": state.level,
        "budget_text": usage.warning_text(state),
        "budget_short": (
            "Budget ausgeschöpft"
            if state.level == usage.LEVEL_EXHAUSTED
            else f"Budget zu {state.percent} % verbraucht"
        ),
    }


@register.inclusion_tag("accounts/_budget_meter.html")
def budget_meter(state, label_id=""):
    """Budgetbalken als <meter> mit Text daneben (auch ohne Farbe verständlich)."""
    ctx = {"state": state, "label_id": label_id}
    if state is not None and state.budget is not None and state.budget > 0:
        # Als Strings mit Punkt: Templates würden Decimal sonst lokalisieren („0,8“).
        ctx.update(
            meter_max=str(state.budget),
            meter_value=str(min(state.spent, state.budget)),
            meter_low=str(state.budget * usage.WARNING_RATIO),
        )
    return ctx


@register.simple_tag
def bar_value(value, maximum) -> str:
    """Wert für einen <meter> der Monatstabelle (0..1, Punkt als Dezimalzeichen)."""
    try:
        if not maximum:
            return "0"
        return f"{min(float(value) / float(maximum), 1.0):.4f}"
    except (TypeError, ValueError, ZeroDivisionError):
        return "0"
