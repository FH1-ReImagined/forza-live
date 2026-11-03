from django.apps import AppConfig


class MembersConfig(AppConfig):
    name = "members"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from members import signals  # noqa: F401
