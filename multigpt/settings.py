"""Settings für MultiGPT.

Alle Werte kommen aus der Umgebung. Zusätzlich wird eine Env-Datei gelesen:
die in MULTI_GPT_ENV_FILE angegebene (Betrieb: /etc/multi-gpt/.env, gesetzt
von mgpt-ctl) oder, falls vorhanden, .env im Projektordner (Entwicklung).
Bereits gesetzte Umgebungsvariablen haben Vorrang vor der Datei.
"""

import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
_env_file = os.environ.get("MULTI_GPT_ENV_FILE") or BASE_DIR / ".env"
if Path(_env_file).is_file():
    env.read_env(str(_env_file))

SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Für Meilenstein 2 (Fernet-verschlüsselte API-Keys).
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default="")

INSTALLED_APPS = [
    # Eigene AdminSite: Zugang nur über can(user, Action.ADMIN), nicht is_staff.
    "multigpt.accounts.apps.FamilyAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "axes",
    "multigpt.accounts",
    "multigpt.chat",
    # Admin-Abschnitt „Dokumente (RAG)“ (nur Proxy-Modelle auf chat).
    "multigpt.rag",
    # Kontenrahmen, Preise, Kurse, Buchungen und Budgets je Konto (M6).
    "multigpt.billing",
]

MIDDLEWARE = [
    # Muss als erste Middleware stehen: beantwortet /healthz/ ohne Host-Prüfung.
    "multigpt.health.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Muss als letzte Middleware stehen.
    "axes.middleware.AxesMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
    # Verwalterrolle -> alle Modellrechte im Admin (nur Rechte, keine Anmeldung).
    "multigpt.accounts.backends.RoleAdminBackend",
]

ROOT_URLCONF = "multigpt.urls"
WSGI_APPLICATION = "multigpt.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "multigpt" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
# Vorlage für die Test-DB (Entwicklung): UTF-8 mit pgvector, siehe make db-create.
# Ohne Angabe nimmt PostgreSQL template1, das je nach Cluster SQL_ASCII ist.
_test_template = env("DB_TEST_TEMPLATE", default="")
if _test_template:
    DATABASES["default"]["TEST"] = {"TEMPLATE": _test_template}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"

# Kosten (multigpt/billing): EZB-Referenzkurs USD automatisch abrufen? Standard
# aus (externer Abruf); Kurse lassen sich immer von Hand im Admin pflegen.
BILLING_ECB_FETCH = env.bool("BILLING_ECB_FETCH", default=False)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "chat:index"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "de"
LANGUAGES = [("de", "Deutsch")]
TIME_ZONE = "Europe/Berlin"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = env.path("STATIC_ROOT", default=str(BASE_DIR / "staticfiles"))
MEDIA_URL = "media/"
MEDIA_ROOT = env.path("MEDIA_ROOT", default=str(BASE_DIR / "media"))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# Sicherheit. SECURE_COOKIES erst aktivieren, wenn TLS (nginx) davor steht.
SECURE_COOKIES = env.bool("SECURE_COOKIES", default=False)
SESSION_COOKIE_SECURE = SECURE_COOKIES
CSRF_COOKIE_SECURE = SECURE_COOKIES
if SECURE_COOKIES:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"

# Downloads hochgeladener Dokumente über nginx (X-Accel-Redirect) statt über
# gunicorn. Nur einschalten, wenn nginx davor steht, die interne location
# X_ACCEL_REDIRECT_PREFIX auf MEDIA_ROOT zeigt und der nginx-Nutzer (www-data)
# MEDIA_ROOT lesen darf. Dateien aus Verzeichnisquellen liefert weiterhin Django.
USE_X_ACCEL_REDIRECT = env.bool("USE_X_ACCEL_REDIRECT", default=False)
X_ACCEL_REDIRECT_PREFIX = "/_protected/media/"

# Login-Drosselung (django-axes): Sperre je Kombination aus Nutzername und IP.
AXES_FAILURE_LIMIT = env.int("AXES_FAILURE_LIMIT", default=5)
AXES_COOLOFF_TIME = env.float("AXES_COOLOFF_HOURS", default=0.25)
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "registration/locked.html"
# Anzahl Reverse Proxys vor gunicorn (Umgebung: AXES_PROXY_COUNT; Debian-Paket mit
# nginx: 1). Ausgewertet von multigpt.accounts.client_ip, da django-ipware nicht
# installiert ist. Der Setting-Name ist nicht AXES_PROXY_COUNT, weil axes diesen
# veralteten Namen mit axes.W004 anmahnt.
REVERSE_PROXY_COUNT = env.int("AXES_PROXY_COUNT", default=0)
AXES_CLIENT_IP_CALLABLE = "multigpt.accounts.client_ip.client_ip"

# Logging nach stdout (journald bzw. Docker). Keine Nachrichteninhalte, keine Keys.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        # httpx/httpcore loggen auf INFO jede Anfrage mit voller URL (Websuche,
        # Seitenabruf, Anbieter) – Logs enthalten nie URLs, also erst ab WARNING.
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
        "mcp": {"level": "WARNING"},
    },
}

# Websuche (M8): Seitenabruf auch von Intranet-Adressen erlauben. Nur für
# Browsertests in der Entwicklung; wirkt ausschließlich zusammen mit DEBUG=True.
WEBSEARCH_ALLOW_PRIVATE = env.bool("WEBSEARCH_ALLOW_PRIVATE", default=False)

# RAG (M7): feste Vektordimension von Chunk.embedding (HNSW-Index braucht eine
# feste Dimension, pgvector indexiert höchstens 2000). 768 = nomic-embed-text-v1.5
# lokal über LM Studio (M7-09); OpenAI text-embedding-3-* würden per
# ``dimensions`` auf 768 gekürzt. Bewusst keine Umgebungsvariable: Eine Änderung
# braucht eine Migration der Spalte und „Alles neu indexieren“.
RAG_EMBEDDING_DIMENSIONS = 768
# Deterministische Schein-Embeddings ohne Anbieteraufruf (Browsertests in der
# Entwicklung); wirkt ausschließlich zusammen mit DEBUG=True.
RAG_FAKE_EMBEDDINGS = env.bool("RAG_FAKE_EMBEDDINGS", default=False)

# RAG-Indexierung (M7, Agent ingest): Upload-Grenze für Dokumente in MB,
# Sprachen der Texterkennung (Tesseract-Sprachpakete, z. B. "deu+eng") und
# Wiederholungen fehlgeschlagener Hintergrundjobs.
DOCUMENT_MAX_UPLOAD_MB = env.int("DOCUMENT_MAX_UPLOAD_MB", default=25)
# Anhänge im Chat: Bilder bis ATTACHMENT_MAX_IMAGE_MB, Dokumente bis
# DOCUMENT_MAX_UPLOAD_MB, höchstens ATTACHMENT_MAX_PER_MESSAGE je Nachricht.
ATTACHMENT_MAX_IMAGE_MB = env.int("ATTACHMENT_MAX_IMAGE_MB", default=20)
ATTACHMENT_MAX_PER_MESSAGE = env.int("ATTACHMENT_MAX_PER_MESSAGE", default=10)
OCR_LANGUAGES = env("OCR_LANGUAGES", default="deu+eng")
JOB_MAX_ATTEMPTS = env.int("JOB_MAX_ATTEMPTS", default=5)

# Verzeichnisquellen (Agent crawler): erlaubte Wurzeln auf dem Server/NAS, aus
# denen Sammlungen Dateien einlesen dürfen (Komma-Liste absoluter Pfade). Leer =
# Funktion aus. Die Dateien bleiben am Ort; der Dienstnutzer braucht Leserechte,
# unter /home zusätzlich ein systemd-Drop-in (ProtectHome, siehe Unit-Dateien).
RAG_SOURCE_ROOTS = [p for p in env.list("RAG_SOURCE_ROOTS", default=[]) if p.strip()]
# Grenze je Einlesevorgang (weitere Dateien kommen beim nächsten Lauf dran).
RAG_SOURCE_MAX_FILES = env.int("RAG_SOURCE_MAX_FILES", default=5000)
