"""A dated copy of the database and the email pepper in FORZA_ACCOUNTS_BACKUPS now, as nightly
makes (7 kept). A second copy on the same day replaces the first.

    docker exec forza-accounts python manage.py backup"""

from django.core.management.base import BaseCommand

from members.management.commands import nightly


class Command(BaseCommand):
    help = "Back up the database and the email pepper now."

    def handle(self, *args, **options):
        self.stdout.write("backup %s" % nightly.backup())
