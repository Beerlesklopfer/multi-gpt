from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group

from .models import Gruppe, User

admin.site.unregister(Group)


@admin.register(User)
class KontoAdmin(UserAdmin):
    pass


@admin.register(Gruppe)
class GruppeAdmin(GroupAdmin):
    pass
