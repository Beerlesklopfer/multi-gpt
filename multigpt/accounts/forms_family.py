"""Formulare der Seite „Familie“ (M6-04). Passwörter kommen hier nie vor:
Start- und neue Passwörter erzeugt der Server (siehe views_family)."""

from decimal import Decimal

from django import forms
from django.contrib.auth.models import Group

from .models import Role, User, UserGroup


class RoleChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.name


def _roles():
    return Role.objects.order_by("-is_admin", "name")


class AccountCreateForm(forms.ModelForm):
    """Neues Konto: Anmeldename, Anzeigename und Rolle (Pflicht, keine Vorauswahl)."""

    role = RoleChoiceField(
        queryset=Role.objects.none(),
        label="Rolle",
        empty_label="Bitte wählen …",
        required=True,
    )

    class Meta:
        model = User
        fields = ("username", "display_name", "role")
        labels = {"username": "Anmeldename", "display_name": "Anzeigename"}
        help_texts = {"username": "Buchstaben, Ziffern und @ . + - _"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].queryset = _roles()
        self.fields["username"].widget.attrs.update({"autocomplete": "off", "maxlength": 150})
        self.fields["display_name"].widget.attrs.update({"autocomplete": "off"})


class RoleForm(forms.Form):
    role = RoleChoiceField(queryset=Role.objects.none(), label="Rolle", empty_label=None)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].queryset = _roles()


class BudgetForm(forms.Form):
    monthly_budget_override = forms.DecimalField(
        label="Eigenes Monatsbudget (EUR)",
        required=False,
        min_value=Decimal("0"),
        max_digits=8,
        decimal_places=2,
        help_text="Leer = Budget der Rolle.",
        localize=True,
    )


class SupervisionForm(forms.Form):
    allow_supervision = forms.BooleanField(label="Einsicht in Chats erlaubt", required=False)


class GroupNameForm(forms.Form):
    name = forms.CharField(label="Name", max_length=150, strip=True)

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        self.fields["name"].widget.attrs.update({"autocomplete": "off"})

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if not name:
            raise forms.ValidationError("Bitte einen Namen eingeben.")
        others = Group.objects.filter(name__iexact=name)
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise forms.ValidationError("Es gibt schon eine Gruppe mit diesem Namen.")
        return name


class GroupMembersForm(forms.Form):
    members = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Mitglieder",
    )

    def __init__(self, *args, group: UserGroup, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["members"]
        field.queryset = User.objects.order_by("display_name", "username")
        field.label_from_instance = lambda user: str(user)
        field.initial = list(group.user_set.values_list("pk", flat=True))
