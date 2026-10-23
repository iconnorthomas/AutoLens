# AutoLens NukeX — node-centric entry points.
#
# Keep this file cheap: it runs at every Nuke launch. The controller module
# (autolens.node) and the workspace are imported only when a command is used
# or a script with an older AutoLens node is opened.
import time

_MENU_START = time.perf_counter()

import nuke
import autolens.startup as autolens_startup


def _create_autolens_node():
    import autolens.node as autolens_node
    return autolens_node.create_autolens_node()


# Add as a normal node from the Nodes toolbar/menu.
_nodes = nuke.menu("Nodes")
_al_nodes = _nodes.addMenu("AutoLens")
_al_nodes.addCommand(
    "AutoLens",
    _create_autolens_node,
    icon=""
)

# Also expose from the main Nuke menu.
_main = nuke.menu("Nuke").addMenu("AutoLens")
_main.addCommand("Add AutoLens Node", _create_autolens_node, shortcut="Ctrl+Alt+L")


# Keep serialized AutoLens controller nodes current when an existing script is
# opened with a newer plugin version. Nodes that are already current are
# skipped without importing autolens.node.
try:
    nuke.addOnScriptLoad(autolens_startup.upgrade_on_script_load)
except Exception:
    pass

try:
    autolens_startup.upgrade_on_script_load()
except Exception:
    pass

autolens_startup.record("menu.py", time.perf_counter() - _MENU_START)
