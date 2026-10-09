"""Django-Rechte für Konten mit Verwalterrolle.

Konten der Rolle mit ``is_admin`` bekommen im Django-Admin alle Modellrechte,
ohne Superuser sein oder einzeln berechtigt werden zu müssen. Grundlage ist
dieselbe Prüfung wie überall: ``can(user, Action.ADMIN)``.
"""

from .permissions import Action, can


class RoleAdminBackend:
    """Nur Rechte, keine Anmeldung (authenticate liefert immer None)."""

    def authenticate(self, request, **credentials):
        return None

    def has_perm(self, user_obj, perm, obj=None):
        return can(user_obj, Action.ADMIN)

    def has_module_perms(self, user_obj, app_label):
        return can(user_obj, Action.ADMIN)

    def get_user(self, user_id):
        return None
