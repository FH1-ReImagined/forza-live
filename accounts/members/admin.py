"""The operator's admin (community accounts design, section 5, "Admin tools"). It answers only
FORZA_ADMIN_ALLOW (members.middleware). Every action that changes an account goes through
members.moderation, which writes the audit event the player sees and a log line.

- Accounts: find one by its id (K7Q2-XM4P, with or without the dash), the linked gamertag or the
  XUID; its game link and audit events on its page; suspend, lift a suspension, sign out
  everywhere, void its key's likes. Deleting an account deletes its key's likes too.
- Game links: unlink (with an audit event), void the key's likes, unfreeze.
- Likes: read-only, filtered by kind, item, key (XUID) and account, each with the most liked or
  most liking first; void the selected likes, or every like of their keys.
- Audit events and sign-ins: read-only lists.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db.models import Count, OuterRef, Subquery
from django.urls import reverse
from django.utils.html import format_html

from members import game, items, moderation
from members.models import AuditEvent, GameLink, Like, MissingItem, SignIn, User
from members.moderation import account_key

TOP = 20  # choices shown in each like filter


def pretty(username):
    return username[:4] + "-" + username[4:] if username else "-"


def likes_url(**query):
    return reverse("admin:members_like_changelist") + "?" + "&".join("%s=%s" % kv for kv in query.items())


class ReadOnly:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ---- Accounts ----

class GameLinkInline(ReadOnly, admin.StackedInline):
    model = GameLink
    can_delete = False
    fields = readonly_fields = ("gamertag", "state", "xuid", "pub", "linked_at", "moved_at", "frozen_at")


class AuditInline(ReadOnly, admin.TabularInline):
    model = AuditEvent
    can_delete = False
    fields = readonly_fields = ("at", "event")
    ordering = ("-at", "-id")


class LinkedFilter(admin.SimpleListFilter):
    title, parameter_name = "game link", "linked"

    def lookups(self, request, model_admin):
        return [("yes", "linked"), ("no", "not linked")]

    def queryset(self, request, queryset):
        if self.value() in ("yes", "no"):
            return queryset.filter(game_link__isnull=self.value() == "no")
        return queryset


@admin.register(User)
class AccountAdmin(UserAdmin):
    list_display = ("account", "date_joined", "last_login", "is_active", "gamertag", "likes", "is_staff")
    list_filter = ("is_active", LinkedFilter, "is_staff")
    search_fields = ("username", "game_link__gamertag", "game_link__xuid")
    ordering = ("-date_joined",)
    fieldsets = (
        (None, {"fields": ("username", "is_active", "last_login", "date_joined")}),
        ("Staff", {"classes": ("collapse",), "fields": ("password", "is_staff", "is_superuser", "groups",
                                                        "user_permissions")}),
    )
    inlines = (GameLinkInline, AuditInline)
    actions = ("suspend", "unsuspend", "sign_out", "void_likes")

    def get_readonly_fields(self, request, obj=None):
        # Suspending goes through the action: it signs the account out and writes the audit event.
        return ("username", "is_active", "last_login", "date_joined") if obj else ()

    def get_inlines(self, request, obj):
        return self.inlines if obj else ()

    def get_queryset(self, request):
        likes = Like.objects.filter(xuid=OuterRef("game_link__xuid")).values("xuid").annotate(n=Count("*")).values("n")
        return super().get_queryset(request).select_related("game_link").annotate(like_count=Subquery(likes))

    def get_search_results(self, request, queryset, search_term):
        return super().get_search_results(request, queryset, account_key(search_term) or search_term)

    @admin.display(description="account", ordering="username")
    def account(self, user):
        return user.public_id

    @admin.display(description="gamertag", ordering="game_link__gamertag")
    def gamertag(self, user):
        link = getattr(user, "game_link", None)
        return "%s (%s)" % (link.gamertag, link.state) if link else "-"

    @admin.display(description="likes given", ordering="like_count")
    def likes(self, user):
        if not user.like_count:
            return 0
        return format_html('<a href="{}">{}</a>', likes_url(account=user.username), user.like_count)

    @admin.action(description="Suspend: sign out everywhere, no sign-in, its likes not counted", permissions=["change"])
    def suspend(self, request, queryset):
        done = sum(moderation.suspend(u) for u in queryset)
        self.message_user(request, "%d account(s) suspended (staff accounts are left alone)" % done)

    @admin.action(description="Lift the suspension", permissions=["change"])
    def unsuspend(self, request, queryset):
        done = sum(moderation.unsuspend(u) for u in queryset)
        self.message_user(request, "%d suspension(s) lifted" % done)

    @admin.action(description="Sign out everywhere", permissions=["change"])
    def sign_out(self, request, queryset):
        ended = [moderation.sign_out(u) for u in queryset]
        self.message_user(request, "%d session(s) ended (staff accounts are left alone)"
                          % sum(n for n in ended if n))

    @admin.action(description="Void the likes of the linked key", permissions=["void"])
    def void_likes(self, request, queryset):
        voided = [moderation.void_account(u) for u in queryset]
        self.message_user(request, "%d like(s) voided" % sum(n for n in voided if n))

    def has_void_permission(self, request):
        return request.user.has_perm("members.delete_like")


# ---- Game links ----

@admin.register(GameLink)
class GameLinkAdmin(admin.ModelAdmin):
    """A frozen link (its XUID is bound to another key now): check with the player, then unfreeze
    it, which pins the key the XUID is bound to now."""
    list_display = ("user", "gamertag", "state", "linked_at", "frozen_at", "likes")
    list_filter = ("state",)
    search_fields = ("user__username", "gamertag", "xuid")
    readonly_fields = ("xuid", "pub", "user", "state", "linked_at", "moved_at", "frozen_at")
    actions = ["unlink", "void_likes", "unfreeze_and_repin"]

    def has_add_permission(self, request):
        return False

    def get_actions(self, request):
        found = super().get_actions(request)
        found.pop("delete_selected", None)  # "Unlink" instead: it writes the audit event
        return found

    def get_queryset(self, request):
        likes = Like.objects.filter(xuid=OuterRef("xuid")).values("xuid").annotate(n=Count("*")).values("n")
        return super().get_queryset(request).select_related("user").annotate(like_count=Subquery(likes))

    def get_search_results(self, request, queryset, search_term):
        return super().get_search_results(request, queryset, account_key(search_term) or search_term)

    @admin.display(description="likes given", ordering="like_count")
    def likes(self, link):
        if not link.like_count:
            return 0
        return format_html('<a href="{}">{}</a>', likes_url(key=link.xuid), link.like_count)

    def delete_model(self, request, obj):
        moderation.unlink(obj)

    def delete_queryset(self, request, queryset):
        for link in queryset:
            moderation.unlink(link)

    @admin.action(description="Unlink (the key keeps its likes, which stop counting)", permissions=["delete"])
    def unlink(self, request, queryset):
        n = 0
        for link in queryset:
            moderation.unlink(link)
            n += 1
        self.message_user(request, "%d link(s) removed" % n)

    @admin.action(description="Void the key's likes", permissions=["void"])
    def void_likes(self, request, queryset):
        n = sum(moderation.void_key(link.xuid) for link in queryset)
        self.message_user(request, "%d like(s) voided" % n)

    def has_void_permission(self, request):
        return request.user.has_perm("members.delete_like")

    @admin.action(description="Unfreeze, pinning the key the XUID is bound to now", permissions=["change"])
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


# ---- Likes ----

class TopFilter(admin.SimpleListFilter):
    """A filter whose choices are the TOP values with the most likes; any value works in the
    address (?item=..., ?key=..., ?account=...), as the links in the lists use."""
    field = None

    def lookups(self, request, model_admin):
        top = (Like.objects.values(self.field).annotate(n=Count("id")).order_by("-n", self.field)[:TOP])
        return [(row[self.field], "%s (%d)" % (row[self.field], row["n"])) for row in top]

    def queryset(self, request, queryset):
        return queryset.filter(**{self.field: self.value()}) if self.value() else queryset


class ItemFilter(TopFilter):
    title, parameter_name, field = "item (most liked)", "item", "item"


class KeyFilter(TopFilter):
    title, parameter_name, field = "key (most likes given)", "key", "xuid"


class AccountFilter(admin.SimpleListFilter):
    title, parameter_name = "account (most likes given)", "account"

    def lookups(self, request, model_admin):
        top = Like.objects.filter(xuid__in=GameLink.objects.values("xuid")).values("xuid") \
            .annotate(n=Count("id")).order_by("-n", "xuid")[:TOP]
        owners = moderation.accounts_of(row["xuid"] for row in top)
        return [(owners[row["xuid"]].username, "%s (%d)" % (owners[row["xuid"]].public_id, row["n"]))
                for row in top if row["xuid"] in owners]

    def queryset(self, request, queryset):
        if not self.value():
            return queryset
        link = GameLink.objects.filter(user__username=account_key(self.value()) or self.value()).first()
        return queryset.filter(xuid=link.xuid) if link else queryset.none()


@admin.register(Like)
class LikeAdmin(ReadOnly, admin.ModelAdmin):
    """Who liked what: for the operator only, never shown anywhere else."""
    list_display = ("at", "kind", "item_link", "key_link", "account_link", "item_state")
    list_filter = ("kind", ItemFilter, KeyFilter, AccountFilter)
    search_fields = ("item", "xuid")
    ordering = ("-at", "-id")
    actions = ["void_selected", "void_keys"]
    list_per_page = 100

    def has_delete_permission(self, request, obj=None):
        return False  # voiding instead: it writes the audit events

    def has_void_permission(self, request):
        return request.user.has_perm("members.delete_like")

    def get_queryset(self, request):
        account = GameLink.objects.filter(xuid=OuterRef("xuid")).values("user__username")[:1]
        return super().get_queryset(request).annotate(account=Subquery(account))

    @admin.display(description="item", ordering="item")
    def item_link(self, like):
        return format_html('<a href="{}">{}</a>', likes_url(item=like.item), like.item)

    @admin.display(description="key (XUID)", ordering="xuid")
    def key_link(self, like):
        return format_html('<a href="{}">{}</a>', likes_url(key=like.xuid), like.xuid)

    @admin.display(description="account", ordering="account")
    def account_link(self, like):
        if not like.account:
            return "not linked"
        return format_html('<a href="{}">{}</a>', likes_url(account=like.account), pretty(like.account))

    @admin.display(description="the item now")
    def item_state(self, like):
        if not game.available():
            return "-"
        found = items.find(like.kind, like.item)
        if found is None:
            return "gone"
        return "%s, %s" % (found.state, "public" if found.public else "not public")

    @admin.action(description="Void the selected likes", permissions=["void"])
    def void_selected(self, request, queryset):
        n = moderation.void_likes(Like.objects.filter(pk__in=list(queryset.values_list("pk", flat=True))))
        self.message_user(request, "%d like(s) voided" % n)

    @admin.action(description="Void every like of the selected likes' keys", permissions=["void"])
    def void_keys(self, request, queryset):
        keys = set(queryset.values_list("xuid", flat=True))
        n = moderation.void_likes(Like.objects.filter(xuid__in=keys))
        self.message_user(request, "%d like(s) of %d key(s) voided" % (n, len(keys)))


@admin.register(MissingItem)
class MissingItemAdmin(ReadOnly, admin.ModelAdmin):
    """Liked items the nightly purge no longer finds in the game server's files: their likes go
    LIKES_PURGE_GRACE_DAYS after `since`."""
    list_display = ("kind", "item", "since")
    list_filter = ("kind",)
    search_fields = ("item",)


# ---- Audit and sign-ins ----

@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnly, admin.ModelAdmin):
    list_display = ("at", "user", "event")
    list_filter = ("event",)
    search_fields = ("user__username", "event")


@admin.register(SignIn)
class SignInAdmin(ReadOnly, admin.ModelAdmin):
    list_display = ("created", "user")
    search_fields = ("user__username",)
    exclude = ("session_key",)
