# Aivory Backend (avry-backend)

Core backend API for the Aivory platform. Handles authentication, user management, and orchestration of microservices.

Built with **FastAPI** (Python 3.11+).

## Architecture

```
app/
├── agents/          # AI agent logic
├── database/        # Database connection & session management
├── events/          # Event-driven communication
├── integrations/    # Third-party integrations
├── llm/             # LLM provider abstraction
├── models/          # SQLAlchemy/Pydantic models
├── prompts/         # AI prompt templates
├── routes/          # API route handlers
├── services/        # Business logic layer
├── utils/           # Shared utilities
├── config.py        # Environment configuration
├── main.py          # FastAPI application entry
└── model_config.py  # LLM model configuration
```

## API Endpoints

- `POST /api/v1/auth/register` — User registration
- `POST /api/v1/auth/login` — Login (returns JWT)
- `POST /api/v1/auth/logout` — Logout
- `GET /api/v1/users/me` — Current user profile
- `GET /docs` — Swagger API documentation

## Prerequisites

- Python 3.11+
- PostgreSQL (via Supabase or local)

## Environment Variables

Copy `.env.example` to `.env`:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/aivery
PORT=8081
ENVIRONMENT=development
JWT_SECRET=your_secret_key_here
LOG_LEVEL=INFO
OPENROUTER_KEY=your_key_here
MIDTRANS_SERVER_KEY=your_key_here
```

## Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload
```

The API runs at `http://localhost:8081`. Swagger docs at `http://localhost:8081/docs`.

## Docker

```bash
# Build
docker build -t avry-backend .

# Run
docker run -p 8081:8081 --env-file .env avry-backend
```

### Docker Compose

```bash
docker compose up -d
```

## Database Migrations

```bash
# Apply migration
psql $DATABASE_URL -f migrations/001_backend_independent_schema.sql
```

## VPS Deployment

1. Clone on VPS:
   ```bash
   git clone https://github.com/ClementHansel/avry-backend.git
   cd avry-backend
   ```
2. Create `.env` with production values.
3. Run:
   ```bash
   docker compose up -d --build
   ```

## Related Services

| Service | Repository | Port |
|---------|-----------|------|
| Website | [avry-website](https://github.com/ClementHansel/avry-website) | 9000 |
| User Dashboard | [avry-user-dashboard](https://github.com/ClementHansel/avry-user-dashboard) | 9001 |
| Payments | [avry-payments](https://github.com/ClementHansel/avry-payments) | 8085 |
| All Services | [aivory](https://github.com/ClementHansel/aivory) | — |

## License

Proprietary — Aivory © 2026. All rights reserved.
