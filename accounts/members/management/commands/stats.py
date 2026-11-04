"""The service's numbers (accounts, links, likes), for the operator.

    docker exec forza-accounts python manage.py stats [--json]"""

import json

from django.core.management.base import BaseCommand

from members import moderation


class Command(BaseCommand):
    help = "Accounts, links and likes in numbers."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        numbers = moderation.stats()
        if options["json"]:
            self.stdout.write(json.dumps(numbers))
        else:
            width = max(map(len, numbers))
            for name, value in numbers.items():
                self.stdout.write("%s  %s" % (name.replace("_", " ").ljust(width), value))
