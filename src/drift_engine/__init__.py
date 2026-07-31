"""Clever sandbox Events API data drift engine.

Makes small, realistic, recurring changes to a sandbox district's CSV roster
stack on a fixed weekday cadence, then re-syncs over SFTP so Clever emits a
predictable stream of Events API activity for application partners to build
against.

Sandbox districts only. See ``safety.py``.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
