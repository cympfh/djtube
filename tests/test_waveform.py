from pathlib import Path

from fastapi.testclient import TestClient

from djtube.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_waveforms_are_stacked_above_the_decks():
    client = TestClient(create_app())
    html = client.get("/djtube/").text
    css = (ROOT / "djtube" / "static" / "app.css").read_text(encoding="utf-8")
    app_js = client.get("/djtube/static/app.js").text
    wave_js = client.get("/djtube/static/waveform.js")
    actions = (ROOT / "djtube" / "static" / "actions.js").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    agent = (ROOT / "AGENT.md").read_text(encoding="utf-8")

    assert wave_js.status_code == 200
    assert 'class="wave-stack"' in html
    assert 'id="wave-A"' in html
    assert 'id="wave-B"' in html
    assert 'id="wave-overview-A"' in html
    assert 'id="wave-overview-B"' in html
    assert 'id="wave-note-A"' in html
    assert 'id="wave-note-B"' in html
    assert html.count("波形を準備中") == 2
    assert html.index('id="wave-A"') < html.index('id="wave-B"')
    assert html.index('class="wave-stack"') < html.index('class="deck-row"')
    assert 'class="wave-playhead"' in html
    assert "./waveform.js" in app_js
    assert "paintDeckWaves" in app_js
    assert "peaksFromChannels" in actions
    assert "export const WAVE_HALF_SECONDS = 4" in wave_js.text
    assert "requestAnimationFrame" in app_js

    detail = css.split(".wave-detail {", 1)[1].split("}", 1)[0]
    assert "grid-template-rows: 72px 72px" in detail
    overview = css.split(".wave-overview {", 1)[1].split("}", 1)[0]
    assert "grid-template-rows: 20px 20px" in overview
    mixer = css.split(".mixer {", 1)[1].split("}", 1)[0]
    assert "auto minmax(0, 1fr) auto" in mixer

    short = css.split("@media (max-height: 900px)", 1)[1].split("@media", 1)[0]
    assert "grid-template-rows: 40px 40px" in short
    assert ".wave-overview { display: none; }" in short
    assert "minmax(340px, 1fr)" in short
    assert "上下の縦線が揃うと拍が合っている" in readme
    assert "波形を準備中" in readme
    assert "再生位置" in readme
    assert "今聞こえている位置" not in readme
    assert "長いので波形なし" in readme
    assert "WAVE_HALF_SECONDS" in agent
    assert "10ms" in agent
    assert "長いので波形なし" in agent
    assert "minmax(340px, 1fr)" in agent
    assert "YOUTUBE_API_KEY" not in wave_js.text
