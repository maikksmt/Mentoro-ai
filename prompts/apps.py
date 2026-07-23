from django.apps import AppConfig


class PromptsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "prompts"

    # django-reversion registration lives in
    # core.reversion_registration.register_editorial_reversion_models(), called
    # from MentoroAdminConfig.ready() before admin autodiscovery (Beta 11.11B1).
    # The ready() hook that used to register Prompt here ran after the admin had
    # already auto-registered it, and its RegistrationError was swallowed - so
    # the intended follow=("translations",) never applied. This config now has
    # no startup work of its own; it stays because Django resolves the app label
    # and default_auto_field through it.
