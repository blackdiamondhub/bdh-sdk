from . import app_spec, setup

setup()

from django.core.asgi import get_asgi_application  # noqa: E402

try:
    from channels.routing import ProtocolTypeRouter
except ImportError:  # an image without the runtime extra's channels: HTTP only
    ProtocolTypeRouter = None

if ProtocolTypeRouter is None:
    application = get_asgi_application()
else:
    from . import websocket

    application = ProtocolTypeRouter({
        "http": get_asgi_application(),
        "websocket": websocket.application(app_spec()),
    })