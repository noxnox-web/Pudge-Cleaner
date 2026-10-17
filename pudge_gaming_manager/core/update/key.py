"""The public half of the key that signs Pudge Cleaner's updates.

Written by `python scripts/update_signing.py keygen`; the private half never enters this
repository. 32 bytes as 64 hex characters. Empty until that command has been run -- a build
with no key shows that an update exists but refuses to install one.
"""

UPDATE_PUBLIC_KEY = "dac398fb74f90c25de00da844672fa51f2b4158e455aa4128c1ade5eb767150a"
