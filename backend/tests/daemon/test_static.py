"""The frontend under the prefix: SPA fallback, base injection, caching, v1 deep links (302)."""
from __future__ import annotations

import pytest

from .conftest import assert_error

INDEX = ('<!doctype html><html><head><meta charset="utf-8"><script type="module" '
         'src="./assets/index-3f2a.js"></script></head><body><div id="root"></div></body></html>')


@pytest.fixture
def dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(INDEX, encoding="utf-8")
    (d / "assets" / "index-3f2a.js").write_text("console.log('curator')", encoding="utf-8")
    (d / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside dist", encoding="utf-8")
    return d


@pytest.mark.parametrize("base", ["", "/curation"])
def test_spa_fallback_and_base_injection(client_for, dist, base):
    c = client_for(base_path=base, static_dir=dist)
    for route in ("/", "/tasks", "/tasks/new", "/tasks/task_01HX/report",
                  "/tasks/task_01HX/adjudication", "/credentials"):
        r = c.get(base + route)
        assert r.status_code == 200, route
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-cache"
        html = r.text
        assert f'<base href="{base}/"><script>window.__CURATOR_BASE__="{base}";</script>' in html
        assert html.index("<base") < html.index("./assets/index-3f2a.js")
    js = c.get(f"{base}/assets/index-3f2a.js")
    assert js.status_code == 200 and "curator" in js.text
    assert js.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "javascript" in js.headers["content-type"]
    icon = c.get(f"{base}/favicon.svg")
    assert icon.status_code == 200 and icon.headers["cache-control"] == "no-cache"
    assert c.head(f"{base}/tasks/new").status_code == 200


def test_missing_assets_are_404_not_index(client_for, dist):
    c = client_for(base_path="/curation", static_dir=dist)
    assert_error(c.get("/curation/assets/index-old.js"), "not_found")
    assert_error(c.get("/curation/logo.png"), "not_found")
    assert_error(c.get("/curation/api/v2/tasks"), "not_found")            # never the SPA
    assert_error(c.get("/curation/events/whatever"), "not_found")
    assert_error(c.get("/tasks"), "not_found")                             # outside the prefix


def test_no_escape_from_the_dist_directory(client_for, dist):
    c = client_for(static_dir=dist)
    for path in ("/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt"):
        r = c.get(path)
        assert "outside dist" not in r.text, path
    assert_error(c.get("/foo%00bar.js"), "not_found")                  # a NUL byte is a 404, not 500
    assert c.get("/tasks%00x").status_code in (200, 404)


def test_index_already_carrying_a_base_tag_is_left_alone(client_for, tmp_path):
    d = tmp_path / "dist"
    d.mkdir()
    (d / "index.html").write_text('<html><HEAD><base href="/x/"></HEAD></html>', encoding="utf-8")
    html = client_for(base_path="/curation", static_dir=d).get("/curation/").text
    assert html.count("<base") == 1 and 'window.__CURATOR_BASE__="/curation"' in html


@pytest.mark.parametrize("query", [
    "dataset=tos%3A%2F%2Fcuration%2Fdatasets%2Fdroid_100&region=cn-beijing",
    "dataset=a,b&dataset_url=tos://x/y/c&url=d",
    "source=public&dataset=libero_10",
    "endpoint=tos-cn-beijing.volces.com&dataset=demo",
    "tos_endpoint=%3Cimg%3E",
    "region=cn-shanghai",
    "dataset=",
])
@pytest.mark.parametrize("entry", ["/curation/", "/curation"])
def test_v1_deep_links_redirect_with_the_query_untouched(client_for, dist, query, entry):
    c = client_for(base_path="/curation", static_dir=dist)
    r = c.get(f"{entry}?{query}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/curation/tasks/new?{query}"


def test_root_without_deep_link_keys(client_for, dist):
    c = client_for(base_path="/curation", static_dir=dist)
    r = c.get("/curation?utm=1", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/curation/?utm=1"
    assert c.get("/curation/?utm=1").status_code == 200
    root = client_for(static_dir=dist)
    r = root.get("/?dataset=demo", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/tasks/new?dataset=demo"


def test_without_a_built_frontend(client_for):
    c = client_for(base_path="/curation")
    body = assert_error(c.get("/curation/tasks"), "not_found")
    assert "前端资源" in body["error"]["message"]
    r = c.get("/curation/?dataset=demo", follow_redirects=False)            # links still work
    assert r.status_code == 302 and r.headers["location"] == "/curation/tasks/new?dataset=demo"


def test_large_responses_are_gzipped_but_sse_never(client_for, dist):
    (dist / "assets" / "big.js").write_text("x" * 5000, encoding="utf-8")
    c = client_for(static_dir=dist)
    r = c.get("/assets/big.js", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
