from . import app_spec, setup

setup()

from channels.routing import ProtocolTypeRouter  # noqa: E402
from django.core.asgi import get_asgi_application  # noqa: E402

from . import websocket  # noqa: E402

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": websocket.application(app_spec()),
})
