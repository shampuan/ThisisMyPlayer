#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# ThisisMyPlayer - GTK4 / libadwaita sürümü
# -------------------------------------------------------------
# Bu, orijinal PyQt6 sürümünün GTK4 + libadwaita ile yeniden
# yazılmış halidir. Video yüzeyi artık Qt'deki gibi mpv'ye
# doğrudan bir pencere kimliği (wid) vererek değil, GTK4'ün
# Gtk.GLArea widget'ı üzerinden libmpv'nin "render API"si ile
# çizdiriliyor. Bunun nedeni: GTK4, GTK3'teki Gtk.Socket'i
# kaldırdı ve Wayland'da harici pencere gömme (foreign surface
# embedding) mümkün değil; bu yüzden GTK4 + mpv birlikteliğinde
# önerilen/standart yöntem budur (hem X11 hem Wayland'da çalışır).
#
# Pencere başlığı "geniş stil" istendiği için Adw.HeaderBar
# kullanılıyor (GNOME/libadwaita'nın CSD - istemci taraflı
# dekorasyon - başlık çubuğu). Klasik Dosya/Oynat/Görünüm/Yardım
# menü çubuğu yerine, libadwaita convention'ına uygun biçimde
# başlık çubuğundaki hamburger menüsü kullanıldı; ama tüm
# fonksiyonlar (kısayollar dahil) korundu.
# -------------------------------------------------------------

import os
import sys
import locale
import html
import tempfile
import time
import ctypes
import ctypes.util

# --- SAYISAL LOCALE GÜVENLİĞİ (mpv/ctypes için önemli) ---
os.environ['LC_NUMERIC'] = 'C'
try:
    locale.setlocale(locale.LC_ALL, 'C')
except locale.Error:
    try:
        locale.setlocale(locale.LC_NUMERIC, 'C')
    except locale.Error:
        pass
# -----------------------------------------------------------

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, GLib, Gio, GObject

# MPV Kütüphane Kontrolü
try:
    import mpv
except ImportError:
    print("HATA: 'python-mpv' kütüphanesi bulunamadı. Lütfen 'pip install python-mpv' kurun.")
    sys.exit(1)


# =================================================================
# MPV RENDER API İÇİN OPENGL PROC-ADDRESS ÇÖZÜMLEYİCİ
# =================================================================
# libmpv'nin render API'si, çizim için gereken OpenGL fonksiyon
# adreslerini bizden ister (get_proc_address). GTK4'ün GLArea'sı
# zaten bir GL bağlamı kurduğu için, bu adresleri sistemin GLX
# (X11/XWayland) veya EGL (saf Wayland) kütüphanesinden ctypes ile
# doğrudan okuyoruz. PyOpenGL gibi ekstra bir bağımlılık gerekmez.

GL_COLOR_BUFFER_BIT = 0x00004000
GL_DEPTH_BUFFER_BIT = 0x00000100
GL_FRAMEBUFFER_BINDING = 0x8CA6


def _load_gl_lib():
    for cand in ('libGL.so.1', 'libGL.so', ctypes.util.find_library('GL')):
        if not cand:
            continue
        try:
            return ctypes.CDLL(cand, mode=ctypes.RTLD_GLOBAL)
        except OSError:
            continue
    return None


def _load_epoxy_lib():
    for cand in ('libepoxy.so.0', 'libepoxy.so', ctypes.util.find_library('epoxy')):
        if not cand:
            continue
        try:
            return ctypes.CDLL(cand, mode=ctypes.RTLD_GLOBAL)
        except OSError:
            continue
    return None


_GL_LIB = _load_gl_lib()
_EPOXY_LIB = _load_epoxy_lib()


def _current_fbo():
    """GTK'nin GLArea için o an bağlı olan framebuffer'ının kimliğini döndürür."""
    if _GL_LIB is None:
        return 0
    fbo = ctypes.c_int(0)
    try:
        _GL_LIB.glGetIntegerv(GL_FRAMEBUFFER_BINDING, ctypes.byref(fbo))
    except Exception:
        return 0
    return fbo.value


def _mpv_get_proc_address(_ctx, name):
    """MPV render API'sinin istediği OpenGL/EGL fonksiyon adreslerini çözer."""
    sym = name.encode('utf-8') if isinstance(name, str) else name

    if _EPOXY_LIB is not None:
        try:
            _EPOXY_LIB.epoxy_get_proc_address.restype = ctypes.c_void_p
            _EPOXY_LIB.epoxy_get_proc_address.argtypes = [ctypes.c_char_p]
            addr = _EPOXY_LIB.epoxy_get_proc_address(sym)
            if addr:
                return addr
        except Exception:
            pass

    if _GL_LIB is not None:
        try:
            _GL_LIB.glXGetProcAddress.restype = ctypes.c_void_p
            _GL_LIB.glXGetProcAddress.argtypes = [ctypes.c_char_p]
            addr = _GL_LIB.glXGetProcAddress(sym)
            if addr:
                return addr
        except Exception:
            pass

    return 0


class ThisisMyPlayerWindow(Adw.ApplicationWindow):

    AUDIO_EXTENSIONS = (
        '.mp3', '.flac', '.wav', '.ogg', '.oga', '.m4a', '.aac',
        '.opus', '.wma', '.alac', '.ape', '.aiff', '.mid', '.midi'
    )
    VIDEO_EXTENSIONS = (
        '.mp4', '.mkv', '.avi', '.webm', '.flv', '.mov', '.wmv',
        '.ts', '.m4v', '.3gp', '.vob', '.ogv'
    )
    MEDIA_EXTENSIONS = AUDIO_EXTENSIONS + VIDEO_EXTENSIONS

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_default_size(850, 580)
        self.set_size_request(640, 360)
        self.set_title("ThisisMyPlayer")

        self.app_dir = os.path.dirname(os.path.realpath(__file__))
        self.logo_path = os.path.join(self.app_dir, "ThisisMyPlayer.png")
        try:
            self.set_icon_name("thisismyplayer")
        except Exception:
            pass

        # Durum değişkenleri
        self._current_duration = 0
        self.current_playing_path = None
        self._last_played_path = None
        self._is_stopped = False
        self.playlist = []
        self.current_index = -1
        self._is_fullscreen = False
        self._seek_dragging = False
        self._pending_click_timer_id = None
        self._controls_hide_timer_id = None
        self._current_temp_srt = None
        self.render_ctx = None

        # MPV motorunu (pencere kimliği vermeden - render API için) kur
        self.mpv_player = mpv.MPV(
            vo='libmpv',
            loop=False,
            keep_open=True,
            sub_auto='no',
            osc=False,
            idle=True,
            volume=100,
            sub_color='#d2c18e',
            sub_border_color='#000000',
            sub_border_size=1.8,
            af='lavfi=[dynaudnorm=f=500:g=31:m=7]',
        )

        self._setup_css()
        self._setup_ui()
        self._setup_actions()
        self._setup_shortcuts()

        # Arayüz hazır olduktan sonra MPV dinleyicilerini bağla
        self.mpv_player.observe_property('pause', self._mpv_pause_changed)
        self.mpv_player.observe_property('mute', self._mpv_mute_changed)
        self.mpv_player.observe_property('volume', self._mpv_volume_changed)
        self.mpv_player.observe_property('eof-reached', self._mpv_eof_changed)
        self.mpv_player.observe_property('time-pos', self._mpv_time_changed)
        self.mpv_player.observe_property('duration', self._mpv_duration_changed)

        self.connect('close-request', self._on_close_request)

        # Komut satırından dosya verilmişse hemen başlat
        if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
            GLib.timeout_add(150, self._play_from_cmdline, sys.argv[1])

    def _play_from_cmdline(self, path):
        self.play_file(path)
        return False

    # -------------------------------------------------------------
    # CSS (video alanı arka planı vs.)
    # -------------------------------------------------------------
    def _setup_css(self):
        css = b"""
        .video-area { background-color: #000000; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # -------------------------------------------------------------
    # ARAYÜZ KURULUMU
    # -------------------------------------------------------------
    def _setup_ui(self):
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # --- Geniş Stil Başlık Çubuğu (Adw.HeaderBar) ---
        self.headerbar = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="ThisisMyPlayer", subtitle="")
        self.headerbar.set_title_widget(self.window_title)

        open_btn = Gtk.Button(icon_name="document-open-symbolic")
        open_btn.set_tooltip_text("Dosya Aç (Ctrl+O)")
        open_btn.connect('clicked', lambda b: self._open_file_dialog())
        self.headerbar.pack_start(open_btn)

        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        self.menu_button.set_tooltip_text("Menü")
        self.menu_button.set_menu_model(self._build_menu())
        self.headerbar.pack_end(self.menu_button)

        toolbar_view.add_top_bar(self.headerbar)

        # --- Ana İçerik ---
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        center_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        center_box.set_vexpand(True)

        # 1. Video Yüzeyi (GLArea + üzerinde logo katmanı)
        self.gl_area = Gtk.GLArea()
        self.gl_area.set_hexpand(True)
        self.gl_area.set_vexpand(True)
        self.gl_area.set_has_depth_buffer(False)
        self.gl_area.set_has_stencil_buffer(False)
        self.gl_area.set_auto_render(False)  # Boş yere VRAM çöplüğünü ekrana basmasını engeller
        self.gl_area.set_visible(False)  # Video oynatılana kadar VRAM çöpünü tamamen gizle
        self.gl_area.connect('realize', self._on_gl_realize)
        self.gl_area.connect('unrealize', self._on_gl_unrealize)
        self.gl_area.connect('render', self._on_gl_render)

        self.logo_picture = Gtk.Picture()
        if os.path.exists(self.logo_path):
            self.logo_picture.set_filename(self.logo_path)
        self.logo_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.logo_picture.set_size_request(86, 86)
        self.logo_picture.set_halign(Gtk.Align.CENTER)
        self.logo_picture.set_valign(Gtk.Align.CENTER)
        self.logo_picture.set_can_target(False)

        self.video_overlay = Gtk.Overlay()
        self.video_overlay.add_css_class('video-area')
        self.video_overlay.set_child(self.gl_area)
        self.video_overlay.add_overlay(self.logo_picture)
        self.video_overlay.set_hexpand(True)
        self.video_overlay.set_vexpand(True)

        click_gesture = Gtk.GestureClick()
        click_gesture.set_button(Gdk.BUTTON_PRIMARY)
        click_gesture.connect('pressed', self._on_video_click_pressed)
        self.video_overlay.add_controller(click_gesture)

        center_box.append(self.video_overlay)

        # 2. Oynatma Listesi Paneli (varsayılan gizli)
        self.playlist_store = Gtk.ListBox()
        self.playlist_store.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.playlist_store.set_activate_on_single_click(False)
        self.playlist_store.connect('row-activated', self._on_playlist_row_activated)

        playlist_scroller = Gtk.ScrolledWindow()
        playlist_scroller.set_child(self.playlist_store)
        playlist_scroller.set_size_request(240, -1)
        playlist_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.playlist_revealer = Gtk.Revealer()
        self.playlist_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.playlist_revealer.set_reveal_child(False)
        self.playlist_revealer.set_child(playlist_scroller)
        center_box.append(self.playlist_revealer)

        main_box.append(center_box)

        # --- Alt Kontrol Paneli ---
        self.bottom_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.bottom_panel.set_margin_top(4)
        self.bottom_panel.set_margin_bottom(8)
        self.bottom_panel.set_margin_start(10)
        self.bottom_panel.set_margin_end(10)

        # 1. Satır: Seek Bar + Süre
        seek_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.seek_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        self.seek_scale.set_range(0, 100)
        self.seek_scale.set_draw_value(False)
        self.seek_scale.set_hexpand(True)
        # Sadece kullanıcının çubuğu elle değiştirdiği anı yakalayan temiz GTK4 sinyali:
        self.seek_scale.connect('change-value', self._on_seek_change_value)
        seek_row.append(self.seek_scale)

        self.time_label = Gtk.Label(label="00:00 / 00:00")
        self.time_label.set_width_chars(12)
        self.time_label.set_halign(Gtk.Align.END)
        seek_row.append(self.time_label)
        self.bottom_panel.append(seek_row)

        # 2. Satır: Butonlar, Ses ve Hız Kontrolleri
        controls_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self.play_pause_btn = Gtk.Button(icon_name="media-playback-start-symbolic")
        self.play_pause_btn.connect('clicked', lambda b: self._toggle_pause())
        controls_row.append(self.play_pause_btn)

        self.stop_btn = Gtk.Button(icon_name="media-playback-stop-symbolic")
        self.stop_btn.set_tooltip_text("Durdur")
        self.stop_btn.connect('clicked', lambda b: self.stop_playback())
        controls_row.append(self.stop_btn)

        self.prev_btn = Gtk.Button(icon_name="media-skip-backward-symbolic")
        self.prev_btn.set_tooltip_text("Önceki Video")
        self.prev_btn.connect('clicked', lambda b: self.play_previous())
        controls_row.append(self.prev_btn)

        self.next_btn = Gtk.Button(icon_name="media-skip-forward-symbolic")
        self.next_btn.set_tooltip_text("Sonraki Video")
        self.next_btn.connect('clicked', lambda b: self.play_next())
        controls_row.append(self.next_btn)

        self.loop_btn = Gtk.ToggleButton(icon_name="media-playlist-repeat-symbolic")
        self.loop_btn.set_tooltip_text("Döngü (Liste Bitince Başa Sar)")
        self.loop_btn.connect('toggled', self._on_loop_toggled)
        controls_row.append(self.loop_btn)

        self.open_folder_btn = Gtk.Button(icon_name="folder-open-symbolic")
        self.open_folder_btn.set_tooltip_text("Kaynak Klasörü Aç")
        self.open_folder_btn.connect('clicked', lambda b: self._open_current_media_folder())
        controls_row.append(self.open_folder_btn)

        spacer1 = Gtk.Box()
        spacer1.set_size_request(10, -1)
        controls_row.append(spacer1)

        self.mute_btn = Gtk.Button(icon_name="audio-volume-high-symbolic")
        self.mute_btn.connect('clicked', lambda b: self._toggle_mute())
        controls_row.append(self.mute_btn)

        self.volume_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        self.volume_scale.set_range(0, 100)
        self.volume_scale.set_value(100)
        self.volume_scale.set_draw_value(False)
        self.volume_scale.set_size_request(90, -1)
        self._volume_scale_handler_id = self.volume_scale.connect(
            'value-changed', lambda s: self._set_volume(s.get_value()))
        controls_row.append(self.volume_scale)

        self.volume_label = Gtk.Label(label="%100")
        self.volume_label.set_width_chars(5)
        controls_row.append(self.volume_label)

        stretch = Gtk.Box()
        stretch.set_hexpand(True)
        controls_row.append(stretch)

        speed_label = Gtk.Label(label="Hız:")
        controls_row.append(speed_label)

        self.speed_options = ["0.50x", "0.75x", "1.00x", "1.25x", "1.50x", "2.00x"]
        self.speed_dropdown = Gtk.DropDown.new_from_strings(self.speed_options)
        self.speed_dropdown.set_selected(2)
        self.speed_dropdown.connect('notify::selected', self._on_speed_changed)
        controls_row.append(self.speed_dropdown)

        self.playlist_btn = Gtk.ToggleButton(icon_name="view-list-symbolic")
        self.playlist_btn.set_tooltip_text("Oynatma Listesi (Ctrl+L)")
        self.playlist_btn.connect('toggled', lambda b: self._toggle_playlist())
        controls_row.append(self.playlist_btn)

        self.fullscreen_btn = Gtk.Button(icon_name="view-fullscreen-symbolic")
        self.fullscreen_btn.connect('clicked', lambda b: self._toggle_fullscreen())
        controls_row.append(self.fullscreen_btn)

        self.bottom_panel.append(controls_row)
        main_box.append(self.bottom_panel)

        toolbar_view.set_content(main_box)

        # --- Sürükle-Bırak ---
        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect('drop', self._on_drop)
        self.add_controller(drop_target)

        # --- Fare Hareketi (Tam Ekranda Kontrolleri Göster/Gizle) ---
        motion_controller = Gtk.EventControllerMotion()
        motion_controller.connect('motion', self._on_pointer_motion)
        self.add_controller(motion_controller)

    def _build_menu(self):
        menu = Gio.Menu()

        file_section = Gio.Menu()
        file_section.append("Dosya Aç…", "win.open-file")
        file_section.append("Klasör Aç…", "win.open-folder")
        file_section.append("Bulunduğu Klasörü Aç", "win.open-location")
        file_section.append("Altyazı Dosyası Ekle…", "win.add-subtitle")
        menu.append_section(None, file_section)

        play_section = Gio.Menu()
        play_section.append("Oynat / Duraklat", "win.toggle-pause")
        play_section.append("Durdur", "win.stop")
        play_section.append("Sonraki Video", "win.play-next")
        play_section.append("Önceki Video", "win.play-previous")
        menu.append_section(None, play_section)

        view_section = Gio.Menu()
        view_section.append("Tam Ekran", "win.toggle-fullscreen")
        view_section.append("Oynatma Listesi", "win.toggle-playlist")
        menu.append_section(None, view_section)

        end_section = Gio.Menu()
        end_section.append("Hakkında…", "win.about")
        end_section.append("Çıkış", "win.quit")
        menu.append_section(None, end_section)

        return menu

    # -------------------------------------------------------------
    # EYLEMLER (Gio.SimpleAction) VE KISAYOLLAR
    # -------------------------------------------------------------
    def _add_action(self, name, callback, accels=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect('activate', lambda a, p: callback())
        self.add_action(action)
        if accels:
            app = self.get_application()
            if app is not None:
                app.set_accels_for_action(f'win.{name}', accels)

    def _setup_actions(self):
        self._add_action('open-file', self._open_file_dialog, ['<Control>o'])
        self._add_action('open-folder', self._open_folder_dialog, ['<Control>f'])
        self._add_action('open-location', self._open_current_media_folder, ['<Control><Shift>o'])
        self._add_action('add-subtitle', self._open_subtitle_dialog, ['<Control>s'])
        self._add_action('toggle-pause', self._toggle_pause, None)
        self._add_action('stop', self.stop_playback, ['<Control>period'])
        self._add_action('play-next', self.play_next, ['<Control>Right'])
        self._add_action('play-previous', self.play_previous, ['<Control>Left'])
        self._add_action('toggle-fullscreen', self._toggle_fullscreen, ['F11'])
        self._add_action('toggle-playlist', self._toggle_playlist, ['<Control>l'])
        self._add_action('about', self._show_about_dialog, None)
        self._add_action('quit', self.close, ['<Control>q'])

    def _setup_shortcuts(self):
        # Menü/eylem sistemiyle temiz biçimde ifade edilemeyen tekil tuşlar
        # (Boşluk, oklar, M, Alt+Enter, Esc) doğrudan tuş denetleyicisiyle ele alınır.
        key_controller = Gtk.EventControllerKey()
        key_controller.connect('key-pressed', self._on_key_pressed)
        self.add_controller(key_controller)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        if alt and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._toggle_fullscreen()
            return True
        if keyval == Gdk.KEY_space:
            self._toggle_pause()
            return True
        if keyval == Gdk.KEY_Escape and self._is_fullscreen:
            self._toggle_fullscreen()
            return True
        if keyval == Gdk.KEY_Left:
            self._seek_relative(-5)
            return True
        if keyval == Gdk.KEY_Right:
            self._seek_relative(5)
            return True
        if keyval == Gdk.KEY_Up:
            self._set_volume(self.volume_scale.get_value() + 5)
            return True
        if keyval == Gdk.KEY_Down:
            self._set_volume(self.volume_scale.get_value() - 5)
            return True
        if keyval in (Gdk.KEY_m, Gdk.KEY_M):
            self._toggle_mute()
            return True
        return False

    # -------------------------------------------------------------
    # GLAREA / MPV RENDER API
    # -------------------------------------------------------------
    def _on_gl_realize(self, area):
        area.make_current()
        if area.get_error() is not None:
            print("GTK GLArea hata:", area.get_error())
            return
        try:
            self._proc_addr_cb = mpv.MpvGlGetProcAddressFn(_mpv_get_proc_address)
            self.render_ctx = mpv.MpvRenderContext(
                self.mpv_player, 'opengl',
                opengl_init_params={'get_proc_address': self._proc_addr_cb}
            )
            self.render_ctx.update_cb = self._on_mpv_render_update
        except Exception as e:
            print("MPV render context oluşturulamadı:", e)
            self.render_ctx = None

    def _on_gl_unrealize(self, area):
        if self.render_ctx is not None:
            try:
                self.render_ctx.free()
            except Exception:
                pass
            self.render_ctx = None

    def _on_mpv_render_update(self):
        GLib.idle_add(self.gl_area.queue_render)

    def _on_gl_render(self, area, ctx):
        # Video oynamıyorsa veya durdurulduysa hiçbir şey çizme (video-area CSS siyahı görünür)
        if self.render_ctx is None or self._is_stopped or not self.current_playing_path:
            return True

        scale = area.get_scale_factor()
        w = int(area.get_width() * scale)
        h = int(area.get_height() * scale)
        if w <= 0 or h <= 0:
            return True

        fbo = _current_fbo()
        try:
            self.render_ctx.render(flip_y=True, opengl_fbo={'w': w, 'h': h, 'fbo': fbo})
        except Exception as e:
            print("MPV render hatası:", e)
        return True

        try:
            self.render_ctx.render(flip_y=True, opengl_fbo={'w': w, 'h': h, 'fbo': fbo})
        except Exception as e:
            print("MPV render hatası:", e)
        return True

    # -------------------------------------------------------------
    # OYNATMA & DOSYA İŞLEMLERİ
    # -------------------------------------------------------------
    def play_file(self, path):
        if not os.path.exists(path):
            return False

        is_audio = path.lower().endswith(self.AUDIO_EXTENSIONS)
        self.logo_picture.set_visible(is_audio)
        self.gl_area.set_visible(not is_audio)  # Ses ise gizli kalsın, video ise aç

        self._is_stopped = False
        self.current_playing_path = path
        self._last_played_path = path
        self._current_duration = 0
        filename = os.path.basename(path)
        self.set_title(f"{filename} - ThisisMyPlayer")
        self.window_title.set_subtitle(filename)
        self._update_time_display(0)

        self.mpv_player.play(path)
        self.mpv_player.pause = False
        self._load_and_fix_subtitles(path)
        return True

    def _open_subtitle_dialog(self):
        if not self.current_playing_path and not self._last_played_path:
            return
        target_path = self.current_playing_path or self._last_played_path
        start_dir = os.path.dirname(os.path.abspath(target_path)) if target_path else os.path.expanduser("~")

        dialog = Gtk.FileDialog()
        dialog.set_title("Altyazı Dosyası Seç")
        dialog.set_initial_folder(Gio.File.new_for_path(start_dir))
        dialog.set_filters(self._build_filter_list(
            [("Altyazı Dosyaları", (".srt", ".vtt", ".ass", ".ssa", ".sub"))]))
        dialog.open(self, None, self._on_subtitle_dialog_ready)

    def _on_subtitle_dialog_ready(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile:
            path = gfile.get_path()
            if path:
                self._load_and_fix_subtitles(path, is_explicit_sub=True)

    def _cleanup_temp_sub(self):
        if self._current_temp_srt:
            if os.path.exists(self._current_temp_srt):
                try:
                    os.remove(self._current_temp_srt)
                except Exception:
                    pass
            self._current_temp_srt = None

    def _load_and_fix_subtitles(self, path, is_explicit_sub=False):
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
            raw_bytes = open(found_srt, 'rb').read()
            try:
                content = raw_bytes.decode('utf-8')
            except UnicodeDecodeError:
                content = raw_bytes.decode('cp1254', errors='replace')

            clean_content = html.unescape(content)
            clean_content = clean_content.replace('&quot;', '"').replace('&apos;', "'").replace('&#39;', "'").replace('&amp;', '&')

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
                return False

            GLib.timeout_add(150, _apply_sub)

        except Exception as e:
            print(f"Altyazı ayrıştırma hatası: {e}")

    def _build_filter_list(self, named_ext_groups):
        """[(isim, (uzantı1, uzantı2, ...)), ...] listesinden Gio.ListStore(Gtk.FileFilter) üretir."""
        flist = Gio.ListStore.new(Gtk.FileFilter)
        for name, exts in named_ext_groups:
            f = Gtk.FileFilter()
            f.set_name(name)
            for ext in exts:
                f.add_pattern(f"*{ext}")
            flist.append(f)
        all_filter = Gtk.FileFilter()
        all_filter.set_name("Tüm Dosyalar")
        all_filter.add_pattern("*")
        flist.append(all_filter)
        return flist

    def _open_file_dialog(self):
        dialog = Gtk.FileDialog()
        dialog.set_title("Medya Dosyası Seç")
        dialog.set_initial_folder(Gio.File.new_for_path(os.path.expanduser("~")))
        dialog.set_filters(self._build_filter_list([
            ("Tüm Medya Dosyaları", self.MEDIA_EXTENSIONS),
            ("Ses Dosyaları", self.AUDIO_EXTENSIONS),
            ("Video Dosyaları", self.VIDEO_EXTENSIONS),
        ]))
        dialog.open(self, None, self._on_open_file_dialog_ready)

    def _on_open_file_dialog_ready(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile:
            path = gfile.get_path()
            if path:
                self.play_file(path)

    def _open_folder_dialog(self):
        dialog = Gtk.FileDialog()
        dialog.set_title("Medya Klasörü Seç")
        dialog.set_initial_folder(Gio.File.new_for_path(os.path.expanduser("~")))
        dialog.select_folder(self, None, self._on_open_folder_dialog_ready)

    def _on_open_folder_dialog_ready(self, dialog, result):
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if gfile:
            folder = gfile.get_path()
            if folder:
                self.add_to_playlist([folder])

    # -------------------------------------------------------------
    # SES, SÜRE VE HIZ KONTROLLERİ
    # -------------------------------------------------------------
    def _toggle_pause(self):
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
        val = max(0, min(100, int(value)))
        if self.mpv_player:
            self.mpv_player.volume = val
        self.volume_label.set_text(f"%{val}")

    def _on_speed_changed(self, dropdown, _pspec):
        idx = dropdown.get_selected()
        if idx < 0 or idx >= len(self.speed_options):
            return
        text = self.speed_options[idx]
        try:
            val = float(text.replace('x', '').strip())
            if self.mpv_player:
                self.mpv_player.speed = val
        except ValueError:
            pass

    def _on_seek_change_value(self, scale, scroll_type, value):
        """Kullanıcı seek çubuğuna tıkladığında veya sürüklediğinde anında tetiklenir."""
        if not self.mpv_player or self._is_stopped or self._current_duration <= 0:
            return False
        
        target_time = max(0, min(self._current_duration, value))
        self._update_time_display(target_time)
        try:
            # MPV'yi anında tıklanan/bırakılan saniyeye zıplat
            self.mpv_player.seek(target_time, reference='absolute', precision='keyframes')
        except Exception as e:
            print(f"Seek hatası: {e}")
        return False

    # -------------------------------------------------------------
    # MPV GERİ ÇAĞIRMALARI (mpv arka plan thread'inden GLib.idle_add ile
    # ana GTK thread'ine güvenli biçimde aktarılır)
    # -------------------------------------------------------------
    def _mpv_pause_changed(self, name, value):
        GLib.idle_add(self._apply_pause_ui, value)

    def _apply_pause_ui(self, value):
        icon = "media-playback-start-symbolic" if value else "media-playback-pause-symbolic"
        self.play_pause_btn.set_icon_name(icon)
        return False

    def _mpv_mute_changed(self, name, value):
        GLib.idle_add(self._apply_mute_ui, value)

    def _apply_mute_ui(self, value):
        icon = "audio-volume-muted-symbolic" if value else "audio-volume-high-symbolic"
        self.mute_btn.set_icon_name(icon)
        return False

    def _mpv_volume_changed(self, name, value):
        if value is not None:
            GLib.idle_add(self._apply_volume_ui, int(value))

    def _apply_volume_ui(self, value):
        self.volume_scale.handler_block(self._volume_scale_handler_id)
        self.volume_scale.set_value(value)
        self.volume_scale.handler_unblock(self._volume_scale_handler_id)
        self.volume_label.set_text(f"%{value}")
        return False

    def _mpv_time_changed(self, name, value):
        if value is not None:
            GLib.idle_add(self._apply_time_ui, value)

    def _apply_time_ui(self, current_seconds):
        if self._is_stopped:
            return False
        self.seek_scale.set_value(current_seconds)
        self._update_time_display(current_seconds)
        return False

    def _mpv_duration_changed(self, name, value):
        if value is not None:
            GLib.idle_add(self._apply_duration_ui, value)

    def _apply_duration_ui(self, value):
        dur = int(value)
        if dur > 0:
            self._current_duration = dur
            self.seek_scale.set_range(0, dur)
            self._update_time_display(self.seek_scale.get_value())
        return False

    def _mpv_eof_changed(self, name, value):
        if value is True:
            GLib.idle_add(self._on_eof_reached)

    def _on_eof_reached(self):
        is_loop_on = self.loop_btn.get_active()

        if self.playlist and self.current_index + 1 < len(self.playlist):
            self.play_next()
            return False

        if is_loop_on:
            if self.playlist:
                self.play_index(0)
            elif self.current_playing_path:
                try:
                    self.mpv_player.seek(0, reference='absolute', precision='exact')
                    self.mpv_player.pause = False
                except Exception:
                    pass
        else:
            self.stop_playback()
        return False

    def _on_loop_toggled(self, button):
        if button.get_active():
            button.set_tooltip_text("Döngü: Açık (Liste Bitince Başa Sar)")
        else:
            button.set_tooltip_text("Döngü: Kapalı")

    def stop_playback(self):
        self._is_stopped = True
        self.current_playing_path = None
        self._current_duration = 0

        if self.mpv_player:
            try:
                self.mpv_player.stop()
            except Exception:
                pass

        self.seek_scale.handler_block(self._seek_scale_handler_id)
        self.seek_scale.set_range(0, 100)
        self.seek_scale.set_value(0)
        self.seek_scale.handler_unblock(self._seek_scale_handler_id)
        self.time_label.set_text("00:00 / 00:00")
        self.play_pause_btn.set_icon_name("media-playback-start-symbolic")

        self.window_title.set_subtitle("")
        self.set_title("ThisisMyPlayer")
        self.logo_picture.set_visible(True)
        self.gl_area.set_visible(False)  # Oynatma durunca video alanını kapat, siyah zemin kalsın

    # -------------------------------------------------------------
    # TAM EKRAN VE AKILLI KONTROL GİZLEME
    # -------------------------------------------------------------
    def _toggle_fullscreen(self):
        now = time.time() * 1000
        if (now - getattr(self, '_last_fs_toggle_time', 0)) < 400:
            return
        self._last_fs_toggle_time = now

        if self._is_fullscreen:
            self.unfullscreen()
            self._is_fullscreen = False
            self.set_cursor(None)
            self.headerbar.set_visible(True)
            self.bottom_panel.set_visible(True)
            self.fullscreen_btn.set_icon_name("view-fullscreen-symbolic")
            if self._controls_hide_timer_id:
                GLib.source_remove(self._controls_hide_timer_id)
                self._controls_hide_timer_id = None
        else:
            self.headerbar.set_visible(False)
            self.fullscreen()
            self._is_fullscreen = True
            self.fullscreen_btn.set_icon_name("view-restore-symbolic")
            self._schedule_hide_controls()

    def _on_pointer_motion(self, controller, x, y):
        if not self._is_fullscreen:
            return
        self.set_cursor(None)
        self.bottom_panel.set_visible(True)
        self._schedule_hide_controls()

    def _schedule_hide_controls(self):
        if not self._is_fullscreen:
            return
        if self._seek_dragging:
            return
        if self._controls_hide_timer_id:
            GLib.source_remove(self._controls_hide_timer_id)
        self._controls_hide_timer_id = GLib.timeout_add(2000, self._hide_controls_fullscreen)

    def _hide_controls_fullscreen(self):
        self._controls_hide_timer_id = None
        if self._is_fullscreen and not self._seek_dragging:
            self.bottom_panel.set_visible(False)
            self.set_cursor(Gdk.Cursor.new_from_name("none"))
        return False

    # -------------------------------------------------------------
    # SÜRÜKLE - BIRAK (DRAG & DROP) DESTEĞİ
    # -------------------------------------------------------------
    def _on_drop(self, drop_target, value, x, y):
        try:
            files = value.get_files()
        except AttributeError:
            files = [value] if isinstance(value, Gio.File) else []
        paths = [f.get_path() for f in files if f.get_path()]
        if paths:
            self.add_to_playlist(paths, clear_existing=True)
        return True

    # -------------------------------------------------------------
    # TEK TIK / ÇİFT TIK (VİDEO ALANI)
    # -------------------------------------------------------------
    def _on_video_click_pressed(self, gesture, n_press, x, y):
        if n_press == 1:
            if self._pending_click_timer_id:
                GLib.source_remove(self._pending_click_timer_id)
            self._pending_click_timer_id = GLib.timeout_add(220, self._on_single_click_timeout)
        elif n_press >= 2:
            if self._pending_click_timer_id:
                GLib.source_remove(self._pending_click_timer_id)
                self._pending_click_timer_id = None
            self._toggle_fullscreen()

    def _on_single_click_timeout(self):
        self._pending_click_timer_id = None
        self._toggle_pause()
        return False

    # -------------------------------------------------------------
    # YARDIMCI METOTLAR
    # -------------------------------------------------------------
    def _format_time(self, seconds):
        seconds = int(seconds or 0)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _update_time_display(self, current_seconds):
        cur_str = self._format_time(current_seconds)
        dur_str = self._format_time(self._current_duration) if self._current_duration > 0 else "00:00"
        self.time_label.set_text(f"{cur_str} / {dur_str}")

    def _open_current_media_folder(self):
        target_path = self.current_playing_path or self._last_played_path
        if target_path and os.path.exists(target_path):
            gfile = Gio.File.new_for_path(target_path)
            launcher = Gtk.FileLauncher.new(gfile)
            launcher.open_containing_folder(self, None, self._on_folder_opened)

    def _on_folder_opened(self, launcher, result):
        try:
            launcher.open_containing_folder_finish(result)
        except GLib.Error as e:
            print(f"Klasör açma hatası: {e}")

    # -------------------------------------------------------------
    # OYNATMA LİSTESİ YÖNETİMİ
    # -------------------------------------------------------------
    def _toggle_playlist(self):
        new_state = not self.playlist_revealer.get_reveal_child()
        self.playlist_revealer.set_reveal_child(new_state)
        if self.playlist_btn.get_active() != new_state:
            self.playlist_btn.set_active(new_state)

    def add_to_playlist(self, paths, clear_existing=False):
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
            child = self.playlist_store.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                self.playlist_store.remove(child)
                child = nxt
            self.current_index = -1

        start_index = len(self.playlist)
        for f in new_files:
            self.playlist.append(f)
            label = Gtk.Label(label=os.path.basename(f), xalign=0)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            row = Gtk.ListBoxRow()
            row.set_child(label)
            self.playlist_store.append(row)

        self.play_index(start_index)

    def play_index(self, index):
        if 0 <= index < len(self.playlist):
            self.current_index = index
            row = self.playlist_store.get_row_at_index(index)
            if row is not None:
                self.playlist_store.select_row(row)
            self.play_file(self.playlist[index])

    def play_next(self):
        if self.playlist and self.current_index + 1 < len(self.playlist):
            self.play_index(self.current_index + 1)

    def play_previous(self):
        if self.playlist and self.current_index - 1 >= 0:
            self.play_index(self.current_index - 1)

    def _on_playlist_row_activated(self, listbox, row):
        self.play_index(row.get_index())

    # -------------------------------------------------------------
    # HAKKINDA PENCERESİ
    # -------------------------------------------------------------
    def _show_about_dialog(self):
        dialog = Gtk.Window(transient_for=self, modal=True, resizable=False,
                             title="ThisisMyPlayer Hakkında")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(18)
        box.set_margin_bottom(16)
        box.set_margin_start(18)
        box.set_margin_end(18)
        dialog.set_child(box)

        if os.path.exists(self.logo_path):
            pic = Gtk.Picture.new_for_filename(self.logo_path)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_size_request(68, 68)
            pic.set_halign(Gtk.Align.CENTER)
            box.append(pic)

        title_label = Gtk.Label()
        title_label.set_use_markup(True)
        title_label.set_markup("<span size='x-large' weight='bold'>ThisisMyPlayer</span>")
        title_label.set_halign(Gtk.Align.CENTER)
        box.append(title_label)

        info_label = Gtk.Label()
        info_label.set_use_markup(True)
        info_label.set_wrap(True)
        info_label.set_justify(Gtk.Justification.LEFT)
        info_label.set_halign(Gtk.Align.START)
        info_label.set_markup(
            "<b>Sürüm:</b> 1.0.0 (GTK4)\n"
            "<b>Lisans:</b> GNU GPLv3\n"
            "<b>Programlama Dili:</b> Python3\n"
            "<b>GUI/UX:</b> GTK4 / libadwaita\n"
            "<b>Geliştirici:</b> A. Serhat KILIÇOĞLU (shampuan)\n"
            "<b>Github:</b> <a href='https://www.github.com/shampuan'>www.github.com/shampuan</a>"
        )
        box.append(info_label)
        box.append(Gtk.Separator())

        desc_label = Gtk.Label()
        desc_label.set_wrap(True)
        desc_label.set_justify(Gtk.Justification.FILL)
        desc_label.set_text(
            "Bu, diğer şişkin alternatiflerine göre çok daha hafif, hızlı ve basit bir video "
            "oynatıcısıdır. Göz yormayan altyazı desteği, otomatik ses optimizasyonu, "
            "iyileştirilmiş kısayol yapısı, sürükle bırak desteği gibi birçok özelliği ile "
            "donatılmıştır."
        )
        box.append(desc_label)

        warranty_label = Gtk.Label()
        warranty_label.set_use_markup(True)
        warranty_label.set_markup("<i><span foreground='#888888'>Bu program hiçbir garanti getirmez.</span></i>")
        box.append(warranty_label)

        copyright_label = Gtk.Label()
        copyright_label.set_use_markup(True)
        copyright_label.set_markup("<b>Telif Hakkı © 2026 - A. Serhat KILIÇOĞLU</b>")
        copyright_label.set_halign(Gtk.Align.CENTER)
        box.append(copyright_label)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        button_row.set_halign(Gtk.Align.END)
        ok_btn = Gtk.Button(label="Tamam")
        ok_btn.connect('clicked', lambda b: dialog.close())
        button_row.append(ok_btn)
        box.append(button_row)

        dialog.present()

    # -------------------------------------------------------------
    # KAPANIŞ TEMİZLİĞİ
    # -------------------------------------------------------------
    def _on_close_request(self, window):
        self._cleanup_temp_sub()
        if self.render_ctx is not None:
            try:
                self.render_ctx.free()
            except Exception:
                pass
            self.render_ctx = None
        if self.mpv_player:
            try:
                self.mpv_player.terminate()
            except Exception:
                pass
        return False


class ThisisMyPlayerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.shampuan.ThisisMyPlayer")

    def do_activate(self):
        win = self.props.active_window
        if not win:
            # Seek/ses çubuklarına tıklandığında imlecin bulunduğu konuma anında
            # atlaması için (Qt sürümündeki ClickableSlider davranışının eşdeğeri).
            # GTK bu noktada (do_activate) tamamen başlatılmış durumdadır.
            settings = Gtk.Settings.get_default()
            if settings is not None:
                try:
                    settings.set_property("gtk-primary-button-warps-slider", True)
                except Exception:
                    pass
            win = ThisisMyPlayerWindow(application=self)
        win.present()


def main():
    app = ThisisMyPlayerApp()
    return app.run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
