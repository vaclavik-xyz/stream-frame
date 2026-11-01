"""The daemon used to be called vrstreamd — this keeps old units, scripts and habit working.

It re-points the name at the real module object rather than copying names out of it: tools and tests set
attributes on `Handler` and on module globals, and those have to be the same objects either way. Running
this file still starts the daemon. Delete it when nothing calls vrstreamd any more.
"""

import sys

import streamframe

sys.modules[__name__] = streamframe

if __name__ == "__main__":
    sys.exit(streamframe.main())
