#!/usr/bin/env python3
"""The accounts service's Django commands: migrate, test, checkdb, nightly, createsuperuser..."""
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fh_accounts.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
