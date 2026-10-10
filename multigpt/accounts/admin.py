from django import forms
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.forms import AdminUserCreationForm
from django.contrib.auth.models import Group

from .models import Role, User, UserGroup

admin.site.unregister(Group)

START_ROLE_KEYS = {Role.ADMIN, Role.ADULT, Role.TEEN, Role.GUEST}


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "key",
        "is_admin",
        "all_models",
        "can_web_search",
        "can_images",
        "can_voice",
        "can_upload_documents",
        "can_share",
        "can_compute",
        "can_create_documents",
        "monthly_budget",
    )
    search_fields = ("name", "key")
    filter_horizontal = ("allowed_models", "allowed_mcp_servers")
    fieldsets = (
        (None, {"fields": ("name", "key", "is_admin")}),
        ("Modelle", {"fields": ("all_models", "allowed_models")}),
        (
            "Funktionen",
            {
                "fields": (
                    "can_web_search",
                    "can_images",
                    "can_voice",
                    "can_upload_documents",
                    "can_share",
                    "can_compute",
                    "can_create_documents",
                )
            },
        ),
        ("MCP-Server", {"fields": ("all_mcp_servers", "allowed_mcp_servers")}),
        ("Budget und System-Prompt", {"fields": ("monthly_budget", "fixed_system_prompt")}),
    )

    def get_readonly_fields(self, request, obj=None):
        # Die Schlüssel der Startrollen nutzt der Code (Vorgaben, Tests) – nicht änderbar.
        if obj is not None and obj.key in START_ROLE_KEYS:
            return ("key",)
        return ()


class AccountCreationForm(AdminUserCreationForm):
    """Beim Anlegen im Admin ist die Rolle Pflicht (keine stille Vorgabe)."""

    role = forms.ModelChoiceField(queryset=Role.objects.all(), label="Rolle")

    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("username", "role")


@admin.register(User)
class AccountUserAdmin(UserAdmin):
    add_form = AccountCreationForm
    list_display = ("username", "display_name", "role", "is_active", "is_superuser")
    list_filter = ("role", "is_active", "is_superuser", "groups")
    search_fields = ("username", "display_name", "first_name", "last_name", "email")
    list_select_related = ("role",)
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Persönliche Daten", {"fields": ("display_name", "first_name", "last_name", "email")}),
        (
            "Rolle und Optionen",
            {
                "fields": (
                    "role",
                    "monthly_budget_override",
                    "allow_supervision",
                    "auto_read_aloud",
                )
            },
        ),
        (
            "Zugang",
            {"fields": ("is_active", "groups", "is_staff", "is_superuser", "user_permissions")},
        ),
        ("Wichtige Daten", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "role", "usable_password", "password1", "password2"),
            },
        ),
    )


@admin.register(UserGroup)
class UserGroupAdmin(GroupAdmin):
    list_display = ("name", "is_default")
    fields = ("name", "is_default", "permissions")
