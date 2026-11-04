"""Voids likes, for abuse (community accounts design, section 5): every like of a key (its XUID)
or of an account (its linked key's, since likes belong to the key). The account linked to the key
gets an audit event, and the log a line with the account id only.

    docker exec forza-accounts python manage.py void_likes --account K7Q2-XM4P
    docker exec forza-accounts python manage.py void_likes --key 0009000000000001 [--dry-run]

The key can like again afterwards: to stop it, suspend the account (the admin) or ban the XUID
on the game server."""

import re

from django.core.management.base import BaseCommand, CommandError

from members import moderation
from members.moderation import account_key
from members.models import GameLink, Like, User


class Command(BaseCommand):
    help = "Void every like of a key (--key XUID) or of an account's linked key (--account K7Q2-XM4P)."

    def add_arguments(self, parser):
        who = parser.add_mutually_exclusive_group(required=True)
        who.add_argument("--key", help="the key's XUID, 16 hex digits")
        who.add_argument("--account", help="the account id, such as K7Q2-XM4P")
        parser.add_argument("--dry-run", action="store_true", help="count them, delete nothing")

    def handle(self, *args, key=None, account=None, dry_run=False, **options):
        if account is not None:
            user = User.objects.filter(username=account_key(account) or "-").first()
            if user is None:
                raise CommandError("no account %s" % account)
            link = GameLink.objects.filter(user=user).first()
            if link is None:
                raise CommandError("account %s is not linked to a game: it has no likes" % user.public_id)
            key = link.xuid
        else:
            if not re.fullmatch(r"[0-9A-Fa-f]{16}", key):
                raise CommandError("a key is its XUID, 16 hex digits")
            key = key.upper()
        if dry_run:
            self.stdout.write("%d like(s) would be voided" % Like.objects.filter(xuid=key).count())
            return
        self.stdout.write("%d like(s) voided" % moderation.void_key(key))
