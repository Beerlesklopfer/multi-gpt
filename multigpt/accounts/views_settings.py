"""Seite „Einstellungen“: persönliche Einstellungen des eigenen Kontos.

Bisher: Zitieren (Stil, Kurzbeleg im Antworttext, Seite/Absatz anzeigen). Die
Seite ändert nur das angemeldete Konto – es gibt keinen Parameter für ein
anderes Konto.
"""

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from multigpt.chat import citations


class SettingsForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ["citation_style", "citation_short", "citation_locator", "mark_unverified_links"]
        labels = {
            "citation_style": "Zitierstil",
            "citation_short": "Quellen im Antworttext als Kurzbeleg zeigen",
            "citation_locator": "Abschnitt, Seite und Absatz anzeigen",
            "mark_unverified_links": "Ungeprüfte Links markieren",
        }
        help_texts = {
            "citation_style": "Für die Quellenliste unter Antworten, „Zitat kopieren“ und den "
            "Kurzbeleg, den das Modell bekommt.",
            "citation_short": "Statt [1] erscheint z. B. (Müller 2024, S. 12). "
            "Aus: nur die Nummer.",
            "citation_locator": "Fundstelle in Quellenliste und Kurzbeleg, z. B. "
            "„Abschn. 7.5.3, S. 12, Abs. 3“.",
            "mark_unverified_links": "Links in Antworten, die aus keiner Quelle und keinem "
            "Werkzeugergebnis stammen, bekommen ein Warnsymbol (möglicherweise erfunden).",
        }
        widgets = {"citation_style": forms.RadioSelect}


@require_http_methods(["GET", "POST"])
@login_required
def settings_page(request):
    form = SettingsForm(request.POST or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Einstellungen gespeichert.")
        return redirect("settings")
    example = citations.Reference(
        title="Jahresbericht 2024",
        type=citations.TYPE_REPORT,
        authors=("Müller, Hans",),
        date="2024",
        publisher="Beispielverlag",
        place="Berlin",
    )
    where = citations.Locator(page=12, paragraph=3)
    samples = [
        {
            "key": key,
            "label": label,
            "entry": citations.entry(example, key),
            "short": citations.short(example, key, where),
        }
        for key, label in citations.STYLE_CHOICES
    ]
    return render(request, "accounts/settings.html", {"form": form, "samples": samples})
