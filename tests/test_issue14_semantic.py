"""Issue #14: semantic core collection with the built-in AI (DeepSeek) and the ready prompt.

The DeepSeek HTTP API is replaced by a fake ``opener``; the real request body, headers and
answer parsing of ``DeepSeekClient`` are exercised.
"""
from io import BytesIO
import json
import os
import re
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from dashboard import create_app, deepseek
from dashboard.config_import import import_config
from dashboard.deepseek import DeepSeekClient, DeepSeekError, parse_keywords
from dashboard.storage import connect, panel_setting, project_keywords, save_project_settings

from tests.test_dashboard import DashboardCase

NO_ENV = {"YANDEX_API_KEY": "", "YANDEX_FOLDER_ID": "", "LOCAL_PF_NOVNC_URL": "", "DEEPSEEK_API_KEY": ""}
KEY = "sk-0123456789abcdef7777"
ANSWER = {"keywords": [
    {"phrase": "Натяжные потолки цена", "cluster": "Цены", "intent": "commercial"},
    {"phrase": "натяжные потолки цена", "cluster": "Цены", "intent": "commercial"},
    {"phrase": "монтаж натяжных потолков", "cluster": "Монтаж", "intent": "commercial"},
    {"phrase": "какой натяжной потолок лучше", "cluster": "Выбор", "intent": "informational"},
    {"phrase": "ремонт", "cluster": "Ремонт", "intent": "commercial"},
    {"phrase": "натяжные потолки в ванную", "cluster": "Ванная", "intent": "unknown"},
]}


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeDeepSeek:
    """Records requests and answers with the queued chat completion contents."""

    def __init__(self, *contents, finish="stop"):
        self.contents = list(contents)
        self.finish = finish
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, json.loads(request.data), timeout))
        content = self.contents.pop(0)
        if isinstance(content, Exception):
            raise content
        body = {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": self.finish}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 300, "total_tokens": 700}}
        return FakeResponse(json.dumps(body).encode())


def http_error(code, message):
    return HTTPError(deepseek.ENDPOINT, code, "error", {}, BytesIO(json.dumps({"error": {"message": message}}).encode()))


class DeepSeekClientTest(unittest.TestCase):
    def test_request_uses_builtin_prompt_and_json_output(self):
        fake = FakeDeepSeek(json.dumps(ANSWER, ensure_ascii=False))
        client = DeepSeekClient(KEY, "deepseek-flash", opener=fake)
        keywords, usage, prompt = client.collect("master-bobr.ru", "натяжные потолки\nмонтаж", "Москва", 50, "без DIY")
        request, body, timeout = fake.requests[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {KEY}")
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])
        self.assertLessEqual(body["max_tokens"], 8192)
        system, user = body["messages"]
        self.assertEqual((system["role"], system["content"]), ("system", deepseek.SYSTEM_PROMPT))
        self.assertIn("json", system["content"])
        self.assertIn('{"keywords": [{"phrase"', system["content"])
        self.assertEqual(user["content"], prompt)
        for part in ("Сайт: master-bobr.ru", "Регион продвижения: Москва", "натяжные потолки\nмонтаж",
                     "Собери 50 поисковых запросов", "json", "Дополнительные требования: без DIY"):
            self.assertIn(part, prompt)
        self.assertEqual(timeout, deepseek.TIMEOUT)
        self.assertEqual(usage["total_tokens"], 700)
        self.assertEqual([word["phrase"] for word in keywords], [
            "натяжные потолки цена", "монтаж натяжных потолков", "какой натяжной потолок лучше", "ремонт",
            "натяжные потолки в ванную",
        ])
        self.assertEqual(keywords[0], {"phrase": "натяжные потолки цена", "cluster": "Цены", "intent": "commercial"})
        self.assertEqual(keywords[-1]["intent"], "")

    def test_blank_answer_is_retried_once(self):
        fake = FakeDeepSeek("   ", json.dumps(ANSWER))
        keywords, _, _ = DeepSeekClient(KEY, opener=fake).collect("a.ru", "окна", "Москва", 10)
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(len(keywords), 5)
        with self.assertRaisesRegex(DeepSeekError, "не в формате JSON"):
            DeepSeekClient(KEY, opener=FakeDeepSeek(" ", " ")).collect("a.ru", "окна", "Москва", 10)

    def test_errors_are_explained(self):
        cases = [
            (http_error(401, "Authentication Fails"), "неверный DEEPSEEK_API_KEY (Authentication Fails)"),
            (http_error(402, "Insufficient Balance"), "на балансе DeepSeek недостаточно средств"),
            (http_error(400, "Model Not Exist"), "некорректный запрос (Model Not Exist)"),
            (OSError("network down"), "DeepSeek недоступен"),
        ]
        for error, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(DeepSeekError, re.escape(message)):
                DeepSeekClient(KEY, opener=FakeDeepSeek(error)).collect("a.ru", "окна", "Москва", 10)
        with self.assertRaisesRegex(DeepSeekError, "обрезан по длине"):
            DeepSeekClient(KEY, opener=FakeDeepSeek('{"keywords": [{"phrase": "ok', finish="length")).collect(
                "a.ru", "окна", "Москва", 200)
        with self.assertRaisesRegex(DeepSeekError, "DEEPSEEK_API_KEY"):
            DeepSeekClient("")

    def test_parse_keywords(self):
        fenced = '```json\n{"keywords": ["Окна ПВХ", {"phrase": "«окна  цена»", "cluster": " Цены ", "intent": "LOCAL"}]}\n```'
        self.assertEqual(parse_keywords(fenced), [
            {"phrase": "окна пвх", "cluster": "", "intent": ""},
            {"phrase": "окна цена", "cluster": "Цены", "intent": "local"},
        ])
        for content, message in (("[]", "ни одной фразы"), ('{"items": []}', "нет списка keywords"), ("нет", "JSON")):
            with self.subTest(content=content), self.assertRaisesRegex(DeepSeekError, message):
                parse_keywords(content)

    def test_form_validation(self):
        self.assertEqual(deepseek.read_request({"seeds": " окна ", "count": "", "extra": ""}),
                         {"seeds": "окна", "extra": "", "count": 50})
        for form, message in (
            ({"seeds": ""}, "базовые фразы"), ({"seeds": "окна", "count": "9"}, "от 10 до 200"),
            ({"seeds": "окна", "count": "201"}, "от 10 до 200"), ({"seeds": "окна", "count": "abc"}, "от 10 до 200"),
            ({"seeds": "x" * 2001}, "до 2000"),
        ):
            with self.subTest(form=form), self.assertRaisesRegex(ValueError, message):
                deepseek.read_request(form)


class SemanticPanelTest(DashboardCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeDeepSeek()
        self.models = []

        def factory(key, model):
            self.models.append((key, model))
            return DeepSeekClient(key, model, opener=self.fake)

        self.app = create_app(self.path, self.mock_search, deepseek_factory=factory)
        self.client = self.app.test_client()
        env = patch.dict(os.environ, NO_ENV)
        env.start()
        self.addCleanup(env.stop)

    def save_settings(self, **fields):
        data = {"csrf": self.csrf("/settings"), "position_source": "api", **fields}
        self.assertEqual(self.client.post("/settings", data=data).status_code, 303)

    def collect(self, project, **fields):
        data = {"csrf": self.csrf(project + "/semantic"), "seeds": "натяжные потолки", "count": "50", **fields}
        response = self.client.post(project + "/semantic", data=data)
        self.assertEqual(response.status_code, 303)
        return response.location

    def test_settings_store_masked_key_with_env_fallback_and_model(self):
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("ИИ · DeepSeek", page)
        self.assertIn("Ключ не задан.", page)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-from-env-0000aaaa"}):
            self.assertIn("DEEPSEEK_API_KEY из .env: ••••aaaa", self.client.get("/settings").get_data(as_text=True))
        self.save_settings(deepseek_api_key=KEY, deepseek_model="deepseek-v4-pro")
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("••••7777", page)
        self.assertNotIn(KEY, page)
        self.assertIn('value="deepseek-v4-pro"', page)
        self.save_settings(deepseek_model="deepseek-v4-pro")
        with connect(self.path) as db:
            self.assertEqual(panel_setting(db, "deepseek_api_key"), KEY)
        response = self.client.post("/settings", data={
            "csrf": self.csrf("/settings"), "position_source": "api", "deepseek_model": "Bad Model"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Модель DeepSeek", response.get_data(as_text=True))
        self.save_settings(clear_deepseek_key="1")
        with connect(self.path) as db:
            self.assertEqual(panel_setting(db, "deepseek_api_key"), "")

    def test_config_import_accepts_deepseek_key(self):
        config = Path(self.directory.name) / "panel.yaml"
        config.write_text(f"settings:\n  deepseek_api_key: {KEY}\n  deepseek_model: deepseek-flash\n", encoding="utf-8")
        import_config(self.path, config)
        with connect(self.path) as db:
            self.assertEqual((panel_setting(db, "deepseek_api_key"), panel_setting(db, "deepseek_model")),
                             (KEY, "deepseek-flash"))

    def test_collect_review_and_add_phrases(self):
        project = self.create_project()
        page = self.client.get(project + "/semantic").get_data(as_text=True)
        self.assertIn("Сбор ядра (ИИ)", page)
        self.assertIn("Укажите DEEPSEEK_API_KEY", page)
        self.assertIn("Встроенный промпт", page)
        location = self.collect(project)
        self.assertIn("Укажите DEEPSEEK_API_KEY в настройках панели.", self.client.get(location).get_data(as_text=True))
        self.assertEqual(self.fake.requests, [])

        self.save_settings(deepseek_api_key=KEY)
        self.fake.contents.append(json.dumps(ANSWER, ensure_ascii=False))
        location = self.collect(project, seeds="натяжные потолки\nмонтаж", count="30")
        self.assertEqual(self.models, [(KEY, "deepseek-flash")])
        prompt = self.fake.requests[0][1]["messages"][1]["content"]
        self.assertIn("Сайт: master-bobr.ru", prompt)
        self.assertIn("Регион продвижения: Москва", prompt)
        self.assertIn("Собери 30 поисковых запросов", prompt)
        page = self.client.get(location).get_data(as_text=True)
        self.assertIn("предложил фраз: 5", page)
        self.assertIn("монтаж натяжных потолков", page)
        self.assertIn("Коммерческий", page)
        self.assertIn("ремонт</label> <span class=\"muted\">· уже в проекте", page)
        self.assertRegex(page, r'name="pick" value="3" id="pick-3" disabled')
        self.assertIn("натяжные потолки\nмонтаж</textarea>", page)

        run_id = int(location.rsplit("=", 1)[1])
        add = f"{project}/semantic/{run_id}/add"
        response = self.client.post(add, data={"csrf": self.csrf(location), "pick": []})
        self.assertIn("Отметьте фразы", self.client.get(response.location).get_data(as_text=True))
        response = self.client.post(add, data={"csrf": self.csrf(location), "pick": ["0", "1", "3", "4", "99", "x"]})
        self.assertEqual(response.status_code, 303)
        self.assertIn("Добавлено фраз в проект: 3. Уже были в проекте: 1.",
                      self.client.get(response.location).get_data(as_text=True))
        with connect(self.path) as db:
            words = project_keywords(db, 1)
        self.assertEqual([(w["phrase"], w["hint_type"], w["brand"], w["cluster"]) for w in words[4:]], [
            ("натяжные потолки цена", "domain", "", "Цены"),
            ("монтаж натяжных потолков", "domain", "", "Монтаж"),
            ("натяжные потолки в ванную", "domain", "", "Ванная"),
        ])
        self.assertEqual(len(words), 7)
        keywords_page = self.client.get(project + "/keywords").get_data(as_text=True)
        self.assertIn("монтаж натяжных потолков", keywords_page)
        self.assertEqual(self.client.post(f"{project}/semantic/999/add",
                                          data={"csrf": self.csrf(location)}).status_code, 404)

    def test_brand_refinement_and_project_limit(self):
        project = self.create_project()
        with connect(self.path) as db:
            save_project_settings(db, 1, {"not_found": "brand", "brand_queries": "Мастер Бобр\nБобр"})
            db.executemany("INSERT INTO keywords VALUES (1, ?, ?, '', 'domain', '')",
                           [(ordinal, f"фраза {ordinal}") for ordinal in range(4, 98)])
        self.save_settings(deepseek_api_key=KEY)
        self.fake.contents.append(json.dumps(ANSWER, ensure_ascii=False))
        location = self.collect(project)
        add = f"{project}/semantic/1/add"
        response = self.client.post(add, data={"csrf": self.csrf(location), "pick": ["0", "1", "2", "4"]})
        self.assertIn("Добавлено фраз в проект: 2. Не добавлено из-за лимита 100 фраз на проект: 2.",
                      self.client.get(response.location).get_data(as_text=True))
        with connect(self.path) as db:
            words = project_keywords(db, 1)
        self.assertEqual(len(words), 100)
        self.assertEqual([(w["phrase"], w["hint_type"], w["brand"]) for w in words[-2:]], [
            ("натяжные потолки цена", "brand", "Мастер Бобр"), ("монтаж натяжных потолков", "brand", "Мастер Бобр"),
        ])

    def test_deepseek_error_keeps_form(self):
        project = self.create_project()
        self.save_settings(deepseek_api_key=KEY)
        self.fake.contents.append(http_error(402, "Insufficient Balance"))
        location = self.collect(project, seeds="окна пвх", count="40")
        page = self.client.get(location).get_data(as_text=True)
        self.assertIn("DeepSeek: на балансе DeepSeek недостаточно средств (Insufficient Balance).", page)
        self.assertIn("окна пвх</textarea>", page)
        self.assertIn('value="40"', page)
        location = self.collect(project, count="5")
        self.assertIn("Количество фраз — число от 10 до 200.", self.client.get(location).get_data(as_text=True))
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM semantic_runs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
