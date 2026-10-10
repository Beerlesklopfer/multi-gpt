"""Zentrale Rechteprüfung (Plan 8f, M2-05).

Jede View und jeder Anbieteraufruf prüft über ``can(user, action, obj=None)``.
Ausblenden in der Oberfläche reicht nicht: ``require_can`` und
``CanRequiredMixin`` antworten bei Verbot mit 403.

Regeln:

- Anonyme und gesperrte (``is_active=False``) Konten dürfen nichts.
- Superuser dürfen alle *Funktionen* (Notfallzugang). Objektbezogene Grenzen
  bleiben bestehen: Ein Modell muss aktiv sein, und fremde Chats/Sammlungen
  sind auch für Superuser und Verwalter nicht lesbar (Privatsphäre, Plan 8f;
  einzige Ausnahme ist die Einsicht, siehe unten).
- Alles andere kommt aus der Rolle des Kontos. Ohne Rolle: nichts.
- READ/WRITE auf Chats und Sammlungen: Besitzer, oder eine Freigabe (``Share``)
  an eine Gruppe, in der das Konto Mitglied ist, bzw. bei Chats auch direkt an
  das Konto; WRITE braucht ``can_write``. UPDATE/DELETE (nur Chats, RWUD):
  ``can_update``/``can_delete``. Über eine Freigabe gibt es W/U/D nur für
  Konten, die chatten dürfen (Recht CHAT). Wer einen Chat „aus seiner Liste
  entfernt“ hat (``Share.left_by``), für den gilt die Freigabe nicht mehr.
- Einsicht (M6-05): READ (nie WRITE) auf Chats eines Jugendlichen-Kontos mit
  ``allow_supervision`` für Konten mit MANAGE_FAMILY, siehe ``supervision_active``.

Budget (M6-03, M6-10): ``budget_allows()`` wird bei USE_MODEL mit Modell
aufgerufen. Ist das Budget eines Abrechnungskontos ausgeschöpft, sind dessen
Modelle gesperrt; beim Gesamtbudget alle kostenpflichtigen (Modelle ohne Preis,
Token- und Pauschalkonten bleiben frei, sofern deren Kontingent reicht).
``model_permitted()`` prüft dasselbe ohne Budget (Modellauswahl: gesperrte
Modelle ausgrauen statt ausblenden; Fehlertext unterscheiden).
"""

import enum
from functools import wraps

from django.contrib.auth.mixins import AccessMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from .models import Role


class Action(enum.StrEnum):
    """Prüfbare Aktionen. Mit Objekt: USE_MODEL (AIModel), USE_MCP_SERVER
    (McpServer), READ/WRITE (Conversation oder Collection), UPDATE/DELETE
    (Conversation; bei Sammlungen nur der Besitzer)."""

    CHAT = "chat"
    USE_MODEL = "use_model"
    WEB_SEARCH = "web_search"
    IMAGES = "images"
    VOICE = "voice"
    UPLOAD_DOCUMENTS = "upload_documents"
    SHARE = "share"
    COMPUTE = "compute"
    CREATE_DOCUMENTS = "create_documents"
    USE_API = "use_api"
    USE_MCP_SERVER = "use_mcp_server"
    READ = "read"
    WRITE = "write"
    UPDATE = "update"
    DELETE = "delete"
    MANAGE_FAMILY = "manage_family"
    VIEW_USAGE_ALL = "view_usage_all"
    ADMIN = "admin"


# Aktion -> Flag der Rolle
_ROLE_FLAGS = {
    Action.WEB_SEARCH: "can_web_search",
    Action.IMAGES: "can_images",
    Action.VOICE: "can_voice",
    Action.UPLOAD_DOCUMENTS: "can_upload_documents",
    Action.SHARE: "can_share",
    Action.COMPUTE: "can_compute",
    Action.CREATE_DOCUMENTS: "can_create_documents",
    Action.USE_API: "can_use_api",
}

_ADMIN_ACTIONS = {Action.MANAGE_FAMILY, Action.VIEW_USAGE_ALL, Action.ADMIN}

# Besitzerfeld je Objektart (app_label.model_name), für READ/WRITE.
_OWNER_FIELDS = {
    "chat.conversation": "user",
    "chat.collection": "owner",
}


def budget_allows(user, ai_model) -> bool:
    """Budget des Abrechnungskontos bzw. Gesamtbudget ausgeschöpft -> Modell gesperrt.

    Wird bei ``can(user, Action.USE_MODEL, ai_model)`` aufgerufen. Gesperrt
    sind nur die Modelle des betroffenen Kontos (Gesamtbudget: alle
    kostenpflichtigen), siehe ``billing.budgets.blocked_reason``.
    """
    from . import usage

    if ai_model is None:
        return True
    return not usage.blocked_reason(user, ai_model)


def model_permitted(user, ai_model) -> bool:
    """Wie ``can(user, USE_MODEL, ai_model)``, aber ohne Budgetprüfung."""
    if user is None or not user.is_authenticated or not user.is_active:
        return False
    role = _role(user)
    if role is None and not user.is_superuser:
        return False
    return _can_use_model(user, role, ai_model, check_budget=False)


def _role(user) -> Role | None:
    return user.role if user.role_id else None


def _is_active_model(obj) -> bool:
    if not getattr(obj, "active", False):
        return False
    provider = getattr(obj, "provider", None)
    return provider is None or getattr(provider, "active", True)


def _can_use_model(user, role, ai_model, check_budget: bool = True) -> bool:
    if ai_model is None:
        # Allgemein: Darf das Konto überhaupt ein Modell nutzen?
        if user.is_superuser or role.all_models:
            return True
        return role.allowed_models.filter(active=True, provider__active=True).exists()
    if not _is_active_model(ai_model):
        return False
    if not (
        user.is_superuser or role.all_models or role.allowed_models.filter(pk=ai_model.pk).exists()
    ):
        return False
    return not check_budget or budget_allows(user, ai_model)


def _can_use_mcp_server(user, role, server) -> bool:
    if server is None:
        if user.is_superuser or role.all_mcp_servers:
            return True
        return role.allowed_mcp_servers.filter(active=True).exists()
    if not getattr(server, "active", False):
        return False
    return (
        user.is_superuser
        or role.all_mcp_servers
        or role.allowed_mcp_servers.filter(pk=server.pk).exists()
    )


def supervision_active(member) -> bool:
    """Einsicht (Plan 8f, M6-05): nur für Konten der Rolle "teen" mit gesetzter
    Option ``allow_supervision``. Wechselt die Rolle, endet die Einsicht sofort."""
    return bool(
        member is not None
        and member.allow_supervision
        and member.role_id
        and member.role.key == Role.TEEN
    )


def supervised_conversation_owner(user, conversation):
    """Besitzer des Chats, wenn ``user`` ihn nur über die Einsicht lesen darf, sonst None."""
    if conversation.user_id == user.pk or not _can_supervise(user, conversation):
        return None
    return conversation.user


def _can_supervise(user, obj) -> bool:
    """Verwalter dürfen Chats eines Kontos mit aktiver Einsicht *lesen* (nie schreiben)."""
    if obj._meta.label_lower != "chat.conversation":
        return False
    if not can(user, Action.MANAGE_FAMILY):
        return False
    return supervision_active(obj.user)


# Recht -> Feld der Freigabe (RWUD; READ braucht nur irgendeine Freigabe).
_SHARE_FLAGS = {
    Action.WRITE: "can_write",
    Action.UPDATE: "can_update",
    Action.DELETE: "can_delete",
}


def applicable_shares(user, obj):
    """Freigaben von ``obj``, die für ``user`` gelten: an eine seiner Gruppen
    oder (Chats) direkt an ihn, ohne die, aus denen er sich ausgetragen hat."""
    from django.db.models import Q

    return obj.shares.filter(Q(group__in=user.groups.values("pk")) | Q(user=user)).exclude(
        left_by=user
    )


def _can_access(user, obj, action) -> bool:
    if obj is None:
        return False
    owner_field = _OWNER_FIELDS.get(obj._meta.label_lower)
    if owner_field is None:
        return False
    if getattr(obj, f"{owner_field}_id") == user.pk:
        return True
    if action is Action.READ and _can_supervise(user, obj):
        return True
    is_conversation = obj._meta.label_lower == "chat.conversation"
    if action in (Action.UPDATE, Action.DELETE) and not is_conversation:
        return False  # Sammlungen: umbenennen/löschen nur der Besitzer
    shares = applicable_shares(user, obj)
    if action is not Action.READ:
        # Ändern über eine Freigabe nur für Konten, die chatten dürfen.
        if is_conversation and not can(user, Action.CHAT):
            return False
        shares = shares.filter(**{_SHARE_FLAGS[action]: True})
    return shares.exists()


def can(user, action, obj=None) -> bool:
    """True, wenn ``user`` die Aktion ``action`` (ggf. auf ``obj``) ausführen darf."""
    action = Action(action)
    if user is None or not user.is_authenticated or not user.is_active:
        return False

    if action in (Action.READ, Action.WRITE, Action.UPDATE, Action.DELETE):
        return _can_access(user, obj, action)

    role = _role(user)
    if role is None and not user.is_superuser:
        return False

    if action is Action.USE_MODEL:
        return _can_use_model(user, role, obj)
    if action is Action.USE_MCP_SERVER:
        return _can_use_mcp_server(user, role, obj)
    if user.is_superuser:
        return True
    if action is Action.CHAT:
        return True
    if action in _ADMIN_ACTIONS:
        return role.is_admin
    return bool(getattr(role, _ROLE_FLAGS[action]))


def _deny(request):
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    raise PermissionDenied


def require_can(action, get_object=None):
    """View-Decorator: prüft ``can(request.user, action, obj)``.

    ``get_object(request, *args, **kwargs)`` liefert optional das Objekt.
    Nicht angemeldet -> Weiterleitung zum Login, sonst bei Verbot 403.
    """
    action = Action(action)

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            obj = get_object(request, *args, **kwargs) if get_object else None
            if not can(request.user, action, obj):
                return _deny(request)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


class CanRequiredMixin(AccessMixin):
    """View-Mixin: ``required_action`` setzen, ggf. ``get_permission_object()``
    überschreiben. Nicht angemeldet -> Login, bei Verbot 403."""

    required_action: Action | None = None

    def get_permission_object(self):
        return None

    def dispatch(self, request, *args, **kwargs):
        if self.required_action is None:
            raise NotImplementedError("CanRequiredMixin braucht required_action.")
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not can(request.user, self.required_action, self.get_permission_object()):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)
