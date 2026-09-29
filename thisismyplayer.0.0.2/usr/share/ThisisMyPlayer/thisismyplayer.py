#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import locale
import html
import tempfile
import time
import gettext
import json
import ctypes
import subprocess

# --- KRİTİK SİSTEM & MPV UYUMLULUK AYARLARI ---
# Hem Qt'yi hem MPV'yi kesin olarak X11/XWayland katmanına kilitler (KDE Wayland uyumluluğu)
os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ.pop("WAYLAND_DISPLAY", None)
os.environ["GDK_BACKEND"] = "x11"
os.environ['LC_NUMERIC'] = 'C'
try:
    locale.setlocale(locale.LC_ALL, 'C')
except locale.Error:
    try:
        locale.setlocale(locale.LC_NUMERIC, 'C')
    except locale.Error:
        pass
# ---------------------------------------------

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QSizePolicy, QSlider, QStyle,
    QFileDialog, QComboBox, QMessageBox, QDialog, QLineEdit
)
from PyQt6.QtGui import QIcon, QFont, QColor, QKeySequence, QAction, QActionGroup, QPixmap, QDesktopServices
from PyQt6.QtCore import Qt, QSize, pyqtSignal, QTimer, QPoint, QEvent, QUrl, QStandardPaths

# MPV Kütüphane Kontrolü
try:
    import mpv
except ImportError:
    print("HATA: 'python-mpv' kütüphanesi bulunamadı. Lütfen 'pip install python-mpv' kurun.")
    sys.exit(1)


class ClickableSlider(QSlider):
    """Tıklanan noktaya anında atlayan ve sürüklemeyi destekleyen gelişmiş çubuk."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Klavyeyi asla çalmasın

    def _value_from_pos(self, pos):
        handle_length = 12
        if self.orientation() == Qt.Orientation.Horizontal:
            span = self.width() - handle_length
            coord = pos.x() - handle_length // 2
        else:
            span = self.height() - handle_length
            coord = pos.y() - handle_length // 2
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), coord, max(1, span))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            value = self._value_from_pos(event.position().toPoint())
            self.setSliderDown(True)
            self.setValue(value)
            self.sliderMoved.emit(value)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            value = self._value_from_pos(event.position().toPoint())
            self.setValue(value)
            self.sliderMoved.emit(value)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(False)
            self.sliderReleased.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class AspectRatioWidget(QWidget):
    """Videonun 16:9 geometrisini koruyan ve MPV yüzeyini barındıran pencere alanı."""
    geometry_changed = pyqtSignal()

    def __init__(self, ratio=16/9, parent=None):
        super().__init__(parent)
        self.ratio = ratio
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        
        self.content_layout = QHBoxLayout(self)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        
        self.inner_content = QWidget(self)
        self.inner_content.setMouseTracking(True)
        self.content_layout.addWidget(self.inner_content)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.apply_geometry()

    def apply_geometry(self):
        w = self.width()
        h = self.height()
        if w == 0 or h == 0:
            return

        target_w = w
        target_h = int(target_w / self.ratio)

        if target_h > h:
            target_h = h
            target_w = int(target_h * self.ratio)

        x = (w - target_w) // 2
        y = (h - target_h) // 2
        self.inner_content.setGeometry(x, y, target_w, target_h)
        self.geometry_changed.emit()

    def mousePressEvent(self, event):
        # Yüzeye sol tıklandığında ana penceredeki tek-tık gecikmesini başlat
        if event.button() == Qt.MouseButton.LeftButton:
            top_level = self.window()
            if hasattr(top_level, '_single_click_requested'):
                top_level._single_click_requested.emit()
        super().mousePressEvent(event)


class ThisisMyPlayer(QMainWindow):
    # MPV Arka Plan Thread'lerinden Qt UI'a Güvenli Sinyal Köprüleri
    _fullscreen_toggle_requested = pyqtSignal()
    _volume_ui_update_requested = pyqtSignal(int)
    _time_ui_update_requested = pyqtSignal(int)
    _single_click_requested = pyqtSignal()
    _eof_reached_signal = pyqtSignal()

    AUDIO_EXTENSIONS = (
        '.mp3', '.flac', '.wav', '.ogg', '.oga', '.m4a', '.aac', 
        '.opus', '.wma', '.alac', '.ape', '.aiff', '.mid', '.midi'
    )
    VIDEO_EXTENSIONS = (
        '.mp4', '.mkv', '.avi', '.webm', '.flv', '.mov', '.wmv', 
        '.ts', '.m4v', '.3gp', '.vob', '.ogv'
    )
    MEDIA_EXTENSIONS = AUDIO_EXTENSIONS + VIDEO_EXTENSIONS

    def __init__(self):
        super().__init__()

        # Uygulama Dizinini Belirle (Sembolik linkler dahil dosyanın gerçek yanıbaşını baz alır)
        self.app_dir = os.path.dirname(os.path.realpath(__file__))

        # Ayar Dizini ve settings.json Yapılandırması (~/.config/ThisisMyPlayer/settings.json)
        self.config_dir = os.path.expanduser("~/.config/ThisisMyPlayer")
        self.config_file = os.path.join(self.config_dir, "settings.json")
        self.settings = self._load_settings()

        # Varsayılan dil: İngilizce ("en")
        self.current_locale = self.settings.get("language", "en")
        self._setup_translation()

        self.setWindowTitle("ThisisMyPlayer")
        self.resize(850, 580)

        # Pencere Çubuğu İkonu (Her zaman dosyanın yanıbaşındaki logodan yüklenir)
        self.logo_path = os.path.join(self.app_dir, "ThisisMyPlayer.png")
        if os.path.exists(self.logo_path):
            self.setWindowIcon(QIcon(self.logo_path))
        self.setMinimumSize(640, 360)
        self.setAcceptDrops(True)  # Drag & Drop aktif

        self._current_duration = 0
        self.current_playing_path = None
        self._last_played_path = None
        self._is_stopped = False
        self._last_mouse_pos = None
        self.playlist = []  # Oynatma listesi dosya yolları
        self.current_index = -1  # Şu an çalan videonun listedeki sırası
        self._last_activation_time = 0  # Pencerenin aktifleşme zamanı
        self._screensaver_cookie = None  # Ekran koruyucu / uyku modu kilit kimliği

        # 1. Video Yüzeyi
        self.video_container = AspectRatioWidget(ratio=16/9, parent=self)
        self.video_container.setStyleSheet("background-color: #000000;")
        self.mpv_area = self.video_container.inner_content
        self.mpv_area.setStyleSheet("background-color: #000000;")
        # MPV'nin pencereye kilitlenmesini sağlayan temiz X11 Native Window öznitelikleri
        self.mpv_area.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.mpv_area.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)

        # Siyah Ekranda Gösterilecek Logo Katmanı (MPV'ye engel olmaması için saydam fare olayları)
        self.logo_label = QLabel(self.video_container)
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.logo_label.setStyleSheet("background: transparent;")
        if os.path.exists(self.logo_path):
            pixmap = QPixmap(self.logo_path)
            self.logo_label.setPixmap(pixmap.scaled(86, 86, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.video_container.geometry_changed.connect(self._reposition_logo)

        # Sinyal Bağlantıları
        self._fullscreen_toggle_requested.connect(self._on_fullscreen_toggle_safe)
        self._volume_ui_update_requested.connect(self._apply_volume_ui)
        self._time_ui_update_requested.connect(self._apply_time_ui)
        self._single_click_requested.connect(self._on_video_single_click)
        self._eof_reached_signal.connect(self._on_eof_reached)

        # Kesintisiz çift tık / tek tık ayrım zamanlayıcısı
        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.timeout.connect(self._toggle_pause)

        # Sürükle-bırak sırasında kazara duraklatmayı önleyen 1.5 saniyelik kilit
        self._pause_locked = False
        self._pause_lock_timer = QTimer(self)
        self._pause_lock_timer.setSingleShot(True)
        self._pause_lock_timer.timeout.connect(self._unlock_pause)

        # 2. Önce Tüm Arayüzü ve Layout'u Kur (Hiyerarşi kilitlensin, reparenting bitsin)
        self._setup_ui()
        self._setup_menu()

        # Kayıtlı "Her Zaman Üstte" ayarını güvenli şekilde yükle
        if self.settings.get("always_on_top", False):
            self.always_on_top_action.blockSignals(True)
            self.always_on_top_action.setChecked(True)
            self.always_on_top_action.blockSignals(False)
            QTimer.singleShot(200, lambda: self._set_window_stay_on_top_x11(True))

        # 3. Layout tamamlandıktan sonra MPV Motorunu sabit WID ile Başlat
        wid_val = int(self.mpv_area.winId())
        self.mpv_player = mpv.MPV(
            vo='gpu',
            loop=False,
            keep_open=True,
            sub_auto='no',  # MPV'nin kirli altyazıyı otomatik seçmesini engeller
            input_default_bindings=True,
            input_doubleclick_time=300,  # Çift tıklama algılama toleransı (ms)
            osc=False,
            idle=True,
            volume=100,
            sub_color='#d2c18e',         # Ten/Bej altyazı rengi
            sub_border_color='#000000',  # Kontrast için siyah dış hat/kenarlık
            sub_border_size=1.8,         # Net okunabilirlik kenarlık kalınlığı
            af='lavfi=[dynaudnorm=f=500:g=31:m=7]',
            wid=wid_val
        )

        # 4. MPV Dinleyicilerini Başlat
        self.mpv_player.observe_property('pause', self._mpv_pause_changed)
        self.mpv_player.observe_property('volume', self._mpv_volume_changed)
        self.mpv_player.observe_property('mute', self._mpv_mute_changed)
        self.mpv_player.observe_property('video-params', self._on_video_params_changed)
        self.mpv_player.observe_property('fullscreen', self._on_mpv_dbl_click_fullscreen)
        self.mpv_player.observe_property('eof-reached', self._on_mpv_eof_reached)
        self.mpv_player.observe_property('time-pos', self._mpv_time_changed)
        self.mpv_player.observe_property('duration', self._mpv_duration_changed)
        self.mpv_player.on_key_press('mbtn_left')(lambda: self._single_click_requested.emit())

        # 5. Tam Ekran Otomatik Gizleme Zamanlayıcıları
        self.controls_hide_timer = QTimer(self)
        self.controls_hide_timer.setSingleShot(True)
        self.controls_hide_timer.setInterval(2000)  # 2 saniye
        self.controls_hide_timer.timeout.connect(self._hide_controls_fullscreen)

        self.hover_poll_timer = QTimer(self)
        self.hover_poll_timer.setInterval(150)
        self.hover_poll_timer.timeout.connect(self._check_mouse_movement)
        self.hover_poll_timer.start()

        # Komut satırından dosya verilmişse hemen başlat
        if len(sys.argv) > 1:
            arg_file = sys.argv[1]
            if os.path.exists(arg_file):
                QTimer.singleShot(100, lambda: self.play_file(arg_file))

    def _load_settings(self):
        """~/.config/ThisisMyPlayer/settings.json dosyasından ayarları okur."""
        default_settings = {"language": "en"}
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return default_settings
        return default_settings

    def _save_settings(self):
        """Ayarları ~/.config/ThisisMyPlayer/settings.json dosyasına yazar."""
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Ayar kaydetme hatası: {e}")

    def _setup_translation(self):
        """gettext motorunu başlatır ve _() fonksiyonunu kurar."""
        locale_dir = os.path.join(self.app_dir, "locales")
        try:
            translation = gettext.translation("thisismyplayer", localedir=locale_dir, languages=[self.current_locale])
            translation.install()
            self._ = translation.gettext
        except Exception:
            self._ = lambda s: s

    def _change_language(self, lang_code):
        """Dili değiştirir, settings.json'a kaydeder ve kullanıcıya yeniden başlatma uyarısı verir."""
        if self.current_locale == lang_code:
            return
        self.settings["language"] = lang_code
        self._save_settings()
        QMessageBox.information(
            self,
            self._("Dil Değiştirildi"),
            self._("Dil ayarının geçerli olması için uygulamanın yeniden başlatılması gerekmektedir.")
        )

    def _get_available_languages(self):
        """locales/ dizininde geçerli .mo dosyası olan dilleri dinamik olarak listeler."""
        # Yaygın dil kodlarının kullanıcı dostu adları
        lang_names = {
            "en": "English",
            "tr": "Türkçe",
            "de": "Deutsch",
            "fr": "Français",
            "es": "Español",
            "it": "Italiano",
            "ru": "Русский",
            "pt": "Português",
            "ja": "日本語",
            "zh": "中文",
            "ar": "العربية"
        }
        
        # İngilizce (varsayılan ana dil) her zaman listede bulunur
        found = {"en": "English"}
        locale_dir = os.path.join(self.app_dir, "locales")
        
        if os.path.exists(locale_dir):
            for code in os.listdir(locale_dir):
                mo_path = os.path.join(locale_dir, code, "LC_MESSAGES", "thisismyplayer.mo")
                # Eğer ilgili dilde derlenmiş .mo dosyası varsa listeye dahil et
                if os.path.isfile(mo_path):
                    found[code] = lang_names.get(code, code.upper())

        # İngilizceyi başa alıp diğer dilleri alfabetik sıralayalım
        sorted_list = [("en", found.pop("en"))]
        sorted_list.extend(sorted(found.items(), key=lambda x: x[1]))
        return sorted_list

    # -------------------------------------------------------------
    # UI KURULUMU
    # -------------------------------------------------------------
    def _setup_ui(self):
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)

        self.main_layout = QVBoxLayout(central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # Video ve Playlist'i Barındıran Orta Katman
        center_layout = QHBoxLayout()
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)

        # 1. Video Alanı
        center_layout.addWidget(self.video_container, 1)

        # 2. Oynatma Listesi Paneli (Varsayılan olarak gizli)
        from PyQt6.QtWidgets import QListWidget
        self.playlist_widget = QListWidget()
        self.playlist_widget.setFixedWidth(240)
        self.playlist_widget.setStyleSheet("""
            QListWidget::item { padding: 6px; }
        """)
        self.playlist_widget.itemDoubleClicked.connect(self._on_playlist_item_double_clicked)
        self.playlist_widget.hide()  # Başlangıçta gizli
        center_layout.addWidget(self.playlist_widget)

        self.main_layout.addLayout(center_layout, 1)

        # Alt Kontrol Paneli (Seekbar + Butonlar - Sistem Temasına Uyumlu)
        self.bottom_control_panel = QFrame()
        self.bottom_control_panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel_layout = QVBoxLayout(self.bottom_control_panel)
        panel_layout.setContentsMargins(10, 4, 10, 8)
        panel_layout.setSpacing(4)

        # 1. Satır: Seek Bar + Süre
        seek_row = QHBoxLayout()
        seek_row.setSpacing(10)

        self.seek_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 100)
        self.seek_slider.setValue(0)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        seek_row.addWidget(self.seek_slider, 1)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setFixedWidth(95)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        seek_row.addWidget(self.time_label)

        panel_layout.addLayout(seek_row)

        # 2. Satır: Butonlar, Ses ve Hız Kontrolleri
        controls_row = QHBoxLayout()
        controls_row.setSpacing(6)

        # Oynat / Duraklat
        self.play_pause_btn = QPushButton()
        self.play_pause_btn.setIcon(self._theme_icon("media-playback-start", QStyle.StandardPixmap.SP_MediaPlay))
        self.play_pause_btn.setIconSize(QSize(22, 22))
        self.play_pause_btn.setFixedSize(36, 36)
        self.play_pause_btn.clicked.connect(self._toggle_pause)
        controls_row.addWidget(self.play_pause_btn)

        # Durdur (Stop)
        self.stop_btn = QPushButton()
        self.stop_btn.setIcon(self._theme_icon("media-playback-stop", QStyle.StandardPixmap.SP_MediaStop))
        self.stop_btn.setIconSize(QSize(20, 20))
        self.stop_btn.setFixedSize(36, 36)
        self.stop_btn.setToolTip(self._("Durdur"))
        self.stop_btn.clicked.connect(self.stop_playback)
        controls_row.addWidget(self.stop_btn)

        # Önceki / Sonraki Video Butonları
        self.prev_btn = QPushButton()
        self.prev_btn.setIcon(self._theme_icon("media-skip-backward", QStyle.StandardPixmap.SP_MediaSkipBackward))
        self.prev_btn.setIconSize(QSize(20, 20))
        self.prev_btn.setFixedSize(36, 36)
        self.prev_btn.setToolTip(self._("Önceki Video"))
        self.prev_btn.clicked.connect(self.play_previous)
        controls_row.addWidget(self.prev_btn)

        self.next_btn = QPushButton()
        self.next_btn.setIcon(self._theme_icon("media-skip-forward", QStyle.StandardPixmap.SP_MediaSkipForward))
        self.next_btn.setIconSize(QSize(20, 20))
        self.next_btn.setFixedSize(36, 36)
        self.next_btn.setToolTip(self._("Sonraki Video"))
        self.next_btn.clicked.connect(self.play_next)
        controls_row.addWidget(self.next_btn)

        # Döngü Butonu (Basıldığında Basılı Kalan Toggle Buton)
        self.loop_btn = QPushButton()
        self.loop_btn.setCheckable(True)
        self.loop_btn.setIcon(self._theme_icon("media-playlist-repeat", QStyle.StandardPixmap.SP_BrowserReload))
        self.loop_btn.setIconSize(QSize(20, 20))
        self.loop_btn.setFixedSize(36, 36)
        self.loop_btn.setToolTip(self._("Döngü (Liste Bitince Başa Sar)"))
        self.loop_btn.toggled.connect(self._on_loop_toggled)
        controls_row.addWidget(self.loop_btn)

        # Kaynak Klasörü Aç Butonu
        self.open_folder_btn = QPushButton()
        self.open_folder_btn.setIcon(self._theme_icon("folder-open", QStyle.StandardPixmap.SP_DirIcon))
        self.open_folder_btn.setIconSize(QSize(20, 20))
        self.open_folder_btn.setFixedSize(36, 36)
        self.open_folder_btn.setToolTip(self._("Kaynak Klasörü Aç"))
        self.open_folder_btn.clicked.connect(self._open_current_media_folder)
        controls_row.addWidget(self.open_folder_btn)

        controls_row.addSpacing(10)

        # Ses Kontrolleri
        self.mute_btn = QPushButton()
        self.mute_btn.setIcon(self._theme_icon("audio-volume-high", QStyle.StandardPixmap.SP_MediaVolume))
        self.mute_btn.setIconSize(QSize(20, 20))
        self.mute_btn.setFixedSize(36, 36)
        self.mute_btn.clicked.connect(self._toggle_mute)
        controls_row.addWidget(self.mute_btn)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.valueChanged.connect(self._set_volume)
        controls_row.addWidget(self.volume_slider)

        self.volume_label = QLabel("%100")
        self.volume_label.setFixedWidth(36)
        controls_row.addWidget(self.volume_label)

        controls_row.addStretch(1)

        # Oynatma Hızı
        speed_label = QLabel(self._("Hız:"))
        controls_row.addWidget(speed_label)

        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.50x", "0.75x", "1.00x", "1.25x", "1.50x", "2.00x"])
        self.speed_combo.setCurrentText("1.00x")
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        controls_row.addWidget(self.speed_combo)

        # Zamana Zıplama Kutusu (Enter ile çalışır)
        self.jump_time_input = QLineEdit()
        self.jump_time_input.setPlaceholderText("00:00:00")
        self.jump_time_input.setFixedWidth(72)
        self.jump_time_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.jump_time_input.setToolTip(self._("Zamana Git (Örn: 14:20 veya 95) [Enter]"))
        self.jump_time_input.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # Yalnızca doğrudan tıklanınca aktif olsun!
        self.jump_time_input.returnPressed.connect(self._jump_to_entered_time)
        controls_row.addWidget(self.jump_time_input)
        
        # Playlist Aç/Kapat Butonu
        self.playlist_btn = QPushButton()
        self.playlist_btn.setIcon(self._theme_icon("view-list-details", QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.playlist_btn.setIconSize(QSize(20, 20))
        self.playlist_btn.setFixedSize(36, 36)
        self.playlist_btn.setToolTip(self._("Oynatma Listesi (Ctrl+L)"))
        self.playlist_btn.clicked.connect(self._toggle_playlist)
        controls_row.addWidget(self.playlist_btn)
        # Tam Ekran Butonu
        self.fullscreen_btn = QPushButton()
        self.fullscreen_btn.setIcon(self._theme_icon("view-fullscreen", QStyle.StandardPixmap.SP_TitleBarMaxButton))
        self.fullscreen_btn.setIconSize(QSize(20, 20))
        self.fullscreen_btn.setFixedSize(36, 36)
        self.fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        controls_row.addWidget(self.fullscreen_btn)

        panel_layout.addLayout(controls_row)
        self.main_layout.addWidget(self.bottom_control_panel)

        # Butonların ve kaydırma çubuklarının klavye odağını çalmasını engelle
        for b in self.bottom_control_panel.findChildren(QPushButton):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.speed_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.seek_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _setup_menu(self):
        self.menu_bar = self.menuBar()

        # --- 1. DOSYA MENÜSÜ ---
        file_menu = self.menu_bar.addMenu(self._("&Dosya"))

        open_action = QAction(self._("Dosya Aç..."), self)
        open_action.setShortcut(QKeySequence("Ctrl+O"))
        open_action.triggered.connect(self._open_file_dialog)
        file_menu.addAction(open_action)

        open_folder_action = QAction(self._("Klasör Aç..."), self)
        open_folder_action.setShortcut(QKeySequence("Ctrl+F"))
        open_folder_action.triggered.connect(self._open_folder_dialog)
        file_menu.addAction(open_folder_action)

        open_loc_action = QAction(self._("Bulunduğu Klasörü Aç"), self)
        open_loc_action.setShortcut(QKeySequence("Ctrl+Shift+O"))
        open_loc_action.triggered.connect(self._open_current_media_folder)
        file_menu.addAction(open_loc_action)

        add_sub_action = QAction(self._("Altyazı Dosyası Ekle..."), self)
        add_sub_action.setShortcut(QKeySequence("Ctrl+S"))
        add_sub_action.triggered.connect(self._open_subtitle_dialog)
        file_menu.addAction(add_sub_action)

        file_menu.addSeparator()

        exit_action = QAction(self._("Çıkış"), self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # --- 2. OYNAT MENÜSÜ ---
        play_menu = self.menu_bar.addMenu(self._("&Oynat"))

        play_pause_act = QAction(self._("Oynat / Duraklat"), self)
        play_pause_act.triggered.connect(self._toggle_pause)
        play_menu.addAction(play_pause_act)

        stop_act = QAction(self._("Durdur"), self)
        stop_act.setShortcut(QKeySequence("Ctrl+."))
        stop_act.triggered.connect(self.stop_playback)
        play_menu.addAction(stop_act)

        play_menu.addSeparator()

        next_act = QAction(self._("Sonraki Video"), self)
        next_act.setShortcut(QKeySequence("Ctrl+Right"))
        next_act.triggered.connect(self.play_next)
        play_menu.addAction(next_act)

        prev_act = QAction(self._("Önceki Video"), self)
        prev_act.setShortcut(QKeySequence("Ctrl+Left"))
        prev_act.triggered.connect(self.play_previous)
        play_menu.addAction(prev_act)

        play_menu.addSeparator()

        mute_act = QAction(self._("Sesi Kapat / Aç"), self)
        mute_act.setShortcut(QKeySequence("M"))
        mute_act.triggered.connect(self._toggle_mute)
        play_menu.addAction(mute_act)

        play_menu.addSeparator()

        snap_act = QAction(self._("Ekran Görüntüsü Al"), self)
        snap_act.setShortcut(QKeySequence("S"))
        snap_act.triggered.connect(self._take_screenshot)
        play_menu.addAction(snap_act)

        # --- 3. SES MENÜSÜ ---
        audio_menu = self.menu_bar.addMenu(self._("&Ses"))

        self.audio_track_menu = audio_menu.addMenu(self._("Ses İzi"))
        self.audio_track_menu.aboutToShow.connect(self._populate_audio_tracks_menu)

        audio_menu.addSeparator()

        audio_mute_act = QAction(self._("Sesi Kapat / Aç"), self)
        audio_mute_act.setShortcut(QKeySequence("M"))
        audio_mute_act.triggered.connect(self._toggle_mute)
        audio_menu.addAction(audio_mute_act)

        # --- 4. ALTYAZI MENÜSÜ ---
        sub_menu = self.menu_bar.addMenu(self._("&Altyazı"))

        self.sub_track_menu = sub_menu.addMenu(self._("Altyazı İzi"))
        self.sub_track_menu.aboutToShow.connect(self._populate_sub_tracks_menu)

        sub_menu.addSeparator()

        add_sub_act = QAction(self._("Altyazı Dosyası Ekle..."), self)
        add_sub_act.setShortcut(QKeySequence("Ctrl+S"))
        add_sub_act.triggered.connect(self._open_subtitle_dialog)
        sub_menu.addAction(add_sub_act)

        reset_sub_act = QAction(self._("Altyazı Senkronunu Sıfırla"), self)
        reset_sub_act.setShortcut(QKeySequence("Shift+Down"))
        reset_sub_act.triggered.connect(self._reset_subtitle_delay)
        sub_menu.addAction(reset_sub_act)

        # --- 5. GÖRÜNÜM MENÜSÜ ---
        view_menu = self.menu_bar.addMenu(self._("&Görünüm"))

        self.always_on_top_action = QAction(self._("Her Zaman Üstte"), self, checkable=True)
        self.always_on_top_action.setShortcut(QKeySequence("Ctrl+T"))
        self.always_on_top_action.toggled.connect(self._toggle_always_on_top)
        view_menu.addAction(self.always_on_top_action)

        view_menu.addSeparator()

        fs_action = QAction(self._("Tam Ekran"), self)
        fs_action.setShortcut(QKeySequence("F11"))
        fs_action.triggered.connect(self._toggle_fullscreen)
        view_menu.addAction(fs_action)

        pl_action = QAction(self._("Oynatma Listesi"), self)
        pl_action.setShortcut(QKeySequence("Ctrl+L"))
        pl_action.triggered.connect(self._toggle_playlist)
        view_menu.addAction(pl_action)

        view_menu.addSeparator()

        info_action = QAction(self._("Medya Bilgisi..."), self)
        info_action.setShortcut(QKeySequence("I"))
        info_action.triggered.connect(self._show_media_info_dialog)
        view_menu.addAction(info_action)

        # --- 4. YARDIM MENÜSÜ ---
        help_menu = self.menu_bar.addMenu(self._("&Yardım"))

        # Dil Seçim Alt Menüsü (locales/ klasörünü dinamik tarar)
        lang_menu = help_menu.addMenu(self._("Dil (Language)"))
        lang_group = QActionGroup(self)
        lang_group.setExclusive(True)

        available_languages = self._get_available_languages()
        for lang_code, lang_name in available_languages:
            act = QAction(lang_name, self, checkable=True)
            if self.current_locale == lang_code:
                act.setChecked(True)
            act.triggered.connect(lambda checked, code=lang_code: self._change_language(code))
            lang_group.addAction(act)
            lang_menu.addAction(act)

        help_menu.addSeparator()

        about_action = QAction(self._("Hakkında..."), self)
        about_action.triggered.connect(self._show_about_dialog)
        help_menu.addAction(about_action)

    # -------------------------------------------------------------
    # OYNATMA & DOSYA İŞLEMLERİ
    # -------------------------------------------------------------
    def play_file(self, path):
        if not os.path.exists(path):
            return False

        # Ses dosyası çalıyorsa logoyu ekranda tut, video ise MPV çizimi için gizle
        is_audio = path.lower().endswith(self.AUDIO_EXTENSIONS)
        if is_audio:
            self._reposition_logo()
            self.logo_label.show()
            self.logo_label.raise_()
        else:
            self.logo_label.hide()

        self._is_stopped = False
        self.current_playing_path = path
        self._last_played_path = path
        self._current_duration = 0
        self.setWindowTitle(f"{os.path.basename(path)} - ThisisMyPlayer")
        self._update_time_display(0)

        self.mpv_player.play(path)
        self.mpv_player.pause = False
        self._load_and_fix_subtitles(path)
        return True

    def _populate_audio_tracks_menu(self):
        """MKV/Medya içindeki ses izlerini (dublajları) dinamik olarak listeler."""
        self.audio_track_menu.clear()
        if not self.mpv_player or self._is_stopped:
            empty_act = QAction(self._("Ses izi yok"), self)
            empty_act.setEnabled(False)
            self.audio_track_menu.addAction(empty_act)
            return

        tracks = [t for t in (getattr(self.mpv_player, 'track_list', []) or []) if t.get('type') == 'audio']
        if not tracks:
            empty_act = QAction(self._("Ses izi bulunamadı"), self)
            empty_act.setEnabled(False)
            self.audio_track_menu.addAction(empty_act)
            return

        group = QActionGroup(self)
        group.setExclusive(True)

        for t in tracks:
            t_id = t.get('id')
            title = t.get('title')
            lang = t.get('lang', '').upper()
            
            # Okunabilir etiket oluşturma
            label_parts = [f"{self._('İz')} {t_id}:"]
            if title:
                label_parts.append(title)
            if lang:
                label_parts.append(f"[{lang}]")
            track_name = " ".join(label_parts)

            act = QAction(track_name, self, checkable=True)
            if t.get('selected', False):
                act.setChecked(True)
            act.triggered.connect(lambda checked, tid=t_id: self._set_audio_track(tid))
            group.addAction(act)
            self.audio_track_menu.addAction(act)

    def _set_audio_track(self, track_id):
        """Seçilen ses izine anında geçiş yapar."""
        if self.mpv_player:
            try:
                self.mpv_player.aid = track_id
            except Exception as e:
                print(f"Ses izi değiştirme hatası: {e}")

    def _populate_sub_tracks_menu(self):
        """MKV/Medya içindeki ve harici eklenmiş tüm altyazıları dinamik listeler."""
        self.sub_track_menu.clear()
        if not self.mpv_player or self._is_stopped:
            empty_act = QAction(self._("Altyazı yok"), self)
            empty_act.setEnabled(False)
            self.sub_track_menu.addAction(empty_act)
            return

        tracks = [t for t in (getattr(self.mpv_player, 'track_list', []) or []) if t.get('type') == 'sub']
        
        group = QActionGroup(self)
        group.setExclusive(True)

        # 1. Altyazıyı Kapatma Seçeneği
        off_act = QAction(self._("Kapalı (Devre Dışı)"), self, checkable=True)
        # Seçili bir altyazı yoksa "Kapalı" işaretlenir
        is_any_selected = any(t.get('selected', False) for t in tracks)
        if not is_any_selected or getattr(self.mpv_player, 'sid', None) in ('no', False, None):
            off_act.setChecked(True)
        off_act.triggered.connect(lambda: self._set_sub_track('no'))
        group.addAction(off_act)
        self.sub_track_menu.addAction(off_act)
        self.sub_track_menu.addSeparator()

        if not tracks:
            return

        # 2. Mevcut Altyazı İzleri
        for t in tracks:
            t_id = t.get('id')
            title = t.get('title')
            lang = t.get('lang', '').upper()
            
            label_parts = [f"{self._('Altyazı')} {t_id}:"]
            if title:
                label_parts.append(title)
            if lang:
                label_parts.append(f"[{lang}]")
            track_name = " ".join(label_parts)

            act = QAction(track_name, self, checkable=True)
            if t.get('selected', False):
                act.setChecked(True)
            act.triggered.connect(lambda checked, tid=t_id: self._set_sub_track(tid))
            group.addAction(act)
            self.sub_track_menu.addAction(act)

    def _set_sub_track(self, track_id):
        """Seçilen altyazı izini aktif eder veya kapatır."""
        if self.mpv_player:
            try:
                self.mpv_player.sid = track_id
                if track_id == 'no':
                    self.mpv_player.sub_visibility = False
                else:
                    self.mpv_player.sub_visibility = True
            except Exception as e:
                print(f"Altyazı izi değiştirme hatası: {e}")

    def _open_subtitle_dialog(self):
        """Kullanıcının harici bir altyazı dosyasını manuel seçmesini sağlar."""
        if not self.current_playing_path and not self._last_played_path:
            return
        
        # O an video çalıyorsa videonun olduğu dizini, çalmıyorsa /home/user dizinini açar
        target_path = self.current_playing_path or self._last_played_path
        start_dir = os.path.dirname(os.path.abspath(target_path)) if target_path else os.path.expanduser("~")

        filters = f"{self._('Altyazı Dosyaları')} (*.srt *.vtt *.ass *.ssa *.sub);;{self._('Tüm Dosyalar')} (*)"
        sub_path, _ = QFileDialog.getOpenFileName(self, self._("Altyazı Dosyası Seç"), start_dir, filters)
        if sub_path:
            self._load_and_fix_subtitles(sub_path, is_explicit_sub=True)

    def _cleanup_temp_sub(self):
        """Eski geçici altyazı dosyasını diskten temizler."""
        if hasattr(self, '_current_temp_srt') and self._current_temp_srt:
            if os.path.exists(self._current_temp_srt):
                try:
                    os.remove(self._current_temp_srt)
                except Exception:
                    pass
            self._current_temp_srt = None

    def _load_and_fix_subtitles(self, path, is_explicit_sub=False):
        """Altyazıyı otomatik bulur veya doğrudan yükler, HTML etiketlerini temizler ve MPV'ye bağlar."""
        found_srt = None

        if is_explicit_sub:
            found_srt = path
        else:
            video_dir = os.path.dirname(path)
            video_name, _ = os.path.splitext(os.path.basename(path))
            if os.path.exists(video_dir):
                for file in os.listdir(video_dir):
                    if file.lower().endswith('.srt') and file.startswith(video_name):
                        found_srt = os.path.join(video_dir, file)
                        break

        if not found_srt or not os.path.exists(found_srt):
            return

        try:
            # Karakter seti tespiti (UTF-8 veya CP1254 Türkçe)
            raw_bytes = open(found_srt, 'rb').read()
            try:
                content = raw_bytes.decode('utf-8')
            except UnicodeDecodeError:
                content = raw_bytes.decode('cp1254', errors='replace')

            # HTML etiketlerini ve tırnak işaretlerini kusursuz temizle
            clean_content = html.unescape(content)
            clean_content = clean_content.replace('&quot;', '"').replace('&apos;', "'").replace('&#39;', "'").replace('&amp;', '&')

            # Önceki geçici altyazıyı temizle ve yenisini aç
            self._cleanup_temp_sub()
            temp_srt = tempfile.NamedTemporaryFile(mode='w', suffix='.srt', delete=False, encoding='utf-8')
            temp_srt.write(clean_content)
            temp_srt.close()
            self._current_temp_srt = temp_srt.name

            def _apply_sub():
                try:
                    self.mpv_player.sub_add(temp_srt.name, 'select')
                    self.mpv_player.sub_visibility = True
                except Exception:
                    pass

            QTimer.singleShot(150, _apply_sub)

        except Exception as e:
            print(f"Altyazı ayrıştırma hatası: {e}")

    def _open_file_dialog(self):
        filters = (
            f"{self._('Tüm Medya Dosyaları')} (*.mp4 *.mkv *.avi *.webm *.flv *.mov *.wmv *.ts *.m4v *.mp3 *.flac *.wav *.ogg *.m4a *.aac *.opus *.wma);;"
            f"{self._('Ses Dosyaları')} (*.mp3 *.flac *.wav *.ogg *.oga *.m4a *.aac *.opus *.wma *.alac *.ape *.aiff);;"
            f"{self._('Video Dosyaları')} (*.mp4 *.mkv *.avi *.webm *.flv *.mov *.wmv *.ts *.m4v *.3gp *.vob);;"
            f"{self._('Tüm Dosyalar')} (*)"
        )
        # Varsayılan arama dizini doğrudan kullanıcının ev dizinidir (/home/user)
        default_dir = os.path.expanduser("~")
        file_path, _ = QFileDialog.getOpenFileName(self, self._("Medya Dosyası Seç"), default_dir, filters)
        if file_path:
            self.play_file(file_path)

    # -------------------------------------------------------------
    # SES, SÜRE VE HIZ KONTROLLERİ
    # -------------------------------------------------------------
    def _toggle_pause(self):
        if self._pause_locked:
            return
        # Eğer video tamamen durmuş ve siyah ekrandaysa: Baştan oynat
        if self._is_stopped:
            if self.playlist and self.current_index != -1:
                self.play_index(self.current_index)
            elif self._last_played_path:
                self.play_file(self._last_played_path)
            return

        if self.mpv_player:
            self.mpv_player.pause = not self.mpv_player.pause

    def _seek_relative(self, seconds):
        if self.mpv_player and not self._is_stopped and self._current_duration > 0:
            try:
                # MPV'nin doğrudan yerleşik komutu (Hata vermez, gecikmesiz atlar)
                self.mpv_player.command('seek', seconds, 'relative')
            except Exception as e:
                print(f"Seek hatası: {e}")

    def _toggle_mute(self):
        if self.mpv_player:
            self.mpv_player.mute = not self.mpv_player.mute

    def _set_volume(self, value):
        if self.mpv_player:
            val = max(0, min(100, int(value)))
            self.mpv_player.volume = val
            self.volume_label.setText(f"%{val}")

    def _on_speed_changed(self, text):
        if self.mpv_player:
            try:
                val = float(text.replace('x', '').strip())
                self.mpv_player.speed = val
            except ValueError:
                pass

    def _on_seek_moved(self, value):
        self._update_time_display(value)

    def _on_seek_released(self):
        if not self.mpv_player:
            return
        try:
            val = int(self.seek_slider.value())
            self.mpv_player.seek(val, reference='absolute', precision='exact')
            self._update_time_display(val)
        except Exception as e:
            print(f"Seek hatası: {e}")

    def _jump_to_entered_time(self):
        """Kullanıcının girdiği zamana (01:15:20, 14:20 veya 75) anında zıplar."""
        text = self.jump_time_input.text().strip()
        self.jump_time_input.clearFocus()  # Yazılan değer kutuda kalsın ama boşluk/ok tuşları için odağı bırak

        if not text or not self.mpv_player or self._current_duration <= 0:
            return

        try:
            # Hem ':' hem '.' girişlerini destekler
            clean_text = text.replace('.', ':')
            parts = [int(p) for p in clean_text.split(':') if p.isdigit()]
            target_seconds = 0

            if len(parts) == 1:
                target_seconds = parts[0]
            elif len(parts) == 2:
                target_seconds = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                target_seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
            else:
                return

            # Süre sınırlarını kontrol et
            target_seconds = max(0, min(int(self._current_duration), target_seconds))
            self.mpv_player.seek(target_seconds, reference='absolute', precision='exact')
            self._update_time_display(target_seconds)
        except Exception as e:
            print(f"Zamana zıplama hatası: {e}")

    def _adjust_subtitle_delay(self, delta):
        """Altyazı gecikmesini (delay) sessizce +/- delta saniye (0.1 sn = 100ms) kaydırır."""
        if not self.mpv_player or self._is_stopped:
            return
        try:
            current_delay = getattr(self.mpv_player, 'sub_delay', 0.0) or 0.0
            new_delay = round(current_delay + delta, 2)
            self.mpv_player.sub_delay = new_delay
        except Exception as e:
            print(f"Altyazı senkron hatası: {e}")

    def _reset_subtitle_delay(self):
        """Altyazı gecikmesini (delay) sıfırlar (0.0 sn)."""
        if not self.mpv_player or self._is_stopped:
            return
        try:
            self.mpv_player.sub_delay = 0.0
        except Exception as e:
            print(f"Altyazı sıfırlama hatası: {e}")

    def _take_screenshot(self):
        """Videonun orijinal çözünürlüğündeki karesini masaüstüne sessizce kaydeder."""
        target_path = self.current_playing_path or self._last_played_path
        if not self.mpv_player or self._is_stopped or not target_path:
            return

        try:
            import datetime
            # Sistemin geçerli Masaüstü dizinini otomatik bulur (~/Masaüstü veya ~/Desktop)
            desktop_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
            if not desktop_dir or not os.path.exists(desktop_dir):
                desktop_dir = os.path.expanduser("~/Masaüstü")
                if not os.path.exists(desktop_dir):
                    desktop_dir = os.path.expanduser("~/Desktop")

            # mpv-GG-AA-YYYY-SS-DD-SS.png formatı
            stamp = datetime.datetime.now().strftime("%d-%m-%Y-%H-%M-%S")
            filename = f"TMP-{stamp}.png"
            full_save_path = os.path.join(desktop_dir, filename)

            # 'video' parametresi: Pencere boyutu ne olursa olsun ham orijinal video çözünürlüğünü alır
            self.mpv_player.command('screenshot-to-file', full_save_path, 'video')
            print(f"Ekran görüntüsü masaüstüne kaydedildi: {full_save_path}")
        except Exception as e:
            print(f"Ekran görüntüsü kaydetme hatası: {e}")

    # -------------------------------------------------------------
    # MPV GERİ ÇAĞIRMALARI (CALLBACKS) & THREAD GÜVENLİĞİ
    # -------------------------------------------------------------
    def _mpv_pause_changed(self, name, value):
        if hasattr(self, 'play_pause_btn'):
            if value:
                self.play_pause_btn.setIcon(self._theme_icon("media-playback-start", QStyle.StandardPixmap.SP_MediaPlay))
                self._uninhibit_sleep()  # Duraklatıldı: Ekran koruyucu/uyku serbest
            else:
                self.play_pause_btn.setIcon(self._theme_icon("media-playback-pause", QStyle.StandardPixmap.SP_MediaPause))
                if not self._is_stopped:
                    self._inhibit_sleep()  # Oynatılıyor: Ekran kararması engellendi

    def _mpv_mute_changed(self, name, value):
        if hasattr(self, 'mute_btn'):
            if value:
                self.mute_btn.setIcon(self._theme_icon("audio-volume-muted", QStyle.StandardPixmap.SP_MediaVolumeMuted))
            else:
                self.mute_btn.setIcon(self._theme_icon("audio-volume-high", QStyle.StandardPixmap.SP_MediaVolume))

    def _mpv_volume_changed(self, name, value):
        if value is not None:
            self._volume_ui_update_requested.emit(int(value))

    def _apply_volume_ui(self, value):
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(value)
        self.volume_slider.blockSignals(False)
        self.volume_label.setText(f"%{value}")


    def _mpv_time_changed(self, name, value):
        if value is not None:
            self._time_ui_update_requested.emit(int(value))

    def _apply_time_ui(self, current_seconds):
        if self._is_stopped or self.seek_slider.isSliderDown():
            return
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(current_seconds)
        self.seek_slider.blockSignals(False)
        self._update_time_display(current_seconds)

    def _mpv_duration_changed(self, name, value):
        if value is not None:
            dur = int(value)
            if dur > 0:
                self._current_duration = dur
                self.seek_slider.setRange(0, dur)
                self._update_time_display(self.seek_slider.value())

    def _on_video_params_changed(self, name, value):
        QTimer.singleShot(0, self.video_container.apply_geometry)

    def _on_mpv_dbl_click_fullscreen(self, name, value):
        if value:
            self.mpv_player.fullscreen = False
            self._fullscreen_toggle_requested.emit()

    def _on_mpv_eof_reached(self, name, value):
        if value is True:
            self._eof_reached_signal.emit()

    def _on_eof_reached(self):
        # Video bitince başa sarıp bekleyebilir veya durdurabilir
        pass

    def _lock_pause_temporarily(self, duration_ms=1500):
        """Duraklatma fonksiyonunu geçici olarak kilitler ve bekleyen tıklamaları iptal eder."""
        self._pause_locked = True
        self._last_click_time = 0
        self._pause_lock_timer.start(duration_ms)

    def _unlock_pause(self):
        self._pause_locked = False

    def _on_video_single_click(self):
        if self._pause_locked:
            return

        # Pencere aktif değilse veya bu tıklama pencereyi aktifleştiren ilk tıklamaysa duraklatma yapma
        if not self.isActiveWindow() or (time.time() - self._last_activation_time) < 0.25:
            self.activateWindow()
            return

        # 190 ms: Mikro-donma yapmaz, videoyu kesintisiz tam ekrana geçirir
        self._single_click_timer.start(180)

    def _on_fullscreen_toggle_safe(self):
        # Çift tık algılandığı an bekleyen duraklatmayı hemen iptal et (Sıfır duraksama)
        if hasattr(self, '_single_click_timer'):
            self._single_click_timer.stop()
        self._toggle_fullscreen()

    # -------------------------------------------------------------
    # OYNATMA LİSTESİ YÖNETİMİ
    # -------------------------------------------------------------
    def _set_window_stay_on_top_x11(self, enable):
        """Pencere kimliğini yok etmeden (X11 BadWindow hatası vermeden) KDE/GNOME'a üstte kal sinyali gönderir."""
        try:
            x11 = ctypes.cdll.LoadLibrary('libX11.so.6')
            display = x11.XOpenDisplay(None)
            if not display:
                return False

            wid = int(self.winId())
            root = x11.XDefaultRootWindow(display)

            wm_state = x11.XInternAtom(display, b"_NET_WM_STATE", False)
            wm_above = x11.XInternAtom(display, b"_NET_WM_STATE_ABOVE", False)

            class XClientMessageEvent(ctypes.Structure):
                _fields_ = [
                    ('type', ctypes.c_int),
                    ('serial', ctypes.c_ulong),
                    ('send_event', ctypes.c_int),
                    ('display', ctypes.c_void_p),
                    ('window', ctypes.c_ulong),
                    ('message_type', ctypes.c_ulong),
                    ('format', ctypes.c_int),
                    ('data', ctypes.c_long * 5)
                ]

            event = XClientMessageEvent()
            event.type = 33  # ClientMessage
            event.window = wid
            event.message_type = wm_state
            event.format = 32
            # 1: Ekle (_NET_WM_STATE_ADD), 0: Kaldır (_NET_WM_STATE_REMOVE)
            event.data[0] = 1 if enable else 0
            event.data[1] = wm_above
            event.data[2] = 0
            event.data[3] = 1
            event.data[4] = 0

            mask = (1 << 20) | (1 << 19)  # SubstructureRedirectMask | SubstructureNotifyMask
            x11.XSendEvent(display, root, False, mask, ctypes.byref(event))
            x11.XFlush(display)
            x11.XCloseDisplay(display)
            return True
        except Exception as e:
            print(f"X11 Always on top hatası: {e}")
            return False

    def _toggle_always_on_top(self, checked):
        """Pencereyi her zaman diğer pencerelerin üstünde tutar veya normale döndürür."""
        self._set_window_stay_on_top_x11(checked)
        self.settings["always_on_top"] = checked
        self._save_settings()

    def _toggle_playlist(self):
        if self.playlist_widget.isVisible():
            self.playlist_widget.hide()
        else:
            self.playlist_widget.show()
        QTimer.singleShot(20, self.video_container.apply_geometry)

    def add_to_playlist(self, paths, clear_existing=False):
        """Dosya veya klasör listesini ekler. clear_existing=True ise eski listeyi silip hemen yenisini başlatır."""
        new_files = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in sorted(files):
                        if f.lower().endswith(self.MEDIA_EXTENSIONS):
                            new_files.append(os.path.join(root, f))
            elif os.path.isfile(p) and p.lower().endswith(self.MEDIA_EXTENSIONS):
                new_files.append(p)

        if not new_files:
            return

        if clear_existing:
            self.playlist.clear()
            self.playlist_widget.clear()
            self.current_index = -1

        start_index = len(self.playlist)
        for f in new_files:
            self.playlist.append(f)
            self.playlist_widget.addItem(os.path.basename(f))

        # Yeni bırakılan videoyu (veya listenin ilkini) hemen oynat
        self.play_index(start_index)

    def play_index(self, index):
        if 0 <= index < len(self.playlist):
            self.current_index = index
            self.playlist_widget.setCurrentRow(index)
            self.play_file(self.playlist[index])

    def play_next(self):
        if self.playlist and self.current_index + 1 < len(self.playlist):
            self.play_index(self.current_index + 1)

    def play_previous(self):
        if self.playlist and self.current_index - 1 >= 0:
            self.play_index(self.current_index - 1)

    def _on_playlist_item_double_clicked(self, item):
        row = self.playlist_widget.row(item)
        self.play_index(row)

    def _open_folder_dialog(self):
        # Varsayılan olarak /home/user dizinini açar
        default_dir = os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, self._("Medya Klasörü Seç"), default_dir)
        if folder:
            self.add_to_playlist([folder])

    def _inhibit_sleep(self):
        """Video oynarken ekranın kararmasını ve sistemin uykuya geçmesini engeller."""
        if self._screensaver_cookie is not None:
            return  # Zaten kilit devrede

        try:
            # FreeDesktop standart DBus Inhibit çağrısı (GNOME, KDE, XFCE uyumlu)
            cmd = [
                "dbus-send", "--session", "--dest=org.freedesktop.ScreenSaver",
                "--type=method_call", "--print-reply=literal",
                "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver.Inhibit",
                "string:ThisisMyPlayer", "string:Playing video"
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1)
            # Dönen çıktı: uint32 12345
            parts = res.stdout.strip().split()
            if len(parts) >= 2 and parts[1].isdigit():
                self._screensaver_cookie = int(parts[1])
        except Exception:
            pass

    def _uninhibit_sleep(self):
        """Video duraklatıldığında veya durduğunda güç tasarrufunu normale döndürür."""
        if self._screensaver_cookie is None:
            return

        try:
            cmd = [
                "dbus-send", "--session", "--dest=org.freedesktop.ScreenSaver",
                "--type=method_call",
                "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver.UnInhibit",
                f"uint32:{self._screensaver_cookie}"
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        except Exception:
            pass
        finally:
            self._screensaver_cookie = None

    def _open_current_media_folder(self):
        """O an çalan veya son oynatılan medyanın klasörünü dosya yöneticisinde açar."""
        target_path = self.current_playing_path or self._last_played_path
        if target_path and os.path.exists(target_path):
            folder_dir = os.path.dirname(os.path.abspath(target_path))
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder_dir))

    def _show_about_dialog(self):
        """Uygulama hakkında özel tasarlanmış diyalog penceresi."""
        dialog = QDialog(self)
        dialog.setWindowTitle(self._("ThisisMyPlayer Hakkında"))
        dialog.setFixedWidth(420)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.setContentsMargins(18, 16, 18, 16)

        # 1. Program Logosu
        if os.path.exists(self.logo_path):
            logo_lbl = QLabel()
            pix = QPixmap(self.logo_path).scaled(68, 68, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            logo_lbl.setPixmap(pix)
            logo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(logo_lbl)

        # 2. Başlık ve Bilgiler
        version_str = self._("Sürüm:")
        license_str = self._("Lisans:")
        lang_str = self._("Programlama Dili:")
        gui_str = self._("GUI/UX:")
        engine_str = self._("Video Oynatım Motoru:")
        dev_str = self._("Geliştirici:")
        desc_str = self._(
            "Bu, diğer şişkin alternatiflerine göre çok daha hafif, hızlı ve basit bir video oynatıcısıdır. "
            "Göz yormayan altyazı desteği, otomatik ses optimizasyonu, iyileştirilmiş kısayol yapısı, "
            "sürükle bırak desteği gibi birçok özelliği ile donatılmıştır."
        )
        warranty_str = self._("Bu program hiçbir garanti getirmez.")
        copy_str = self._("Telif Hakkı &copy; 2026 - A. Serhat KILIÇOĞLU")

        info_label = QLabel()
        info_label.setOpenExternalLinks(True)
        info_label.setTextFormat(Qt.TextFormat.RichText)
        info_label.setWordWrap(True)
        info_label.setText(
            f"<h2 style='text-align: center; margin: 4px 0;'>ThisisMyPlayer</h2>"
            f"<p style='line-height: 140%; margin-top: 6px;'>"
            f"<b>{version_str}</b> 0.0.2<br>"
            f"<b>{license_str}</b> GNU GPLv3<br>"
            f"<b>{lang_str}</b> Python3<br>"
            f"<b>{gui_str}</b> PyQt-6<br>"
            f"<b>{engine_str}</b> MPV, FFmpeg<br>"
            f"<b>{dev_str}</b> A. Serhat KILIÇOĞLU (shampuan)<br>"
            f"<b>Github:</b> <a href='https://www.github.com/shampuan'>www.github.com/shampuan</a>"
            f"</p>"
            f"<hr>"
            f"<p style='text-align: justify; line-height: 130%;'>{desc_str}</p>"
            f"<p style='font-style: italic; color: #888888; margin-top: 4px;'>{warranty_str}</p>"
            f"<p style='text-align: center; margin-top: 8px; font-weight: bold;'>{copy_str}</p>"
        )
        layout.addWidget(info_label)

        # 3. Tamam Butonu
        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)
        ok_btn = QPushButton(self._("Tamam"))
        ok_btn.setFixedSize(85, 30)
        ok_btn.clicked.connect(dialog.accept)
        btn_layout.addWidget(ok_btn)
        btn_layout.addStretch(1)
        layout.addLayout(btn_layout)

        dialog.exec()

    def _on_loop_toggled(self, checked):
        """Döngü butonunun durumuna göre ipucu bilgisini günceller."""
        if checked:
            self.loop_btn.setToolTip(self._("Döngü: Açık (Liste Bitince Başa Sar)"))
        else:
            self.loop_btn.setToolTip(self._("Döngü: Kapalı"))

    def stop_playback(self):
        """Oynatmayı tamamen durdurur, ekranı siyaha düşürür, imleci ve süreyi sıfırlar."""
        self._uninhibit_sleep()
        self._is_stopped = True
        self.current_playing_path = None
        self._current_duration = 0

        if self.mpv_player:
            try:
                self.mpv_player.stop()
            except Exception:
                pass

        # Seekbar ve göstergeleri kesin olarak sıfırla
        self.seek_slider.blockSignals(True)
        self.seek_slider.setRange(0, 100)
        self.seek_slider.setSliderPosition(0)
        self.seek_slider.setValue(0)
        self.seek_slider.blockSignals(False)
        self.time_label.setText("00:00 / 00:00")

        if hasattr(self, 'play_pause_btn'):
            self.play_pause_btn.setIcon(self._theme_icon("media-playback-start", QStyle.StandardPixmap.SP_MediaPlay))

        # Video durduğunda logo katmanını güvenle öne çıkar ve göster
        self._reposition_logo()
        self.logo_label.show()
        self.logo_label.raise_()

    def _reposition_logo(self):
        """Pencere yeniden boyutlandığında logonun tam merkezde kalmasını sağlar."""
        if hasattr(self, 'logo_label'):
            self.logo_label.setGeometry(self.video_container.rect())

    def _on_eof_reached(self):
        """Video bittiğinde sıradaki videoya geçer; liste biterse döngü durumunu kontrol eder."""
        is_loop_on = self.loop_btn.isChecked()

        # 1. Öncelik: Listede sıradaki video var mı?
        if self.playlist and self.current_index + 1 < len(self.playlist):
            self.play_next()
            return

        # 2. Öncelik: Sıradaki video yok (Listenin sonu veya tek video). Döngü açık mı?
        if is_loop_on:
            if self.playlist:
                # Liste varsa listenin en başındaki (0. indeks) videoya dön
                self.play_index(0)
            elif self.current_playing_path:
                # Tek bir video oynatılıyorsa onu baştan başlat
                try:
                    self.mpv_player.seek(0, reference='absolute', precision='exact')
                    self.mpv_player.pause = False
                except Exception:
                    pass
        else:
            # Döngü kapalıysa oynatmayı durdur ve başa sar
            self.stop_playback()

    # -------------------------------------------------------------
    # TAM EKRAN VE AKILLI KONTROL GİZLEME
    # -------------------------------------------------------------
    def _toggle_fullscreen(self):
        import time
        now = time.time() * 1000
        # 400 milisaniye içinde mükerrer tam ekran çağrılarını engelle (Geri sekme önleyici)
        if (now - getattr(self, '_last_fs_toggle_time', 0)) < 400:
            return
        self._last_fs_toggle_time = now

        is_fs = self.isFullScreen()

        if is_fs:
            self.showNormal()
            self.unsetCursor()  # Pencere modunda fare imlecini normale döndür
            self.menu_bar.setVisible(True)
            self.bottom_control_panel.setVisible(True)
            self.fullscreen_btn.setIcon(self._theme_icon("view-fullscreen", QStyle.StandardPixmap.SP_TitleBarMaxButton))
        else:
            self.menu_bar.setVisible(False)
            self.showFullScreen()
            self.fullscreen_btn.setIcon(self._theme_icon("view-restore", QStyle.StandardPixmap.SP_TitleBarNormalButton))
            self._schedule_hide_controls()

        QTimer.singleShot(50, self.video_container.apply_geometry)

    def _check_mouse_movement(self):
        if not self.isFullScreen():
            return

        from PyQt6.QtGui import QCursor
        curr_pos = QCursor.pos()
        if curr_pos != self._last_mouse_pos:
            self._last_mouse_pos = curr_pos
            self.unsetCursor()  # Fare hareket ettiğinde imleci göster
            self.bottom_control_panel.setVisible(True)
            self._schedule_hide_controls()

    def _schedule_hide_controls(self):
        if not self.isFullScreen():
            return
        if self.seek_slider.isSliderDown():
            return
        self.controls_hide_timer.start(2000)

    def _hide_controls_fullscreen(self):
        if self.isFullScreen() and not self.seek_slider.isSliderDown():
            self.bottom_control_panel.setVisible(False)
            self.setCursor(Qt.CursorShape.BlankCursor)  # 2 saniye sonra fareyi gizle

    # -------------------------------------------------------------
    # SÜRÜKLE - BIRAK (DRAG & DROP) DESTEĞİ
    # -------------------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            # Dosya pencereye girdiği an duraklatmayı kilitle
            self._lock_pause_temporarily(1500)
            event.acceptProposedAction()

    def dropEvent(self, event):
        # Dosya bırakıldığı an kilidi 1.5 saniye daha uzat
        self._lock_pause_temporarily(1500)
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        if paths:
            self.add_to_playlist(paths, clear_existing=True)
        # Dosya yöneticisinden odağı kesin olarak geri al
        QTimer.singleShot(50, lambda: (self.raise_(), self.activateWindow(), self.setFocus()))

    # -------------------------------------------------------------
    # KLAVYE KISAYOLLARI
    # -------------------------------------------------------------
    def keyPressEvent(self, event):
        key = event.key()
        text = event.text().lower()

        # Türkçe Q (ı/i) ve İngilizce Q (i) çakışmasız yakalama
        if text in ('ı', 'i') and event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
            self._show_media_info_dialog()
            return

        if (event.modifiers() == Qt.KeyboardModifier.AltModifier and 
                key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self._toggle_fullscreen()
        elif key == Qt.Key.Key_Space:
            self._toggle_pause()
        elif key == Qt.Key.Key_Escape and self.isFullScreen():
            self._toggle_fullscreen()
        elif key == Qt.Key.Key_Left:
            if event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                self._adjust_subtitle_delay(-0.1)  # 100 ms geriye çek (daha erken gelsin)
            else:
                self._seek_relative(-5)
        elif key == Qt.Key.Key_Right:
            if event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                self._adjust_subtitle_delay(0.1)   # 100 ms ileriye at (daha geç gelsin)
            else:
                self._seek_relative(5)
        elif key == Qt.Key.Key_Up:
            self._set_volume(self.volume_slider.value() + 5)
        elif key == Qt.Key.Key_Down:
            if event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                self._reset_subtitle_delay()
            else:
                self._set_volume(self.volume_slider.value() - 5)
        elif key == Qt.Key.Key_M:
            self._toggle_mute()
        elif key == Qt.Key.Key_S or text == 's':
            self._take_screenshot()
        elif key == Qt.Key.Key_Period or event.text() == '.':
            if self.mpv_player and not self._is_stopped:
                try:
                    self.mpv_player.frame_step()
                except Exception:
                    pass
        elif key == Qt.Key.Key_Comma or event.text() == ',':
            if self.mpv_player and not self._is_stopped:
                try:
                    self.mpv_player.frame_back_step()
                except Exception:
                    pass
        else:
            super().keyPressEvent(event)

    # -------------------------------------------------------------
    # YARDIMCI METOTLAR
    # -------------------------------------------------------------
    def _format_time(self, seconds):
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _update_time_display(self, current_seconds):
        cur_str = self._format_time(int(current_seconds or 0))
        dur_str = self._format_time(int(self._current_duration)) if self._current_duration > 0 else "00:00"
        self.time_label.setText(f"{cur_str} / {dur_str}")

    def _theme_icon(self, theme_name, fallback_pixmap):
        icon = QIcon.fromTheme(theme_name)
        return QApplication.instance().style().standardIcon(fallback_pixmap) if icon.isNull() else icon

    def _show_media_info_dialog(self):
        """O an oynatılan medyanın dosya yolunu, boyutunu ve ayrıntılı MPV teknik verilerini gösterir."""
        from PyQt6.QtWidgets import QGroupBox, QFormLayout, QLineEdit

        # Eğer pencere zaten açıksa öne getir ve çık
        if hasattr(self, '_media_info_dialog') and self._media_info_dialog and self._media_info_dialog.isVisible():
            self._media_info_dialog.raise_()
            self._media_info_dialog.activateWindow()
            return

        target_path = self.current_playing_path or self._last_played_path
        if not target_path or not os.path.exists(target_path):
            QMessageBox.information(self, self._("Medya Bilgisi"), self._("Şu anda yüklü bir medya dosyası yok."))
            return

        # 1. Akıllı Kapsül / Format Adlandırma (FFmpeg ham çıktısını temizler)
        ext = os.path.splitext(target_path)[1].lstrip('.').lower()
        raw_fmt = getattr(self.mpv_player, 'file_format', '') or ''
        
        format_map = {
            'mp4': 'MP4 (MPEG-4 Part 14)',
            'mkv': 'MKV (Matroska)',
            'webm': 'WebM',
            'avi': 'AVI (Audio Video Interleaved)',
            'mov': 'QuickTime (MOV)',
            'flv': 'Flash Video (FLV)',
            'ts': 'MPEG-TS (Transport Stream)',
            'm4v': 'M4V (Apple Video)',
            'mp3': 'MP3 (MPEG Audio Layer 3)',
            'flac': 'FLAC (Free Lossless Audio Codec)',
            'wav': 'WAV (Waveform Audio)',
            'ogg': 'OGG Container'
        }
        
        if ext in format_map:
            file_fmt = format_map[ext]
        elif 'mp4' in raw_fmt.lower():
            file_fmt = 'MP4 (MPEG-4)'
        elif 'matroska' in raw_fmt.lower():
            file_fmt = 'MKV (Matroska)'
        elif raw_fmt:
            file_fmt = raw_fmt.split(',')[0].strip().upper()
        else:
            file_fmt = ext.upper() if ext else self._("Bilinmiyor")

        # 2. Boyut ve Süre Bilgisi
        try:
            size_bytes = os.path.getsize(target_path)
            if size_bytes >= 1024 * 1024 * 1024:
                size_str = f"{size_bytes / (1024 * 1024 * 1024):.2f} GB ({size_bytes:,} bayt)"
            else:
                size_str = f"{size_bytes / (1024 * 1024):.2f} MB ({size_bytes:,} bayt)"
        except Exception:
            size_str = self._("Bilinmiyor")

        dur_str = self._format_time(int(self._current_duration)) if self._current_duration > 0 else self._("Bilinmiyor")

        # 3. Ayrıntılı Video Parametreleri
        v_codec = getattr(self.mpv_player, 'video_codec', None) or self._("Yok")
        w = getattr(self.mpv_player, 'width', None)
        h = getattr(self.mpv_player, 'height', None)
        res_str = f"{w} x {h}" if (w and h) else self._("Bilinmiyor")

        v_params = getattr(self.mpv_player, 'video_params', None) or {}
        aspect = v_params.get('aspect', None) if isinstance(v_params, dict) else None
        aspect_str = f"{aspect:.2f}:1" if aspect else self._("Bilinmiyor")
        pix_fmt = v_params.get('pixelformat', '-') if isinstance(v_params, dict) else '-'

        fps = getattr(self.mpv_player, 'container_fps', None) or getattr(self.mpv_player, 'fps', None)
        fps_str = f"{fps:.3f} fps" if fps else self._("Bilinmiyor")

        v_bitrate = getattr(self.mpv_player, 'video_bitrate', None)
        v_bitrate_str = f"{int(v_bitrate / 1000):,} kbps" if v_bitrate else self._("Değişken / Bilinmiyor")

        hwdec = getattr(self.mpv_player, 'hwdec_current', None)
        hwdec_str = hwdec.upper() if (hwdec and hwdec != 'no') else self._("Kapalı (Yazılımsal CPU)")

        # 4. Ayrıntılı Ses Parametreleri
        a_codec = getattr(self.mpv_player, 'audio_codec_name', None) or self._("Yok")
        a_bitrate = getattr(self.mpv_player, 'audio_bitrate', None)
        a_bitrate_str = f"{int(a_bitrate / 1000):,} kbps" if a_bitrate else self._("Değişken / Bilinmiyor")

        a_params = getattr(self.mpv_player, 'audio_params', None) or {}
        if isinstance(a_params, dict):
            channels = a_params.get('channels', '-')
            channel_cnt = a_params.get('channel-count', '-')
            ch_str = f"{channels} ({channel_cnt} {self._('kanal')})" if channels != '-' else str(channel_cnt)
            srate = a_params.get('samplerate', '-')
            srate_str = f"{srate:,} Hz" if srate != '-' else '-'
            sample_fmt = a_params.get('format', '-')
        else:
            ch_str, srate_str, sample_fmt = "-", "-", "-"

        # 5. Non-Modal (Ana Pencereyi Kilitlemeyen) Bilgi Penceresi
        dialog = QDialog(self)
        dialog.setWindowTitle(self._("Medya Özellikleri"))
        dialog.setFixedWidth(520)

        # Bilgi penceresi odaktayken Boşluk tuşu videoyu duraklatsın / I tuşu pencereyi kapatsın
        def _dialog_key_press(event):
            if event.key() == Qt.Key.Key_Space:
                self._toggle_pause()
            elif event.text().lower() in ('ı', 'i') or event.key() == Qt.Key.Key_Escape:
                dialog.close()
            else:
                QDialog.keyPressEvent(dialog, event)

        dialog.keyPressEvent = _dialog_key_press

        main_layout = QVBoxLayout(dialog)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(14, 14, 14, 14)

        # Üst Başlık ve Sistem "dialog-information" Simgesi
        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)

        icon_label = QLabel()
        sys_info_icon = self._theme_icon("dialog-information", QStyle.StandardPixmap.SP_MessageBoxInformation)
        icon_label.setPixmap(sys_info_icon.pixmap(48, 48))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        header_layout.addWidget(icon_label)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(4)
        file_title = QLabel(f"<b>{os.path.basename(target_path)}</b>")
        file_title.setStyleSheet("font-size: 13px;")
        file_title.setWordWrap(True)
        title_layout.addWidget(file_title)

        path_box = QLineEdit(target_path)
        path_box.setReadOnly(True)
        path_box.setToolTip(self._("Dosya yolunu seçip kopyalayabilirsiniz"))
        title_layout.addWidget(path_box)

        header_layout.addLayout(title_layout, 1)
        main_layout.addLayout(header_layout)

        # --- GRUP 1: GENEL BİLGİLER ---
        gen_group = QGroupBox(self._("Genel Bilgiler"))
        gen_form = QFormLayout(gen_group)
        gen_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        gen_form.addRow(self._("Kapsül Biçimi:"), QLabel(file_fmt))
        gen_form.addRow(self._("Dosya Boyutu:"), QLabel(size_str))
        gen_form.addRow(self._("Toplam Süre:"), QLabel(dur_str))
        main_layout.addWidget(gen_group)

        # --- GRUP 2: VİDEO AKIŞI ---
        vid_group = QGroupBox(self._("Video Akışı"))
        vid_form = QFormLayout(vid_group)
        vid_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        vid_form.addRow(self._("Kodek:"), QLabel(v_codec.upper()))
        vid_form.addRow(self._("Çözünürlük:"), QLabel(f"{res_str}  ({self._('En-Boy:')} {aspect_str})"))
        vid_form.addRow(self._("Kare Hızı (FPS):"), QLabel(fps_str))
        vid_form.addRow(self._("Bit Hızı (Bitrate):"), QLabel(v_bitrate_str))
        vid_form.addRow(self._("Piksel Formatı:"), QLabel(pix_fmt))
        vid_form.addRow(self._("Donanım Hızlandırma:"), QLabel(hwdec_str))
        main_layout.addWidget(vid_group)

        # --- GRUP 3: SES AKIŞI ---
        aud_group = QGroupBox(self._("Ses Akışı"))
        aud_form = QFormLayout(aud_group)
        aud_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        aud_form.addRow(self._("Kodek:"), QLabel(a_codec.upper()))
        aud_form.addRow(self._("Bit Hızı (Bitrate):"), QLabel(a_bitrate_str))
        aud_form.addRow(self._("Kanal Yapısı:"), QLabel(ch_str))
        aud_form.addRow(self._("Örnekleme Hızı:"), QLabel(srate_str))
        aud_form.addRow(self._("Örnekleme Formatı:"), QLabel(sample_fmt))
        main_layout.addWidget(aud_group)

        # Alt Kapat Butonu
        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)
        close_btn = QPushButton(self._("Kapat"))
        close_btn.setFixedSize(90, 30)
        close_btn.clicked.connect(dialog.close)
        close_btn.setDefault(True)
        btn_layout.addWidget(close_btn)
        main_layout.addLayout(btn_layout)

        # Garbage collector silmesin diye pencere referansını tut ve non-modal aç
        self._media_info_dialog = dialog
        dialog.show()

    def changeEvent(self, event):
        """Pencere odağı değiştiğinde (pasiften aktife geçtiğinde) zamanı kaydet."""
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._last_activation_time = time.time()
        super().changeEvent(event)

    def closeEvent(self, event):
        """Kapatılırken MPV arka plan işlemlerini çökmeden temizle."""
        self._uninhibit_sleep()
        self._cleanup_temp_sub()
        if hasattr(self, 'mpv_player') and self.mpv_player:
            try:
                self.mpv_player.terminate()
            except Exception:
                pass
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setDesktopFileName("thisismyplayer")  # GNOME, KDE ve Wayland uyumluluğu
    player = ThisisMyPlayer()
    player.show()
    sys.exit(app.exec())