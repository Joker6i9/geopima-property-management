# Geopima Property Management

Property management system for rental agencies: landlords, properties, units, tenants, invoicing, M-Pesa/Coop collections, SMS and reporting.

| Folder | Contents |
|---|---|
| `backend/` | Django + DRF + PostgreSQL API |
| `frontend/` | Next.js (TypeScript, Tailwind) app |
| `legacy-frontend/` | Original static HTML/JS pages, kept as a reference while pages are ported |
| `docs/` | API contract, ERD, decisions |

## Backend setup
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in secret key and Neon DB details (DB_SSLMODE=require)
python manage.py migrate
python manage.py runserver
```

## Frontend setup
```bash
cd frontend
npm install
npm run dev
```

## Workflow
Branches: `type/issue#-short-name` off `main`, squash-merged via PR (`Closes #n`). Commits follow Conventional Commits.
