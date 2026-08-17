"""``python -m air_platform`` / the ``air-platform`` console script.

Reads host, port and worker count from settings rather than from argv so that the
same configuration source drives a container, a compose file and a laptop. Anything
uvicorn-specific that is not in settings belongs on the command line instead —
``make run`` uses ``uvicorn`` directly for exactly that reason (``--reload``).
"""

from __future__ import annotations

import uvicorn

from air_platform.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "air_platform.main:app",
        host=settings.app.host,
        port=settings.app.port,
        # `workers` only takes effect for a string app target, which is why the app
        # is named rather than imported here.
        workers=settings.app.workers,
        # Access logging is ours (api/middleware.AccessLogMiddleware), and uvicorn's
        # would be a second, differently-shaped line for every request.
        access_log=False,
        timeout_graceful_shutdown=int(settings.app.shutdown_grace_seconds),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
