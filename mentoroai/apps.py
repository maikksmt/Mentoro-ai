from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig


class MentoroAdminConfig(AdminConfig):
    default_site = "mentoroai.admin_site.MentoroAdminSite"

    def ready(self):
        """
        Register the editorial models with django-reversion *before* admin
        autodiscovery, then hand over to Django's own admin ready().

        The order is load-bearing, not cosmetic. ``AdminConfig.ready()`` calls
        ``admin.autodiscover()``, which instantiates every ``ModelAdmin``, and
        ``reversion.admin.VersionAdmin.__init__()`` auto-registers any model
        that is not registered yet - deriving ``follow`` from the admin's
        inlines. Because this app config is ``INSTALLED_APPS[0]``, it used to
        win that race against the editorial apps' own ``ready()`` methods,
        whose ``reversion.register()`` calls then failed with a swallowed
        ``RegistrationError`` (Beta 11.11A audit; fixed in Beta 11.11B1).

        Registering here first makes the graph deterministic: by the time any
        ``VersionAdmin`` is constructed the models are already registered, so
        its auto-registration branch is a no-op and cannot change ``follow``.
        """
        from core.reversion_registration import register_editorial_reversion_models

        register_editorial_reversion_models()
        super().ready()


class MentoroAIConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mentoroai"
