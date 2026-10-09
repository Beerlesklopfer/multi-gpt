"""Zentrale Rechteprüfung (Plan 8f, M2-05).

Jede View und jeder Anbieteraufruf prüft über ``can(user, action, obj=None)``.
Ausblenden in der Oberfläche reicht nicht: ``require_can`` und
``CanRequiredMixin`` antworten bei Verbot mit 403.

Regeln:

- Anonyme und gesperrte (``is_active=False``) Konten dürfen nichts.
- Superuser dürfen alle *Funktionen* (Notfallzugang). Objektbezogene Grenzen
  bleiben bestehen: Ein Modell muss aktiv sein, und fremde Chats/Sammlungen
  sind auch für Superuser und Verwalter nicht lesbar (Privatsphäre, Plan 8f).
- Alles andere kommt aus der Rolle des Kontos. Ohne Rolle: nichts.
- READ/WRITE auf Chats und Sammlungen: Besitzer, oder eine Freigabe (``Share``)
  an eine Gruppe, in der das Konto Mitglied ist; WRITE braucht ``can_write``.

Erweiterungspunkt Budget (M6): ``budget_allows()`` wird bei USE_MODEL mit
Modell aufgerufen und erlaubt vorerst immer.
"""

import enum
from functools import wraps

from django.contrib.auth.mixins import AccessMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from .models import Role


class Action(enum.StrEnum):
    """Prüfbare Aktionen. Mit Objekt: USE_MODEL (AIModel), USE_MCP_SERVER
    (McpServer), READ/WRITE (Conversation oder Collection)."""

    CHAT = "chat"
    USE_MODEL = "use_model"
    WEB_SEARCH = "web_search"
    IMAGES = "images"
    VOICE = "voice"
    UPLOAD_DOCUMENTS = "upload_documents"
    SHARE = "share"
    USE_MCP_SERVER = "use_mcp_server"
    READ = "read"
    WRITE = "write"
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
}

_ADMIN_ACTIONS = {Action.MANAGE_FAMILY, Action.VIEW_USAGE_ALL, Action.ADMIN}

# Besitzerfeld je Objektart (app_label.model_name), für READ/WRITE.
_OWNER_FIELDS = {
    "chat.conversation": "user",
    "chat.collection": "owner",
}


def budget_allows(user, ai_model) -> bool:
    """Erweiterungspunkt für M6: Monatsbudget erschöpft -> nur noch lokale Modelle.

    Wird bei ``can(user, Action.USE_MODEL, ai_model)`` aufgerufen.
    """
    return True


def _role(user) -> Role | None:
    return user.role if user.role_id else None


def _is_active_model(obj) -> bool:
    if not getattr(obj, "active", False):
        return False
    provider = getattr(obj, "provider", None)
    return provider is None or getattr(provider, "active", True)


def _can_use_model(user, role, ai_model) -> bool:
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
    return budget_allows(user, ai_model)


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


def _can_access(user, obj, write: bool) -> bool:
    if obj is None:
        return False
    owner_field = _OWNER_FIELDS.get(obj._meta.label_lower)
    if owner_field is None:
        return False
    if getattr(obj, f"{owner_field}_id") == user.pk:
        return True
    shares = obj.shares.filter(group__in=user.groups.values("pk"))
    if write:
        shares = shares.filter(can_write=True)
    return shares.exists()


def can(user, action, obj=None) -> bool:
    """True, wenn ``user`` die Aktion ``action`` (ggf. auf ``obj``) ausführen darf."""
    action = Action(action)
    if user is None or not user.is_authenticated or not user.is_active:
        return False

    if action in (Action.READ, Action.WRITE):
        return _can_access(user, obj, write=action is Action.WRITE)

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
