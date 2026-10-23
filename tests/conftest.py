import pytest
from racksdock_subnet.verifier.config import TrustConfig
from racksdock_subnet.verifier.devca import DevCA

from racksdock_platform.marketplace.app import create_app
from racksdock_platform.marketplace.config import Settings


@pytest.fixture
def ca(tmp_path):
    return DevCA.create(tmp_path / "ca")


@pytest.fixture
def settings(tmp_path, ca):
    return Settings(db_url=f"sqlite:///{tmp_path}/m.db", base_url="http://test", chain_kind="mock", chain_file=tmp_path / "chain.json", platform_key_path=tmp_path / "platform.key",
                    probe_timeout_s=1.0, challenge_timeout_s=20.0, trust=TrustConfig(mode="dev", dev_ca_dir=ca.dir))


@pytest.fixture
def app(settings):
    return create_app(settings, background=False)
