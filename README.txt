Использование
============

1. Установите зависимости:
   pip install -r requirements.txt

2. Создайте папку с медиа (имя укажите в config media_folder, например "media")
   и положите туда фото и видео.
   Поддерживаются: .jpg .png .gif .bmp .webp и .mp4 .avi .mkv .mov .wmv .webm
   Порядок показа — по имени файла.

3. Настройте config.json:
   - stream_url — URL стрима (или ip и port)
   - stream_seconds, video_seconds, image_display_seconds — длительности (сек)
   - media_folder — папка с медиа (относительно папки скрипта)
   - config_check_interval — как часто проверять изменение конфига (сек)
   - display_width, display_height — разрешение окна (например 1024 и 600 или 1920 и 1080)
     Используется для размера окна и чёрного фона при отсутствии кадра.

4. Запуск:
   python stream_and_video.py

   Окно на весь экран. Выход: ESC.

Конфиг перечитывается при сохранении config.json. Видео продолжается с места
остановки при каждом следующем цикле; для каждого видеофайла позиция хранится отдельно.

user - sverk1
password - raspberry