"""Vorgaben für neue Konten.

- Rolle: Ein Konto ohne Rolle bekommt beim Speichern "admin", wenn es Superuser
  ist (createsuperuser, Notfallzugang), sonst "guest". Gast ist die Rolle mit
  den wenigsten Rechten: Wird die Rolle irgendwo vergessen (Shell, Fremdcode),
  bekommt das Konto zu wenig statt zu viel Rechte (fail closed). Die regulären
  Wege – `make user`, Admin-Maske, später die Seite "Familie" – verlangen eine
  ausdrückliche Rollenwahl.
- Gruppen: Jedes neu angelegte Konto wird Mitglied aller Standardgruppen
  (`UserGroup.is_default`, ab Werk "Familie").
"""

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import Role, User, UserGroup


def default_role_key(user):
    return Role.ADMIN if user.is_superuser else Role.GUEST


@receiver(pre_save, sender=User, dispatch_uid="accounts_assign_default_role")
def assign_default_role(sender, instance, raw=False, **kwargs):
    if raw or instance.role_id is not None:
        return
    instance.role = Role.objects.filter(key=default_role_key(instance)).first()


@receiver(post_save, sender=User, dispatch_uid="accounts_join_default_groups")
def join_default_groups(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    group_ids = UserGroup.objects.filter(is_default=True).values_list("group_ptr_id", flat=True)
    instance.groups.add(*group_ids)
