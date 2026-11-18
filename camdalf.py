#!/usr/bin/env python3
"""Camdalf 2 - GUI de control para la camara PTZ (Novatek NT98566, ONVIF).

    /usr/bin/python3 camdalf.py

Solo necesita PyQt6 (paquete del sistema) y ffmpeg. No usa OpenCV ni python-mpv.

- Video: ffmpeg lee el RTSP y escupe RGB crudo por tuberia; un QThread lo
  convierte en QImage. Se puede elegir stream principal (4K->1280x720) o
  secundario (800x448).
- PTZ: todas las ordenes SOAP pasan por un unico hilo serializado, porque este
  firmware se atasca si recibe movimientos solapados.
- El punto amarillo del centro es un overlay de Qt (la camara no permite un
  segundo OSD de texto).
"""

import os
import sys
import base64
import hashlib
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPainter, QColor, QPen, QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QGridLayout,
    QVBoxLayout, QHBoxLayout, QSlider, QComboBox, QCheckBox, QStatusBar,
    QGroupBox, QSizePolicy, QMessageBox, QFileDialog,
)

# --------------------------------------------------------------------------- camara

HOST = os.environ.get("CAM_HOST", "10.205.52.34")
PORT = os.environ.get("CAM_PORT", "8899")
USER = os.environ.get("CAM_USER", "admin")
PASS = os.environ.get("CAM_PASS", "admin")
ONVIF_URL = f"http://{HOST}:{PORT}/onvif/device_service"

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}
NSDECL = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())

RTSP = {
    "main": f"rtsp://{USER}:{PASS}@{HOST}:554/user={USER}&password={PASS}&channel=1&stream=0.sdp?real_stream",
    "sub": f"rtsp://{USER}:{PASS}@{HOST}:554/user={USER}&password={PASS}&channel=1&stream=1.sdp?real_stream",
}
SNAP_URL = f"http://{HOST}/webcapture.jpg?command=snap&channel=0&user={USER}&password={PASS}"


def _envelope(body: str) -> bytes:
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + PASS.encode()).digest()
    ).decode()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope {NSDECL}
 xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
 <s:Header><wsse:Security><wsse:UsernameToken>
  <wsse:Username>{USER}</wsse:Username>
  <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>
  <wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>
  <wsse:Created>{created}</wsse:Created>
 </wsse:UsernameToken></wsse:Security></s:Header>
 <s:Body>{body}</s:Body>
</s:Envelope>""".encode()


class Camera:
    """Cliente ONVIF minimo. No es thread-safe: lo usa un unico hilo."""

    def __init__(self):
        self.profile_token = None

    def call(self, body: str, timeout: float = 8.0):
        req = urllib.request.Request(
            ONVIF_URL, data=_envelope(body), headers={"Content-Type": "application/soap+xml"}
        )
        try:
            return ET.fromstring(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")
            import re
            vals = re.findall(r"<[^>]*(?:Text)[^>]*>([^<]+)", txt)
            raise RuntimeError(vals[0] if vals else f"HTTP {e.code}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"sin conexion: {e.reason}")

    def token(self) -> str:
        if self.profile_token is None:
            root = self.call("<trt:GetProfiles/>")
            p = root.find(".//trt:Profiles", NS)
            if p is None:
                raise RuntimeError("la camara no devuelve perfiles")
            self.profile_token = p.get("token")
        return self.profile_token

    # --- movimiento
    def continuous(self, x=0.0, y=0.0, z=0.0):
        self.call(
            f"<tptz:ContinuousMove><tptz:ProfileToken>{self.token()}</tptz:ProfileToken>"
            f'<tptz:Velocity><tt:PanTilt x="{x}" y="{y}"/><tt:Zoom x="{z}"/></tptz:Velocity>'
            "</tptz:ContinuousMove>"
        )

    def stop(self):
        # este firmware no implementa tptz:Stop: se frena con velocidad 0
        self.continuous(0, 0, 0)

    def absolute(self, x=0.0, y=0.0, z=0.0):
        self.call(
            f"<tptz:AbsoluteMove><tptz:ProfileToken>{self.token()}</tptz:ProfileToken>"
            f'<tptz:Position><tt:PanTilt x="{x}" y="{y}"/><tt:Zoom x="{z}"/></tptz:Position>'
            "</tptz:AbsoluteMove>", timeout=15,
        )

    def home(self):
        self.call(f"<tptz:GotoHomePosition><tptz:ProfileToken>{self.token()}</tptz:ProfileToken></tptz:GotoHomePosition>", timeout=15)

    # --- presets
    def presets(self):
        root = self.call(f"<tptz:GetPresets><tptz:ProfileToken>{self.token()}</tptz:ProfileToken></tptz:GetPresets>")
        return [(p.get("token"), p.findtext("tt:Name", "?", NS))
                for p in root.findall(".//tptz:Preset", NS)]

    def goto(self, n):
        self.call(f"<tptz:GotoPreset><tptz:ProfileToken>{self.token()}</tptz:ProfileToken>"
                  f"<tptz:PresetToken>{n}</tptz:PresetToken></tptz:GotoPreset>", timeout=15)

    def save(self, n):
        self.call(f"<tptz:SetPreset><tptz:ProfileToken>{self.token()}</tptz:ProfileToken>"
                  f"<tptz:PresetToken>{n}</tptz:PresetToken>"
                  f"<tptz:PresetName>preset{n}</tptz:PresetName></tptz:SetPreset>")

    def delete(self, n):
        self.call(f"<tptz:RemovePreset><tptz:ProfileToken>{self.token()}</tptz:ProfileToken>"
                  f"<tptz:PresetToken>{n}</tptz:PresetToken></tptz:RemovePreset>")

    # --- info
    def info(self):
        r = self.call("<tds:GetDeviceInformation/>")
        return {k: r.findtext(".//tds:" + k, "?", NS)
                for k in ("Manufacturer", "Model", "FirmwareVersion")}

    def encoders(self):
        root = self.call("<trt:GetVideoEncoderConfigurations/>")
        out = []
        for c in root.findall(".//trt:Configurations", NS):
            out.append((c.get("token"), c.findtext("tt:Encoding", "?", NS),
                        c.findtext(".//tt:Width", "?", NS), c.findtext(".//tt:Height", "?", NS),
                        c.findtext(".//tt:FrameRateLimit", "?", NS)))
        return out


class CameraWorker(QThread):
    """Serializa todas las ordenes a la camara: una cola, una hebra."""

    done = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)

    def __init__(self, cam: Camera, parent=None):
        super().__init__(parent)
        self.cam = cam
        self.q = queue.Queue()
        self.running = True

    def submit(self, tag: str, fn, *a, **kw):
        self.q.put((tag, fn, a, kw))

    def run(self):
        while self.running:
            try:
                tag, fn, a, kw = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.done.emit(tag, fn(*a, **kw))
            except Exception as e:
                self.failed.emit(tag, str(e))

    def quit(self):
        self.running = False
        self.wait(2000)


# ---------------------------------------------------------------------------- video

class VideoThread(QThread):
    """ffmpeg -> RGB crudo -> QImage."""

    frame = pyqtSignal(QImage)
    error = pyqtSignal(str)

    def __init__(self, url: str, width: int, height: int, parent=None):
        super().__init__(parent)
        self.url = url
        self.w, self.h = width, height
        self.proc = None
        self.running = True

    def run(self):
        size = self.w * self.h * 3
        while self.running:
            cmd = [
                "ffmpeg", "-nostdin", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-fflags", "nobuffer", "-flags", "low_delay",
                "-probesize", "500000", "-analyzeduration", "1000000",
                "-i", self.url, "-an",
                "-vf", f"scale={self.w}:{self.h}",
                "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
            ]
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, bufsize=size)
            except Exception as e:
                self.error.emit(f"no se pudo lanzar ffmpeg: {e}")
                return
            buf = bytearray()
            while self.running:
                chunk = self.proc.stdout.read(size - len(buf))
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) < size:
                    continue
                img = QImage(bytes(buf), self.w, self.h, self.w * 3, QImage.Format.Format_RGB888)
                self.frame.emit(img.copy())
                del buf[:size]
            try:
                self.proc.terminate()
            except Exception:
                pass
            if self.running:
                self.error.emit("se ha cortado el video, reconectando...")
                time.sleep(1.5)

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass


class VideoView(QLabel):
    """Muestra el frame, con punto central y cruz opcionales."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(480, 270)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:#0b0b0b;")
        self._img = None
        self.dot = True
        self.dot_px = 9
        self.cross = False
        self.fps = 0.0
        self._t0 = time.time()
        self._n = 0

    def set_image(self, img: QImage):
        self._img = img
        self._n += 1
        dt = time.time() - self._t0
        if dt >= 1.0:
            self.fps = self._n / dt
            self._n = 0
            self._t0 = time.time()
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0b0b"))
        if self._img is None:
            p.setPen(QColor("#666"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "sin video")
            return
        # escalar manteniendo aspecto
        scaled = self._img.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        p.drawImage(x, y, scaled)
        cx, cy = x + scaled.width() // 2, y + scaled.height() // 2
        if self.dot:
            r = self.dot_px / 2
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setBrush(QColor(255, 220, 0, 230))
            p.setPen(QPen(QColor(0, 0, 0, 200), 1))
            p.drawEllipse(int(cx - r), int(cy - r), int(self.dot_px), int(self.dot_px))
        if self.cross:
            p.setPen(QPen(QColor(255, 220, 0, 110), 1))
            p.drawLine(x, cy, x + scaled.width(), cy)
            p.drawLine(cx, y, cx, y + scaled.height())

    def grab_frame(self):
        return self._img


# ----------------------------------------------------------------------------- UI

DIRS = {
    "↖": (-1, 1), "↑": (0, 1), "↗": (1, 1),
    "←": (-1, 0), "■": (None, None), "→": (1, 0),
    "↙": (-1, -1), "↓": (0, -1), "↘": (1, -1),
}


class HoldButton(QPushButton):
    """Boton que emite start al pulsar y stop al soltar (movimiento continuo)."""

    def __init__(self, text, on_start, on_stop):
        super().__init__(text)
        self._start = on_start
        self._stop = on_stop
        self.setFixedSize(52, 52)
        self.setAutoRepeat(False)
        self.pressed.connect(self._start)
        self.released.connect(self._stop)

    def keyPressEvent(self, e):
        if e.isAutoRepeat():
            return
        self._start()
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.isAutoRepeat():
            return
        self._stop()
        super().keyReleaseEvent(e)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Camdalf 2 - control PTZ")
        self.resize(1180, 720)
        self.cam = Camera()
        self.worker = CameraWorker(self.cam)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.start()
        self.video = None
        self.pressing = 0
        self.speed = 0.4
        self._build_ui()
        self._start_video()
        self.worker.submit("info", self.cam.info)
        self.worker.submit("presets", self.cam.presets)
        self.worker.submit("encoders", self.cam.encoders)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)

    # --- construccion
    def _build_ui(self):
        central = QWidget()
        root = QHBoxLayout(central)

        self.view = VideoView()
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.view, 1)

        side = QVBoxLayout()
        side.setSpacing(10)

        # pad de direcciones
        pad_box = QGroupBox("Movimiento")
        grid = QGridLayout(pad_box)
        grid.setSpacing(4)
        for i, (txt, (dx, dy)) in enumerate(DIRS.items()):
            if dx is None:
                b = QPushButton(txt)
                b.setFixedSize(52, 52)
                b.clicked.connect(lambda: self.worker.submit("stop", self.cam.stop))
            else:
                b = HoldButton(txt, lambda dx=dx, dy=dy: self._start_move(dx, dy),
                               self._stop_move)
            grid.addWidget(b, i // 3, i % 3)
        side.addWidget(pad_box)

        # zoom
        zbox = QGroupBox("Zoom")
        zl = QHBoxLayout(zbox)
        self.bzin = HoldButton("+", lambda: self._start_zoom(1), self._stop_move)
        self.bzout = HoldButton("−", lambda: self._start_zoom(-1), self._stop_move)
        zl.addWidget(self.bzin)
        zl.addWidget(self.bzout)
        side.addWidget(zbox)

        # velocidad y paso
        vbox = QGroupBox("Ajustes")
        vl = QGridLayout(vbox)
        vl.addWidget(QLabel("Velocidad"), 0, 0)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(5, 100)
        self.speed_slider.setValue(40)
        self.speed_slider.valueChanged.connect(lambda v: setattr(self, "speed", v / 100))
        vl.addWidget(self.speed_slider, 0, 1)
        self.dot_cb = QCheckBox("Punto central")
        self.dot_cb.setChecked(True)
        self.dot_cb.toggled.connect(self._toggle_dot)
        vl.addWidget(self.dot_cb, 1, 0, 1, 2)
        self.cross_cb = QCheckBox("Cruz de referencia")
        self.cross_cb.toggled.connect(lambda v: setattr(self.view, "cross", v) or self.view.update())
        vl.addWidget(self.cross_cb, 2, 0, 1, 2)
        vl.addWidget(QLabel("Stream"), 3, 0)
        self.stream_cb = QComboBox()
        self.stream_cb.addItems(["secundario 800x448", "principal 1280x720"])
        self.stream_cb.currentIndexChanged.connect(self._restart_video)
        vl.addWidget(self.stream_cb, 3, 1)
        side.addWidget(vbox)

        # presets
        pbox = QGroupBox("Presets")
        pl = QGridLayout(pbox)
        self.preset_cb = QComboBox()
        self.preset_cb.setMinimumWidth(120)
        pl.addWidget(self.preset_cb, 0, 0, 1, 2)
        b_go = QPushButton("Ir")
        b_go.clicked.connect(self._goto_preset)
        b_set = QPushButton("Guardar")
        b_set.clicked.connect(self._save_preset)
        b_del = QPushButton("Borrar")
        b_del.clicked.connect(self._del_preset)
        pl.addWidget(b_go, 1, 0)
        pl.addWidget(b_set, 1, 1)
        pl.addWidget(b_del, 2, 0)
        b_home = QPushButton("Home")
        b_home.clicked.connect(lambda: self.worker.submit("home", self.cam.home))
        pl.addWidget(b_home, 2, 1)
        side.addWidget(pbox)

        # acciones
        abox = QGroupBox("Camara")
        al = QVBoxLayout(abox)
        b_center = QPushButton("Centrar (vista general)")
        b_center.clicked.connect(lambda: self.worker.submit("center", self.cam.absolute, 0, 0, 0))
        b_snap = QPushButton("Guardar foto...")
        b_snap.clicked.connect(self._snapshot)
        b_refresh = QPushButton("Refrescar presets")
        b_refresh.clicked.connect(lambda: self.worker.submit("presets", self.cam.presets))
        al.addWidget(b_center)
        al.addWidget(b_snap)
        al.addWidget(b_refresh)
        side.addWidget(abox)
        side.addStretch(1)

        root.addLayout(side)
        self.setCentralWidget(central)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.lbl_info = QLabel("conectando...")
        self.lbl_fps = QLabel("")
        self.status.addWidget(self.lbl_info, 1)
        self.status.addPermanentWidget(self.lbl_fps)

        # atajos de teclado
        for key, (dx, dy) in {
            Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, 1), Qt.Key.Key_Down: (0, -1),
        }.items():
            a = QAction(self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(lambda _=False, dx=dx, dy=dy: self._tap(dx, dy))
            self.addAction(a)

    # --- video
    def _start_video(self):
        main = self.stream_cb.currentIndex() == 1
        url = RTSP["main" if main else "sub"]
        w, h = (1280, 720) if main else (800, 448)
        self.video = VideoThread(url, w, h)
        self.video.frame.connect(self.view.set_image)
        self.video.error.connect(lambda m: self.status.showMessage(m, 5000))
        self.video.start()

    def _restart_video(self):
        if self.video:
            self.video.stop()
            self.video.wait(2000)
        self._start_video()

    # --- acciones de camara
    def _start_move(self, dx, dy):
        self.pressing += 1
        v = self.speed
        self.worker.submit("move", self.cam.continuous, dx * v, dy * v, 0)

    def _start_zoom(self, sign):
        self.pressing += 1
        self.worker.submit("zoom", self.cam.continuous, 0, 0, sign * self.speed)

    def _stop_move(self):
        self.pressing = max(0, self.pressing - 1)
        if self.pressing == 0:
            self.worker.submit("stop", self.cam.stop)

    def _tap(self, dx, dy):
        """Paso corto con teclado (no continuo)."""
        def go():
            self.cam.continuous(dx * self.speed, dy * self.speed, 0)
            time.sleep(0.25)
            self.cam.stop()
        self.worker.submit("tap", go)

    def _toggle_dot(self, v):
        self.view.dot = v
        self.view.update()

    def _snapshot(self):
        path, _ = QFileDialog.getSaveFileName(self, "Guardar captura", "camdalf.jpg", "JPEG (*.jpg)")
        if path:
            try:
                with urllib.request.urlopen(SNAP_URL, timeout=10) as r:
                    data = r.read()
                with open(path, "wb") as f:
                    f.write(data)
                self.status.showMessage(f"guardado {path}", 4000)
            except Exception as e:
                QMessageBox.warning(self, "Captura", str(e))

    def _goto_preset(self):
        n = self.preset_cb.currentData()
        if n is not None:
            self.worker.submit("goto", self.cam.goto, n)

    def _save_preset(self):
        n = self.preset_cb.currentData()
        if n is None:
            n = "1"
        self.worker.submit("save", self.cam.save, n)

    def _del_preset(self):
        n = self.preset_cb.currentData()
        if n is not None:
            self.worker.submit("delete", self.cam.delete, n)
            QTimer.singleShot(800, lambda: self.worker.submit("presets", self.cam.presets))

    # --- respuestas del worker
    def _done(self, tag, data):
        if tag == "info":
            self.lbl_info.setText(f"{data.get('Model','?')}  ·  {data.get('FirmwareVersion','?')}  ·  {HOST}")
        elif tag == "presets":
            self.preset_cb.clear()
            for tok, name in data:
                self.preset_cb.addItem(f"{tok} - {name}", tok)
            if not data:
                self.preset_cb.addItem("(sin presets)", None)
        elif tag == "encoders":
            txt = "  ".join(f"{t}:{w}x{h}@{f}" for t, e, w, h, f in data if e == "H264")
            self.status.showMessage(txt, 8000)
        elif tag in ("save", "delete"):
            self.worker.submit("presets", self.cam.presets)

    def _failed(self, tag, msg):
        self.status.showMessage(f"error en {tag}: {msg}", 6000)

    def _tick(self):
        self.lbl_fps.setText(f"{self.view.fps:.0f} fps")

    def closeEvent(self, ev):
        if self.video:
            self.video.stop()
            self.video.wait(1500)
        try:
            self.cam.stop()
        except Exception:
            pass
        self.worker.quit()
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Camdalf 2")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
