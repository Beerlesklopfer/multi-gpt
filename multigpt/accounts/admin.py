from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group

from .models import User, UserGroup

admin.site.unregister(Group)


@admin.register(User)
class AccountUserAdmin(UserAdmin):
    pass


@admin.register(UserGroup)
class UserGroupAdmin(GroupAdmin):
    pass
