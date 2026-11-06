"""Photo of the Month from the command line (members.potm), as the admin's review does it:

    docker exec forza-accounts python manage.py potm standings [2026-11] [--json]
    docker exec forza-accounts python manage.py potm voters 2026-11 PHOTOID
    docker exec forza-accounts python manage.py potm disqualify 2026-11 PHOTOID --note "why"
    docker exec forza-accounts python manage.py potm requalify 2026-11 PHOTOID
    docker exec forza-accounts python manage.py potm confirm 2026-11 PHOTOID --votes N [--note ...]
    docker exec forza-accounts python manage.py potm withdraw 2026-11

confirm takes the vote count the standings showed: when they changed since, it refuses, so the
operator confirms what they reviewed."""

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from members import game, potm

BY = "the operator (command line)"


class Command(BaseCommand):
    help = "Photo of the Month: standings, voters, disqualify, requalify, confirm, withdraw."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="step", required=True)
        p = sub.add_parser("standings")
        p.add_argument("month", nargs="?")
        p.add_argument("--json", action="store_true")
        p = sub.add_parser("voters")
        p.add_argument("month")
        p.add_argument("photo")
        p = sub.add_parser("disqualify")
        p.add_argument("month")
        p.add_argument("photo")
        p.add_argument("--note", required=True)
        p = sub.add_parser("requalify")
        p.add_argument("month")
        p.add_argument("photo")
        p = sub.add_parser("confirm")
        p.add_argument("month")
        p.add_argument("photo")
        p.add_argument("--votes", type=int, required=True, help="the leader's votes in the standings you checked")
        p.add_argument("--note", default="")
        p = sub.add_parser("withdraw")
        p.add_argument("month")

    def handle(self, *args, step, **o):
        if not potm.running():
            raise CommandError("Photo of the Month is off: set FORZA_POTM_FROM")
        if not game.available():
            raise CommandError("FORZA_GAME_DATA is not set")
        month = o.get("month") or potm.current()
        if not potm.contest_month(month):
            raise CommandError("%s is not a contest month (%s to %s)" % (month, settings.POTM_FROM, potm.current()))
        try:
            if step == "standings":
                self.standings(month, o["json"])
            elif step == "voters":
                for v in potm.voters(month, o["photo"]):
                    self.stdout.write("%s  %-9s  %-10s  account %s (%s days then)  likes that month %d" % (
                        v["at"].strftime("%Y-%m-%d %H:%M:%S"), "vote" if v["vote"] else "not a vote", v["key"],
                        v["account"] or "-", "-" if v["account_days"] is None else v["account_days"],
                        v["likes_this_month"]) + ("" if v["vote"] else "  (%s)" % v["why"]))
            elif step == "disqualify":
                potm.disqualify(month, o["photo"], o["note"], BY)
                self.stdout.write("%s is out of %s" % (o["photo"], month))
            elif step == "requalify":
                potm.requalify(month, o["photo"], BY)
                self.stdout.write("%s is back in %s" % (o["photo"], month))
            elif step == "confirm":
                w = potm.confirm(month, o["photo"], BY, expect_votes=o["votes"], note=o["note"])
                self.stdout.write("%s is the Photo of the Month %s (%d votes)" % (w.photo, month, w.votes))
            elif step == "withdraw":
                potm.withdraw(month, BY)
                self.stdout.write("the winner of %s is withdrawn" % month)
        except potm.PotmError as e:
            raise CommandError(str(e)) from None

    def standings(self, month, as_json):
        s = potm.standings(month, fresh=True)
        if as_json:
            self.stdout.write(json.dumps(potm.standings_doc(month), indent=1))
            return
        self.stdout.write("%s: %s, %d entries; the month %s" % (
            month, potm.state_of(s, potm.MonthWinner.objects.filter(month=month).first()), len(s.entries),
            "is over" if s.closed else "ends %s UTC" % s.end.strftime("%Y-%m-%d %H:%M")))
        for n, e in enumerate(s.entries[:settings.POTM_TOP], 1):
            self.stdout.write("%2d. %s  %3d votes  reached %s  %d likes that month%s" % (
                n, e.id, e.votes, e.reached.strftime("%Y-%m-%d %H:%M:%S") if e.reached else "-", e.likes,
                "".join("; %d: %s" % (k, why) for why, k in sorted(e.not_counted.items()))))
