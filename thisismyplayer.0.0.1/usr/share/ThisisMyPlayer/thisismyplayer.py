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
    QFileDialog, QComboBox, QMessageBox, QDialog
)
from PyQt6.QtGui import QIcon, QFont, QColor, QKeySequence, QAction, QActionGroup, QPixmap, QDesktopServices
from PyQt6.QtCore import Qt, QSize, pyqtSignal, QTimer, QPoint, QEvent, QUrl

# MPV Kütüphane Kontrolü
try:
    import mpv
except ImportError:
    print("HATA: 'python-mpv' kütüphanesi bulunamadı. Lütfen 'pip install python-mpv' kurun.")
    sys.exit(1)


class ClickableSlider(QSlider):
    """Tıklanan noktaya anında atlayan ve sürüklemeyi destekleyen gelişmiş çubuk."""
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

        # Butonların klavye odağını çalıp Boşluk (Space) tuşunu ezmesini engelle
        for b in self.bottom_control_panel.findChildren(QPushButton):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.speed_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)

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

        # --- 3. GÖRÜNÜM MENÜSÜ ---
        view_menu = self.menu_bar.addMenu(self._("&Görünüm"))

        fs_action = QAction(self._("Tam Ekran"), self)
        fs_action.setShortcut(QKeySequence("F11"))
        fs_action.triggered.connect(self._toggle_fullscreen)
        view_menu.addAction(fs_action)

        pl_action = QAction(self._("Oynatma Listesi"), self)
        pl_action.setShortcut(QKeySequence("Ctrl+L"))
        pl_action.triggered.connect(self._toggle_playlist)
        view_menu.addAction(pl_action)

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
        if self.mpv_player and self._current_duration > 0:
            try:
                self.mpv_player.seek(seconds, reference='relative', precision='exact')
            except Exception:
                pass

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

    # -------------------------------------------------------------
    # MPV GERİ ÇAĞIRMALARI (CALLBACKS) & THREAD GÜVENLİĞİ
    # -------------------------------------------------------------
    def _mpv_pause_changed(self, name, value):
        if hasattr(self, 'play_pause_btn'):
            if value:
                self.play_pause_btn.setIcon(self._theme_icon("media-playback-start", QStyle.StandardPixmap.SP_MediaPlay))
            else:
                self.play_pause_btn.setIcon(self._theme_icon("media-playback-pause", QStyle.StandardPixmap.SP_MediaPause))

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
            f"<b>{version_str}</b> 0.0.1<br>"
            f"<b>{license_str}</b> GNU GPLv3<br>"
            f"<b>{lang_str}</b> Python3<br>"
            f"<b>{gui_str}</b> PyQt-6<br>"
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

    # -------------------------------------------------------------
    # KLAVYE KISAYOLLARI
    # -------------------------------------------------------------
    def keyPressEvent(self, event):
        key = event.key()

        if (event.modifiers() == Qt.KeyboardModifier.AltModifier and 
                key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self._toggle_fullscreen()
        elif key == Qt.Key.Key_Space:
            self._toggle_pause()
        elif key == Qt.Key.Key_Escape and self.isFullScreen():
            self._toggle_fullscreen()
        elif key == Qt.Key.Key_Left:
            self._seek_relative(-5)
        elif key == Qt.Key.Key_Right:
            self._seek_relative(5)
        elif key == Qt.Key.Key_Up:
            self._set_volume(self.volume_slider.value() + 5)
        elif key == Qt.Key.Key_Down:
            self._set_volume(self.volume_slider.value() - 5)
        elif key == Qt.Key.Key_M:
            self._toggle_mute()
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

    def changeEvent(self, event):
        """Pencere odağı değiştiğinde (pasiften aktife geçtiğinde) zamanı kaydet."""
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._last_activation_time = time.time()
        super().changeEvent(event)

    def closeEvent(self, event):
        """Kapatılırken MPV arka plan işlemlerini çökmeden temizle."""
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