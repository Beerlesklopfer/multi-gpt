"""Zentrale Rechteprüfung can() je Rolle und Aktion, Decorator, Mixin, Admin-Zugang (M2-05)."""

import pytest
from django.http import HttpResponse
from django.urls import path
from django.views import View

from multigpt import urls as project_urls
from multigpt.accounts.models import Role, User, UserGroup
from multigpt.accounts.permissions import Action, CanRequiredMixin, can, require_can
from multigpt.chat.models import AIModel, Collection, Conversation, McpServer, Provider, Share

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"
ROLES = ("admin", "adult", "teen", "guest")


# --- Test-Views (für direkte URL-Aufrufe: pytest.mark.urls auf dieses Modul) --


@require_can(Action.SHARE)
def share_view(request):
    return HttpResponse("ok")


class AdminOnlyView(CanRequiredMixin, View):
    required_action = Action.MANAGE_FAMILY

    def get(self, request):
        return HttpResponse("ok")


urlpatterns = [
    path("share/", share_view),
    path("family/", AdminOnlyView.as_view()),
    *project_urls.urlpatterns,
]


# --- Hilfen -------------------------------------------------------------------


def make_user(role_key, username=None, **extra):
    return User.objects.create_user(
        username or f"u_{role_key}",
        password=PASSWORD,
        role=Role.objects.get(key=role_key),
        **extra,
    )


@pytest.fixture
def provider():
    return Provider.objects.create(name="Testanbieter", kind=Provider.Kind.OPENAI_COMPAT)


@pytest.fixture
def ai_model(provider):
    return AIModel.objects.create(provider=provider, model_id="m-1", display_name="Modell 1")


@pytest.fixture
def mcp_server():
    return McpServer.objects.create(
        name="Testserver", transport=McpServer.Transport.HTTP, url="http://mcp.example.lan/"
    )


# --- Funktionen je Rolle ----------------------------------------------------------

# Erwartung laut Plan 8f und Datenmigration 0003.
MATRIX = {
    Action.CHAT: {"admin", "adult", "teen", "guest"},
    Action.WEB_SEARCH: {"admin", "adult", "teen"},
    Action.IMAGES: {"admin", "adult"},
    Action.VOICE: {"admin", "adult", "teen"},
    Action.UPLOAD_DOCUMENTS: {"admin", "adult", "teen"},
    Action.SHARE: {"admin", "adult"},
    Action.MANAGE_FAMILY: {"admin"},
    Action.VIEW_USAGE_ALL: {"admin"},
    Action.ADMIN: {"admin"},
}


@pytest.mark.parametrize("role_key", ROLES)
@pytest.mark.parametrize("action", list(MATRIX), ids=str)
def test_capability_matrix(role_key, action):
    user = make_user(role_key)
    assert can(user, action) is (role_key in MATRIX[action])


@pytest.mark.parametrize("action", list(Action), ids=str)
def test_inactive_user_may_nothing(action, ai_model, mcp_server):
    user = make_user("admin", is_active=False)
    obj = {Action.USE_MODEL: ai_model, Action.USE_MCP_SERVER: mcp_server}.get(action)
    assert not can(user, action, obj)
    assert not can(user, action)


@pytest.mark.parametrize("action", list(Action), ids=str)
def test_anonymous_may_nothing(action):
    from django.contrib.auth.models import AnonymousUser

    assert not can(AnonymousUser(), action)
    assert not can(None, action)


def test_user_without_role_may_nothing(ai_model):
    user = make_user("adult")
    User.objects.filter(pk=user.pk).update(role=None)
    user.refresh_from_db()
    assert user.role is None
    for action in (Action.CHAT, Action.WEB_SEARCH, Action.ADMIN):
        assert not can(user, action)
    assert not can(user, Action.USE_MODEL, ai_model)


def test_superuser_may_all_functions():
    user = User.objects.create_superuser("root", password=PASSWORD)
    user.role = Role.objects.get(key="guest")
    user.save()
    for action in MATRIX:
        assert can(user, action)


def test_role_flags_are_live():
    """Änderungen an der Rolle wirken sofort (Admin-Pflege)."""
    user = make_user("guest")
    assert not can(user, Action.WEB_SEARCH)
    Role.objects.filter(key="guest").update(can_web_search=True)
    user = User.objects.get(pk=user.pk)
    assert can(user, Action.WEB_SEARCH)


def test_action_accepts_string():
    assert can(make_user("adult"), "share")
    with pytest.raises(ValueError):
        can(make_user("guest", "g2"), "fly")


# --- Modelle --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role_key", "expected"), [("admin", True), ("adult", True), ("teen", False), ("guest", False)]
)
def test_use_model_by_role(role_key, expected, ai_model):
    user = make_user(role_key)
    assert can(user, Action.USE_MODEL, ai_model) is expected
    assert can(user, Action.USE_MODEL) is expected


@pytest.mark.parametrize("role_key", ["teen", "guest"])
def test_use_model_only_allowed_list(role_key, ai_model, provider):
    other = AIModel.objects.create(provider=provider, model_id="m-2", display_name="Modell 2")
    Role.objects.get(key=role_key).allowed_models.add(ai_model)
    user = make_user(role_key)
    assert can(user, Action.USE_MODEL, ai_model)
    assert not can(user, Action.USE_MODEL, other)
    assert can(user, Action.USE_MODEL)


@pytest.mark.parametrize("role_key", ROLES)
def test_inactive_model_forbidden(role_key, ai_model):
    Role.objects.get(key=role_key).allowed_models.add(ai_model)
    ai_model.active = False
    ai_model.save()
    assert not can(make_user(role_key), Action.USE_MODEL, ai_model)


def test_inactive_provider_forbids_model(ai_model, provider):
    provider.active = False
    provider.save()
    ai_model.refresh_from_db()
    assert not can(make_user("adult"), Action.USE_MODEL, ai_model)
    superuser = User.objects.create_superuser("root", password=PASSWORD)
    assert not can(superuser, Action.USE_MODEL, ai_model)


def test_budget_hook_is_consulted(ai_model, monkeypatch):
    monkeypatch.setattr("multigpt.accounts.permissions.budget_allows", lambda user, model: False)
    assert not can(make_user("adult"), Action.USE_MODEL, ai_model)


# --- MCP-Server -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role_key", "expected"), [("admin", True), ("adult", True), ("teen", False), ("guest", False)]
)
def test_use_mcp_server_by_role(role_key, expected, mcp_server):
    assert can(make_user(role_key), Action.USE_MCP_SERVER, mcp_server) is expected


def test_use_mcp_server_allowed_list_and_inactive(mcp_server):
    Role.objects.get(key="teen").allowed_mcp_servers.add(mcp_server)
    user = make_user("teen")
    assert can(user, Action.USE_MCP_SERVER, mcp_server)
    assert can(user, Action.USE_MCP_SERVER)
    mcp_server.active = False
    mcp_server.save()
    assert not can(user, Action.USE_MCP_SERVER, mcp_server)
    assert not can(make_user("admin"), Action.USE_MCP_SERVER, mcp_server)


# --- Lesen/Schreiben: Besitz und Freigaben ------------------------------------------------


@pytest.fixture
def owner():
    return make_user("adult", "owner")


@pytest.fixture
def other():
    return make_user("adult", "other")


@pytest.fixture(params=["conversation", "collection"])
def target(request, owner):
    if request.param == "conversation":
        return Conversation.objects.create(user=owner, title="Privat")
    return Collection.objects.create(owner=owner, name="Privat")


def _share(target, group, can_write=False):
    field = target._meta.model_name
    return Share.objects.create(**{field: target}, group=group, can_write=can_write)


def test_owner_reads_and_writes(target, owner):
    assert can(owner, Action.READ, target)
    assert can(owner, Action.WRITE, target)


def test_stranger_neither_reads_nor_writes(target, other):
    assert not can(other, Action.READ, target)
    assert not can(other, Action.WRITE, target)


def test_admin_and_superuser_do_not_see_foreign_chats(target):
    assert not can(make_user("admin", "verwalter"), Action.READ, target)
    superuser = User.objects.create_superuser("root", password=PASSWORD)
    assert not can(superuser, Action.READ, target)


def test_read_share(target, other):
    group = UserGroup.objects.create(name="Eltern")
    other.groups.add(group)
    _share(target, group)
    assert can(other, Action.READ, target)
    assert not can(other, Action.WRITE, target)


def test_write_share(target, other):
    group = UserGroup.objects.create(name="Eltern")
    other.groups.add(group)
    _share(target, group, can_write=True)
    assert can(other, Action.READ, target)
    assert can(other, Action.WRITE, target)


def test_share_to_foreign_group_does_not_help(target, other):
    _share(target, UserGroup.objects.create(name="Eltern"), can_write=True)
    assert not can(other, Action.READ, target)


def test_default_group_share_reaches_new_members(target, owner):
    _share(target, UserGroup.objects.get(name="Familie"))
    newcomer = make_user("guest", "neu")
    assert can(newcomer, Action.READ, target)
    assert not can(newcomer, Action.WRITE, target)


def test_revoking_share_takes_effect_immediately(target, other):
    group = UserGroup.objects.create(name="Eltern")
    other.groups.add(group)
    share = _share(target, group)
    assert can(other, Action.READ, target)
    share.delete()
    assert not can(other, Action.READ, target)


def test_leaving_group_takes_effect_immediately(target, other):
    group = UserGroup.objects.create(name="Eltern")
    other.groups.add(group)
    _share(target, group)
    other.groups.remove(group)
    assert not can(other, Action.READ, target)


def test_inactive_owner_may_not_read(target, owner):
    owner.is_active = False
    owner.save()
    assert not can(owner, Action.READ, target)


def test_read_without_or_with_unknown_object(owner, ai_model):
    assert not can(owner, Action.READ)
    assert not can(owner, Action.WRITE, ai_model)


# --- Decorator und Mixin bei direktem URL-Aufruf ------------------------------------


@pytest.mark.urls("tests.test_permissions")
@pytest.mark.parametrize(("role_key", "status"), [("adult", 200), ("teen", 403), ("guest", 403)])
def test_require_can_decorator(client, role_key, status):
    client.force_login(make_user(role_key))
    assert client.get("/share/").status_code == status


@pytest.mark.urls("tests.test_permissions")
@pytest.mark.parametrize(
    ("role_key", "status"), [("admin", 200), ("adult", 403), ("teen", 403), ("guest", 403)]
)
def test_can_required_mixin(client, role_key, status):
    client.force_login(make_user(role_key))
    assert client.get("/family/").status_code == status


@pytest.mark.urls("tests.test_permissions")
@pytest.mark.parametrize("url", ["/share/", "/family/"])
def test_anonymous_redirected_to_login(client, url):
    response = client.get(url)
    assert response.status_code == 302
    assert response.url.startswith("/konto/login/")


@pytest.mark.urls("tests.test_permissions")
def test_inactive_session_user_forbidden(client):
    user = make_user("admin")
    client.force_login(user)
    User.objects.filter(pk=user.pk).update(is_active=False)
    # ModelBackend lehnt inaktive Konten ab -> Sitzung gilt als anonym.
    assert client.get("/family/").status_code == 302


# --- Django-Admin -------------------------------------------------------------


ADMIN_URLS = [
    "/admin/",
    "/admin/login/",
    "/admin/accounts/user/",
    "/admin/accounts/role/",
    "/admin/accounts/usergroup/",
    "/admin/accounts/role/add/",
]


@pytest.mark.parametrize("role_key", ["adult", "teen", "guest"])
@pytest.mark.parametrize("url", ADMIN_URLS)
def test_non_admin_roles_get_403_even_with_is_staff(client, role_key, url):
    client.force_login(make_user(role_key, is_staff=True))
    assert client.get(url).status_code == 403


@pytest.mark.parametrize("url", ADMIN_URLS[:1] + ADMIN_URLS[2:])
def test_admin_role_without_superuser_reaches_admin(client, url):
    client.force_login(make_user("admin"))
    assert client.get(url).status_code == 200


def test_admin_role_without_is_staff_reaches_admin(client):
    client.force_login(make_user("admin", is_staff=False))
    assert client.get("/admin/").status_code == 200
    # Eingeloggt und berechtigt: Admin-Login leitet zur Übersicht.
    response = client.get("/admin/login/")
    assert response.status_code == 302
    assert response.url == "/admin/"


def test_superuser_reaches_admin(client):
    client.force_login(User.objects.create_superuser("root", password=PASSWORD))
    assert client.get("/admin/accounts/role/").status_code == 200


def test_anonymous_admin_goes_to_app_login(client):
    response = client.get("/admin/", follow=True)
    assert response.redirect_chain[-1][0].startswith("/konto/login/")
    assert response.status_code == 200


def test_admin_creates_account_with_role(client):
    client.force_login(make_user("admin"))
    teen = Role.objects.get(key="teen")
    response = client.post(
        "/admin/accounts/user/add/",
        {
            "username": "nina",
            "role": teen.pk,
            "usable_password": "true",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    nina = User.objects.get(username="nina")
    assert nina.role == teen
    assert nina.groups.filter(name="Familie").exists()


def test_admin_add_form_requires_role(client):
    client.force_login(make_user("admin"))
    response = client.post(
        "/admin/accounts/user/add/",
        {
            "username": "otto",
            "usable_password": "true",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )
    assert response.status_code == 200
    assert not User.objects.filter(username="otto").exists()
