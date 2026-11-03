"""The daily upkeep: expired sessions, audit events past 90 days, accounts unused for 30 days that
are not linked to a game, email accounts whose code was never used after a day, link requests and
launcher nonces that are over, then a dated backup of the database and the email pepper (7
kept)."""

import datetime
import os
import shutil
import sqlite3

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from members.models import AuditEvent, LinkRequest, SignedNonce, SignIn, User

AUDIT_DAYS = 90
IDLE_DAYS = 30
UNUSED_CODE_DAYS = 1
BACKUPS = 7


def purge(now=None):
    now = now or timezone.now()
    Session.objects.filter(expire_date__lt=now).delete()
    SignIn.objects.exclude(session_key__in=Session.objects.values("session_key")).delete()
    AuditEvent.objects.filter(at__lt=now - datetime.timedelta(days=AUDIT_DAYS)).delete()
    idle = now - datetime.timedelta(days=IDLE_DAYS)
    unused = now - datetime.timedelta(days=UNUSED_CODE_DAYS)
    LinkRequest.objects.filter(expires__lt=now - datetime.timedelta(hours=1)).delete()
    SignedNonce.objects.filter(until__lt=now).delete()
    stale = User.objects.filter(is_staff=False, is_superuser=False, game_link__isnull=True).filter(
        Q(last_login__lt=idle) | Q(last_login__isnull=True, date_joined__lt=unused))
    return stale.delete()[1].get("members.User", 0)


def backup(now=None, source=None):
    """source: a sqlite3 connection (default: the service's database)."""
    now = now or timezone.now()
    folder = settings.BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / ("accounts-%s.sqlite3" % now.strftime("%Y-%m-%d"))
    tmp = dest.with_suffix(".tmp")
    if source is None:
        connection.ensure_connection()
        source = connection.connection
    target = sqlite3.connect(tmp)
    try:
        source.backup(target)
    finally:
        target.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    if settings.EMAIL_PEPPER_FILE.exists():
        shutil.copyfile(settings.EMAIL_PEPPER_FILE, folder / settings.EMAIL_PEPPER_FILE.name)
        os.chmod(folder / settings.EMAIL_PEPPER_FILE.name, 0o600)
    for old in sorted(folder.glob("accounts-*.sqlite3"))[:-BACKUPS]:
        old.unlink()
    return dest


class Command(BaseCommand):
    help = "Purge expired sessions, old audit events, unused accounts and old link requests, then back up."

    def handle(self, *args, **options):
        gone = purge()
        dest = backup()
        self.stdout.write("deleted %d unused accounts; backup %s" % (gone, dest))
