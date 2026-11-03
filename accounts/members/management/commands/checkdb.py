"""PRAGMA integrity_check before the service starts: a damaged database stops it, naming the
newest backup to restore."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Check accounts.sqlite3 (PRAGMA integrity_check); fails with the newest backup's name."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA integrity_check")
            result = cursor.fetchone()[0]
        if result != "ok":
            backups = sorted(settings.BACKUP_DIR.glob("accounts-*.sqlite3"))
            raise CommandError("%s failed its integrity check (%s); the newest backup is %s"
                               % (settings.DATABASES["default"]["NAME"], result, backups[-1] if backups else "none"))
        self.stdout.write("ok")
