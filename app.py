import os
from dotenv import load_dotenv
import random
import re

import oracledb
from flask import Flask, render_template, request, jsonify
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
load_dotenv()

# ORACLE CONFIGURATION
DSN = os.getenv("DB_DSN")
USER = os.getenv("DB_USER")
PASSWORD = os.getenv("DB_PASSWORD")


def conn():
    return oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)


def rows(sql, params=None):
    with conn() as c:
        cur = c.cursor()
        cur.execute(sql, params or {})
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def names(sql):
    return [r["n"] for r in rows(sql)]


TRANSLATE = {
    "Slot nahi mila:": "No free slot found:",
    "Department nahi mila:": "Department not found:",
    "Class, subject ya faculty ka naam match nahi hua":
        "Class, subject or faculty name not found",
}


def english(msg):
    for k, v in TRANSLATE.items():
        msg = msg.replace(k, v)
    return msg


ADD = {
    "department": ("add_department", ["name"]),
    "faculty": ("add_faculty", ["name", "dept"]),
    "subject": ("add_subject", ["name", "dept", "is_lab"]),
    "class": ("add_class", ["name", "dept"]),
    "room": ("add_room", ["name", "is_lab"]),
    "slots": ("add_slots", ["days", "periods"]),
    "assign": (
        "assign_subject",
        ["class", "subject", "faculty", "periods"]
    ),
}


# ------------------------------------------
# CONSTRAINT VALIDATION
# ------------------------------------------

def validate_constraints():
    errors = []
    warnings = []

    slot_info = rows("""
        SELECT COUNT(*) total_slots,
               NVL(MAX(day_no), 0) total_days,
               NVL(MAX(period_no), 0) periods_per_day
        FROM time_slot
    """)[0]

    total_slots = slot_info["total_slots"]

    if total_slots == 0:
        errors.append("No time slots have been created.")

    assignments = rows("""
        SELECT cs.class_id, c.class_name,
               c.student_strength,
               cs.subject_id, s.subject_name, s.is_lab,
               cs.faculty_id, f.faculty_name,
               f.max_periods_per_week,
               cs.periods_per_week
        FROM class_subject cs
        JOIN classes c ON c.class_id = cs.class_id
        JOIN subject s ON s.subject_id = cs.subject_id
        JOIN faculty f ON f.faculty_id = cs.faculty_id
        ORDER BY c.class_name, s.subject_name
    """)

    if not assignments:
        errors.append("No subjects have been assigned to sections.")

    # Validate individual assignments and room capacity
    lab_rooms = rows("""
        SELECT COUNT(*) total
        FROM room
        WHERE is_lab = 1
    """)[0]["total"]

    normal_rooms = rows("""
        SELECT COUNT(*) total
        FROM room
        WHERE is_lab = 0
    """)[0]["total"]

    for a in assignments:
        periods = a["periods_per_week"]

        if periods is None or periods <= 0:
            errors.append(
                f"{a['subject_name']} in {a['class_name']} "
                "has invalid weekly periods."
            )
            continue

        if periods > total_slots:
            errors.append(
                f"{a['subject_name']} in {a['class_name']} "
                "requires more periods than available slots."
            )

        if a["is_lab"] == 1:
            if lab_rooms == 0:
                errors.append(
                    f"No lab room available for {a['subject_name']}."
                )

            if periods % 2 != 0:
                errors.append(
                    f"Lab subject {a['subject_name']} requires "
                    "an even number of weekly periods."
                )

        elif normal_rooms == 0:
            errors.append(
                f"No normal classroom available for {a['subject_name']}."
            )

        if a["student_strength"] is None:
            warnings.append(
                f"Student strength missing for {a['class_name']}."
            )

    # Faculty workload validation
    faculty_loads = rows("""
        SELECT f.faculty_id, f.faculty_name,
               f.max_periods_per_week,
               SUM(cs.periods_per_week) total_periods
        FROM faculty f
        JOIN class_subject cs ON cs.faculty_id = f.faculty_id
        GROUP BY f.faculty_id, f.faculty_name,
                 f.max_periods_per_week
    """)

    for f in faculty_loads:
        if f["max_periods_per_week"] is not None:
            if f["total_periods"] > f["max_periods_per_week"]:
                errors.append(
                    f"{f['faculty_name']} has workload "
                    f"{f['total_periods']}, exceeding maximum "
                    f"{f['max_periods_per_week']}."
                )

    # Section workload validation
    section_loads = rows("""
        SELECT c.class_name,
               SUM(cs.periods_per_week) total_periods
        FROM classes c
        JOIN class_subject cs ON cs.class_id = c.class_id
        GROUP BY c.class_name
    """)

    for s in section_loads:
        if s["total_periods"] > total_slots:
            errors.append(
                f"{s['class_name']} requires more periods "
                "than available weekly slots."
            )

    # Room capacity feasibility
    sections = rows("""
        SELECT class_id, class_name, student_strength
        FROM classes
    """)

    rooms = rows("""
        SELECT room_id, room_name, is_lab, capacity
        FROM room
    """)

    for section in sections:
        if section["student_strength"] is None:
            continue

        for a in assignments:
            if a["class_id"] != section["class_id"]:
                continue

            suitable = [
                r for r in rooms
                if r["is_lab"] == a["is_lab"]
                and r["capacity"] is not None
                and r["capacity"] >= section["student_strength"]
            ]

            if not suitable:
                errors.append(
                    f"No suitable-capacity room for "
                    f"{a['subject_name']} in {section['class_name']}."
                )

    # Faculty availability feasibility
    availability = rows("""
        SELECT faculty_id,
               COUNT(*) unavailable_slots
        FROM faculty_availability
        WHERE is_available = 0
        GROUP BY faculty_id
    """)

    unavailable_map = {
        x["faculty_id"]: x["unavailable_slots"]
        for x in availability
    }

    for f in faculty_loads:
        unavailable = unavailable_map.get(f["faculty_id"], 0)
        available_slots = total_slots - unavailable

        if f["total_periods"] > available_slots:
            errors.append(
                f"{f['faculty_name']} has more assigned periods "
                "than their available slots."
            )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings
    }


# ------------------------------------------
# ERROR HANDLING
# ------------------------------------------

@app.errorhandler(oracledb.DatabaseError)
def db_error(e):
    app.logger.exception("ORACLE DATABASE ERROR")
    msg = str(e)
    if "ORA-02292" in msg:
        m = re.search(r"\(([\w.$]+)\)", msg)
        msg = ("Cannot delete: this entry is still used by other records"
               + (f" [{m.group(1)}]" if m else "")
               + ". Remove those first.")
    elif "ORA-00001" in msg:
        msg = "An entry with this name already exists."
    return jsonify(error=english(msg)), 400


@app.errorhandler(Exception)
def general_error(e):
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    app.logger.exception("APPLICATION ERROR")
    return jsonify(
        error="Application error. Check Flask terminal."
    ), 500


# ------------------------------------------
# HOME
# ------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


# ------------------------------------------
# FETCH DATA
# ------------------------------------------

@app.route("/api/data")
def data():
    return jsonify(
        departments=names(
            "SELECT dept_name n FROM department ORDER BY 1"
        ),
        faculty=rows("""
            SELECT faculty_name name, max_periods_per_week max_periods
            FROM faculty ORDER BY faculty_name
        """),
        subjects=rows(
            "SELECT subject_name name, is_lab FROM subject ORDER BY 1"
        ),
        classes=rows("""
            SELECT class_name name, student_strength
            FROM classes ORDER BY class_name
        """),
        rooms=rows("""
            SELECT room_name name, capacity, is_lab
            FROM room ORDER BY room_name
        """),
        slots=rows("""
            SELECT COUNT(*) total,
                   NVL(MAX(day_no),0) days,
                   NVL(MAX(period_no),0) periods
            FROM time_slot
        """)[0],
        assignments=rows("""
            SELECT c.class_name, s.subject_name,
                   f.faculty_name, cs.periods_per_week
            FROM class_subject cs
            JOIN classes c ON c.class_id = cs.class_id
            JOIN subject s ON s.subject_id = cs.subject_id
            JOIN faculty f ON f.faculty_id = cs.faculty_id
            ORDER BY c.class_name, s.subject_name
        """)
    )


# ------------------------------------------
# ADD ACADEMIC DATA
# ------------------------------------------

@app.route("/api/add/<kind>", methods=["POST"])
def add(kind):
    if kind not in ADD:
        return jsonify(error="Unknown form"), 404

    body = request.get_json(silent=True) or {}
    proc, fields = ADD[kind]

    args = [body.get(f) for f in fields]

    with conn() as c:
        cur = c.cursor()
        cur.callproc(proc, args)

        # Save additional faculty constraints
        if kind == "faculty":
            cur.execute("""
                UPDATE faculty
                SET max_periods_per_week = :maximum
                WHERE faculty_name = :name
            """, {
                "maximum": body.get("max_periods", 20),
                "name": body["name"]
            })

        # Save room capacity
        elif kind == "room":
            cur.execute("""
                UPDATE room
                SET capacity = :capacity
                WHERE room_name = :name
            """, {
                "capacity": body.get("capacity", 60),
                "name": body["name"]
            })

        # Save section strength
        elif kind == "class":
            cur.execute("""
                UPDATE classes
                SET student_strength = :strength
                WHERE class_name = :name
            """, {
                "strength": body.get("strength", 60),
                "name": body["name"]
            })

        c.commit()

    return jsonify(ok=True)


# ------------------------------------------
# DELETE / EDIT ENTRIES
# ------------------------------------------

# Timetable rows are removed too, since the old timetable becomes invalid
DELETE_SQL = {
    "department": [
        "DELETE FROM department WHERE dept_name = :name",
    ],
    "faculty": [
        """DELETE FROM timetable WHERE faculty_id IN
           (SELECT faculty_id FROM faculty WHERE faculty_name = :name)""",
        """DELETE FROM class_subject WHERE faculty_id IN
           (SELECT faculty_id FROM faculty WHERE faculty_name = :name)""",
        """DELETE FROM faculty_availability WHERE faculty_id IN
           (SELECT faculty_id FROM faculty WHERE faculty_name = :name)""",
        "DELETE FROM faculty WHERE faculty_name = :name",
    ],
    "subject": [
        """DELETE FROM timetable WHERE subject_id IN
           (SELECT subject_id FROM subject WHERE subject_name = :name)""",
        """DELETE FROM class_subject WHERE subject_id IN
           (SELECT subject_id FROM subject WHERE subject_name = :name)""",
        "DELETE FROM subject WHERE subject_name = :name",
    ],
    "class": [
        """DELETE FROM timetable WHERE class_id IN
           (SELECT class_id FROM classes WHERE class_name = :name)""",
        """DELETE FROM class_subject WHERE class_id IN
           (SELECT class_id FROM classes WHERE class_name = :name)""",
        "DELETE FROM classes WHERE class_name = :name",
    ],
    "room": [
        """DELETE FROM timetable WHERE room_id IN
           (SELECT room_id FROM room WHERE room_name = :name)""",
        "DELETE FROM room WHERE room_name = :name",
    ],
}

EDIT_SQL = {
    "department": (
        "UPDATE department SET dept_name = :name WHERE dept_name = :old", []),
    "faculty": (
        """UPDATE faculty SET faculty_name = :name,
           max_periods_per_week = :max_periods
           WHERE faculty_name = :old""", ["max_periods"]),
    "subject": (
        """UPDATE subject SET subject_name = :name, is_lab = :is_lab
           WHERE subject_name = :old""", ["is_lab"]),
    "class": (
        """UPDATE classes SET class_name = :name,
           student_strength = :student_strength
           WHERE class_name = :old""", ["student_strength"]),
    "room": (
        """UPDATE room SET room_name = :name, capacity = :capacity,
           is_lab = :is_lab WHERE room_name = :old""",
        ["capacity", "is_lab"]),
}


@app.route("/api/delete/<kind>", methods=["DELETE"])
def delete_entry(kind):
    if kind not in DELETE_SQL:
        return jsonify(error="Unknown entry type"), 404

    name = (request.get_json(silent=True) or {}).get("name")
    if not name:
        return jsonify(error="Name is required."), 400

    with conn() as c:
        cur = c.cursor()
        for sql in DELETE_SQL[kind]:
            cur.execute(sql, {"name": name})
        if cur.rowcount == 0:          # last statement = main table
            return jsonify(error="Entry not found."), 404
        c.commit()

    return jsonify(ok=True)


@app.route("/api/edit/<kind>", methods=["PUT"])
def edit_entry(kind):
    if kind not in EDIT_SQL:
        return jsonify(error="Unknown entry type"), 404

    body = request.get_json(silent=True) or {}
    old = body.get("old_name")
    new = (body.get("name") or "").strip()

    if not old or not new:
        return jsonify(error="Name is required."), 400

    sql, extra = EDIT_SQL[kind]
    params = {"old": old, "name": new}
    for f in extra:
        params[f] = body.get(f)

    with conn() as c:
        cur = c.cursor()
        cur.execute(sql, params)
        if cur.rowcount == 0:
            return jsonify(error="Entry not found."), 404
        c.commit()

    return jsonify(ok=True)


# ----- Assigned subjects (class_subject) -----

ASSIGN_WHERE = """
    class_id IN (SELECT class_id FROM classes WHERE class_name = :cls)
    AND subject_id IN (SELECT subject_id FROM subject
                       WHERE subject_name = :subject)
"""

CLEAR_TT = """
    DELETE FROM timetable WHERE
    class_id IN (SELECT class_id FROM classes WHERE class_name = :cls)
    AND subject_id IN (SELECT subject_id FROM subject
                       WHERE subject_name = :subject)
"""


@app.route("/api/assignment", methods=["PUT", "DELETE"])
def assignment():
    body = request.get_json(silent=True) or {}
    p = {"cls": body.get("class"), "subject": body.get("subject")}

    if not p["cls"] or not p["subject"]:
        return jsonify(error="Section and subject are required."), 400

    with conn() as c:
        cur = c.cursor()
        cur.execute(CLEAR_TT, p)

        if request.method == "DELETE":
            cur.execute(f"DELETE FROM class_subject WHERE {ASSIGN_WHERE}", p)
        else:
            p["faculty"] = body.get("faculty")
            p["periods"] = body.get("periods")
            cur.execute(f"""
                UPDATE class_subject
                SET faculty_id = (SELECT MIN(faculty_id) FROM faculty
                                  WHERE faculty_name = :faculty),
                    periods_per_week = :periods
                WHERE {ASSIGN_WHERE}
            """, p)

        if cur.rowcount == 0:
            return jsonify(error="Assignment not found."), 404
        c.commit()

    return jsonify(ok=True)


# ------------------------------------------
# FACULTY AVAILABILITY
# ------------------------------------------

@app.route("/api/availability", methods=["POST"])
def set_availability():
    body = request.get_json(silent=True) or {}

    faculty_name = body.get("faculty")
    day_no = body.get("day")
    period_no = body.get("period")
    is_available = body.get("is_available")

    if not faculty_name or day_no is None or period_no is None:
        return jsonify(error="Faculty, day and period are required."), 400

    if is_available not in (0, 1):
        return jsonify(error="Availability must be 0 or 1."), 400

    with conn() as c:
        cur = c.cursor()

        cur.execute("""
            SELECT faculty_id
            FROM faculty
            WHERE faculty_name = :name
        """, {"name": faculty_name})

        faculty = cur.fetchone()

        if not faculty:
            return jsonify(error="Faculty not found."), 404

        cur.execute("""
            SELECT slot_id
            FROM time_slot
            WHERE day_no = :day
              AND period_no = :period
        """, {"day": day_no, "period": period_no})

        slot = cur.fetchone()

        if not slot:
            return jsonify(error="Selected time slot does not exist."), 400

        cur.execute("""
            MERGE INTO faculty_availability fa
            USING (
                SELECT :faculty_id faculty_id,
                       :slot_id slot_id,
                       :available is_available
                FROM dual
            ) src
            ON (
                fa.faculty_id = src.faculty_id
                AND fa.slot_id = src.slot_id
            )
            WHEN MATCHED THEN
                UPDATE SET fa.is_available = src.is_available
            WHEN NOT MATCHED THEN
                INSERT (faculty_id, slot_id, is_available)
                VALUES (
                    src.faculty_id,
                    src.slot_id,
                    src.is_available
                )
        """, {
            "faculty_id": faculty[0],
            "slot_id": slot[0],
            "available": is_available
        })

        c.commit()

    return jsonify(ok=True)


# ------------------------------------------
# GET AVAILABILITY
# ------------------------------------------

@app.route("/api/availability")
def get_availability():
    return jsonify(rows("""
        SELECT f.faculty_name,
               ts.day_no,
               ts.period_no,
               fa.is_available
        FROM faculty_availability fa
        JOIN faculty f ON f.faculty_id = fa.faculty_id
        JOIN time_slot ts ON ts.slot_id = fa.slot_id
        ORDER BY f.faculty_name, ts.day_no, ts.period_no
    """))


# ------------------------------------------
# VALIDATE
# ------------------------------------------

@app.route("/api/validate")
def validate():
    return jsonify(validate_constraints())


# ------------------------------------------
# GENERATE
# ------------------------------------------

LUNCH_AFTER = 3        # lunch sits between period 3 and 4
GREEDY_ATTEMPTS = 40
IMPROVE_TOP = 3
IMPROVE_ITERS = 2500


def build_timetable(fixed_room=False, room_map=None):
    """Constraint-aware scheduler. Hard rules: no class/faculty/room clash,
    faculty availability, room type + capacity, labs = 2 consecutive periods
    (never across lunch). Soft goals: no free periods between classes for
    sections AND faculty (a late start of the day is fine), subjects
    spread over different days.
    Phase 1 builds several greedy timetables, phase 2 improves the best one
    by repeatedly re-placing single sessions.
    fixed_room=True: each section gets a home classroom (room_map picks it
    by section name, otherwise auto) used for all its theory periods."""

    room_map = room_map or {}

    slots = rows("SELECT slot_id, day_no, period_no FROM time_slot")
    slot_id = {(s["day_no"], s["period_no"]): s["slot_id"] for s in slots}
    days = sorted({s["day_no"] for s in slots})
    max_p = max(s["period_no"] for s in slots)

    assigns = rows("""
        SELECT cs.class_id, c.class_name, c.student_strength,
               cs.subject_id, s.subject_name, s.is_lab,
               cs.faculty_id, cs.periods_per_week
        FROM class_subject cs
        JOIN classes c ON c.class_id = cs.class_id
        JOIN subject s ON s.subject_id = cs.subject_id
    """)

    room_list = rows(
        "SELECT room_id, room_name, is_lab, capacity FROM room"
    )

    unavailable = {
        (r["faculty_id"], r["day_no"], r["period_no"])
        for r in rows("""
            SELECT fa.faculty_id, ts.day_no, ts.period_no
            FROM faculty_availability fa
            JOIN time_slot ts ON ts.slot_id = fa.slot_id
            WHERE fa.is_available = 0
        """)
    }

    def make_unit(x):
        need = x["student_strength"] or 0
        return {
            "class_ids": [x["class_id"]],
            "class_names": [x["class_name"]],
            "subject_id": x["subject_id"],
            "subject_name": x["subject_name"],
            "faculty_id": x["faculty_id"],
            "is_lab": x["is_lab"],
            "periods": x["periods_per_week"],
            "home_id": None,
            "rooms": sorted(
                [r for r in room_list
                 if r["is_lab"] == x["is_lab"]
                 and r["capacity"] is not None
                 and r["capacity"] >= need],
                key=lambda r: r["capacity"]
            ),
        }

    units = [make_unit(x) for x in assigns]

    # ---- home classroom per section ----
    home_names = {}
    if fixed_room:
        normal = [r for r in room_list
                  if r["is_lab"] == 0 and r["capacity"] is not None]
        by_name = {r["room_name"]: r for r in normal}
        usage = {r["room_id"]: 0 for r in normal}
        strength, cname = {}, {}
        for x in assigns:
            strength[x["class_id"]] = x["student_strength"] or 0
            cname[x["class_id"]] = x["class_name"]

        home = {}
        for cid in sorted(strength, key=lambda c: -strength[c]):
            wanted = by_name.get(room_map.get(cname[cid]))
            if wanted is not None and wanted["capacity"] >= strength[cid]:
                r = wanted
            else:
                fits = [x for x in normal if x["capacity"] >= strength[cid]]
                if not fits:
                    continue
                r = min(fits, key=lambda x: (usage[x["room_id"]], x["capacity"]))
            usage[r["room_id"]] += 1
            home[cid] = r
            home_names[cname[cid]] = r["room_name"]

        for u in units:
            cid = u["class_ids"][0]
            if u["is_lab"] == 0 and cid in home:
                h = home[cid]
                u["home_id"] = h["room_id"]
                # home room first, other rooms only as a fallback
                u["rooms"] = [h] + [r for r in u["rooms"]
                                    if r["room_id"] != h["room_id"]]

    fac_load, sessions = {}, []
    for u in units:
        fac_load[u["faculty_id"]] = fac_load.get(u["faculty_id"], 0) + u["periods"]
        length = 2 if u["is_lab"] == 1 else 1
        for _ in range(u["periods"] // length):
            sessions.append((u, length))

    # ---------------- schedule state + cost ----------------

    class State:
        def __init__(self):
            self.cocc = {}      # (class_id, day)   -> set(periods)
            self.focc = {}      # (faculty_id, day) -> set(periods)
            self.rbusy = set()  # (room_id, day, period)
            self.sd = {}        # (class_id, subject_id, day) -> count
            self.pl = []        # placements (unit, day, cells, room)

    def gap_c(occ):   # free periods BETWEEN a section's first and last class
        return (max(occ) - min(occ) + 1 - len(occ)) if occ else 0

    def gap_f(occ):   # free periods between a faculty's first and last class
        return (max(occ) - min(occ) + 1 - len(occ)) if occ else 0

    def cost_day(st, cids, fid, d, add=()):
        add = set(add)
        t = sum(gap_c(st.cocc.get((cid, d), set()) | add) for cid in cids)
        return t + gap_f(st.focc.get((fid, d), set()) | add)

    def gaps_of(st):
        cg = sum(gap_c(o) for o in st.cocc.values())
        fg = sum(gap_f(o) for o in st.focc.values())
        return cg, fg

    def candidates(st, u, length, rng):
        cids, fid = u["class_ids"], u["faculty_id"]
        for d in days:
            before = cost_day(st, cids, fid, d)
            load = sum(len(st.cocc.get((cid, d), ())) for cid in cids)
            repeat = 1 if any(st.sd.get((cid, u["subject_id"], d), 0)
                              for cid in cids) else 0
            fo = st.focc.get((fid, d), ())
            for p in range(1, max_p - length + 2):
                if length == 2 and p == LUNCH_AFTER:
                    continue
                cells = [(d, p + i) for i in range(length)]
                if any(c not in slot_id for c in cells):
                    continue
                ps = {c[1] for c in cells}
                if any(q in st.cocc.get((cid, d), ())
                       for cid in cids for q in ps):
                    continue
                if any(q in fo or (fid, d, q) in unavailable for q in ps):
                    continue
                room = next(
                    (r for r in u["rooms"]
                     if all((r["room_id"], d, q) not in st.rbusy for q in ps)),
                    None
                )
                if room is None:
                    continue
                delta = cost_day(st, cids, fid, d, ps) - before
                yield ((delta, repeat, load, p, rng.random()), d, cells, room)

    def add(st, u, d, cells, room):
        ps = [c[1] for c in cells]
        for cid in u["class_ids"]:
            st.cocc.setdefault((cid, d), set()).update(ps)
            k = (cid, u["subject_id"], d)
            st.sd[k] = st.sd.get(k, 0) + 1
        st.focc.setdefault((u["faculty_id"], d), set()).update(ps)
        for q in ps:
            st.rbusy.add((room["room_id"], d, q))
        pl = (u, d, cells, room)
        st.pl.append(pl)

    def remove(st, pl):
        u, d, cells, room = pl
        ps = [c[1] for c in cells]
        for cid in u["class_ids"]:
            st.cocc[(cid, d)].difference_update(ps)
            st.sd[(cid, u["subject_id"], d)] -= 1
        st.focc[(u["faculty_id"], d)].difference_update(ps)
        for q in ps:
            st.rbusy.discard((room["room_id"], d, q))
        st.pl.remove(pl)

    def best_slot(st, u, length, rng):
        return min(candidates(st, u, length, rng),
                   default=None, key=lambda x: x[0])

    # ---------------- phase 1: greedy construction ----------------

    def construct(rng):
        st, unplaced = State(), []
        order = sorted(
            sessions,
            key=lambda x: (-x[1], -fac_load[x[0]["faculty_id"]], rng.random())
        )
        for u, length in order:
            best = best_slot(st, u, length, rng)
            if best is None:
                unplaced.append((u, length))
                continue
            _, d, cells, room = best
            add(st, u, d, cells, room)
        return st, unplaced

    # ---------------- phase 2: local improvement ----------------

    def improve(st, unplaced, rng, iters):
        for _ in range(iters):
            for item in list(unplaced):
                best = best_slot(st, item[0], item[1], rng)
                if best:
                    _, d, cells, room = best
                    add(st, item[0], d, cells, room)
                    unplaced.remove(item)
            if not st.pl:
                break
            pl = rng.choice(st.pl)
            u, _, cells0, _ = pl
            remove(st, pl)
            best = best_slot(st, u, len(cells0), rng)   # old spot is a candidate
            _, d, cells, room = best
            add(st, u, d, cells, room)

    def score(st, unplaced):
        return (len(unplaced), sum(gaps_of(st)))

    pool = []
    for i in range(GREEDY_ATTEMPTS):
        st, unp = construct(random.Random(i))
        pool.append((score(st, unp), i, st, unp))
        if score(st, unp) == (0, 0):
            break
    pool.sort(key=lambda x: x[0])

    best = None
    for sc, i, st, unp in pool[:IMPROVE_TOP]:
        improve(st, unp, random.Random(1000 + i), IMPROVE_ITERS)
        sc = score(st, unp)
        if best is None or sc < best[0]:
            best = (sc, st, unp)
        if sc == (0, 0):
            break

    _, st, unp = best

    placed = []
    off_home = 0
    for u, d, cells, room in st.pl:
        if u["home_id"] and room["room_id"] != u["home_id"]:
            off_home += len(cells)
        for cid in u["class_ids"]:
            for c in cells:
                placed.append((
                    cid, u["subject_id"], u["faculty_id"],
                    room["room_id"], slot_id[c]
                ))

    cg, fg = gaps_of(st)
    info = {"homes": home_names, "off_home": off_home,
            "class_gaps": cg, "faculty_gaps": fg}
    return placed, [u for u, _ in unp], info


@app.route("/api/generate", methods=["POST"])
def generate():
    body = request.get_json(silent=True) or {}
    fixed_room = bool(body.get("fixed_room"))
    room_map = body.get("room_map") or {}

    validation = validate_constraints()

    if not validation["valid"]:
        return jsonify(
            ok=False,
            message="Constraint validation failed: "
                    + " | ".join(validation["errors"][:3]),
            validation=validation
        ), 400

    placed, unplaced, info = build_timetable(fixed_room, room_map)

    if unplaced:
        sample = ", ".join(
            f"{u['subject_name']} ({'+'.join(u['class_names'])})"
            for u in unplaced[:5]
        )
        hint = (" Fixed rooms may be too strict: add more rooms or "
                "untick the fixed-room option." if fixed_room else "")
        return jsonify(
            ok=False,
            message=(
                f"Could not place {len(unplaced)} session(s): {sample}. "
                "Try more rooms, more faculty availability, or fewer periods."
                + hint
            )
        ), 409

    with conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM timetable")
        cur.executemany("""
            INSERT INTO timetable
                (class_id, subject_id, faculty_id, room_id, slot_id)
            VALUES (:1, :2, :3, :4, :5)
        """, placed)
        c.commit()

    msg = (f"Timetable generated successfully! ({len(placed)} periods | "
           f"free-period gaps: students {info['class_gaps']}, "
           f"faculty {info['faculty_gaps']})")

    if fixed_room and info["homes"]:
        msg += " | Rooms: " + ", ".join(
            f"{s} → {r}" for s, r in sorted(info["homes"].items()))
        if info["off_home"]:
            msg += f" ({info['off_home']} period(s) in another room)"

    return jsonify(ok=True, message=msg)


# ------------------------------------------
# VIEW TIMETABLE
# ------------------------------------------

@app.route("/api/timetable")
def timetable():
    return jsonify(rows=rows("""
        SELECT c.class_name, ts.day_no, ts.period_no,
               s.subject_name, f.faculty_name, r.room_name
        FROM timetable t
        JOIN classes c ON c.class_id = t.class_id
        JOIN subject s ON s.subject_id = t.subject_id
        JOIN faculty f ON f.faculty_id = t.faculty_id
        JOIN room r ON r.room_id = t.room_id
        JOIN time_slot ts ON ts.slot_id = t.slot_id
        ORDER BY c.class_name, ts.day_no, ts.period_no
    """))


if __name__ == "__main__":
    print("Starting UniOpt-X...")
    print("Oracle User:", USER)
    print("Oracle DSN:", DSN)

    app.run(host="127.0.0.1", port=5001, debug=True)