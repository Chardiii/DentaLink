r"""Sample data for demonstrations: 14 dentists and 15 technicians (approved, email already
confirmed, so no emails are sent) and about 80 job orders spread over the last 8 weeks, in every
stage from draft to delivered, with their status history, dispatch labels and two open
delivery-issue reports. Inventory is not touched.

Accounts use @example.com addresses (reserved, never deliverable), so they're easy to find and
remove later. Re-running skips accounts that already exist and refuses to add cases twice.

The password for the sample accounts is not stored in this repository. Set it first:
    PowerShell:  $env:SAMPLE_ACCOUNT_PASSWORD = "..."   (8+ characters, upper, lower, number)
Then, from the project folder:
    venv\Scripts\python.exe scripts\seed_sample_data.py --dry   # preview, writes nothing
    venv\Scripts\python.exe scripts\seed_sample_data.py         # write to the database in .env
"""
import os, sys, json, random, secrets, tempfile, uuid
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))
from supabase import create_client

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
DRY = "--dry" in sys.argv

PASSWORD = os.environ.get("SAMPLE_ACCOUNT_PASSWORD", "")
if not DRY and not (len(PASSWORD) >= 8 and any(ch.isupper() for ch in PASSWORD)
                    and any(ch.islower() for ch in PASSWORD) and any(ch.isdigit() for ch in PASSWORD)):
    sys.exit("Set SAMPLE_ACCOUNT_PASSWORD (8+ characters with an uppercase letter, a lowercase letter and a number).")

_admin = (sb.table("profiles").select("id, display_name").eq("role", "admin").eq("status", "approved")
          .order("created_at").limit(1).execute().data)
if not _admin:
    sys.exit("No approved admin account found; the sample cases need one to assign and dispatch them.")
ADMIN_ID = _admin[0]["id"]
ADMIN_NAME = _admin[0].get("display_name") or "Admin"
TERMS_VERSION = "2026-11-04"
PH = timezone(timedelta(hours=8))
NOW = datetime.now(timezone.utc)
rng = random.Random(20261117)
iso = lambda t: t.astimezone(timezone.utc).isoformat()

DENTISTS = [
    ("Maria", "Santos", "Santos Family Dental Clinic"),
    ("Jose", "Reyes", "Reyes Dental Care"),
    ("Ana", "Cruz", "Smile Avenue Dental Clinic"),
    ("Mark", "Bautista", "Bautista Dental Center"),
    ("Carla", "Mendoza", "Mendoza Dental and Orthodontic Clinic"),
    ("Paolo", "Garcia", "Garcia Dental Studio"),
    ("Liza", "Villanueva", "Bright Smile Dental Clinic"),
    ("Ramon", "Aquino", "Aquino Dental Clinic"),
    ("Patricia", "Ramos", "Ramos Family Dentistry"),
    ("Miguel", "Torres", "Torres Dental Care Center"),
    ("Kristine", "Flores", "Pearl White Dental Clinic"),
    ("Daniel", "Navarro", "Navarro Dental Hub"),
    ("Angela", "Castillo", "Castillo Dental Clinic"),
    ("Victor", "Dela Rosa", "Dela Rosa Dental Clinic"),
]
TECHS = [
    ("Bryan", "Lopez"), ("Carlo", "Mercado"), ("Jenny", "Pascual"), ("Rico", "Salazar"),
    ("Joanna", "Domingo"), ("Kevin", "Soriano"), ("Mae", "Gutierrez"), ("Arnel", "Fernandez"),
    ("Lea", "Manalo"), ("Noel", "Valdez"), ("Grace", "Tolentino"), ("Jayson", "Robles"),
    ("Rowena", "Castro"), ("Dennis", "Ocampo"), ("Shiela", "Francisco"),
]
slug = lambda s: s.lower().replace(" ", "")


def account_rows():
    rows = []
    for i, (first, last, clinic) in enumerate(DENTISTS):
        rows.append(dict(first_name=first, last_name=last, display_name=f"Dr. {first} {last}", role="dentist",
                         clinic_name=clinic, phone=f"0917555{i + 101:04d}", professional_id=f"SAMPLE-D{i + 1:02d}",
                         email=f"dr.{slug(first)}.{slug(last)}@example.com"))
    for i, (first, last) in enumerate(TECHS):
        rows.append(dict(first_name=first, last_name=last, display_name=f"{first} {last}", role="technician",
                         clinic_name=None, phone=f"0918555{i + 201:04d}", professional_id=f"SAMPLE-T{i + 1:02d}",
                         email=f"{slug(first)}.{slug(last)}.tech@example.com"))
    return rows


def retry(fn, tries=5):
    import time
    for n in range(tries):
        try:
            return fn()
        except Exception as exc:
            if n == tries - 1:
                raise
            print("retrying after:", type(exc).__name__, str(exc)[:120])
            time.sleep(3 * (n + 1))


def existing_users():
    found, page = {}, 1
    while True:
        users = retry(lambda: sb.auth.admin.list_users(page=page, per_page=1000))
        for u in users:
            found[(u.email or "").lower()] = u.id
        if len(users) < 1000:
            return found
        page += 1


def create_accounts():
    have = existing_users()
    accounts = []
    for n, a in enumerate(account_rows()):
        signed_up = datetime(2026, 9, 1, 9, 0, tzinfo=PH) + timedelta(days=n % 10, hours=rng.randint(0, 8))
        uid = have.get(a["email"])
        if not uid and not DRY:
            try:
                uid = sb.auth.admin.create_user({"email": a["email"], "password": PASSWORD, "email_confirm": True}).user.id
            except Exception as exc:   # a timed-out request may still have created the user
                print("create_user", a["email"], "->", type(exc).__name__, str(exc)[:120])
                uid = existing_users().get(a["email"]) or retry(
                    lambda: sb.auth.admin.create_user({"email": a["email"], "password": PASSWORD, "email_confirm": True})).user.id
        uid = uid or f"dry-{n}"
        profile = {k: a[k] for k in ("first_name", "last_name", "display_name", "role", "clinic_name", "phone", "professional_id")}
        profile.update(id=uid, status="approved", created_at=iso(signed_up),
                       approved_at=iso(signed_up + timedelta(hours=rng.randint(2, 20))), approved_by=ADMIN_ID,
                       terms_version=TERMS_VERSION, terms_accepted_at=iso(signed_up),
                       last_seen=iso(NOW - timedelta(days=rng.randint(1, 6), hours=rng.randint(0, 12))))
        if not DRY:
            sb.table("profiles").upsert(profile).execute()
        accounts.append(dict(a, id=uid))
    return accounts


# ---------- job orders ----------
PATIENT_FIRST = ["Juan", "Maria", "Jose", "Ana", "Pedro", "Rosa", "Antonio", "Carmen", "Ricardo", "Elena", "Fernando",
                 "Teresa", "Roberto", "Luz", "Manuel", "Gloria", "Eduardo", "Cristina", "Alfredo", "Marites", "Rogelio",
                 "Josephine", "Benjamin", "Lourdes", "Ernesto", "Imelda", "Danilo", "Remedios", "Rodel", "Nida",
                 "Jerome", "Kimberly", "Christian", "Nicole", "Joshua", "Camille", "Adrian", "Bea", "Francis", "Trisha"]
PATIENT_LAST = ["Dizon", "Santiago", "Lim", "Tan", "Ong", "Chua", "Sy", "Gomez", "Rivera", "Morales", "Perez",
                "Hernandez", "Diaz", "Alvarez", "Romero", "Jimenez", "Medina", "Aguilar", "Vargas", "Ortiz",
                "Panganiban", "Macaraeg", "Dimaculangan", "Magbanua", "Buenaventura", "Pacheco", "Velasco",
                "Manalastas", "Sarmiento", "Ignacio"]
INSTRUCTIONS = [None, None, None, "Light occlusal contact, please.", "Match the shade of the adjacent central incisor.",
                "Patient has a strong bite; keep the occlusal surface thick enough.", "Tight proximal contacts.",
                "Rush: patient is leaving for abroad.", "Natural characterization, slight translucency at the incisal edge.",
                "Please return the models with the case.", "Check clearance against the opposing crown."]
COURIERS = ["Marlon (lab courier)", "Jun (lab courier)", "Lalamove rider", "Grab Express rider"]
REVISION_NOTES = [
    "The margin on the scan for tooth {t} is not clear. Please send a new scan or impression.",
    "The bite registration doesn't match the models. Please send a new bite record.",
]
STAGE_LABELS = {
    "cad_designing": "in CAD Designing (exocad)", "wax_up_or_try_in": "in Wax-up / Tooth Setup",
    "milling_or_printing": "in Milling / 3D Printing", "packing_and_curing": "in Acrylic Packing & Curing",
    "finishing_and_quality_check": "in Finishing & Quality Check",
}
FIXED = ["cad_designing", "milling_or_printing", "finishing_and_quality_check"]
DENTURE = ["wax_up_or_try_in", "packing_and_curing", "finishing_and_quality_check"]


def run_of_teeth(n, anterior=False):
    q = rng.randint(1, 4)
    span = (1, 3) if anterior else (4, 7)
    n = min(n, span[1] - span[0] + 1)
    start = rng.randint(span[0], span[1] - n + 1)
    return [str(q * 10 + s) for s in range(start, start + n)]


def pick_restoration(kind=None):
    kinds = [("zirconia", 50), ("emax", 12), ("veneer", 6), ("inlay", 5), ("pfm", 12), ("denture", 15)]
    if kind == "fixed":
        kinds = [k for k in kinds if k[0] != "denture"]
    if kind == "denture":
        kinds = [("denture", 1)]
    kind = rng.choices([k for k, _ in kinds], [w for _, w in kinds])[0]
    if kind == "zirconia":
        teeth = run_of_teeth(rng.choices([1, 2, 3], [60, 20, 20])[0])
        return "crown", teeth, "zirconia", rng.choice(["A1", "A2", "A2", "A2", "A3", "A3", "A3.5", "B1", "BL2"])
    if kind == "emax":
        return "crown", run_of_teeth(rng.choice([1, 1, 2]), anterior=rng.random() < .6), "emax", rng.choice(["A1", "A2", "B1", "BL2"])
    if kind == "veneer":
        q = rng.choice([1, 2])
        n = rng.choice([2, 4, 6])
        teeth = [str(10 + s) for s in range(1, n // 2 + 1)] + [str(20 + s) for s in range(1, n // 2 + 1)]
        return "veneer", teeth, "emax", rng.choice(["BL2", "BL3", "A1", "B1"])
    if kind == "inlay":
        return "inlay", run_of_teeth(1), "emax", rng.choice(["A2", "A3"])
    if kind == "pfm":
        return "crown", run_of_teeth(rng.choice([1, 2, 3])), "pfm", rng.choice(["A2", "A3", "A3.5"])
    return "partial_denture", run_of_teeth(rng.choice([2, 3, 4])), rng.choice(["acrylic", "acrylic", "flexible_denture"]), rng.choice(["A2", "A3"])


def lab_day_time(days_ago):
    """A weekday (Mon-Sat) business-hours time about `days_ago` days back, in the past."""
    day = (NOW.astimezone(PH) - timedelta(days=days_ago)).date()
    while day.weekday() == 6:
        day -= timedelta(days=1)
    t = datetime(day.year, day.month, day.day, rng.randint(8, 16), rng.randint(0, 59), rng.randint(0, 59), tzinfo=PH)
    return min(t, NOW - timedelta(minutes=rng.randint(40, 200)))


# (status, how many, created between N and M days ago)
PLAN = [
    ("delivered", 46, (7, 56)), ("in_transit", 3, (3, 6)), ("ready_for_dispatch", 3, (3, 6)),
    ("pending_admin_review", 3, (2, 5)), ("finishing_and_quality_check", 3, (2, 5)),
    ("packing_and_curing", 2, (2, 4)), ("milling_or_printing", 3, (1, 4)), ("wax_up_or_try_in", 2, (1, 3)),
    ("cad_designing", 3, (1, 3)), ("assigned", 3, (0, 2)), ("needs_revision", 2, (1, 3)),
    ("submitted", 5, (0, 1)), ("draft", 2, (0, 2)),
]
STEP_HOURS = {"assigned": (1, 5), "stage": (6, 28), "pending_admin_review": (2, 10), "ready_for_dispatch": (1, 5),
              "in_transit": (1, 4), "delivered": (2, 6), "needs_revision": (2, 20)}


def build_case(status, created, dentist, tech):
    kind = "denture" if status in ("wax_up_or_try_in", "packing_and_curing") else (
        "fixed" if status in ("cad_designing", "milling_or_printing") else None)
    case_type, teeth, material, shade = pick_restoration(kind)
    pipeline = DENTURE if case_type == "partial_denture" else FIXED
    rush = rng.random() < .1
    steps = []
    if status not in ("draft", "submitted"):
        steps.append("assigned")
        if status == "needs_revision":
            steps += pipeline[:rng.randint(0, 1)] + ["needs_revision"]
        else:
            order = pipeline + ["pending_admin_review", "ready_for_dispatch", "in_transit", "delivered"]
            if status != "assigned":
                steps += order[:order.index(status) + 1]

    gaps = [rng.uniform(*STEP_HOURS["stage" if s in STAGE_LABELS else s]) * (.6 if rush else 1) for s in steps]
    room = (NOW - timedelta(minutes=rng.randint(20, 300)) - created).total_seconds() / 3600
    if gaps and sum(gaps) > room:
        gaps = [g * room / sum(gaps) for g in gaps]
    t, times = created, []
    for g in gaps:
        t += timedelta(hours=g)
        times.append(t)

    case_id = str(uuid.uuid4())
    case = dict(id=case_id, dentist_id=dentist["id"], technician_id=tech["id"] if steps else None,
                patient_name=f"{rng.choice(PATIENT_FIRST)} {rng.choice(PATIENT_LAST)}",
                patient_reference=f"Chart {rng.randint(1000, 4999)}" if rng.random() < .5 else None,
                case_type=case_type, instructions=rng.choice(INSTRUCTIONS), status=status, is_rush=rush,
                created_at=iso(created), updated_at=iso(times[-1] if times else created),
                revision_notes=None, dispatched_at=None, delivered_at=None)
    teeth_rows = [dict(case_id=case_id, tooth_number=n, restoration_type=case_type, material=material, shade=shade,
                       notes="", created_at=iso(created)) for n in teeth]
    history, dispatches, events = [], [], []
    label_id = None
    for step, when in zip(steps, times):
        if step == "assigned":
            history.append(dict(status="Assigned", changed_by=ADMIN_ID, notes=None))
        elif step in STAGE_LABELS:
            history.append(dict(status=STAGE_LABELS[step], changed_by=tech["id"], notes=f"Updated by technician {tech['display_name']}"))
        elif step == "needs_revision":
            note = rng.choice(REVISION_NOTES).format(t=teeth[0])
            case["revision_notes"] = note
            history.append(dict(status="Returned to dentist for revision", changed_by=tech["id"], notes=note))
        elif step == "pending_admin_review":
            history.append(dict(status="Submitted for admin QC", changed_by=tech["id"], notes=f"Submitted by technician {tech['display_name']}"))
        elif step == "ready_for_dispatch":
            label_id = str(uuid.uuid4())
            dispatches.append(dict(id=label_id, case_id=case_id, token=secrets.token_urlsafe(16), status="active",
                                   created_by=ADMIN_ID, created_at=iso(when)))
            events.append(dict(case_id=case_id, dispatch_id=label_id, event_type="issued", actor_id=ADMIN_ID, created_at=iso(when)))
            history.append(dict(status="QC approved, ready for dispatch", changed_by=ADMIN_ID, notes=f"Approved by {ADMIN_NAME}"))
        elif step == "in_transit":
            courier, web = rng.choice(COURIERS), rng.random() < .5
            case["dispatched_at"] = iso(when)
            events.append(dict(case_id=case_id, dispatch_id=label_id, event_type="dispatched", actor_id=ADMIN_ID,
                               courier_name=courier, notes="Recorded on the web (label not scanned)" if web else None, created_at=iso(when)))
            history.append(dict(status="Dispatched (in transit)", changed_by=ADMIN_ID,
                                notes=f"Handed to courier {courier} by {ADMIN_NAME}" + (" (recorded on the web)" if web else "")))
        elif step == "delivered":
            case["delivered_at"] = iso(when)
            if rng.random() < .65:   # the clinic confirmed receipt in DentaLink
                who = rng.choice([dentist["display_name"], "Front desk", "Clinic assistant"])
                events.append(dict(case_id=case_id, dispatch_id=label_id, event_type="delivered", actor_id=dentist["id"],
                                   received_by=who, created_at=iso(when)))
                history.append(dict(status="Delivered", changed_by=dentist["id"], notes=f"Received by {who}"))
            else:                    # the lab recorded it (courier confirmed drop-off)
                who = rng.choice(["Front Desk", "Clinic receptionist"])
                events.append(dict(case_id=case_id, dispatch_id=label_id, event_type="delivered", actor_id=ADMIN_ID,
                                   received_by=who, notes=f"Marked delivered by the lab ({ADMIN_NAME})", created_at=iso(when)))
                history.append(dict(status="Delivered", changed_by=ADMIN_ID, notes=f"Received by {who} (recorded by the lab)"))
        history[-1].update(case_id=case_id, created_at=iso(when))

    created_day = created.astimezone(PH).date()
    if status == "delivered":
        done_day = times[-1].astimezone(PH).date()
        slack = rng.choices([-2, -1, 0, 1, 2, 3], [4, 8, 20, 30, 25, 13])[0]   # about 1 in 8 late
        due = max(done_day + timedelta(days=slack), created_day + timedelta(days=1))
    else:
        due = created_day + timedelta(days=rng.randint(2, 3) if rush else rng.randint(5, 9))
    case["due_date"] = due.isoformat()
    return case, teeth_rows, history, dispatches, events


def create_cases(accounts):
    dentists = [a for a in accounts if a["role"] == "dentist"]
    techs = [a for a in accounts if a["role"] == "technician"]
    if not DRY:
        already = sb.table("dental_cases").select("id").in_("dentist_id", [d["id"] for d in dentists]).limit(1).execute().data
        if already:
            sys.exit("Sample dentists already have cases; not adding them twice.")
    dentist_weights = [rng.choice([1, 2, 3, 4]) for _ in dentists]
    built = []
    for status, count, (lo, hi) in PLAN:
        for _ in range(count):
            created = lab_day_time(rng.uniform(lo, hi))
            dentist = rng.choices(dentists, dentist_weights)[0]
            built.append(build_case(status, created, dentist, rng.choice(techs)))
    built.sort(key=lambda b: b[0]["created_at"])

    latest = (sb.table("dental_cases").select("case_number").like("case_number", "DL-%")
              .order("case_number", desc=True).limit(1).execute().data)
    next_no = int(latest[0]["case_number"].split("-")[1]) + 1 if latest else 1
    for i, b in enumerate(built):
        b[0]["case_number"] = f"DL-{next_no + i:05d}"

    cases = [b[0] for b in built]
    teeth = [t for b in built for t in b[1]]
    history = [h for b in built for h in b[2]]
    dispatches = [d for b in built for d in b[3]]
    events = [e for b in built for e in b[4]]

    # Two open delivery-issue reports on recent deliveries, for the Delivery Issues page.
    recent = [c for c in cases if c["status"] == "delivered" and c["delivered_at"] > iso(NOW - timedelta(days=14))]
    issues = []
    for c, (kind, details) in zip(recent[-2:], [("poor_fit", "The crown is high on the bite; the patient feels it when closing."),
                                                 ("wrong_shade", "The shade looks lighter than ordered under the clinic light.")]):
        when = min(datetime.fromisoformat(c["delivered_at"]) + timedelta(hours=20), NOW - timedelta(hours=3))
        issues.append(dict(case_id=c["id"], dentist_id=c["dentist_id"], issue_type=kind, details=details, status="open",
                           created_at=iso(when)))

    if not DRY:
        sb.table("dental_cases").insert(cases).execute()
        sb.table("case_teeth").insert(teeth).execute()
        sb.table("case_status_history").insert(history).execute()
        if dispatches:
            sb.table("case_dispatches").insert(dispatches).execute()
        if events:
            sb.table("case_dispatch_events").insert(events).execute()
        if issues:
            sb.table("case_issue_reports").insert(issues).execute()
    return cases, teeth, issues


accounts = create_accounts()
cases, teeth, issues = create_cases(accounts)
summary = {s: sum(1 for c in cases if c["status"] == s) for s, _, _ in PLAN}
print("accounts:", len(accounts), "cases:", len(cases), "units:", len(teeth), "issues:", len(issues))
print(json.dumps(summary))
print("first/last:", cases[0]["case_number"], cases[0]["created_at"], cases[-1]["case_number"], cases[-1]["created_at"])
late = [c for c in cases if c["status"] == "delivered" and c["delivered_at"][:10] > c["due_date"]]
print("delivered late:", len(late))
bad = [c["case_number"] for c in cases if c["updated_at"] > iso(NOW)]
print("future timestamps:", bad)
result_path = os.path.join(tempfile.gettempdir(), "dentalink_seed_result.json")
with open(result_path, "w") as f:
    json.dump({"accounts": accounts, "cases": [{k: c[k] for k in ("id", "case_number", "status", "dentist_id", "technician_id")}
                                               for c in cases]}, f, indent=1)
print("account and case list written to", result_path)
