"""Seite „Familie“ für Verwalter (Plan 8f, M6-04, M6-05).

- Alle Seiten und Aktionen brauchen MANAGE_FAMILY (die Verbrauchsübersicht
  zusätzlich VIEW_USAGE_ALL). Aktionen nur per POST mit CSRF-Token, danach
  Weiterleitung (PRG) – außer bei neuen Passwörtern, siehe unten.
- Schutzregeln: Niemand sperrt sich selbst oder ändert die eigene Rolle; das
  letzte aktive Verwalterkonto bleibt erhalten (nicht sperren, Rolle nicht
  entziehen). Superuser-Konten (Notfallzugang) ändert nur ein Superuser.
- Start- und neue Passwörter erzeugt der Server zufällig. Sie stehen nur in der
  Antwort auf den POST (``Cache-Control: no-store``), nie in Session, Datenbank
  (nur als Hash), Meldungen oder Log.
- Einsicht (M6-05): Option ``allow_supervision`` nur für die Rolle „teen“. Die
  Liste der Chats erscheint nur, solange die Einsicht wirkt; geöffnet wird die
  normale Chatansicht, die ohne Schreibrecht nur lesend ist (permissions).
"""

import secrets
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from multigpt.chat.models import Conversation

from . import usage
from .forms_family import (
    AccountCreateForm,
    BudgetForm,
    GroupMembersForm,
    GroupNameForm,
    RoleForm,
    SupervisionForm,
)
from .models import Role, User, UserGroup
from .permissions import Action, require_can, supervision_active

# Zufallspasswort: 4 Blöcke à 5 Zeichen, ohne leicht verwechselbare Zeichen.
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_BLOCKS = 4
PASSWORD_BLOCK_LENGTH = 5
USAGE_MONTHS = 12

manage_family = require_can(Action.MANAGE_FAMILY)


def generate_password(user=None) -> str:
    """Zufallspasswort (ca. 113 Bit), das die Passwortvalidatoren besteht."""
    for _ in range(20):
        password = "-".join(
            "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_BLOCK_LENGTH))
            for _ in range(PASSWORD_BLOCKS)
        )
        try:
            validate_password(password, user)
        except ValidationError:
            continue
        return password
    raise RuntimeError("Kein gültiges Zufallspasswort erzeugt.")


def _no_store(response):
    response["Cache-Control"] = "private, no-store"
    return response


def _member_url(member) -> str:
    return reverse("family:member", args=[member.pk])


def _managers():
    """Aktive Konten, die die Familie verwalten können."""
    return User.objects.filter(is_active=True).filter(Q(role__is_admin=True) | Q(is_superuser=True))


def _is_manager(user) -> bool:
    return bool(user.is_active and (user.is_superuser or (user.role_id and user.role.is_admin)))


def _last_manager(member) -> bool:
    """True, wenn ``member`` das einzige aktive Verwalterkonto ist. Sperrt die
    Verwalterzeilen bis zum Ende der Transaktion (parallele Änderungen)."""
    if not _is_manager(member):
        return False
    ids = list(_managers().select_for_update(of=("self",)).values_list("pk", flat=True))
    return ids == [member.pk]


def _target(request, pk) -> User:
    """Zielkonto einer Aktion; Superuser-Konten nur für Superuser."""
    member = get_object_or_404(User.objects.select_related("role"), pk=pk)
    if member.is_superuser and not request.user.is_superuser:
        raise PermissionDenied
    return member


def _budget_row(member, spent):
    """Budget und Verbrauch für die Anzeige (Stufen wie usage.budget_state)."""
    budget = usage.budget_for(member)
    percent = None
    if budget is not None:
        percent = 100 if budget <= 0 else min(100, int(spent * 100 / budget))
    return {
        "spent": spent,
        "spent_text": usage.format_eur(spent),
        "budget": budget,
        "budget_text": usage.format_eur(budget),
        "percent": percent,
        "level": usage.level_for(spent, budget),
    }


def _with_cost_text(rows: list[dict]) -> list[dict]:
    for row in rows:
        row["cost_text"] = usage.format_eur(row["cost"])
    return rows


# --- Übersicht ------------------------------------------------------------------


@require_GET
@manage_family
def overview(request):
    members = list(
        User.objects.select_related("role")
        .annotate(last_chat=Max("conversations__updated"))
        .order_by("-is_active", "display_name", "username")
    )
    start, end = usage.month_bounds(timezone.now())
    spent = usage.spent_by_user(members, start, end)
    for member in members:
        member.usage = _budget_row(member, spent.get(member.pk, Decimal("0")))
        stamps = [s for s in (member.last_login, member.last_chat) if s]
        member.last_activity = max(stamps) if stamps else None
        member.supervised = supervision_active(member)
    context = {
        "members": members,
        "create_form": AccountCreateForm(),
        "month_start": start,
        "groups": UserGroup.objects.annotate(member_count=Count("user")).order_by(
            "-is_default", "name"
        ),
    }
    return render(request, "accounts/family/overview.html", context)


@require_GET
@manage_family
def member_detail(request, pk):
    member = get_object_or_404(User.objects.select_related("role"), pk=pk)
    start, end = usage.month_bounds(timezone.now())
    spent = usage.spent_by_user([member], start, end).get(member.pk)
    supervised = supervision_active(member)
    context = {
        "member": member,
        "is_self": member.pk == request.user.pk,
        "editable": not (member.is_superuser and not request.user.is_superuser),
        "usage": _budget_row(member, spent or Decimal("0")),
        "model_usage": _with_cost_text(usage.usage_by_model(member, start, end)),
        "month_start": start,
        "role_form": RoleForm(initial={"role": member.role_id}),
        "budget_form": BudgetForm(
            initial={"monthly_budget_override": member.monthly_budget_override}
        ),
        "is_teen": bool(member.role_id and member.role.key == Role.TEEN),
        "supervised": supervised,
        "conversations": (
            Conversation.objects.filter(user=member).order_by("-updated", "-pk")
            if supervised
            else None
        ),
        "groups": member.groups.order_by("name"),
    }
    return render(request, "accounts/family/member.html", context)


# --- Aktionen auf Konten --------------------------------------------------------


def _password_page(request, member, created: bool):
    response = render(
        request,
        "accounts/family/password.html",
        {"member": member, "password": member._new_password, "created": created},
    )
    return _no_store(response)


@require_POST
@manage_family
def member_create(request):
    form = AccountCreateForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            "Konto nicht angelegt: "
            + " ".join(msg for errors in form.errors.values() for msg in errors),
        )
        return redirect("family:overview")
    member = form.save(commit=False)
    member.is_staff = member.role.is_admin
    password = generate_password(member)
    member.set_password(password)
    with transaction.atomic():
        member.save()
    member._new_password = password
    return _password_page(request, member, created=True)


@require_POST
@manage_family
def member_password(request, pk):
    member = _target(request, pk)
    if member.pk == request.user.pk:
        messages.error(request, "Dein eigenes Passwort änderst du über „Passwort ändern“.")
        return redirect(_member_url(member))
    password = generate_password(member)
    member.set_password(password)
    member.save(update_fields=["password"])
    member._new_password = password
    return _password_page(request, member, created=False)


@require_POST
@manage_family
def member_active(request, pk):
    member = _target(request, pk)
    activate = request.POST.get("active") == "1"
    with transaction.atomic():
        if not activate:
            if member.pk == request.user.pk:
                messages.error(request, "Du kannst dein eigenes Konto nicht sperren.")
                return redirect(_member_url(member))
            if _last_manager(member):
                messages.error(
                    request, "Das letzte aktive Verwalterkonto kann nicht gesperrt werden."
                )
                return redirect(_member_url(member))
        member.is_active = activate
        member.save(update_fields=["is_active"])
    if activate:
        messages.success(request, f"Das Konto „{member}“ ist wieder aktiv.")
    else:
        messages.success(request, f"Das Konto „{member}“ ist gesperrt.")
    return redirect(_member_url(member))


@require_POST
@manage_family
def member_role(request, pk):
    member = _target(request, pk)
    form = RoleForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Bitte eine gültige Rolle wählen.")
        return redirect(_member_url(member))
    if member.pk == request.user.pk:
        messages.error(request, "Deine eigene Rolle kann nur ein anderer Verwalter ändern.")
        return redirect(_member_url(member))
    role = form.cleaned_data["role"]
    with transaction.atomic():
        if not role.is_admin and not member.is_superuser and _last_manager(member):
            messages.error(
                request, "Das letzte aktive Verwalterkonto muss die Verwalterrolle behalten."
            )
            return redirect(_member_url(member))
        member.role = role
        member.is_staff = role.is_admin
        fields = ["role", "is_staff"]
        if role.key != Role.TEEN and member.allow_supervision:
            # Einsicht gibt es nur für Jugendliche; bei Rollenwechsel endet sie.
            member.allow_supervision = False
            fields.append("allow_supervision")
        member.save(update_fields=fields)
    messages.success(request, f"„{member}“ hat jetzt die Rolle „{role.name}“.")
    return redirect(_member_url(member))


@require_POST
@manage_family
def member_budget(request, pk):
    member = _target(request, pk)
    form = BudgetForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            "Budget nicht gespeichert: "
            + " ".join(msg for errors in form.errors.values() for msg in errors),
        )
        return redirect(_member_url(member))
    member.monthly_budget_override = form.cleaned_data["monthly_budget_override"]
    member.save(update_fields=["monthly_budget_override"])
    if member.monthly_budget_override is None:
        messages.success(request, f"Für „{member}“ gilt wieder das Budget der Rolle.")
    else:
        messages.success(
            request,
            f"Monatsbudget für „{member}“: {usage.format_eur(member.monthly_budget_override)}.",
        )
    return redirect(_member_url(member))


@require_POST
@manage_family
def member_supervision(request, pk):
    member = _target(request, pk)
    form = SupervisionForm(request.POST)
    form.is_valid()
    enable = bool(form.cleaned_data.get("allow_supervision"))
    if enable and not (member.role_id and member.role.key == Role.TEEN):
        messages.error(request, "Die Einsicht gibt es nur für Konten der Rolle Jugendlicher.")
        return redirect(_member_url(member))
    member.allow_supervision = enable
    member.save(update_fields=["allow_supervision"])
    if enable:
        messages.success(
            request,
            f"Einsicht für „{member}“ ist an. Das Mitglied sieht dauerhaft einen Hinweis.",
        )
    else:
        messages.success(request, f"Einsicht für „{member}“ ist aus.")
    return redirect(_member_url(member))


# --- Gruppen --------------------------------------------------------------------


@require_GET
@manage_family
def group_list(request):
    groups = UserGroup.objects.annotate(member_count=Count("user")).order_by("-is_default", "name")
    return render(
        request,
        "accounts/family/groups.html",
        {"groups": groups, "create_form": GroupNameForm()},
    )


@require_POST
@manage_family
def group_create(request):
    form = GroupNameForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(form.errors.get("name", ["Ungültiger Name."])))
        return redirect("family:groups")
    group = UserGroup.objects.create(name=form.cleaned_data["name"])
    messages.success(request, f"Gruppe „{group.name}“ angelegt.")
    return redirect("family:group", pk=group.pk)


@require_GET
@manage_family
def group_detail(request, pk):
    group = get_object_or_404(UserGroup, pk=pk)
    return render(
        request,
        "accounts/family/group.html",
        {
            "group": group,
            "name_form": GroupNameForm(instance=group, initial={"name": group.name}),
            "members_form": GroupMembersForm(group=group),
            "members": group.user_set.order_by("display_name", "username"),
        },
    )


@require_POST
@manage_family
def group_rename(request, pk):
    group = get_object_or_404(UserGroup, pk=pk)
    form = GroupNameForm(request.POST, instance=group)
    if not form.is_valid():
        messages.error(request, " ".join(form.errors.get("name", ["Ungültiger Name."])))
    else:
        group.name = form.cleaned_data["name"]
        group.save(update_fields=["name"])
        messages.success(request, f"Gruppe heißt jetzt „{group.name}“.")
    return redirect("family:group", pk=group.pk)


@require_POST
@manage_family
def group_members(request, pk):
    group = get_object_or_404(UserGroup, pk=pk)
    if group.is_default:
        # Standardgruppe („Familie“): enthält automatisch alle Konten.
        messages.error(request, "Die Standardgruppe enthält immer alle Konten.")
        return redirect("family:group", pk=group.pk)
    form = GroupMembersForm(request.POST, group=group)
    if not form.is_valid():
        messages.error(request, "Ungültige Auswahl.")
        return redirect("family:group", pk=group.pk)
    group.user_set.set(form.cleaned_data["members"])
    messages.success(request, "Mitglieder gespeichert.")
    return redirect("family:group", pk=group.pk)


@require_POST
@manage_family
def group_delete(request, pk):
    group = get_object_or_404(UserGroup, pk=pk)
    if group.is_default:
        messages.error(request, "Die Standardgruppe kann nicht gelöscht werden.")
        return redirect("family:group", pk=group.pk)
    name = group.name
    # Löscht auch die auth.Group (Mitgliedschaften, Freigaben an die Gruppe).
    group.group_ptr.delete()
    messages.success(request, f"Gruppe „{name}“ gelöscht.")
    return redirect("family:groups")


# --- Verbrauch aller (VIEW_USAGE_ALL) -------------------------------------------


def _month_options(now):
    """Die letzten 12 Monate (neueste zuerst) als Monatsanfänge (date)."""
    year, month = now.year, now.month
    options = []
    for _ in range(USAGE_MONTHS):
        options.append(date(year, month, 1))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return options


@require_GET
@manage_family
@require_can(Action.VIEW_USAGE_ALL)
def usage_all(request):
    now = timezone.localtime()
    options = _month_options(now)
    selected = options[0]
    raw = request.GET.get("monat", "")
    for option in options:
        if raw == f"{option:%Y-%m}":
            selected = option
    start, end = usage.month_bounds(usage.month_start(selected.year, selected.month))
    members = list(User.objects.select_related("role").order_by("display_name", "username"))
    spent = usage.spent_by_user(None, start, end)
    by_member: dict[int, list[dict]] = {}
    for row in _with_cost_text(usage.usage_by_model(None, start, end, by_user=True)):
        by_member.setdefault(row["user_id"], []).append(row)
    rows = []
    total = Decimal("0")
    for member in members:
        member_spent = spent.get(member.pk, Decimal("0"))
        total += member_spent
        rows.append(
            {
                "member": member,
                "usage": _budget_row(member, member_spent),
                "models": by_member.get(member.pk, []),
            }
        )
    context = {
        "rows": rows,
        "options": options,
        "selected": selected,
        "is_current": selected == options[0],
        "total_text": usage.format_eur(total),
    }
    return render(request, "accounts/family/usage.html", context)
