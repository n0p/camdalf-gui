# Camdalf 2 — GUI de control PTZ

Aplicación PyQt6 para la cámara PTZ de laboratorio (Novatek NT98566, ONVIF 21.06).

    /usr/bin/python3 camdalf.py

Requisitos: `python-pyqt6` (paquete del sistema) y `ffmpeg`. Nada más: no usa
OpenCV, ni python-mpv, ni GStreamer.

## Qué hace

- **Vídeo** por RTSP. ffmpeg escupe RGB crudo por tubería y un `QThread` lo
  convierte en `QImage`. Stream secundario 800x448 (por defecto) o principal a
  1280x720. Se reconecta solo si se corta.
- **Movimiento** con 8 direcciones en pulsación mantenida (al soltar manda
  velocidad 0, que es como se frena este firmware, porque `tptz:Stop` no está
  implementado) y botón central de parada.
- **Zoom** con `+`/`−`, también mantenidos.
- **Velocidad** ajustable de 0,05 a 1.
- **Punto central** amarillo (y cruz opcional) dibujados con `QPainter` sobre el
  vídeo: la cámara solo admite un OSD de texto, así que el punto va en el cliente.
- **Presets**: listar, ir, guardar y borrar.
- **Home** (posición de reposo del firmware) y **Centrar** (`AbsoluteMove 0,0,0`,
  que es la vista general; el home de fábrica apunta a la mesa).
- **Guardar foto** por HTTP desde `/webcapture.jpg`.
- Teclado: flechas mueven en pasos cortos.

Todas las órdenes SOAP pasan por un único hilo con cola: este firmware se atasca
si recibe movimientos solapados.

## Variables de entorno

    CAM_HOST=10.205.52.34
    CAM_PORT=8899
    CAM_USER=admin
    CAM_PASS=admin

## Comprobación sin pantalla

    QT_QPA_PLATFORM=offscreen /usr/bin/python3 selftest.py

Abre la ventana, arranca el vídeo y guarda una captura en `/tmp/camdalf_ui.png`.
