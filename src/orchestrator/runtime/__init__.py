"""Host bridge: the stream socket server (live events from skills running in the sandbox)."""

__all__ = ["StreamSocketServer"]


def __getattr__(name: str):
    if name == "StreamSocketServer":
        from orchestrator.runtime.stream_server import StreamSocketServer

        return StreamSocketServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
