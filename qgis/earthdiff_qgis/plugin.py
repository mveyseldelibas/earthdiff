"""Menu entry, toolbar button and the dock panel."""
from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon

try:  # Qt 6 (QGIS 4)
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt 5 (QGIS 3)
    from qgis.PyQt.QtWidgets import QAction

from .i18n import t


class EarthdiffPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None

    def initGui(self):
        icon = QIcon(str(Path(__file__).with_name("icon.png")))
        self.action = QAction(icon, t("menu"), self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self._toggle)  # only the user's click; visibilityChanged just syncs the check mark
        self.iface.addRasterToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu("earthdiff", self.action)

    def _toggle(self, on: bool):
        if on:
            if self.dock is None:
                from .dock import EarthdiffDock
                self.dock = EarthdiffDock(self.iface, self.iface.mainWindow())
                self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
                self.dock.visibilityChanged.connect(lambda vis: self.action.setChecked(vis))
            self.dock.show()
            self.dock.raise_()
        elif self.dock is not None:
            self.dock.close()  # same as the panel's own close button: clears the drawing, offers to remove the results

    def unload(self):
        if self.dock is not None:
            self.dock.cleanup()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginRasterMenu("earthdiff", self.action)
            self.iface.removeRasterToolBarIcon(self.action)
            self.action = None
