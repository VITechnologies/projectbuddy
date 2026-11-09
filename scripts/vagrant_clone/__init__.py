"""Vagrant clone verifier (VITechnologies/projectbuddy#185, designed in #166 section 8).

Modules:
- ``profile``: the ``projectbuddy/ubuntu-vagrant-clone-profile@1`` data that says what a clone must show.
- ``core``: the neutral checks: the VM's UUID and name, ``running``, one loopback SSH forward, Vagrant's
  machine ID, and the REQ-103 check for ``actions.runner.*`` units.
- ``live``: the check against a running clone, from host-key enrollment to the verified receipt.
- ``receipt``: the ``projectbuddy/ubuntu-vagrant-clone@2`` receipt and its verifier, which also verifies
  ``@1`` receipts against a pinned profile.

Host side only, POSIX hosts, Python 3.11 or later, standard library only. Errors are the lane box's:
a ``LaneBoxError`` is a refusal or failure, reported with what failed.
"""
