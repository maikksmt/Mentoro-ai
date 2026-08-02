from django.apps import AppConfig


class GuidesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "guides"

    def ready(self):
        # django-reversion registration lives in
        # core.reversion_registration.register_editorial_reversion_models(),
        # called from MentoroAdminConfig.ready() before admin autodiscovery
        # (Beta 11.11B1). Registering it here ran too late - the admin had
        # already auto-registered Guide - and the resulting RegistrationError
        # was swallowed, so the intended follow=("translations",) never applied.
        from . import signals  # noqa: F401
