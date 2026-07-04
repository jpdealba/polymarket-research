import csv
import glob
import os
from decimal import Decimal
from collections import defaultdict

WALLET = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea".lower()
NULL = "0x0000000000000000000000000000000000000000"

RAW_PATTERN = "rn1_cashflows_raw_*.csv"
LABELS_FILE = "rn1_address_labels.csv"

INTERNAL_KEYWORDS = [
    "polymarket",
    "ctf",
    "neg risk",
    "exchange",
    "proxy",
    "adapter",
    "collateral",
    "conditional",
]


def load_labels(path):
    labels = {}

    if not os.path.exists(path):
        raise SystemExit(f"Falta {path}")

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            addr = row["address"].strip().lower()
            label = row.get("label", "").strip()
            is_internal_raw = row.get("is_internal", "").strip().lower()
            notes = row.get("notes", "").strip()

            is_internal = is_internal_raw in ["true", "1", "yes", "y", "si", "sí"]

            labels[addr] = {
                "label": label,
                "is_internal": is_internal,
                "notes": notes,
            }

    return labels


def infer_internal_from_label(label):
    l = (label or "").lower()
    return any(k in l for k in INTERNAL_KEYWORDS)


def classify_counterparty(counterparty, labels):
    cp = counterparty.lower()

    if cp == NULL:
        return "NULL"

    info = labels.get(cp)

    if info:
        label = info["label"]
        if info["is_internal"] or infer_internal_from_label(label):
            return "INTERNAL_POLYMARKET"
        if label.upper() == "UNKNOWN_CHECK":
            return "UNKNOWN_CHECK"
        return "EXTERNAL_CANDIDATE"

    return "UNKNOWN_CHECK"


def dec(x):
    return Decimal(str(x))


def main():
    labels = load_labels(LABELS_FILE)

    raw_files = sorted(glob.glob(RAW_PATTERN))

    if not raw_files:
        raise SystemExit(f"No encontré archivos con patrón: {RAW_PATTERN}")

    print("Archivos raw encontrados:")
    for f in raw_files:
        print(" -", f)

    classified_rows = []
    summary = defaultdict(lambda: {"count": 0, "amount": Decimal("0")})
    by_day = defaultdict(lambda: {"count": 0, "amount": Decimal("0")})
    counterparties = defaultdict(lambda: {"count": 0, "amount": Decimal("0")})

    seen = set()

    for file in raw_files:
        with open(file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            for row in reader:
                h = row.get("hash", "")
                log_key = (
                    row.get("token_bucket", ""),
                    h,
                    row.get("blockNumber", ""),
                    row.get("from", "").lower(),
                    row.get("to", "").lower(),
                    row.get("amount", ""),
                )

                if log_key in seen:
                    continue

                seen.add(log_key)

                token = row["token_bucket"]
                flow_type = row["flow_type"]
                amount = dec(row["amount"])
                counterparty = row["counterparty"].lower()
                day = row.get("day_utc") or row.get("datetime_utc", "")[:10]

                cp_class = classify_counterparty(counterparty, labels)

                # Cashflow sign from RN1 perspective
                if flow_type in ["TRANSFER_IN", "MINT_TO_RN1"]:
                    signed_amount = amount
                elif flow_type in ["TRANSFER_OUT", "BURN_FROM_RN1"]:
                    signed_amount = -amount
                else:
                    signed_amount = Decimal("0")

                out = dict(row)
                out["counterparty_class"] = cp_class
                out["signed_amount"] = str(signed_amount)
                classified_rows.append(out)

                skey = (token, flow_type, cp_class)
                summary[skey]["count"] += 1
                summary[skey]["amount"] += amount

                dkey = (day, token, flow_type, cp_class)
                by_day[dkey]["count"] += 1
                by_day[dkey]["amount"] += amount

                ckey = (token, flow_type, counterparty, cp_class)
                counterparties[ckey]["count"] += 1
                counterparties[ckey]["amount"] += amount

    # Outputs
    with open("rn1_cashflows_classified.csv", "w", newline="", encoding="utf-8") as f:
        fieldnames = list(classified_rows[0].keys())
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(classified_rows)

    with open("rn1_cashflows_classified_summary.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["token", "flow_type", "counterparty_class", "count", "amount"])

        for (token, flow_type, cp_class), data in sorted(summary.items()):
            writer.writerow([token, flow_type, cp_class, data["count"], str(data["amount"])])

    with open("rn1_cashflows_classified_by_day.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["day", "token", "flow_type", "counterparty_class", "count", "amount"])

        for (day, token, flow_type, cp_class), data in sorted(by_day.items()):
            writer.writerow([day, token, flow_type, cp_class, data["count"], str(data["amount"])])

    with open("rn1_counterparty_todo.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["token", "flow_type", "counterparty", "counterparty_class", "count", "amount", "polygonscan_url"])

        rows = sorted(
            counterparties.items(),
            key=lambda x: abs(x[1]["amount"]),
            reverse=True,
        )

        for (token, flow_type, counterparty, cp_class), data in rows:
            if cp_class == "UNKNOWN_CHECK":
                writer.writerow([
                    token,
                    flow_type,
                    counterparty,
                    cp_class,
                    data["count"],
                    str(data["amount"]),
                    f"https://polygonscan.com/address/{counterparty}",
                ])

    print("\nResumen:")
    for (token, flow_type, cp_class), data in sorted(summary.items()):
        print(f"{token:12} {flow_type:16} {cp_class:22} count={data['count']:8} amount={data['amount']}")

    print("\nArchivos creados:")
    print("rn1_cashflows_classified.csv")
    print("rn1_cashflows_classified_summary.csv")
    print("rn1_cashflows_classified_by_day.csv")
    print("rn1_counterparty_todo.csv")


if __name__ == "__main__":
    main()