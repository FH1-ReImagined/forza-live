from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from members import game
from members.models import AuditEvent, GameLink, SignIn, User

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


@admin.register(GameLink)
class GameLinkAdmin(admin.ModelAdmin):
    """A frozen link (its XUID is bound to another key now): check with the player, then unfreeze
    it, which pins the key the XUID is bound to now."""
    list_display = ("user", "gamertag", "state", "linked_at", "frozen_at")
    list_filter = ("state",)
    search_fields = ("user__username", "gamertag", "xuid")
    readonly_fields = ("xuid", "pub", "user", "linked_at", "moved_at", "frozen_at")
    actions = ["unfreeze_and_repin"]

    @admin.action(description="Unfreeze, pinning the key the XUID is bound to now")
    def unfreeze_and_repin(self, request, queryset):
        done = 0
        for link in queryset:
            pub = game.bound_pub(int(link.xuid, 16)) if game.available() else None
            if pub is None:
                continue
            link.pub, link.state, link.frozen_at = pub, GameLink.ACTIVE, None
            link.save(update_fields=["pub", "state", "frozen_at"])
            AuditEvent.objects.create(user=link.user, event="the operator unfroze the game link")
            done += 1
        self.message_user(request, "%d link(s) unfrozen" % done)
