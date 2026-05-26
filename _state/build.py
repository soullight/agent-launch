#!/usr/bin/env python3
"""
Build state.json for byagentforagent.com.

Runs server-side (no browser-origin restrictions) and queries Solana mainnet
for the canonical AGNT state: balances, contributions, last activity. Output
gets committed to the GH Pages repo so /curve and /proof read it same-origin
and avoid the public-RPC browser-CORS dead zone.

Schedule via GitHub Actions (.github/workflows/refresh-state.yml).
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

# Constants (locked at launch)
RECEIVING_WALLET = "7qzwEWW3XiLyg5A9AbUpHSHvcvNzP56Pm21iuyuqY2m3"
VAULT_ATA        = "37CgnYFMd39oiAjEixWVFFJhBCWVUdxENnY3DSFNf6Ke"
TREASURY_ATA     = "BnSnoc1BqBPezn7A1TFaEL3SACAeFTG3Ssg3cqhY8DVu"
OP_ATA           = "5ET8ZD8f71jCPsjJwgbRtjUxMJk5cBJmSh8e5xgMofMv"
AGNT_MINT        = "2Dgzi3jJbvt59cgiRRRECr9tHagqysGgQAV8fEjKswBJ"
FOUNDER_WALLET   = "Aj3d1vSrgv7feDa41m4wSoi7uMrqtgxVG3VmEjvu4r4Q"
TREASURY_WALLET  = "J8DKiddekoJwSdRHA4Zm4muUKZMhxtPNPrbhXPmeqHhH"
VAULT_WALLET     = "CoPEL3agTFbizRHfHvwHiRdS9aRdDnuRVR79oSftGZf7"

SELF_WALLETS = {FOUNDER_WALLET, TREASURY_WALLET, VAULT_WALLET,
                RECEIVING_WALLET.replace('7qzwEWW3', '7qzwEWW3')}  # explicit

TOTAL_SUPPLY = 1_000_000_000
MAX_SOLD     = 800_000_000  # 80% curve-mintable
BASE         = 2.42e-5
K_FLOOR      = 0.18

RPC_ENDPOINTS = [
    # Primary — Solana's official mainnet-beta. Works server-side (no Origin
    # header → no 403). Tolerates paced ~1-2 req/s but hostile to bursts.
    "https://api.mainnet-beta.solana.com",
    # Free public fallbacks — used only when mainnet-beta 429s the runner.
    # CAUTION (per project memory): public RPCs silently drop BATCH JSONRPC
    # results. This script only does sequential single calls, so the batch
    # gotcha doesn't apply here. If you ever batch in this file, REMOVE these.
    "https://solana-rpc.publicnode.com",
    "https://solana.drpc.org",
]


def rpc(method, params, timeout=15, max_retries=4):
    """Server-side RPC with multi-endpoint fallback + 429-aware backoff.

    Tries every endpoint in sequence on each attempt before backing off; only
    when ALL endpoints in a single pass return 429 do we sleep + retry from
    the top. The original single-endpoint code break'd on 429 and re-tried
    the same hostile endpoint — defeating the point of having fallbacks at
    all. This iteration uses the full endpoint list on every pass."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err = None
    for attempt in range(max_retries):
        all_rate_limited = True
        for url in RPC_ENDPOINTS:
            try:
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json", "User-Agent": "agnt-state-builder/1"}
                )
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read())
                if "error" in d:
                    last_err = d["error"]
                    all_rate_limited = False  # got a real response, just an error result
                    continue
                return d.get("result")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    last_err = f"429 from {url} (attempt {attempt + 1})"
                    continue  # try next endpoint before giving up on this attempt
                last_err = str(e)
                all_rate_limited = False
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = str(e)
                all_rate_limited = False
        # End of endpoint loop without returning
        if all_rate_limited:
            backoff = (2 ** attempt) + 1
            time.sleep(backoff)
            continue  # retry the whole endpoint list
        # At least one endpoint responded with a non-429 error — retrying
        # the same list with the same payload won't fix that. Bail.
        break
    raise RuntimeError(f"all RPC endpoints failed for {method}: {last_err}")


def lamports_to_sol(lamports):
    return lamports / 1_000_000_000


def price_at(u):
    """Curve formula: price(u) = BASE × (K + √u)."""
    import math
    return BASE * (K_FLOOR + math.sqrt(max(0, min(u, MAX_SOLD / TOTAL_SUPPLY))))


def collect_contributions():
    """Walk all signatures on receiving wallet, identify real contributions.
    Caches per-tx JSON to _state/tx_cache/ so cron runs don't re-fetch and
    transient 429s on a single sig can recover on the next run."""
    cache_dir = os.path.join(os.path.dirname(__file__), "tx_cache")
    os.makedirs(cache_dir, exist_ok=True)

    sigs = rpc("getSignaturesForAddress", [RECEIVING_WALLET, {"limit": 1000}])
    contribs = []
    fetch_errors = 0
    if not sigs:
        return contribs

    for i, s in enumerate(sigs):
        if s.get("err"):
            continue
        sig = s["signature"]
        cache_path = os.path.join(cache_dir, sig + ".json")
        tx = None
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    tx = json.load(f)
            except Exception:
                tx = None
        if tx is None:
            time.sleep(1.2)  # mainnet-beta tolerates sustained ≤1/s
            try:
                tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                if tx:
                    with open(cache_path, "w") as f:
                        json.dump(tx, f)
            except Exception as e:
                fetch_errors += 1
                if fetch_errors <= 3:
                    print(f"  tx fetch err for {sig[:12]}…: {e}", file=sys.stderr)
                continue
        if not tx or (tx.get("meta") or {}).get("err"):
            continue

        msg = tx.get("transaction", {}).get("message", {})
        instructions = msg.get("instructions", [])
        # find a SOL transfer where dest == receiving_wallet from a non-self source
        for ix in instructions:
            if not isinstance(ix, dict):
                continue
            parsed = ix.get("parsed")
            if not isinstance(parsed, dict):
                continue  # some parsed instructions are bare strings (memo, etc.)
            if parsed.get("type") != "transfer":
                continue
            info = parsed.get("info", {})
            if info.get("destination") != RECEIVING_WALLET:
                continue
            source = info.get("source")
            if source in SELF_WALLETS:
                continue
            lamports = int(info.get("lamports", 0))
            sol = lamports_to_sol(lamports)
            if sol < 0.001:  # dust filter
                continue
            contribs.append({
                "sig":   sig,
                "from":  source,
                "sol":   sol,
                "ts":    s.get("blockTime") or int(time.time()),
            })
            break

    # Sort chronologically
    contribs.sort(key=lambda c: c["ts"])
    return contribs


def main():
    contribs = collect_contributions()
    total_sol = sum(c["sol"] for c in contribs)
    contributors = len(set(c["from"] for c in contribs))

    # Balances
    try:
        vault = rpc("getTokenAccountBalance", [VAULT_ATA])
        vault_agnt = float(vault["value"]["uiAmountString"])
    except Exception:
        vault_agnt = None
    try:
        op = rpc("getTokenAccountBalance", [OP_ATA])
        op_agnt = float(op["value"]["uiAmountString"])
    except Exception:
        op_agnt = None
    try:
        treasury = rpc("getTokenAccountBalance", [TREASURY_ATA])
        treasury_agnt = float(treasury["value"]["uiAmountString"])
    except Exception:
        treasury_agnt = None

    # Computed: total AGNT minted = MAX_SOLD - (op_agnt + leaks-still-in-vault)
    # Simpler approximation: total dispensed ≈ initial Op funding − current Op balance
    OP_INITIAL = 10_000_000
    if op_agnt is not None:
        net_op_outflow = max(0.0, OP_INITIAL - op_agnt)
    else:
        net_op_outflow = 0
    # Bug leaks returned to vault; subtract those (Vault.ATA gained back what Op leaked)
    if vault_agnt is not None:
        VAULT_AFTER_SETUP = 790_000_000
        leak_returned = max(0.0, vault_agnt - VAULT_AFTER_SETUP)
        total_agnt_minted = max(0.0, net_op_outflow - leak_returned)
    else:
        total_agnt_minted = sum(c.get("agnt", 0) for c in contribs)

    last = contribs[-1] if contribs else None
    fraction_sold = total_agnt_minted / TOTAL_SUPPLY if TOTAL_SUPPLY else 0

    state = {
        "schema_version": 1,
        "updated_at": int(time.time()),
        "receiving_wallet": RECEIVING_WALLET,
        "agnt_mint": AGNT_MINT,
        "total_sol": round(total_sol, 6),
        "total_agnt_minted": round(total_agnt_minted, 4),
        "contributors": contributors,
        "fraction_sold": fraction_sold,
        "fraction_of_curve_filled": (total_agnt_minted / MAX_SOLD) if MAX_SOLD else 0,
        "current_price_per_token": price_at(fraction_sold),
        "current_tokens_per_sol": 1.0 / price_at(fraction_sold) if price_at(fraction_sold) > 0 else 0,
        "sold_out": fraction_sold >= (MAX_SOLD / TOTAL_SUPPLY),
        "vault_agnt_balance": vault_agnt,
        "op_agnt_balance": op_agnt,
        "treasury_agnt_balance": treasury_agnt,
        "last_tx":  last["sig"] if last else None,
        "last_sol": last["sol"] if last else None,
        "last_ts":  last["ts"]  if last else None,
        "contributions": contribs,
        "curve": {
            "base": BASE, "k": K_FLOOR,
            "total_supply": TOTAL_SUPPLY, "max_sold_fraction": MAX_SOLD / TOTAL_SUPPLY,
        },
    }

    out_path = os.environ.get("STATE_OUT", os.path.join(os.path.dirname(__file__), "..", "state.json"))
    out_path = os.path.abspath(out_path)

    # Never downgrade: if a prior run captured contributions we couldn't reach
    # this time (transient 429s), keep the union. Balance-derived fields always
    # come from the fresh on-chain reads.
    if os.path.exists(out_path):
        try:
            with open(out_path) as f:
                prior = json.load(f)
            prior_contribs = prior.get("contributions") or []
            seen = {c["sig"] for c in state["contributions"]}
            for c in prior_contribs:
                if c.get("sig") and c["sig"] not in seen:
                    state["contributions"].append(c)
                    seen.add(c["sig"])
            state["contributions"].sort(key=lambda c: c["ts"])
            state["total_sol"] = round(sum(c["sol"] for c in state["contributions"]), 6)
            state["contributors"] = len(set(c["from"] for c in state["contributions"]))
            if state["contributions"]:
                last = state["contributions"][-1]
                state["last_tx"]  = last["sig"]
                state["last_sol"] = last["sol"]
                state["last_ts"]  = last["ts"]
        except Exception as e:
            print(f"  prior state.json merge skipped: {e}", file=sys.stderr)

    with open(out_path, "w") as f:
        json.dump(state, f, indent=2)
    print(f"wrote {out_path}")
    print(f"  total_sol={state['total_sol']} contributors={state['contributors']} "
          f"total_agnt_minted={state['total_agnt_minted']} last_tx={state['last_tx']}")


if __name__ == "__main__":
    main()
