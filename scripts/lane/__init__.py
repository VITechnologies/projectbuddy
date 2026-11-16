"""Ubuntu lane executor (VITechnologies/projectbuddy#185, designed in #166).

Modules:
- ``common``: lane instance names, VM UUID targets, and Vagrant's machine ID path.
- ``teardown``: the REQ-102 allow-list, the per-UUID host lock, teardown by UUID, and the
  ``projectbuddy/lane-teardown@1`` receipt and its verifier.
- ``cli``: the command line behind ``scripts/Invoke-Lane.py``.

Host side only, POSIX hosts, Python 3.11 or later, standard library only. The error classes
are the lane box's: ``LaneBoxError`` is a refusal or failure, reported with a named rule.
"""
