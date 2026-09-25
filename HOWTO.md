```bash
cd photo-dedup

# 1. Хост-корень фото + настройки (создайте СВОИМ пользователем, см. §7 про права)
cp .env.example .env          # PHOTOS_ROOT=./photos — или ваш путь, напр. /home/me/myfotos
cp settings.example.toml settings.toml   # в [paths] укажите пути КАК НА ХОСТЕ:
                                         # src = "/home/me/myfotos/src"
mkdir -p photos/src photos/trash
# положите фотографии в photos/src (для быстрого теста можно сгенерировать набор, см. ниже)

# 2. Сборка и запуск БД + UI
docker compose build
docker compose up -d db web

docker compose up -d  web
# UI: http://localhost:9501 — запускать движок можно кнопками на вкладке «Монитор»

# 3. Тестовый набор с поворотами/отражениями (критерий приёмки №2):
docker compose run --rm --entrypoint python engine scripts/make_testset.py /photos/src/testset 5

# 4. Полный цикл: scan → analyze → move (с Resume)
docker compose run --rm engine run

# 5. Статус / мягкая остановка / возобновление
docker compose run --rm engine status
docker compose run --rm engine stop
docker compose run --rm engine run          # продолжит с места остановки

# 6. Остановить фоновые сервисы (данные в volume pgdata сохранятся)
docker compose down
```

Примечание о `stop`: флаг действует на **работающий** движок — он доделает текущий
файл/батч и остановится. При старте следующей рабочей команды флаг сбрасывается,
поэтому устаревший флаг не помешает будущим запускам.

Движок можно запускать и при закрытом UI, и поэтапно:

```bash
docker compose run --rm engine scan                     # только индексация
docker compose run --rm engine analyze                  # только поиск групп
docker compose run --rm engine move --dry-run           # план переноса
docker compose run --rm engine move                     # перенос (auto-режим)
docker compose run --rm engine move --move-mode manual  # только подтверждённые группы
docker compose run --rm engine run --dry-run --threads 4 --threshold 6
```