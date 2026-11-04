"""Puts a backup back (community accounts design, section 3.5): the database becomes the
backup's, through SQLite's backup API, which is safe while the service runs. The current
database is kept first as pre-restore-<time>.sqlite3 in the backups folder, and the email pepper
comes back from the backups folder when it is missing.

    docker exec forza-accounts python manage.py restore 2026-11-03 --yes
    docker exec forza-accounts python manage.py restore /data/backups/accounts-2026-11-03.sqlite3 --yes

Then restart the container: its start applies the migrations (for a backup from an older
version) and every worker starts afresh. Sessions made since the backup are gone, and its
accounts, links and likes are as they were that night."""

import os
import re
import shutil
import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone


def backup_path(which):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", which):
        return settings.BACKUP_DIR / ("accounts-%s.sqlite3" % which)
    return Path(which)


def check(path):
    """The backup is a whole SQLite database of this service, else CommandError."""
    if not path.is_file():
        raise CommandError("no backup %s" % path)
    try:
        source = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            result = source.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            source.close()
    except sqlite3.DatabaseError as e:
        raise CommandError("%s is not a database: %s" % (path, e)) from None
    if result != "ok":
        raise CommandError("%s failed its integrity check (%s)" % (path, result))
    if not {"django_migrations", "members_user"} <= tables:
        raise CommandError("%s is not a backup of this service" % path)


def restore(path, target=None, now=None):
    """Copies the backup at `path` over `target` (a sqlite3 connection; default the service's
    database), keeping the current one first. The safety copy's path."""
    now = now or timezone.now()
    check(path)
    if target is None:
        connection.ensure_connection()
        target = connection.connection
    folder = settings.BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    keep = folder / ("pre-restore-%s.sqlite3" % now.strftime("%Y%m%d-%H%M%S"))
    copy = sqlite3.connect(keep)
    try:
        target.backup(copy)
    finally:
        copy.close()
    os.chmod(keep, 0o600)
    source = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        source.backup(target)
    finally:
        source.close()
    pepper = folder / settings.EMAIL_PEPPER_FILE.name
    if not settings.EMAIL_PEPPER_FILE.exists() and pepper.exists():
        shutil.copyfile(pepper, settings.EMAIL_PEPPER_FILE)
        os.chmod(settings.EMAIL_PEPPER_FILE, 0o600)
    return keep


class Command(BaseCommand):
    help = "Restore the database from a backup (a date such as 2026-11-03, or a file)."

    def add_arguments(self, parser):
        parser.add_argument("backup", help="YYYY-MM-DD (backups/accounts-<date>.sqlite3) or a file")
        parser.add_argument("--yes", action="store_true", help="really replace the database")

    def handle(self, *args, backup, yes=False, **options):
        path = backup_path(backup)
        if not yes:
            check(path)
            raise CommandError("%s is a good backup. Run again with --yes to replace the database with it" % path)
        keep = restore(path)
        self.stdout.write("restored %s (the database before it: %s). Restart the container now." % (path, keep))
