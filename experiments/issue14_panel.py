"""Run an empty panel whose DeepSeek answers are canned, for the issue #14 screenshots.

    python experiments/issue14_panel.py --port 8092

Only the HTTP call to api.deepseek.com is replaced; the request body, the built-in prompt, parsing
and storage run as in the real panel. The database lives in a temporary directory and starts empty.
"""
import argparse
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard import create_app  # noqa: E402
from dashboard.deepseek import DeepSeekClient  # noqa: E402

ANSWER = [
    ("натяжные потолки", "Натяжные потолки", "commercial"),
    ("натяжные потолки цена за м2", "Цены на натяжные потолки", "commercial"),
    ("натяжные потолки под ключ", "Цены на натяжные потолки", "commercial"),
    ("заказать натяжной потолок", "Заказ натяжного потолка", "commercial"),
    ("натяжные потолки москва", "Натяжные потолки в Москве", "local"),
    ("установка натяжных потолков", "Монтаж натяжных потолков", "commercial"),
    ("монтаж натяжного потолка в квартире", "Монтаж натяжных потолков", "commercial"),
    ("матовый или глянцевый натяжной потолок", "Выбор натяжного потолка", "informational"),
    ("сколько сохнет натяжной потолок после монтажа", "Выбор натяжного потолка", "informational"),
    ("натяжные потолки с подсветкой", "Потолки с подсветкой", "commercial"),
]


def opener(request, timeout):
    body = json.loads(request.data)
    print(f"DeepSeek ← model={body['model']}, {len(body['messages'][1]['content'])} символов промпта", flush=True)
    content = json.dumps({"keywords": [
        {"phrase": phrase, "cluster": cluster, "intent": intent} for phrase, cluster, intent in ANSWER
    ]}, ensure_ascii=False)
    return io.BytesIO(json.dumps({
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 512, "completion_tokens": 420, "total_tokens": 932},
    }).encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="local-pf-issue14-") as folder:
        app = create_app(
            Path(folder) / "dashboard.sqlite3",
            deepseek_factory=lambda key, model: DeepSeekClient(key, model, opener=opener),
        )
        app.run(host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
