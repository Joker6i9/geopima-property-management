# Backend changes: billing, M-Pesa and access control

Branch `fix/3-billing-mpesa-permissions`. This note says what changed, how to deploy it, and how the pieces work. It covers the critical and high findings in `CURRENT_STATE_REVIEW.md` (C1–C5, H1–H5).

## Deploying this branch

1. **Back up the database.** One migration drops tables (see "Migrations").
2. `pip install -r requirements.txt` (no new packages).
3. Set `MPESA_CALLBACK_SECRET` to a long random string (see `.env.example`). Without it the M-Pesa callbacks are **closed** outside `DEBUG`.
   Also set `MPESA_PAYBILL` (the business number tenants pay to; the tenant portal shows it).
4. `python manage.py migrate`
5. `python manage.py seed_roles`: creates the five staff roles and copies each user's Django-group membership onto the matching role. Run it once, then assign roles in Configuration → Users.
6. Re-register the Daraja C2B URLs with the new secret path (below).
7. Schedule `python manage.py generate_monthly_invoices` for the 1st of each month and `python manage.py mark_overdue_invoices` daily (Render cron). Both are safe to run twice.

## Migrations

| Migration | What it does | Risk |
|---|---|---|
| `billing 0003_invoice_sequence` | Adds the table that hands out invoice numbers. Starts after the highest existing `INVnnnnnn`. | None |
| `mpesa 0003_split_settlements_and_raw_payload` | `PaymentSettlement.transaction` becomes a foreign key (one payment can settle several invoices). Adds `raw_payload` and `assigned_by`. | None; existing rows are kept |
| `properties 0006_retire_duplicate_payment_models_landlord_user` | **Deletes** `properties.MpesaTransaction` and `properties.PaymentAllocation`, the duplicate payment tables, and adds `Landlord.user`. | **Drops those two tables.** Nothing in the code ever wrote to them. If you entered rows by hand, export them first. |
| `properties 0007`, `tenants 0003` | `registration_date` and `entry_date` defaulted to a datetime, which made creating a landlord through the API crash with a 500. They now default to today's date. | None |
| `tenants 0002_alter_tenant_id` | The pending tenant_id migration from `fix/2-tenant-id-migration`. | None |

Tested by migrating a database at the old schema, inserting an invoice, payment and settlement, then migrating forward: all rows survived and numbering continued.

## What was broken and is fixed

| Review item | Fix |
|---|---|
| C1 invoice generator could not run | Rewritten in `billing/services.py` against the real fields. Idempotent per tenant, unit and month. Rent, water, garbage, security, electricity and internet come from the unit; the deposit is billed **only on a lease's first invoice**. |
| C2 settlement crashed and never changed balances | Rewritten in `mpesa/services.py`. Balances now come from settlements, so a payment actually reduces what is owed. |
| C3 two payment models | One kept: `mpesa.MpesaTransaction` + `PaymentSettlement`. The duplicates are removed. |
| C4 open money endpoints | Every endpoint needs sign-in and a role (below). |
| C5 unverified webhooks, leaked errors, missing templates | Callback URLs contain a secret; errors are logged, not returned; the broken template views are replaced by a JSON API. |
| H1 permissions never worked | One role system (`User.roles`), enforced by `RolePermission`. |
| H2 `?cluster=` crashed | Filters on `unit__property__cluster`. |
| H3 invoice gaps | Invoice numbers come from a locked sequence (the old count-based number collided across months). Status is updated on payment and by `mark_overdue_invoices`. Overpayments are kept as credit and applied to the next invoice. Lists prefetch items and settlements instead of querying per invoice. |
| H4 ambiguous tenant matching | See "How a payment is matched". |
| H5 hardcoded dashboard | `dashboard/metrics/` and `dashboard/payment-summaries/` are computed from real data. The duplicate hardcoded copies were removed. |
| Login | The root URLs shadowed the custom login view, so the role was never returned. Removed the duplicate token routes. |
| Invoice header | Agency name, address and contacts now come from the Agency profile, not hardcoded strings. |

## Roles

Roles are `UserRole` rows assigned through `User.roles`. Django groups with the same name are still honoured so older accounts keep working until `seed_roles` is run.

| Area | Super Admin / Admin | Accountant | Secretary | Field Agent |
|---|---|---|---|---|
| View portfolio (tenants, properties, units, landlords, leases) | yes | yes | yes | yes |
| Change portfolio, move tenants in and out | yes | no | yes | no |
| View invoices, deposits, vote heads, banks | yes | yes | yes | yes |
| Generate / raise invoices, refund deposits, edit vote heads and banks | yes | yes | no | no |
| View and assign M-Pesa payments | yes | yes | no | no |
| Dashboard metrics | yes | yes | yes | yes |
| Payment summaries | yes | yes | no | no |
| Templates (SMS, agreements) | yes | yes | yes (edit) | view |
| Users, roles, agency profile (edit) | yes | no | no | no |

Superusers pass every check. Tenant and landlord accounts are rejected by every staff endpoint; they get their own portal.

## Sign-in and `/me/`

`POST /api/v1/token/` returns `access`, `refresh` and a `user` object (`role`, `roles`, `permissions`). `GET /api/v1/me/` (also at `/auth/me/`) returns:

```json
{ "id": 4, "username": "alice", "full_name": "Alice W", "role": "admin",
  "roles": ["Accountant"], "permissions": [], "tenant_id": null, "landlord_id": null }
```

`role` is the portal: `admin` for any staff role, `tenant` or `landlord` when the user is linked to a `Tenant` (`Tenant.user`) or `Landlord` (`Landlord.user`, new), otherwise `null`. The Next.js app reads this to choose the portal. Access tokens now last `JWT_ACCESS_MINUTES` (default 60) instead of a day.

## M-Pesa

**Callback URLs.** Register these with Daraja, replacing `<secret>` with `MPESA_CALLBACK_SECRET`:

```
https://<host>/api/v1/mpesa/c2b/<secret>/validation/
https://<host>/api/v1/mpesa/c2b/<secret>/confirmation/
```

A wrong secret gets a 404, the same as any unknown URL. Validation accepts everything, so a mistyped account number never blocks a tenant's payment; mismatches are sorted out afterwards. Confirmation stores the payload (kept in `raw_payload`) before trying to settle, so a matching problem never loses a payment.

**How a payment is matched.** In order:
1. the account reference equals a tenant ID (any status);
2. the account reference equals a unit name of a current (non-vacated) tenant. If two properties have a unit with that name, the sender's phone must pick exactly one, otherwise the payment is left unmatched;
3. if the reference is blank or matches nothing, the sender's phone, when exactly one current tenant has it.

Anything else stays **unmatched**. Staff assign it from the app, or the API.

**How it is applied.** To the tenant's open invoices, oldest due date first. Each invoice gets its own settlement row, so one payment can clear several. Whatever is left is stored as credit (a settlement with no invoice) and used automatically when the tenant's next invoice is generated. The same `TransID` delivered twice is applied once.

## API additions

| Endpoint | Roles | Purpose |
|---|---|---|
| `GET /mpesa/transactions/?status=settled\|unmatched` | finance | List payments with tenant and allocations |
| `POST /mpesa/transactions/<id>/assign/` `{tenant}` | finance | Assign an unmatched payment (records who did it) |
| `POST /mpesa/transactions/<id>/retry/` | finance | Re-run automatic matching |
| `POST /billing/invoices/generate-monthly/` `{period?: "YYYY-MM"}` | finance | Bill everyone for a month; returns `created` and `skipped` |
| `POST /billing/invoices/raise/` `{tenant, period}` | finance | Bill one tenant; 400 if already billed |
| `GET /billing/invoices/?status=&period=&tenant=&property=` | staff | Invoices are read-only; no create, edit or delete |
| `POST /billing/deposits/<id>/refund/` | finance | Only after the tenant has vacated, and only once |
| `POST /tenants/tenants/move-in/` | portfolio | Creates tenant, lease, held deposit, marks the unit occupied, bills the first month |
| `POST /tenants/tenants/<id>/vacate/` | portfolio | Ends the lease, frees the unit, returns the outstanding balance |
| `POST /tenants/tenants/<id>/suspend/` and `/reactivate/` | portfolio | Status changes for current tenants |
| `GET /properties/units/<id>/tenants/` | staff | Current tenants of a unit (the route existed but the action did not) |

These line up with the actions in the new Next.js admin (move in, vacate, raise invoice, assign payment, refund deposit).

## Tenant and landlord portals

A new `portal` app serves the two portals under `/api/v1/portal/`. Each endpoint reads the signed-in user's own tenant or landlord record, so there is no id in the URL to tamper with. Staff get 403 here, and tenants and landlords get 403 on every staff endpoint.

| Endpoint | Who | Returns |
|---|---|---|
| `GET tenant/home/` | tenant | balance, credit, overdue flag, next invoice due, latest 3 invoices and 5 payments, paybill and account number |
| `GET tenant/invoices/[?status=]`, `tenant/invoices/<uuid>/` | tenant | own invoices in the same shape as the staff invoice detail; someone else's invoice is a 404 |
| `GET tenant/payments/` | tenant | own M-Pesa payments with the invoices each was applied to |
| `GET tenant/lease/` | tenant | lease, unit, deposit, landlord name, agency contacts |
| `GET landlord/overview/` | landlord | this month's billed, collected, commission and net payable, per property and in total |
| `GET landlord/properties/`, `landlord/properties/<id>/` | landlord | own properties and their units; tenants by name only; another landlord's property is a 404 |
| `GET landlord/statements/[?months=1..60]` | landlord | a row per month and property: billed, collected, arrears, commission, net |

**How landlord figures work.** The deposit is not counted: it is held for the tenant, not income, so no commission is taken on it. Payments are not tracked per charge, so collected is the amount paid on an invoice, capped at its non-deposit total (the deposit is treated as paid last). Commission is the property's commission % of what was collected.

**Linking accounts.** A portal login is a normal user linked to a `Tenant` (`Tenant.user`) or `Landlord` (`Landlord.user`) record. There is no screen for this yet; set it in the Django admin. `/me/` then returns `role: "tenant"` or `"landlord"` with the record id.

Money in these responses is a 2-decimal string, as everywhere else in the API.

## Running the checks

```bash
cd backend
DB_ENGINE=sqlite DJANGO_SECRET_KEY=test python manage.py test      # 76 tests, no Postgres needed
python manage.py makemigrations --check --dry-run                  # CI also runs this
```

`DB_ENGINE=sqlite` is for local checks only; CI and production use Postgres. The tests cover invoice generation, settlement (splitting, credit, duplicates, ambiguity), the webhook secret, the role matrix across every endpoint, move-in and vacate, and the filters.

## Not done yet

- Audit log of money and permission changes, SMS sending, and `UnitCharge` history (DESIGN.md section 8).
- Throttling on login and the webhook, and a `/health/` endpoint.
- The legacy frontend sends a token for a day and never refreshes it; with 60-minute tokens it needs a refresh call (the Next.js app does not use this path).
- `Tenant.tenant_id` is `PROP-<random>`; the new UI shows `TN-0001` style codes. Pick one.
