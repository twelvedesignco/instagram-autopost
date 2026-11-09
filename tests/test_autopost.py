import json, shutil, sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import autopost as ap


@pytest.fixture
def repo(tmp_path, monkeypatch):
    src = Path(__file__).resolve().parents[1]
    for f in ["config.yaml"]:
        shutil.copy(src / f, tmp_path / f)
    shutil.copytree(src / "media", tmp_path / "media")
    monkeypatch.setattr(ap, "ROOT", tmp_path)
    monkeypatch.setattr(ap, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(ap, "GEN_DIR", tmp_path / "generated")
    monkeypatch.setattr(ap, "LOG_FILE", tmp_path / "posted.json")
    monkeypatch.setattr(ap, "CONFIG_FILE", tmp_path / "config.yaml")
    monkeypatch.setattr(ap, "CSV_FILE", tmp_path / "posts.csv")
    monkeypatch.setattr(ap, "ask_claude", lambda cfg, prompt, n=600: (
        "Short tip: read the definitions section first." if "graphic" in prompt
        else "Caption body.\n\n#policy #law #ontario #compliance #governance #extra #toomany"))
    return tmp_path


def write_csv(repo, rows):
    head = "id,post_at,type,media,caption,topic,graphic_text,alt_text\n"
    (repo / "posts.csv").write_text(head + "\n".join(rows) + "\n")


def ts(delta_h):
    return (datetime.now(ZoneInfo("America/Toronto")) + timedelta(hours=delta_h)).strftime("%Y-%m-%d %H:%M")


def test_schedule_validation(repo, capsys):
    write_csv(repo, [
        f"ok,{ts(1)},image,example-photo.jpg,hi,,,",
        f"bad-type,{ts(1)},tiktok,example-photo.jpg,hi,,,",
        f"reel-no-video,{ts(1)},reel,example-photo.jpg,hi,,,",
        f"empty,{ts(1)},image,,,,,",
        f"bad date,{ts(1)},image,x.jpg,hi,,,",
        f"ok,{ts(2)},image,example-photo.jpg,dupe,,,",
        f"# comment row,,,,,,,",
    ])
    posts = ap.read_schedule(ap.load_config())
    assert [p.id for p in posts] == ["ok"]
    out = capsys.readouterr().out
    for word in ["type must be", "reel needs", "needs media", "id can only", "used twice"]:
        assert word in out


def test_prepare_all_kinds(repo):
    write_csv(repo, [
        f"photo,{ts(1)},image,example-photo.jpg,Given caption,,,",
        f"graphic,{ts(1)},image,,,,Good policy is clear policy.,",
        f"auto,{ts(1)},image,,,reading regulations,,",
        f"wide,{ts(1)},carousel,example-photo.jpg|example-wide.jpg,,topic,,",
        f"story,{ts(1)},story,,,,Tap my profile,",
    ])
    cfg = ap.load_config()
    ap.cmd_prepare(cfg)
    g = repo / "generated"
    d = {p: json.loads((g / p / "post.json").read_text()) for p in ["photo", "graphic", "auto", "wide", "story"]}
    assert d["photo"]["caption"] == "Given caption"
    assert d["auto"]["graphic_text"].startswith("Short tip")
    assert d["auto"]["caption"].count("#") == 5          # hashtags trimmed to limit
    assert d["story"]["caption"] == ""
    # aspect ratios valid
    for p in ["photo", "graphic", "wide"]:
        for m in d[p]["media"]:
            im = Image.open(repo / m)
            assert im.format == "JPEG" and 0.8 - 1e-3 <= im.width / im.height <= 1.91 + 1e-3
    for m in d["wide"]["media"]:
        im = Image.open(repo / m); assert abs(im.width / im.height - 0.8) < 0.01
    s = Image.open(repo / d["story"]["media"][0])
    assert s.size == (1080, 1920)
    # edits to post.json are respected
    data = d["photo"]; data["caption"] = "Edited"; (g / "photo" / "post.json").write_text(json.dumps(data))
    assert ap.prepare(cfg, ap.read_schedule(cfg)[0])["caption"] == "Edited"


def test_lead_time(repo):
    write_csv(repo, [f"soon,{ts(2)},image,example-photo.jpg,x,,,", f"later,{ts(100)},image,example-photo.jpg,x,,,"])
    ap.cmd_prepare(ap.load_config())
    assert (repo / "generated/soon/post.json").exists()
    assert not (repo / "generated/later").exists()


class FakeResp:
    def __init__(self, body, code=200): self._b, self.status_code, self.text = body, code, json.dumps(body)
    def json(self): return self._b


def test_publish_flow(repo, monkeypatch):
    write_csv(repo, [
        f"due,{ts(-0.5)},image,example-photo.jpg,Hello,,,alt words",
        f"car,{ts(-0.2)},carousel,example-photo.jpg|example-wide.jpg,Many,,,",
        f"future,{ts(5)},image,example-photo.jpg,Later,,,",
        f"stale,{ts(-48)},image,example-photo.jpg,Old,,,",
    ])
    calls = []
    def get(url, params=None, timeout=None):
        calls.append(("GET", url, params))
        if url.endswith("/me"): return FakeResp({"user_id": "178", "username": "cat"})
        if "permalink" in params.get("fields", ""): return FakeResp({"permalink": "https://instagram.com/p/x"})
        return FakeResp({"status_code": "FINISHED"})
    n = iter(range(100))
    def post(url, data=None, timeout=None):
        calls.append(("POST", url, data))
        return FakeResp({"id": f"c{next(n)}"})
    monkeypatch.setattr(ap.requests, "get", get)
    monkeypatch.setattr(ap.requests, "post", post)
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("MEDIA_BASE_URL", "https://raw.example/abc/")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    ap.cmd_publish(ap.load_config())
    log = json.loads((repo / "posted.json").read_text())
    assert set(log["posted"]) == {"due", "car"}
    assert log["failed"]["stale"]["error"] == "missed window"
    posts = [c for c in calls if c[0] == "POST"]
    first = posts[0][2]
    assert first["image_url"] == "https://raw.example/abc/generated/due/1.jpg"
    assert first["alt_text"] == "alt words" and first["caption"] == "Hello"
    carousel_parent = [c for c in posts if c[2].get("media_type") == "CAROUSEL"][0][2]
    assert len(carousel_parent["children"].split(",")) == 2
    assert all(c[2].get("is_carousel_item") == "true" for c in posts[2:4])
    assert sum(1 for c in posts if c[1].endswith("/media_publish")) == 2
    # second run posts nothing new
    before = len(calls); ap.cmd_publish(ap.load_config())
    assert not [c for c in calls[before:] if c[0] == "POST"]


def test_publish_waits_for_push_on_github(repo, monkeypatch):
    write_csv(repo, [f"due,{ts(-0.1)},image,example-photo.jpg,Hi,,,"])
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok"); monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(ap.Instagram, "publish", lambda *a: pytest.fail("should wait"))
    ap.cmd_publish(ap.load_config())
    assert "due" not in json.loads((repo / "posted.json").read_text()).get("posted", {})


def test_instagram_error_marks_failure(repo, monkeypatch):
    write_csv(repo, [f"due,{ts(-0.1)},image,example-photo.jpg,Hi,,,"])
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok"); monkeypatch.setenv("MEDIA_BASE_URL", "https://x/")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(ap.requests, "get", lambda *a, **k: FakeResp({"user_id": "1"}))
    monkeypatch.setattr(ap.requests, "post", lambda *a, **k: FakeResp(
        {"error": {"message": "Invalid token", "code": 190}}, 400))
    with pytest.raises(SystemExit):
        ap.cmd_publish(ap.load_config())
    assert "Invalid token" in json.loads((repo / "posted.json").read_text())["failed"]["due"]["error"]
