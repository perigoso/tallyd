"""Detect whether a call is ongoing by watching microphone and camera capture.

Platform-independent by construction: it observes the OS capture state rather
than any conferencing vendor's API, so Teams, Meet, Zoom, Discord and anything
else look identical.
"""

__version__ = "0.1.0"
