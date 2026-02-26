# -*- coding: utf-8 -*-
"""
Чередование стрима с камеры и медиа из папки (фото + видео).
Стрим n сек -> все файлы из папки (фото по N сек, видео по M сек с продолжения) -> снова стрим -> ...
Конфиг перечитывается при изменении config.json. Вывод в полноэкранном режиме.
"""

import os
import sys
import json
import time
import threading

import cv2
import numpy as np
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# Расширения: изображения и видео
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
VIDEO_EXT = {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".webm", ".m4v"}

# Позиция воспроизведения для каждого видеофайла (путь -> кадр)
video_position_by_path = {}

# Текущий индекс медиа в очереди (один файл за раз на video_seconds, потом снова стрим)
media_index = 0

# Зоны клика: левый край = закрепить/снять удержание; правый край = выход
CORNER_ZONE_LEFT = 0.2   # левые 20% = удержание
CORNER_ZONE_RIGHT = 0.35  # правые 35% = выход (шире, чтобы срабатывало на стриме)
CORNER_MIN_PX = 150
WINDOW_NAME = "Stream / Video"

# Логировать каждый клик для отладки (поставь False чтобы отключить)
LOG_ALL_CLICKS = False


def _log(msg):
    """Лог в терминал с временем, сразу в вывод."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _make_mouse_callback(ui_state):
    def _on_mouse(event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        try:
            try:
                rect = cv2.getWindowImageRect(WINDOW_NAME)
                w, h = rect[2], rect[3]
            except Exception:
                w, h = 0, 0
            if w <= 0 or h <= 0:
                w = ui_state.get("display_width", 1024)
                h = ui_state.get("display_height", 600)
            left_thresh = max(w * CORNER_ZONE_LEFT, CORNER_MIN_PX)
            right_thresh = w - max(w * CORNER_ZONE_RIGHT, CORNER_MIN_PX)
            in_left = x <= left_thresh
            in_right = x >= right_thresh
            if LOG_ALL_CLICKS:
                _log(f"КЛИК x={x} y={y} окно w={w} h={h} лево={in_left} право={in_right}")
            if in_right:
                _log("КЛИК: правый край -> выход")
                ui_state["request_exit"] = True
            elif in_left:
                _log("КЛИК: левый край -> закрепить/открепить режим")
                ui_state["request_lock_toggle"] = True
        except Exception:
            pass
    return _on_mouse


# Состояние UI: выход, блокировка режима, текущая фаза, всплывашка режима (5 сек)
ui_state = {
    "request_exit": False,
    "request_lock_toggle": False,
    "lock_mode": None,
    "current_phase": "stream",
    "mode_toast_until": 0,
    "mode_toast_text": "",
}

MODE_TOAST_DURATION = 5.0


def _default_config():
    return {
        "stream_url": "http://192.168.1.58:7777/",
        "stream_seconds": 5,
        "video_seconds": 10,
        "image_display_seconds": 3.0,
        "media_folder": "media",
        "config_check_interval": 2.0,
        "display_width": 1024,
        "display_height": 600,
    }


def _parse_display_size(data):
    """Разрешение из display_width/display_height или resolution (например 1920x1080)."""
    if "display_width" in data and "display_height" in data:
        return int(data["display_width"]), int(data["display_height"])
    res = (data.get("resolution") or "").strip().lower()
    if res == "1920x1080" or res == "1920*1080":
        return 1920, 1080
    if res == "1024x600" or res == "1024*600":
        return 1024, 600
    if "x" in res or "*" in res:
        parts = res.replace("*", "x").split("x")
        if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            return int(parts[0].strip()), int(parts[1].strip())
    return 1024, 600


def load_config():
    """Загружает конфиг из config.json. При ошибке возвращает дефолт — приложение не падает."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "stream_url" in data and data["stream_url"]:
            url = data["stream_url"].rstrip("/") + "/"
        else:
            host = data.get("ip", "192.168.1.58")
            port = data.get("port", 7777)
            url = f"http://{host}:{port}/"
        dw, dh = _parse_display_size(data)
        return {
            "stream_url": url,
            "stream_seconds": int(data.get("stream_seconds", data.get("stream_duration", 5))),
            "video_seconds": int(data.get("video_seconds", data.get("media_duration", 10))),
            "image_display_seconds": float(data.get("image_display_seconds", 3)),
            "media_folder": (data.get("media_folder") or "").strip(),
            "config_check_interval": float(data.get("config_check_interval", 2)),
            "display_width": dw,
            "display_height": dh,
        }
    except Exception:
        return _default_config()


def _get_display_size(ui_state):
    """(width, height) для окна/фона из конфига (ui_state или дефолт 1024x600)."""
    w = ui_state.get("display_width", 1024)
    h = ui_state.get("display_height", 600)
    return max(1, int(w)), max(1, int(h))


def collect_media_from_folder(media_folder, script_dir):
    """
    Сканирует папку, возвращает список (path, "image"|"video") по имени файла.
    При любой ошибке возвращает [] — приложение не падает.
    """
    try:
        if not media_folder:
            return []
        folder = os.path.join(script_dir, media_folder) if not os.path.isabs(media_folder) else media_folder
        if not os.path.isdir(folder):
            return []
        items = []
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in IMAGE_EXT:
                items.append((path, "image"))
            elif ext in VIDEO_EXT:
                items.append((path, "video"))
        return items
    except Exception:
        return []


def get_content_list(config, script_dir):
    """
    Список контента для показа из media_folder (фото + видео).
    Возвращает список (path, "image"|"video").
    """
    return collect_media_from_folder(config.get("media_folder") or "", script_dir)


def get_stream_frame_http(url, timeout=1):
    """Получает один кадр по HTTP GET. Короткий timeout чтобы не копить задержку."""
    try:
        r = requests.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        data = r.content
        if not data:
            return None
        arr = np.frombuffer(data, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def get_stream_frame(config, stream_cap):
    """
    Получает кадр стрима: OpenCV (MJPEG) или HTTP.
    Возвращает None при ошибке/отсутствии стрима — без исключений.
    """
    try:
        if stream_cap is not None and stream_cap.isOpened():
            ret, frame = stream_cap.read()
            if ret and frame is not None:
                return frame
        return get_stream_frame_http(config["stream_url"])
    except Exception:
        return None


def get_latest_stream_frame(config, stream_cap):
    """
    Возвращает самый актуальный кадр: сбрасываем предыдущий, берём свежий.
    При задержках не показываем устаревший кадр.
    """
    frame = get_stream_frame(config, stream_cap)
    if frame is None:
        return None
    newer = get_stream_frame(config, stream_cap)
    return newer if newer is not None else frame


# Таймаут проверки стрима при входе (сек): если за это время нет ни одного кадра — считаем стрим недоступным
STREAM_PROBE_TIMEOUT = 2.5

# Общий буфер и замок для кадра стрима из фонового потока (главный поток только читает и показывает)
_stream_frame_buffer = [None]  # [np.ndarray | None]
_stream_frame_lock = threading.Lock()


def _stream_fetch_worker(config_ref, stream_cap_ref, stop_event):
    """Фоновый поток: получает кадры стрима и кладёт последний в _stream_frame_buffer. Главный поток не блокируется."""
    while not stop_event.is_set():
        try:
            cfg = config_ref[0]
            cap = stream_cap_ref[0]
            frame = get_stream_frame(cfg, cap)
            if frame is not None:
                with _stream_frame_lock:
                    _stream_frame_buffer[0] = frame.copy()
        except Exception:
            pass
        stop_event.wait(timeout=0.03)


def _stream_available(config, stream_cap):
    """Проверка без падения: есть ли хотя бы один кадр стрима."""
    return get_stream_frame(config, stream_cap) is not None


def _check_ui_break(ui_state, stop_event):
    """Проверка кликов: выход или снятие блокировки — прервать текущую фазу."""
    if ui_state.get("request_exit"):
        stop_event.set()
        return True
    if ui_state.get("request_lock_toggle"):
        return True
    return False


def _fit_frame_vertical_crop(frame, window_name, ui_state=None):
    """
    Для вертикального видео: подгонка по высоте, чёрные полосы слева и справа. Без растягивания.
    Размер окна из getWindowImageRect или из ui_state (display_width/height).
    """
    if frame is None or frame.size == 0:
        return frame
    fallback = _get_display_size(ui_state or {}) if ui_state is not None else (1024, 600)
    try:
        rect = cv2.getWindowImageRect(window_name)
        tw, th = rect[2], rect[3]
    except Exception:
        tw, th = fallback
    if tw <= 0 or th <= 0:
        tw, th = fallback
    fh, fw = frame.shape[:2]
    if fh <= fw:
        return frame
    # Вертикальный кадр: подгоняем по высоте, по ширине остаётся место — чёрные полосы слева и справа
    scale = th / fh
    new_h = th
    new_w = int(fw * scale)
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    out = np.zeros((th, tw, 3), dtype=np.uint8)  # чёрный фон
    x0 = (tw - new_w) // 2
    out[:, x0 : x0 + new_w] = resized
    return out


def _composite_to_display_size(frame, ui_state):
    """
    Всегда возвращает кадр ровно display_width x display_height с чёрным фоном.
    Входной кадр масштабируется по размеру (сохраняя пропорции) и центрируется.
    Убирает белые пустые места на RPi и других дисплеях.
    """
    tw, th = _get_display_size(ui_state)
    if frame is None or frame.size == 0:
        return np.zeros((th, tw, 3), dtype=np.uint8)
    fh, fw = frame.shape[:2]
    scale = min(tw / fw, th / fh)
    new_w = int(fw * scale)
    new_h = int(fh * scale)
    if new_w <= 0 or new_h <= 0:
        return np.zeros((th, tw, 3), dtype=np.uint8)
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    out = np.zeros((th, tw, 3), dtype=np.uint8)
    x0 = (tw - new_w) // 2
    y0 = (th - new_h) // 2
    out[y0 : y0 + new_h, x0 : x0 + new_w] = resized
    return out


def _draw_mode_toast(frame, ui_state):
    """Draws 'Mode: Stream' / 'Mode: Media' toast over the frame, hides after 5 sec."""
    if frame is None or frame.size == 0:
        return frame
    until = ui_state.get("mode_toast_until") or 0
    if time.time() >= until:
        return frame
    text = ui_state.get("mode_toast_text") or ""
    if not text:
        return frame
    frame = frame.copy()
    h, w = frame.shape[:2]
    label = f"Mode: {text}"
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(1.2, min(w, h) / 500)
    thick = max(2, int(scale))
    (tw, th), _ = cv2.getTextSize(label, font, scale, thick)
    pad = int(20 * scale)
    x1, y1 = pad, pad
    x2, y2 = x1 + tw + pad * 2, y1 + th + pad * 2
    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 0), -1)
    cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 255, 255), 2)
    cv2.putText(frame, label, (x1 + pad, y1 + th + pad), font, scale, (255, 255, 255), thick, cv2.LINE_AA)
    return frame


def show_stream_phase(config, window_name, config_mtime_ref, stop_event, ui_state, duration_sec=None):
    """
    Показывает стрим duration_sec секунд (или config["stream_seconds"]). Всегда самый актуальный кадр.
    Если стрим недоступен — возвращаем (config, stream_ok=False). При request_exit/request_lock_toggle выходим.
    Возвращает (current_config, stream_was_available).
    """
    duration = duration_sec if duration_sec is not None else config["stream_seconds"]
    _log(f"Фаза стрима: начало, {duration} сек")

    url = config["stream_url"]
    stream_cap = None
    try:
        stream_cap = cv2.VideoCapture(url)
        if not stream_cap.isOpened():
            stream_cap.release()
            stream_cap = None
    except Exception:
        stream_cap = None

    probe_end = time.time() + STREAM_PROBE_TIMEOUT
    stream_ok = False
    while time.time() < probe_end and not stop_event.is_set():
        if _check_ui_break(ui_state, stop_event):
            if stream_cap is not None:
                stream_cap.release()
            _log("Фаза стрима: прервано (клик)")
            return config, False
        if _stream_available(config, stream_cap):
            stream_ok = True
            break
        key = cv2.waitKey(100) & 0xFF
        if key == 27 or key == ord("q") or key == ord("Q"):
            ui_state["request_exit"] = True
            stop_event.set()
            if stream_cap is not None:
                stream_cap.release()
            return config, False

    if not stream_ok:
        if stream_cap is not None:
            stream_cap.release()
        _log("Фаза стрима: недоступен, переход к медиа")
        return config, False

    current_config = config
    # Фоновый поток получает кадры; главный только показывает и обрабатывает клики (waitKey часто)
    stream_fetch_stop = threading.Event()
    config_ref = [current_config]
    stream_cap_ref = [stream_cap]
    with _stream_frame_lock:
        _stream_frame_buffer[0] = None
    fetcher = threading.Thread(
        target=_stream_fetch_worker,
        args=(config_ref, stream_cap_ref, stream_fetch_stop),
        daemon=True,
    )
    fetcher.start()

    end_time = time.time() + duration
    last_cfg_check = time.time()
    check_interval = config["config_check_interval"]
    current_config = config
    last_frame = None
    # Повторно привязываем callback к окну (на Windows при смене контента он может теряться)
    cv2.setMouseCallback(WINDOW_NAME, _make_mouse_callback(ui_state))

    try:
        while time.time() < end_time and not stop_event.is_set():
            # Сначала подкачка событий, чтобы клик «выход» успел попасть в callback
            cv2.waitKey(1)
            if _check_ui_break(ui_state, stop_event):
                _log("Фаза стрима: прервано (клик)")
                break
            with _stream_frame_lock:
                frame = _stream_frame_buffer[0]
            if frame is not None:
                last_frame = frame
            if last_frame is not None:
                disp = _fit_frame_vertical_crop(last_frame, window_name, ui_state)
            else:
                h = ui_state.get("display_height", 600)
                w = ui_state.get("display_width", 1024)
                disp = np.zeros((h, w, 3), dtype=np.uint8)
            disp = _composite_to_display_size(disp, ui_state)
            cv2.imshow(window_name, _draw_mode_toast(disp, ui_state))
            key = cv2.waitKey(30) & 0xFF
            if key == 27 or key == ord("q") or key == ord("Q"):
                ui_state["request_exit"] = True
                stop_event.set()
                break
            if time.time() - last_cfg_check >= check_interval:
                current_config = _check_config_reload(config_mtime_ref, current_config)
                config_ref[0] = current_config
                last_cfg_check = time.time()
    finally:
        stream_fetch_stop.set()
        fetcher.join(timeout=1.0)
        if stream_cap is not None:
            stream_cap.release()

    _log("Фаза стрима: конец")
    return current_config, True


def _check_config_reload(config_mtime_ref, current_config):
    """Проверяет изменение config.json и перезагружает. Возвращает актуальный config."""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
        if mtime != config_mtime_ref[0]:
            config_mtime_ref[0] = mtime
            return load_config()
    except Exception:
        pass
    return current_config


def show_image_item(path, config, window_name, config_mtime_ref, stop_event, ui_state, duration_sec=None):
    """Показывает одно изображение duration_sec или image_display_seconds. Возвращает (config, stopped)."""
    frame = cv2.imread(path)
    if frame is None:
        return config, stop_event.is_set()
    duration = duration_sec if duration_sec is not None else config["image_display_seconds"]
    end_time = time.time() + duration
    last_cfg_check = time.time()
    check_interval = config["config_check_interval"]
    current_config = config

    while time.time() < end_time and not stop_event.is_set():
        cv2.waitKey(1)
        if _check_ui_break(ui_state, stop_event):
            return current_config, True
        disp = _fit_frame_vertical_crop(frame, window_name, ui_state)
        disp = _composite_to_display_size(disp, ui_state)
        cv2.imshow(window_name, _draw_mode_toast(disp, ui_state))
        key = cv2.waitKey(50) & 0xFF
        if key == 27 or key == ord("q") or key == ord("Q"):
            ui_state["request_exit"] = True
            stop_event.set()
            return current_config, True
        if time.time() - last_cfg_check >= check_interval:
            current_config = _check_config_reload(config_mtime_ref, current_config)
            last_cfg_check = time.time()

    return current_config, stop_event.is_set()


def show_video_item(path, config, window_name, config_mtime_ref, stop_event, ui_state):
    """
    Воспроизводит одно видео video_seconds секунд с продолжения с сохранённой позиции.
    Обновляет video_position_by_path[path]. Возвращает (config, stopped).
    """
    global video_position_by_path
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return config, stop_event.is_set()

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    start_frame = min(video_position_by_path.get(path, 0), max(0, total_frames - 1))
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    end_time = time.time() + config["video_seconds"]
    last_cfg_check = time.time()
    check_interval = config["config_check_interval"]
    current_config = config

    while time.time() < end_time and not stop_event.is_set():
        cv2.waitKey(1)
        if _check_ui_break(ui_state, stop_event):
            cap.release()
            return current_config, True
        ret, frame = cap.read()
        if not ret:
            video_position_by_path[path] = 0
            cap.release()
            return current_config, stop_event.is_set()

        video_position_by_path[path] = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
        disp = _fit_frame_vertical_crop(frame, window_name, ui_state)
        disp = _composite_to_display_size(disp, ui_state)
        cv2.imshow(window_name, _draw_mode_toast(disp, ui_state))

        key = cv2.waitKey(max(1, int(1000 / fps))) & 0xFF
        if key == 27 or key == ord("q") or key == ord("Q"):
            ui_state["request_exit"] = True
            stop_event.set()
            cap.release()
            return current_config, True

        if time.time() - last_cfg_check >= check_interval:
            current_config = _check_config_reload(config_mtime_ref, current_config)
            last_cfg_check = time.time()

    cap.release()
    return current_config, stop_event.is_set()


def show_one_media_item(config, window_name, content_list, media_index, config_mtime_ref, stop_event, ui_state):
    """
    Показывает один медиафайл (content_list[media_index]) ровно video_seconds.
    Видео — с продолжения с места остановки; картинка — video_seconds.
    Возвращает (config, stopped, next_media_index). next_media_index: для картинки следующий;
    для видео — следующий если ролик закончился, иначе тот же (продолжим в следующий слот).
    """
    global video_position_by_path
    n = len(content_list)
    if n == 0:
        return config, stop_event.is_set(), 0

    idx = media_index % n
    path, kind = content_list[idx]
    name = os.path.basename(path)
    _log(f"Медиа: показ одного файла ({kind}) {name}, {config['video_seconds']} сек")

    if kind == "image":
        current_config, stopped = show_image_item(
            path, config, window_name, config_mtime_ref, stop_event, ui_state,
            duration_sec=config["video_seconds"],
        )
        next_idx = (idx + 1) % n
        _log(f"Медиа: конец показа картинки, следующий индекс {next_idx}")
        return current_config, stopped, next_idx

    # Видео: video_seconds с продолжения
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        _log(f"Медиа: не удалось открыть видео {name}, переход к следующему")
        return config, stop_event.is_set(), (idx + 1) % n

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    start_frame = min(video_position_by_path.get(path, 0), max(0, total_frames - 1))
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    end_time = time.time() + config["video_seconds"]
    last_cfg_check = time.time()
    check_interval = config["config_check_interval"]
    current_config = config
    video_ended = False

    while time.time() < end_time and not stop_event.is_set():
        cv2.waitKey(1)
        if _check_ui_break(ui_state, stop_event):
            cap.release()
            return current_config, True, idx
        ret, frame = cap.read()
        if not ret:
            video_ended = True
            video_position_by_path[path] = 0
            break
        video_position_by_path[path] = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
        disp = _fit_frame_vertical_crop(frame, window_name, ui_state)
        disp = _composite_to_display_size(disp, ui_state)
        cv2.imshow(window_name, _draw_mode_toast(disp, ui_state))
        key = cv2.waitKey(max(1, int(1000 / fps))) & 0xFF
        if key == 27 or key == ord("q") or key == ord("Q"):
            ui_state["request_exit"] = True
            stop_event.set()
            cap.release()
            return current_config, True, idx
        if time.time() - last_cfg_check >= check_interval:
            current_config = _check_config_reload(config_mtime_ref, current_config)
            last_cfg_check = time.time()

    cap.release()
    next_idx = (idx + 1) % n if video_ended else idx
    _log(f"Медиа: конец показа видео (закончился={video_ended}), следующий индекс {next_idx}")
    return current_config, stop_event.is_set(), next_idx


def show_media_phase(config, window_name, content_list, config_mtime_ref, stop_event, ui_state):
    """
    Показывает все элементы подряд (режим «закрепить медиа»): фото — image_display_seconds,
    видео — video_seconds с продолжения. При request_exit/request_lock_toggle выходит.
    """
    current_config = config
    for path, kind in content_list:
        if stop_event.is_set() or _check_ui_break(ui_state, stop_event):
            break
        if kind == "image":
            current_config, stopped = show_image_item(
                path, current_config, window_name, config_mtime_ref, stop_event, ui_state
            )
            if stopped:
                return current_config
        else:
            current_config, stopped = show_video_item(
                path, current_config, window_name, config_mtime_ref, stop_event, ui_state
            )
            if stopped:
                return current_config
    return current_config


def main():
    global media_index
    os.chdir(SCRIPT_DIR)
    config = load_config()
    config_mtime_ref = [os.path.getmtime(CONFIG_PATH)]
    stop_event = threading.Event()
    _log("Старт: правый край (35%) или Q = выход, левый край = удержание/снять удержание")

    ui_state["display_width"] = config.get("display_width", 1024)
    ui_state["display_height"] = config.get("display_height", 600)
    dw, dh = ui_state["display_width"], ui_state["display_height"]

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, int(dw), int(dh))
    cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    cv2.setMouseCallback(WINDOW_NAME, _make_mouse_callback(ui_state))
    # Сразу показываем чёрный фон (на RPi иначе до первого кадра видно белые поля)
    black = np.zeros((int(dh), int(dw), 3), dtype=np.uint8)
    cv2.imshow(WINDOW_NAME, black)
    cv2.waitKey(1)
    ui_state["mode_toast_until"] = time.time() + MODE_TOAST_DURATION
    ui_state["mode_toast_text"] = "Auto"

    def _black_frame():
        w, h = ui_state.get("display_width", 1024), ui_state.get("display_height", 600)
        return np.zeros((h, w, 3), dtype=np.uint8)

    def apply_click_actions():
        if ui_state.get("request_exit"):
            stop_event.set()
            return
        if ui_state.get("request_lock_toggle"):
            if ui_state["lock_mode"] is None:
                # Одно нажатие — удержание текущего режима
                ui_state["lock_mode"] = ui_state["current_phase"]
                _log(f"Удержание режима: {ui_state['lock_mode']}")
                mode_name = "Stream" if ui_state["lock_mode"] == "stream" else "Media"
                ui_state["mode_toast_until"] = time.time() + MODE_TOAST_DURATION
                ui_state["mode_toast_text"] = mode_name
            else:
                # Следующее нажатие — снять удержание и перейти в обычное чередование, начиная с другого режима
                other = "media" if ui_state["lock_mode"] == "stream" else "stream"
                ui_state["lock_mode"] = None
                ui_state["current_phase"] = other
                _log(f"Снято удержание, далее режим без удержания, начиная с: {other}")
                ui_state["mode_toast_until"] = time.time() + MODE_TOAST_DURATION
                ui_state["mode_toast_text"] = "Auto"
            ui_state["request_lock_toggle"] = False

    try:
        while not stop_event.is_set():
            ui_state["display_width"] = config.get("display_width", 1024)
            ui_state["display_height"] = config.get("display_height", 600)
            apply_click_actions()
            if stop_event.is_set():
                break

            lock = ui_state.get("lock_mode")

            if lock == "stream":
                # Закреплён режим стрима — показываем стрим до повторного нажатия левого низа
                while not stop_event.is_set() and ui_state.get("lock_mode") == "stream":
                    apply_click_actions()
                    if stop_event.is_set():
                        break
                    ui_state["current_phase"] = "stream"
                    config, _ = show_stream_phase(
                        config, WINDOW_NAME, config_mtime_ref, stop_event, ui_state,
                        duration_sec=86400,
                    )
                    if stop_event.is_set() or ui_state.get("request_exit"):
                        break
                    if ui_state.get("request_lock_toggle"):
                        apply_click_actions()
                        break
                continue

            if lock == "media":
                # Закреплён режим медиа — показываем медиа до повторного нажатия левого низа
                content_list = get_content_list(config, SCRIPT_DIR)
                while not stop_event.is_set() and ui_state.get("lock_mode") == "media":
                    apply_click_actions()
                    if stop_event.is_set():
                        break
                    ui_state["current_phase"] = "media"
                    config = show_media_phase(
                        config, WINDOW_NAME, content_list, config_mtime_ref, stop_event, ui_state
                    )
                    if stop_event.is_set() or ui_state.get("request_exit"):
                        break
                    if ui_state.get("request_lock_toggle"):
                        apply_click_actions()
                        break
                    content_list = get_content_list(config, SCRIPT_DIR)
                continue

            try:
                content_list = get_content_list(config, SCRIPT_DIR)
                phase = ui_state.get("current_phase") or "stream"
                if phase == "stream":
                    ui_state["current_phase"] = "stream"
                    config, stream_ok = show_stream_phase(
                        config, WINDOW_NAME, config_mtime_ref, stop_event, ui_state
                    )
                    apply_click_actions()
                    if stop_event.is_set():
                        break
                    if ui_state.get("lock_mode") is not None:
                        continue
                    ui_state["current_phase"] = "media"
                    if not content_list:
                        _log("Медиа: папка пуста, пропуск слота медиа")
                        continue
                    config, stopped, next_idx = show_one_media_item(
                        config, WINDOW_NAME, content_list, media_index, config_mtime_ref, stop_event, ui_state
                    )
                    media_index = next_idx
                    apply_click_actions()
                    if stop_event.is_set():
                        break
                    if ui_state.get("lock_mode") is not None:
                        continue
                else:
                    ui_state["current_phase"] = "media"
                    if not content_list:
                        _log("Медиа: папка пуста, пропуск слота медиа")
                        ui_state["current_phase"] = "stream"
                        continue
                    config, stopped, next_idx = show_one_media_item(
                        config, WINDOW_NAME, content_list, media_index, config_mtime_ref, stop_event, ui_state
                    )
                    media_index = next_idx
                    apply_click_actions()
                    if stop_event.is_set():
                        break
                    if ui_state.get("lock_mode") is not None:
                        continue
                    ui_state["current_phase"] = "stream"
                    config, stream_ok = show_stream_phase(
                        config, WINDOW_NAME, config_mtime_ref, stop_event, ui_state
                    )
                    apply_click_actions()
                    if stop_event.is_set():
                        break
                    if ui_state.get("lock_mode") is not None:
                        continue
            except Exception:
                try:
                    config = _check_config_reload(config_mtime_ref, config)
                    content_list = get_content_list(config, SCRIPT_DIR)
                    if content_list:
                        config = show_media_phase(
                            config, WINDOW_NAME, content_list, config_mtime_ref, stop_event, ui_state
                        )
                except Exception:
                    pass
                time.sleep(1)
    finally:
        cv2.destroyAllWindows()
        _log("Выход")

    return 0


if __name__ == "__main__":
    sys.exit(main())
