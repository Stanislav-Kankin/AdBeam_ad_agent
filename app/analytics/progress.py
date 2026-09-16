from contextvars import ContextVar

# Each manual request owns a dictionary inherited by its child tasks.
progress_state: ContextVar[dict | None] = ContextVar("progress_state", default=None)


def stage(text):
    state = progress_state.get()
    if state is not None:
        state["stage"] = text
