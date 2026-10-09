from django.apps import AppConfig
from django.contrib.admin import apps as admin_apps


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "multigpt.accounts"
    label = "accounts"
    verbose_name = "Konten und Gruppen"

    def ready(self):
        from . import signals  # noqa: F401


class FamilyAdminConfig(admin_apps.AdminConfig):
    """Ersetzt "django.contrib.admin" in INSTALLED_APPS: eigene AdminSite mit can()."""

    default = False
    default_site = "multigpt.accounts.admin_site.FamilyAdminSite"
