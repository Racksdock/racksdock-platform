"""The forward migration keeps an old database usable, and evidence storage only tolerates an exact duplicate."""
import pytest
from fastapi.testclient import TestClient
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.verifier.config import TrustConfig
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_api_units import register_node  # pytest puts this directory on sys.path (prepend import mode)

from racksdock_platform.marketplace.app import create_app
from racksdock_platform.marketplace.config import Settings
from racksdock_platform.marketplace.db import Database
from racksdock_platform.marketplace.models import Node
from racksdock_platform.marketplace.services.records import store_envelope


def _rebuild_with_stale_column(db: Database, table: str) -> None:
    """Give an existing table a NOT NULL column the model does not know, the way a removed model field leaves one behind."""
    with db.engine.begin() as conn:
        ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = :t"), {"t": table}).scalar_one()
        conn.execute(text(f"DROP TABLE {table}"))
        conn.execute(text(ddl.replace("PRIMARY KEY", "stale_flag BOOLEAN NOT NULL, \n\tPRIMARY KEY", 1)))


def test_a_stale_not_null_column_is_dropped_so_new_nodes_persist(tmp_path, ca):
    url = f"sqlite:///{tmp_path}/old.db"
    db = Database(url)
    _rebuild_with_stale_column(db, "nodes")
    db.engine.dispose()
    st = Settings(db_url=url, base_url="http://test", chain_kind="mock", chain_file=tmp_path / "chain.json", platform_key_path=tmp_path / "platform.key",
                  trust=TrustConfig(mode="dev", dev_ca_dir=ca.dir))
    app = create_app(st, background=False)
    with app.state.db.engine.connect() as conn:
        assert "stale_flag" not in {r[1] for r in conn.execute(text("PRAGMA table_info('nodes')"))}
    c = TestClient(app)
    rec, r = register_node(c, app.state.chain, SigningKey.generate(), [f"sha256:{i:064x}" for i in range(8)])
    assert r.status_code == 201
    with app.state.db.session() as s:
        assert s.get(Node, rec.node_id) is not None, "a 201 must mean the node row is on disk"


def test_store_envelope_hides_only_an_exact_duplicate(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/m.db")
    env = SignedEnvelope.create("validator_note", {"i": 1}, SigningKey.generate())
    with db.session() as s:
        assert store_envelope(s, env, "validator_note") is True
        assert store_envelope(s, env, "validator_note") is False
    _rebuild_with_stale_column(db, "evidence")  # after the constructor, so the migration has not repaired it
    with pytest.raises(IntegrityError), db.session() as s:
        store_envelope(s, SignedEnvelope.create("validator_note", {"i": 2}, SigningKey.generate()), "validator_note")
