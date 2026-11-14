"""Move an app's database rows between the hub and the app's own container.

An app that used to run inside the hub keeps its rows in the hub's database.
When it moves to a container they need to land in the container's SQLite::

    # in the hub (its usual DJANGO_SETTINGS_MODULE)
    python -m bdh_sdk.appdata export cameras -o cameras.json
    # in the app's container
    python -m bdh_sdk.runtime.manage import_app_data /data/cameras.json

The file is a Django fixture (the ``json`` serializer, so ``BinaryField`` is
base64 and ``loaddata`` can read it too). Only models of the one app label
are exported, and :func:`import_rows` refuses rows of any other app.

Needs Django; nothing here imports it until a function is called.
"""
import argparse
import os
import sys

__all__ = ["export_rows", "import_rows", "main"]


def _models(app_label):
    from django.apps import apps

    return [m for m in apps.get_app_config(app_label).get_models()
            if not m._meta.proxy and m._meta.managed]


def export_rows(app_label, stream, using="default"):
    """Write every row of ``app_label``'s models to ``stream``; return the count."""
    from django.core import serializers

    count = 0

    def rows():
        nonlocal count
        for model in _models(app_label):
            for obj in model._default_manager.using(using).order_by("pk").iterator():
                count += 1
                yield obj

    serializers.serialize("json", rows(), stream=stream, indent=None)
    return count


def import_rows(app_label, stream, using="default", replace=False):
    """Load rows exported by :func:`export_rows` into ``app_label``; return the count.

    ``replace`` deletes the app's existing rows first. Everything happens in
    one transaction, and database sequences are reset afterwards so new rows
    don't collide with imported primary keys.
    """
    from django.core import serializers
    from django.core.management.color import no_style
    from django.db import connections, transaction

    models = _models(app_label)
    count = 0
    with transaction.atomic(using=using):
        if replace:
            for model in reversed(models):
                model._default_manager.using(using).all().delete()
        for obj in serializers.deserialize("json", stream, using=using):
            label = obj.object._meta.app_label
            if label != app_label:
                raise ValueError(f"row of app {label!r} in a fixture for {app_label!r}")
            obj.save(using=using)
            count += 1
        connection = connections[using]
        sql = connection.ops.sequence_reset_sql(no_style(), models)
        if sql:
            with connection.cursor() as cursor:
                for statement in sql:
                    cursor.execute(statement)
    return count


def default_app_label():
    """The Django app label of the app this container serves (``BDH_APP_ID``)."""
    from django.apps import apps

    from .runtime import app_spec

    module = app_spec().app
    for config in apps.get_app_configs():
        if config.name == module:
            return config.label
    return module.rsplit(".", 1)[-1]


def _setup():
    if os.environ.get("DJANGO_SETTINGS_MODULE"):
        import django

        django.setup()
    else:
        from .runtime import setup

        setup()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m bdh_sdk.appdata",
                                     description="Export or import an app's database rows.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    exp = sub.add_parser("export", help="write the app's rows as a JSON fixture")
    exp.add_argument("app_label", nargs="?")
    exp.add_argument("-o", "--output", help="file to write (default: stdout)")
    imp = sub.add_parser("import", help="load a JSON fixture into the app's tables")
    imp.add_argument("file")
    imp.add_argument("app_label", nargs="?")
    imp.add_argument("--replace", action="store_true", help="delete the app's rows first")
    args = parser.parse_args(argv)

    _setup()
    label = args.app_label or default_app_label()
    if args.cmd == "export":
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                n = export_rows(label, fh)
        else:
            n = export_rows(label, sys.stdout)
            sys.stdout.write("\n")
        print(f"exported {n} rows of {label}", file=sys.stderr)
    else:
        from django.core.management import call_command

        call_command("migrate", interactive=False, verbosity=0)
        with open(args.file, encoding="utf-8") as fh:
            n = import_rows(label, fh, replace=args.replace)
        print(f"imported {n} rows into {label}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
