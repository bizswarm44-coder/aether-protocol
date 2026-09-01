"""
Tests for AETHER Extension 2 — Registry Federation
==================================================

Covers the store, server endpoints, background sync, and multi-URL client:

* two in-process registries A + B; publish to A, sync, assert B mirrors it;
* an invalid-signature manifest offered by a peer is REJECTED on pull;
* a future-dated manifest (beyond clock skew) is REJECTED on pull;
* a stale manifest (older issued_at) does NOT overwrite a newer one (LWW);
* client-side federated discover merges + dedups across registries;
* federated discover tolerates an unreachable registry;
* store.updated_since() and the /peer/info endpoint.

Tests are hermetic and fast: ephemeral localhost ports, manual sync triggers
(no timers), and clean server teardown.
"""

import threading
import time

from aether import crypto, CapabilityManifest, PriceSchedule, ManifestStore, RegistryClient


# -- helpers ---------------------------------------------------------------

def _signed_manifest(priv, pub, reputation=0.5, issued_at=None,
                     task_types=None, price=1.0, name="Agent"):
    task_types = task_types or ["market_research"]
    m = CapabilityManifest(
        agent_id=pub,
        display_name=name,
        task_types=task_types,
        pricing=[PriceSchedule(t, price) for t in task_types],
        reputation=reputation,
    )
    if issued_at is not None:
        m.issued_at = issued_at  # set BEFORE signing so it is covered
    return m.sign(priv)


def _make_agent(name, task_types, price=1.0, reputation=0.5):
    priv, pub = crypto.generate_keypair()
    return priv, _signed_manifest(priv, pub, reputation, None, task_types, price, name)


def _start(store, peers=None):
    """Start a registry HTTP server on an ephemeral port with its own store."""
    from registry import server
    httpd = server.serve(
        host="127.0.0.1", port=0, store=store, peers=peers or [], start_sync=False
    )
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.1)
    return httpd, f"http://127.0.0.1:{port}"


# -- store: updated_since --------------------------------------------------

def test_updated_since_filters_by_issued_at():
    store = ManifestStore()
    p1, pub1 = crypto.generate_keypair()
    p2, pub2 = crypto.generate_keypair()
    store.publish(_signed_manifest(p1, pub1, issued_at=1000.0, task_types=["x"]))
    store.publish(_signed_manifest(p2, pub2, issued_at=3000.0, task_types=["y"]))

    assert {m.agent_id for m in store.updated_since(0)} == {pub1, pub2}
    assert {m.agent_id for m in store.updated_since(2000)} == {pub2}
    assert store.updated_since(4000) == []


def test_origin_tag_recorded_but_local_is_none():
    store = ManifestStore()
    priv, pub = crypto.generate_keypair()
    store.publish(_signed_manifest(priv, pub))          # local publish
    assert store.origin_of(pub) is None
    store.publish(_signed_manifest(priv, pub, issued_at=time.time() + 1),
                  origin="http://peer-a")                # mirrored publish
    assert store.origin_of(pub) == "http://peer-a"


# -- end-to-end: two registries mirror via pull sync -----------------------

def test_peer_pull_mirrors_manifest():
    store_a, store_b = ManifestStore(), ManifestStore()
    httpd_a, url_a = _start(store_a)
    httpd_b, url_b = _start(store_b, peers=[url_a])
    try:
        priv, m = _make_agent("Orion", ["market_research"], price=4.0, reputation=0.9)
        RegistryClient(url_a).publish(m)

        assert store_b.get(m.agent_id) is None          # not synced yet
        assert httpd_b.sync is not None
        httpd_b.sync.sync_once()                         # deterministic sync

        mirrored = store_b.get(m.agent_id)
        assert mirrored is not None
        assert mirrored.agent_id == m.agent_id
        assert store_b.origin_of(m.agent_id) == url_a    # origin recorded
        # and it is discoverable through a client hitting B
        found = RegistryClient(url_b).discover("market_research")
        assert any(r.agent_id == m.agent_id for r in found)
    finally:
        httpd_a.shutdown()
        httpd_b.shutdown()


def test_peer_info_endpoint():
    store = ManifestStore()
    httpd, url = _start(store, peers=["http://peer-b"])
    try:
        info = RegistryClient(url).peer_info()
        assert info["peers"] == ["http://peer-b"]
        assert isinstance(info["registry_id"], str) and info["registry_id"]
    finally:
        httpd.shutdown()


# -- anti-abuse: rejection on pull -----------------------------------------

def test_invalid_signature_rejected_on_pull(monkeypatch):
    from registry import server
    store = ManifestStore()
    priv, pub = crypto.generate_keypair()
    tampered = _signed_manifest(priv, pub, reputation=0.5, issued_at=time.time())
    tampered.reputation = 0.99                           # tamper AFTER signing
    assert not tampered.verify()

    class _StubClient:
        def __init__(self, *a, **k):
            pass

        def peer_manifests(self, since=0.0):
            return [tampered]

    monkeypatch.setattr(server, "RegistryClient", _StubClient)
    sync = server.FederationSync(store, peers=["http://peer.invalid"],
                                 clock_skew=300, pull_gap=0)
    assert sync.sync_once() == 0
    assert store.get(pub) is None                        # rejected, not stored


def test_future_dated_manifest_rejected_on_pull(monkeypatch):
    from registry import server
    store = ManifestStore()
    priv, pub = crypto.generate_keypair()
    future = _signed_manifest(priv, pub, issued_at=time.time() + 10_000)
    assert future.verify()                               # signature is valid...

    class _StubClient:
        def __init__(self, *a, **k):
            pass

        def peer_manifests(self, since=0.0):
            return [future]

    monkeypatch.setattr(server, "RegistryClient", _StubClient)
    sync = server.FederationSync(store, peers=["http://peer.invalid"],
                                 clock_skew=300, pull_gap=0)
    assert sync.sync_once() == 0                         # ...but too far ahead
    assert store.get(pub) is None


# -- last-writer-wins across the mesh --------------------------------------

def test_stale_manifest_does_not_overwrite_newer():
    store_a, store_b = ManifestStore(), ManifestStore()
    httpd_a, url_a = _start(store_a)
    httpd_b, url_b = _start(store_b, peers=[url_a])
    try:
        priv, pub = crypto.generate_keypair()
        older = _signed_manifest(priv, pub, reputation=0.5, issued_at=1000.0)
        newer = _signed_manifest(priv, pub, reputation=0.9, issued_at=2000.0)

        RegistryClient(url_a).publish(older)             # A holds the OLD one
        store_b.publish(newer)                            # B holds the NEW one
        httpd_b.sync.sync_once()                          # B pulls A's older

        kept = store_b.get(pub)
        assert kept.issued_at == 2000.0                   # newer retained
        assert kept.reputation == 0.9
    finally:
        httpd_a.shutdown()
        httpd_b.shutdown()


def test_newer_manifest_from_peer_replaces_older():
    store_a, store_b = ManifestStore(), ManifestStore()
    httpd_a, url_a = _start(store_a)
    httpd_b, url_b = _start(store_b, peers=[url_a])
    try:
        priv, pub = crypto.generate_keypair()
        older = _signed_manifest(priv, pub, reputation=0.5, issued_at=1000.0)
        newer = _signed_manifest(priv, pub, reputation=0.9, issued_at=2000.0)

        store_b.publish(older)                            # B holds the OLD one
        RegistryClient(url_a).publish(newer)              # A holds the NEW one
        httpd_b.sync.sync_once()                          # B pulls A's newer

        kept = store_b.get(pub)
        assert kept.issued_at == 2000.0                   # replaced with newer
        assert kept.reputation == 0.9
    finally:
        httpd_a.shutdown()
        httpd_b.shutdown()


# -- client-side federated discovery ---------------------------------------

def test_client_federated_discover_merges_and_dedups():
    store_a, store_b = ManifestStore(), ManifestStore()
    httpd_a, url_a = _start(store_a)
    httpd_b, url_b = _start(store_b)
    try:
        # Same agent, different versions on each registry.
        priv, pub = crypto.generate_keypair()
        store_a.publish(_signed_manifest(priv, pub, reputation=0.4,
                                         issued_at=1000.0, task_types=["x"]))
        store_b.publish(_signed_manifest(priv, pub, reputation=0.8,
                                         issued_at=2000.0, task_types=["x"]))
        # A distinct agent only on B.
        priv2, pub2 = crypto.generate_keypair()
        store_b.publish(_signed_manifest(priv2, pub2, reputation=0.6,
                                         issued_at=1500.0, task_types=["x"]))

        results = RegistryClient([url_a, url_b]).discover("x")
        ids = [r.agent_id for r in results]
        assert ids.count(pub) == 1                        # deduped
        merged = next(r for r in results if r.agent_id == pub)
        assert merged.issued_at == 2000.0                 # newest version won
        assert merged.reputation == 0.8
        assert pub2 in ids                                # union includes B-only
    finally:
        httpd_a.shutdown()
        httpd_b.shutdown()


def test_client_discover_tolerates_down_registry():
    store_a = ManifestStore()
    httpd_a, url_a = _start(store_a)
    try:
        priv, m = _make_agent("A", ["x"])
        store_a.publish(m)
        # Second URL is a dead port; discovery must still return A's results.
        fed = RegistryClient([url_a, "http://127.0.0.1:1"], timeout=2.0)
        results = fed.discover("x")
        assert any(r.agent_id == m.agent_id for r in results)
    finally:
        httpd_a.shutdown()


def test_single_url_discover_unchanged():
    """A single-URL client returns the server's ordered results as-is (v0.1)."""
    store = ManifestStore()
    httpd, url = _start(store)
    try:
        p1, m1 = _make_agent("Hi", ["x"], price=5.0, reputation=0.9)
        p2, m2 = _make_agent("Lo", ["x"], price=1.0, reputation=0.3)
        store.publish(m1)
        store.publish(m2)
        results = RegistryClient(url).discover("x")
        # server sorts by reputation desc -> high reputation first
        assert [r.display_name for r in results] == ["Hi", "Lo"]
    finally:
        httpd.shutdown()
