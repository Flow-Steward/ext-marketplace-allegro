# Allegro integration for Flow Steward

<img src="icon.png" width="96" alt="Allegro integration for Flow Steward">

[![CI](https://img.shields.io/github/actions/workflow/status/Flow-Steward/ext-marketplace-allegro/ci.yml?branch=main&label=CI)](https://github.com/Flow-Steward/ext-marketplace-allegro/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/Flow-Steward/ext-marketplace-allegro?label=release)](https://github.com/Flow-Steward/ext-marketplace-allegro/releases/latest)

Automate Allegro selling with Flow Steward workflows and AI agents: sync stock and prices, read orders, update offers — without writing code.

This Flow Steward extension is an Allegro integration for marketplace automation. It works through the Allegro REST API, keeps stock and prices of your offers in sync with your warehouse or ERP, brings orders into your workflows and makes every documented API operation available for workflow automation.

## What is this

Allegro is a Polish online marketplace where shops and companies sell their goods. This extension is a connector: it adds ready Allegro steps to the Flow Steward workflow editor and the same operations as tools for AI agents. Read more about Flow Steward at <https://docs.flowsteward.org>.

## What you can automate

- Update stock and Buy Now prices of all your Allegro offers from your warehouse or ERP — many offers in one run.
- Bring new paid Allegro orders into your accounting, ERP or fulfilment, no retyping.
- React to order events — bought, paid, cancelled — by reading only the events since the last run.
- Add parcel tracking numbers to orders and set the seller order status after shipping.
- Attach the invoice file to an Allegro order once your accounting system has issued it.
- Collect customer returns, disputes and buyer messages for your support team.
- Create offers from products in the Allegro catalog and publish or end offers in batches.

## Why teams use it

- **A change is not sent twice.** Each stock or price command gets an ID derived from your batch key and its content, so running the same update again is recognized by Allegro instead of being applied twice. Changes are never retried automatically.
- **Accepted is not mistaken for applied.** Every step that changes data returns `processing`: `completed` when Allegro answered that the change is made, `accepted` when Allegro accepted the request (HTTP 202) or registered a command it applies afterwards. With `accepted`, `applied` is false and `check` names the step that reads the outcome. An answer with a status Allegro does not document for the operation is never taken as success.
- **Amounts without rounding errors.** Prices are exact decimals with at most two places, from input to Allegro and back.
- **A test run changes nothing.** Writes are declared external effects, and Flow Steward suppresses them in test runs. Allegro Sandbox gives a separate test marketplace on top of that.
- **The whole documented API is covered.** 276 steps: 6 everyday steps plus a step for each of the 270 operations of the Allegro REST API.
- **Tokens stay secret.** Flow Steward keeps and renews the authorization; a step receives only the current access token and removes it from every answer.

## What's inside

Everyday steps:

- **Test connection** — checks the authorization and returns the account's login.
- **Get all my offers** — reads all your offers page by page, filtered by name, IDs, status or category, into the step output or a JSON file.
- **Update stock and prices** — sets stock and Buy Now price of many offers with Allegro's batch commands of up to 1000 offers each.
- **Get stock and price update results** — how far Allegro got with those commands: offers done, failed and waiting, and why an offer failed.
- **Get orders** — reads orders page by page, filtered by status, buyer and dates.
- **Get order events** — reads only the order events after the last one you saw.

All 270 operations of the Allegro REST API are also available as steps: offers and batch offer changes, products and categories, pricing and promotion, orders, returns, disputes and messages, shipping and delivery, account and fulfilment. The extension's **Coverage** page lists each one.

## Get started

1. In Flow Steward, open **Extensions → Catalog**, find **Allegro** and install it.
2. Register an application at [apps.developer.allegro.pl](https://apps.developer.allegro.pl) with the Allegro account that will own it. Paste the OAuth Redirect URI shown on the extension's **Connection** page into the application's redirect addresses, and choose the permissions your steps need.
3. On the **Connection** page, enter the application's **Client ID** and **Client Secret**, press **Connect**, sign in to Allegro with the seller account and allow the access. Then press **Test connection**.
4. Build your first workflow: for example, on a schedule → **Update stock and prices** with rows from your warehouse → **Get stock and price update results** a few minutes later.

## Requirements

- An Allegro seller account.
- An Allegro application registered by your company at apps.developer.allegro.pl; its Client ID and Client Secret are entered in the connection. Allegro Sandbox uses its own application registered at apps.developer.allegro.pl.allegrosandbox.pl and the **Connection (Sandbox)** page.
- The Allegro permissions the steps name, for example `allegro:api:sale:offers:read` to read offers, `allegro:api:sale:offers:write` to change stock and prices, `allegro:api:orders:read` for orders; `allegro:api:profile:read` is always needed.
- Allegro's batch stock change accepts a quantity of 1 or more; to stop selling an offer, end it with **Publish or end offers**.
- Allegro applies batch stock and price changes asynchronously: **Update stock and prices** registers the commands (`processing` is `accepted`), and **Get stock and price update results** reads the outcome per offer.
- Downloading shipment labels and protocols, pickup proposals, the fee preview, the Allegro Prices query and ingredient parsing change nothing in Allegro, so test runs send them. **Get fulfillment stock** returns JSON, or with `format` `csv` a CSV workflow file.
- Allegro keeps an authorization for three months without use and limits the request rate; an HTTP 429 answer says how long to wait.
- Flow Steward 0.9.16 or later.

## FAQ

**How do I sync stock and prices to Allegro automatically?**
Run **Update stock and prices** on a schedule with one row per offer (offer ID and the new quantity, price or both), then read the outcome with **Get stock and price update results**.

**How do I import Allegro orders into my ERP or accounting system?**
Use **Get order events** to see which orders are new or paid since the last run, read them with **Get orders**, and pass them to the steps of your ERP or accounting extension.

**What happens if the same stock update runs twice?**
With the same rows and batch key, Allegro recognizes the commands as already registered and does not apply them again. If Allegro refuses one command after others were registered, the step succeeds, marks each command `registered`, `refused` or `not_sent` and says what to fix; run it again with the same rows and batch key after the fix.

**Can I try the Allegro integration without touching my live offers?**
Yes. A test run of a workflow never sends changes, and a connection to Allegro Sandbox works against Allegro's separate test marketplace.

**Which Allegro API operations are supported?**
All 270 operations of the Allegro REST API specification the release is built on, each as its own step, plus the six everyday steps.

## In your language

Integracja Allegro z Flow Steward automatyzuje sprzedaż na Allegro bez pisania kodu. Rozszerzenie korzysta z Allegro REST API: aktualizuje stany magazynowe i ceny wielu ofert naraz, pobiera zamówienia i zdarzenia zamówień, a każda z 270 operacji API jest dostępna jako krok workflow. Do połączenia potrzebna jest własna aplikacja Allegro (Client ID i Client Secret).

## Links

- Flow Steward documentation: <https://docs.flowsteward.org>
- Flow Steward extension catalog: <https://github.com/Flow-Steward/extensions>
- Issues and support: <https://github.com/Flow-Steward/ext-marketplace-allegro/issues>
- Allegro REST API documentation: <https://developer.allegro.pl/>
- Changes: [CHANGELOG.md](CHANGELOG.md)

## For developers

Every step is generated from the pinned Allegro specification (`tests/reference/allegro_api.json`, OpenAPI 3.0.0, downloaded 2026-11-18). To rebuild the contracts, download `https://developer.allegro.pl/swagger.yaml` to `tests/reference/sources/swagger.yaml` (not committed), then:

```bash
python3 tests/reference/extract_reference.py
python3 tests/reference/build_contracts.py
```

From a Flow Steward checkout (the checks run in the `fs-audit` container):

```bash
./core/entrypoints/cli/scripts/lint.sh --checks extension-static,extension-tests --extension-root "$PWD/workspace/extensions/flowsteward_allegro"
./flow-steward extensions validate flowsteward.allegro --root workspace/extensions --health --marketplace
./flow-steward extensions test flowsteward.allegro --root workspace/extensions
```

Allegro and its marks belong to Allegro.pl sp. z o.o.; see [BRANDING.md](BRANDING.md) for the icon's source.
