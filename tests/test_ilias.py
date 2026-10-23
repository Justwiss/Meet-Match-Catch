from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest
from requests.adapters import BaseAdapter
from requests.models import Response

from conftest import fixture
from studiradar.config import IliasConfig
from studiradar.sources.common import fill_login_form, find_login_form
from studiradar.sources.ilias import (
    IliasClient,
    container_items,
    exercise_assignments,
    forum_signature,
    membership_links,
    parse_obj_link,
)

BASE = "https://www.ilias.uni-koeln.de/ilias"


def test_parse_obj_link_variants():
    assert parse_obj_link("goto_uk_crs_123.html") == ("crs", "123")
    assert parse_obj_link(f"{BASE}/goto_uk_file_55_download.html") == ("file", "55")
    assert parse_obj_link("goto.php?target=exc_77&client_id=uk") == ("exc", "77")
    assert parse_obj_link("goto.php/fold/88") == ("fold", "88")
    assert parse_obj_link("ilias.php?ref_id=9&cmdClass=ilobjforumgui&cmd=showThreads") == ("frm", "9")
    assert parse_obj_link("ilias.php?ref_id=9&cmd=foo") is None
    assert parse_obj_link("javascript:void(0)") is None


def test_container_items_course():
    items = {f"{i.type}:{i.ref_id}": i for i in container_items(fixture("ilias_course.html"), f"{BASE}/goto_uk_crs_100.html", "100")}
    # Menü/Brotkrumen (crs_999, crs_100, cat_1) werden ignoriert
    assert set(items) == {"file:201", "fold:202", "exc:203", "frm:204", "tst:205", "sess:206", "webr:207"}
    f = items["file:201"]
    assert f.title == "Vorlesung 05 – Datenbanken"
    assert "Version: 2" in f.props_text and "Hochgeladen" in f.props_text
    assert "Herunterladen" not in f.title
    assert items["fold:202"].description == "Alle Blätter"
    assert f.url.startswith(BASE)


def test_memberships_skip_mainbar():
    links = membership_links(fixture("ilias_memberships.html"), f"{BASE}/ilias.php")
    titles = {l.title for l in links}
    assert "Wirtschaftsinformatik" in titles and "Tutorium Gruppe 3" in titles
    assert "Zuletzt: Mathe" not in titles
    assert "Mathe für WiWis" in titles


def test_exercise_accordion():
    ass = exercise_assignments(fixture("ilias_exercise.html"))
    assert [a.title for a in ass] == ["Übungsblatt 1: ER-Modellierung", "Übungsblatt 2: SQL"]
    assert ass[0].deadline == "2026-10-30T23:59+01:00"
    assert ass[0].submitted is True and ass[0].ass_id == "5011"
    assert ass[1].deadline == "2026-11-06T12:00+01:00"
    assert ass[1].submitted is False


def test_exercise_ui_framework():
    ass = exercise_assignments(fixture("ilias_exercise_ui.html"))
    assert len(ass) == 1
    assert ass[0].title == "Case Study Abgabe"
    assert ass[0].deadline is not None and "T18:00" in ass[0].deadline
    assert ass[0].submitted is False and ass[0].ass_id == "77"


def test_forum_signature():
    sig, unread = forum_signature("Beiträge: 37 · Ungelesen: 4 · Letzter Beitrag: 22. Okt 2026, 18:02 von Max")
    assert sig == "2026-10-22T18:02+02:00"
    assert unread == 4
    assert forum_signature("Beiträge: 12")[0] == "posts:12"


def test_login_form_filling():
    form = find_login_form(fixture("ilias_login.html"))
    action, data = fill_login_form(form, f"{BASE}/login.php", "jdoe", "geheim")
    assert data["username"] == "jdoe" and data["password"] == "geheim"
    assert data["cmd[doStandardAuthentication]"] == "Anmelden"
    assert data["foo"] == "bar"
    assert action.startswith(f"{BASE}/ilias.php?") and "cmdClass=ilstartupgui" in action


# ---------- kompletter Durchlauf gegen ein simuliertes ILIAS ----------

EMPTY = "<html><body><div id='il_center_col'></div><a href='logout.php'>Logout</a></body></html>"
FOLDER = """<html><body><div id='il_center_col'>
<div class='il_ContainerListItem'><h3><a href='goto_uk_file_301_download.html'>Blatt 3.pdf</a></h3>
<div class='il_ItemProperties'><span class='il_ItemProperty'>Hochgeladen: 20. Okt 2026, 09:00</span></div></div>
</div><a href='logout.php'>Logout</a></body></html>"""


class FakeIlias(BaseAdapter):
    def __init__(self):
        super().__init__()
        self.logged_in = False
        self.calls = []

    def send(self, request, **kwargs):
        url = urlparse(request.url)
        qs = parse_qs(url.query)
        self.calls.append((request.method, request.url))
        body = EMPTY
        if url.path.endswith("login.php"):
            body = fixture("ilias_login.html")
        elif request.method == "POST":
            form = parse_qs(request.body if isinstance(request.body, str) else request.body.decode())
            if form.get("username") == ["jdoe"] and form.get("password") == ["geheim"]:
                self.logged_in = True
                body = fixture("ilias_memberships.html")
            else:
                body = fixture("ilias_login.html").replace("<form", "<div class='alert-danger'>Falsches Passwort</div><form")
        elif not self.logged_in:
            body = fixture("ilias_login.html")
        elif "baseClass" in qs:
            body = fixture("ilias_memberships.html")
        elif "target" in qs:
            target = qs["target"][0]
            body = {
                "crs_100": fixture("ilias_course.html"),
                "fold_202": FOLDER,
                "exc_203": fixture("ilias_exercise.html"),
            }.get(target, EMPTY)
        resp = Response()
        resp.status_code = 200
        resp.raw = BytesIO(body.encode())
        resp.encoding = "utf-8"
        resp.url = request.url
        resp.request = request
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp

    def close(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("studiradar.sources.ilias.time.sleep", lambda s: None)
    cfg = IliasConfig(username="jdoe", password="geheim", base_url=BASE)
    c = IliasClient(cfg, tmp_path)
    fake = FakeIlias()
    c.session.mount("https://", fake)
    c.fake = fake
    return c


def test_full_crawl(client):
    items, errors = client.crawl()
    assert errors == []
    by_id = {i.ext_id: i for i in items}
    assert by_id["file:201"].course == "Wirtschaftsinformatik"
    assert by_id["file:201"].sig.startswith("v2|2026-10-21T10:15")
    assert by_id["file:301"].detail.startswith("Übungsmaterial")
    assert by_id["exc:203:ass:5012"].kind == "deadline"
    assert by_id["exc:203:ass:5012"].due_at == "2026-11-06T12:00+01:00"
    assert by_id["tst:205"].due_at == "2026-11-15T23:59+01:00"
    assert by_id["sess:206"].kind == "event" and by_id["sess:206"].due_at == "2026-10-27T10:00+01:00"
    assert by_id["frm:204"].data["unread"] == 4
    assert "crs:555" in by_id and "grp:101" in by_id
    assert (client.data_path / "ilias_cookies.pkl").exists()


def test_wrong_password(client):
    from studiradar.sources.common import LoginError

    client.cfg.password = "falsch"
    client.cfg.login_method = "local"
    with pytest.raises(LoginError, match="Falsches Passwort"):
        client.login(force=True)
