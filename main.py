"""Entry point — kept for `python main.py`."""
import multiprocessing
import os
import sys


def _entry():
    print(f"[entry] PID={os.getpid()} parent={os.getppid()}", flush=True)
    from bot import main
    main()


if __name__ == "__main__":
    # On Windows, python-telegram-bot may spawn a child process via
    # multiprocessing (spawn start method). freeze_support ensures the
    # child doesn't re-run the bot's main loop — which would otherwise
    # cause a Conflict on getUpdates.
    multiprocessing.freeze_support()
    _entry()
