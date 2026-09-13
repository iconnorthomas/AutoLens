# AutoLens NukeX bootstrap.
import os
import nuke

_ROOT = os.path.dirname(os.path.abspath(__file__))
nuke.pluginAddPath(os.path.join(_ROOT, "python"))
nuke.pluginAddPath(os.path.join(_ROOT, "icons"))
