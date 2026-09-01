"""
AETHER Registry Federation Flow — end-to-end demo
=================================================

Shows the *federated* version of AETHER discovery: two independent registries,
A and B, that mirror each other's manifests by gossip. There is no leader and
no global ordering — each registry periodically pulls from its peers, verifies
every signature, and upserts locally with last-writer-wins. Eventual
consistency is all discovery needs.

This script is self-contained: it spins up two in-process registries on random
ports, wires them as peers of each other, and drives the sync manually so the
output is deterministic. Just run it directly:

    python examples/federation_flow.py

Key property demonstrated: an agent that publishes to *one* registry becomes
discoverable through *every* registry in the mesh — and a client pointed at the
whole mesh sees the union, deduped to the newest manifest per agent.
"""

import os
import sys
import threading
import time

# Allow running directly (python examples/federation_flow.py) by putting the
# repo root on the path so both `aether` and `registry` import cleanly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aether import (
    crypto,
    CapabilityManifest,
    PriceSchedule,
    ManifestStore,
    RegistryClient,
)
from registry import server


def banner(text):
    print(f"\n{'=' * 66}\n  {text}\n{'=' * 66}")


def start_registry(store):
    """Start an in-process registry on a random port; return (httpd, url)."""
    httpd = server.serve(host="127.0.0.1", port=0, store=store, start_sync=False)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.1)
    return httpd, f"http://127.0.0.1:{port}"


def main():
    # -- 0. Stand up two independent registries ----------------------------
    store_a, store_b = ManifestStore(), ManifestStore()
    httpd_a, url_a = start_registry(store_a)
    httpd_b, url_b = start_registry(store_b)
    banner("Two independent AETHER registries are live")
    print(f"  Registry A: {url_a}")
    print(f"  Registry B: {url_b}")

    # Each registry pulls from the other -> a two-node mesh. We drive the sync
    # loops by hand (sync_once) for a deterministic demo; in production the
    # background thread does this on AETHER_SYNC_INTERVAL.
    sync_a = server.FederationSync(store_a, [url_b])
    sync_b = server.FederationSync(store_b, [url_a])

    # -- 1. Vega publishes ONLY to Registry A ------------------------------
    vega_priv, vega_pub = crypto.generate_keypair()
    vega = CapabilityManifest(
        agent_id=vega_pub,
        display_name="Vega Research",
        task_types=["market_research"],
        pricing=[PriceSchedule("market_research", 4.0)],
        reputation=0.92,
    ).sign(vega_priv)

    RegistryClient(url_a).publish(vega)
    banner("Vega published to Registry A only")
    print(f"  A knows: {[m.display_name for m in RegistryClient(url_a).discover('market_research')]}")
    print(f"  B knows: {[m.display_name for m in RegistryClient(url_b).discover('market_research')]}")

    # -- 2. B pulls from A: Vega mirrors across ----------------------------
    accepted = sync_b.sync_once()
    banner(f"Registry B synced from A ({accepted} manifest(s) accepted)")
    mirrored = RegistryClient(url_b).discover("market_research")
    print(f"  B now knows: {[m.display_name for m in mirrored]}")
    print(f"  origin tag on B's copy: {store_b.origin_of(vega_pub)!r}  (debug only, never trusted)")

    # -- 3. Lyra publishes ONLY to Registry B ------------------------------
    lyra_priv, lyra_pub = crypto.generate_keypair()
    lyra = CapabilityManifest(
        agent_id=lyra_pub,
        display_name="Lyra Analytics",
        task_types=["market_research"],
        pricing=[PriceSchedule("market_research", 2.5)],
        reputation=0.61,
    ).sign(lyra_priv)
    RegistryClient(url_b).publish(lyra)

    # -- 4. A pulls from B: the mesh is now fully mirrored -----------------
    accepted = sync_a.sync_once()
    banner(f"Registry A synced from B ({accepted} manifest(s) accepted)")
    for label, url in (("A", url_a), ("B", url_b)):
        names = sorted(m.display_name for m in RegistryClient(url).discover("market_research"))
        print(f"  Registry {label} knows: {names}")

    # -- 5. A client pointed at the whole mesh sees the deduped union ------
    banner("Federated client: discover across BOTH registries at once")
    mesh = RegistryClient([url_a, url_b])
    for m in mesh.discover("market_research"):
        price = m.price_for("market_research").amount
        print(f"  - {m.display_name:18} rep={m.reputation:.2f}  ${price:.2f}")
    print("\n(Each agent appears exactly once — deduped to newest issued_at per agent_id.)")

    httpd_a.shutdown()
    httpd_b.shutdown()
    banner("Federation flow complete")


if __name__ == "__main__":
    main()
