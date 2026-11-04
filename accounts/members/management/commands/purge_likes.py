"""Deletes the likes of photos and designs that are no longer in the game server's files, once
they have been missing for LIKES_PURGE_GRACE_DAYS (members.moderation.purge_missing_likes).
nightly runs it every day; this runs it on demand.

    docker exec forza-accounts python manage.py purge_likes [--dry-run] [--grace-days N]"""

from django.core.management.base import BaseCommand, CommandError

from members import game, moderation


class Command(BaseCommand):
    help = "Delete the likes of items missing from the game server's files for the grace period."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
        parser.add_argument("--grace-days", type=int, default=None,
                            help="days an item must have been missing (default LIKES_PURGE_GRACE_DAYS)")

    def handle(self, *args, dry_run=False, grace_days=None, **options):
        if not game.available():
            raise CommandError("FORZA_GAME_DATA is not set: there is nothing to compare the likes with")
        if grace_days is not None and grace_days < 0:
            raise CommandError("--grace-days cannot be negative")
        report = moderation.purge_missing_likes(grace_days=grace_days, dry_run=dry_run)
        self.stdout.write(summary(report, dry_run))


def summary(report, dry_run=False):
    parts = []
    for kind, r in report.items():
        if r["skipped"]:
            parts.append("%ss: not judged (the file is missing, damaged or empty)" % kind)
        else:
            parts.append("%ss: %d missing, %d like(s) of %d item(s) %s" % (
                kind, r["missing"], r["likes"], r["purged"], "would go" if dry_run else "deleted"))
    return "; ".join(parts) or "nothing to do"
