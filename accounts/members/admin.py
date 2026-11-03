from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from members.models import AuditEvent, SignIn, User

admin.site.register(User, UserAdmin)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("at", "user", "event")
    list_filter = ("event",)
    search_fields = ("user__username",)


@admin.register(SignIn)
class SignInAdmin(admin.ModelAdmin):
    list_display = ("created", "user")
    search_fields = ("user__username",)
    exclude = ("session_key",)
