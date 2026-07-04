# Phase 11 Maker/Taker Role Audit - RN1

Date: 2026-07-04

Wallet audited: `0x2005d16a84ceefa912d4e380cd32e7ff827875ea`

Status: **not accepted**. RN1 is not proven to be ~100% maker. The current
`fill_enrichment.role` output is biased toward `maker` because the enrichment
join stores the first matching OrderFilled log for a `wallet_events` row, and
maker-side source pages are processed before taker-side source pages.

This audit did not use `fill_enrichment.fee` as actual fee evidence and did not
change fee reporting.

## Finding

The current enriched table says RN1 is 100% maker:

```text
sqlite3 -header -csv data\db\pmresearch.db "
BEGIN;
SELECT datetime('now') AS sqlite_utc_now;
SELECT COUNT(*) AS fill_enrichment_rows FROM fill_enrichment;
SELECT fe.source, fe.role, COUNT(*) AS fills,
       printf('%.6f', SUM(CASE WHEN CAST(we.usdc_size AS REAL) != 0
          THEN ABS(CAST(we.usdc_size AS REAL))
          ELSE ABS(CAST(we.delta_usdc AS REAL)) END)) AS volume
FROM fill_enrichment fe
JOIN wallet_events we ON we.id = fe.event_id
WHERE we.wallet = '0x2005d16a84ceefa912d4e380cd32e7ff827875ea'
GROUP BY fe.source, fe.role
ORDER BY fe.source, fe.role;
WITH enriched AS (SELECT event_id FROM fill_enrichment)
SELECT CASE WHEN e.event_id IS NULL THEN 'unenriched' ELSE 'enriched' END status,
       COUNT(*) fills, MIN(ts) min_ts, MAX(ts) max_ts,
       printf('%.6f', SUM(CASE WHEN CAST(usdc_size AS REAL) != 0
          THEN ABS(CAST(usdc_size AS REAL))
          ELSE ABS(CAST(delta_usdc AS REAL)) END)) volume
FROM wallet_events we
LEFT JOIN enriched e ON e.event_id = we.id
WHERE we.wallet = '0x2005d16a84ceefa912d4e380cd32e7ff827875ea'
  AND we.event_type = 'TRADE'
GROUP BY status;
COMMIT;"
```

Output from a read transaction:

| metric | value |
|---|---:|
| sqlite_utc_now | 2026-07-04 09:58:17 |
| fill_enrichment_rows | 3,035,748 |

| source | role | fills | volume |
|---|---|---:|---:|
| polygonscan | maker | 543,588 | 53,592,296.488973 |
| subgraph | maker | 2,492,160 | 206,706,162.071107 |

| status | fills | min_ts | max_ts | volume |
|---|---:|---:|---:|---:|
| enriched | 3,035,748 | 1752062570 | 1779736839 | 260,298,458.559907 |
| unenriched | 668,306 | 1752088781 | 1783096165 | 127,381,051.490230 |

However, decoded raw OrderFilled payloads do contain RN1 as `taker`:

```text
Raw-store decode/dedupe over subgraph and PolygonScan payloads
```

| source | unique raw fills | RN1 == maker | RN1 == taker | RN1 == neither | RN1 == both |
|---|---:|---:|---:|---:|---:|
| subgraph | 2,836,058 | 2,558,010 | 278,048 | 0 | 0 |
| polygonscan | 602,225 | 546,432 | 55,793 | 0 | 0 |

Source pages queried both roles:

| source | endpoint | requested_role | pages |
|---|---|---|---:|
| polygonscan | logs.getLogs | maker | 1,556 |
| polygonscan | logs.getLogs | taker | 438 |
| rpc | eth_getLogs | maker | 1,474 |
| rpc | eth_getLogs | taker | 1,458 |
| subgraph | orderFilledEvents | maker | 5,145 |
| subgraph | orderFilledEvents | taker | 562 |

Conclusion: taker OrderFilled logs are present in raw data but are not reflected
in `fill_enrichment.role`.

## Local Logic Review

Relevant code:

- `pmresearch/ingest/enrichment.py:64` selects candidate `wallet_events` by
  exact lower-case `wallet`, `tx_hash`, and `token_id`.
- `pmresearch/ingest/enrichment.py:173` assigns `maker` when
  `fill.maker == wallet`; `pmresearch/ingest/enrichment.py:175` assigns
  `taker` when `fill.taker == wallet`.
- `pmresearch/ingest/enrichment.py:182` then matches by traded token and
  `abs(delta_shares)` plus optional USDC amount.
- `pmresearch/sources/subgraph.py:49` fetches roles in order
  `("maker", "taker")`.
- `pmresearch/sources/rpc.py:292` fetches maker topic before taker topic.
- `pmresearch/sources/polygonscan.py:34` defines maker/taker topic slots.

Address normalization is lower-case for wallet, source maker/taker addresses,
and tx hashes. Case sensitivity is not the issue.

The issue is not that `join_fills` cannot assign `taker` for one fill; unit
tests already cover that. The issue is that one `wallet_events` row can match
multiple OrderFilled logs in the same transaction and token amount. Because
`fill_enrichment.event_id` is unique, the first matching log wins. Maker pages
are processed first, so an exchange-facing maker log can occupy the event before
the user-facing taker log is processed.

## OrderFilled Semantics

V1 decoder:

- `orderHash`, `maker`, and `taker` are indexed topics.
- `makerAssetId`, `takerAssetId`, `makerAmountFilled`, `takerAmountFilled`,
  and `fee` are data words.
- `resolve_traded()` treats `makerAssetId == 0` as maker paid USDC, so the
  traded token is `takerAssetId`; otherwise the traded token is `makerAssetId`.

V2 decoder:

- `orderHash`, `maker`, and `taker` are indexed topics.
- Data has `side`, `tokenId`, `makerAmountFilled`, `takerAmountFilled`, `fee`,
  plus extra fields ignored by the current decoder.
- `side == 0` is normalized to `makerAssetId = 0`, `takerAssetId = tokenId`.
- `side == 1` is normalized to `makerAssetId = tokenId`, `takerAssetId = 0`.

The decoded `maker` field is the maker of the specific order emitted in that
OrderFilled event, not always the liquidity-provider role for the Data API trade
row. In Polymarket V1 `_matchOrders`, the contract emits:

- a taker-order event with `maker = takerOrder.maker` and `taker = address(this)`;
- maker-order events with `maker = makerOrder.maker` and
  `taker = takerOrder.maker`.

Sources:

- Polymarket `Trading.sol` emits the taker-order `OrderFilled` with the exchange
  contract as `taker`, then emits maker-order `OrderFilled` rows in
  `_fillMakerOrder`: https://github.com/Polymarket/ctf-exchange/blob/main/src/exchange/mixins/Trading.sol
- Polymarket `OrderStructs.sol` defines order `maker` as source of funds and
  includes a separate `signer`: https://github.com/Polymarket/ctf-exchange/blob/main/src/exchange/libraries/OrderStructs.sol
- V2 keeps batched CTF operations and event emission patterns:
  https://github.com/Polymarket/ctf-exchange-v2

## Required Samples

These are the stored enriched fills and the decoded OrderFilled row selected by
`fill_enrichment.order_hash`.

| event_id | tx_hash | side | price | delta_shares | delta_usdc | decoded maker | decoded taker | assigned role | maker_asset_id | taker_asset_id | maker_amount_filled | taker_amount_filled | source |
|---:|---|---|---:|---:|---:|---|---|---|---|---|---:|---:|---|
| 1181233 | `0x32067a899b9d0088dfbe615e68b30065565d0e0586a7ff6cc01de2ff493b4a87` | BUY | 0.3700000819000303 | 3.174602 | -1.174603 | RN1 | `0x8e4a02ff7248e0b7640dd42fb8dd14c65f7df43d` | maker | 0 | `60504870629391346311651923636674617393376172851569827575179466590116479331839` | 1174603 | 3174602 | subgraph |
| 1181234 | `0x2b4309e29ed8c049afd5716de907bca57c8c15869e03dad97bd9447632527153` | BUY | 0.3700000819000303 | 3.174602 | -1.174603 | RN1 | `0x8e4a02ff7248e0b7640dd42fb8dd14c65f7df43d` | maker | 0 | `60504870629391346311651923636674617393376172851569827575179466590116479331839` | 1174603 | 3174602 | subgraph |
| 1181235 | `0xea4e456a2afb61b6b8ae03603167f562bf49ded303157cd67669cb42c79b97d2` | BUY | 0.37 | 14.67 | -5.4279 | RN1 | `0x1cfb895689080133141e5e5899b145ad52ef8888` | maker | 0 | `60504870629391346311651923636674617393376172851569827575179466590116479331839` | 5427900 | 14670000 | subgraph |
| 1181236 | `0x14281bb0586c0f3469f25b2095357c1a7e134af82bd4ce999f280eb1b9e10b18` | BUY | 0.37 | 3.17 | -1.1729 | RN1 | `0x52f61a4018d34dee59fe8c3cfe9750053535af63` | maker | 0 | `60504870629391346311651923636674617393376172851569827575179466590116479331839` | 1172900 | 3170000 | subgraph |
| 1181237 | `0x35e4f8bf90115e15e1872211a99c0a7e5049aa1566d3a6d5b9f0bad035b0dab6` | BUY | 0.27 | 1000.11 | -270.0297 | RN1 | `0x7002384aa87593b80ff51491e37b8ea64e33395c` | maker | 0 | `108938672809282372236616084275748158503800744552703283066155671445197098819102` | 270029700 | 1000110000 | subgraph |
| 1465555 | `0x61a2c7b22991ccb3fd672e932ea712160ea23efb149f840dac53fd26ca8e9ca9` | SELL | 0.999 | -72708.97 | 72636.26103 | RN1 | `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e` | maker | `33452948997980905557804941007634696395030267885996680604531537840506301850362` | 0 | 72708970000 | 72636261030 | subgraph |
| 1468204 | `0xecbc800afb57c8a65d91751b6c7ec846327cb99a0101fad8d33653df1999e587` | SELL | 0.999 | -74626.58 | 74551.95342 | RN1 | `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e` | maker | `78733577445979725465042404331019364624832785842172712575695189851284124078831` | 0 | 74626580000 | 74551953420 | subgraph |
| 1468205 | `0xde019ad029655cb12325bb42fda1d39dd35e977181ee8e1b4af03038386ae7f9` | SELL | 0.999 | -725 | 724.275 | RN1 | `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e` | maker | `78733577445979725465042404331019364624832785842172712575695189851284124078831` | 0 | 725000000 | 724275000 | subgraph |
| 1468206 | `0xae718bb8d9aed6b377a634889d8d382982cf9911b5172f83441a3575177feeb7` | SELL | 0.999 | -525 | 524.475 | RN1 | `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e` | maker | `78733577445979725465042404331019364624832785842172712575695189851284124078831` | 0 | 525000000 | 524475000 | subgraph |
| 1468207 | `0x8efccbd2124913d417a16454b7a7d68975ba2563a9d3c8db860ee245186aaeef` | SELL | 0.999 | -520 | 519.48 | RN1 | `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e` | maker | `78733577445979725465042404331019364624832785842172712575695189851284124078831` | 0 | 520000000 | 519480000 | subgraph |
| 621449 | `0x6dd78aa59060ac90086a12274823c9a2c4124cb0a807e0ce79d901a7de3aeff1` | BUY | 0.42 | 90 | -37.8 | RN1 | `0xd473ede4cdf5b0a2672db12834a923fac5783feb` | maker | 0 | `37887497712221839897989216765660666123748794866448881440166418229944351909474` | 37800000 | 90000000 | polygonscan |
| 621450 | `0x9dcbf305727c4ffce17efb07f5a9185dc8004552d4d1b82a8f2f393a40aac556` | BUY | 0.84 | 12.5 | -10.5 | RN1 | `0x60f16ed9a609f403b8c23cd3f43834d41db9a9e9` | maker | 0 | `49697558666388027355939310250581234256977157222771712630460073965297967014855` | 10500000 | 12500000 | polygonscan |
| 621451 | `0x70a435ed48f1afa7b6bb655b2aeb602ce16f0fe02432f9edecbb143faf58517f` | BUY | 0.7900001323 | 4.761903 | -3.761904 | RN1 | `0xd522846d6ab14d6e2094db2abcc05e65a887cdc8` | maker | 0 | `2434135886241509469337360774997931654094057067737505770902332587369252164266` | 3761904 | 4761903 | polygonscan |
| 621455 | `0x6df7d8cfaec2372049de123f5f15b95ba02e6aaa29290134648d3e35fcdc157c` | BUY | 0.37 | 333.54 | -123.4098 | RN1 | `0xde9f7f4e77a1595623ceb58e469f776257ccd43c` | maker | 0 | `57023391704695879853853010199589445122709911298702522706680810052402421594549` | 123409800 | 333540000 | polygonscan |
| 621454 | `0x6df7d8cfaec2372049de123f5f15b95ba02e6aaa29290134648d3e35fcdc157c` | BUY | 0.37 | 1080.01 | -399.6037 | RN1 | `0xde9f7f4e77a1595623ceb58e469f776257ccd43c` | maker | 0 | `57023391704695879853853010199589445122709911298702522706680810052402421594549` | 399603700 | 1080010000 | polygonscan |
| 621453 | `0xe8d7bc9764260f9c78a0613e6c8ff75a38052206fafdebe5b5d58f4dc98931bc` | BUY | 0.38 | 1080.01 | -410.4038 | RN1 | `0x42c99f38d2b951b0dc8e8bd5371fa80c9dd19623` | maker | 0 | `57023391704695879853853010199589445122709911298702522706680810052402421594549` | 410403800 | 1080010000 | polygonscan |
| 621452 | `0xe8d7bc9764260f9c78a0613e6c8ff75a38052206fafdebe5b5d58f4dc98931bc` | BUY | 0.37 | 2530.78 | -936.3886 | RN1 | `0x42c99f38d2b951b0dc8e8bd5371fa80c9dd19623` | maker | 0 | `57023391704695879853853010199589445122709911298702522706680810052402421594549` | 936388600 | 2530780000 | polygonscan |
| 621456 | `0xddc3b62653e7b145e9f98bf38267ae1a2f9e4da7cda48f8ca6c060424d063cb4` | BUY | 0.84 | 32.5 | -27.3 | RN1 | `0x226b5750d1482f528b431c3f26789e8eab1bbb68` | maker | 0 | `49697558666388027355939310250581234256977157222771712630460073965297967014855` | 27300000 | 32500000 | polygonscan |
| 621458 | `0x1ece534632da177ae64e42b8e303f93d62706b70fb72704127d885a37d40f45c` | BUY | 0.79 | 48.07 | -37.9753 | RN1 | `0x5046653dcc9b61684695b013f4a9b44302c0a07e` | maker | 0 | `2434135886241509469337360774997931654094057067737505770902332587369252164266` | 37975300 | 48070000 | polygonscan |
| 621457 | `0x12131ef3eb1c484321841d783d6026269f5c5d4b2f5526c341d707c3e932c615` | BUY | 0.48 | 14.19 | -6.8112 | RN1 | `0xa6a786cac8fee1d184e3ef986894c7a7244fe5d4` | maker | 0 | `82398715312639116468916534844619196152512587917121562948081765825814868049115` | 6811200 | 14190000 | polygonscan |

## Manual Checks

### BUY assigned maker - `0xead88605cf5a71861841e3769c095809a4d14ec184edf599fc2abc4fd7317844`

The same Data API wallet event can be matched by two raw OrderFilled logs:

| page_role | order_hash | maker | taker | maker_asset_id | taker_asset_id | maker_amount | taker_amount |
|---|---|---|---|---|---|---:|---:|
| maker | `0x8eea67655de3e7eadf8e6c42213ccec14532e707d27f317d289ac404ce3e8101` | RN1 | `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e` | 0 | `112244671665445617674984699605987278954793224122257642826125662373739794385868` | 1000000 | 20000000 |
| taker | `0xbf2433f6394539fd57c0f04fe03ec88342db79819173627690a322271dd15fa0` | `0xbaca95eec36414e72165efcbc139115642f949e6` | RN1 | `112244671665445617674984699605987278954793224122257642826125662373739794385868` | 0 | 20000000 | 1000000 |

The stored row uses the maker-page order hash and assigns `maker`. The taker
log also matches the same transaction/token/amount and indicates RN1 was the
taking agent against another wallet.

### SELL assigned maker - `0x61a2c7b22991ccb3fd672e932ea712160ea23efb149f840dac53fd26ca8e9ca9`

| page_role | order_hash | maker | taker | maker_asset_id | taker_asset_id | maker_amount | taker_amount |
|---|---|---|---|---|---|---:|---:|
| maker | `0x7955c12ef64e77dd19adb431fc07a8d22d3e478ab98f7300b6a6e769d73ab29e` | RN1 | `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e` | `33452948997980905557804941007634696395030267885996680604531537840506301850362` | 0 | 72708970000 | 72636261030 |
| taker | `0x5d382ad7dd5c522b5290272c5d7ab8b36180fa2a22f4f3aeac12761e478ce492` | `0x751a2b86cab503496efd325c8344e10159349ea1` | RN1 | 0 | `33452948997980905557804941007634696395030267885996680604531537840506301850362` | 72636261030 | 72708970000 |

The stored `maker` role is from the exchange-facing taker-order event. The
companion maker-order event indicates RN1 as the taker. This is exactly the
ordering bias.

### PolygonScan recent/manual fill - `0xc91d0b2d85803899bd37472f95b076548ed91405aa6a412b053dfbba2e5cc802`

| page_role | version | v2_side | order_hash | maker | taker | maker_asset_id | taker_asset_id | maker_amount | taker_amount |
|---|---|---|---|---|---|---|---|---:|---:|
| taker | v2 | BUY | `0x454f8cd00565f94dd3cb3c047e707cba99d55484dd729362bf87548bed4f7d05` | `0xc96aeabae8c81faf8d803201da1d2461cefc396a` | RN1 | 0 | `21882123948779429970988468170478398658761336314919474180719036337338299173057` | 37581420960 | 37619040000 |
| maker | v2 | SELL | `0x173cd902681d31dfbeba43f4c9f9c6f95fae594948c8c4a337c673745884a9cf` | RN1 | `0xe2222d279d744050d28e00520010520000310f59` | `21882123948779429970988468170478398658761336314919474180719036337338299173057` | 0 | 37619040000 | 37581420960 |

Again, the current stored row chose the exchange-facing maker-page log. The
non-exchange companion log indicates RN1 as taker.

## Bias Tests

- **Only matching maker side?** Yes, in practice. The code can match taker logs,
  but maker pages are fetched first and the unique `event_id` row is inserted
  before the taker companion can be considered.
- **Taker matches missed because proxy vs signer?** Not the primary issue here.
  Raw source queries returned many direct RN1-as-taker logs; RN1 is present in
  the decoded `taker` field. Proxy/signer mismatch remains unresolved for the
  668,306 currently unenriched trades because wallet-filtered source queries
  cannot prove what was never returned.
- **Proxy wallets represented differently across sources?** The enriched and raw
  decoded rows use the same lower-case RN1 address. `neither=0` in raw
  wallet-filtered decoded rows, so the raw rows themselves are not an address
  normalization failure.
- **Wrong address field?** For CTF Exchange matching, yes at the semantic level.
  The event `maker` field is the maker of that emitted order. If `taker` is the
  exchange contract, `maker` can be the active taker-order owner, not a passive
  liquidity maker.
- **Join excluding taker fills before role assignment?** Not before assignment,
  but after first assignment. The taker fill loses to prior maker-page insertion
  through `ON CONFLICT(event_id) DO NOTHING`.

## Recommended Fix

Do not accept maker/taker role attribution yet.

Recommended implementation direction:

1. Treat exchange-contract counterparties specially:
   `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e`,
   `0xc5d563a36ae78145c45a50134d48a1215220f80a`,
   `0xe111180000d2663c0091e4f400237545b87b996b`,
   `0xe2222d279d744050d28e00520010520000310f59`.
2. For a matching OrderFilled where `fill.maker == wallet` and
   `fill.taker` is an exchange contract, classify the wallet as taker-order
   owner, not liquidity maker.
3. Resolve all matching fills for an event before insertion. Do not let source
   pagination order choose the role.
4. If both maker and taker evidence remain after exchange-facing normalization,
   store `ambiguous`/leave unenriched until a rule is explicit.
5. Add tests:
   maker match assigns maker;
   taker match assigns taker;
   address normalization works;
   exchange-facing taker-order event is classified as taker;
   companion maker/taker logs for one wallet event do not make maker win by
   fetch order;
   subgraph and RPC/PolygonScan behave consistently;
   fee reporting remains unchanged and does not trust `fill_enrichment.fee`.

Outcome classification: **B. Role attribution is biased**. No acceptance until
role matching is fixed and RN1 enrichment is rebuilt or updated.
