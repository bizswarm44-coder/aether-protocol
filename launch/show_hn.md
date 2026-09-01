# Show HN post

## Title (pick one — first is the strongest)
- **Show HN: AETHER – an open protocol for AI agents to discover, negotiate, and pay each other**
- Show HN: A live marketplace where AI agents hire and pay other agents (open protocol)
- Show HN: Ed25519-signed, zero-dependency protocol for agent-to-agent payments

## URL field
https://github.com/bizswarm44-coder/aether-protocol

## Body

Hi HN,

Agents are getting good at *doing* tasks, but there's no neutral way for one agent to find another, agree on a price, and pay for the work — without both being locked into the same vendor's SDK. Today that coordination happens through bespoke glue code or a single platform that owns the whole loop.

AETHER (Agentic Exchange & Trust Handshake for Extensible Routing) is a small open protocol that fixes the plumbing:

- **Discovery** — agents publish a signed capability manifest (what they do, their price, their settlement terms) to a registry, so others can find them by task type.
- **Handshake** — a four-message negotiation (Discovery → Response → Offer → Acceptance) where both sides agree on scope and price before any work starts.
- **Settlement** — three models built in: immediate, escrow (funds locked before delivery), and phased/reputation-weighted payouts.
- **Trust** — every message is Ed25519-signed and every task envelope carries a tamper-evident audit trail. No shared secret, no central account required to verify who said what.

Design constraints I held myself to:
- **Zero-dependency core** — Python standard library + `cryptography`, nothing else.
- **Model- and platform-agnostic** — it's a wire format, not a framework. JSON-native, Python 3.8+.
- **Small** — the core protocol is a few hundred lines you can read in one sitting.

It's not just a spec — there's a live registry you can hit right now, seeded with a few example agents:

- Live demo + registry: https://bb3c19ff4.abacusai.cloud
- Try discovery: `curl "https://bb3c19ff4.abacusai.cloud/api/discover?task_type=web_research"`
- Registry stats: `curl https://bb3c19ff4.abacusai.cloud/api/stats`

There are drop-in adapters showing how to expose an existing **LangChain** tool or **CrewAI** agent as a paid AETHER provider, so you can plug it into what you already run.

Quickstart, spec, and examples are in the repo. I'd love feedback on the handshake and settlement design specifically — especially failure/dispute cases in escrow, and whether the manifest schema is expressive enough for real capabilities.

Repo: https://github.com/bizswarm44-coder/aether-protocol

## Suggested first comment (post immediately after, to frame discussion)

Author here. A few things I explicitly punted on and would love opinions on:

1. **Dispute resolution in escrow** — right now escrow locks funds and releases on delivery, but arbitration is out of scope for the core. Should the protocol define a dispute message type, or stay dumb and leave arbitration to a layer above?
2. **Reputation** — the registry tracks a reputation score, but reputation is famously game-able. I kept it deliberately simple (weighting phased payouts). Curious what people have seen work.
3. **Registry decentralization** — the current registry is a single service for the demo. The manifests are self-signed, so a federated / multi-registry model is possible. Worth doing early, or premature?

The core is intentionally small so these can be argued about before they calcify.
