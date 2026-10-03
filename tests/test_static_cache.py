from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from djtube.app import create_app
from djtube.assets import asset_version, stamp_module_specifiers
from djtube.paths import PUBLIC_PREFIX, STATIC_DIR

ROOT = Path(__file__).resolve().parents[1]


def test_build_token_is_stable_and_stamps_only_relative_imports():
    assert asset_version() == asset_version()
    source = 'import { setFilter } from "./actions.js";\nconst note = "from \\"./actions.js\\"";\n'
    stamped = stamp_module_specifiers(source, "abc")
    assert 'from "./actions.js?v=abc"' in stamped
    assert stamped.count("?v=abc") == 1
    assert "setFilter" in stamped
    assert stamp_module_specifiers(stamped, "abc") == stamped


def test_reload_uses_one_build_for_the_module_graph():
    client = TestClient(create_app())
    version = asset_version()
    page = client.get(f"{PUBLIC_PREFIX}/")
    assert page.headers["cache-control"] == "no-cache"
    html = page.text
    script = re.search(r'src="([^"]+/static/app\.js\?v=([0-9a-f]+))"', html)
    style = re.search(r'href="([^"]+/static/app\.css\?v=([0-9a-f]+))"', html)
    assert script is not None and style is not None
    assert script.group(2) == style.group(2) == version

    seen: dict[str, str] = {}

    def load(url: str) -> None:
        if url in seen:
            return
        response = client.get(url)
        assert response.status_code == 200, url
        seen[url] = response.text
        if ".js" not in url:
            return
        assert response.headers["cache-control"].startswith("public")
        assert "immutable" in response.headers["cache-control"]
        for spec in re.findall(r"""(?:from|import)\s*\(?\s*['"](\./[^'"]+)['"]""", response.text):
            assert spec.endswith(f".js?v={version}"), spec
            load(f"{PUBLIC_PREFIX}/static/{spec[2:]}")

    load(script.group(1))
    actions_url = f"{PUBLIC_PREFIX}/static/actions.js?v={version}"
    player_url = f"{PUBLIC_PREFIX}/static/player.js?v={version}"
    filter_url = f"{PUBLIC_PREFIX}/static/filter.js?v={version}"
    assert actions_url in seen
    assert player_url in seen
    assert filter_url in seen
    assert "setFilter," in seen[actions_url]
    assert "actions.setFilter" in seen[f"{PUBLIC_PREFIX}/static/app.js?v={version}"]

    for name in ("app.js", "actions.js", "player.js", "filter.js"):
        served = seen[f"{PUBLIC_PREFIX}/static/{name}?v={version}"]
        assert served.replace(f"?v={version}", "") == (STATIC_DIR / name).read_text(encoding="utf-8")

    stale = client.get(f"{PUBLIC_PREFIX}/static/app.js?v=stale")
    assert stale.headers["cache-control"] == "no-cache"
    assert f'from "./actions.js?v={version}"' in stale.text
    assert 'from "./actions.js"' not in stale.text

    plain = client.get(f"{PUBLIC_PREFIX}/static/app.js")
    assert plain.headers["cache-control"] == "no-cache"
    assert f'"./actions.js?v={version}"' in plain.text

    etag = client.get(f"{PUBLIC_PREFIX}/static/actions.js?v={version}").headers["etag"]
    again = client.get(f"{PUBLIC_PREFIX}/static/actions.js?v={version}", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["cache-control"].startswith("public")
