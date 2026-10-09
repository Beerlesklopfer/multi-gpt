"""AdminSite, die den Zugang über ``can(user, Action.ADMIN)`` regelt.

Djangos Vorgabe (``is_active and is_staff``) reicht nicht: Ein fälschlich
gesetztes ``is_staff`` würde Gast oder Jugendlichen in die Verwaltung lassen.
Hier entscheidet allein die Rolle (bzw. Superuser).

- Angemeldet ohne Recht -> 403 auf allen Admin-Seiten (statt Login-Formular).
- Nicht angemeldet -> Weiterleitung zum normalen Login unter /konto/ (dort
  greift auch die Login-Drosselung); das Admin-Login-Formular mit seiner
  ``is_staff``-Prüfung wird nicht verwendet.
"""

from functools import update_wrapper

from django.contrib import admin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.urls import reverse

from .permissions import Action, can


class FamilyAdminSite(admin.AdminSite):
    site_header = "MultiGPT-Verwaltung"
    site_title = "MultiGPT-Verwaltung"
    index_title = "Übersicht"

    def has_permission(self, request):
        return can(request.user, Action.ADMIN)

    def admin_view(self, view, cacheable=False):
        inner = super().admin_view(view, cacheable)

        def wrapper(request, *args, **kwargs):
            # Gilt auch für admin:logout – Abmelden geht über /konto/logout/.
            if request.user.is_authenticated and not self.has_permission(request):
                raise PermissionDenied
            return inner(request, *args, **kwargs)

        return update_wrapper(wrapper, inner)

    def login(self, request, extra_context=None):
        if request.user.is_authenticated:
            if not self.has_permission(request):
                raise PermissionDenied
            return HttpResponseRedirect(reverse("admin:index", current_app=self.name))
        next_url = request.GET.get("next") or reverse("admin:index", current_app=self.name)
        return redirect_to_login(next_url)
