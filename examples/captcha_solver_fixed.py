"""Пример подключаемого решателя SmartCaptcha для агента.

Агент вызывает функцию, указанную в LOCAL_PF_CAPTCHA_SOLVER (``модуль:функция``),
когда встречает задание с картинками. Функция получает словарь ``task``:

* ``type`` — ``"image"``;
* ``kind`` — ``"clicks"`` (нажать объекты) или ``"slider"`` (калейдоскоп с ползунком);
* ``instruction`` — текст задания, например «Нажмите в таком порядке»;
* ``image`` — PNG-снимок области задания (``bytes``);
* ``screenshot`` — путь к сохранённому снимку или ``None``.

Функция возвращает список точек ``[(x, y), ...]`` в CSS-пикселях относительно
левого верхнего угла снимка. Для ``slider`` используется только первая точка:
агент перетащит ползунок к её координате ``x``. Пустой список означает
«модель не уверена», и агент передаёт капчу оператору.

Этот пример не распознаёт изображение: он возвращает точки из переменной
окружения ``LOCAL_PF_CAPTCHA_POINTS`` (``"50,60;200,120"``). Замените тело
функции вызовом своей модели (YOLO, OpenCV и т. п.).

Запуск агента с примером::

    LOCAL_PF_CAPTCHA_SOLVER=examples.captcha_solver_fixed:solve \
    LOCAL_PF_CAPTCHA_POINTS="50,60;200,120" python agent.py
"""
import os


def solve(task):
    raw = os.getenv("LOCAL_PF_CAPTCHA_POINTS", "")
    points = []
    for pair in raw.split(";"):
        if pair.strip():
            x, y = pair.split(",")
            points.append((float(x), float(y)))
    return points
