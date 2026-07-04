import csv
import os
import time
import requests
from decimal import Decimal
from collections import defaultdict
from datetime import datetime, timezone

# =========================
# CONFIG
# =========================

WALLET = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea".lower()
NULL = "0x0000000000000000000000000000000000000000"

API_KEY = os.environ.get("ETHERSCAN_API_KEY")
PUSD_CONTRACT = os.environ.get("PUSD_CONTRACT", "").lower()

if not API_KEY:
    raise SystemExit("Falta ETHERSCAN_API_KEY")

if not PUSD_CONTRACT:
    raise SystemExit("Falta PUSD_CONTRACT")

# Cambia estas fechas según el rango que quieras bajar.
# Ejemplo: primeros 2 meses de pUSD
START_DATE = "2026-06-01"
END_DATE = "2026-07-04"

# Para correr solo pUSD:
TOKENS = {
    "USDC_NATIVE": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
    "USDC_E": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
    "PUSD": PUSD_CONTRACT,
}

# Si después quieres USDC.e también, usa esto:
# TOKENS = {
#     "USDC_E": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
# }

# Si quieres USDC native:
# TOKENS = {
#     "USDC_NATIVE": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
# }

BASE_URL = "https://api.etherscan.io/v2/api"
MAX_ROWS = 10_000


# =========================
# API HELPERS
# =========================

def timestamp_utc(date_str):
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def api_get(params):
    params = dict(params)
    params["chainid"] = "137"  # Polygon
    params["apikey"] = API_KEY

    for attempt in range(5):
        try:
            r = requests.get(BASE_URL, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()

            result = data.get("result", [])

            if isinstance(result, str):
                msg = result.lower()

                if "rate limit" in msg or "max rate limit" in msg:
                    wait = 2 + attempt * 2
                    print(f"Rate limit. Esperando {wait}s...")
                    time.sleep(wait)
                    continue

                if "no transactions found" in msg:
                    return []

                print("API result string:", result)
                return []

            return result

        except Exception as e:
            wait = 2 + attempt * 2
            print(f"API error: {e}. Reintentando en {wait}s...")
            time.sleep(wait)

    raise RuntimeError("API falló después de varios intentos")


def get_block_by_time(date_str, closest="before"):
    ts = timestamp_utc(date_str)

    params = {
        "chainid": "137",
        "module": "block",
        "action": "getblocknobytime",
        "timestamp": str(ts),
        "closest": closest,
        "apikey": API_KEY,
    }

    for attempt in range(10):
        r = requests.get(BASE_URL, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()

        if data.get("status") == "1":
            block = int(data["result"])
            print(f"{date_str} -> block {block}")
            time.sleep(0.5)
            return block

        msg = str(data.get("result", "")).lower()

        if "rate limit" in msg or "max calls" in msg:
            wait = 2 + attempt
            print(f"Rate limit obteniendo bloque {date_str}. Esperando {wait}s...")
            time.sleep(wait)
            continue

        raise RuntimeError(f"No pude obtener bloque para {date_str}: {data}")

    raise RuntimeError(f"No pude obtener bloque para {date_str}: rate limit persistente")


# =========================
# FETCH TOKEN TRANSFERS
# =========================

progress_counts = defaultdict(int)


def fetch_range(token_name, contract, startblock, endblock, depth=0):
    params = {
        "module": "account",
        "action": "tokentx",
        "address": WALLET,
        "contractaddress": contract,
        "startblock": str(startblock),
        "endblock": str(endblock),
        "page": "1",
        "offset": str(MAX_ROWS),
        "sort": "asc",
    }

    rows = api_get(params)

    indent = "  " * depth
    print(f"{indent}{token_name} blocks {startblock}-{endblock}: {len(rows)} rows")

    # Si son menos de 10k, ya tenemos todo ese rango.
    if len(rows) < MAX_ROWS:
        progress_counts[token_name] += len(rows)
        print(f"{indent}✅ {token_name} accumulated_leaf_rows={progress_counts[token_name]:,}")
        return rows

    # Si son 10k exactos, puede estar truncado. Hay que dividir.
    if startblock >= endblock:
        progress_counts[token_name] += len(rows)
        print(f"{indent}⚠️ Rango indivisible con 10k rows. Puede estar truncado.")
        return rows

    mid = (startblock + endblock) // 2

    left = fetch_range(token_name, contract, startblock, mid, depth + 1)
    time.sleep(0.2)
    right = fetch_range(token_name, contract, mid + 1, endblock, depth + 1)

    return left + right


# =========================
# CLASSIFICATION
# =========================

def classify(row):
    frm = row["from"].lower()
    to = row["to"].lower()

    if frm == NULL and to == WALLET:
        return "MINT_TO_RN1"

    if frm == WALLET and to == NULL:
        return "BURN_FROM_RN1"

    if to == WALLET:
        return "TRANSFER_IN"

    if frm == WALLET:
        return "TRANSFER_OUT"

    return "OTHER"


def amount_of(row):
    decimals = int(row.get("tokenDecimal") or 6)
    return Decimal(row["value"]) / (Decimal(10) ** decimals)


def dt_from_timestamp(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


# =========================
# MAIN
# =========================

def main():
    print(f"Wallet: {WALLET}")
    print(f"Date range: {START_DATE} to {END_DATE}")

    start_block = get_block_by_time(START_DATE, "after")
    time.sleep(1.0)
    end_block = get_block_by_time(END_DATE, "before")

    print(f"Using Polygon blocks: {start_block} to {end_block}")
    print(f"Tokens: {TOKENS}")

    raw_rows = []
    summary = defaultdict(lambda: {"count": 0, "amount": Decimal("0")})
    counterparties = defaultdict(lambda: {"count": 0, "amount": Decimal("0")})
    by_day = defaultdict(lambda: {"count": 0, "amount": Decimal("0")})

    seen = set()

    for token_name, contract in TOKENS.items():
        print(f"\n=== Fetching {token_name} ===")
        rows = fetch_range(token_name, contract, start_block, end_block)

        print(f"\n{token_name}: TOTAL FETCHED RAW ROWS = {len(rows):,}\n")

        for i, row in enumerate(rows, start=1):
            if i % 10_000 == 0:
                print(f"{token_name}: processed {i:,}/{len(rows):,}")

            dedupe_key = (
                token_name,
                row.get("hash", ""),
                row.get("logIndex", ""),
                row.get("transactionIndex", ""),
            )

            if dedupe_key in seen:
                continue

            seen.add(dedupe_key)

            flow_type = classify(row)
            amount = amount_of(row)
            frm = row["from"].lower()
            to = row["to"].lower()
            timestamp = int(row["timeStamp"])
            block_number = int(row["blockNumber"])

            if to == WALLET:
                counterparty = frm
            elif frm == WALLET:
                counterparty = to
            else:
                counterparty = "other"

            dt = dt_from_timestamp(timestamp)
            day = dt[:10]

            out = {
                "token_bucket": token_name,
                "token_symbol": row.get("tokenSymbol", ""),
                "hash": row.get("hash", ""),
                "timestamp": timestamp,
                "datetime_utc": dt,
                "day_utc": day,
                "blockNumber": block_number,
                "from": frm,
                "to": to,
                "amount": str(amount),
                "flow_type": flow_type,
                "counterparty": counterparty,
            }

            raw_rows.append(out)

            key = (token_name, flow_type)
            summary[key]["count"] += 1
            summary[key]["amount"] += amount

            ckey = (token_name, flow_type, counterparty)
            counterparties[ckey]["count"] += 1
            counterparties[ckey]["amount"] += amount

            dkey = (day, token_name, flow_type)
            by_day[dkey]["count"] += 1
            by_day[dkey]["amount"] += amount

        time.sleep(0.5)

    raw_rows.sort(key=lambda r: (r["timestamp"], r["blockNumber"], r["hash"]))

    suffix = f"{START_DATE}_to_{END_DATE}".replace("-", "")

    raw_file = f"rn1_cashflows_raw_{suffix}.csv"
    summary_file = f"rn1_cashflows_summary_{suffix}.csv"
    counterparties_file = f"rn1_cashflows_counterparties_{suffix}.csv"
    by_day_file = f"rn1_cashflows_by_day_{suffix}.csv"

    with open(raw_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "token_bucket",
                "token_symbol",
                "hash",
                "timestamp",
                "datetime_utc",
                "day_utc",
                "blockNumber",
                "from",
                "to",
                "amount",
                "flow_type",
                "counterparty",
            ],
        )
        writer.writeheader()
        writer.writerows(raw_rows)

    with open(summary_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["token", "flow_type", "count", "amount"])

        for (token, flow_type), data in sorted(summary.items()):
            writer.writerow([token, flow_type, data["count"], str(data["amount"])])

    with open(counterparties_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["token", "flow_type", "counterparty", "count", "amount"])

        for (token, flow_type, counterparty), data in sorted(
            counterparties.items(),
            key=lambda x: abs(x[1]["amount"]),
            reverse=True,
        ):
            writer.writerow([token, flow_type, counterparty, data["count"], str(data["amount"])])

    with open(by_day_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["day_utc", "token", "flow_type", "count", "amount"])

        for (day, token, flow_type), data in sorted(by_day.items()):
            writer.writerow([day, token, flow_type, data["count"], str(data["amount"])])

    print("\nResumen:")
    for (token, flow_type), data in sorted(summary.items()):
        print(f"{token:12} {flow_type:16} count={data['count']:10} amount={data['amount']}")

    print("\nArchivos creados:")
    print(raw_file)
    print(summary_file)
    print(counterparties_file)
    print(by_day_file)


if __name__ == "__main__":
    main()