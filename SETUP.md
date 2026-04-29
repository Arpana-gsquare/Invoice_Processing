# InvoiceIQ – Setup & Run Guide

## Prerequisites
- Python 3.10+
- MongoDB 6+ (local or Atlas)
- Google Gemini API key (from https://aistudio.google.com/)

---

## 1. Install Dependencies

```bash
pip install -r requirements.txt
```

> On Linux/Mac, if you hit system package conflicts: `pip install -r requirements.txt --break-system-packages`

> **pdf2image** on Ubuntu also needs poppler: `sudo apt install poppler-utils`

---

## 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and set:

```
GEMINI_API_KEY=your-api-key-here
MONGODB_URI=mongodb://localhost:27017/
MONGODB_DB_NAME=invoice_processor
FLASK_SECRET_KEY=some-long-random-string
```

---

## 3. Start MongoDB

```bash
# Local MongoDB
mongod --dbpath /data/db

# Or with Docker:
docker run -d -p 27017:27017 --name mongo mongo:7
```

---

## 4. Run the App

```bash
python run.py
```

Open: **http://localhost:5000**

Default login: `admin@company.com` / `Admin@123456`

---

## 5. Production Deployment

```bash
gunicorn -w 4 -b 0.0.0.0:5000 "run:app"
```

---

## Folder Structure

```
invoice_processing/
├── run.py                          # Entry point
├── requirements.txt
├── .env.example
├── uploads/                        # Stored invoice files
└── app/
    ├── __init__.py                 # Flask app factory
    ├── config.py                   # All configuration
    ├── extensions.py               # MongoDB init + indexes
    ├── models/
    │   ├── invoice.py              # Invoice CRUD + schema
    │   ├── user.py                 # User + Flask-Login
    │   └── audit.py               # Immutable audit trail
    ├── services/
    │   ├── gemini_service.py       # AI extraction (Gemini 2.5)
    │   ├── fraud_detection.py      # Risk scoring engine
    │   └── export_service.py      # CSV + Excel export
    ├── blueprints/
    │   ├── auth/routes.py          # Login/logout/user mgmt
    │   ├── dashboard/routes.py     # KPI dashboard
    │   ├── invoices/routes.py      # Upload/list/detail/approve
    │   └── api/routes.py           # REST JSON API
    ├── utils/
    │   ├── validators.py           # Field validation + currency
    │   └── helpers.py             # File save, pagination, filters
    └── templates/
        ├── base.html
        ├── dashboard.html
        ├── auth/ (login, users)
        └── invoices/ (upload, list, detail)
```

---

## REST API Endpoints

| Method | URL | Description |
|--------|-----|-------------|
| GET | /api/v1/invoices | List invoices (filterable, paginated) |
| GET | /api/v1/invoices/:id | Get invoice detail |
| PATCH | /api/v1/invoices/:id | Update editable fields |
| POST | /api/v1/invoices/:id/status | Approve/reject |
| POST | /api/v1/invoices/upload | Upload via API |
| GET | /api/v1/stats | Dashboard statistics |
| GET | /api/v1/vendors | Vendor list with risk summary |
| GET | /api/v1/invoices/:id/audit | Audit trail |

---

## Gemini Prompt Design

The extraction prompt in `app/services/gemini_service.py` uses:
- **Strict JSON schema** with every field documented
- **Low temperature (0.1)** for deterministic extraction
- **Explicit currency normalisation** instructions
- **Multi-page PDF support** via PyMuPDF page rendering
- **Retry logic** with exponential backoff (3 attempts)
- **Fallback JSON parsing** to strip markdown fences if model wraps output

---

## Risk Detection Logic

| Check | Method | Weight |
|-------|--------|--------|
| Duplicate invoice | Exact + fuzzy matching (invoice#, vendor, amount, month) | 100 pts |
| Amount anomaly | Z-score vs global + per-vendor history (threshold: 2.5σ) | 40 pts |
| Vendor risk | Rejection rate + overdue rate + prior flags | 0–30 pts |
| Data quality | Missing fields + math inconsistency | 5–15 pts |

**Classification:**
- 0–29 pts → **SAFE**
- 30–69 pts → **MODERATE**
- 70+ pts → **HIGH RISK**
- Duplicate detected → **DUPLICATE** (regardless of score)
