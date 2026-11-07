# DentaLink User Manual and Setup Guide

DentaLink: A Web-Based Laboratory Information System with Machine Learning-Based Inventory Forecasting for Mrkzter Dental Laboratory

- Live system: https://dentalink-production-1de3.up.railway.app/login
- Source code: https://github.com/Chardiii/DentaLink

## Contents

1. [System requirements](#1-system-requirements)
2. [Local installation](#2-local-installation)
3. [Deployment](#3-deployment)
4. [User roles](#4-user-roles)
5. [Getting started: registration and sign-in](#5-getting-started-registration-and-sign-in)
6. [Dentist / partner clinic guide](#6-dentist--partner-clinic-guide)
7. [Technician guide](#7-technician-guide)
8. [Administrator guide](#8-administrator-guide)
9. [QR dispatch workflow](#9-qr-dispatch-workflow)
10. [Inventory forecasting](#10-inventory-forecasting)
11. [Reports and printing](#11-reports-and-printing)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. System requirements

**To use the system:** any device with a modern web browser (Chrome, Edge, Firefox, or Safari) and an internet connection. A phone with a camera is needed to scan QR dispatch labels.

**To run it locally:**

| Requirement | Version / notes |
|---|---|
| Python | 3.11 or later |
| Git | any recent version |
| Supabase project | provides the database, authentication, and file storage |
| Brevo account (optional) | for account approval and backup failure emails |

## 2. Local installation

### 2.1 Get the code

```bash
git clone https://github.com/Chardiii/DentaLink.git
cd DentaLink
```

### 2.2 Create a virtual environment and install dependencies

Windows:

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

macOS / Linux:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2.3 Configure environment variables

Create a file named `.env` in the project folder. It is listed in `.gitignore`, so it is never committed. Fill in your own values:

```
FLASK_SECRET_KEY=any-long-random-string
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_PUBLISHABLE_KEY=your-supabase-publishable-key
SUPABASE_SERVICE_ROLE_KEY=your-supabase-service-role-key
APP_URL=http://localhost:5000
BREVO_SMTP_PASS=your-brevo-api-key
BREVO_SENDER_EMAIL=your-sender@example.com
```

| Variable | Purpose |
|---|---|
| `FLASK_SECRET_KEY` | Signs user sessions. Use a long random value. |
| `SUPABASE_URL` | Your Supabase project URL (Project Settings → API). |
| `SUPABASE_PUBLISHABLE_KEY` | Public key used for user sign-in. |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only key for administrative database access. Never share it or put it in front-end code. |
| `APP_URL` | Public address of the app. Used in emails and in the links inside QR dispatch labels. |
| `BREVO_SMTP_PASS`, `BREVO_SENDER_EMAIL` | Email sending for account approval or rejection and backup failure alerts. |

### 2.4 Prepare the database

1. In your Supabase project, make sure the core tables exist (`profiles`, `dental_cases`, `case_teeth`, `case_files`, `case_status_history`, `inventory`, `inventory_logs`, `material_requests`, `notifications`).
2. Run each SQL file in `supabase/migrations/` **in filename order**, using the Supabase SQL Editor or the Supabase CLI (`supabase db push`).
3. Create a Storage bucket named `dental-case-files` for case attachments. The scheduled backups migration creates a private `system-backups` bucket for saved backups.

### 2.5 Run the application

```bash
python app.py
```

Open http://localhost:5000 in your browser.

## 3. Deployment

DentaLink runs on [Railway](https://railway.app), with Supabase providing the database, authentication, and file storage.

1. Push the code to GitHub.
2. In Railway, choose **New Project → Deploy from GitHub repo** and select `DentaLink`.
3. Under the service's **Variables** tab, add every variable from section 2.3. Set `APP_URL` to the Railway public URL (for example `https://dentalink-production-1de3.up.railway.app`).
4. Railway installs `requirements.txt` and starts the app with the `Procfile` command: `gunicorn --bind 0.0.0.0:5000 app:app`.
5. Under **Settings → Networking**, generate a public domain.
6. Each new push to the `main` branch redeploys the app automatically.

> `APP_URL` must match the public address. QR dispatch labels contain this address, so labels printed with the wrong value will not open on a phone.

## 4. User roles

| Role | Who | Main tasks |
|---|---|---|
| **Administrator** | Laboratory owner / administrators | Approve users, assign cases, quality-control review, dispatch, inventory, forecasting, analytics, reports, backups |
| **Technician** | CAD designers and porcelain/denture technicians | Work on assigned cases, update case stages, log materials used, request restocks, view reports |
| **Dentist** | Dentists and staff of partner clinics | Submit job orders, upload files, track cases, receive deliveries, report issues, view reports |

## 5. Getting started: registration and sign-in

1. Open the system and click **Register**.
2. Fill in your name, email, password, and role. Dentists **must** enter their clinic name; the administrator confirms or corrects it when approving the account.
3. Read and accept the **Terms and Conditions**, then submit.
4. Verify your email address using the link sent to you.
5. Wait for an administrator to **approve** your account. You will receive an email once it is approved.
6. Sign in on the **Login** page. If you forget your password, use **Forgot password** to receive a reset link.

> Administrator accounts can be signed in on only one device at a time. Signing in on a new device signs the other one out.

## 6. Dentist / partner clinic guide

### 6.1 Submit a new case (job order)
1. From the **Dentist Dashboard**, click **New Case**.
2. Enter the patient and case details. Patient names from your previous cases are suggested as you type.
3. Select the teeth on the odontogram and choose the restoration type, material, and shade.
4. Attach the intraoral scan (STL) and clinical photos. Each file shows a preview with a remove button.
5. Click **Dispatch to Lab** to send the case, or **Save As Draft** to finish it later. Both ask you to confirm first.

You can **edit** a case while it is a draft or after the laboratory returns it for revision. Only drafts can be **deleted**; once a case is sent, contact the laboratory to cancel it.

### 6.2 Track your cases
- The dashboard lists your cases and their current stage, updated live.
- Open a case to see its summary, attached files, and status history.
- Notifications appear when your case changes stage, is returned for revision, or is dispatched.
- For a remake, use **View original job order** to see the case it replaces.
- Use **Print job order** for a one-page job order slip.

### 6.3 Receive a delivery
1. When the courier arrives, scan the **QR label** on the delivery box with your phone.
2. Sign in if asked, check that the case is yours, and confirm **Received**.

### 6.4 Report a problem
If a delivered restoration has a problem, open the case and choose **Report Issue**. Describe the problem and attach photos if needed. The laboratory reviews the report and may create a **remake** linked to the original case.

## 7. Technician guide

### 7.1 View assigned cases
The **Technician Dashboard** shows the cases assigned to you, with rush cases highlighted.

### 7.2 Update a case stage
Open a case and update its stage as you work. The bench stages are:

`Assigned → In Progress → CAD Designing → Wax-up / Try-in → Milling / Printing → Packing and Curing → Finishing and Quality Check`

Use **Save & Advance Stage** (or **Done** on the pipeline) to move a case forward. Each change is recorded in the case's status history with the date and time.

### 7.3 Log materials used
At **Milling**, **Packing**, and **Finishing**, you must record the materials used before the case can move on:

1. DentaLink fills in typical amounts from the prescription, based on the administrator's usage rules. Check and adjust them.
2. For **zirconia**, choose the open disc you milled from, or enter the ID written on a **new disc's box** to open it. Opening a disc takes one disc from stock. Tick **Disc is now used up** when a disc is finished.
3. Save. The amounts are deducted from inventory.

If inventory is short, saving is blocked and the case stays at its current stage. Use **Request restock** in the message; the administrator is notified under **Restock Requests**.

### 7.4 Submit for quality control
When the work is finished, choose **Submit for QC review**. The case moves to the administrator for quality-control review.

### 7.5 Return a case to the dentist
If information or files are missing, choose **Return to the dentist** and explain what is needed.

### 7.6 Request materials
If you need materials that are low or out of stock, use **Request restock** on the case. The administrator sees and resolves the request.

### 7.7 Reports
Open **Reports** to print your production summary or workload sheet. See [section 11](#11-reports-and-printing).

## 8. Administrator guide

### 8.1 Dashboard
Shows pending user approvals, active cases by stage, cases ready for dispatch, restock requests, and stock alerts.

### 8.2 User management
Review new registrations, then **Approve** or **Reject** each one. When approving a dentist, confirm or correct the clinic name; the approval date is recorded. Rejected accounts can be reopened. **Login History** lists sign-in activity.

### 8.3 Case management
- **Assign** submitted cases to a technician.
- **Reassign** a case to another technician. The dropdown shows each technician's open cases, and a confirmation names both technicians. The previous technician is notified and the change is logged in the case history.
- Mark urgent cases as **Rush**.
- Open any case to see its details, files, and full history.

### 8.4 Quality-control review
Cases submitted for review appear in the review list. **Approve QC** to make a case ready for dispatch, or **Reject QC** to send it back to the technician with notes.

### 8.5 Dispatch
See [section 9](#9-qr-dispatch-workflow).

### 8.6 Delivery issues
Review issue reports from clinics under **Delivery Issues** and decide on each one, for example by creating a remake.

### 8.7 Inventory
- **Inventory:** add, edit, delete, and adjust stock items.
- **Usage rules:** for each material, set the stage it is used at (Milling, Packing, or Finishing), the case types it applies to, the basis (per unit, per arch, or per case), and the typical amount. These fill in technicians' material logs automatically.
- **Zirconia discs:** materials marked to track discs list each **open disc** by the ID written on its box, with the units milled from it. Use **Mark used up** when a disc is finished; units per disc show the real yield.
- **Audit log:** history of every stock change.
- **Restock requests:** resolve technicians' material requests.

### 8.8 Job order history
Use **Import job orders** to add the laboratory's past paper job orders. The page lists every finished job order: imported paper orders and cases delivered through DentaLink. Both count as real demand in Forecast Analytics.

### 8.9 Analytics
**Analytics** shows job orders per month, top clinics, the prosthesis mix, clinic rankings with change from last month, new partner clinics (including those with no orders yet), clinics with no orders in 60 days, and on-time delivery and remake rate trends. Each chart can also be shown as a table. Analytics counts DentaLink cases only, not imported paper job orders.

### 8.10 Backup and restore
Under **Backup & Recovery**:

- **Create a backup** downloads a ZIP of the database records and case files, and keeps a copy under **Saved backups**.
- **Automatic backups:** set **How often** (off, daily, weekly, or monthly), the time (Philippine time), and how many backups to keep (default 7), then click **Save schedule**. DentaLink checks every minute and makes the backup when it is due; older backups are deleted automatically. If a backup fails, every administrator is notified in the app and by email.
- **Import a previous archive** restores data from a ZIP. Matching records may be overwritten, records not in the archive are kept, and secret keys are never included.

Backup archives are not encrypted and may contain patient data, so store downloaded copies securely.

## 9. QR dispatch workflow

1. **Approve QC** on a finished case. It becomes **Ready for Dispatch**.
2. Open the case's **Dispatch Slip** and print it. The slip has a QR label linked to the case.
3. Attach the label to the delivery box.
4. At handover, the administrator or a technician scans the QR code, enters the **courier's name**, and confirms dispatch. The case becomes **In Transit**.
5. At the clinic, the dentist scans the same QR code and confirms receipt. The case becomes **Delivered**.

Every scan is logged. A label opened by the wrong clinic, or a label that has been **reissued** (voided), is refused, and the attempt is recorded.

## 10. Inventory forecasting

Open **Forecast Analytics** from the administrator menu.

1. Click **Run Forecast**. A run takes about a minute.
2. The engine builds weekly demand for each **zirconia disc shade (A1, A2, A3, OM)** and for **e.max**, backtests the naïve method, a 4-week moving average, simple exponential smoothing, Croston's method, and a Random Forest over the last 26 weeks, and keeps the most accurate model for each series.
3. The page shows, for each material:
   - forecast demand for the next 4 weeks
   - projected stock levels compared with current inventory
   - stockout risk
   - recommended order quantity and reorder point
4. Use **Download Excel** or **Printable report** to save or print the results. The report shows when the forecast was generated.

> The forecasts support the administrator's purchasing decisions; the system never places orders automatically. Because the laboratory had no past consumption records, forecasts start from a simulated history based on the laboratory's profile and improve as real job orders accumulate.

## 11. Reports and printing

**Reports** is available to every role. Each role sees only its own reports and data:

| Role | Reports |
|---|---|
| Administrator | Job Orders Summary (including imported paper job orders), Turnaround & On-Time Delivery, Remakes & Quality, Material Usage & Stock |
| Technician | My Production Summary, My Workload Sheet |
| Dentist | Case History (filter by patient for a patient's case record), Lab Service Summary |

Choose a report and a **Period** (monthly, quarterly, annual, or all time), then click **Show report**. Reports can be printed or downloaded as a formatted Excel file.

**Print job order** produces a one-page job order slip from the dentist, technician, or administrator case page. For dispatch slips, see [section 9](#9-qr-dispatch-workflow).

## 12. Troubleshooting

| Problem | Solution |
|---|---|
| "Account pending" after sign-in | An administrator has not approved the account yet. |
| No verification or approval email | Check the spam folder, and check the Brevo variables in section 2.3. |
| QR label opens the wrong address | `APP_URL` was wrong when the slip was printed. Fix `APP_URL`, then **reissue** the dispatch label. |
| "Label voided" when scanning | The label was reissued. Use the newest printed slip. |
| Forecast run fails with "Need at least … weeks of history" | Use the simulated history, or wait until enough real job orders exist. |
| App fails to start locally | Check that `.env` exists and every variable in section 2.3 is set. |
| Technician can't move a case past Milling, Packing, or Finishing | Materials must be logged first. If inventory is short, restock or correct the count, or the technician can use **Request restock**. |
| "Automatic backups need the scheduled backups migration" | Run `supabase/migrations/20261108000000_scheduled_backups.sql` in Supabase. |
