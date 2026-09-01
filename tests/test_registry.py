"""Tests for the AETHER Discovery Registry (store + live HTTP round-trip)."""

import threading
import time

from aether import crypto, CapabilityManifest, PriceSchedule, ManifestStore, RegistryClient


def _make_agent(name, task_types, price=1.0, reputation=0.5):
    priv, pub = crypto.generate_keypair()
    manifest = CapabilityManifest(
        agent_id=pub,
        display_name=name,
        task_types=task_types,
        pricing=[PriceSchedule(t, price) for t in task_types],
        reputation=reputation,
    ).sign(priv)
    return priv, manifest


# -- store: publish / verification ----------------------------------------

def test_publish_valid_manifest():
    store = ManifestStore()
    _, m = _make_agent("A", ["research"])
    stored = store.publish(m)
    assert stored.agent_id == m.agent_id
    assert store.get(m.agent_id) is not None


def test_publish_rejects_invalid_signature():
    store = ManifestStore()
    _, m = _make_agent("A", ["research"])
    m.reputation = 0.99  # tamper after signing -> signature no longer valid
    try:
        store.publish(m)
        assert False, "expected ValueError on tampered manifest"
    except ValueError:
        pass
    assert store.get(m.agent_id) is None


def test_latest_version_wins():
    store = ManifestStore()
    priv, m1 = _make_agent("A", ["research"], reputation=0.3)
    store.publish(m1)
    # newer manifest for same agent (higher issued_at) should replace
    m2 = CapabilityManifest(
        agent_id=m1.agent_id,
        display_name="A v2",
        task_types=["research"],
        pricing=[PriceSchedule("research", 1.0)],
        reputation=0.9,
        issued_at=m1.issued_at + 10,
    ).sign(priv)
    store.publish(m2)
    assert store.get(m1.agent_id).reputation == 0.9
    # older manifest must not overwrite the newer one
    store.publish(m1)
    assert store.get(m1.agent_id).reputation == 0.9


# -- store: discovery filtering -------------------------------------------

def test_discover_filters_and_sorts():
    store = ManifestStore()
    _, cheap = _make_agent("Cheap", ["research"], price=0.5, reputation=0.4)
    _, trusted = _make_agent("Trusted", ["research"], price=2.0, reputation=0.9)
    _, other = _make_agent("Other", ["coding"], price=1.0, reputation=0.8)
    for m in (cheap, trusted, other):
        store.publish(m)

    results = store.discover("research")
    assert [m.display_name for m in results] == ["Trusted", "Cheap"]  # rep desc

    # max_price excludes the expensive trusted agent
    results = store.discover("research", max_price=1.0)
    assert [m.display_name for m in results] == ["Cheap"]

    # min_reputation excludes the cheap low-rep agent
    results = store.discover("research", min_reputation=0.5)
    assert [m.display_name for m in results] == ["Trusted"]

    # unknown task type -> nothing
    assert store.discover("translation") == []


def test_stats():
    store = ManifestStore()
    _, a = _make_agent("A", ["research", "coding"], reputation=0.6)
    _, b = _make_agent("B", ["research"], reputation=0.4)
    store.publish(a)
    store.publish(b)
    stats = store.stats()
    assert stats["total_agents"] == 2
    assert stats["task_types"]["research"] == 2
    assert stats["task_types"]["coding"] == 1
    assert stats["distinct_task_types"] == 2
    assert stats["avg_reputation"] == 0.5


def test_persistence_roundtrip(tmp_path=None):
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "reg.json")
    store = ManifestStore(persist_path=path)
    _, m = _make_agent("A", ["research"])
    store.publish(m)
    # a fresh store loading the same file should see the manifest
    reloaded = ManifestStore(persist_path=path)
    assert reloaded.get(m.agent_id) is not None


# -- live HTTP round-trip via the client ----------------------------------

def _start_server():
    import os
    os.environ["AETHER_REGISTRY_DB"] = ""  # in-memory only for this run
    from registry import server
    server.STORE = ManifestStore()  # fresh, non-persistent store
    httpd = server.serve(host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    return httpd, port


def test_http_publish_discover_remove():
    httpd, port = _start_server()
    try:
        client = RegistryClient(f"http://127.0.0.1:{port}")
        priv, m = _make_agent("HttpAgent", ["research"], price=1.0, reputation=0.7)
        client.publish(m)

        found = client.discover("research")
        assert len(found) == 1
        assert found[0].display_name == "HttpAgent"

        one = client.get_agent(m.agent_id)
        assert one is not None and one.agent_id == m.agent_id

        stats = client.stats()
        assert stats["total_agents"] == 1

        # signed self-removal
        assert client.remove(m.agent_id, priv) is True
        assert client.get_agent(m.agent_id) is None
    finally:
        httpd.shutdown()


def test_http_rejects_tampered_manifest():
    httpd, port = _start_server()
    try:
        client = RegistryClient(f"http://127.0.0.1:{port}")
        _, m = _make_agent("Bad", ["research"])
        m.reputation = 0.99  # tamper -> server must reject with 400
        try:
            client.publish(m)
            assert False, "expected RegistryError"
        except Exception as exc:
            assert "400" in str(exc) or "invalid" in str(exc).lower()
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} registry tests passed")
