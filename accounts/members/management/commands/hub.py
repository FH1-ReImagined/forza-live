"""The mods and texture packs hub from the command line (members.hub), as the admin does it:

    docker exec forza-accounts python manage.py hub list [--state review]
    docker exec forza-accounts python manage.py hub collect
    docker exec forza-accounts python manage.py hub approve <id>
    docker exec forza-accounts python manage.py hub hide <id>
    docker exec forza-accounts python manage.py hub unhide <id>
    docker exec forza-accounts python manage.py hub refuse <id> [--why "..."]
    docker exec forza-accounts python manage.py hub take-down <id>
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from members import hub
from members.models import HubItem

BY = "the operator (command line)"


class Command(BaseCommand):
    help = "The hub: list, collect, approve, hide, unhide, refuse, take-down."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="step", required=True)
        p = sub.add_parser("list")
        p.add_argument("--state", choices=[s for s, _ in HubItem.STATES])
        sub.add_parser("collect")
        for name in ("approve", "hide", "unhide", "refuse", "take-down"):
            p = sub.add_parser(name)
            p.add_argument("id")
            if name == "refuse":
                p.add_argument("--why", default="a maintainer did not approve it")

    def handle(self, *args, step, **o):
        if not settings.HUB:
            raise CommandError("the hub is off: set FORZA_HUB=1")
        if step == "list":
            rows = HubItem.objects.all()
            if o.get("state"):
                rows = rows.filter(state=o["state"])
            for h in rows:
                self.stdout.write("%s  %-4s  %-8s  %10d bytes  %s  %s" % (h.id, h.kind, h.state, h.file_size or h.upload_size,
                                                                          h.sha256 or "-", h.title))
            return
        if step == "collect":
            self.stdout.write("%d result(s) collected" % hub.collect(force=True))
            return
        item = HubItem.objects.filter(pk=o["id"]).first()
        if item is None:
            raise CommandError("no item %s" % o["id"])
        try:
            if step == "approve":
                hub.approve(item, BY)
            elif step == "hide":
                if not hub.hide(item, "hidden by a maintainer"):
                    raise CommandError("%s is %s, not live" % (item.id, item.state))
            elif step == "unhide":
                hub.unhide(item, BY)
            elif step == "refuse":
                hub.refuse(item, BY, o["why"])
            else:
                hub.take_down(item, "a maintainer")
        except hub.HubError as e:
            raise CommandError(str(e)) from None
        self.stdout.write("%s: %s" % (item.id, HubItem.objects.get(pk=item.pk).state))
