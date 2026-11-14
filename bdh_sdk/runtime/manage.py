"""``python -m bdh_sdk.runtime.manage <command>``: Django management in an app container."""
import sys


def main(argv=None):
    from django.core.management import execute_from_command_line

    from . import setup

    setup()
    execute_from_command_line(["manage.py", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    main()
