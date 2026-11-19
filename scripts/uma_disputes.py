#!/usr/bin/env python3
"""Query UMA Optimistic Oracle contracts on Polygon for DisputePrice events.

  scan      pull DisputePrice logs for a block/date range -> JSONL (resumable)
  discover  find which contracts emitted disputes in known dispute tx hashes
  compare   check on-chain output against dispute_tx_hash in the raw dataset

Needs a Polygon JSON-RPC endpoint (--rpc or env POLYGON_RPC_URL).
"""
import argparse, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from Crypto.Hash import keccak
from eth_abi import decode

# Default oracles. MOOV2 is listed in UMA's docs. OOv2 is from memory: confirm it
# with `discover` before trusting a full scan (override with --oracle).
ORACLES = {
    "MOOV2": "0x2C0367a9DB231dDeBd88a94b4f6461a6e47C58B1",
    "OOv2": "0xeE3Afe347D5C74317041E2618C49534dAf887c24",
}
DISPUTE_SIG = ("DisputePrice(address,address,address,bytes32,uint256,bytes,int256)")


def k256(b: bytes) -> str:
    return "0x" + keccak.new(digest_bits=256, data=b).hexdigest()


DISPUTE_TOPIC = k256(DISPUTE_SIG.encode())


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self, url):
        self.url, self.s = url, requests.Session()

    def call(self, method, params, retries=6):
        for i in range(retries):
            try:
                r = self.s.post(self.url, timeout=60, json={
                    "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                if r.status_code in (429, 502, 503, 504):
                    raise requests.RequestException(r.status_code)
                r.raise_for_status()
                j = r.json()
                if "error" in j:
                    raise RpcError(j["error"])
                return j["result"]
            except requests.RequestException:
                if i == retries - 1:
                    raise
                time.sleep(2 ** i)

    def block_ts(self, n):
        return int(self.call("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)


def block_at(rpc, ts):
    """First block whose timestamp >= ts (binary search)."""
    lo, hi = 1, int(rpc.call("eth_blockNumber", []), 16)
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (mid + 1, hi) if rpc.block_ts(mid) < ts else (lo, mid)
    return lo


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_ts(s):
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def decode_dispute(log):
    t = log["topics"]
    ident, req_ts, anc, price = decode(
        ["bytes32", "uint256", "bytes", "int256"], bytes.fromhex(log["data"][2:]))
    return {
        "oracle": log["address"].lower(),
        "block_number": int(log["blockNumber"], 16),
        "tx_hash": log["transactionHash"],
        "log_index": int(log["logIndex"], 16),
        "requester": "0x" + t[1][-40:],
        "proposer": "0x" + t[2][-40:],
        "disputer": "0x" + t[3][-40:],
        "identifier": ident.rstrip(b"\0").decode(errors="replace"),
        "request_timestamp": req_ts,
        "proposed_price": str(price),
        "question_id": k256(anc),  # Polymarket's UMA adapter uses keccak256(ancillaryData)
        "ancillary_text": anc.decode("utf-8", errors="replace"),
    }


def scan_logs(rpc, addrs, start, end, step, max_step):
    b = start
    while b <= end:
        e = min(b + step - 1, end)
        try:
            logs = rpc.call("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(e),
                                             "address": addrs, "topics": [DISPUTE_TOPIC]}])
        except RpcError:  # range/result limit: shrink and retry
            if step == 1:
                raise
            step = max(step // 2, 1)
            continue
        yield b, e, logs
        b, step = e + 1, min(step * 2, max_step)


def cmd_scan(a):
    rpc = Rpc(a.rpc)
    oracles = dict(o.split("=", 1) for o in a.oracle) if a.oracle else ORACLES
    addrs = list(oracles.values())
    ckpt = a.out + ".checkpoint"
    seen = set()
    if a.resume and os.path.exists(ckpt):
        start = int(open(ckpt).read()) + 1
        if os.path.exists(a.out):
            seen = {(r["tx_hash"], r["log_index"]) for r in map(json.loads, open(a.out))}
    else:
        start = a.from_block if a.from_block is not None else block_at(rpc, date_ts(a.from_date))
        open(a.out, "w").close()
    end = a.to_block if a.to_block is not None else (
        block_at(rpc, date_ts(a.to_date)) if a.to_date else int(rpc.call("eth_blockNumber", []), 16))
    print(f"scanning blocks {start}-{end} on {oracles}", file=sys.stderr)

    n = 0
    with open(a.out, "a") as f:
        for b, e, logs in scan_logs(rpc, addrs, start, end, a.chunk, a.chunk):
            recs = [decode_dispute(l) for l in logs]
            recs = [r for r in recs if (r["tx_hash"], r["log_index"]) not in seen]
            blocks = sorted({r["block_number"] for r in recs})
            with ThreadPoolExecutor(8) as ex:
                ts = dict(zip(blocks, ex.map(rpc.block_ts, blocks)))
            for r in recs:
                r["dispute_time"] = utc(ts[r["block_number"]])
                f.write(json.dumps(r) + "\n")
            f.flush()
            open(ckpt, "w").write(str(e))
            n += len(recs)
            print(f"  {b}-{e}: {len(recs)} disputes (total {n})", file=sys.stderr)
    print(f"done: {n} new dispute events -> {a.out}", file=sys.stderr)


def cmd_discover(a):
    """Which contract addresses emit DisputePrice in these txs? Use to confirm --oracle."""
    rpc = Rpc(a.rpc)
    txs = a.tx
    if a.raw:  # sample dispute_tx_hash values from the raw dataset
        rows = [json.loads(l) for l in open(a.raw)]
        txs += [r["dispute_tx_hash"] for r in rows[:: max(1, len(rows) // a.n)] if r.get("dispute_tx_hash")][: a.n]
    counts = {}
    for tx in txs:
        rc = rpc.call("eth_getTransactionReceipt", [tx])
        if not rc:
            continue
        for l in rc["logs"]:
            if l["topics"] and l["topics"][0] == DISPUTE_TOPIC:
                counts[l["address"].lower()] = counts.get(l["address"].lower(), 0) + 1
    known = {v.lower(): k for k, v in ORACLES.items()}
    for addr, c in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"{addr}  {c} DisputePrice logs  ({known.get(addr, 'NOT in default ORACLES: pass --oracle')})")
    print(f"checked {len(txs)} txs", file=sys.stderr)


def cmd_compare(a):
    onchain = {r["tx_hash"].lower() for r in map(json.loads, open(a.out))}
    rows = [json.loads(l) for l in open(a.raw)]
    want = {r["dispute_tx_hash"].lower() for r in rows if r.get("dispute_tx_hash")}
    hit = want & onchain
    print(f"raw dataset dispute txs: {len(want)} | found on-chain: {len(hit)} ({len(hit)/max(len(want),1):.1%})"
          f" | on-chain txs not in raw: {len(onchain - want)}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    rpc_default = os.environ.get("POLYGON_RPC_URL")

    s = sub.add_parser("scan")
    s.add_argument("--rpc", default=rpc_default)
    s.add_argument("--from-date", default="2022-07-01", help="UTC YYYY-MM-DD")
    s.add_argument("--to-date", help="UTC YYYY-MM-DD (default: latest block)")
    s.add_argument("--from-block", type=int)
    s.add_argument("--to-block", type=int)
    s.add_argument("--oracle", action="append", metavar="NAME=0xADDR", help="repeatable; replaces defaults")
    s.add_argument("--chunk", type=int, default=5000, help="max blocks per eth_getLogs (auto-shrinks on limits)")
    s.add_argument("--out", default="out/disputes_onchain.jsonl")
    s.add_argument("--resume", action="store_true")
    s.set_defaults(fn=cmd_scan)

    d = sub.add_parser("discover")
    d.add_argument("--rpc", default=rpc_default)
    d.add_argument("--tx", action="append", default=[], help="dispute tx hash (repeatable)")
    d.add_argument("--raw", help="uma_disputed_settled_raw.jsonl: sample its dispute_tx_hash values")
    d.add_argument("--n", type=int, default=25)
    d.set_defaults(fn=cmd_discover)

    c = sub.add_parser("compare")
    c.add_argument("--out", default="out/disputes_onchain.jsonl")
    c.add_argument("--raw", default="data/raw/uma_disputed_settled_raw.jsonl")
    c.set_defaults(fn=cmd_compare)

    a = p.parse_args()
    if a.cmd != "compare" and not a.rpc:
        p.error("set --rpc or POLYGON_RPC_URL")
    if a.cmd == "scan":
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    a.fn(a)


if __name__ == "__main__":
    main()
