"""Seite „Einstellungen“: persönliche Einstellungen des eigenen Kontos.

Bisher: Zitieren (Stil, Kurzbeleg im Antworttext, Seite/Absatz anzeigen) und
das eigene Kontextmenü im Chat (M13-01) und die Sprache (M10: Modelle, Stimme, automatisch
vorlesen, nur mit dem Recht „Sprache“ und eingerichteten Modellen). Die
Seite ändert nur das angemeldete Konto – es gibt keinen Parameter für ein
anderes Konto.
"""

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from multigpt.chat import citations, speech
from multigpt.chat.models import AIModel, SpeechPreference


class SettingsForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = [
            "citation_style",
            "citation_short",
            "citation_locator",
            "mark_unverified_links",
            "context_menu",
        ]
        labels = {
            "citation_style": "Zitierstil",
            "citation_short": "Quellen im Antworttext als Kurzbeleg zeigen",
            "citation_locator": "Abschnitt, Seite und Absatz anzeigen",
            "mark_unverified_links": "Ungeprüfte Links markieren",
            "context_menu": "Eigenes Kontextmenü im Chat",
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
            "context_menu": "Markierten Text per Rechtsklick, Leiste unter der Auswahl oder "
            "Umschalt+F10 zitieren, nachfragen und kopieren. Umschalt+Rechtsklick zeigt immer "
            "das Browsermenü. Aus: nur das Browsermenü.",
        }
        widgets = {"citation_style": forms.RadioSelect}


class SpeechForm(forms.Form):
    """Spracheinstellungen des Kontos (M10): Modelle, Sprache der Aufnahme, Stimme."""

    stt_model = forms.ModelChoiceField(
        label="Spracherkennung (Mikrofon)",
        queryset=AIModel.objects.none(),
        required=False,
        empty_label="Standard",
    )
    stt_language = forms.CharField(
        label="Sprache der Aufnahme",
        required=False,
        max_length=10,
        help_text="Sprachcode wie de oder en; leer = das Modell erkennt die Sprache selbst.",
    )
    tts_model = forms.ModelChoiceField(
        label="Sprachausgabe (Vorlesen)",
        queryset=AIModel.objects.none(),
        required=False,
        empty_label="Standard",
    )
    voice = forms.CharField(
        label="Stimme",
        required=False,
        max_length=64,
        help_text="Name der Stimme; die Liste des Modells steht als Vorschlag im Feld. Leer = "
        "Standard des Modells (manche Modelle, etwa mit geklonten Stimmen, brauchen eine Stimme).",
    )
    auto_read_aloud = forms.BooleanField(
        label="Antworten automatisch vorlesen",
        required=False,
        help_text="Jede neue Antwort wird nach dem Erscheinen vorgelesen (kostet je Zeichen; "
        "manche Browser spielen Ton erst nach einem Klick ab).",
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        for name, capability in (
            ("stt_model", AIModel.Capability.STT),
            ("tts_model", AIModel.Capability.TTS),
        ):
            ids = [m.pk for m in speech.candidate_models(user, capability)]
            self.fields[name].queryset = AIModel.objects.filter(pk__in=ids).order_by("display_name")
            self.fields[name].label_from_instance = lambda m: (
                f"{m.display_name} ({m.provider.name})"
            )
        self.fields["stt_model"].queryset = self.fields["stt_model"].queryset.select_related(
            "provider"
        )
        self.fields["tts_model"].queryset = self.fields["tts_model"].queryset.select_related(
            "provider"
        )

        self.fields["voice"].widget.attrs.update(
            {"list": "voice-suggestions", "autocomplete": "off"}
        )
        self.fields["stt_language"].widget.attrs.update({"autocapitalize": "none", "size": 6})

    def clean_stt_language(self):
        value = (self.cleaned_data["stt_language"] or "").strip()
        try:
            return speech.clean_language(value) or ""
        except speech.SpeechError as exc:
            raise forms.ValidationError(exc.message) from None

    def clean_voice(self):
        value = (self.cleaned_data["voice"] or "").strip()
        if value and not speech.VOICE_PATTERN.match(value):
            raise forms.ValidationError(
                "Nur Buchstaben, Ziffern, Punkt, Unterstrich und Bindestrich (höchstens 64)."
            )
        return value

    def save(self):
        """Nur die Felder speichern, die das Formular hat (ohne Sprachausgabe bzw. Erkennung fehlen
        die jeweils anderen)."""
        data = self.cleaned_data
        values = {
            name: (data[name] or "") if name in ("stt_language", "voice") else data[name]
            for name in ("stt_model", "stt_language", "tts_model", "voice")
            if name in data
        }
        if values:
            SpeechPreference.objects.update_or_create(user=self.user, defaults=values)
        if "auto_read_aloud" in data:
            get_user_model().objects.filter(pk=self.user.pk).update(
                auto_read_aloud=data["auto_read_aloud"]
            )


def _speech_form(request):
    """``SpeechForm`` mit den gespeicherten Werten, ``None`` ohne Recht oder ohne Sprachmodelle."""
    user = request.user
    available = speech.availability(user)
    if not (available["stt"] or available["tts"]):
        return None
    pref = speech.preference(user)
    initial = {
        "stt_model": pref.stt_model_id if pref else None,
        "stt_language": pref.stt_language if pref else "de",
        "tts_model": pref.tts_model_id if pref else None,
        "voice": pref.voice if pref else "",
        "auto_read_aloud": user.auto_read_aloud,
    }
    form = SpeechForm(request.POST or None, initial=initial, user=user, prefix="speech")
    if not available["stt"]:
        del form.fields["stt_model"], form.fields["stt_language"]
    if not available["tts"]:
        del form.fields["tts_model"], form.fields["voice"], form.fields["auto_read_aloud"]
    return form


def _voice_suggestions(user, form) -> list[str]:
    """Stimmen des gewählten (sonst ersten nutzbaren) Sprachausgabe-Modells als Vorschlag."""
    if form is None or "voice" not in form.fields:
        return []
    models = speech.ordered_models(user, AIModel.Capability.TTS)
    if not models:
        return []
    return speech.voices(models[0])


@require_http_methods(["GET", "POST"])
@login_required
def settings_page(request):
    form = SettingsForm(request.POST or None, instance=request.user)
    speech_form = _speech_form(request)
    if request.method == "POST":
        valid = form.is_valid()
        if speech_form is not None:
            valid = speech_form.is_valid() and valid
        if valid:
            form.save()
            if speech_form is not None:
                speech_form.save()
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
    context = {
        "form": form,
        "samples": samples,
        "speech_form": speech_form,
        "voice_suggestions": _voice_suggestions(request.user, speech_form),
    }
    return render(request, "accounts/settings.html", context)
