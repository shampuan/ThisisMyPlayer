# ThisisMyPlayer
Lightweight, elegant and simple media player powered by mpv


ThisisMyPlayer is a bloat-free, resource-friendly media player built with
 Python 3 and PyQt6, utilizing the powerful mpv core library directly without
 relying on the standalone mpv player interface.
 .
 Features include:
  - Direct embedded libmpv playback with X11/XWayland hardware acceleration.
  - Automatic dynamic audio normalization filter (lavfi dynaudnorm).
  - Eye-friendly custom subtitle styling and automated encoding fixes.
  - Minimalist playlist support and smart auto-hiding controls in fullscreen.
  - Native multi-language support (i18n via gettext).
  - MKV support added; you can now select from multiple audio tracks and subtitles.

<img width="86" height="86" alt="ThisisMyPlayer" src="https://github.com/user-attachments/assets/40162640-b0ea-4b88-b56e-740244f1f809" />


The new features:

- "Jump to specific time" feature added.

- Frame-by-frame navigation added (using period and comma keys); useful for not missing key scenes or subtitles.

- Subtitle synchronization adjustment added using Shift and arrow keys: Shift+Left to advance, Shift+Right to delay, and Shift+Down to reset.

- Screenshot capture and auto-save functionality added (S key).

- "Always on top" toggle added.

- Automatically disables the screensaver and power-saving mode while watching a movie.

- Video properties can be viewed via the 'I' key or the menu.

- Minor correction in the "About" menu (added a note that it uses mpv).

- Logo updated.

<img width="860" height="614" alt="Ekran görüntüsü_2026-09-13_14-09-23" src="https://github.com/user-attachments/assets/f696921e-7248-483c-8531-024d551b01c2" />

<img width="860" height="614" alt="Ekran görüntüsü_2026-09-13_14-08-53" src="https://github.com/user-attachments/assets/46aac990-4232-4c28-9354-e48f9b18f865" />
