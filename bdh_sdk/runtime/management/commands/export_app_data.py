from django.core.management.base import BaseCommand

from bdh_sdk import appdata


class Command(BaseCommand):
    help = "Write this app's database rows as a JSON fixture (see bdh_sdk.appdata)."

    def add_arguments(self, parser):
        parser.add_argument("app_label", nargs="?")
        parser.add_argument("-o", "--output", help="file to write (default: stdout)")

    def handle(self, *args, app_label=None, output=None, **options):
        label = app_label or appdata.default_app_label()
        if output:
            with open(output, "w", encoding="utf-8") as fh:
                n = appdata.export_rows(label, fh, using=options.get("database", "default"))
        else:
            n = appdata.export_rows(label, self.stdout)
            self.stdout.write("")
        self.stderr.write(f"exported {n} rows of {label}")
