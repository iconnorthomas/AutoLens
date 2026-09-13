# AutoLens NukeX v0.10.3 — node-centric entry points.
import nuke
import autolens.node as autolens_node

# Add as a normal node from the Nodes toolbar/menu.
_nodes = nuke.menu("Nodes")
_al_nodes = _nodes.addMenu("AutoLens")
_al_nodes.addCommand(
    "AutoLens",
    autolens_node.create_autolens_node,
    icon=""
)

# Also expose from the main Nuke menu.
_main = nuke.menu("Nuke").addMenu("AutoLens")
_main.addCommand("Add AutoLens Node", autolens_node.create_autolens_node, shortcut="Ctrl+Alt+L")


# Keep serialized AutoLens controller nodes current when an existing script is
# opened with a newer plugin version.
try:
    nuke.addOnScriptLoad(autolens_node.upgrade_existing_nodes)
except Exception:
    pass

try:
    autolens_node.upgrade_existing_nodes()
except Exception:
    pass
