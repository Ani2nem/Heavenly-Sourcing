# HeavenlySourcing

Restaurant procurement automation: menu parsing → distributor discovery → RFP negotiation → quote approval.

## Stack

- **Backend**: FastAPI + SQLModel + PostgreSQL
- **Frontend**: React 18 + Vite + Tailwind CSS
- **LLM**: OpenAI GPT-4o-mini (menu vision, email parsing, recommendation)
- **Deploy**: Render (Static Site + Web Service + PostgreSQL)

## Local Setup

### Backend

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in your keys
alembic upgrade head         # run migrations
uvicorn main:app --reload    # starts on :8000
```

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local   # set VITE_API_URL if needed
npm run dev                  # starts on :5173
```

## Environment Variables

See `backend/.env.example` and `frontend/.env.example` for all required keys.

## Deployment (Render)

- **Frontend**: Static Site — build command `npm run build`, publish dir `dist/`
- **Backend**: Web Service — start command `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Database**: Render PostgreSQL — attach via `DATABASE_URL`

Set `CORS_ALLOWED_ORIGINS` on the backend service to your frontend Render URL.
