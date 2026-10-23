"""The sitemap names the console's public pages and one page per listed server, nothing unlisted."""
from fastapi.testclient import TestClient
from racksdock_subnet.core.keys import SigningKey
from test_api_units import enroll_validator, qualify, register_node


def test_sitemap_lists_home_miner_and_listed_servers_only(app):
    c = TestClient(app)
    chain = app.state.chain
    listed, _ = register_node(c, chain, SigningKey.generate(), [f"sha256:{i:064x}" for i in range(8)], hotkey="5Fa")
    unlisted, _ = register_node(c, chain, SigningKey.generate(), [f"sha256:{i:064x}" for i in range(100, 108)], hotkey="5Fb")
    vkey = SigningKey.generate()
    key = enroll_validator(c, vkey)
    assert qualify(c, vkey, key, listed).status_code == 201
    r = c.get("/v1/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert "<loc>http://test/</loc>" in r.text and "<loc>http://test/miner</loc>" in r.text
    assert f"<loc>http://test/nodes/{listed.node_id}</loc>" in r.text
    assert unlisted.node_id not in r.text
