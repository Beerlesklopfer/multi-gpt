from django.contrib.auth.models import AbstractUser, Group


class User(AbstractUser):
    """Familienkonto. Erweitert Djangos User; Rolle, Budget und Optionen folgen in M2."""

    class Meta(AbstractUser.Meta):
        swappable = "AUTH_USER_MODEL"
        verbose_name = "Konto"
        verbose_name_plural = "Konten"


class UserGroup(Group):
    """Erweitert Djangos Group (Tabellenvererbung, 1:1 über group_ptr).

    Mitgliedschaft läuft weiter über User.groups; die Zusatzfelder liegen hier.
    """

    class Meta:
        verbose_name = "Gruppe"
        verbose_name_plural = "Gruppen"
