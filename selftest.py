import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, "/home/n0p/src/camdalf-gui")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import camdalf

app = QApplication(sys.argv)
w = camdalf.MainWindow()
w.resize(1180, 720)
w.show()


def shot():
    w.grab().save("/tmp/camdalf_ui.png")
    print("fps:", round(w.view.fps, 1), "| frame:", w.view._img.size() if w.view._img else None)
    print("info:", w.lbl_info.text())
    print("presets:", [(w.preset_cb.itemText(i), w.preset_cb.itemData(i)) for i in range(w.preset_cb.count())])
    app.quit()


QTimer.singleShot(9000, shot)
app.exec()
