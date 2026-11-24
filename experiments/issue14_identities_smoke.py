"""Smoke check of the profile/proxy upload routes on a throwaway database."""
import io
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import create_app  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    app = create_app(Path(tmp) / "db.sqlite3")
    client = app.test_client()
    csrf = lambda: re.search(r'name="csrf" value="([^"]+)"', client.get("/settings").get_data(as_text=True))[1]
    r = client.post("/settings/proxies", data={"csrf": csrf(), "bulk": "1.2.3.4:80\nhttp://u:p@5.6.7.8:3128\n1.2.3.4:80"})
    print(r.status_code)
    r = client.post("/settings/profiles", data={"csrf": csrf(), "name": "p1", "files": (io.BytesIO(b'[{"name":"a","value":"b","domain":".yandex.ru"}]'), "acc2.json")}, content_type="multipart/form-data")
    print(r.status_code)
    page = client.get("/settings").get_data(as_text=True)
    print(re.findall(r'role="status">([^<]*)', page), "p1" in page, "acc2" in page, "u:••••@5.6.7.8" in page, ":p@" in page)
    r = client.post("/settings/proxies", data={"csrf": csrf(), "bulk": "bad proxy"})
    print(r.status_code, re.findall(r'role="alert">([^<]*)', r.get_data(as_text=True)))
