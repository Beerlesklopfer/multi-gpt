"""Startdaten (M2-04): vier Rollen nach Plan 8f und die Standardgruppe "Familie".

Bereits vorhandene Konten bekommen dieselbe Vorgabe wie neue (Superuser ->
Verwalter, sonst Gast, siehe signals.py) und werden Mitglied der "Familie".
Die Modell- und MCP-Freigaben der Rollen Jugendlicher und Gast bleiben leer;
der Verwalter wählt sie im Admin aus, sobald Modelle angelegt sind.
"""

from decimal import Decimal

from django.db import migrations

TEEN_PROMPT = (
    "Du sprichst mit einer jugendlichen Person aus unserer Familie. Antworte "
    "altersgerecht, freundlich und sachlich. Hilf bei Hausaufgaben, indem du "
    "erklärst und zum eigenen Denken anregst, statt fertige Lösungen zu liefern. "
    "Lehne gefährliche, gewaltverherrlichende oder nicht jugendfreie Inhalte ab."
)

ROLES = [
    {
        "key": "admin",
        "name": "Verwalter",
        "is_admin": True,
        "all_models": True,
        "all_mcp_servers": True,
        "can_web_search": True,
        "can_images": True,
        "can_voice": True,
        "can_upload_documents": True,
        "can_share": True,
        "monthly_budget": None,
        "fixed_system_prompt": "",
    },
    {
        "key": "adult",
        "name": "Erwachsener",
        "is_admin": False,
        "all_models": True,
        "all_mcp_servers": True,
        "can_web_search": True,
        "can_images": True,
        "can_voice": True,
        "can_upload_documents": True,
        "can_share": True,
        "monthly_budget": None,
        "fixed_system_prompt": "",
    },
    {
        "key": "teen",
        "name": "Jugendlicher",
        "is_admin": False,
        "all_models": False,
        "all_mcp_servers": False,
        "can_web_search": True,
        "can_images": False,
        "can_voice": True,
        "can_upload_documents": True,
        "can_share": False,
        "monthly_budget": Decimal("10.00"),
        "fixed_system_prompt": TEEN_PROMPT,
    },
    {
        "key": "guest",
        "name": "Gast",
        "is_admin": False,
        "all_models": False,
        "all_mcp_servers": False,
        "can_web_search": False,
        "can_images": False,
        "can_voice": False,
        "can_upload_documents": False,
        "can_share": False,
        "monthly_budget": Decimal("2.00"),
        "fixed_system_prompt": "",
    },
]

FAMILY = "Familie"


def create_initial_data(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    UserGroup = apps.get_model("accounts", "UserGroup")
    User = apps.get_model("accounts", "User")

    roles = {}
    for data in ROLES:
        data = dict(data)
        key = data.pop("key")
        roles[key], _ = Role.objects.get_or_create(key=key, defaults=data)

    # Eine evtl. schon vorhandene Gruppe "Familie" (auth.Group ohne UserGroup-Zeile)
    # wird übernommen statt doppelt angelegt.
    Group = apps.get_model("auth", "Group")
    group = Group.objects.filter(name=FAMILY).first()
    if group is None:
        family = UserGroup.objects.create(name=FAMILY, is_default=True)
    else:
        family = UserGroup.objects.filter(group_ptr_id=group.pk).first()
        if family is None:
            family = UserGroup(group_ptr_id=group.pk, name=group.name, is_default=True)
            family.save()
        else:
            family.is_default = True
            family.save(update_fields=["is_default"])

    for user in User.objects.all():
        if user.role_id is None:
            user.role = roles["admin" if user.is_superuser else "guest"]
            user.save(update_fields=["role"])
        user.groups.add(family.group_ptr_id)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_roles_and_account_fields"),
    ]

    operations = [
        migrations.RunPython(create_initial_data, migrations.RunPython.noop),
    ]
