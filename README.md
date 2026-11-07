# DentaLink

**A Web-Based Laboratory Information System with Machine Learning-Based Inventory Forecasting for Mrkzter Dental Laboratory**

Capstone project, Bachelor of Science in Information Technology (Web and Mobile Application Development)
College of Computer Studies, Laguna State Polytechnic University, Sta. Cruz, Laguna

- **Project website:** https://chardiii.github.io/DentaLink/
- **Live system:** https://dentalink-production-1de3.up.railway.app/login
- **User manual and setup guide:** [docs/USER_MANUAL.md](docs/USER_MANUAL.md)

## Team

| Name | Role |
|---|---|
| Jamelle Ivan C. Lao | Researcher / Developer |
| Shiella Mae B. Rico | Researcher / Developer |
| Cliff Richard R. Veluz | Researcher / Developer |
| Gener F. Mosico, MSIT | Thesis Adviser |

## About the project

Mrkzter Dental Laboratory in Marikina City handled case communication through Messenger and Viber, received handwritten job orders from partner clinics, had no real-time view of case progress, kept no record of deliveries, and kept no inventory records. DentaLink brings these activities into one web-based system.

**General objective:** to develop a web-based laboratory information system that improves the management of dental cases, laboratory workflow, client orders, digital case files, and inventory at Mrkzter Dental Laboratory.

**Specific objectives:**

1. Design and develop a web-based laboratory information system with:
   - Dental Case and Client Order Management
   - Digital Case-File Management
   - Real-Time Departmental Workflow Tracking with QR-Enabled Dispatch
   - Laboratory Inventory Management
2. Develop a machine learning-based inventory forecasting module for:
   - Future Material Demand Forecasting
   - Future Inventory-Level Forecasting
   - Stockout-Risk Prediction
   - Replenishment Recommendations
3. Assess the functionality of the developed system based on its key features.
4. Evaluate the system in terms of functionality, usability, and acceptability among its intended users.

## Features

| Module | Description |
|---|---|
| Case and client order management | Dentists submit job orders online with an odontogram, STL scans, and photos; the administrator assigns or reassigns technicians and can flag rush cases. |
| Digital case-file management | Scans, photos, and design files are attached to each case (Supabase Storage). |
| Departmental workflow tracking | Technicians move cases through the bench stages, from CAD design and milling to finishing and quality check, with a full status history. |
| Quality-control review | The administrator approves or rejects each finished case before dispatch. |
| QR-enabled dispatch | Each case gets a printable dispatch slip with a QR label. Scanning it records the courier handover and the clinic's receipt. |
| Delivery issues and remakes | Clinics report problems with delivered cases; remakes are linked to the original case. |
| Inventory management | Stock levels, audit log, restock requests, and usage rules that pre-fill the materials technicians log at milling, packing, and finishing. |
| Zirconia disc tracking | Each opened disc is tracked by the ID written on its box, with the units milled from it, to measure real yield per disc. |
| ML-based inventory forecasting | Forecasts the next 4 weeks of demand for each zirconia disc shade and for e.max, with stock projections, stockout risk, and order recommendations. |
| Job order history | Imports the laboratory's past paper job orders for search and forecasting. |
| Dashboards and notifications | Role-based dashboards and live notifications for administrators, technicians, and dentists. |
| Analytics | Job orders per month, top clinics, prosthesis mix, new and inactive clinics, and on-time and remake rate trends. |
| Reports and printing | Role-specific reports (monthly, quarterly, annual, or all time) downloadable as Excel, and printable job order slips. |
| Administration | User approval with clinic name confirmation, login history, and backup and restore with scheduled automatic backups. |

### Forecasting approach

The forecasting engine (`ml_models/`) builds weekly demand series from job orders, backtests every candidate model on the last 26 weeks with a rolling origin, keeps the most accurate one, and forecasts 4 weeks ahead. The candidate models are the naïve method, a 4-week moving average, simple exponential smoothing, Croston's method for intermittent demand, and a Random Forest. Because the laboratory had no consumption records, the engine can start from a simulated history built from the laboratory's profile.

## Technology stack

| Layer | Technology |
|---|---|
| Back end | Python 3, Flask 3.1, Jinja2 templates |
| Database, authentication, file storage | Supabase (PostgreSQL, Auth, Storage) |
| Machine learning | scikit-learn, pandas, NumPy |
| Reports | openpyxl (Excel) |
| Email | Brevo SMTP |
| Hosting | Railway (Gunicorn) |

## Repository structure

```
DentaLink/
├── app.py                  # Flask application: routes, roles, and business logic
├── ml_models/              # Forecasting engine
│   ├── demand.py           #   weekly demand series from job orders
│   ├── simulate.py         #   simulated history from the laboratory profile
│   ├── lab_profile.py      #   laboratory materials and shade mix
│   ├── models.py           #   candidate forecasting models
│   ├── backtest.py         #   rolling-origin backtesting and model selection
│   ├── forecaster.py       #   forecast generation
│   └── engine.py           #   end-to-end forecast run and stock plan
├── templates/              # HTML pages (Jinja2)
├── static/images/          # Logos and images
├── supabase/migrations/    # SQL migrations for the Supabase database
├── docs/                   # Project website (GitHub Pages), user manual, diagrams
├── requirements.txt        # Python dependencies
└── Procfile                # Railway / Gunicorn start command
```

## Quick start

```bash
git clone https://github.com/Chardiii/DentaLink.git
cd DentaLink
python -m venv venv
venv\Scripts\activate        # Windows  (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
# create a .env file (see docs/USER_MANUAL.md, section 2.3)
python app.py
```

Open http://localhost:5000. Full installation, deployment, and usage steps are in the [user manual](docs/USER_MANUAL.md).
