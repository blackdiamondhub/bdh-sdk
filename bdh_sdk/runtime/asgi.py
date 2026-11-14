from . import setup

setup()

from django.core.asgi import get_asgi_application  # noqa: E402

application = get_asgi_application()
