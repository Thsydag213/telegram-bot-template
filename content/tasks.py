"""
content/tasks.py — задания бинго. Единственный источник правды.

Отсюда берутся подписи кнопок, описания и типы сдачи (см. config.py)
и начальное наполнение таблицы tasks в базе.

Поля задания:
  task_number      номер (уникальный; порядок кнопок — по порядку в списке)
  emoji, name      подпись кнопки
  description      условие, можно с HTML-разметкой (<b>, <i>)
  submission_type  photo — фото или альбом, text — текст, link — ссылка,
                   photo_text — фото с подписью

Число заданий любое, сетка подстраивается сама.

Описание участник видит ИЗ БАЗЫ. Новые задания досеиваются при старте,
а изменённые здесь описания применяет команда /reseed_tasks. Описания,
которые админы поменяли в боте через /edit_desc, она не трогает
(/reseed_tasks force перезапишет и их).
"""

TASKS_DATA = [
    {
        "task_number": 1,
        "emoji": "📲",
        "name": "Скриншот-Подписка",
        "description": (
            "Подпишись на соцсети организаторов и партнёров ивента "
            "и отправь скриншот подтверждения"
        ),
        "submission_type": "photo",
    },
    {
        "task_number": 2,
        "emoji": "🏢",
        "name": "Partner Corner",
        "description": "Сфотографировать любой предмет с логотипом партнёра, который тебе понравился",
        "submission_type": "photo",
    },
    {
        "task_number": 3,
        "emoji": "🎨",
        "name": "Pinterest aesthetic",
        "description": "Сделать фото вашего рабочего места во время решения кейса с эстетикой",
        "submission_type": "photo",
    },
    {
        "task_number": 4,
        "emoji": "🔗",
        "name": "LETS LINK",
        "description": (
            "Создать новых 15 connections с участниками ивента "
            "и отправить скриншот что контакт установлен"
        ),
        "submission_type": "photo",
    },
    {
        "task_number": 5,
        "emoji": "☕",
        "name": "Кофе-брейк тайм",
        "description": (
            "Сфотографировать свой перекус или стакан кофе на фоне площадки "
            "и выложить его в сторис, отметив нас и продукцию"
        ),
        "submission_type": "photo",
    },
    {
        "task_number": 6,
        "emoji": "⭐",
        "name": "Популярити",
        "description": "Снять сторис и отметить организаторов и партнёров ивента",
        "submission_type": "photo",
    },
    {
        "task_number": 7,
        "emoji": "🌟",
        "name": "Поймать Звезду",
        "description": "Сделать фото с любым организатором ивента",
        "submission_type": "photo",
    },
    {
        "task_number": 8,
        "emoji": "💼",
        "name": "CONNECTION",
        "description": "Во время мероприятия сделать пост в LinkedIn про своё участие в нём",
        "submission_type": "photo",
    },
    {
        "task_number": 9,
        "emoji": "📸",
        "name": "Лицо ивента",
        "description": "Сделать селфи с браслетом участника ивента.",
        "submission_type": "photo",
    },
]
