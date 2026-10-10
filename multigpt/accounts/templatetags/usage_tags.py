"""Verbrauch in Templates (M6): Euro-Format, Budgetbalken, Hinweis in der Kopfzeile.

``{% budget_banner %}`` fragt die Budgets ab (eine Abfrage) und den Verbrauch
nur, wenn das Konto ein Budget hat (eine Aggregat-Abfrage je Seite).
"""

from django import template

from multigpt.billing import budgets

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
    """Hinweis ab 80 % eines Budgets (gelb) bzw. bei ausgeschöpftem Budget.

    Maßgeblich ist das am stärksten ausgeschöpfte Budget (gesamt oder je
    Abrechnungskonto, billing.budgets); bei einem Konto steht dessen Name dabei.
    """
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"budget_level": usage.LEVEL_OK}
    state = budgets.snapshot(user).worst()
    if state is None or state.level == usage.LEVEL_OK:
        return {"budget_level": usage.LEVEL_OK}
    suffix = f" ({state.name})" if state.account is not None else ""
    return {
        "budget_level": state.level,
        "budget_text": state.warning_text(),
        "budget_short": (
            "Budget ausgeschöpft" + suffix
            if state.level == usage.LEVEL_EXHAUSTED
            else f"Budget zu {state.percent} % verbraucht" + suffix
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


@register.filter
def amount(value, currency) -> str:
    """Betrag in Kontowährung („1,23 $“ bzw. „1,23 €“)."""
    return usage.format_amount(value, currency)


@register.inclusion_tag("accounts/_account_meter.html")
def account_meter(state):
    """Balken für ein Budget je Abrechnungskonto (EUR oder Tokens)."""
    ctx = {"state": state}
    if state.limit is not None and state.limit > 0:
        ctx.update(
            meter_max=str(state.limit),
            meter_value=str(min(state.spent, state.limit)),
            meter_low=str(state.limit * usage.WARNING_RATIO),
        )
    return ctx
