from django.core.management.base import BaseCommand

from bdh_sdk import appdata


class Command(BaseCommand):
    help = "Load a JSON fixture written by export_app_data into this app's tables."

    def add_arguments(self, parser):
        parser.add_argument("file")
        parser.add_argument("app_label", nargs="?")
        parser.add_argument("--replace", action="store_true",
                            help="delete the app's existing rows first")

    def handle(self, *args, file, app_label=None, replace=False, **options):
        label = app_label or appdata.default_app_label()
        with open(file, encoding="utf-8") as fh:
            n = appdata.import_rows(label, fh, replace=replace)
        self.stderr.write(f"imported {n} rows into {label}")
