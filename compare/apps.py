from django.apps import AppConfig


class CompareConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "compare"

    # This app never registered anything with django-reversion itself; until
    # Beta 11.11B1 the whole Comparison graph came from ComparisonAdmin's
    # VersionAdmin auto-registration, which reached the parent and the tool
    # entries but neither model's translations. Registration now lives in
    # core.reversion_registration.register_editorial_reversion_models(); do not
    # add a competing reversion.register() call here.
